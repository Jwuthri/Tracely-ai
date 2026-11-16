"""Turn a thing that happened into the event payload an alert flow renders against.

One builder per trigger, and BOTH callers use them: the pipeline hooks (a gate finished FAIL, a
turn failed, a cluster was created) and the `/test` endpoint (run this rule against a real subject
I pick). That is deliberate — a test that built its context differently from production would be
a test of the wrong thing.

The event is a flat dict with the context groups on it (`trace`, `gate`, `cluster`, `metric`,
`scores`, …) plus the fields the matcher and the legacy channel payload need (`type`, `agent`,
`env`, `summary`, `text`, `path`). `domain/alerting/context.build_context` shapes it into the Jinja
namespace; nothing here renders anything.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from tracely.config import settings
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.spans import TraceReader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.models import Agent, FailureCluster, GateRun, Project

log = structlog.get_logger()

_TEXT_LIMIT = 4000


def _url(path: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/{path.lstrip('/')}" if path else ""


def _clip(value: Any, limit: int = _TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _agent_slug(session: Session, agent_id: str) -> str:
    if not agent_id:
        return ""
    agent = session.get(Agent, agent_id)
    return agent.slug if agent else ""


def project_name(session: Session, project_id: str) -> str:
    p = session.get(Project, project_id)
    return p.name if p else ""


# ── trace_failed ──────────────────────────────────────────────────────────────


def trace_event(
    session: Session,
    project_id: str,
    trace_id: str,
    *,
    failing: list[dict] | None = None,
    reader: TraceReader | None = None,
) -> dict[str, Any]:
    """"A conversation broke": the failing turn, its I/O, and what the judges said.

    `failing` is the caller's already-computed list of non-advisory failures
    (`[{name, comment}]`) — the eval path has them in hand. Without it (the `/test` path) they are
    read back from the trace's own scores, minus this project's advisory evaluators, so the alert
    still agrees with the badge.
    """
    tr = reader or TraceReader()
    spans = tr.read_spans(project_id, trace_id)
    root = root_span(spans) if spans else {}
    scores = tr.scores_by_trace(project_id, [trace_id]).get(trace_id, [])
    if failing is None:
        advisory = set(repo.advisory_score_names(session, project_id))
        failing = [
            {"name": s["name"], "comment": s.get("comment") or ""}
            for s in scores
            if s.get("verdict") == "FAIL" and s["name"] not in advisory
        ]
    names = [f["name"] for f in failing]
    reason = "; ".join(f["comment"] for f in failing if f.get("comment"))[:_TEXT_LIMIT]
    latency, tokens, _ = tr.candidate_metrics(project_id, [trace_id])
    slug = _agent_slug(session, str(root.get("agent_id") or ""))
    where = slug or "agent"
    summary = (
        f"{', '.join(names)} FAILED on {where}" + (f" — {reason}" if reason else "")
        if names
        else f"Turn on {where} failed"
    )
    thread_id = str(root.get("conversation_id") or "") or trace_id
    return {
        "type": "trace_failed",
        "agent": slug,
        "agent_id": str(root.get("agent_id") or ""),
        "env": str(root.get("env") or ""),
        "subject_id": trace_id,
        "summary": summary,
        # What `contains` matches: evaluator names AND the judges' own words.
        "text": " ".join(names) + " " + reason,
        "path": f"/traces/{trace_id}",
        "url": _url(f"/traces/{trace_id}"),
        "score_names": names,
        "ref": {"trace_id": trace_id, "thread_id": thread_id},
        "trace": {
            "id": trace_id,
            "url": _url(f"/traces/{trace_id}"),
            "thread_id": thread_id,
            "input": _clip(root.get("input")),
            "output": _clip(root.get("output")),
            "error": _clip(root.get("status_message"), 500),
            "latency_ms": round(latency),
            "tokens": tokens,
            "cost_usd": 0.0,
        },
        "scores": [
            {
                "name": s["name"],
                "verdict": s.get("verdict") or "",
                "value": s.get("value"),
                "comment": _clip(s.get("comment"), 500),
            }
            for s in scores
        ],
        "failing_evaluators": names,
        "failure_reason": reason,
    }


# ── gate_failed ───────────────────────────────────────────────────────────────


def gate_event(session: Session, gate: GateRun) -> dict[str, Any]:
    """"The gate failed": the run, its counts, the branch, the PR, the soft warnings."""
    slug = _agent_slug(session, gate.agent_id) or gate.agent_id
    summary = (
        f"CI gate {gate.status} on {slug} ({gate.env or 'ci'}) — "
        f"{gate.failed} failed / {gate.passed} passed / {gate.skipped} skipped"
    )
    if gate.git_ref:
        summary += f" · {gate.git_ref}"
    if gate.pr_number:
        summary += f" · PR #{gate.pr_number}"
    warnings = list(gate.warnings or [])
    return {
        "type": "gate_failed",
        "agent": _agent_slug(session, gate.agent_id),
        "agent_id": gate.agent_id,
        "env": gate.env or "",
        "subject_id": gate.id,
        "summary": summary,
        "text": f"{summary} {'; '.join(warnings)}",
        "path": f"/gates/{gate.id}",
        "url": _url(f"/gates/{gate.id}"),
        "ref": {
            "gate_run_id": gate.id,
            "status": gate.status,
            "pr_number": gate.pr_number,
            "git_ref": gate.git_ref,
        },
        "gate": {
            "id": gate.id,
            "url": _url(f"/gates/{gate.id}"),
            "status": gate.status,
            "env": gate.env or "",
            "git_ref": gate.git_ref or "",
            "pr_number": gate.pr_number or 0,
            "passed": gate.passed or 0,
            "failed": gate.failed or 0,
            "skipped": gate.skipped or 0,
            "warnings": warnings,
        },
    }


# ── cluster_new ───────────────────────────────────────────────────────────────


def cluster_event(session: Session, cluster: FailureCluster) -> dict[str, Any]:
    """"A new failure mode": the cluster nothing had produced before."""
    slug = _agent_slug(session, cluster.agent_id)
    return {
        "type": "cluster_new",
        "agent": slug,
        "agent_id": cluster.agent_id,
        "env": "",
        "subject_id": cluster.id,
        "summary": f"New failure mode on {slug or cluster.agent_id}: {cluster.label}",
        "text": f"{cluster.label} {cluster.taxonomy} {cluster.signature or ''}",
        "path": f"/clusters/{cluster.id}",
        "url": _url(f"/clusters/{cluster.id}"),
        "ref": {"cluster_id": cluster.id, "taxonomy": cluster.taxonomy},
        "cluster": {
            "id": cluster.id,
            "url": _url(f"/clusters/{cluster.id}"),
            "label": cluster.label or "",
            "taxonomy": cluster.taxonomy or "",
        },
    }


# ── threshold triggers ────────────────────────────────────────────────────────


def metric_event(monitor: Any, verdict: Any) -> dict[str, Any]:
    """"A rate crossed a line": the polled half, from the condition verdict the beat computed."""
    cond = monitor.condition or {}
    return {
        "type": str(cond.get("type") or ""),
        "agent": monitor.target_agent or "",
        "agent_id": "",
        "env": "",
        "subject_id": monitor.id,
        "summary": verdict.summary,
        "text": verdict.summary,
        "path": "/settings/alerts",
        "url": _url("/settings/alerts"),
        "ref": {"score": verdict.score, "sample_size": verdict.sample_size},
        "metric": {
            "name": str(cond.get("score_name") or cond.get("type") or ""),
            "value": verdict.score if verdict.score is not None else 0,
            "threshold": float(cond.get("threshold") or 0),
            "window_minutes": int(cond.get("window_minutes") or 0),
            "sample_size": verdict.sample_size,
        },
    }


# ── the test picker ───────────────────────────────────────────────────────────


def subjects_for_trigger(session: Session, project_id: str, trigger: str, limit: int = 20) -> list[dict]:
    """Real things in this workspace a rule can be tested against — the "run it on this one"
    dropdown. Empty for threshold triggers: those are tested against the live window instead."""
    if trigger == "gate_failed":
        rows = session.execute(
            select(GateRun)
            .where(GateRun.project_id == project_id)
            .order_by(desc(GateRun.created_at))
            .limit(limit)
        ).scalars()
        return [
            {
                "id": g.id,
                "label": f"{g.status} · {g.env or 'ci'} · {g.git_ref or g.id[:8]}",
                "detail": f"{g.failed} failed / {g.passed} passed",
            }
            for g in rows
        ]
    if trigger == "cluster_new":
        rows = session.execute(
            select(FailureCluster)
            .where(FailureCluster.project_id == project_id)
            .order_by(desc(FailureCluster.last_seen_at))
            .limit(limit)
        ).scalars()
        return [
            {"id": c.id, "label": c.label or c.id[:8], "detail": c.taxonomy or ""} for c in rows
        ]
    if trigger == "trace_failed":
        # Failing traces only — testing a "conversation broke" rule against a passing turn
        # renders an empty failure_reason and teaches the user nothing.
        try:
            rows = TraceReader().recent_failing_traces(project_id, limit)
        except Exception as exc:  # a CH hiccup must not break the editor
            log.warning("alert_subjects_failed", error=str(exc))
            return []
        return [
            {
                "id": r["trace_id"],
                "label": _clip(r.get("input") or r["trace_id"], 70),
                "detail": str(r.get("ts") or "")[:19],
            }
            for r in rows
        ]
    return []


def event_for_subject(
    session: Session, project_id: str, trigger: str, subject_id: str
) -> dict[str, Any] | None:
    """The event a `/test` run should use. `None` when the subject no longer exists."""
    if trigger == "trace_failed":
        return trace_event(session, project_id, subject_id)
    if trigger == "gate_failed":
        gate = session.get(GateRun, subject_id)
        if gate is None or gate.project_id != project_id:
            return None
        return gate_event(session, gate)
    if trigger == "cluster_new":
        cluster = session.get(FailureCluster, subject_id)
        if cluster is None or cluster.project_id != project_id:
            return None
        return cluster_event(session, cluster)
    return None
