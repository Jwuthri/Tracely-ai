"""Trace + span response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SpanOut(BaseModel):
    span_id: str
    parent_span_id: str
    name: str
    type: str
    level: str
    status_message: str
    start_time: datetime
    end_time: datetime | None = None
    latency_ms: float | None = None
    # Time-to-first-CONTENT-token on a streamed call (the SDK stamps it). Splits the span into
    # thinking-vs-answering for a reasoning model; None whenever the call was not streamed.
    ttft_ms: float | None = None
    agent_id: str = ""
    agent_run_id: str = ""
    turn_id: str = ""
    step_name: str = ""
    model_id: str = ""
    tokens: int = 0
    cost: float = 0.0
    metadata: dict[str, str] = {}
    input: str | None = None
    output: str | None = None


class TraceDetail(BaseModel):
    trace_id: str
    # The conversation thread this trace belongs to (== trace_id for a 1-turn thread). The UI
    # uses it to scope conversation-level metric columns + thread-wide eval runs.
    thread_id: str | None = None
    spans: list[SpanOut]
    scores: list[dict] = []
    eval_verdict: str | None = None
