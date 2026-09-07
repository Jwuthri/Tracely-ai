"""Shared Celery app. The API (producer) and worker (consumer) both import this.
Tasks live in tracely.workers.tasks (included via `include`).
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_postrun

from tracely.config import settings

celery_app = Celery(
    "tracely",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["tracely.workers.tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.celery_queue,
    task_acks_late=True,
    worker_prefetch_multiplier=4,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # With acks_late on Redis, an unacked task is redelivered after `visibility_timeout` — so a long
    # task (cluster rebuild, batch eval) that outruns the default 1h gets run a SECOND time while the
    # first is still going (double work, double LLM spend). Raise the window past our slowest task.
    broker_transport_options={"visibility_timeout": 3 * 60 * 60},  # 3h
    # Bound task runtime so a hung task can't pin the (solo) worker forever.
    task_time_limit=30 * 60,  # hard kill at 30m
    task_soft_time_limit=25 * 60,  # SoftTimeLimitExceeded at 25m (lets a task clean up)
    # Periodic schedule. NOTHING here fires unless a beat scheduler is running: the local docker
    # worker embeds one (`--beat` in docker-compose.yml), and prod must run exactly one beat
    # process. Monitors stay quiet by default — they only fire when a condition crosses its
    # threshold and the dedup interval has elapsed.
    beat_schedule={
        "tracely.evaluate_monitors-every-5-min": {
            "task": "tracely.evaluate_monitors",
            "schedule": crontab(minute="*/5"),
        },
        # The deployment watching itself: queue depth, worker liveness, ingest freshness.
        # Logs every tick (a heartbeat log-based alerting can watch) and pages when degraded.
        "tracely.selfcheck-every-5-min": {
            "task": "tracely.selfcheck",
            "schedule": crontab(minute="*/5"),
        },
        # Judge-conversation checkpoints grow quadratically and are never re-read. Nightly is
        # plenty — this reclaims space, it does not keep anything correct.
        "tracely.prune_chats-nightly": {
            "task": "tracely.prune_chats",
            "schedule": crontab(hour="4", minute="17"),
        },
        # Plan retention (hosted cloud only): the free tier keeps a week, the table TTL keeps
        # the rest. Offset from the chat prune so two sweeps don't share the solo worker's slot.
        "tracely.enforce_retention-nightly": {
            "task": "tracely.enforce_retention",
            "schedule": crontab(hour="4", minute="47"),
        },
    },
    timezone="UTC",
)


@task_postrun.connect
def _stamp_worker_alive(**_: object) -> None:
    """The worker's pulse: "a task finished just now". A deep queue is only an incident when
    nothing is coming off it, and this is what tells those two apart (`domain/ops/selfcheck.py`).
    Imported lazily — this module is imported by the API producer too."""
    from tracely.services.selfcheck_service import LAST_TASK_KEY, stamp

    stamp(LAST_TASK_KEY)
