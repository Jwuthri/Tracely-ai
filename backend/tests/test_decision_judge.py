"""Decision-model columns (TypeSafe Jev on OpenRouter's Decisions API) and the per-model context
gate every judge call goes through.

The Decisions API is faked at the httpx layer (`httpx.post`), so the request body the provider
builds is asserted byte-for-byte; the LLM fallback is faked at `run_structured_agent`, like the
rest of the judge suite.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tracely.config import settings
from tracely.domain.evaluation import budget, decision
from tracely.domain.evaluation.evaluators.base import RUN
from tracely.domain.evaluation.evaluators.llm_judge import LLMJudgeEvaluator
from tracely.domain.evaluation.results import RunContext
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.llm import provider

JEV = "typesafe/jev-1.13"


def _span(**kw) -> dict:
    base = {
        "span_id": "s1",
        "parent_span_id": "",
        "type": "GENERATION",
        "name": "llm",
        "level": "DEFAULT",
        "status_message": "",
        "start_time": None,
        "end_time": None,
        "agent_id": "agent",
        "agent_run_id": "run-1",
        "turn_id": "",
        "step_id": "",
        "model_id": "m",
        "input": "refund my order",
        "output": "Refund issued.",
        "tool_call_names": [],
        "trace_id": "t1",
        "is_app_root": 1,
        "conversation_id": "",
    }
    base.update(kw)
    return base


def _ctx(spans: list[dict]) -> RunContext:
    return RunContext("p", "t1", "run-1", spans, root_span(spans))


def _judge() -> LLMJudgeEvaluator:
    ev = LLMJudgeEvaluator()
    ev.level = RUN
    ev.score_name = "probe"
    return ev


@pytest.fixture(autouse=True)
def or_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")


class _Decisions:
    """Fake `httpx.post` for the Decisions API: records each request, replays canned responses."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, *, headers, json, timeout):  # noqa: A002 — httpx's own kwarg name
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _ok(answer: dict, input_tokens: int = 300, *, answers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "gen-dec-1",
            "model": "typesafe/jev-1.13-20260917",
            "provider": "TypeSafe",
            "answers": answers if answers is not None else {"q": answer},
            "usage": {"input_tokens": input_tokens, "output_tokens": 20, "cost": 0.0000126},
        },
    )


def _install(monkeypatch, fake: _Decisions) -> _Decisions:
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setattr(provider.time, "sleep", lambda s: None)
    return fake


NOUL = {
    "model": JEV,
    "output_type": "decision_binary",
    "question": "Did the agent complete the task?",
    "criteria": {"true": "Task done", "false": "Task not done"},
    "threshold": 0.5,
}
CHOICE = {
    "model": JEV,
    "output_type": "decision_multiclass",
    "question": "What went wrong?",
    "criteria": {"ok": "Nothing", "wrong": "Wrong answer", "refused": None},
    "fail_options": ["wrong", "refused"],
}
MULTILABEL = {
    "model": JEV,
    "output_type": "decision_multilabel",
    "question": "What is the user asking about?",
    "criteria": {
        "refund": "Getting money back",
        "shipping": None,
        "complaint": "Unhappy with service",
    },
    "fail_options": ["complaint"],
    "threshold": 0.5,
}


# ── the wire ─────────────────────────────────────────────────────────────────


def test_noul_request_shape_and_result(monkeypatch):
    fake = _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 0.93})))
    [r] = _judge().run(_ctx([_span()]), dict(NOUL))

    req = fake.requests[0]
    assert req["url"] == settings.openrouter_decisions_url
    assert req["headers"]["Authorization"] == "Bearer test-key"
    body = req["json"]
    assert body["model"] == JEV
    assert "refund my order" in body["state"] and "Refund issued." in body["state"]
    assert body["questions"] == {
        "q": {
            "type": "noul",
            "instructions": "Did the agent complete the task?",
            "criteria": {"true": "Task done", "false": "Task not done"},
        }
    }
    assert (r.verdict, r.data_type, r.value) == ("PASS", "NUMERIC", 0.93)
    assert "P(yes)=0.93" in r.comment
    assert r.usage["input_tokens"] == 300 and r.usage["model"] == JEV


def test_noul_pass_when_no_inverts_the_verdict_not_the_question(monkeypatch):
    fake = _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 0.9})))
    cfg = {**NOUL, "question": "Did the agent hallucinate?", "pass_when": "no"}
    [r] = _judge().run(_ctx([_span()]), cfg)
    assert r.verdict == "FAIL" and r.value == 0.9
    # the question goes out exactly as written — never negated behind the user's back
    assert (
        fake.requests[0]["json"]["questions"]["q"]["instructions"] == "Did the agent hallucinate?"
    )


def test_choice_fail_options_and_informational(monkeypatch):
    ans = {
        "type": "choice",
        "choice": "wrong",
        "confidence": 0.8,
        "probabilities": {"ok": 0.1, "wrong": 0.85, "refused": 0.05},
    }
    _install(monkeypatch, _Decisions(_ok(ans)))
    [r] = _judge().run(_ctx([_span()]), dict(CHOICE))
    assert (r.verdict, r.data_type, r.string_value, r.value) == (
        "FAIL",
        "CATEGORICAL",
        "wrong",
        0.8,
    )
    assert r.comment == "wrong (confidence 0.80)"  # stable text: failure clustering groups on it

    _install(monkeypatch, _Decisions(_ok({**ans, "choice": "ok"})))
    [r] = _judge().run(_ctx([_span()]), dict(CHOICE))
    assert r.verdict == "PASS"

    # no fail options marked → an informational label, never a verdict
    _install(monkeypatch, _Decisions(_ok(ans)))
    [r] = _judge().run(_ctx([_span()]), {**CHOICE, "fail_options": []})
    assert r.verdict == "" and r.string_value == "wrong"


def test_choice_null_description_is_sent_as_null(monkeypatch):
    fake = _install(
        monkeypatch, _Decisions(_ok({"type": "choice", "choice": "ok", "confidence": 1}))
    )
    _judge().run(_ctx([_span()]), dict(CHOICE))
    assert fake.requests[0]["json"]["questions"]["q"]["criteria"]["refused"] is None


def test_multilabel_fans_out_one_noul_per_label_in_one_request(monkeypatch):
    fake = _install(
        monkeypatch,
        _Decisions(
            _ok(
                {},
                answers={
                    "l0": {"type": "noul", "noul": 0.91},
                    "l1": {"type": "noul", "noul": 0.12},
                    "l2": {"type": "noul", "noul": 0.64},
                },
            )
        ),
    )
    [r] = _judge().run(_ctx([_span()]), dict(MULTILABEL))

    assert len(fake.requests) == 1  # one call, every label answered in parallel
    qs = fake.requests[0]["json"]["questions"]
    assert list(qs) == ["l0", "l1", "l2"]
    assert all(q["type"] == "noul" for q in qs.values())
    assert (
        "`refund`" in qs["l0"]["instructions"]
        and "What is the user asking about?" in qs["l0"]["instructions"]
    )
    assert qs["l0"]["criteria"]["true"] == "Getting money back"
    assert qs["l1"]["criteria"]["true"] == "`shipping` applies"  # no description → literal default

    assert (r.data_type, r.string_value, r.value) == ("CATEGORICAL", "refund, complaint", 2.0)
    assert r.verdict == "FAIL"  # `complaint` fired and is a fail label
    assert r.comment == "applies: refund 0.91, complaint 0.64 (threshold 0.5)"  # label order


def test_multilabel_nothing_fired_and_no_fail_labels(monkeypatch):
    low = {f"l{i}": {"type": "noul", "noul": 0.1} for i in range(3)}
    _install(monkeypatch, _Decisions(_ok({}, answers=low)))
    [r] = _judge().run(_ctx([_span()]), dict(MULTILABEL))
    assert (r.verdict, r.string_value) == ("PASS", "none")
    _install(monkeypatch, _Decisions(_ok({}, answers=low)))
    [r] = _judge().run(_ctx([_span()]), {**MULTILABEL, "fail_options": []})
    assert r.verdict == ""  # informational tags


def test_multilabel_unanswered_label_writes_no_score(monkeypatch):
    _install(monkeypatch, _Decisions(_ok({}, answers={"l0": {"type": "noul", "noul": 1}})))
    assert _judge().run(_ctx([_span()]), dict(MULTILABEL)) == []


def test_multilabel_fallback_selects_labels(monkeypatch):
    _install(
        monkeypatch,
        _Decisions(
            httpx.Response(
                400,
                json={
                    "error": {
                        "message": 'HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}'
                    }
                },
            )
        ),
    )
    seen: dict = {}

    def llm(prompt, *, response_format, system_prompt=None, **_):
        seen["system"] = system_prompt
        seen["fields"] = {k: f.description for k, f in response_format.model_fields.items()}
        return response_format(l0=0.2, l1=0.8, l2=0.4)

    monkeypatch.setattr(provider, "run_structured_agent", llm)
    [r] = _judge().run(_ctx([_span()]), {**MULTILABEL, "fallback_model": "openai/gpt-5.4-mini"})
    assert "Judge each label on its own" in seen["system"]
    # one probability per label, described by the label — same threshold semantics as Jev's nouls
    assert seen["fields"]["l0"] == "probability that label `refund` applies (Getting money back)"
    assert seen["fields"]["l1"] == "probability that label `shipping` applies"
    assert (r.verdict, r.string_value) == ("PASS", "shipping")


def test_rate_limit_is_retried(monkeypatch):
    fake = _install(
        monkeypatch,
        _Decisions(
            httpx.Response(429, json={"error": {"code": 429, "message": "slow down"}}),
            httpx.Response(529, json={"error": {"code": 529, "message": "overloaded"}}),
            _ok({"type": "noul", "noul": 0.7}),
        ),
    )
    [r] = _judge().run(_ctx([_span()]), dict(NOUL))
    assert len(fake.requests) == 3 and r.verdict == "PASS"


def test_other_errors_write_no_score(monkeypatch):
    _install(monkeypatch, _Decisions(httpx.Response(401, json={"error": {"message": "bad key"}})))
    assert _judge().run(_ctx([_span()]), dict(NOUL)) == []


def test_no_openrouter_key_makes_no_call(monkeypatch):
    fake = _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 1})))
    monkeypatch.setattr(provider, "_encrypted_key_for", lambda pid: None)
    with provider.use_project_key("p"):
        assert _judge().run(_ctx([_span()]), dict(NOUL)) == []
    assert fake.requests == []


def test_decision_model_never_reaches_the_chat_path():
    with pytest.raises(RuntimeError, match="decision model"):
        provider.get_chat_model(JEV)


# ── the context gate ─────────────────────────────────────────────────────────


def _huge() -> list[dict]:
    """A message with 150 tool steps. `@CURRENT_STEPS` clips each field but has no total cap, so
    this resolves to well over Jev's 32k-token budget — the real-world way an item overflows."""
    root = _span(span_id="root", type="AGENT", name="agent")
    steps = [
        _span(
            span_id=f"t{i}",
            parent_span_id="root",
            type="TOOL",
            name=f"tool{i}",
            is_app_root=0,
            input="lookup " * 400,
            output="result row " * 300,
        )
        for i in range(150)
    ]
    return [root, *steps]


ADVANCED_NOUL = {**NOUL, "is_advanced": True, "prompt": "Steps:\n@CURRENT_STEPS"}


def test_over_budget_without_fallback_is_a_visible_neutral_skip(monkeypatch):
    fake = _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 1})))
    [r] = _judge().run(_ctx(_huge()), dict(ADVANCED_NOUL))
    assert fake.requests == []  # never spent a call the provider would reject
    assert (r.verdict, r.data_type, r.string_value) == ("", "TEXT", "Skipped — input too long")
    assert "32k" in r.comment and JEV in r.comment and "fallback" in r.comment


def test_over_budget_goes_to_the_fallback_llm_with_the_same_answer_space(monkeypatch):
    fake = _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 1})))
    seen: dict = {}

    def llm(prompt, *, response_format, system_prompt=None, model=None, on_usage=None, **_):
        seen.update(model=model, system=system_prompt, fields=set(response_format.model_fields))
        return response_format(probability_yes=0.2)

    monkeypatch.setattr(provider, "run_structured_agent", llm)
    cfg = {**ADVANCED_NOUL, "fallback_model": "google/gemini-3.6-flash"}
    [r] = _judge().run(_ctx(_huge()), cfg)
    assert fake.requests == []
    assert seen["model"] == "google/gemini-3.6-flash"
    assert seen["fields"] == {"probability_yes"}
    assert "Did the agent complete the task?" in seen["system"]
    # same shape as a Jev answer would have produced — the column never changes data type
    assert (r.verdict, r.data_type, r.value) == ("FAIL", "NUMERIC", 0.2)
    assert r.comment.startswith("[graded by fallback google/gemini-3.6-flash")


def test_provider_rejection_for_length_also_falls_back(monkeypatch):
    # the body OpenRouter actually returns for an over-budget Jev request (seen live)
    _install(
        monkeypatch,
        _Decisions(
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": 'HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}',
                    }
                },
            )
        ),
    )
    monkeypatch.setattr(
        provider,
        "run_structured_agent",
        lambda prompt, *, response_format, **_: response_format(label="ok", confidence=0.9),
    )
    [r] = _judge().run(_ctx([_span()]), {**CHOICE, "fallback_model": "openai/gpt-5.4-mini"})
    assert (r.verdict, r.string_value) == ("PASS", "ok")
    assert "fallback" in r.comment


def test_llm_column_context_gate_uses_catalog_context(monkeypatch):
    """Generic per model: a plain LLM column whose item overflows its model's catalog context
    goes to the fallback too."""
    monkeypatch.setattr(
        provider,
        "_openrouter_models",
        lambda: {
            "openai/tiny": {"name": "tiny", "context_length": 8_000, "output_modalities": ["text"]},
            "openai/big": {
                "name": "big",
                "context_length": 1_000_000,
                "output_modalities": ["text"],
            },
        },
    )
    used: list = []

    def llm(prompt, *, response_format, model=None, **_):
        used.append(model)
        return response_format(passed=True, reason="ok")

    monkeypatch.setattr(provider, "run_structured_agent", llm)
    cfg = {
        "model": "openai/tiny",
        "fallback_model": "openai/big",
        "output_type": "boolean",
        "is_advanced": True,
        "prompt": "Judge @CURRENT_STEPS",
    }
    [r] = _judge().run(_ctx(_huge()), cfg)
    assert used == ["openai/big"] and r.verdict == "PASS"

    used.clear()
    [r] = _judge().run(_ctx(_huge()[:2]), cfg)  # a short item stays on the primary
    assert used == ["openai/tiny"]


def test_llm_overflow_error_is_classified():
    assert provider.is_context_overflow("This model's maximum context length is 128000 tokens")
    assert provider.is_context_overflow("prompt is too long: 250000 tokens > 200000 maximum")
    assert provider.is_context_overflow('HTTP 400: {"detail":{"error_type":"max_tokens_exceeded"}}')
    assert not provider.is_context_overflow("Rate limit exceeded")


# ── budget + catalog ─────────────────────────────────────────────────────────


def test_budget_math():
    # a lower bound: 8 bytes/token, under anything real text reaches (prose ~6, JSON/code ~2.4)
    assert budget.estimate_tokens("abcdefgh" * 10) == 10
    assert budget.estimate_tokens("é" * 4) == 1  # bytes, not characters
    dec = {"kind": "decision", "context_tokens": 32_000}
    assert budget.fits(dec, 32_000, reserved_output=4096)  # decision models emit no text
    assert not budget.fits(dec, 32_001)
    llm = {"kind": "llm", "context_tokens": 10_000}
    assert not budget.fits(llm, 7_000, reserved_output=4096)
    assert budget.fits({"kind": "llm", "context_tokens": None}, 10**9)


def test_jev_is_listed_as_a_decision_model_despite_missing_from_models(monkeypatch):
    monkeypatch.setattr(
        provider, "_openrouter_model_names", lambda: {"openai/gpt-5.4-nano": "Nano"}
    )
    models = provider.list_models()
    jev = next(m for m in models if m["id"] == JEV)
    assert jev == {
        "id": JEV,
        "label": "TypeSafe Jev 1.13",
        "kind": "decision",
        "context_tokens": 32000,
    }
    assert provider.model_info(JEV)["kind"] == "decision"
    assert provider.model_pricing(JEV) == (0.042, 0.0)


def test_jev_not_offered_without_an_openrouter_key(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_judge_api_key", "legacy")
    assert all(m["kind"] == "llm" for m in provider.list_models())


# ── config validation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"question": " "}, "question is required"),
        ({"criteria": {"true": "yes"}}, '"false"'),
        ({"threshold": 1.0}, "strictly between 0 and 1"),
        ({"pass_when": "maybe"}, "pass_when"),
    ],
)
def test_noul_validation(patch, needle):
    assert needle in (decision.validate({**NOUL, **patch}) or "")


@pytest.mark.parametrize(
    "patch, needle",
    [
        ({"criteria": {"only": None}}, "between 2 and 255"),
        ({"criteria": {f"o{i}": None for i in range(256)}}, "between 2 and 255"),
        ({"fail_options": ["nope"]}, "subset"),
        ({"fail_options": ["ok", "wrong", "refused"]}, "every label"),
    ],
)
def test_choice_validation(patch, needle):
    assert needle in (decision.validate({**CHOICE, **patch}) or "")


def test_multilabel_validation():
    assert "between 2 and 50" in decision.validate(
        {**MULTILABEL, "criteria": {f"l{i}": None for i in range(51)}}
    )
    assert "strictly between 0 and 1" in decision.validate({**MULTILABEL, "threshold": 0})
    # unlike multi-class, every label may be a failure label (a "violations" column)
    assert (
        decision.validate({**MULTILABEL, "fail_options": ["refund", "shipping", "complaint"]})
        is None
    )
    assert decision.validate(NOUL) is None and decision.validate(CHOICE) is None
    assert decision.validate(MULTILABEL) is None
    # the retired Jev `score` primitive is not an output type
    assert "output_type must be one of" in decision.validate(
        {**NOUL, "output_type": "decision_score"}
    )


def test_recording_carries_request_and_answer(monkeypatch):
    """The cell's prompt panel reads the eval recording — it must show what Jev actually read."""
    from tracely.domain import introspection

    _install(monkeypatch, _Decisions(_ok({"type": "noul", "noul": 0.6})))
    added: list = []

    class Rec:
        context = ""

        def add(self, label, **kw):
            added.append(kw)

    monkeypatch.setattr(introspection, "active", lambda: Rec())
    _judge().run(_ctx([_span()]), dict(NOUL))
    [kw] = added
    assert json.loads(kw["input"])["questions"]["q"]["type"] == "noul"
    assert json.loads(kw["output"]) == {"q": {"type": "noul", "noul": 0.6}}
    assert kw["tokens"]["input_tokens"] == 300
