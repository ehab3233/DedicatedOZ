"""Live power state for the panel.

A person looking at a server page wants to know if it is on *now*, not what
the last job said. This reads it directly -- over IPMI first, which answers in
tens of milliseconds -- with short timeouts, and caches the answer for a few
seconds so a page full of polling tabs cannot hammer a BMC that allows only a
handful of sessions.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.drivers import BMCError, get_driver
from app.drivers.fallback import protocol_label
from app.models import Server
from app.secrets import SecretNotFoundError

_cache: dict[uuid.UUID, tuple[float, dict]] = {}
_lock = threading.Lock()

#: Even a forced refresh waits this long between real reads of one BMC.
MIN_INTERVAL = 2.0


def read_power(db: Session, server: Server, *, fresh: bool = False) -> dict:
    now = time.monotonic()
    max_age = MIN_INTERVAL if fresh else settings.bmc_status_cache_seconds
    with _lock:
        hit = _cache.get(server.id)
        if hit and now - hit[0] < max_age:
            return {**hit[1], "cached": True}

    checked_at = datetime.now(UTC).isoformat()
    try:
        with get_driver(server, interactive=True) as driver:
            state = driver.power_status().state
            via = protocol_label(driver)
        result = {"state": state, "via": via, "checked_at": checked_at, "error": None}
        if server.last_power_state != state:
            server.last_power_state = state
            db.add(server)
            db.commit()
    except (BMCError, SecretNotFoundError, ValueError) as exc:
        result = {"state": "unknown", "via": None, "checked_at": checked_at, "error": str(exc)}

    with _lock:
        _cache[server.id] = (time.monotonic(), result)
    return {**result, "cached": False}


def forget(server_id: uuid.UUID) -> None:
    with _lock:
        _cache.pop(server_id, None)
