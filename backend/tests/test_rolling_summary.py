"""Rolling summary: span decomposition, item construction (verbatim + tool linkage), @HISTORY,
and the 20k-budget recursive compaction."""

from __future__ import annotations

from unittest.mock import patch

from tracely.domain.evaluation.rolling_summary import (
    assistant_text,
    components_token_total,
    decompose_turn_span,
    dedup_consecutive,
    format_summary_as_history,
    items_from_components,
    step_components,
    summary_token_total,
    user_input_component,
    user_input_for_turn,
    user_text,
)
from tracely.services import rolling_summary_service as rss
from tracely.services.rolling_summary_service import RollingSummaryService


def test_thinking_span_component():
    comps = step_components({"type": "THINKING", "output": "I should call the weather tool."})
    assert len(comps) == 1
    assert (comps[0].role, comps[0].type) == ("assistant", "thinking")


def test_tool_span_yields_call_and_result():
    comps = step_components(
        {"type": "TOOL", "name": "get_weather", "input": '{"city":"Paris"}', "output": '{"temp_c":18}'}
    )
    assert [(c.role, c.type) for c in comps] == [
        ("assistant", "tool_call"),
        ("tool", "tool_result"),
    ]
    assert comps[0].tool_name == "get_weather"
    assert "Paris" in comps[0].content


def test_generation_structured_vs_text():
    structured = step_components({"type": "GENERATION", "output": {"answer": "yes"}})
    assert structured[0].type == "output_structured"
    text = step_components({"type": "GENERATION", "output": "It is sunny."})
    assert text[0].type == "output_content"


def test_items_link_tool_call_to_result():
    comps = step_components(
        {"type": "TOOL", "name": "get_weather", "input": "{}", "output": "sunny"}
    )
    items = items_from_components(comps)
    call, result = items
    assert call.tool_call_id and call.tool_call_id == result.tool_call_id


def test_verbatim_under_budget_keeps_content():
    comps = step_components({"type": "GENERATION", "output": "It is 18C and sunny in Paris."})
    assert components_token_total(comps) < 512
    items = items_from_components(comps)  # no summaries → verbatim
    assert items[0].content == "It is 18C and sunny in Paris."


def test_llm_summaries_replace_content_but_keep_structure():
    comps = step_components(
        {"type": "TOOL", "name": "search", "input": "{}", "output": "x" * 50}
    )
    items = items_from_components(comps, summaries=["searched", "found x"])
    assert items[0].type == "tool_call" and items[0].content == "searched"
    assert items[1].type == "tool_result" and items[1].content == "found x"


def test_user_input_component():
    uc = user_input_component({"input": "What is the weather?"})
    assert uc is not None and uc.role == "user"
    assert user_input_component({"input": ""}) is None


def test_history_render_format():
    comps = [
        user_input_component({"input": "Weather in Paris?"}),
        *step_components({"type": "GENERATION", "output": "18C and sunny."}),
    ]
    items = [it.model_dump() for it in items_from_components([c for c in comps if c])]
    history = format_summary_as_history(items)
    assert "[user]: Weather in Paris?" in history
    assert "[assistant]: 18C and sunny." in history


def test_format_history_full_vs_clipped():
    items = [{"role": "assistant", "type": "output_content", "content": "x" * 1000}]
    assert len(format_summary_as_history(items, max_chars=0)) > 900  # full (no clip)
    assert len(format_summary_as_history(items, max_chars=100)) <= 100  # clipped


def _big_items(n: int, tokens_each: int = 3000):
    chars = tokens_each * 4
    return [
        {
            "role": "assistant",
            "type": "output_content",
            "content": "x" * chars,
            "tool_call_id": None,
            "tool_name": None,
            "tool_arguments": None,
        }
        for _ in range(n)
    ]


def test_summary_token_total():
    items = _big_items(3, tokens_each=100)  # 400 chars ≈ 100 tokens each
    assert summary_token_total(items) == 300


def test_compaction_folds_older_items_into_prev_summary_item():
    svc = RollingSummaryService.__new__(RollingSummaryService)  # skip TraceReader init
    items = _big_items(10, tokens_each=3000)  # 30k tokens, over the 20k budget
    with patch.object(rss.provider, "llm_enabled", return_value=False):
        out, n = svc._compact_to_budget(list(items))
    assert n >= 1
    assert summary_token_total(out) <= 20000  # under budget
    # a flat list: one leading prev_summary item, then the last 2 verbatim
    assert out[0]["role"] == "prev_summary" and out[0]["type"] == "summary"
    assert len(out) == 3
    assert out[-1]["content"] == items[-1]["content"]
    assert out[-2]["content"] == items[-2]["content"]


def test_compaction_noop_under_budget():
    svc = RollingSummaryService.__new__(RollingSummaryService)
    running = _big_items(3, tokens_each=1000)  # 3k tokens, under budget
    with patch.object(rss.provider, "llm_enabled", return_value=False):
        out, n = svc._compact_to_budget(list(running))
    assert n == 0
    assert out == running  # untouched


# --- readable-text extraction (the "object stored as content" defects) --------

_Q = "Where is my order ORD-4471, and is the Alpine Winter Coat (SKU-COAT-01) back in stock?"


def test_user_text_unwraps_question_envelope():  # CrewAI / OpenAI / Anthropic / LiteLLM
    assert user_text('{"question": "%s"}' % _Q) == _Q


def test_user_text_unwraps_messages_envelope():  # LangChain / LangGraph / OpenRouter
    assert user_text('{"messages": [{"role": "user", "content": "%s"}]}' % _Q) == _Q


def test_user_text_unwraps_google_adk_new_message():
    raw = '{"user_id": "ada", "session_id": "s", "new_message": {"parts": [{"text": "%s"}]}}' % _Q
    assert user_text(raw) == _Q


def test_user_text_picks_last_user_message_in_multiturn_input():
    raw = (
        '[{"role": "system", "content": "You are a bot"},'
        ' {"role": "user", "content": "first question"},'
        ' {"role": "assistant", "content": "an answer"},'
        ' {"role": "user", "content": "second question"}]'
    )
    assert user_text(raw) == "second question"


def test_user_text_empty_for_unrecognized_blob():  # LlamaIndex workflow init_state
    assert user_text('{"init_state": {"is_running": false, "config": {}}}') == ""
    assert user_text(None) == ""


def test_assistant_text_unwraps_message_array():  # the headline defect
    raw = '[{"role": "assistant", "content": "Your order is out for delivery."}]'
    assert assistant_text(raw) == "Your order is out for delivery."


def test_assistant_text_unwraps_model_role_and_blocks():  # Gemini / OpenAI-Agents / ADK
    assert assistant_text('[{"role": "model", "content": "stock is fine"}]') == "stock is fine"
    blocks = '[{"role": "assistant", "content": [{"type": "text", "text": "block answer"}]}]'
    assert assistant_text(blocks) == "block answer"


def test_assistant_text_empty_for_pure_tool_call_turn():  # content:"" + tool_calls
    raw = '[{"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {}}]}]'
    assert assistant_text(raw) == ""
    assert assistant_text('{"role": "assistant", "content": null}') == ""


def test_generation_message_array_becomes_output_content_not_structured():
    # CrewAI ReAct: the inner content text is extracted, NOT the [{role,content}] wrapper.
    raw = '[{"role": "assistant", "content": "Thought: I need the order status.\\nFinal Answer: done."}]'
    comps = step_components({"type": "GENERATION", "output": raw})
    assert len(comps) == 1
    assert comps[0].type == "output_content"
    assert comps[0].content.startswith("Thought:")
    assert "role" not in comps[0].content  # no JSON wrapper leaked into content


def test_generation_tool_call_turn_yields_nothing():
    # empty content + tool_calls → the TOOL spans represent the call; the generation adds nothing.
    raw = '[{"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {}}]}]'
    assert step_components({"type": "GENERATION", "output": raw}) == []


def test_tool_args_unwraps_llama_index_kwargs():
    comps = step_components({"type": "TOOL", "name": "get_order_status",
                            "input": '{"kwargs": {"order_id": "ORD-4471"}}', "output": "{}"})
    assert comps[0].tool_arguments == '{"order_id": "ORD-4471"}'
    assert "kwargs" not in comps[0].content


def test_tool_result_prefers_raw_output_and_adk_response():
    li = step_components({"type": "TOOL", "name": "t",
                          "input": "{}", "output": '{"blocks": [{"text": "junk"}], "raw_output": {"ok": 1}}'})
    assert li[1].content == '{"ok": 1}'
    adk = step_components({"type": "TOOL", "name": "t",
                           "input": "{}", "output": '{"id": "x", "name": "t", "response": {"ok": 2}}'})
    assert adk[1].content == '{"ok": 2}'


def test_decompose_skips_nonroot_chain_and_agent():
    # LangGraph tools_condition router / LlamaIndex BaseWorkflowAgent.* → contribute nothing
    routing = {"type": "CHAIN", "name": "tools_condition", "output": "__end__"}
    assert decompose_turn_span(routing, is_root=False, multi=True, user_comp=None) == []
    agent = {"type": "AGENT", "name": "sub", "output": "noise"}
    assert decompose_turn_span(agent, is_root=False, multi=True, user_comp=None) == []
    # a non-root GENERATION is still decomposed
    gen = {"type": "GENERATION", "output": '[{"role": "assistant", "content": "hi"}]'}
    out = decompose_turn_span(gen, is_root=False, multi=True, user_comp=None)
    assert out and out[0].type == "output_content" and out[0].content == "hi"


def test_user_input_for_turn_falls_back_to_generation_input():
    # LlamaIndex: root input is a config blob → use the user message in the GENERATION input
    root = {"span_id": "r", "type": "CHAIN", "input": '{"init_state": {"config": {}}}'}
    gen = {"span_id": "g", "type": "GENERATION",
           "input": '[{"role": "system", "content": "sys"}, {"role": "user", "content": "%s"}]' % _Q}
    comp = user_input_for_turn([root, gen], root_id="r")
    assert comp is not None and comp.role == "user" and comp.content == _Q


def test_dedup_consecutive_drops_repeated_final_answer():
    a = {"role": "assistant", "type": "output_content", "content": "same"}
    b = {"role": "assistant", "type": "output_content", "content": "same"}
    c = {"role": "assistant", "type": "output_content", "content": "different"}
    assert dedup_consecutive(None, [a, b, c]) == [a, c]
    assert dedup_consecutive(a, [b, c]) == [c]  # dedups against the prior stored tail too



def test_a_failed_step_keeps_its_error():
    from tracely.domain.evaluation.rolling_summary import step_components

    comps = step_components({"type": "TOOL", "name": "run_sql", "input": '{"sql": "insert"}', "output": "",
                             "level": "ERROR", "status_message": "permission denied (role: analyst_ro)"})
    assert [c.type for c in comps] == ["tool_call", "error"]
    assert comps[-1].content == "ERROR: permission denied (role: analyst_ro)"
    assert step_components({"type": "TOOL", "name": "ok", "output": "fine"})[-1].type == "tool_result"
