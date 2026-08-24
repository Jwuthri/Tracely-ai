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
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tracely.api.advisory import advisory_score_names
from tracely.api.auth import get_project_id
from tracely.config import settings
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
        payload = await _shared_conversation(project_id, subject_id)
    elif kind == "gate":
        payload = await _shared_gate(project_id, subject_id)
    else:
        raise _NOT_FOUND  # a kind this build doesn't read — same answer as a bad token
    # The footer tells a visitor how long the link lives; the token already knows.
    return {**payload, "expires_at": claims["expires_at"]}


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


# A gate verdict is an ALLOW-LIST of fields, not "the gate page minus secrets". The page is
# forwardable and lands in public pull requests that get crawled within hours, so anything not
# named here is absent by construction — including fields nobody has written yet.
#
# SHOWN: agent name, verdict, case counts, case labels, the NAMES of the checks that failed.
# HIDDEN, on purpose and against the authed view:
#   · every scrap of prompt/response/tool I/O, system prompts, candidate-vs-original diffs
#   · judge rationale — `failed_scores` reads "name: the agent replied '…'", and that quote is
#     the customer's end-user PII. Only the name before the colon survives.
#   · `failed_expectations` / `detail.reason` (authored scenario text), `detail.error` and
#     `warnings` (hostnames, endpoint URLs, stack traces)
#   · model + provider names, token counts, cost, latency — vendor stack and spend
#   · branch names (`git_ref` is emitted only when it looks like a commit SHA), repo/org, env
#   · trace ids, case/scenario ids, the gate id, project/workspace name, and who triggered the run
_SHA = re.compile(r"[0-9a-f]{7,40}\Z")

_STRUCTURAL_CHECKS = (
    # (detail key, predicate, public check name). A fixed category name, never a message.
    ("failed_expectations", bool, "scenario_expectation"),
    ("error", bool, "endpoint_error"),
    ("missing_tools", bool, "missing_tools"),
    ("run_errors", bool, "run_outcome"),
    ("erroring_steps", bool, "tool_errors"),
    ("tools_ok", lambda v: v is False, "tool_sequence"),
    ("quality_pass", lambda v: v is False, "answer_quality"),
)


def _failed_checks(detail: dict | None) -> list[str]:
    """WHAT failed, never WHY.

    Evaluator names come from `failed_scores`, which the gate writes as `"name: judge comment"` —
    the comment quotes the agent's answer, so everything from the first colon on is dropped.
    Structural failures have no evaluator, so they report a fixed category name instead.
    """
    d = detail or {}
    out = [
        name
        for raw in (d.get("failed_scores") or [])
        if (name := str(raw).split(":", 1)[0].strip())
    ]
    out += [name for key, ok, name in _STRUCTURAL_CHECKS if ok(d.get(key))]
    return list(dict.fromkeys(out))  # same evaluator can sink several turns


def _public_gate(gate: GateRun, agent_slug: str | None, cases: list[tuple]) -> dict:
    """The verdict, and only the verdict — see the allow-list note above."""
    ran_at = gate.finished_at or gate.created_at
    return {
        "kind": "gate",
        "agent": agent_slug,
        "status": gate.status,
        "total": gate.total,
        "passed": gate.passed,
        "failed": gate.failed,
        "skipped": gate.skipped,
        "pr_number": gate.pr_number,
        # A SHA is public the moment the PR is; a branch name is roadmap ("feat/acme-acquisition").
        "sha": gate.git_ref[:7] if gate.git_ref and _SHA.match(gate.git_ref) else None,
        "ran_at": ran_at.isoformat() if ran_at else None,
        # Whether this deployment lets a stranger sign up — it picks which CTA the page shows.
        # Our own config, not a customer's; discoverable by visiting /signup either way.
        "signup_open": settings.allow_public_signup,
        "cases": [
            {
                "label": title,
                "verdict": gc.verdict,
                "evaluators": _failed_checks(gc.detail),
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
