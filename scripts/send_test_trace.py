"""Send a sample multi-span OTLP trace (agent -> llm -> failing tool) to the running API.

    uv run python scripts/send_test_trace.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    InstrumentationScope,
    KeyValue,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

API = os.environ.get("TRACELY_API", "http://localhost:8000")
KEY = os.environ.get("TRACELY_KEY", "tracely_dev_key")

# A handful of realistic user queries so random runs don't all look identical.
_QUERIES = [
    ("What's the weather like in San Francisco right now?", "San Francisco, CA"),
    ("Give me a weather update for New York.", "New York, NY"),
    ("Is it going to rain in Seattle today?", "Seattle, WA"),
    ("How hot is it in Austin this afternoon?", "Austin, TX"),
    ("Weather forecast for Chicago this weekend?", "Chicago, IL"),
]


def kv(k: str, v) -> KeyValue:
    if isinstance(v, bool):
        return KeyValue(key=k, value=AnyValue(bool_value=v))
    if isinstance(v, int):
        return KeyValue(key=k, value=AnyValue(int_value=v))
    if isinstance(v, float):
        return KeyValue(key=k, value=AnyValue(double_value=v))
    return KeyValue(key=k, value=AnyValue(string_value=str(v)))


def kv_arr(k: str, items) -> KeyValue:
    return KeyValue(
        key=k,
        value=AnyValue(array_value=ArrayValue(values=[AnyValue(string_value=str(x)) for x in items])),
    )


def main() -> None:
    # Four demo run variants:
    #   default       -> get_weather ERRORS              (explicit failure)
    #   FIXED=1       -> get_weather SUCCEEDS            (the fix; gate PASS)
    #   SILENT=1      -> model requests get_weather but it NEVER runs  (SILENT failure:
    #                    no error span, but the requested tool was not executed)
    #   HALLUCINATE=1 -> get_weather SUCCEEDS but the model fabricates a wrong, self-
    #                    contradictory answer (quality failure caught by the LLM judge,
    #                    not by any structural check)
    fixed = os.environ.get("FIXED", "") not in ("", "0", "false")
    silent = os.environ.get("SILENT", "") not in ("", "0", "false")
    hallucinate = os.environ.get("HALLUCINATE", "") not in ("", "0", "false")
    env = os.environ.get("ENV", "")  # e.g. "ci" for a CI run; empty -> prod
    random_run = os.environ.get("RANDOM", "") not in ("", "0", "false")
    # QUERY_IDX picks the user query (0-based into _QUERIES). Different queries → different inputs
    # → different input_digests, so two demo scenarios (e.g. a silent-failure case and a
    # hallucination case) don't collide on the idempotency key.
    query_idx = int(os.environ.get("QUERY_IDX", "0") or "0") % len(_QUERIES)

    if random_run:
        import secrets
        trace_id = secrets.token_bytes(16)
        # Spread random runs over the last 3 days so they don't all pile on the same second.
        spread_ns = secrets.randbelow(3 * 24 * 3600 * 1_000_000_000)
        now = time.time_ns() - spread_ns
        # Pick a random query variant for diversity.
        idx = int.from_bytes(trace_id[:1], "big") % len(_QUERIES)
    else:
        # Deterministic base: same (variant, query, env) always produces the same trace_id so
        # re-runs dedup. Query index is in a distinct bit region so scenarios stay separable.
        base = 0xFACADE if hallucinate else 0x511EE7 if silent else 0xF0FFEE if fixed else 0xC0FFEE
        base += (0x010000 if env == "ci" else 0) + (query_idx * 0x01000000)
        trace_id = base.to_bytes(16, "big")
        now = time.time_ns()
        idx = query_idx

    user_query, city = _QUERIES[idx]

    root_id = (1).to_bytes(8, "big")
    llm_id  = (2).to_bytes(8, "big")
    tool_id = (3).to_bytes(8, "big")

    # ── Compose realistic LLM input / output as proper message objects ───────
    # Bare array — the frontend ChatPill triggers on Array<{role}>
    llm_input = json.dumps([
        {"role": "system", "content": "You are a helpful assistant with access to real-time weather tools."},
        {"role": "user",   "content": user_query},
    ])

    if hallucinate:
        # Tool succeeds (get_weather returns 64°F/sunny) but the model fabricates a contradictory,
        # absurd answer — a QUALITY failure no structural check catches (the tool DID run).
        fabricated = (
            f"It's 9000°F and snowing heavily in {city} right now, "
            "and the midnight sun is blazing overhead at noon."
        )
        llm_output = json.dumps({
            "role": "assistant",
            "content": fabricated,
            "finish_reason": "stop",
        })
        agent_answer = fabricated
    elif fixed or silent:
        llm_output = json.dumps({
            "role": "assistant",
            "content": None,
            "finish_reason": "tool_calls",
            "tool_calls": [{
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": city})},
            }],
        })
        agent_answer = f"It's 64°F and sunny in {city}."
    else:
        llm_output = json.dumps({
            "role": "assistant",
            "content": None,
            "finish_reason": "tool_calls",
            "tool_calls": [{
                "id": "call_abc123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"city": city})},
            }],
        })
        agent_answer = f"Sorry, I couldn't fetch the weather for {city} right now."

    # ── Spans ─────────────────────────────────────────────────────────────────
    root = Span(
        name="planner", trace_id=trace_id, span_id=root_id,
        start_time_unix_nano=now,
        end_time_unix_nano=now + 5_000_000_000,
    )
    root.attributes.extend([
        kv("tracely.agent.id", "planner"),
        kv("tracely.observation.type", "AGENT"),
        kv("session.id", "sess-1"),
        # message-level I/O as structured message objects (role + typed content blocks)
        kv("tracely.input",  json.dumps({"role": "user", "content": [{"type": "text", "text": user_query}]})),
        kv("tracely.output", json.dumps({"role": "assistant", "content": [{"type": "text", "text": agent_answer}]})),
    ])

    llm = Span(
        name="gpt-4o", trace_id=trace_id, span_id=llm_id, parent_span_id=root_id,
        start_time_unix_nano=now + 100_000_000,
        end_time_unix_nano=now + 3_000_000_000,
    )
    llm.attributes.extend([
        kv("gen_ai.operation.name", "chat"),
        kv("gen_ai.request.model", "gpt-4o"),
        kv("gen_ai.request.temperature", 0.7),
        kv("gen_ai.request.top_p", 1.0),
        kv("gen_ai.request.max_tokens", 1024),
        kv("gen_ai.usage.input_tokens", 812),
        kv("gen_ai.usage.output_tokens", 96),
        kv("tracely.agent.id", "planner"),
        kv("tracely.metadata.prompt_version", "v2"),
        kv("tracely.input",  llm_input),   # structured messages array
        kv("tracely.output", llm_output),  # structured assistant message
        kv_arr("tracely.tool_calls", ["get_weather"]),
    ])

    spans = [root, llm]

    if not silent:
        tool = Span(
            name="get_weather", trace_id=trace_id, span_id=tool_id, parent_span_id=root_id,
            start_time_unix_nano=now + 3_200_000_000,
            end_time_unix_nano=now + 4_500_000_000,
        )
        tool_input  = json.dumps({"city": city})
        tool_output = json.dumps({"tempF": 64, "condition": "sunny", "humidity_pct": 58})
        if not fixed and not hallucinate:
            tool.status.code = 2  # ERROR
            tool.status.message = "upstream timeout after 3 retries"
        tool.attributes.extend([
            kv("gen_ai.operation.name", "execute_tool"),
            kv("gen_ai.tool.name", "get_weather"),
            kv("tracely.agent.id", "planner"),
            kv("tracely.input",  tool_input),
            kv("tracely.output", tool_output),
        ])
        spans.append(tool)

    if env:
        for sp in spans:
            sp.attributes.append(kv("tracely.env", env))
    if os.environ.get("TRACELY_SAMPLE"):  # seeded demo data: never the customer's activation
        for sp in spans:
            sp.attributes.append(kv("tracely.sample", True))

    req = ExportTraceServiceRequest(resource_spans=[
        ResourceSpans(
            resource=Resource(attributes=[
                kv("service.name", "demo"),
                kv("telemetry.sdk.language", "python"),
            ]),
            scope_spans=[ScopeSpans(scope=InstrumentationScope(name="demo"), spans=spans)],
        )
    ])

    body = req.SerializeToString()
    request = urllib.request.Request(
        f"{API}/v1/traces", data=body,
        headers={"Content-Type": "application/x-protobuf", "Authorization": f"Bearer {KEY}"},
    )
    resp = urllib.request.urlopen(request)
    kind = (
        "hallucinated-answer" if hallucinate
        else "silent-failure" if silent
        else "fixed" if fixed
        else "failing"
    )
    suffix = f", env={env}" if env else ""
    print(f"POST /v1/traces -> {resp.status}  ({kind}{suffix})")
    print(f"trace_id (hex): {trace_id.hex()}")


if __name__ == "__main__":
    main()
