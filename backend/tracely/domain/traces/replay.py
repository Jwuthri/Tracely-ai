"""Conversation replay: turn a thread's spans into a time-ordered SCRIPT the UI can act out.

Pure — no I/O. The caller supplies the spans (`async_reader.thread_spans_full`), a
`agent_id -> display name` map, and an alias map (slug/name -> agent_id); this module derives
WHO acted, WHEN, UNDER WHOM — and, for the fleet office view, WHERE: every event carries a
`station` (desk / computer / library / peer) and delegations carry the callee, so the UI can
walk a character over and put the handoff text in a speech bubble.

The nesting is the point: a span's actor is the nearest AGENT/SUBAGENT ancestor, so an
`llm`/`tool` event is attributed to the agent that actually ran it, and an agent span nested
under another agent is a SUB-agent (rendered as a helper pulled in for one job, not staff).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

# observation type -> replay event kind. Anything else rides as "step".
_KIND = {
    "AGENT": "turn",
    "CHAIN": "turn",
    "SUBAGENT": "spawn",
    "GENERATION": "llm",
    "EMBEDDING": "llm",
    "THINKING": "think",
    "TOOL": "tool",
    "RETRIEVER": "tool",
    "GUARDRAIL": "guard",
    "DELEGATE": "delegate",  # a container: the handover brackets the work the callee then does
    "SKILL": "skill",
}
_ACTOR_TYPES = {"AGENT", "SUBAGENT"}
# The person the whole office works for. Synthetic actor id — no agent slug can collide with it.
CUSTOMER = "__customer__"

# event kind -> where the actor performs it. The span TYPE is authoritative: SKILL and
# RETRIEVER are knowledge access (the library), TOOL is an action (the computer) — instrument
# with the right span type rather than relying on tool naming. DELEGATE resolves to the
# callee's desk.
_STATION = {
    "turn": "desk",
    "spawn": "desk",
    "llm": "desk",
    "think": "desk",
    "guard": "desk",
    "step": "desk",
    "tool": "computer",
    "skill": "library",
    "delegate": "peer",
    "ask": "door",
}


def _ms(a: datetime | None, b: datetime | None) -> int:
    """Milliseconds from `a` to `b`, clamped at 0 (clock skew across services is real)."""
    if a is None or b is None:
        return 0
    return max(0, int((b - a).total_seconds() * 1000))


def _preview(value: Any, limit: int = 120) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _text_of(value: Any, limit: int = 120) -> str:
    """Human text from a span output. Outputs are often a JSON message list
    (`[{"role": "assistant", "content": ...}]`) — a speech bubble must show the words,
    never the envelope. Falls back to the raw preview."""
    if isinstance(value, str) and value.lstrip().startswith(("[", "{", '"')):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return _preview(value, limit)
        # ingest stores a `set_io` string JSON-encoded, so a plain sentence arrives as
        # '"Delegating the FAQ \u2014 …"'. Decode it, or the bubble shows quotes and escapes.
        if isinstance(parsed, str):
            return _preview(parsed, limit)
        msgs = parsed if isinstance(parsed, list) else [parsed]
        envelope = any(isinstance(m, dict) and ("role" in m or "tool_calls" in m) for m in msgs)
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return _preview(content, limit)
            if isinstance(content, list):  # Anthropic-style content blocks
                text = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
                ).strip()
                if text:
                    return _preview(text, limit)
            # A turn that ONLY calls tools has empty content — say what it reached for.
            calls = _called_names(m)
            if calls:
                return _preview("→ " + ", ".join(calls), limit)
        # A message envelope with nothing sayable: better silence than the raw JSON in a
        # speech bubble. Non-message payloads (a tool's `{"total": 1299}`) still preview.
        if envelope:
            return ""
    return _preview(value, limit)


def _user_text_of(value: Any, limit: int = 160) -> str:
    """The CUSTOMER's words in a span input: the last `user` message of a message list, a
    plain string, or a `{"messages"|"input"|"question"|"query": …}` wrapper. Anything else
    (a framework's config payload, a tool's arguments) is "" — never a JSON envelope."""
    if isinstance(value, str) and value.lstrip().startswith(("[", "{", '"')):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return _preview(value, limit)
    if isinstance(value, str):
        return _preview(value, limit)
    if isinstance(value, dict):
        if "role" in value:
            value = [value]
        else:
            for key in ("messages", "input", "question", "query"):
                if value.get(key):
                    return _user_text_of(value[key], limit)
            return ""
    if isinstance(value, list):
        for m in reversed(value):
            if isinstance(m, dict) and str(m.get("role") or "").lower() in ("user", "human"):
                content = m.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
                    )
                if isinstance(content, str) and content.strip():
                    return _preview(content, limit)
    return ""


def _called_names(message: dict) -> list[str]:
    """Function names of a message's tool calls — tolerant of every junk shape the wire
    delivers (a non-list, a string entry, a `function` that isn't a dict). Covers both
    conventions: OpenAI's `tool_calls[].function.name` and Anthropic's `tool_use` content
    blocks, which carry the name on the block itself."""
    names = []
    raw = message.get("tool_calls")
    for call in raw if isinstance(raw, list) else []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        name = (fn.get("name") if isinstance(fn, dict) else None) or call.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") not in ("tool_use", "tool_call"):
            continue
        name = block.get("name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _output_calls(value: Any) -> list[str]:
    """The tools a model REQUESTED, dug out of the span's stored output.

    The `tool_call_names` column is the intended source, but ingest only fills it from OTLP
    attributes it recognizes as messages — on real traces it is empty for the very shapes that
    matter (a bare `{"content": …, "tool_calls": […]}`, Anthropic `tool_use` blocks). An
    answer that IS a tool call must never render as an empty head, so parse the payload."""
    if not isinstance(value, str) or not value.lstrip().startswith(("[", "{")):
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return []
    msgs = parsed if isinstance(parsed, list) else [parsed]
    names: list[str] = []
    for m in msgs:
        if isinstance(m, dict):
            names.extend(n for n in _called_names(m) if n not in names)
    return names


def _delegation_say(span: dict, callee_alias: str) -> str:
    """The handoff text for a speech bubble. A DELEGATE span carries it as its input (`set_io`,
    a plain string); a tool-call-style handoff buries it in the function arguments of the
    assistant message. A GENERATION span's `input` is the prompt as a JSON message list — a
    bubble must NEVER show that envelope, so the direct-input shortcut only applies to plain
    strings."""
    direct = span.get("input")
    if isinstance(direct, str) and direct.strip() and not direct.lstrip().startswith(("[", "{")):
        return _text_of(direct)  # decodes a JSON-encoded scalar; plain strings pass through
    raw = span.get("output")
    if isinstance(raw, str) and raw.lstrip().startswith(("[", "{")):
        try:
            msgs = json.loads(raw)
        except (ValueError, TypeError):
            return ""
        for m in msgs if isinstance(msgs, list) else [msgs]:
            for call in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue  # free-form envelopes carry bare strings here — never crash on them
                if str(fn.get("name") or "").lower() != callee_alias.lower():
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    return _preview(fn.get("arguments"))
                if isinstance(args, dict):
                    return _preview(" ".join(str(v) for v in args.values() if v))
                return _preview(args)
    return ""


def build_replay(
    spans: list[dict],
    names: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict:
    """`{duration_ms, actors[], events[]}` — the conversation as a playable script.

    `actors` are ordered by first appearance (stable seating), led by the synthetic `customer`
    (kind `customer`) whose `ask` events carry each turn's user message. Each carries `parent` (the actor
    that spawned it, empty for top-level) and `depth`. `events` are ordered by start time with
    `t_ms` relative to the conversation's first span, so the UI plays one clock. Each event also
    carries `station` (desk/computer/library/peer) and, for handoffs, `delegate_to` + `say`.

    `aliases` maps lowercase slugs/display names -> agent_id, so a DELEGATE span's
    `callee_agent_id` (the raw slug the SDK recorded) and tool calls named after agents resolve
    to the same actor the spans use.
    """
    names = names or {}
    aliases = {k.lower(): v for k, v in (aliases or {}).items()}
    rows = [s for s in spans if s.get("start_time")]
    if not rows:
        return {"duration_ms": 0, "actors": [], "events": []}
    rows.sort(key=lambda s: (s["start_time"], s.get("span_id") or ""))
    t0 = rows[0]["start_time"]

    # Span ids are unique per TRACE, not per thread — a conversation's turns routinely reuse
    # them (any SDK numbering spans from 1 does). Keying by span_id alone made turn 2's parents
    # resolve against turn 1's spans, which silently merged two agents into one.
    def ref(span: dict, sid: Any) -> tuple[str, str]:
        return (str(span.get("trace_id") or ""), str(sid or ""))

    by_id = {ref(s, s.get("span_id")): s for s in rows if s.get("span_id")}

    # ── resolve each span's owning actor by walking up to the nearest AGENT/SUBAGENT ──
    actor_of: dict[tuple[str, str], str] = {}  # (trace, span) -> actor key
    parent_of: dict[str, str] = {}  # actor key -> parent actor key

    def actor_key(span: dict) -> str:
        # agent_id is the stable identity across turns; span_id only when a trace names no agent.
        return str(span.get("agent_id") or "") or f"{span.get('trace_id')}:{span.get('span_id')}"

    for span in rows:
        sid = str(span.get("span_id") or "")
        key_self = ref(span, sid)
        stype = str(span.get("type") or "").upper()
        # nearest ancestor that is itself an actor span
        owner = ""
        cursor = by_id.get(ref(span, span.get("parent_span_id")))
        hops = 0
        while cursor is not None and hops < 64:  # hops: cycle guard on malformed parent chains
            if str(cursor.get("type") or "").upper() in _ACTOR_TYPES:
                owner = actor_key(cursor)
                break
            cursor = by_id.get(ref(cursor, cursor.get("parent_span_id")))
            hops += 1
        if stype in _ACTOR_TYPES:
            key = actor_key(span)
            actor_of[key_self] = key
            # An actor nested under another actor is that actor's sub-agent. First parent wins:
            # a sub-agent reused across turns keeps its original owner.
            if owner and owner != key and key not in parent_of:
                parent_of[key] = owner
        else:
            # The span's OWN agent_id is authoritative — real teams tag every span with the
            # agent that ran it, and attributing by enclosing AGENT envelope collapsed a whole
            # team (supervisor + specialists) into the wrapper's single actor. The ancestor
            # walk only fills the gap for instrumentation that doesn't tag child spans.
            actor_of[key_self] = str(span.get("agent_id") or "") or owner or "agent"

    # ── actors, ordered by first appearance ──
    order: list[str] = []
    seen: dict[str, dict] = {}
    for span in rows:
        key = actor_of[ref(span, span.get("span_id"))]
        end = span.get("end_time") or span.get("start_time")
        if key not in seen:
            order.append(key)
            seen[key] = {
                "id": key,
                "name": names.get(key) or key,
                "parent": "",
                "depth": 0,
                "first_ms": _ms(t0, span["start_time"]),
                "last_ms": _ms(t0, end),
                "events": 0,
                "errors": 0,
            }
        seen[key]["last_ms"] = max(seen[key]["last_ms"], _ms(t0, end))

    # Aliases every actor answers to: registry slug/display (from `aliases`), replay display
    # name, and the raw key. Used for both hierarchy edges and per-event delegation targets.
    by_name: dict[str, str] = {}
    for alias, aid in aliases.items():
        if aid in seen:
            by_name[alias] = aid
    for key, actor in seen.items():
        by_name.setdefault(str(actor["name"]).lower(), key)
        by_name.setdefault(key.lower(), key)

    def resolve_actor(alias: str) -> str:
        return by_name.get(alias.lower(), "")

    # ── hierarchy edges beyond nesting ──
    # 1) A DELEGATE span is an EXPLICIT edge: its actor is the caller, `callee_agent_id` the
    #    callee. Recorded first so it wins over the tool-name heuristic below.
    for span in rows:
        if str(span.get("type") or "").upper() != "DELEGATE":
            continue
        caller = actor_of[ref(span, span.get("span_id"))]
        # explicit callee id first; else the span NAME — harnesses name the delegate span
        # after the specialist it calls ("balance", "store_locations")
        callee = resolve_actor(str(span.get("callee_agent_id") or "")) or resolve_actor(
            str(span.get("name") or "")
        )
        if callee and callee != caller and callee not in parent_of:
            parent_of[callee] = caller
    # 2) Multi-agent harnesses call a sub-agent as a TOOL, and often emit spans whose real parent
    #    span was never exported — so nesting alone loses the hierarchy. A tool call NAMED after
    #    another actor is an explicit, non-heuristic edge: the caller owns the callee.
    for span in rows:
        caller = actor_of[ref(span, span.get("span_id"))]
        for tool in span.get("tool_call_names") or []:
            callee = resolve_actor(str(tool))
            if callee and callee != caller and not parent_of.get(callee):
                parent_of[callee] = caller

    for key, actor in seen.items():
        parent = parent_of.get(key, "")
        # never let an edge point back into the callee's own subtree (would spin the depth walk)
        cursor, hops = parent, 0
        while cursor and hops < 16:
            if cursor == key:
                parent = ""
                break
            cursor = parent_of.get(cursor, "")
            hops += 1
        actor["parent"] = parent if parent in seen else ""
        depth, cursor, hops = 0, actor["parent"], 0
        while cursor and hops < 16:
            depth += 1
            cursor = seen.get(cursor, {}).get("parent", "")
            hops += 1
        actor["depth"] = depth
        actor["kind"] = "subagent" if depth else "agent"

    # Explicit DELEGATE callees per trace: an llm span requesting the same specialist in the
    # same trace is the SAME handoff — inferring a second walk for it doubles the scene.
    explicit_delegations: set[tuple[str, str]] = set()
    for span in rows:
        if str(span.get("type") or "").upper() != "DELEGATE":
            continue
        callee_ref = str(span.get("callee_agent_id") or "") or str(span.get("name") or "")
        target = resolve_actor(callee_ref)
        if target:
            explicit_delegations.add((str(span.get("trace_id") or ""), target))

    # ── events ──
    events: list[dict] = []
    for span in rows:
        sid = str(span.get("span_id") or "")
        stype = str(span.get("type") or "").upper()
        kind = _KIND.get(stype, "step")
        key = actor_of[ref(span, sid)]
        error = str(span.get("level") or "").upper() == "ERROR"
        seen[key]["events"] += 1
        if error:
            seen[key]["errors"] += 1

        name = str(span.get("name") or kind)
        station = "library" if stype == "RETRIEVER" else _STATION.get(kind, "desk")

        # Delegation target: explicit on a DELEGATE span; inferred on a WORK event (never a
        # turn/spawn container — one inferred edge would walk the agent for the whole turn)
        # whose REQUESTED tool is another actor. A TOOL span's own name deliberately does not
        # count: agent slugs and tool names share a namespace ("search", "billing"), and a
        # colliding plain tool must stay a tool. The residual risk — an llm requesting a tool
        # that happens to share an agent's slug — is accepted; it's the same signal the agents
        # panel uses for hierarchy.
        delegate_to, say = "", ""
        if stype == "DELEGATE":
            callee_ref = str(span.get("callee_agent_id") or "") or str(span.get("name") or "")
            delegate_to = resolve_actor(callee_ref)
            say = _delegation_say(span, callee_ref)
        elif kind not in ("turn", "spawn"):
            for alias in span.get("tool_call_names") or []:
                target = resolve_actor(str(alias))
                if (
                    target
                    and target != key
                    and (str(span.get("trace_id") or ""), target) not in explicit_delegations
                ):
                    delegate_to, station = target, "peer"
                    say = _delegation_say(span, str(alias))
                    break

        # A model turn that ANSWERED with a tool call instead of words has no sayable output —
        # the bubble came up empty. Say what it reached for instead: the indexed names when
        # ingest captured them, else the ones in the payload \u2014 on real traces it usually did
        # NOT capture them (see `_output_calls`). `calls` travels as its own field so the UI
        # can render a "called X" chip instead of parsing this line back out of `detail`.
        called = [str(t) for t in (span.get("tool_call_names") or []) if t] or _output_calls(
            span.get("output")
        )
        detail = _preview(span.get("status_message")) or _text_of(span.get("output"))
        if not detail and called:
            detail = _preview("\u2192 " + ", ".join(called))

        events.append(
            {
                "t_ms": _ms(t0, span["start_time"]),
                "dur_ms": _ms(span["start_time"], span.get("end_time")),
                "actor": key,
                "kind": kind,
                "name": name,
                "status": "error" if error else "ok",
                # Containers bracket other spans rather than doing work — the UI shows them as the
                # turn's envelope, and never as the agent "working".
                "container": kind in ("turn", "spawn", "delegate"),
                "station": station,
                "delegate_to": delegate_to,
                "say": say,
                "model": str(span.get("model_id") or ""),
                "detail": detail,
                "calls": called,
                "span_id": sid,
                "trace_id": str(span.get("trace_id") or ""),
                "turn_id": str(span.get("turn_id") or ""),
                "tokens": int(span.get("tokens") or 0),
                "cost": float(span.get("cost") or 0.0),
            }
        )

    # ── the customer: one `ask` per turn (trace), spoken at the turn's first span ──
    # The question is the root span's input (manual SDK: a plain string; frameworks: the
    # normalized message list whose LAST user message is this turn's ask). A framework root
    # that carries a config payload instead falls back to the earliest GENERATION's prompt —
    # same preference order as `session_turns`.
    turns: dict[str, list[dict]] = {}
    for span in rows:
        turns.setdefault(str(span.get("trace_id") or ""), []).append(span)
    asks: list[dict] = []
    for tid, tspans in turns.items():
        roots = [s for s in tspans if ref(s, s.get("parent_span_id")) not in by_id]
        gens = [s for s in tspans if str(s.get("type") or "").upper() == "GENERATION"]
        ask = next((t for s in roots + gens + tspans if (t := _user_text_of(s.get("input")))), "")
        asks.append(
            {
                "t_ms": _ms(t0, tspans[0]["start_time"]),
                "dur_ms": 0,
                "actor": CUSTOMER,
                "kind": "ask",
                "name": "asks",
                "status": "ok",
                "container": False,
                "station": "door",
                "delegate_to": "",
                "say": "",
                "model": "",
                "detail": ask,
                "calls": [],
                "span_id": str(tspans[0].get("span_id") or ""),
                "trace_id": tid,
                "turn_id": str(tspans[0].get("turn_id") or ""),
            }
        )
    events = sorted(asks + events, key=lambda e: e["t_ms"])  # stable: an ask leads its turn
    customer = {
        "id": CUSTOMER,
        "name": "customer",
        "kind": "customer",
        "parent": "",
        "depth": 0,
        "first_ms": asks[0]["t_ms"],
        "last_ms": asks[-1]["t_ms"],
        "events": len(asks),
        "errors": 0,
    }

    duration = max((e["t_ms"] + e["dur_ms"]) for e in events) if events else 0
    return {
        "duration_ms": duration,
        "actors": [customer, *(seen[k] for k in order)],
        "events": events,
    }
