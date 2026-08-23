"""Shared machinery for the non-patching drop-ins (`tracely_sdk.openai`, `tracely_sdk.anthropic`,
`tracely_sdk.google`, `tracely_sdk.mistral`, …).

`wrap_method(resource, name, capture)` replaces `resource.<name>` with a version that opens a
GENERATION span around the call — on the *instance* only, so nothing is patched globally. The
provider-specific `capture(span, response)` records output/usage/tool-calls. Sync + async; idempotent
(re-wrapping is a no-op).

`input_extractor` lets providers whose request shape differs from OpenAI/Anthropic (e.g. Google's
`contents=` instead of `messages=`) declare how to pull the input off `kwargs`.

Streaming calls keep the span open until the stream is consumed, and stamp
`tracely.completion_start_time` at the first CONTENT delta. That one mark does two jobs: it is the
classic time-to-first-token, and — for a reasoning model, whose thinking arrives as its own delta
kind before any content — it is the boundary between thinking and answering. Without it the two are
one indivisible span: `tracely.thinking()` wrapped around a call produces a THINKING and a
GENERATION span with the same start and the same duration, and nothing on the timeline can say
which of them actually spent the time.
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable

from . import llm, set_io


def _default_input_extractor(kwargs: dict) -> Any:
    return kwargs.get("messages")


def openai_delta(chunk: Any) -> tuple[str, str]:
    """`(kind, text)` off one streamed chunk, OpenAI's shape — also OpenRouter, xAI and Mistral.

    `kind` is "content", "reasoning", or "" for neither (a role-only opening chunk, a tool-call
    fragment, the usage-only final chunk). Reasoning has no single field name in the wild:
    OpenRouter sends `reasoning`, DeepSeek-style APIs `reasoning_content`, so both are read.
    """
    try:
        delta = chunk.choices[0].delta
    except (AttributeError, IndexError, TypeError):
        return "", ""
    text = getattr(delta, "content", None)
    if text:
        return "content", str(text)
    for field in ("reasoning_content", "reasoning"):
        thought = getattr(delta, field, None)
        if thought:
            return "reasoning", str(thought)
    return "", ""


def anthropic_delta(chunk: Any) -> tuple[str, str]:
    """Anthropic's event stream: `content_block_delta` events carrying `text_delta` (the answer)
    or `thinking_delta` (extended thinking)."""
    try:
        if getattr(chunk, "type", "") != "content_block_delta":
            return "", ""
        delta = chunk.delta
    except AttributeError:
        return "", ""
    dtype = getattr(delta, "type", "")
    if dtype == "text_delta":
        return "content", str(getattr(delta, "text", "") or "")
    if dtype == "thinking_delta":
        return "reasoning", str(getattr(delta, "thinking", "") or "")
    return "", ""


class _StreamRecorder:
    """Accumulates a stream onto its span and closes the span exactly once.

    The span cannot end when `create()` returns: for a stream that moment is *before a single token
    exists*, which is why streaming calls used to record the request and nothing else.
    """

    def __init__(self, span: Any, cm: Any, delta: Callable[[Any], tuple[str, str]]) -> None:
        self._span = span
        self._cm = cm
        self._delta = delta
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._marked = False
        self._done = False

    def feed(self, chunk: Any) -> None:
        try:
            kind, text = self._delta(chunk)
            if kind == "content":
                if not self._marked:
                    # Epoch nanoseconds — what the backend's `_completion_start` parses.
                    self._span.set_attribute("tracely.completion_start_time", time.time_ns())
                    self._marked = True
                self._content.append(text)
            elif kind == "reasoning":
                self._reasoning.append(text)
        except Exception:  # never let trace capture break the caller's stream
            pass

    def finish(self, exc: BaseException | None = None) -> None:
        if self._done:
            return
        self._done = True
        try:
            out: dict[str, Any] = {"role": "assistant", "content": "".join(self._content)}
            if self._reasoning:
                out["reasoning"] = "".join(self._reasoning)
            set_io(self._span, output=out)
        except Exception:
            pass
        if exc is not None:
            self._cm.__exit__(type(exc), exc, exc.__traceback__)
        else:
            self._cm.__exit__(None, None, None)


class _TracedStream:
    """Passes the provider's stream through untouched while recording it. Iteration, the context
    manager protocol, `close()` and every other attribute forward to the real object, so callers
    cannot tell the difference."""

    def __init__(self, inner: Any, rec: _StreamRecorder) -> None:
        self._inner = inner
        self._rec = rec

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._inner)
        except StopIteration:
            self._rec.finish()
            raise
        except BaseException as e:
            self._rec.finish(e)
            raise
        self._rec.feed(chunk)
        return chunk

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        try:
            chunk = await self._inner.__anext__()
        except StopAsyncIteration:
            self._rec.finish()
            raise
        except BaseException as e:
            self._rec.finish(e)
            raise
        self._rec.feed(chunk)
        return chunk

    def __enter__(self) -> _TracedStream:
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        self._rec.finish()
        if hasattr(self._inner, "__exit__"):
            return self._inner.__exit__(*exc)
        return None

    async def __aenter__(self) -> _TracedStream:
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        self._rec.finish()
        if hasattr(self._inner, "__aexit__"):
            return await self._inner.__aexit__(*exc)
        return None

    def close(self) -> Any:
        self._rec.finish()
        closer = getattr(self._inner, "close", None)
        return closer() if closer else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __del__(self) -> None:
        # Backstop for a caller who abandons the stream part-way: an unended span is never
        # exported at all, so the call would vanish from the trace entirely.
        try:
            self._rec.finish()
        except Exception:
            pass


def wrap_method(
    resource: Any,
    name: str,
    capture: Callable[[Any, Any], None],
    *,
    input_extractor: Callable[[dict], Any] = _default_input_extractor,
    model_key: str = "model",
    stream_delta: Callable[[Any], tuple[str, str]] = openai_delta,
) -> None:
    original = getattr(resource, name)
    # our own sentinel — NOT __wrapped__, which providers' own decorators (e.g. openai's
    # @required_args) already set, which would make us think it's already traced.
    if getattr(original, "_tracely_wrapped", False):
        return

    def _open(kwargs: dict) -> Any:
        cm = llm(kwargs.get(model_key, "") or "")
        span = cm.__enter__()
        inp = input_extractor(kwargs)
        if inp is not None:
            set_io(span, input=inp)
        return cm, span

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def traced(*args: Any, **kwargs: Any) -> Any:
            cm, span = _open(kwargs)
            try:
                resp = await original(*args, **kwargs)
            except BaseException as e:
                cm.__exit__(type(e), e, e.__traceback__)
                raise
            if kwargs.get("stream"):
                return _TracedStream(resp, _StreamRecorder(span, cm, stream_delta))
            capture(span, resp)
            cm.__exit__(None, None, None)
            return resp
    else:

        @functools.wraps(original)
        def traced(*args: Any, **kwargs: Any) -> Any:
            cm, span = _open(kwargs)
            try:
                resp = original(*args, **kwargs)
            except BaseException as e:
                cm.__exit__(type(e), e, e.__traceback__)
                raise
            if kwargs.get("stream"):
                return _TracedStream(resp, _StreamRecorder(span, cm, stream_delta))
            capture(span, resp)
            cm.__exit__(None, None, None)
            return resp

    traced._tracely_wrapped = True  # type: ignore[attr-defined]
    setattr(resource, name, traced)
