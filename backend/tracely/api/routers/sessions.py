"""Session/thread endpoints: traces grouped into conversations + per-turn rollups.

Pure HTTP shaping — all ClickHouse access lives in `infrastructure.clickhouse.async_reader`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from tracely.api.advisory import advisory_score_names
from tracely.config import settings
from tracely.api.auth import get_project_id, require_user
from tracely.domain.traces.replay import build_replay
from tracely.domain.evaluation.verdict import rollup_verdict
from tracely.infrastructure.clickhouse import async_reader, deletes
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.services.rolling_summary_service import RollingSummaryService

router = APIRouter(prefix="/api")


@router.get("/sessions")
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    from_ts: str | None = None,
    to_ts: str | None = None,
    evals: bool = False,
    sort: str = "recent",
    order: str = "desc",
    agent: str = "",
    project_id: str = Depends(get_project_id),
) -> list[dict]:
    """Traces grouped into threads by conversation/session (a trace with no conversation is its
    own 1-turn thread). Each row: first user input, last agent answer, turns, tokens, cost,
    status, and the thread's CONVERSATION-level eval scores (the C-row metric columns).

    `from_ts`/`to_ts` (ISO-8601, treated as UTC) bound the trace's `start_time`; `offset` pages
    the threads (newest first) for the UI's "Load more". Callers derive "has more" from
    `len(rows) == limit`.

    `evals=true` also lists Tracely's own runs — an evaluation, a scenario — as ordinary rows
    tagged with `internal_kind`. That is the traces list's "Evals" toggle.

    `sort` is one of `async_reader.SESSION_SORTS` (`recent` | `started` | `duration` | `tokens`)
    with `order` `asc`/`desc` — the table's sortable column headers. An unknown
    value falls back to `recent` rather than erroring: a sort is a view preference, and 400-ing a
    saved link because a column was renamed is worse than showing the default order.

    `agent` (a registry agent id) keeps only that agent's threads. Each row also carries `agent`
    (the slug) — the conversation's agent, i.e. the tenant when the SDK declared one."""
    advisory = await advisory_score_names(project_id)
    rows = await async_reader.sessions_overview(
        project_id,
        limit,
        offset,
        from_ts,
        to_ts,
        advisory,
        include_internal=evals,
        sort=sort,
        order=order,
        agent_id=agent,
    )
    conv_scores = await async_reader.conversation_scores_by_thread(
        project_id, [r["thread"] for r in rows]
    )

    def slugs() -> dict[str, str]:
        with SyncSessionLocal() as s:
            return {a.id: a.slug for a in repo.agents_list(s, project_id)}

    slug_of = await run_in_threadpool(slugs) if rows else {}
    for r in rows:
        r["scores"] = conv_scores.get(r["thread"], [])
        # Never blank: a conversation Tracely can't name an agent for still belongs to the
        # fallback agent — that IS how ingest files it, and a blank cell reads as "no data" when
        # the real answer is `default`. The exception is Tracely's own runs (the Evals toggle):
        # they were produced by no agent, and `default` there would claim a customer agent
        # produced the product's work.
        slug = slug_of.get(r.get("agent_id") or "", "")
        r["agent"] = slug or ("" if r.get("internal_kind") else settings.default_agent_slug)
    return rows


@router.get("/sessions/{thread_id}/export")
async def export_session(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """The whole conversation as one JSON object — what the table's copy button puts on the
    clipboard.

    Assembled server-side from three queries rather than letting the browser stitch it from a
    session fetch plus one trace fetch per turn: on a 60-turn thread that was 61 requests, and the
    result depended on which rows happened to be expanded.
    """
    return await _export_thread(project_id, thread_id, await advisory_score_names(project_id))


async def _export_thread(project_id: str, thread_id: str, advisory: Sequence[str]) -> dict:
    """One conversation's export payload. Shared with the bulk export so a line of the NDJSON dump
    and a single-thread fetch are byte-identical — two shapes for "the conversation" is exactly the
    drift that makes an export untrustworthy."""
    turns = await async_reader.session_turns(project_id, thread_id, advisory)
    spans = await async_reader.thread_spans_full(project_id, thread_id)
    conv_scores = await async_reader.conversation_scores(project_id, thread_id)
    per_trace = await async_reader.scores_by_trace(project_id, [t["trace_id"] for t in turns])

    by_trace: dict[str, list[dict]] = {}
    for s in sorted(spans, key=lambda s: s.get("start_time") or ""):
        by_trace.setdefault(s.get("trace_id", ""), []).append(
            {
                "span_id": s.get("span_id"),
                "parent_span_id": s.get("parent_span_id"),
                "type": s.get("type"),
                "name": s.get("name"),
                "level": s.get("level"),
                "status_message": s.get("status_message") or None,
                "agent_id": s.get("agent_id") or None,
                "model": s.get("model_id") or None,
                "start_time": _iso(s.get("start_time")),
                "end_time": _iso(s.get("end_time")),
                "input": s.get("input"),
                "output": s.get("output"),
                # Already in the row we selected — dropping them made a span export useless for the
                # question people actually ask of one ("what did the agent call, with what?").
                "tool_calls": s.get("tool_calls") or None,
                "tool_call_names": list(s.get("tool_call_names") or []),
            }
        )

    return {
        "thread_id": thread_id,
        "turns": len(turns),
        "tokens": sum(int(t.get("tokens") or 0) for t in turns),
        "cost_usd": sum(float(t.get("cost") or 0.0) for t in turns),
        "conversation_scores": conv_scores,
        "messages": [
            {
                "index": i,
                "trace_id": t["trace_id"],
                "ts": _iso(t.get("ts")),
                "input": t.get("input"),
                "output": t.get("output"),
                "model": t.get("model") or None,
                "tokens": int(t.get("tokens") or 0),
                "cost_usd": float(t.get("cost") or 0.0),
                "latency_ms": t.get("latency_ms"),
                "failing": bool(t.get("failing")),
                "scores": per_trace.get(t["trace_id"], []),
                "steps": by_trace.get(t["trace_id"], []),
            }
            for i, t in enumerate(turns, start=1)
        ],
    }


def _iso(v) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (v or None)


def _meta_of(row: dict) -> dict:
    """A listing row's metadata. `sessions_overview` hands it over already parsed by
    `parse_thread_meta`, with the `tracely.metadata.` prefix stripped — so `meta=business_id=…`
    matches the key the UI shows, not the raw attribute name. Anything else is "no metadata":
    a surprise from one conversation must not abort an export that is already streaming."""
    meta = row.get("metadata")
    return meta if isinstance(meta, dict) else {}


_EXPORT_PAGE = 200  # threads per sessions_overview call


@router.get("/export")
async def export_workspace(
    limit: int = 0,
    from_ts: str | None = None,
    to_ts: str | None = None,
    evals: bool = False,
    meta: str | None = None,
    project_id: str = Depends(get_project_id),
) -> StreamingResponse:
    """Every conversation in the workspace as NDJSON — one line per thread, each line exactly what
    `GET /sessions/{thread_id}/export` returns for that thread.

    Streamed and paged (`_EXPORT_PAGE` threads per query) rather than assembled into one list: a
    workspace's whole history does not fit in the response buffer of the box exporting it, and a
    client that has to wait for the last thread before seeing the first cannot resume.

    `limit` caps the thread count (0 = all); `from_ts`/`to_ts`/`evals` mean what they do on
    `GET /sessions`. Like that list, content-less 1-turn threads (no input, no output, not failing)
    are not emitted — they carry nothing to export.

    `meta=<key>=<value>` keeps only conversations whose merged span metadata has that exact pair —
    `meta=business_id=2a73b883-…` for one tenant's traffic. Matched against the listing row, which
    already carries the map, so a non-matching conversation costs one comparison instead of the
    four queries its export would have taken.
    """
    advisory = await advisory_score_names(project_id)
    meta_key, _, meta_value = (meta or "").partition("=")

    async def gen() -> AsyncIterator[str]:
        offset = seen = 0
        while True:
            page = await async_reader.sessions_overview(
                project_id,
                _EXPORT_PAGE,
                offset,
                from_ts,
                to_ts,
                advisory,
                include_internal=evals,
            )
            for row in page:
                if meta_key and _meta_of(row).get(meta_key) != meta_value:
                    continue
                payload = await _export_thread(project_id, row["thread"], advisory)
                # default=str: score rows carry datetimes that FastAPI would have encoded for us —
                # streaming means we do our own dumping, and a raw TypeError here would truncate an
                # otherwise-good export mid-line.
                yield json.dumps(payload, default=str) + "\n"
                seen += 1
                if limit and seen >= limit:
                    return
            if len(page) < _EXPORT_PAGE:
                return
            offset += _EXPORT_PAGE

    # ponytail: 4 queries per thread (reuses the single-thread path). Batch by trace_id chunks if
    # exporting a large workspace gets slow.
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="tracely-export.ndjson"',
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class DeleteSessionsBody(BaseModel):
    threads: list[str]


@router.delete("/sessions", dependencies=[Depends(require_user)])
async def delete_sessions(
    body: DeleteSessionsBody, project_id: str = Depends(get_project_id)
) -> dict:
    """Delete whole conversations (multi-select in the UI): every trace in each thread plus its
    scores. Idempotent — threads that no longer exist just don't contribute to the count."""
    threads = [t for t in dict.fromkeys(body.threads) if t]
    if not threads:
        raise HTTPException(status_code=400, detail="no threads given")
    traces = await deletes.delete_threads(project_id, threads)
    return {"threads": len(threads), "traces": traces}


@router.get("/sessions/{thread_id}")
async def get_session(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """The turns (traces) inside one thread, oldest-first — a simple conversation replay. Each
    turn carries its auto-eval scores (the same the trace page shows); the thread carries its
    own CONVERSATION-level scores."""
    advisory = await advisory_score_names(project_id)
    turns = await async_reader.session_turns(project_id, thread_id, advisory)
    by_trace = await async_reader.scores_by_trace(project_id, [t["trace_id"] for t in turns])
    for t in turns:
        t["scores"] = by_trace.get(t["trace_id"], [])
        t["verdict"] = rollup_verdict(t["scores"], advisory)
    thread_scores = await async_reader.conversation_scores(project_id, thread_id)
    return {"thread_id": thread_id, "turns": turns, "scores": thread_scores}


def _shape_declared_agent(ag: dict, obs_counts: dict[str, int]) -> dict:
    """Normalize a user-declared agent into the panel shape: `name`/`description`/`tools` are
    guaranteed present, each tool annotated with how many times it actually ran (observed spans).
    `tools` may be a dict-of-tools (the documented shape) or a list.

    Every OTHER key the caller declared (`system_prompt`, `model`, `guardrails`, `config`, …) is
    passed through untouched — the catalog is free-form JSON on the way in, so narrowing it here
    would silently drop config the user deliberately sent. Same for per-tool extras."""
    raw = ag.get("tools")
    entries = (
        raw.items()
        if isinstance(raw, dict)
        else (
            [(t.get("name", ""), t) for t in raw if isinstance(t, dict)]
            if isinstance(raw, list)
            else []
        )
    )
    tools = []
    for key, tdef in entries:
        tdef = tdef if isinstance(tdef, dict) else {}
        name = tdef.get("name") or key
        tools.append(
            {
                **tdef,
                "name": name,
                "description": tdef.get("description") or "",
                "parameters": tdef.get("parameters") or {},
                "count": obs_counts.get(name, 0),
            }
        )
    return {
        **ag,
        "name": ag.get("name") or "agent",
        "description": ag.get("description") or "",
        "tools": tools,
    }


@router.get("/sessions/{thread_id}/agents")
async def get_session_agents(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """A conversation's agents — both the user-DECLARED catalog (sent via the SDK, rich: name,
    description, tools with parameters) and the OBSERVED agents derived from the trace spans (with
    tool-execution counts). The panel shows declared first; observed fills in when nothing was
    declared. Declared tools are annotated with their observed execution counts."""
    observed = await async_reader.thread_agents(project_id, thread_id)
    ids = [a["agent_id"] for a in observed if a["agent_id"]]

    def work() -> tuple[dict[str, dict], list]:
        names: dict[str, dict] = {}
        with SyncSessionLocal() as s:
            for aid in ids:
                a = repo.agent_in_project(s, project_id, aid)
                if a:
                    names[aid] = {"slug": a.slug, "display_name": a.display_name or a.slug}
            row = repo.conversation_agents_get(s, project_id, thread_id)
            declared = list(row.agents) if row and row.agents else []
        return names, declared

    name_map, declared_raw = await run_in_threadpool(work)
    for a in observed:
        info = name_map.get(a["agent_id"])
        a["slug"] = info["slug"] if info else ""
        a["name"] = (info["display_name"] if info else "") or a["agent_id"] or "agent"

    obs_counts: dict[str, int] = {}
    for a in observed:
        for t in a["tools"]:
            obs_counts[t["name"]] = obs_counts.get(t["name"], 0) + t["count"]
    declared = [
        _shape_declared_agent(ag, obs_counts) for ag in declared_raw if isinstance(ag, dict)
    ]

    return {"thread_id": thread_id, "declared": declared, "observed": observed}


def _declared_tools(agent: dict) -> list[dict]:
    """`{name, description}` per tool of a free-form declared agent. `tools` may be a
    dict-of-tools (the documented shape), a list, or any junk the config POST accepted — the
    catalog is deliberately unvalidated, so a non-iterable here must degrade to [], never 500.
    The description is what the Fleet view's tool-wall card shows."""
    raw = agent.get("tools")
    if isinstance(raw, dict):
        return [
            {"name": str(k), "description": str((v or {}).get("description", "") if isinstance(v, dict) else "")}
            for k, v in raw.items()
        ]
    if isinstance(raw, list):
        return [
            {
                "name": str(t.get("name", "") if isinstance(t, dict) else t),
                "description": str(t.get("description", "")) if isinstance(t, dict) else "",
            }
            for t in raw
        ]
    return []


@router.get("/sessions/{thread_id}/replay")
async def get_session_replay(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """The conversation as a playable script: who acted, when, under whom, and at which station
    (`domain.traces.replay` — the Replay and Fleet views share this one read). Registry
    slugs/display names double as the alias map, so DELEGATE callees and agent-named tool calls
    land on the right actor. `declared` is the user-sent agent-definition catalog for this
    conversation (name/description/tools) when one exists — the Fleet view's inspect card."""
    return await replay_payload(project_id, thread_id)


async def replay_payload(project_id: str, thread_id: str) -> dict:
    """The replay body, shared by the authed endpoint above and the public share read
    (`share.py`) — the share page's fleet replay must be the same script the authed one plays."""
    spans = await async_reader.thread_spans_full(project_id, thread_id)
    ids = sorted({str(s.get("agent_id")) for s in spans if s.get("agent_id")})

    def registry() -> tuple[dict[str, str], dict[str, str], list[dict]]:
        names: dict[str, str] = {}
        aliases: dict[str, str] = {}
        with SyncSessionLocal() as s:
            for aid in ids:
                row = repo.agent_in_project(s, project_id, aid)
                if row:
                    names[aid] = row.display_name or row.slug
                    aliases[row.slug] = aid
                    if row.display_name:
                        aliases[row.display_name] = aid
            cfg = repo.conversation_agents_get(s, project_id, thread_id)
            declared_raw = list(cfg.agents) if cfg and cfg.agents else []
        declared = [
            {
                "name": str(a.get("name") or ""),
                "description": str(a.get("description") or ""),
                "tools": _declared_tools(a),
            }
            for a in declared_raw
            if isinstance(a, dict)
        ]
        return names, aliases, declared

    name_map, aliases, declared = await run_in_threadpool(registry)
    return {
        "thread_id": thread_id,
        "declared": declared,
        **build_replay(spans, name_map, aliases),
    }


class SessionConfigBody(BaseModel):
    agents: list[dict]  # free-form; only `name`/`description`/`tools` carry panel meaning
    meta: dict | None = None


@router.post("/sessions/{thread_id}/config")
async def put_session_config(
    thread_id: str, body: SessionConfigBody, project_id: str = Depends(get_project_id)
) -> dict:
    """Declare a conversation's agent config without the SDK — the HTTP twin of
    `tracely.trace(..., agents=[...])`. Same store, same latest-wins-per-thread semantics; read it
    back (merged with observed agents) from `GET /sessions/{thread_id}/agents`.

    Each agent is free-form JSON. `name`/`description`/`tools` drive the panel; anything else
    (`system_prompt`, `model`, `guardrails`, `config`, …) is stored and returned verbatim."""
    if not thread_id:
        raise HTTPException(status_code=400, detail="thread_id required")

    def work() -> None:
        with SyncSessionLocal() as s:
            repo.conversation_agents_upsert(
                s, project_id, thread_id=thread_id, agents=body.agents, meta=body.meta
            )

    await run_in_threadpool(work)
    return {"thread_id": thread_id, "agents": len(body.agents)}


class GenerateSummaryBody(BaseModel):
    force: bool = False  # rebuild from scratch (drop cached step rows)


@router.get("/sessions/{thread_id}/chain-progress")
async def get_chain_progress(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """Where each sequential column's durable judge conversation stands on this thread: how many
    of the thread's turns are chained (`chained`/`turns`, `up_to_date`), the payload the next turn
    will be seeded with, and when the chain last advanced. One entry per enabled sequential
    message/step column (conversation-level columns have one item — the mode is inert there);
    columns with no progress row yet report `chained: 0`.
    """
    turns = await async_reader.thread_turn_count(project_id, thread_id)

    def load():
        with SyncSessionLocal() as s:
            return (
                repo.evaluator_enabled_specs(s, project_id),
                repo.chain_progress_load(s, project_id, thread_id),
            )

    specs, progress = await run_in_threadpool(load)
    metrics = []
    for spec in specs:
        config = spec.get("config") or {}
        if str(config.get("execution_mode") or "batch") != "sequential":
            continue
        if spec["level"] == "CONVERSATION":
            continue
        row = progress.get(spec["score_name"]) or {}
        chained = len(row.get("turn_ids") or [])
        metrics.append(
            {
                "score_name": spec["score_name"],
                "level": spec["level"],
                "chained": chained,
                "turns": turns,
                "up_to_date": turns > 0 and chained >= turns,
                "last_payload": row.get("last_payload"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {"thread_id": thread_id, "metrics": metrics}


@router.get("/sessions/{thread_id}/rolling-summary")
async def get_rolling_summary(thread_id: str, project_id: str = Depends(get_project_id)) -> dict:
    """The stored rolling summary for a conversation (accumulated items + the @HISTORY rendering),
    or an empty shell when none has been generated yet."""
    return await run_in_threadpool(RollingSummaryService.get_for_thread, project_id, thread_id)


@router.get("/sessions/{thread_id}/rolling-summary/by-level")
async def get_rolling_summary_by_level(
    thread_id: str, project_id: str = Depends(get_project_id)
) -> dict:
    """Per-row rolling summaries for the table's three levels — `conversation` (whole thread),
    `traces[trace_id]` (through that turn), and `spans[span_id]` (through that step). Each is the
    accumulated summary AS OF that row, rendered + clipped for display (the cell shows the top
    512 chars)."""
    return await run_in_threadpool(RollingSummaryService.by_level, project_id, thread_id)


@router.post("/sessions/{thread_id}/rolling-summary/generate")
async def generate_rolling_summary(
    thread_id: str,
    body: GenerateSummaryBody = GenerateSummaryBody(),
    project_id: str = Depends(get_project_id),
) -> dict:
    """Generate (or refresh with `force`) the per-span rolling summary for a conversation. Short
    steps are kept verbatim; only large steps hit the summarizer LLM."""

    def work() -> dict:
        return RollingSummaryService().build_for_thread(project_id, thread_id, force=body.force)

    return await run_in_threadpool(work)
