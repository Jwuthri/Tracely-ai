"""Postgres twin of `clickhouse.deletes` (exported as `spans.deletes` on the Postgres store) — same functions, same return values.

Real DELETEs: no tombstones, no masked rows that every query still reads, no compaction job.
Every path that removes spans keeps the `traces` rollup in step in the same transaction.

ponytail: each project-wide delete is one statement. Fine at prod's size (tens of thousands of
spans); batch by ctid if a single workspace ever holds millions and the request times out.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from tracely.infrastructure.spans import _pg, rollup


def delete_trace(project_id: str, trace_id: str, step_names: list[str] | None = None) -> None:
    """Drop one trace's spans — or only `step_names`' spans plus the root (which carries no
    group) — before a re-recorded internal run lands. See the ClickHouse twin for why the
    narrowing exists."""
    sql = "DELETE FROM events WHERE project_id = :p AND trace_id = :t"
    params: dict = {"p": project_id, "t": trace_id}
    if step_names:
        sql += " AND (step_name = ANY(:n) OR step_name = '')"
        params["n"] = list(step_names)
    with _pg.SYNC.begin() as conn:
        rollup.lock(conn, project_id, [trace_id])
        if conn.execute(text(sql), params).rowcount:
            rollup.refresh(conn, project_id, [trace_id])


async def delete_threads(project_id: str, threads: list[str]) -> int:
    """Delete every trace in these threads, plus their scores. Returns traces removed."""
    if not threads:
        return 0
    async with _pg.ASYNC.begin() as conn:
        res = await conn.execute(
            text(
                "DELETE FROM traces WHERE project_id = :p "
                "AND (thread_id = ANY(:t) OR trace_id = ANY(:t)) RETURNING trace_id"
            ),
            {"p": project_id, "t": list(threads)},
        )
        trace_ids = [r[0] for r in res]
        if not trace_ids:
            return 0
        params = {"p": project_id, "ids": trace_ids, "t": list(threads)}
        await conn.execute(
            text("DELETE FROM events WHERE project_id = :p AND trace_id = ANY(:ids)"), params
        )
        await conn.execute(
            text(
                "DELETE FROM scores WHERE project_id = :p "
                "AND (trace_id = ANY(:ids) OR session_id = ANY(:t))"
            ),
            params,
        )
    return len(trace_ids)


async def delete_project_events(project_id: str) -> dict[str, int]:
    """Delete every span and score in a project. Returns the row counts removed."""
    counts: dict[str, int] = {}
    async with _pg.ASYNC.begin() as conn:
        for table in ("events", "scores"):
            res = await conn.execute(
                text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id}
            )
            counts[table] = res.rowcount or 0
        await conn.execute(text("DELETE FROM traces WHERE project_id = :p"), {"p": project_id})
    return {k: v for k, v in counts.items() if v}


def delete_expired(project_ids: list[str], days: int) -> int:
    """Drop traces whose NEWEST span is older than `days`, with their spans; scores are cut on
    their own `created_at` (the ClickHouse twin's rule). Whole traces, so a long-running trace is
    never cut in half the way a per-span TTL did. Returns spans removed."""
    if not project_ids or days <= 0:
        return 0
    params = {"p": list(project_ids), "d": days}
    cutoff = "now() - make_interval(days => :d)"
    with _pg.SYNC.begin() as conn:
        doomed = conn.execute(
            text(
                f"DELETE FROM traces WHERE project_id = ANY(:p) AND ts_max < {cutoff} "
                "RETURNING project_id, trace_id"
            ),
            params,
        ).all()
        if not doomed:
            spans = 0
        else:
            spans = (
                conn.execute(
                    text(
                        "DELETE FROM events e USING unnest(CAST(:pp AS text[]), CAST(:tt AS text[])) AS d(p, t) "
                        "WHERE e.project_id = d.p AND e.trace_id = d.t"
                    ),
                    {"pp": [r[0] for r in doomed], "tt": [r[1] for r in doomed]},
                ).rowcount
                or 0
            )
        conn.execute(
            text(f"DELETE FROM scores WHERE project_id = ANY(:p) AND created_at < {cutoff}"), params
        )
    return spans


def expire_all(days: int) -> int:
    """The ClickHouse table TTL's twin: every project, `SPAN_RETENTION_DAYS`. Returns spans removed."""
    if days <= 0:
        return 0
    projects = [r[0] for r in _pg.query_rows("SELECT DISTINCT project_id FROM traces")]
    return delete_expired(projects, days)


def delete_project_rows(project_id: str) -> None:
    """Every span, score and rollup row of one project — the orphan sweep's worker-side delete."""
    with _pg.SYNC.begin() as conn:
        for table in ("events", "scores", "traces"):
            conn.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id})


def last_write_by_project() -> dict[str, datetime]:
    """`{project_id: newest write}` across spans and scores — the orphan sweep's guard. Naive UTC,
    like the ClickHouse twin (`purge_service` compares it with naive timestamps)."""
    out: dict[str, datetime] = {}
    for sql in (
        "SELECT project_id, max(updated_at) FROM traces GROUP BY project_id",
        "SELECT project_id, max(created_at) FROM scores GROUP BY project_id",
    ):
        for pid, ts in _pg.query_rows(sql):
            out[pid] = max(ts, out[pid]) if pid in out else ts
    return out
