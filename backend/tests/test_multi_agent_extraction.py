"""What a judge reads from a multi-agent turn — regression for a real production trace.

The user message and answer are the ROOT span's input and output; nothing else is consulted.

An orchestrator delegated to an FAQ sub-agent (`agent_faq`). The sub-agent's generations carry the
orchestrator's REWRITTEN query as their user message plus 15 FAQ results, so they hold the longest
chat histories in the trace — and every judge was graded against that paraphrase ("Client signale
qu'à 00h27 il ne voit pas la promotion…") instead of what the customer typed ("Jumia actuellement
il fait 00h27 on voit pas promo tout à 100f la hein")."""

from __future__ import annotations

import json

from tracely.domain.evaluation.evaluators import prompts
from tracely.domain.evaluation.template_resolver import build_context, template_resolver
from tracely.domain.evaluation.text import agent_answer, user_message
from tracely.domain.traces.spans import root_span

USER = "Jumia actuellement il fait 00h27 on voit pas promo tout à 100f la hein"
REWRITTEN = "Client signale qu’à 00h27 il ne voit pas la promotion « tout à 100 F » sur Jumia CI actuellement."
FALLBACK = "Vous pouvez contacter notre équipe d'assistance clientèle via le chat en direct."
SYSTEM_ORCH = {"role": "system", "content": "# Routing …"}
SYSTEM_FAQ = {"role": "system", "content": "You are a friendly store support agent …"}
FAQ_ROWS = [{"id": f"1_{i}", "question": f"Question {i} à propos de Jumia ?", "answer": "Réponse."} for i in range(15)]
ORCH, TURN, FAQ = "orch-agent", "turn-agent", "faq-agent"


def _span(span_id, parent, type_, name, agent, inp=None, out=None, **kw):
    return {"trace_id": "t1", "span_id": span_id, "parent_span_id": parent, "type": type_, "name": name,
            "agent_id": agent, "input": inp, "output": out, "is_app_root": 1 if not parent else 0,
            "level": "DEFAULT", "status_message": "", "tool_calls": None, "tool_call_names": [], **kw}


def _dumps(v):
    return json.dumps(v, ensure_ascii=True)  # production payloads arrive ASCII-escaped


def _trace() -> list[dict]:
    tool_call = {"role": "assistant", "name": "agent_faq", "content": "",
                 "tool_calls": [{"function": {"name": "lookup_faq", "arguments": "{}"}}]}
    faq_history = [SYSTEM_FAQ, {"role": "user", "content": REWRITTEN}, tool_call,
                   {"role": "tool", "name": "lookup_faq", "content": FAQ_ROWS}]
    return [
        _span("root", "", "SPAN", "agent_teams.message", TURN,
              _dumps([SYSTEM_ORCH, {"role": "user", "content": USER}]),
              _dumps({"role": "assistant", "name": "FAQ agent", "content": FALLBACK})),
        _span("turn", "root", "SPAN", "agent_teams.turn", TURN, _dumps([{"role": "user", "content": USER}]), FALLBACK),
        _span("g1", "turn", "GENERATION", "openai/gpt-5.6-terra", ORCH,
              _dumps([SYSTEM_ORCH, {"role": "user", "content": USER}]),
              _dumps({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "agent_faq"}}]}),
              tool_call_names=["agent_faq"]),
        _span("dlg", "turn", "DELEGATE", "agent_faq", ORCH, REWRITTEN,
              _dumps({"answer": "I don't know", "confidence": 0, "document_used": []})),
        _span("f1", "dlg", "GENERATION", "google/gemini-3.5-flash-lite", FAQ,
              _dumps([SYSTEM_FAQ, {"role": "user", "content": REWRITTEN}]), _dumps(tool_call)),
        _span("tool", "dlg", "TOOL", "lookup_faq", FAQ,
              _dumps({"question": "promotion tout à 100 F Jumia CI"}), _dumps(FAQ_ROWS),
              tool_call_names=["lookup_faq"]),
        _span("f2", "dlg", "GENERATION", "google/gemini-3.5-flash-lite", FAQ, _dumps(faq_history),
              _dumps({"role": "assistant", "content": {"answer": "I don't know", "confidence": 0}})),
        _span("g2", "turn", "GENERATION", "openai/gpt-5.6-terra", ORCH,
              _dumps([SYSTEM_ORCH, {"role": "user", "content": USER}, tool_call,
                      {"role": "tool", "name": "agent_faq", "content": {"answer": "I don't know"}}]),
              _dumps({"role": "assistant", "name": "FAQ agent", "content": FALLBACK})),
    ]


def test_the_user_message_and_answer_come_from_the_root_span():
    root = root_span(_trace())
    assert user_message(root) == USER
    assert agent_answer(root) == FALLBACK


def test_basic_message_body_and_templates_agree():
    spans = _trace()
    body = prompts.message_body(spans)
    assert f"User request:\n{USER}" in body and REWRITTEN not in body
    ctx = build_context("AGENT_RUN", thread_spans=spans, current_trace_id="t1")
    assert template_resolver.resolve("@CURRENT_MESSAGE.input", ctx).resolved_text == USER


def test_tool_payloads_are_readable_and_sub_agents_named():
    ctx = build_context("AGENT_RUN", thread_spans=_trace(), current_trace_id="t1")
    steps = template_resolver.resolve("@CURRENT_STEPS.tool", ctx).resolved_text
    assert "promotion tout à 100 F" in steps and "\\u00e0" not in steps  # not ASCII-escaped
    assert "TOOL `lookup_faq` (agent: agent_faq)" in steps  # named from its DELEGATE, not a UUID
