"""`@VARIABLE` template resolution for Advanced-mode LLM-judge evaluators.

A basic judge gets its context **auto-injected** (request / answer / tool results / transcript /
step I/O). Advanced mode hands that control to the user: they write the rubric with `@VARIABLE`
placeholders that are resolved against the real trace/thread data at run time, deciding exactly
what the judge reads.

Three pieces:
- `TEMPLATE_VARIABLES` — the catalog (UI / autocomplete metadata): name, description, type,
  applicable levels, nested object props. Mirrored in `frontend/app/lib/templateVariables.ts`.
- `build_context(...)` — turns already-fetched span dicts into an `EvaluationContext` for a level.
  **Pure** (no I/O), and materializes ONLY the referenced vars (`wanted_vars`) so an unused
  `@HISTORY`/`@AGENTS` costs nothing in the grading hot path.
- `TemplateResolver.resolve(...)` — substitutes every `@NAME` / `@NAME.prop` match; a value that
  isn't present becomes the literal `[No <REF> available]` (soft miss — never an error, never
  blocks a grade).

The same builder + resolver power both the run path (`LLMJudgeEvaluator`, sync spans) and the
preview endpoint (async spans), so "what you preview" matches "what runs".

Level / type constants are duplicated here as plain strings (mirroring
`evaluators/base.py`) rather than imported, to avoid triggering the `evaluators` package import
(which registers the judge, which imports this module).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tracely.domain.evaluation.text import agent_answer, content_text, readable_io, user_message
from tracely.domain.traces.spans import root_span

# Mirrors evaluators/base.py + the ingest vocabulary (otel/types.py). Local copies dodge an
# import cycle (base → registers the judge → imports this module).
CONVERSATION = "CONVERSATION"
RUN = "AGENT_RUN"
SPAN = "SPAN"
TOOL = "TOOL"
GENERATION = "GENERATION"
CHAIN = "CHAIN"
THINKING = "THINKING"

# Catalog levels (coarser than evaluator levels): every step-flavored evaluator level maps to
# "step", AGENT_RUN to "message", CONVERSATION to "conversation".
CL_CONVERSATION = "conversation"
CL_MESSAGE = "message"
CL_STEP = "step"
_ALL = (CL_CONVERSATION, CL_MESSAGE, CL_STEP)

# Group 1 = UPPERCASE variable name, group 2 = optional lowercase `.property`. The SAME regex is
# used to extract (here + the frontend) and to resolve. Case matters: `email@x.com` / lowercase
# `@foo` are NOT variables.
VARIABLE_RE = re.compile(r"@([A-Z_]+)(?:\.([a-z_]+))?")



# ── catalog (UI metadata; not on the grading hot path) ───────────────────────


@dataclass(frozen=True)
class TemplateVariable:
    name: str
    description: str
    type: str  # "string" | "object"
    levels: tuple[str, ...]  # catalog levels that expose it
    props: tuple[tuple[str, str], ...] = ()  # (prop, description) for object vars
    sequential_only: bool = False  # only meaningful when execution_mode == "sequential"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "levels": list(self.levels),
            "props": [{"name": n, "description": d} for n, d in self.props],
            "sequential_only": self.sequential_only,
        }


# `@CURRENT_STEPS.<type>` — the turn's steps of one type. The resolver matches ANY lowercase prop
# against the span's `type`, so this tuple is autocomplete metadata, not a whitelist; it lists the
# types worth grading against (the ingest vocabulary is `otel/types.py`).
_STEP_TYPE_PROPS = (
    ("tool", "Only the TOOL steps — each call with its arguments and result"),
    ("retriever", "Only the RETRIEVER steps — the retrieved context"),
    ("generation", "Only the GENERATION steps — the model calls"),
    ("thinking", "Only the THINKING steps — the agent's reasoning"),
    ("chain", "Only the CHAIN steps"),
    ("skill", "Only the SKILL steps — the named capabilities the agent ran"),
    ("delegate", "Only the DELEGATE steps — handovers to another agent"),
)
_STEP_PROPS = (
    ("tool_call", "The tool invocation (name + arguments) at this step"),
    ("tool_result", "The tool's returned result"),
    ("thinking", "The step's reasoning/thinking text (THINKING steps)"),
    ("output_content", "The readable text output of the step"),
    ("output_structured", "The raw structured (JSON) output of the step"),
    ("input", "The step's readable input (tool arguments, a model call's messages, a query, a task)"),
    ("error", "The step's error message, when it failed"),
)

TEMPLATE_VARIABLES: tuple[TemplateVariable, ...] = (
    # common (all levels)
    TemplateVariable("HISTORY", "Full formatted conversation history", "string", _ALL),
    TemplateVariable(
        "ROLLING_SUMMARY",
        "Accumulated rolling summary of the conversation so far (compact, prefix-stable); "
        "empty when no summary has been generated for this thread",
        "string", _ALL,
    ),
    TemplateVariable("GOAL", "User's overall goal/intent (first request in the thread)", "string", _ALL),
    TemplateVariable(
        "AGENTS",
        "The agents of this conversation, each with its description and tools — declared, offered "
        "to the model, or only seen being called",
        "object", _ALL,
        props=(
            ("tools", "Every tool the agents had, with where it came from and how often it was called"),
            ("called", "Only the tools that were called, with how many times"),
        ),
    ),
    TemplateVariable(
        "DEPENDENCIES",
        "This item's results from the columns in Depends On — label, value, verdict and reason each",
        "string", _ALL,
    ),
    # conversation only
    TemplateVariable("MESSAGES", "All turns formatted ([role]: text)", "string", (CL_CONVERSATION,)),
    TemplateVariable("USER_MESSAGES", "All user requests only", "string", (CL_CONVERSATION,)),
    TemplateVariable("ASSISTANT_MESSAGES", "All assistant answers only", "string", (CL_CONVERSATION,)),
    TemplateVariable("FIRST_USER_MSG", "The first user request", "string", (CL_CONVERSATION,)),
    TemplateVariable("LAST_USER_MSG", "The last user request", "string", (CL_CONVERSATION,)),
    TemplateVariable("LAST_ASSISTANT_MSG", "The last assistant answer", "string", (CL_CONVERSATION,)),
    # message level (step inherits)
    TemplateVariable("PREVIOUS_USER_MSG", "The previous turn's user request", "string", (CL_MESSAGE, CL_STEP)),
    TemplateVariable("PREVIOUS_ASSISTANT_MSG", "The previous turn's assistant answer", "string", (CL_MESSAGE, CL_STEP)),
    TemplateVariable(
        "CURRENT_MESSAGE", "The turn under evaluation", "object", (CL_MESSAGE, CL_STEP),
        props=(("input", "The user request"), ("output", "The assistant answer"), ("role", "Always 'assistant'")),
    ),
    TemplateVariable(
        "CURRENT_STEPS",
        "All steps of the current turn, formatted — `.tool` / `.retriever` / … narrow it to one "
        "step type, which is how a message-level rubric asks for the evidence it grades against",
        "object", (CL_MESSAGE, CL_STEP), props=_STEP_TYPE_PROPS,
    ),
    TemplateVariable("CURRENT_STEPS_COUNT", "Number of steps in the current turn", "string", (CL_MESSAGE, CL_STEP)),
    # step only
    TemplateVariable("PREVIOUS_STEP", "The previous step in this turn", "object", (CL_STEP,), props=_STEP_PROPS),
    TemplateVariable("CURRENT_STEP", "The step under evaluation", "object", (CL_STEP,), props=_STEP_PROPS),
    TemplateVariable("STEP_NUMBER", "1-indexed position of the current step", "string", (CL_STEP,)),
    # sequential mode only (message + step)
    TemplateVariable(
        "METRIC_PREVIOUS_RESULT", "The previous item's result of this metric (sequential mode)",
        "string", (CL_MESSAGE, CL_STEP), sequential_only=True,
    ),
)

_BY_NAME = {v.name: v for v in TEMPLATE_VARIABLES}


def catalog_level(level: str) -> str:
    """Map an evaluator level (CONVERSATION / AGENT_RUN / SPAN / TOOL / …) to a catalog level."""
    upper = (level or "").upper()
    if upper == CONVERSATION:
        return CL_CONVERSATION
    if upper in (RUN, "RUN", "MESSAGE"):
        return CL_MESSAGE
    return CL_STEP


def variables_for_level(level: str) -> list[TemplateVariable]:
    cl = catalog_level(level)
    return [v for v in TEMPLATE_VARIABLES if cl in v.levels]


def variables_for_level_json(level: str) -> list[dict[str, Any]]:
    """The level's variables as JSON — drives the discovery endpoint + the editor's count."""
    return [v.to_json() for v in variables_for_level(level)]


def extract_template_variables(prompt: str) -> list[str]:
    """De-duplicated list of refs used in `prompt` (e.g. ['HISTORY', 'CURRENT_STEP.tool_call'])."""
    out: list[str] = []
    for m in VARIABLE_RE.finditer(prompt or ""):
        ref = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
        if ref not in out:
            out.append(ref)
    return out


def _base_names(refs: list[str] | None) -> set[str] | None:
    """The bare variable names (drop `.prop`) referenced — drives lazy context materialization."""
    if refs is None:
        return None
    return {r.split(".", 1)[0] for r in refs}


# ── resolution context (one bundle per evaluated item) ───────────────────────


@dataclass
class EvaluationContext:
    """Everything a template might reference for ONE evaluated item. A `None` field is treated as
    "not present" → the resolver renders `[No <REF> available]`. Built by `build_context`."""

    history: str | None = None
    rolling_summary: str | None = None
    goal: str | None = None
    agents: list[AgentView] | None = None  # the conversation up to the graded item
    messages: str | None = None
    user_messages: str | None = None
    assistant_messages: str | None = None
    first_user_msg: str | None = None
    last_user_msg: str | None = None
    last_assistant_msg: str | None = None
    previous_user_msg: str | None = None
    previous_assistant_msg: str | None = None
    current_message: dict[str, Any] | None = None  # {input, output, role}
    current_steps: str | None = None
    current_step_spans: list[dict[str, Any]] = field(default_factory=list)
    root_agent: str = ""  # the turn's top-level agent — steps of any other agent are attributed
    current_steps_count: str | None = None
    previous_step: dict[str, Any] | None = None  # a span dict
    current_step: dict[str, Any] | None = None  # a span dict
    step_number: str | None = None
    metric_previous_result: dict[str, Any] | None = None
    dependencies: dict[str, Any] | None = None  # {score_name: payload}, from `depends_on`


@dataclass
class ResolvedTemplate:
    resolved_text: str
    variables_used: list[str] = field(default_factory=list)
    variables_missing: list[str] = field(default_factory=list)


# ── span / turn formatters ───────────────────────────────────────────────────


def _turn_io(spans: list[dict]) -> tuple[str, str]:
    """A turn's (user message, agent answer): its root span's input and output (`text.py`)."""
    root = root_span(spans)
    return user_message(root), agent_answer(root)


def _group_turns(thread_spans: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group spans into turns by `trace_id`, preserving first-seen order (spans arrive ordered by
    start_time, so the first time a trace_id appears is its position in the thread)."""
    by_trace: dict[str, list[dict]] = {}
    order: list[str] = []
    for s in thread_spans:
        tid = s.get("trace_id") or ""
        if tid not in by_trace:
            by_trace[tid] = []
            order.append(tid)
        by_trace[tid].append(s)
    return [(tid, by_trace[tid]) for tid in order]


# ── @AGENTS: the conversation's agents and their tools ──────────────────────
# A tool's definition comes from one of three places, best first: the catalog the SDK DECLARED
# for the conversation (`tracely.trace(agents=[...])`), the tool list an instrumented model call
# was OFFERED (OpenInference `llm.tools.N.tool.json_schema`, OTel `gen_ai.tool.definitions`), or
# the TOOL span of a call (`tool.description`). A tool nothing describes is known only because it
# was called. Each tool carries where it came from: "was the right tool available?" is a question
# about what was on offer, and a declared catalog is often missing tools the agent really has.

_DECLARED, _OFFERED, _CALLED = "declared", "offered", "called"
_SOURCE_RANK = {_DECLARED: 0, _OFFERED: 1, _CALLED: 2}


@dataclass
class ToolView:
    name: str
    description: str = ""
    source: str = _CALLED
    ran: int = 0  # TOOL spans that executed it
    requested: int = 0  # times a model call asked for it (`tool_call_names`)

    @property
    def calls(self) -> int:
        # Executions when the tools are instrumented; the model's requests when they aren't
        # (a requested call that also ran would otherwise count twice).
        return self.ran or self.requested


@dataclass
class AgentView:
    name: str
    description: str = ""
    tools: dict[str, ToolView] = field(default_factory=dict)

    def tool(self, name: str, description: str = "", source: str = _CALLED) -> ToolView:
        t = self.tools.get(name)
        if t is None:
            t = self.tools[name] = ToolView(name, description, source)
        elif _SOURCE_RANK[source] < _SOURCE_RANK[t.source]:
            t.source, t.description = source, description or t.description
        elif not t.description:
            t.description = description
        return t


def _span_meta(span: dict) -> dict:
    meta = span.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def _tool_defs(raw: Any) -> list[tuple[str, str]]:
    """(name, description) of every tool in one tool-definition attribute — a single OpenAI-style
    `{"type": "function", "function": {...}}`, a flat `{name, description}`, or a list of them."""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    for item in obj if isinstance(obj, list) else [obj]:
        if not isinstance(item, dict):
            continue
        fn = item["function"] if isinstance(item.get("function"), dict) else item
        if fn.get("name"):
            out.append((str(fn["name"]), str(fn.get("description") or "")))
    return out


def _offered_tools(meta: dict) -> list[tuple[str, str]]:
    """The tools a model call was offered, in the order it was offered them."""

    def index(key: str) -> int:
        part = key.split(".")[2]
        return int(part) if part.isdigit() else 0

    keys = sorted(
        (k for k in meta if k.startswith("llm.tools.") and k.endswith(".tool.json_schema")), key=index
    )
    out = [d for k in keys for d in _tool_defs(meta[k])]
    if "gen_ai.tool.definitions" in meta:
        out.extend(_tool_defs(meta["gen_ai.tool.definitions"]))
    return out


def collect_agents(spans: list[dict], declared: list[dict] | None = None) -> list[AgentView]:
    """The agents of `spans` with every tool they had, merged from the declared catalog, the tools
    their model calls were offered and the tools they called.

    A span belongs to the nearest ancestor (itself included) named like a declared agent: graph
    frameworks stamp one `agent_id` on everything and show a sub-agent only as the node its calls
    run under (LangGraph's `lg-support` CHAIN). Failing that, to its `agent_id`'s agent, which is
    the same agent as a declared one of that name; with no agent id, to the only declared agent,
    or to an unnamed one."""
    agents: dict[str, AgentView] = {}
    for ag in declared or []:
        if not isinstance(ag, dict):
            continue
        view = AgentView(str(ag.get("name") or "agent"), str(ag.get("description") or ""))
        agents.setdefault(view.name.casefold(), view)
        raw = ag.get("tools")
        items = (
            list(raw.items())
            if isinstance(raw, dict)
            else [(t.get("name", ""), t) for t in raw if isinstance(t, dict)]
            if isinstance(raw, list)
            else []
        )
        for key, tdef in items:
            tdef = tdef if isinstance(tdef, dict) else {}
            if name := str(tdef.get("name") or key or ""):
                view.tool(name, str(tdef.get("description") or ""), _DECLARED)

    names = _agent_names(spans)
    only_declared = next(iter(agents.values())) if len(agents) == 1 else None
    by_id = {s.get("span_id"): s for s in spans}

    def declared_ancestor(span: dict) -> AgentView | None:
        seen: set = set()
        node: dict | None = span
        while node is not None and node.get("span_id") not in seen:
            seen.add(node.get("span_id"))
            if (hit := agents.get(str(node.get("name") or "").casefold())) is not None:
                return hit
            node = by_id.get(node.get("parent_span_id"))
        return None

    def agent_of(span: dict) -> AgentView:
        if agents and (hit := declared_ancestor(span)) is not None:
            return hit
        aid = str(span.get("agent_id") or "")
        if not aid and only_declared:
            return only_declared
        name = names.get(aid) or aid or "agent"
        for key in (name.casefold(), aid.casefold()):
            if key and key in agents:
                return agents[key]
        return agents.setdefault(name.casefold(), AgentView(name))

    for s in spans:
        stype = s.get("type")
        meta = _span_meta(s)
        offered = _offered_tools(meta) if stype == GENERATION else []
        requested = [str(n) for n in (s.get("tool_call_names") or []) if n] if stype != TOOL else []
        ran = str(meta.get("tool.name") or s.get("name") or "") if stype == TOOL else ""
        if not (offered or requested or ran or stype == "AGENT"):
            continue
        agent = agent_of(s)
        for name, desc in offered:
            agent.tool(name, desc, _OFFERED)
        for name in requested:
            agent.tool(name).requested += 1
        if ran:
            agent.tool(ran, str(meta.get("tool.description") or "")).ran += 1
    return list(agents.values())


def _tool_line(t: ToolView, prefix: str = "") -> str:
    head = f"{prefix}{t.name}" + (f" — {t.description}" if t.description else "")
    if t.source == _CALLED:
        return f"{head} [called {t.calls}×, no definition]"
    return f"{head} [{t.source} · " + (f"called {t.calls}×]" if t.calls else "not called]")


def format_agents(agents: list[AgentView], prop: str | None = None) -> str | None:
    """`@AGENTS` (each agent, its description and its tools), `.tools` (every tool) or `.called`
    (only the tools that were used, with counts). Names and descriptions only: argument names
    answer "how would I call this?", which is not a question a judge asks, and they were most of
    the catalog's length — competing for room with the transcript it is supposed to help grade."""
    if prop is None:
        lines: list[str] = []
        for a in agents:
            lines.append(f"- {a.name}" + (f": {a.description}" if a.description else ""))
            lines.extend(f"    • {_tool_line(t)}" for t in a.tools.values())
        return "\n".join(lines) or None
    multi = sum(1 for a in agents if a.tools) > 1
    if prop == "tools":
        lines = [
            _tool_line(t, f"{a.name} / " if multi else "") for a in agents for t in a.tools.values()
        ]
    elif prop == "called":
        lines = [
            f"{a.name + ' / ' if multi else ''}{t.name} ×{t.calls}"
            for a in agents
            for t in a.tools.values()
            if t.calls
        ]
    else:
        return None
    return "\n".join(f"- {line}" for line in lines) or None


def _step_error(span: dict) -> str | None:
    """A failed step's error. `status_message` is where every exporter puts it; a step can be
    ERROR-level with no message, and that still has to read as a failure."""
    msg = str(span.get("status_message") or "").strip()
    if msg:
        return msg
    return "(failed, no error message)" if str(span.get("level") or "").upper() == "ERROR" else None


def _requested_tools(span: dict) -> str | None:
    """The tool calls a model step REQUESTED (a GENERATION's `tool_calls`) — not a TOOL span,
    whose `tool_call_names` is just its own name and says nothing its header doesn't."""
    tcs = span.get("tool_calls")
    if tcs:
        return json.dumps(tcs, ensure_ascii=False, indent=2)
    names = [str(n) for n in (span.get("tool_call_names") or []) if n]
    return ", ".join(names) or None


def _resolve_step_field(span: dict, prop: str) -> str | None:
    """One property of a step span. Soft-miss (None) when the field doesn't apply to this span."""
    stype = span.get("type")
    if prop == "tool_call":
        if stype == TOOL:
            # A TOOL span's call IS its name + arguments. Its `tool_call_names` is only its own name,
            # and preferring that used to replace the arguments with the bare tool name — so every
            # judge was shown `run_pricing_model` and never what it was called with.
            args = readable_io(span.get("input"))
            name = span.get("name") or ""
            return f"{name}({args})" if args else name or None
        return _requested_tools(span)
    if prop == "tool_result":
        if stype == TOOL:
            return readable_io(span.get("output")) or _step_error(span)
        return None
    if prop == "thinking":
        if stype == THINKING:
            txt = content_text(span.get("output")) or content_text(span.get("input"))
            return txt or None
        return None
    if prop == "input":
        return readable_io(span.get("input")) or None
    if prop == "error":
        return _step_error(span)
    if prop == "output_content":
        return content_text(span.get("output")) or None
    if prop == "output_structured":
        return _structured(span.get("output"))
    return None  # unknown property → soft miss


def _structured(out: Any) -> str | None:
    """The step output as pretty JSON when it's structured; soft-miss otherwise."""
    if isinstance(out, (dict, list)):
        return json.dumps(out, ensure_ascii=False, indent=2)
    if isinstance(out, str):
        s = out.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
            except ValueError:
                return None
    return None


# How each step type's input and output read to a judge. Every step shows BOTH — the input is
# half of what happened (the arguments a tool got, the query a retriever ran, the task a
# sub-agent was handed, the messages a model was called with), and it used to be dropped for
# everything but TOOL. Types not listed use the generic pair.
_IO_LABELS: dict[str, tuple[str, str]] = {
    TOOL: ("Arguments", "Result"),
    GENERATION: ("Prompt", "Response"),
    "RETRIEVER": ("Query", "Retrieved"),
    "DELEGATE": ("Task", "Returned"),
    "AGENT": ("Input", "Output"),
    "GUARDRAIL": ("Checked", "Verdict"),
    "SKILL": ("Input", "Output"),
}


def _format_step(span: dict, root_agent: str = "", agent_names: dict[str, str] | None = None) -> str | None:
    """Everything one step did, for a judge: what it was, whose it was, what went in, what came
    out, and whether it failed. Each field once (THINKING and TOOL output used to be printed twice
    under two labels). Nothing is truncated."""
    stype = str(span.get("type") or "STEP")
    head = f"{stype}"
    if span.get("name"):
        head += f" `{span['name']}`"
    agent = str(span.get("agent_id") or "")
    if agent and agent != root_agent and stype != "AGENT":  # an AGENT span's header names it already
        head += f" (agent: {(agent_names or {}).get(agent) or agent})"
    lines = [head]
    if stype == THINKING:
        thought = _resolve_step_field(span, "thinking")
        if thought:
            lines.append(f"Thinking: {thought}")
    else:
        in_label, out_label = _IO_LABELS.get(stype, ("Input", "Output"))
        inp = _resolve_step_field(span, "input")
        if inp:
            lines.append(f"{in_label}: {inp}")
        out = readable_io(span.get("output"))
        if out:
            lines.append(f"{out_label}: {out}")
        if stype != TOOL:
            requested = _requested_tools(span)
            if requested:
                lines.append(f"Requested tool calls: {requested}")
    err = _step_error(span)
    if err:
        lines.append(f"ERROR: {err}")
    return "\n".join(lines)


def _agent_names(spans: list[dict]) -> dict[str, str]:
    """`agent_id → name`: ids are opaque (often UUIDs), and "a step of 6a10a2c9-…" tells a judge
    nothing that "a step of market-analyst" does. From the turn's AGENT spans, else from a
    DELEGATE span's name for the agent running underneath it (frameworks that emit no AGENT span
    for a sub-agent — `agent_faq` handing work to its own generations and tools)."""
    names: dict[str, str] = {}
    by_id = {s.get("span_id"): s for s in spans}
    for s in spans:
        aid, name = s.get("agent_id"), s.get("name")
        if s.get("type") == "AGENT" and aid and name:
            names[str(aid)] = str(name)
    for s in spans:
        aid = str(s.get("agent_id") or "")
        parent = by_id.get(s.get("parent_span_id"))
        if aid and aid not in names and parent and parent.get("type") == "DELEGATE" and parent.get("name"):
            if str(parent.get("agent_id") or "") != aid:
                names[aid] = str(parent["name"]).removeprefix("delegate:")
    return names


def _format_steps(
    spans: list[dict], root_agent: str = "", names: dict[str, str] | None = None
) -> str | None:
    if not spans:
        return None
    # names come from the WHOLE turn: a `.tool` filter drops the AGENT/DELEGATE spans they're read from
    names = _agent_names(spans) if names is None else names
    blocks = []
    for i, s in enumerate(spans, start=1):
        body = _format_step(s, root_agent, names) or ""
        blocks.append(f"Step {i} · {body}")
    return "\n\n".join(blocks)


def _resolve_message_field(msg: dict, prop: str) -> str | None:
    if prop == "role":
        return msg.get("role") or "assistant"
    val = msg.get(prop)
    return val if val else None


def _format_message(msg: dict) -> str | None:
    parts = []
    if msg.get("input"):
        parts.append(f"User: {msg['input']}")
    if msg.get("output"):
        parts.append(f"Assistant: {msg['output']}")
    return "\n".join(parts) or None


# ── context builder (pure) ───────────────────────────────────────────────────


_STRING_ATTRS = {
    "HISTORY": "history", "ROLLING_SUMMARY": "rolling_summary",
    "GOAL": "goal", "MESSAGES": "messages",
    "USER_MESSAGES": "user_messages", "ASSISTANT_MESSAGES": "assistant_messages",
    "FIRST_USER_MSG": "first_user_msg", "LAST_USER_MSG": "last_user_msg",
    "LAST_ASSISTANT_MSG": "last_assistant_msg", "PREVIOUS_USER_MSG": "previous_user_msg",
    "PREVIOUS_ASSISTANT_MSG": "previous_assistant_msg", "CURRENT_STEPS": "current_steps",
    "CURRENT_STEPS_COUNT": "current_steps_count", "STEP_NUMBER": "step_number",
}
_OBJECT_ATTRS = {"CURRENT_MESSAGE": "current_message", "CURRENT_STEP": "current_step", "PREVIOUS_STEP": "previous_step"}

# Vars that need the WHOLE thread (used to gate the extra thread-spans read in the service).
CONVERSATION_SCOPED_VARS = frozenset({
    "HISTORY", "MESSAGES", "USER_MESSAGES", "ASSISTANT_MESSAGES", "FIRST_USER_MSG",
    "LAST_USER_MSG", "LAST_ASSISTANT_MSG", "GOAL", "AGENTS",
    "PREVIOUS_USER_MSG", "PREVIOUS_ASSISTANT_MSG",
})


def references_conversation_scope(refs: list[str] | None) -> bool:
    """True when any ref needs cross-trace data — the service uses this to decide whether to
    fetch the full thread for an advanced trace/step eval."""
    names = _base_names(refs)
    return bool(names and names & CONVERSATION_SCOPED_VARS)


def build_context(
    level: str,
    *,
    thread_spans: list[dict],
    current_trace_id: str = "",
    current_span_id: str | None = None,
    metric_previous_result: dict | None = None,
    wanted_vars: list[str] | None = None,
    history_override: str | None = None,
    declared_agents: list[dict] | None = None,
    dependencies: dict[str, Any] | None = None,
) -> EvaluationContext:
    """Build the `EvaluationContext` for ONE evaluated item from already-fetched span dicts.

    `thread_spans` is the full conversation when conversation-scoped vars are referenced, else
    just the current trace's spans (the conversation collapses to one turn — fine, since the
    cross-trace vars aren't materialized). `wanted_vars` (bare names) restricts materialization to
    referenced variables; `None` materializes everything applicable.

    At message/step level `@HISTORY`/`@MESSAGES` end at the current turn (never later turns); the
    caller passes a `history_override` cut at the same point.

    `history_override` (optional): the thread's rolling summary, for `@ROLLING_SUMMARY` only.
    `@HISTORY`/`@MESSAGES` are always the recorded conversation.
    """
    ctx = EvaluationContext(metric_previous_result=metric_previous_result, dependencies=dependencies or None)
    want = _base_names(wanted_vars)

    def need(name: str) -> bool:
        return want is None or name in want

    # The rolling summary is its own explicit variable (soft-miss when absent), passed in (own table).
    if need("ROLLING_SUMMARY") and history_override:
        ctx.rolling_summary = history_override

    turns = _group_turns(thread_spans)
    if not turns:
        return ctx
    ios = [(_turn_io(spans)) for _, spans in turns]  # [(user, answer), ...]
    cl = catalog_level(level)
    # The turn being graded (message/step levels). History-shaped variables stop at it: a thread is
    # usually graded after it settles, when its later turns already exist, and a judge grading turn
    # 2 must not read how turns 3-10 went — it would grade the message with hindsight the agent
    # never had (a "did the user have to re-ask?" check finds the re-ask in the future turn).
    cur_idx = next((i for i, (tid, _) in enumerate(turns) if tid == current_trace_id), len(turns) - 1)
    upto = ios if cl == CL_CONVERSATION else ios[: cur_idx + 1]

    # @HISTORY / @MESSAGES are the conversation as recorded — always. The rolling summary is its
    # own variable (@ROLLING_SUMMARY); it used to stand in for @HISTORY silently whenever one
    # existed, so a column asking for the history got a compressed paraphrase without any sign.
    if need("HISTORY") or need("MESSAGES"):
        lines: list[str] = []
        for user, answer in upto:
            if user:
                lines.append(f"[user]: {user}")
            if answer:
                lines.append(f"[assistant]: {answer}")
        transcript = "\n".join(lines)
        ctx.history = transcript or None
        ctx.messages = transcript or None
    if need("USER_MESSAGES"):
        ctx.user_messages = "\n".join(f"[user]: {u}" for u, _ in ios if u) or None
    if need("ASSISTANT_MESSAGES"):
        ctx.assistant_messages = "\n".join(f"[assistant]: {a}" for _, a in ios if a) or None
    users = [u for u, _ in ios if u]
    answers = [a for _, a in ios if a]
    if need("FIRST_USER_MSG") or need("GOAL"):
        first_user = users[0] if users else None
        ctx.first_user_msg = first_user
        ctx.goal = first_user  # best-effort: the initial request is the user's goal/intent
    if need("LAST_USER_MSG"):
        ctx.last_user_msg = users[-1] if users else None
    if need("LAST_ASSISTANT_MSG"):
        ctx.last_assistant_msg = answers[-1] if answers else None
    if need("AGENTS"):
        # Like @HISTORY, stops at the graded turn: a tool first called in turn 5 is hindsight to a
        # judge grading turn 2.
        seen = thread_spans if cl == CL_CONVERSATION else [s for _, t in turns[: cur_idx + 1] for s in t]
        ctx.agents = collect_agents(seen, declared_agents) or None

    if cl == CL_CONVERSATION:
        return ctx

    # message / step: the current turn (by trace_id; the last turn when it isn't found)
    cur_spans = turns[cur_idx][1]
    cur_user, cur_answer = ios[cur_idx]
    # "steps" = the turn's spans minus the agent-root wrapper (so the first real step is step 1),
    # falling back to all spans for a single-span (root-is-the-step) trace.
    cur_root = root_span(cur_spans)
    step_spans = [s for s in cur_spans if s.get("span_id") != cur_root.get("span_id")] or cur_spans
    if need("CURRENT_MESSAGE"):
        ctx.current_message = {"input": cur_user, "output": cur_answer, "role": "assistant"}
    ctx.root_agent = str(cur_root.get("agent_id") or "")
    ctx.current_step_spans = step_spans  # also names the agents in a bare @CURRENT_STEP dump
    if need("CURRENT_STEPS"):
        ctx.current_steps = _format_steps(step_spans, ctx.root_agent)
    if need("CURRENT_STEPS_COUNT"):
        ctx.current_steps_count = str(len(step_spans))
    if cur_idx > 0:
        prev_user, prev_answer = ios[cur_idx - 1]
        if need("PREVIOUS_USER_MSG"):
            ctx.previous_user_msg = prev_user if prev_user else None
        if need("PREVIOUS_ASSISTANT_MSG"):
            ctx.previous_assistant_msg = prev_answer if prev_answer else None

    if cl != CL_STEP:
        return ctx

    # step: locate the current step within the turn (by span_id; fall back to the first step)
    step_idx = next(
        (i for i, s in enumerate(step_spans) if s.get("span_id") == current_span_id), 0
    )
    if step_spans:
        if need("CURRENT_STEP"):
            ctx.current_step = step_spans[step_idx]
        if need("STEP_NUMBER"):
            ctx.step_number = str(step_idx + 1)
        if need("PREVIOUS_STEP") and step_idx > 0:
            ctx.previous_step = step_spans[step_idx - 1]
    return ctx


# ── resolver ─────────────────────────────────────────────────────────────────


def _resolve_variable(name: str, prop: str | None, ctx: EvaluationContext) -> str | None:
    if name == "DEPENDENCIES":
        deps = ctx.dependencies
        if not deps:
            return None
        return "\n".join(f"- {n}: {json.dumps(p, ensure_ascii=False)}" for n, p in deps.items())
    if name == "METRIC_PREVIOUS_RESULT":
        r = ctx.metric_previous_result
        return json.dumps(r, ensure_ascii=False, indent=2) if r else None
    if name == "AGENTS":
        return format_agents(ctx.agents, prop) if ctx.agents else None
    if name == "CURRENT_STEPS" and prop:
        wanted = prop.upper()
        return _format_steps(
            [s for s in ctx.current_step_spans if str(s.get("type") or "").upper() == wanted],
            ctx.root_agent,
            _agent_names(ctx.current_step_spans),
        )
    if name in _STRING_ATTRS:
        return getattr(ctx, _STRING_ATTRS[name])
    if name in _OBJECT_ATTRS:
        obj = getattr(ctx, _OBJECT_ATTRS[name])
        if not obj:
            return None
        if name == "CURRENT_MESSAGE":
            return _resolve_message_field(obj, prop) if prop else _format_message(obj)
        return (
            _resolve_step_field(obj, prop)
            if prop
            else _format_step(obj, ctx.root_agent, _agent_names(ctx.current_step_spans))
        )
    return None  # unknown variable → soft miss


class TemplateResolver:
    """Substitutes `@VARIABLE` / `@VARIABLE.prop` against an `EvaluationContext`. Missing values
    become `[No <REF> available]`; never raises (a formatter blowup degrades to a soft miss)."""

    def resolve(self, template: str, context: EvaluationContext) -> ResolvedTemplate:
        used: list[str] = []
        missing: list[str] = []

        def repl(m: re.Match) -> str:
            name, prop = m.group(1), m.group(2)
            ref = name + (f".{prop}" if prop else "")
            try:
                val = _resolve_variable(name, prop, context)
            except Exception:
                val = None
            if not val:
                if ref not in missing:
                    missing.append(ref)
                return f"[No {ref} available]"
            if ref not in used:
                used.append(ref)
            return val

        text = VARIABLE_RE.sub(repl, template or "")
        return ResolvedTemplate(text, used, missing)


template_resolver = TemplateResolver()
