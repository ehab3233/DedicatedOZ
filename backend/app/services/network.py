"""The switch port and the PXE lease on the router, from what the panel knows.

For a server the panel already holds everything the router needs: the
customer's public block with its VLAN and gateway, the address assigned to
the server, its PXE MAC and the switch port it is cabled to. This turns that
record into RouterOS configuration: paste-ready commands always, and applied
over the RouterOS 7 REST API when DOZ_ROUTEROS_URL is set.

What gets programmed, per server, idempotently:

- a VLAN interface on the bridge for the block's VLAN, with the block's
  gateway address on it, and the bridge VLAN entry that gives the router a
  leg in it;
- a DHCP server on that interface that hands out addresses only to known
  MACs (`static-only`), with the block as its network and the PXE options
  pointing at the management server;
- the server's switch port moved into the VLAN (`pvid`);
- a lease binding the server's PXE MAC to its assigned address.

The installed OS never uses DHCP (the installer writes the address
statically), so the lease serves the PXE ROM, the ramdisk and the OS
installer, and a manual install from an ISO.
"""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
import urllib3
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.drivers.base import LogSink, null_sink
from app.models import IPAssignment, Server


class PlanError(ValueError):
    """The panel does not know enough about this server to program the router."""


class NetworkError(RuntimeError):
    """The router refused, or could not be reached."""


@dataclass(frozen=True)
class NetworkPlan:
    serial: str
    vlan: int
    port: str
    mac: str
    address: str
    prefix_len: int
    gateway: str
    #: The block, e.g. 103.167.10.0/28: the DHCP server's network.
    network: str
    next_server: str
    boot_file: str
    dns_server: str

    @property
    def vlan_interface(self) -> str:
        return f"vlan{self.vlan}"

    @property
    def dhcp_server(self) -> str:
        return f"dhcp-vlan{self.vlan}"

    def as_dict(self) -> dict:
        return {**asdict(self), "vlan_interface": self.vlan_interface,
                "dhcp_server": self.dhcp_server}


def configured() -> bool:
    """Whether the panel has a router to program (DOZ_ROUTEROS_URL)."""
    return bool(settings.routeros_url)


def next_server() -> str:
    """What PXE clients are sent to: the management server's address."""
    if settings.routeros_next_server:
        return settings.routeros_next_server
    return urlsplit(settings.control_plane_url).hostname or ""


def plan_for(db: Session, server: Server) -> NetworkPlan:
    """What the router should hold for this server, or why that cannot be said yet."""
    assignment = db.execute(
        select(IPAssignment).where(
            IPAssignment.server_id == server.id,
            IPAssignment.released_at.is_(None),
            IPAssignment.is_primary.is_(True),
        ).limit(1)
    ).scalar_one_or_none()
    if assignment is None:
        raise PlanError("no primary address assigned (Network tab → Assign address)")
    block = assignment.block
    if block.vlan is None:
        raise PlanError(f"block {block.cidr} has no VLAN (IP space → the block's VLAN)")
    if not block.gateway:
        raise PlanError(f"block {block.cidr} has no gateway")
    if not server.provisioning_mac:
        raise PlanError("no PXE MAC recorded (Hardware tab)")
    if not server.switch_port:
        raise PlanError("no switch port recorded (Edit → Switch port, the RouterOS interface name)")
    network = ipaddress.ip_network(block.cidr, strict=False)
    return NetworkPlan(
        serial=server.serial,
        vlan=int(block.vlan),
        port=server.switch_port.strip(),
        mac=server.provisioning_mac.upper(),
        address=str(assignment.address),
        prefix_len=int(assignment.prefix_len),
        gateway=str(block.gateway),
        network=str(network),
        next_server=next_server(),
        boot_file=settings.routeros_boot_file,
        dns_server=settings.routeros_dns_server,
    )


def routeros_script(plan: NetworkPlan) -> str:
    """The same configuration as RouterOS 7 commands, for doing it by hand."""
    bridge = settings.routeros_bridge
    return "\n".join([
        f"# {plan.serial}: {plan.port} into VLAN {plan.vlan}; "
        f"{plan.address}/{plan.prefix_len} by static lease on its PXE MAC.",
        "# Each line is safe to skip if the object already exists.",
        f"/interface vlan add name={plan.vlan_interface} interface={bridge} vlan-id={plan.vlan}",
        f"/ip address add address={plan.gateway}/{plan.prefix_len} interface={plan.vlan_interface}",
        f"/interface bridge vlan add bridge={bridge} vlan-ids={plan.vlan} tagged={bridge}",
        f"/ip dhcp-server add name={plan.dhcp_server} interface={plan.vlan_interface} "
        "address-pool=static-only lease-time=1d",
        f"/ip dhcp-server network add address={plan.network} gateway={plan.gateway} "
        f"dns-server={plan.dns_server} next-server={plan.next_server} "
        f"boot-file-name={plan.boot_file}",
        f"/interface bridge port set [find interface={plan.port}] pvid={plan.vlan}",
        f"/ip dhcp-server lease add server={plan.dhcp_server} address={plan.address} "
        f'mac-address={plan.mac} comment="doz {plan.serial}"',
        "",
    ])


class RouterOS:
    """The RouterOS 7 REST API: GET lists, PUT adds, PATCH changes, DELETE removes.

    Needs the `www-ssl` service on the router and a user with the `read`,
    `write`, `api` and `rest-api` policies.
    """

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        verify_tls: bool | None = None,
        timeout: int | None = None,
        log: LogSink = null_sink,
    ) -> None:
        self.base_url = (base_url or settings.routeros_url).rstrip("/")
        self._auth = (username or settings.routeros_username,
                      password or settings.routeros_password)
        self._verify = settings.routeros_verify_tls if verify_tls is None else verify_tls
        self._timeout = timeout or settings.routeros_timeout_seconds
        self._log = log
        self._session = requests.Session()
        if not self._verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        record = {"protocol": "routeros", "method": method, "url": url,
                  "params": params, "body": body}
        try:
            resp = self._session.request(
                method, url, params=params, json=body, auth=self._auth,
                verify=self._verify, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"router unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise NetworkError("the router refused the credentials "
                               "(DOZ_ROUTEROS_USERNAME / DOZ_ROUTEROS_PASSWORD)")
        try:
            data = resp.json() if resp.text else None
        except ValueError:
            data = None
        if resp.status_code >= 400:
            detail = ((data or {}).get("detail") or (data or {}).get("message")
                      or resp.text[:200] or f"HTTP {resp.status_code}")
            self._log(f"router: {method} {path} -> HTTP {resp.status_code}: {detail}",
                      level="error", request=record,
                      response={"status": resp.status_code, "body": resp.text[:1000]})
            raise NetworkError(f"{method} {path}: {detail}")
        return data

    def find(self, path: str, **match: str) -> dict | None:
        items = self._request("GET", path, params=match) or []
        return items[0] if items else None

    def ensure(self, path: str, match: dict[str, str],
               desired: dict[str, str]) -> tuple[bool, dict]:
        """Create `match`+`desired` if nothing matches, else bring it to `desired`."""
        current = self.find(path, **match)
        if current is None:
            created = self._request("PUT", path, body={**match, **desired})
            self._log(f"router: added {path} {match}")
            return True, created or {**match, **desired}
        changes = {k: v for k, v in desired.items() if str(current.get(k, "")) != str(v)}
        if not changes:
            return False, current
        updated = self._request("PATCH", f"{path}/{current['.id']}", body=changes)
        self._log(f"router: changed {path} {match}: {changes}")
        return True, updated or {**current, **changes}

    def apply(self, plan: NetworkPlan) -> dict:
        bridge = settings.routeros_bridge
        changes: list[str] = []

        # The VLAN's interface on the bridge, keeping an existing one's name.
        vlan = self.find("/interface/vlan", **{"vlan-id": str(plan.vlan), "interface": bridge})
        if vlan is None:
            vlan = self._request("PUT", "/interface/vlan", body={
                "name": plan.vlan_interface, "interface": bridge, "vlan-id": str(plan.vlan),
            }) or {"name": plan.vlan_interface}
            changes.append(f"VLAN interface {plan.vlan_interface}")
        iface = vlan["name"]

        changed, _ = self.ensure(
            "/ip/address", {"address": f"{plan.gateway}/{plan.prefix_len}", "interface": iface}, {}
        )
        if changed:
            changes.append(f"gateway {plan.gateway}/{plan.prefix_len} on {iface}")

        # The router's own leg in the VLAN (tagged on the bridge interface).
        entry = self.find("/interface/bridge/vlan", bridge=bridge, **{"vlan-ids": str(plan.vlan)})
        if entry is None:
            self._request("PUT", "/interface/bridge/vlan", body={
                "bridge": bridge, "vlan-ids": str(plan.vlan), "tagged": bridge,
            })
            changes.append(f"bridge VLAN {plan.vlan}")
        elif bridge not in (entry.get("tagged") or "").split(","):
            tagged = ",".join(filter(None, [entry.get("tagged"), bridge]))
            self._request("PATCH", f"/interface/bridge/vlan/{entry['.id']}",
                          body={"tagged": tagged})
            changes.append(f"bridge VLAN {plan.vlan} tagged on {bridge}")

        # DHCP for the install only: known MACs, nothing else.
        dhcp = self.find("/ip/dhcp-server", interface=iface)
        if dhcp is None:
            dhcp = self._request("PUT", "/ip/dhcp-server", body={
                "name": plan.dhcp_server, "interface": iface,
                "address-pool": "static-only", "lease-time": "1d",
            }) or {"name": plan.dhcp_server}
            changes.append(f"DHCP server {plan.dhcp_server}")
        elif dhcp.get("address-pool") != "static-only":
            self._log(
                f"router: DHCP server {dhcp['name']} on {iface} has a dynamic pool "
                f"({dhcp.get('address-pool')}): any device in VLAN {plan.vlan} can take an address",
                level="warning",
            )
        changed, _ = self.ensure("/ip/dhcp-server/network", {"address": plan.network}, {
            "gateway": plan.gateway, "dns-server": plan.dns_server,
            "next-server": plan.next_server, "boot-file-name": plan.boot_file,
        })
        if changed:
            changes.append(f"DHCP network {plan.network} with the PXE options")

        # The server's port into the VLAN.
        port = self.find("/interface/bridge/port", interface=plan.port)
        if port is None:
            raise NetworkError(
                f"{plan.port} is not a port of bridge {bridge} on the router; the server's "
                "switch port must be the RouterOS interface name (ether3, sfp-sfpplus1, ...)"
            )
        if str(port.get("pvid")) != str(plan.vlan):
            self._request("PATCH", f"/interface/bridge/port/{port['.id']}",
                          body={"pvid": str(plan.vlan)})
            changes.append(f"{plan.port} pvid {port.get('pvid')} -> {plan.vlan}")

        # The lease: its address must not be someone else's.
        holder = self.find("/ip/dhcp-server/lease", address=plan.address)
        if holder is not None and (holder.get("mac-address") or "").upper() != plan.mac:
            raise NetworkError(
                f"{plan.address} is leased to {holder.get('mac-address')} on the router "
                f"({holder.get('comment') or 'no comment'}); release it there first"
            )
        changed, _ = self.ensure("/ip/dhcp-server/lease", {"mac-address": plan.mac}, {
            "address": plan.address, "server": dhcp["name"], "comment": f"doz {plan.serial}",
        })
        if changed:
            changes.append(f"lease {plan.address} for {plan.mac}")

        description = (
            f"router: {plan.port} in VLAN {plan.vlan}, {plan.address} leased to {plan.mac}"
            + (f"; changed: {'; '.join(changes)}" if changes else "; nothing to change")
        )
        self._log(description)
        return {"vlan": plan.vlan, "port": plan.port, "address": plan.address,
                "mac": plan.mac, "changes": changes, "description": description}

    def remove_lease(self, mac: str) -> bool:
        lease = self.find("/ip/dhcp-server/lease", **{"mac-address": mac.upper()})
        if lease is None:
            return False
        self._request("DELETE", f"/ip/dhcp-server/lease/{lease['.id']}")
        self._log(f"router: removed the lease {lease.get('address')} for {mac.upper()}")
        return True


def apply(plan: NetworkPlan, *, log: LogSink = null_sink) -> dict:
    return RouterOS(log=log).apply(plan)


def remove_lease(mac: str, *, log: LogSink = null_sink) -> bool:
    return RouterOS(log=log).remove_lease(mac)
