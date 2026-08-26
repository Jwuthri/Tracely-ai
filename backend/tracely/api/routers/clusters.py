"""Failure cluster endpoints: list, detail, rebuild, promote, unpromote, ignore.

Slim wrapper: HTTP shaping only. Business logic lives in:
- `domain/evaluation/evaluator_suggestion.py` (suggested evaluator generation)
- `domain/failure/histogram.py` (occurrence bucketing)
- `infrastructure/db/repositories.py` (Postgres queries)
- `infrastructure/clickhouse/trace_reader.py:TraceReader.member_meta` (CH read)
- `services/regression_service.py:RegressionService.promote_trace` (promote action)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id, require_user
from tracely.config import settings
from tracely.domain.evaluation.evaluator_suggestion import suggest_evaluator
from tracely.domain.failure.histogram import histogram
from tracely.infrastructure.clickhouse.trace_reader import TraceReader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Agent, FailureCluster
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.embeddings import embeddings_enabled
from tracely.infrastructure.llm.provider import llm_enabled
from tracely.infrastructure.text import message_text
from tracely.services.regression_service import NotFound, RegressionService
from tracely.workers.tasks import rebuild_clusters_task

router = APIRouter(prefix="/api")


def _cluster_dict(
    cl: FailureCluster, agent_slug: str | None = None, members: list | None = None
) -> dict[str, Any]:
    d = {
        "id": cl.id,
        "agent_id": cl.agent_id,
        "agent": agent_slug,
        "label": cl.label,
        "taxonomy": cl.taxonomy,
        "description": cl.description,
        "proposed_fix": cl.proposed_fix,
        "severity": cl.severity,
        "method": cl.method,
        "count": cl.count,
        "status": cl.status,
        "candidate_case_id": cl.candidate_case_id,
        "signature": cl.signature,
        "first_seen_at": cl.first_seen_at.isoformat() if cl.first_seen_at else None,
        "last_seen_at": cl.last_seen_at.isoformat() if cl.last_seen_at else None,
    }
    if members is not None:
        d["members"] = members
    return d


@router.post("/clusters/rebuild")
async def rebuild(project_id: str = Depends(get_project_id)) -> dict:
    # Embeddings and the analysis agents both go through OpenRouter (OPENAI_API_KEY is only a
    # fallback for embeddings on older deployments).
    with provider.use_project_key(project_id):
        ready = embeddings_enabled() and llm_enabled()
    if not ready:
        raise HTTPException(
            status_code=400,
            detail="Failure clustering needs this workspace's OpenRouter key (embeddings + agent "
            "analysis). Add one in Settings -> OpenRouter key.",
        )
    rebuild_clusters_task.delay(project_id)
    return {"status": "started"}


@router.get("/clusters")
async def list_clusters(
    project_id: str = Depends(get_project_id),
    min_size: int = Query(default=None, ge=2, description="hide clusters with fewer members"),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """One page of failure clusters (biggest first) plus the `total` matching `min_size`.

    `open` is counted project-wide too, not derived from the page — the header badge would
    otherwise read "3 open" when it means "3 open on page 1 of 9"."""
    floor = settings.cluster_min_size if min_size is None else min_size

    def work():
        with SyncSessionLocal() as s:
            return {
                "items": [
                    _cluster_dict(cl, slug)
                    for cl, slug in repo.clusters_list_with_agent(
                        s, project_id, floor, limit=limit, offset=offset
                    )
                ],
                "total": repo.clusters_count(s, project_id, floor),
                "open": repo.clusters_count(s, project_id, floor, status="OPEN"),
            }

    return await run_in_threadpool(work)


@router.get("/clusters/{cluster_id}")
async def get_cluster(
    cluster_id: str, project_id: str = Depends(get_project_id), members: int = 25
) -> dict:
    """One cluster, with the first `members` linked traces.

    The member list is capped: a cluster that fired thousands of times used to send every one of
    them — each with its full input text — to the browser. The histogram still reads EVERY
    member's timestamp (a separate, light query), so the "seen over time" shape stays truthful
    rather than becoming "the most recent page".
    """
    cap = max(1, min(members, 200))

    def work():
        reader = TraceReader()
        with SyncSessionLocal() as s:
            cl = repo.cluster_get(s, project_id, cluster_id)
            if not cl:
                return None
            all_ids = [m.trace_id for m in repo.cluster_members(s, cl.id)]
            page = repo.cluster_members(s, cl.id, limit=cap)
            meta = reader.member_meta(project_id, [m.trace_id for m in page])
            # WHY each member failed. `summary` is only written when the cluster went through LLM
            # analysis, and a crash trace carries no input at all — so with neither of these the
            # member list is 25 opaque trace ids, which is no help in deciding what to do.
            fails = {
                tid: [sc for sc in scores if sc.get("verdict") == "FAIL"]
                for tid, scores in reader.scores_by_trace(
                    project_id, [m.trace_id for m in page]
                ).items()
            }
            # Drop members whose trace no longer exists in events (wiped, or aged out by
            # ClickHouse TTL retention) so the detail shows real linked traces.
            shown = [
                {
                    "trace_id": m.trace_id,
                    "is_medoid": m.is_medoid,
                    "summary": m.summary,
                    "input": message_text(meta[m.trace_id].get("input", "")),
                    "latency_ms": meta[m.trace_id].get("latency_ms", 0.0),
                    "ts": meta[m.trace_id].get("ts"),
                    "error": meta[m.trace_id].get("error", ""),
                    "failed": [
                        {"name": sc["name"], "reason": sc.get("comment", "")}
                        for sc in fails.get(m.trace_id, [])
                    ],
                }
                for m in page
                if m.trace_id in meta
            ]
            agent = s.get(Agent, cl.agent_id)
            d = _cluster_dict(cl, agent.slug if agent else None, shown)
            # How many the cluster really has, so the UI can say "25 of 312" instead of implying
            # the page is the whole thing.
            d["member_total"] = len(all_ids)
            d["histogram"] = histogram(reader.member_timestamps(project_id, all_ids))
            d["suggested_evaluator"] = suggest_evaluator(cl.label, cl.taxonomy)
            return d

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    return res


@router.post("/clusters/{cluster_id}/promote")
async def promote_cluster(cluster_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            cl = repo.cluster_get(s, project_id, cluster_id)
            if not cl:
                return ("err", "cluster not found")
            members = repo.cluster_members(s, cl.id)  # medoid first, then by added_at
            if not members:
                return ("err", "cluster has no members")
            # Promote the first member whose trace is STILL in ClickHouse. The medoid is the
            # oldest member, so it's the first to be deleted or aged out by the 90-day TTL —
            # keying only on it made promote 404 on clusters that had perfectly good newer
            # traces. `promote_trace` raises before it writes anything, so retrying is safe.
            svc = RegressionService(s)
            case = None
            for m in members:
                try:
                    case = svc.promote_trace(project_id, m.trace_id, title=cl.label)
                    break
                except NotFound:
                    continue
            if case is None:
                return (
                    "err",
                    f"none of this cluster's {len(members)} traces are still stored — they were "
                    "deleted or aged out by the events TTL, so there is nothing to record",
                )
            cl.status = "PROMOTED"
            cl.candidate_case_id = case.id
            s.commit()
            return ("ok", {"case_id": case.id, "cluster_status": cl.status})

    status, payload = await run_in_threadpool(work)
    if status == "err":
        raise HTTPException(status_code=404, detail=payload)
    return payload


@router.post("/clusters/{cluster_id}/unpromote", dependencies=[Depends(require_user)])
async def unpromote_cluster(cluster_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """Undo a promote: delete the case it created (replay history included) and reopen the
    cluster. `require_user` because it destroys what someone built — the same bar as deleting
    the case directly."""

    def work():
        with SyncSessionLocal() as s:
            cl = repo.cluster_get(s, project_id, cluster_id)
            if not cl:
                return None
            if cl.candidate_case_id:
                # no-op if the case was already deleted from the cases page
                repo.case_delete(s, project_id, cl.candidate_case_id)
            cl.status = "OPEN"
            cl.candidate_case_id = None
            s.commit()
            return {"status": cl.status}

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    return res


class DeleteClustersBody(BaseModel):
    cluster_ids: list[str]


@router.delete("/clusters", dependencies=[Depends(require_user)])
async def delete_clusters(
    body: DeleteClustersBody, project_id: str = Depends(get_project_id)
) -> dict:
    """Delete clusters (multi-select in the UI). Clusters are derived from the traces, so a later
    Analyze re-forms any issue whose failing traces still exist — this prunes noise and orphans."""
    ids = [c for c in dict.fromkeys(body.cluster_ids) if c]
    if not ids:
        raise HTTPException(status_code=400, detail="no clusters given")

    def work():
        with SyncSessionLocal() as s:
            return repo.clusters_delete(s, project_id, ids)

    return {"deleted": await run_in_threadpool(work)}


@router.post("/clusters/{cluster_id}/ignore")
async def ignore_cluster(cluster_id: str, project_id: str = Depends(get_project_id)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            cl = repo.cluster_get(s, project_id, cluster_id)
            if not cl:
                return None
            cl.status = "IGNORED"
            s.commit()
            return {"status": cl.status}

    res = await run_in_threadpool(work)
    if res is None:
        raise HTTPException(status_code=404, detail="cluster not found")
    return res
