"""The span store: where `events` and `scores` live, picked once at import by `SPAN_STORE`.

Every caller imports the store from here — `async_reader`, `deletes`, `TraceReader`,
`ScoreWriter`, `insert_events` — never from `infrastructure.clickhouse` or the Postgres modules
directly, so flipping the setting moves the whole product at once. The two implementations have
identical names, signatures and return shapes. (The Postgres delete module is `pg_deletes`, not
`deletes`: a submodule named like an attribute this package exports gets swapped in by the import
system the moment anything imports it, silently pointing every `spans.deletes` caller at the
other store.)

ponytail: two stores exist only for the cutover (design/part2-tracely/14-clickhouse-to-postgres.md
§8). Once prod has run on Postgres for a while, delete `infrastructure/clickhouse/` and this switch.
"""

from __future__ import annotations

from typing import Any

from tracely.config import settings

POSTGRES = settings.span_store == "postgres"

if POSTGRES:
    from tracely.infrastructure.spans import pg_deletes as deletes
    from tracely.infrastructure.spans import reads as async_reader
    from tracely.infrastructure.spans.score_writer import ScoreWriter
    from tracely.infrastructure.spans.trace_reader import TraceReader
    from tracely.infrastructure.spans.writer import insert_events
else:
    from tracely.infrastructure.clickhouse import async_reader, deletes
    from tracely.infrastructure.clickhouse.score_writer import ScoreWriter
    from tracely.infrastructure.clickhouse.trace_reader import TraceReader

    def insert_events(events: list[dict[str, Any]]) -> int:
        from tracely.infrastructure.clickhouse.client import get_client, insert_rows
        from tracely.infrastructure.clickhouse.events_schema import EVENT_COLUMNS, to_rows

        insert_rows(get_client(), "events", EVENT_COLUMNS, to_rows(events))
        return len(events)


__all__ = ["POSTGRES", "ScoreWriter", "TraceReader", "async_reader", "deletes", "insert_events"]
