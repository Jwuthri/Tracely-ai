"""Rolling-summary data model + pure helpers.

A rolling summary is an ACCUMULATING, compressed record of a conversation up to a given step. It
is always stored at the STEP (span) level — one row per span — each holding the full summary of
every step from the start of the thread up to it (see the `rolling_summaries` table). This module
owns:

- the item schema (`ConversationSummaryItem` / `ConversationSummary`) the summarizer LLM emits,
- `step_components` — decomposing one span into its typed pieces (thinking / tool_call /
  tool_result / output_structured / output_content),
- `format_summary_as_history` — rendering accumulated items back into the `@HISTORY` string.

Pure: no I/O, no LLM. The service drives the accumulate loop; the agent does the summarizing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from tracely.domain.evaluation.text import content_text

# Local type constants (mirror otel vocabulary; avoid importing the evaluators package).
THINKING = "THINKING"
TOOL = "TOOL"
GENERATION = "GENERATION"
CHAIN = "CHAIN"
AGENT = "AGENT"

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"
ROLE_SYSTEM = "system"
ROLE_PREV_SUMMARY = "prev_summary"  # the compacted older-history item's role

# component / item types
T_THINKING = "thinking"
T_TOOL_CALL = "tool_call"
T_TOOL_RESULT = "tool_result"
T_OUTPUT_STRUCTURED = "output_structured"
T_OUTPUT_CONTENT = "output_content"
T_SUMMARY = "summary"  # a compacted block of older items (budget compaction)
T_ERROR = "error"  # a failed step's error message

_HISTORY_CLIP = 8000
_ITEM_CLIP = 2000


class ConversationSummaryItem(BaseModel):
    """One element of the accumulated summary. `content` is a ~10-20 word summary (or verbatim when
    the source was already short). Tool fields link a tool_call to its following tool_result and
    allow a cache-friendly LangChain message conversion."""

    role: str = Field(description="user | assistant | tool")
    type: str = Field(description="thinking | tool_call | tool_result | output_structured | output_content")
    content: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: str | None = None


class ConversationSummary(BaseModel):
    items: list[ConversationSummaryItem] = Field(default_factory=list)


@dataclass
class Component:
    """A typed piece of a step, pre-summary. The summarizer turns each into a summary item."""

    role: str
    type: str
    content: str
    tool_name: str | None = None
    tool_arguments: str | None = None
    meta: dict = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Good enough for budget decisions — we never bill on it."""
    return max(1, len(text or "") // 4)


def components_token_total(components: list[Component]) -> int:
    return sum(estimate_tokens(c.content) for c in components)


# --- readable-text extraction -------------------------------------------------
# Span I/O arrives as JSON strings wrapping chat messages / content blocks / framework envelopes.
# `user_text` pulls the human REQUEST out of an input; `assistant_text` pulls the model REPLY out of
# an output. Both return "" when there is no human text (e.g. a pure tool-calling turn), which is how
# the caller decides between an `output_content` item and a structured fallback. Distinct from
# `content_text` (first-text-or-raw) so a structured blob is never stored verbatim as `content`.

# user-request envelope keys, in priority order (CrewAI/OpenAI {question}, LangChain {messages}, …)
_USER_KEYS = ("question", "query", "prompt", "input", "text", "content", "message")


def _maybe_json(value: object) -> object:
    """Parse a JSON-looking string into its object; pass anything else (incl. plain strings) through."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return value
    return value


def _message_items(value: object) -> list[dict]:
    """The chat-message dicts in `value` — a single `{role|content}` dict or a list of them. A
    content-block array (`[{type,text}]`) is NOT messages (handled by `_block_text`)."""
    if isinstance(value, dict):
        return [value] if ("role" in value or "content" in value) else []
    if isinstance(value, list):
        return [m for m in value if isinstance(m, dict) and ("role" in m or "content" in m)]
    return []


def _block_text(content: object) -> str:
    """Text from a message `content`: a plain string, or a content-block array `[{type:text,text}]`
    / Gemini-style `[{text}]`, joined. "" when there's nothing textual."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return ""


def assistant_text(value: object) -> str:
    """The model's readable reply from a span output (last non-empty assistant/model message, or a
    bare content/content-block). "" for a pure tool-calling turn (empty `content` + `tool_calls`)."""
    value = _maybe_json(value)
    msgs = _message_items(value)
    if msgs:
        for m in reversed(msgs):  # prefer the last assistant/model message with text
            if m.get("role") in (ROLE_ASSISTANT, "model", None):
                t = _block_text(m.get("content"))
                if t.strip():
                    return t
        for m in reversed(msgs):  # otherwise any message with text
            t = _block_text(m.get("content"))
            if t.strip():
                return t
        return ""
    if isinstance(value, str):
        return value
    return _block_text(value)  # bare content-block array


def user_text(value: object) -> str:
    """The human request from a span input — unwraps message arrays (last `user` message), common
    envelopes ({question}/{messages}/{input}/…), Google-ADK `{new_message:{parts:[{text}]}}`, and
    content-block arrays. "" when nothing readable is found."""
    value = _maybe_json(value)
    msgs = _message_items(value)
    if msgs:
        for m in reversed(msgs):  # the most recent user turn
            if m.get("role") == ROLE_USER:
                t = _block_text(m.get("content"))
                if t.strip():
                    return t
        t = _block_text(msgs[0].get("content"))  # no explicit user role → first message
        if t.strip():
            return t
    if isinstance(value, dict):
        nm = value.get("new_message")  # Google ADK
        if isinstance(nm, dict):
            t = _block_text(nm.get("parts"))
            if t.strip():
                return t
        if value.get("messages"):
            t = user_text(value["messages"])
            if t.strip():
                return t
        for k in _USER_KEYS:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if v is not None and not isinstance(v, str):  # nested envelope, e.g. input:{messages:[…]}
                t = user_text(v)
                if t.strip():
                    return t
        return ""
    if isinstance(value, str):
        return value
    return _block_text(value)


def compacted_item(content: str) -> dict:
    """The compacted older-history block — one item with the `prev_summary` role (not a wrapper
    key). Sits at the front of the list ahead of the verbatim recent items."""
    return {"role": ROLE_PREV_SUMMARY, "type": T_SUMMARY, "content": content}


def as_summary_items(stored: object) -> list[dict]:
    """Normalize a stored summary to the canonical FLAT list of items. Accepts the list, a legacy
    `{prev_summary, items}` object (the compacted string becomes a leading prev_summary item), or
    None — so reads never break across shape changes."""
    if isinstance(stored, list):
        return list(stored)
    if isinstance(stored, dict):
        items = list(stored.get("items") or [])
        prev = stored.get("prev_summary")
        return ([compacted_item(prev)] + items) if prev else items
    return []


def summary_token_total(items: object) -> int:
    """Total estimated tokens across the summary's items — drives the 20k budget compaction."""
    return sum(estimate_tokens(it.get("content") or "") for it in as_summary_items(items))


def _clip(s: str, n: int = _ITEM_CLIP) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _as_structured(out: object) -> str | None:
    """The output rendered as pretty JSON when it's structured; None otherwise."""
    if isinstance(out, (dict, list)):
        return _clip(json.dumps(out, ensure_ascii=False), _ITEM_CLIP)
    if isinstance(out, str):
        t = out.strip()
        if t[:1] in ("[", "{"):
            try:
                return _clip(json.dumps(json.loads(t), ensure_ascii=False), _ITEM_CLIP)
            except ValueError:
                return None
    return None


def _tool_args(span: dict) -> str | None:
    """The tool-call arguments as a compact JSON string. Unwraps a sole `{kwargs:{…}}` / `{args:[],
    kwargs:{…}}` envelope (LlamaIndex) down to the real kwargs."""
    tcs = span.get("tool_calls")
    if tcs:
        try:
            return _clip(json.dumps(tcs, ensure_ascii=False), 600)
        except (TypeError, ValueError):
            return None
    obj = _maybe_json(span.get("input"))
    if isinstance(obj, dict) and isinstance(obj.get("kwargs"), dict) and set(obj) <= {"args", "kwargs"}:
        obj = obj["kwargs"]
    if isinstance(obj, (dict, list)):
        try:
            return _clip(json.dumps(obj, ensure_ascii=False), 600) or None
        except (TypeError, ValueError):
            pass
    return _clip(content_text(span.get("input")), 600) or None


def _tool_result_text(value: object) -> str:
    """The tool's result, preferring the real payload over framework wrappers — LlamaIndex
    `{raw_output:…}`, Google-ADK `{response:…}` — falling back to readable text."""
    obj = _maybe_json(value)
    if isinstance(obj, dict):
        for key in ("raw_output", "response"):
            if obj.get(key) is not None:
                try:
                    return _clip(json.dumps(obj[key], ensure_ascii=False))
                except (TypeError, ValueError):
                    break
    return _clip(content_text(value))


def user_input_component(span: dict) -> Component | None:
    """The user request carried on a turn's root span input, if any."""
    text = user_text(span.get("input"))
    return Component(ROLE_USER, T_OUTPUT_CONTENT, _clip(text)) if text and text.strip() else None


def user_input_for_turn(tspans: list[dict], root_id: str | None = None) -> Component | None:
    """The user request for a whole turn: the root span's input, else (frameworks whose root input is
    a workflow-config blob, e.g. LlamaIndex) the last `user` message in the turn's first GENERATION
    input. Returns None when no human request can be found."""
    root = next((s for s in tspans if s.get("span_id") == root_id), None) if root_id else None
    root = root or (tspans[0] if tspans else {})
    uc = user_input_component(root)
    if uc:
        return uc
    for s in tspans:
        if (s.get("type") or "").upper() == GENERATION:
            text = user_text(s.get("input"))
            if text and text.strip():
                return Component(ROLE_USER, T_OUTPUT_CONTENT, _clip(text))
    return None


def step_components(span: dict) -> list[Component]:
    """Decompose one span (step) into its typed components. Empty components are dropped. A failed
    step also carries its error — a failed tool usually has no result, so without it the history
    reads as a call that silently returned nothing."""
    comps = _step_components(span)
    msg = str(span.get("status_message") or "").strip()
    if msg or str(span.get("level") or "").upper() == "ERROR":
        role = ROLE_TOOL if (span.get("type") or "").upper() == TOOL else ROLE_ASSISTANT
        comps.append(Component(role, T_ERROR, _clip(f"ERROR: {msg or 'step failed'}"),
                               tool_name=span.get("name") or None))
    return comps


def _step_components(span: dict) -> list[Component]:
    stype = (span.get("type") or "").upper()
    comps: list[Component] = []

    if stype == THINKING:
        txt = content_text(span.get("output")) or content_text(span.get("input"))
        if txt:
            comps.append(Component(ROLE_ASSISTANT, T_THINKING, _clip(txt)))
        return comps

    if stype == TOOL:
        name = str(span.get("name") or "tool")
        args = _tool_args(span)
        call_text = f"{name}({args})" if args else name
        comps.append(
            Component(ROLE_ASSISTANT, T_TOOL_CALL, call_text, tool_name=name, tool_arguments=args)
        )
        result = _tool_result_text(span.get("output"))
        if result:
            comps.append(Component(ROLE_TOOL, T_TOOL_RESULT, result, tool_name=name))
        return comps

    # generation / other: the model's readable reply, else a genuinely structured (non-message)
    # output. Bare tool-call announces are intentionally dropped — the TOOL spans carry the real
    # calls (name + args + result), so announcing them here would only duplicate.
    out = span.get("output")
    text = assistant_text(out)
    if text and text.strip():
        comps.append(Component(ROLE_ASSISTANT, T_OUTPUT_CONTENT, _clip(text)))
    elif not _message_items(_maybe_json(out)):  # not a (text-less) chat message → real structured data
        structured = _as_structured(out)
        if structured:
            comps.append(Component(ROLE_ASSISTANT, T_OUTPUT_STRUCTURED, structured))
    return comps


def decompose_turn_span(
    span: dict, *, is_root: bool, multi: bool, user_comp: Component | None
) -> list[Component]:
    """Components a span contributes to the rolling summary. The turn's root contributes the user
    request (plus its own content only when it's the turn's single span); a non-root CHAIN/AGENT span
    is a framework wrapper/router (LangGraph `tools_condition`, LlamaIndex `BaseWorkflowAgent.*`) whose
    real content already lives in its child GENERATION/TOOL spans, so it contributes nothing."""
    if is_root:
        comps: list[Component] = []
        if user_comp:
            comps.append(user_comp)
        if not multi:
            comps += step_components(span)
        return comps
    if (span.get("type") or "").upper() in (CHAIN, AGENT):
        return []
    return step_components(span)


def dedup_consecutive(prev_last: dict | None, new_items: list[dict]) -> list[dict]:
    """`new_items` with any run of items identical (role+type+content) to the preceding item removed —
    collapses the duplicate final-answer rows frameworks emit (wrapper span + generation span)."""
    out: list[dict] = []
    last = prev_last
    for it in new_items:
        if (
            last
            and last.get("role") == it.get("role")
            and last.get("type") == it.get("type")
            and (last.get("content") or "") == (it.get("content") or "")
        ):
            continue
        out.append(it)
        last = it
    return out


def items_from_components(
    components: list[Component], summaries: list[str] | None = None
) -> list[ConversationSummaryItem]:
    """Build summary items from components. `summaries` (one per component, same order) replaces the
    content when the step was LLM-summarized; `None` keeps each component verbatim. Tool calls get a
    step-local id and the following tool_result inherits it (so a LangChain conversion can pair
    them). Pure + deterministic — ids are positional, not random."""
    items: list[ConversationSummaryItem] = []
    last_call_id: str | None = None
    for i, c in enumerate(components):
        content = c.content
        if summaries is not None and i < len(summaries) and summaries[i]:
            content = summaries[i]
        item = ConversationSummaryItem(
            role=c.role,
            type=c.type,
            content=_clip(content, _ITEM_CLIP),
            tool_name=c.tool_name,
            tool_arguments=c.tool_arguments,
        )
        if c.type == T_TOOL_CALL:
            last_call_id = f"call_{i}"
            item.tool_call_id = last_call_id
        elif c.type == T_TOOL_RESULT and last_call_id:
            item.tool_call_id = last_call_id
        items.append(item)
    return items


def _line(item: dict) -> str:
    role = item.get("role") or ROLE_ASSISTANT
    itype = item.get("type") or T_OUTPUT_CONTENT
    content = item.get("content") or ""
    if itype == T_SUMMARY:
        return f"[earlier conversation, summarized]\n{content}"
    if itype == T_OUTPUT_CONTENT and role in (ROLE_USER, ROLE_ASSISTANT):
        return f"[{role}]: {content}"
    return f"[{role}:{itype}] {content}"


def format_summary_as_history(items: object, max_chars: int = 0) -> str:
    """Render the summary's items into the `@HISTORY` string: a leading prev_summary block (if any)
    then [role]/[role:type] lines for the verbatim items. `max_chars > 0` clips; `<= 0` returns the
    full text (the budget's compaction already bounds size). Tolerates the legacy object shape."""
    lines = [_line(it) for it in as_summary_items(items) if (it.get("content") or "").strip()]
    text = "\n".join(lines)
    return _clip(text, max_chars) if max_chars and max_chars > 0 else text


# --- per-workspace budget -------------------------------------------------------
# The two knobs a workspace can set (the Rolling summary column's settings). Bounds keep a typo
# from producing a summary that is useless (a 200-token history) or one no judge can read.

MAX_TOKENS_RANGE = (2_000, 64_000)
STEP_MAX_TOKENS_RANGE = (64, 4_000)


@dataclass(frozen=True)
class SummaryBudget:
    max_tokens: int  # whole-summary budget; over it, older items fold into one `prev_summary`
    step_max_tokens: int  # a step at or under this is kept verbatim; larger ones get summarized

    def to_json(self) -> dict:
        return {"max_tokens": self.max_tokens, "step_max_tokens": self.step_max_tokens}


def summary_budget(raw: dict | None, *, default_max: int, default_step: int) -> SummaryBudget:
    """The effective budget: the workspace's stored values over the server defaults. Stored values
    are validated on write (`budget_problem`); anything unusable here falls back to the default."""
    raw = raw if isinstance(raw, dict) else {}

    def pick(key: str, default: int, bounds: tuple[int, int]) -> int:
        v = raw.get(key)
        ok = isinstance(v, int) and not isinstance(v, bool) and bounds[0] <= v <= bounds[1]
        return v if ok else default

    return SummaryBudget(
        max_tokens=pick("max_tokens", default_max, MAX_TOKENS_RANGE),
        step_max_tokens=pick("step_max_tokens", default_step, STEP_MAX_TOKENS_RANGE),
    )


def budget_problem(raw: dict) -> str | None:
    """The first problem with a submitted budget, or None."""
    allowed = {"max_tokens", "step_max_tokens"}
    extra = set(raw) - allowed
    if extra:
        return f"unknown keys: {sorted(extra)}; allowed: {sorted(allowed)}"
    for key, (lo, hi) in (("max_tokens", MAX_TOKENS_RANGE), ("step_max_tokens", STEP_MAX_TOKENS_RANGE)):
        if key in raw:
            v = raw[key]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                return f"{key} must be an integer between {lo} and {hi}"
    if raw.get("step_max_tokens", 0) > raw.get("max_tokens", MAX_TOKENS_RANGE[1]):
        return "step_max_tokens cannot exceed max_tokens"
    return None
