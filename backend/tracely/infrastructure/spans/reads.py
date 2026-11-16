"""Async Postgres reads for the API — the twin of `clickhouse.async_reader`, function for function.

Same names, signatures and return shapes, so routers and services do not change (pick the store
in `tracely.infrastructure.spans`). Two sources:

- `traces`  — the per-trace rollup (`spans.rollup`): every list, count and trend;
- `events`  — spans, by index: one trace or thread, span-level metrics (per model, per span name).

The SQL twins every caller relies on, defined once each:

- `_FAILING` / `_FAILING_THREADS` — the verdict policy: a FAIL on a NON-advisory evaluator,
  `name <> ALL(:adv)` (an empty array excludes nothing, exactly like ClickHouse `NOT IN []`).
  Mirror of `domain.evaluation.verdict`; change them together.
- `_REAL` — `internal_kind = ''`: Tracely's own eval/sim recordings stay out of every list, count
  and metric; `sessions_overview(include_internal=True)` is the only opt-in.

Deliberate differences from ClickHouse are listed in design/part2-tracely/14-clickhouse-to-postgres.md §5.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from tracely.domain.traces.metadata import parse_thread_meta
from tracely.domain.traces.spans import system_prompt
from tracely.infrastructure.spans._pg import aquery, aquery_rows, like

_ONLINE = "source = 'EVAL' AND evaluation_case_id = ''"
_FAILING = (
    f"SELECT trace_id FROM scores WHERE project_id = :p AND {_ONLINE} AND verdict = 'FAIL' "
    "AND name <> ALL(:adv)"
)
_FAILING_THREADS = (
    f"SELECT session_id FROM scores WHERE project_id = :p AND {_ONLINE} AND verdict = 'FAIL' "
    "AND evaluation_level = 'CONVERSATION' AND name <> ALL(:adv)"
)
_REAL = "internal_kind = ''"
_SCORE_COLS = (
    "name, evaluation_level, observation_id, value, string_value, verdict, comment, data_type"
)

# The span columns the eval runner / regression / gate / FI read (`trace_reader._SPAN_COLS`).
SPAN_COLS = [
    "span_id",
    "parent_span_id",
    "type",
    "name",
    "level",
    "status_message",
    "start_time",
    "end_time",
    "completion_start_time",
    "agent_id",
    "agent_version_id",
    "agent_run_id",
    "turn_id",
    "step_id",
    "model_id",
    "input",
    "output",
    "tool_call_names",
    "tool_calls",
    "trace_id",
    "is_app_root",
    "conversation_id",
    "env",
    "caller_agent_id",
    "callee_agent_id",
    "metadata",
    "tokens",
    "cost",
]


# ClickHouse dateDiff('millisecond', a, b): whole milliseconds, each side truncated.
def _ms(a: str, b: str) -> str:
    return (
        f"(floor(extract(epoch FROM {b}) * 1000) - floor(extract(epoch FROM {a}) * 1000))::bigint"
    )


_SPAN_MS = _ms("start_time", "coalesce(end_time, start_time)")
_UTC_DAY = "(ts_min AT TIME ZONE 'UTC')::date"  # ClickHouse toDate(): the UTC calendar day


def _days_ago(param: str = "d") -> str:
    return f"now() - make_interval(days => :{param})"


def _mins_ago(param: str = "w") -> str:
    return f"now() - make_interval(mins => :{param})"


def _int(v) -> int:
    return int(v) if v is not None else 0


# ── traces ────────────────────────────────────────────────────────────────────


async def traces_overview(project_id: str, limit: int, advisory: Sequence[str] = ()) -> list[dict]:
    rows = await aquery(
        "SELECT trace_id, ts_min AS ts, spans, coalesce(nullif(root_name, ''), first_name) AS root_name, "
        "agent_id, CASE WHEN has_error THEN 1 ELSE 0 END AS has_error "
        f"FROM traces WHERE project_id = :p AND {_REAL} ORDER BY ts_min DESC LIMIT :n",
        {"p": project_id, "n": max(limit, 0)},
    )
    if rows:
        ev = await aquery_rows(
            "SELECT trace_id, bool_or(verdict = 'FAIL' AND name <> ALL(:adv)) FROM scores "
            f"WHERE project_id = :p AND {_ONLINE} AND trace_id = ANY(:ids) GROUP BY trace_id",
            {"p": project_id, "adv": list(advisory), "ids": [r["trace_id"] for r in rows]},
        )
        verdict = {tid: ("FAIL" if fail else "PASS") for tid, fail in ev}
        for r in rows:
            r["eval"] = verdict.get(r["trace_id"])
    return rows


async def trace_spans(project_id: str, trace_id: str) -> list[dict]:
    return await aquery(
        "SELECT span_id, parent_span_id, name, type, level, status_message, "
        "start_time, end_time, completion_start_time, agent_id, agent_run_id, turn_id, step_name, "
        "model_id, input, output, metadata, conversation_id, tokens, cost "
        "FROM events WHERE project_id = :p AND trace_id = :t ORDER BY start_time",
        {"p": project_id, "t": trace_id},
    )


async def thread_spans_full(project_id: str, thread_id: str) -> list[dict]:
    """Same columns as the sync eval reader, so the template preview resolves what the run grades."""
    return await aquery(
        f"SELECT {', '.join(SPAN_COLS)} FROM events WHERE project_id = :p "
        "AND (conversation_id = :th OR trace_id = :th) ORDER BY start_time",
        {"p": project_id, "th": thread_id},
    )


async def thread_turn_count(project_id: str, thread_id: str) -> int:
    rows = await aquery_rows(
        "SELECT count(*) FROM traces WHERE project_id = :p "
        f"AND (conversation_id = :th OR trace_id = :th) AND {_REAL}",
        {"p": project_id, "th": thread_id},
    )
    return _int(rows[0][0]) if rows else 0


async def trace_scores(project_id: str, trace_id: str, thread_id: str) -> list[dict]:
    return await aquery(
        f"SELECT {_SCORE_COLS} FROM scores WHERE project_id = :p AND {_ONLINE} "
        "AND (trace_id = :t OR (evaluation_level = 'CONVERSATION' AND session_id = :th)) "
        "ORDER BY evaluation_level, name",
        {"p": project_id, "t": trace_id, "th": thread_id},
    )


def _meta_int(key: str) -> str:
    """ClickHouse toUInt64OrZero(metadata[key]): the number, or 0 for missing / not a number."""
    v = f"(metadata->>'{key}')"
    return f"CASE WHEN {v} ~ '^[0-9]+$' THEN {v}::bigint ELSE 0 END"


async def evaluator_cost(project_id: str, days: int = 30) -> dict[str, dict]:
    rows = await aquery_rows(
        "SELECT name, count(*) FILTER (WHERE metadata ? 'eval.total_tokens') AS runs, "
        f"sum({_meta_int('eval.input_tokens')})::bigint, sum({_meta_int('eval.output_tokens')})::bigint, "
        f"sum({_meta_int('eval.total_tokens')})::bigint, "
        "(array_agg(coalesce(metadata->>'eval.model', '') ORDER BY created_at DESC))[1] "
        f"FROM scores WHERE project_id = :p AND {_ONLINE} AND created_at >= {_days_ago()} "
        "GROUP BY name HAVING count(*) FILTER (WHERE metadata ? 'eval.total_tokens') > 0",
        {"p": project_id, "d": days},
    )
    return {
        r[0]: {
            "runs": _int(r[1]),
            "input_tokens": _int(r[2]),
            "output_tokens": _int(r[3]),
            "total_tokens": _int(r[4]),
            "model": r[5] or "",
        }
        for r in rows
    }


async def traces_in_window(project_id: str, days: int) -> int:
    rows = await aquery_rows(
        f"SELECT count(*) FROM traces WHERE project_id = :p AND ts_max >= {_days_ago()} "
        f"AND env <> 'ci' AND {_REAL}",
        {"p": project_id, "d": days},
    )
    return _int(rows[0][0]) if rows else 0


# ── monitors ──────────────────────────────────────────────────────────────────


async def score_samples_in_window(
    project_id: str, score_name: str, window_minutes: int, target_agent: str = ""
) -> list[dict]:
    """See the ClickHouse twin. `target_agent` matches any span of the trace carrying that agent,
    exactly as before (span-level, through the `events_agent` index)."""
    params = {"p": project_id, "n": score_name, "w": window_minutes}
    agent = ""
    if target_agent:
        agent = (
            "AND trace_id IN (SELECT DISTINCT trace_id FROM events WHERE project_id = :p "
            f"AND agent_id = :ag AND env <> 'ci' AND {_REAL} AND start_time >= {_mins_ago()}) "
        )
        params["ag"] = target_agent
    rows = await aquery_rows(
        f"SELECT verdict, value FROM scores WHERE project_id = :p AND {_ONLINE} AND name = :n "
        f"AND created_at >= {_mins_ago()} {agent}",
        params,
    )
    return [{"verdict": v or "", "value": val} for v, val in rows]


async def trace_failure_samples_in_window(
    project_id: str, window_minutes: int, advisory: Sequence[str], target_agent: str = ""
) -> list[dict]:
    params: dict[str, object] = {"p": project_id, "w": window_minutes, "adv": list(advisory)}
    agent = ""
    if target_agent:
        agent = "AND agent_id = :ag"
        params["ag"] = target_agent
    rows = await aquery_rows(
        "WITH m AS (SELECT trace_id FROM traces WHERE project_id = :p AND env <> 'ci' "
        f"  AND {_REAL} AND ts_max >= {_mins_ago()} {agent}) "
        "SELECT CASE WHEN m.trace_id IN (" + _FAILING + ") THEN 'FAIL' ELSE 'PASS' END FROM m",
        params,
    )
    return [{"verdict": r[0]} for r in rows]


# ── calibration ───────────────────────────────────────────────────────────────

# A score is reviewable only while its trace still exists (and is not one of Tracely's own runs).
_REVIEWABLE = (
    "EXISTS (SELECT 1 FROM traces t WHERE t.project_id = scores.project_id "
    "AND t.trace_id = scores.trace_id AND t.internal_kind = '')"
)


async def evaluator_catalog(project_id: str) -> list[dict]:
    rows = await aquery_rows(
        "SELECT name, (array_agg(evaluation_level ORDER BY created_at DESC))[1], count(*), "
        "count(*) FILTER (WHERE verdict = 'FAIL') "
        f"FROM scores WHERE project_id = :p AND {_ONLINE} AND verdict <> '' AND {_REVIEWABLE} "
        "GROUP BY name ORDER BY count(*) DESC",
        {"p": project_id},
    )
    return [
        {"name": r[0], "level": r[1] or "", "total": _int(r[2]), "fails": _int(r[3])} for r in rows
    ]


async def evaluator_score_queue(
    project_id: str, name: str, limit: int = 25, offset: int = 0, verdict: str = ""
) -> list[dict]:
    """A stable random sample (ordered by a hash of the score id), as in the ClickHouse twin."""
    rows = await aquery_rows(
        "SELECT trace_id, observation_id, session_id, evaluation_level, verdict, value, comment, "
        "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') "
        f"FROM scores WHERE project_id = :p AND {_ONLINE} AND name = :n AND verdict <> '' "
        f"AND {_REVIEWABLE} AND (:v = '' OR verdict = :v) "
        "ORDER BY md5(id) LIMIT :lim OFFSET :off",
        {
            "p": project_id,
            "n": name,
            "lim": max(limit, 0),
            "off": max(offset, 0),
            "v": verdict.upper(),
        },
    )
    return [
        {
            "trace_id": r[0] or "",
            "observation_id": r[1] or "",
            "session_id": r[2] or "",
            "evaluation_level": r[3] or "",
            "verdict": r[4] or "",
            "value": r[5],
            "comment": r[6] or "",
            "created_at": r[7],
        }
        for r in rows
    ]


# ── sessions / threads ────────────────────────────────────────────────────────

# Sortable list columns -> the aggregate each displays (a whitelist: ORDER BY cannot be bound).
SESSION_SORTS = {
    "recent": "last_ts",
    "started": "first_ts",
    "duration": "(last_ts - first_ts)",
    "tokens": "tokens",
}

_SESSION_HAYSTACK = "concat_ws(' ', model, metadata, agent_id, _root_name, subject_id)"


def session_filter_clauses(failing: bool | None, multi: bool | None, q: str) -> tuple[str, dict]:
    """Status / multi-turn / text filters over the whole thread set (not the loaded page).
    Returns `(sql_suffix, params)`; `None` = don't filter."""
    parts, params = [], {}
    if failing is True:
        parts.append(" AND failing = 1")
    elif failing is False:
        parts.append(" AND failing = 0")
    if multi is True:
        parts.append(" AND turns > 1")
    elif multi is False:
        parts.append(" AND turns = 1")
    if q and q.strip():
        parts.append(f" AND (text_hit OR {_SESSION_HAYSTACK} ILIKE :q)")
        params["q"] = like(q.strip()[:200])
    return "".join(parts), params


def session_order_clause(sort: str, order: str) -> str:
    """Ends in the unique `thread` (byte order, like ClickHouse) so LIMIT/OFFSET pages are total."""
    expr = SESSION_SORTS.get(sort, SESSION_SORTS["recent"])
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    return f'ORDER BY {expr} {direction}, last_ts DESC, thread COLLATE "C" ASC'


async def sessions_overview(
    project_id: str,
    limit: int,
    offset: int,
    from_ts: str | None = None,
    to_ts: str | None = None,
    advisory: Sequence[str] = (),
    include_internal: bool = False,
    sort: str = "recent",
    order: str = "desc",
    agent_id: str = "",
    failing: bool | None = None,
    multi: bool | None = None,
    q: str = "",
    count_only: bool = False,
) -> list[dict] | int:
    """Traces grouped into threads from the `traces` rollup. See the ClickHouse twin for the
    contract (filters over the whole set, content-less 1-turn threads dropped, text fetched for
    the page only — here straight from the rollup's root text)."""
    params: dict = {"p": project_id, "n": max(limit, 0), "o": max(offset, 0), "adv": list(advisory)}
    where = "" if include_internal else f" AND tr.{_REAL}"
    if from_ts:
        where += " AND tr.ts_max >= CAST(CAST(:from AS text) AS timestamptz)"
        params["from"] = from_ts
    if to_ts:
        where += " AND tr.ts_min < CAST(CAST(:to AS text) AS timestamptz)"
        params["to"] = to_ts
    if agent_id:
        where += " AND tr.agent_id = :ag"
        params["ag"] = agent_id
    filter_clause, fparams = session_filter_clauses(failing, multi, q)
    params.update(fparams)
    hits_cte = hit_col = hit_join = text_hit = ""
    if "q" in fparams:
        # any span's text in the thread (GENERATION inputs excluded: whole history + system prompt)
        hits_cte = (
            ", hits AS (SELECT DISTINCT trace_id FROM events WHERE project_id = :p "
            "AND (output ILIKE :q OR (type <> 'GENERATION' AND input ILIKE :q)))"
        )
        hit_col = ", (h.trace_id IS NOT NULL) AS t_q"
        hit_join = "LEFT JOIN hits h ON h.trace_id = tr.trace_id"
        text_hit = ", bool_or(t_q) AS text_hit"
    body = f"""
        WITH fail_t AS ({_FAILING}), fail_th AS ({_FAILING_THREADS}){hits_cte},
        t AS (
          SELECT tr.*, (f.trace_id IS NOT NULL) AS t_failing{hit_col}
          FROM traces tr LEFT JOIN (SELECT DISTINCT trace_id FROM fail_t) f ON f.trace_id = tr.trace_id
          {hit_join}
          WHERE tr.project_id = :p{where}
        ),
        meta AS (
          SELECT t.thread_id, jsonb_object_agg(kv.key, kv.value ORDER BY t.ts_max) AS md
          FROM t, jsonb_each_text(t.metadata) kv GROUP BY t.thread_id
        ),
        th AS (
          SELECT t.thread_id                                        AS thread,
            count(*)                                                AS turns,
            (array_agg(t.agent_id ORDER BY t.ts_max DESC))[1]       AS agent_id,
            (array_agg(t.trace_id ORDER BY t.ts_min))[1]            AS _first_trace,
            (array_agg(t.trace_id ORDER BY t.ts_min DESC))[1]       AS _last_trace,
            max(t.root_name)                                        AS _root_name,
            CASE WHEN max(t.internal_kind) <> '' THEN (max(t.root_name) <> '' OR max(t.subject_id) <> '')
                 ELSE (bool_or(t.has_input) OR bool_or(t.has_output)) END AS _has_content,
            sum(t.tokens)::bigint                                   AS tokens,
            sum(t.input_tokens)::bigint                             AS input_tokens,
            sum(t.output_tokens)::bigint                            AS output_tokens,
            (array_agg(t.model_id ORDER BY t.tokens DESC))[1]       AS model,
            sum(t.cost)::float8                                     AS cost,
            min(t.ts_min)                                           AS first_ts,
            max(t.ts_max)                                           AS last_ts,
            (array_agg(t.trace_id ORDER BY t.ts_max DESC))[1]       AS last_trace_id,
            CASE WHEN bool_or(t.t_failing) OR t.thread_id IN (SELECT session_id FROM fail_th)
                 THEN 1 ELSE 0 END                                  AS failing,
            max(t.internal_kind)                                    AS internal_kind,
            max(t.subject_id)                                       AS subject_id{text_hit}
          FROM t GROUP BY t.thread_id
        ),
        x AS (
          SELECT th.*, coalesce(meta.md, '{{}}'::jsonb)::text AS metadata
          FROM th LEFT JOIN meta ON meta.thread_id = th.thread
        )
        SELECT * FROM x
        WHERE NOT (turns = 1 AND NOT _has_content AND failing = 0){filter_clause}
        """
    if count_only:
        rows = await aquery_rows(f"SELECT count(*) FROM ({body}) c", params)
        return _int(rows[0][0]) if rows else 0
    rows = await aquery(f"{body} {session_order_clause(sort, order)} LIMIT :n OFFSET :o", params)
    ids = sorted(
        {t for r in rows if not r["internal_kind"] for t in (r["_first_trace"], r["_last_trace"])}
    )
    texts: dict[str, tuple] = {}
    if ids:
        texts = {
            r[0]: (r[1], r[2])
            for r in await aquery_rows(
                "SELECT trace_id, root_input, root_output FROM traces "
                "WHERE project_id = :p AND trace_id = ANY(:ids)",
                {"p": project_id, "ids": ids},
            )
        }
    for d in rows:
        first, last = d.pop("_first_trace"), d.pop("_last_trace")
        root_name = d.pop("_root_name")
        d.pop("_has_content")
        d.pop("text_hit", None)
        if d["internal_kind"]:
            d["first_input"], d["last_output"] = root_name, d["subject_id"]
        else:
            d["first_input"] = texts.get(first, ("", ""))[0]
            d["last_output"] = texts.get(last, ("", ""))[1]
        d["metadata"] = parse_thread_meta(d.get("metadata"))
    return rows


async def thread_agents(
    project_id: str, thread_id: str, spans: list[dict] | None = None
) -> list[dict]:
    """Agents in a thread and the tools each used, derived from its spans (pure Python — identical
    to the ClickHouse twin)."""
    if spans is None:
        spans = await thread_spans_full(project_id, thread_id)
    spans_by_agent: dict[str, int] = defaultdict(int)
    tools_touched: dict[str, set[str]] = defaultdict(set)
    tool_execs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_agent: dict[str, list[dict]] = defaultdict(list)
    models: dict[str, set[str]] = defaultdict(set)
    for s in spans:
        aid = s.get("agent_id") or ""
        spans_by_agent[aid] += 1
        by_agent[aid].append(s)
        if s.get("model_id"):
            models[aid].add(str(s["model_id"]))
        name = s.get("name")
        if s.get("type") == "TOOL" and name:
            tool_execs[aid][str(name)] += 1
            tools_touched[aid].add(str(name))
        for t in s.get("tool_call_names") or []:
            if t:
                tools_touched[aid].add(str(t))
    out: list[dict] = []
    for aid, span_count in spans_by_agent.items():
        tools = sorted(
            ({"name": n, "count": tool_execs[aid].get(n, 0)} for n in tools_touched[aid]),
            key=lambda t: (-t["count"], t["name"]),
        )
        out.append(
            {
                "agent_id": aid,
                "tools": tools,
                "span_count": span_count,
                "tool_call_count": sum(t["count"] for t in tools),
                "system_prompt": system_prompt(by_agent[aid]),
                "models": sorted(models[aid]),
            }
        )
    out.sort(key=lambda a: (a["tool_call_count"], a["span_count"]), reverse=True)
    return out


async def session_turns(
    project_id: str, thread_id: str, advisory: Sequence[str] = ()
) -> list[dict]:
    """A thread's turns oldest-first. An internal run's "message" is its root span's title plus
    input (what it did), not the judge's raw prompt — same rule as the ClickHouse twin."""
    return await aquery(
        "SELECT trace_id, "
        "CASE WHEN internal_kind <> '' THEN root_name || chr(10) || chr(10) || coalesce(root_input, '') "
        "     ELSE root_input END AS input, "
        "CASE WHEN internal_kind <> '' THEN coalesce(root_output, '') ELSE root_output END AS output, "
        "tokens, input_tokens, output_tokens, model_id AS model, cost, latency_ms, ts_min AS ts, "
        f"CASE WHEN trace_id IN ({_FAILING}) THEN 1 ELSE 0 END AS failing "
        "FROM traces WHERE project_id = :p AND thread_id = :th ORDER BY ts_min ASC",
        {"p": project_id, "th": thread_id, "adv": list(advisory)},
    )


# ── scores ────────────────────────────────────────────────────────────────────


async def scores_by_trace(project_id: str, trace_ids: list[str]) -> dict[str, list[dict]]:
    if not trace_ids:
        return {}
    rows = await aquery(
        f"SELECT trace_id, {_SCORE_COLS} FROM scores WHERE project_id = :p "
        f"AND trace_id = ANY(:t) AND {_ONLINE} ORDER BY evaluation_level, name",
        {"p": project_id, "t": list(trace_ids)},
    )
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for d in rows:
        by_trace[d.pop("trace_id")].append(d)
    return by_trace


async def conversation_scores_by_thread(
    project_id: str, thread_ids: list[str]
) -> dict[str, list[dict]]:
    if not thread_ids:
        return {}
    rows = await aquery(
        f"SELECT session_id, {_SCORE_COLS} FROM scores WHERE project_id = :p AND {_ONLINE} "
        "AND evaluation_level = 'CONVERSATION' AND session_id = ANY(:t) ORDER BY name",
        {"p": project_id, "t": list(thread_ids)},
    )
    by_thread: dict[str, list[dict]] = defaultdict(list)
    for d in rows:
        by_thread[d.pop("session_id")].append(d)
    return by_thread


async def conversation_scores(project_id: str, thread_id: str) -> list[dict]:
    return (await conversation_scores_by_thread(project_id, [thread_id])).get(thread_id, [])


async def agent_trace_ids(project_id: str, agent_id: str, limit: int = 2000) -> list[dict]:
    params: dict = {"p": project_id, "n": max(1, limit)}
    agent = ""
    if agent_id:
        agent = "AND agent_id = :a"
        params["a"] = agent_id
    rows = await aquery_rows(
        f"SELECT trace_id, thread_id FROM traces WHERE project_id = :p AND {_REAL} {agent} "
        "ORDER BY ts_min DESC LIMIT :n",
        params,
    )
    return [{"trace_id": r[0], "thread": r[1]} for r in rows]


async def trace_agent_ids(project_id: str) -> set[str]:
    """Agents DECLARED as a turn owner (the app-root span's agent) — see the ClickHouse twin."""
    rows = await aquery_rows(
        f"SELECT DISTINCT app_root_agent_id FROM traces WHERE project_id = :p AND {_REAL} "
        "AND app_root_agent_id <> ''",
        {"p": project_id},
    )
    return {r[0] for r in rows}


async def agent_ids_with_spans(project_id: str) -> set[str]:
    rows = await aquery_rows(
        "SELECT DISTINCT agent_id FROM events WHERE project_id = :p AND agent_id <> ''",
        {"p": project_id},
    )
    return {r[0] for r in rows}


async def _trace_ops_rows(project_id: str, trace_ids: list[str]) -> list[dict]:
    rows = await aquery_rows(
        f"SELECT trace_id, latency_ms, tokens, tool_errors FROM traces WHERE project_id = :p "
        f"AND {_REAL} AND trace_id = ANY(:ids)",
        {"p": project_id, "ids": list(trace_ids)},
    )
    return [
        {
            "trace_id": r[0],
            "ops.latency_ms": float(r[1]),
            "ops.tokens": float(r[2]),
            "ops.tool_errors": float(r[3]),
        }
        for r in rows
    ]


async def agent_score_rows(project_id: str, agent_id: str, max_traces: int = 2000) -> list[dict]:
    """Flat online-eval + operational rows for meta-analysis (composition only — see the
    ClickHouse twin)."""
    traces = await agent_trace_ids(project_id, agent_id, max_traces)
    if not traces:
        return []
    thread_of = {t["trace_id"]: t["thread"] for t in traces}
    trace_ids = list(thread_of)
    threads = list(dict.fromkeys(thread_of.values()))
    by_trace = await scores_by_trace(project_id, trace_ids)
    conv = await conversation_scores_by_thread(project_id, threads)

    def row(thread: str, tid: str | None, sc: dict) -> dict:
        return {
            "conversation_id": thread,
            "trace_id": tid,
            "metric_name": sc["name"],
            "evaluation_level": sc["evaluation_level"],
            "value": sc["value"],
            "string_value": sc.get("string_value", ""),
            "verdict": sc.get("verdict", ""),
        }

    rows = [row(thread_of.get(tid, tid), tid, sc) for tid, scs in by_trace.items() for sc in scs]
    rows += [row(thread, None, sc) for thread, scs in conv.items() for sc in scs]

    def ops_row(thread: str, tid: str | None, metric: str, value: float) -> dict:
        return {
            "conversation_id": thread,
            "trace_id": tid,
            "metric_name": metric,
            "evaluation_level": "OPS",
            "value": value,
            "string_value": "",
            "verdict": "",
        }

    for o in await _trace_ops_rows(project_id, trace_ids):
        thread = thread_of.get(o["trace_id"], o["trace_id"])
        for metric in ("ops.latency_ms", "ops.tokens", "ops.tool_errors"):
            rows.append(ops_row(thread, o["trace_id"], metric, o[metric]))
    turns: dict[str, int] = {}
    for thread in thread_of.values():
        turns[thread] = turns.get(thread, 0) + 1
    for thread, n in turns.items():
        rows.append(ops_row(thread, None, "ops.turns", float(n)))
    return rows


# ── search / stats / trends ───────────────────────────────────────────────────


async def search_threads(project_id: str, q: str, limit: int = 8) -> list[dict]:
    """Threads with a turn whose root (user) message contains `q`, newest first."""
    rows = await aquery_rows(
        "SELECT thread_id, (array_agg(root_input ORDER BY ts_min))[1], "
        "(array_agg(trace_id ORDER BY ts_max DESC))[1], count(*), max(ts_max) AS last_ts "
        f"FROM traces WHERE project_id = :p AND {_REAL} AND thread_id IN ("
        f"  SELECT thread_id FROM traces WHERE project_id = :p AND {_REAL} AND root_input ILIKE :q) "
        "GROUP BY thread_id ORDER BY last_ts DESC LIMIT :n",
        {"p": project_id, "q": like(q), "n": limit},
    )
    return [
        {"thread": th, "first_input": fi, "last_trace": lt, "turns": _int(n)}
        for th, fi, lt, n, _ in rows
    ]


async def stats_counts(project_id: str, advisory: Sequence[str] = ()) -> dict:
    r = await aquery_rows(
        "SELECT count(*), coalesce(sum(spans), 0)::bigint, count(*) FILTER (WHERE has_error) "
        f"FROM traces WHERE project_id = :p AND {_REAL}",
        {"p": project_id},
    )
    af = await aquery_rows(
        f"SELECT count(DISTINCT trace_id) FROM ({_FAILING}) f",
        {"p": project_id, "adv": list(advisory)},
    )
    traces, spans, failing = (_int(r[0][0]), _int(r[0][1]), _int(r[0][2])) if r else (0, 0, 0)
    return {
        "traces": traces,
        "spans": spans,
        "failing_traces": failing,
        "auto_failures": _int(af[0][0]) if af else 0,
    }


def _pct(q: float, col: str, cond: str = "") -> str:
    f = f" FILTER (WHERE {cond})" if cond else ""
    return f"floor(percentile_cont({q}) WITHIN GROUP (ORDER BY {col}){f})::bigint"


async def ops_metrics(project_id: str, days: int) -> dict:
    """Latency / throughput / cost over the last `days`: per day (from the rollup), per model and
    by slowest span name (span-level). `GROUP BY ROLLUP` appends the grand total in the same scan,
    keyed like ClickHouse's WITH ROLLUP (`1970-01-01` / `''`) so the shaping below is unchanged."""
    params = {"p": project_id, "d": days}
    daily_rows = await aquery_rows(
        "SELECT coalesce(d, DATE '1970-01-01') AS d, traces, p50, p95, p99, errors, tokens, cost FROM ("
        f"  SELECT {_UTC_DAY} AS d, count(*) AS traces, {_pct(0.5, 'latency_ms')} AS p50, "
        f"  {_pct(0.95, 'latency_ms')} AS p95, {_pct(0.99, 'latency_ms')} AS p99, "
        "  count(*) FILTER (WHERE has_error) AS errors, sum(tokens)::bigint AS tokens, "
        "  sum(cost)::float8 AS cost "
        f"  FROM traces WHERE project_id = :p AND ts_min >= {_days_ago()} AND {_REAL} "
        f"  GROUP BY ROLLUP ({_UTC_DAY})) x ORDER BY d",
        params,
    )
    model_rows = await aquery_rows(
        "SELECT coalesce(model_id, ''), calls, p50, p95, ttft_p50, errors, tokens, in_tokens, out_tokens, cost FROM ("
        f"  SELECT model_id, count(*) AS calls, {_pct(0.5, 'ms')} AS p50, {_pct(0.95, 'ms')} AS p95, "
        f"  coalesce({_pct(0.5, 'ttft', 'ttft > 0')}, 0) AS ttft_p50, "
        "  count(*) FILTER (WHERE level = 'ERROR') AS errors, sum(tokens)::bigint AS tokens, "
        "  sum(input_tokens)::bigint AS in_tokens, sum(output_tokens)::bigint AS out_tokens, "
        "  sum(cost)::float8 AS cost "
        f"  FROM (SELECT model_id, level, {_SPAN_MS} AS ms, "
        f"        {_ms('start_time', 'completion_start_time')} AS ttft, tokens, input_tokens, "
        "        output_tokens, cost FROM events WHERE project_id = :p AND model_id <> '' "
        f"        AND {_REAL} AND start_time >= {_days_ago()}) s "
        "  GROUP BY ROLLUP (model_id)) x ORDER BY calls DESC",
        params,
    )
    slow_rows = await aquery_rows(
        f"SELECT name, type, count(*) AS calls, {_pct(0.5, 'ms')} AS p50, {_pct(0.95, 'ms')} AS p95, "
        "max(ms) AS p_max, count(*) FILTER (WHERE level = 'ERROR') AS errors "
        f"FROM (SELECT name, type, level, {_SPAN_MS} AS ms FROM events WHERE project_id = :p "
        f"      AND {_REAL} AND start_time >= {_days_ago()}) s "
        "GROUP BY name, type ORDER BY p95 DESC LIMIT 8",
        params,
    )

    def _lat(r: Sequence) -> dict:
        return {
            "traces": _int(r[1]),
            "p50": _int(r[2]),
            "p95": _int(r[3]),
            "p99": _int(r[4]),
            "errors": _int(r[5]),
            "tokens": _int(r[6]),
            "cost": float(r[7] or 0),
        }

    total = next((_lat(r) for r in daily_rows if str(r[0]) == "1970-01-01"), _lat([0] * 8))
    daily = [{"date": str(r[0]), **_lat(r)} for r in daily_rows if str(r[0]) != "1970-01-01"]

    def _model(r: Sequence) -> dict:
        return {
            "model": r[0],
            "calls": _int(r[1]),
            "p50": _int(r[2]),
            "p95": _int(r[3]),
            "ttft_p50": _int(r[4]),
            "errors": _int(r[5]),
            "tokens": _int(r[6]),
            "input_tokens": _int(r[7]),
            "output_tokens": _int(r[8]),
            "cost": float(r[9] or 0),
        }

    models = [_model(r) for r in model_rows if r[0]]
    gen_total = next((_model(r) for r in model_rows if not r[0]), _model([""] + [0] * 9))
    return {
        "days": days,
        "summary": {
            **total,
            "error_rate": round(total["errors"] / total["traces"], 4) if total["traces"] else 0.0,
            "traces_per_day": round(total["traces"] / days, 1),
            "ttft_p50": gen_total["ttft_p50"],
            "llm_calls": gen_total["calls"],
            "cost_per_1k_traces": round(total["cost"] / total["traces"] * 1000, 2)
            if total["traces"]
            else 0.0,
        },
        "daily": daily,
        "models": models[:8],
        "slowest": [
            {
                "name": r[0],
                "type": r[1],
                "calls": _int(r[2]),
                "p50": _int(r[3]),
                "p95": _int(r[4]),
                "max": _int(r[5]),
                "errors": _int(r[6]),
            }
            for r in slow_rows
        ],
    }


async def daily_trace_failures(
    project_id: str, days: int, advisory: Sequence[str] = ()
) -> list[dict]:
    rows = await aquery_rows(
        f"SELECT {_UTC_DAY} AS d, count(*), count(*) FILTER (WHERE trace_id IN ({_FAILING})) "
        f"FROM traces WHERE project_id = :p AND ts_min >= {_days_ago()} AND {_REAL} "
        "GROUP BY d ORDER BY d",
        {"p": project_id, "d": days, "adv": list(advisory)},
    )
    return [{"date": str(d), "traces": _int(t), "failures": _int(f)} for d, t, f in rows]


async def trace_failure_totals(project_id: str, advisory: Sequence[str] = ()) -> tuple[int, int]:
    total = await aquery_rows(
        f"SELECT count(*) FROM traces WHERE project_id = :p AND {_REAL}", {"p": project_id}
    )
    fails = await aquery_rows(
        f"SELECT count(DISTINCT trace_id) FROM ({_FAILING}) f",
        {"p": project_id, "adv": list(advisory)},
    )
    return (_int(total[0][0]) if total else 0), (_int(fails[0][0]) if fails else 0)


# ── ops ───────────────────────────────────────────────────────────────────────


async def store_health() -> tuple[object, float | None]:
    """`(newest span time, disk free %)` for the self-check. Postgres cannot see its filesystem
    from SQL, so the disk half is None here (the verdict is skipped, as when the store is down)."""
    rows = await aquery_rows("SELECT max(ts_max) FROM traces")
    return (rows[0][0] if rows else None), None
