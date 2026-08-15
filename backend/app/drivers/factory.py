"""Driver construction.

Resolving a `Server` row into a live driver is the one place that touches the
secrets backend, so it is kept small and obvious.
"""

from __future__ import annotations

from app.drivers.base import BMCDriver, LogSink, null_sink
from app.drivers.redfish import RedfishDriver
from app.models import Server
from app.secrets import get_secrets_backend


def get_driver(server: Server, *, log: LogSink = null_sink) -> BMCDriver:
    """Build a driver for `server`, pulling credentials at call time.

    Credentials are fetched per call rather than cached on the model so a
    rotated password takes effect on the next job, not the next restart.
    """
    credential = get_secrets_backend().get_bmc_credential(server.cimc_credential_ref)
    return RedfishDriver(
        host=str(server.cimc_ip),
        credential=credential,
        log=log,
        system_path=server.redfish_system_path,
    )
