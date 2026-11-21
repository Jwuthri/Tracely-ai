"""ClickHouse client + the events insert path.

We deliberately do NOT replicate Langfuse's in-process `ClickhouseWriter` buffer:
Celery tasks are separate processes with no shared in-memory queue, so we lean on
ClickHouse server-side `async_insert` for batching (mirrors the same durability /
throughput goal). See design: 01-steal-and-do-not-copy.md (write path) + canonical §0.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from tracely.config import settings


def get_client(database: str | None = None) -> Client:
    """Synchronous client (used by the worker insert path + migrations)."""
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=database if database is not None else settings.clickhouse_database,
    )


_async_client = None
_async_client_loop = None
_async_client_lock = asyncio.Lock()


async def _new_async_client(database: str | None):
    return await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=database if database is not None else settings.clickhouse_database,
    )


async def get_async_client(database: str | None = None):
    """Shared async client for the read API.

    Created once and reused for the process lifetime: clickhouse-connect's async client owns a
    connection pool and is safe to share across coroutines, so opening a new one per query (the old
    behavior) leaked a pool — and its sockets/file descriptors — on every API read. Disposed in the
    FastAPI `lifespan` via `close_async_client()`. A non-default `database` (migrations/tests only)
    always returns a fresh client the caller is responsible for closing."""
    global _async_client, _async_client_loop
    if database is not None and database != settings.clickhouse_database:
        return await _new_async_client(database)
    # The client's HTTP session belongs to the loop that made it. The API has one loop for its
    # whole life; a worker task calls `asyncio.run` per run, so the client a previous task left
    # behind is unusable ("belongs to a different event loop") and every read after the first
    # task failed — monitors and the selfcheck's store probe among them.
    loop = asyncio.get_running_loop()
    if _async_client is None or _async_client_loop is not loop:
        async with _async_client_lock:
            if _async_client is None or _async_client_loop is not loop:  # double-checked
                stale = _async_client
                _async_client, _async_client_loop = await _new_async_client(None), loop
                if stale is not None:
                    try:  # its own loop is gone; closing here is what releases the sockets
                        await stale.close()
                    except Exception:  # noqa: BLE001 — nothing more to do with a dead client
                        pass
    return _async_client


async def close_async_client() -> None:
    """Dispose the shared async client (called on FastAPI shutdown)."""
    global _async_client, _async_client_loop
    if _async_client is not None:
        await _async_client.close()
        _async_client = _async_client_loop = None


def insert_rows(
    client: Client,
    table: str,
    column_names: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> None:
    """Insert column-aligned rows, relying on CH async_insert for server-side batching."""
    if not rows:
        return
    ch_settings: dict[str, Any] = {}
    if settings.clickhouse_async_insert:
        ch_settings = {"async_insert": 1, "wait_for_async_insert": 1}
    client.insert(table, list(rows), column_names=list(column_names), settings=ch_settings)
