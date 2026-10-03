"""Source-level guards on the two ClickHouse read rules that nothing else can enforce.

Both tables are `ReplacingMergeTree`, so a row is only ever "the current one" once the duplicate
versions have been collapsed — which happens at read time via `FINAL`. `scores` in particular is
partitioned by `created_at`, the WRITE time, so re-grading a trace in a later calendar month writes
the same score `id` into a different partition and background merges can never collapse the two.
`FINAL` is the only thing that reads them as one row (see the comment in `ddl/0002_scores.up.sql`).

Grepping the source is a blunt instrument, but it is the only one available: a missing FINAL is not
a crash, an exception, or a failing query — it is a score silently reported twice, potentially with
the stale verdict first. A test that runs in 5ms beats finding that in production.
"""

from __future__ import annotations

import re
from pathlib import Path

_CH_DIR = Path(__file__).resolve().parents[1] / "tracely" / "infrastructure" / "clickhouse"

# `FROM <table>` not followed by FINAL. Tolerates the quote/space runs the string-concatenated SQL
# leaves between clauses, and an alias in either form (`FROM scores AS s FINAL`, `FROM scores s
# FINAL`) — the alias sits between the table and the keyword.
_UNFINAL = r"FROM\s+{table}\b(?![\s\"']*(?:(?:AS[\s\"']+)?\w+[\s\"']+)?FINAL)"

# `deletes.py` issues lightweight DELETEs (`DELETE FROM events WHERE …`), which take no FINAL, and
# `migrations.py` runs the DDL itself.
_EXEMPT = {"deletes.py", "migrations.py"}


def _offenders(table: str) -> list[str]:
    pattern = re.compile(_UNFINAL.format(table=table), re.IGNORECASE)
    hits: list[str] = []
    for path in sorted(_CH_DIR.rglob("*.py")):
        if path.name in _EXEMPT:
            continue
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]  # a comment mentioning `FROM scores` is not a query
            if pattern.search(code):
                hits.append(f"{path.name}:{i}: {line.strip()}")
    return hits


def test_scores_reads_are_final():
    """Non-negotiable: `scores` duplicates survive on disk across month boundaries, so a read
    without FINAL returns a re-graded score twice."""
    assert _offenders("scores") == []


def test_events_reads_are_final():
    """Same rule for `events`: a span re-delivered by a retrying OTLP exporter is two rows until
    merged, and any `count()` over them reports a span total nothing else in the UI agrees with."""
    assert _offenders("events") == []


# ── which agent a trace belongs to ────────────────────────────────────────────
# Python (`domain.traces.spans.root_span`) and SQL (`async_reader._TRACE_AGENT`) must agree, or the
# traces list's Agent column and the features scoped by that agent point at different agents. The
# case that bit: a run Tracely drove through `traceparent` has no parent-less span at all.


def test_sql_trace_agent_honours_is_app_root_like_python_root_span():
    from tracely.domain.traces.spans import root_span
    from tracely.infrastructure.clickhouse.async_reader import _TRACE_AGENT

    spans = [
        {"span_id": "child", "parent_span_id": "remote", "agent_id": "a1"},
        {"span_id": "root", "parent_span_id": "remote", "is_app_root": True, "agent_id": "a2"},
    ]
    assert root_span(spans)["agent_id"] == "a2"      # Python already handles it
    assert "is_app_root" in _TRACE_AGENT             # …and so must the SQL twin
    assert "agent_id != ''" in _TRACE_AGENT          # plus the any-span fallback


def test_every_root_agent_read_uses_the_shared_expression():
    """A hand-rolled `anyIf(agent_id, parent_span_id = '')` is the drift this prevents: it silently
    attributes traceparent-joined runs to no agent, in whichever read forgot the rule."""
    src = (_CH_DIR / "async_reader.py").read_text()
    body = re.sub(r"_TRACE_AGENT = \([\s\S]*?\n\)\n", "", src, count=1)  # minus the definition
    assert body != src, "the _TRACE_AGENT definition moved — fix this guard"
    assert "anyIf(agent_id, parent_span_id" not in body


def test_the_agent_picker_counts_only_declared_turn_owners():
    """The Agent select must list only agents a turn/conversation was declared under.

    `_TRACE_AGENT`'s last resort is "any span's agent", which files an ORPHAN fragment — a tool or
    chain span whose turn root never reached us — under whatever sub-agent label it carried. The
    threads list drops those as content-less 1-turn threads, so building the picker on
    `_TRACE_AGENT` offered `supervisor`/`agent_faq`-style sub-agents that own nothing selecting
    them can show. Only `is_app_root` is a declaration of ownership."""
    src = (_CH_DIR / "async_reader.py").read_text()
    body = src[src.index("async def trace_agent_ids") :]
    body = body[: body.index("async def", 10)]
    assert "anyIf(agent_id, is_app_root)" in body
    assert "{_TRACE_AGENT}" not in body  # the interpolation, not the prose above it


# ── the threads list's sortable headers ───────────────────────────────────────
# ORDER BY cannot be parameterized, so the sort key is the one piece of this query built by string
# interpolation. These pin the whitelist that keeps it safe, and the tie-break that keeps
# LIMIT/OFFSET paging honest once the sort column has ties (which is the normal case).

from tracely.infrastructure.clickhouse.async_reader import (  # noqa: E402
    SESSION_SORTS,
    session_order_clause,
)


def test_every_sort_key_maps_to_a_known_expression():
    for key in SESSION_SORTS:
        assert session_order_clause(key, "desc").startswith(f"ORDER BY {SESSION_SORTS[key]} DESC")


def test_unknown_sort_falls_back_instead_of_interpolating():
    """A renamed column in a bookmarked URL shows the default order; it never reaches the query."""
    for hostile in ("", "cost", "1; DROP TABLE events", "last_ts DESC, 1", None):
        clause = session_order_clause(hostile, "desc")  # type: ignore[arg-type]
        assert clause == "ORDER BY last_ts DESC, last_ts DESC, thread ASC"


def test_direction_is_two_valued():
    assert "ASC, last_ts DESC" in session_order_clause("tokens", "asc")
    for junk in ("ASC; DELETE", "descending", "", None):
        assert " DESC, last_ts DESC" in session_order_clause("tokens", junk)  # type: ignore[arg-type]


def test_every_sort_is_tie_broken():
    """Without this, page 2 of a duration-sorted list repeats rows from page 1 and drops others."""
    for key in SESSION_SORTS:
        for order in ("asc", "desc"):
            assert ", last_ts DESC" in session_order_clause(key, order)


def test_the_order_is_total():
    """The last tie-break must be a column that is UNIQUE per row, or the order still is not total
    and LIMIT/OFFSET keeps dropping threads.

    This is the regression: `recent` sorts on `last_ts`, so the clause used to end
    "last_ts DESC, last_ts DESC" — a tie-break on the very column being tied. A whole-workspace
    export paged 200 at a time and silently came back short."""
    for key in SESSION_SORTS:
        for order in ("asc", "desc"):
            clause = session_order_clause(key, order)
            assert clause.endswith(", thread ASC")
            assert clause[: -len(", thread ASC")].count("thread") == 0


# ── by-trace reads must PREWHERE ──────────────────────────────────────────────
# `events` is ordered by (project_id, toStartOfMinute(start_time), xxHash32(trace_id), …), so a
# lookup by bare `trace_id`/`conversation_id` can use neither the primary key nor a skip index —
# it reads every row. With `input`/`output`/`metadata` in the SELECT that is the whole table
# decompressed to return one trace. PREWHERE evaluates the filter on that one narrow column first
# and only then materializes the blobs. ClickHouse will not do this for us: with FINAL the move is
# gated behind `optimize_move_to_prewhere_if_final` (default 0), and forcing that on still leaves a
# non-PK column in the WHERE. Measured on prod: 961 MiB -> 11.9 MiB read, 322 MiB -> 8.2 MiB RAM.


def _fn_body(path: Path, name: str) -> str:
    """The function's code with its docstring and comments removed — so a comment that merely
    *mentions* PREWHERE cannot satisfy a guard that the SQL dropped."""
    src = path.read_text()
    body = src[src.index(f"def {name}(") :]
    nxt = re.search(r"\n(?:async def |def |    def )", body[1:])
    body = body[: nxt.start()] if nxt else body
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def test_by_trace_span_reads_use_prewhere():
    """A plain WHERE here is not a bug you can see — it is a query that still returns the right
    spans while reading the entire table, ~30k times a day, until ClickHouse runs out of RAM."""
    for filename, fn in (
        ("trace_reader.py", "read_spans"),
        ("trace_reader.py", "read_thread_spans"),
        ("trace_reader.py", "thread_trace_ids"),
        ("trace_reader.py", "candidate_metrics"),
        ("trace_reader.py", "member_meta"),
        ("async_reader.py", "thread_spans_full"),
        ("async_reader.py", "trace_spans"),
        ("deletes.py", "delete_trace"),  # ran per evaluation; was a full scan without it
    ):
        assert "PREWHERE" in _fn_body(_CH_DIR / filename, fn), f"{filename}:{fn} lost its PREWHERE"


# ── tombstoned spans must not come back ───────────────────────────────────────
# `deletes.delete_trace` replaces a re-recorded internal run by writing a `ReplacingMergeTree`
# tombstone (`is_deleted = 1`) rather than running a `DELETE FROM` mutation — see its docstring for
# why (one mutation per evaluated message rewrote every part of the table and was the single
# largest source of ClickHouse's memory use). FINAL collapses the pair down to the tombstone, so a
# read that does not drop `is_deleted = 1` shows the spans the re-recording was meant to replace.
#
# Tombstones only ever land on internal recordings, so a read already scoped to `internal_kind = ''`
# can never see one. Everything else must say so explicitly.


def _events_queries(path: Path) -> list[tuple[int, str]]:
    """Each `FROM events FINAL` in the file, paired with the query text that follows it."""
    src = path.read_text()
    out = []
    for m in re.finditer(r"FROM events FINAL", src):
        tail = src[m.start() : m.start() + 800]
        cut = tail.find("parameters=")
        out.append((src[: m.start()].count("\n") + 1, tail[:cut] if cut > 0 else tail))
    return out


def test_every_events_read_excludes_tombstones_or_internal_runs():
    offenders = [
        f"{f}:{line}"
        for f in ("trace_reader.py", "async_reader.py")
        for line, q in _events_queries(_CH_DIR / f)
        if "is_deleted" not in q and "internal_kind" not in q and "_REAL" not in q
    ]
    assert offenders == [], (
        "these reads would show spans a re-recording replaced — add `AND is_deleted = 0` "
        f"(in WHERE, never PREWHERE): {offenders}"
    )


def test_is_deleted_is_never_pushed_into_prewhere():
    """PREWHERE runs BEFORE FINAL collapses the row versions, so filtering `is_deleted = 0` there
    discards the tombstone and hands back the very row it was written to hide. The by-trace
    PREWHEREs are safe because `trace_id`/`conversation_id` are identical in both versions."""
    for f in ("trace_reader.py", "async_reader.py"):
        for line, q in _events_queries(_CH_DIR / f):
            pre = q.find("PREWHERE")
            if pre < 0:
                continue
            where = q.find("WHERE", pre + 8)
            assert "is_deleted" not in q[pre : where if where > 0 else len(q)], (
                f"{f}:{line} filters is_deleted in PREWHERE — it must sit in WHERE"
            )



def test_the_threads_list_skips_internal_recordings_in_prewhere():
    """Eval recordings carry the judge prompt (the whole conversation) — the widest rows, and
    ~75% of them in prod. Filtering them in WHERE under FINAL decompressed every one of them on
    each page load of the threads list, which is what made it take seconds."""
    body = _fn_body(_CH_DIR / "async_reader.py", "sessions_overview")
    assert 'internal_prewhere = "" if include_internal else f"PREWHERE {_REAL} "' in body
    assert "FROM events FINAL {internal_prewhere}WHERE" in body
