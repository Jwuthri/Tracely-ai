"""GET /api/evaluations/prompt — the judge prompt behind one score cell, and the delete scoping
that keeps it readable.

The eval recording is shared by every column graded at the same (subject, level), so these two
things have to hold together: the reader filters that trace down to ONE column (and, at step
level, one span), and the writer replaces only the groups it re-recorded.
"""

from __future__ import annotations

import pytest

from tracely.api.routers import evaluations
from tracely.domain import introspection
from tracely.infrastructure.clickhouse import deletes


def _span(step_name: str, type_: str, name: str = "", input_: str = "") -> dict:
    return {
        "step_name": step_name, "type": type_, "name": name or step_name,
        "model_id": "openai/gpt-x", "input": input_, "output": "{}",
    }


@pytest.fixture
def spans(monkeypatch):
    rows: list[dict] = []

    async def fake(project_id: str, trace_id: str) -> list[dict]:
        return rows

    monkeypatch.setattr(evaluations.async_reader, "trace_spans", fake)
    return rows


async def test_returns_only_this_evaluators_llm_calls(spans):
    spans += [
        _span("", "CHAIN", "eval · msg · 2 column(s)"),
        _span("on_topic", "CHAIN"),  # the evaluator's own group span, not an LLM call
        _span("on_topic", "GENERATION", "gpt", "ON-TOPIC PROMPT"),
        _span("intent", "GENERATION", "gpt", "INTENT PROMPT"),
    ]
    out = await evaluations.evaluation_prompt("tr-1", "msg", "on_topic", project_id="p1")
    assert [s["input"] for s in out["steps"]] == ["ON-TOPIC PROMPT"]
    assert out["trace_id"] == introspection.stable_trace_id("p1", introspection.EVAL, "tr-1", "msg")


async def test_step_narrows_to_one_span_then_falls_back(spans):
    spans += [
        _span("tool_ok", "GENERATION", "TOOL search", "PROMPT A"),
        _span("tool_ok", "GENERATION", "TOOL refund", "PROMPT B"),
    ]
    out = await evaluations.evaluation_prompt("tr-1", "step", "tool_ok", step="TOOL refund", project_id="p1")
    assert [s["input"] for s in out["steps"]] == ["PROMPT B"]
    # A label that matches nothing shows every call rather than an empty panel.
    out = await evaluations.evaluation_prompt("tr-1", "step", "tool_ok", step="TOOL gone", project_id="p1")
    assert len(out["steps"]) == 2


async def test_clips_a_huge_prompt(spans):
    spans.append(_span("on_topic", "GENERATION", "gpt", "x" * (evaluations.PROMPT_CHARS + 500)))
    out = await evaluations.evaluation_prompt("tr-1", "msg", "on_topic", project_id="p1")
    assert out["steps"][0]["input"].endswith("… (truncated)")
    assert len(out["steps"][0]["input"]) < evaluations.PROMPT_CHARS + 50


def test_delete_trace_scopes_to_the_recorded_groups(monkeypatch):
    """A per-column re-run must not erase the sibling columns' prompts from the shared eval trace."""
    sent: list[tuple[str, dict]] = []

    class FakeClient:
        def query(self, sql, parameters=None):
            sent.append((sql, parameters or {}))
            return type("R", (), {"result_rows": [(1,)]})()

        def command(self, sql, parameters=None):
            sent.append((sql, parameters or {}))

    monkeypatch.setattr(deletes, "get_client", lambda: FakeClient())
    deletes.delete_trace("p1", "t1", ["on_topic"])
    sql, params = sent[-1]
    assert "step_name IN" in sql and params["n"] == ["on_topic"]

    sent.clear()
    deletes.delete_trace("p1", "t1")  # no groups → the old whole-trace replace
    assert "step_name" not in sent[-1][0]
