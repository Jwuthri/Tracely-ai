"""Plan-scoped trace retention — the nightly sweep behind `tracely.enforce_retention`.

The ClickHouse tables carry one global TTL (90 days, `ddl/0003_events_ttl`) and a plan lives in
Postgres, so ClickHouse cannot express "free keeps a week". This closes that gap: read every
workspace's plan, group the workspaces by the window their plan buys, and delete what has aged
past it. The TTL stays the floor — this only ever shortens.

Hosted-cloud only. Like the quota, every function is a no-op unless `BILLING_ENABLED`: on a
self-hosted deployment every org sits on the default `free` plan, and sweeping those to a week
would delete the operator's own data.
"""

from __future__ import annotations

from collections import defaultdict

import structlog

from tracely.config import settings
from tracely.domain.billing import retention_days_for
from tracely.infrastructure.clickhouse.deletes import delete_expired
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.repositories import project_plans

log = structlog.get_logger()


def enforce() -> dict:
    """Sweep every workspace past its plan's retention window. Returns `{swept, spans}`."""
    if not settings.billing_enabled:
        return {"swept": 0, "spans": 0, "skipped": "billing_disabled"}

    with SyncSessionLocal() as s:
        plans = project_plans(s)

    by_days: dict[int, list[str]] = defaultdict(list)
    for project_id, plan in plans:
        days = retention_days_for(plan, settings.free_retention_days, settings.pro_retention_days)
        if days:
            by_days[days].append(project_id)

    swept = spans = 0
    for days, project_ids in sorted(by_days.items()):
        # One DELETE per window, not per workspace: a mutation is a table-level operation, and
        # the free tier is where the rows (and the workspaces) are.
        n = delete_expired(project_ids, days)
        if n:
            swept += len(project_ids)
            spans += n
            log.info("retention_swept", days=days, projects=len(project_ids), spans=n)
    return {"swept": swept, "spans": spans}
