"""The Postgres span insert — the twin of `clickhouse.client.insert_rows(..., "events", ...)`.

Idempotent the way ReplacingMergeTree was: a span is keyed by (project_id, trace_id, span_id), so a
re-delivered batch overwrites instead of duplicating, and the trace's rollup is recomputed from the
spans in the same transaction (see `rollup`)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, text

from tracely.infrastructure.spans import _pg, rollup
from tracely.infrastructure.spans.schema import COLUMNS, to_row

_KEY = ("project_id", "trace_id", "span_id")
_JSONB = {"usage_details", "cost_details", "tool_definitions", "metadata"}


def _value(col: str) -> str:
    return f"CAST(:{col} AS jsonb)" if col in _JSONB else f":{col}"


UPSERT = (
    f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join(_value(c) for c in COLUMNS)}) "
    f"ON CONFLICT ({', '.join(_KEY)}) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in _KEY)
)


def write_events(conn: Connection, events: Iterable[dict[str, Any]], *, lock: bool = True) -> int:
    """Upsert spans + refresh their traces' rollups on `conn` (caller owns the transaction)."""
    now = datetime.now(timezone.utc)
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        if ev.get("project_id") and ev.get("trace_id") and ev.get("span_id"):
            by_project[ev["project_id"]].append(to_row(ev, now))
    n = 0
    for project_id, rows in by_project.items():
        trace_ids = {r["trace_id"] for r in rows}
        if lock:
            rollup.lock(conn, project_id, trace_ids)
        # executemany = one statement per row, so a span repeated inside one batch is simply
        # written twice (last wins) instead of tripping "ON CONFLICT cannot affect a row twice".
        conn.execute(text(UPSERT), rows)
        rollup.refresh(conn, project_id, trace_ids)
        n += len(rows)
    return n


def insert_events(events: list[dict[str, Any]]) -> int:
    """`IngestionService`'s one write: spans + rollups, atomically."""
    if not events:
        return 0
    with _pg.SYNC.begin() as conn:
        return write_events(conn, events)
