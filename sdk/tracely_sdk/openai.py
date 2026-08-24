"""Drop-in OpenAI tracing — the non-patching alternative to `init(instrument=["openai"])` (R13).

For environments where global monkey-patching (the OpenInference/OpenLLMetry instrumentors) is
undesirable, wrap a client *instance* instead — only that client is traced, nothing global changes:

    from tracely_sdk.openai import OpenAI            # a pre-wrapped client class
    client = OpenAI()
    client.chat.completions.create(model="gpt-4o", messages=[...])   # GENERATION span, no patching

    # or wrap a client you already built:
    from tracely_sdk.openai import wrap_openai
    client = wrap_openai(OpenAI())

    # or the Langfuse-style module import:
    from tracely_sdk.openai import openai
    openai.OpenAI().chat.completions.create(...)

It emits the same `gen_ai.*`/`tracely.*` attributes as the manual `llm()` helper, so it flows
through the context processor (inheriting `tracely.trace(...)`) and the backend mapping identically.
Sync + async calls capture model · messages · output · usage · tool calls; streaming calls capture
output, reasoning text and time-to-first-token."""

from __future__ import annotations

from typing import Any

from . import set_io, set_usage
from ._wrap import wrap_method

try:  # the real OpenAI SDK is an optional dependency of this drop-in
    import openai as _openai
except ImportError as e:  # pragma: no cover
    raise ImportError("tracely_sdk.openai requires the OpenAI SDK — pip install openai") from e


def _capture(span: Any, resp: Any) -> None:
    """Best-effort capture of a (non-streaming) ChatCompletion onto the span."""
    try:
        choice = resp.choices[0]
        msg = choice.message
        out: dict[str, Any] = {"role": getattr(msg, "role", "assistant"), "content": msg.content}
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            out["tool_calls"] = [tc.model_dump() for tc in tool_calls]
            span.set_attribute(
                "tracely.tool_calls", [tc.function.name for tc in tool_calls if tc.function]
            )
        if getattr(choice, "finish_reason", None):
            out["finish_reason"] = choice.finish_reason
        set_io(span, output=out)
        usage = getattr(resp, "usage", None)
        if usage:
            set_usage(
                span,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_tokens=getattr(
                    getattr(usage, "prompt_tokens_details", None), "cached_tokens", None
                ),
            )
    except Exception:  # never let trace capture break the caller's call
        pass


def wrap_openai(client: Any) -> Any:
    """Trace an OpenAI/AsyncOpenAI client *instance* by wrapping its `chat.completions.create`
    on the instance only (no global patching). Returns the same client. Idempotent."""
    wrap_method(client.chat.completions, "create", _capture)
    return client


def OpenAI(*args: Any, **kwargs: Any) -> Any:
    """`openai.OpenAI(...)`, pre-wrapped for tracing."""
    return wrap_openai(_openai.OpenAI(*args, **kwargs))


def AsyncOpenAI(*args: Any, **kwargs: Any) -> Any:
    """`openai.AsyncOpenAI(...)`, pre-wrapped for tracing."""
    return wrap_openai(_openai.AsyncOpenAI(*args, **kwargs))


class _OpenAIProxy:
    """Mirrors the `openai` module but hands back traced clients — supports the Langfuse-style
    `from tracely_sdk.openai import openai`."""

    OpenAI = staticmethod(OpenAI)
    AsyncOpenAI = staticmethod(AsyncOpenAI)

    def __getattr__(self, name: str) -> Any:
        return getattr(_openai, name)


openai = _OpenAIProxy()
