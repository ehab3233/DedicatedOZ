"""Job polling and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_customer, get_owned_job
from app.enums import TERMINAL_JOB_STATES, JobState
from app.models import Customer, Job, JobLogEntry, Subscription
from app.schemas import JobDetailOut, JobLogEntryOut, JobOut

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(
    state: JobState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> list[Job]:
    query = (
        select(Job)
        .join(Subscription, Subscription.server_id == Job.server_id)
        .where(Subscription.customer_id == customer.id, Subscription.ended_at.is_(None))
        .order_by(Job.created_at.desc())
        .limit(limit)
    )
    if state:
        query = query.where(Job.state == state.value)
    return list(db.execute(query).scalars().all())


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(
    job: Job = Depends(get_owned_job),
    db: Session = Depends(get_db),
    customer: Customer = Depends(current_customer),
) -> JobDetailOut:
    """Job detail with its log.

    Customers see the narrative lines only. Raw BMC requests and responses are
    admin-only — they expose firmware detail and OOB addressing.
    """
    query = select(JobLogEntry).where(JobLogEntry.job_id == job.id)
    if not customer.is_admin:
        query = query.where(JobLogEntry.customer_visible.is_(True))
    entries = db.execute(query.order_by(JobLogEntry.sequence)).scalars().all()

    detail = JobDetailOut.model_validate(job)
    detail.log = [JobLogEntryOut.model_validate(e) for e in entries]
    # `result` carries installer bookkeeping keys; strip the internal ones.
    detail.result = {k: v for k, v in (job.result or {}).items() if not k.startswith("_")}
    return detail


@router.post("/{job_id}/cancel", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(
    job: Job = Depends(get_owned_job),
    db: Session = Depends(get_db),
) -> Job:
    """Request cancellation.

    Cancellation is cooperative: a queued job stops immediately, a running one
    stops at its next stage boundary. A reinstall that is already writing to
    disk will finish that stage first — there is no safe way to abort mid-write.
    """
    if JobState(job.state) in TERMINAL_JOB_STATES:
        raise HTTPException(status_code=409, detail=f"job is already {job.state}")

    job.cancel_requested = True
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
