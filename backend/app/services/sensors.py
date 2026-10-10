"""Sensor reports: read from the BMC, kept in Redis, refreshed by the worker.

A full sensor read is slow (one IPMI round trip per sensor, then the CIMC's
utilisation object), so the panel does not make the person wait for it. The
worker reads every server on a timer and stores the report; the API serves
the stored one at once, and reads live only on "Read now" or when nothing is
stored yet.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import redis

from app.config import settings
from app.drivers.base import BMCError
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import credential_for
from app.drivers.ipmi import IpmiDriver, estimate_power
from app.drivers.redfish import RedfishDriver
from app.models import Server
from app.secrets import SecretNotFoundError

log = logging.getLogger(__name__)

KEY = "doz:sensors:{}"


def redfish_for(server: Server, *, timeout: int | None = None) -> RedfishDriver:
    return RedfishDriver(
        host=str(server.cimc_ip), credential=credential_for(server), port=server.redfish_port,
        system_path=server.redfish_system_path,
        timeout=timeout or settings.bmc_status_timeout_seconds, retries=1,
    )


def utilization(server: Server) -> tuple[dict | None, str | None]:
    """The CIMC's own CPU, memory and IO figures, and why not if not."""
    try:
        with CimcXmlApi(str(server.cimc_ip), credential_for(server), port=server.redfish_port,
                        timeout=10) as api:
            figures = api.server_utilization()
    except (BMCError, SecretNotFoundError, ValueError) as exc:
        return None, str(exc)
    if figures is None:
        return None, "this CIMC does not report serverUtilization"
    return figures, None


def collect(server: Server, ipmi: IpmiDriver,
            redfish: Callable[[], RedfishDriver] = None) -> dict:  # type: ignore[assignment]
    """One full report: sensors, power draw, utilisation, how it was read.

    IPMI first; when it is off or its key is wrong, the Redfish Thermal and
    Power resources carry most of the same readings.
    """
    try:
        readings = ipmi.sensors()
        report = {
            "sensors": readings,
            "power": ipmi.power_reading() or estimate_power(readings),
            "via": "ipmi",
            "fallback_reason": None,
        }
    except BMCError as ipmi_error:
        driver = (redfish or (lambda: redfish_for(server)))()
        try:
            readings = driver.sensors()
            report = {
                "sensors": readings,
                "power": driver.power_reading() or estimate_power(readings),
                "via": "redfish",
                "fallback_reason": str(ipmi_error),
            }
        except BMCError as redfish_error:
            raise BMCError(f"IPMI: {ipmi_error}. Redfish: {redfish_error}") from redfish_error
        finally:
            driver.close()
    figures, why_not = utilization(server)
    report.update({
        "utilization": figures,
        "utilization_error": why_not,
        "checked_at": datetime.now(UTC).isoformat(),
    })
    return report


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=2)


def store(server_id: uuid.UUID, report: dict) -> None:
    """Keep the report where every API process and the worker can see it."""
    try:
        _client().set(KEY.format(server_id), json.dumps(report),
                      ex=settings.sensors_cache_ttl_seconds)
    except redis.RedisError as exc:  # the live read still served; only the cache is lost
        log.warning("sensor report for %s not cached: %s", server_id, exc)


def load(server_id: uuid.UUID) -> dict | None:
    """The last report stored for this server, if it is recent enough to show."""
    try:
        raw = _client().get(KEY.format(server_id))
    except redis.RedisError as exc:
        log.warning("sensor cache unavailable: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def forget(server_id: uuid.UUID) -> None:
    try:
        _client().delete(KEY.format(server_id))
    except redis.RedisError:
        pass
