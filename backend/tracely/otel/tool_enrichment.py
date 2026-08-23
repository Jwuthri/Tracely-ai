"""Reconstruct TOOL spans' input/output for instrumentors that don't capture them
(OpenInference openai-agents, langchain TOOL spans).

Strategy: walk each TOOL to its nearest GENERATION ancestor/sibling, parse its `tool_calls`
for arguments, and pull tool results out of the next GENERATION's input messages.

Match (best-first):
  1) `tool_call_id` on the TOOL span ↔ `tool_call.id` in the generation
  2) Tool name + positional order among TOOL siblings of the same parent
"""

from __future__ import annotations

import json
from typing import Any

from tracely.otel.attributes import _first, _to_str
from tracely.otel.types import GENERATION, TOOL


def _tool_call_names(attrs: dict[str, Any], otype: str, output: str | None = None) -> list[str]:
    """Tools the model requested. Explicit `tracely.tool_calls` (array) wins; else the function
    names parsed out of the span's RESOLVED output (`_io_field`, i.e. what we actually store);
    else the tool name on a TOOL span.

    Reading the resolved output rather than re-deriving from `_io_messages` is the point: the
    manual `tracely.output` / `langfuse.observation.output` escape hatches never go through
    `_io_messages`, so a plain `{"content": …, "tool_calls": […]}` dict indexed nothing — and an
    empty `tool_call_names` silently kills `missing_tools` in `domain/traces/spans.py`.
    """
    tc = attrs.get("tracely.tool_calls")
    if isinstance(tc, list):
        return [str(x) for x in tc if x]
    names = [c["name"] for c in _extract_tool_calls(output or "")]
    if names:
        return names
    name = _first(attrs, ["gen_ai.tool.name", "tool.name"])
    return [str(name)] if (otype == TOOL and name) else []


def _extract_tool_calls(output_str: str) -> list[dict[str, Any]]:
    """Pull a list of {id, name, arguments} from a GENERATION span's output. `args` is the
    parsed JSON object (instrumentors store it as a JSON string)."""
    if not output_str:
        return []
    try:
        parsed = json.loads(output_str)
    except (json.JSONDecodeError, ValueError):
        return []
    msgs = parsed if isinstance(parsed, list) else [parsed]
    out: list[dict[str, Any]] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        # Anthropic-style content blocks: `{"type":"tool_use","name":…,"input":{…}}`.
        for block in m.get("content") if isinstance(m.get("content"), list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                out.append({
                    "id": block.get("id"),
                    "name": str(block["name"]),
                    "args": block.get("input"),
                })
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            raw_args = fn.get("arguments") if isinstance(fn, dict) else None
            args: Any = raw_args
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    args = raw_args
            if name:
                out.append({"id": tc.get("id"), "name": str(name), "args": args})
    return out


def _tool_results_from_input(input_str: str) -> dict[str, str]:
    """Pull `{tool_call_id -> result_content}` from a GENERATION span's input (conversation
    history). The model that responds to tool dispatch carries the results as `{role:"tool",
    tool_call_id, content}` entries in its input messages."""
    if not input_str:
        return {}
    try:
        parsed = json.loads(input_str)
    except (json.JSONDecodeError, ValueError):
        return {}
    msgs = parsed if isinstance(parsed, list) else []
    out: dict[str, str] = {}
    pending: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        if str(m.get("role") or "").lower() == "tool":
            content = m.get("content")
            if isinstance(content, list):
                content = json.dumps(content)
            elif not isinstance(content, str):
                content = _to_str(content)
            cid = m.get("tool_call_id") or ""
            if cid:
                out[str(cid)] = str(content)
            else:
                pending.append(str(content))
    if pending and not out:
        # Anonymous tool results — keep them in order under positional keys so the caller can
        # match by index when no `tool_call_id` is available.
        for i, c in enumerate(pending):
            out[f"__pos_{i}"] = c
    return out


def _enrich_tool_io(events: list[dict[str, Any]]) -> None:
    """In-place: fill TOOL spans' input/output by joining to nearby GENERATION spans."""
    by_id: dict[str, dict[str, Any]] = {e["span_id"]: e for e in events if e.get("span_id")}
    if not by_id:
        return
    # children-by-parent for sibling-order matching
    children: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        children.setdefault(e.get("parent_span_id") or "", []).append(e)
    # Sort each parent's children by start_time so positional indices are stable.
    for siblings in children.values():
        siblings.sort(key=lambda x: x.get("start_time") or "")

    # Index GENERATION spans by trace_id so the lookup is per-trace. Two indexes: the one keyed
    # on `output` supplies a tool's ARGUMENTS (the call the model made), the one keyed on `input`
    # supplies its RESULT (the tool message fed back on the next turn).
    gens_by_trace: dict[str, list[dict[str, Any]]] = {}
    gens_with_input_by_trace: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("type") != GENERATION or not e.get("start_time"):
            continue
        trace = e.get("trace_id") or ""
        if e.get("output"):
            gens_by_trace.setdefault(trace, []).append(e)
        if e.get("input"):
            gens_with_input_by_trace.setdefault(trace, []).append(e)
    for index in (gens_by_trace, gens_with_input_by_trace):
        for gens in index.values():
            gens.sort(key=lambda x: x["start_time"])

    def _nearest_gen(tool_ev: dict[str, Any]) -> dict[str, Any] | None:
        st = tool_ev.get("start_time")
        if not st:
            return None
        gens = gens_by_trace.get(tool_ev.get("trace_id") or "", [])
        match: dict[str, Any] | None = None
        for g in gens:
            if g["start_time"] <= st:
                match = g
            else:
                break
        return match

    def _next_gen_after(span: dict[str, Any]) -> dict[str, Any] | None:
        """The next generation in the SAME trace. Scoping matters: one OTLP batch carries many
        traces, and an unscoped scan let a tool span adopt a different conversation's tool result
        as its own output — wrong data, silently, in whichever trace lost the race."""
        st = span.get("start_time")
        if not st:
            return None
        for g in gens_with_input_by_trace.get(span.get("trace_id") or "", []):
            if g["start_time"] > st:
                return g
        return None

    for ev in events:
        if ev.get("type") != TOOL:
            continue
        # `[` as well as `{`: a tool whose arguments are a list ("["a","b"]") had a perfectly good
        # recorded input treated as missing and overwritten by the reconstruction below.
        has_full_input = bool(ev.get("input")) and ev["input"].lstrip().startswith(("{", "["))
        if has_full_input and ev.get("output"):
            continue  # already complete
        gen = _nearest_gen(ev)
        if not gen:
            continue
        calls = _extract_tool_calls(gen.get("output") or "")
        if not calls:
            continue
        # 1) try tool_call_id match (most precise)
        tcid = ev.get("tool_call_id") or ""
        match = next(
            (c for c in calls if c.get("id") and str(c["id"]) == str(tcid)), None
        ) if tcid else None
        # 2) fall back to name + positional order among same-parent TOOL siblings
        if match is None:
            name = (ev.get("name") or "").lower()
            same_parent_tools = [
                s for s in children.get(ev.get("parent_span_id") or "", [])
                if s.get("type") == TOOL
            ]
            idx_in_parent = same_parent_tools.index(ev) if ev in same_parent_tools else -1
            named = [c for c in calls if (c.get("name") or "").lower() == name]
            if named and 0 <= idx_in_parent < len(named):
                match = named[idx_in_parent]
            elif named:
                match = named[0]
            elif 0 <= idx_in_parent < len(calls):
                match = calls[idx_in_parent]
        if not match:
            continue
        if not has_full_input and match.get("args") is not None:
            args = match["args"]
            ev["input"] = _to_str(args) if not isinstance(args, str) else args
            if not isinstance(args, str):
                ev["input"] = json.dumps(args)
        # Reconstruct output from the NEXT generation's tool results
        if not ev.get("output"):
            next_gen = _next_gen_after(ev)
            if next_gen:
                results = _tool_results_from_input(next_gen.get("input") or "")
                cid = match.get("id")
                if cid and str(cid) in results:
                    ev["output"] = results[str(cid)]
                else:
                    # positional fallback (no tool_call_id propagation)
                    same_parent_tools = [
                        s for s in children.get(ev.get("parent_span_id") or "", [])
                        if s.get("type") == TOOL
                    ]
                    idx_in_parent = same_parent_tools.index(ev) if ev in same_parent_tools else -1
                    pos_key = f"__pos_{idx_in_parent}"
                    if pos_key in results:
                        ev["output"] = results[pos_key]
