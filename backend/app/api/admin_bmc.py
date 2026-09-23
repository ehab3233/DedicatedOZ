"""Admin API for the BMC itself: vKVM launch, BMC preparation, system health."""

from __future__ import annotations

import shutil
import subprocess
import uuid

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import client_ip, current_admin
from app.drivers import BMCError
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import credential_for, protocol_for
from app.drivers.ipmi import ipmitool_path
from app.enums import ActorType, JobType
from app.models import Customer, Job, Server
from app.schemas import JobOut
from app.secrets import SecretNotFoundError
from app.services import jobs as job_service
from app.services.audit import record_audit
from app.services.dispatch import enqueue

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(current_admin)])

#: Queues the platform routes work to, and what breaks if nobody consumes one.
QUEUES = {
    "power": "power actions, Prepare BMC",
    "provision": "reinstall, rescue, wipe",
    "poll": "inventory sync, health checks, scheduled sweeps",
}


def _server(db: Session, server_id: uuid.UUID) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return server


@router.post("/servers/{server_id}/kvm")
def launch_kvm(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """One-time vKVM launch links for the CIMC.

    The browser opening these must be able to reach the CIMC directly -- true
    on a flat network. The tokens are single-use and expire in about a
    minute, so the panel opens the link immediately.
    """
    server = _server(db, server_id)
    try:
        credential = credential_for(server)
    except (SecretNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"no CIMC credential: {exc}") from exc

    try:
        with CimcXmlApi(str(server.cimc_ip), credential, port=server.redfish_port,
                        timeout=15) as api:
            links = api.kvm_launch()
            firmware = api.version
    except BMCError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"could not get KVM tokens from the CIMC: {exc}. "
                "The CIMC web UI link still works if you log in by hand."
            ),
        ) from exc

    record_audit(
        db,
        action="console.kvm_launched",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={"html5_found": bool(links["html5"])},
    )
    db.commit()
    return {**links, "firmware": firmware}


@router.post(
    "/servers/{server_id}/prepare-bmc",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def prepare_bmc(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    """Queue a job that switches on IPMI over LAN and SOL, and proves both."""
    server = _server(db, server_id)
    try:
        job, _ = job_service.create_job(
            db,
            job_type=JobType.BMC_SETUP,
            server_id=server.id,
            requested_by_id=admin.id,
            requested_by_type=ActorType.ADMIN,
        )
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        action="server.prepare_bmc",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
    )
    db.commit()
    db.refresh(job)
    return enqueue(db, job)


@router.get("/system")
def system_status(db: Session = Depends(get_db)) -> dict:
    """Is everything the panel depends on actually running?

    The question this answers is "I clicked restart and nothing happened --
    why?" Usually: no worker is consuming the power queue.
    """
    out: dict = {
        "database": "ok",
        "redis": "ok",
        "workers": [],
        "queues": {},
        "ipmitool": None,
        "bmc_protocol": settings.bmc_protocol,
        "ipmi_cipher_suite": settings.ipmi_cipher_suite or "auto",
        "secrets_backend": settings.secrets_backend,
    }
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        out["database"] = f"error: {exc}"

    try:
        redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
    except Exception as exc:  # noqa: BLE001
        out["redis"] = f"error: {exc}"

    consumers: dict[str, list[str]] = {q: [] for q in QUEUES}
    if out["redis"] == "ok":
        try:
            from app.workers.celery_app import celery_app

            active = celery_app.control.inspect(timeout=1.0).active_queues() or {}
            for node, queues in active.items():
                names = sorted(q["name"] for q in queues)
                out["workers"].append({"name": node, "queues": names})
                for name in names:
                    consumers.setdefault(name, []).append(node)
        except Exception as exc:  # noqa: BLE001
            out["workers_error"] = str(exc)
    out["queues"] = {
        name: {"workers": consumers.get(name, []), "handles": QUEUES[name]} for name in QUEUES
    }

    path = shutil.which(ipmitool_path())
    if path:
        try:
            version = subprocess.run(  # noqa: S603
                [path, "-V"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            version = "unknown"
        out["ipmitool"] = {"path": path, "version": version}
    return out


@router.get("/servers/{server_id}/bmc")
def bmc_settings(server_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """How the platform will talk to this server's BMC."""
    server = _server(db, server_id)
    try:
        credential = credential_for(server)
        credential_ok, username = True, credential.username
    except (SecretNotFoundError, ValueError):
        credential_ok, username = False, None
    return {
        "protocol": protocol_for(server),
        "ipmi_port": server.ipmi_port or settings.ipmi_port,
        "redfish_port": server.redfish_port or 443,
        "credential_ref": server.cimc_credential_ref,
        "credential_resolves": credential_ok,
        "username": username,
    }
