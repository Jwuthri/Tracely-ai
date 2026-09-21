"""Gather the numbers behind `domain/ops/selfcheck.py`, and say something when they're bad.

Runs on the same Celery beat that drives monitors. Two consumers:
  • the beat task — logs `selfcheck` every tick (so log-based alerting has a heartbeat to watch)
    and pushes to the operator's channels when the verdict is degraded, deduped so a long outage
    pages once an hour rather than every five minutes;
  • `GET /health/queue` — the same snapshot on demand, for a dashboard or an uptime probe.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog

from tracely.config import settings
from tracely.domain.ops.selfcheck import Snapshot, Verdict, evaluate, summarize
from tracely.infrastructure.notifications.dispatch import dispatch_alert

log = structlog.get_logger()

# Redis keys the worker stamps as it goes — cheap heartbeats, no extra store.
LAST_TASK_KEY = "tracely:ops:last_task"
LAST_ACCEPT_KEY = "tracely:ops:last_accept"
BEAT_KEY = "tracely:ops:last_beat"
ALERT_KEY = "tracely:ops:last_alert"
ALERT_EVERY_S = 60 * 60


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url)


def stamp(key: str) -> None:
    """Record 'this just happened'. Best-effort: a Redis blip must never break the thing being
    stamped — the check degrades to 'could not be measured', which the verdict reports."""
    try:
        _redis().set(key, str(time.time()))
    except Exception as exc:  # noqa: BLE001
        log.debug("selfcheck_stamp_failed", key=key, error=str(exc))


def _age(client, key: str) -> float | None:
    try:
        raw = client.get(key)
        return time.time() - float(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


async def snapshot() -> Snapshot:
    """Measure the deployment. Every field is best-effort; None means unmeasurable."""
    queue_depth = unacked = 0
    last_task = last_accept = beat = None
    try:
        client = _redis()
        queue_depth = int(client.llen(settings.celery_queue or "ingestion") or 0)
        # Prefetched tasks leave the list, so LLEN alone reads 0 while the worker is buried —
        # Celery parks those in the `unacked` hash (verified: a backlog is invisible without it).
        unacked = int(client.hlen("unacked") or 0)
        last_task, last_accept, beat = (
            _age(client, LAST_TASK_KEY),
            _age(client, LAST_ACCEPT_KEY),
            _age(client, BEAT_KEY),
        )
    except Exception as exc:  # noqa: BLE001 — Redis down IS the finding, not a crash
        log.warning("selfcheck_redis_unreachable", error=str(exc))

    last_trace_age = disk_free_pct = None
    try:
        from tracely.infrastructure.clickhouse.client import get_async_client

        ch = await get_async_client()
        rows = (await ch.query("SELECT max(start_time) FROM events")).result_rows
        newest = rows[0][0] if rows and rows[0] else None
        if isinstance(newest, datetime):
            newest = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
            last_trace_age = (datetime.now(timezone.utc) - newest).total_seconds()
        # The store's own disk. One query, same connection: a full disk is the failure mode that
        # takes ClickHouse down entirely, and it arrives gradually enough to be caught.
        disks = (
            await ch.query(
                "SELECT free_space, total_space FROM system.disks ORDER BY total_space DESC LIMIT 1"
            )
        ).result_rows
        if disks and disks[0][1]:
            disk_free_pct = 100.0 * float(disks[0][0]) / float(disks[0][1])
    except Exception as exc:  # noqa: BLE001
        log.warning("selfcheck_clickhouse_unreachable", error=str(exc))

    return Snapshot(
        queue_depth=queue_depth,
        unacked=unacked,
        last_task_age_s=last_task,
        last_trace_age_s=last_trace_age,
        # "Accepted recently" is what makes a stale store damning rather than just quiet: no
        # traffic and no new traces is a weekend, traffic and no new traces is an outage.
        accepted_recently=last_accept is not None and last_accept < 60 * 60,
        beat_age_s=beat,
        disk_free_pct=disk_free_pct,
    )


def _channels() -> list[dict]:
    """Operator alert channels, from config — the same channel shape monitors use."""
    url = settings.ops_alert_webhook
    if not url:
        return []
    return [{"type": "slack" if "hooks.slack.com" in url else "webhook", "url": url}]


def _should_alert(client) -> bool:
    """Once per ALERT_EVERY_S while degraded. An outage that pages every beat tick trains people
    to mute the channel, which is worse than not alerting at all."""
    try:
        if _age(client, ALERT_KEY) is not None and (_age(client, ALERT_KEY) or 0) < ALERT_EVERY_S:
            return False
        client.set(ALERT_KEY, str(time.time()))
    except Exception:  # noqa: BLE001
        return True  # can't dedup → still alert; a duplicate page beats a missed one
    return True


async def run() -> dict:
    """One beat tick: measure, log, and alert if degraded. Never raises."""
    stamp(BEAT_KEY)
    snap = await snapshot()
    verdict: Verdict = evaluate(snap)
    payload = {
        "queue_depth": snap.queue_depth,
        "unacked": snap.unacked,
        "last_task_age_s": snap.last_task_age_s,
        "last_trace_age_s": snap.last_trace_age_s,
        "beat_age_s": snap.beat_age_s,
        "disk_free_pct": snap.disk_free_pct,
        "degraded": verdict.degraded,
        "problems": verdict.problems,
    }
    if verdict.degraded:
        log.warning("selfcheck_degraded", **payload)
        channels = _channels()
        if channels:
            try:
                if _should_alert(_redis()):
                    dispatch_alert(
                        channels,
                        title="Tracely deployment is degraded",
                        summary=summarize(verdict),
                        webhook_payload=payload,
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("selfcheck_alert_failed", error=str(exc))
    else:
        log.info("selfcheck", **payload)
    return payload
