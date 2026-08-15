"""Job workers.

Every task follows the same shape: claim the job row, do the work, record the
outcome. `job_runner` owns that shape so individual tasks only contain the
part that is actually specific to them.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_scope
from app.drivers import BMCError, LogSink, get_driver, null_sink
from app.drivers.redfish import RedfishDriver
from app.enums import ActorType, JobState, JobType, PowerAction, ServerState
from app.models import Job, Server
from app.services import jobs as job_service
from app.services.lifecycle import IllegalTransition, transition_server
from app.workers.celery_app import celery_app


class JobCancelled(RuntimeError):
    pass


@contextmanager
def job_runner(job_id: str) -> Iterator[tuple[Session, Job, Server | None] | None]:
    """Claim a job, run the body, and record the outcome exactly once.

    Yields `None` when there is nothing to do, so callers can bail out without
    a second code path. A job not in `queued` is skipped rather than re-run:
    with `task_acks_late`, a redelivered message must not repeat an install
    that already happened.
    """
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(Job, job_uuid)
        if job is None:
            yield None
            return
        if JobState(job.state) is not JobState.QUEUED:
            job_service.log(
                db, job, f"worker skipped job already in state {job.state}", level="warning"
            )
            yield None
            return
        if job.cancel_requested:
            job_service.transition_job(db, job, JobState.CANCELLED)
            yield None
            return

        job_service.transition_job(db, job, JobState.RUNNING)
        server = db.get(Server, job.server_id) if job.server_id else None
        db.commit()

        try:
            yield db, job, server
        except BaseException as exc:  # noqa: BLE001 - the job must record why it died
            # The in-flight transaction is unusable after a failure, so the
            # outcome is written from a clean session rather than by trying to
            # salvage the one that just blew up.
            db.rollback()
            _record_failure(job_uuid, exc)
            raise
        else:
            if JobState(job.state) is JobState.RUNNING:
                job_service.transition_job(db, job, JobState.SUCCEEDED)
            db.commit()


def _record_failure(job_uuid: uuid.UUID, exc: BaseException) -> None:
    """Write a terminal state for a job whose body raised."""
    cancelled = isinstance(exc, JobCancelled)
    with session_scope() as db:
        job = db.get(Job, job_uuid)
        if job is None or JobState(job.state) is not JobState.RUNNING:
            return
        if cancelled:
            job_service.transition_job(db, job, JobState.CANCELLED)
            job_service.log(db, job, "job cancelled", level="warning", customer_visible=True)
            return

        job_service.transition_job(db, job, JobState.FAILED, error=str(exc) or repr(exc))
        job_service.log(
            db,
            job,
            f"job failed: {exc}",
            level="error",
            request=getattr(exc, "request", None),
            response=getattr(exc, "response", None),
            customer_visible=True,
        )


def _check_cancel(db: Session, job: Job) -> None:
    """Cancellation is cooperative — polled at each stage boundary.

    A reinstall cannot be interrupted mid-write without leaving the disks in an
    unknown state, so the checks sit between stages, not inside them.
    """
    db.commit()
    db.refresh(job, ["cancel_requested"])
    if job.cancel_requested:
        raise JobCancelled()


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------

_POWER_ACTIONS: dict[str, PowerAction] = {
    JobType.POWER_ON.value: PowerAction.ON,
    JobType.POWER_OFF.value: PowerAction.OFF,
    JobType.POWER_RESET.value: PowerAction.FORCE_RESTART,
}


@celery_app.task(name="doz.power.execute", bind=True, max_retries=0)
def power_task(self, job_id: str) -> dict:  # noqa: ANN001
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("power job has no server")

        sink = job_service.make_log_sink(db, job)
        with get_driver(server, log=sink) as driver:
            before = driver.power_status()
            job_service.set_stage(db, job, f"power is {before.state}", progress=10)
            db.commit()

            if job.type == JobType.POWER_CYCLE.value:
                job_service.set_stage(db, job, "power cycling", progress=40)
                db.commit()
                driver.power_cycle()
                want = "on"
            else:
                action = _POWER_ACTIONS[job.type]
                if (
                    (action is PowerAction.ON and before.state == "on")
                    or (action is PowerAction.OFF and before.state == "off")
                ):
                    job_service.set_stage(
                        db, job, f"already powered {before.state}", progress=100
                    )
                    job.result = {"power_state": before.state, "no_op": True}
                    db.commit()
                    return {"power_state": before.state, "no_op": True}
                job_service.set_stage(db, job, f"sending {action.value}", progress=40)
                db.commit()
                driver.power(action)
                want = {"on": "on", "off": "off", "force_restart": "on"}[action.value]

            job_service.set_stage(db, job, "confirming power state", progress=75)
            db.commit()
            reached = driver.wait_for_power_state(want, timeout=180)
            final = driver.power_status()
            server.last_power_state = final.state
            db.add(server)

            job.result = {"power_state": final.state, "reached_target": reached}
            if not reached:
                # Graceful shutdown depends on the guest OS cooperating. Report
                # it rather than pretending the job succeeded.
                job_service.log(
                    db,
                    job,
                    f"server did not reach '{want}' within 180s (now: {final.state})",
                    level="warning",
                    customer_visible=True,
                )
            db.commit()
            return job.result
    return {"skipped": True}


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _netboot_into_ramdisk(db: Session, job: Job, server: Server, driver) -> None:  # noqa: ANN001
    """Shared front half of install / rescue / wipe.

    Sets a one-time PXE boot and power cycles. The boot script the machine then
    fetches is chosen by `services.boot` from this same job row, which is why
    the three rails differ only in what happens after this point.
    """
    job_service.set_stage(db, job, "setting one-time PXE boot", progress=5)
    db.commit()
    driver.set_boot_once("pxe")

    job_service.set_stage(db, job, "power cycling into installer", progress=10)
    db.commit()
    driver.power_cycle()

    job_service.set_stage(db, job, "waiting for installer to check in", progress=15)
    db.commit()


def _wait_for_callback(db: Session, job: Job, timeout: int) -> dict:
    """Block until the ramdisk reports done, or the deadline passes.

    Progress in between arrives through the callback API, which updates the job
    row directly; this loop only watches for a terminal result.
    """
    deadline = time.monotonic() + timeout
    poll_interval = 10
    while time.monotonic() < deadline:
        # End the read transaction each pass. Holding one open for the length
        # of an install would pin the snapshot and hide the very callbacks this
        # loop is waiting for.
        db.commit()
        db.refresh(job)
        result = job.result or {}
        if result.get("_installer_status") == "succeeded":
            return result
        if result.get("_installer_status") == "failed":
            raise RuntimeError(
                f"installer reported failure: {result.get('_installer_error', 'no detail')}"
            )
        if job.cancel_requested:
            raise JobCancelled()
        time.sleep(poll_interval)

    raise TimeoutError(
        f"installer did not check in within {timeout}s. "
        "Check the serial console and the job log for the last BMC exchange."
    )


@celery_app.task(name="doz.provision.install", bind=True, max_retries=0)
def install_task(self, job_id: str) -> dict:  # noqa: ANN001
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("install job has no server")

        _safe_transition(db, job, server, ServerState.PROVISIONING)
        db.commit()

        sink = job_service.make_log_sink(db, job)
        with get_driver(server, log=sink) as driver:
            _netboot_into_ramdisk(db, job, server, driver)
            _check_cancel(db, job)
            result = _wait_for_callback(db, job, settings.install_timeout_seconds)

            job_service.set_stage(db, job, "clearing boot override", progress=95)
            db.commit()
            # Leave the machine booting from disk. A stuck PXE override turns
            # every later customer reboot into a surprise reinstall prompt.
            driver.clear_boot_override()

        _safe_transition(db, job, server, ServerState.ACTIVE)
        server.hostname = (job.payload or {}).get("hostname") or server.hostname
        server.last_wiped_at = None
        db.add(server)
        job_service.set_stage(db, job, "install complete", progress=100)
        db.commit()
        return result
    return {"skipped": True}


@celery_app.task(name="doz.provision.rescue", bind=True, max_retries=0)
def rescue_task(self, job_id: str) -> dict:  # noqa: ANN001
    """Boot the rescue image and leave the machine sitting in it.

    Unlike install, this does not wait for a completion callback — the rescue
    environment is the destination, not a step. The job succeeds once the
    ramdisk announces it is up with SSH listening.
    """
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("rescue job has no server")

        sink = job_service.make_log_sink(db, job)
        with get_driver(server, log=sink) as driver:
            _netboot_into_ramdisk(db, job, server, driver)
            result = _wait_for_callback(db, job, timeout=1800)

        _safe_transition(db, job, server, ServerState.RESCUE)
        job_service.set_stage(db, job, "rescue environment ready", progress=100)
        db.commit()
        return result
    return {"skipped": True}


@celery_app.task(name="doz.provision.wipe", bind=True, max_retries=0)
def wipe_task(self, job_id: str) -> dict:  # noqa: ANN001
    """Destroy customer data before a server returns to stock.

    The M4 has no hardware sanitize API — CIMC data sanitization is 4.3+ and
    M5-only — so the erase happens inside the rescue ramdisk via ATA secure
    erase / `nvme format` / `sg_format`, and this task's job is to prove it
    finished before the server becomes sellable again.
    """
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("wipe job has no server")

        _safe_transition(db, job, server, ServerState.WIPING)
        db.commit()

        sink = job_service.make_log_sink(db, job)
        with get_driver(server, log=sink) as driver:
            _netboot_into_ramdisk(db, job, server, driver)
            result = _wait_for_callback(db, job, timeout=settings.install_timeout_seconds * 4)

            job_service.set_stage(db, job, "powering down", progress=95)
            db.commit()
            driver.clear_boot_override()
            driver.power(PowerAction.FORCE_OFF)

        wiped = result.get("drives_wiped") or []
        if settings.require_wipe_before_stock and not wiped:
            raise RuntimeError(
                "wipe callback reported no drives erased; server held in 'wiping' "
                "for manual inspection"
            )

        server.last_wiped_at = datetime.now(UTC)
        db.add(server)
        _safe_transition(db, job, server, ServerState.IN_STOCK)
        job_service.set_stage(db, job, f"wipe complete ({len(wiped)} drives)", progress=100)
        db.commit()
        return result
    return {"skipped": True}


def _safe_transition(db: Session, job: Job, server: Server, target: ServerState) -> None:
    try:
        transition_server(
            db,
            server,
            target,
            actor_type=ActorType.SYSTEM,
            actor_label=f"job:{job.type}",
            reason=str(job.id),
        )
    except IllegalTransition as exc:
        job_service.log(db, job, str(exc), level="error", customer_visible=True)
        raise


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def _sync_inventory(db: Session, server: Server, sink: LogSink = null_sink) -> dict:
    """Read hardware detail from the BMC into the database.

    Also captures the PXE MAC, which the whole netboot rail keys on.
    """
    with get_driver(server, log=sink) as driver:
        inv = driver.inventory()
        if isinstance(driver, RedfishDriver):
            server.redfish_system_path = driver.system_path

    server.cpu_model = inv.cpu_model or server.cpu_model
    server.cpu_count = inv.cpu_count or server.cpu_count
    server.cpu_cores_total = inv.cpu_cores_total or server.cpu_cores_total
    server.ram_gb = inv.ram_gb or server.ram_gb
    server.bios_version = inv.bios_version or server.bios_version
    server.cimc_firmware = inv.bmc_firmware or server.cimc_firmware
    server.hardware_spec = {
        "manufacturer": inv.manufacturer,
        "model": inv.model,
        "nics": inv.nics,
        "drives": inv.drives,
        "synced_at": datetime.now(UTC).isoformat(),
    }
    if inv.model:
        server.model = inv.model

    # Only adopt a MAC automatically when it is unambiguous. Guessing wrong
    # here points the boot script at the wrong NIC, and the install then
    # silently never starts.
    if not server.provisioning_mac and len(inv.nics) == 1:
        server.provisioning_mac = inv.nics[0]["mac"]

    db.add(server)
    return {"nics": len(inv.nics), "drives": len(inv.drives), "mac": server.provisioning_mac}


def _poll_health(db: Session, server: Server, sink: LogSink = null_sink) -> dict:
    try:
        with get_driver(server, log=sink) as driver:
            health = driver.health()
            power = driver.power_status()
    except BMCError as exc:
        # A BMC that stopped answering is itself the alert, so it is recorded
        # rather than raised — a dead CIMC must not stall the whole sweep.
        server.health_status = "unknown"
        server.health_detail = {"error": str(exc)}
        server.health_checked_at = datetime.now(UTC)
        db.add(server)
        return {"status": "unknown", "error": str(exc)}

    server.health_status = health.status
    server.health_detail = health.subsystems
    server.health_checked_at = datetime.now(UTC)
    server.last_power_state = power.state
    db.add(server)
    return {"status": health.status, "power_state": power.state}


@celery_app.task(name="doz.poll.inventory_sync", max_retries=0)
def inventory_sync_task(job_id: str) -> dict:
    """Admin-triggered inventory sync, recorded as a job."""
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("inventory sync job has no server")
        result = _sync_inventory(db, server, job_service.make_log_sink(db, job))
        job.result = result
        job_service.set_stage(db, job, "inventory synced", progress=100)
        db.commit()
        return result
    return {"skipped": True}


@celery_app.task(name="doz.poll.health", max_retries=0)
def health_task(job_id: str) -> dict:
    """Admin-triggered health check, recorded as a job."""
    with job_runner(job_id) as ctx:
        if ctx is None:
            return {"skipped": True}
        db, job, server = ctx
        if server is None:
            raise RuntimeError("health job has no server")
        result = _poll_health(db, server, job_service.make_log_sink(db, job))
        job.result = result
        job_service.set_stage(db, job, f"health: {result['status']}", progress=100)
        db.commit()
        return result
    return {"skipped": True}


@celery_app.task(name="doz.poll.health_one", max_retries=0)
def health_sweep_one_task(server_id: str) -> dict:
    """Scheduled health check for one server.

    Deliberately not a job: a fifteen-minute sweep across the fleet would bury
    the job list under thousands of rows nobody reads. The result lands on the
    server row, which is where the admin UI looks.
    """
    with session_scope() as db:
        server = db.get(Server, uuid.UUID(server_id))
        if server is None:
            return {"error": "server not found"}
        return _poll_health(db, server)


@celery_app.task(name="doz.poll.health_all", max_retries=0)
def health_all_task() -> dict:
    with session_scope() as db:
        ids = (
            db.execute(
                select(Server.id).where(
                    Server.state.notin_([ServerState.RETIRED.value, ServerState.RMA.value])
                )
            )
            .scalars()
            .all()
        )
    for server_id in ids:
        health_sweep_one_task.delay(str(server_id))
    return {"queued": len(ids)}


@celery_app.task(name="doz.poll.bandwidth_all", max_retries=0)
def bandwidth_all_task() -> dict:
    """Placeholder for switch counter collection.

    Bandwidth comes from the access switch, not the BMC — the CIMC cannot see
    customer traffic. Wire this to SNMP or gNMI once the switch model is fixed;
    `app.services.bandwidth` already knows how to store and read the samples.
    """
    return {"collected": 0, "note": "switch poller not configured"}


@celery_app.task(name="doz.poll.reap_jobs", max_retries=0)
def reap_jobs_task() -> dict:
    with session_scope() as db:
        return {"reaped": job_service.reap_stale_jobs(db)}


#: Job type -> Celery task. The API enqueues through this map only.
TASK_FOR_JOB_TYPE: dict[JobType, Callable] = {
    JobType.POWER_ON: power_task,
    JobType.POWER_OFF: power_task,
    JobType.POWER_CYCLE: power_task,
    JobType.POWER_RESET: power_task,
    JobType.INSTALL: install_task,
    JobType.RESCUE: rescue_task,
    JobType.WIPE: wipe_task,
    JobType.INVENTORY_SYNC: inventory_sync_task,
    JobType.HEALTH_POLL: health_task,
}


def dispatch(job: Job) -> str:
    """Enqueue a job row. Returns the Celery task id."""
    task = TASK_FOR_JOB_TYPE[JobType(job.type)]
    async_result = task.delay(str(job.id))
    return async_result.id


@celery_app.task(name="doz.poll.redispatch_queued", max_retries=0)
def redispatch_queued_task(older_than_seconds: int = 120) -> dict:
    """Re-enqueue jobs whose broker message never arrived.

    The database is the source of truth, so a job the API committed but could
    not hand to Redis is still sitting in `queued` and is still correct — it is
    just not moving. This sweep is what makes that recoverable without anyone
    noticing, and it is safe to re-send: `job_runner` refuses to run a job that
    is not in `queued`, so a duplicate delivery is a no-op rather than a second
    install.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    with session_scope() as db:
        stuck = (
            db.execute(
                select(Job).where(
                    Job.state == JobState.QUEUED.value,
                    Job.created_at < cutoff,
                    Job.type.in_([t.value for t in TASK_FOR_JOB_TYPE]),
                )
            )
            .scalars()
            .all()
        )
        redispatched = 0
        for job in stuck:
            try:
                job.celery_task_id = dispatch(job)
            except Exception as exc:  # noqa: BLE001 - the broker is still down
                job_service.log(
                    db, job, f"re-dispatch failed: {exc}", level="warning"
                )
                continue
            job_service.log(db, job, "re-dispatched after broker delivery failure")
            db.add(job)
            redispatched += 1
    return {"stuck": len(stuck), "redispatched": redispatched}
