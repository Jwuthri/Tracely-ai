"""Unit tests for the OTLP -> events mapping (no infra needed)."""

from __future__ import annotations

import json

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from tracely.infrastructure.clickhouse.events_schema import EVENT_COLUMNS, to_rows
from tracely.otel import events_from_request
from tracely.otel.convention import _convention
from tracely.otel.types import map_observation_type


def _kv(k: str, v) -> KeyValue:
    if isinstance(v, bool):
        return KeyValue(key=k, value=AnyValue(bool_value=v))
    if isinstance(v, int):
        return KeyValue(key=k, value=AnyValue(int_value=v))
    return KeyValue(key=k, value=AnyValue(string_value=str(v)))


def _request() -> ExportTraceServiceRequest:
    span = Span(
        name="gpt-4o call",
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        start_time_unix_nano=1_000,
        end_time_unix_nano=2_000,
    )
    span.attributes.extend(
        [
            _kv("gen_ai.operation.name", "chat"),
            _kv("gen_ai.request.model", "gpt-4o"),
            _kv("gen_ai.usage.input_tokens", 812),
            _kv("gen_ai.usage.output_tokens", 96),
            _kv("tracely.agent.id", "planner"),
            _kv("session.id", "sess-1"),
        ]
    )
    ss = ScopeSpans(scope=InstrumentationScope(name="test"), spans=[span])
    rs = ResourceSpans(resource=Resource(attributes=[_kv("service.name", "svc")]), scope_spans=[ss])
    return ExportTraceServiceRequest(resource_spans=[rs])


def test_generation_mapping() -> None:
    events = events_from_request(_request(), "proj1")
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "GENERATION"
    assert e["model_id"] == "gpt-4o"
    assert e["agent_slug"] == "planner"
    assert e["conversation_id"] == "sess-1"
    assert e["project_id"] == "proj1"
    assert e["is_app_root"] is True  # no parent span
    assert e["usage_details"] == {"input": 812, "output": 96}


def test_type_classification() -> None:
    assert map_observation_type({"gen_ai.operation.name": "execute_tool"}) == "TOOL"
    assert map_observation_type({"openinference.span.kind": "RETRIEVER"}) == "RETRIEVER"
    assert map_observation_type({"tracely.observation.type": "AGENT"}) == "AGENT"
    assert map_observation_type({"foo": "bar"}) == "SPAN"


def test_delegate_and_skill_are_first_class_types() -> None:
    # Both are explicit-only: no gen_ai / OpenInference kind means them, so an unknown type
    # falling back to SPAN (instead of being dropped) is what keeps them safe to emit.
    assert map_observation_type({"tracely.observation.type": "DELEGATE"}) == "DELEGATE"
    assert map_observation_type({"tracely.observation.type": "skill"}) == "SKILL"
    # Harnesses that model agent-to-agent routing call it a handoff; same notion.
    assert map_observation_type({"tracely.observation.type": "HANDOFF"}) == "DELEGATE"
    # A model call inside a skill span is still a GENERATION — the type is per span, not per subtree.
    assert map_observation_type({"gen_ai.request.model": "gpt-4o"}) == "GENERATION"


def test_to_rows_shape() -> None:
    rows = to_rows(events_from_request(_request(), "p"))
    assert len(rows) == 1
    assert len(rows[0]) == len(EVENT_COLUMNS)


# ── PRD 12: ingest real instrumentor output (R5/R15/§8) ─────────────────────


def _event(
    attrs: dict, *, status_code: int = 0, name: str = "span", events: list | None = None
) -> dict:
    """Build one span from a flat {key: value} attr dict and return its mapped event.

    `events` is a list of `(event_name, {attr: value})` — span events, which carry both recorded
    exceptions and the OTel GenAI *event* message convention.
    """
    span = Span(
        name=name,
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        start_time_unix_nano=1_000,
        end_time_unix_nano=2_000,
    )
    span.attributes.extend([_kv(k, v) for k, v in attrs.items()])
    for ev_name, ev_attrs in events or []:
        ev = span.events.add()
        ev.name = ev_name
        ev.time_unix_nano = 1_500
        ev.attributes.extend([_kv(k, v) for k, v in ev_attrs.items()])
    if status_code:
        span.status.code = status_code
    ss = ScopeSpans(scope=InstrumentationScope(name="instr"), spans=[span])
    rs = ResourceSpans(resource=Resource(attributes=[_kv("service.name", "svc")]), scope_spans=[ss])
    return events_from_request(ExportTraceServiceRequest(resource_spans=[rs]), "p")[0]


def test_openinference_flattened_messages_and_usage() -> None:
    """Arize OpenInference emits flattened llm.* — model, usage, params, messages, tool calls."""
    e = _event(
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": "gpt-4o",
            "llm.invocation_parameters": '{"temperature": 0.7, "max_tokens": 256, "model": "gpt-4o"}',
            "llm.token_count.prompt": 812,
            "llm.token_count.completion": 96,
            "llm.token_count.total": 908,
            "llm.input_messages.0.message.role": "user",
            "llm.input_messages.0.message.content": "What is the weather in Paris?",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "",
            "llm.output_messages.0.message.tool_calls.0.tool_call.id": "call_1",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.name": "get_weather",
            "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments": '{"city": "Paris"}',
        }
    )
    assert e["type"] == "GENERATION"
    assert e["model_id"] == "gpt-4o"
    # total dropped because input+output present -> additive sum stays correct (no double count)
    assert e["usage_details"] == {"input": 812, "output": 96}
    params = json.loads(e["model_parameters"])
    assert params["temperature"] == 0.7 and params["max_tokens"] == 256
    msgs_in = json.loads(e["input"])
    assert msgs_in == [{"role": "user", "content": "What is the weather in Paris?"}]
    msgs_out = json.loads(e["output"])
    assert msgs_out[0]["role"] == "assistant"
    assert msgs_out[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert e["tool_call_names"] == ["get_weather"]
    # message attrs must NOT leak into the lossless metadata map
    assert not any(k.startswith("llm.input_messages") for k in e["metadata"])
    assert "gen_ai.input.messages" not in e["metadata"]


def test_genai_structured_messages() -> None:
    """OTel GenAI structured messages arrive as a JSON-string attribute (the common OTLP shape)."""
    e = _event(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "claude-3-5-sonnet",
            "gen_ai.usage.input_tokens": 40,
            "gen_ai.usage.output_tokens": 12,
            "gen_ai.input.messages": json.dumps(
                [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]
            ),
            "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "hello!"}]),
        }
    )
    assert e["type"] == "GENERATION"
    assert e["model_id"] == "claude-3-5-sonnet"
    assert json.loads(e["input"]) == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert json.loads(e["output"]) == [{"role": "assistant", "content": "hello!"}]


def test_openllmetry_legacy_flattened_messages() -> None:
    """OpenLLMetry legacy flattened gen_ai.prompt.<i>.* / gen_ai.completion.<i>.* + tool calls."""
    e = _event(
        {
            "gen_ai.request.model": "gpt-5.4-mini",
            "gen_ai.prompt.0.role": "system",
            "gen_ai.prompt.0.content": "You are helpful.",
            "gen_ai.prompt.1.role": "user",
            "gen_ai.prompt.1.content": "Book a table.",
            "gen_ai.completion.0.role": "assistant",
            "gen_ai.completion.0.tool_calls.0.name": "book_table",
            "gen_ai.completion.0.tool_calls.0.arguments": '{"time": "7pm"}',
        }
    )
    msgs = json.loads(e["input"])
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert e["tool_call_names"] == ["book_table"]


def test_usage_total_fallback_only_when_components_absent() -> None:
    e = _event({"gen_ai.request.model": "m", "gen_ai.usage.total_tokens": 500})
    assert e["usage_details"] == {"total": 500}


def test_langgraph_node_metadata_maps_to_step() -> None:
    """OpenInference/LangChain packs LangGraph node info into a `metadata` JSON attr (R11)."""
    e = _event(
        {
            "openinference.span.kind": "CHAIN",
            "metadata": json.dumps(
                {"ls_integration": "langgraph", "langgraph_step": 1, "langgraph_node": "call"}
            ),
        },
        name="call",
    )
    assert e["type"] == "CHAIN"
    assert e["step_name"] == "call" and e["step_id"] == "1"


def test_langgraph_node_output_marked_as_state_delta() -> None:
    """A NODE span's output is the channel dict the node returned — mark it so the State view can
    fold it, without copying the body into metadata a second time."""
    e = _event(
        {
            "openinference.span.kind": "CHAIN",
            "metadata": json.dumps(
                {"ls_integration": "langgraph", "langgraph_step": 1, "langgraph_node": "planner"}
            ),
        },
        name="planner",
    )
    assert e["metadata"]["tracely.state_source"] == "output"


def test_langgraph_graph_root_is_not_marked_as_state_delta() -> None:
    """The compiled-graph span carries `ls_integration` too, but its output is the full final
    state, not a delta — marking it would double-count every channel in the fold."""
    e = _event(
        {
            "openinference.span.kind": "CHAIN",
            "metadata": json.dumps({"ls_integration": "langgraph"}),
        },
        name="my-graph",
    )
    assert "tracely.state_source" not in e["metadata"]


# ── PRD 12 P3: convention-version-aware ingestion (R14/D4) ──────────────────


def test_convention_detection() -> None:
    assert _convention({"gen_ai.input.messages": "[]"}) == "gen_ai/structured"
    assert _convention({"gen_ai.prompt.0.role": "user"}) == "gen_ai/legacy"
    assert _convention({"gen_ai.completion": "hi"}) == "gen_ai/legacy"
    assert _convention({"llm.model_name": "gpt-4o"}) == "openinference"
    assert _convention({"llm.input_messages.0.message.role": "user"}) == "openinference"
    assert _convention({"openinference.span.kind": "LLM"}) == "openinference"
    assert _convention({"gen_ai.request.model": "x"}) == "gen_ai/other"
    assert _convention({"tracely.observation.type": "AGENT"}) == "tracely/manual"
    assert _convention({"foo": "bar"}) == "unknown"


def test_convention_version_provenance_in_metadata() -> None:
    """schema_url (semconv version) + instrumentor scope version + detected shape are recorded."""
    span = Span(
        name="s",
        trace_id=b"\x01" * 16,
        span_id=b"\x02" * 8,
        start_time_unix_nano=1,
        end_time_unix_nano=2,
    )
    span.attributes.extend(
        [_kv("gen_ai.input.messages", "[]"), _kv("gen_ai.request.model", "gpt-4o")]
    )
    ss = ScopeSpans(
        scope=InstrumentationScope(name="openinference.instrumentation.openai", version="0.1.51"),
        spans=[span],
        schema_url="https://opentelemetry.io/schemas/1.27.0",
    )
    rs = ResourceSpans(resource=Resource(attributes=[_kv("service.name", "svc")]), scope_spans=[ss])
    e = events_from_request(ExportTraceServiceRequest(resource_spans=[rs]), "p")[0]
    md = e["metadata"]
    assert md["tracely.otel.gen_ai_convention"] == "gen_ai/structured"
    assert md["tracely.otel.schema_url"] == "https://opentelemetry.io/schemas/1.27.0"
    assert md["tracely.otel.scope_version"] == "0.1.51"


def test_unknown_openinference_kind_falls_through_to_span() -> None:
    # not a model/tool span and an unrecognized kind -> SPAN (R15: no hard-coded enum)
    assert map_observation_type({"openinference.span.kind": "SOMETHING_NEW"}) == "SPAN"
    # but gen_ai.operation.name still wins over an unknown kind
    assert (
        map_observation_type(
            {"openinference.span.kind": "SOMETHING_NEW", "gen_ai.operation.name": "chat"}
        )
        == "GENERATION"
    )


def test_langchain_serialized_messages_normalized() -> None:
    """LangChain ChatModel spans arrive with lc-constructor messages under `input.value` and a
    `{generations:[[…]]}` envelope under `output.value`; both must reduce to canonical messages so the
    UI (and evals) see the same `[{role, content}]` shape every other provider produces."""
    gen_in = json.dumps({"messages": [[
        {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "SystemMessage"],
         "kwargs": {"content": "You are helpful.", "type": "system"}},
        {"lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "HumanMessage"],
         "kwargs": {"content": "hi", "type": "human"}},
    ]]})
    gen_out = json.dumps({"generations": [[{"text": "", "type": "ChatGeneration", "message": {
        "lc": 1, "type": "constructor", "id": ["langchain", "schema", "messages", "AIMessage"],
        "kwargs": {"content": "", "additional_kwargs": {"tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}}}]],
        "llm_output": {"token_usage": {}}})
    span = Span(name="ChatOpenAI", trace_id=b"\x03" * 16, span_id=b"\x04" * 8,
                start_time_unix_nano=1_000, end_time_unix_nano=2_000)
    span.attributes.extend([
        _kv("gen_ai.operation.name", "chat"),
        _kv("input.value", gen_in),
        _kv("output.value", gen_out),
    ])
    ss = ScopeSpans(scope=InstrumentationScope(name="test"), spans=[span])
    rs = ResourceSpans(resource=Resource(attributes=[_kv("service.name", "svc")]), scope_spans=[ss])
    e = events_from_request(ExportTraceServiceRequest(resource_spans=[rs]), "p")[0]
    inp, out = json.loads(e["input"]), json.loads(e["output"])
    assert [m["role"] for m in inp] == ["system", "user"]
    assert inp[1]["content"] == "hi"
    assert out[0]["role"] == "assistant"
    assert out[0]["tool_calls"][0]["function"]["name"] == "lookup"


def test_langchain_messages_to_dict_and_node_updates() -> None:
    """LangGraph CHAIN nodes serialize via `messages_to_dict` ({type, data}) and node updates as
    [{update:{messages:[…]}}]; the unwrap maps roles (human→user, ai→assistant) and keeps tool ids."""
    from tracely.otel.messages import _decode_langchain

    chain = {"messages": [{"type": "ai", "data": {"content": "ok", "type": "ai"}}]}
    assert _decode_langchain(chain) == [{"role": "assistant", "content": "ok"}]

    upd = [{"graph": None, "update": {"messages": [
        {"type": "tool", "data": {"content": '{"k": 1}', "type": "tool", "tool_call_id": "c1", "name": "t"}}]}}]
    out = _decode_langchain(upd)
    assert out[0]["role"] == "tool" and out[0]["tool_call_id"] == "c1" and out[0]["name"] == "t"


def test_a_recording_is_never_attributed_to_the_fallback_agent():
    """`default` in the Agent column would claim a customer agent produced Tracely's own work —
    and would file the product's runs under that agent everywhere agent scoping applies."""
    from tracely.services.ingestion_service import IngestionService

    events = [
        {"trace_id": "t1", "span_id": "s1", "is_app_root": True, "agent_slug": "",
         "internal_kind": "eval"},
        {"trace_id": "t2", "span_id": "s2", "is_app_root": True, "agent_slug": ""},
    ]
    IngestionService._attribute_default_agent(events)

    assert events[0]["agent_slug"] == ""          # the recording stays agent-less
    assert events[1]["agent_slug"] != ""          # a real agent-less trace still gets the default


# ── framework coverage: the conventions that were silently dropped ───────────
# Each of these was verified end-to-end against a running stack by ingesting the payload the
# framework actually emits and reading the trace back. They are the difference between "the
# span arrived" and "the span is legible".


def test_a_recorded_exception_is_a_failure_even_without_an_error_status():
    """`level=ERROR` is the ONLY failure signal Tracely has — clustering, failure detection and
    the gate all key off it. Instrumentors routinely call `record_exception()` without also
    setting the span status, which used to render a thrown run as a clean green trace."""
    e = _event(
        {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "charge_card"},
        events=[("exception", {
            "exception.type": "TimeoutError",
            "exception.message": "upstream did not respond in 30s",
        })],
    )
    assert e["level"] == "ERROR"
    assert e["status_message"] == "TimeoutError: upstream did not respond in 30s"


def test_the_semconv_error_type_attribute_is_a_failure():
    e = _event({"gen_ai.operation.name": "chat", "error.type": "RateLimitError"})
    assert (e["level"], e["status_message"]) == ("ERROR", "RateLimitError")


def test_an_explicit_ok_status_beats_a_handled_exception():
    """A framework that caught the exception and marked the span OK said so deliberately —
    flagging it would turn every retry-then-succeed into a failure."""
    e = _event(
        {"gen_ai.operation.name": "chat"},
        status_code=1,
        events=[("exception", {"exception.type": "ValueError", "exception.message": "retried"})],
    )
    assert e["level"] == "DEFAULT"


def test_genai_event_convention_messages_are_read():
    """`opentelemetry-instrumentation-openai-v2` — which the Tracely SDK itself activates as a
    fallback — puts prompts and completions in span EVENTS, not attributes. Those spans used to
    arrive with a model and a token count but no conversation at all."""
    e = _event(
        {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-4o"},
        events=[
            ("gen_ai.user.message", {"content": "what is 2+2?"}),
            ("gen_ai.choice", {"message": json.dumps({"role": "assistant", "content": "4"})}),
        ],
    )
    assert json.loads(e["input"])[0] == {"role": "user", "content": "what is 2+2?"}
    assert json.loads(e["output"])[0]["content"] == "4"


def test_attributes_win_over_events():
    """Events only fill what the attributes left empty — never overwrite a real payload."""
    e = _event(
        {"gen_ai.operation.name": "chat", "input.value": "from attributes"},
        events=[("gen_ai.user.message", {"content": "from events"})],
    )
    assert e["input"] == "from attributes"


def test_vercel_ai_sdk_generation_maps():
    """The dominant TS/JS agent stack names the operation rather than the type and emits no
    gen_ai/OpenInference kind, so every span of a Next.js agent landed as an untyped SPAN."""
    e = _event({
        "ai.operationId": "ai.generateText",
        "ai.model.id": "gpt-4o",
        "ai.prompt": json.dumps({"prompt": "Summarize this ticket"}),
        "ai.response.text": "The customer wants a refund.",
        "ai.usage.promptTokens": 210,
        "ai.usage.completionTokens": 30,
    }, name="ai.generateText")
    assert e["type"] == "GENERATION"
    assert e["model_id"] == "gpt-4o"
    assert e["usage_details"] == {"input": 210, "output": 30}
    assert "Summarize this ticket" in (e["input"] or "")
    assert e["output"] == "The customer wants a refund."


def test_vercel_ai_sdk_tool_call_maps():
    e = _event({
        "ai.operationId": "ai.toolCall",
        "ai.toolCall.name": "lookup_ticket",
        "ai.toolCall.args": '{"id":"T-1"}',
        "ai.toolCall.result": '{"status":"open"}',
    }, name="ai.toolCall")
    assert e["type"] == "TOOL"
    assert e["name"] == "lookup_ticket"          # the tool, not the framework's span name
    assert json.loads(e["input"]) == {"id": "T-1"}
    assert json.loads(e["output"]) == {"status": "open"}


def test_vercel_v5_token_key_names():
    e = _event({
        "ai.operationId": "ai.generateText", "ai.model.id": "gpt-4o",
        "ai.usage.inputTokens": 11, "ai.usage.outputTokens": 22,
    })
    assert e["usage_details"] == {"input": 11, "output": 22}


def test_litellm_usage_survives_python_literals_in_the_repr():
    """LiteLLM's usage repr routinely carries `None`/`True`. The old quote-swap + json.loads
    raised on those and dropped every token count for the span."""
    e = _event({
        "llm.openai.model": "gpt-4o-mini",
        "llm.openai.usage": (
            "{'prompt_tokens': 120, 'completion_tokens': 18, 'total_tokens': 138, "
            "'completion_tokens_details': None, 'cached': False}"
        ),
    })
    assert e["usage_details"] == {"input": 120, "output": 18}


def test_a_framework_agent_name_never_registers_an_agent():
    """The agent is DECLARED, not inferred. A harness (OpenAI Agents, ADK, CrewAI, Pydantic AI)
    stamps `gen_ai.agent.name` on every sub-agent it spins up — reading it registered dozens of
    agents nobody chose. The span still classifies as an AGENT and keeps the name in metadata; it
    just falls back to the trace's declared agent (else `default`)."""
    e = _event({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "billing-specialist"})
    assert e["type"] == "AGENT"
    assert e["agent_slug"] == ""
    assert e["metadata"]["gen_ai.agent.name"] == "billing-specialist"


def test_a_declared_agent_name_wins():
    e = _event({"gen_ai.agent.name": "billing-specialist", "tracely.agent.id": "support"})
    assert e["agent_slug"] == "support"


def test_tenant_is_the_traces_registry_agent_and_labels_stay_labels():
    """`trace(agent="supervisor", tenant="cust-1")`: the conversation belongs to cust-1 — every
    span, so gates/clusters/cases attach there — while each span's own agent label survives in
    metadata for the Agent column. A span with no label of its own inherits the tenant."""
    from tracely.services.ingestion_service import IngestionService

    e = _event({"tracely.agent.id": "supervisor", "tracely.tenant.id": "cust-1"})
    assert e["tenant_slug"] == "cust-1" and e["agent_slug"] == "supervisor"

    events = [
        {"trace_id": "t", "span_id": "root", "is_app_root": True,
         "agent_slug": "supervisor", "tenant_slug": "cust-1",
         "metadata": {"tracely.agent.id": "supervisor"}},
        {"trace_id": "t", "span_id": "sub", "is_app_root": False,
         "agent_slug": "billing", "tenant_slug": "cust-1",
         "metadata": {"tracely.agent.id": "billing"}},
        {"trace_id": "t", "span_id": "bare", "is_app_root": False,
         "agent_slug": "", "tenant_slug": "cust-1", "metadata": {}},
    ]
    IngestionService._attribute_default_agent(events)

    assert [e["agent_slug"] for e in events] == ["cust-1", "cust-1", "cust-1"]
    assert events[0]["metadata"]["tracely.agent.id"] == "supervisor"   # label untouched
    assert events[1]["metadata"]["tracely.agent.id"] == "billing"      # sub-agent label untouched
    assert events[2]["metadata"]["tracely.agent.id.inherited"] == "cust-1"
    assert "tracely.agent.id.inherited" not in events[0]["metadata"]


def test_without_a_tenant_the_older_rule_holds():
    from tracely.services.ingestion_service import IngestionService

    events = [
        {"trace_id": "t", "span_id": "root", "is_app_root": True, "agent_slug": "supervisor"},
        {"trace_id": "t", "span_id": "sub", "is_app_root": False, "agent_slug": "billing"},
        {"trace_id": "t", "span_id": "bare", "is_app_root": False, "agent_slug": ""},
    ]
    IngestionService._attribute_default_agent(events)
    assert [e["agent_slug"] for e in events] == ["supervisor", "billing", "supervisor"]


def test_gen_ai_conversation_id_threads_a_run():
    e = _event({"gen_ai.operation.name": "chat", "gen_ai.conversation.id": "conv-9"})
    assert e["conversation_id"] == "conv-9"


def test_langgraph_thread_id_threads_a_run():
    """LangGraph's thread id rides in the same `metadata` blob the step columns come from."""
    e = _event({
        "openinference.span.kind": "CHAIN",
        "metadata": json.dumps({"langgraph_node": "planner", "thread_id": "lg-1"}),
    })
    assert e["conversation_id"] == "lg-1"
    assert e["step_name"] == "planner"


def test_tracely_conversation_id_still_wins():
    e = _event({
        "tracely.conversation.id": "explicit",
        "gen_ai.conversation.id": "semconv",
        "metadata": json.dumps({"thread_id": "lg"}),
    })
    assert e["conversation_id"] == "explicit"


def test_traceloop_decorator_spans_get_a_type_and_io():
    """OpenLLMetry's @workflow/@task/@agent/@tool spans carry their payload under
    `traceloop.entity.*` — they used to arrive typeless and empty."""
    e = _event({
        "traceloop.span.kind": "tool",
        "traceloop.entity.input": '{"city":"SF"}',
        "traceloop.entity.output": '{"tempF":64}',
    })
    assert e["type"] == "TOOL"
    assert json.loads(e["input"]) == {"city": "SF"}
    assert json.loads(e["output"]) == {"tempF": 64}


def test_a_bad_turn_index_does_not_take_the_batch_down():
    """The mapper runs inside the ingest task, which retries then drops the WHOLE payload — so
    one span with a junk index used to cost every other span in the request."""
    e = _event({"gen_ai.operation.name": "chat", "tracely.turn.index": "not-a-number"})
    assert e["turn_index"] == 0


def test_model_falls_back_to_the_invocation_parameters_blob():
    """Found by scanning real ingested data: the OpenInference OpenAI instrumentor emits some
    spans with NO `llm.model_name`, carrying the model only inside `llm.invocation_parameters`.
    An empty model_id costs the Model column and every cost figure keyed off it."""
    e = _event({
        "openinference.span.kind": "LLM",
        "llm.provider": "openai",
        "llm.invocation_parameters": '{"model": "gpt-4o-mini"}',
    })
    assert e["type"] == "GENERATION"
    assert e["model_id"] == "gpt-4o-mini"


def test_an_explicit_model_still_wins_over_the_blob():
    e = _event({
        "openinference.span.kind": "LLM",
        "llm.model_name": "gpt-4o",
        "llm.invocation_parameters": '{"model": "gpt-4o-mini"}',
    })
    assert e["model_id"] == "gpt-4o"


def test_retriever_documents_become_the_span_output():
    """What a retrieval returned is the point of the step, and it lives only in these flattened
    keys — no `output.value`. Every RAG agent's retrieval used to render an empty Output, so
    neither a human nor a judge could tell a good retrieval from a bad one."""
    e = _event({
        "openinference.span.kind": "RETRIEVER",
        "input.value": "refund policy",
        "retrieval.documents.0.document.id": "doc-1",
        "retrieval.documents.0.document.score": 0.91,
        "retrieval.documents.0.document.content": "Refunds within 30 days.",
        "retrieval.documents.1.document.id": "doc-2",
        "retrieval.documents.1.document.content": "Photograph damaged items.",
    })
    assert e["type"] == "RETRIEVER"
    docs = json.loads(e["output"])
    assert [d["id"] for d in docs] == ["doc-1", "doc-2"]
    assert docs[0]["content"] == "Refunds within 30 days."
    # and they must not also be dumped into metadata
    assert not any(k.startswith("retrieval.documents") for k in e["metadata"])


def test_anthropic_thinking_blocks_render_as_text():
    """Extended thinking arrives as `{type:'thinking', thinking:'…'}`. The block keeps its type
    (reasoning is styled differently) but mirrors into `text`, which is what renderers read —
    otherwise the model's reasoning displayed as a raw JSON blob."""
    e = _event({
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "claude-sonnet-4-5",
        "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Policy says 30 days."},
            {"type": "text", "text": "You can get a refund within 30 days."},
        ]}]),
    })
    blocks = json.loads(e["output"])[0]["content"]
    assert blocks[0] == {"type": "thinking", "text": "Policy says 30 days."}
    assert blocks[1]["text"] == "You can get a refund within 30 days."


# ── tool I/O reconstruction must not cross traces ────────────────────────────


def _tool_batch() -> list[dict]:
    """Two traces in ONE OTLP batch. Trace A's tool has no recorded output; trace B's later
    generation carries a tool result. They must not be joined."""
    return [
        {"span_id": "a1", "trace_id": "A", "type": "GENERATION", "start_time": "1",
         "parent_span_id": "", "output": json.dumps([{"role": "assistant", "tool_calls": [
             {"id": "call_a", "function": {"name": "get_order", "arguments": '{"id":"A"}'}}]}])},
        {"span_id": "a2", "trace_id": "A", "type": "TOOL", "name": "get_order",
         "start_time": "2", "parent_span_id": "a1", "input": "", "output": ""},
        {"span_id": "b1", "trace_id": "B", "type": "GENERATION", "start_time": "3",
         "parent_span_id": "", "input": json.dumps([
             {"role": "tool", "tool_call_id": "call_a", "content": "TRACE B SECRET"}])},
    ]


def test_a_tool_never_adopts_another_traces_result():
    from tracely.otel.tool_enrichment import _enrich_tool_io

    events = _tool_batch()
    _enrich_tool_io(events)
    tool = next(e for e in events if e["span_id"] == "a2")
    assert "TRACE B SECRET" not in (tool.get("output") or "")


def test_a_tool_still_gets_its_own_traces_result():
    from tracely.otel.tool_enrichment import _enrich_tool_io

    events = _tool_batch()
    events[2]["trace_id"] = "A"  # same trace now — the join is correct here
    events[2]["start_time"] = "3"
    _enrich_tool_io(events)
    tool = next(e for e in events if e["span_id"] == "a2")
    assert tool["output"] == "TRACE B SECRET"


def test_an_array_shaped_tool_input_is_not_overwritten():
    """`has_full_input` tested `startswith("{")`, so a list-shaped argument payload looked
    missing and was replaced by the reconstruction."""
    from tracely.otel.tool_enrichment import _enrich_tool_io

    events = _tool_batch()
    tool = events[1]
    tool["input"] = '["already", "recorded"]'
    _enrich_tool_io(events)
    assert json.loads(tool["input"]) == ["already", "recorded"]


def test_tool_calls_indexed_from_a_bare_output_dict() -> None:
    """`tracely.output` bypasses `_io_messages` entirely — a single (unlisted) message dict must
    still index its tool calls, or `missing_tools` can never fire."""
    e = _event(
        {
            "gen_ai.request.model": "gpt-4o",
            "tracely.output": json.dumps(
                {
                    "content": "on it",
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "search", "arguments": '{"q": "x"}'}}
                    ],
                }
            ),
        }
    )
    assert e["tool_call_names"] == ["search"]


def test_tool_calls_indexed_from_anthropic_tool_use_blocks() -> None:
    e = _event(
        {
            "gen_ai.request.model": "claude-sonnet-4",
            "gen_ai.output.messages": json.dumps(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "checking"},
                            {"type": "tool_use", "id": "tu_1", "name": "get_weather",
                             "input": {"city": "SF"}},
                        ],
                    }
                ]
            ),
        }
    )
    assert e["tool_call_names"] == ["get_weather"]
