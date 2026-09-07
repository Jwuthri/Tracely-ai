"""Tiny ClickHouse migration runner: apply *.up.sql in order. Idempotent (IF NOT EXISTS)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from clickhouse_connect.driver.client import Client

from tracely.config import settings
from tracely.infrastructure.clickhouse.client import get_client

MIGRATIONS_DIR = Path(__file__).parent / "ddl"

# This is the FIRST thing that touches the network on a deploy (Railway's backend pre-deploy chain,
# docker-compose's migrate step), so it eats every not-ready-yet condition: Railway's private network
# needs a moment to initialize inside a fresh container — internal DNS answers `Name or service not
# known` until it does — and a just-created ClickHouse service may still be booting. Waiting here is
# enough for the whole chain: once the mesh is up, alembic/seeding/S3 resolve too.
# ponytail: one bounded wait in the first step, not a readiness abstraction per dependency.
WAIT_SECONDS = 120.0


def _connect_when_ready(timeout: float = WAIT_SECONDS) -> Client:
    # clickhouse-connect logs a full traceback per failed attempt; while we're deliberately polling a
    # not-ready service that's a screen of noise hiding the one line that matters. Restored after.
    driver_log = logging.getLogger("clickhouse_connect")
    was = driver_log.level
    driver_log.setLevel(logging.CRITICAL)
    try:
        return _poll(timeout)
    finally:
        driver_log.setLevel(was)


def _poll(timeout: float) -> Client:
    deadline = time.monotonic() + timeout
    delay = 1.0
    while True:
        try:
            return get_client(database="default")
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"ClickHouse unreachable at {settings.clickhouse_host}:"
                    f"{settings.clickhouse_port} after {timeout:.0f}s. Check the service is running "
                    "and that CLICKHOUSE_HOST/PORT point at it (on Railway: the ClickHouse service's "
                    "RAILWAY_PRIVATE_DOMAIN, HTTP port 8123)."
                ) from exc
            print(f"clickhouse not ready ({type(exc).__name__}); retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 8.0)


# ClickHouse's own `system.*_log` tables ship with NO retention and are, on a small deployment, an
# order of magnitude bigger than the customer data: 8 GiB of them against 85 MiB of `events` here,
# `asynchronous_metric_log` alone holding 58 BILLION rows. They cannot be TTL'd from a `.up.sql`
# (ALTER TABLE has no IF EXISTS, and a log table that hasn't flushed yet would fail the whole
# pre-deploy chain), so the sweep is driven off the table list instead — which also picks up any log
# table a future ClickHouse version adds. Re-applied every deploy because an upgrade that changes a
# log's schema RENAMES the old table and creates a fresh one, silently dropping the TTL with it.
_SYSTEM_LOG_TTL_DAYS = 3
# Kept longer: these are the ones worth having when something has already gone wrong, and they are
# small — `query_log` is what tells you which query ate the disk.
_SYSTEM_LOG_TTL_KEEP_LONGER = {"query_log": 7, "part_log": 7, "error_log": 14}


def _cap_system_logs(admin: Client) -> None:
    """Give every `system.*_log` a TTL. Best-effort: never fails a deploy over housekeeping."""
    try:
        rows = admin.query(
            "SELECT name FROM system.tables WHERE database = 'system' AND engine LIKE '%MergeTree%' "
            "AND name LIKE '%_log' AND name IN "
            "  (SELECT table FROM system.columns WHERE database = 'system' AND name = 'event_date')"
        ).result_rows
    except Exception as exc:  # noqa: BLE001
        print(f"system log TTL skipped ({type(exc).__name__}: {exc})")
        return
    for (name,) in rows:
        days = _SYSTEM_LOG_TTL_KEEP_LONGER.get(name, _SYSTEM_LOG_TTL_DAYS)
        try:
            # `materialize_ttl_after_modify=0`: without it ClickHouse rewrites every existing part
            # to apply the new TTL, which on a multi-billion-row log runs for minutes and would
            # hang the deploy. Metadata-only is enough — the next merge drops the expired rows.
            admin.command(
                f"ALTER TABLE system.{name} MODIFY TTL event_date + INTERVAL {days} DAY",
                settings={"materialize_ttl_after_modify": 0},
            )
        except Exception as exc:  # noqa: BLE001 — a log without a TTL costs disk, never a trace
            print(f"system.{name} TTL failed ({type(exc).__name__}: {exc})")


def main() -> None:
    admin = _connect_when_ready()
    admin.command(f"CREATE DATABASE IF NOT EXISTS {settings.clickhouse_database}")
    _cap_system_logs(admin)

    client = get_client()
    # Convention: one statement per *.up.sql file. We send the whole file so that
    # semicolons inside `--` comments don't get mis-split into empty statements.
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.up.sql")):
        sql = sql_file.read_text().strip()
        if sql:
            client.command(sql)
        print(f"applied {sql_file.name}")


if __name__ == "__main__":
    main()
