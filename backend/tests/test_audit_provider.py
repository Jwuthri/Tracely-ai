"""`provider.run_chat` — the raw chat call both sides of an audit use. Faked at `httpx.post`.

It exists because `create_agent` owns the loop and runs the tools, and neither the target (whose
tools must NEVER run) nor the attacker (whose tools only mutate audit state) can live with that —
and because the LangChain client drops the provider's reasoning text.
"""

from __future__ import annotations

import httpx
import pytest

from tracely.config import settings
from tracely.infrastructure.llm import provider


class _Chat:
    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, *, headers, json, timeout):  # noqa: A002
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _completion(message: dict, **usage) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, **usage},
    })


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-server")
    monkeypatch.setattr(provider.time, "sleep", lambda s: None)

    def install(*responses):
        fake = _Chat(*responses)
        monkeypatch.setattr(httpx, "post", fake)
        return fake

    return install


TOOLS = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]


def test_run_chat_returns_unexecuted_tool_calls(chat):
    fake = chat(_completion({
        "content": None, "reasoning": "I should look this up",
        "reasoning_details": [{"type": "reasoning.text", "text": "I should look this up"}],
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "search", "arguments": '{"q": "order 7"}'}}],
    }, cost=0.0042))
    seen: dict = {}
    out = provider.run_chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=TOOLS, model="anthropic/claude-sonnet-5", on_usage=seen.update,
    )
    assert out["tool_calls"] == [{"id": "call_1", "name": "search", "args": {"q": "order 7"}}]
    assert out["reasoning"] == "I should look this up"  # what ChatOpenAI would have dropped
    assert out["message"]["reasoning_details"] and out["message"]["tool_calls"]
    assert out["usage"]["input_tokens"] == 120 and seen["cost_usd"] == 0.0042
    sent = fake.requests[0]
    assert sent["url"].endswith("/chat/completions") and sent["json"]["model"] == "anthropic/claude-sonnet-5"
    assert sent["headers"]["Authorization"] == "Bearer sk-server"
    assert sent["json"]["tools"] == TOOLS and sent["json"]["max_tokens"] == settings.llm_max_output_tokens


def test_run_chat_reports_malformed_tool_arguments_instead_of_raising(chat):
    chat(_completion({"content": "", "tool_calls": [
        {"id": "ok", "type": "function", "function": {"name": "search", "arguments": "{}"}},
        {"id": "bad", "type": "function", "function": {"name": "search", "arguments": '{"q": "trunc'}},
    ]}))
    out = provider.run_chat([{"role": "user", "content": "hi"}], tools=TOOLS)
    assert [c["id"] for c in out["tool_calls"]] == ["ok"]
    assert out["invalid_tool_calls"][0]["id"] == "bad" and "trunc" in out["invalid_tool_calls"][0]["raw"]


def test_run_chat_omits_tools_when_there_are_none(chat):
    fake = chat(_completion({"content": "hello"}))
    out = provider.run_chat([{"role": "user", "content": "hi"}], tools=[])
    assert "tools" not in fake.requests[0]["json"]  # an empty list is a 400 on several providers
    assert out["content"] == "hello" and out["tool_calls"] == []


def test_run_chat_fails_closed_without_a_project_key(chat, monkeypatch):
    """Inside `use_project_key` for a workspace with no key, the server's key must not be used —
    the same rule the LangChain path enforces in `get_chat_model`."""
    fake = chat(_completion({"content": "should never be reached"}))
    monkeypatch.setattr(provider, "_encrypted_key_for", lambda project_id: None)
    with provider.use_project_key("p1"), pytest.raises(RuntimeError, match="no OpenRouter API key"):
        provider.run_chat([{"role": "user", "content": "hi"}])
    assert fake.requests == []


def test_run_chat_retries_a_rate_limit_then_succeeds(chat):
    fake = chat(httpx.Response(429, json={"error": {"message": "slow down"}}), _completion({"content": "ok"}))
    assert provider.run_chat([{"role": "user", "content": "hi"}])["content"] == "ok"
    assert len(fake.requests) == 2


def test_run_chat_names_an_error_body_with_no_choices(chat):
    """OpenRouter reports credit exhaustion as HTTP 200 + `{error}`."""
    chat(httpx.Response(200, json={"error": {"message": "Insufficient credits"}}))
    with pytest.raises(RuntimeError, match="Insufficient credits"):
        provider.run_chat([{"role": "user", "content": "hi"}])


def test_run_chat_raises_context_overflow_as_its_own_type(chat):
    chat(httpx.Response(400, json={"error": {"message": "maximum context length is 8000 tokens"}}))
    with pytest.raises(provider.ContextOverflowError):
        provider.run_chat([{"role": "user", "content": "x" * 10}])
