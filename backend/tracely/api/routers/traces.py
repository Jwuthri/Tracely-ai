"""Trace endpoints: list traces, get trace detail (spans + scores + verdict).

Pure HTTP shaping — all ClickHouse access lives in `infrastructure.clickhouse.async_reader`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from tracely.api.advisory import advisory_score_names
from tracely.api.auth import get_project_id
from tracely.api.dto.traces import SpanOut, TraceDetail
from tracely.domain.evaluation.verdict import rollup_verdict
from tracely.infrastructure.clickhouse import async_reader

router = APIRouter(prefix="/api")


@router.get("/traces")
async def list_traces(limit: int = 20, project_id: str = Depends(get_project_id)) -> list[dict]:
    advisory = await advisory_score_names(project_id)
    return await async_reader.traces_overview(project_id, limit, advisory)


def _span_out(d: dict) -> SpanOut:
    """One reader row → the wire shape. Shared by the trace detail and the internal-run readers,
    so a recording's spans carry the same derived fields (latency above all) as any other span —
    the alternative was a second, quietly different shape behind the same TypeScript type."""
    latency = None
    if d.get("end_time") and d.get("start_time"):
        latency = (d["end_time"] - d["start_time"]).total_seconds() * 1000.0
    ttft = None
    if d.get("completion_start_time") and d.get("start_time"):
        ttft = (d["completion_start_time"] - d["start_time"]).total_seconds() * 1000.0
        # A mark outside the span's own window is a clock artefact, not a measurement.
        if ttft < 0 or (latency is not None and ttft > latency):
            ttft = None
    return SpanOut(
        span_id=d["span_id"],
        parent_span_id=d["parent_span_id"],
        name=d["name"],
        type=d["type"],
        level=d["level"],
        status_message=d["status_message"],
        start_time=d["start_time"],
        end_time=d["end_time"],
        latency_ms=latency,
        ttft_ms=ttft,
        agent_id=d["agent_id"],
        agent_run_id=d["agent_run_id"],
        turn_id=d["turn_id"],
        step_name=d["step_name"],
        model_id=d["model_id"],
        tokens=int(d.get("tokens") or 0),
        cost=float(d.get("cost") or 0.0),
        metadata={str(k): str(v) for k, v in (d.get("metadata") or {}).items()},
        input=d["input"],
        output=d["output"],
    )


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str, project_id: str = Depends(get_project_id)
) -> TraceDetail:
    raw_spans = await async_reader.trace_spans(project_id, trace_id)
    thread_id = trace_id  # a trace with no conversation is its own 1-turn thread
    spans: list[SpanOut] = []
    for d in raw_spans:
        if d.get("conversation_id"):
            thread_id = d["conversation_id"]
        spans.append(_span_out(d))
    scores = await async_reader.trace_scores(project_id, trace_id, thread_id)
    eval_verdict = rollup_verdict(scores, await advisory_score_names(project_id))
    return TraceDetail(
        trace_id=trace_id, thread_id=thread_id, spans=spans, scores=scores, eval_verdict=eval_verdict
    )
