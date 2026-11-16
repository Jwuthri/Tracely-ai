"""The per-trace rollup (`traces`): one row per trace, recomputed from its spans.

`refresh` runs in the SAME transaction as every span write or delete, and recomputes from scratch
(never increments), so late spans, re-deliveries and partial deletes all converge — the property
ReplacingMergeTree used to give. Each column is the SQL twin of a ClickHouse read-time expression:

- `root_*` / `has_*`  — `async_reader._root` / `_root_has`: the parentless span, else the
  `is_app_root` one (most of prod has no parentless span);
- `agent_id`          — `_TRACE_AGENT` (root's, else app-root's, else any span's);
- `app_root_agent_id` — `trace_agent_ids`' stricter rule (the app-root span only);
- `metadata`          — the `tracely.metadata.*` keys the threads list surfaces.

Verdicts are deliberately absent: scores change on their own schedule and the advisory set can
change at any time, so "failing" stays a read-time join (the one verdict policy, one place).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, text

_ROOT = "parent_span_id = ''"


def _root(expr_agg: str, col: str) -> str:
    """`_root(col)`: from the parentless span when there is one, else from the app-root span."""
    return (
        f"CASE WHEN bool_or({_ROOT}) THEN {expr_agg.format(col=col, cond=_ROOT)} "
        f"ELSE {expr_agg.format(col=col, cond='is_app_root')} END"
    )


_FIRST = "(array_agg({col} ORDER BY start_time) FILTER (WHERE {cond} AND {col} IS NOT NULL))[1]"
_HAS = "coalesce(bool_or({col} IS NOT NULL) FILTER (WHERE {cond}), false)"

REFRESH = f"""
INSERT INTO traces (
  project_id, trace_id, thread_id, conversation_id, env, internal_kind, subject_id,
  agent_id, app_root_agent_id, root_name, first_name, root_input, root_output, has_input,
  has_output, spans, ts_min, ts_max, latency_ms, tokens, input_tokens, output_tokens, cost,
  model_id, has_error, tool_errors, first_error, has_emulated_turn, metadata, updated_at)
SELECT
  g.project_id, g.trace_id,
  coalesce(nullif(max(g.conversation_id), ''), g.trace_id),
  max(g.conversation_id),
  max(g.env),
  max(g.internal_kind),
  max(g.subject_id),
  coalesce(nullif(max(g.agent_id) FILTER (WHERE g.{_ROOT}), ''),
           nullif(max(g.agent_id) FILTER (WHERE g.is_app_root), ''),
           max(g.agent_id) FILTER (WHERE g.agent_id <> ''), ''),
  coalesce(max(g.agent_id) FILTER (WHERE g.is_app_root), ''),
  coalesce({_root(_FIRST, "name")}, ''),
  (array_agg(g.name ORDER BY g.start_time))[1],
  {_root(_FIRST, "input")},
  {_root(_FIRST, "output")},
  {_root(_HAS, "input")},
  {_root(_HAS, "output")},
  count(*),
  min(g.start_time),
  max(coalesce(g.end_time, g.start_time)),
  (floor(extract(epoch FROM max(coalesce(g.end_time, g.start_time))) * 1000)
   - floor(extract(epoch FROM min(g.start_time)) * 1000))::bigint,
  sum(g.tokens)::bigint,
  sum(g.input_tokens)::bigint,
  sum(g.output_tokens)::bigint,
  sum(g.cost)::float8,
  coalesce((array_agg(g.model_id ORDER BY g.tokens DESC)
            FILTER (WHERE g.type = 'GENERATION' AND g.model_id <> ''))[1], ''),
  bool_or(g.level = 'ERROR'),
  count(*) FILTER (WHERE g.type = 'TOOL' AND g.level = 'ERROR'),
  coalesce((array_agg(g.status_message ORDER BY g.start_time) FILTER (WHERE g.status_message <> ''))[1], ''),
  bool_or(g.name = 'emulated.turn'),
  coalesce((SELECT jsonb_object_agg(kv.key, kv.value ORDER BY m.start_time)
            FROM events m, jsonb_each_text(m.metadata) kv
            WHERE m.project_id = g.project_id AND m.trace_id = g.trace_id
              AND kv.key LIKE 'tracely.metadata.%'), '{{}}'::jsonb),
  now()
FROM events g
WHERE g.project_id = :p AND g.trace_id = ANY(:ids)
GROUP BY g.project_id, g.trace_id
ON CONFLICT (project_id, trace_id) DO UPDATE SET
  thread_id = EXCLUDED.thread_id, conversation_id = EXCLUDED.conversation_id, env = EXCLUDED.env,
  internal_kind = EXCLUDED.internal_kind, subject_id = EXCLUDED.subject_id,
  agent_id = EXCLUDED.agent_id, app_root_agent_id = EXCLUDED.app_root_agent_id,
  root_name = EXCLUDED.root_name, first_name = EXCLUDED.first_name,
  root_input = EXCLUDED.root_input, root_output = EXCLUDED.root_output,
  has_input = EXCLUDED.has_input, has_output = EXCLUDED.has_output, spans = EXCLUDED.spans,
  ts_min = EXCLUDED.ts_min, ts_max = EXCLUDED.ts_max, latency_ms = EXCLUDED.latency_ms,
  tokens = EXCLUDED.tokens, input_tokens = EXCLUDED.input_tokens,
  output_tokens = EXCLUDED.output_tokens, cost = EXCLUDED.cost, model_id = EXCLUDED.model_id,
  has_error = EXCLUDED.has_error, tool_errors = EXCLUDED.tool_errors,
  first_error = EXCLUDED.first_error, has_emulated_turn = EXCLUDED.has_emulated_turn,
  metadata = EXCLUDED.metadata, updated_at = EXCLUDED.updated_at
"""

# The rows of traces whose last span just went (a delete), so the rollup never outlives its spans.
PRUNE = """
DELETE FROM traces t
WHERE t.project_id = :p AND t.trace_id = ANY(:ids)
  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id = t.project_id AND e.trace_id = t.trace_id)
"""


def lock(conn: Connection, project_id: str, trace_ids: Iterable[str]) -> None:
    """Serialize writers per trace for the rest of this transaction. Without it two workers
    ingesting spans of one trace each recompute without the other's uncommitted spans, and the
    later commit's rollup silently misses some. Sorted, so two writers never deadlock.
    Free today (`--pool=solo --concurrency=1`); load-bearing the day concurrency goes above 1."""
    for tid in sorted(set(trace_ids)):
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"{project_id}:{tid}"},
        )


def refresh(conn: Connection, project_id: str, trace_ids: Iterable[str]) -> None:
    ids = sorted({t for t in trace_ids if t})
    if not ids:
        return
    conn.execute(text(REFRESH), {"p": project_id, "ids": ids})
    conn.execute(text(PRUNE), {"p": project_id, "ids": ids})
