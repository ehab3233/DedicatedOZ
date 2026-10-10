"""Celery application.

Workers are the only processes with a route to the OOB plane. The API never
talks to a BMC directly — that separation is what keeps a bug in a request
handler from being reachable from the customer plane.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery("doz", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # BMC work is slow and mostly waiting on hardware. Prefetching would let a
    # single worker sit on jobs it cannot start for twenty minutes.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    task_routes={
        "doz.provision.*": {"queue": "provision"},
        "doz.power.*": {"queue": "power"},
        "doz.poll.*": {"queue": "poll"},
    },
    beat_schedule={
        # PSU, fan, temperature and drive state onto the server row.
        "poll-health": {
            "task": "doz.poll.health_all",
            "schedule": crontab(minute="*/5"),
        },
        # Drives, NICs, firmware, BIOS: slower to read, slower to change.
        "poll-inventory": {
            "task": "doz.poll.inventory_all",
            "schedule": crontab(minute="7,37"),
        },
        "poll-bandwidth": {
            "task": "doz.poll.bandwidth_all",
            "schedule": crontab(minute="*/5"),
        },
        "reap-stale-jobs": {
            "task": "doz.poll.reap_jobs",
            "schedule": crontab(minute="*/5"),
        },
        # Catches jobs the API committed but could not hand to the broker.
        "sensors-all": {
            "task": "doz.poll.sensors_all",
            "schedule": settings.sensors_poll_interval_seconds,
        },
        "redispatch-queued": {
            "task": "doz.poll.redispatch_queued",
            "schedule": crontab(minute="*/2"),
        },
    },
)

# Import for side effects: registers the tasks.
celery_app.autodiscover_tasks(["app.workers"], related_name="tasks", force=True)

from app.workers import tasks  # noqa: E402,F401
