"""Sync Postgres reads for workers and services — the twin of `clickhouse.trace_reader.TraceReader`,
method for method (same names, signatures, return shapes). Span-level reads hit `events` by
index; per-trace facts come from the `traces` rollup."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from tracely.infrastructure.spans import _pg
from tracely.infrastructure.spans.reads import SPAN_COLS


def _uniq(ids: Iterable[str]) -> list[str]:
    return sorted({t for t in ids if t})


class TraceReader:
    def __init__(self, client: Any = None) -> None:  # `client` kept for call-site compatibility
        self._client = client

    @property
    def client(self) -> Any:
        """Services hand this to `ScoreWriter(...)`; on Postgres both share the engine."""
        return self._client or _pg.SYNC

    # ── span reads ────────────────────────────────────────────────────────────

    def read_spans(self, project_id: str, trace_id: str) -> list[dict]:
        return _pg.query(
            f"SELECT {', '.join(SPAN_COLS)} FROM events WHERE project_id = :p AND trace_id = :t "
            "ORDER BY start_time",
            {"p": project_id, "t": trace_id},
        )

    def read_thread_spans(self, project_id: str, thread_id: str) -> list[dict]:
        return _pg.query(
            f"SELECT {', '.join(SPAN_COLS)} FROM events WHERE project_id = :p "
            "AND (conversation_id = :th OR trace_id = :th) ORDER BY start_time",
            {"p": project_id, "th": thread_id},
        )

    def thread_trace_ids(self, project_id: str, thread_id: str) -> list[str]:
        rows = _pg.query_rows(
            "SELECT trace_id FROM events WHERE project_id = :p "
            "AND (conversation_id = :th OR trace_id = :th) GROUP BY trace_id ORDER BY min(start_time)",
            {"p": project_id, "th": thread_id},
        )
        return [tid for (tid,) in rows]

    def trace_generations(self, project_id: str, trace_id: str) -> list[tuple]:
        """`(input, output, start_time)` of one trace's GENERATION spans — the ingest-time
        "Agent workflow" root fill (was raw SQL inside `IngestionService`)."""
        return _pg.query_rows(
            "SELECT input, output, start_time FROM events WHERE project_id = :p AND trace_id = :t "
            "AND type = 'GENERATION'",
            {"p": project_id, "t": trace_id},
        )

    def candidate_metrics(
        self, project_id: str, trace_ids: Iterable[str]
    ) -> tuple[float, int, dict[str, tuple[float, int]]]:
        uniq = _uniq(trace_ids)
        if not uniq:
            return 0.0, 0, {}
        rows = _pg.query_rows(
            "SELECT trace_id, latency_ms, tokens FROM traces WHERE project_id = :p AND trace_id = ANY(:t)",
            {"p": project_id, "t": uniq},
        )
        per = {tid: (float(lat), int(toks)) for tid, lat, toks in rows}
        return sum(v[0] for v in per.values()), sum(v[1] for v in per.values()), per

    def latest_traces_for_env(
        self, project_id: str, agent_id: str, env: str, limit: int = 300
    ) -> list[str]:
        """Span-level, exactly as before: among this agent's spans in `env`, no `emulated.turn`."""
        rows = _pg.query_rows(
            "SELECT trace_id FROM events WHERE project_id = :p AND agent_id = :a AND env = :e "
            "AND internal_kind = '' GROUP BY trace_id "
            "HAVING count(*) FILTER (WHERE name = 'emulated.turn') = 0 "
            "ORDER BY max(start_time) DESC LIMIT :n",
            {"p": project_id, "a": agent_id, "e": env, "n": limit},
        )
        return [tid for (tid,) in rows]

    def traces_with_input(
        self, project_id: str, agent_id: str, input_text: str, limit: int = 20
    ) -> list[dict]:
        rows = _pg.query_rows(
            "SELECT trace_id, max(env), max(start_time), max(metadata->>'tracely.replay.run_id'), "
            "max(level) FROM events WHERE project_id = :p AND agent_id = :a AND internal_kind = '' "
            "AND is_app_root AND md5(input) = md5(:i) AND input = :i "
            "GROUP BY trace_id ORDER BY max(start_time) DESC LIMIT :n",
            {"p": project_id, "a": agent_id, "i": input_text, "n": limit},
        )
        return [
            {
                "trace_id": tid,
                "env": env,
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "run_id": run_id or "",
                "level": level or "",
            }
            for tid, env, ts, run_id, level in rows
        ]

    def traces_for_run(
        self, project_id: str, agent_id: str, run_id: str, limit: int = 500
    ) -> list[str]:
        rows = _pg.query_rows(
            "SELECT trace_id FROM events WHERE project_id = :p AND agent_id = :a AND internal_kind = '' "
            "AND metadata ? 'tracely.replay.run_id' AND metadata->>'tracely.replay.run_id' = :r "
            "GROUP BY trace_id ORDER BY max(start_time) DESC LIMIT :n",
            {"p": project_id, "a": agent_id, "r": run_id, "n": limit},
        )
        return [tid for (tid,) in rows]

    # ── score reads ───────────────────────────────────────────────────────────

    def failing_trace_reasons(
        self, project_id: str, limit: int = 5000
    ) -> dict[str, list[tuple[str, str]]]:
        rows = _pg.query_rows(
            "SELECT trace_id, name, comment FROM scores WHERE project_id = :p AND source = 'EVAL' "
            "AND verdict = 'FAIL' AND evaluation_case_id = '' LIMIT :n",
            {"p": project_id, "n": limit},
        )
        by_tid: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for tid, name, comment in rows:
            by_tid[tid].append((name, comment))
        return by_tid

    def recent_failing_traces(self, project_id: str, limit: int = 20) -> list[dict]:
        rows = _pg.query_rows(
            "SELECT t.trace_id, t.ts_max, t.agent_id, t.root_input FROM traces t "
            "WHERE t.project_id = :p AND t.internal_kind = '' AND t.trace_id IN ("
            "  SELECT trace_id FROM scores WHERE project_id = :p AND verdict = 'FAIL') "
            "ORDER BY t.ts_max DESC LIMIT :n",
            {"p": project_id, "n": limit},
        )
        return [
            {"trace_id": tid, "ts": str(ts), "agent_id": aid, "input": inp}
            for tid, ts, aid, inp in rows
        ]

    def scores_by_trace(self, project_id: str, trace_ids: Iterable[str]) -> dict[str, list[dict]]:
        uniq = _uniq(trace_ids)
        if not uniq:
            return {}
        rows = _pg.query_rows(
            "SELECT trace_id, name, verdict, value, comment FROM scores "
            "WHERE project_id = :p AND trace_id = ANY(:t)",
            {"p": project_id, "t": uniq},
        )
        by_tid: dict[str, list[dict]] = defaultdict(list)
        for tid, name, verdict, value, comment in rows:
            by_tid[tid].append(
                {"name": name, "verdict": verdict, "value": value, "comment": comment}
            )
        return dict(by_tid)

    def span_count(self, project_id: str, trace_ids: Iterable[str]) -> int:
        """A stability signal (spans still arriving?), so it counts spans, not the rollup."""
        uniq = _uniq(trace_ids)
        if not uniq:
            return 0
        rows = _pg.query_rows(
            "SELECT count(*) FROM events WHERE project_id = :p AND trace_id = ANY(:t)",
            {"p": project_id, "t": uniq},
        )
        return int(rows[0][0]) if rows else 0

    # ── ui helpers ────────────────────────────────────────────────────────────

    def member_timestamps(self, project_id: str, trace_ids: Iterable[str]) -> list[Any]:
        uniq = _uniq(trace_ids)
        if not uniq:
            return []
        rows = _pg.query_rows(
            "SELECT ts_min FROM traces WHERE project_id = :p AND trace_id = ANY(:t)",
            {"p": project_id, "t": uniq},
        )
        return [r[0] for r in rows]

    def version_breakdown(
        self, project_id: str, agent_id: str, member_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        rows = _pg.query_rows(
            "SELECT v, count(*), count(*) FILTER (WHERE trace_id = ANY(:m)), min(ts) FROM ("
            "  SELECT trace_id, coalesce(max(agent_version_id) FILTER (WHERE agent_version_id <> ''), '') AS v, "
            "  min(start_time) AS ts FROM events WHERE project_id = :p AND agent_id = :ag "
            "  AND env <> 'ci' AND internal_kind = '' GROUP BY trace_id) x GROUP BY v",
            {"p": project_id, "ag": agent_id, "m": _uniq(member_ids)},
        )
        return [
            {"version_id": v, "runs": int(runs), "members": int(hits), "first_seen": first}
            for v, runs, hits, first in rows
        ]

    def member_meta(self, project_id: str, trace_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        uniq = _uniq(trace_ids)
        if not uniq:
            return {}
        rows = _pg.query_rows(
            "SELECT trace_id, ts_min, latency_ms, root_input, first_error FROM traces "
            "WHERE project_id = :p AND trace_id = ANY(:t)",
            {"p": project_id, "t": uniq},
        )
        return {
            r[0]: {"ts": r[1], "latency_ms": float(r[2]), "input": r[3], "error": r[4]}
            for r in rows
        }
