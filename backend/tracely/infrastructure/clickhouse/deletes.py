"""ClickHouse deletes for the API — the write twin of `async_reader` (same rule: no SQL in routers).

Conversation deletion and the whole-project wipe live here. Uses lightweight `DELETE FROM` (GA
since CH 23.3): rows are masked from every read immediately and dropped on the next merge, so the
UI reflects a delete on the next fetch without waiting for a mutation.
"""

from __future__ import annotations

from tracely.infrastructure.clickhouse.client import get_async_client, get_client


def delete_trace(project_id: str, trace_id: str) -> None:
    """Drop one trace's spans, synchronously — the worker's half of this module.

    Used to *replace* a re-recorded internal run (`Recording.stable`): the recording keeps its
    trace id, so the previous spans have to go before the new ones land.

    Reads before it deletes, because the common case is that there is nothing there — a first
    grading. ClickHouse runs a mutation for a `DELETE` that matches no rows just the same, and one
    mutation per evaluated message is the kind of thing that quietly wrecks a table.
    """
    client = get_client()
    params = {"p": project_id, "t": trace_id}
    res = client.query(
        "SELECT count() FROM events WHERE project_id = {p:String} AND trace_id = {t:String}",
        parameters=params,
    )
    if not res.result_rows or not int(res.result_rows[0][0]):
        return
    client.command(
        "DELETE FROM events WHERE project_id = {p:String} AND trace_id = {t:String}",
        parameters=params,
    )


async def delete_threads(project_id: str, threads: list[str]) -> int:
    """Delete every trace in these conversation threads, plus their scores. Returns traces removed.

    A "thread" is `conversation_id` when the traces carry one, else the trace_id itself (the
    definition `sessions_overview` groups by), so both forms are matched. Conversation-level
    scores hang off `session_id = thread`, turn/span scores off `trace_id`.

    ponytail: leaves the raw OTLP blobs (keyed per ingest batch, not per trace) and any
    cluster/case rows that reference these traces — cases are promoted regression fixtures with
    their own S3 bundle, and clusters rebuild. Add a cascade when retention/GDPR needs one.
    """
    if not threads:
        return 0
    client = await get_async_client()
    res = await client.query(
        "SELECT DISTINCT trace_id FROM events WHERE project_id = {p:String} "
        "AND (conversation_id IN {t:Array(String)} OR trace_id IN {t:Array(String)})",
        parameters={"p": project_id, "t": threads},
    )
    trace_ids = [r[0] for r in res.result_rows]
    if not trace_ids:
        return 0
    params = {"p": project_id, "ids": trace_ids, "t": threads}
    await client.command(
        "DELETE FROM events WHERE project_id = {p:String} AND trace_id IN {ids:Array(String)}",
        parameters=params,
    )
    await client.command(
        "DELETE FROM scores WHERE project_id = {p:String} "
        "AND (trace_id IN {ids:Array(String)} OR session_id IN {t:Array(String)})",
        parameters=params,
    )
    return len(trace_ids)


async def delete_project_events(project_id: str) -> dict[str, int]:
    """Delete every span and score in a project. Returns the row counts removed.

    Counted before deleting (lightweight DELETE reports no rowcount), so the numbers are what the
    UI shows back — a concurrent ingest could land a span between the count and the delete, which
    is fine: the next wipe gets it.

    ponytail: leaves the raw OTLP blobs in S3, same as `delete_threads` — they're keyed per ingest
    batch, not per project-scan, and re-ingest is idempotent. Empty the bucket if you need it gone.
    """
    client = await get_async_client()
    counts: dict[str, int] = {}
    for table in ("events", "scores"):
        res = await client.query(
            f"SELECT count() FROM {table} WHERE project_id = {{p:String}}",
            parameters={"p": project_id},
        )
        counts[table] = int(res.result_rows[0][0]) if res.result_rows else 0
        if counts[table]:
            await client.command(
                f"DELETE FROM {table} WHERE project_id = {{p:String}}",
                parameters={"p": project_id},
            )
    return {k: v for k, v in counts.items() if v}


def delete_expired(project_ids: list[str], days: int) -> int:
    """Drop spans + scores older than `days` for these projects. Returns spans removed.

    The plan's retention window, enforced nightly (`tracely.enforce_retention`) because the table
    TTL is one global expression and a plan lives in Postgres. Counted first, same reason as
    `delete_project_events`: a lightweight DELETE reports no rowcount, and a mutation that matches
    nothing is still a mutation — this runs over every free workspace, every night.

    `scores` is cut on `created_at` (its own TTL column), so a score written today for a span from
    last month survives a few extra days. ponytail: cheap and self-correcting — the next sweep
    takes it — and the alternative is joining scores to events nightly.
    """
    if not project_ids or days <= 0:
        return 0
    client = get_client()
    params = {"p": project_ids, "d": days}
    res = client.query(
        "SELECT count() FROM events WHERE project_id IN {p:Array(String)} "
        "AND start_time < now() - INTERVAL {d:UInt32} DAY",
        parameters=params,
    )
    spans = int(res.result_rows[0][0]) if res.result_rows else 0
    if not spans:
        return 0
    client.command(
        "DELETE FROM events WHERE project_id IN {p:Array(String)} "
        "AND start_time < now() - INTERVAL {d:UInt32} DAY",
        parameters=params,
    )
    client.command(
        "DELETE FROM scores WHERE project_id IN {p:Array(String)} "
        "AND created_at < now() - INTERVAL {d:UInt32} DAY",
        parameters=params,
    )
    return spans
