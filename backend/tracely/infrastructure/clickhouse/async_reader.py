"""Async read access to ClickHouse for the API layer.

EVERY ClickHouse query the routers need lives here — routers parse the request, await these,
and shape the HTTP response; they never embed SQL. This is the async twin of
`trace_reader.TraceReader` (the sync reader used by Celery workers and the
regression/gate/failure-intel services). One place owns the column lists and parameter
shapes, so a schema change touches one file.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from tracely.domain.traces.metadata import parse_thread_meta
from tracely.domain.traces.spans import system_prompt
from tracely.infrastructure.clickhouse.client import get_async_client
from tracely.infrastructure.clickhouse.trace_reader import _SPAN_COLS

# Online-eval score filter: auto/on-demand evaluator results only (regression/gate verdict
# rows carry an evaluation_case_id and are excluded everywhere in the UI reads).
_ONLINE = "source = 'EVAL' AND evaluation_case_id = ''"

# A trace counts as failing iff it has a FAIL on a NON-advisory evaluator. "Advisory" evaluators
# (the subjective answer-quality judge, etc.) are excluded via the `{adv:Array(String)}` bind param —
# the SQL twin of `domain.evaluation.verdict`. Every consumer binds `adv` (the project's advisory
# score-names from `repositories.advisory_score_names`); an empty array excludes nothing
# (`x NOT IN []` is always true in ClickHouse). This replaced the hardcoded `name != 'tracely.run.quality'`
# magic string, which special-cased one judge and was applied inconsistently across the read paths.
_FAILING = (
    "SELECT trace_id FROM scores FINAL WHERE project_id = {p:String} "
    f"AND {_ONLINE} AND verdict = 'FAIL' "
    "AND name NOT IN {adv:Array(String)}"
)

_SCORE_COLS = (
    "name, evaluation_level, observation_id, value, string_value, verdict, comment, data_type"
)

# Recordings of Tracely's own work (an evaluation, a scenario run — see `domain/introspection.py`)
# live in `events` like any trace, but they are ABOUT the product rather than produced by the
# customer's agent. Every list, count and metric excludes them, or a project's trace count would
# double the day evaluators were switched on. Two deliberate exceptions: fetch-by-id readers (so a
# recording opens like any trace) and `sessions_overview(include_internal=True)`, which is what the
# traces list's "Evals" toggle asks for.
_REAL = "internal_kind = ''"

# A TRACE's agent — the SQL twin of `domain.traces.spans.root_span`, and what the traces list's
# conversation-level Agent column, the per-agent reads below and every agent-scoped feature key off.
# The `is_app_root` half is load-bearing, not tidiness: a run Tracely drove through `traceparent`
# (a scenario, `simulate`, any customer honouring the header) has NO parent-less span, so keying on
# `parent_span_id = ''` alone left those traces with a BLANK agent — invisible in the Agent column
# and skipped by every per-agent read, while Python's `root_span` happily found one. Same rule both
# sides or the two disagree about which agent a conversation belongs to.
# Assumes a GROUP BY over one trace's spans.
# Ordered, not OR'd: `is_app_root` is also set by a *marked* span (`tracely.is_app_root`,
# langfuse's `as_root`), which a framework can stamp on a child. OR-ing the two would leave
# `anyIf` free to pick that child's agent over the real root's — a trace filed under its
# sub-agent, non-deterministically.
_TRACE_AGENT = (
    "if(anyIf(agent_id, parent_span_id = '') != '', anyIf(agent_id, parent_span_id = ''), "
    "if(anyIf(agent_id, is_app_root) != '', anyIf(agent_id, is_app_root), "
    "anyIf(agent_id, agent_id != '')))"
)


# ── traces ────────────────────────────────────────────────────────────────────


async def traces_overview(project_id: str, limit: int, advisory: Sequence[str] = ()) -> list[dict]:
    """Newest traces with span counts + the per-trace online-eval verdict (advisory FAILs excluded)."""
    client = await get_async_client()
    res = await client.query(
        f"""
        SELECT trace_id,
               min(start_time)                       AS ts,
               count()                               AS spans,
               -- The parentless span names the trace, but there isn't always one: if the app's
               -- spans hang off a parent that was never exported to us (another instrumentation's
               -- server span, say), every span here has a parent and this went empty — which the
               -- dashboard then rendered as the literal word "trace" on every row. Fall back to
               -- the earliest span, which is the closest thing to a root we hold.
               if(anyIf(name, parent_span_id = '') != '',
                  anyIf(name, parent_span_id = ''),
                  argMin(name, start_time))          AS root_name,
               {_TRACE_AGENT}                        AS agent_id,
               maxIf(1, level = 'ERROR')             AS has_error
        -- FINAL, like every other read: without it a span re-delivered by a retrying exporter is
        -- still two unmerged rows here, and `count()` reports a span total nothing else agrees with.
        FROM events FINAL
        WHERE project_id = {{p:String}} AND internal_kind = ''
        GROUP BY trace_id
        ORDER BY ts DESC
        LIMIT {{n:UInt32}}
        """,
        parameters={"p": project_id, "n": max(limit, 0)},
    )
    rows = [dict(zip(res.column_names, row)) for row in res.result_rows]
    ev = await client.query(
        "SELECT trace_id, maxIf(1, verdict = 'FAIL' AND name NOT IN {adv:Array(String)}) AS fail "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} GROUP BY trace_id",
        parameters={"p": project_id, "adv": list(advisory)},
    )
    verdict = {r[0]: ("FAIL" if r[1] else "PASS") for r in ev.result_rows}
    for r in rows:
        r["eval"] = verdict.get(r["trace_id"])
    return rows


async def trace_spans(project_id: str, trace_id: str) -> list[dict]:
    """One trace's spans (ordered), as raw dicts with derived tokens/cost per span."""
    client = await get_async_client()
    res = await client.query(
        """
        SELECT span_id, parent_span_id, name, type, level, status_message,
               start_time, end_time, completion_start_time,
               agent_id, agent_run_id, turn_id, step_name,
               model_id, input, output, metadata, conversation_id,
               toUInt64(arraySum(mapValues(usage_details)))               AS tokens,
               toFloat64(arraySum(mapValues(cost_details)))               AS cost
        FROM events FINAL
        WHERE project_id = {p:String} AND trace_id = {t:String}
        ORDER BY start_time
        """,
        parameters={"p": project_id, "t": trace_id},
    )
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


async def thread_spans_full(project_id: str, thread_id: str) -> list[dict]:
    """All spans across a thread with the SAME columns as the sync eval reader (`_SPAN_COLS`) — so
    the advanced-template PREVIEW resolves against data identical to what the run path grades. The
    `trace_spans` / sessions UI readers select a lighter, divergent set (no `tool_calls`, no
    `is_app_root`); do NOT reuse those here or the preview would lie about production."""
    client = await get_async_client()
    res = await client.query(
        f"SELECT {', '.join(_SPAN_COLS)} FROM events FINAL "
        "WHERE project_id = {p:String} "
        "AND (conversation_id = {th:String} OR trace_id = {th:String}) "
        "ORDER BY start_time",
        parameters={"p": project_id, "th": thread_id},
    )
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


async def thread_turn_count(project_id: str, thread_id: str) -> int:
    """How many turns (traces) a conversation holds — the denominator for chain progress.
    `_REAL` keeps Tracely's own eval/sim recordings out of the count, same as every listing."""
    client = await get_async_client()
    res = await client.query(
        "SELECT count(DISTINCT trace_id) FROM events FINAL "
        "WHERE project_id = {p:String} "
        "AND (conversation_id = {th:String} OR trace_id = {th:String}) "
        f"AND {_REAL}",
        parameters={"p": project_id, "th": thread_id},
    )
    return int(res.result_rows[0][0]) if res.result_rows else 0


async def trace_scores(project_id: str, trace_id: str, thread_id: str) -> list[dict]:
    """Online scores for one trace PLUS its thread's CONVERSATION-level scores (so the
    conversation metric columns render on the trace page)."""
    client = await get_async_client()
    res = await client.query(
        f"SELECT {_SCORE_COLS} "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} "
        "AND (trace_id = {t:String} OR (evaluation_level = 'CONVERSATION' AND session_id = {th:String})) "
        "ORDER BY evaluation_level, name",
        parameters={"p": project_id, "t": trace_id, "th": thread_id},
    )
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


async def evaluator_cost(project_id: str, days: int = 30) -> dict[str, dict]:
    """Per-evaluator LLM-judge token usage over the last `days` (from `scores.metadata`), keyed by
    `score_name` — the cost of each judge column. Structural checks make no LLM call so they don't
    appear. Shape: `{<score_name>: {runs, input_tokens, output_tokens, total_tokens, model}}`."""
    client = await get_async_client()
    res = await client.query(
        "SELECT name, "
        "countIf(mapContains(metadata, 'eval.total_tokens')) AS runs, "
        "sum(toUInt64OrZero(metadata['eval.input_tokens'])) AS input_tokens, "
        "sum(toUInt64OrZero(metadata['eval.output_tokens'])) AS output_tokens, "
        "sum(toUInt64OrZero(metadata['eval.total_tokens'])) AS total_tokens, "
        "anyLast(metadata['eval.model']) AS model "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} "
        "AND created_at >= now() - toIntervalDay({d:UInt32}) "
        "GROUP BY name HAVING runs > 0",
        parameters={"p": project_id, "d": days},
    )
    return {
        r[0]: {
            "runs": int(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
            "total_tokens": int(r[4]),
            "model": r[5] or "",
        }
        for r in res.result_rows
    }


async def traces_in_window(project_id: str, days: int) -> int:
    """Distinct production trace count over the last `days` — the denominator for $/1k-traces
    math on the cost view (so judge spend is normalized to traffic, not just an absolute total
    that grows with the demo seed). Production-only: env='ci' replay traces don't dilute the
    customer-facing rate."""
    client = await get_async_client()
    res = await client.query(
        "SELECT countDistinct(trace_id) FROM events FINAL "
        "WHERE project_id = {p:String} "
        "AND start_time >= now() - toIntervalDay({d:UInt32}) "
        f"AND env != 'ci' AND {_REAL}",
        parameters={"p": project_id, "d": days},
    )
    rows = res.result_rows
    return int(rows[0][0]) if rows else 0


# ── monitors: score & trace samples over a time window ───────────────────────


async def score_samples_in_window(
    project_id: str,
    score_name: str,
    window_minutes: int,
    target_agent: str = "",
) -> list[dict]:
    """Samples (`{verdict, value}` dicts) for one evaluator over the last `window_minutes`,
    optionally scoped to `target_agent` (matched on `agent_id`). Used by the monitoring engine —
    `domain.monitoring.conditions.evaluate_condition` consumes these.

    Trace-scoped scores carry an `agent_run_id` we can match against — conversation scores don't
    (they're keyed by thread). When `target_agent` is set and the score is conversation-level the
    join would be ambiguous, so we restrict to trace-scoped rows. Empty `target_agent` matches
    everything across the project."""
    client = await get_async_client()
    if target_agent:
        # Join scores → events to filter by agent_id. Cheap because both are partitioned by month
        # and the window is short; we only pull rows in `target_agent`'s production traces.
        sql = (
            "SELECT s.verdict AS verdict, s.value AS value "
            "FROM scores AS s FINAL "
            "INNER JOIN ("
            "  SELECT trace_id FROM events FINAL WHERE project_id = {p:String} "
            f"  AND agent_id = {{ag:String}} AND env != 'ci' AND {_REAL} "
            "  AND start_time >= now() - toIntervalMinute({w:UInt32}) "
            "  GROUP BY trace_id"
            ") AS e USING trace_id "
            f"WHERE s.project_id = {{p:String}} AND s.{_ONLINE} "
            "AND s.name = {n:String} "
            "AND s.created_at >= now() - toIntervalMinute({w:UInt32})"
        )
        params = {"p": project_id, "n": score_name, "w": window_minutes, "ag": target_agent}
    else:
        sql = (
            "SELECT verdict, value FROM scores FINAL "
            f"WHERE project_id = {{p:String}} AND {_ONLINE} "
            "AND name = {n:String} "
            "AND created_at >= now() - toIntervalMinute({w:UInt32})"
        )
        params = {"p": project_id, "n": score_name, "w": window_minutes}
    res = await client.query(sql, parameters=params)
    return [{"verdict": r[0] or "", "value": r[1]} for r in res.result_rows]


async def trace_failure_samples_in_window(
    project_id: str,
    window_minutes: int,
    advisory: Sequence[str],
    target_agent: str = "",
) -> list[dict]:
    """One sample per production trace in the window (`{verdict: 'FAIL'|'PASS'}`), using the same
    advisory-aware failing-trace definition as the trends page. Used by `trace_failure_rate`.

    Trace-scoped reads only: we collect each trace's `(agent_id, has_failing_non_advisory_score)`
    over its spans + scores. Empty `target_agent` matches everything."""
    client = await get_async_client()
    sql = (
        "WITH trace_meta AS ( "
        f"  SELECT trace_id, {_TRACE_AGENT} AS root_agent "
        "  FROM events FINAL "
        # `_REAL` is load-bearing here, not tidiness. Tracely records its own evaluations as
        # traces, at `env='prod'`, roughly ONE PER GRADED TRACE. Without this they land in the
        # denominator carrying no scores — so they all read PASS, the sample count doubles and the
        # measured failure rate halves. A monitor set to fire at 20% would need 40% real failures.
        f"  WHERE project_id = {{p:String}} AND env != 'ci' AND {_REAL} "
        "  AND start_time >= now() - toIntervalMinute({w:UInt32}) "
        "  GROUP BY trace_id"
        "), trace_verdict AS ( "
        "  SELECT trace_id, maxIf(1, verdict = 'FAIL' AND name NOT IN {adv:Array(String)}) AS fail "
        f"  FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} "
        "  GROUP BY trace_id"
        ") "
        "SELECT IF(coalesce(v.fail, 0) = 1, 'FAIL', 'PASS') AS verdict "
        "FROM trace_meta AS m LEFT JOIN trace_verdict AS v USING trace_id "
        + ("WHERE m.root_agent = {ag:String}" if target_agent else "")
    )
    params: dict[str, object] = {
        "p": project_id,
        "w": window_minutes,
        "adv": list(advisory),
    }
    if target_agent:
        params["ag"] = target_agent
    res = await client.query(sql, parameters=params)
    return [{"verdict": r[0]} for r in res.result_rows]


# A score is only reviewable while the run behind it is still readable. Calibration asks a human to
# second-guess a verdict, and the evidence panel needs the spans to show what was judged — a score
# whose trace was deleted by hand or aged out by the events TTL renders as an empty card with
# nothing to grade. Nothing cascades scores when a trace goes (separate tables, no FK), so they pile
# up: 40% of one workspace's queue was unreviewable. `internal_kind = ''` keeps Tracely's own eval /
# sim runs out for the same reason every other listing does.
#
# ponytail: a subquery per call, not a join or a materialized flag. `events` is already the smaller
# side after the project filter; revisit if the queue ever feels slow. FINAL is not strictly needed
# for an existence test (an unmerged duplicate span is still the same trace present), but the read
# rule holds without exceptions here — see `tests/test_ch_read_invariants.py`.
_REVIEWABLE = (
    "trace_id IN (SELECT trace_id FROM events FINAL "
    "WHERE project_id = {p:String} AND internal_kind = '')"
)


async def evaluator_catalog(project_id: str) -> list[dict]:
    """Every evaluator that has produced a verdict-bearing online score, with its volume + fail count
    — the set of judges a reviewer can calibrate (independent of whether any have been labeled yet)."""
    client = await get_async_client()
    res = await client.query(
        "SELECT name, anyLast(evaluation_level) AS level, count() AS total, "
        "countIf(verdict = 'FAIL') AS fails "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} AND verdict != '' "
        # Same filters as the queue below, or the header lies: "1 / 50" measured against rows the
        # reviewer will never be shown is a denominator nobody can ever finish.
        f"AND is_deleted = 0 AND {_REVIEWABLE} "
        "GROUP BY name ORDER BY total DESC",
        parameters={"p": project_id},
    )
    return [
        {"name": r[0], "level": r[1] or "", "total": int(r[2]), "fails": int(r[3])}
        for r in res.result_rows
    ]


async def evaluator_score_queue(
    project_id: str, name: str, limit: int = 25, offset: int = 0, verdict: str = ""
) -> list[dict]:
    """One page of verdict-bearing online scores for one evaluator — the labeling queue. Each row
    is a judge decision (its target identity + verdict + rationale comment) a reviewer labels.
    Optionally narrowed to one verdict (`FAIL` — the ones that block a merge).

    Only scores whose trace still exists are served (`_REVIEWABLE`): the reviewer is being asked to
    judge a verdict against its evidence, and a deleted or TTL-expired trace has none.

    Ordered by a hash of the score id, i.e. a *random sample*, not newest-first. Two reasons: an
    agreement % is only an estimate of the judge's accuracy if the labeled rows are a random draw,
    and newest-first serves the reviewer a screen of consecutive runs from the same batch — twenty
    near-identical PASSes teach nobody anything. The hash is deterministic, so the sample is stable
    across pages (a random ORDER BY would repeat some rows on page 2 and drop others)."""
    client = await get_async_client()
    res = await client.query(
        "SELECT trace_id, observation_id, session_id, evaluation_level, verdict, value, comment, "
        "toString(created_at) AS created_at "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} "
        "AND name = {n:String} AND verdict != '' "
        # `is_deleted` matters even under FINAL: it collapses versions of a row, it does not drop
        # tombstoned ones — without this a deleted score is still served for review.
        f"AND is_deleted = 0 AND {_REVIEWABLE} "
        "AND ({v:String} = '' OR verdict = {v:String}) "
        "ORDER BY cityHash64(id) LIMIT {lim:UInt32} OFFSET {off:UInt32}",
        parameters={
            "p": project_id, "n": name, "lim": max(limit, 0), "off": max(offset, 0),
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
        for r in res.result_rows
    ]


# ── sessions / threads ────────────────────────────────────────────────────────

# Sortable list columns → the aggregate each one displays. A whitelist, not a free-form column:
# ORDER BY cannot be parameterized, so anything else here would be string-interpolated SQL.
# Keys are the UI's sort ids; every expression is an alias from the outer SELECT below.
SESSION_SORTS = {
    "recent": "last_ts",  # default — last activity, what the list has always shown
    "started": "first_ts",  # the Datetime column renders first_ts, so its header sorts on that
    "duration": "dateDiff('millisecond', first_ts, last_ts)",
    "tokens": "tokens",
}


def session_order_clause(sort: str, order: str) -> str:
    """ORDER BY for the threads list. Unknown keys fall back to the default rather than raising:
    a sort is a view preference, and 400-ing a stale link is worse than showing the usual order.

    The tie-breaks are load-bearing, not cosmetic. Ties are the common case here (duration 0,
    tokens 0), and LIMIT/OFFSET over a non-deterministic order silently repeats some threads on
    page 2 while dropping others entirely.

    `thread` is the one that makes the order TOTAL, and it is why this ends in a unique column
    rather than `last_ts`: under the default `recent` sort the expression IS `last_ts`, so
    "… last_ts DESC, last_ts DESC" tie-broke nothing and paging quietly lost conversations — which
    a whole-workspace export makes obvious and the UI's "Load more" does not."""
    expr = SESSION_SORTS.get(sort, SESSION_SORTS["recent"])
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    return f"ORDER BY {expr} {direction}, last_ts DESC, thread ASC"


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
) -> list[dict]:
    """Traces grouped into threads by conversation (a trace with no conversation is its own
    1-turn thread), newest-last-activity first, with per-thread rollups + parsed metadata.

    Each row carries `agent_id`: the registry agent of the thread's latest trace (its ROOT span's,
    the same attribution failure intel, regression and the gate use). `agent_id` as a filter keeps
    only threads whose traces belong to that agent — the traces list's Agent select.

    `include_internal` adds Tracely's own runs — an evaluation, a scenario — as ordinary rows,
    tagged with `internal_kind` so the table can mark them. Off by default: they are about the
    product, not the customer's agent, and unasked-for they double a project's trace list.
    The optional time window bounds each trace's start_time INSIDE the per-trace subquery so
    ClickHouse can prune by the `toYYYYMM(start_time)` partition. `advisory` excludes those
    evaluators' FAILs from the per-thread `failing` flag (see `_FAILING`). Content-less 1-turn
    threads (no input, no output, not failing) are dropped entirely — see the HAVING clause.

    `sort`/`order` reorder the whole list, not the page: the table's sortable headers have to see
    past the loaded window, or "sort by slowest" would only ever surface the slowest of the 50 rows
    already on screen."""
    client = await get_async_client()
    order_clause = session_order_clause(sort, order)
    time_clause = ""
    internal_clause = "" if include_internal else f" AND {_REAL}"
    params: dict = {"p": project_id, "n": max(limit, 0), "o": max(offset, 0), "adv": list(advisory)}
    if from_ts:
        time_clause += " AND start_time >= parseDateTimeBestEffort({from:String})"
        params["from"] = from_ts
    if to_ts:
        time_clause += " AND start_time < parseDateTimeBestEffort({to:String})"
        params["to"] = to_ts
    agent_clause = ""
    if agent_id:
        agent_clause = "HAVING t_agent = {ag:String}"
        params["ag"] = agent_id
    res = await client.query(
        f"""
        SELECT
          if(conv != '', conv, trace_id)        AS thread,
          count()                               AS turns,
          argMax(t_agent, ts_max)               AS agent_id,
          -- An internal run is titled by its root span ("eval · 5 evaluator(s)"), not by the
          -- first GENERATION input — which for a recording is the judge's raw system prompt and
          -- reads as noise in the list. Its "answer" is what it was about.
          if(max(t_internal) != '', max(t_root_name), argMin(t_input, ts_min))  AS first_input,
          if(max(t_internal) != '', max(t_subject), argMax(t_output, ts_min))   AS last_output,
          sum(t_tokens)                         AS tokens,
          sum(t_input_tokens)                   AS input_tokens,
          sum(t_output_tokens)                  AS output_tokens,
          argMax(t_model, t_tokens)             AS model,
          sum(t_cost)                           AS cost,
          min(ts_min)                           AS first_ts,
          max(ts_max)                           AS last_ts,
          argMax(trace_id, ts_max)              AS last_trace_id,
          max(t_failing)                        AS failing,
          max(t_internal)                       AS internal_kind,
          max(t_subject)                        AS subject_id,
          toJSONString(CAST(
            (groupArrayArray(mapKeys(t_meta)), groupArrayArray(mapValues(t_meta))),
            'Map(String, String)'))             AS metadata
        FROM (
          SELECT trace_id,
            max(conversation_id)                                          AS conv,
            -- Prefer the EARLIEST GENERATION input (carries the actual user message in the chat
            -- array), fall back to the earliest non-empty input from any other span — so the
            -- conversation title isn't pinned to framework internals like CrewAI's agent-config
            -- payload or LlamaIndex's workflow-start event.
            if(argMinIf(input, start_time, input != '' AND type = 'GENERATION') != '',
               argMinIf(input, start_time, input != '' AND type = 'GENERATION'),
               argMinIf(input, start_time, input != ''))                    AS t_input,
            -- Pick the LATEST GENERATION output as the run's answer (skip TOOL results and
            -- framework CHAIN router signals like LangGraph's `__end__`). Fall back to root, then
            -- to any non-TOOL non-CHAIN span.
            if(argMaxIf(output, start_time, output != '' AND type = 'GENERATION') != '',
               argMaxIf(output, start_time, output != '' AND type = 'GENERATION'),
               if(anyIf(output, parent_span_id = '' AND output != '') != '',
                  anyIf(output, parent_span_id = '' AND output != ''),
                  argMaxIf(output, start_time, output != '' AND type NOT IN ('TOOL','CHAIN')))) AS t_output,
            toUInt64(sum(arraySum(mapValues(usage_details))))             AS t_tokens,
            toUInt64(sum(usage_details['input']))                         AS t_input_tokens,
            toUInt64(sum(usage_details['output']))                        AS t_output_tokens,
            argMaxIf(model_id, arraySum(mapValues(usage_details)),
                     type = 'GENERATION' AND model_id != '')              AS t_model,
            toFloat64(sum(arraySum(mapValues(cost_details))))             AS t_cost,
            min(start_time)                                               AS ts_min,
            max(coalesce(end_time, start_time))                           AS ts_max,
            maxIf(1, trace_id IN ({_FAILING}))                            AS t_failing,
            max(internal_kind)                                            AS t_internal,
            max(subject_id)                                               AS t_subject,
            anyIf(name, parent_span_id = '')                              AS t_root_name,
            {_TRACE_AGENT}                                                AS t_agent,
            CAST(
              (groupArrayArray(mapKeys(mapFilter((k, v) -> startsWith(k, 'tracely.metadata.'), CAST(metadata, 'Map(String, String)')))),
               groupArrayArray(mapValues(mapFilter((k, v) -> startsWith(k, 'tracely.metadata.'), CAST(metadata, 'Map(String, String)'))))),
              'Map(String, String)')                                      AS t_meta
          FROM events FINAL WHERE project_id = {{p:String}}{time_clause}{internal_clause}
          GROUP BY trace_id
          {agent_clause}
        )
        GROUP BY thread
        -- Drop 1-turn threads with no message content on either side (e.g. a lone TOOL/RETRIEVER
        -- span the output-normalizer couldn't map to text) unless an evaluator flagged it — pure
        -- ingestion noise, not a conversation worth listing.
        HAVING NOT (turns = 1 AND first_input = '' AND last_output = '' AND failing = 0)
        {order_clause}
        LIMIT {{n:UInt32}} OFFSET {{o:UInt32}}
        """,
        parameters=params,
    )
    rows = []
    for row in res.result_rows:
        d = dict(zip(res.column_names, row))
        d["metadata"] = parse_thread_meta(d.get("metadata"))
        rows.append(d)
    return rows


async def thread_agents(project_id: str, thread_id: str) -> list[dict]:
    """The agents that participated in a thread and the tools each used, DERIVED from the thread's
    spans (Tracely ingests OTLP — there is no richer agent catalog than the trace itself). Each:
    `{agent_id, tools: [{name, count}], span_count, tool_call_count, system_prompt, models}` where
    `count` is the number of executed TOOL spans for that tool (a tool only *requested* — seen in
    `tool_call_names` — shows count 0). `system_prompt` is recovered from the agent's own messages
    and is "" when its traces never sent one; `models` are the distinct `model_id`s it ran on.
    Sorted by tool activity then span volume. The router resolves friendly names."""
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
    """The turns (traces) inside one thread, oldest-first — a simple conversation replay. `advisory`
    excludes those evaluators' FAILs from each turn's `failing` flag (see `_FAILING`)."""
    client = await get_async_client()
    res = await client.query(
        f"""
        SELECT trace_id,
               -- An internal run's "message" is what it did, not the judge's raw prompt: the root
               -- span already says `eval · msg · 5 column(s)` and carries the verdict summary.
               -- Same reason the list title is derived this way (see `sessions_overview`).
               -- The inner aliases are t_* so these names don't resolve back into the aggregates.
               if(internal != '', root_in, t_input)   AS input,
               if(internal != '', root_out, t_output) AS output,
               tokens, input_tokens, output_tokens, model, cost, latency_ms, ts, failing
        FROM (
          SELECT trace_id,
            max(conversation_id)                                          AS conv,
            max(internal_kind)                                            AS internal,
            concat(anyIf(name, parent_span_id = ''), '\n\n',
                   coalesce(anyIf(input, parent_span_id = ''), ''))       AS root_in,
            coalesce(anyIf(output, parent_span_id = ''), '')              AS root_out,
            -- Prefer the EARLIEST GENERATION input (the actual user message) over framework
            -- internals (CrewAI agent-config payload, LlamaIndex workflow state, etc.).
            if(argMinIf(input, start_time, input != '' AND type = 'GENERATION') != '',
               argMinIf(input, start_time, input != '' AND type = 'GENERATION'),
               argMinIf(input, start_time, input != ''))                    AS t_input,
            -- Prefer the latest GENERATION output (skip TOOL + CHAIN router signals like
            -- LangGraph's `__end__`); fall back to root output, then any non-TOOL/non-CHAIN.
            if(argMaxIf(output, start_time, output != '' AND type = 'GENERATION') != '',
               argMaxIf(output, start_time, output != '' AND type = 'GENERATION'),
               if(anyIf(output, parent_span_id = '' AND output != '') != '',
                  anyIf(output, parent_span_id = '' AND output != ''),
                  argMaxIf(output, start_time, output != '' AND type NOT IN ('TOOL','CHAIN')))) AS t_output,
            toUInt64(sum(arraySum(mapValues(usage_details))))             AS tokens,
            toUInt64(sum(usage_details['input']))                         AS input_tokens,
            toUInt64(sum(usage_details['output']))                        AS output_tokens,
            argMaxIf(model_id, arraySum(mapValues(usage_details)),
                     type = 'GENERATION' AND model_id != '')              AS model,
            toFloat64(sum(arraySum(mapValues(cost_details))))             AS cost,
            dateDiff('millisecond', min(start_time), max(coalesce(end_time, start_time))) AS latency_ms,
            min(start_time)                                               AS ts,
            maxIf(1, trace_id IN ({_FAILING}))                            AS failing
          -- Narrow to the thread INSIDE the subquery. The outer WHERE alone made opening one
          -- conversation group every span in the project first and throw all but one thread away:
          -- O(project) per page view. A thread's traces either carry its conversation_id or ARE it
          -- (a trace with no conversation is its own 1-turn thread) — same predicate the sync
          -- reader's `read_thread_spans` uses, and the bloom filter on conversation_id covers it.
          FROM events FINAL WHERE project_id = {{p:String}}
            AND (conversation_id = {{th:String}} OR trace_id = {{th:String}})
          GROUP BY trace_id
        )
        -- Still guarded: a trace whose id happens to equal this thread's while ALSO carrying a
        -- different conversation_id belongs to that other thread, not this one.
        WHERE if(conv != '', conv, trace_id) = {{th:String}}
        ORDER BY ts ASC
        """,
        parameters={"p": project_id, "th": thread_id, "adv": list(advisory)},
    )
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


# ── scores ────────────────────────────────────────────────────────────────────


async def scores_by_trace(project_id: str, trace_ids: list[str]) -> dict[str, list[dict]]:
    """`{trace_id: [score, …]}` for the given traces (all levels, online evals only)."""
    if not trace_ids:
        return {}
    client = await get_async_client()
    res = await client.query(
        f"SELECT trace_id, {_SCORE_COLS} "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND trace_id IN {{t:Array(String)}} "
        f"AND {_ONLINE} ORDER BY evaluation_level, name",
        parameters={"p": project_id, "t": trace_ids},
    )
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for row in res.result_rows:
        d = dict(zip(res.column_names, row))
        by_trace[d.pop("trace_id")].append(d)
    return by_trace


async def conversation_scores_by_thread(
    project_id: str, thread_ids: list[str]
) -> dict[str, list[dict]]:
    """`{thread: [score, …]}` of CONVERSATION-level scores (the C-row metric columns)."""
    if not thread_ids:
        return {}
    client = await get_async_client()
    res = await client.query(
        f"SELECT session_id, {_SCORE_COLS} "
        f"FROM scores FINAL WHERE project_id = {{p:String}} AND {_ONLINE} "
        "AND evaluation_level = 'CONVERSATION' AND session_id IN {t:Array(String)} ORDER BY name",
        parameters={"p": project_id, "t": thread_ids},
    )
    by_thread: dict[str, list[dict]] = defaultdict(list)
    for row in res.result_rows:
        d = dict(zip(res.column_names, row))
        by_thread[d.pop("session_id")].append(d)
    return by_thread


async def conversation_scores(project_id: str, thread_id: str) -> list[dict]:
    """One thread's CONVERSATION-level scores."""
    return (await conversation_scores_by_thread(project_id, [thread_id])).get(thread_id, [])


async def agent_trace_ids(project_id: str, agent_id: str, limit: int = 2000) -> list[dict]:
    """`[{trace_id, thread}]` for an agent's traces (newest first, capped). A trace is attributed
    by `_TRACE_AGENT` (matching how evaluation/failure-intel attribute runs); a trace
    with a conversation belongs to that thread, otherwise it is its own 1-turn thread. `agent_id`
    blank → every trace in the project (whole-project analysis)."""
    client = await get_async_client()
    having = f"HAVING {_TRACE_AGENT} = {{a:String}}" if agent_id else ""
    params: dict = {"p": project_id, "n": max(1, limit)}
    if agent_id:
        params["a"] = agent_id
    res = await client.query(
        f"""
        SELECT trace_id,
               if(max(conversation_id) != '', max(conversation_id), trace_id) AS thread,
               min(start_time) AS ts
        FROM events FINAL
        WHERE project_id = {{p:String}} AND {_REAL}
        GROUP BY trace_id
        {having}
        ORDER BY ts DESC
        LIMIT {{n:UInt32}}
        """,
        parameters=params,
    )
    return [{"trace_id": r[0], "thread": r[1]} for r in res.result_rows]


async def trace_agent_ids(project_id: str) -> set[str]:
    """Agent ids the USER declared as a turn/conversation owner — the Scenario / CI-gate / Traces
    agent pickers.

    Deliberately NOT `_TRACE_AGENT`: its last resort is "any span's agent", which files an ORPHAN
    fragment (a tool/chain span whose turn root was never exported — real production exporters
    drop them constantly) under whichever sub-agent label that span carried. `sessions_overview`
    hides those fragments as content-less 1-turn threads, so a picker built on `_TRACE_AGENT`
    offers sub-agents (`supervisor`, `agent_faq`) that own nothing the list can even show.
    Only the app-root span — `parent_span_id = ''` or an explicit `tracely.is_app_root` — is a
    declaration of "this agent owns this turn", so that is the only thing counted here.
    """
    client = await get_async_client()
    res = await client.query(
        "SELECT DISTINCT agent FROM "
        "(SELECT anyIf(agent_id, is_app_root) AS agent FROM events FINAL "
        f"WHERE project_id = {{p:String}} AND {_REAL} GROUP BY trace_id) WHERE agent != ''",
        parameters={"p": project_id},
    )
    return {r[0] for r in res.result_rows}


async def agent_ids_with_spans(project_id: str) -> set[str]:
    """Every agent id that still has a span — the keep-list for `repositories.agents_prune`.
    Includes internal recordings on purpose: any span at all means the agent is in use."""
    client = await get_async_client()
    res = await client.query(
        "SELECT DISTINCT agent_id FROM events FINAL "
        "WHERE project_id = {p:String} AND agent_id != ''",
        parameters={"p": project_id},
    )
    return {r[0] for r in res.result_rows}


async def _trace_ops_rows(project_id: str, trace_ids: list[str]) -> list[dict]:
    """Per-TRACE operational aggregates for meta-analysis: wall-clock ms, total tokens, errored
    tool calls. One row per (trace, metric); the matrix builder averages them per conversation,
    so `ops.latency_ms` reads as "average turn latency of this conversation"."""
    client = await get_async_client()
    res = await client.query(
        f"""
        SELECT trace_id,
               dateDiff('millisecond', min(start_time), max(coalesce(end_time, start_time))) AS ms,
               toUInt64(sum(arraySum(mapValues(usage_details))))                             AS tokens,
               countIf(type = 'TOOL' AND level = 'ERROR')                                    AS tool_errors
        FROM events FINAL
        WHERE project_id = {{p:String}} AND {_REAL} AND trace_id IN {{ids:Array(String)}}
        GROUP BY trace_id
        """,
        parameters={"p": project_id, "ids": trace_ids},
    )
    return [
        {"trace_id": r[0], "ops.latency_ms": float(r[1]), "ops.tokens": float(r[2]), "ops.tool_errors": float(r[3])}
        for r in res.result_rows
    ]


async def agent_score_rows(project_id: str, agent_id: str, max_traces: int = 2000) -> list[dict]:
    """Flat online-eval score rows for an agent, across ALL levels, for meta-analysis. Each row:
    `{conversation_id (thread), trace_id, metric_name, evaluation_level, value, string_value,
    verdict}`. Composes the trace lookup with the existing per-trace + per-thread score readers
    (so conversation-level scores — which carry no trace_id — are included via their thread).

    Also emits OPERATIONAL rows (`ops.latency_ms`, `ops.tokens`, `ops.tool_errors` per turn, and
    `ops.turns` per conversation) so the analysis can correlate quality with cost and speed —
    "low groundedness is the slow conversations" is exactly the kind of finding a score-only
    matrix cannot produce."""
    traces = await agent_trace_ids(project_id, agent_id, max_traces)
    if not traces:
        return []
    thread_of = {t["trace_id"]: t["thread"] for t in traces}
    trace_ids = list(thread_of)
    threads = list(dict.fromkeys(thread_of.values()))

    by_trace = await scores_by_trace(project_id, trace_ids)
    conv = await conversation_scores_by_thread(project_id, threads)

    rows: list[dict] = []
    for tid, scores in by_trace.items():
        thread = thread_of.get(tid, tid)
        for sc in scores:
            # scores_by_trace returns all levels for a trace; conversation-level rows have no
            # trace_id so they don't appear here — they come from conv below (deduped per thread).
            rows.append(
                {
                    "conversation_id": thread,
                    "trace_id": tid,
                    "metric_name": sc["name"],
                    "evaluation_level": sc["evaluation_level"],
                    "value": sc["value"],
                    "string_value": sc.get("string_value", ""),
                    "verdict": sc.get("verdict", ""),
                }
            )
    for thread, scores in conv.items():
        for sc in scores:
            rows.append(
                {
                    "conversation_id": thread,
                    "trace_id": None,
                    "metric_name": sc["name"],
                    "evaluation_level": sc["evaluation_level"],
                    "value": sc["value"],
                    "string_value": sc.get("string_value", ""),
                    "verdict": sc.get("verdict", ""),
                }
            )

    def ops_row(thread: str, tid: str | None, metric: str, value: float) -> dict:
        return {
            "conversation_id": thread, "trace_id": tid, "metric_name": metric,
            "evaluation_level": "OPS", "value": value, "string_value": "", "verdict": "",
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
    """Threads whose first user message matches `q` (case-insensitive), newest first. Reports
    the whole THREAD: first message, total turn count, latest trace."""
    client = await get_async_client()
    res = await client.query(
        """
        SELECT thread, argMin(ti, tmin) AS first_input,
               argMax(trace_id, tmax) AS last_trace, count() AS turns, max(tmax) AS last_ts
        FROM (
          SELECT trace_id,
                 if(max(conversation_id) != '', max(conversation_id), trace_id) AS thread,
                 argMinIf(input, start_time, input != '') AS ti,
                 positionCaseInsensitive(argMinIf(input, start_time, input != ''), {q:String}) > 0 AS matched,
                 min(start_time) AS tmin, max(coalesce(end_time, start_time)) AS tmax
          FROM events FINAL WHERE project_id = {p:String} AND internal_kind = '' GROUP BY trace_id
        )
        GROUP BY thread HAVING max(matched) > 0
        ORDER BY last_ts DESC LIMIT {n:UInt32}
        """,
        parameters={"p": project_id, "q": q, "n": limit},
    )
    return [
        {
            "thread": thread,
            "first_input": first_input,
            "last_trace": last_trace,
            "turns": int(turns),
        }
        for thread, first_input, last_trace, turns, _ in res.result_rows
    ]


async def stats_counts(project_id: str, advisory: Sequence[str] = ()) -> dict:
    """Headline counters for the dashboard: traces/spans, error traces, auto-eval failures
    (advisory evaluators' FAILs excluded, so `auto_failures` matches the per-trace verdict)."""
    client = await get_async_client()
    r = (
        await client.query(
            "SELECT uniqExact(trace_id), count() FROM events FINAL "
            "WHERE project_id = {p:String} AND internal_kind = ''",
            parameters={"p": project_id},
        )
    ).result_rows
    traces, spans = (int(r[0][0]), int(r[0][1])) if r else (0, 0)
    f = (
        await client.query(
            "SELECT uniqExact(trace_id) FROM events FINAL "
            "WHERE project_id = {p:String} AND level = 'ERROR' AND internal_kind = ''",
            parameters={"p": project_id},
        )
    ).result_rows
    failing = int(f[0][0]) if f else 0
    af = (
        await client.query(
            "SELECT uniqExact(trace_id) FROM scores FINAL WHERE project_id = {p:String} "
            f"AND {_ONLINE} AND verdict = 'FAIL' AND name NOT IN {{adv:Array(String)}}",
            parameters={"p": project_id, "adv": list(advisory)},
        )
    ).result_rows
    auto_failures = int(af[0][0]) if af else 0
    return {
        "traces": traces,
        "spans": spans,
        "failing_traces": failing,
        "auto_failures": auto_failures,
    }


_MS = "dateDiff('millisecond', start_time, coalesce(end_time, start_time))"


async def ops_metrics(project_id: str, days: int) -> dict:
    """Operational roll-up over the last `days` — the latency/throughput/cost view every
    observability tool shows: end-to-end trace latency p50/p95/p99, error rate, tokens and
    spend, broken out per day, per model, and by slowest span name.

    `GROUP BY … WITH ROLLUP` appends the grand-total row in the same scan (`d` = 1970-01-01,
    `model_id` = ''), so the headline numbers are real percentiles over the whole window and
    not an average of per-day percentiles — one query instead of two."""
    client = await get_async_client()

    daily_rows = (
        await client.query(
            """
            SELECT toDate(t0)                    AS d,
                   count()                       AS traces,
                   toUInt32(quantile(0.5)(ms))   AS p50,
                   toUInt32(quantile(0.95)(ms))  AS p95,
                   toUInt32(quantile(0.99)(ms))  AS p99,
                   countIf(errs > 0)             AS errors,
                   toUInt64(sum(tokens))         AS tokens,
                   toFloat64(sum(cost))          AS cost
            FROM (
                SELECT min(start_time) AS t0,
                       dateDiff('millisecond', min(start_time),
                                max(coalesce(end_time, start_time)))  AS ms,
                       countIf(level = 'ERROR')                       AS errs,
                       sum(arraySum(mapValues(usage_details)))        AS tokens,
                       sum(arraySum(mapValues(cost_details)))         AS cost
                FROM events FINAL
                WHERE project_id = {p:String} AND start_time >= subtractDays(now(), {d:UInt32}) AND internal_kind = ''
                GROUP BY trace_id
            )
            GROUP BY d WITH ROLLUP ORDER BY d
            """,
            parameters={"p": project_id, "d": days},
        )
    ).result_rows

    model_rows = (
        await client.query(
            f"""
            SELECT model_id,
                   count()                                                   AS calls,
                   toUInt32(quantile(0.5)(ms))                               AS p50,
                   toUInt32(quantile(0.95)(ms))                              AS p95,
                   toUInt32(ifNotFinite(quantileIf(0.5)(ttft, ttft > 0), 0)) AS ttft_p50,
                   countIf(level = 'ERROR')                                  AS errors,
                   toUInt64(sum(tokens))                                     AS tokens,
                   toUInt64(sum(in_tokens))                                  AS in_tokens,
                   toUInt64(sum(out_tokens))                                 AS out_tokens,
                   toFloat64(sum(cost))                                      AS cost
            FROM (
                SELECT model_id, level,
                       {_MS}                                                             AS ms,
                       dateDiff('millisecond', start_time, completion_start_time)        AS ttft,
                       arraySum(mapValues(usage_details))                                AS tokens,
                       usage_details['input']                                            AS in_tokens,
                       usage_details['output']                                           AS out_tokens,
                       arraySum(mapValues(cost_details))                                 AS cost
                FROM events FINAL
                WHERE project_id = {{p:String}} AND model_id != '' AND {_REAL}
                  AND start_time >= subtractDays(now(), {{d:UInt32}})
            )
            GROUP BY model_id WITH ROLLUP ORDER BY calls DESC
            """,
            parameters={"p": project_id, "d": days},
        )
    ).result_rows

    slow_rows = (
        await client.query(
            f"""
            SELECT name, type,
                   count()                       AS calls,
                   toUInt32(quantile(0.5)(ms))   AS p50,
                   toUInt32(quantile(0.95)(ms))  AS p95,
                   toUInt32(max(ms))             AS p_max,
                   countIf(level = 'ERROR')      AS errors
            FROM (SELECT name, type, level, {_MS} AS ms FROM events FINAL
                  WHERE project_id = {{p:String}} AND {_REAL}
                    AND start_time >= subtractDays(now(), {{d:UInt32}}))
            GROUP BY name, type ORDER BY p95 DESC LIMIT 8
            """,
            parameters={"p": project_id, "d": days},
        )
    ).result_rows

    def _lat(r: Sequence) -> dict:
        return {
            "traces": int(r[1]),
            "p50": int(r[2]),
            "p95": int(r[3]),
            "p99": int(r[4]),
            "errors": int(r[5]),
            "tokens": int(r[6]),
            "cost": float(r[7]),
        }

    total = next((_lat(r) for r in daily_rows if str(r[0]) == "1970-01-01"), _lat([0] * 8))
    daily = [{"date": str(r[0]), **_lat(r)} for r in daily_rows if str(r[0]) != "1970-01-01"]

    def _model(r: Sequence) -> dict:
        return {
            "model": r[0],
            "calls": int(r[1]),
            "p50": int(r[2]),
            "p95": int(r[3]),
            "ttft_p50": int(r[4]),
            "errors": int(r[5]),
            "tokens": int(r[6]),
            "input_tokens": int(r[7]),
            "output_tokens": int(r[8]),
            "cost": float(r[9]),
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
                "calls": int(r[2]),
                "p50": int(r[3]),
                "p95": int(r[4]),
                "max": int(r[5]),
                "errors": int(r[6]),
            }
            for r in slow_rows
        ],
    }


async def daily_trace_failures(
    project_id: str, days: int, advisory: Sequence[str] = ()
) -> list[dict]:
    """Per-day trace + failing-trace counts, both dated by the trace's own start_time (so
    failures<=traces); a trace 'failed' if it has any online EVAL FAIL on a non-advisory evaluator."""
    client = await get_async_client()
    rows = (
        await client.query(
            "SELECT toDate(start_time) AS d, uniqExact(trace_id) AS traces, "
            "uniqExactIf(trace_id, trace_id IN ("
            "  SELECT trace_id FROM scores FINAL WHERE project_id = {p:String} "
            f"  AND {_ONLINE} AND verdict = 'FAIL' AND name NOT IN {{adv:Array(String)}})) AS failures "
            "FROM events FINAL "
            "WHERE project_id = {p:String} AND start_time >= subtractDays(now(), {d:UInt32}) "
            "AND internal_kind = '' "
            "GROUP BY d ORDER BY d",
            parameters={"p": project_id, "d": days, "adv": list(advisory)},
        )
    ).result_rows
    return [{"date": str(d), "traces": int(t), "failures": int(f)} for d, t, f in rows]


async def trace_failure_totals(project_id: str, advisory: Sequence[str] = ()) -> tuple[int, int]:
    """(total traces, total traces with an online EVAL FAIL on a non-advisory evaluator)."""
    client = await get_async_client()

    async def _scalar(sql: str, extra: dict | None = None) -> int:
        r = (await client.query(sql, parameters={"p": project_id, **(extra or {})})).result_rows
        return int(r[0][0]) if r and r[0][0] is not None else 0

    total = await _scalar(
        "SELECT uniqExact(trace_id) FROM events FINAL "
        "WHERE project_id = {p:String} AND internal_kind = ''"
    )
    failures = await _scalar(
        "SELECT uniqExact(trace_id) FROM scores FINAL WHERE project_id = {p:String} "
        f"AND {_ONLINE} AND verdict = 'FAIL' AND name NOT IN {{adv:Array(String)}}",
        {"adv": list(advisory)},
    )
    return total, failures
