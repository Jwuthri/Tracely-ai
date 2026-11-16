"""The Postgres span store against a real Postgres: write path, the `traces` rollup, the verdict
policy and the readers' shapes. ClickHouse-vs-Postgres parity is what
`python -m tracely.infrastructure.spans.copy --verify-only` proves on real data; this pins the rules.

Needs a scratch database — `TRACELY_TEST_PG_URL=postgresql://user:pass@host:5432/db` (CI sets it);
skipped otherwise, so the local suite stays infra-free. The tables are dropped and recreated.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

URL = os.environ.get("TRACELY_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRACELY_TEST_PG_URL not set")

P = "p-test"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(scope="module")
def store():
    from sqlalchemy import create_engine, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from tracely.infrastructure.spans import _pg

    base = URL.split("://", 1)[1]
    sync = create_engine(f"postgresql+psycopg://{base}", poolclass=NullPool)
    ddl = _load_upgrade_sql()
    with sync.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS traces, scores, events"))
        conn.execute(text(ddl))
    old = _pg.SYNC, _pg.ASYNC
    # NullPool: every pytest-asyncio test runs its own loop, and asyncpg connections are loop-bound
    _pg.SYNC = sync
    _pg.ASYNC = create_async_engine(f"postgresql+asyncpg://{base}", poolclass=NullPool)
    try:
        yield _pg
    finally:
        _pg.SYNC, _pg.ASYNC = old


def _load_upgrade_sql() -> str:
    """The 0040 migration's DDL, without needing Alembic's runtime."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0040_span_store.py"
    ).read_text()
    tree = ast.parse(src)
    env: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("_T", "UPGRADE"):
            exec(compile(ast.Module([node], []), "0040", "exec"), env)  # noqa: S102
    return env["UPGRADE"]


def _span(trace, span, **kw):
    ev = dict(project_id=P, trace_id=trace, span_id=span, start_time=NOW, name="s", type="SPAN")
    ev.update(kw)
    return ev


class _Res:
    def __init__(self, name, verdict, level="TRACE"):
        self.name, self.level, self.verdict, self.data_type = name, level, verdict, "BOOLEAN"
        self.value, self.string_value, self.target_span_id, self.comment, self.usage = (
            0.0,
            "",
            "",
            "",
            None,
        )


def _seed(store):
    from tracely.infrastructure.spans import pg_deletes, writer
    from tracely.infrastructure.spans.score_writer import ScoreWriter

    pg_deletes.delete_project_rows(P)
    # T1: a two-turn conversation's first turn, a normal root + a GENERATION delivered late
    writer.insert_events(
        [
            _span(
                "T1",
                "r",
                name="agent.run",
                type="CHAIN",
                input="refund please",
                end_time=NOW + timedelta(seconds=2),
                is_app_root=True,
                agent_id="a",
                conversation_id="C",
                metadata={"tracely.metadata.customer": "acme"},
            )
        ]
    )
    gen = _span(
        "T1",
        "g",
        parent_span_id="r",
        type="GENERATION",
        model_id="m",
        input="",
        output="sure",
        start_time=NOW + timedelta(milliseconds=10),
        end_time=NOW + timedelta(seconds=1),
        step_name="judge",
        usage_details={"input": 10, "output": 5},
        cost_details={"input": 0.25},
    )
    writer.insert_events([gen, gen])  # late + duplicated inside one batch
    # T2: second turn, ROOTLESS (driven through traceparent): app-root span only, an error
    writer.insert_events(
        [
            _span(
                "T2",
                "x",
                parent_span_id="ext",
                is_app_root=True,
                agent_id="a",
                conversation_id="C",
                input="thanks",
                level="ERROR",
                status_message="ValueError: kaboom",
                start_time=NOW + timedelta(minutes=1),
            )
        ]
    )
    # T3: Tracely's own eval run — never listed
    writer.insert_events([_span("T3", "e", internal_kind="eval", input="judge prompt")])
    sw = ScoreWriter()
    sw.write_eval_scores(
        P, "T1", "T1", [_Res("tool_ok", "FAIL"), _Res("quality", "FAIL")], thread_id="C"
    )
    sw.write_eval_scores(
        P, "T1", "T1", [_Res("tool_ok", "FAIL")], thread_id="C"
    )  # re-grade: same id


def test_rollup_recomputes_from_spans(store):
    _seed(store)
    rows = {
        r["trace_id"]: r
        for r in store.query("SELECT * FROM traces WHERE project_id = :p", {"p": P})
    }
    t1, t2 = rows["T1"], rows["T2"]
    assert (t1["spans"], t1["tokens"], t1["model_id"], t1["thread_id"]) == (2, 15, "m", "C")
    assert t1["cost"] == pytest.approx(0.25) and t1["latency_ms"] == 2000
    assert (t1["root_input"], t1["has_input"], t1["has_output"]) == ("refund please", True, False)
    assert t1["metadata"] == {"tracely.metadata.customer": "acme"}
    # no parentless span: the app-root span is the root (the case `_TRACE_AGENT` exists for)
    assert (t2["root_input"], t2["agent_id"], t2["has_error"], t2["first_error"]) == (
        "thanks",
        "a",
        True,
        "ValueError: kaboom",
    )
    # '' is stored as NULL, so `has_input` never reads text
    assert store.query_rows("SELECT input IS NULL FROM events WHERE span_id = 'g'")[0][0] is True
    assert (
        store.query_rows("SELECT count(*) FROM scores WHERE project_id = :p", {"p": P})[0][0] == 2
    )


async def test_readers_apply_the_verdict_policy_and_hide_internal_runs(store):
    from tracely.infrastructure.spans import reads

    _seed(store)
    stats = await reads.stats_counts(P)
    assert stats == {"traces": 2, "spans": 3, "failing_traces": 1, "auto_failures": 1}
    # both FAILs advisory -> nothing fails (`name <> ALL(:adv)`), an empty list excludes nothing
    assert (await reads.stats_counts(P, ["tool_ok", "quality"]))["auto_failures"] == 0
    page = await reads.sessions_overview(P, 50, 0)
    assert [r["thread"] for r in page] == ["C"]
    th = page[0]
    assert (th["turns"], th["failing"], th["first_input"], th["metadata"]) == (
        2,
        1,
        "refund please",
        {"customer": "acme"},
    )
    assert (
        isinstance(th["last_ts"], datetime) and th["last_ts"].tzinfo is None
    )  # naive UTC, like ClickHouse
    assert await reads.sessions_overview(P, 50, 0, count_only=True) == 1
    assert await reads.sessions_overview(P, 50, 0, include_internal=True, count_only=True) == 2
    assert await reads.sessions_overview(P, 50, 0, q="REFUND", count_only=True) == 1
    assert (
        await reads.sessions_overview(P, 50, 0, q="100%", count_only=True) == 0
    )  # wildcard escaped
    turns = await reads.session_turns(P, "C")
    assert [(t["trace_id"], t["failing"]) for t in turns] == [("T1", 1), ("T2", 0)]
    assert [s["span_id"] for s in await reads.trace_spans(P, "T1")] == ["r", "g"]
    assert await reads.thread_turn_count(P, "C") == 2
    ops = await reads.ops_metrics(P, 30)
    assert ops["summary"]["traces"] == 2 and ops["models"][0]["model"] == "m"


def test_deletes_keep_the_rollup_in_step(store):
    from tracely.infrastructure.spans import pg_deletes, writer
    from tracely.infrastructure.spans.score_writer import ScoreWriter

    _seed(store)
    pg_deletes.delete_trace(P, "T1", ["other"])  # the root (no group) goes, the `judge` span stays
    assert store.query_rows("SELECT spans, root_input FROM traces WHERE trace_id = 'T1'")[0] == (
        1,
        None,
    )
    pg_deletes.delete_trace(P, "T1")
    assert store.query_rows("SELECT count(*) FROM traces WHERE trace_id = 'T1'")[0][0] == 0
    assert ScoreWriter().retract_eval_scores(P, ["quality"], trace_id="T1", keep_ids=set()) == 1
    writer.insert_events([_span("OLD", "o", start_time=NOW - timedelta(days=100))])
    assert pg_deletes.delete_expired([P], 90) == 1
    assert store.query_rows("SELECT count(*) FROM traces WHERE trace_id = 'OLD'")[0][0] == 0
