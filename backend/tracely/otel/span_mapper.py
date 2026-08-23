"""The central OTLP span → Tracely event row mapper.

Pulls in every other module in this package: attribute primitives, type classification, message
normalization, IO field resolution, usage extraction, convention detection, tool-call name
extraction. Keep this file small — it's a serialization rule, not business logic.
"""

from __future__ import annotations

from typing import Any

from tracely.otel.attributes import _attrs, _first, _ns_to_dt, _to_str, _truthy
from tracely.otel.convention import _convention
from tracely.otel.io_field import (
    _io_field,
    _is_msg_attr,
    _is_strippable_usage_for_non_llm,
)
from tracely.otel.attributes import _to_str as _stringify
from tracely.otel.messages import _as_obj, _normalize_parsed
from tracely.otel.span_events import events_io, exception_text
from tracely.otel.tool_enrichment import _tool_call_names
from tracely.otel.types import EMBEDDING, GENERATION, TOOL, map_observation_type
from tracely.otel.usage import (
    _completion_start,
    _invocation_model,
    _model_parameters,
    _usage,
)


def _int_or(value: Any, default: int) -> int:
    """A bad `turn.index` must not take the batch down with it: the mapper runs inside the ingest
    task, which retries six times and then drops the WHOLE OTLP payload — so one span with a
    non-numeric index used to cost every other span in the request."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _level_and_message(span: Any, a: dict[str, Any]) -> tuple[str, str]:
    """A span's failure state. `level = 'ERROR'` is the ONLY failure signal Tracely has — every
    detected failure, cluster and gate verdict keys off it — so it reads three sources, not one:

    1. an explicit ERROR status,
    2. a recorded `exception` event (what `record_exception()` writes; plenty of instrumentors
       record one without also setting the status, which used to render as a green trace),
    3. the semconv `error.type` attribute.

    An explicit OK status wins over 2 and 3: a framework that caught and handled the exception
    said so deliberately, and second-guessing it would flag every retry-then-succeed as a failure.
    """
    if span.status.code == 2:  # ERROR
        return "ERROR", (span.status.message or exception_text(span) or "")
    if span.status.code == 1:  # explicitly OK — handled
        return "DEFAULT", ""
    exc = exception_text(span)
    if exc:
        return "ERROR", exc
    err_type = _first(a, ["error.type"])
    if err_type:
        return "ERROR", str(err_type)
    return "DEFAULT", ""


def _map_span(
    resource_attrs: dict[str, Any],
    scope_name: str,
    scope_version: str,
    span: Any,
    project_id: str,
    schema_url: str = "",
) -> dict[str, Any]:
    a = {**resource_attrs, **_attrs(list(span.attributes))}
    trace_id = span.trace_id.hex()
    span_id = span.span_id.hex()
    parent_span_id = span.parent_span_id.hex() if span.parent_span_id else ""

    level, status_message = _level_and_message(span, a)

    otype = map_observation_type(a)
    agent_run_id = str(_first(a, ["tracely.agent.run_id", "tracely.run.id"]) or trace_id)
    # OpenInference/LangChain packs node info into a `metadata` JSON attr; promote LangGraph's
    # node name + step number to first-class step columns (R11; §13 LangGraph shape).
    lc_meta = _as_obj(a.get("metadata")) if "metadata" in a else None
    lc_meta = lc_meta if isinstance(lc_meta, dict) else {}

    # Attributes first; GenAI *events* only fill what they left empty (see otel/span_events.py).
    span_input, span_output = _io_field(a, "input"), _io_field(a, "output")
    if span_input is None or span_output is None:
        ev_in, ev_out = events_io(span)
        if span_input is None and ev_in:
            span_input = _stringify(_normalize_parsed(ev_in))
        if span_output is None and ev_out:
            span_output = _stringify(_normalize_parsed(ev_out))

    return {
        "project_id": project_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "start_time": _ns_to_dt(span.start_time_unix_nano),
        "end_time": _ns_to_dt(span.end_time_unix_nano),
        "completion_start_time": _completion_start(a),
        # For TOOL spans, prefer the actual tool name from attrs over the framework's generic
        # span name (LlamaIndex uses `FunctionTool.acall`, etc.) — so the UI shows the tool
        # name and the tool_consistency eval can match "requested" against "executed".
        "name": (
            str(_first(a, ["tool.name", "gen_ai.tool.name", "ai.toolCall.name"]) or span.name or "")
            if otype == TOOL
            else (span.name or "")
        ),
        "type": otype,
        "environment": str(
            _first(a, ["deployment.environment", "langfuse.environment"]) or "default"
        ),
        "env": str(_first(a, ["tracely.env"]) or "prod"),
        "version": str(_first(a, ["tracely.version", "service.version"]) or ""),
        "level": level,
        "status_message": status_message,
        "is_app_root": parent_span_id == ""
        or _truthy(_first(a, ["tracely.is_app_root", "langfuse.internal.as_root"]) or ""),
        "trace_name": str(_first(a, ["tracely.trace.name"]) or ""),
        "user_id": str(_first(a, ["tracely.user.id", "user.id", "langfuse.user.id"]) or ""),
        "session_id": str(
            _first(a, ["session.id", "tracely.session.id", "langfuse.session.id"]) or ""
        ),
        # The agent is DECLARED, never inferred. Only what the user set — SDK `agent=` /
        # `init(agent=)` / the raw attribute — names an agent; a trace that declares nothing gets
        # the single `default` agent (ingestion_service._attribute_default_agent). Framework keys
        # like `gen_ai.agent.name` are deliberately NOT read here: a harness stamps one on every
        # sub-agent it spins up, which registered dozens of agents nobody chose and shredded the
        # dimension the product groups, gates and clusters on. They stay visible in metadata.
        "agent_slug": str(_first(a, ["tracely.agent.id", "langfuse.agent.id"]) or ""),
        # The conversation's tenant — which customer / workspace / bot this run belongs to when one
        # codebase serves many. When set it IS the trace's registry agent (every span, see
        # ingestion_service._attribute_default_agent), so gates, clusters, cases and scenarios are
        # per-tenant while `agent_slug` above stays a per-span label. Helper key, never a column.
        "tenant_slug": str(_first(a, ["tracely.tenant.id"]) or ""),
        "agent_version_ref": str(
            _first(a, ["tracely.agent.version", "tracely.agent.version_id"]) or ""
        ),
        "agent_run_id": agent_run_id,
        # `gen_ai.conversation.id` is the semconv key; LangGraph's `thread_id` rides in the same
        # `metadata` blob the step columns come from. Both are how a run becomes a *conversation*
        # rather than an orphan trace, which is the unit the whole product reads.
        "conversation_id": str(
            _first(a, ["tracely.conversation.id", "gen_ai.conversation.id", "session.id"])
            or lc_meta.get("thread_id")
            or ""
        ),
        "turn_id": str(_first(a, ["tracely.turn.id"]) or ""),
        "turn_index": _int_or(_first(a, ["tracely.turn.index"]), 0),
        "step_id": str(
            _first(a, ["tracely.step.id", "langgraph_step"]) or lc_meta.get("langgraph_step") or ""
        ),
        "step_name": str(
            _first(a, ["tracely.step.name", "langgraph_node"])
            or lc_meta.get("langgraph_node")
            or ""
        ),
        "tool_call_id": str(_first(a, ["tracely.tool_call.id", "gen_ai.tool.call.id"]) or ""),
        "caller_agent_id": str(_first(a, ["tracely.handoff.caller_agent_id"]) or ""),
        "callee_agent_id": str(_first(a, ["tracely.handoff.callee_agent_id"]) or ""),
        "edge_type": str(_first(a, ["tracely.edge.type"]) or ""),
        # Tracely's own work recorded as a trace (see domain/introspection.py). Non-empty here is
        # what keeps the span out of every list AND out of the evaluator's reach — grading an eval
        # run would record another eval run, forever.
        "internal_kind": str(_first(a, ["tracely.internal.kind"]) or ""),
        "subject_id": str(_first(a, ["tracely.internal.subject_id"]) or ""),
        # model / usage — only meaningful for spans that *are* an LLM call. Non-LLM spans
        # (AGENT, CHAIN, TOOL, ...) get a polluted `model_id` when a framework callback (LiteLLM,
        # etc.) accidentally stamps `llm.openai.model` on the enclosing span; strip it.
        "model_id": (
            str(
                _first(a, [
                    "gen_ai.response.model",
                    "gen_ai.request.model",
                    "llm.model_name",
                    "llm.openai.model",
                    "ai.model.id",  # Vercel AI SDK
                    "embedding.model_name",  # OpenInference EMBEDDING spans
                    "tracely.model",
                ])
                # Last resort: OpenInference sometimes carries the model ONLY inside its
                # invocation-parameters blob.
                or _invocation_model(a)
                or ""
            )
            if otype in (GENERATION, EMBEDDING)
            else ""
        ),
        "model_parameters": _model_parameters(a),
        "usage_details": _usage(a),
        "tool_call_names": _tool_call_names(a, otype, span_output),
        "input": span_input,
        "output": span_output,
        "metadata": {
            **{
                k: _to_str(v) or ""
                for k, v in a.items()
                if not _is_msg_attr(k) and not _is_strippable_usage_for_non_llm(k, otype)
            },
            "tracely.otel.gen_ai_convention": _convention(a),
            **({"tracely.otel.schema_url": schema_url} if schema_url else {}),
            **({"tracely.otel.scope_version": scope_version} if scope_version else {}),
            **({"tracely.state_source": "output"} if lc_meta.get("langgraph_node") else {}),
        },
        "source": "otel",
        "service_name": str(resource_attrs.get("service.name", "")),
        "scope_name": scope_name,
        "telemetry_sdk_language": str(resource_attrs.get("telemetry.sdk.language", "")),
        "telemetry_sdk_name": str(resource_attrs.get("telemetry.sdk.name", "")),
        "telemetry_sdk_version": str(resource_attrs.get("telemetry.sdk.version", "")),
    }
