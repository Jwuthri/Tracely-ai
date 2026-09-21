"""What "the deployment is unwell" means, as pure functions.

Tracely's failure modes are quiet ones: the worker dies and ingest keeps 202-ing while nothing
lands in ClickHouse; a slow evaluator backs the queue up until traces are hours stale; beat stops
and monitors never fire again. None of that turns the API red, so nothing pages anyone — today it
is found by a human opening the UI and noticing the numbers stopped moving.

This module holds only the judgement (no Redis, no ClickHouse); `services/selfcheck_service.py`
gathers the numbers and delivers the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Thresholds are deliberately generous: a page nobody trusts is worse than no page. They describe
# "something is actually wrong", not "something is slower than I'd like".
QUEUE_BACKLOG = 500  # tasks waiting on the default queue
WORKER_SILENT_S = 15 * 60  # no task has finished in this long, while work is queued
INGEST_STALE_S = 60 * 60  # nothing reached ClickHouse in this long, while spans were accepted
# Disk is the one resource whose exhaustion takes the store down with it, and ClickHouse fills it
# with its own telemetry long before customer traces do (`clickhouse/maintenance.py` caps that
# nightly). Warn with enough room left to act — a full disk is a restore, not a fix.
DISK_FREE_PCT = 15.0


@dataclass(frozen=True)
class Snapshot:
    """Everything the checks read. All ages are seconds; `None` means "could not be measured",
    which is itself reported — a check that silently skips is a check that isn't running."""

    queue_depth: int = 0
    unacked: int = 0
    last_task_age_s: float | None = None
    last_trace_age_s: float | None = None
    accepted_recently: bool = False
    beat_age_s: float | None = None
    disk_free_pct: float | None = None


@dataclass(frozen=True)
class Verdict:
    problems: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.problems)


def evaluate(s: Snapshot) -> Verdict:
    """The problems worth waking someone for, in the order they'd be diagnosed."""
    problems: list[str] = []

    # A backlog only matters if it isn't draining: a burst of 2k tasks with a worker chewing
    # through them is healthy, the same 2k with a silent worker is an outage.
    stalled_worker = s.last_task_age_s is not None and s.last_task_age_s > WORKER_SILENT_S
    if (s.queue_depth + s.unacked) > QUEUE_BACKLOG and stalled_worker:
        problems.append(
            f"{s.queue_depth + s.unacked} tasks queued and nothing has finished in "
            f"{int((s.last_task_age_s or 0) / 60)}m — the worker looks stuck"
        )
    elif (s.queue_depth + s.unacked) > QUEUE_BACKLOG:
        problems.append(f"{s.queue_depth + s.unacked} tasks queued (draining, but deep)")
    elif stalled_worker and (s.queue_depth + s.unacked) > 0:
        problems.append(f"work is queued but no task has finished in {int(s.last_task_age_s / 60)}m")

    # The quiet one: the API kept accepting spans (202) and none of them landed. That is exactly
    # what a dead worker looks like from the outside — traffic is fine, data stops.
    if s.accepted_recently and (s.last_trace_age_s is None or s.last_trace_age_s > INGEST_STALE_S):
        age = "never" if s.last_trace_age_s is None else f"{int(s.last_trace_age_s / 60)}m ago"
        problems.append(f"spans were accepted but the last trace stored was {age}")

    # Running out of disk ends the deployment, and the nightly sweeps (system-log TTL, plan
    # retention, chat prune) only help if someone hears about it while there is still room.
    if s.disk_free_pct is not None and s.disk_free_pct < DISK_FREE_PCT:
        problems.append(
            f"ClickHouse disk is {s.disk_free_pct:.0f}% free — check `system.*` log growth "
            "(scripts/storage_report.py) and the nightly cap"
        )

    # Beat drives the monitors; when it dies, alerting dies silently — including this check.
    if s.beat_age_s is not None and s.beat_age_s > 3 * 60 * 60:
        problems.append(f"celery beat last ticked {int(s.beat_age_s / 3600)}h ago")

    return Verdict(problems=problems)


def summarize(v: Verdict) -> str:
    return "; ".join(v.problems) if v.problems else "all clear"
