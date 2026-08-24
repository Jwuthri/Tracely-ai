"""Public share links.

A share token is a signed, read-only capability for exactly ONE object — today a conversation or a
CI gate run. It is verified HERE, by `verify_share`, and never passed to `resolve_principal`:
routing it through the normal auth path would turn a share link into a full project read key. That
is the whole security argument for this module — the anonymous endpoint touches no auth dependency,
and the token's own claims supply the `project_id` scope that every reader call still requires.

Two rules the readers below must keep:

* **Every failure is a 404.** Invalid, expired, revoked, foreign project, wrong kind, deleted
  subject — one indistinguishable answer, so a stranger can never learn that an id exists.
* **The payload is an allowlist.** A gate link lands in a public pull request, so the response
  carries the verdict and nothing that would widen it: no trace ids, no sibling runs, no keys.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tracely.api.advisory import advisory_score_names
from tracely.api.auth import get_project_id
from tracely.auth.tokens import (
    SHARE_KINDS,
    SHARE_TTL_SECONDS,
    TokenError,
    issue_share,
    verify_share,
)
from tracely.domain.evaluation.verdict import rollup_verdict
from tracely.infrastructure.clickhouse import async_reader
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.db.models import Agent, GateRun

router = APIRouter(prefix="/api")

_NOT_FOUND = HTTPException(status_code=404, detail="link expired or invalid")


class ShareBody(BaseModel):
    """`{kind, id}`; `{thread_id}` is the pre-kinds spelling and still works."""

    kind: str | None = None
    id: str | None = None
    thread_id: str | None = None


def _subject(body: ShareBody) -> tuple[str, str]:
    kind = (body.kind or "conversation").strip()
    subject_id = (body.id or body.thread_id or "").strip()
    if kind not in SHARE_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {', '.join(SHARE_KINDS)}")
    if not subject_id:
        raise HTTPException(status_code=400, detail="id required")
    return kind, subject_id


@router.post("/share")
async def create_share(body: ShareBody, project_id: str = Depends(get_project_id)) -> dict:
    """Mint a public link. Minting again returns a fresh token with a new expiry; earlier tokens
    keep working until theirs runs out or the subject is revoked."""
    kind, subject_id = _subject(body)
    return {
        "token": issue_share(project_id, subject_id, kind=kind),
        "kind": kind,
        "expires_in": SHARE_TTL_SECONDS,
    }


@router.post("/share/revoke")
async def revoke_share(body: ShareBody, project_id: str = Depends(get_project_id)) -> dict:
    """Stop sharing one subject: every link minted for it so far stops resolving.

    Deliberately NOT behind `require_user`. The same ingest key that mints a link in CI is the one
    that has to be able to pull it back, and revoking only ever narrows access — it destroys no
    data. It is also scoped by `project_id`, so a key can only revoke its own workspace's links.
    """
    kind, subject_id = _subject(body)

    def work():
        with SyncSessionLocal() as s:
            repo.share_revoke(s, project_id, kind, subject_id)

    await run_in_threadpool(work)
    return {"revoked": True, "kind": kind, "id": subject_id}


@router.get("/share/{token}")
async def read_share(token: str) -> dict:
    """Anonymous read of a shared object — NO auth dependency, by design."""
    try:
        claims = verify_share(token)
    except TokenError:
        raise _NOT_FOUND from None
    project_id, kind, subject_id = claims["project_id"], claims["kind"], claims["subject_id"]

    revoked_at = await run_in_threadpool(_revoked_at, project_id, kind, subject_id)
    if revoked_at is not None and claims["issued_at"] <= revoked_at:
        raise _NOT_FOUND

    if kind == "conversation":
        return await _shared_conversation(project_id, subject_id)
    if kind == "gate":
        return await _shared_gate(project_id, subject_id)
    raise _NOT_FOUND  # a kind this build doesn't read — same answer as a bad token


def _revoked_at(project_id: str, kind: str, subject_id: str) -> int | None:
    with SyncSessionLocal() as s:
        ts = repo.share_revoked_at(s, project_id, kind, subject_id)
    return int(ts.timestamp()) if ts else None


async def _shared_conversation(project_id: str, thread_id: str) -> dict:
    advisory = await advisory_score_names(project_id)
    turns = await async_reader.session_turns(project_id, thread_id, advisory)
    if not turns:
        raise _NOT_FOUND

    by_trace = await async_reader.scores_by_trace(project_id, [t["trace_id"] for t in turns])
    # Spans in parallel, same as the authed page does with Promise.all over getTrace.
    spans = await asyncio.gather(
        *(async_reader.trace_spans(project_id, t["trace_id"]) for t in turns)
    )
    for t, t_spans in zip(turns, spans, strict=True):
        t["scores"] = by_trace.get(t["trace_id"], [])
        t["verdict"] = rollup_verdict(t["scores"], advisory)
        t["spans"] = t_spans

    return {
        "kind": "conversation",
        "thread_id": thread_id,
        "turns": turns,
        "scores": await async_reader.conversation_scores(project_id, thread_id),
    }


# What a case's `detail` may say in public. An ALLOWLIST, not a blocklist: the dict is written by
# the gate/scenario pipeline and grows, and the keys being kept out are the linking ones
# (`trace_ids`, `candidate_trace_id`) — a new key must be opted in, never leak by default.
_PUBLIC_DETAIL_KEYS = frozenset(
    {
        "allow_tool_errors",
        "erroring_steps",
        "error",
        "expectations",
        "failed_expectations",
        "failed_scores",
        "match_mode",
        "missing_tools",
        "quality_pass",
        "quality_reason",
        "reason",
        "run_errors",
        "scores",
        "tool_errors",
        "tools_ok",
        "turns",
    }
)


def _public_gate(gate: GateRun, agent_slug: str | None, cases: list[tuple]) -> dict:
    """The verdict, and only the verdict.

    Left out on purpose, because this URL ends up in a public pull request: `candidate_trace_id`
    and `detail["trace_ids"]` (they name traces nobody anonymous may read), `evaluation_case_id` /
    `scenario_id` (internal ids that would enumerate the suite), `project_id`, and `agent_id`.
    """
    return {
        "kind": "gate",
        "id": gate.id,
        "agent": agent_slug,
        "env": gate.env,
        "git_ref": gate.git_ref,
        "pr_number": gate.pr_number,
        "status": gate.status,
        "total": gate.total,
        "passed": gate.passed,
        "failed": gate.failed,
        "skipped": gate.skipped,
        "latency_ms": gate.latency_ms,
        "total_tokens": gate.total_tokens,
        "warnings": gate.warnings or [],
        "created_at": gate.created_at.isoformat() if gate.created_at else None,
        "finished_at": gate.finished_at.isoformat() if gate.finished_at else None,
        "cases": [
            {
                "title": title,
                "verdict": gc.verdict,
                "detail": {k: v for k, v in (gc.detail or {}).items() if k in _PUBLIC_DETAIL_KEYS},
            }
            for gc, title in cases
        ],
    }


async def _shared_gate(project_id: str, gate_id: str) -> dict:
    def work():
        with SyncSessionLocal() as s:
            g = s.get(GateRun, gate_id)
            if not g or g.project_id != project_id:
                return None
            agent = s.get(Agent, g.agent_id)
            return _public_gate(
                g, agent.slug if agent else None, repo.gate_cases_with_titles(s, gate_id)
            )

    res = await run_in_threadpool(work)
    if res is None:
        raise _NOT_FOUND
    return res
