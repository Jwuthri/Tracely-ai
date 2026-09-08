"""Hermetic-replay bridge for `@observe(as_type="tool")`.

The decorator twin of `call_tool`: inside a `with tracely.fixtures(bundle):` block, an
`@observe`-decorated tool serves the recorded output (or raises ToolError) instead of running — so an
auto-instrumented agent whose tools are merely decorated replays deterministically in CI, with no
`call_tool` rewrite. Outside replay it's a strict no-op (the real function runs)."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracely_sdk as tracely


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    tracely.init(env="prod", instrument=False)
    exp = InMemorySpanExporter()
    tracely._provider.add_span_processor(SimpleSpanProcessor(exp))
    return exp


@pytest.fixture(autouse=True)
def _clear(exporter: InMemorySpanExporter):
    exporter.clear()
    yield
    exporter.clear()


def _span(exporter: InMemorySpanExporter, name: str):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def test_no_fixtures_runs_the_real_tool(exporter: InMemorySpanExporter) -> None:
    ran = []

    @tracely.observe(as_type="tool")
    def get_order_status(order_id: str) -> dict:
        ran.append(order_id)
        return {"status": "live"}

    assert get_order_status("ORD-1") == {"status": "live"}
    assert ran == ["ORD-1"]  # production path: the real fn runs
    assert "tracely.replay.fixture" not in dict(_span(exporter, "get_order_status").attributes)


def test_fixture_served_without_running_the_tool(exporter: InMemorySpanExporter) -> None:
    ran = []

    @tracely.observe(as_type="tool")
    def get_order_status(order_id: str) -> dict:
        ran.append(order_id)
        return {"status": "LIVE — should not happen"}

    bundle = {"version": 2, "tools": [{"name": "get_order_status", "args": {"order_id": "ORD-1"},
                                       "output": {"status": "recorded"}, "error": None}]}
    with tracely.fixtures(bundle):
        out = get_order_status("ORD-1")
    assert out == {"status": "recorded"}  # served from the fixture
    assert ran == []  # the real fn was NEVER called
    assert dict(_span(exporter, "get_order_status").attributes)["tracely.replay.fixture"] is True


def test_recorded_entries_served_in_fifo_order(exporter: InMemorySpanExporter) -> None:
    @tracely.observe(as_type="tool")
    def check_inventory(sku: str) -> dict:
        raise AssertionError("should not run in replay")

    bundle = {"version": 2, "tools": [
        {"name": "check_inventory", "args": None, "output": {"n": 1}, "error": None},
        {"name": "check_inventory", "args": None, "output": {"n": 2}, "error": None},
    ]}
    with tracely.fixtures(bundle):
        assert check_inventory("A") == {"n": 1}
        assert check_inventory("B") == {"n": 2}


def test_args_match_picks_the_right_entry(exporter: InMemorySpanExporter) -> None:
    @tracely.observe(as_type="tool")
    def check_inventory(sku: str) -> dict:
        raise AssertionError("should not run in replay")

    bundle = {"version": 2, "tools": [
        {"name": "check_inventory", "args": {"sku": "COAT"}, "output": {"item": "coat"}, "error": None},
        {"name": "check_inventory", "args": {"sku": "MUG"}, "output": {"item": "mug"}, "error": None},
    ]}
    with tracely.fixtures(bundle):
        assert check_inventory("MUG") == {"item": "mug"}  # matched by args, not order
        assert check_inventory("COAT") == {"item": "coat"}


def test_recorded_error_raises_toolerror_and_marks_span(exporter: InMemorySpanExporter) -> None:
    ran = []

    @tracely.observe(as_type="tool")
    def flaky(x: str) -> dict:
        ran.append(x)
        return {"ok": True}

    bundle = {"version": 2, "tools": [{"name": "flaky", "args": None, "output": None,
                                       "error": "upstream timeout"}]}
    with tracely.fixtures(bundle), pytest.raises(tracely.ToolError, match="upstream timeout"):
        flaky("x")
    assert ran == []  # error replayed without running the real fn
    assert _span(exporter, "flaky").status.status_code.name == "ERROR"


def test_unrecorded_tool_raises_in_recorded_mode(exporter: InMemorySpanExporter) -> None:
    ran = []

    @tracely.observe(as_type="tool")
    def other_tool(x: str) -> str:
        ran.append(x)
        return "live-result"

    # bundle has a DIFFERENT tool — other_tool has no recorded entry. Recorded mode is strict:
    # the real fn must NOT run; only the legacy lenient mode falls through (and reports it).
    bundle = {"version": 2, "tools": [{"name": "some_other", "args": None, "output": "x", "error": None}]}
    with tracely.fixtures(bundle), pytest.raises(tracely.ReplayError):
        other_tool("y")
    assert ran == []
    with tracely.fixtures(bundle, strict=False) as rep:
        assert other_tool("y") == "live-result"
    assert ran == ["y"] and rep.live == ["tools:other_tool"]


def test_non_tool_observe_is_not_bridged(exporter: InMemorySpanExporter) -> None:
    """Only as_type="tool" consults fixtures; a generation/agent span always runs (its name is the
    function, not a model, so it can't be keyed safely)."""
    ran = []

    @tracely.observe(as_type="generation")
    def gen(prompt: str) -> str:
        ran.append(prompt)
        return "real"

    bundle = {"version": 2, "tools": [{"name": "gen", "args": None, "output": "nope", "error": None}]}
    with tracely.fixtures(bundle):
        assert gen("hi") == "real"
    assert ran == ["hi"]


async def test_async_tool_bridges(exporter: InMemorySpanExporter) -> None:
    ran = []

    @tracely.observe(as_type="tool")
    async def afetch(q: str) -> dict:
        ran.append(q)
        return {"status": "live"}

    bundle = {"version": 2, "tools": [{"name": "afetch", "args": None, "output": {"status": "recorded"}, "error": None}]}
    with tracely.fixtures(bundle):
        out = await afetch("q")
    assert out == {"status": "recorded"} and ran == []
