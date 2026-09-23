"""Serial-over-LAN console, bridged to the browser over a websocket.

    browser (xterm.js) <-- websocket --> this module <-- pty --> ipmitool sol activate
                                                                     |
                                                              RMCP+ / UDP 623
                                                                     |
                                                                   CIMC

Why a pseudo-terminal: ipmitool puts its stdin into raw mode and expects a
terminal on the other end. Given plain pipes it limps along, printing termios
errors; given a pty it behaves exactly as it does in an operator's shell,
byte-for-byte, which is what BIOS screens with cursor addressing need. OpenStack
Ironic runs ipmitool SOL under a pty for the same reason.

Wire protocol on the websocket:
    binary frames, both directions   terminal bytes
    text frames, server -> browser   JSON status: {"type": "status", "state": ...}
    text frames, browser -> server   ignored (reserved for control messages)
Keeping input binary-only means no control message can ever be mistaken for
keystrokes.

Authentication: the browser first POSTs for a ticket (normal bearer auth),
then opens the websocket with `?ticket=`. Tickets live for a minute and work
once, so the one that ends up in an nginx access log is useless. A session JWT
in the URL -- what this used to do -- would have been a credential in the log.

The CIMC has exactly one SOL slot. When it is taken, the browser is told
("busy") and can reconnect with `force=1`, which releases the slot first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import secrets as pysecrets
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.deps import current_customer, get_owned_server
from app.drivers import BMCError, get_ipmi_driver
from app.drivers.ipmi import IpmiDriver
from app.enums import ActorType, ServerState
from app.models import Customer, Server, Subscription
from app.services.audit import record_audit

router = APIRouter(prefix="/api/v1/console", tags=["console"])

#: ipmitool's escape character. Ctrl-] -- the telnet escape -- so nobody
#: types it by accident at the start of a line and drops their own session.
ESCAPE_CHAR = "\x1d"

#: ipmitool output -> console state for the UI. First match wins.
_MARKERS: list[tuple[bytes, str, str]] = [
    (b"SOL Session operational", "connected", ""),
    (b"SOL payload already active on another session", "busy",
     "Someone else has this server's serial console open."),
    (b"SOL payload disabled", "sol_disabled",
     "Serial-over-LAN is disabled on this BMC. An admin can run Prepare BMC."),
    (b"Unable to establish IPMI v2 / RMCP+ session", "ipmi_unreachable",
     "Could not open an IPMI session: wrong credentials, IPMI over LAN disabled "
     "on the CIMC, or UDP 623 blocked."),
    (b"Insufficient privilege level", "ipmi_unreachable",
     "The BMC user is not an IPMI administrator."),
]


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

_used_tickets: dict[str, float] = {}
_used_lock = threading.Lock()


def issue_ticket(customer_id: uuid.UUID, server_id: uuid.UUID) -> tuple[str, int]:
    ttl = settings.console_ticket_ttl_seconds
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "typ": "console",
            "sub": str(customer_id),
            "srv": str(server_id),
            "jti": pysecrets.token_urlsafe(12),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return token, ttl


def redeem_ticket(ticket: str, server_id: uuid.UUID) -> uuid.UUID | None:
    """Customer id for a valid, unused ticket for this server; else None."""
    try:
        claims = jwt.decode(
            ticket, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "jti"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "console" or claims.get("srv") != str(server_id):
        return None
    now = time.time()
    with _used_lock:
        for jti, expiry in list(_used_tickets.items()):
            if expiry < now:
                del _used_tickets[jti]
        if claims["jti"] in _used_tickets:
            return None
        _used_tickets[claims["jti"]] = float(claims["exp"])
    try:
        return uuid.UUID(claims["sub"])
    except ValueError:
        return None


@router.post("/{server_id}/ticket")
def console_ticket(
    server: Server = Depends(get_owned_server),
    customer: Customer = Depends(current_customer),
) -> dict:
    if ServerState(server.state) is ServerState.SUSPENDED and not customer.is_admin:
        raise HTTPException(status_code=409, detail="server is suspended; contact support")
    ticket, ttl = issue_ticket(customer.id, server.id)
    return {"ticket": ticket, "expires_in": ttl}


# ---------------------------------------------------------------------------
# Session setup (blocking DB work, run in a thread)
# ---------------------------------------------------------------------------


@dataclass
class ConsoleContext:
    customer_id: uuid.UUID
    customer_email: str
    is_admin: bool
    server_id: uuid.UUID
    serial: str
    driver: IpmiDriver


def _open_context(
    customer_id: uuid.UUID, server_id: uuid.UUID, force: bool
) -> ConsoleContext | None:
    """Re-check entitlement and load the BMC credential.

    Uses its own short-lived session: holding a pooled DB connection for the
    hours a console can stay open would starve the API.
    """
    with SessionLocal() as db:
        customer = db.get(Customer, customer_id)
        server = db.get(Server, server_id)
        if customer is None or not customer.is_active or server is None:
            return None
        if not customer.is_admin:
            owns = db.execute(
                select(Subscription.id).where(
                    Subscription.server_id == server_id,
                    Subscription.customer_id == customer.id,
                    Subscription.ended_at.is_(None),
                )
            ).scalar_one_or_none()
            if owns is None or ServerState(server.state) is ServerState.SUSPENDED:
                return None
        try:
            driver = get_ipmi_driver(server)
        except Exception:  # noqa: BLE001 - a missing credential is a refusal, not a crash
            return None
        record_audit(
            db,
            action="console.sol_opened",
            actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
            actor_id=customer.id,
            actor_label=customer.email,
            target_type="server",
            target_id=str(server_id),
            detail={"force": force},
        )
        db.commit()
        return ConsoleContext(
            customer_id=customer.id,
            customer_email=customer.email,
            is_admin=customer.is_admin,
            server_id=server.id,
            serial=server.serial,
            driver=driver,
        )


def _audit_close(ctx: ConsoleContext, detail: dict) -> None:
    with SessionLocal() as db:
        record_audit(
            db,
            action="console.sol_closed",
            actor_type=ActorType.ADMIN if ctx.is_admin else ActorType.CUSTOMER,
            actor_id=ctx.customer_id,
            actor_label=ctx.customer_email,
            target_type="server",
            target_id=str(ctx.server_id),
            detail=detail,
        )
        db.commit()


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


@router.websocket("/{server_id}/sol")
async def serial_console(
    websocket: WebSocket,
    server_id: uuid.UUID,
    ticket: str = Query(...),
    force: bool = Query(False),
) -> None:
    customer_id = redeem_ticket(ticket, server_id)
    if customer_id is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    ctx = await run_in_threadpool(_open_context, customer_id, server_id, force)
    if ctx is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    session = SolSession(websocket, ctx, force=force)
    try:
        await session.run()
    finally:
        await session.cleanup()
        await run_in_threadpool(_audit_close, ctx, session.summary())


class SolSession:
    def __init__(self, websocket: WebSocket, ctx: ConsoleContext, *, force: bool) -> None:
        self.ws = websocket
        self.ctx = ctx
        self.force = force
        self.proc: subprocess.Popen | None = None
        self.master: int | None = None
        self.started = time.monotonic()
        self.last_activity = self.started
        self.bytes_in = 0
        self.bytes_out = 0
        self.state = "connecting"
        self.reason = ""
        self._head = b""  # first bytes of ipmitool output, scanned for markers
        self._closed = False

    async def status(self, state: str, message: str = "") -> None:
        self.state = state
        if message:
            self.reason = message
        with contextlib.suppress(Exception):
            await self.ws.send_text(
                json.dumps({"type": "status", "state": state, "message": message})
            )

    def summary(self) -> dict:
        return {
            "duration_s": int(time.monotonic() - self.started),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "final_state": self.state,
            "reason": self.reason,
        }

    async def run(self) -> None:
        await self.status("connecting", f"Opening serial console on {self.ctx.serial}...")
        if self.force:
            await self.status("connecting", "Releasing the existing session...")
            await run_in_threadpool(self.ctx.driver.sol_deactivate)

        master, slave = pty.openpty()
        cmd = [*self.ctx.driver.base_command(), "-e", ESCAPE_CHAR, "sol", "activate"]
        try:
            self.proc = subprocess.Popen(  # noqa: S603 - argv, no shell
                cmd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self.ctx.driver.environment(),
                start_new_session=True,
                close_fds=True,
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            await self.status("error", "ipmitool is not installed on the management server.")
            return
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master

        loop = asyncio.get_running_loop()
        output: asyncio.Queue[bytes | None] = asyncio.Queue()

        def readable() -> None:
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""  # EIO: the child closed its end
            if not data:
                loop.remove_reader(master)
                output.put_nowait(None)
                return
            output.put_nowait(data)

        loop.add_reader(master, readable)

        tasks = [
            asyncio.create_task(self._pump_out(output)),
            asyncio.create_task(self._pump_in()),
            asyncio.create_task(self._watchdog()),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _pump_out(self, output: asyncio.Queue[bytes | None]) -> None:
        while True:
            data = await output.get()
            if data is None:
                code = self.proc.poll() if self.proc else None
                if self.state not in {"busy", "sol_disabled", "ipmi_unreachable"}:
                    await self.status("closed", f"Console session ended (ipmitool exited {code}).")
                return
            self.last_activity = time.monotonic()
            self.bytes_out += len(data)
            if self.state == "connecting":
                await self._scan(data)
            await self.ws.send_bytes(data)

    async def _scan(self, data: bytes) -> None:
        self._head = (self._head + data)[-4096:]
        for marker, state, message in _MARKERS:
            if marker in self._head:
                await self.status(state, message)
                return

    async def _pump_in(self) -> None:
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                self.reason = self.reason or "browser disconnected"
                return
            data = message.get("bytes")
            if not data:
                continue  # text frames are control messages; none are defined yet
            self.last_activity = time.monotonic()
            self.bytes_in += len(data)
            await self._write(data)

    async def _write(self, data: bytes) -> None:
        assert self.master is not None
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.master, view)
                view = view[written:]
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                return

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if now - self.last_activity > settings.sol_idle_timeout_seconds:
                await self.status(
                    "closed",
                    f"Closed after {settings.sol_idle_timeout_seconds // 60} minutes with no "
                    "activity, to free the BMC's console for others.",
                )
                return
            if now - self.started > settings.sol_max_session_seconds:
                await self.status("closed", "Maximum console session length reached.")
                return

    async def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        if self.master is not None:
            with contextlib.suppress(Exception):
                loop.remove_reader(self.master)
            with contextlib.suppress(OSError):
                os.close(self.master)
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            for _ in range(30):
                if self.proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if self.proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        # Killing ipmitool does not always tell the BMC; without an explicit
        # deactivate the slot stays taken and the next person is refused.
        # Skipped when we never got the slot in the first place, so we do not
        # knock someone else's working session off.
        if self.state not in {"busy", "ipmi_unreachable", "error"}:
            with contextlib.suppress(BMCError):
                await run_in_threadpool(self.ctx.driver.sol_deactivate)
        with contextlib.suppress(Exception):
            await self.ws.close()
