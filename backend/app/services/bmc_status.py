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
#: A read that failed quickly (a lost packet, not a dead BMC) is tried once
#: more after this pause before anything is reported.
RETRY_PAUSE = 1.0
#: server id -> (read at, result) of the last read that worked.
_last_good: dict[uuid.UUID, tuple[float, dict]] = {}


def _with_retry(fn: Callable[[], T]) -> T:
    """Call `fn`; on a fast BMC failure, once more after a pause.

    IPMI is UDP: one lost datagram is a failed command. A failure that took
    the whole timeout is a BMC that is not answering, and a second wait
    would only double the delay, so only quick failures are retried.
    """
    started = time.monotonic()
    try:
        return fn()
    except BMCError:
        if time.monotonic() - started >= settings.bmc_status_timeout_seconds:
            raise
        time.sleep(RETRY_PAUSE)
        return fn()


def read_power(db: Session, server: Server, *, fresh: bool = False) -> dict:
    now = time.monotonic()
    max_age = MIN_INTERVAL if fresh else settings.bmc_status_cache_seconds
    with _lock:
        hit = _cache.get(server.id)
        if hit and now - hit[0] < max_age:
            return {**hit[1], "cached": True}

    checked_at = datetime.now(UTC).isoformat()

    def read() -> tuple[str, str]:
        with get_driver(server, interactive=True) as driver:
            return driver.power_status().state, protocol_label(driver)

    try:
        state, via = _with_retry(read)
        result = {
            "state": state, "via": via, "checked_at": checked_at, "error": None,
            "stale": False, "last_seen": checked_at,
        }
        with _lock:
            _last_good[server.id] = (time.monotonic(), result)
        if server.last_power_state != state:
            server.last_power_state = state
            db.add(server)
            db.commit()
    except (BMCError, SecretNotFoundError, ValueError) as exc:
        with _lock:
            good = _last_good.get(server.id)
        if good and time.monotonic() - good[0] < settings.bmc_stale_after_seconds:
            # One missed poll is not an outage: show what we last knew, say
            # it is old, and keep trying.
            result = {**good[1], "checked_at": checked_at, "error": str(exc), "stale": True}
        else:
            result = {
                "state": "unknown", "via": None, "checked_at": checked_at, "error": str(exc),
                "stale": False, "last_seen": good[1]["last_seen"] if good else None,
            }

    with _lock:
        _cache[server.id] = (time.monotonic(), result)
    return {**result, "cached": False}


def forget(server_id: uuid.UUID) -> None:
    with _lock:
        _cache.pop(server_id, None)
        _last_good.pop(server_id, None)
        for key in [k for k in _live if k[1] == server_id]:
            _live.pop(key, None)
        for key in [k for k in _errors if k[1] == server_id]:
            _errors.pop(key, None)


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
#: (what, server id) -> (failed at, error). A BMC that just refused a session
#: is left alone for this long: a CIMC holds a session it never got to close
#: until its own timeout, and retrying every poll only adds to the pile.
_errors: dict[tuple[str, uuid.UUID], tuple[float, BMCError]] = {}
ERROR_HOLD_SECONDS = 15.0


def cached(what: str, server_id: uuid.UUID, fn: Callable[[], T], *, fresh: bool = False) -> T:
    """Memoise a live BMC read for `settings.bmc_live_cache_seconds`, and a
    failed one for `ERROR_HOLD_SECONDS`.

    Several people with the same server page open share one IPMI session
    instead of each opening their own against a BMC that allows a handful.
    """
    key = (what, server_id)
    now = time.monotonic()
    with _lock:
        hit = _live.get(key)
        if hit and not fresh and now - hit[0] < settings.bmc_live_cache_seconds:
            return hit[1]  # type: ignore[return-value]
        failed = _errors.get(key)
        if failed and not fresh and now - failed[0] < ERROR_HOLD_SECONDS:
            stale = _stale(key, failed[1], now)
            if stale is not None:
                return stale  # type: ignore[return-value]
            raise failed[1]
    try:
        value = _with_retry(fn)
    except BMCError as exc:
        with _lock:
            _errors[key] = (time.monotonic(), exc)
            stale = _stale(key, exc, time.monotonic())
        if stale is not None:
            return stale  # type: ignore[return-value]
        raise
    with _lock:
        _live[key] = (time.monotonic(), value)
        _errors.pop(key, None)
    return value


def _stale(key: tuple[str, uuid.UUID], exc: BMCError, now: float) -> dict | None:
    """The last good reading, marked stale, while it is recent enough to show.
    Caller holds the lock."""
    hit = _live.get(key)
    if not hit or not isinstance(hit[1], dict):
        return None
    if now - hit[0] >= settings.bmc_stale_after_seconds:
        return None
    return {**hit[1], "stale": True, "error": str(exc)}
