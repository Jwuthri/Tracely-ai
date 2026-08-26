"""Regression: promote a trace, list/get cases, replay a case + the dashboard stats.

Pure HTTP shaping — ClickHouse counters live in `infrastructure.clickhouse.async_reader`,
Postgres queries in `infrastructure.db.repositories`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from tracely.api.advisory import advisory_score_names
from tracely.api.auth import get_project_id, require_user
from tracely.infrastructure.clickhouse import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import EvaluationCase
from tracely.services.regression_service import NotFound, RegressionService

router = APIRouter(prefix="/api")


@router.get("/stats")
async def stats(project_id: str = Depends(get_project_id)) -> dict:
    counters = await async_reader.stats_counts(project_id, await advisory_score_names(project_id))

    def registry():
        with SyncSessionLocal() as s:
            return repo.registry_counts(s, project_id)

    return {**counters, **await run_in_threadpool(registry)}


def _case_dict(
    c: EvaluationCase, replays: list | None = None, agent_slug: str | None = None
) -> dict[str, Any]:
    d = {
        "id": c.id,
        "agent_id": c.agent_id,
        # The case's only real binding: the gate replays a suite *per agent*, so the UI has to
        # say which one, or "why didn't my case run?" has no visible answer.
        "agent": agent_slug,
        "level": c.level,
        "title": c.title,
        "status": c.status,
        "origin": c.origin,
        "source_trace_id": c.source_trace_id,
        "input_digest": c.input_digest,
        "match_mode": c.match_mode,
        "fail_to_pass_validated": c.fail_to_pass_validated,
        "assertions": c.assertions,
        "reference_trajectory": c.reference_trajectory,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if replays is not None:
        d["replays"] = [
            {
                "verdict": r.verdict,
                "candidate_trace_id": r.candidate_trace_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in replays
        ]
    return d


@router.post("/traces/{trace_id}/promote")
async def promote(trace_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            try:
                case = RegressionService(s).promote_trace(project_id, trace_id)
            except NotFound as e:
                return ("err", str(e))
            return ("ok", _case_dict(case))

    status, payload = await run_in_threadpool(work)
    if status == "err":
        raise HTTPException(status_code=404, detail=payload)
    return payload


@router.get("/traces/{trace_id}/case")
async def case_for_trace(trace_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """The regression case this trace was promoted into, or 404 if it never was."""

    def work():
        with SyncSessionLocal() as s:
            c = repo.case_for_trace(s, project_id, trace_id)
            return _case_dict(c) if c else None

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="no case for this trace")
    return res


@router.get("/cases")
async def list_cases(
    limit: int = 50, offset: int = 0, project_id: str = Depends(get_project_id)
) -> dict:
    """One page of regression cases, newest first, plus the project-wide `total`.

    Paginated because this list only grows: every promoted failure is a case that lives for ever,
    and the page used to render all of them. `total` is a COUNT so the header can still say how
    many exist without shipping them all."""
    def work():
        with SyncSessionLocal() as s:
            slugs = {a.id: a.slug for a in repo.agents_list(s, project_id)}
            items = []
            for c in repo.cases_list(s, project_id, limit=limit, offset=offset):
                last = repo.case_last_replay(s, c.id)
                d = _case_dict(c, agent_slug=slugs.get(c.agent_id))
                d["last_verdict"] = last.verdict if last else None
                items.append(d)
            return {
                "items": items,
                "total": repo.cases_count(s, project_id),
                # Per-agent totals for the gate launcher's ranking — computed here so it never
                # has to load every case just to count them.
                "by_agent": repo.cases_count_by_agent(s, project_id),
            }

    return await run_in_threadpool(work)


@router.get("/cases/{case_id}")
async def get_case(case_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            c = repo.case_get(s, project_id, case_id)
            if not c:
                return None
            a = repo.agent_in_project(s, project_id, c.agent_id)
            return _case_dict(c, repo.case_replays(s, case_id), a.slug if a else None)

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="case not found")
    return res


@router.delete("/cases/{case_id}", dependencies=[Depends(require_user)])
async def delete_case(case_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """Delete a regression case and its replay history. The source trace stays — promote it again
    to recreate the case."""

    def work():
        with SyncSessionLocal() as s:
            return repo.case_delete(s, project_id, case_id)

    if not await run_in_threadpool(work):
        raise HTTPException(status_code=404, detail="case not found")
    return {"deleted": case_id}


@router.post("/cases/{case_id}/replay")
async def replay(
    case_id: str,
    project_id: str = Depends(get_project_id),
    body: dict = Body(default={}),
) -> dict:
    candidate = body.get("candidate_trace_id")

    def work():
        with SyncSessionLocal() as s:
            c = repo.case_get(s, project_id, case_id)
            if not c:
                return ("err", "case not found")
            tid = candidate or c.source_trace_id
            try:
                r = RegressionService(s).replay_case(project_id, case_id, tid)
            except NotFound as e:
                return ("err", str(e))
            return (
                "ok",
                {
                    "verdict": r.verdict,
                    "candidate_trace_id": r.candidate_trace_id,
                    "detail": r.detail,
                },
            )

    status, payload = await run_in_threadpool(work)
    if status == "err":
        raise HTTPException(status_code=404, detail=payload)
    return payload
