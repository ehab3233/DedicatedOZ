"""IPMI over LAN driver, via ipmitool.

On the C220 M4 this is the most dependable way to drive power: IPMI is a tiny,
old, well-trodden protocol, and ipmitool is on every Linux box. It cannot do
virtual media or rich inventory, so it is used for power, boot device and
serial console, with Redfish doing the rest.

Everything here was written against real ipmitool output, captured from
OpenIPMI's `ipmi_sim` (see deploy/sim/). Two behaviours worth knowing:

* ipmitool 1.8.19 probes for the best cipher suite before every command. A BMC
  that does not answer that probe costs ten seconds per call. Cipher suite 3
  is pinned by default (settings.ipmi_cipher_suite) -- every CIMC supports it.
* The password is passed in the environment (`-E`), never on the command
  line, where any local user could read it from the process table.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from app.config import settings
from app.drivers.base import (
    BMCDriver,
    BMCError,
    HardwareInventory,
    HealthStatus,
    LogSink,
    PowerStatus,
    null_sink,
)
from app.enums import PowerAction
from app.secrets import BMCCredential

#: Generic verb -> `ipmitool chassis power` argument. IPMI has no graceful
#: restart; asking for one is an error rather than a silent hard reset.
POWER_COMMANDS: dict[PowerAction, str] = {
    PowerAction.ON: "on",
    PowerAction.OFF: "soft",
    PowerAction.FORCE_OFF: "off",
    PowerAction.FORCE_RESTART: "reset",
}

#: Generic boot target -> `ipmitool chassis bootdev` argument.
BOOT_DEVICES: dict[str, str] = {
    "pxe": "pxe",
    "hdd": "disk",
    "cd": "cdrom",
    "bios": "bios",
}

#: What `chassis bootparam get 5` prints for each device, for read-back.
BOOT_SELECTOR_TEXT: dict[str, str] = {
    "pxe": "Force PXE",
    "hdd": "Force Boot from default Hard-Drive",
    "cd": "Force Boot from CD/DVD",
    "bios": "Force Boot into BIOS Setup",
}

#: stderr lines that are noise, not failure.
_NOISE = (
    "Unable to Get Channel Cipher Suites",
    "Get HPM.x Capabilities request failed",
)

#: ipmitool failure text -> an explanation an operator can act on.
#: Specific messages first: ipmitool often prints the generic session failure
#: as well, and the specific one is the useful one.
_DIAGNOSES: list[tuple[str, str]] = [
    ("RAKP 2 HMAC is invalid", "the BMC rejected the IPMI password"),
    (
        "RAKP 2 message indicates an error : unauthorized name",
        "the BMC does not know this IPMI username",
    ),
    ("Invalid user name", "the BMC does not know this IPMI username"),
    ("Insufficient privilege level", "this BMC user is not an IPMI administrator"),
    (
        "SOL payload already active on another session",
        "someone else has the serial console open",
    ),
    ("SOL payload disabled", "Serial-over-LAN is disabled on this BMC; run Prepare BMC"),
    (
        # ipmitool prints exactly this for a wrong password, an unknown user,
        # IPMI switched off, and a host that is not there. It cannot tell them
        # apart, so neither can we; say all of it.
        "Unable to establish IPMI v2 / RMCP+ session",
        "IPMI session failed. Either the username or password is wrong, IPMI "
        "over LAN is disabled on the CIMC (Admin > Communication Services > "
        "IPMI over LAN, or run Prepare BMC), or UDP 623 is not reachable from "
        "the management server.",
    ),
]


def diagnose(text: str) -> str | None:
    for needle, explanation in _DIAGNOSES:
        if needle.lower() in text.lower():
            return explanation
    return None


def ipmitool_path() -> str:
    path = shutil.which(settings.ipmitool_path) or settings.ipmitool_path
    return path


class IpmiDriver(BMCDriver):
    def __init__(
        self,
        host: str,
        credential: BMCCredential,
        *,
        log: LogSink = null_sink,
        port: int | None = None,
        cipher_suite: str | None = None,
        timeout: int | None = None,
        retransmit: tuple[int, int] | None = None,
    ) -> None:
        self.host = host
        self.port = port or settings.ipmi_port
        self._cred = credential
        self._log = log
        self._cipher = settings.ipmi_cipher_suite if cipher_suite is None else cipher_suite
        self._timeout = timeout or settings.ipmi_timeout_seconds
        #: (seconds between retransmits, retries) -> ipmitool -N / -R.
        self._retransmit = retransmit

    # -- plumbing ----------------------------------------------------------

    def base_command(self) -> list[str]:
        """ipmitool invocation up to (not including) the subcommand."""
        cmd = [
            ipmitool_path(),
            "-I", "lanplus",
            "-H", self.host,
            "-p", str(self.port),
            "-U", self._cred.username,
            "-E",
        ]
        if self._cipher:
            cmd += ["-C", str(self._cipher)]
        if self._retransmit:
            cmd += ["-N", str(self._retransmit[0]), "-R", str(self._retransmit[1])]
        return cmd

    def environment(self) -> dict[str, str]:
        """Environment carrying the password. Both names, for both ipmitool generations."""
        return {
            "PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"),
            "IPMI_PASSWORD": self._cred.password,
            "IPMITOOL_PASSWORD": self._cred.password,
        }

    def log(self, message: str, *, level: str = "info", request=None, response=None) -> None:  # noqa: ANN001
        self._log(message, level=level, request=request, response=response)

    def run(self, *args: str, timeout: int | None = None) -> str:
        """Run one ipmitool subcommand; return stdout or raise BMCError."""
        cmd = [*self.base_command(), *args]
        request = {"protocol": "ipmi", "host": self.host, "port": self.port, "command": list(args)}
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv, no shell
                cmd,
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=timeout or self._timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise BMCError(
                f"ipmitool not found at {cmd[0]!r}; install it (apt install ipmitool)",
                request=request,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            self.log(f"ipmitool {' '.join(args)} timed out", level="error", request=request)
            raise BMCError(
                f"ipmitool {' '.join(args)} timed out after {timeout or self._timeout}s; "
                "the BMC is not answering IPMI",
                request=request,
                retryable=True,
            ) from exc

        stderr = "\n".join(
            line for line in proc.stderr.splitlines() if not any(n in line for n in _NOISE)
        ).strip()
        response = {
            "rc": proc.returncode,
            "stdout": proc.stdout.strip()[:4000],
            "stderr": stderr[:4000],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if proc.returncode != 0:
            detail = stderr or proc.stdout.strip() or f"exit status {proc.returncode}"
            hint = diagnose(detail)
            self.log(
                f"ipmitool {' '.join(args)} failed: {detail}",
                level="error",
                request=request,
                response=response,
            )
            raise BMCError(
                f"ipmitool {' '.join(args)}: {hint or detail}",
                request=request,
                response=response,
            )
        self.log(f"ipmitool {' '.join(args)} -> ok", request=request, response=response)
        return proc.stdout

    # -- power -------------------------------------------------------------

    def power_status(self) -> PowerStatus:
        out = self.run("chassis", "power", "status")
        match = re.search(r"Chassis Power is (on|off)", out, re.IGNORECASE)
        state = match.group(1).lower() if match else "unknown"
        return PowerStatus(state=state, raw={"ipmi": out.strip()})

    def power(self, action: PowerAction) -> None:
        command = POWER_COMMANDS.get(action)
        if command is None:
            raise BMCError(f"{action.value} is not available over IPMI")
        self.run("chassis", "power", command)

    # -- boot --------------------------------------------------------------

    def set_boot_once(self, target: str) -> None:
        if target not in BOOT_DEVICES:
            raise ValueError(f"unknown boot target {target!r}")
        args = ["chassis", "bootdev", BOOT_DEVICES[target]]
        if settings.ipmi_boot_efi:
            args.append("options=efiboot")
        self.run(*args)

        # Read back, as with Redfish. The IPMI spec lets a BMC drop boot flags
        # sixty seconds after they are set if no restart follows, so this is
        # also a check that the flags are still there when we look.
        out = self.run("chassis", "bootparam", "get", "5")
        want = BOOT_SELECTOR_TEXT[target]
        if want.lower() not in out.lower():
            raise BMCError(
                f"boot device did not take effect: wanted {want!r}",
                response={"bootparam": out.strip()[:2000]},
            )

    def clear_boot_override(self) -> None:
        self.run("chassis", "bootdev", "none")

    # -- inventory and health ---------------------------------------------

    def inventory(self) -> HardwareInventory:
        """Best effort. IPMI knows the BMC and the FRU, not the host's NICs."""
        inv = HardwareInventory()
        mc = self.run("mc", "info")
        fields = _colon_fields(mc)
        inv.bmc_firmware = fields.get("Firmware Revision")
        try:
            fru = _colon_fields(self.run("fru", "print", "0"))
            inv.manufacturer = fru.get("Product Manufacturer") or fru.get("Board Mfg")
            inv.model = fru.get("Product Name") or fru.get("Board Product")
            inv.serial = fru.get("Product Serial") or fru.get("Board Serial")
        except BMCError:
            self.log("FRU 0 not readable over IPMI", level="warning")
        inv.raw = {"mc": fields}
        return inv

    def health(self) -> HealthStatus:
        """Sensor states from the SDR. `cr`/`nr` critical, `nc` warning."""
        out = self.run("sdr", "elist")
        subsystems: dict[str, dict] = {}
        rank = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}
        worst = "ok"
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            name, status = parts[0], parts[2].lower()
            mapped = (
                "critical" if status in {"cr", "nr", "lcr", "ucr", "lnr", "unr"}
                else "warning" if status in {"nc", "lnc", "unc"}
                else "ok" if status == "ok"
                else "unknown"
            )
            if mapped == "unknown":
                continue  # "ns" = no reading, which is normal for absent sensors
            subsystems[name] = {"status": mapped, "reading": parts[4] if len(parts) > 4 else None}
            if rank[mapped] > rank[worst]:
                worst = mapped
        if not subsystems:
            worst = "unknown"
        return HealthStatus(status=worst, subsystems={"sensors": {
            "status": worst,
            "detail": subsystems,
        }} if subsystems else {}, raw={"sdr_lines": len(out.splitlines())})

    # -- serial over LAN ---------------------------------------------------

    def sol_deactivate(self) -> None:
        """Release the BMC's single SOL slot. Harmless if nothing holds it."""
        try:
            self.run("sol", "deactivate", timeout=10)
        except BMCError as exc:
            # "SOL payload already de-activated" is the normal case.
            if "de-activated" not in str(exc).lower() and "not active" not in str(exc).lower():
                self.log(f"sol deactivate: {exc}", level="warning")

    def sol_info(self) -> dict[str, str]:
        return _colon_fields(self.run("sol", "info", "1"))

    def sol_enable(self, bitrate: str = "115.2") -> dict[str, str]:
        """Enable SOL at `bitrate` kbps. Needs IPMI over LAN already working."""
        self.run("sol", "set", "enabled", "true", "1")
        for parameter in ("non-volatile-bit-rate", "volatile-bit-rate"):
            try:
                self.run("sol", "set", parameter, bitrate, "1")
            except BMCError as exc:
                self.log(f"could not set {parameter}: {exc}", level="warning")
        return self.sol_info()

    # -- unsupported -------------------------------------------------------

    def insert_virtual_media(self, image_url: str) -> None:
        raise BMCError("virtual media is not available over IPMI; use Redfish")

    def eject_virtual_media(self) -> None:
        raise BMCError("virtual media is not available over IPMI; use Redfish")


def _colon_fields(text: str) -> dict[str, str]:
    """Parse ipmitool's `Key   : value` blocks."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and key not in out:
            out[key] = value
    return out
