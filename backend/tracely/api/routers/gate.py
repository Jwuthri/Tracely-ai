"""CI/CD gate endpoints: run a gate, list gates, gate detail, fetch the replay suite.

Pure HTTP shaping — Postgres queries live in `infrastructure.db.repositories`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Agent, GateRun
from tracely.services.gate_service import GateService
from tracely.workers.tasks import run_scenario_gate_task

router = APIRouter(prefix="/api")


def _gate_dict(
    g: GateRun, agent_slug: str | None = None, cases: list | None = None
) -> dict[str, Any]:
    d = {
        "id": g.id,
        "agent_id": g.agent_id,
        "agent": agent_slug,
        "env": g.env,
        "git_ref": g.git_ref,
        "pr_number": g.pr_number,
        "status": g.status,
        # The execution manifest: which CI run produced the candidates, in which mode. Empty
        # run_id = paired by input digest, not verified against a specific execution.
        "run_id": g.run_id or "",
        "execution_mode": g.execution_mode or "",
        "total": g.total,
        "passed": g.passed,
        "failed": g.failed,
        "skipped": g.skipped,
        "incomplete": g.incomplete or 0,
        "latency_ms": g.latency_ms,
        "total_tokens": g.total_tokens,
        "warnings": g.warnings or [],
        "created_at": g.created_at.isoformat() if g.created_at else None,
        # CI polls a simulated gate until this is set — RUNNING with no finish means keep waiting.
        "finished_at": g.finished_at.isoformat() if g.finished_at else None,
    }
    if cases is not None:
        d["cases"] = cases
    return d


def _cases(session, gate_id: str) -> list[dict]:
    return [
        {
            "title": title,
            "verdict": gc.verdict,
            "candidate_trace_id": gc.candidate_trace_id,
            "detail": gc.detail,
            "evaluation_case_id": gc.evaluation_case_id,
            "scenario_id": gc.scenario_id,
        }
        for gc, title in repo.gate_cases_with_titles(session, gate_id)
    ]


def _ids(body: dict, key: str) -> list[str] | None:
    """`None` (key absent) = run the whole half; a list = run exactly these, `[]` included."""
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, list):
        raise HTTPException(status_code=400, detail=f"{key} must be a list of ids")
    return [str(x) for x in v]


@router.post("/gate")
async def run_gate(
    project_id: str = Depends(get_project_id), body: dict = Body(default={})
) -> dict:
    agent_ref = body.get("agent")
    env = body.get("env") or "ci"
    git_ref = body.get("git_ref") or ""
    pr_number = body.get("pr_number")
    candidates = body.get("candidates") or None  # {case_id: trace_id} from `tracely replay`
    case_ids = _ids(body, "case_ids")
    run_id = str(body.get("run_id") or "")[:64]
    failed = body.get("failed") or None  # {case_id: reason} — commands that did not complete
    if failed is not None and not isinstance(failed, dict):
        raise HTTPException(status_code=400, detail="failed must be a {case_id: reason} object")
    execution_mode = str(body.get("execution_mode") or "")[:16]
    if not agent_ref:
        raise HTTPException(status_code=400, detail="agent required")

    def work():
        with SyncSessionLocal() as s:
            gate_svc = GateService(s)
            aid = gate_svc.resolve_agent_id(project_id, agent_ref)
            if not aid:
                return ("err", f"agent '{agent_ref}' not found")
            g = gate_svc.run_gate(
                project_id, aid, env=env, git_ref=git_ref, pr_number=pr_number,
                candidates=candidates, case_ids=case_ids, run_id=run_id,
                failed={str(k): str(v) for k, v in (failed or {}).items()} or None,
                execution_mode=execution_mode,
            )
            agent = s.get(Agent, aid)
            return ("ok", _gate_dict(g, agent.slug if agent else None, _cases(s, g.id)))

    status, payload = await run_in_threadpool(work)
    if status == "err":
        raise HTTPException(status_code=404, detail=payload)
    return payload


@router.post("/gate/simulate")
async def run_simulated_gate(
    project_id: str = Depends(get_project_id), body: dict = Body(default={})
) -> dict:
    """Start a gate that drives the agent's enabled scenarios against its registered endpoint.

    Returns immediately with a RUNNING gate id: emulated conversations make real HTTP calls and
    then wait on the eval pipeline, so this is minutes of work. CI polls `GET /api/gates/{id}`
    until `finished_at` is set.
    """
    agent_ref = body.get("agent")
    if not agent_ref:
        raise HTTPException(status_code=400, detail="agent required")
    case_ids = _ids(body, "case_ids")
    scenario_ids = _ids(body, "scenario_ids")
    min_pass_rate = body.get("min_pass_rate")
    if min_pass_rate is not None:
        try:
            min_pass_rate = float(min_pass_rate)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="min_pass_rate must be a number") from None
        if not 0.0 <= min_pass_rate <= 1.0:
            raise HTTPException(status_code=400, detail="min_pass_rate must be between 0 and 1")

    def work():
        with SyncSessionLocal() as s:
            aid = GateService(s).resolve_agent_id(project_id, agent_ref)
            if not aid:
                return ("err", f"agent '{agent_ref}' not found")
            gate = GateRun(
                id=str(uuid.uuid4()), project_id=project_id, agent_id=aid,
                env=body.get("env") or "ci", git_ref=body.get("git_ref") or "",
                pr_number=body.get("pr_number"), status="RUNNING",
            )
            s.add(gate)
            s.commit()
            return ("ok", (gate.id, aid, gate.env, gate.git_ref, gate.pr_number))

    status, payload = await run_in_threadpool(work)
    if status == "err":
        raise HTTPException(status_code=404, detail=payload)

    gate_id, aid, env, git_ref, pr_number = payload
    run_scenario_gate_task.delay(
        project_id, aid, gate_id, env, git_ref, pr_number, min_pass_rate,
        case_ids, scenario_ids,
    )
    return {"id": gate_id, "status": "RUNNING", "agent": agent_ref, "env": env}


@router.get("/gate/run-traces")
async def gate_run_traces(
    agent: str, run_id: str, project_id: str = Depends(get_project_id)
) -> dict:
    """The traces a CI execution has produced so far (stamped `tracely.replay.run_id`) — what
    `tracely replay --cmd` polls instead of sleeping a fixed interval."""

    def work():
        with SyncSessionLocal() as s:
            gate_svc = GateService(s)
            aid = gate_svc.resolve_agent_id(project_id, agent)
            if not aid:
                return None
            return {"run_id": run_id, "trace_ids": gate_svc.trace_reader.traces_for_run(project_id, aid, run_id)}

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent}' not found")
    return res


@router.get("/gate/suite")
async def gate_suite(agent: str, project_id: str = Depends(get_project_id)) -> dict:
    """The promoted regression suite (+ recorded inputs) for an agent — what `tracely replay`
    runs."""
    def work():
        with SyncSessionLocal() as s:
            gate_svc = GateService(s)
            aid = gate_svc.resolve_agent_id(project_id, agent)
            if not aid:
                return None
            return {
                "agent": agent,
                "agent_id": aid,
                "cases": gate_svc.replay_suite(project_id, aid),
            }

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent}' not found")
    return res


@router.get("/gates")
async def list_gates(
    limit: int = 50, offset: int = 0, project_id: str = Depends(get_project_id)
) -> dict:
    """One page of gate runs, newest first, plus the project-wide `total`.

    The fastest-growing table in the product — one row per agent per PR run — so this list is
    paginated rather than returned whole."""
    def work():
        with SyncSessionLocal() as s:
            return {
                "items": [
                    _gate_dict(g, slug)
                    for g, slug in repo.gates_list_with_agent(
                        s, project_id, limit=limit, offset=offset
                    )
                ],
                "total": repo.gates_count(s, project_id),
            }

    return await run_in_threadpool(work)


@router.get("/gates/{gate_id}")
async def get_gate(gate_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            g = s.get(GateRun, gate_id)
            if not g or g.project_id != project_id:
                return None
            agent = s.get(Agent, g.agent_id)
            return _gate_dict(g, agent.slug if agent else None, _cases(s, g.id))

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="gate not found")
    return res
