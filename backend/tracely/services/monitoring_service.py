"""Run a project's (or every project's) enabled monitors — the two halves of alerting.

**Polled** monitors (threshold over a window) are the beat's job: pull the window of samples from
ClickHouse, evaluate each condition (`domain.monitoring.conditions`), dispatch alerts to the
configured channels, and persist the monitor's `last_evaluated_at` / `last_fired_at` /
`last_fired_summary`.

Dedup: a monitor that just fired won't notify again until `min_interval_seconds` has passed
since `last_fired_at` (the engine still RE-evaluates, so `last_evaluated_at` updates every tick).

**Event** monitors are the pipeline's job: `notify_event` is called inline the moment the thing
happens (a CI gate finished FAIL, a conversation failed an evaluator, a new failure cluster
appeared) and notifies every enabled monitor whose condition matches. Sync, best-effort, and
deliberately NOT queued — an alert that arrives after the deploy is not an alert. The beat skips
event monitors (`is_event_condition`) so it never overwrites their state with a window it can't
evaluate.

Both halves share the channels, the `min_interval_seconds` dedup and the `last_fired_*` columns,
so the UI shows one list whichever way a monitor is triggered.

Async because every ClickHouse read in this codebase is async (via `async_reader`); the
Celery beat wrapper calls into `evaluate_all` via `asyncio.run`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy.orm import Session

from tracely.api.advisory import advisory_score_names
from tracely.config import settings
from tracely.domain.monitoring.conditions import (
    Sample,
    Verdict,
    evaluate_condition,
    event_matches,
    is_event_condition,
)
from tracely.infrastructure.spans import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Monitor
from tracely.infrastructure.notifications import dispatch_alert
from tracely.services import alert_events
from tracely.services.alert_flow_service import has_flow, run_flow

log = structlog.get_logger()


def _abs_url(path: str) -> str:
    """Absolute app URL for a relative path — the clickable link in every alert payload."""
    return f"{settings.app_base_url.rstrip('/')}/{path.lstrip('/')}" if path else ""


def _view_url(monitor: Monitor) -> str:
    """Where a polled alert points: the alerts screen, which shows this monitor's current state."""
    return _abs_url("/settings/alerts")


async def _samples_for(monitor: Monitor) -> list[Sample]:
    """Pull the window of samples the monitor's condition needs. The condition `type` decides
    whether we read the score table (per-evaluator) or the trace verdict (project-level)."""
    cond = monitor.condition or {}
    cond_type = str(cond.get("type") or "").strip()
    window_minutes = max(int(cond.get("window_minutes") or 60), 1)
    if cond_type in ("fail_rate_over", "score_below"):
        score_name = str(cond.get("score_name") or "").strip()
        if not score_name:
            return []
        rows = await async_reader.score_samples_in_window(
            monitor.project_id, score_name, window_minutes, monitor.target_agent or ""
        )
        return [Sample(verdict=r["verdict"], value=r["value"]) for r in rows]
    if cond_type == "trace_failure_rate":
        adv = await advisory_score_names(monitor.project_id)
        rows = await async_reader.trace_failure_samples_in_window(
            monitor.project_id, window_minutes, adv, monitor.target_agent or ""
        )
        return [Sample(verdict=r["verdict"], value=None) for r in rows]
    return []


def _should_notify(monitor: Monitor, now: datetime) -> bool:
    """Anti-spam: re-notify only when `min_interval_seconds` has passed since `last_fired_at`."""
    last = monitor.last_fired_at
    if last is None:
        return True
    elapsed = (now - last).total_seconds()
    return elapsed >= float(monitor.min_interval_seconds or 0)


async def _evaluate_monitor(monitor: Monitor, session: Session) -> dict:
    """One tick for one monitor — pull samples, evaluate condition, dispatch alerts, update row.
    Returns a small dict for the caller to log/aggregate."""
    now = datetime.now(timezone.utc)
    try:
        samples = await _samples_for(monitor)
    except Exception as exc:  # CH hiccup must not crash the whole evaluator loop
        log.warning("monitor_samples_failed", monitor_id=monitor.id, error=str(exc))
        return {"id": monitor.id, "status": "samples_error"}

    verdict: Verdict = evaluate_condition(monitor.condition or {}, samples)

    # Always update last_evaluated_at + (a fresh) `last_fired_summary` so the UI shows the
    # current state even when the condition is quiet ("avg score 0.78 (≥0.60) over 23 samples").
    monitor.last_evaluated_at = now
    monitor.last_fired_summary = verdict.summary[:500]
    fired = verdict.fires and _should_notify(monitor, now)
    delivered = {"ok": 0, "fail": 0, "skipped": 0}
    execution_id = ""
    if fired and has_flow(monitor):
        # The action is a flow: run the steps instead of POSTing to channels.
        monitor.last_fired_at = now
        session.commit()
        event = alert_events.metric_event(monitor, verdict)
        event["alert"] = _alert_group(monitor, event, now, session)
        ex = run_flow(session, monitor, event)
        execution_id = ex.id
    elif fired:
        monitor.last_fired_at = now
        delivered = dispatch_alert(
            monitor.channels or [],
            title=f"{monitor.name} fired",
            summary=verdict.summary,
            view_url=_view_url(monitor),
            webhook_payload={
                "source": "tracely",
                "event": "monitor.fired",
                "monitor": {
                    "id": monitor.id, "name": monitor.name, "project_id": monitor.project_id,
                },
                "title": f"{monitor.name} fired",
                "summary": verdict.summary,
                "score": verdict.score,
                "sample_size": verdict.sample_size,
                "view_url": _view_url(monitor),
                "fired_at": now.isoformat(),
            },
        )
    session.commit()
    return {
        "id": monitor.id,
        "fired": fired,
        "evaluated": verdict.skipped_reason == "",
        "skipped_reason": verdict.skipped_reason,
        "sample_size": verdict.sample_size,
        "score": verdict.score,
        "delivered": delivered,
        "execution_id": execution_id,
    }


def _alert_group(monitor: Monitor, event: dict, now: datetime, session: Session) -> dict:
    """The `alert.*` half of the template namespace — per monitor, because the name is the
    monitor's and the URL points at whatever fired it."""
    return {
        "name": monitor.name,
        "trigger": str(event.get("type") or ""),
        "summary": str(event.get("summary") or ""),
        "url": _abs_url(str(event.get("path") or "")),
        "fired_at": now.isoformat(),
        "project": alert_events.project_name(session, monitor.project_id),
    }


class MonitoringService:
    """Evaluator for the enabled monitors across a project, or across every project (the Celery
    beat fan-out). Stateless — open a sync Session per call (matches the rest of the codebase)."""

    async def evaluate_all(self) -> dict:
        """Every enabled monitor in every project. Called by the beat task."""
        with SyncSessionLocal() as s:
            monitors = repo.enabled_monitors_across_projects(s)
            results = []
            for m in monitors:
                # Event monitors are fired by the pipeline, not polled — evaluating one here would
                # replace its `last_fired_summary` with "unknown condition type" every 5 minutes.
                if is_event_condition(m.condition or {}):
                    continue
                results.append(await _evaluate_monitor(m, s))
        fired = sum(1 for r in results if r.get("fired"))
        return {"monitors": len(results), "fired": fired, "results": results}

    async def evaluate_one(self, project_id: str, monitor_id: str) -> dict | None:
        """One specific monitor (the API `/test` endpoint, e.g. "what does this look like now?").

        For an event monitor there is no window to evaluate, so `/test` sends a sample alert down
        its channels instead — which is the thing you actually want to test before arming it."""
        with SyncSessionLocal() as s:
            monitor = repo.monitor_get(s, project_id, monitor_id)
            if monitor is None:
                return None
            if is_event_condition(monitor.condition or {}):
                return _send_test_alert(monitor)
            return await _evaluate_monitor(monitor, s)


def _send_test_alert(monitor: Monitor) -> dict:
    """A "this is what it will look like" alert down every channel of an event monitor. Bypasses
    the dedup interval and writes nothing — a test must never make the real alert go quiet."""
    delivered = dispatch_alert(
        monitor.channels or [],
        title=f"{monitor.name} (test)",
        summary=(
            f"Test alert from Tracely. This monitor fires on "
            f"{(monitor.condition or {}).get('type') or 'an event'}."
        ),
        view_url=_abs_url("/settings/alerts"),
        webhook_payload={
            "source": "tracely",
            "event": "monitor.test",
            "monitor": {
                "id": monitor.id, "name": monitor.name, "project_id": monitor.project_id,
            },
            "condition": monitor.condition or {},
            "title": f"{monitor.name} (test)",
            "summary": "Test alert from Tracely.",
            "view_url": _abs_url("/settings/alerts"),
        },
    )
    return {"id": monitor.id, "fired": False, "test": True, "delivered": delivered}


def notify_event(project_id: str, event: dict) -> dict:
    """Fire every enabled event monitor in `project_id` that matches `event`.

    Called inline from the pipeline that produced the event (eval, gate, clustering), so it is
    sync and it swallows nothing quietly but everything loudly: the CALLER wraps this in
    try/except, because a monitor is an observer and an observer must never fail the thing it
    observes.

    `event` keys: `type` (one of `EVENT_TYPES`), `summary` (the alert line), `text` (what
    `contains` matches — defaults to `summary`), `path` (relative app link), plus the optional
    `agent` / `agent_id` / `env` / `score_names` filters and a `ref` dict of ids merged into the
    webhook payload.
    """
    etype = str(event.get("type") or "")
    summary = str(event.get("summary") or "")[:500]
    # `contains` matches `text`; the summary is the sane default for the callers that have nothing
    # longer to offer than the line they'd put in Slack.
    event = {**event, "text": str(event.get("text") or event.get("summary") or "")}
    view_url = _abs_url(str(event.get("path") or ""))
    now = datetime.now(timezone.utc)
    fired = 0
    with SyncSessionLocal() as s:
        for m in repo.monitors_list(s, project_id):
            if not m.enabled or not event_matches(m.condition or {}, event, m.target_agent or ""):
                continue
            m.last_evaluated_at = now
            # Rate limit, not deduplication: two distinct gates failing inside the interval mean
            # one alert. That is the point of the knob — set it to 0 for "tell me every time".
            if not _should_notify(m, now):
                continue
            m.last_fired_at = now
            m.last_fired_summary = summary
            fired += 1
            if has_flow(m):
                s.commit()  # the run appends execution rows; don't hold the monitor update open
                run_flow(s, m, {**event, "alert": _alert_group(m, event, now, s)})
                continue
            dispatch_alert(
                m.channels or [],
                title=f"{m.name} fired",
                summary=summary,
                view_url=view_url,
                webhook_payload={
                    "source": "tracely",
                    "event": f"monitor.{etype}",
                    "monitor": {"id": m.id, "name": m.name, "project_id": project_id},
                    "title": f"{m.name} fired",
                    "summary": summary,
                    "score_names": list(event.get("score_names") or []),
                    "agent": event.get("agent") or "",
                    "env": event.get("env") or "",
                    "view_url": view_url,
                    "fired_at": now.isoformat(),
                    **(event.get("ref") or {}),
                },
            )
        s.commit()
    if fired:
        log.info("monitor_event", project_id=project_id, type=etype, fired=fired)
    return {"fired": fired}
