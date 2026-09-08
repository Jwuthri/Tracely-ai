"""Activation milestones (W6): the durable, per-workspace record of the first time each real
outcome happened — emitted from confirmed successful operations, never from a button click.

    first_trace_received     a non-internal trace was ingested
    first_check_completed    an evaluator produced a result on a trace
    source_failure_confirmed a promoted case's source fails its contract
    case_reproduced          a candidate replay under recorded execution reproduced the failure
    candidate_verified       a specific candidate passed the contract (mark_verified)
    ci_check_completed       a run-scoped gate finished PASS or FAIL with cases in it

Each row says whether it came from SAMPLE data (the seeded demo / anything stamped
`tracely.sample`), the integration path, and how long after the workspace's first trace it
landed. Idempotent: the first occurrence wins, retries are no-ops. Mirrored to PostHog when a
server key is configured — identity and durations only, never prompts/outputs/content.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tracely.config import settings
from tracely.infrastructure.db.models import ProjectMilestone

log = structlog.get_logger()

MILESTONES = (
    "first_trace_received",
    "first_check_completed",
    "source_failure_confirmed",
    "case_reproduced",
    "candidate_verified",
    "ci_check_completed",
)
SAMPLE_ENVS = frozenset({"demo", "sample"})


def is_sample(spans: list[dict]) -> bool:
    """Sample data is anything the seeded demo produced (`TRACELY_SAMPLE` stamps every span
    `tracely.sample=true`) or a run tagged env demo/sample."""
    for s in spans:
        meta = s.get("metadata") or {}
        if str(meta.get("tracely.sample", "")).lower() in ("true", "1"):
            return True
        if str(s.get("env") or "") in SAMPLE_ENVS:
            return True
    return False


def integration_of(spans: list[dict]) -> str:
    """The instrumentation path, from what the first span says about itself (scope name / SDK
    language / source) — e.g. `tracely`, `openinference.instrumentation.openai`, `otlp`."""
    for s in spans:
        scope = str(s.get("scope_name") or "")
        if scope:
            return scope[:64]
        lang = str(s.get("telemetry_sdk_language") or "")
        if lang:
            return f"otlp:{lang}"[:64]
    return str((spans[0].get("source") if spans else "") or "unknown")[:64]


def record(
    session: Session,
    project_id: str,
    name: str,
    *,
    sample: bool = False,
    integration: str = "",
    meta: dict | None = None,
) -> bool:
    """Record the first occurrence of `name` for the workspace. Returns True when this call was
    the first. Commits its own row; never raises (activation must not fail the operation)."""
    if name not in MILESTONES:
        raise ValueError(f"unknown milestone {name}")
    try:
        existing = session.get(ProjectMilestone, (project_id, name))
        if existing is not None:
            return False
        now = datetime.now(timezone.utc)
        first = session.get(ProjectMilestone, (project_id, "first_trace_received"))
        elapsed = None
        if first is not None and first.first_at is not None:
            elapsed = int((now - first.first_at.replace(tzinfo=first.first_at.tzinfo or timezone.utc)).total_seconds() * 1000)
        session.add(
            ProjectMilestone(
                project_id=project_id, name=name, first_at=now, sample=sample,
                integration=integration[:64], elapsed_ms=elapsed, meta=meta or {},
            )
        )
        session.commit()
    except Exception as exc:  # a second writer won the race, or the table is missing
        session.rollback()
        log.warning("milestone_record_failed", project_id=project_id, name=name, error=str(exc))
        return False
    _capture(project_id, name, sample, integration, elapsed)
    return True


def record_own(project_id: str, name: str, **kw) -> bool:
    """`record` in a session of its own. Callers in the middle of their own transaction (the
    gate, a promote) must use this: a milestone write can never roll back — or commit — the
    operation it observes."""
    try:
        from tracely.infrastructure.db.engine import SyncSessionLocal

        with SyncSessionLocal() as s:
            return record(s, project_id, name, **kw)
    except Exception as exc:  # noqa: BLE001
        log.warning("milestone_record_failed", project_id=project_id, name=name, error=str(exc))
        return False


def _capture(project_id: str, name: str, sample: bool, integration: str, elapsed_ms: int | None) -> None:
    if not settings.posthog_api_key:
        return
    try:
        import posthog

        posthog.api_key = settings.posthog_api_key
        posthog.host = settings.posthog_host
        posthog.capture(
            distinct_id=f"project:{project_id}",
            event=f"milestone:{name}",
            properties={
                "project_id": project_id, "sample": sample, "integration": integration,
                "elapsed_ms": elapsed_ms,
            },
        )
    except Exception as exc:  # noqa: BLE001 — analytics must never fail the product
        log.warning("milestone_capture_failed", name=name, error=str(exc))


def for_project(session: Session, project_id: str) -> list[dict]:
    rows = session.execute(
        select(ProjectMilestone).where(ProjectMilestone.project_id == project_id)
    ).scalars()
    by_name = {r.name: r for r in rows}
    return [
        {
            "name": n,
            "first_at": by_name[n].first_at.isoformat() if n in by_name and by_name[n].first_at else None,
            "sample": by_name[n].sample if n in by_name else None,
            "integration": by_name[n].integration if n in by_name else "",
            "elapsed_ms": by_name[n].elapsed_ms if n in by_name else None,
        }
        for n in MILESTONES
    ]


def funnel(session: Session) -> dict:
    """Deployment-wide counts per milestone, real vs sample, plus the drop-off between
    consecutive milestones — the founder's view, no analytics subsystem required."""
    rows = session.execute(
        select(ProjectMilestone.name, ProjectMilestone.sample, func.count())
        .group_by(ProjectMilestone.name, ProjectMilestone.sample)
    ).all()
    counts = {n: {"real": 0, "sample": 0} for n in MILESTONES}
    for name, sample, c in rows:
        if name in counts:
            counts[name]["sample" if sample else "real"] += int(c)
    steps = []
    prev = None
    for n in MILESTONES:
        real = counts[n]["real"]
        steps.append({
            "name": n, **counts[n],
            "drop_off": (prev - real) if prev is not None else None,
        })
        prev = real
    return {"steps": steps, "projects_started": counts["first_trace_received"]["real"]}
