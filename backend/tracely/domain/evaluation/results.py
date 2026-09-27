"""Shape of an evaluator's return value + the trace context it's given.

`EvalResult` is the raw output of a single check — many checks emit one result, but
`ToolSuccessEvaluator` emits one per TOOL span and SPAN-level judges emit one per step.

`chain_payload` is the one rendering of a result as sequential-mode context — shared by the
judge (chaining steps within a message) and the service (seeding the next turn), so both sides
of a chain show the model the same shape.

`RunContext` is the bundle the runner hands every evaluator: trace identifiers, all spans,
and the root span pre-computed (so each evaluator doesn't re-derive it). For CONVERSATION-level
evaluation `spans` holds EVERY span across the thread (each row carries its `trace_id`) and
`thread_id` is set; trace-scoped fields are left blank.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalResult:
    name: str
    level: str
    verdict: str  # PASS | FAIL | "" (neutral — informational scores carry no verdict)
    data_type: str = "BOOLEAN"
    value: float | None = None
    string_value: str = ""  # CATEGORICAL / TEXT / JSON payloads
    target_span_id: str = ""
    comment: str = ""
    # LLM-judge token usage for THIS grade ({input_tokens, output_tokens, total_tokens, model}),
    # so eval spend is attributable per evaluator. None for structural checks (no LLM call).
    usage: dict | None = None


def chain_payload(
    *, value: float | None, verdict: str, comment: str, string_value: str
) -> dict:
    """A result as the compact object that chains into the NEXT item's context in sequential
    mode — the ONE rendering used both within a turn (the judge chaining its own steps) and
    across turns (the service seeding `CFG_PREVIOUS` from the persisted score).

    A `json` column's result keeps its schema shape (the user's own fields, with the
    score/verdict/reason envelope re-attached where absent); every other output type collapses
    to `{label, value, verdict, reason}` minus empty fields."""
    label = None
    if string_value:
        try:
            parsed = json.loads(string_value)
        except ValueError:
            parsed = None
            label = string_value  # not JSON at all → a label (see below)
        if isinstance(parsed, dict):
            out = dict(parsed)
            if value is not None:
                out.setdefault("score", value)
            if verdict:
                out.setdefault("verdict", verdict)
            if comment:
                out.setdefault("reason", comment)
            return out
    # A plain (non-JSON) string_value is a decision column's label — `order_status`, or a
    # multi-label column's `refund, complaint`. It is the whole answer of that column, so it rides
    # as its own field: a dependent's prompt reads it and a `run_if` label condition gates on it
    # (`domain/evaluation/conditions.py`). Left in the comment only, it was prose to be parsed.
    payload = {
        "label": label,
        "value": value,
        "verdict": verdict or None,
        "reason": comment or None,
    }
    return {k: v for k, v in payload.items() if v is not None}


@dataclass
class RunContext:
    project_id: str
    trace_id: str
    agent_run_id: str
    spans: list[dict[str, Any]]
    root: dict[str, Any] = field(default_factory=dict)
    # Set for CONVERSATION-level evaluation: the thread being graded (spans then covers the
    # whole thread, ordered by start_time, each span dict carrying its own trace_id). The
    # service also sets it on trace/step runs so an advanced judge can scope @HISTORY etc.
    thread_id: str = ""
    # All spans across the whole thread, populated by the service ONLY when an advanced
    # non-conversation judge references a conversation-scoped var (@HISTORY/@MESSAGES/@PREVIOUS_*/
    # @GOAL/@LIST_AGENT). None ⇒ not fetched; the context builder falls back to `spans`.
    thread_spans: list[dict[str, Any]] | None = None
