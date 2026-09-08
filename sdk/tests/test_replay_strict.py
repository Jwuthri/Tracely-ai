"""Strict recorded replay (W1): a recorded execution can never silently become a live one.

The contract under test:
- `fixtures(None)`   → no replay requested; everything is live (unchanged).
- `fixtures(bundle)` → recorded mode, strict by default. A call with no recorded entry (missing),
  no entries left (exhausted), or recorded args that don't match (mismatch) raises `ReplayError`
  instead of running the real function. `{}` is an *explicitly empty* recording, not "live".
- `fixtures(bundle, strict=False)` → the legacy lenient behaviour (live fall-through, ordered
  serve on mismatch), still reported — never labelled recorded.
- Recorded args are JSON strings when the bundle came from ClickHouse; matching canonicalises
  both sides so a dict on the live side matches.
- The block yields a `ReplayReport` listing served / unused / diverged / live calls.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracely_sdk as tracely
from tracely_sdk import ReplayError, _args_match, _llm_input_match, _patch_class_method, _reconstruct_openai_chat


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


def _tool(name: str, args: object, output: object = "ok", error: str | None = None) -> dict:
    return {"name": name, "args": args, "output": output, "error": error}


# ── no replay vs explicitly empty replay ──────────────────────────────────────


def test_none_means_live() -> None:
    ran = []
    with tracely.fixtures(None) as rep:
        assert tracely.call_tool("t", lambda: ran.append(1) or "live") == "live"
    assert ran == [1]
    assert rep.mode == "live"


def test_empty_bundle_is_recorded_not_live() -> None:
    with tracely.fixtures({}) as rep, pytest.raises(ReplayError) as ei:
        tracely.call_tool("t", lambda: pytest.fail("must not run live"))
    assert ei.value.reason == "missing"
    assert rep.mode == "recorded"
    assert rep.errors and rep.errors[0].reason == "missing"


# ── missing / exhausted / mismatch ────────────────────────────────────────────


def test_missing_tool_raises_instead_of_running(exporter: InMemorySpanExporter) -> None:
    @tracely.observe(as_type="tool")
    def other_tool(x: str) -> str:
        pytest.fail("must not run live")

    with tracely.fixtures({"version": 2, "tools": [_tool("some_other", None)]}):
        with pytest.raises(ReplayError, match="no recorded call") as ei:
            other_tool("y")
    assert ei.value.kind == "tools" and ei.value.key == "other_tool"
    span = next(s for s in exporter.get_finished_spans() if s.name == "other_tool")
    assert span.status.status_code.name == "ERROR"


def test_exhausted_queue_raises() -> None:
    bundle = {"version": 2, "tools": [_tool("t", None, 1)]}
    with tracely.fixtures(bundle):
        assert tracely.call_tool("t", lambda: pytest.fail("live")) == 1
        with pytest.raises(ReplayError) as ei:
            tracely.call_tool("t", lambda: pytest.fail("live"))
    assert ei.value.reason == "exhausted"
    assert "1 recorded" in str(ei.value)


def test_args_mismatch_does_not_consume_a_different_call() -> None:
    bundle = {"version": 2, "tools": [_tool("lookup", {"id": "A"}, "for-A")]}
    with tracely.fixtures(bundle) as rep:
        with pytest.raises(ReplayError) as ei:
            tracely.call_tool("lookup", lambda: pytest.fail("live"), args={"id": "B"})
        # the A entry is still there — a mismatch must not eat it
        assert tracely.call_tool("lookup", lambda: pytest.fail("live"), args={"id": "A"}) == "for-A"
    assert ei.value.reason == "mismatch"
    assert ei.value.detail["recorded"] == [{"id": "A"}]
    assert rep.unused == []


def test_entry_recorded_without_args_matches_by_order() -> None:
    bundle = {"version": 2, "tools": [_tool("t", None, "first"), _tool("t", None, "second")]}
    with tracely.fixtures(bundle):
        assert tracely.call_tool("t", lambda: None, args={"any": 1}) == "first"
        assert tracely.call_tool("t", lambda: None, args={"other": 2}) == "second"


# ── canonical matching (ClickHouse hands back JSON strings) ───────────────────


def test_args_match_canonicalises_json_strings_and_key_order() -> None:
    assert _args_match('{"b": 2, "a": 1}', {"a": 1, "b": 2})
    assert _args_match({"a": [1, 2]}, {"a": (1, 2)})
    assert _args_match("plain", "plain")
    assert not _args_match('{"a": 1}', {"a": 2})
    assert _args_match(None, {"anything": True})  # no recorded args = ordered strategy


def test_recorded_json_string_args_match_live_dict() -> None:
    bundle = {"version": 2, "tools": [
        _tool("lookup", '{"id": "B"}', "for-B"),
        _tool("lookup", '{"id": "A"}', "for-A"),
    ]}
    with tracely.fixtures(bundle):
        assert tracely.call_tool("lookup", lambda: None, args={"id": "A"}) == "for-A"
        assert tracely.call_tool("lookup", lambda: None, args={"id": "B"}) == "for-B"


# ── LLM: presence is strict, input divergence is reported ─────────────────────


def test_llm_missing_raises() -> None:
    with tracely.fixtures({"version": 2, "llm": []}), pytest.raises(ReplayError) as ei:
        tracely.call_llm("gpt-4o", lambda: pytest.fail("live"))
    assert ei.value.kind == "llm" and ei.value.reason == "missing"


def test_llm_input_divergence_is_served_in_order_but_reported(
    exporter: InMemorySpanExporter,
) -> None:
    bundle = {"version": 2, "llm": [
        {"model": "gpt-4o", "input": [{"role": "user", "content": "old prompt"}],
         "output": "recorded", "error": None},
    ]}
    with tracely.fixtures(bundle) as rep:
        out = tracely.call_llm("gpt-4o", lambda: None, input=[{"role": "user", "content": "NEW"}])
    assert out == "recorded"
    assert len(rep.diverged) == 1 and rep.diverged[0].key == "gpt-4o"
    span = next(s for s in exporter.get_finished_spans() if s.name == "gpt-4o")
    assert dict(span.attributes)["tracely.replay.divergence"] == "input"


def test_llm_falls_back_to_recorded_span_name_when_model_id_differs() -> None:
    bundle = {"version": 2, "llm": [{"model": "ChatCompletion", "input": None, "output": "x", "error": None}]}
    with tracely.fixtures(bundle) as rep:
        assert tracely.call_llm("gpt-4o", lambda: None) == "x"
    assert rep.served == ["llm:ChatCompletion"]


# ── provider adapters follow the same policy ──────────────────────────────────


class _Completions:
    def create(self, *, model: str, messages: list) -> dict:
        return {"live": True}


def test_patched_provider_miss_raises_in_strict_mode() -> None:
    _patch_class_method(
        _Completions, "create", model_key="model",
        input_extractor=lambda kw: kw.get("messages"), reconstruct=_reconstruct_openai_chat,
    )
    with tracely.fixtures({"version": 2, "llm": []}), pytest.raises(ReplayError):
        _Completions().create(model="gpt-4o", messages=[])
    with tracely.fixtures(None):  # not replaying → live as before
        assert _Completions().create(model="gpt-4o", messages=[]) == {"live": True}


# ── lenient (legacy) mode ─────────────────────────────────────────────────────


def test_lenient_mode_falls_through_and_reports_live_calls() -> None:
    ran = []
    with tracely.fixtures({"version": 2, "tools": []}, strict=False) as rep:
        assert tracely.call_tool("t", lambda: ran.append(1) or "live") == "live"
    assert ran == [1]
    assert rep.mode == "lenient"
    assert rep.live == ["tools:t"]


def test_lenient_mismatch_serves_in_order_and_reports() -> None:
    bundle = {"version": 2, "tools": [_tool("lookup", {"id": "A"}, "for-A")]}
    with tracely.fixtures(bundle, strict=False) as rep:
        assert tracely.call_tool("lookup", lambda: None, args={"id": "B"}) == "for-A"
    assert [d.reason for d in rep.diverged] == ["mismatch"]


# ── the report ────────────────────────────────────────────────────────────────


def test_report_lists_unused_and_served() -> None:
    bundle = {"version": 2, "tools": [_tool("a", None, 1), _tool("b", None, 2)]}
    with tracely.fixtures(bundle) as rep:
        tracely.call_tool("a", lambda: None)
    assert rep.served == ["tools:a"]
    assert rep.unused == ["tools:b ×1"]
    assert rep.clean is False
    d = rep.to_dict()
    assert d["mode"] == "recorded" and d["unused"] == ["tools:b ×1"]


def test_clean_report_when_everything_replayed() -> None:
    with tracely.fixtures({"version": 2, "tools": [_tool("a", None, 1)]}) as rep:
        tracely.call_tool("a", lambda: None)
    assert rep.clean is True and rep.unused == [] and rep.errors == []


def test_llm_input_matches_on_the_same_user_turn():
    recorded = '[{"role": "system", "content": "be brief"}, {"role": "user", "content": "weather in NYC?"}]'
    assert _llm_input_match(recorded, "weather in NYC?")
    assert _llm_input_match(recorded, {"role": "user", "content": [{"type": "text", "text": "weather in NYC?"}]})
    assert _llm_input_match(recorded, '{"role": "user", "content": "weather in NYC?"}')
    assert not _llm_input_match(recorded, "weather in SF?")
    # the fallback needs an actual user turn on the recorded side; a system-only recording
    # matches nothing (exact equality still applies on its own)
    assert not _llm_input_match('[{"role": "system", "content": "be brief"}]', "hi")


def test_call_llm_with_a_prompt_does_not_diverge_from_a_recorded_messages_list():
    bundle = {"version": 2, "llm": [{"model": "gpt-4o", "input": '[{"role": "user", "content": "hi"}]', "output": "rec", "error": None}]}
    with tracely.fixtures(bundle) as rep:
        assert tracely.call_llm("gpt-4o", lambda: None, input="hi") == "rec"
    assert rep.diverged == [] and rep.clean
