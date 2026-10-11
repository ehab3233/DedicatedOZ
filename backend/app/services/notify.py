"""What customers are told about, and when.

One thing for now: a reinstall, rescue boot or wipe on a server has
finished, well or badly. The mail goes to the customer holding the server,
and to the customer who asked for it if that is someone else, each only if
they have notifications on. Admin-only work (health polls, BMC setup) is
never mailed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.enums import JobState, JobType
from app.models import Customer, Job, Server, Subscription
from app.services import mail

NOTIFIED_JOB_TYPES = {JobType.INSTALL, JobType.RESCUE, JobType.WIPE}

_WHAT = {
    JobType.INSTALL.value: "reinstall",
    JobType.RESCUE.value: "rescue boot",
    JobType.WIPE.value: "wipe",
}


def recipients(db, job: Job) -> list[Customer]:  # noqa: ANN001
    """Who hears about this job: the holder of the server, and the requester."""
    people: dict[uuid.UUID, Customer] = {}
    if job.server_id:
        holder = db.execute(
            select(Customer)
            .join(Subscription, Subscription.customer_id == Customer.id)
            .where(Subscription.server_id == job.server_id, Subscription.ended_at.is_(None))
        ).scalars().first()
        if holder is not None:
            people[holder.id] = holder
    if job.requested_by_id:
        requester = db.get(Customer, job.requested_by_id)
        if requester is not None and not requester.is_admin:
            people[requester.id] = requester
    return [c for c in people.values() if c.notify_jobs and c.email]


def compose(job: Job, server: Server | None) -> tuple[str, str]:
    what = _WHAT.get(job.type, job.type.replace("_", " "))
    name = (server.hostname or server.serial) if server else "your server"
    outcome = {
        JobState.SUCCEEDED.value: "finished",
        JobState.FAILED.value: "failed",
        JobState.CANCELLED.value: "was cancelled",
    }.get(job.state, job.state)
    subject = f"{name}: {what} {outcome}"
    lines = [f"The {what} of {name}" + (f" ({server.serial})" if server else "") + f" {outcome}."]
    if job.state == JobState.FAILED.value and job.error:
        lines += ["", f"Reason: {job.error}"]
    if job.state == JobState.SUCCEEDED.value and job.stage:
        lines += ["", f"Last step: {job.stage}"]
    base = settings.portal_url or settings.control_plane_url
    if job.server_id:
        lines += ["", f"Server: {base.rstrip('/')}/servers/{job.server_id}"]
    lines += [f"Job log: {base.rstrip('/')}/jobs/{job.id}", "",
              "You are receiving this because notifications are on for your account."]
    return subject, "\n".join(lines)


def job_finished(job_id: uuid.UUID) -> int:
    """Mail everyone who should hear that `job_id` reached a terminal state.
    Returns how many messages were sent."""
    if not mail.configured():
        return 0
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None or JobType(job.type) not in NOTIFIED_JOB_TYPES:
            return 0
        if JobState(job.state) not in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
            return 0
        server = db.get(Server, job.server_id) if job.server_id else None
        subject, body = compose(job, server)
        sent = 0
        for person in recipients(db, job):
            if mail.send(person.email, subject, body):
                sent += 1
        return sent
