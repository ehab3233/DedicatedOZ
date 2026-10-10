"""Cisco IMC XML API.

The one Cisco-specific piece of the driver layer, and deliberately small. It
does the things neither IPMI nor Redfish can do on a C220 M4:

* switch IPMI over LAN on (it is off by default on many CIMC builds, and
  without it there is no SOL console and no IPMI power path);
* switch Serial-over-LAN on at 115200 on COM0, and point the BIOS console
  redirection at the same port;
* switch on the other services the panel uses (virtual media, KVM, Redfish),
  PXE on the LAN ports, the boot order and NTP;
* mint the pair of one-time tokens that launch the vKVM console without the
  operator typing CIMC credentials.

Settings are read before they are written (`ensure`). Rewriting a setting the
CIMC already has is not a no-op on this firmware: rewriting IPMI over LAN
restarts the IPMI service and drops every session on it, so a second run of
Prepare BMC used to make the panel lose the server for a while.

Object and attribute names follow the published C-Series XML API (the same
ones Cisco's imcsdk generates): `aaaLogin`, `aaaGetComputeAuthTokens`,
`solIf`, `commIpmiLan`, `biosVfConsoleRedirection`. Requests are POSTed to
`/nuova` exactly as imcsdk does, form-encoded content type included.

Every login is paired with a logout in a `finally`: XML API sessions count
against the CIMC's small session limit just like Redfish ones do.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import requests
import urllib3

from app.config import settings
from app.drivers.base import BMCError, LogSink, null_sink
from app.secrets import BMCCredential

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RACK_UNIT = "sys/rack-unit-1"
SOL_DN = f"{RACK_UNIT}/sol-if"
IPMI_LAN_DN = "sys/svc-ext/ipmi-lan-svc"
VMEDIA_DN = "sys/svc-ext/vmedia-svc"
KVM_DN = "sys/svc-ext/kvm-svc"
REDFISH_DN = "sys/svc-ext/redfish-svc"
NTP_DN = "sys/svc-ext/ntp-svc"
CONSOLE_REDIRECTION_DN = f"{RACK_UNIT}/bios/bios-settings/Console-redirection"
LOM_OPTION_ROM_DN = f"{RACK_UNIT}/bios/bios-settings/LOMPort-OptionROM"
BOOT_PRECISION_DN = f"{RACK_UNIT}/boot-precision"

#: Paths the HTML5 vKVM viewer has lived at across CIMC releases. Probed in
#: order; the first one the CIMC serves wins. Override with
#: DOZ_KVM_URL_TEMPLATE once you know what your firmware uses.
HTML5_KVM_CANDIDATES = (
    "/html/kvmViewer.html",
    "/html/kvm.html",
    "/kvmViewer.html",
)


def quoteattr(value: str) -> str:
    """A double-quoted XML attribute value, the way imcsdk writes them.

    `xml.sax.saxutils.quoteattr` switches to single quotes when the value
    contains a double quote. That is valid XML, but the CIMC's parser has only
    ever been fed imcsdk's double-quoted form, so that is what it gets.
    """
    return '"' + escape(value, {'"': "&quot;"}) + '"'


class CimcXmlApi:
    def __init__(
        self,
        host: str,
        credential: BMCCredential,
        *,
        port: int | None = None,
        log: LogSink = null_sink,
        timeout: int = 20,
        verify_tls: bool | None = None,
    ) -> None:
        self.host = host
        self.base_url = f"https://{host}:{port}" if port else f"https://{host}"
        self._cred = credential
        self._log = log
        self._timeout = timeout
        self._session = requests.Session()
        # Same reasoning as the Redfish driver: no environment proxies or CA
        # bundles for BMC traffic.
        self._session.trust_env = False
        self._session.verify = settings.redfish_verify_tls if verify_tls is None else verify_tls
        self.cookie: str | None = None
        self.version: str | None = None

    # -- transport ---------------------------------------------------------

    def _post(self, body: str, *, what: str) -> ET.Element:
        url = f"{self.base_url}/nuova"
        record = {"protocol": "cimc-xml", "url": url, "method": what}
        try:
            resp = self._session.post(
                url,
                data=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise BMCError(f"CIMC XML API unreachable: {exc}", request=record) from exc

        text = resp.text or ""
        response = {"status": resp.status_code, "body": _redact_xml(text)[:4000]}
        if resp.status_code != 200:
            self._log(f"{what} -> HTTP {resp.status_code}", level="error",
                      request=record, response=response)
            raise BMCError(
                f"{what} returned HTTP {resp.status_code}; is the XML API enabled on the CIMC "
                "(Admin > Communication Services > XML API)?",
                request=record,
                response=response,
            )
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise BMCError(f"{what}: CIMC returned something that is not XML",
                           request=record, response=response) from exc

        if root.get("errorCode") or root.get("errorDescr"):
            detail = root.get("errorDescr") or root.get("invocationResult") or "unknown error"
            self._log(f"{what} -> error {root.get('errorCode')}: {detail}", level="error",
                      request=record, response=response)
            raise BMCError(f"{what}: {detail} (CIMC error {root.get('errorCode')})",
                           request=record, response=response)
        self._log(f"{what} -> ok", request=record, response=response)
        return root

    # -- session -----------------------------------------------------------

    def login(self) -> None:
        root = self._post(
            f"<aaaLogin inName={quoteattr(self._cred.username)} "
            f"inPassword={quoteattr(self._cred.password)} />",
            what="aaaLogin",
        )
        self.cookie = root.get("outCookie")
        self.version = root.get("outVersion")
        if not self.cookie:
            raise BMCError("aaaLogin returned no session cookie")

    def logout(self) -> None:
        if not self.cookie:
            return
        cookie, self.cookie = self.cookie, None
        try:
            self._post(
                f"<aaaLogout cookie={quoteattr(cookie)} inCookie={quoteattr(cookie)} />",
                what="aaaLogout",
            )
        except BMCError:
            # A failed logout leaves a session to time out on the CIMC. Worth
            # a log line, not worth failing the operation that preceded it.
            self._log("aaaLogout failed; the session will expire on its own", level="warning")

    def __enter__(self) -> CimcXmlApi:
        self.login()
        return self

    def __exit__(self, *exc: object) -> None:
        self.logout()
        self._session.close()

    # -- generic object access --------------------------------------------

    def resolve_class(self, class_id: str) -> list[dict[str, str]]:
        root = self._post(
            f"<configResolveClass cookie={quoteattr(self.cookie or '')} "
            f'inHierarchical="false" classId={quoteattr(class_id)} />',
            what=f"configResolveClass {class_id}",
        )
        out = root.find("outConfigs")
        return [dict(child.attrib) for child in (out if out is not None else [])]

    def configure(self, dn: str, class_id: str, **attrs: str) -> dict[str, str]:
        attributes = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
        root = self._post(
            f"<configConfMo cookie={quoteattr(self.cookie or '')} dn={quoteattr(dn)} "
            f'inHierarchical="false"><inConfig>'
            f"<{class_id} dn={quoteattr(dn)} {attributes} />"
            f"</inConfig></configConfMo>",
            what=f"configConfMo {dn}",
        )
        out = root.find("outConfig")
        child = out[0] if out is not None and len(out) else None
        return dict(child.attrib) if child is not None else {}

    def configure_tree(
        self,
        dn: str,
        class_id: str,
        attrs: dict[str, str],
        children: list[tuple[str, dict[str, str]]],
    ) -> dict[str, str]:
        """configConfMo with child objects, for things the CIMC models as a tree."""
        parent_attrs = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())
        inner = "".join(
            f"<{cls} " + " ".join(f"{k}={quoteattr(v)}" for k, v in a.items()) + " />"
            for cls, a in children
        )
        root = self._post(
            f"<configConfMo cookie={quoteattr(self.cookie or '')} dn={quoteattr(dn)} "
            f'inHierarchical="true"><inConfig>'
            f"<{class_id} dn={quoteattr(dn)} {parent_attrs}>{inner}</{class_id}>"
            f"</inConfig></configConfMo>",
            what=f"configConfMo {dn}",
        )
        out = root.find("outConfig")
        child = out[0] if out is not None and len(out) else None
        return dict(child.attrib) if child is not None else {}

    def resolve_dn(self, dn: str) -> dict[str, str] | None:
        """The object at `dn`, or None when the CIMC has nothing there."""
        root = self._post(
            f"<configResolveDn cookie={quoteattr(self.cookie or '')} dn={quoteattr(dn)} "
            'inHierarchical="false" />',
            what=f"configResolveDn {dn}",
        )
        out = root.find("outConfig")
        child = out[0] if out is not None and len(out) else None
        return dict(child.attrib) if child is not None else None

    def ensure(self, dn: str, class_id: str, **attrs: str) -> tuple[bool, dict[str, str]]:
        """Set `attrs` on `dn` unless the CIMC already has every one of them.

        Returns (changed, the object's attributes afterwards). A setting that
        cannot be read first is written anyway, so an unreadable object
        behaves exactly as a plain write did.
        """
        try:
            current = self.resolve_dn(dn)
        except BMCError as exc:
            self._log(f"could not read {dn} before setting it ({exc})", level="warning")
            current = None
        if current is not None and all(
            str(current.get(key, "")).lower() == value.lower() for key, value in attrs.items()
        ):
            return False, current
        return True, self.configure(dn, class_id, **attrs)

    # -- the things we actually need -------------------------------------
    # Each returns (changed, attributes): see `ensure`.

    def enable_ipmi_over_lan(self) -> tuple[bool, dict[str, str]]:
        # The encryption key is forced to the all-zeros default: ipmitool is
        # not given one, and a CIMC with a custom key refuses every session
        # with the same message as a wrong password.
        return self.ensure(
            IPMI_LAN_DN, "commIpmiLan", adminState="enabled", priv="admin", key="0" * 40
        )

    # -- RAID through the CIMC (fallback for firmware without Redfish volumes) --

    def storage_controllers(self) -> list[dict[str, str]]:
        return self.resolve_class("storageController")

    def local_disks(self, controller_dn: str) -> list[dict[str, str]]:
        return [d for d in self.resolve_class("storageLocalDisk")
                if d.get("dn", "").startswith(controller_dn + "/")]

    def virtual_drives(self, controller_dn: str) -> list[dict[str, str]]:
        return [v for v in self.resolve_class("storageVirtualDrive")
                if v.get("dn", "").startswith(controller_dn + "/")]

    def delete_virtual_drive(self, dn: str) -> dict[str, str]:
        # No adminAction deletes a virtual drive (the CIMC lists the valid
        # ones: initialise, set-boot-drive, hide...); as in Cisco's own SDK,
        # the object is removed by marking it deleted.
        return self.configure(dn, "storageVirtualDrive", status="deleted")

    def clear_foreign_config(self, controller_dn: str) -> dict[str, str]:
        """Drop array metadata left on disks by a previous controller or array;
        until it is cleared the disks cannot join a new array."""
        return self.configure(
            controller_dn, "storageController", adminAction="clear-foreign-config"
        )

    def clear_boot_drive(self, controller_dn: str) -> dict[str, str]:
        """Unmark the controller's boot virtual drive. The CIMC refuses to
        delete a virtual drive while it is the boot ("OS") drive."""
        return self.configure(controller_dn, "storageController", adminAction="clear-boot-drive")

    def make_unconfigured_good(self, dn: str) -> dict[str, str]:
        return self.configure(dn, "storageLocalDisk", adminAction="make-unconfigured-good")

    def set_boot_drive(self, dn: str) -> dict[str, str]:
        return self.configure(dn, "storageVirtualDrive", adminAction="set-boot-drive")

    def create_virtual_drive(
        self,
        controller_dn: str,
        *,
        name: str,
        raid_level: int,
        drive_groups: list[list[int]],
        size: str,
    ) -> dict[str, str]:
        """Build a virtual drive from unconfigured-good disks.

        `drive_groups` is [[1, 2]] for RAID 0/1/5/6 and [[1, 2], [3, 4]] for
        RAID 10; `size` is "<n> MB". These are the attributes Cisco's imcsdk
        sends for the same object.
        """
        group = "".join("[" + ",".join(str(i) for i in g) + "]" for g in drive_groups)
        return self.configure(
            f"{controller_dn}/virtual-drive-create",
            "storageVirtualDriveCreatorUsingUnusedPhysicalDrive",
            virtualDriveName=name,
            raidLevel=str(raid_level),
            driveGroup=group,
            size=size,
            adminState="trigger",
        )

    def enable_sol(
        self, *, speed: str = "115200", comport: str = "com0"
    ) -> tuple[bool, dict[str, str]]:
        return self.ensure(SOL_DN, "solIf", adminState="enable", speed=speed, comport=comport)

    def set_console_redirection(self, *, baud: str = "115200") -> tuple[bool, dict[str, str]]:
        """BIOS console redirection to COM0. Applies at the next host boot."""
        return self.ensure(
            CONSOLE_REDIRECTION_DN,
            "biosVfConsoleRedirection",
            vpConsoleRedirection="com-0",
            vpBaudRate=baud,
            vpFlowControl="none",
            vpTerminalType="vt100-plus",
        )

    def enable_vmedia(self) -> tuple[bool, dict[str, str]]:
        """Virtual media service on: what ISO installs mount through."""
        return self.ensure(VMEDIA_DN, "commVMedia", adminState="enabled")

    def enable_kvm(self) -> tuple[bool, dict[str, str]]:
        return self.ensure(KVM_DN, "commKvm", adminState="enabled", port="2068")

    def enable_redfish(self) -> tuple[bool, dict[str, str]]:
        """Redfish on (CIMC 3.0 and later; older builds reject the object)."""
        return self.ensure(REDFISH_DN, "commRedfish", adminState="enabled")

    def set_ntp(self, servers: list[str]) -> tuple[bool, dict[str, str]]:
        """Point the CIMC's clock at NTP, so event-log timestamps mean something."""
        attrs = {"ntpEnable": "yes"}
        for i, server in enumerate(servers[:4], start=1):
            attrs[f"ntpServer{i}"] = server
        return self.ensure(NTP_DN, "commNtpProvider", **attrs)

    def enable_lom_pxe(self) -> tuple[bool, dict[str, str]]:
        """Option ROMs on the LAN-on-motherboard ports, so they can PXE boot."""
        return self.ensure(
            LOM_OPTION_ROM_DN, "biosVfLOMPortOptionROM", vpLOMPortsAllState="Enabled"
        )

    def set_boot_order(self, order: str = "disk,pxe") -> tuple[bool, dict[str, str]]:
        """Persistent boot order, in the mode installs use (legacy unless
        DOZ_IPMI_BOOT_EFI is set).

        "disk,pxe" boots the installed OS by default and leaves PXE available
        for the one-time override installs use; "pxe,disk" makes every boot
        ask the management server first. Always written: the CIMC applies it
        at the next boot without restarting anything, and comparing a boot
        tree the admin may have renamed is not worth the guesswork.
        """
        devices = [d.strip() for d in order.split(",") if d.strip()]
        if not devices or any(d not in {"disk", "pxe"} for d in devices):
            raise ValueError("boot order must be a list of disk and pxe, e.g. disk,pxe")
        children: list[tuple[str, dict[str, str]]] = []
        for position, device in enumerate(devices, start=1):
            if device == "disk":
                children.append(("lsbootHdd", {
                    "rn": "hdd-local", "name": "local", "order": str(position),
                    "state": "Enabled",
                }))
            else:
                children.append(("lsbootPxe", {
                    "rn": "pxe-net", "name": "net", "order": str(position),
                    "state": "Enabled", "slot": "L", "port": "0",
                }))
        mode = "Uefi" if settings.ipmi_boot_efi else "Legacy"
        return True, self.configure_tree(
            BOOT_PRECISION_DN, "lsbootDevPrecision",
            {"configuredBootMode": mode, "rebootOnUpdate": "no"}, children,
        )

    def compute_auth_tokens(self) -> tuple[str, str]:
        root = self._post(
            f"<aaaGetComputeAuthTokens cookie={quoteattr(self.cookie or '')} />",
            what="aaaGetComputeAuthTokens",
        )
        tokens = (root.get("outTokens") or "").split(",")
        if len(tokens) != 2 or not all(tokens):
            raise BMCError("aaaGetComputeAuthTokens returned no tokens")
        return tokens[0], tokens[1]

    # -- utilisation -----------------------------------------------------

    def server_utilization(self) -> dict[str, int | None] | None:
        """The CIMC's own CPU, memory and IO utilisation, in percent.

        This is the `serverUtilization` object the CIMC summary page charts
        (one per rack unit; the figures come from the Intel management
        engine's CUPS counters). A CIMC without the object, or with nothing
        to report, gives None rather than an error: the panel shows "not
        reported" and carries on.
        """
        try:
            rows = self.resolve_class("serverUtilization")
        except BMCError as exc:
            self._log(f"serverUtilization not readable: {exc}", level="warning")
            return None
        if not rows:
            return None
        row = rows[0]

        def percent(key: str) -> int | None:
            value = (row.get(key) or "").strip()
            try:
                return max(0, min(100, int(float(value))))
            except ValueError:
                return None  # "N/A" while the host is off

        return {
            "overall": percent("overallUtilization"),
            "cpu": percent("cpuUtilization"),
            "memory": percent("memoryUtilization"),
            "io": percent("ioUtilization"),
        }

    # -- KVM -------------------------------------------------------------

    def kvm_launch(self) -> dict[str, str | bool | None | list]:
        """One-time vKVM launch URLs. Tokens expire quickly; open immediately.

        Older CIMC builds answer `aaaGetComputeAuthTokens` with "Method not
        supported" (error 2009). That is not a failure of the server or the
        network, so it is reported as `tokens_unsupported` with the CIMC web
        UI as the way in, rather than raised.
        """
        try:
            tkn1, tkn2 = self.compute_auth_tokens()
        except BMCError as exc:
            text = str(exc)
            if "not supported" in text.lower() or "error 2009" in text:
                self._log(f"KVM tokens not available on this firmware: {text}", level="warning")
                return {
                    "html5": None,
                    "java": None,
                    "cimc": f"{self.base_url}/",
                    "tokens_unsupported": True,
                    "reason": text,
                    "probe": [],
                }
            raise
        # The same four parameters Cisco documents for the Java launcher; the
        # HTML5 viewer at /html/kvmViewer.html is given them too, since the
        # tokens alone are not enough for it to know which CIMC to talk to.
        query = urlencode({"cimcAddr": self.host, "cimcName": "KVM",
                           "tkn1": tkn1, "tkn2": tkn2})
        html5: str | None = None
        probe: list[dict] = []
        if settings.kvm_url_template:
            html5 = settings.kvm_url_template.format(
                host=self.base_url.removeprefix("https://"), tkn1=tkn1, tkn2=tkn2
            )
        else:
            path, probe = self._probe_html5_viewer()
            if path:
                html5 = f"{self.base_url}{path}?{query}"
        java = f"{self.base_url}/kvm.jnlp?{query}"
        return {"html5": html5, "java": java, "cimc": f"{self.base_url}/",
                "tokens_unsupported": False, "reason": None, "probe": probe}

    def _probe_html5_viewer(self) -> tuple[str | None, list[dict]]:
        """Which of the known viewer paths this CIMC serves, with what each
        answered, so a miss can be read from the panel rather than guessed at.

        Only a page that actually mentions the KVM counts: a CIMC that serves
        its login page for any path would otherwise "have" the first path
        tried, and the panel would open a login screen with tokens on it.
        """
        probe: list[dict] = []
        for path in HTML5_KVM_CANDIDATES:
            try:
                resp = self._session.get(
                    f"{self.base_url}{path}", timeout=self._timeout, allow_redirects=False
                )
            except requests.RequestException as exc:
                probe.append({"path": path, "status": None, "error": str(exc)[:120]})
                continue
            viewer = resp.status_code == 200 and "kvm" in (resp.text or "")[:65536].lower()
            probe.append({"path": path, "status": resp.status_code, "viewer": viewer,
                          "location": resp.headers.get("Location")})
            if viewer:
                self._log(f"HTML5 KVM viewer found at {path}")
                return path, probe
        self._log(
            "no HTML5 KVM viewer at any known path: "
            + ", ".join(f"{p['path']} -> {p.get('status') or p.get('error')}" for p in probe),
            level="warning",
        )
        return None, probe


def _redact_xml(text: str) -> str:
    """Strip passwords and session material from logged XML."""
    import re

    text = re.sub(
        r'(inPassword|outCookie|cookie|inCookie|outTokens|key)="[^"]*"', r'\1="***"', text
    )
    return text
