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
        # Source-failure evidence (the source trace fails the contract) — NOT a verified fix.
        "fail_to_pass_validated": c.fail_to_pass_validated,
        "version": c.version or 1,
        # Candidate-verified evidence: a specific candidate passed, at this case version.
        "verified_candidate_trace_id": c.verified_candidate_trace_id or "",
        "verified_case_version": c.verified_case_version,
        "verified_at": c.verified_at.isoformat() if c.verified_at else None,
        "verified_by": c.verified_by or "",
        # The durable artifact (W3): empty key = not yet snapshotted (legacy) → backfill/recapture.
        "artifact_key": c.artifact_s3_key or "",
        "artifact_digest": c.artifact_digest or "",
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
            d = _case_dict(c, repo.case_replays(s, case_id), a.slug if a else None)
            lg = repo.case_last_gate(s, case_id)
            d["last_gate"] = (
                {
                    "id": lg[1].id, "status": lg[1].status, "verdict": lg[0].verdict,
                    "run_id": lg[1].run_id or "", "created_at": lg[1].created_at.isoformat() if lg[1].created_at else None,
                }
                if lg
                else None
            )
            return d

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


@router.patch("/cases/{case_id}/expectations", dependencies=[Depends(require_user)])
async def update_expectations(
    case_id: str, project_id: str = Depends(get_project_id), body: dict = Body(default={})
) -> dict:
    """Edit the case's expected behaviour (whitelisted assertion keys). Bumps the version and
    re-snapshots the artifact; re-validates source failure when the source still exists."""

    def work():
        with SyncSessionLocal() as s:
            try:
                c = RegressionService(s).update_expectations(project_id, case_id, body or {})
            except NotFound as e:
                return ("404", str(e))
            except ValueError as e:
                return ("400", str(e))
            return ("ok", _case_dict(c))

    status, payload = await run_in_threadpool(work)
    if status != "ok":
        raise HTTPException(status_code=int(status), detail=payload)
    return payload


@router.get("/cases/{case_id}/candidates")
async def case_candidates(case_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """Recent runs with exactly this case's input — the compatible candidates to verify."""

    def work():
        with SyncSessionLocal() as s:
            try:
                return RegressionService(s).candidates(project_id, case_id)
            except NotFound:
                return None

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="case not found")
    return {"items": res}


@router.get("/cases/{case_id}/compare")
async def case_compare(
    case_id: str, candidate: str, project_id: str = Depends(get_project_id)
) -> dict:
    """Original vs candidate: aligned relevant steps, first divergence, structural verdict."""

    def work():
        with SyncSessionLocal() as s:
            try:
                return RegressionService(s).compare(project_id, case_id, candidate)
            except NotFound as e:
                return ("err", str(e))

    res = await run_in_threadpool(work)
    if isinstance(res, tuple):
        raise HTTPException(status_code=404, detail=res[1])
    return res


@router.post("/cases/backfill-artifacts", dependencies=[Depends(require_user)])
async def backfill_artifacts(project_id: str = Depends(get_project_id)) -> dict:
    """Snapshot every case in this workspace that has no durable artifact yet, from its source
    trace while that still exists. Cases whose source already expired come back `unrecoverable`
    with the reason — recapture those from a fresh trace of the same input."""

    def work():
        with SyncSessionLocal() as s:
            return RegressionService(s).backfill_artifacts(project_id)

    return await run_in_threadpool(work)


@router.post("/cases/{case_id}/recapture")
async def recapture(
    case_id: str, project_id: str = Depends(get_project_id), body: dict = Body(default={})
) -> dict:
    """Re-record a case from a fresh trace with the same input (digest must match); bumps the
    case version. The step after a fix: a fixed run that ADDS a call the old recording lacks
    cannot be replayed from that recording — re-recording from the fixed run gives the suite a
    truthful one. Additive like promote/replay, so an ingest key (CI) may do it."""
    trace_id = str(body.get("trace_id") or "")
    if not trace_id:
        raise HTTPException(status_code=400, detail="trace_id is required")

    def work():
        with SyncSessionLocal() as s:
            try:
                art = RegressionService(s).recapture(project_id, case_id, trace_id)
            except NotFound as e:
                return ("404", str(e))
            except ValueError as e:
                return ("409", str(e))
            return ("ok", {"case_id": case_id, "case_version": art.case_version, "artifact_digest": art.digest()})

    status, payload = await run_in_threadpool(work)
    if status != "ok":
        raise HTTPException(status_code=int(status), detail=payload)
    return payload


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
