"""Trends & analytics: time-series + roll-ups over traces, failures, gates, and clusters.

Pure HTTP shaping — ClickHouse series live in `infrastructure.clickhouse.async_reader`,
Postgres rollups in `infrastructure.db.repositories`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from tracely.api.advisory import advisory_score_names
from tracely.api.auth import get_project_id
from tracely.infrastructure.clickhouse import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal

router = APIRouter(prefix="/api")


@router.get("/onboarding/milestones")
async def onboarding_milestones(project_id: str = Depends(get_project_id)) -> dict:
    """The workspace's activation milestones — durable, sample-vs-real, with elapsed times."""
    from tracely.services import milestones

    def work():
        with SyncSessionLocal() as s:
            return {"items": milestones.for_project(s, project_id)}

    return await run_in_threadpool(work)


@router.get("/ops")
async def ops(days: int = 14, project_id: str = Depends(get_project_id)) -> dict:
    """Latency / throughput / cost roll-up (the observability panel on Trends)."""
    return await async_reader.ops_metrics(project_id, max(1, min(days, 90)))


@router.get("/trends")
async def trends(days: int = 14, project_id: str = Depends(get_project_id)) -> dict:
    days = max(1, min(days, 90))
    advisory = await advisory_score_names(project_id)
    daily = await async_reader.daily_trace_failures(project_id, days, advisory)
    total_traces, total_failures = await async_reader.trace_failure_totals(project_id, advisory)

    def registry():
        with SyncSessionLocal() as s:
            return repo.gate_cluster_trends(s, project_id)

    rollups = await run_in_threadpool(registry)
    return {
        "days": days,
        "daily": daily,
        "gates_daily": rollups.pop("gates_daily"),
        "summary": {
            "total_traces": total_traces,
            "total_failures": total_failures,
            "failure_rate": round(total_failures / total_traces, 3) if total_traces else 0.0,
            **rollups,
        },
    }
