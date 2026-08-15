"""Customer-facing server operations."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import client_ip, current_customer, get_owned_server
from app.enums import ActorType, JobType, ServerState
from app.models import Customer, IPAssignment, Job, Server, Subscription
from app.schemas import (
    BandwidthSeries,
    IPAssignmentOut,
    JobOut,
    PowerRequest,
    RDNSUpdate,
    ReinstallRequest,
    RescueRequest,
    ServerDetailOut,
    ServerHealthOut,
    ServerOut,
)
from app.services import jobs as job_service
from app.services import provisioning
from app.services.audit import record_audit
from app.services.bandwidth import series_for_server
from app.services.dispatch import enqueue

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])

_POWER_JOB_TYPES = {
    "on": JobType.POWER_ON,
    "off": JobType.POWER_OFF,
    "cycle": JobType.POWER_CYCLE,
    "reset": JobType.POWER_RESET,
}


@router.get("", response_model=list[ServerOut])
def list_servers(
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> list[Server]:
    """Servers the caller currently subscribes to.

    Admins get their own subscriptions here, not the fleet — the fleet lives
    under /api/v1/admin so an admin's portal view is not silently different.
    """
    return list(
        db.execute(
            select(Server)
            .join(Subscription, Subscription.server_id == Server.id)
            .where(
                Subscription.customer_id == customer.id,
                Subscription.ended_at.is_(None),
            )
            .order_by(Server.serial)
        )
        .scalars()
        .all()
    )


@router.get("/{server_id}", response_model=ServerDetailOut)
def get_server(
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
) -> ServerDetailOut:
    detail = ServerDetailOut.model_validate(server)
    spec = server.hardware_spec or {}
    detail.drives = spec.get("drives", [])
    detail.nics = [
        # The BMC's own MAC list is not customer business beyond what is on
        # their machine; interface names and speeds are.
        {"name": n.get("name"), "speed_mbps": n.get("speed_mbps"), "link": n.get("link_status")}
        for n in spec.get("nics", [])
    ]
    detail.ip_addresses = _ip_assignments(db, server)
    detail.health = ServerHealthOut(
        status=server.health_status,
        checked_at=server.health_checked_at,
        subsystems=server.health_detail or {},
    )
    return detail


def _ip_assignments(db: Session, server: Server) -> list[IPAssignmentOut]:
    rows = (
        db.execute(
            select(IPAssignment)
            .where(IPAssignment.server_id == server.id, IPAssignment.released_at.is_(None))
            .order_by(IPAssignment.is_primary.desc(), IPAssignment.address)
        )
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        item = IPAssignmentOut.model_validate(row)
        item.gateway = str(row.block.gateway) if row.block and row.block.gateway else None
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


@router.post("/{server_id}/power", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def power_action(
    payload: PowerRequest,
    request: Request,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> JobOut:
    """Queue a power action. Returns immediately with a job to poll."""
    if ServerState(server.state) is ServerState.SUSPENDED and not customer.is_admin:
        raise HTTPException(
            status_code=409, detail="server is suspended; contact support"
        )

    job = _queue(
        db,
        request,
        customer,
        server,
        _POWER_JOB_TYPES[payload.action],
        payload={"action": payload.action},
        audit_action=f"server.power.{payload.action}",
    )
    return JobOut.model_validate(job)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@router.post(
    "/{server_id}/reinstall", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED
)
def reinstall(
    payload: ReinstallRequest,
    request: Request,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> JobOut:
    if not payload.confirm_data_loss:
        raise HTTPException(
            status_code=400,
            detail="set confirm_data_loss=true; reinstall destroys everything on the array",
        )
    try:
        job = provisioning.create_install_job(
            db,
            server,
            os_template_id=payload.os_template_id,
            hostname=payload.hostname,
            raid_level=payload.raid_level,
            ssh_key_ids=payload.ssh_key_ids,
            root_password=payload.root_password,
            customer=customer,
            actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _finalise(db, request, customer, server, job, "server.reinstall")
    return JobOut.model_validate(job)


@router.post("/{server_id}/rescue", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def rescue(
    payload: RescueRequest,
    request: Request,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> JobOut:
    """Boot into the rescue environment. Nothing on disk is touched."""
    try:
        job = provisioning.create_rescue_job(
            db,
            server,
            ssh_key_ids=payload.ssh_key_ids,
            customer=customer,
            actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _finalise(db, request, customer, server, job, "server.rescue")
    return JobOut.model_validate(job)


# ---------------------------------------------------------------------------
# Jobs, bandwidth, IPs
# ---------------------------------------------------------------------------


@router.get("/{server_id}/jobs", response_model=list[JobOut])
def server_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
) -> list[JobOut]:
    rows = (
        db.execute(
            select(Job)
            .where(Job.server_id == server.id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [JobOut.model_validate(r) for r in rows]


@router.get("/{server_id}/bandwidth", response_model=BandwidthSeries)
def bandwidth(
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
) -> BandwidthSeries:
    return series_for_server(db, server.id, period)


@router.get("/{server_id}/ips", response_model=list[IPAssignmentOut])
def server_ips(
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
) -> list[IPAssignmentOut]:
    return _ip_assignments(db, server)


@router.patch("/{server_id}/ips/{assignment_id}/rdns", response_model=IPAssignmentOut)
def set_rdns(
    assignment_id: uuid.UUID,
    payload: RDNSUpdate,
    request: Request,
    server: Server = Depends(get_owned_server),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> IPAssignmentOut:
    """Record a reverse DNS entry.

    Stored here and published by the DNS zone generator; the delegation itself
    has to exist upstream before this has any visible effect.
    """
    assignment = db.get(IPAssignment, assignment_id)
    if assignment is None or assignment.server_id != server.id or assignment.released_at:
        raise HTTPException(status_code=404, detail="address not assigned to this server")

    assignment.rdns = payload.rdns.strip() if payload.rdns else None
    db.add(assignment)
    record_audit(
        db,
        action="ip.rdns_updated",
        actor_type=ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="ip_assignment",
        target_id=str(assignment_id),
        source_ip=client_ip(request),
        detail={"address": str(assignment.address), "rdns": assignment.rdns},
    )
    db.commit()
    db.refresh(assignment)

    out = IPAssignmentOut.model_validate(assignment)
    out.gateway = (
        str(assignment.block.gateway) if assignment.block and assignment.block.gateway else None
    )
    return out


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _queue(
    db: Session,
    request: Request,
    customer: Customer,
    server: Server,
    job_type: JobType,
    *,
    payload: dict,
    audit_action: str,
):
    try:
        job, _ = job_service.create_job(
            db,
            job_type=job_type,
            server_id=server.id,
            payload=payload,
            requested_by_id=customer.id,
            requested_by_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        )
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _finalise(db, request, customer, server, job, audit_action)
    return job


def _finalise(
    db: Session,
    request: Request,
    customer: Customer,
    server: Server,
    job,
    audit_action: str,
) -> None:
    """Audit, commit, then dispatch.

    Dispatch happens strictly after the commit. Enqueuing first would let a
    worker pick up a job row that its own transaction cannot see yet.
    """
    record_audit(
        db,
        action=audit_action,
        actor_type=ActorType.ADMIN if customer.is_admin else ActorType.CUSTOMER,
        actor_id=customer.id,
        actor_label=customer.email,
        target_type="server",
        target_id=str(server.id),
        source_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        detail={"job_id": str(job.id), "job_type": job.type},
    )
    db.commit()
    db.refresh(job)
    enqueue(db, job)
