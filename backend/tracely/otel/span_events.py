"""Span *events* — the half of OTLP the attribute mappers can't see.

Three things ride on events rather than attributes, and all were being dropped:

1. **Exceptions.** `span.record_exception()` writes an `exception` event with the type, message
   and stacktrace. Plenty of instrumentors record one WITHOUT also setting the span status to
   ERROR. `level = 'ERROR'` is the only failure signal Tracely has — clustering, failure
   detection and the gate all key off it — so a swallowed exception meant a green trace for a
   run that threw.

2. **Messages, in the OTel GenAI *event* convention.** `opentelemetry-instrumentation-openai-v2`
   (which the Tracely SDK itself activates as a fallback) emits prompts and completions as
   `gen_ai.user.message` / `gen_ai.choice` events instead of attributes, so those spans arrived
   with a model and a token count but no conversation at all.

3. **Time to first token.** The OpenInference instrumentors — the default `init(instrument=[...])`
   path — mark the first streamed chunk with a `First Token Stream Event` span event rather than an
   attribute, so `completion_start_time` (attribute-only until now) was always NULL on that path and
   the ops TTFT metric had nothing to read. See `first_token_time`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from tracely.otel.attributes import _attrs, _ns_to_dt

# Event name → the role the message carries. `gen_ai.choice` is the model's reply.
_ROLE_EVENTS = {
    "gen_ai.system.message": "system",
    "gen_ai.user.message": "user",
    "gen_ai.assistant.message": "assistant",
    "gen_ai.tool.message": "tool",
}
_CHOICE_EVENT = "gen_ai.choice"

# OpenInference's first-streamed-chunk marker. Named, not versioned — same string across their
# openai / mistralai / llama-index instrumentors.
_FIRST_TOKEN_EVENT = "First Token Stream Event"

# Where an event puts its payload, in order. Emitters disagree: the stable semconv uses the
# `content`/`role` attribute pair, the 0.4x-era one packed a JSON body under `gen_ai.event.content`.
_BODY_KEYS = ("content", "gen_ai.event.content", "body", "message")


def _event_attrs(event: Any) -> dict[str, Any]:
    try:
        return _attrs(list(event.attributes))
    except (AttributeError, TypeError):
        return {}


def exception_text(span: Any) -> str:
    """`"TypeError: str is not callable"` from the first `exception` event, or "" if there is none."""
    for event in getattr(span, "events", None) or ():
        if getattr(event, "name", "") != "exception":
            continue
        a = _event_attrs(event)
        etype = str(a.get("exception.type") or "").strip()
        emsg = str(a.get("exception.message") or "").strip()
        if etype and emsg:
            return f"{etype}: {emsg}"
        if etype or emsg:
            return etype or emsg
    return ""


def first_token_time(span: Any) -> datetime | None:
    """When the first streamed chunk arrived, from the instrumentors' own first-token span event.

    OpenInference's stream proxies (`openai`, `mistralai`, `llama_index`) call
    `span.add_event("First Token Stream Event")` on the first chunk — this maps that existing signal
    instead of instrumenting the default path ourselves. Note it fires on the first chunk of ANY
    kind, so unlike the drop-in wrappers' `tracely.completion_start_time` (first CONTENT delta, which
    doubles as the thinking→answering boundary) it is plain TTFT: for a reasoning model it lands at
    the start of the thinking, not after it.

    The other instrumentors (anthropic, langchain, google-genai, groq, bedrock) emit no such event,
    so their streamed spans still have no TTFT — they do, however, already hold the span open until
    the stream is exhausted, so nothing is lost beyond this one mark.
    """
    for event in getattr(span, "events", None) or ():
        if getattr(event, "name", "") == _FIRST_TOKEN_EVENT:
            return _ns_to_dt(getattr(event, "time_unix_nano", 0) or 0)
    return None


def _payload(a: dict[str, Any]) -> Any:
    """The message body out of an event's attributes — parsed when it is a JSON string."""
    for key in _BODY_KEYS:
        if key in a and a[key] not in (None, ""):
            raw = a[key]
            if isinstance(raw, str):
                t = raw.strip()
                if t.startswith(("{", "[")):
                    try:
                        return json.loads(t)
                    except (ValueError, json.JSONDecodeError):
                        return raw
            return raw
    return None


def events_io(span: Any) -> tuple[list | None, list | None]:
    """`(input_messages, output_messages)` reassembled from GenAI events, or `(None, None)`.

    Returned in the same `[{role, content}, ...]` shape every other convention normalizes to, so
    the caller can hand it to the ordinary message pipeline.
    """
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for event in getattr(span, "events", None) or ():
        name = getattr(event, "name", "")
        if name not in _ROLE_EVENTS and name != _CHOICE_EVENT:
            continue
        a = _event_attrs(event)
        body = _payload(a)
        if name == _CHOICE_EVENT:
            # The choice body is usually the assistant message itself, sometimes wrapped.
            msg = body.get("message") if isinstance(body, dict) and "message" in body else body
            if isinstance(msg, dict):
                outputs.append({"role": msg.get("role") or "assistant", **msg})
            elif msg not in (None, ""):
                outputs.append({"role": "assistant", "content": msg})
            continue
        role = str(a.get("role") or _ROLE_EVENTS[name])
        if isinstance(body, dict):
            inputs.append({"role": body.get("role") or role, **body})
        elif body not in (None, ""):
            inputs.append({"role": role, "content": body})
    return (inputs or None), (outputs or None)
