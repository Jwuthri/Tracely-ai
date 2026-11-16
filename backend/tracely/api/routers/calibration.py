"""Judge-vs-human calibration: human labels on evaluator verdicts + per-evaluator agreement.

A reviewer agrees/disagrees with a judge's verdict on a target (trace / span / thread). Labels live
in Postgres (`score_annotations`, keyed by the score's natural identity + labeler); the evaluator
catalog and the labeling queue come from ClickHouse `scores`; the agreement math is pure
(`domain.evaluation.calibration`). The router shapes HTTP only — sync Postgres work runs in a
threadpool, ClickHouse reads are async, and no SQL is built here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_principal
from tracely.auth import Principal
from tracely.domain.evaluation.calibration import merge_catalog_with_agreement
from tracely.infrastructure.spans import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal

router = APIRouter(prefix="/api")


def _labeler(p: Principal) -> str:
    """Stable per-reviewer key for the unique label constraint (single-user dev → "default")."""
    return p.user_id or "default"


class AnnotationBody(BaseModel):
    score_name: str
    human_verdict: str  # the reviewer's verdict (PASS | FAIL | …) — agree = same as judge_verdict
    evaluation_level: str = ""
    trace_id: str = ""
    session_id: str = ""
    observation_id: str = ""
    judge_verdict: str = ""  # snapshot of the judge's verdict at label time
    note: str | None = None


@router.get("/calibration")
async def calibration_summary(principal: Principal = Depends(get_principal)) -> list[dict]:
    """Every evaluator that has produced verdicts, joined with its human-agreement stats (labeled,
    agreement %, false_pass, false_fail). Unlabeled evaluators show labeled=0."""
    pid = principal.project_id
    catalog = await async_reader.evaluator_catalog(pid)

    def fetch() -> tuple[list[dict], dict[str, str]]:
        with SyncSessionLocal() as s:
            annotations = [
                {"score_name": a.score_name, "judge_verdict": a.judge_verdict,
                 "human_verdict": a.human_verdict}
                for a in repo.score_annotations_for_project(s, pid)
            ]
            kinds = {e.score_name: e.kind for e in repo.evaluators_list(s, pid)}
        return annotations, kinds

    annotations, kinds = await run_in_threadpool(fetch)
    rows = merge_catalog_with_agreement(catalog, annotations)
    # `kind` sorts the UI: a structural evaluator is deterministic code, so labeling its verdicts
    # measures nothing — only llm_judge rows are worth a reviewer's attention.
    for r in rows:
        r["kind"] = kinds.get(r["name"], "")
    return rows


@router.get("/calibration/{score_name}/queue")
async def calibration_queue(
    score_name: str,
    limit: int = 25,
    offset: int = 0,
    verdict: str = "",
    principal: Principal = Depends(get_principal),
) -> list[dict]:
    """One page of the evaluator's recent judge decisions, each annotated with THIS reviewer's
    label (if any) — the labeling queue.

    Paged rather than dumped: an evaluator that has graded 992 runs was sending every verdict (with
    its comment) to the browser, and labeling is a one-at-a-time activity — nobody reads past the
    first screen. Callers derive "has more" from `len(rows) == limit`, as `/api/sessions` does."""
    pid, me = principal.project_id, _labeler(principal)
    queue = await async_reader.evaluator_score_queue(pid, score_name, limit, offset, verdict)

    def fetch() -> dict[tuple, object]:
        with SyncSessionLocal() as s:
            rows = repo.score_annotations_for_project(s, pid, score_name)
        return {
            (r.evaluation_level, r.trace_id, r.session_id, r.observation_id): r
            for r in rows
            if r.labeled_by == me
        }

    labels = await run_in_threadpool(fetch)
    out = []
    for row in queue:
        lab = labels.get(
            (row["evaluation_level"], row["trace_id"], row["session_id"], row["observation_id"])
        )
        out.append({**row, "human_verdict": lab.human_verdict if lab else None,
                    "note": lab.note if lab else None})
    return out


@router.post("/annotations")
async def upsert_annotation(
    body: AnnotationBody, principal: Principal = Depends(get_principal)
) -> dict:
    """Record (or update) this reviewer's verdict on a judge score."""
    pid, me = principal.project_id, _labeler(principal)

    def work() -> dict:
        with SyncSessionLocal() as s:
            row = repo.score_annotation_upsert(
                s, pid, score_name=body.score_name, human_verdict=body.human_verdict,
                evaluation_level=body.evaluation_level, trace_id=body.trace_id,
                session_id=body.session_id, observation_id=body.observation_id,
                judge_verdict=body.judge_verdict, note=body.note, labeled_by=me,
            )
            return {"id": row.id, "human_verdict": row.human_verdict}

    return await run_in_threadpool(work)


@router.delete("/annotations")
async def delete_annotation(
    body: AnnotationBody, principal: Principal = Depends(get_principal)
) -> dict:
    """Clear this reviewer's label on a judge score."""
    pid, me = principal.project_id, _labeler(principal)

    def work() -> dict:
        with SyncSessionLocal() as s:
            ok = repo.score_annotation_delete(
                s, pid, score_name=body.score_name, evaluation_level=body.evaluation_level,
                trace_id=body.trace_id, session_id=body.session_id,
                observation_id=body.observation_id, labeled_by=me,
            )
            return {"deleted": ok}

    return await run_in_threadpool(work)
