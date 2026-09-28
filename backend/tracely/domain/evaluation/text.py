"""Text extraction used by evaluators (LLM-judge in particular) — pulls the readable answer
out of a span's structured I/O so the judge grades the actual reply, not its JSON wrapper.

Reuses `tracely.infrastructure.text.extract_text` (the text walker that powers
`message_text` for the routers / UI) so there's exactly one definition of "what's the human
text inside this value?" in the codebase.
"""

from __future__ import annotations

import json
from typing import Any

from tracely.infrastructure.text import extract_text


def content_text(value: Any) -> str:
    """Handles plain strings AND structured content (JSON content-block arrays / chat messages)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return extract_text(value)
    s = value.strip()
    if s[:1] in ("[", "{"):
        try:
            return extract_text(json.loads(s)) or value
        except (ValueError, TypeError):
            return value
    return value


_USER_ROLES = {"user", "human"}
# Everything that is plainly NOT the person talking to the agent. The second pass uses this, so a
# framework that calls the human `customer` or `contact` still resolves — the rule that survives an
# unknown vocabulary is "the last message that isn't the agent's", not a list of user synonyms.
_NON_USER_ROLES = {"assistant", "ai", "system", "tool", "function", "developer", "model"}


def _chat_messages(value: Any) -> list[dict]:
    """`value` as a list of chat messages, or [] when it isn't one."""
    if isinstance(value, str):
        s = value.strip()
        if s[:1] not in ("[", "{"):
            return []
        try:
            value = json.loads(s)
        except (ValueError, TypeError):
            return []
    # `{"messages": [...]}` — how LangChain/LangGraph state and several SDKs hand over a history.
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        value = value["messages"]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [m for m in value if isinstance(m, dict) and (m.get("role") or m.get("type"))]


def _last_user_text(value: Any) -> str:
    """The last thing the human said in this message list.

    Two passes: first a role we know, then anything that isn't the agent's own side. The roles in
    the wild are not always `user` (`customer`, `contact`, a framework prefix…), and falling
    through to "first readable text" is what put turn 1's greeting on every turn of a transcript.
    """
    msgs = _chat_messages(value)
    for known_only in (True, False):
        for m in reversed(msgs):
            role = str(m.get("role") or m.get("type") or "").lower()
            if (role in _USER_ROLES) if known_only else (role not in _NON_USER_ROLES):
                text = content_text(m.get("content", m))
                if text:
                    return text
    return ""


def _unescaped_json(text: str) -> str:
    """A JSON string re-serialized with real characters: tool payloads usually arrive
    ASCII-escaped, and a judge reading `\\u00e0` for `à` in a French FAQ is reading noise."""
    s = text.strip()
    if s[:1] not in ("[", "{") or "\\u" not in s:
        return text
    try:
        return json.dumps(json.loads(s), ensure_ascii=False)
    except (ValueError, TypeError):
        return text


def _tool_calls_text(message: dict) -> str:
    """A message's tool calls as `name(arguments)`, comma-joined; "" when it has none."""
    rendered = []
    for c in message.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        rendered.append(f"{fn.get('name') or c.get('name') or 'tool'}({fn.get('arguments') or c.get('arguments') or ''})")
    return ", ".join(rendered)


def _message_text(message: dict) -> str:
    """One chat message as a judge reads it: its text, then any tool calls it made. A message that
    is only a tool call used to come out as its raw JSON wrapper."""
    content = message.get("content")
    text = content_text(content) if content not in (None, "", []) else ""
    calls = _tool_calls_text(message)
    if calls:
        text = f"{text}\n[calls {calls}]".strip()
    return text


def readable_io(value: Any) -> str:
    """A span's I/O rendered for a judge to read: a chat message list as `role: text` lines (tool
    calls included), a single message as its text, anything else (a tool's JSON, a plain string)
    as-is with real characters instead of `\\u` escapes. Nothing is dropped or truncated."""
    msgs = _chat_messages(value)
    if not msgs:
        return _unescaped_json(content_text(value))
    if len(msgs) == 1:
        return _message_text(msgs[0]) or _unescaped_json(content_text(value))
    lines = []
    for m in msgs:
        role = str(m.get("role") or m.get("type") or "?").lower()
        text = _message_text(m)
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) or _unescaped_json(content_text(value))


# ── the two things a judge reads by default ─────────────────────────────────
# One rule, no guessing: a turn's USER MESSAGE is what the root span received and its ANSWER is
# what the root span returned. The root is the turn's entry point — the one span whose input and
# output are, by definition, the exchange with the user. Nothing is inferred from other spans (a
# sub-agent's prompt, the "richest" history, the latest generation): every such rule was right for
# one trace shape and silently wrong for the next, and none of them could be seen from the UI. If
# a column needs more — tool calls, sub-agents, errors — it asks for them with `@CURRENT_STEPS`,
# which dumps every span as recorded.
#
# When the root carries nothing readable, that is what the judge is told, in so many words, so a
# badly instrumented trace is visible instead of papered over.

NO_USER_MESSAGE = "(no user message recorded on this turn's root span)"
NO_ANSWER = "(no answer recorded on this turn's root span)"


def user_message(root: dict) -> str:
    """The user's message on this turn: the last user message in the root span's input when it is
    a chat message list, otherwise the root input as text. "" when the root has none."""
    text = _last_user_text(root.get("input"))
    if text:
        return text
    msgs = _chat_messages(root.get("input"))
    if msgs:  # a message list with no user turn in it (system-only, assistant-only)
        return ""
    return content_text(root.get("input")).strip()


def agent_answer(root: dict) -> str:
    """The agent's answer on this turn: the root span's output as text. A chat-shaped output (one
    message, or the conversation state a graph returns) is read as its LAST message; one that is
    only a tool call (the agent answered by acting — a card, a handoff) is shown as that call
    rather than as an empty reply. "" when the root has none."""
    out = root.get("output")
    msgs = _chat_messages(out)
    if msgs:
        last = msgs[-1]
        text = content_text(last.get("content")) if last.get("content") not in (None, "", []) else ""
        if text:
            return text
        calls = _tool_calls_text(last)
        return f"[no text reply — the agent called {calls}]" if calls else ""
    return content_text(out).strip()
