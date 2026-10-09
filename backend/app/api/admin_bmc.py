"""Admin API for the BMC itself: vKVM launch, BMC preparation, system health."""

from __future__ import annotations

import secrets
import shutil
import string
import subprocess
import uuid
from datetime import UTC, datetime

import redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import client_ip, current_admin
from app.drivers import BMCError, get_driver
from app.drivers.cimc import CimcXmlApi
from app.drivers.factory import cipher_for, credential_for, protocol_for
from app.drivers.ipmi import IpmiDriver, ipmitool_path
from app.drivers.redfish import RedfishDriver
from app.enums import ActorType, JobType, ServerState
from app.models import Customer, Image, Job, Server
from app.schemas import (
    BmcPasswordRequest,
    BootOverrideRequest,
    IdentifyRequest,
    JobOut,
    PowerPolicyRequest,
    RaidConfigureRequest,
    VmediaBootRequest,
)
from app.secrets import BMCCredential, SecretNotFoundError, get_secrets_backend
from app.services import bmc_status, bmc_test, images
from app.services import jobs as job_service
from app.services.audit import record_audit
from app.services.dispatch import enqueue

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(current_admin)])

#: Queues the platform routes work to, and what breaks if nobody consumes one.
QUEUES = {
    "power": "power actions, boot device, virtual media, Prepare BMC",
    "provision": "reinstall, rescue, wipe, image downloads",
    "poll": "inventory sync, health checks, scheduled sweeps",
}


def _server(db: Session, server_id: uuid.UUID) -> Server:
    server = db.get(Server, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    return server


def _ipmi(server: Server, *, timeout: int | None = None) -> IpmiDriver:
    """An IPMI driver tuned for someone waiting on the answer.

    Short retransmits, so a dead BMC comes back as an error in a couple of
    seconds instead of twenty. Credential problems surface as a 400 with the
    reason; BMC problems as a 502.
    """
    try:
        credential = credential_for(server)
    except (SecretNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"no CIMC credential: {exc}") from exc
    return IpmiDriver(
        host=str(server.cimc_ip),
        credential=credential,
        port=server.ipmi_port,
        cipher_suite=cipher_for(server),
        timeout=timeout or settings.bmc_status_timeout_seconds,
        retransmit=(1, 1),
    )


def _bmc_call(fn):  # noqa: ANN001
    """Run a live BMC read for an endpoint, mapping driver failures to HTTP."""
    try:
        return fn()
    except BMCError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _audit(db: Session, request: Request, admin: Customer, server: Server, action: str,
           detail: dict | None = None) -> None:
    record_audit(
        db,
        action=action,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        actor_label=admin.email,
        target_type="server",
        target_id=str(server.id),
        source_ip=client_ip(request),
        detail=detail,
    )


def _queue(db: Session, request: Request, admin: Customer, server: Server, job_type: JobType,
           *, payload: dict, audit_action: str) -> Job:
    try:
        job, _ = job_service.create_job(
            db,
            job_type=job_type,
            server_id=server.id,
            payload=payload,
            requested_by_id=admin.id,
            requested_by_type=ActorType.ADMIN,
        )
    except job_service.JobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(db, request, admin, server, audit_action, {**payload, "job_id": str(job.id)})
    db.commit()
    db.refresh(job)
    bmc_status.forget(server.id)
    return enqueue(db, job)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        detail={"html5_found": bool(links["html5"]),
                "tokens_unsupported": bool(links.get("tokens_unsupported"))},
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
        "ipmi_cipher_suite": bmc_test.effective_cipher(server) or "auto",
        "cipher_source": "server" if server.ipmi_cipher_suite else "platform",
        "credential_ref": server.cimc_credential_ref,
        "credential_resolves": credential_ok,
        "username": username,
    }


@router.post("/servers/{server_id}/bmc/test")
def test_bmc(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Try every way the platform talks to this BMC and report what each
    said. A cipher suite that works where the configured one does not is
    remembered for this server."""
    server = _server(db, server_id)
    try:
        credential = credential_for(server)
    except (SecretNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"no CIMC credential: {exc}") from exc
    report = bmc_test.run(server, credential)
    report["cipher_saved"] = False
    working = report["working_cipher"]
    if working is not None and working != report["configured_cipher"]:
        server.ipmi_cipher_suite = working
        db.add(server)
        bmc_status.forget(server.id)
        report["cipher_saved"] = True
    _audit(db, request, admin, server, "server.bmc_test",
           detail={"ok": report["ok"], "working_cipher": working})
    db.commit()
    return report


# ---------------------------------------------------------------------------
# Live readings: sensors, event log, the BMC itself
# ---------------------------------------------------------------------------


@router.get("/power")
def fleet_power(fresh: bool = Query(default=False), db: Session = Depends(get_db)) -> dict:
    """Live power state of every server, for the fleet page. Read in parallel."""
    ids = (
        db.execute(
            select(Server.id).where(Server.state != ServerState.RETIRED.value)
        )
        .scalars()
        .all()
    )
    return {"servers": bmc_status.read_power_many(list(ids), fresh=fresh), "checked_at": _now()}


@router.get("/servers/{server_id}/sensors")
def sensors(
    server_id: uuid.UUID, fresh: bool = Query(default=False), db: Session = Depends(get_db)
) -> dict:
    """Every sensor the BMC has, with its reading now: temperatures, fans,
    voltages, power supplies, plus the DCMI power draw where the BMC has it."""
    server = _server(db, server_id)
    driver = _ipmi(server, timeout=45)

    def read() -> dict:
        readings = driver.sensors()
        return {
            "sensors": readings,
            "power": driver.power_reading(),
            "checked_at": _now(),
            "via": "ipmi",
        }

    return _bmc_call(lambda: bmc_status.cached("sensors", server.id, read, fresh=fresh))


@router.get("/servers/{server_id}/sel")
def event_log(
    server_id: uuid.UUID, fresh: bool = Query(default=False), db: Session = Depends(get_db)
) -> dict:
    """The System Event Log, newest first."""
    server = _server(db, server_id)
    driver = _ipmi(server, timeout=45)

    def read() -> dict:
        entries = driver.sel_entries()
        entries.reverse()
        return {"info": driver.sel_info(), "entries": entries, "checked_at": _now()}

    return _bmc_call(lambda: bmc_status.cached("sel", server.id, read, fresh=fresh))


@router.delete("/servers/{server_id}/sel")
def clear_event_log(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    server = _server(db, server_id)
    driver = _ipmi(server)
    before = _bmc_call(driver.sel_info)
    _bmc_call(driver.sel_clear)
    _audit(db, request, admin, server, "server.sel_cleared", {"entries": before["entries"]})
    db.commit()
    bmc_status.forget(server.id)
    return {"cleared": True, "entries_removed": before["entries"]}


@router.get("/servers/{server_id}/bmc/info")
def bmc_info(
    server_id: uuid.UUID, fresh: bool = Query(default=False), db: Session = Depends(get_db)
) -> dict:
    """What the BMC says about itself: firmware, its network settings, chassis
    state and the power-restore policy, and which IPMI users exist."""
    server = _server(db, server_id)
    driver = _ipmi(server)

    def read() -> dict:
        return {
            "mc": driver.mc_info(),
            "lan": driver.lan_config(),
            "chassis": driver.chassis_status(),
            "users": [u for u in driver.users() if u["name"]],
            "checked_at": _now(),
        }

    return _bmc_call(lambda: bmc_status.cached("bmc_info", server.id, read, fresh=fresh))


# ---------------------------------------------------------------------------
# Immediate actions on the BMC
# ---------------------------------------------------------------------------


@router.post("/servers/{server_id}/identify")
def identify(
    server_id: uuid.UUID,
    payload: IdentifyRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Blink the chassis locator LED so remote hands can find the box."""
    server = _server(db, server_id)
    driver = _ipmi(server)
    seconds = min(payload.seconds, 255)
    _bmc_call(lambda: driver.identify(seconds, force=payload.force))
    state = "on" if payload.force else "off" if seconds == 0 else f"{seconds}s"
    _audit(db, request, admin, server, "server.identify", {"led": state})
    db.commit()
    return {"led": state}


@router.post("/servers/{server_id}/bmc/reset")
def reset_bmc(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Cold-reset the BMC. The host keeps running; IPMI, Redfish, the web UI
    and the consoles drop for a minute or two while it reboots."""
    server = _server(db, server_id)
    driver = _ipmi(server)
    via = "ipmi"
    try:
        driver.bmc_reset("cold")
    except BMCError as ipmi_error:
        # The usual reason to reset a BMC is that IPMI has stopped answering,
        # so the reset itself must not depend on it.
        redfish = RedfishDriver(
            host=str(server.cimc_ip), credential=credential_for(server),
            port=server.redfish_port, timeout=settings.bmc_status_timeout_seconds, retries=1,
        )
        try:
            redfish.manager_reset()
            via = "redfish"
        except BMCError as redfish_error:
            raise HTTPException(
                status_code=502,
                detail=f"IPMI: {ipmi_error}. Redfish: {redfish_error}",
            ) from redfish_error
    _audit(db, request, admin, server, "server.bmc_reset", detail={"via": via})
    db.commit()
    bmc_status.forget(server.id)
    return {"reset": "cold", "via": via,
            "note": "the BMC is rebooting; expect it back in one to two minutes"}


@router.post("/servers/{server_id}/bmc/power-policy")
def set_power_policy(
    server_id: uuid.UUID,
    payload: PowerPolicyRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """What the server does when mains power returns."""
    server = _server(db, server_id)
    driver = _ipmi(server)
    _bmc_call(lambda: driver.set_power_restore_policy(payload.policy))
    chassis = _bmc_call(driver.chassis_status)
    _audit(db, request, admin, server, "server.power_policy", {"policy": payload.policy})
    db.commit()
    bmc_status.forget(server.id)
    return {"policy": chassis.get("power_restore_policy", payload.policy), "chassis": chassis}


def generate_bmc_password() -> str:
    """16 characters with upper, lower, digit and a symbol the CIMC accepts."""
    alphabet = string.ascii_letters + string.digits
    while True:
        body = "".join(secrets.choice(alphabet) for _ in range(13))
        if any(c.islower() for c in body) and any(c.isupper() for c in body):
            break
    return body + secrets.choice(string.digits) + secrets.choice("-_.") + secrets.choice(
        string.ascii_uppercase
    )


@router.post("/servers/{server_id}/bmc/password")
def rotate_bmc_password(
    server_id: uuid.UUID,
    payload: BmcPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> dict:
    """Change the IPMI password the platform uses for this server.

    The order is what makes this safe: refuse unless the new password can be
    stored; change it on the BMC; prove a fresh session works with it; store
    it. The password is returned in the response, once, so that a store that
    fails after a successful change on the BMC does not lock anyone out.
    """
    server = _server(db, server_id)
    backend = get_secrets_backend()
    if settings.secrets_backend.lower() not in {"file", "vault"}:
        raise HTTPException(
            status_code=409,
            detail="the env secrets backend is read-only, so a new password could not be "
                   "stored; rotate it by hand and update DOZ_CIMC_*_PASS",
        )
    driver = _ipmi(server)
    username = driver._cred.username  # noqa: SLF001 - same module family
    user_id = _bmc_call(lambda: driver.user_id_for(username))
    if user_id is None:
        raise HTTPException(
            status_code=502, detail=f"the BMC has no IPMI user named {username!r} on channel 1"
        )
    new_password = payload.password or generate_bmc_password()
    _bmc_call(lambda: driver.set_user_password(user_id, new_password))

    verify = IpmiDriver(
        host=str(server.cimc_ip), credential=BMCCredential(username, new_password),
        port=server.ipmi_port, timeout=15, retransmit=(1, 1),
        cipher_suite=cipher_for(server),
    )
    error: str | None = None
    try:
        verify.power_status()
        verified = True
    except BMCError as exc:
        verified, error = False, str(exc)

    stored = False
    if verified:
        try:
            backend.put_bmc_credential(
                server.cimc_credential_ref, BMCCredential(username, new_password)
            )
            stored = True
        except Exception as exc:  # noqa: BLE001 - the password must still reach the operator
            error = f"changed on the BMC but not stored: {exc}"

    _audit(db, request, admin, server, "server.bmc_password_rotated",
           {"username": username, "verified": verified, "stored": stored})
    db.commit()
    bmc_status.forget(server.id)
    return {
        "username": username,
        "password": new_password,
        "verified": verified,
        "stored": stored,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Boot device and virtual media (jobs)
# ---------------------------------------------------------------------------


@router.post("/servers/{server_id}/boot", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED)
def boot_override(
    server_id: uuid.UUID,
    payload: BootOverrideRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    """One-time boot from PXE, disk, CD/virtual media or into BIOS setup."""
    server = _server(db, server_id)
    return _queue(
        db, request, admin, server, JobType.BOOT_OVERRIDE,
        payload={"device": payload.device, "then": payload.then},
        audit_action="server.boot_override",
    )


@router.post("/servers/{server_id}/raid", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED)
def configure_raid(
    server_id: uuid.UUID,
    payload: RaidConfigureRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    """Build a virtual drive of the given level on the server's RAID
    controller through its BMC, replacing whatever is there; level none
    deletes every virtual drive. Destroys the data on those disks."""
    if not payload.confirm_data_loss:
        raise HTTPException(
            status_code=400,
            detail="set confirm_data_loss=true; rebuilding the array destroys everything on it",
        )
    server = _server(db, server_id)
    return _queue(
        db, request, admin, server, JobType.RAID_CONFIGURE,
        payload={"level": payload.level.value}, audit_action="server.raid_configure",
    )


@router.get("/servers/{server_id}/vmedia")
def virtual_media(server_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    """What is mounted as virtual media right now (Redfish)."""
    server = _server(db, server_id)
    try:
        with get_driver(server, interactive=True) as driver:
            media = driver.virtual_media()
    except (SecretNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"no CIMC credential: {exc}") from exc
    except BMCError as exc:
        return {"supported": False, "media": [], "error": str(exc), "checked_at": _now()}
    return {"supported": True, "media": media, "error": None, "checked_at": _now()}


@router.post("/servers/{server_id}/vmedia/boot", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED)
def vmedia_boot(
    server_id: uuid.UUID,
    payload: VmediaBootRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    """Mount an ISO from the image store on the BMC and boot the server from it."""
    server = _server(db, server_id)
    image = db.get(Image, payload.image_id)
    if image is None:
        raise HTTPException(status_code=404, detail="image not found")
    if image.status != "ready":
        raise HTTPException(status_code=409, detail=f"image is {image.status}, not ready")
    if not (images.image_dir() / image.filename).exists():
        raise HTTPException(status_code=409, detail="the image file is missing from disk")
    return _queue(
        db, request, admin, server, JobType.VMEDIA_BOOT,
        payload={
            "image_id": str(image.id),
            "image_name": image.name,
            "image_url": images.public_url(image.filename),
            "boot": payload.boot,
        },
        audit_action="server.vmedia_boot",
    )


@router.post("/servers/{server_id}/vmedia/eject", response_model=JobOut,
             status_code=status.HTTP_202_ACCEPTED)
def vmedia_eject(
    server_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    admin: Customer = Depends(current_admin),
) -> Job:
    server = _server(db, server_id)
    return _queue(
        db, request, admin, server, JobType.VMEDIA_EJECT, payload={},
        audit_action="server.vmedia_eject",
    )
