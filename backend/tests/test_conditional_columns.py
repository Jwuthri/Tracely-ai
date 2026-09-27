"""Conditional columns (`config.run_if`): run a column only when its dependencies came out a
certain way — e.g. a Jev intent column gates an LLM explanation column."""

from __future__ import annotations

import contextlib

import httpx
import pytest

from tracely.config import settings
from tracely.domain.evaluation import conditions
from tracely.domain.evaluation.evaluators.base import CFG_DEPENDENCIES, RUN, SPAN
from tracely.domain.evaluation.evaluators.llm_judge import LLMJudgeEvaluator
from tracely.domain.evaluation.results import RunContext
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.llm import provider

INTENT = "tracely.run.intent"
LABEL_REFUND = [{"column": INTENT, "field": "label", "op": "in", "values": ["refund"]}]


@pytest.fixture(autouse=True)
def or_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")


def _span(**kw) -> dict:
    base = {
        "span_id": "root", "parent_span_id": "", "type": "AGENT", "name": "agent", "level": "DEFAULT",
        "status_message": "", "start_time": None, "end_time": None, "agent_id": "a",
        "agent_run_id": "r", "turn_id": "", "step_id": "", "model_id": "m",
        "input": "I want my money back", "output": "Refund issued.", "tool_call_names": [],
        "trace_id": "t1", "is_app_root": 1, "conversation_id": "c1",
    }
    base.update(kw)
    return base


def _judge(level=RUN, name="explain") -> LLMJudgeEvaluator:
    ev = LLMJudgeEvaluator()
    ev.level, ev.score_name = level, name
    return ev


def _ctx(spans):
    return RunContext("p", "t1", "r", spans, root_span(spans), thread_id="c1")


# ── the condition language ───────────────────────────────────────────────────


@pytest.mark.parametrize("cond, payload, expected", [
    ({"field": "label", "op": "in", "values": ["refund"]}, {"label": "refund"}, True),
    ({"field": "label", "op": "in", "values": ["refund"]}, {"label": "greeting"}, False),
    ({"field": "label", "op": "not_in", "values": ["greeting"]}, {"label": "refund"}, True),
    # a multi-label column's fired set is comma-joined; ANY label matches
    ({"field": "label", "op": "in", "values": ["complaint"]}, {"label": "refund, complaint"}, True),
    ({"field": "label", "op": "in", "values": ["complaint"]}, {"label": "none"}, False),
    ({"field": "verdict", "op": "in", "values": ["FAIL"]}, {"verdict": "FAIL"}, True),
    ({"field": "verdict", "op": "in", "values": ["NONE"]}, {"value": 0.2}, True),  # informational
    ({"field": "value", "op": "lt", "value": 0.5}, {"value": 0.2}, True),
    ({"field": "value", "op": "gte", "value": 0.5}, {"value": 0.2}, False),
    # a json column's first string field reads as its label
    ({"field": "label", "op": "in", "values": ["complaint"]}, {"intent": "complaint", "score": 1}, True),
])
def test_condition_semantics(cond, payload, expected):
    ok, _ = conditions.evaluate([{"column": "c", **cond}], {"c": payload})
    assert ok is expected


def test_a_missing_dependency_result_means_not_run():
    ok, why = conditions.evaluate(LABEL_REFUND, {})
    assert not ok and "has no result" in why


def test_all_conditions_must_hold():
    run_if = [*LABEL_REFUND, {"column": "q", "field": "verdict", "op": "in", "values": ["FAIL"]}]
    assert conditions.evaluate(run_if, {INTENT: {"label": "refund"}, "q": {"verdict": "FAIL"}})[0]
    ok, why = conditions.evaluate(run_if, {INTENT: {"label": "refund"}, "q": {"verdict": "PASS"}})
    assert not ok and why == "Not run: q verdict is PASS (runs when verdict is FAIL)."


@pytest.mark.parametrize("run_if, needle", [
    ([], "non-empty list"),
    ([{"field": "label", "op": "in", "values": ["x"]}], "needs `column`"),
    ([{"column": "c", "field": "mood", "op": "in", "values": ["x"]}], "field must be one of"),
    ([{"column": "c", "field": "value", "op": "in", "value": 1}], "op must be one of"),
    ([{"column": "c", "field": "value", "op": "gt", "value": "high"}], "must be a number"),
    ([{"column": "c", "field": "verdict", "op": "in", "values": ["MAYBE"]}], "PASS"),
    ([{"column": "c", "field": "label", "op": "in", "values": []}], "non-empty list of strings"),
])
def test_validation(run_if, needle):
    assert needle in (conditions.validate(run_if) or "")


# ── the gate in the judge ────────────────────────────────────────────────────


def _llm_text(monkeypatch, seen: list):
    def fake(prompt, *, response_format, system_prompt=None, **_):
        seen.append(prompt)
        return response_format(text="The user wanted a refund and got one.", reason="clear")

    monkeypatch.setattr(provider, "run_structured_agent", fake)


EXPLAIN = {"output_type": "text", "model": "openai/gpt-6-luna", "prompt": "Explain the outcome.",
           "depends_on": [INTENT], "run_if": LABEL_REFUND}


def test_the_gate_runs_the_column_when_the_condition_holds(monkeypatch):
    seen: list = []
    _llm_text(monkeypatch, seen)
    cfg = {**EXPLAIN, CFG_DEPENDENCIES: {INTENT: [{"span_id": "", "payload": {"label": "refund", "value": 0.97}}]}}
    [r] = _judge().run(_ctx([_span()]), cfg)
    assert r.string_value.startswith("The user wanted a refund")
    # the dependent reads the label as a field, not buried in prose
    assert '"label": "refund"' in seen[0]


def test_the_gate_writes_a_neutral_not_run_otherwise(monkeypatch):
    seen: list = []
    _llm_text(monkeypatch, seen)
    cfg = {**EXPLAIN, CFG_DEPENDENCIES: {INTENT: [{"span_id": "", "payload": {"label": "greeting"}}]}}
    [r] = _judge().run(_ctx([_span()]), cfg)
    assert seen == []  # no LLM spend
    assert (r.verdict, r.data_type, r.string_value) == ("", "TEXT", "Not run")
    assert r.comment == f"Not run: {INTENT} label is greeting (runs when label is refund)."


def test_step_level_gates_each_step_on_its_own_prerequisite(monkeypatch):
    seen: list = []
    _llm_text(monkeypatch, seen)
    spans = [
        _span(),
        _span(span_id="s1", parent_span_id="root", type="TOOL", name="a", is_app_root=0),
        _span(span_id="s2", parent_span_id="root", type="TOOL", name="b", is_app_root=0),
    ]
    run_if = [{"column": "tool.ok", "field": "verdict", "op": "in", "values": ["FAIL"]}]
    cfg = {"output_type": "text", "model": "openai/gpt-6-luna", "prompt": "Why did it fail?",
           "span_types": ["TOOL"], "depends_on": ["tool.ok"], "run_if": run_if,
           CFG_DEPENDENCIES: {"tool.ok": [{"span_id": "s1", "payload": {"verdict": "PASS"}},
                                          {"span_id": "s2", "payload": {"verdict": "FAIL"}}]}}
    out = {r.target_span_id: r for r in _judge(SPAN).run(_ctx(spans), cfg)}
    assert out["s1"].string_value == "Not run" and out["s2"].string_value.startswith("The user")
    assert len(seen) == 1


def test_advanced_templates_read_dependencies(monkeypatch):
    seen: list = []
    _llm_text(monkeypatch, seen)
    cfg = {**EXPLAIN, "is_advanced": True,
           "prompt": "Intent verdicts:\n@DEPENDENCIES\n\nAnswer:\n@CURRENT_MESSAGE.output",
           CFG_DEPENDENCIES: {INTENT: [{"span_id": "", "payload": {"label": "refund"}}]}}
    _judge().run(_ctx([_span()]), cfg)
    assert f'- {INTENT}: {{"label": "refund"}}' in seen[0]


# ── end to end: Jev intent → gated LLM explanation, through the real dispatcher ─


def test_jev_intent_gates_an_llm_explanation_end_to_end(monkeypatch):
    from tracely.services import evaluation_service
    from tracely.services.evaluation_service import EvaluationService

    monkeypatch.setattr(evaluation_service, "record", lambda *a, **k: contextlib.nullcontext(None))
    monkeypatch.setattr(provider, "use_project_key", lambda pid: contextlib.nullcontext())
    label = {"value": "refund"}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(200, json={
        "answers": {"q": {"type": "choice", "choice": label["value"], "confidence": 0.9}}, "usage": {}}))
    seen: list = []
    _llm_text(monkeypatch, seen)

    intent = {"id": "1", "kind": "llm_judge", "score_name": INTENT, "level": RUN, "config": {
        "model": "typesafe/jev-1.13", "output_type": "decision_multiclass", "question": "Intent?",
        "criteria": {"refund": None, "greeting": None}}}
    # listed FIRST: the dispatcher's topological sort must still run the intent column before it
    explain = {"id": "2", "kind": "llm_judge", "score_name": "explain", "level": RUN, "config": EXPLAIN}
    svc = EvaluationService(trace_reader=object(), score_writer=object())  # type: ignore[arg-type]

    results = {r.name or "": r for r in svc._dispatch_specs([explain, intent], _ctx([_span()]))}
    assert len(seen) == 1 and results["explain"].string_value.startswith("The user wanted")

    label["value"] = "greeting"
    seen.clear()
    results = {r.name or "": r for r in svc._dispatch_specs([explain, intent], _ctx([_span()]))}
    assert seen == [] and results["explain"].string_value == "Not run"
