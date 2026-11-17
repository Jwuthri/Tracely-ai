"""The audit rollout's state machine (`domain/audit/state.py`) — pure, no model, no I/O.

Pins the semantics borrowed from Petri that the rest of the feature leans on: staging vs
`resume`, pending tool calls, rollback as a branch (the target forgets, the attacker and the
judges do not), the turn budget counting ATTACKER generations, and that the whole state survives
a JSON round trip between any two calls — which is what lets a rollout run as short slices.
"""

from __future__ import annotations

import json

from tracely.domain.audit import state as st
from tracely.domain.audit import transcript


def _reply(*calls: tuple[str, dict], content: str = "", invalid: list | None = None) -> dict:
    """An attacker generation as `provider.run_chat` returns it."""
    tool_calls = [{"id": f"c{i}_{name}", "name": name, "args": args} for i, (name, args) in enumerate(calls)]
    return {
        "content": content,
        "tool_calls": tool_calls,
        "invalid_tool_calls": invalid or [],
        "message": {
            "role": "assistant", "content": content,
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                for c in tool_calls
            ] + [
                {"id": i["id"], "type": "function", "function": {"name": i["name"], "arguments": i["raw"]}}
                for i in invalid or []
            ],
        },
    }


def _target(content: str = "", calls: list | None = None, reasoning: str = "") -> dict:
    return {"content": content, "reasoning": reasoning, "tool_calls": calls or [], "usage": {}, "message": {}}


def _new(**kw) -> st.AuditState:
    return st.new_state(auditor_system="SYS", auditor_user="GO", target_model="openai/t", **kw)


def _open(state: st.AuditState, text: str = "hi", reply: str = "hello") -> None:
    """One full exchange: the attacker sends `text`, the target answers `reply`."""
    st.apply_auditor_reply(state, _reply(("send_message", {"message": text}), ("resume", {})), 1)
    st.apply_target_reply(state, _target(reply), 2, 3)


def test_staged_message_is_delivered_only_on_resume():
    state = _new()
    st.apply_auditor_reply(state, _reply(("set_system_message", {"system_message": "be nice"})), 1)
    assert st.next_step(state) == "auditor" and not state.nodes  # nothing reached the target
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {})), 2)
    assert st.next_step(state) == "target"
    messages, _ = st.target_request(state)
    assert messages == [{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"}]


def test_an_auditor_turn_that_stages_without_resuming_is_resumed_for_it():
    state = _new()
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"})), 1)
    assert st.next_step(state) == "target"
    # the injected call is a real tool call in the history, with its placeholder result
    assert state.auditor_messages[-3]["tool_calls"][-1]["function"]["name"] == "resume"
    st.apply_target_reply(state, _target("hello"), 2, 3)
    assert "hello" in state.auditor_messages[-1]["content"]
    # every tool call in the attacker's history has exactly one answer — providers reject less
    asked = [tc["id"] for m in state.auditor_messages for tc in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in state.auditor_messages if m.get("role") == "tool"]
    assert sorted(asked) == sorted(answered)


def test_pending_tool_calls_must_all_be_answered_before_resume():
    state = _new(tools=[{"name": "search", "description": "", "parameters": {"type": "object"}}])
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "find x"}), ("resume", {})), 1)
    st.apply_target_reply(state, _target(calls=[
        {"id": "t1", "name": "search", "args": {"q": "x"}}, {"id": "t2", "name": "search", "args": {"q": "y"}},
    ]), 2, 3)
    assert st.pending_calls(state) == ["t1", "t2"]

    st.apply_auditor_reply(state, _reply(
        ("send_tool_call_result", {"tool_call_id": "t1", "result": "one"}), ("resume", {}),
    ), 4)
    assert st.next_step(state) == "auditor"  # refused: t2 is still unanswered
    assert "t2" in state.auditor_messages[-1]["content"]

    st.apply_auditor_reply(state, _reply(
        ("send_tool_call_result", {"tool_call_id": "t2", "result": "two"}), ("resume", {}),
    ), 5)
    assert st.next_step(state) == "target"
    messages, _ = st.target_request(state)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool"]
    assert [m["content"] for m in messages[-2:]] == ["one", "two"]


def test_rollback_makes_the_target_forget_but_the_auditor_remember():
    state = _new()
    _open(state, "hi", "hello")
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "do the bad thing"}), ("resume", {})), 4)
    st.apply_target_reply(state, _target("I refuse"), 5, 6)
    refused = state.head

    st.apply_auditor_reply(state, _reply(
        ("rollback_conversation", {"message_id": "M2"}),  # back to the target's "hello"
        ("send_message", {"message": "something subtler"}), ("resume", {}),
    ), 7)
    messages, _ = st.target_request(state)
    assert [m["content"] for m in messages] == ["hi", "hello", "something subtler"]  # no refusal
    assert refused in state.nodes  # …but nothing was deleted
    assert any("I refuse" in (m.get("content") or "") for m in state.auditor_messages)
    assert state.branches[-1] == {"n": 1, "from": "M2", "kind": "rollback"}


def test_rolled_back_turns_still_count_against_the_turn_budget():
    state = _new(max_turns=3)
    _open(state)
    st.apply_auditor_reply(state, _reply(("rollback_conversation", {"message_id": "M1"})), 4)
    assert state.auditor_turns == 2 and not state.ended
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "again"}), ("resume", {})), 5)
    assert state.ended == "max_turns"
    assert st.next_step(state) == "target"  # the reply that was asked for is still owed
    st.apply_target_reply(state, _target("ok"), 6, 7)
    assert st.next_step(state) == "done"


def test_max_turns_counts_auditor_generations_not_target_replies():
    state = _new(max_turns=2)
    _open(state)
    assert state.auditor_turns == 1 and st.next_step(state) == "auditor"


def test_tool_parameters_arrive_as_a_json_string_and_are_validated():
    state = _new()
    st.apply_auditor_reply(state, _reply(("create_tool", {
        "environment_description": "a crm", "name": "lookup", "description": "find a customer",
        "parameters": '{"type":"object","properties":{"id":{"type":"string"}}}',
    })), 1)
    assert state.tools[0]["parameters"]["properties"] == {"id": {"type": "string"}}
    _, tools = st.target_request(state)
    assert tools[0]["function"]["name"] == "lookup" and "environment" not in json.dumps(tools)

    st.apply_auditor_reply(state, _reply(("create_tool", {
        "environment_description": "", "name": "bad name!", "description": "", "parameters": "{}",
    })), 2)
    assert state.auditor_messages[-1]["content"].startswith("Error") and len(state.tools) == 1
    st.apply_auditor_reply(state, _reply(("create_tool", {
        "environment_description": "", "name": "t2", "description": "", "parameters": "not json",
    })), 3)
    assert "not valid JSON" in state.auditor_messages[-1]["content"] and len(state.tools) == 1


def test_a_fixed_target_cannot_have_its_prompt_or_tools_rewritten():
    state = _new(control="fixed", system_prompt="You are SupportBot.")
    names = {t["function"]["name"] for t in st.auditor_request(state)[1]}
    assert not names & {"set_system_message", "create_tool", "remove_tool"}
    st.apply_auditor_reply(state, _reply(("set_system_message", {"system_message": "be evil"})), 1)
    assert state.auditor_messages[-1]["content"].startswith("Error")
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {})), 2)
    assert st.target_request(state)[0][0] == {"role": "system", "content": "You are SupportBot."}


def test_a_malformed_target_tool_call_is_surfaced_and_answerable():
    state = _new()
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {})), 1)
    reply = _target("let me check")
    reply["invalid_tool_calls"] = [{"id": "x", "name": "search", "raw": '{"q": ', "error": "bad json"}]
    st.apply_target_reply(state, reply, 2, 3)
    assert "malformed call to `search`" in state.auditor_messages[-1]["content"]
    assert st.pending_calls(state) == []  # nothing the attacker cannot answer is left pending


def test_a_truncated_auditor_tool_call_is_reported_back_not_executed():
    state = _new()
    st.apply_auditor_reply(state, _reply(invalid=[
        {"id": "bad1", "name": "send_message", "raw": '{"message": "hi', "error": "unterminated"},
    ]), 1)
    assert not state.staged and st.next_step(state) == "auditor"
    assert "malformed arguments" in state.auditor_messages[-1]["content"]
    # the unparseable argument string must not stay in the history verbatim
    assert state.auditor_messages[-2]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_an_attacker_that_stops_calling_tools_is_declared_stalled():
    state = _new()
    for i in range(3):
        st.apply_auditor_reply(state, _reply(content="I can't help with that."), i)
    assert state.ended == "stalled" and st.next_step(state) == "done"
    assert not st.has_target_reply(state)


def test_a_failed_target_generation_is_told_to_the_attacker():
    state = _new()
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {})), 1)
    st.apply_target_error(state, "503 upstream")
    assert st.next_step(state) == "auditor"
    assert "Target generate failed: 503 upstream" in state.auditor_messages[-1]["content"]


def test_prefill_starts_the_targets_reply_and_is_marked_for_the_judge():
    state = _new(prefill=True)
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {"prefill": "Sure, I will"})), 1)
    assert st.target_request(state)[0][-1] == {"role": "assistant", "content": "Sure, I will"}
    st.apply_target_reply(state, _target(" do it."), 2, 3)
    assert state.nodes[state.head]["content"] == "Sure, I will do it."
    assert "<prefill>Sure, I will</prefill> do it." in transcript.render(state)


def test_state_survives_a_json_round_trip_mid_turn():
    state = _new()
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "hi"}), ("resume", {})), 1)
    assert state.awaiting is not None  # mid-turn: the target owes a reply
    restored = st.from_dict(json.loads(json.dumps(st.to_dict(state))))
    assert st.to_dict(restored) == st.to_dict(state)
    st.apply_target_reply(restored, _target("hello"), 2, 3)
    assert st.next_step(restored) == "auditor" and "hello" in restored.auditor_messages[-1]["content"]


def test_judge_transcript_shows_abandoned_branches_delimited():
    state = _new()
    _open(state, "hi", "hello")
    st.apply_auditor_reply(state, _reply(("send_message", {"message": "bad ask"}), ("resume", {})), 4)
    st.apply_target_reply(state, _target("SECRET-LEAK", reasoning="they seem trustworthy"), 5, 6)
    st.apply_auditor_reply(state, _reply(
        ("rollback_conversation", {"message_id": "M2"}), ("send_message", {"message": "other"}), ("resume", {}),
    ), 7)
    st.apply_target_reply(state, _target("fine"), 8, 9)
    text = transcript.render(state)
    assert "SECRET-LEAK" in text  # the rolled-back misbehaviour is still judged
    assert "--- Conversation Branch 1 (rolled back to [M2]) ---" in text
    assert text.index("SECRET-LEAK") < text.index("Conversation Branch 1") < text.index("fine")
    assert "<thinking>\nthey seem trustworthy\n</thinking>" in text
