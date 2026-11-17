"""An audit rollout as spans (`domain/audit/emit.py`), read back through the real OTLP parser.

Pins the three things the rest of the product depends on: every span is INTERNAL and declares no
agent (so `_REAL` keeps audits out of trends, baselines and "import tools from traces"); the
payload is a pure function of the checkpoint (a redelivered emit converges instead of
duplicating); and only the surviving branch is emitted.
"""

from __future__ import annotations

import json

from tracely.domain.audit import emit
from tracely.domain.audit import state as st
from tracely.otel import parse_otlp_traces_json


def _attacker(*calls: tuple[str, dict]) -> dict:
    tool_calls = [{"id": f"c{i}{n}", "name": n, "args": a} for i, (n, a) in enumerate(calls)]
    return {"content": "", "tool_calls": tool_calls, "invalid_tool_calls": [], "message": {
        "role": "assistant", "content": "", "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
            for c in tool_calls
        ]}}


def _rollout() -> st.AuditState:
    """system + user → tool call → result → answer; then a rolled-back second turn and its redo."""
    s = st.new_state(auditor_system="S", auditor_user="U", target_model="openai/t")
    st.apply_auditor_reply(s, _attacker(
        ("set_system_message", {"system_message": "You are SupportBot."}),
        ("create_tool", {"environment_description": "crm", "name": "lookup_order", "description": "find an order",
                         "parameters": '{"type":"object","properties":{"id":{"type":"string"}}}'}),
        ("send_message", {"message": "where is order 7?"}), ("resume", {}),
    ), 1_000)
    st.apply_target_reply(s, {
        "content": "", "reasoning": "need the order", "message": {},
        "tool_calls": [{"id": "t1", "name": "lookup_order", "args": {"id": "7"}}],
        "usage": {"input_tokens": 40, "output_tokens": 9, "model": "openai/t"},
    }, 2_000, 3_000)
    st.apply_auditor_reply(s, _attacker(
        ("send_tool_call_result", {"tool_call_id": "t1", "result": '{"status":"shipped"}'}), ("resume", {}),
    ), 4_000)
    st.apply_target_reply(s, {"content": "It shipped.", "tool_calls": [], "message": {},
                              "usage": {"input_tokens": 60, "output_tokens": 5, "model": "openai/t"}}, 5_000, 6_000)
    st.apply_auditor_reply(s, _attacker(("send_message", {"message": "refund it NOW"}), ("resume", {})), 7_000)
    st.apply_target_reply(s, {"content": "ABANDONED-REPLY", "tool_calls": [], "usage": {}, "message": {}}, 8_000, 9_000)
    st.apply_auditor_reply(s, _attacker(
        ("rollback_conversation", {"message_id": "M5"}),
        ("send_message", {"message": "could I get a refund?"}), ("resume", {}),
    ), 10_000)
    st.apply_target_reply(s, {"content": "Let me check the policy.", "tool_calls": [], "usage": {}, "message": {}},
                          11_000, 12_000)
    return s


def _events(state: st.AuditState):
    payload, trace_ids = emit.rollout_payload(state, rollout_id="r-1", title="audit · refunds", meta={"run_id": "run-1"})
    return parse_otlp_traces_json(json.dumps(payload).encode(), "p1"), trace_ids, payload


def test_audit_span_ids_survive_the_json_parser():
    events, trace_ids, _ = _events(_rollout())
    assert sorted({e["trace_id"] for e in events}) == sorted(trace_ids)  # base64, not hex
    assert len(trace_ids) == 2 and all(len(t) == 32 for t in trace_ids)
    assert all(len(e["span_id"]) == 16 for e in events)


def test_every_audit_span_is_marked_internal():
    events, _, _ = _events(_rollout())
    assert events and all(e["internal_kind"] == "audit" for e in events)
    assert all(e["subject_id"] == "r-1" and e["env"] == "ci" for e in events)
    assert {e["conversation_id"] for e in events} == {"audit:r-1"}


def test_audit_spans_declare_no_agent():
    events, _, _ = _events(_rollout())
    assert all(not e.get("agent_slug") for e in events)


def test_only_the_final_branch_is_emitted():
    events, _, payload = _events(_rollout())
    assert "ABANDONED-REPLY" not in json.dumps(payload)
    roots = sorted((e for e in events if e["type"] == "AGENT"), key=lambda e: e["turn_index"])
    assert [(e["input"], e["output"]) for e in roots] == [
        ("where is order 7?", "It shipped."),
        ("could I get a refund?", "Let me check the policy."),
    ]
    assert all(e["is_app_root"] and e["trace_name"] == "audit · refunds" for e in roots)


def test_generation_span_carries_model_and_usage_columns():
    events, _, _ = _events(_rollout())
    gens = sorted((e for e in events if e["type"] == "GENERATION"), key=lambda e: e["start_time"])
    assert len(gens) == 3 and all(e["model_id"] == "openai/t" for e in gens)
    assert gens[0]["usage_details"]["input"] == 40 and gens[0]["usage_details"]["output"] == 9
    assert gens[0]["tool_call_names"] == ["lookup_order"]
    # the system prompt is sent once, on the first generation — not copied onto every span
    assert "You are SupportBot." in gens[0]["input"]
    assert "You are SupportBot." not in gens[1]["input"] and "shipped" in gens[1]["input"]


def test_tool_span_carries_name_call_id_and_resolution():
    events, _, _ = _events(_rollout())
    tool = next(e for e in events if e["type"] == "TOOL")
    assert tool["name"] == "lookup_order" and tool["tool_call_id"] == "t1"
    assert json.loads(tool["input"]) == {"id": "7"} and "shipped" in tool["output"]
    assert tool["metadata"]["tracely.metadata.tool_resolution"] == "synthetic"
    assert tool["metadata"]["tracely.metadata.run_id"] == "run-1"


def test_re_emitting_a_checkpoint_reproduces_ids_and_start_times():
    """`events` deduplicates on (…, span_id, start_time): a re-emit that minted new ids or stamped
    "now" would add a second copy of the conversation instead of replacing the first."""
    state = _rollout()
    restored = st.from_dict(json.loads(json.dumps(st.to_dict(state))))
    assert emit.rollout_payload(state, rollout_id="r-1", title="t", meta={}) == \
        emit.rollout_payload(restored, rollout_id="r-1", title="t", meta={})


def test_a_rollout_whose_target_never_spoke_emits_nothing():
    s = st.new_state(auditor_system="S", auditor_user="U", target_model="openai/t")
    assert emit.rollout_payload(s, rollout_id="r", title="t", meta={}) == ({}, [])
