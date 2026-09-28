"""Write access to the ClickHouse `scores` table.

Two flavors of scores go in here:
- Online eval scores (one per evaluator per target; ids derived from a stable UUID5 namespace so
  re-evaluating a target replaces rather than duplicates via ReplacingMergeTree). A target is a
  trace (AGENT_RUN), one of its spans (SPAN/TOOL/…, via `observation_id`), or — for
  CONVERSATION-level evaluators — the whole thread (addressed by `session_id`, no trace_id).
- Regression/gate verdict scores (one per case×trace; carries `evaluation_case_id`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol

from clickhouse_connect.driver.client import Client

from tracely.infrastructure.clickhouse.client import get_client, insert_rows

# UUID5 namespace for online eval scores — stable across re-evaluations so spans arriving in
# multiple batches converge to the same id under ReplacingMergeTree.
_ONLINE_EVAL_NS = uuid.UUID("c0ffee00-0000-0000-0000-000000000001")

_CONVERSATION = "CONVERSATION"

_ONLINE_EVAL_COLS = [
    "project_id", "id", "trace_id", "observation_id", "session_id", "agent_run_id", "name",
    "source", "data_type", "value", "string_value", "verdict", "evaluation_level", "comment",
    "metadata", "created_at", "event_ts",
]


def _usage_metadata(usage: dict | None) -> dict[str, str]:
    """LLM-judge token usage → string-map entries for the `scores.metadata` column (so eval spend
    is attributable per evaluator). Empty for structural checks (no LLM call)."""
    if not usage:
        return {}
    return {
        "eval.input_tokens": str(int(usage.get("input_tokens") or 0)),
        "eval.output_tokens": str(int(usage.get("output_tokens") or 0)),
        "eval.total_tokens": str(int(usage.get("total_tokens") or 0)),
        "eval.model": str(usage.get("model") or ""),
    }

_REGRESSION_VERDICT_COLS = [
    "project_id", "id", "trace_id", "name", "source", "data_type", "value",
    "verdict", "evaluation_case_id", "evaluation_level", "comment", "created_at", "event_ts",
]


class _EvalResultLike(Protocol):
    """Structural type so we don't have to import EvalResult from domain.evaluation here."""

    name: str
    level: str
    verdict: str
    data_type: str
    value: float | None
    string_value: str
    target_span_id: str
    comment: str
    usage: dict | None


class ScoreWriter:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = get_client()
        return self._client

    def write_eval_scores(
        self,
        project_id: str,
        trace_id: str,
        agent_run_id: str,
        results: list[_EvalResultLike],
        thread_id: str = "",
    ) -> None:
        if not results:
            return
        now = datetime.now(timezone.utc)
        rows = []
        for r in results:
            # Thread-scoped CONVERSATION rows are keyed by the thread (no trace_id — readers find
            # them via session_id); everything else by (trace, evaluator, span).
            sid = self.eval_score_id(trace_id, thread_id, r.name, r.level, r.target_span_id)
            if r.level == _CONVERSATION:
                row_trace, row_session = None, thread_id
            else:
                row_trace, row_session = trace_id, thread_id or None
            rows.append([
                project_id, sid, row_trace, r.target_span_id or None, row_session, agent_run_id,
                r.name, "EVAL", r.data_type, r.value, r.string_value or "", r.verdict, r.level,
                r.comment, _usage_metadata(getattr(r, "usage", None)), now, now,
            ])
        insert_rows(self.client, "scores", _ONLINE_EVAL_COLS, rows)

    @staticmethod
    def eval_score_id(trace_id: str, thread_id: str, name: str, level: str, span_id: str) -> str:
        """The deterministic id `write_eval_scores` gives a result — one place, so retraction
        computes exactly the ids a write just produced."""
        if level == _CONVERSATION:
            return str(uuid.uuid5(_ONLINE_EVAL_NS, f"thread:{thread_id}:{name}"))
        return str(uuid.uuid5(_ONLINE_EVAL_NS, f"{trace_id}:{name}:{span_id}"))

    def retract_eval_scores(
        self,
        project_id: str,
        names: list[str],
        *,
        trace_id: str = "",
        thread_id: str = "",
        keep_ids: set[str] | None = None,
    ) -> int:
        """Delete these evaluators' online scores for one item except `keep_ids` — the rows the
        evaluator's latest run did NOT produce. `trace_id` scopes to a turn (every level but
        CONVERSATION); with only `thread_id` it scopes to the thread's CONVERSATION rows.

        Without this a re-run could only ever ADD: a step no longer graded, a column moved to
        another level, a turn that now has nothing gradable all kept their old verdict — still
        failing the trace. Tombstones (a newer row with is_deleted = 1, which FINAL drops), never a
        mutation, for the reason `deletes.delete_trace` gives. Returns rows retracted."""
        if not names or not (trace_id or thread_id):
            return 0
        keep = keep_ids or set()
        if trace_id:
            scope, params = "trace_id = {t:String}", {"t": trace_id}
        else:
            scope, params = "session_id = {s:String} AND evaluation_level = 'CONVERSATION'", {"s": thread_id}
        rows = self.client.query(
            "SELECT id, name FROM scores FINAL WHERE project_id = {p:String} AND source = 'EVAL' "
            f"AND evaluation_case_id = '' AND name IN {{n:Array(String)}} AND {scope}",
            parameters={"p": project_id, "n": sorted(set(names)), **params},
        ).result_rows
        doomed = [(sid, name) for sid, name in rows if sid not in keep]
        if not doomed:
            return 0
        now = datetime.now(timezone.utc)
        insert_rows(
            self.client, "scores",
            ["project_id", "id", "name", "source", "is_deleted", "event_ts", "created_at"],
            [[project_id, sid, name, "EVAL", 1, now, now] for sid, name in doomed],
        )
        return len(doomed)

    def retract_evaluator(self, project_id: str, name: str) -> int:
        """Delete every online score of one evaluator — what deleting the column means. Its
        verdicts otherwise kept failing traces for the 90-day TTL, and a deleted ADVISORY column's
        historic FAILs started counting as real failures the moment it left the advisory set."""
        rows = self.client.query(
            "SELECT id FROM scores FINAL WHERE project_id = {p:String} AND source = 'EVAL' "
            "AND evaluation_case_id = '' AND name = {n:String}",
            parameters={"p": project_id, "n": name},
        ).result_rows
        if not rows:
            return 0
        now = datetime.now(timezone.utc)
        insert_rows(
            self.client, "scores",
            ["project_id", "id", "name", "source", "is_deleted", "event_ts", "created_at"],
            [[project_id, sid, name, "EVAL", 1, now, now] for (sid,) in rows],
        )
        return len(rows)

    def write_regression_verdict(self, case, trace_id: str, verdict: str) -> None:
        now = datetime.now(timezone.utc)
        row = [
            case.project_id, str(uuid.uuid4()), trace_id, "tracely.regression.verdict", "EVAL",
            "BOOLEAN", 1.0 if verdict == "PASS" else 0.0, verdict, case.id, case.level, "", now, now,
        ]
        insert_rows(self.client, "scores", _REGRESSION_VERDICT_COLS, [row])
