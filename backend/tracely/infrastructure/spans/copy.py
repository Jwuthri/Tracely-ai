"""Copy spans + scores from ClickHouse into the Postgres span store, then prove the two agree.

    python -m tracely.infrastructure.spans.copy                 # every project, then --verify
    python -m tracely.infrastructure.spans.copy --project P     # one project
    python -m tracely.infrastructure.spans.copy --verify-only   # compare, copy nothing

Reads ClickHouse the way the product does (FINAL, `is_deleted = 0`, lightweight-deleted rows
masked), so only LIVE data crosses — prod's 89% of dead rows never do. Writes through the same
`writer.write_events` the ingest path uses, so spans and the `traces` rollup land together.
Idempotent: every write is an upsert on a deterministic key, so re-running after a crash, or once
more right before the cutover to pick up the stragglers, converges instead of duplicating.

Verification runs the product's own readers against BOTH stores (ClickHouse `async_reader` vs
Postgres `reads`) for every project and a sample of its threads and traces, and reports every
disagreement. Exit status 1 on any mismatch — do not flip `SPAN_STORE` until it is 0.
Known, accepted differences are listed in design/part2-tracely/14-clickhouse-to-postgres.md §5;
the readers they touch are compared on the parts that did not change.

ponytail: one process, trace batches of 500. Prod is ~66k spans (about a minute); a much bigger
workspace wants a per-month split.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import text

from tracely.infrastructure.clickhouse import async_reader as ch_reads
from tracely.infrastructure.clickhouse.client import get_client
from tracely.infrastructure.clickhouse.events_schema import EVENT_COLUMNS
from tracely.infrastructure.spans import _pg
from tracely.infrastructure.spans import reads as pg_reads
from tracely.infrastructure.spans.score_writer import _COLS as SCORE_COLS
from tracely.infrastructure.spans.score_writer import _UPSERT as SCORE_UPSERT
from tracely.infrastructure.spans.score_writer import _row as score_row
from tracely.infrastructure.spans.writer import write_events

TRACE_BATCH = 500
SCORE_BATCH = 5000
_CH_SCORE_COLS = [c for c in SCORE_COLS if c != "metadata"] + ["metadata"]


# ── copy ──────────────────────────────────────────────────────────────────────


def projects(ch) -> list[str]:
    rows = ch.query(
        "SELECT DISTINCT project_id FROM events FINAL WHERE is_deleted = 0 "
        "UNION DISTINCT SELECT DISTINCT project_id FROM scores FINAL WHERE is_deleted = 0"
    ).result_rows
    return sorted(r[0] for r in rows)


def copy_project(ch, project_id: str, log=print) -> dict[str, int]:
    trace_ids = [
        r[0]
        for r in ch.query(
            "SELECT DISTINCT trace_id FROM events FINAL WHERE project_id = {p:String} AND is_deleted = 0",
            parameters={"p": project_id},
        ).result_rows
    ]
    spans = 0
    for i in range(0, len(trace_ids), TRACE_BATCH):
        batch = trace_ids[i : i + TRACE_BATCH]
        res = ch.query(
            f"SELECT {', '.join(EVENT_COLUMNS)} FROM events FINAL PREWHERE trace_id IN {{t:Array(String)}} "
            "WHERE project_id = {p:String} AND is_deleted = 0",
            parameters={"p": project_id, "t": batch},
        )
        events = [dict(zip(res.column_names, row)) for row in res.result_rows]
        with _pg.SYNC.begin() as conn:  # whole traces per batch, so each rollup sees all its spans
            spans += write_events(conn, events, lock=False)
        log(
            f"  {project_id}: spans {spans} ({min(i + TRACE_BATCH, len(trace_ids))}/{len(trace_ids)} traces)"
        )
    scores = 0
    offset = 0
    while True:
        res = ch.query(
            f"SELECT {', '.join(_CH_SCORE_COLS)} FROM scores FINAL WHERE project_id = {{p:String}} "
            "AND is_deleted = 0 ORDER BY id LIMIT {n:UInt32} OFFSET {o:UInt32}",
            parameters={"p": project_id, "n": SCORE_BATCH, "o": offset},
        )
        rows = [score_row(**dict(zip(res.column_names, r))) for r in res.result_rows]
        if not rows:
            break
        with _pg.SYNC.begin() as conn:
            conn.execute(text(SCORE_UPSERT), rows)
        scores += len(rows)
        offset += SCORE_BATCH
    log(f"  {project_id}: scores {scores}")
    return {"traces": len(trace_ids), "spans": spans, "scores": scores}


# ── verify ────────────────────────────────────────────────────────────────────


def _blank(v: Any) -> bool:
    return v is None or v == ""


def same(a: Any, b: Any, path: str = "") -> list[str]:
    """Differences between a ClickHouse result and a Postgres one. Tolerates what is not a
    difference: None vs '' (ClickHouse Nullable text vs a NULL), int vs float of equal value,
    float noise, and ±1 on integer-rounded percentiles."""
    if _blank(a) and _blank(b):
        return []
    if path.endswith(".metadata") and isinstance(a, dict) and isinstance(b, dict):
        # A thread whose turns disagree on a metadata key: ClickHouse keeps an arbitrary one,
        # Postgres the latest turn's. The KEYS must match; the values are not comparable.
        return [] if set(a) == set(b) else [f"{path}: keys {sorted(a)} vs {sorted(b)}"]
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b), key=str):
            out += same(a.get(k), b.get(k), f"{path}.{k}")
        return out
    if isinstance(a, (list, tuple, set)) and isinstance(b, (list, tuple, set)):
        if isinstance(a, set) or isinstance(b, set):
            a, b = sorted(a, key=str), sorted(b, key=str)
        if len(a) != len(b):
            return [f"{path}: {len(a)} vs {len(b)} items"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += same(x, y, f"{path}[{i}]")
        return out
    if isinstance(a, bool) or isinstance(b, bool):
        return [] if bool(a) == bool(b) else [f"{path}: {a!r} vs {b!r}"]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9):
            return []
        if any(k in path for k in ("p50", "p95", "p99", "ttft")) and abs(a - b) <= 1:
            return []
        # ClickHouse dates a trace by EACH span's day, so one crossing midnight UTC counts on both
        # days; Postgres dates it once, by its first span. Off by a trace or two per day, no more.
        if "daily" in path:
            if abs(a - b) <= max(1, 0.01 * max(abs(a), abs(b))):
                return []
        return [f"{path}: {a!r} vs {b!r}"]
    if isinstance(a, datetime) and isinstance(b, datetime):
        return [] if a.replace(tzinfo=None) == b.replace(tzinfo=None) else [f"{path}: {a} vs {b}"]
    return [] if a == b else [f"{path}: {str(a)[:80]!r} vs {str(b)[:80]!r}"]


def _by(rows: list[dict], key: str) -> dict:
    return {r[key]: r for r in rows}


async def verify_project(project_id: str, samples: int = 25) -> list[str]:
    """Every mismatch between the two stores for one project (empty list = they agree)."""
    problems: list[str] = []

    async def check(name: str, ch_call, pg_call, key: str | None = None, sort: bool = False):
        try:
            a, b = await ch_call, await pg_call
        except Exception as exc:  # noqa: BLE001 — a reader that crashes IS the finding
            problems.append(f"{name}: raised {exc!r}")
            return
        if key:
            a, b = _by(a, key), _by(b, key)
        elif sort:
            a, b = sorted(a, key=str), sorted(b, key=str)
        problems.extend(same(a, b, name)[:5])

    p = project_id
    await check("stats_counts", ch_reads.stats_counts(p), pg_reads.stats_counts(p))
    await check(
        "trace_failure_totals", ch_reads.trace_failure_totals(p), pg_reads.trace_failure_totals(p)
    )
    await check(
        "traces_in_window", ch_reads.traces_in_window(p, 7), pg_reads.traces_in_window(p, 7)
    )
    await check(
        "daily_trace_failures",
        ch_reads.daily_trace_failures(p, 30),
        pg_reads.daily_trace_failures(p, 30),
    )
    await check("ops_metrics", ch_reads.ops_metrics(p, 30), pg_reads.ops_metrics(p, 30))
    await check(
        "traces_overview",
        ch_reads.traces_overview(p, 200),
        pg_reads.traces_overview(p, 200),
        key="trace_id",
    )
    await check("trace_agent_ids", ch_reads.trace_agent_ids(p), pg_reads.trace_agent_ids(p))
    await check(
        "agent_ids_with_spans", ch_reads.agent_ids_with_spans(p), pg_reads.agent_ids_with_spans(p)
    )
    await check(
        "evaluator_catalog",
        ch_reads.evaluator_catalog(p),
        pg_reads.evaluator_catalog(p),
        key="name",
    )
    await check("evaluator_cost", ch_reads.evaluator_cost(p), pg_reads.evaluator_cost(p))
    await check(
        "sessions_overview.count",
        ch_reads.sessions_overview(p, 0, 0, count_only=True),
        pg_reads.sessions_overview(p, 0, 0, count_only=True),
    )
    await check(
        "sessions_overview.count(failing)",
        ch_reads.sessions_overview(p, 0, 0, failing=True, count_only=True),
        pg_reads.sessions_overview(p, 0, 0, failing=True, count_only=True),
    )
    await check(
        "sessions_overview.page",
        ch_reads.sessions_overview(p, 100, 0),
        pg_reads.sessions_overview(p, 100, 0),
    )
    threads = [r["thread"] for r in await pg_reads.sessions_overview(p, samples, 0)]
    for th in threads:
        await check(
            f"session_turns[{th}]",
            ch_reads.session_turns(p, th),
            pg_reads.session_turns(p, th),
            key="trace_id",
        )
        await check(
            f"thread_turn_count[{th}]",
            ch_reads.thread_turn_count(p, th),
            pg_reads.thread_turn_count(p, th),
        )
        await check(
            f"thread_spans_full[{th}]",
            ch_reads.thread_spans_full(p, th),
            pg_reads.thread_spans_full(p, th),
            key="span_id",
        )
        await check(
            f"conversation_scores[{th}]",
            ch_reads.conversation_scores(p, th),
            pg_reads.conversation_scores(p, th),
            sort=True,
        )
    traces = [r["trace_id"] for r in await pg_reads.traces_overview(p, samples)]
    for t in traces:
        await check(
            f"trace_spans[{t}]",
            ch_reads.trace_spans(p, t),
            pg_reads.trace_spans(p, t),
            key="span_id",
        )
        await check(
            f"trace_scores[{t}]",
            ch_reads.trace_scores(p, t, ""),
            pg_reads.trace_scores(p, t, ""),
            sort=True,
        )
    return problems


def _counts_ch(ch, project_id: str) -> dict[str, int]:
    q = lambda sql: int(ch.query(sql, parameters={"p": project_id}).result_rows[0][0])  # noqa: E731
    return {
        "spans": q(
            "SELECT count() FROM events FINAL WHERE project_id = {p:String} AND is_deleted = 0"
        ),
        "traces": q(
            "SELECT uniqExact(trace_id) FROM events FINAL WHERE project_id = {p:String} AND is_deleted = 0"
        ),
        "scores": q(
            "SELECT count() FROM scores FINAL WHERE project_id = {p:String} AND is_deleted = 0"
        ),
    }


def _counts_pg(project_id: str) -> dict[str, int]:
    q = lambda sql: int(_pg.query_rows(sql, {"p": project_id})[0][0])  # noqa: E731
    return {
        "spans": q("SELECT count(*) FROM events WHERE project_id = :p"),
        "traces": q("SELECT count(*) FROM traces WHERE project_id = :p"),
        "scores": q("SELECT count(*) FROM scores WHERE project_id = :p"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", action="append", help="only this project (repeatable)")
    ap.add_argument("--verify-only", action="store_true", help="compare the stores, copy nothing")
    ap.add_argument("--no-verify", action="store_true", help="copy, skip the comparison")
    ap.add_argument("--samples", type=int, default=25, help="threads/traces compared per project")
    args = ap.parse_args(argv)

    ch = get_client()
    todo = args.project or projects(ch)
    print(f"{len(todo)} project(s)")
    if not args.verify_only:
        for p in todo:
            print(f"copy {p}: {copy_project(ch, p)}")
    if args.no_verify:
        return 0
    # One event loop for every project: both stores' async pools are bound to the loop they
    # were opened on, so a fresh `asyncio.run` per project would reuse dead connections.
    failed = asyncio.run(_verify_all(ch, todo, args.samples))
    print("FAILED — do not switch SPAN_STORE" if failed else "OK — stores agree")
    return 1 if failed else 0


async def _verify_all(ch, todo: list[str], samples: int) -> bool:
    failed = False
    for p in todo:
        a, b = _counts_ch(ch, p), _counts_pg(p)
        counts_ok = a == b
        problems = await verify_project(p, samples)
        failed |= (not counts_ok) or bool(problems)
        print(
            f"verify {p}: counts {'ok' if counts_ok else f'MISMATCH clickhouse={a} postgres={b}'}, "
            f"{len(problems)} reader mismatch(es)"
        )
        for line in problems[:40]:
            print(f"    {line}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
