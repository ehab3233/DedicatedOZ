"""Generic Redfish driver.

Written against the DMTF schema, not against Cisco. Everything Cisco-specific
is confined to `_cisco_quirks()` and the OEM virtual-media fallback, both of
which are clearly marked so they can be deleted when the M4s are.

Known M4 constraints, all handled below:
  * CIMC < 3.0 has no Redfish at all -- the fleet must be on 4.1(2f).
  * `Boot` override on the M4 accepts only `Once`/`Continuous`/`Disabled`
    and rejects `BootSourceOverrideMode` changes on some builds, so mode is
    only sent when the resource advertises it as settable.
  * VirtualMedia `InsertMedia` action is present on 4.x, but older builds
    expose it only via the OEM path; both are attempted.
  * The BMC serialises everything. Concurrent requests to one CIMC produce
    500s, so a driver instance is single-threaded by construction and callers
    hold one per server per job.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import urllib3

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

# The OOB plane is isolated and the M4's certificate can never be replaced by a
# CA-signed one, so verification is off by default. Suppress only the resulting
# noise, not the setting itself.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#: Generic verb -> Redfish ResetType.
RESET_TYPES: dict[PowerAction, str] = {
    PowerAction.ON: "On",
    PowerAction.OFF: "GracefulShutdown",
    PowerAction.FORCE_OFF: "ForceOff",
    PowerAction.RESTART: "GracefulRestart",
    PowerAction.FORCE_RESTART: "ForceRestart",
}

#: Generic boot target -> Redfish BootSourceOverrideTarget.
BOOT_TARGETS: dict[str, str] = {
    "pxe": "Pxe",
    "hdd": "Hdd",
    "cd": "Cd",
    "bios": "BiosSetup",
}

_REDACTED = "***"


def _redfish_message(body: Any) -> str:
    """The readable reason in a Redfish error body, if there is one.

    `error.@Message.ExtendedInfo[].Message` is where a BMC says what was
    actually wrong; `error.message` is usually only "A general error has
    occurred".
    """
    if not isinstance(body, dict):
        return ""
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    messages: list[str] = []
    for info in error.get("@Message.ExtendedInfo") or []:
        if isinstance(info, dict):
            text = str(info.get("Message") or info.get("MessageId") or "").strip()
            if text and text not in messages:
                messages.append(text)
    if not messages and error.get("message"):
        messages.append(str(error["message"]).strip())
    return "; ".join(messages)[:500]


def _redact(payload: Any) -> Any:
    """Strip anything credential-shaped before it reaches the job log."""
    if isinstance(payload, dict):
        return {
            k: (_REDACTED if k.lower() in {"password", "username", "token", "authorization"}
                else _redact(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_redact(v) for v in payload]
    return payload


class RedfishDriver(BMCDriver):
    def __init__(
        self,
        host: str,
        credential: BMCCredential,
        *,
        log: LogSink = null_sink,
        port: int | None = None,
        system_path: str | None = None,
        verify_tls: bool | None = None,
        timeout: int | None = None,
        retries: int | None = None,
    ) -> None:
        self.host = host
        self.base_url = f"https://{host}:{port}" if port else f"https://{host}"
        self._retries = retries
        self._cred = credential
        self._log = log
        self._verify = settings.redfish_verify_tls if verify_tls is None else verify_tls
        self._timeout = timeout or settings.redfish_timeout_seconds
        self._session = requests.Session()
        # BMC traffic must never pick up the environment: requests lets
        # REQUESTS_CA_BUNDLE silently override verify=False (so every call to
        # the CIMC's self-signed certificate fails), and would send BMC traffic
        # through any HTTPS_PROXY it finds. Found by running against a real
        # Redfish service from a host with a proxy configured.
        self._session.trust_env = False
        self._session.verify = self._verify
        self._session.headers.update({"Accept": "application/json", "OData-Version": "4.0"})
        self._system_path = system_path
        self._manager_path: str | None = None
        #: Redfish session resource, deleted on close. None means basic auth.
        self._auth_session_path: str | None = None
        self._authenticated = False

    # -- authentication ----------------------------------------------------

    def _ensure_authenticated(self) -> None:
        """Log in once with a Redfish session, falling back to basic auth.

        This is not cosmetic. The CIMC opens an internal session for every
        basic-auth request and caps them (four on 4.x). A health sweep doing a
        dozen GETs per server exhausts that in seconds and every later call
        fails with "max session limit reached" until they time out, which takes
        several minutes. One session per driver instance, deleted on close,
        keeps the BMC well inside its limit.
        """
        if self._authenticated:
            return
        self._authenticated = True  # set first so a failed login does not loop
        url = urljoin(self.base_url, "/redfish/v1/SessionService/Sessions")
        try:
            resp = self._session.post(
                url,
                json={"UserName": self._cred.username, "Password": self._cred.password},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            self._log(f"session login transport error: {exc}; using basic auth", level="warning")
            self._session.auth = (self._cred.username, self._cred.password)
            return

        token = resp.headers.get("X-Auth-Token")
        if resp.status_code in (200, 201) and token:
            self._session.headers["X-Auth-Token"] = token
            self._auth_session_path = resp.headers.get("Location") or (
                (resp.json() or {}).get("@odata.id") if resp.content else None
            )
            self._log("redfish session established")
            return

        if resp.status_code == 401:
            raise BMCError(
                "BMC rejected the credentials",
                request={"method": "POST", "url": url, "body": _REDACTED},
                response={"status": 401},
            )
        # Older builds without a working SessionService: basic auth still works,
        # it just has to be used sparingly.
        self._log(
            f"session login returned {resp.status_code}; falling back to basic auth",
            level="warning",
        )
        self._session.auth = (self._cred.username, self._cred.password)

    def _logout(self) -> None:
        if not self._auth_session_path:
            return
        try:
            self._session.delete(
                urljoin(self.base_url, self._auth_session_path), timeout=self._timeout
            )
        except requests.RequestException:
            pass
        finally:
            self._auth_session_path = None
            self._session.headers.pop("X-Auth-Token", None)

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        expect: tuple[int, ...] = (200, 201, 202, 204),
        retries: int | None = None,
    ) -> dict:
        self._ensure_authenticated()
        url = urljoin(self.base_url, path)
        if retries is None:
            retries = self._retries
        attempts = settings.redfish_max_retries if retries is None else retries
        last_error: Exception | None = None
        req_record = {"method": method, "url": url, "body": _redact(json_body)}

        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.request(
                    method, url, json=json_body, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                self._log(
                    f"{method} {path} transport error (attempt {attempt}/{attempts}): {exc}",
                    level="warning",
                    request=req_record,
                )
                # The CIMC drops connections while it is busy; back off and retry.
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 15))
                    continue
                raise BMCError(
                    f"{method} {path} failed after {attempts} attempts: {exc}",
                    request=req_record,
                    retryable=True,
                ) from exc

            body: Any
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {"_raw": resp.text[:4000]}

            resp_record = {
                "status": resp.status_code,
                "body": _redact(body),
            }

            if resp.status_code in expect:
                self._log(
                    f"{method} {path} -> {resp.status_code}",
                    request=req_record,
                    response=resp_record,
                )
                return body if isinstance(body, dict) else {"_body": body}

            # 401 is terminal: the wrong password will not become right.
            retryable = resp.status_code in {429, 500, 502, 503, 504}
            self._log(
                f"{method} {path} -> {resp.status_code} (attempt {attempt}/{attempts})",
                level="warning" if retryable and attempt < attempts else "error",
                request=req_record,
                response=resp_record,
            )
            if retryable and attempt < attempts:
                time.sleep(min(2 ** attempt, 15))
                continue
            reason = _redfish_message(body)
            raise BMCError(
                f"{method} {path} returned {resp.status_code}" + (f": {reason}" if reason else ""),
                request=req_record,
                response=resp_record,
                retryable=retryable,
            )

        raise BMCError(f"{method} {path} exhausted retries", request=req_record) from last_error

    def _get(self, path: str) -> dict:
        return self._request("GET", path, expect=(200,))

    # -- resource discovery ------------------------------------------------

    @property
    def system_path(self) -> str:
        """Path of the ComputerSystem, discovered once and cached.

        The M4 exposes exactly one, but the collection is walked properly so
        the same driver works on chassis that expose several.
        """
        if self._system_path:
            return self._system_path
        collection = self._get("/redfish/v1/Systems")
        members = collection.get("Members") or []
        if not members:
            raise BMCError("no ComputerSystem members exposed by this BMC")
        self._system_path = members[0]["@odata.id"]
        return self._system_path

    @property
    def manager_path(self) -> str:
        if self._manager_path:
            return self._manager_path
        collection = self._get("/redfish/v1/Managers")
        members = collection.get("Members") or []
        if not members:
            raise BMCError("no Manager members exposed by this BMC")
        self._manager_path = members[0]["@odata.id"]
        return self._manager_path

    def service_root(self) -> dict:
        return self._get("/redfish/v1/")

    def manager_reset(self) -> str:
        """Reboot the BMC itself (Manager.Reset); the host keeps running.
        Returns the reset type used."""
        manager = self._get(self.manager_path)
        action = manager.get("Actions", {}).get("#Manager.Reset", {})
        target = action.get("target") or f"{self.manager_path}/Actions/Manager.Reset"
        allowed = action.get("ResetType@Redfish.AllowableValues") or []
        reset_type = next(
            (t for t in ("ForceRestart", "GracefulRestart") if not allowed or t in allowed),
            allowed[0] if allowed else "ForceRestart",
        )
        self._request(
            "POST", target, json_body={"ResetType": reset_type}, expect=(200, 202, 204)
        )
        return reset_type

    # -- power -------------------------------------------------------------

    def log(self, message: str, *, level: str = "info", request=None, response=None) -> None:  # noqa: ANN001
        self._log(message, level=level, request=request, response=response)

    def power_status(self) -> PowerStatus:
        system = self._get(self.system_path)
        raw_state = (system.get("PowerState") or "").lower()
        state = raw_state if raw_state in {"on", "off"} else "unknown"
        return PowerStatus(state=state, raw={"PowerState": system.get("PowerState")})

    def power(self, action: PowerAction) -> None:
        reset_type = RESET_TYPES[action]
        system = self._get(self.system_path)
        reset_path = (
            system.get("Actions", {})
            .get("#ComputerSystem.Reset", {})
            .get("target")
            or f"{self.system_path}/Actions/ComputerSystem.Reset"
        )

        allowed = (
            system.get("Actions", {})
            .get("#ComputerSystem.Reset", {})
            .get("ResetType@Redfish.AllowableValues")
        )
        if allowed and reset_type not in allowed:
            # Graceful variants need an ACPI-aware OS. On a machine sitting in
            # the installer or with no OS at all they are silently ignored, so
            # fall back to the forced equivalent rather than hanging.
            fallback = {
                "GracefulShutdown": "ForceOff",
                "GracefulRestart": "ForceRestart",
            }.get(reset_type)
            if fallback and fallback in allowed:
                self._log(
                    f"BMC does not advertise {reset_type}; using {fallback}",
                    level="warning",
                )
                reset_type = fallback
            else:
                raise BMCError(
                    f"BMC does not support ResetType {reset_type}; allowed: {allowed}"
                )

        self._request("POST", reset_path, json_body={"ResetType": reset_type})

    # -- boot --------------------------------------------------------------

    def set_boot_once(self, target: str) -> None:
        if target not in BOOT_TARGETS:
            raise ValueError(f"unknown boot target {target!r}")
        system = self._get(self.system_path)
        boot: dict[str, Any] = {
            "BootSourceOverrideEnabled": "Once",
            "BootSourceOverrideTarget": BOOT_TARGETS[target],
        }

        # Only send the mode when the BMC says it is writable. Some M4 builds
        # reject the property outright and fail the whole PATCH with it.
        settable = system.get("Boot@Redfish.AllowableValues") or system.get("@Redfish.Settings")
        mode_values = system.get("Boot", {}).get(
            "BootSourceOverrideMode@Redfish.AllowableValues"
        )
        if mode_values and settable is not None:
            current_mode = system.get("Boot", {}).get("BootSourceOverrideMode")
            if current_mode in mode_values:
                boot["BootSourceOverrideMode"] = current_mode

        self._request("PATCH", self.system_path, json_body={"Boot": boot})

        # Trust nothing: read it back. A silently-ignored PATCH here means a
        # reinstall boots the old OS and destroys nothing, which looks like a
        # hung job rather than an error.
        after = self._get(self.system_path).get("Boot", {})
        if after.get("BootSourceOverrideTarget") != BOOT_TARGETS[target]:
            raise BMCError(
                "boot override did not take effect: "
                f"wanted {BOOT_TARGETS[target]}, BMC reports "
                f"{after.get('BootSourceOverrideTarget')!r}",
                response={"Boot": after},
            )
        if after.get("BootSourceOverrideEnabled") not in {"Once", "Continuous"}:
            raise BMCError(
                "boot override target set but override is disabled",
                response={"Boot": after},
            )

    def clear_boot_override(self) -> None:
        """Turn the override off.

        Many implementations (sushy, some Dell and HPE builds) reject a PATCH
        of `BootSourceOverrideEnabled` on its own and want the target in the
        same body, so the conventional two-field form goes first and the bare
        form is the fallback for BMCs that dislike `Target: None`.
        """
        try:
            self._request(
                "PATCH",
                self.system_path,
                json_body={
                    "Boot": {
                        "BootSourceOverrideEnabled": "Disabled",
                        "BootSourceOverrideTarget": "None",
                    }
                },
                retries=1,
            )
        except BMCError as exc:
            if (exc.response or {}).get("status") != 400:
                raise
            self._request(
                "PATCH",
                self.system_path,
                json_body={"Boot": {"BootSourceOverrideEnabled": "Disabled"}},
            )

    # -- virtual media -----------------------------------------------------

    def _virtual_media_collection(self) -> str:
        """Where this BMC keeps virtual media.

        Follow the links rather than assume a path: Redfish 1.0 (the M4's
        CIMC) hangs VirtualMedia off the Manager, newer schemas moved it to
        the ComputerSystem, and both publish a link to wherever it lives.
        """
        manager = self._get(self.manager_path)
        link = (manager.get("VirtualMedia") or {}).get("@odata.id")
        if link:
            return link
        system = self._get(self.system_path)
        link = (system.get("VirtualMedia") or {}).get("@odata.id")
        return link or f"{self.manager_path}/VirtualMedia"

    def _virtual_media_members(self) -> list[dict]:
        collection = self._get(self._virtual_media_collection())
        return collection.get("Members") or []

    def _find_removable_media(self) -> dict:
        """Pick the CD/DVD slot. The M4 exposes several; only some take ISOs."""
        for member in self._virtual_media_members():
            resource = self._get(member["@odata.id"])
            media_types = resource.get("MediaTypes") or []
            if any(t in {"CD", "DVD"} for t in media_types):
                return resource
        raise BMCError("no CD/DVD virtual media slot exposed by this BMC")

    def insert_virtual_media(self, image_url: str) -> None:
        resource = self._find_removable_media()
        path = resource["@odata.id"]
        if resource.get("Inserted"):
            self.eject_virtual_media()

        insert_action = (
            resource.get("Actions", {}).get("#VirtualMedia.InsertMedia", {}).get("target")
        )
        if insert_action:
            # The CIMC wants the transport named as well as the URL (its
            # vMedia mount types are NFS, CIFS and WWW); other BMCs ignore it.
            scheme = urlparse(image_url).scheme.lower()
            protocol = {"http": "HTTP", "https": "HTTPS", "nfs": "NFS", "cifs": "CIFS",
                        "smb": "CIFS"}.get(scheme)
            body: dict[str, Any] = {
                "Image": image_url, "Inserted": True, "WriteProtected": True,
            }
            if protocol:
                body["TransferProtocolType"] = protocol
            self._request("POST", insert_action, json_body=body)
        else:
            # Cisco quirk: pre-4.1 CIMC omits the InsertMedia action and
            # expects a PATCH of the Image property instead.
            self._log("InsertMedia action absent; falling back to PATCH Image", level="warning")
            self._request(
                "PATCH", path, json_body={"Image": image_url, "Inserted": True}
            )

        after = self._get(path)
        if not after.get("Inserted"):
            raise BMCError(
                "virtual media did not report as inserted", response=_redact(after)
            )

    def virtual_media(self) -> list[dict]:
        out = []
        for member in self._virtual_media_members():
            resource = self._get(member["@odata.id"])
            out.append(
                {
                    "id": resource.get("Id"),
                    "name": resource.get("Name"),
                    "media_types": resource.get("MediaTypes") or [],
                    "inserted": bool(resource.get("Inserted")),
                    "image": resource.get("Image"),
                    "image_name": resource.get("ImageName"),
                    "connected_via": resource.get("ConnectedVia"),
                }
            )
        return out

    def eject_virtual_media(self) -> None:
        try:
            resource = self._find_removable_media()
        except BMCError:
            return
        path = resource["@odata.id"]
        eject_action = (
            resource.get("Actions", {}).get("#VirtualMedia.EjectMedia", {}).get("target")
        )
        if eject_action:
            self._request("POST", eject_action, json_body={}, expect=(200, 202, 204))
        else:
            self._request("PATCH", path, json_body={"Image": None, "Inserted": False})

    # -- inventory ---------------------------------------------------------

    def inventory(self) -> HardwareInventory:
        system = self._get(self.system_path)
        inv = HardwareInventory(
            manufacturer=system.get("Manufacturer"),
            model=system.get("Model"),
            serial=system.get("SerialNumber"),
            bios_version=system.get("BiosVersion"),
            raw={"system": system},
        )

        summary = system.get("ProcessorSummary") or {}
        inv.cpu_count = summary.get("Count")
        inv.cpu_model = summary.get("Model")
        inv.cpu_cores_total = summary.get("LogicalProcessorCount")

        mem = system.get("MemorySummary") or {}
        if mem.get("TotalSystemMemoryGiB"):
            inv.ram_gb = int(mem["TotalSystemMemoryGiB"])

        try:
            manager = self._get(self.manager_path)
            inv.bmc_firmware = manager.get("FirmwareVersion")
        except BMCError:
            self._log("could not read Manager firmware version", level="warning")

        inv.nics = self._collect_nics(system)
        inv.drives = self._collect_drives(system)
        return inv

    def _collect_nics(self, system: dict) -> list[dict]:
        nics: list[dict] = []
        # EthernetInterfaces is the portable path; the M4 populates it for the
        # onboard i350 ports, which is what we PXE from.
        path = (system.get("EthernetInterfaces") or {}).get("@odata.id")
        if not path:
            return nics
        try:
            for member in (self._get(path).get("Members") or []):
                iface = self._get(member["@odata.id"])
                mac = iface.get("MACAddress") or iface.get("PermanentMACAddress")
                if not mac:
                    continue
                nics.append(
                    {
                        "mac": mac.lower(),
                        "name": iface.get("Id") or iface.get("Name"),
                        "speed_mbps": iface.get("SpeedMbps"),
                        "link_status": iface.get("LinkStatus"),
                    }
                )
        except BMCError:
            self._log("could not enumerate ethernet interfaces", level="warning")
        return nics

    def _collect_drives(self, system: dict) -> list[dict]:
        """Best-effort physical drive list.

        The M4's RAID controller hides member disks behind virtual drives, so
        this is inventory only. Anything that actually manipulates the array
        happens in the ramdisk via StorCLI.
        """
        drives: list[dict] = []
        storage_path = (system.get("Storage") or {}).get("@odata.id")
        if not storage_path:
            return drives
        try:
            for controller_ref in (self._get(storage_path).get("Members") or []):
                controller = self._get(controller_ref["@odata.id"])
                for drive_ref in (controller.get("Drives") or []):
                    drive = self._get(drive_ref["@odata.id"])
                    capacity = drive.get("CapacityBytes")
                    drives.append(
                        {
                            "name": drive.get("Name") or drive.get("Id"),
                            "serial": drive.get("SerialNumber"),
                            "model": drive.get("Model"),
                            "media": drive.get("MediaType"),
                            "protocol": drive.get("Protocol"),
                            "capacity_gb": round(capacity / 1_000_000_000) if capacity else None,
                            "health": (drive.get("Status") or {}).get("Health"),
                            "failure_predicted": drive.get("FailurePredicted"),
                        }
                    )
        except BMCError:
            self._log("could not enumerate storage", level="warning")
        return drives

    # -- health ------------------------------------------------------------

    def health(self) -> HealthStatus:
        system = self._get(self.system_path)
        status = system.get("Status") or {}
        overall = self._map_health(status.get("HealthRollup") or status.get("Health"))

        subsystems: dict[str, Any] = {
            "system": {
                "status": self._map_health(status.get("Health")),
                "state": status.get("State"),
            }
        }

        for key, label in (("ProcessorSummary", "cpu"), ("MemorySummary", "memory")):
            block = system.get(key) or {}
            sub_status = block.get("Status") or {}
            if sub_status:
                subsystems[label] = {
                    "status": self._map_health(
                        sub_status.get("HealthRollup") or sub_status.get("Health")
                    ),
                    "state": sub_status.get("State"),
                }

        subsystems.update(self._chassis_health())

        # Roll the worst subsystem up, so a failed PSU on an otherwise-ok
        # system does not report as healthy.
        rank = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}
        worst = max(
            [overall] + [s.get("status", "unknown") for s in subsystems.values()],
            key=lambda s: rank.get(s, 1),
        )
        return HealthStatus(status=worst, subsystems=subsystems, raw={"system": status})

    def _chassis_health(self) -> dict[str, Any]:
        """PSU / fan / temperature state.

        These are what actually fail on eight-year-old hardware, so they are
        collected even though the M4's Redfish Thermal resource is patchy.
        """
        out: dict[str, Any] = {}
        try:
            chassis_members = self._get("/redfish/v1/Chassis").get("Members") or []
        except BMCError:
            return out

        for ref in chassis_members:
            try:
                chassis = self._get(ref["@odata.id"])
            except BMCError:
                continue

            power_ref = (chassis.get("Power") or {}).get("@odata.id")
            if power_ref:
                try:
                    power = self._get(power_ref)
                    supplies = [
                        {
                            "name": ps.get("Name"),
                            "status": self._map_health((ps.get("Status") or {}).get("Health")),
                            "state": (ps.get("Status") or {}).get("State"),
                        }
                        for ps in (power.get("PowerSupplies") or [])
                    ]
                    if supplies:
                        out["power_supplies"] = {
                            "status": self._worst([s["status"] for s in supplies]),
                            "detail": supplies,
                        }
                except BMCError:
                    pass

            thermal_ref = (chassis.get("Thermal") or {}).get("@odata.id")
            if thermal_ref:
                try:
                    thermal = self._get(thermal_ref)
                    fans = [
                        {
                            "name": fan.get("Name"),
                            "reading": fan.get("Reading"),
                            "status": self._map_health((fan.get("Status") or {}).get("Health")),
                        }
                        for fan in (thermal.get("Fans") or [])
                    ]
                    temps = [
                        {
                            "name": t.get("Name"),
                            "celsius": t.get("ReadingCelsius"),
                            "upper_critical": t.get("UpperThresholdCritical"),
                            "status": self._map_health((t.get("Status") or {}).get("Health")),
                        }
                        for t in (thermal.get("Temperatures") or [])
                        if t.get("ReadingCelsius") is not None
                    ]
                    if fans:
                        out["fans"] = {
                            "status": self._worst([f["status"] for f in fans]),
                            "detail": fans,
                        }
                    if temps:
                        out["temperatures"] = {
                            "status": self._worst([t["status"] for t in temps]),
                            "detail": temps,
                        }
                except BMCError:
                    pass
        return out

    @staticmethod
    def _map_health(value: str | None) -> str:
        return {
            "OK": "ok",
            "Warning": "warning",
            "Critical": "critical",
        }.get(value or "", "unknown")

    @staticmethod
    def _worst(statuses: list[str]) -> str:
        rank = {"ok": 0, "unknown": 1, "warning": 2, "critical": 3}
        return max(statuses or ["unknown"], key=lambda s: rank.get(s, 1))

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._logout()
        self._session.close()
