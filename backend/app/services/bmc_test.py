"""Why can the panel not talk to this BMC?

One click runs the three paths the platform uses -- HTTPS/Redfish, the CIMC
XML API and IPMI over LAN -- and reports what each one actually said, raw
tool output included, instead of the panel's one-line guess. IPMI is tried
with the configured cipher suite first and then the others a CIMC can
speak, so a firmware that dropped a suite is found and remembered for that
server.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from app.config import settings
from app.drivers.base import BMCError
from app.drivers.cimc import IPMI_LAN_DN, SOL_DN, CimcXmlApi
from app.drivers.ipmi import IpmiDriver
from app.drivers.redfish import RedfishDriver
from app.models import Server
from app.secrets import BMCCredential

#: Suites a CIMC may accept: 3 (HMAC-SHA1 + AES, the long-time default), 17
#: (HMAC-SHA256 + AES, what newer firmware prefers), then ipmitool's own
#: probe ("" = no -C).
CIPHER_CANDIDATES = ("3", "17", "")
#: IPMI 2.0 limits; a CIMC web password can be longer, and then HTTPS logs in
#: while IPMI never does.
IPMI_MAX_PASSWORD = 20
IPMI_MAX_USERNAME = 16


def effective_cipher(server: Server) -> str:
    """The cipher suite the platform uses for this server: a number, or ""
    to let ipmitool probe."""
    if server.ipmi_cipher_suite is None:
        return settings.ipmi_cipher_suite
    return "" if server.ipmi_cipher_suite == "auto" else server.ipmi_cipher_suite


def _check(name: str, ok: bool, summary: str, *, raw: str = "", hint: str | None = None) -> dict:
    return {"name": name, "ok": ok, "summary": summary, "raw": raw[:4000], "hint": hint}


def _raw(exc: BMCError) -> str:
    response = exc.response if isinstance(exc.response, dict) else {}
    # Only what the tool or BMC itself said; the summary already carries the
    # message, so without a response there is nothing more to show.
    parts = [str(response[k]) for k in ("stderr", "stdout", "body") if response.get(k)]
    return "\n".join(parts)


def _cipher_label(cipher: str) -> str:
    return f"cipher suite {cipher}" if cipher else "ipmitool's own cipher probe"


def ping(host: str, count: int = 5) -> dict | None:
    """`count` ICMP echoes; None when there is no ping binary to run."""
    binary = shutil.which("ping")
    if not binary:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [binary, "-n", "-c", str(count), "-i", "0.3", "-W", "1", host],
            capture_output=True, text=True, timeout=count * 2 + 5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"sent": count, "received": 0, "loss_pct": 100, "avg_ms": None,
                "raw": str(exc)}
    out = proc.stdout + proc.stderr
    sent = received = 0
    stats = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", out)
    if stats:
        sent, received = int(stats.group(1)), int(stats.group(2))
    avg = re.search(r"= [\d.]+/([\d.]+)/", out)
    return {
        "sent": sent or count,
        "received": received,
        "loss_pct": round(100 * (1 - received / (sent or count))),
        "avg_ms": float(avg.group(1)) if avg else None,
        "raw": out.strip(),
    }


def run(server: Server, credential: BMCCredential) -> dict:
    host = str(server.cimc_ip)
    timeout = settings.bmc_status_timeout_seconds
    checks: list[dict] = []

    # -- is it even there -----------------------------------------------------
    echo = ping(host)
    if echo is not None:
        lossy = echo["loss_pct"] > 0
        summary = (
            f"ping: {echo['received']} of {echo['sent']} answered"
            + (f", {echo['loss_pct']}% lost" if lossy else "")
            + (f", {echo['avg_ms']:.1f} ms average" if echo["avg_ms"] is not None else "")
        )
        checks.append(_check(
            "ping", echo["received"] > 0, summary, raw=echo["raw"],
            hint=(
                "Packet loss on the way to the CIMC. IPMI is UDP with no retransmit of its "
                "own, so every lost packet is a failed read; the panel keeps the last good "
                "reading through short gaps, but the link itself needs looking at (duplex "
                "mismatch, a saturated uplink, a flapping port)."
            ) if lossy and echo["received"] > 0 else None,
        ))
    facts: dict = {
        "firmware": None, "ipmi_over_lan": None, "ipmi_priv": None,
        "encryption_key_custom": None, "sol": None,
    }

    # -- the credential's shape ---------------------------------------------
    too_long = (
        len(credential.password) > IPMI_MAX_PASSWORD
        or len(credential.username) > IPMI_MAX_USERNAME
    )
    checks.append(_check(
        "credential", not too_long,
        f"user {credential.username}, password {len(credential.password)} characters"
        + (
            f": longer than IPMI allows ({IPMI_MAX_PASSWORD} for the password, "
            f"{IPMI_MAX_USERNAME} for the user), so IPMI can never log in where HTTPS can"
            if too_long else ": within IPMI's limits"
        ),
        hint=(
            "Rotate IPMI password sets a 16-character one; or set a shorter CIMC "
            "password by hand and update the credential."
        ) if too_long else None,
    ))

    # -- HTTPS / Redfish ----------------------------------------------------
    https_port = server.redfish_port or 443
    redfish = RedfishDriver(
        host=host, credential=credential, port=server.redfish_port,
        system_path=server.redfish_system_path, timeout=timeout, retries=1,
    )
    try:
        state = redfish.power_status().state
        checks.append(_check(
            "redfish", True,
            f"HTTPS {https_port}: Redfish answers with these credentials; power is {state}",
        ))
    except BMCError as exc:
        checks.append(_check("redfish", False, f"HTTPS {https_port}: {exc}", raw=_raw(exc)))

    # -- CIMC XML API: what the CIMC says its IPMI settings are -------------
    try:
        with CimcXmlApi(host, credential, port=server.redfish_port, timeout=timeout) as api:
            facts["firmware"] = api.version
            lan = api.resolve_dn(IPMI_LAN_DN) or {}
            sol = api.resolve_dn(SOL_DN) or {}
        facts["ipmi_over_lan"] = lan.get("adminState")
        facts["ipmi_priv"] = lan.get("priv")
        key = lan.get("key") or ""
        facts["encryption_key_custom"] = bool(key) and key.strip("0") != ""
        if sol:
            facts["sol"] = f"{sol.get('adminState')} at {sol.get('speed')} on {sol.get('comport')}"
        summary = (
            f"CIMC {api.version}: IPMI over LAN {facts['ipmi_over_lan'] or 'unknown'}, "
            f"privilege limit {facts['ipmi_priv'] or 'unknown'}, encryption key "
            f"{'custom' if facts['encryption_key_custom'] else 'default'}"
        )
        if facts["sol"]:
            summary += f"; Serial-over-LAN {facts['sol']}"
        checks.append(_check("xml_api", True, summary))
    except BMCError as exc:
        checks.append(_check("xml_api", False, f"CIMC XML API: {exc}", raw=_raw(exc)))

    # -- IPMI over LAN, configured cipher first ------------------------------
    configured = effective_cipher(server)
    working: str | None = None
    for cipher in [configured, *[c for c in CIPHER_CANDIDATES if c != configured]]:
        driver = IpmiDriver(
            host=host, credential=credential, port=server.ipmi_port, cipher_suite=cipher,
            timeout=timeout, retransmit=(1, 1),
        )
        name = f"ipmi:{cipher or 'auto'}"
        try:
            out = driver.run("chassis", "power", "status").strip()
        except BMCError as exc:
            checks.append(_check(name, False, f"IPMI with {_cipher_label(cipher)}: {exc}",
                                 raw=_raw(exc)))
            continue
        checks.append(_check(
            name, True,
            f"IPMI over LAN on UDP {server.ipmi_port or settings.ipmi_port} with "
            f"{_cipher_label(cipher)}: {out}",
        ))
        working = cipher
        break

    verdict, hint = _verdict(checks, facts, configured, working)
    return {
        "ok": working is not None,
        "checks": checks,
        "facts": facts,
        "configured_cipher": configured or "auto",
        "working_cipher": None if working is None else (working or "auto"),
        "verdict": verdict,
        "hint": hint,
    }


def _verdict(
    checks: list[dict], facts: dict, configured: str, working: str | None
) -> tuple[str, str | None]:
    by = {c["name"]: c for c in checks}
    https_ok = by["redfish"]["ok"] or by["xml_api"]["ok"]
    if working is not None and working == configured:
        return (
            f"IPMI works with {_cipher_label(working)}: power, sensors, the event log and "
            "the consoles all work.",
            None,
        )
    if working is not None:
        return (
            f"IPMI works with {_cipher_label(working)} but not with the configured "
            f"{_cipher_label(configured)}. This server now uses {_cipher_label(working)}.",
            None,
        )
    if not by["credential"]["ok"]:
        return "IPMI cannot log in: the credential is longer than IPMI allows.", (
            by["credential"]["hint"]
        )
    if facts["ipmi_over_lan"] and facts["ipmi_over_lan"] != "enabled":
        return "IPMI over LAN is switched off in the CIMC.", (
            "Run Prepare BMC, or switch it on in the CIMC web UI: Admin > Communication "
            "Services > IPMI over LAN."
        )
    if facts["encryption_key_custom"]:
        return "The CIMC has a custom IPMI encryption key, which the platform does not send.", (
            "Run Prepare BMC: it sets the key back to all zeros (the CIMC's IPMI service "
            "restarts, so the panel loses the server for a few seconds). Or do it in the "
            "CIMC web UI: Admin > Communication Services > IPMI over LAN > Encryption Key."
        )
    if https_ok:
        return (
            "The CIMC answers on HTTPS with these credentials, so the address and password "
            "are right, but no IPMI session opens with any cipher suite.",
            "Either UDP 623 is dropped between the management server and the CIMC (a "
            "firewall or ACL on the route; ipmitool from a host on the CIMC's own VLAN "
            "tells you which), or the CIMC's IPMI service has stopped answering: Reset BMC "
            "restarts the CIMC without touching the host.",
        )
    return "Nothing answers: not HTTPS, not the XML API, not IPMI.", (
        "Check the CIMC address, that the management server can reach it, and the credential."
    )
