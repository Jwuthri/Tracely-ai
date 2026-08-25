"""Monthly trace quota — counting (worker) and enforcement (OTLP edge).

Hosted-cloud only: every function is a no-op unless `BILLING_ENABLED`. Design rules, all of
which favor the customer:

- **Counting is exact and idempotent.** The worker SADDs each batch's non-internal trace ids
  into one Redis set per (project, month); SADD's return value is the number of ids never seen
  before, so task retries and traces whose spans arrive across many batches count once. Only
  that delta is added to the Postgres counter (the source of truth the gate reads).
- **Every failure undercounts, never overcounts, and never breaks ingest.** Redis down → the
  batch isn't counted (logged). The Postgres upsert failing after SADD → the delta is lost, not
  retried — re-running the task would re-ingest the whole batch just to chase a counter.
- **Enforcement is fail-open.** The gate answers from a 60-second Redis cache of
  (used, limit); Redis and Postgres errors both mean "allow". A quota is a product limit, not a
  security boundary — an outage must degrade to Langfuse-mode, not drop customer traces.

What counts: externally-POSTed OTLP traces (`/v1/traces`), excluding Tracely's own recordings
(`internal_kind`). Emulated scenario turns are emitted inline (never through the counting task),
so driving scenarios doesn't consume quota — unless the customer's own instrumented endpoint
exports spans for those turns, which are their spans arriving like any other trace.

Counting stays per-project (exact, dedup'd per project-month); only the gate's READ side pools:
usage is summed across every workspace in the project's ORGANIZATION, and the org's plan sets
the cap. Workspaces are containers, accounts are what's billed — so adding workspaces never adds
quota (see `_usage_from_pg`).
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tracely.config import settings
from tracely.domain.billing import PLAN_FREE, current_period, trace_limit_for
from tracely.infrastructure.db.models import Organization, Project, UsageCounter
from tracely.infrastructure.redis_client import async_redis, sync_redis

log = structlog.get_logger()

_SEEN_KEY = "tracely:quota:seen:{p}:{period}"
_GATE_KEY = "tracely:quota:gate:{p}:{period}"
# Dedupe set outlives its month by a few days so late batches of a month-boundary trace still
# dedupe; one key per project-month keeps Redis memory bounded (it shares the Celery broker).
_SEEN_TTL_S = 35 * 24 * 3600
# The gate re-reads Postgres at most once a minute per project — bounded staleness, bounded load.
_GATE_TTL_S = 60


def record_ingested_traces(
    project_id: str, trace_ids: Iterable[str], internal_ids: Iterable[str]
) -> int:
    """Count a batch's never-seen-before external traces into the project's month.

    Called from the ingest worker task, OUTSIDE its retry try/except — this function swallows
    everything, because a counting hiccup must never re-run `process_blob`. Returns how many
    new traces were counted (0 on any failure — the deliberate undercount).
    """
    if not settings.billing_enabled:
        return 0
    ids = sorted(set(trace_ids) - set(internal_ids))
    if not ids:
        return 0
    period = current_period()
    try:
        r = sync_redis()
        key = _SEEN_KEY.format(p=project_id, period=period)
        n = int(r.sadd(key, *ids))
        r.expire(key, _SEEN_TTL_S)
    except Exception as exc:
        log.warning("quota_count_redis_failed", project_id=project_id, error=str(exc))
        return 0
    if n <= 0:
        return 0
    try:
        from tracely.infrastructure.db import repositories
        from tracely.infrastructure.db.engine import SyncSessionLocal

        with SyncSessionLocal() as s:
            repositories.usage_increment(s, project_id, period, n)
            s.commit()
    except Exception as exc:
        # The ids are already in the seen-set, so this delta is gone for good — logged loudly,
        # because repeated hits here mean the quota is silently under-enforcing.
        log.warning("quota_count_pg_failed", project_id=project_id, lost=n, error=str(exc))
        return 0
    return n


async def quota_gate(project_id: str, session: AsyncSession) -> None:
    """Raise 429 when the project's month is over its plan's cap. Fail-open on any error."""
    if not settings.billing_enabled:
        return
    period = current_period()
    gate_key = _GATE_KEY.format(p=project_id, period=period)
    used: int | None = None
    limit_raw: str | None = None
    try:
        cached = await async_redis().get(gate_key)
        if cached:
            used_s, limit_raw = cached.decode().split(":", 1)
            used = int(used_s)
    except Exception:
        pass  # cache is an optimization; fall through to Postgres

    if used is None:
        try:
            used, limit = await _snapshot_from_pg(project_id, period, session)
            limit_raw = "" if limit is None else str(limit)
            try:
                await async_redis().set(gate_key, f"{used}:{limit_raw}", ex=_GATE_TTL_S)
            except Exception:
                pass
        except Exception as exc:
            log.warning("quota_gate_pg_failed", project_id=project_id, error=str(exc))
            return  # fail open

    limit = int(limit_raw) if limit_raw else None
    if limit is not None and used is not None and used >= limit:
        raise HTTPException(status_code=429, detail=_over_quota_detail(limit),
                            # Most OTLP exporters drop on 429; spec-compliant ones back off.
                            headers={"Retry-After": "3600"})


def _over_quota_detail(limit: int) -> str:
    """Why the trace was refused, and what to do about it.

    The "upgrade" half is conditional on purpose: `BILLING_ENABLED` can be on while Stripe is
    not configured (checkout answers 501), and telling someone to upgrade at a URL where they
    physically cannot pay is a dead end at the worst possible moment. Without Stripe the quota is
    still a real cap — it just has to be lifted by the operator, so say that instead.
    """
    from tracely.services.billing_service import stripe_configured

    plan = "Pro" if limit == settings.pro_trace_limit else "Free"
    reached = f"monthly trace quota reached ({limit:,} traces on the {plan} plan)"
    if stripe_configured():
        return f"{reached} — upgrade at {settings.app_base_url.rstrip('/')}/settings/billing"
    return f"{reached} — contact the operator of this deployment to raise it"


async def usage_snapshot(project_id: str, session: AsyncSession) -> dict:
    """The billing page's numbers: plan, this month's count, and the cap (None = uncapped)."""
    period = current_period()
    used, limit, plan, pooled = await _usage_from_pg(project_id, period, session)
    return {
        "billing_enabled": settings.billing_enabled,
        "plan": plan,
        "period": period,
        "traces_used": used,
        "trace_limit": limit,
        # "account" = summed across every workspace in this organization (adding workspaces
        # adds no quota); "workspace" = an org-less project counted on its own.
        "quota_scope": "account" if pooled else "workspace",
    }


async def _snapshot_from_pg(
    project_id: str, period: str, session: AsyncSession
) -> tuple[int, int | None]:
    used, limit, _plan, _pooled = await _usage_from_pg(project_id, period, session)
    return used, limit


async def _usage_from_pg(
    project_id: str, period: str, session: AsyncSession
) -> tuple[int, int | None, str, bool]:
    """(used, limit, plan, pooled). The billed entity is the ORGANIZATION: its plan sets the cap
    and its usage is the sum over all of its workspaces, so a team on one subscription can split
    work across workspaces freely and nobody mints quota by creating more. A project with no org
    (dev mode, CLI seed) falls back to its own counter on the free cap."""
    row = (
        await session.execute(
            select(Project.organization_id, Organization.plan)
            .join(Organization, Organization.id == Project.organization_id, isouter=True)
            .where(Project.id == project_id)
        )
    ).first()
    org_id = row[0] if row else None
    plan = (row[1] if row else None) or PLAN_FREE
    limit = trace_limit_for(plan, settings.free_trace_limit, settings.pro_trace_limit)
    pooled = org_id is not None
    if pooled:
        used = await session.scalar(
            select(func.coalesce(func.sum(UsageCounter.traces), 0))
            .select_from(UsageCounter)
            .join(Project, Project.id == UsageCounter.project_id)
            .where(Project.organization_id == org_id, UsageCounter.period == period)
        )
    else:
        used = await session.scalar(
            select(UsageCounter.traces).where(
                UsageCounter.project_id == project_id, UsageCounter.period == period
            )
        )
    return int(used or 0), limit, plan, pooled
