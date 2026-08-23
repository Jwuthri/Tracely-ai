"""Drop-in Anthropic (Claude) tracing — the non-patching alternative to
`init(instrument=["anthropic"])` (R13). Mirrors `tracely_sdk.openai`: wrap a client *instance*, no
global patching.

    from tracely_sdk.anthropic import Anthropic       # a pre-wrapped client
    client = Anthropic()
    client.messages.create(model="claude-3-5-sonnet-latest", max_tokens=256, messages=[...])

    # or wrap one you already built / the module import:
    from tracely_sdk.anthropic import wrap_anthropic, anthropic
    client = wrap_anthropic(Anthropic())
    anthropic.Anthropic().messages.create(...)

Captures model · messages · output (text + tool_use blocks) · token usage · tool calls for
non-streaming sync + async calls; emits the same attributes as the manual `llm()` helper."""

from __future__ import annotations

from typing import Any

from . import set_io, set_usage
from ._wrap import anthropic_delta, wrap_method

try:  # the real Anthropic SDK is an optional dependency of this drop-in
    import anthropic as _anthropic
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "tracely_sdk.anthropic requires the Anthropic SDK — pip install anthropic"
    ) from e


def _capture(span: Any, resp: Any) -> None:
    """Best-effort capture of a (non-streaming) Anthropic Message onto the span."""
    try:
        blocks: list[dict[str, Any]] = []
        tool_names: list[str] = []
        for b in getattr(resp, "content", None) or []:
            btype = getattr(b, "type", None)
            if btype == "text":
                blocks.append({"type": "text", "text": b.text})
            elif btype == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                tool_names.append(b.name)
            else:
                blocks.append({"type": str(btype)})
        out: dict[str, Any] = {"role": "assistant", "content": blocks}
        if getattr(resp, "stop_reason", None):
            out["stop_reason"] = resp.stop_reason
        set_io(span, output=out)
        if tool_names:
            span.set_attribute("tracely.tool_calls", tool_names)
        usage = getattr(resp, "usage", None)
        if usage:
            set_usage(
                span,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", None),
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
            )
    except Exception:  # never let trace capture break the caller's call
        pass


def _input_with_system(kwargs: dict) -> Any:
    """Anthropic carries the system prompt as a separate `system=` kwarg, not in `messages`. Fold it
    in as a leading system message so the captured input reads as a full conversation (system + the
    user/assistant turns) like the OpenAI shape — otherwise the system prompt is invisible in the UI."""
    msgs = kwargs.get("messages")
    system = kwargs.get("system")
    if system and isinstance(msgs, list):
        return [{"role": "system", "content": system}, *msgs]
    return msgs


def wrap_anthropic(client: Any) -> Any:
    """Trace an Anthropic/AsyncAnthropic client *instance* by wrapping its `messages.create` on the
    instance only (no global patching). Returns the same client. Idempotent."""
    wrap_method(
        client.messages,
        "create",
        _capture,
        input_extractor=_input_with_system,
        # Anthropic streams its own event shape, and extended thinking arrives as
        # `thinking_delta` before any `text_delta` — the boundary we mark.
        stream_delta=anthropic_delta,
    )
    return client


def Anthropic(*args: Any, **kwargs: Any) -> Any:
    """`anthropic.Anthropic(...)`, pre-wrapped for tracing."""
    return wrap_anthropic(_anthropic.Anthropic(*args, **kwargs))


def AsyncAnthropic(*args: Any, **kwargs: Any) -> Any:
    """`anthropic.AsyncAnthropic(...)`, pre-wrapped for tracing."""
    return wrap_anthropic(_anthropic.AsyncAnthropic(*args, **kwargs))


class _AnthropicProxy:
    """Mirrors the `anthropic` module but hands back traced clients."""

    Anthropic = staticmethod(Anthropic)
    AsyncAnthropic = staticmethod(AsyncAnthropic)

    def __getattr__(self, name: str) -> Any:
        return getattr(_anthropic, name)


anthropic = _AnthropicProxy()
