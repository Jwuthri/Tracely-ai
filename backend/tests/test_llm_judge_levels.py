"""The multi-level / multi-output-type LLM judge, with the LangChain agent layer stubbed.

The judge calls `provider.run_structured_agent` (create_agent + response_format) for typed
outputs and `provider.run_text_agent` for the free-form `json` output type — tests patch those
two provider functions and exercise everything above them.
"""

from __future__ import annotations

import json

import pytest

from tracely.config import settings
from tracely.domain.evaluation.evaluators.base import CONVERSATION, RUN, SPAN
from tracely.domain.evaluation.evaluators.llm_judge import LLMJudgeEvaluator
from tracely.domain.evaluation.results import RunContext
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.llm import provider


def _span(**kw) -> dict:
    base = {
        "span_id": "s1", "parent_span_id": "", "type": "GENERATION", "name": "llm",
        "level": "DEFAULT", "status_message": "", "start_time": None, "end_time": None,
        "agent_id": "agent", "agent_run_id": "run-1", "turn_id": "", "step_id": "",
        "model_id": "m", "input": "hi", "output": "hello", "tool_call_names": [],
        "trace_id": "t1", "is_app_root": 1, "conversation_id": "",
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def judge_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")


def _judge(level: str) -> LLMJudgeEvaluator:
    ev = LLMJudgeEvaluator()
    ev.level = level
    ev.score_name = "probe"
    return ev


def _ctx(spans: list[dict], thread_id: str = "") -> RunContext:
    from tracely.domain.traces.spans import root_span

    return RunContext("p", "t1", "run-1", spans, root_span(spans), thread_id=thread_id)


def _stub_structured(monkeypatch, fields: dict, prompts: list | None = None, systems: list | None = None):
    """Patch run_structured_agent to build the requested response_format with canned fields."""

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        if prompts is not None:
            prompts.append(prompt)
        if systems is not None:
            systems.append(system_prompt)
        return response_format(**fields)

    monkeypatch.setattr(provider, "run_structured_agent", fake)


def test_trace_level_score_threshold(monkeypatch):
    systems: list = []
    _stub_structured(monkeypatch, {"score": 0.4, "reason": "meh"}, systems=systems)
    results = _judge(RUN).run(_ctx([_span()]), {"prompt": "Grade.", "threshold": 0.6})
    assert len(results) == 1
    r = results[0]
    assert (r.verdict, r.data_type, r.value, r.comment) == ("FAIL", "NUMERIC", 0.4, "meh")
    # the rubric rides as the agent's system prompt
    assert systems == ["Grade."]

    _stub_structured(monkeypatch, {"score": 0.9, "reason": "good"})
    assert _judge(RUN).run(_ctx([_span()]), {"threshold": 0.6})[0].verdict == "PASS"


def test_boolean_output(monkeypatch):
    _stub_structured(monkeypatch, {"passed": False, "reason": "leaked"})
    r = _judge(RUN).run(_ctx([_span()]), {"prompt": "PII?", "output_type": "boolean"})[0]
    assert (r.verdict, r.data_type, r.value) == ("FAIL", "BOOLEAN", 0.0)


def test_text_output(monkeypatch):
    _stub_structured(monkeypatch, {"text": "concise summary"})
    r = _judge(RUN).run(_ctx([_span()]), {"output_type": "text"})[0]
    assert (r.data_type, r.string_value, r.verdict) == ("TEXT", "concise summary", "")


def test_span_level_grades_each_step(monkeypatch):
    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "fine"}, prompts=prompts)
    spans = [
        _span(span_id="root", type="AGENT"),
        _span(span_id="tool-1", type="TOOL", name="lookup", parent_span_id="root"),
        _span(span_id="gen-1", type="GENERATION", parent_span_id="root"),
        _span(span_id="chain-1", type="CHAIN", parent_span_id="root"),
    ]
    results = _judge(SPAN).run(_ctx(spans), {"prompt": "Grade the step."})
    # a step is every event INSIDE the message — the message root is the message, not a step
    assert [r.target_span_id for r in results] == ["tool-1", "gen-1", "chain-1"]
    assert all(r.level == SPAN for r in results)
    assert "Step 1 of 3" in prompts[0]

    # span_types narrows the candidates
    only_tools = _judge(SPAN).run(_ctx(spans), {"span_types": ["TOOL"]})
    assert [r.target_span_id for r in only_tools] == ["tool-1"]


def test_conversation_level_builds_transcript(monkeypatch):
    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 0.2, "reason": "goal missed"}, prompts=prompts)
    spans = [
        _span(trace_id="t1", span_id="a", input="book a flight", output="which date?", conversation_id="th-9"),
        _span(trace_id="t2", span_id="b", input="tomorrow", output="booked!", conversation_id="th-9"),
    ]
    results = _judge(CONVERSATION).run(_ctx(spans, thread_id="th-9"), {"threshold": 0.6})
    assert len(results) == 1
    assert results[0].level == CONVERSATION
    assert results[0].verdict == "FAIL"
    assert "Turn 1 — user: book a flight" in prompts[0]
    assert "Turn 2 — agent: booked!" in prompts[0]
    assert "2 turns" in prompts[0]


def test_number_output(monkeypatch):
    _stub_structured(monkeypatch, {"value": 42.5, "reason": "counted"})
    r = _judge(RUN).run(_ctx([_span()]), {"output_type": "number"})[0]
    assert (r.data_type, r.value, r.verdict, r.comment) == ("NUMERIC", 42.5, "", "counted")
    # threshold turns it into a pass/fail check
    r2 = _judge(RUN).run(_ctx([_span()]), {"output_type": "number", "threshold": 50})[0]
    assert r2.verdict == "FAIL"


def test_json_without_schema_falls_back_to_freeform(monkeypatch):
    payload = {"score": 0.9, "issues": [], "reason": "clean"}
    monkeypatch.setattr(
        provider, "run_text_agent",
        lambda prompt, *, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_:
            "```json\n" + json.dumps(payload) + "\n```",
    )
    r = _judge(RUN).run(_ctx([_span()]), {"output_type": "json", "threshold": 0.5})[0]
    assert r.verdict == "PASS"
    assert json.loads(r.string_value) == payload


def test_json_with_schema_enforces_user_contract(monkeypatch):
    """The schema builder's stored JSON Schema compiles to the structured-output contract with
    EXACTLY the user's fields — nothing appended. Enum fields are Literal-enforced; a user-defined
    numeric `score` drives value/verdict and a `reasoning` field becomes the comment, while every
    field (score included) stays in string_value."""
    seen: dict = {}

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        seen["fields"] = dict(response_format.model_fields)
        return response_format(intent="complaint", score=0.2, reasoning="the user is upset")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    config = {
        "output_type": "json",
        "threshold": 0.5,
        "output_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["question", "complaint", "other"]},
                "score": {"type": "number"},
                "reasoning": {"type": "string"},
            },
            "required": ["intent", "score", "reasoning"],
        },
    }
    r = _judge(RUN).run(_ctx([_span()]), config)[0]
    # the contract carried only the user's fields — no envelope
    assert set(seen["fields"]) == {"intent", "score", "reasoning"}
    # the user-defined score drove value/verdict; reasoning became the comment; all fields kept
    assert (r.value, r.verdict, r.data_type) == (0.2, "FAIL", "TEXT")
    assert json.loads(r.string_value) == {"intent": "complaint", "score": 0.2, "reasoning": "the user is upset"}
    assert r.comment == "the user is upset"


def test_sequential_steps_chain_previous_result(monkeypatch):
    """execution_mode=sequential: step i+1 sees what step i DID (the run so far) and how it was
    graded. Batch grades each step alone — that is the whole difference between the modes."""
    prompts: list[str] = []

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        prompts.append(prompt)
        return response_format(score=0.4, reason=f"grade {len(prompts)}")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    spans = [
        _span(span_id="root", type="AGENT"),
        _span(span_id="tool-1", type="TOOL", name="lookup", parent_span_id="root",
              output="found order 42"),
        _span(span_id="tool-2", type="TOOL", name="update", parent_span_id="root"),
    ]
    results = _judge(SPAN).run(
        _ctx(spans), {"execution_mode": "sequential", "span_types": ["TOOL"], "threshold": 0.6}
    )
    assert len(results) == 2
    assert "Previous result of this metric" not in prompts[0]  # first item has no chain context
    assert "Steps already taken" not in prompts[0]
    assert "Previous result of this metric" in prompts[1]
    assert "grade 1" in prompts[1]  # the first grade's reason rode along
    # …and the step itself, not just its verdict: what the agent actually did before this one
    assert "Steps already taken" in prompts[1]
    assert "found order 42" in prompts[1]
    # batch mode never chains
    prompts.clear()
    _judge(SPAN).run(_ctx(spans), {"span_types": ["TOOL"]})
    assert all("Previous result of this metric" not in p for p in prompts)
    assert all("Steps already taken" not in p for p in prompts)


def test_trace_level_previous_result_seed(monkeypatch):
    """Thread runs seed cross-turn chaining via config.__previous_result__."""
    prompts: list[str] = []

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        prompts.append(prompt)
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    config = {"execution_mode": "sequential", "__previous_result__": {"value": 0.3, "verdict": "FAIL"}}
    _judge(RUN).run(_ctx([_span()]), config)
    assert "Previous result of this metric" in prompts[0]
    assert '"verdict": "FAIL"' in prompts[0]


def test_sequential_message_sees_the_earlier_turns(monkeypatch):
    """A sequential message judge grades turn N in the light of turns 1..N-1 — the conversation
    that led here, not just the previous verdict. Batch grades the message on its own."""
    prompts: list[str] = []

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        prompts.append(prompt)
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    thread = [
        _span(trace_id="t0", span_id="r0", input="where is my order?", output="order 42 shipped"),
        _span(trace_id="t1", span_id="r1", input="when does it arrive?", output="tomorrow"),
    ]
    ctx = RunContext(
        "p", "t1", "run-1", [thread[1]], thread[1], thread_id="c1", thread_spans=thread
    )
    _judge(RUN).run(ctx, {"execution_mode": "sequential"})
    assert "Conversation so far" in prompts[0]
    assert "where is my order?" in prompts[0]
    assert "when does it arrive?" not in prompts[0].split("User request:")[0]  # not the turn itself

    prompts.clear()
    _judge(RUN).run(ctx, {})
    assert "Conversation so far" not in prompts[0]


def test_the_graded_request_is_the_roots_last_user_message(monkeypatch):
    """The root's input is the whole message array — system prompt, earlier turns, then the new
    one. The request is its LAST user message, never the system prompt or turn 1."""
    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "ok"}, prompts=prompts)
    messages = json.dumps([
        {"role": "system", "content": "You are a returns specialist."},
        {"role": "user", "content": "where is my order?"},
        {"role": "assistant", "content": "it shipped"},
        {"role": "user", "content": [{"type": "text", "text": "refund the duplicate charge"}]},
    ])
    _judge(RUN).run(_ctx([_span(span_id="root", type="AGENT", input=messages, output="Refund started.")]), {})
    assert "User request:\nrefund the duplicate charge" in prompts[0]
    assert "You are a returns specialist." not in prompts[0].split("Agent answer:")[0]


def test_a_root_without_input_says_so_instead_of_borrowing_another_span(monkeypatch):
    """No guessing from child spans: a root that recorded no user message is shown to the judge
    (and in the prompt panel) as exactly that, so the instrumentation gap is visible."""
    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "ok"}, prompts=prompts)
    history = json.dumps([{"role": "user", "content": "refund the duplicate charge"}])
    spans = [
        _span(span_id="root", type="AGENT", input=None, output="Refund started."),
        _span(span_id="gen-1", type="GENERATION", parent_span_id="root", input=history),
    ]
    _judge(RUN).run(_ctx(spans), {})
    assert "User request:\n(no user message recorded on this turn's root span)" in prompts[0]


def test_no_key_skips_entirely(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_judge_api_key", "")
    assert _judge(RUN).run(_ctx([_span()]), {}) == []


def test_transport_error_is_a_visible_neutral_error(monkeypatch):
    def boom(prompt, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(provider, "run_structured_agent", boom)
    [r] = _judge(RUN).run(_ctx([_span()]), {})
    assert (r.string_value, r.verdict, r.graded) == ("Error", "", False)
    assert "connection refused" in r.comment


# ── token-usage capture (per-evaluator cost) ─────────────────────────────────
def test_judge_attaches_token_usage(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0, on_usage=None, **_):
        if on_usage:
            on_usage({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "model": "openai/gpt-5.4-nano"})
        return response_format(score=0.9, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    r = _judge(RUN).run(_ctx([_span()]), {"output_type": "score", "threshold": 0.5})[0]
    assert r.usage and r.usage["total_tokens"] == 120 and r.usage["model"] == "openai/gpt-5.4-nano"


def test_usage_metadata_maps_to_strings():
    from tracely.infrastructure.clickhouse.score_writer import _usage_metadata

    assert _usage_metadata(None) == {}
    m = _usage_metadata({"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "model": "x"})
    assert m == {
        "eval.input_tokens": "100", "eval.output_tokens": "20",
        "eval.total_tokens": "120", "eval.model": "x",
    }


# ── advanced (template) mode ─────────────────────────────────────────────────


def test_advanced_sends_the_resolved_template_as_the_human_message(monkeypatch):
    """is_advanced=True: the resolved `@VARIABLE` prompt IS the prompt — carried ONCE, as the human
    message, with NO auto-injected trace context and a fixed system preamble."""
    from tracely.domain.evaluation.evaluators.llm_judge import ADVANCED_SYSTEM

    systems: list = []
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 0.9, "reason": "ok"}, prompts=prompts, systems=systems)
    config = {
        "is_advanced": True,
        "prompt": "Grade: @CURRENT_MESSAGE.output (asked: @CURRENT_MESSAGE.input)",
        "threshold": 0.6,
    }
    results = _judge(RUN).run(_ctx([_span(input="ping", output="pong")]), config)
    assert len(results) == 1 and results[0].verdict == "PASS"
    assert prompts == ["Grade: pong (asked: ping)"]
    assert systems == [ADVANCED_SYSTEM]


def test_advanced_message_judge_can_grade_against_the_tool_results(monkeypatch):
    """The whole point of the advanced path at message level: the basic item is `[request,
    answer]`, so a faithfulness rubric has to ask for the evidence by name — and gets the tool
    and retrieval steps, not the model calls."""
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "grounded"}, prompts=prompts)
    spans = [
        _span(span_id="root", type="AGENT", input="when is my parcel collected?",
              output="Kept for 5 working days."),
        _span(span_id="tool-1", type="TOOL", name="pickup_policy", parent_span_id="root",
              is_app_root=0, input="{}", output="Parcels are held for 5 working days."),
        _span(span_id="gen-1", type="GENERATION", parent_span_id="root", is_app_root=0,
              input="system rubble", output="Kept for 5 working days."),
    ]
    config = {
        "is_advanced": True,
        "prompt": "Answer: @CURRENT_MESSAGE.output\n\nEvidence:\n@CURRENT_STEPS.tool",
        "threshold": 0.6,
    }
    assert [r.verdict for r in _judge(RUN).run(_ctx(spans), config)] == ["PASS"]
    assert "held for 5 working days" in prompts[0]  # the evidence reached the judge
    assert "system rubble" not in prompts[0]  # the model call did not


def test_advanced_history_uses_full_thread_spans(monkeypatch):
    """At message level @HISTORY is the WHOLE conversation — the service feeds `thread_spans` even
    though `ctx.spans` is only the current turn."""
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "ok"}, prompts=prompts)
    cur = [_span(trace_id="t2", span_id="b", input="tomorrow", output="booked!")]
    thread = [
        _span(trace_id="t1", span_id="a", input="book a flight", output="which date?"),
        _span(trace_id="t2", span_id="b", input="tomorrow", output="booked!"),
    ]
    ctx = RunContext("p", "t2", "run-1", cur, root_span(cur), thread_id="th-9", thread_spans=thread)
    _judge(RUN).run(ctx, {"is_advanced": True, "prompt": "@HISTORY"})
    assert "book a flight" in prompts[0] and "booked!" in prompts[0]


def test_advanced_step_grades_each_candidate_with_step_vars(monkeypatch):
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "fine"}, prompts=prompts)
    spans = [
        _span(span_id="root", type="AGENT", input="q", output="a"),
        _span(span_id="tool-1", type="TOOL", name="lookup", parent_span_id="root", is_app_root=0,
              input="x", output="result-1"),
        _span(span_id="tool-2", type="TOOL", name="update", parent_span_id="root", is_app_root=0,
              input="y", output="result-2"),
    ]
    config = {"is_advanced": True, "span_types": ["TOOL"],
              "prompt": "step @STEP_NUMBER: @CURRENT_STEP.tool_result"}
    results = _judge(SPAN).run(_ctx(spans), config)
    assert [r.target_span_id for r in results] == ["tool-1", "tool-2"]
    assert "step 1: result-1" in prompts[0]
    assert "step 2: result-2" in prompts[1]


def test_needs_thread_context_gates_on_conversation_scoped_vars():
    from tracely.services.evaluation_service import _needs_thread_context

    step_local = {"kind": "llm_judge", "config": {"is_advanced": True, "template_variables": ["CURRENT_STEP.tool_call"]}}
    convo = {"kind": "llm_judge", "config": {"is_advanced": True, "template_variables": ["HISTORY"]}}
    basic = {"kind": "llm_judge", "config": {"prompt": "grade it"}}
    assert _needs_thread_context([step_local]) is False  # step-local pays nothing
    assert _needs_thread_context([basic]) is False
    assert _needs_thread_context([convo]) is True


# ── an unanswered run is graded, not skipped ──────────────────────────────────


def test_a_run_with_no_answer_is_still_graded(monkeypatch):
    """It used to return no result at all, so the worst outcome there is — the agent crashed,
    timed out, or replied with nothing — produced no score and the cell read "not graded yet"."""
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 0.0, "reason": "no answer"}, prompts=prompts)
    spans = [_span(output="", input="where is my refund?")]
    results = _judge(RUN).run(_ctx(spans), {"prompt": "Grade.", "threshold": 0.6})
    assert [r.verdict for r in results] == ["FAIL"]
    assert "(no answer recorded on this turn's root span)" in prompts[0]


def test_a_run_with_neither_request_nor_answer_is_skipped(monkeypatch):
    """Nothing to grade is not the same as a failure — don't spend a judge call on it."""
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "x"})
    assert _judge(RUN).run(_ctx([_span(input="", output="")]), {"prompt": "Grade."}) == []


def test_include_answer_false_grades_the_user_message_alone(monkeypatch):
    """A classification column (intent) labels what the USER wanted: sending the agent's answer
    would both double the item and let the label follow what the agent DID."""
    prompts: list = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "x"}, prompts=prompts)
    spans = [_span(input="where is my refund?", output="Here are our shipping FAQs.")]
    _judge(RUN).run(_ctx(spans), {"prompt": "Label.", "include_answer": False})
    assert "where is my refund?" in prompts[0]
    assert "shipping FAQs" not in prompts[0]


def test_include_answer_false_skips_a_turn_with_no_user_message(monkeypatch):
    """No user message ⇒ no user intent to label — don't spend a call on the agent's monologue."""
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "x"})
    spans = [_span(input="", output="Your order shipped.")]
    assert _judge(RUN).run(_ctx(spans), {"prompt": "Label.", "include_answer": False}) == []


def test_an_advanced_judge_skips_a_turn_where_no_variable_resolved(monkeypatch):
    """The same rule for the ADVANCED path, which had no guard at all.

    Real exporters ship orphan fragments — a tool/chain span whose turn root never arrived, with
    no input and no output anywhere. Every `@VARIABLE` soft-misses, so the judge was handed a
    rubric made of `[No … available]` and asked for a verdict; it dutifully invented one, and 69
    of them clustered into "the agent provided no answer at all". Ingestion noise, one LLM call
    each."""
    _stub_structured(monkeypatch, {"score": 0.0, "reason": "no answer"})
    spans = [_span(type="CHAIN", name="agent_teams.tool", input="", output="", is_app_root=0,
                   parent_span_id="never-exported")]
    config = {"is_advanced": True, "prompt": "Request @CURRENT_MESSAGE.input\nAnswer @CURRENT_MESSAGE.output"}
    assert _judge(RUN).run(_ctx(spans), config) == []


def test_an_advanced_judge_still_grades_a_request_with_no_answer(monkeypatch):
    """The guard is "nothing resolved", not "something missing": an agent that answered nothing is
    a real failure, and the one that stops being graded the moment it breaks is useless."""
    _stub_structured(monkeypatch, {"score": 0.0, "reason": "no answer"})
    spans = [_span(input="where is my refund?", output="")]
    config = {"is_advanced": True, "threshold": 0.6,
              "prompt": "Request @CURRENT_MESSAGE.input\nAnswer @CURRENT_MESSAGE.output"}
    assert [r.verdict for r in _judge(RUN).run(_ctx(spans), config)] == ["FAIL"]


def test_an_advanced_rubric_with_no_variables_is_never_skipped(monkeypatch):
    """Nothing to miss is not the same as nothing to grade."""
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "x"})
    spans = [_span(input="", output="")]
    assert len(_judge(RUN).run(_ctx(spans), {"is_advanced": True, "prompt": "Always score 1."})) == 1


# ── the advanced template is sent once, not twice ─────────────────────────────


def test_the_advanced_template_is_not_sent_as_both_system_and_message(monkeypatch):
    """It used to ride in both slots: double the input tokens on every advanced grade, and a
    rubric repeated verbatim measurably degrades instruction-following."""
    from tracely.domain.evaluation.evaluators.llm_judge import ADVANCED_SYSTEM

    prompts: list = []
    systems: list = []
    _stub_structured(monkeypatch, {"score": 0.9, "reason": "ok"}, prompts=prompts, systems=systems)
    config = {"is_advanced": True, "prompt": "Grade @CURRENT_MESSAGE.output", "threshold": 0.6}
    _judge(RUN).run(_ctx([_span()]), config)
    assert systems == [ADVANCED_SYSTEM]
    assert prompts[0] != ADVANCED_SYSTEM and "Grade" in prompts[0]


# ── durable judge conversations ──────────────────────────────────────────────


def test_a_sequential_column_grades_on_one_chat_thread(monkeypatch):
    """Sequential holds ONE conversation with the model: rubric → item → verdict → item. The
    transcript is the continuity, so the prior verdict is no longer pasted into the prompt and the
    cached prefix carries everything but the new item."""
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", object())  # a reachable checkpointer
    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)
    seen: list = []

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0,
             on_usage=None, chat_id=None, **_):
        seen.append((chat_id, prompt))
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    thread = [
        _span(trace_id="t0", span_id="r0", input="first", output="a"),
        _span(trace_id="t1", span_id="r1", input="second", output="b"),
    ]
    ctx = RunContext("p", "t1", "run-1", [thread[1]], thread[1],
                     thread_id="c1", thread_spans=thread)
    # `__chain_pass__` marks the ordered whole-thread pass (stamped by evaluate_thread) — the one
    # context where a message-level judge may extend its durable conversation.
    _judge(RUN).run(ctx, {"execution_mode": "sequential", "__chain_pass__": True,
                          "__previous_result__": {"verdict": "FAIL"}})

    chat_id, prompt = seen[0]
    assert chat_id.endswith(":c1")
    # the thread already holds them, so re-sending would double the tokens and break the prefix
    assert "Previous result of this metric" not in prompt
    assert "Conversation so far" not in prompt

    # WITHOUT the marker (a lone re-grade of one mid-thread turn) the durable conversation must
    # not be extended — appending that turn again, out of order, is how re-runs used to corrupt
    # it. The judge falls back to pasting the earlier turns into its own prompt.
    seen.clear()
    _judge(RUN).run(ctx, {"execution_mode": "sequential",
                          "__previous_result__": {"verdict": "FAIL"}})
    chat_id, prompt = seen[0]
    assert chat_id is None
    assert "Conversation so far" in prompt and "Previous result of this metric" in prompt


def test_an_unreachable_checkpointer_falls_back_instead_of_degrading_to_batch(monkeypatch):
    """The memoized failure is a `False` sentinel; leaking it made `is None` answer "yes there is
    a checkpointer", so the judge dropped the pasted-in history AND had no thread to read it from.
    Sequential silently became batch."""
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)
    monkeypatch.setattr(checkpointer, "_saver", False)
    assert checkpointer.get_checkpointer() is None

    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "ok"}, prompts=prompts)
    _judge(RUN).run(_ctx([_span()]), {"execution_mode": "sequential",
                                      "__previous_result__": {"verdict": "FAIL"}})
    assert "Previous result of this metric" in prompts[0]


def test_an_advanced_column_never_opens_a_chat_thread(monkeypatch):
    """Advanced templates stay one-shot by design: the rubric and its resolved context are one
    blob in the human message, so chaining them would store that blob once per item inside the
    transcript. Sequential advanced chains through @METRIC_PREVIOUS_RESULT instead."""
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", object())  # a reachable checkpointer
    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)
    seen: list = []

    def fake(prompt, *, response_format, system_prompt=None, model=None, temperature=0.0,
             on_usage=None, chat_id=None, **_):
        seen.append(chat_id)
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    _judge(RUN).run(_ctx([_span()]), {
        "is_advanced": True, "prompt": "Grade @CURRENT_MESSAGE.output",
        "execution_mode": "sequential",
    })
    assert seen == [None]


def test_chained_steps_record_the_earlier_steps(monkeypatch):
    """On a chat thread the earlier steps stay on the conversation instead of going over the wire
    again — so they must reach the RECORDING, or step 3's trace row is a batch grade's row."""
    from tracely.domain import introspection

    def fake(prompt, *, response_format, system_prompt=None, **_):
        with provider._recorded(prompt, system_prompt, None) as sink:  # the real recording seam
            sink.append(("{}", {}))
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    monkeypatch.setattr(
        "tracely.domain.evaluation.evaluators.llm_judge._chat_id", lambda *a, **k: "chat-1"
    )
    spans = [
        _span(span_id="root", type="AGENT"),
        _span(span_id="tool-1", type="TOOL", name="faq", parent_span_id="root",
              output="30 days with a receipt"),
        _span(span_id="tool-2", type="TOOL", name="echo", parent_span_id="root"),
    ]
    rec = introspection.Recording(kind=introspection.EVAL, subject_id="t1", name="eval", project_id="p")
    token = introspection._active.set(rec)
    try:
        _judge(SPAN).run(
            _ctx(spans), {"execution_mode": "sequential", "span_types": ["TOOL"], "threshold": 0.6}
        )
    finally:
        introspection._active.reset(token)

    assert rec.context == ""  # consumed by the call it described
    first, second = (s.input for s in rec.steps)
    assert "Step 1 of 2" in first and "Step 2 of 2" not in first
    # step 2's row shows the run so far, then its own turn — in the order the model read them
    assert second.index("Step 1 of 2") < second.index("Step 2 of 2")
    assert "30 days with a receipt" in second


def test_a_step_chat_is_per_message_and_reset_before_step_one(monkeypatch):
    """A step judge's conversation spans the steps of ONE message — keyed by the trace, not the
    thread (message 2's steps must not continue message 1's conversation) — and every `_run_steps`
    call is a complete pass, so it resets the conversation first: a re-graded trace rebuilds from
    step 1 instead of appending a second copy of every step."""
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", object())
    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)
    resets: list[str] = []
    monkeypatch.setattr(checkpointer, "reset_chat", resets.append)
    seen: list = []

    def fake(prompt, *, response_format, chat_id=None, **_):
        seen.append((chat_id, prompt))
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    spans = [
        _span(span_id="root", type="AGENT"),
        _span(span_id="s1", type="TOOL", name="faq", parent_span_id="root", output="found"),
        _span(span_id="s2", type="TOOL", name="echo", parent_span_id="root"),
    ]
    ctx = RunContext("p", "t1", "run-1", spans, spans[0], thread_id="c1")
    _judge(SPAN).run(ctx, {
        "execution_mode": "sequential", "span_types": ["TOOL"],
        "__previous_result__": {"verdict": "FAIL", "reason": "prior turn"},
    })

    assert resets == ["p:probe:t1"]  # trace-keyed, cleared once before the pass
    assert [c for c, _ in seen] == ["p:probe:t1", "p:probe:t1"]
    # the cross-turn seed lands on the first step only — the fresh conversation carries the rest
    assert "Previous result of this metric" in seen[0][1]
    assert "prior turn" in seen[0][1]
    assert "Previous result of this metric" not in seen[1][1]
    assert "Steps already taken" not in seen[1][1]  # the transcript, not a paste, is the context


def test_a_conversation_level_column_never_chats(monkeypatch):
    """The thread is one item; each settle-pass re-grades it whole. A durable conversation would
    only show the judge stale copies of the same transcript."""
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", object())
    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)
    seen: list = []

    def fake(prompt, *, response_format, chat_id=None, **_):
        seen.append(chat_id)
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    thread = [
        _span(trace_id="t0", span_id="r0", input="hi", output="hello"),
        _span(trace_id="t1", span_id="r1", input="bye", output="later"),
    ]
    ctx = RunContext("p", "", "", thread, thread[0], thread_id="c1")
    _judge(CONVERSATION).run(ctx, {"execution_mode": "sequential"})
    assert seen == [None]


def test_a_chain_pass_message_row_records_the_earlier_turns(monkeypatch):
    """With the earlier turns riding on the chat thread instead of the wire, the recording must
    re-render them or a chained message grade's INPUT reads like a batch grade's."""
    from tracely.domain import introspection
    from tracely.infrastructure.llm import checkpointer

    monkeypatch.setattr(checkpointer, "_saver", object())
    monkeypatch.setattr(checkpointer.settings, "eval_chat_enabled", True)

    def fake(prompt, *, response_format, system_prompt=None, **_):
        with provider._recorded(prompt, system_prompt, None) as sink:
            sink.append(("{}", {}))
        return response_format(score=1.0, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", fake)
    thread = [
        _span(trace_id="t0", span_id="r0", input="first question", output="first answer"),
        _span(trace_id="t1", span_id="r1", input="second question", output="second answer"),
    ]
    ctx = RunContext("p", "t1", "run-1", [thread[1]], thread[1],
                     thread_id="c1", thread_spans=thread)
    rec = introspection.Recording(kind=introspection.EVAL, subject_id="t1", name="eval", project_id="p")
    token = introspection._active.set(rec)
    try:
        _judge(RUN).run(ctx, {"execution_mode": "sequential", "__chain_pass__": True})
    finally:
        introspection._active.reset(token)

    (row,) = rec.steps
    assert "first question" in row.input and "first answer" in row.input
    assert row.input.index("first question") < row.input.index("second question")


def test_malformed_traces_grade_or_skip_predictably(monkeypatch):
    """Partial/broken traces: a message with SOME content still grades (stating what's missing);
    only a message with neither request nor answer, an empty thread, or steps without I/O are
    skipped — silently producing nothing is reserved for 'genuinely nothing to grade'."""
    prompts: list[str] = []
    _stub_structured(monkeypatch, {"score": 1.0, "reason": "ok"}, prompts=prompts)

    # message with no I/O at all → no result
    silent = _span(input="", output="")
    assert _judge(RUN).run(_ctx([silent]), {}) == []

    # a request with no answer still grades, saying so
    half = _span(input="hello?", output="")
    assert len(_judge(RUN).run(_ctx([half]), {})) == 1
    assert "(no answer recorded on this turn's root span)" in prompts[-1]

    # step level: spans with no input/output are not gradable items
    spans = [
        _span(span_id="root", type="AGENT"),
        _span(span_id="empty", type="TOOL", parent_span_id="root", input="", output=""),
        _span(span_id="real", type="TOOL", parent_span_id="root", input="q", output="a"),
    ]
    results = _judge(SPAN).run(_ctx(spans), {"span_types": ["TOOL"]})
    assert [r.target_span_id for r in results] == ["real"]

    # conversation level: a thread with no renderable turns → no result
    assert _judge(CONVERSATION).run(_ctx([silent], thread_id="c1"), {}) == []
