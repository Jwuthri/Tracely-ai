"""Keeping ClickHouse from filling its own disk (beat, nightly).

ClickHouse writes its own telemetry — `query_log`, `trace_log`, `part_log`, … — with **no TTL by
default**. On a small deployment that telemetry routinely outgrows the product's data: the
storage report that prompted this found the `system` database holding more bytes than every
customer trace combined, on a box whose disk was the thing we were paying for.

So this is the sweep that gives those tables a TTL, nightly, instead of an engineer noticing.
Two properties it is built around:

- **Idempotent.** A table that already carries a TTL expression is skipped, so the nightly tick
  is a few cheap reads once the cap is in place — not an `ALTER` per table per night.
- **Never the reason a beat tick dies.** Reclaiming space is not correctness; every failure is
  logged and the next night tries again (`workers/tasks.py`).

`MODIFY TTL` materializes against existing parts (ClickHouse's `materialize_ttl_after_modify`
defaults to 1), so the first run is also what reclaims the backlog — on the next merge.
"""

from __future__ import annotations

import re

import structlog

from tracely.config import settings
from tracely.infrastructure.clickhouse.client import get_client

log = structlog.get_logger(__name__)

# Table names are interpolated (ClickHouse cannot parameterize an object name), so they must match
# this before they reach a DDL string. They come from `system.parts` — this is the belt to that
# suspenders, and the reason nothing user-supplied can ever reach `cap_system_logs`.
_SAFE_NAME = re.compile(r"^[a-z0-9_]{1,64}$")

# `TTL event_date + toIntervalDay(30)` in a table's `engine_full`. Recent ClickHouse images ship
# their system logs with a 30-day TTL of their own, so "has a TTL" is NOT the same as "capped" —
# read the number and only leave the table alone when it is already at or below ours.
_TTL_DAYS = re.compile(r"TTL\s+event_date\s*\+\s*toIntervalDay\((\d+)\)")


def _current_days(engine: str) -> int | None:
    """Days in the table's existing event_date TTL, or None when it has no TTL we recognize."""
    m = _TTL_DAYS.search(engine)
    return int(m.group(1)) if m else None


def cap_system_logs(days: int | None = None) -> dict:
    """Put a TTL on ClickHouse's own log tables. Returns what it did, for the beat log.

    `days` defaults to `CH_SYSTEM_LOG_TTL_DAYS`; 0 (or less) disables the sweep entirely — a
    self-hoster debugging a slow query wants their `query_log`, and this is how they keep it.
    """
    days = settings.ch_system_log_ttl_days if days is None else days
    if days <= 0:
        return {"skipped": "disabled"}

    client = get_client(database="system")
    # Only tables that actually occupy disk, biggest first: a system log that was never written
    # has no parts, and altering it would be a mutation for nothing.
    sized = {
        t: int(b)
        for t, b in client.query(
            "SELECT table, sum(bytes_on_disk) FROM system.parts "
            "WHERE active AND database = 'system' GROUP BY table HAVING sum(bytes_on_disk) > 0"
        ).result_rows
    }
    if not sized:
        return {"capped": [], "skipped": [], "bytes": 0}

    # `engine_full` carries the TTL clause when one is set — the cheapest way to ask "did a
    # previous run already do this?", and it survives restarts the way a local marker would not.
    # A table with no `event_date` (not a *_log table) is left alone rather than guessed at.
    # Two flat queries, not one correlated subquery: ClickHouse rejects the latter.
    engines = {
        name: engine or ""
        for name, engine in client.query(
            "SELECT name, engine_full FROM system.tables "
            "WHERE database = 'system' AND name IN {n:Array(String)}",
            parameters={"n": list(sized)},
        ).result_rows
    }
    dated = {
        r[0]
        for r in client.query(
            "SELECT table FROM system.columns WHERE database = 'system' "
            "AND name = 'event_date' AND table IN {n:Array(String)}",
            parameters={"n": list(sized)},
        ).result_rows
    }

    capped, skipped, freed = [], [], 0
    for name in sorted(sized, key=lambda n: -sized[n]):
        engine = engines.get(name, "")
        if not _SAFE_NAME.match(name) or name not in dated:
            skipped.append(name)
            continue
        current = _current_days(engine)
        if current is not None and current <= days:
            continue  # already capped this tight by an earlier run (or by the image's own config)
        if current is None and "TTL" in engine:
            continue  # some other TTL expression — the operator's, not ours to overwrite
        try:
            client.command(f"ALTER TABLE system.{name} MODIFY TTL event_date + INTERVAL {days} DAY")
        except Exception as exc:  # noqa: BLE001 — one stubborn table must not stop the rest
            log.warning("system_log_ttl_failed", table=name, error=str(exc))
            skipped.append(name)
            continue
        capped.append(name)
        freed += sized[name]

    if capped:
        log.info("system_logs_capped", tables=capped, days=days, bytes_under_ttl=freed)
    return {"capped": capped, "skipped": skipped, "bytes": freed, "days": days}


def compact_tables() -> dict:
    """`OPTIMIZE TABLE … FINAL` on `events` and `scores` (beat, nightly).

    Nothing else ever rewrites the big parts, and two kinds of dead rows only leave a table when
    a part is rewritten:

    - **Lightweight-deleted rows.** `DELETE FROM` (a workspace wipe, thread delete, retention)
      only *masks* rows; they stay on disk and every query still reads them. Prod after one wipe:
      733,788 physical rows behind 1,083 visible ones, every read paying for all of them.
    - **Superseded eval recordings.** `deletes.delete_trace` tombstones the previous recording
      instead of mutating; `ReplacingMergeTree` only collapses a span with its tombstone when the
      two land in the same merge.

    Measured on prod: 406 MiB -> 202 KiB in 2 s, visible rows unchanged — FINAL reads already
    see the collapsed, unmasked view, this just makes the disk agree with it.

    ponytail: whole-table rewrite. Fine at hundreds of MiB; once `events` is in the GiBs, switch
    to `OPTIMIZE … PARTITION <id> FINAL` for partitions whose parts carry a delete mask.
    """
    client = get_client()
    done = []
    for table in ("events", "scores"):
        client.command(f"OPTIMIZE TABLE {table} FINAL")
        done.append(table)
    log.info("tables_compacted", tables=done)
    return {"compacted": done}
