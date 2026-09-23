"""Driver construction.

Resolving a `Server` row into a live driver is the one place that touches the
secrets backend, so it is kept small and obvious.
"""

from __future__ import annotations

from app.config import settings
from app.drivers.base import BMCDriver, LogSink, null_sink
from app.drivers.fallback import FallbackDriver
from app.drivers.ipmi import IpmiDriver
from app.drivers.redfish import RedfishDriver
from app.models import Server
from app.secrets import BMCCredential, get_secrets_backend

PROTOCOLS = {"auto", "redfish", "ipmi"}


def protocol_for(server: Server) -> str:
    protocol = (server.bmc_protocol or settings.bmc_protocol or "auto").lower()
    return protocol if protocol in PROTOCOLS else "auto"


def credential_for(server: Server) -> BMCCredential:
    return get_secrets_backend().get_bmc_credential(server.cimc_credential_ref)


def get_driver(
    server: Server,
    *,
    log: LogSink = null_sink,
    interactive: bool = False,
) -> BMCDriver:
    """Build a driver for `server`, pulling credentials at call time.

    Credentials are fetched per call rather than cached on the model so a
    rotated password takes effect on the next job, not the next restart.

    `interactive` is for a person waiting on the answer: short timeouts, no
    Redfish retries. Jobs use the patient defaults.
    """
    credential = credential_for(server)
    protocol = protocol_for(server)

    ipmi = IpmiDriver(
        host=str(server.cimc_ip),
        credential=credential,
        log=log,
        port=server.ipmi_port,
        timeout=settings.bmc_status_timeout_seconds if interactive else None,
        # One retransmit at one-second spacing: a dead BMC fails in about two
        # seconds instead of twenty (ipmitool's defaults), measured.
        retransmit=(1, 1) if interactive else None,
    )
    if protocol == "ipmi":
        return ipmi

    redfish = RedfishDriver(
        host=str(server.cimc_ip),
        credential=credential,
        log=log,
        port=server.redfish_port,
        system_path=server.redfish_system_path,
        timeout=settings.bmc_status_timeout_seconds if interactive else None,
        retries=1 if interactive else None,
    )
    if protocol == "redfish":
        return redfish
    return FallbackDriver(ipmi, redfish, rich=redfish, log=log)


def get_ipmi_driver(server: Server, *, log: LogSink = null_sink) -> IpmiDriver:
    """IPMI specifically -- the serial console has no Redfish equivalent."""
    return IpmiDriver(
        host=str(server.cimc_ip),
        credential=credential_for(server),
        log=log,
        port=server.ipmi_port,
    )
