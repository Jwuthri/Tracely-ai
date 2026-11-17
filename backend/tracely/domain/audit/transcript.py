"""The rollout as the judges read it.

Every branch, not just the surviving one: a target that misbehaved before the attacker rewound it
still misbehaved (Petri judges all branches for the same reason). Branch 0 is printed in full;
each later branch prints only its own messages, under a header saying where it forked.

Message labels `[M4]` are the node ids, so a judge's citation points at a real message.
"""

from __future__ import annotations

import json

from tracely.domain.audit import media
from tracely.domain.audit.state import AuditState


def _message(node: dict) -> str:
    role = node["role"]
    label = f"[{node['id']}]"
    if role == "system":
        return f"{label} SYSTEM (written by the auditor):\n{node.get('content') or ''}"
    if role == "user":
        return f"{label} USER (the auditor):\n{media.node_text(node)}"
    if role == "tool":
        status = " — error" if node.get("status") == "error" else ""
        how = "auditor-simulated" if node.get("resolution", "synthetic") == "synthetic" else "real execution"
        return (
            f"{label} TOOL RESULT for {node.get('name') or node.get('tool_call_id')} "
            f"({how}{status}):\n{media.node_text(node)}"
        )
    parts = [f"{label} ASSISTANT (the target):"]
    if node.get("reasoning"):
        parts.append(f"<thinking>\n{node['reasoning']}\n</thinking>")
    content = node.get("content") or ""
    prefill = node.get("prefill") or ""
    if prefill and content.startswith(prefill):
        content = f"<prefill>{prefill}</prefill>{content[len(prefill):]}"
    if content:
        parts.append(content)
    for call in node.get("tool_calls") or []:
        parts.append(f"[calls {call['name']}({json.dumps(call['args'], ensure_ascii=False)})]")
    return "\n".join(parts)


def render(state: AuditState) -> str:
    """The whole rollout — target system prompt, tools, then every branch in creation order."""
    out: list[str] = []
    if state.control == "fixed" and state.system_prompt:
        out.append(f"TARGET SYSTEM PROMPT (fixed, under test):\n{state.system_prompt}")
    if state.tools:
        out.append("TARGET TOOLS:\n" + "\n".join(
            f"- {t['name']}: {(t.get('description') or '').strip()[:300]}" for t in state.tools
        ))
    by_branch: dict[int, list[dict]] = {}
    for node_id in state.order:
        node = state.nodes[node_id]
        by_branch.setdefault(node["branch"], []).append(node)
    for branch in state.branches:
        nodes = by_branch.get(branch["n"]) or []
        if not nodes:
            continue
        if branch["n"] == 0:
            out.extend(_message(n) for n in nodes)
            continue
        where = f"rolled back to [{branch['from']}]" if branch["from"] else "restart"
        out.append(f"--- Conversation Branch {branch['n']} ({where}) ---")
        out.extend(_message(n) for n in nodes)
        out.append(f"--- End Conversation Branch {branch['n']} ---")
    # Never truncated to fit: a transcript too long for a judge is refused by the context gate
    # in the service and the cell records why, exactly as evaluator columns do.
    return "\n\n".join(out)


def has_branches(state: AuditState) -> bool:
    return len(state.branches) > 1
