"""Alert rules: a trigger + a flow of steps, plus everything the visual editor needs.

An alert rule is one `Monitor` row: **when** (the `condition` — an event the pipeline fires, or a
threshold the beat polls) and **what happens** (`steps`, a DAG whose graph lives in `flow_layout`
as React Flow's own `{nodes, edges}` JSON, read by the engine itself). A rule with no steps falls
back to `channels`, the simple "POST here" action every pre-flow row uses.

Pure HTTP shaping: Postgres access lives in `infrastructure.db.repositories`, DAG semantics in
`domain.alerting`, execution in `services.alert_flow_service`, event assembly in
`services.alert_events`.

**Route order matters.** `/monitors/inputs/*`, `/monitors/subjects` and `/monitors/step-types` are
declared BEFORE `/monitors/{monitor_id}` — register a path parameter first and it swallows every
literal segment after it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_user
from tracely.domain.alerting import (
    BASE_INPUTS,
    catalog_for_trigger,
    declared_outputs,
    flow_layout_error,
    linear_flow_layout,
)
from tracely.domain.monitoring.conditions import EVENT_TYPES, POLLED_TYPES, is_event_condition
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Monitor, MonitorExecution
from tracely.infrastructure.net import UnsafeURL, assert_public_url
from tracely.services import alert_events
from tracely.services.alert_flow_service import STEP_TYPES, run_flow
from tracely.services.monitoring_service import MonitoringService

router = APIRouter(prefix="/api")

VALID_CONDITION_TYPES = POLLED_TYPES | EVENT_TYPES
VALID_CHANNEL_TYPES = {"slack", "webhook", "email"}
_MAX_STEPS = 20


def _step_dict(step: Any) -> dict[str, Any]:
    return {
        "id": step.id,
        "order_index": step.order_index,
        "name": step.name,
        "step_type": step.step_type,
        "config": step.config or {},
    }


def _monitor_dict(m: Monitor) -> dict[str, Any]:
    steps = sorted(m.steps or [], key=lambda s: s.order_index)
    layout = m.flow_layout if isinstance(m.flow_layout, dict) else None
    if layout is None or not layout.get("nodes"):
        # An API-created rule (or one from the assistant tool) opens on the canvas looking exactly
        # like one drawn by hand, instead of as an empty pane.
        layout = (
            linear_flow_layout(
                [(s.id, s.name, s.step_type) for s in steps],
                trigger_label=str((m.condition or {}).get("type") or "trigger"),
            )
            if steps
            else {"nodes": [], "edges": []}
        )
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "target_agent": m.target_agent,
        "condition": m.condition or {},
        "channels": m.channels or [],
        "enabled": m.enabled,
        "min_interval_seconds": m.min_interval_seconds,
        "steps": [_step_dict(s) for s in steps],
        "flow_layout": layout,
        "last_evaluated_at": m.last_evaluated_at.isoformat() if m.last_evaluated_at else None,
        "last_fired_at": m.last_fired_at.isoformat() if m.last_fired_at else None,
        "last_fired_summary": m.last_fired_summary,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _execution_dict(ex: MonitorExecution) -> dict[str, Any]:
    return {
        "id": ex.id,
        "monitor_id": ex.monitor_id,
        "trigger_type": ex.trigger_type,
        "subject_id": ex.subject_id,
        "status": ex.status,
        "started_at": ex.started_at.isoformat() if ex.started_at else None,
        "completed_at": ex.completed_at.isoformat() if ex.completed_at else None,
        "error": ex.error,
        "is_test": ex.is_test,
        "steps": ex.step_results or [],
    }


def _validate_condition(cond: dict) -> None:
    cond_type = (cond or {}).get("type")
    if cond_type not in VALID_CONDITION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"condition.type must be one of {sorted(VALID_CONDITION_TYPES)}",
        )
    if cond_type in EVENT_TYPES:
        # Event conditions carry only optional filters (`env`, `score_name`, `contains`) — no
        # filters at all is the valid, common case: "every failing gate in this workspace".
        if len(str(cond.get("contains") or "")) > 200:
            raise HTTPException(status_code=400, detail="condition.contains is too long (max 200)")
        return
    # Each polled type has a numeric threshold; `score_name` is required for score-based types.
    if cond_type in ("fail_rate_over", "score_below") and not (cond.get("score_name") or "").strip():
        raise HTTPException(
            status_code=400, detail=f"condition.score_name is required for {cond_type}"
        )
    if cond.get("threshold") is None:
        raise HTTPException(status_code=400, detail="condition.threshold is required")


def _check_url(url: str, field: str) -> None:
    """Reject an unreachable-from-here URL at save time — unless it is a template.

    A templated URL (`{{ trace.url }}`, a host from an LLM step) cannot be resolved yet, so the
    check that matters is the one the engine does on the RENDERED url immediately before the
    request (`alert_flow_service`). Both exist: this one catches the paste of a private address,
    that one catches everything.
    """
    if not url or "{{" in url:
        return
    try:
        assert_public_url(url)
    except UnsafeURL as exc:
        raise HTTPException(status_code=400, detail=f"{field}: {exc}") from None


def _validate_channels(channels: list[dict]) -> None:
    for ch in channels or []:
        ctype = (ch or {}).get("type")
        if ctype not in VALID_CHANNEL_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"channel.type must be one of {sorted(VALID_CHANNEL_TYPES)}",
            )
        if ctype == "email":
            to = (ch.get("to") or "").strip()
            # Not an RFC validator: the address is handed to Resend, which is the real judge. This
            # only catches the "pasted a Slack URL into the email field" class of mistake.
            if "@" not in to or " " in to:
                raise HTTPException(status_code=400, detail="channel.to must be an email address")
            continue
        if not (ch.get("url") or "").strip():
            raise HTTPException(status_code=400, detail="channel.url is required")
        _check_url(ch["url"], "channel.url")


def _validate_steps(steps: list[dict]) -> None:
    if len(steps) > _MAX_STEPS:
        raise HTTPException(status_code=400, detail=f"a rule may have at most {_MAX_STEPS} steps")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        stype = str((step or {}).get("step_type") or "")
        if stype not in STEP_TYPES:
            raise HTTPException(
                status_code=400, detail=f"steps[{i}].step_type must be one of {sorted(STEP_TYPES)}"
            )
        sid = str(step.get("id") or "")
        if sid and sid in seen:
            # Duplicate ids would collapse two canvas nodes into one row and silently drop a step.
            raise HTTPException(status_code=400, detail=f"steps[{i}].id is duplicated: {sid}")
        seen.add(sid)
        cfg = step.get("config") or {}
        if not isinstance(cfg, dict):
            raise HTTPException(status_code=400, detail=f"steps[{i}].config must be an object")
        if stype in ("webhook", "slack"):
            _check_url(str(cfg.get("url") or ""), f"steps[{i}].config.url")


def _validate_layout(layout: dict | None) -> None:
    err = flow_layout_error(layout)
    if err:
        raise HTTPException(status_code=400, detail=err)


class StepIn(BaseModel):
    id: str = Field(default="", max_length=64)
    order_index: int = 0
    name: str = Field(default="", max_length=120)
    step_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=400)
    target_agent: str = Field(default="", max_length=80)
    condition: dict[str, Any]
    channels: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[StepIn] = Field(default_factory=list)
    flow_layout: dict[str, Any] | None = None
    enabled: bool = True
    min_interval_seconds: int = Field(default=900, ge=0, le=86400)


class MonitorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    target_agent: str | None = Field(default=None, max_length=80)
    condition: dict[str, Any] | None = None
    channels: list[dict[str, Any]] | None = None
    steps: list[StepIn] | None = None
    flow_layout: dict[str, Any] | None = None
    enabled: bool | None = None
    min_interval_seconds: int | None = Field(default=None, ge=0, le=86400)


@router.get("/monitors")
async def list_monitors(project_id: str = Depends(get_project_id)) -> list[dict]:
    def work():
        with SyncSessionLocal() as s:
            return [_monitor_dict(m) for m in repo.monitors_list(s, project_id)]

    return await run_in_threadpool(work)


@router.post("/monitors", dependencies=[Depends(require_user)])
async def create_monitor(body: MonitorCreate, project_id: str = Depends(get_project_id)) -> dict:
    _validate_condition(body.condition)
    _validate_channels(body.channels)
    steps = [s.model_dump() for s in body.steps]
    _validate_steps(steps)
    _validate_layout(body.flow_layout)

    def work():
        with SyncSessionLocal() as s:
            m = repo.monitor_create(
                s, project_id,
                name=body.name, description=body.description, target_agent=body.target_agent,
                condition=body.condition, channels=body.channels, enabled=body.enabled,
                min_interval_seconds=body.min_interval_seconds,
            )
            if body.flow_layout is not None:
                m.flow_layout = body.flow_layout
            if steps:
                repo.monitor_steps_replace(s, m, steps)
            s.commit()
            s.refresh(m)
            return _monitor_dict(m)

    return await run_in_threadpool(work)


# ── the editor's read-only helpers (declared before /{monitor_id}) ────────────


@router.get("/monitors/step-types")
async def step_types(_: str = Depends(get_project_id)) -> list[dict]:
    """Every step type with the fields it produces — the Output column of the inspector, sourced
    from the same table the engine and the assistant use."""
    return [{"step_type": t, "outputs": declared_outputs(t)} for t in STEP_TYPES]


@router.get("/monitors/inputs/schema")
async def inputs_schema(trigger: str = "", _: str = Depends(get_project_id)) -> list[dict]:
    """The variable chips available to a rule with this trigger: `{path, type, description,
    example}`. Filtered by trigger, because a gate alert has no `trace.*` to read."""
    if not trigger:
        return [{k: v for k, v in row.items() if k != "triggers"} for row in BASE_INPUTS]
    return catalog_for_trigger(trigger)


@router.get("/monitors/inputs/sample")
async def inputs_sample(
    trigger: str,
    subject_id: str = "",
    project_id: str = Depends(get_project_id),
) -> dict:
    """The same catalog joined with REAL values from one subject, so a chip can show what it will
    actually resolve to. Empty `subject_id` = catalog only."""

    def work():
        with SyncSessionLocal() as s:
            catalog = catalog_for_trigger(trigger)
            values: dict[str, Any] = {}
            if subject_id:
                event = alert_events.event_for_subject(s, project_id, trigger, subject_id)
                if event is None:
                    raise HTTPException(status_code=404, detail="subject not found")
                from tracely.domain.alerting import build_context

                values = build_context(event)
            rows = []
            for row in catalog:
                rows.append({**row, "sample": _sample_at(values, row["path"])})
            return {"trigger": trigger, "subject_id": subject_id, "catalog": rows}

    return await run_in_threadpool(work)


def _sample_at(values: dict, path: str) -> Any:
    """The literal value a chip resolves to, truncated for preview. `None` when we have no subject
    (the editor then shows the catalog's `example` instead)."""
    cur: Any = values
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    if isinstance(cur, str):
        return cur if len(cur) <= 160 else cur[:159] + "…"
    if isinstance(cur, list):
        return f"{len(cur)} items" if cur else "[]"
    return cur


@router.get("/monitors/subjects")
async def subjects(trigger: str, project_id: str = Depends(get_project_id)) -> list[dict]:
    """Real things this rule can be tested against — recent failing turns, gate runs, clusters.
    Empty for threshold triggers: those are tested against the live window instead."""

    def work():
        with SyncSessionLocal() as s:
            return alert_events.subjects_for_trigger(s, project_id, trigger)

    return await run_in_threadpool(work)


@router.get("/monitors/{monitor_id}")
async def get_monitor(monitor_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            m = repo.monitor_get(s, project_id, monitor_id)
            return None if m is None else _monitor_dict(m)

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    return res


@router.patch("/monitors/{monitor_id}", dependencies=[Depends(require_user)])
async def update_monitor(
    monitor_id: str,
    body: MonitorUpdate,
    project_id: str = Depends(get_project_id),
) -> dict:
    patch = body.model_dump(exclude_unset=True)
    if "condition" in patch:
        _validate_condition(patch["condition"])
    if "channels" in patch:
        _validate_channels(patch["channels"])
    steps = patch.pop("steps", None)
    if steps is not None:
        _validate_steps(steps)
    if "flow_layout" in patch:
        _validate_layout(patch["flow_layout"])

    def work():
        with SyncSessionLocal() as s:
            m = repo.monitor_update(s, project_id, monitor_id, patch)
            if m is None:
                return None
            if steps is not None:
                # Steps are replaced wholesale when present — the canvas is the source of truth
                # while you edit, and a partial merge is how the two drift.
                repo.monitor_steps_replace(s, m, steps)
                s.commit()
                s.refresh(m)
            return _monitor_dict(m)

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="monitor not found")
    return res


@router.delete("/monitors/{monitor_id}", dependencies=[Depends(require_user)])
async def delete_monitor(monitor_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            return repo.monitor_delete(s, project_id, monitor_id)

    ok = await run_in_threadpool(work)
    if not ok:
        raise HTTPException(status_code=404, detail="monitor not found")
    return {"deleted": monitor_id}


@router.get("/monitors/{monitor_id}/executions")
async def monitor_executions(
    monitor_id: str, limit: int = 20, project_id: str = Depends(get_project_id)
) -> list[dict]:
    """This rule's runs, newest first, each with its per-step audit trail."""

    def work():
        with SyncSessionLocal() as s:
            rows = repo.monitor_executions(s, project_id, monitor_id, min(max(limit, 1), 100))
            return [_execution_dict(e) for e in rows]

    return await run_in_threadpool(work)


@router.post("/monitors/{monitor_id}/test", dependencies=[Depends(require_user)])
async def test_monitor(
    monitor_id: str,
    body: dict = Body(default={}),
    project_id: str = Depends(get_project_id),
) -> dict:
    """Run this rule NOW and return the execution, per step, with what each field rendered to.

    Real side effects, on purpose — it is the same code path the real alert uses, and "does this
    actually reach me?" is the question worth answering before arming it. Three shapes:

    - a flow + a chosen `subject_id` → run the steps against that real trace / gate / cluster
    - a flow + no subject → run against the newest subject of the rule's trigger
    - no flow (channel-only rule) → the legacy path: evaluate the window, or send a sample alert
    """
    subject_id = str((body or {}).get("subject_id") or "")

    def work():
        with SyncSessionLocal() as s:
            m = repo.monitor_get(s, project_id, monitor_id)
            if m is None:
                return {"error": "not_found"}
            if not m.steps:
                return {"error": "no_flow"}
            trigger = str((m.condition or {}).get("type") or "")
            event: dict[str, Any] | None = None
            if is_event_condition(m.condition or {}):
                sid = subject_id
                if not sid:
                    picks = alert_events.subjects_for_trigger(s, project_id, trigger, limit=1)
                    if not picks:
                        return {"error": "no_subject"}
                    sid = str(picks[0]["id"])
                event = alert_events.event_for_subject(s, project_id, trigger, sid)
                if event is None:
                    return {"error": "subject_not_found"}
            else:
                # A threshold rule has no subject: evaluate the live window, then run the flow with
                # whatever the metric currently reads — including when it is quiet, so the user can
                # see the message either way.
                from tracely.domain.monitoring.conditions import Verdict

                event = alert_events.metric_event(
                    m, Verdict(False, m.last_fired_summary or "test run", None, 0)
                )
            from datetime import datetime, timezone

            from tracely.services.monitoring_service import _alert_group

            now = datetime.now(timezone.utc)
            ex = run_flow(s, m, {**event, "alert": _alert_group(m, event, now, s)}, is_test=True)
            return {"execution": _execution_dict(ex), "ok": ex.status == "completed"}

    res = await run_in_threadpool(work)
    if res.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="monitor not found")
    if res.get("error") == "subject_not_found":
        raise HTTPException(status_code=404, detail="that subject no longer exists")
    if res.get("error") == "no_subject":
        raise HTTPException(
            status_code=400,
            detail="nothing to test against yet — this workspace has no matching trace, gate run or cluster",
        )
    if res.get("error") == "no_flow":
        # Channel-only rule: the pre-flow behaviour, still the right answer for those rows.
        legacy = await MonitoringService().evaluate_one(project_id, monitor_id)
        if legacy is None:
            raise HTTPException(status_code=404, detail="monitor not found")
        return legacy
    return res
