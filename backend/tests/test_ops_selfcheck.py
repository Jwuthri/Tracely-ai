"""The deployment's own health verdict — the judgement only, no Redis or ClickHouse.

Each case is a real failure mode this deployment has had or would hide:
a buried worker, a dead worker behind a still-200 API, beat quietly stopping.
"""

from __future__ import annotations

from tracely.domain.ops.selfcheck import (
    INGEST_STALE_S,
    QUEUE_BACKLOG,
    WORKER_SILENT_S,
    Snapshot,
    evaluate,
    summarize,
)


def test_quiet_deployment_is_healthy():
    assert not evaluate(Snapshot()).degraded
    assert summarize(evaluate(Snapshot())) == "all clear"


def test_a_draining_burst_is_not_an_incident():
    """2k queued tasks with a worker chewing through them is a busy afternoon, not a page —
    but it is still worth saying out loud."""
    v = evaluate(Snapshot(queue_depth=QUEUE_BACKLOG + 1500, last_task_age_s=5))
    assert v.degraded
    assert "draining" in v.problems[0]
    assert "stuck" not in v.problems[0]


def test_deep_queue_plus_silent_worker_reads_as_stuck():
    v = evaluate(
        Snapshot(queue_depth=QUEUE_BACKLOG + 1, last_task_age_s=WORKER_SILENT_S + 60)
    )
    assert "looks stuck" in v.problems[0]


def test_prefetched_backlog_still_counts():
    """Celery moves prefetched tasks off the list into `unacked`, so LLEN reads ~0 while the
    worker is buried — the depth has to be the sum or the check misses the outage entirely."""
    v = evaluate(Snapshot(queue_depth=1, unacked=QUEUE_BACKLOG, last_task_age_s=WORKER_SILENT_S + 1))
    assert v.degraded


def test_a_little_work_and_a_silent_worker():
    v = evaluate(Snapshot(queue_depth=3, last_task_age_s=WORKER_SILENT_S + 1))
    assert "no task has finished" in v.problems[0]
    # …but silence with an empty queue is just an idle deployment
    assert not evaluate(Snapshot(queue_depth=0, last_task_age_s=WORKER_SILENT_S + 1)).degraded


def test_accepted_spans_that_never_landed():
    """The quiet one: ingest keeps 202-ing while nothing reaches ClickHouse."""
    v = evaluate(Snapshot(accepted_recently=True, last_trace_age_s=INGEST_STALE_S + 60))
    assert "last trace stored was" in v.problems[0]
    # unmeasurable is reported too — a check that silently skips is a check that isn't running
    assert evaluate(Snapshot(accepted_recently=True, last_trace_age_s=None)).problems == [
        "spans were accepted but the last trace stored was never"
    ]
    # no traffic + no new traces is a weekend, not an outage
    assert not evaluate(Snapshot(accepted_recently=False, last_trace_age_s=None)).degraded


def test_beat_death_is_visible():
    assert evaluate(Snapshot(beat_age_s=4 * 3600)).degraded
    assert not evaluate(Snapshot(beat_age_s=600)).degraded


def test_problems_are_summarized_for_one_line_alerts():
    v = evaluate(
        Snapshot(
            queue_depth=QUEUE_BACKLOG + 10,
            last_task_age_s=WORKER_SILENT_S + 10,
            accepted_recently=True,
            last_trace_age_s=INGEST_STALE_S + 10,
        )
    )
    assert len(v.problems) == 2
    assert "; " in summarize(v)


def test_a_filling_disk_is_an_incident_before_it_is_full():
    """The one resource whose exhaustion takes ClickHouse down with it — and the one that fills
    from ClickHouse's own logs rather than from customer traffic."""
    from tracely.domain.ops.selfcheck import DISK_FREE_PCT

    assert not evaluate(Snapshot(disk_free_pct=DISK_FREE_PCT + 1)).degraded
    v = evaluate(Snapshot(disk_free_pct=4.0))
    assert v.degraded and "disk is 4% free" in summarize(v)
    # Unmeasurable (old ClickHouse, restricted user) must not read as an empty disk.
    assert not evaluate(Snapshot(disk_free_pct=None)).degraded
