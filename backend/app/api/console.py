"""Serial-over-LAN console, bridged to the browser over a websocket.

`ipmitool sol activate` is spawned on the worker side of the network boundary
and its stdio is pumped across a websocket. The browser never learns the CIMC
address or its credentials.

The CIMC allows exactly one SOL session per server. If a session is already
open, `ipmitool` says so and exits; that message is forwarded verbatim rather
than being turned into an opaque failure, because "someone else has the
console" is a thing the customer can act on.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.enums import ActorType
from app.models import Customer, Server, Subscription
from app.secrets import get_secrets_backend
from app.security import decode_access_token
from app.services.audit import record_audit

router = APIRouter(prefix="/api/v1/console", tags=["console"])


def _authorise(db: Session, token: str, server_id: uuid.UUID) -> tuple[Customer, Server] | None:
    """Resolve a websocket token to a customer entitled to this console.

    The token arrives as a query parameter: browsers cannot set headers on a
    websocket handshake. It is a normal short-lived access token, so the
    exposure is the same as any URL-borne credential — which is why the SOL
    endpoint is the only place it is accepted this way.
    """
    try:
        payload = decode_access_token(token)
        customer_id = uuid.UUID(payload["sub"])
    except Exception:  # noqa: BLE001 - any decode failure is simply a refusal
        return None

    customer = db.get(Customer, customer_id)
    server = db.get(Server, server_id)
    if customer is None or not customer.is_active or server is None:
        return None
    if customer.is_admin:
        return customer, server

    owns = db.execute(
        select(Subscription.id).where(
            Subscription.server_id == server_id,
            Subscription.customer_id == customer.id,
            Subscription.ended_at.is_(None),
        )
    ).scalar_one_or_none()
    return (customer, server) if owns else None


@router.websocket("/{server_id}/sol")
async def serial_console(
    websocket: WebSocket,
    server_id: uuid.UUID,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> None:
    authorised = _authorise(db, token, server_id)
    if authorised is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    customer, server = authorised

    credential = get_secrets_backend().get_bmc_credential(server.cimc_credential_ref)
    await websocket.accept()

    record_audit(
        db,
        action="console.sol_opened",
        actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="server",
        target_id=str(server_id),
    )
    db.commit()

    # -I lanplus is required for IPMI 2.0; -e sets the escape character to one
    # a customer will not type by accident. The password goes through -E and
    # the environment rather than -P: an argv password is readable by every
    # process on the host.
    process = await asyncio.create_subprocess_exec(
        settings.ipmitool_path,
        "-I", "lanplus",
        "-H", str(server.cimc_ip),
        "-U", credential.username,
        "-E",
        "-e", "&",
        "sol", "activate",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={"IPMI_PASSWORD": credential.password, "PATH": os.environ.get("PATH", "")},
    )

    async def pump_out() -> None:
        assert process.stdout is not None
        while True:
            chunk = await process.stdout.read(1024)
            if not chunk:
                break
            await websocket.send_bytes(chunk)

    async def pump_in() -> None:
        assert process.stdin is not None
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is None and message.get("text") is not None:
                data = message["text"].encode()
            if data:
                process.stdin.write(data)
                await process.stdin.drain()

    outbound = asyncio.create_task(pump_out())
    inbound = asyncio.create_task(pump_in())

    try:
        # Either direction ending tears the session down: a half-open console
        # would hold the CIMC's single SOL slot indefinitely.
        done, pending = await asyncio.wait(
            [outbound, inbound],
            timeout=settings.sol_idle_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        # `sol deactivate` matters as much as killing the process: without it
        # the CIMC keeps the slot open and the next connect is refused.
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        await _deactivate_sol(server, credential)
        with contextlib.suppress(RuntimeError):
            await websocket.close()


async def _deactivate_sol(server: Server, credential) -> None:  # noqa: ANN001
    deactivate = await asyncio.create_subprocess_exec(
        settings.ipmitool_path,
        "-I", "lanplus",
        "-H", str(server.cimc_ip),
        "-U", credential.username,
        "-E",
        "sol", "deactivate",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={"IPMI_PASSWORD": credential.password, "PATH": os.environ.get("PATH", "")},
    )
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(deactivate.wait(), timeout=10)
