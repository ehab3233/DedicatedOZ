"""Job creation, transitions and logging.

The database is the source of truth for job state, not Celery. Celery is only
a delivery mechanism: if a broker message is lost, a job sits in `queued` and
is visible; if a worker dies mid-run, the job sits in `running` past its
deadline and the reaper fails it. Neither case silently disappears.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import (
    JOB_TRANSITIONS,
    TERMINAL_JOB_STATES,
    ActorType,
    JobState,
    JobType,
)
from app.models import Job, JobLogEntry
from app.security import generate_callback_token

#: Job types that must not overlap on one server. Power reads are exempt.
EXCLUSIVE_JOB_TYPES = {
    JobType.INSTALL,
    JobType.RESCUE,
    JobType.WIPE,
    JobType.POWER_ON,
    JobType.POWER_OFF,
    JobType.POWER_FORCE_OFF,
    JobType.POWER_CYCLE,
    JobType.POWER_RESET,
    JobType.BMC_SETUP,
}


class JobConflict(RuntimeError):
    """Another job already holds this server."""


class IllegalJobTransition(ValueError):
    pass


def active_job_for_server(db: Session, server_id: uuid.UUID) -> Job | None:
    return db.execute(
        select(Job)
        .where(
            Job.server_id == server_id,
            Job.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
            Job.type.in_([t.value for t in EXCLUSIVE_JOB_TYPES]),
        )
        .order_by(Job.created_at)
        .limit(1)
    ).scalar_one_or_none()


def create_job(
    db: Session,
    *,
    job_type: JobType,
    server_id: uuid.UUID | None,
    payload: dict | None = None,
    requested_by_id: uuid.UUID | None = None,
    requested_by_type: ActorType = ActorType.CUSTOMER,
    with_callback_token: bool = False,
    allow_concurrent: bool = False,
) -> tuple[Job, str | None]:
    """Create a queued job. Returns `(job, callback_token_or_None)`.

    The plaintext callback token is returned once and never stored; the caller
    hands it to the installer through the boot script.

    `allow_concurrent` skips the one-job-per-server lock. It exists for one
    case: an operator resetting a machine that is stuck mid-install, where
    waiting for the install job to time out is the wrong answer.
    """
    if server_id and job_type in EXCLUSIVE_JOB_TYPES and not allow_concurrent:
        existing = active_job_for_server(db, server_id)
        if existing:
            raise JobConflict(
                f"server already has an active {existing.type} job ({existing.id})"
            )

    job = Job(
        type=job_type,
        state=JobState.QUEUED,
        server_id=server_id,
        payload=payload or {},
        requested_by_id=requested_by_id,
        requested_by_type=requested_by_type,
    )

    token: str | None = None
    if with_callback_token:
        token, token_hash = generate_callback_token()
        job.callback_token_hash = token_hash
        job.callback_expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.callback_token_ttl_seconds
        )

    db.add(job)
    db.flush()
    log(db, job, f"job {job_type.value} queued", customer_visible=True)
    return job, token


def transition_job(db: Session, job: Job, target: JobState, *, error: str | None = None) -> Job:
    current = JobState(job.state)
    if current == target:
        return job
    if target not in JOB_TRANSITIONS.get(current, set()):
        raise IllegalJobTransition(f"cannot move job from {current.value} to {target.value}")

    job.state = target
    if target is JobState.RUNNING:
        job.started_at = datetime.now(UTC)
        job.attempts += 1
    elif target in TERMINAL_JOB_STATES:
        job.finished_at = datetime.now(UTC)
        # The callback token dies with the job, in both the hashed and the
        # plaintext copy, so a leaked boot script cannot be replayed later.
        job.callback_token_hash = None
        job.callback_expires_at = None
        if job.payload and "_callback_token" in job.payload:
            payload = dict(job.payload)
            payload.pop("_callback_token", None)
            job.payload = payload
        if target is JobState.SUCCEEDED:
            job.progress = 100

    if error:
        job.error = error[:8000]

    db.add(job)
    return job


def set_stage(db: Session, job: Job, stage: str, progress: int | None = None) -> None:
    job.stage = stage
    if progress is not None:
        job.progress = max(0, min(100, progress))
    db.add(job)
    log(db, job, stage, customer_visible=True)


def log(
    db: Session,
    job: Job,
    message: str,
    *,
    level: str = "info",
    request: dict | None = None,
    response: dict | None = None,
    customer_visible: bool = False,
) -> JobLogEntry:
    """Append a log line, allocating the next sequence number.

    Sequence is computed from the database rather than a Python counter so the
    ordering survives a job being touched by more than one process (the worker
    writing progress while the installer posts callbacks, for instance).
    """
    next_seq = (
        db.execute(
            select(func.coalesce(func.max(JobLogEntry.sequence), 0) + 1).where(
                JobLogEntry.job_id == job.id
            )
        ).scalar_one()
    )
    entry = JobLogEntry(
        job_id=job.id,
        sequence=next_seq,
        level=level,
        message=message[:8000],
        request=request,
        response=response,
        customer_visible=customer_visible,
    )
    db.add(entry)
    db.flush()
    return entry


def make_log_sink(db: Session, job: Job):
    """Adapt `log()` to the driver `LogSink` protocol.

    Driver traffic is admin-only: raw BMC responses leak firmware detail and
    internal addressing that customers have no business seeing.
    """

    def sink(
        message: str,
        *,
        level: str = "info",
        request: dict | None = None,
        response: dict | None = None,
    ) -> None:
        log(
            db,
            job,
            message,
            level=level,
            request=request,
            response=response,
            customer_visible=False,
        )
        db.commit()

    return sink


def reap_stale_jobs(db: Session, *, running_timeout: int | None = None) -> int:
    """Fail jobs whose worker vanished. Returns how many were reaped."""
    timeout = running_timeout or settings.install_timeout_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout)
    stale = (
        db.execute(
            select(Job).where(Job.state == JobState.RUNNING.value, Job.started_at < cutoff)
        )
        .scalars()
        .all()
    )
    for job in stale:
        transition_job(
            db,
            job,
            JobState.FAILED,
            error=f"job exceeded {timeout}s without completing; worker presumed lost",
        )
        log(db, job, "job timed out", level="error", customer_visible=True)
    return len(stale)
