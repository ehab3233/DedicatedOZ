"""Admin portal API: fleet inventory, lifecycle, IPAM, abuse, audit."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import client_ip, current_admin
from app.enums import ActorType, JobType, ServerState
from app.models import (
    AbuseReport,
    AuditLog,
    Customer,
    IPAssignment,
    IPBlock,
    Job,
    JobLogEntry,
    OSTemplate,
    Server,
    Subscription,
)
from app.schemas import (
    AdminJobDetailOut,
    AdminJobLogEntryOut,
    AdminServerOut,
    IPAssignCreate,
    IPAssignmentOut,
    IPBlockCreate,
    IPBlockOut,
    JobOut,
    OSTemplateCreate,
    OSTemplateOut,
    ServerCreate,
    ServerDetailOut,
    ServerHealthOut,
    ServerStateChange,
    ServerUpdate,
    WipeRequest,
)
from app.secrets import SecretNotFoundError, get_secrets_backend
from app.security import normalise_mac
from app.services import jobs as job_service
from app.services import provisioning
from app.services.audit import record_audit
from app.services.dispatch import enqueue
from app.services.lifecycle import IllegalTransition, transition_server

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(current_admin)])


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------


@router.get("/servers", response_model=list[AdminServerOut])
def list_fleet(
    state: ServerState | None = None,
    datacenter: str | None = None,
    rack: str | None = None,
    db: Session = Depends(get_db),
) -> list[AdminServerOut]:
    query = select(Server).order_by(Server.datacenter, Server.rack, Server.rack_unit)
    if state:
        query = query.where(Server.state == state.value)
    if datacenter:
        query = query.where(Server.datacenter == datacenter)
    if rack:
        query = query.where(Server.rack == rack)
    return [_admin_server(db, s) for s in db.execute(query).scalars().all()]


@router.get("/servers/{server_id}", response_model=AdminServerOut)
def get_fleet_server(server_id: uuid.UUID, db: Session = Depends(get_db)) -> AdminServerOut:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _admin_server(db, server)


@router.post("/servers", response_model=AdminServerOut, status_code=status.HTTP_201_CREATED)
def create_server(
    payload: ServerCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminServerOut:
    """Rack a server into inventory.

    The credential ref must already resolve in the secrets backend; a server
    whose password nobody can look up is worse than one that is not registered.
    """
    try:
        ipaddress.ip_address(payload.cimc_ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="cimc_ip is not an IP address") from exc

    try:
        get_secrets_backend().get_bmc_credential(payload.cimc_credential_ref)
    except (SecretNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"credential ref does not resolve: {exc}"
        ) from exc

    if db.execute(select(Server).where(Server.serial == payload.serial)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="serial already registered")

    mac = payload.provisioning_mac
    if mac:
        try:
            mac = normalise_mac(mac)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    server = Server(
        **{**payload.model_dump(exclude={"prepare_bmc"}), "provisioning_mac": mac}
    )
    db.add(server)
    record_audit(
        db,
        action="server.created",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=payload.serial,
        source_ip=client_ip(request),
        detail={"cimc_ip": payload.cimc_ip},
    )
    db.commit()
    db.refresh(server)

    if payload.prepare_bmc:
        # Switch the CIMC's services on first; the inventory sync (which needs
        # Redfish) is queued by that job when it finishes. The PXE MAC comes
        # from the sync.
        _dispatch(db, JobType.BMC_SETUP, server, admin, payload={"inventory_after": True})
    else:
        _dispatch(db, JobType.INVENTORY_SYNC, server, admin)
    return _admin_server(db, server)


@router.patch("/servers/{server_id}", response_model=AdminServerOut)
def update_server(
    server_id: uuid.UUID,
    payload: ServerUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminServerOut:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("cimc_ip"):
        try:
            ipaddress.ip_address(changes["cimc_ip"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="cimc_ip is not an IP address") from exc
    elif "cimc_ip" in changes:
        changes.pop("cimc_ip")
    if "provisioning_mac" in changes and changes["provisioning_mac"]:
        try:
            changes["provisioning_mac"] = normalise_mac(changes["provisioning_mac"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    for field, value in changes.items():
        setattr(server, field, value)
    db.add(server)
    record_audit(
        db,
        action="server.updated",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(server)
    return _admin_server(db, server)


@router.post("/servers/{server_id}/state", response_model=AdminServerOut)
def change_state(
    server_id: uuid.UUID,
    payload: ServerStateChange,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminServerOut:
    """Force a lifecycle transition.

    Still goes through the state machine: an admin may move a server between
    legal states, not into arbitrary ones.
    """
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    if (
        payload.state is ServerState.IN_STOCK
        and server.last_wiped_at is None
        and ServerState(server.state) is not ServerState.RMA
    ):
        raise HTTPException(
            status_code=409,
            detail="server has never been wiped; run a wipe job before returning it to stock",
        )

    try:
        transition_server(
            db,
            server,
            payload.state,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            actor_label=admin.email,
            reason=payload.reason,
        )
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(server)
    return _admin_server(db, server)


@router.post(
    "/servers/{server_id}/inventory-sync",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def sync_inventory(
    server_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _dispatch(db, JobType.INVENTORY_SYNC, server, admin)


@router.post(
    "/servers/{server_id}/health-check",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def health_check(
    server_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return _dispatch(db, JobType.HEALTH_POLL, server, admin)


@router.post(
    "/servers/{server_id}/wipe", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED
)
def wipe(
    server_id: uuid.UUID,
    payload: WipeRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    """Destroy every byte of customer data on a server.

    Deliberately admin-only and deliberately not reachable from the customer
    portal: the on-demand wipe-with-certificate product is a later phase.
    """
    if not payload.confirm_data_loss:
        raise HTTPException(status_code=400, detail="set confirm_data_loss=true")

    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")

    try:
        job = provisioning.create_wipe_job(
            db, server, method=payload.method, requested_by=admin
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_audit(
        db,
        action="server.wipe",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={"method": payload.method, "job_id": str(job.id)},
    )
    db.commit()
    db.refresh(job)
    _enqueue(db, job)
    return job


@router.post("/servers/{server_id}/suspend", response_model=AdminServerOut)
def suspend(
    server_id: uuid.UUID,
    request: Request,
    reason: str = Query(default="abuse"),
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminServerOut:
    """One-click suspend.

    The lifecycle flip is immediate; the switch port shutdown is the part that
    actually stops traffic and is executed by the network automation hook. That
    hook is not wired to a switch model yet, so this records intent and the
    port must be downed by hand until it is. The audit entry says which.
    """
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    try:
        transition_server(
            db,
            server,
            ServerState.SUSPENDED,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            actor_label=admin.email,
            reason=reason,
        )
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    record_audit(
        db,
        action="server.suspended",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={
            "reason": reason,
            "switch_port": server.switch_port,
            "port_shutdown_automated": False,
        },
    )
    db.commit()
    db.refresh(server)
    return _admin_server(db, server)


@router.post("/servers/{server_id}/unsuspend", response_model=AdminServerOut)
def unsuspend(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> AdminServerOut:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    try:
        transition_server(
            db,
            server,
            ServerState.ACTIVE,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            actor_label=admin.email,
            reason="unsuspended",
        )
    except IllegalTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        action="server.unsuspended",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={"switch_port": server.switch_port, "port_shutdown_automated": False},
    )
    db.commit()
    db.refresh(server)
    return _admin_server(db, server)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobOut])
def list_all_jobs(
    state: str | None = None,
    server_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Job]:
    query = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if state:
        query = query.where(Job.state == state)
    if server_id:
        query = query.where(Job.server_id == server_id)
    return list(db.execute(query).scalars().all())


@router.get("/jobs/{job_id}", response_model=AdminJobDetailOut)
def get_job_detail(job_id: uuid.UUID, db: Session = Depends(get_db)) -> AdminJobDetailOut:
    """Full job log including raw BMC requests and responses.

    This is the 2am vMedia debugging view. Nothing is filtered except the
    credential fields the driver already redacted.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    entries = (
        db.execute(
            select(JobLogEntry)
            .where(JobLogEntry.job_id == job.id)
            .order_by(JobLogEntry.sequence)
        )
        .scalars()
        .all()
    )
    detail = AdminJobDetailOut.model_validate(job)
    detail.log = [AdminJobLogEntryOut.model_validate(e) for e in entries]
    detail.payload = {k: v for k, v in (job.payload or {}).items() if k != "_callback_token"}
    return detail


# ---------------------------------------------------------------------------
# IPAM
# ---------------------------------------------------------------------------


@router.get("/ip-blocks", response_model=list[IPBlockOut])
def list_ip_blocks(db: Session = Depends(get_db)) -> list[IPBlockOut]:
    assigned = dict(
        db.execute(
            select(IPAssignment.block_id, func.count())
            .where(IPAssignment.released_at.is_(None))
            .group_by(IPAssignment.block_id)
        ).all()
    )
    out = []
    for block in db.execute(select(IPBlock).order_by(IPBlock.cidr)).scalars().all():
        item = IPBlockOut.model_validate(block)
        network = ipaddress.ip_network(block.cidr, strict=False)
        item.total_hosts = max(network.num_addresses - 2, 1)
        item.assigned = assigned.get(block.id, 0)
        out.append(item)
    return out


@router.post("/ip-blocks", response_model=IPBlockOut, status_code=201)
def create_ip_block(
    payload: IPBlockCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> IPBlockOut:
    try:
        network = ipaddress.ip_network(payload.cidr, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"not a valid CIDR: {exc}") from exc
    if payload.gateway:
        try:
            gateway = ipaddress.ip_address(payload.gateway)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="gateway is not an IP") from exc
        if gateway not in network:
            raise HTTPException(status_code=400, detail="gateway is outside the block")
    if db.execute(select(IPBlock).where(IPBlock.cidr == str(network))).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="block already exists")

    block = IPBlock(
        **{**payload.model_dump(), "cidr": str(network), "version": network.version}
    )
    db.add(block)
    record_audit(
        db,
        action="ip_block.created",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="ip_block",
        target_id=str(network),
        source_ip=client_ip(request),
    )
    db.commit()
    db.refresh(block)
    item = IPBlockOut.model_validate(block)
    item.total_hosts = max(network.num_addresses - 2, 1)
    return item


@router.get("/ip-blocks/{block_id}/free")
def free_addresses(
    block_id: uuid.UUID,
    limit: int = Query(default=32, ge=1, le=256),
    db: Session = Depends(get_db),
) -> dict:
    """Unassigned addresses in a block.

    Network, broadcast and gateway are excluded. Computed live rather than
    tracked in a pool table, so it cannot drift from reality.
    """
    block = db.get(IPBlock, block_id)
    if block is None:
        raise HTTPException(status_code=404, detail="block not found")

    network = ipaddress.ip_network(block.cidr, strict=False)
    taken = {
        str(a)
        for a in db.execute(
            select(IPAssignment.address).where(
                IPAssignment.block_id == block_id, IPAssignment.released_at.is_(None)
            )
        )
        .scalars()
        .all()
    }
    if block.gateway:
        taken.add(str(block.gateway))

    free: list[str] = []
    candidates = network.hosts() if network.num_addresses > 2 else iter(network)
    for address in candidates:
        if str(address) not in taken:
            free.append(str(address))
        if len(free) >= limit:
            break

    return {
        "cidr": block.cidr,
        "total_hosts": max(network.num_addresses - 2, 1),
        "assigned": len(taken),
        "free_sample": free,
    }


@router.post("/servers/{server_id}/ips", response_model=IPAssignmentOut, status_code=201)
def assign_ip(
    server_id: uuid.UUID,
    payload: IPAssignCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> IPAssignmentOut:
    block_id, address, is_primary = payload.block_id, payload.address, payload.is_primary
    server = db.get(Server, server_id)
    block = db.get(IPBlock, block_id)
    if server is None or block is None:
        raise HTTPException(status_code=404, detail="server or block not found")

    network = ipaddress.ip_network(block.cidr, strict=False)
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="not an IP address") from exc
    if parsed not in network:
        raise HTTPException(status_code=400, detail=f"{address} is not inside {block.cidr}")

    clash = db.execute(
        select(IPAssignment).where(
            IPAssignment.address == address, IPAssignment.released_at.is_(None)
        )
    ).scalar_one_or_none()
    if clash:
        raise HTTPException(status_code=409, detail="address already assigned")

    subscription = db.execute(
        select(Subscription).where(
            Subscription.server_id == server_id, Subscription.ended_at.is_(None)
        )
    ).scalar_one_or_none()

    if is_primary:
        # Exactly one primary per server: it is what the installer configures.
        for existing in (
            db.execute(
                select(IPAssignment).where(
                    IPAssignment.server_id == server_id,
                    IPAssignment.released_at.is_(None),
                    IPAssignment.is_primary.is_(True),
                )
            )
            .scalars()
            .all()
        ):
            existing.is_primary = False
            db.add(existing)

    assignment = IPAssignment(
        block_id=block_id,
        address=address,
        prefix_len=network.prefixlen,
        server_id=server_id,
        customer_id=subscription.customer_id if subscription else None,
        is_primary=is_primary,
    )
    db.add(assignment)
    record_audit(
        db,
        action="ip.assigned",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server_id),
        source_ip=client_ip(request),
        detail={"address": address, "primary": is_primary},
    )
    db.commit()
    db.refresh(assignment)

    out = IPAssignmentOut.model_validate(assignment)
    out.gateway = str(block.gateway) if block.gateway else None
    return out


@router.delete("/ips/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def release_ip(
    assignment_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> None:
    """Release an address.

    Soft release: the row stays so the assignment history survives an abuse
    complaint about traffic from six months ago.
    """
    assignment = db.get(IPAssignment, assignment_id)
    if assignment is None or assignment.released_at:
        raise HTTPException(status_code=404, detail="assignment not found")
    assignment.released_at = datetime.now(UTC)
    db.add(assignment)
    record_audit(
        db,
        action="ip.released",
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="ip_assignment",
        target_id=str(assignment_id),
        detail={"address": str(assignment.address)},
    )
    db.commit()


# ---------------------------------------------------------------------------
# OS templates
# ---------------------------------------------------------------------------


@router.post("/os-templates", response_model=OSTemplateOut, status_code=201)
def create_template(
    payload: OSTemplateCreate,
    db: Session = Depends(get_db),
) -> OSTemplate:
    if db.execute(
        select(OSTemplate).where(OSTemplate.slug == payload.slug)
    ).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="slug already exists")
    template = OSTemplate(**payload.model_dump())
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


# ---------------------------------------------------------------------------
# Abuse and audit
# ---------------------------------------------------------------------------


@router.get("/abuse")
def list_abuse(
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(AbuseReport).order_by(AbuseReport.created_at.desc()).limit(200)
    if status_filter:
        query = query.where(AbuseReport.status == status_filter)
    return [
        {
            "id": str(r.id),
            "server_id": str(r.server_id) if r.server_id else None,
            "reported_ip": str(r.reported_ip) if r.reported_ip else None,
            "source": r.source,
            "category": r.category,
            "status": r.status,
            "due_at": r.due_at,
            "created_at": r.created_at,
        }
        for r in db.execute(query).scalars().all()
    ]


@router.get("/audit")
def list_audit(
    action: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
    if action:
        query = query.where(AuditLog.action == action)
    if target_id:
        query = query.where(AuditLog.target_id == target_id)
    return [
        {
            "timestamp": e.timestamp,
            "actor_type": e.actor_type,
            "actor_label": e.actor_label,
            "action": e.action,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "source_ip": str(e.source_ip) if e.source_ip else None,
            "detail": e.detail,
        }
        for e in db.execute(query).scalars().all()
    ]


@router.get("/summary")
def fleet_summary(db: Session = Depends(get_db)) -> dict:
    """Numbers for the admin dashboard."""
    by_state = dict(
        db.execute(select(Server.state, func.count()).group_by(Server.state)).all()
    )
    active_jobs = db.execute(
        select(func.count()).select_from(Job).where(Job.state.in_(["queued", "running"]))
    ).scalar_one()
    failed_recently = db.execute(
        select(func.count())
        .select_from(Job)
        .where(
            Job.state == "failed",
            Job.finished_at >= datetime.now(UTC) - timedelta(days=1),
        )
    ).scalar_one()
    unhealthy = db.execute(
        select(func.count())
        .select_from(Server)
        .where(Server.health_status.in_(["warning", "critical"]))
    ).scalar_one()
    open_abuse = db.execute(
        select(func.count()).select_from(AbuseReport).where(AbuseReport.status != "resolved")
    ).scalar_one()

    return {
        "servers_by_state": by_state,
        "total_servers": sum(by_state.values()),
        "active_jobs": active_jobs,
        "failed_jobs_24h": failed_recently,
        "unhealthy_servers": unhealthy,
        "open_abuse_reports": open_abuse,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_server(db: Session, server: Server) -> AdminServerOut:
    out = AdminServerOut.model_validate(
        {
            **ServerDetailOut.model_validate(server).model_dump(),
            "cimc_ip": str(server.cimc_ip),
            "cimc_credential_ref": server.cimc_credential_ref,
            "bmc_protocol": server.bmc_protocol,
            "ipmi_port": server.ipmi_port,
            "redfish_port": server.redfish_port,
            "cimc_firmware": server.cimc_firmware,
            "bios_version": server.bios_version,
            "rack": server.rack,
            "rack_unit": server.rack_unit,
            "switch_name": server.switch_name,
            "switch_port": server.switch_port,
            "customer_vlan": server.customer_vlan,
            "provisioning_mac": server.provisioning_mac,
            "last_wiped_at": server.last_wiped_at,
            "state_changed_at": server.state_changed_at,
            "bmc_prepared_at": server.bmc_prepared_at,
            "notes": server.notes,
        }
    )
    spec = server.hardware_spec or {}
    out.drives = spec.get("drives", [])
    out.nics = spec.get("nics", [])
    out.health = ServerHealthOut(
        status=server.health_status,
        checked_at=server.health_checked_at,
        subsystems=server.health_detail or {},
    )

    subscription = db.execute(
        select(Subscription).where(
            Subscription.server_id == server.id, Subscription.ended_at.is_(None)
        )
    ).scalar_one_or_none()
    if subscription:
        customer = db.get(Customer, subscription.customer_id)
        out.customer_email = customer.email if customer else None

    out.ip_addresses = []
    for assignment in (
        db.execute(
            select(IPAssignment).where(
                IPAssignment.server_id == server.id, IPAssignment.released_at.is_(None)
            )
        )
        .scalars()
        .all()
    ):
        item = IPAssignmentOut.model_validate(assignment)
        item.gateway = (
            str(assignment.block.gateway)
            if assignment.block and assignment.block.gateway
            else None
        )
        out.ip_addresses.append(item)
    return out


def _dispatch(
    db: Session, job_type: JobType, server: Server, admin: Customer, payload: dict | None = None
) -> Job:
    try:
        job, _ = job_service.create_job(
            db,
            job_type=job_type,
            server_id=server.id,
            payload=payload or {},
            requested_by_id=admin.id,
            requested_by_type=ActorType.ADMIN,
        )
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(job)
    _enqueue(db, job)
    return job


def _enqueue(db: Session, job: Job) -> None:
    enqueue(db, job)
