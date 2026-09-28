"""What the judge reads — every string the LLM judge sends, built here and only here.

Prompt architecture (the invariant the whole judge rests on):

- SYSTEM prompt = the column's rubric, verbatim and static. Nothing per-item ever goes in it —
  a byte-stable system prompt is what makes provider prompt caching work and keeps two grades of
  the same column comparable. (Advanced `@VARIABLE` columns use a fixed one-line system preamble
  instead — `ADVANCED_SYSTEM` — because their template mixes rubric and context by design.)
- USER message = the item being graded, and only dynamic material: the item body (built per level
  below), the run-so-far fallback when no durable conversation exists, the cross-turn seed, and
  dependency results. Assembled by the evaluator (`llm_judge._grade`); rendered here.

Item bodies per level:

- CONVERSATION → `turn_lines`: the whole thread as compact `Turn n — user/agent` lines.
- AGENT_RUN (message) → `message_body`: `User request / Agent answer`, the pair alone
  (`include_answer=False` drops the answer for a column that labels what the USER wanted). A
  rubric that needs the steps — tool results, retrievals — is an ADVANCED column: it asks for
  them by name with `@CURRENT_STEPS.tool`, which is the one mechanism for choosing context.
- SPAN/TOOL/GENERATION/CHAIN (step) → `step_body`: `Step i of n — TYPE name` + the step's I/O.

Sequential-mode context, when the durable judge conversation is unavailable (see
`llm_judge._chat_id`), is pasted into the user message instead: `turn_lines(..., stop_before=)`
for a message judge (the turns that LED to this one, never the future), `step_line` rows for a
step judge (this message's earlier steps). `earlier_messages` re-renders prior turns for the
introspection recording only — with a durable conversation the wire carries just the new item,
and a recording of that alone is indistinguishable from batch.

Everything here is pure text over span dicts: no I/O, no policy, no model calls. Nothing is
truncated: what the span recorded is what the judge reads. A column whose item is too long for its
model goes to its fallback model or is skipped visibly (`domain/evaluation/budget.py`).
"""

from __future__ import annotations

from tracely.domain.evaluation.text import (
    NO_ANSWER,
    NO_USER_MESSAGE,
    agent_answer,
    content_text,
    readable_io,
    user_message,
)
from tracely.domain.traces.spans import root_span

# The system prompt for an ADVANCED grade. Deliberately says nothing about what to grade: the
# user's resolved template is the rubric AND the context, and it arrives as the human message.
ADVANCED_SYSTEM = (
    "You are an evaluator. Follow the instructions in the message exactly and return the "
    "structured verdict you are asked for — nothing else."
)


def by_trace(spans: list[dict]) -> list[list[dict]]:
    """The thread's spans grouped into traces (turns), oldest first — spans arrive ordered by
    start_time, so first-seen order is turn order."""
    grouped: dict[str, list[dict]] = {}
    for s in spans:
        grouped.setdefault(s.get("trace_id") or "", []).append(s)
    return list(grouped.values())


def message_body(spans: list[dict], *, include_answer: bool = True) -> str:
    """One message as the judge reads it: user request vs final answer. Empty only when there is
    neither — an agent that crashed into silence still gets graded, stating plainly that there was
    no answer (the rubric decides what that means; a "did it refuse?" column may call it a PASS).

    `include_answer=False` sends the user's message alone, for a column that labels what the USER
    wanted (intent). Two reasons, and cost is the smaller one: the answer is the long half of
    every item, and a judge that reads it labels what the agent DID — hiding exactly the mismatch
    (user asked for a refund, agent answered an FAQ) the label exists to expose. It costs little
    context: a sequential column already has the earlier turns, answers included, on its
    conversation, so a terse "yes" still resolves against the question that prompted it. With no
    user message there is no intent to label, so the item is skipped."""
    root = root_span(spans)
    user_in = user_message(root)
    if not include_answer:
        return f"User message:\n{user_in}" if user_in else ""
    answer = agent_answer(root)
    if not answer and not user_in:
        return ""
    return (
        f"User request:\n{user_in or NO_USER_MESSAGE}\n\n"
        f"Agent answer:\n{answer or NO_ANSWER}"
    )


def step_body(candidates: list[dict], i: int, user_request: str = "") -> str:
    """The prompt for one step: the turn's user message (what the step was in service of — a
    "was this the right tool?" question can't be answered without it), then `Step i of n` with
    its input, output and error. The earlier-steps re-render for the recording passes no request."""
    s = candidates[i]
    head = f"User request:\n{user_request}\n\n" if user_request else ""
    body = head + (
        f"Step {i + 1} of {len(candidates)} — {s.get('type')} `{s.get('name') or s.get('step_id') or ''}`\n"
        f"Step input:\n{readable_io(s.get('input'))}\n\n"
        f"Step output:\n{readable_io(s.get('output'))}"
    )
    # A failed step usually has NO output — the error is the whole story, and without it a judge
    # grading "was this the right call?" sees an empty result and has to guess why.
    err = str(s.get("status_message") or "").strip()
    if err or str(s.get("level") or "").upper() == "ERROR":
        body += f"\n\nStep error:\n{err or '(failed, no error message)'}"
    return body


def step_line(span: dict, n: int) -> str:
    """One earlier event, as a sequential judge sees it: what kind, what it was called, what came
    back. Output over input — the input is usually the prompt the judge already has."""
    body = content_text(span.get("output")) or content_text(span.get("input"))
    return (
        f"{n}. {span.get('type')} `{span.get('name') or span.get('step_id') or ''}`: "
        f"{body}"
    )


def turn_lines(spans: list[dict], stop_before: str = "") -> list[str]:
    """The thread as `Turn n — user/agent` lines, oldest first. `stop_before` cuts it at that
    trace, so a sequential message judge sees the conversation that LED to the message it grades
    and not the message itself."""
    lines: list[str] = []
    for n, trace_spans in enumerate(by_trace(spans), start=1):
        if stop_before and (trace_spans[0].get("trace_id") or "") == stop_before:
            break
        root = root_span(trace_spans)
        user_in = user_message(root)
        answer = agent_answer(root)
        if user_in:
            lines.append(f"Turn {n} — user: {user_in}")
        if answer:
            lines.append(f"Turn {n} — agent: {answer}")
    return lines


def earlier_messages(thread_spans: list[dict] | None, current_trace_id: str) -> str:
    """The earlier turns already on this column's chat thread, re-rendered exactly as they were
    sent — for the RECORDING only (`Recording.context`); the wire carries just the new item."""
    if not thread_spans:
        return ""
    parts: list[str] = []
    for trace_spans in by_trace(thread_spans):
        if (trace_spans[0].get("trace_id") or "") == current_trace_id:
            break
        body = message_body(trace_spans)
        if body:
            parts.append(f"{body}\n\n[user]\n")
    return "".join(parts)
