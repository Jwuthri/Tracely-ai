"""Postgres twin of `clickhouse.score_writer.ScoreWriter` — same methods, same deterministic ids.

ReplacingMergeTree + FINAL becomes `ON CONFLICT (project_id, id) DO UPDATE`: re-grading a target
overwrites its score (and `created_at`, exactly as FINAL surfaced the newest version), and a
retraction is a real DELETE instead of a tombstone."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from tracely.infrastructure.clickhouse.score_writer import (
    _CONVERSATION,
    ScoreWriter as _ClickHouseScoreWriter,
    _EvalResultLike,
    _usage_metadata,
)
from tracely.infrastructure.spans import _pg

_COLS = [
    "project_id",
    "id",
    "trace_id",
    "observation_id",
    "session_id",
    "agent_run_id",
    "name",
    "source",
    "data_type",
    "value",
    "string_value",
    "verdict",
    "evaluation_case_id",
    "evaluation_level",
    "comment",
    "metadata",
    "created_at",
    "turn_id",
    "gate_run_id",
    "execution_trace_id",
]
_UPSERT = (
    f"INSERT INTO scores ({', '.join(_COLS)}) VALUES ("
    + ", ".join("CAST(:metadata AS jsonb)" if c == "metadata" else f":{c}" for c in _COLS)
    + ") ON CONFLICT (project_id, id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS if c not in ("project_id", "id"))
)


def _row(**kw) -> dict:
    row = {c: kw.get(c) for c in _COLS}
    for c in (
        "agent_run_id",
        "string_value",
        "verdict",
        "evaluation_case_id",
        "evaluation_level",
        "comment",
        "data_type",
        "turn_id",
        "gate_run_id",
        "execution_trace_id",
    ):
        row[c] = row[c] or ""
    row["metadata"] = json.dumps(dict(kw.get("metadata") or {}))
    row["created_at"] = _pg.utc(row["created_at"]) or datetime.now(timezone.utc)
    return row


class ScoreWriter:
    def __init__(self, client=None) -> None:  # `client` kept for call-site compatibility
        self._client = client

    eval_score_id = staticmethod(_ClickHouseScoreWriter.eval_score_id)

    def _write(self, rows: list[dict]) -> None:
        if rows:
            with _pg.SYNC.begin() as conn:
                conn.execute(text(_UPSERT), rows)

    def write_eval_scores(
        self,
        project_id: str,
        trace_id: str,
        agent_run_id: str,
        results: list[_EvalResultLike],
        thread_id: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        rows = []
        for r in results:
            sid = self.eval_score_id(trace_id, thread_id, r.name, r.level, r.target_span_id)
            if r.level == _CONVERSATION:
                row_trace, row_session = None, thread_id
            else:
                row_trace, row_session = trace_id, thread_id or None
            rows.append(
                _row(
                    project_id=project_id,
                    id=sid,
                    trace_id=row_trace,
                    observation_id=r.target_span_id or None,
                    session_id=row_session,
                    agent_run_id=agent_run_id,
                    name=r.name,
                    source="EVAL",
                    data_type=r.data_type,
                    value=r.value,
                    string_value=r.string_value,
                    verdict=r.verdict,
                    evaluation_level=r.level,
                    comment=r.comment,
                    metadata=_usage_metadata(getattr(r, "usage", None)),
                    created_at=now,
                )
            )
        self._write(rows)

    def retract_eval_scores(
        self,
        project_id: str,
        names: list[str],
        *,
        trace_id: str = "",
        thread_id: str = "",
        keep_ids: set[str] | None = None,
    ) -> int:
        """Same contract as the ClickHouse twin: delete these evaluators' online scores for one
        item except `keep_ids`. Returns rows retracted."""
        if not names or not (trace_id or thread_id):
            return 0
        if trace_id:
            scope, params = "trace_id = :t", {"t": trace_id}
        else:
            scope, params = (
                "session_id = :s AND evaluation_level = 'CONVERSATION'",
                {"s": thread_id},
            )
        with _pg.SYNC.begin() as conn:
            res = conn.execute(
                text(
                    "DELETE FROM scores WHERE project_id = :p AND source = 'EVAL' "
                    f"AND evaluation_case_id = '' AND name = ANY(:n) AND {scope} "
                    "AND NOT (id = ANY(:keep))"
                ),
                {
                    "p": project_id,
                    "n": sorted(set(names)),
                    "keep": sorted(keep_ids or ()),
                    **params,
                },
            )
            return res.rowcount or 0

    def retract_evaluator(self, project_id: str, name: str) -> int:
        with _pg.SYNC.begin() as conn:
            res = conn.execute(
                text(
                    "DELETE FROM scores WHERE project_id = :p AND source = 'EVAL' "
                    "AND evaluation_case_id = '' AND name = :n"
                ),
                {"p": project_id, "n": name},
            )
            return res.rowcount or 0

    def write_regression_verdict(self, case, trace_id: str, verdict: str) -> None:
        self._write(
            [
                _row(
                    project_id=case.project_id,
                    id=str(uuid.uuid4()),
                    trace_id=trace_id,
                    name="tracely.regression.verdict",
                    source="EVAL",
                    data_type="BOOLEAN",
                    value=1.0 if verdict == "PASS" else 0.0,
                    verdict=verdict,
                    evaluation_case_id=case.id,
                    evaluation_level=case.level,
                    created_at=datetime.now(timezone.utc),
                )
            ]
        )
