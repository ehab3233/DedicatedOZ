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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TypeVar

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
        for key in [k for k in _live if k[1] == server_id]:
            _live.pop(key, None)


def read_power_many(
    server_ids: list[uuid.UUID], *, fresh: bool = False, workers: int = 8
) -> dict[str, dict]:
    """Power state for a whole fleet page, read in parallel.

    Each server gets its own session and thread; the per-server cache still
    applies, so a page refreshing every thirty seconds costs one IPMI call
    per server per cache period, not per viewer.
    """
    from app.db import SessionLocal

    def one(server_id: uuid.UUID) -> dict | None:
        db = SessionLocal()
        try:
            server = db.get(Server, server_id)
            if server is None:
                return None
            return read_power(db, server, fresh=fresh)
        finally:
            db.close()

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(server_ids) or 1))) as pool:
        for server_id, result in zip(server_ids, pool.map(one, server_ids), strict=True):
            if result is not None:
                out[str(server_id)] = result
    return out


T = TypeVar("T")

#: (what, server id) -> (read at, value). Sensors, event log, BMC info.
_live: dict[tuple[str, uuid.UUID], tuple[float, object]] = {}


def cached(what: str, server_id: uuid.UUID, fn: Callable[[], T], *, fresh: bool = False) -> T:
    """Memoise a live BMC read for `settings.bmc_live_cache_seconds`.

    Several people with the same server page open share one IPMI session
    instead of each opening their own against a BMC that allows a handful.
    """
    key = (what, server_id)
    now = time.monotonic()
    with _lock:
        hit = _live.get(key)
        if hit and not fresh and now - hit[0] < settings.bmc_live_cache_seconds:
            return hit[1]  # type: ignore[return-value]
    value = fn()
    with _lock:
        _live[key] = (time.monotonic(), value)
    return value
