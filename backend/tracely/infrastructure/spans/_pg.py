"""Connections + result shaping shared by the Postgres span-store modules.

Everything here exists so a Postgres reader returns *exactly* what its ClickHouse twin returned —
the routers, services and frontend were written against those shapes:

- datetimes come back **naive UTC** (clickhouse-connect returns naive `DateTime64`; a stray aware
  one breaks every `now() - ts` in the services and changes the JSON the frontend parses);
- `Decimal` (Postgres `sum(bigint)`/`numeric`) becomes float — readers cast integer sums to
  `bigint` in SQL, this is only the safety net.

`SYNC` / `ASYNC` are module globals so tests can point them at a scratch database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from tracely.infrastructure.db import engine as _engine

SYNC = _engine.sync_engine
ASYNC = _engine.async_engine


def norm(v: Any) -> Any:
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(v, Decimal):
        return float(v)
    return v


def utc(v: Any) -> Any:
    """Writes: a naive datetime is UTC (that is what ClickHouse stored and what the OTel mapper
    produces), so say so before Postgres reads it in the session's time zone."""
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def dicts(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: norm(v) for k, v in r.items()} for r in rows]


def tuples(rows: Iterable[Iterable[Any]]) -> list[tuple]:
    return [tuple(norm(v) for v in r) for r in rows]


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Sync read -> list of dicts (column name -> value)."""
    with SYNC.connect() as conn:
        return dicts(conn.execute(text(sql), dict(params or {})).mappings())


def query_rows(sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    with SYNC.connect() as conn:
        return tuples(conn.execute(text(sql), dict(params or {})))


async def aquery(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Async read (API) -> list of dicts."""
    async with ASYNC.connect() as conn:
        res = await conn.execute(text(sql), dict(params or {}))
        return dicts(res.mappings())


async def aquery_rows(sql: str, params: Mapping[str, Any] | None = None) -> list[tuple]:
    async with ASYNC.connect() as conn:
        res = await conn.execute(text(sql), dict(params or {}))
        return tuples(res)


def like(q: str) -> str:
    """`q` as a case-insensitive *substring* pattern (ClickHouse `positionCaseInsensitive > 0`):
    escape the LIKE wildcards the user typed, so `100%` searches for a literal percent sign."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
