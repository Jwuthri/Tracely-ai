"""Conversation agents: deriving agents from spans, the agent view behind @AGENTS,
and the panel shaper that merges observed tool counts into the declared catalog."""

from __future__ import annotations

import json

from tracely.api.routers.sessions import _shape_declared_agent
from tracely.domain.evaluation.template_resolver import (
    build_context,
    collect_agents,
    format_agents,
    template_resolver,
)
from tracely.domain.traces.spans import system_prompt
from tracely.infrastructure.clickhouse import async_reader

_CATALOG = [
    {
        "name": "Support Agent",
        "description": "Handles customer inquiries",
        "tools": {
            "lookup_order": {
                "name": "lookup_order",
                "description": "Look up order by ID",
                "parameters": {"order_id": "string"},
            }
        },
    }
]


def test_format_agent_catalog_includes_names_and_descriptions():
    out = format_agents(collect_agents([], _CATALOG))
    assert "Support Agent: Handles customer inquiries" in out
    assert "lookup_order" in out
    assert "Look up order by ID" in out
    # Arguments deliberately omitted — see test_the_catalog_is_names_and_descriptions_only.
    assert "params" not in out


def test_agents_includes_the_declared_catalog():
    # @AGENTS carries the declared catalog when one was sent
    ctx = build_context(
        "AGENT_RUN", thread_spans=[{"trace_id": "t", "agent_id": "x"}],
        current_trace_id="t", wanted_vars=["AGENTS"], declared_agents=_CATALOG,
    )
    out = format_agents(ctx.agents)
    assert out and "Support Agent" in out and "lookup_order" in out


def test_shape_declared_agent_merges_observed_counts():
    shaped = _shape_declared_agent(_CATALOG[0], {"lookup_order": 3})
    assert shaped["name"] == "Support Agent"
    tool = shaped["tools"][0]
    assert tool["name"] == "lookup_order"
    assert tool["count"] == 3  # observed execution count merged in
    assert tool["parameters"] == {"order_id": "string"}


def test_shape_declared_agent_passes_extra_config_through():
    # config the trace can't show (guardrails, prompts, model) must survive the panel shaper
    shaped = _shape_declared_agent(
        {
            "name": "router",
            "system_prompt": "You route.",
            "model": {"name": "claude-opus-4", "temperature": 0.2},
            "guardrails": [{"name": "pii_filter", "on": "output"}],
            "tools": {"search": {"description": "web search", "timeout_s": 30}},
        },
        {"search": 1},
    )
    assert shaped["system_prompt"] == "You route."
    assert shaped["model"] == {"name": "claude-opus-4", "temperature": 0.2}
    assert shaped["guardrails"][0]["name"] == "pii_filter"
    assert shaped["tools"][0]["timeout_s"] == 30  # per-tool extras survive too
    assert shaped["tools"][0]["count"] == 1  # …without clobbering the derived count


def test_shape_declared_agent_extras_never_override_panel_keys():
    shaped = _shape_declared_agent(
        {"tools": {"t": {"name": "t", "count": 999}}}, {"t": 2}
    )
    assert shaped["name"] == "agent"  # missing name still defaulted
    assert shaped["tools"][0]["count"] == 2  # observed count wins over a declared one


def test_system_prompt_from_spans():
    spans = [
        {"input": json.dumps([{"role": "user", "content": "hi"}])},  # no system message
        {"input": json.dumps([
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "hi"},
        ])},
        {"input": json.dumps([{"role": "system", "content": "later, ignored"}])},
    ]
    assert system_prompt(spans) == "You are terse."  # first one wins


def test_system_prompt_handles_blocks_and_absence():
    blocks = [{"input": json.dumps([
        {"role": "system", "content": [
            {"type": "text", "text": "line one"},
            {"type": "image", "url": "x"},  # non-text blocks ignored
            {"type": "text", "text": "line two"},
        ]}
    ])}]
    assert system_prompt(blocks) == "line one\nline two"
    assert system_prompt([{"input": json.dumps([{"role": "user", "content": "hi"}])}]) == ""
    assert system_prompt([{"input": "not json {\"system\"}"}, {"input": None}, {}]) == ""


async def test_thread_agents_derivation(monkeypatch):
    spans = [
        # agent a1 executes `search`, then a generation that only *requests* `compare`
        {"agent_id": "a1", "type": "TOOL", "name": "search", "output": "ok"},
        {"agent_id": "a1", "type": "TOOL", "name": "search", "output": "ok"},
        {"agent_id": "a1", "type": "GENERATION", "output": "done", "tool_call_names": ["compare"]},
        # agent a2 executes `lookup` once
        {"agent_id": "a2", "type": "TOOL", "name": "lookup", "output": "ok"},
    ]

    async def fake_spans(project_id, thread_id):
        return spans

    monkeypatch.setattr(async_reader, "thread_spans_full", fake_spans)
    agents = await async_reader.thread_agents("p", "thread-1")

    by_id = {a["agent_id"]: a for a in agents}
    assert set(by_id) == {"a1", "a2"}

    a1 = by_id["a1"]
    tools = {t["name"]: t["count"] for t in a1["tools"]}
    assert tools == {"search": 2, "compare": 0}  # executed twice; compare only requested
    assert a1["tool_call_count"] == 2
    assert a1["span_count"] == 3

    # sorted by tool activity then span volume → a1 (2 calls) before a2 (1 call)
    assert agents[0]["agent_id"] == "a1"


async def test_thread_agents_empty(monkeypatch):
    async def fake_spans(project_id, thread_id):
        return []

    monkeypatch.setattr(async_reader, "thread_spans_full", fake_spans)
    assert await async_reader.thread_agents("p", "t") == []


# ── the catalog in the judge's prompt ─────────────────────────────────────────


def test_the_catalog_is_names_and_descriptions_only():
    """Argument names answer "how would I call this?" — not a question a judge asks. They were
    also most of the catalog's length, competing for room with the transcript being graded."""
    rendered = format_agents(collect_agents([], [{
        "name": "Weather Agent",
        "tools": [{
            "name": "get_weather",
            "description": "Current conditions.",
            "parameters": {
                "type": "object",
                "properties": {"city": {}, "units": {}},
                "required": ["city"],
            },
        }],
    }]))
    assert "get_weather — Current conditions." in rendered
    for noise in ("params", "properties", "city", "required"):
        assert noise not in rendered


def test_a_basic_judge_is_never_handed_the_tool_catalog(monkeypatch):
    """It used to be stapled under every basic prompt. At message level that made the judge grade
    tool choice on a greeting; at conversation level it answered "which tools should have run?"
    instead of "how did this conversation go". The catalog reaches a judge one way now — an
    advanced template asking for `@AGENTS` — because there the author asked for it."""
    from tracely.domain.evaluation.evaluators import llm_judge as mod
    import tracely.services.conversation_agents_service as cas

    monkeypatch.setattr(
        cas.ConversationAgentsService, "for_thread",
        staticmethod(lambda p, t: [{"name": "Support", "tools": [{"name": "issue_refund"}]}]),
    )
    assert not hasattr(mod.LLMJudgeEvaluator, "_capabilities")


# ── @AGENTS: declared ∪ offered ∪ called ─────────────────────────────────────


def _offered(n: int, name: str, desc: str) -> tuple[str, str]:
    schema = {"type": "function", "function": {"name": name, "description": desc, "parameters": {}}}
    return f"llm.tools.{n}.tool.json_schema", json.dumps(schema)


def _turn(tid: str, *, offered=(), called=(), requested=()) -> list[dict]:
    """One turn of agent `a1` (named "support" by its AGENT span): a model call offered `offered`
    and asked for `requested`, and a TOOL span ran for each of `called`."""
    root = {"trace_id": tid, "span_id": f"{tid}-root", "type": "AGENT", "name": "support",
            "agent_id": "a1", "input": "hi", "output": "ok"}
    gen = {"trace_id": tid, "span_id": f"{tid}-gen", "parent_span_id": root["span_id"],
           "type": "GENERATION", "agent_id": "a1", "tool_call_names": list(requested),
           "metadata": dict(_offered(i, n, d) for i, (n, d) in enumerate(offered))}
    tools = [{"trace_id": tid, "span_id": f"{tid}-t{i}", "parent_span_id": root["span_id"],
              "type": "TOOL", "name": n, "agent_id": "a1",
              "metadata": {"tool.name": n, "tool.description": d}}
             for i, (n, d) in enumerate(called)]
    return [root, gen, *tools]


def test_agents_merges_declared_offered_and_called_tools():
    spans = _turn(
        "t1",
        offered=[("get_order", "Look up an order."), ("check_inventory", "Stock for a SKU.")],
        called=[("get_order", ""), ("refund", "")],
    )
    declared = [{"name": "support", "description": "Order help",
                 "tools": [{"name": "get_order", "description": "Declared lookup."}]}]
    [agent] = collect_agents(spans, declared)  # the observed agent IS the declared one (same name)
    out = format_agents([agent])
    assert out.splitlines()[0] == "- support: Order help"
    assert "get_order — Declared lookup. [declared · called 1×]" in out  # declared wins the text
    assert "check_inventory — Stock for a SKU. [offered · not called]" in out
    assert "refund [called 1×, no definition]" in out


def test_agents_tools_without_a_declared_catalog():
    # The SDK sent no agent definition — the tools still come from what the model was offered.
    spans = _turn("t1", offered=[("get_order", "Look up an order.")])
    assert format_agents(collect_agents(spans), "tools") == (
        "- get_order — Look up an order. [offered · not called]"
    )


def test_agents_called_counts_requests_only_when_nothing_ran():
    ran = _turn("t1", requested=["get_order"], called=[("get_order", "")])
    assert format_agents(collect_agents(ran), "called") == "- get_order ×1"  # not ×2
    asked = _turn("t1", requested=["get_order", "get_order"])  # tools not instrumented
    assert format_agents(collect_agents(asked), "called") == "- get_order ×2"


def test_agents_prefixes_the_agent_only_when_there_are_several():
    spans = _turn("t1", called=[("get_order", "")])
    spans.append({"trace_id": "t1", "span_id": "x", "type": "TOOL", "name": "search", "agent_id": "a2"})
    assert format_agents(collect_agents(spans), "called") == "- support / get_order ×1\n- a2 / search ×1"


def test_agents_stops_at_the_graded_turn():
    spans = _turn("t1", called=[("get_order", "")]) + _turn("t2", called=[("refund", "")])
    ctx = build_context("AGENT_RUN", thread_spans=spans, current_trace_id="t1", wanted_vars=["AGENTS"])
    text = template_resolver.resolve("@AGENTS.called", ctx).resolved_text
    assert "get_order" in text and "refund" not in text
    whole = build_context("CONVERSATION", thread_spans=spans, wanted_vars=["AGENTS"])
    assert "refund" in template_resolver.resolve("@AGENTS.called", whole).resolved_text


def test_list_agent_is_gone():
    ctx = build_context("CONVERSATION", thread_spans=_turn("t1", called=[("get_order", "")]))
    assert template_resolver.resolve("@LIST_AGENT", ctx).resolved_text == "[No LIST_AGENT available]"


def test_agents_attributes_graph_nodes_to_the_declared_sub_agent():
    """LangGraph stamps the supervisor's agent id on every span; a sub-agent is only the node its
    calls run under. Its tools must land on it, not pile up on whatever the AGENT spans are named."""
    spans = [
        {"trace_id": "t", "span_id": "root", "type": "CHAIN", "name": "lg-supervisor", "agent_id": "a1"},
        {"trace_id": "t", "span_id": "h", "parent_span_id": "root", "type": "AGENT",
         "name": "support_agent", "agent_id": "a1"},
        {"trace_id": "t", "span_id": "node", "parent_span_id": "h", "type": "CHAIN",
         "name": "lg-support", "agent_id": "a1"},
        {"trace_id": "t", "span_id": "tool", "parent_span_id": "node", "type": "TOOL",
         "name": "get_order", "agent_id": "a1"},
    ]
    declared = [
        {"name": "lg-supervisor", "tools": []},
        {"name": "lg-support", "tools": [{"name": "get_order", "description": "Look up."}]},
    ]
    assert format_agents(collect_agents(spans, declared)) == (
        "- lg-supervisor\n- lg-support\n    • get_order — Look up. [declared · called 1×]"
    )
