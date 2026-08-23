"""Read access to the ClickHouse `events` and `scores` tables.

Consolidates the SELECTs that were scattered across `regression_service` (read_trace_spans),
`gate_service` (_candidate_metrics, latest ci traces), `failure_intel_service` (failing-trace
reasons), and `api/routers/clusters.py` (_member_meta). One place owns the column lists and
parameter shapes, so a schema change touches one file.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from clickhouse_connect.driver.client import Client

from tracely.infrastructure.clickhouse.client import get_client

# Columns used by services to reconstruct a trace's spans (regression promote/replay, gate
# replay, failure intel summarization, evaluation runner). `tool_calls` (the structured
# invocation array) backs the advanced-judge `@CURRENT_STEP.tool_call` template variable.
_SPAN_COLS = [
    "span_id", "parent_span_id", "type", "name", "level", "status_message",
    # `completion_start_time` is the first-CONTENT-token mark the SDK stamps on a streamed call.
    # It splits a GENERATION span into thinking-vs-answering, which wall time alone cannot.
    "start_time", "end_time", "completion_start_time",
    "agent_id", "agent_version_id", "agent_run_id",
    "turn_id", "step_id", "model_id", "input", "output", "tool_call_names", "tool_calls",
    "trace_id", "is_app_root", "conversation_id", "env",
    # handoff edge (DELEGATE spans): raw slugs as the SDK recorded them — the replay resolves
    # them to registry agents through its alias map.
    "caller_agent_id", "callee_agent_id",
]


class TraceReader:
    """All `events` / `scores` SELECTs go through here.

    `client` is constructed lazily — the API path passes its async client elsewhere; this is the
    sync reader used by Celery workers and the regression/gate/fi orchestrators.
    """

    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = get_client()
        return self._client

    # ── span reads ────────────────────────────────────────────────────────────

    def read_spans(self, project_id: str, trace_id: str) -> list[dict]:
        """All spans for one trace, ordered by start_time, with the columns services need."""
        res = self.client.query(
            f"SELECT {', '.join(_SPAN_COLS)} FROM events FINAL "
            "WHERE project_id = {p:String} AND trace_id = {t:String} ORDER BY start_time",
            parameters={"p": project_id, "t": trace_id},
        )
        return [dict(zip(res.column_names, row)) for row in res.result_rows]

    def read_thread_spans(self, project_id: str, thread_id: str) -> list[dict]:
        """All spans across a conversation thread, ordered by start_time. A thread is every
        trace sharing `conversation_id == thread_id`; a trace with no conversation is its own
        1-turn thread (`thread_id == trace_id`) — mirror of the sessions API's grouping."""
        res = self.client.query(
            f"SELECT {', '.join(_SPAN_COLS)} FROM events FINAL "
            "WHERE project_id = {p:String} "
            "AND (conversation_id = {th:String} OR trace_id = {th:String}) "
            "ORDER BY start_time",
            parameters={"p": project_id, "th": thread_id},
        )
        return [dict(zip(res.column_names, row)) for row in res.result_rows]

    def thread_trace_ids(self, project_id: str, thread_id: str) -> list[str]:
        """The trace ids (turns) inside one thread, oldest first."""
        rows = self.client.query(
            "SELECT trace_id FROM events FINAL WHERE project_id = {p:String} "
            "AND (conversation_id = {th:String} OR trace_id = {th:String}) "
            "GROUP BY trace_id ORDER BY min(start_time)",
            parameters={"p": project_id, "th": thread_id},
        ).result_rows
        return [tid for (tid,) in rows]

    def candidate_metrics(
        self, project_id: str, trace_ids: Iterable[str]
    ) -> tuple[float, int, dict[str, tuple[float, int]]]:
        """Per-trace latency (ms) + token totals across `trace_ids`. Returns
        (total_lat, total_tok, per_trace_map). Latency/cost are exact for live runs and ~0 for
        hermetic replay (expected)."""
        uniq = sorted({t for t in trace_ids if t})
        if not uniq:
            return 0.0, 0, {}
        rows = self.client.query(
            "SELECT trace_id, "
            "dateDiff('millisecond', min(start_time), max(coalesce(end_time, start_time))) AS lat, "
            "toUInt64(sum(arraySum(mapValues(usage_details)))) AS toks "
            "FROM events FINAL WHERE project_id = {p:String} AND trace_id IN {t:Array(String)} "
            "GROUP BY trace_id",
            parameters={"p": project_id, "t": uniq},
        ).result_rows
        per = {tid: (float(lat), int(toks)) for tid, lat, toks in rows}
        return sum(v[0] for v in per.values()), sum(v[1] for v in per.values()), per

    def latest_traces_for_env(
        self, project_id: str, agent_id: str, env: str, limit: int = 300
    ) -> list[str]:
        """Recent trace_ids for `(agent_id, env)`, newest first. The gate uses this to find
        candidate traces when no explicit `tracely replay` pairing was provided.

        Two kinds of Tracely-made traces are excluded, because every simulate run emits both
        under this agent in env `ci` and on a busy project they crowd the real CI candidates out
        of the window — every case then SKIPs and the gate lands on NO_COVERAGE, worse the more
        the product is used: internal recordings (`internal_kind`), and the emulated scenario
        turns themselves (`emulated.turn` spans — those are graded as conversations, never as
        replay candidates)."""
        rows = self.client.query(
            "SELECT trace_id FROM events FINAL WHERE project_id = {p:String} AND agent_id = {a:String} "
            "AND env = {e:String} AND internal_kind = '' "
            "GROUP BY trace_id HAVING countIf(name = 'emulated.turn') = 0 "
            "ORDER BY max(start_time) DESC LIMIT {n:UInt32}",
            parameters={"p": project_id, "a": agent_id, "e": env, "n": limit},
        ).result_rows
        return [tid for (tid,) in rows]

    # ── score reads ───────────────────────────────────────────────────────────

    def failing_trace_reasons(
        self, project_id: str, limit: int = 5000
    ) -> dict[str, list[tuple[str, str]]]:
        """`{trace_id: [(score_name, comment), ...]}` for every auto-eval FAIL. Used by failure
        intelligence to know WHY each trace was flagged."""
        rows = self.client.query(
            "SELECT trace_id, name, comment FROM scores FINAL WHERE project_id = {p:String} "
            "AND source = 'EVAL' AND verdict = 'FAIL' AND evaluation_case_id = '' "
            # `is_deleted = 0` like `scores_by_trace`: FINAL collapses versions of a row, it does
            # not drop tombstoned ones, so without this a deleted score still names a failure.
            "AND is_deleted = 0 "
            "LIMIT {n:UInt32}",
            parameters={"p": project_id, "n": limit},
        ).result_rows
        by_tid: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for tid, name, comment in rows:
            by_tid[tid].append((name, comment))
        return by_tid

    def recent_failing_traces(self, project_id: str, limit: int = 20) -> list[dict]:
        """Recent traces that a non-advisory-agnostic FAIL landed on, newest first, with enough to
        label them in a picker: `[{trace_id, ts, agent_id, input}]`.

        Serves the alert editor's "test this rule against a real turn" dropdown — testing a
        "conversation broke" rule against a passing turn renders an empty failure and teaches the
        user nothing. Advisory filtering is deliberately NOT applied here: the picker offers
        candidates, and `alert_events.trace_event` is what applies the verdict policy.
        """
        rows = self.client.query(
            "SELECT e.trace_id, max(e.start_time) AS ts, any(e.agent_id) AS agent_id, "
            "  argMin(e.input, e.start_time) AS input "
            "FROM events e FINAL "
            "INNER JOIN ("
            "  SELECT DISTINCT trace_id FROM scores FINAL WHERE project_id = {p:String} "
            "  AND verdict = 'FAIL' AND is_deleted = 0"
            ") s ON s.trace_id = e.trace_id "
            "WHERE e.project_id = {p:String} AND e.internal_kind = '' "
            "GROUP BY e.trace_id ORDER BY ts DESC LIMIT {n:UInt32}",
            parameters={"p": project_id, "n": limit},
        ).result_rows
        return [
            {"trace_id": tid, "ts": str(ts), "agent_id": aid, "input": inp}
            for tid, ts, aid, inp in rows
        ]

    def scores_by_trace(
        self, project_id: str, trace_ids: Iterable[str]
    ) -> dict[str, list[dict]]:
        """`{trace_id: [{name, verdict, value, comment}, ...]}` — the sync twin of the async
        reader's per-trace score fetch, for workers. Used by the gate to roll an emulated
        conversation's turns up into one verdict; the advisory filtering is applied by
        `domain.evaluation.verdict`, not here, so the one policy stays in one place."""
        uniq = sorted({t for t in trace_ids if t})
        if not uniq:
            return {}
        rows = self.client.query(
            "SELECT trace_id, name, verdict, value, comment FROM scores FINAL "
            "WHERE project_id = {p:String} AND trace_id IN {t:Array(String)} AND is_deleted = 0",
            parameters={"p": project_id, "t": uniq},
        ).result_rows
        by_tid: dict[str, list[dict]] = defaultdict(list)
        for tid, name, verdict, value, comment in rows:
            by_tid[tid].append(
                {"name": name, "verdict": verdict, "value": value, "comment": comment}
            )
        return dict(by_tid)

    def span_count(self, project_id: str, trace_ids: Iterable[str]) -> int:
        """Total spans across `trace_ids`. The gate samples this to tell when a customer's own
        spans have finished arriving on an emulated conversation (count stops growing).

        FINAL because this is a *stability* signal: an unmerged duplicate of a span already counted
        looks exactly like a new span arriving, so the poll keeps waiting for traffic that already
        landed — or, once merged between two samples, the count DROPS and the loop reads that as
        settled while spans are still coming.
        """
        uniq = sorted({t for t in trace_ids if t})
        if not uniq:
            return 0
        rows = self.client.query(
            "SELECT count() FROM events FINAL WHERE project_id = {p:String} "
            "AND trace_id IN {t:Array(String)}",
            parameters={"p": project_id, "t": uniq},
        ).result_rows
        return int(rows[0][0]) if rows else 0

    # ── ui helpers ────────────────────────────────────────────────────────────

    def member_timestamps(self, project_id: str, trace_ids: Iterable[str]) -> list[Any]:
        """Just the start times of these traces — what the cluster's "seen over time" histogram
        needs. Separate from `member_meta` because that one also carries each trace's input text:
        pulling those for a 5,000-member cluster to draw a bar chart is the whole payload for a
        few pixels. Missing traces (wiped / TTL'd) are simply absent."""
        uniq = sorted({t for t in trace_ids if t})
        if not uniq:
            return []
        rows = self.client.query(
            "SELECT min(start_time) AS ts FROM events FINAL "
            "WHERE project_id = {p:String} AND trace_id IN {t:Array(String)} GROUP BY trace_id",
            parameters={"p": project_id, "t": uniq},
        ).result_rows
        return [r[0] for r in rows]

    def member_meta(
        self, project_id: str, trace_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """Per-member facts used by the cluster detail view: timestamp, latency, input snippet,
        error message. Returns `{trace_id: {ts, latency_ms, input, error}}`. Drops trace_ids no
        longer present in events (wiped, or aged out by ClickHouse TTL).

        `error` is the earliest non-empty `status_message` in the trace — for a crash cluster
        that IS the failure ("ValueError: kaboom"), and those traces often carry no input at all,
        so without it the cluster's member list has nothing to say about any of them."""
        uniq = sorted({t for t in trace_ids if t})
        if not uniq:
            return {}
        rows = self.client.query(
            "SELECT trace_id, min(start_time) AS ts, "
            "dateDiff('millisecond', min(start_time), max(coalesce(end_time, start_time))) AS lat, "
            "argMinIf(input, start_time, input != '') AS inp, "
            "argMinIf(coalesce(status_message, ''), start_time, "
            "  coalesce(status_message, '') != '') AS err "
            "FROM events FINAL WHERE project_id = {p:String} AND trace_id IN {t:Array(String)} "
            "GROUP BY trace_id",
            parameters={"p": project_id, "t": uniq},
        ).result_rows
        # input is left raw here; routers/services that want a readable snippet should pass it
        # through `tracely.infrastructure.text.message_text`.
        return {
            r[0]: {"ts": r[1], "latency_ms": float(r[2]), "input": r[3], "error": r[4]}
            for r in rows
        }
