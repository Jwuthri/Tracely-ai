"""message_text — readable text from structured message I/O (used by search + cluster members)."""

from __future__ import annotations

import json

from tracely.domain.evaluation.text import agent_answer, readable_io, user_message
from tracely.infrastructure.text import extract_text, message_text


def test_chat_message_object():
    assert message_text('{"role": "user", "content": [{"type": "text", "text": "hello there"}]}') == "hello there"


def test_content_block_array():
    raw = '[{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]'
    assert message_text(raw) == "hi"


def test_assistant_object_with_string_content():
    assert message_text('{"role": "assistant", "content": "the answer"}') == "the answer"


def test_plain_string_passes_through():
    assert message_text("just a plain user message") == "just a plain user message"


def test_empty_and_none():
    assert message_text("") == ""
    assert message_text(None) == ""


def test_attachment_only_falls_back_to_raw():
    # no text block to extract -> keep the raw value rather than blanking the label
    raw = '[{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]'
    assert message_text(raw) == raw


def test_invalid_json_passes_through():
    assert message_text("{not valid json") == "{not valid json"


def test_extract_text_walks_nested_content():
    assert extract_text({"content": [{"type": "text", "text": "deep"}]}) == "deep"


# ── what a judge reads by default: the ROOT span's input and output ─────────────
# One rule (domain/evaluation/text.py): the user message is what the root span received, the answer
# is what it returned. No other span is consulted — a longer history elsewhere, a later generation,
# a sub-agent's prompt: none of them is what the user said or was told.


def _hist(*msgs, key="role"):
    return json.dumps([{key: r, "content": c} for r, c in msgs])


def test_the_user_message_is_the_last_user_turn_of_the_root_input():
    root = {"input": _hist(("system", "s"), ("user", "first"), ("assistant", "a"), ("user", "02/08/2026"))}
    assert user_message(root) == "02/08/2026"


def test_the_history_can_hide_under_a_messages_key():
    root = {"input": json.dumps({"messages": [{"role": "user", "content": "12345678900"}]})}
    assert user_message(root) == "12345678900"


def test_a_plain_or_single_message_root_input():
    assert user_message({"input": "plain string"}) == "plain string"
    root = {"input": '{"role": "user", "content": [{"type": "text", "text": "where is my order?"}]}'}
    assert user_message(root) == "where is my order?"


def test_no_user_message_on_the_root_reads_as_none():
    assert user_message({"input": None}) == ""
    assert user_message({"input": _hist(("system", "only a prompt"))}) == ""


def test_the_answer_is_the_root_output():
    assert agent_answer({"output": json.dumps({"role": "assistant", "content": "your refund is on its way"})}) \
        == "your refund is on its way"
    assert agent_answer({"output": "handled"}) == "handled"
    assert agent_answer({"output": None}) == ""


def test_an_answer_that_is_only_a_tool_call_is_shown_as_that_call():
    out = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "show_card", "arguments": "{\"sku\": 42}"}}]}
    assert agent_answer({"output": json.dumps(out)}) == '[no text reply — the agent called show_card({"sku": 42})]'


def test_a_step_input_renders_the_whole_exchange_not_just_the_system_prompt():
    """A generation's input is the message array it was called with. Rendered as "first readable
    text" the step judge saw the agent's rubric under Step input and never the request."""
    body = readable_io(_hist(
        ("system", "You are Realize."),
        ("user", "hellohello"),
        ("assistant", "CPF?"),
        ("user", "12345678900"),
    ))
    assert body.splitlines() == [
        "system: You are Realize.",
        "user: hellohello",
        "assistant: CPF?",
        "user: 12345678900",
    ]


def test_readable_io_leaves_tool_json_and_single_messages_alone():
    assert readable_io('{"open_count": 1}') == '{"open_count": 1}'
    assert readable_io('{"role": "assistant", "content": "done"}') == "done"


