"""The netboot rail: boot scripts out, installer callbacks in.

Trust model. These endpoints are reachable from the provisioning VLAN, which
is where machines with no operating system live — they cannot hold a
credential before they have been installed. So the protection is layered:

1. Network position. The provisioning VLAN is not the customer plane and is
   not routed to the internet. This is the actual boundary.
2. An active job. A MAC with no queued or running install/rescue/wipe job gets
   a "boot from local disk" script and nothing else. The window in which a
   server's boot material exists at all is the length of one job.
3. Client pinning. The first request for a MAC records its source address;
   later requests for the same MAC must come from it. A second machine on the
   VLAN cannot fetch another host's answer file mid-install.
4. A per-MAC signature on the follow-on URLs, so a mistyped or guessed MAC
   fails cleanly rather than half-working.

Writes (progress and completion) additionally require the per-job callback
token, which is generated when the job is created, embedded in the boot
script, and destroyed when the job ends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import client_ip
from app.drivers import BMCError, get_driver
from app.enums import ActorType, JobState, JobType
from app.models import Job, Server
from app.schemas import InstallerComplete, InstallerProgress
from app.secrets import SecretNotFoundError
from app.security import hash_api_token, normalise_mac, verify_boot_signature
from app.services import boot as boot_service
from app.services import jobs as job_service
from app.services.audit import record_audit

router = APIRouter(prefix="/boot", tags=["boot"])

# The scripts below never `reboot` and never chain into themselves. A loop is
# always a `goto` within one script, and moving on is always `chain --replace`,
# which swaps the running script for the next one instead of nesting them, so
# a server can sit polling for hours without iPXE running out of stack. When a
# script ends, iPXE exits and the BIOS tries the next boot device.

#: Sent to a machine whose MAC is unusable. Nothing to do for it here.
BOOT_LOCAL_SCRIPT = """#!ipxe
echo No provisioning job for this host. Booting from local disk.
sanboot --no-describe --drive 0x80 || exit
"""

#: Sent to a known server with no job waiting for it. One with an OS boots
#: it. One without (fresh from the rack, or wiped) would otherwise hand back
#: to the BIOS, which PXE-boots it again, through a full POST every time;
#: instead it parks at the boot loader and asks again every minute, so the
#: console says what is going on and a job started later is picked up.
BOOT_IDLE_SCRIPT = """#!ipxe
echo No provisioning job for this host. Booting from local disk.
sanboot --no-describe --drive 0x80 || goto nodisk
exit
:nodisk
echo No bootable disk. Waiting here for a job; asking the management server every {idle} seconds.
:wait
sleep {idle}
chain --replace {base}/boot/ipxe?mac={mac}&waiting=1 || goto wait
"""

#: Sent to a server that PXE-boots while its job is still preparing the disks.
#: A box with no OS falls through to PXE on every boot, and the RAID step
#: powers the host on, so this is the normal first boot of a reinstall. The
#: server parks and polls; the control plane answers 503 until the boot script
#: is ready, then hands it over, and the worker sees that and skips the power
#: cycle it would otherwise do.
BOOT_WAIT_SCRIPT = """#!ipxe
echo The management server is still preparing the disks for this install.
echo Waiting here; asking again every {poll} seconds.
:wait
sleep {poll}
chain --replace {base}/boot/ipxe?mac={mac}&waiting=1 || goto wait
"""

#: Sent when the entry point is hit without a MAC. iPXE expands `${net0/mac}`
#: itself when it runs this, so DHCP only ever has to hand out a fixed URL --
#: no reliance on the DHCP server passing `${...}` through untouched.
BOOT_CHAIN_SCRIPT = """#!ipxe
chain --replace {base}/boot/ipxe?mac=${{net0/mac}} || goto local
:local
sanboot --no-describe --drive 0x80 || exit
"""

#: What a parked server's poll gets while there is nothing new for it. iPXE
#: treats the error status as a failed `chain`, so the script's `goto wait`
#: runs and it asks again.
NOT_READY_RESPONSE = "nothing to boot yet; ask again\n"


def _resolve(db: Session, mac: str, request: Request, *, signature: str | None = None):
    """Look up the server and job for a MAC, enforcing the guards above."""
    try:
        mac = normalise_mac(mac)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if signature is not None and not verify_boot_signature(mac, signature):
        raise HTTPException(status_code=403, detail="invalid boot signature")

    try:
        server, job = boot_service.find_provisioning_job(db, mac)
    except boot_service.NoActiveInstall as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _pin_client(db, job, request)
    return server, job


def _pin_client(db: Session, job: Job, request: Request) -> None:
    """Bind a job's boot material to the first address that asked for it."""
    source = client_ip(request)
    if not settings.boot_pin_client_ip:
        return
    payload = dict(job.payload or {})
    pinned = payload.get("_boot_client_ip")

    if pinned is None:
        if source is None:
            # Nothing to pin to (a proxy that strips the address, or the test
            # client). Logging "checked in from None" on every poll helps no one.
            return
        payload["_boot_client_ip"] = source
        job.payload = payload
        db.add(job)
        job_service.log(db, job, f"installer checked in from {source}")
        db.commit()
        return

    if source != pinned:
        job_service.log(
            db,
            job,
            f"rejected boot request from {source}; job is pinned to {pinned}",
            level="error",
        )
        db.commit()
        raise HTTPException(status_code=403, detail="boot request from unexpected address")


# ---------------------------------------------------------------------------
# Boot scripts
# ---------------------------------------------------------------------------


@router.get("/ipxe", response_class=PlainTextResponse)
def ipxe_entry(
    request: Request,
    mac: str = "",
    waiting: bool = False,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """Entry point. DHCP option 67 points every machine here.

    Called as `/boot/ipxe?mac=${net0/mac}` — iPXE substitutes the MAC itself,
    so one DHCP option serves the whole fleet.

    `waiting=1` marks a poll from a server parked at the boot loader by one of
    the scripts above. It gets 503 while there is nothing new for it, which
    keeps its loop going, and the real script the moment there is.
    """
    base = settings.control_plane_url.rstrip("/")
    if not mac:
        return PlainTextResponse(BOOT_CHAIN_SCRIPT.format(base=base), media_type="text/plain")

    try:
        normalised = normalise_mac(mac)
    except ValueError:
        return PlainTextResponse(BOOT_LOCAL_SCRIPT, media_type="text/plain")

    idle = BOOT_IDLE_SCRIPT.format(
        base=base, mac=normalised, idle=settings.boot_idle_poll_seconds
    )
    try:
        server, job = boot_service.find_provisioning_job(db, normalised)
    except boot_service.NoActiveInstall:
        # Not an error: this is the normal path for every reboot of every
        # server that is not being provisioned, and for a parked server whose
        # job has ended (it boots its disk if it has one).
        return PlainTextResponse(idle, media_type="text/plain")

    _pin_client(db, job, request)
    result = job.result or {}
    if result.get("_installer_status"):
        # The installer has already reported in and the worker is closing the
        # job. Serving the installer again now would reinstall on every
        # reboot until it had.
        job_service.log(
            db, job, "server PXE-booted after the installer reported in; told it to "
            "boot from disk",
        )
        db.commit()
        return PlainTextResponse(idle, media_type="text/plain")

    if not (job.payload or {}).get("_netboot_ready") and not result.get("_handoff"):
        # The worker has not set the boot flag yet: the disks may be
        # mid-rebuild. Park the server; the stage tells the worker it is here.
        if waiting and job.stage == boot_service.STAGE_PARKED:
            return PlainTextResponse(
                NOT_READY_RESPONSE, status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain",
            )
        # First contact for this job, whether a fresh PXE boot or a poll from
        # a server that was parked with no job (that loop asks once a minute;
        # this script asks every 20 s, and replaces it).
        if job.stage != boot_service.STAGE_PARKED:
            job_service.set_stage(db, job, boot_service.STAGE_PARKED)
            db.commit()
        return PlainTextResponse(
            BOOT_WAIT_SCRIPT.format(base=base, mac=normalised, poll=settings.boot_poll_seconds),
            media_type="text/plain",
        )

    script = boot_service.render_ipxe_script(db, server, job)
    if job.type == JobType.INSTALL.value and result.get("_handoff"):
        stage, progress = "loading the OS installer", 55
    else:
        stage, progress = boot_service.STAGE_SCRIPT_FETCHED, 20
    # A script that could not load what it points at asks again every minute;
    # the stage is only worth recording once.
    if job.stage != stage:
        job_service.set_stage(db, job, stage, progress=progress)
        db.commit()
    return PlainTextResponse(script, media_type="text/plain")


@router.get("/answer/{mac}", response_class=PlainTextResponse)
def answer_file(
    mac: str,
    request: Request,
    sig: str = "",
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """Kickstart / autoinstall / preseed / unattend for this host."""
    server, job = _resolve(db, mac, request, signature=sig)
    try:
        content_type, body = boot_service.render_answer_file(db, server, job)
    except boot_service.NoActiveInstall as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    job_service.log(db, job, "answer file served")
    db.commit()
    return PlainTextResponse(body, media_type=content_type)


@router.get("/nocloud/{mac}/{sig}/user-data", response_class=PlainTextResponse)
def nocloud_user_data(
    mac: str,
    sig: str,
    request: Request,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """Ubuntu autoinstall seed.

    cloud-init's NoCloud datasource is given a directory URL and appends
    `user-data` and `meta-data` itself, so the signature has to ride in the
    path rather than the query string. The body is the same answer file the
    generic endpoint serves.
    """
    server, job = _resolve(db, mac, request, signature=sig)
    try:
        content_type, body = boot_service.render_answer_file(db, server, job)
    except boot_service.NoActiveInstall as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job_service.log(db, job, "nocloud user-data served")
    db.commit()
    return PlainTextResponse(body, media_type=content_type)


@router.get("/nocloud/{mac}/{sig}/meta-data", response_class=PlainTextResponse)
def nocloud_meta_data(
    mac: str,
    sig: str,
    request: Request,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    server, job = _resolve(db, mac, request, signature=sig)
    hostname = (job.payload or {}).get("hostname") or server.hostname or f"srv-{server.serial}"
    body = f"instance-id: doz-{job.id}\nlocal-hostname: {hostname}\n"
    return PlainTextResponse(body, media_type="text/yaml")


@router.get("/nocloud/{mac}/{sig}/vendor-data", response_class=PlainTextResponse)
def nocloud_vendor_data(mac: str, sig: str) -> PlainTextResponse:
    # cloud-init probes for it; an empty document stops the 404 noise.
    return PlainTextResponse("", media_type="text/yaml")


@router.get("/provision/{mac}", response_class=PlainTextResponse)
def provision_script(
    mac: str,
    request: Request,
    sig: str = "",
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The shell script the ramdisk runs: wipe, RAID, install, phone home."""
    server, job = _resolve(db, mac, request, signature=sig)
    body = boot_service.render_provision_script(db, server, job)
    job_service.log(db, job, "provision script served")
    db.commit()
    return PlainTextResponse(body, media_type="text/x-shellscript")


# ---------------------------------------------------------------------------
# Installer callbacks
# ---------------------------------------------------------------------------


def authorised_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> Job:
    """Authenticate a callback against the job's own token."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not job.callback_token_hash:
        raise HTTPException(status_code=409, detail="job is no longer accepting callbacks")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing callback token")

    token = authorization[7:].strip()
    if hash_api_token(token) != job.callback_token_hash:
        raise HTTPException(status_code=401, detail="invalid callback token")

    if job.callback_expires_at and job.callback_expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=401, detail="callback token expired")
    if JobState(job.state) not in {JobState.QUEUED, JobState.RUNNING}:
        raise HTTPException(status_code=409, detail=f"job is {job.state}")
    return job


@router.post("/callback/{job_id}/progress", status_code=status.HTTP_204_NO_CONTENT)
def installer_progress(
    payload: InstallerProgress,
    job: Job = Depends(authorised_job),
    db: Session = Depends(get_db),
) -> None:
    """Progress ping from the ramdisk. Drives the customer-visible bar."""
    # Progress is clamped below 95: the last stretch belongs to the worker,
    # which still has to clear the boot override and flip the server state.
    job_service.set_stage(db, job, payload.stage, progress=min(payload.progress, 94))
    if payload.message:
        job_service.log(db, job, payload.message, customer_visible=True)
    db.commit()


@router.post("/callback/{job_id}/handoff", status_code=status.HTTP_204_NO_CONTENT)
def installer_handoff(
    job: Job = Depends(authorised_job),
    db: Session = Depends(get_db),
) -> None:
    """The ramdisk has prepared the disks and is about to reboot.

    The next PXE boot must load the OS installer instead of the ramdisk, so
    the job is flagged and the BMC's one-time PXE boot is set again (the
    first one was used up by the boot that is ending). If the BMC cannot be
    reached the job carries on: a wiped disk has nothing to boot, so the
    persistent order Prepare BMC set (disk, then PXE) falls through to PXE.
    """
    if job.type != JobType.INSTALL.value:
        raise HTTPException(status_code=409, detail="only an install hands off to an OS installer")
    result = dict(job.result or {})
    result["_handoff"] = True
    job.result = result
    db.add(job)
    job_service.set_stage(db, job, "rebooting into the OS installer", progress=50)
    db.commit()

    server = db.get(Server, job.server_id) if job.server_id else None
    if server is not None:
        try:
            sink = job_service.make_log_sink(db, job)
            with get_driver(server, log=sink, interactive=True) as driver:
                driver.set_boot_once("pxe")
            job_service.log(db, job, "one-time PXE boot set for the OS installer")
        except (BMCError, SecretNotFoundError, ValueError) as exc:
            job_service.log(
                db, job,
                f"could not set the PXE boot flag ({exc}); relying on the boot order "
                "falling through to PXE",
                level="warning", customer_visible=True,
            )
    db.commit()


@router.post("/callback/{job_id}/complete", status_code=status.HTTP_204_NO_CONTENT)
def installer_complete(
    payload: InstallerComplete,
    request: Request,
    job: Job = Depends(authorised_job),
    db: Session = Depends(get_db),
) -> None:
    """Terminal report from the ramdisk.

    This does not move the job to a terminal state — the worker is still
    waiting to clear the boot override and transition the server. It records
    the outcome the worker is polling for.
    """
    result = dict(job.result or {})
    result["_installer_status"] = "succeeded" if payload.success else "failed"
    if not payload.success:
        result["_installer_error"] = payload.message or "installer reported failure"
    if payload.drives_wiped:
        result["drives_wiped"] = payload.drives_wiped
    if payload.host_keys:
        result["host_keys"] = payload.host_keys
    if payload.detail:
        result["installer_detail"] = payload.detail
    job.result = result
    db.add(job)

    job_service.log(
        db,
        job,
        payload.message or ("installer finished" if payload.success else "installer failed"),
        level="info" if payload.success else "error",
        customer_visible=True,
    )
    record_audit(
        db,
        action="installer.callback",
        actor_type=ActorType.INSTALLER,
        actor_label=f"job:{job.id}",
        target_type="server",
        target_id=str(job.server_id),
        source_ip=client_ip(request),
        detail={"success": payload.success, "drives_wiped": len(payload.drives_wiped)},
    )
    db.commit()
