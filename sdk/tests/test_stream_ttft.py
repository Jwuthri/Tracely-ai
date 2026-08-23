"""Streaming calls must record a span, and mark where thinking ends and answering begins.

Before this, a streamed call closed its span the moment `create()` returned — before a single token
existed — so it captured the request and nothing else, and `tracely.completion_start_time` (the
column, the mapper and the ops-metric all already exist) was never written by anything.

The mark is the answer to "thinking and generation show the same 3.4s": for a reasoning model the
first CONTENT delta is exactly the boundary between the two.
"""

from __future__ import annotations

import types

from tracely_sdk._wrap import anthropic_delta, openai_delta, wrap_method


def _chunk(content=None, reasoning=None, reasoning_content=None):
    delta = types.SimpleNamespace(content=content)
    if reasoning is not None:
        delta.reasoning = reasoning
    if reasoning_content is not None:
        delta.reasoning_content = reasoning_content
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


class FakeSpan:
    def __init__(self):
        self.attrs: dict = {}
        self.ended = False

    def set_attribute(self, k, v):
        self.attrs[k] = v

    def end(self):
        self.ended = True


class Resource:
    """A stand-in provider client whose `create` returns a chunk iterator when stream=True."""

    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **kwargs):
        return iter(self._chunks)


def _wrapped(chunks, monkeypatch, **kw):
    """Wrap `Resource.create`, with the SDK's `llm()` span swapped for a fake we can inspect."""
    import contextlib

    from tracely_sdk import _wrap

    span = FakeSpan()

    @contextlib.contextmanager
    def fake_llm(_model):
        yield span

    monkeypatch.setattr(_wrap, "llm", fake_llm)
    monkeypatch.setattr(_wrap, "set_io", lambda s, **kwargs: s.attrs.update(kwargs))
    r = Resource(chunks)
    wrap_method(r, "create", lambda s, resp: None, **kw)
    return r, span


# ── delta shapes ─────────────────────────────────────────────────────────────

def test_openai_delta_distinguishes_content_from_reasoning():
    assert openai_delta(_chunk(content="hi")) == ("content", "hi")
    assert openai_delta(_chunk(reasoning="hmm")) == ("reasoning", "hmm")
    assert openai_delta(_chunk(reasoning_content="hmm")) == ("reasoning", "hmm")
    assert openai_delta(_chunk()) == ("", "")
    assert openai_delta(types.SimpleNamespace(choices=[])) == ("", "")  # usage-only final chunk


def test_anthropic_delta_reads_thinking_and_text():
    def ev(dtype, **kw):
        return types.SimpleNamespace(
            type="content_block_delta", delta=types.SimpleNamespace(type=dtype, **kw)
        )

    assert anthropic_delta(ev("text_delta", text="hi")) == ("content", "hi")
    assert anthropic_delta(ev("thinking_delta", thinking="hmm")) == ("reasoning", "hmm")
    assert anthropic_delta(types.SimpleNamespace(type="message_start")) == ("", "")


# ── the span ─────────────────────────────────────────────────────────────────

def test_ttft_is_stamped_at_the_first_content_delta(monkeypatch):
    """Reasoning first, then the answer — the mark lands on the ANSWER, not on the thinking."""
    chunks = [_chunk(reasoning="think"), _chunk(reasoning="ing"), _chunk(content="Hel"), _chunk(content="lo")]
    r, span = _wrapped(chunks, monkeypatch)

    stream = r.create(model="m", stream=True)
    assert "tracely.completion_start_time" not in span.attrs  # nothing consumed yet
    out = list(stream)

    assert len(out) == 4  # every chunk still reaches the caller
    mark = span.attrs["tracely.completion_start_time"]
    assert isinstance(mark, int) and mark > 1_000_000_000_000_000_000  # epoch NANOseconds


def test_stream_records_content_and_reasoning_separately(monkeypatch):
    chunks = [_chunk(reasoning="because "), _chunk(reasoning="reasons"), _chunk(content="42")]
    r, span = _wrapped(chunks, monkeypatch)
    list(r.create(model="m", stream=True))
    assert span.attrs["output"] == {"role": "assistant", "content": "42", "reasoning": "because reasons"}


def test_span_stays_open_until_the_stream_is_consumed(monkeypatch):
    r, span = _wrapped([_chunk(content="a")], monkeypatch)
    stream = r.create(model="m", stream=True)
    assert "output" not in span.attrs  # would have been the old behaviour: closed, empty
    list(stream)
    assert span.attrs["output"]["content"] == "a"


def test_a_stream_with_no_content_still_closes(monkeypatch):
    """A call that only ever reasons (or is cut off) must not leave the span open — an unended
    span is never exported, so the call would vanish from the trace."""
    r, span = _wrapped([_chunk(reasoning="...")], monkeypatch)
    list(r.create(model="m", stream=True))
    assert "tracely.completion_start_time" not in span.attrs
    assert span.attrs["output"] == {"role": "assistant", "content": "", "reasoning": "..."}


def test_abandoned_stream_is_closed_by_close(monkeypatch):
    r, span = _wrapped([_chunk(content="a"), _chunk(content="b")], monkeypatch)
    stream = r.create(model="m", stream=True)
    next(iter(stream))
    stream.close()
    assert span.attrs["output"]["content"] == "a"


def test_non_streaming_calls_are_unchanged(monkeypatch):
    captured = {}
    import contextlib

    from tracely_sdk import _wrap

    span = FakeSpan()

    @contextlib.contextmanager
    def fake_llm(_m):
        yield span

    monkeypatch.setattr(_wrap, "llm", fake_llm)
    monkeypatch.setattr(_wrap, "set_io", lambda s, **kw: None)

    class R:
        def create(self, **kwargs):
            return {"ok": True}

    r = R()
    wrap_method(r, "create", lambda s, resp: captured.update(resp))
    assert r.create(model="m") == {"ok": True}
    assert captured == {"ok": True}  # capture() still runs for non-streaming
