"""Cisco IMC XML API.

The one Cisco-specific piece of the driver layer, and deliberately small. It
does the three things neither IPMI nor Redfish can do on a C220 M4:

* switch IPMI over LAN on (it is off by default on many CIMC builds, and
  without it there is no SOL console and no IPMI power path);
* switch Serial-over-LAN on at 115200 on COM0, and point the BIOS console
  redirection at the same port;
* mint the pair of one-time tokens that launch the vKVM console without the
  operator typing CIMC credentials.

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
CONSOLE_REDIRECTION_DN = f"{RACK_UNIT}/bios/bios-settings/Console-redirection"

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

    # -- the things we actually need -------------------------------------

    def enable_ipmi_over_lan(self) -> dict[str, str]:
        return self.configure(IPMI_LAN_DN, "commIpmiLan", adminState="enabled", priv="admin")

    def enable_sol(self, *, speed: str = "115200", comport: str = "com0") -> dict[str, str]:
        return self.configure(SOL_DN, "solIf", adminState="enable", speed=speed, comport=comport)

    def set_console_redirection(self, *, baud: str = "115200") -> dict[str, str]:
        """BIOS console redirection to COM0. Applies at the next host boot."""
        return self.configure(
            CONSOLE_REDIRECTION_DN,
            "biosVfConsoleRedirection",
            vpConsoleRedirection="com-0",
            vpBaudRate=baud,
            vpFlowControl="none",
            vpTerminalType="vt100-plus",
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

    # -- KVM -------------------------------------------------------------

    def kvm_launch(self) -> dict[str, str | None]:
        """One-time vKVM launch URLs. Tokens expire quickly; open immediately."""
        tkn1, tkn2 = self.compute_auth_tokens()
        query = urlencode({"tkn1": tkn1, "tkn2": tkn2})
        html5: str | None = None
        if settings.kvm_url_template:
            html5 = settings.kvm_url_template.format(
                host=self.base_url.removeprefix("https://"), tkn1=tkn1, tkn2=tkn2
            )
        else:
            path = self._probe_html5_viewer()
            if path:
                html5 = f"{self.base_url}{path}?{query}"
        java_query = urlencode({"cimcAddr": self.host, "tkn1": tkn1, "tkn2": tkn2})
        java = f"{self.base_url}/kvm.jnlp?{java_query}"
        return {"html5": html5, "java": java, "cimc": f"{self.base_url}/"}

    def _probe_html5_viewer(self) -> str | None:
        for path in HTML5_KVM_CANDIDATES:
            try:
                resp = self._session.get(
                    f"{self.base_url}{path}", timeout=self._timeout, allow_redirects=False
                )
            except requests.RequestException:
                continue
            if resp.status_code != 404:
                self._log(f"HTML5 KVM viewer found at {path} (HTTP {resp.status_code})")
                return path
        self._log("no HTML5 KVM viewer found at any known path", level="warning")
        return None


def _redact_xml(text: str) -> str:
    """Strip passwords and session material from logged XML."""
    import re

    text = re.sub(r'(inPassword|outCookie|cookie|inCookie|outTokens)="[^"]*"', r'\1="***"', text)
    return text
