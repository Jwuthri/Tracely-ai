"""SDK redaction (init(redact=...)): scrub PII/sensitive content from span attributes on export.

Redaction runs in `_RedactingSpanExporter`, which wraps the OTLP exporter — so it covers BOTH
manual `set_io`/metadata and zero-touch auto-instrumentor attributes (everything funnels through the
exporter). These tests exercise the resolver and the in-place attribute scrub on a real span."""

from __future__ import annotations

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracely_sdk as tracely


def test_build_redactor_off() -> None:
    assert tracely._build_redactor(None) is None
    assert tracely._build_redactor(False) is None
    assert tracely._build_redactor([]) is None


def test_build_redactor_default_pii() -> None:
    r = tracely._build_redactor(True)
    assert r is not None
    assert r("tracely.input", "email me at jane@example.com please") == (
        "email me at [REDACTED] please"
    )
    assert r("tracely.output", "ssn 123-45-6789") == "ssn [REDACTED]"
    assert "[REDACTED]" in r("k", "card 4111 1111 1111 1111")
    assert r("k", "nothing sensitive here") == "nothing sensitive here"


def test_build_redactor_custom_patterns() -> None:
    r = tracely._build_redactor([r"secret-\d+"])
    assert r is not None
    assert r("k", "token secret-123 end") == "token [REDACTED] end"
    # custom patterns REPLACE the defaults — an email is left untouched
    assert r("k", "a@b.com") == "a@b.com"


def test_build_redactor_callable_passthrough() -> None:
    def only_input(key: str, value: str) -> str:
        return "***" if key == "tracely.input" else value

    r = tracely._build_redactor(only_input)
    assert r is only_input
    assert r("tracely.input", "secret") == "***"
    assert r("tracely.output", "secret") == "secret"


def test_redacting_exporter_scrubs_span_attributes() -> None:
    """End-to-end on a real span: PII set on attributes is gone by the time the inner exporter
    receives it. Also proves the in-place mutation works on the installed OTel version."""
    tracely.init(env="prod", instrument=False)  # global provider (idempotent)
    capture = InMemorySpanExporter()
    tracely._provider.add_span_processor(SimpleSpanProcessor(capture))

    with tracely._t().start_as_current_span("redact-e2e") as span:
        tracely.set_io(span, input="reach me at jane@example.com", output="ok")
        span.set_attribute("llm.prompt", "card 4111 1111 1111 1111")
        span.set_attribute("gen_ai.usage.input_tokens", 42)  # non-string untouched

    readable = next(s for s in capture.get_finished_spans() if s.name == "redact-e2e")
    inner = InMemorySpanExporter()
    wrap = tracely._RedactingSpanExporter(inner, tracely._build_redactor(True))
    wrap.export([readable])

    out = dict(inner.get_finished_spans()[0].attributes)
    assert "jane@example.com" not in out["tracely.input"]
    assert "[REDACTED]" in out["tracely.input"]
    assert "[REDACTED]" in out["llm.prompt"]
    assert out["tracely.output"] == "ok"  # no PII → unchanged
    assert out["gen_ai.usage.input_tokens"] == 42  # ints pass through


def test_redaction_survives_immutable_attribute_maps() -> None:
    """Regression: a finished span carries `BoundedAttributes`, whose `__setitem__` raises. Scrubbing
    in place therefore silently did nothing and PII reached the exporter. Redaction must replace the
    mapping, so it holds on every OTel version — not only the ones where the map happens to be
    writable."""
    from opentelemetry.attributes import BoundedAttributes

    tracely.init(env="prod", instrument=False)
    capture = InMemorySpanExporter()
    tracely._provider.add_span_processor(SimpleSpanProcessor(capture))

    with tracely._t().start_as_current_span("redact-immutable") as span:
        tracely.set_io(span, input="reach me at jane@example.com", output="ok")
        span.add_event("tool.call", {"arg": "jane@example.com"})

    readable = next(s for s in capture.get_finished_spans() if s.name == "redact-immutable")
    # Guard the premise: if OTel ever ships a writable map, this test still passes but stops
    # proving anything — assert we are exercising the immutable path.
    assert isinstance(readable._attributes, BoundedAttributes)

    inner = InMemorySpanExporter()
    tracely._RedactingSpanExporter(inner, tracely._build_redactor(True)).export([readable])
    exported = inner.get_finished_spans()[0]

    assert "jane@example.com" not in exported.attributes["tracely.input"]
    assert "jane@example.com" not in str(dict(exported.events[0].attributes))
