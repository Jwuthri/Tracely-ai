"""The Postgres `events` row: the ClickHouse column list (`events_schema.EVENT_COLUMNS`, still the
one source of truth for what a span carries) minus ReplacingMergeTree bookkeeping, plus the four
spend columns summed once here instead of `arraySum(mapValues(...))` on every read."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from tracely.infrastructure.clickhouse.events_schema import (
    _ARRAYS,
    _BOOLS,
    _INTS,
    _MAPS,
    _NULLABLE,
    EVENT_COLUMNS,
)
from tracely.infrastructure.spans._pg import utc

_DROPPED = {"event_ts", "is_deleted"}
SPEND = ["tokens", "input_tokens", "output_tokens", "cost"]
COLUMNS: list[str] = [c for c in EVENT_COLUMNS if c not in _DROPPED] + SPEND
_TIMES = {"start_time", "end_time", "completion_start_time", "created_at"}


def _default(col: str) -> Any:
    if col in _NULLABLE:
        return None
    if col in _MAPS:
        return {}
    if col in _ARRAYS:
        return []
    if col in _INTS:
        return 0
    if col in _BOOLS:
        return False
    return ""


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def to_row(ev: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """One event dict (the OTel mapper's shape) -> bind params for `writer._UPSERT`.

    '' is stored as NULL for `input`/`output`: "no text" has ONE representation, so the rollup's
    `has_input` is a NULL test that never reads the text."""
    row: dict[str, Any] = {}
    for col in COLUMNS[: -len(SPEND)]:
        v = ev.get(col)
        if v is None or (v == "" and col in _NULLABLE):
            v = _default(col)
        if col in _MAPS:
            v = json.dumps(
                {str(k): (_num(x) if col == "cost_details" else x) for k, x in dict(v).items()},
                default=str,
            )
        elif col in _ARRAYS:
            v = [str(x) for x in v]
        elif col in _TIMES:
            v = utc(v)
        row[col] = v
    row["created_at"] = row["created_at"] or (now or datetime.now(timezone.utc))
    usage = ev.get("usage_details") or {}
    row["tokens"] = int(sum(_num(x) for x in usage.values()))
    row["input_tokens"] = int(_num(usage.get("input")))
    row["output_tokens"] = int(_num(usage.get("output")))
    row["cost"] = sum(_num(x) for x in (ev.get("cost_details") or {}).values())
    return row
