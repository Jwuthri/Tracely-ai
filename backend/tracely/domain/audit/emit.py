"""A finished rollout as OTLP: the conversation the target actually had, one trace per turn.

Three decisions, each of which is an invariant:

1. **Internal, and no agent.** Every span carries `tracely.internal.kind = "audit"` and none
   declares an agent. Tracely simulated this target — nothing here is the customer's agent
   answering a real request — so it must stay out of trends, meta-analysis, gate baselines and
   "import tools from traces", which `_REAL` already guarantees for every internal kind.
2. **A pure function of the checkpoint.** Ids are derived from `(rollout, turn, message)` and the
   timestamps are the ones recorded when each message happened. The `events` sort key ends in
   `start_time`, so re-emitting with fresh timestamps would not replace the rows, it would
   duplicate them; emitting from the checkpoint twice produces byte-identical spans instead.
3. **The final branch only.** Every reader of a thread linearises it. Rolled-back turns live in
   the checkpoint, where the judges read them and the Audits UI shows them.

Shape per turn: a root AGENT span (user message in, the target's last text out), one GENERATION
span per target model call, one TOOL span per answered tool call.
"""

from __future__ import annotations

import json
import uuid

from tracely.domain.audit import media
from tracely.domain import otlp_payload as otlp
from tracely.domain.audit.state import AuditState, path

INTERNAL_KIND = "audit"
# Fixed namespace for the derived ids — changing it orphans every audit thread already emitted.
_NS = uuid.UUID("b3a1f0c2-7d4e-5c1a-9e6b-2f8a4d0c6e13")


def conversation_id(rollout_id: str) -> str:
    """Namespaced so an audit thread can never merge with a real conversation's id."""
    return f"audit:{rollout_id}"


def _trace_id(rollout_id: str, turn: int) -> bytes:
    return uuid.uuid5(_NS, f"{rollout_id}:{turn}").bytes


def _span_id(rollout_id: str, key: str) -> bytes:
    return uuid.uuid5(_NS, f"{rollout_id}:span:{key}").bytes[:8]


def _wire(node: dict) -> dict:
    """A message node in OpenAI wire form — what the tool-enrichment pass reads tool calls from."""
    if node["role"] == "assistant":
        msg: dict = {"role": "assistant", "content": node.get("content") or ""}
        if node.get("tool_calls"):
            msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                for c in node["tool_calls"]
            ]
        return msg
    if node["role"] == "tool":
        return {"role": "tool", "tool_call_id": node.get("tool_call_id") or "", "content": node.get("content") or ""}
    return {"role": node["role"], "content": media.node_text(node)}


def rollout_payload(
    state: AuditState, *, rollout_id: str, title: str, meta: dict[str, str]
) -> tuple[dict, list[str]]:
    """`(ExportTraceServiceRequest, [trace id hex per turn])` for the surviving conversation.
    `({}, [])` when the target never got a turn."""
    nodes = path(state)
    conv = conversation_id(rollout_id)
    turns: list[list[dict]] = []
    preamble: list[dict] = []  # what precedes the first user message (an auditor-written system prompt)
    for node in nodes:
        if node["role"] == "user":
            turns.append([node])
        elif turns:
            turns[-1].append(node)
        else:
            preamble.append(node)
    if not turns:
        return {}, []

    system = (
        [{"role": "system", "content": state.system_prompt}]
        if state.control == "fixed" and state.system_prompt
        else [_wire(n) for n in preamble if n["role"] == "system"]
    )
    spans: list[dict] = []
    trace_ids: list[str] = []
    for index, turn in enumerate(turns):
        trace_id = _trace_id(rollout_id, index)
        trace_ids.append(trace_id.hex())
        root_id = _span_id(rollout_id, f"turn{index}")

        def base(node_id: str = "") -> list[dict]:
            return [
                otlp.attr("tracely.internal.kind", INTERNAL_KIND),
                otlp.attr("tracely.internal.subject_id", rollout_id),
                otlp.attr("tracely.env", "ci"),
                otlp.attr("tracely.conversation.id", conv),
                otlp.attr("tracely.turn.id", f"{conv}:{index}"),
                otlp.attr("tracely.turn.index", index),
                *(otlp.attr(f"tracely.metadata.{k}", v) for k, v in meta.items() if v),
                *([otlp.attr("tracely.metadata.message_id", node_id)] if node_id else []),
            ]

        user = turn[0]
        start_ns = int(user["ts_ns"])
        end_ns = max(int(n.get("end_ns") or n["ts_ns"]) for n in turn)
        last_text = next(
            (n.get("content") or "" for n in reversed(turn) if n["role"] == "assistant"), ""
        )
        spans.append(otlp.span(
            trace_id=trace_id, span_id=root_id, name="audit.turn",
            start_ns=start_ns, end_ns=max(end_ns, start_ns),
            attributes=base(user["id"]) + [
                otlp.attr("tracely.observation.type", "AGENT"),
                otlp.attr("tracely.is_app_root", True),
                otlp.attr("tracely.trace.name", title),
                # Text and markers only: media stays in the blob store, never base64 in a span.
                otlp.attr("tracely.input", media.node_text(user)),
                otlp.attr("tracely.output", last_text),
            ],
        ))

        # What each generation was SENT that the previous one was not: the system prompt once,
        # then only the new messages — the full history on every span would store the
        # conversation N times over.
        unsent: list[dict] = (system if index == 0 else []) + [_wire(user)]
        cursor_ns = start_ns
        for node in turn[1:]:
            if node["role"] == "assistant":
                node_start = int(node["ts_ns"])
                node_end = max(int(node.get("end_ns") or node_start), node_start)
                usage = node.get("usage") or {}
                spans.append(otlp.span(
                    trace_id=trace_id, span_id=_span_id(rollout_id, node["id"]),
                    parent_span_id=root_id, name=str(node.get("model") or state.target_model),
                    start_ns=node_start, end_ns=node_end,
                    attributes=base(node["id"]) + [
                        otlp.attr("tracely.observation.type", "GENERATION"),
                        otlp.attr("tracely.model", str(node.get("model") or state.target_model)),
                        otlp.attr("tracely.input", json.dumps(unsent, ensure_ascii=False)),
                        otlp.attr("tracely.output", json.dumps(_wire(node), ensure_ascii=False)),
                        *([otlp.attr("gen_ai.usage.input_tokens", int(usage["input_tokens"]))]
                          if usage.get("input_tokens") else []),
                        *([otlp.attr("gen_ai.usage.output_tokens", int(usage["output_tokens"]))]
                          if usage.get("output_tokens") else []),
                        *([otlp.attr("tracely.metadata.reasoning", str(node["reasoning"])[:8000])]
                          if node.get("reasoning") else []),
                    ],
                ))
                unsent = []
                cursor_ns = node_end
            elif node["role"] == "tool":
                node_end = max(int(node["ts_ns"]), cursor_ns)
                spans.append(otlp.span(
                    trace_id=trace_id, span_id=_span_id(rollout_id, node["id"]),
                    parent_span_id=root_id, name=str(node.get("name") or "tool"),
                    start_ns=cursor_ns, end_ns=node_end,
                    attributes=base(node["id"]) + [
                        otlp.attr("tracely.observation.type", "TOOL"),
                        otlp.attr("tool.name", str(node.get("name") or "tool")),
                        otlp.attr("tracely.tool_call.id", str(node.get("tool_call_id") or "")),
                        otlp.attr("tracely.input", json.dumps(node.get("args") or {}, ensure_ascii=False)),
                        otlp.attr("tracely.output", node.get("content") or ""),
                        # Which path produced this result: fabricated by the attacker, or real.
                        otlp.attr("tracely.metadata.tool_resolution", str(node.get("resolution") or "synthetic")),
                    ],
                    error="tool returned an error" if node.get("status") == "error" else "",
                ))
                unsent.append(_wire(node))
                cursor_ns = node_end
    return otlp.request(spans, scope_name="tracely.internal.audit"), trace_ids
