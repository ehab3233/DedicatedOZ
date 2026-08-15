"""Handing a committed job row to the queue.

Kept separate from the routers because the failure mode matters and should be
handled identically everywhere: the job row is already committed and durable,
so a broker that is down is a delivery problem, not a request failure. The
caller still gets its job back, the job still shows as queued, and the
`doz.poll.redispatch_queued` sweep picks it up when Redis returns.

Returning a 500 here would be worse than useless: the customer would see an
error for an action that is, in fact, going to happen.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import Job
from app.services import jobs as job_service

log = logging.getLogger(__name__)


def enqueue(db: Session, job: Job) -> Job:
    """Send `job` to the queue, tolerating a broker outage."""
    from app.workers.tasks import dispatch

    try:
        job.celery_task_id = dispatch(job)
    except Exception as exc:  # noqa: BLE001 - broker failures must not 500
        log.error("could not enqueue job %s: %s", job.id, exc)
        job_service.log(
            db,
            job,
            f"queued, but could not be handed to a worker yet ({exc}). "
            "It will be picked up automatically.",
            level="warning",
            customer_visible=True,
        )
        db.commit()
        db.refresh(job)
        return job

    db.add(job)
    db.commit()
    db.refresh(job)
    return job
