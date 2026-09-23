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
import pty
import re
import select
import shutil
import signal
import subprocess
import time
from datetime import datetime

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

POWER_RESTORE_POLICIES = ("always-on", "always-off", "previous")

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

    # -- sensors and event log ---------------------------------------------

    def sensors(self) -> list[dict]:
        """Every sensor in the SDR with its current reading (`sdr elist`).

        One IPMI round trip per sensor inside ipmitool, so a fully populated
        M4 takes a second or two. Cached briefly by the API for the panel.
        """
        return parse_sdr_elist(self.run("sdr", "elist"))

    def power_reading(self) -> dict | None:
        """DCMI power draw in watts, or None where the BMC has no DCMI."""
        try:
            out = self.run("dcmi", "power", "reading")
        except BMCError:
            return None
        fields = _colon_fields(out)
        current = _int_prefix(fields.get("Instantaneous power reading"))
        if current is None:
            return None
        return {
            "watts": current,
            "minimum": _int_prefix(fields.get("Minimum during sampling period")),
            "maximum": _int_prefix(fields.get("Maximum during sampling period")),
            "average": _int_prefix(fields.get("Average power reading over sample period")),
        }

    def sel_info(self) -> dict:
        fields = _colon_fields(self.run("sel", "info"))
        return {
            "entries": _int_prefix(fields.get("Entries")) or 0,
            "free_bytes": _int_prefix(fields.get("Free Space")),
            "percent_used": _int_prefix(fields.get("Percent Used")),
            "last_add": fields.get("Last Add Time"),
            "last_clear": fields.get("Last Del Time"),
            "overflow": fields.get("Overflow", "").lower() == "true",
        }

    def sel_entries(self) -> list[dict]:
        """The System Event Log, newest last, with sensor names resolved."""
        return parse_sel_elist(self.run("sel", "elist"))

    def sel_clear(self) -> None:
        self.run("sel", "clear")

    # -- chassis -----------------------------------------------------------

    def chassis_status(self) -> dict:
        """Power, faults and the power-restore policy, as `chassis status` reports them."""
        fields = _colon_fields(self.run("chassis", "status"))
        return {_snake(key): value for key, value in fields.items()}

    def identify(self, seconds: int = 15, *, force: bool = False) -> None:
        """Blink the chassis locator LED. `seconds=0` turns it off; `force` leaves it on."""
        if force:
            self.run("chassis", "identify", "force")
        else:
            self.run("chassis", "identify", str(max(0, min(int(seconds), 255))))

    def set_power_restore_policy(self, policy: str) -> None:
        """What the server does when mains power returns: always-on, always-off, previous."""
        if policy not in POWER_RESTORE_POLICIES:
            raise ValueError(f"policy must be one of {', '.join(POWER_RESTORE_POLICIES)}")
        self.run("chassis", "policy", policy)

    # -- the BMC itself ----------------------------------------------------

    def mc_info(self) -> dict:
        fields = _colon_fields(self.run("mc", "info"))
        return {
            "firmware": fields.get("Firmware Revision"),
            "ipmi_version": fields.get("IPMI Version"),
            "manufacturer": fields.get("Manufacturer Name"),
            "product": fields.get("Product Name"),
            "device_id": fields.get("Device ID"),
            "available": fields.get("Device Available", "").lower() == "yes",
        }

    def lan_config(self, channel: int = 1) -> dict:
        """The BMC's own network settings (`lan print`)."""
        fields = _colon_fields(self.run("lan", "print", str(channel)))
        return {
            "channel": channel,
            "ip_address": fields.get("IP Address"),
            "subnet_mask": fields.get("Subnet Mask"),
            "gateway": fields.get("Default Gateway IP"),
            "mac_address": fields.get("MAC Address"),
            "source": fields.get("IP Address Source"),
            "vlan": fields.get("802.1q VLAN ID"),
        }

    def bmc_reset(self, kind: str = "cold") -> None:
        """Reboot the BMC. The host keeps running; IPMI and the web UI drop for a minute."""
        if kind not in ("cold", "warm"):
            raise ValueError("kind must be cold or warm")
        self.run("mc", "reset", kind, timeout=15)

    def users(self, channel: int = 1) -> list[dict]:
        return parse_user_list(self.run("user", "list", str(channel)))

    def user_id_for(self, username: str) -> int | None:
        for user in self.users():
            if user["name"] == username:
                return user["id"]
        return None

    def set_user_password(self, user_id: int, password: str) -> None:
        """Change a BMC user's password.

        Never on the command line, where the process table would show it:
        ipmitool asks for it twice on its terminal, so it is typed into a pty.
        The prompt path sets a 16-byte password, the length every BMC accepts.
        """
        if not 1 <= len(password) <= 16:
            raise ValueError("IPMI passwords are 1 to 16 characters")
        cmd = [*self.base_command(), "user", "set", "password", str(user_id)]
        request = {
            "protocol": "ipmi", "host": self.host, "port": self.port,
            "command": ["user", "set", "password", str(user_id), "***"],
        }
        output, rc = _run_on_pty(
            cmd, self.environment(), answer=password, prompt="assword", answers=2,
            timeout=self._timeout,
        )
        output = output.replace(password, "***")
        response = {"rc": rc, "output": output.strip()[:2000]}
        if rc != 0 or "successful" not in output.lower():
            lines = [ln for ln in output.strip().splitlines() if "assword for user" not in ln]
            detail = lines[-1].strip() if lines else f"exit status {rc}"
            self.log(
                f"ipmitool user set password {user_id} failed: {detail}",
                level="error", request=request, response=response,
            )
            raise BMCError(
                f"set user password: {diagnose(detail) or detail}",
                request=request, response=response,
            )
        self.log(f"ipmitool user set password {user_id} -> ok", request=request,
                 response=response)

    # -- unsupported -------------------------------------------------------

    def insert_virtual_media(self, image_url: str) -> None:
        raise BMCError("virtual media is not available over IPMI; use Redfish")

    def eject_virtual_media(self) -> None:
        raise BMCError("virtual media is not available over IPMI; use Redfish")

    def virtual_media(self) -> list[dict]:
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


def _snake(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _int_prefix(value: str | None) -> int | None:
    match = re.search(r"-?\d+", value or "")
    return int(match.group()) if match else None


#: ipmitool's unit spellings -> what the panel shows.
_UNITS = {
    "degrees c": "°C",
    "degrees f": "°F",
    "volts": "V",
    "watts": "W",
    "amps": "A",
    "rpm": "RPM",
    "percent": "%",
    "unspecified": "",
}
_KIND_BY_UNIT = {"°C": "temperature", "°F": "temperature", "RPM": "fan", "V": "voltage",
                 "W": "power", "A": "current"}
#: `sdr elist` status column -> the platform's three-level health.
_SDR_STATUS = {
    "ok": "ok",
    "nc": "warning", "lnc": "warning", "unc": "warning",
    "cr": "critical", "lcr": "critical", "ucr": "critical",
    "nr": "critical", "lnr": "critical", "unr": "critical",
    "ns": "no_reading",
}


def _split_reading(reading: str) -> tuple[float | None, str | None]:
    """"47 degrees C" -> (47.0, "°C"); "Presence detected" / "0x0180" -> (None, None)."""
    text = reading.strip()
    if text.lower().startswith("0x"):
        return None, None  # a discrete sensor's state bits, not a quantity
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*(.*)$", text)
    if not match:
        return None, None
    value = float(match.group(1))
    unit = match.group(2).strip()
    return value, _UNITS.get(unit.lower(), unit)


def _guess_kind(name: str) -> str:
    lowered = name.lower()
    if "temp" in lowered:
        return "temperature"
    if "fan" in lowered:
        return "fan"
    if "psu" in lowered or "power" in lowered or "pwr" in lowered:
        return "power"
    if "volt" in lowered or re.match(r"^p?\d+v", lowered) or "vbat" in lowered:
        return "voltage"
    return "discrete"


def parse_sdr_elist(text: str) -> list[dict]:
    """Parse `ipmitool sdr elist`: `Name | 30h | ok | 3.1 | 47 degrees C`."""
    sensors: list[dict] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        name, ident, status, entity, reading = parts[:5]
        match = re.match(r"^([0-9a-fA-F]+)h$", ident)
        value, unit = _split_reading(reading)
        sensors.append(
            {
                "name": name,
                "number": int(match.group(1), 16) if match else None,
                "status": _SDR_STATUS.get(status.lower(), "unknown"),
                "raw_status": status,
                "entity": entity,
                "reading": reading,
                "value": value,
                "unit": unit,
                "kind": _KIND_BY_UNIT.get(unit or "") or _guess_kind(name),
            }
        )
    return sensors


def _sel_timestamp(date: str, clock: str) -> str | None:
    try:
        return datetime.strptime(f"{date} {clock}", "%m/%d/%Y %H:%M:%S").isoformat()
    except ValueError:
        return None  # "Pre-Init" on a BMC whose clock is not set


def _sel_severity(event: str, direction: str) -> str:
    if direction.lower().startswith("deasserted"):
        return "info"
    lowered = event.lower()
    if "non-critical" in lowered or "non-recoverable" not in lowered and any(
        word in lowered for word in ("warning", "predictive", "degraded")
    ):
        return "warning"
    if any(word in lowered for word in ("critical", "non-recoverable", "fail", "fault", "error")):
        return "critical"
    return "info"


def parse_sel_elist(text: str) -> list[dict]:
    """Parse `ipmitool sel elist` rows.

    `   1 | 09/23/2026 | 14:03:11 | Fan FAN1 Tach | Lower Critical going low | Asserted | ...`
    """
    entries: list[dict] = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue  # "SEL has no entries", or a stray line
        ident, date, clock, sensor, event = parts[:5]
        try:
            record_id = int(ident, 16)
        except ValueError:
            continue
        direction = parts[5] if len(parts) > 5 else ""
        entries.append(
            {
                "id": record_id,
                "timestamp": _sel_timestamp(date, clock),
                "raw_time": f"{date} {clock}".strip(),
                "sensor": sensor,
                "event": event,
                "direction": direction,
                "detail": " | ".join(parts[6:]) if len(parts) > 6 else None,
                "severity": _sel_severity(event, direction),
            }
        )
    return entries


_USER_LINE = re.compile(
    r"^\s*(\d+)\s+(.*?)\s+(true|false)\s+(true|false)\s+(true|false)\s+(.+?)\s*$"
)


def parse_user_list(text: str) -> list[dict]:
    """Parse `ipmitool user list N`. Nameless slots are kept: they show what is free."""
    users: list[dict] = []
    for line in text.splitlines():
        match = _USER_LINE.match(line)
        if not match:
            continue
        user_id, name, callin, link_auth, ipmi_msg, privilege = match.groups()
        users.append(
            {
                "id": int(user_id),
                "name": name.strip(),
                "callin": callin == "true",
                "link_auth": link_auth == "true",
                "ipmi_messaging": ipmi_msg == "true",
                "privilege": privilege.strip(),
            }
        )
    return users


def _run_on_pty(
    cmd: list[str],
    env: dict[str, str],
    *,
    answer: str,
    prompt: str,
    answers: int,
    timeout: int,
) -> tuple[str, int]:
    """Run `cmd` on a pseudo-terminal, typing `answer` at each `prompt`.

    For ipmitool commands that insist on reading a secret from the terminal.
    Returns (everything the command printed, exit status).
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvpe(cmd[0], cmd, env)
        finally:
            os._exit(127)

    buffer = b""
    given = 0
    needle = prompt.encode()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise BMCError(f"{cmd[-4]} {cmd[-3]} timed out after {timeout}s", retryable=True)
            ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break  # EIO: the child has exited and the slave side is closed
            if not chunk:
                break
            buffer += chunk
            while given < answers and buffer.count(needle) > given:
                os.write(fd, (answer + "\n").encode())
                given += 1
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return buffer.decode("utf-8", "replace"), os.waitstatus_to_exitcode(status)
