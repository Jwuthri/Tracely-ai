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

Everything here is pure text over span dicts: no I/O, no policy, no model calls. Budgets are the
`_TRUNC_*` constants; every clip is per-field so one huge tool dump can't evict the rest.
"""

from __future__ import annotations

from tracely.domain.evaluation.evaluators.base import CHAIN, GENERATION, TOOL
from tracely.domain.evaluation.text import answer_for, content_text, readable_io, request_for
from tracely.domain.traces.spans import root_span

TRUNC_IO = 1500  # per-step input/output excerpt
TRUNC_TURN = 800  # per-turn excerpt in the conversation transcript
TRUNC_STEP_LINE = 400  # one earlier step, in a sequential judge's trajectory
TRUNC_TRAJECTORY = 4000  # the whole run-so-far block (steps, or earlier turns)

# The system prompt for an ADVANCED grade. Deliberately says nothing about what to grade: the
# user's resolved template is the rubric AND the context, and it arrives as the human message.
ADVANCED_SYSTEM = (
    "You are an evaluator. Follow the instructions in the message exactly and return the "
    "structured verdict you are asked for — nothing else."
)


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


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
    user_in = request_for(root, spans)
    if not include_answer:
        return f"User message:\n{clip(user_in, 2000)}" if user_in else ""
    answer = answer_for(root, spans, TOOL, GENERATION, CHAIN)
    if not answer and not user_in:
        return ""
    return (
        f"User request:\n{clip(user_in, 2000)}\n\n"
        f"Agent answer:\n{clip(answer, 2000) or '(the agent produced no answer)'}"
    )


def step_body(candidates: list[dict], i: int) -> str:
    """The prompt for one step — `Step i of n` plus its I/O. Used for the item being graded and,
    in chained mode, to re-render the earlier items for the recording."""
    s = candidates[i]
    body = (
        f"Step {i + 1} of {len(candidates)} — {s.get('type')} `{s.get('name') or s.get('step_id') or ''}`\n"
        f"Step input:\n{clip(readable_io(s.get('input')), TRUNC_IO)}\n\n"
        f"Step output:\n{clip(readable_io(s.get('output')), TRUNC_IO)}"
    )
    # A failed step usually has NO output — the error is the whole story, and without it a judge
    # grading "was this the right call?" sees an empty result and has to guess why.
    err = str(s.get("status_message") or "").strip()
    if err or str(s.get("level") or "").upper() == "ERROR":
        body += f"\n\nStep error:\n{clip(err or '(failed, no error message)', TRUNC_IO)}"
    return body


def step_line(span: dict, n: int) -> str:
    """One earlier event, as a sequential judge sees it: what kind, what it was called, what came
    back. Output over input — the input is usually the prompt the judge already has."""
    body = content_text(span.get("output")) or content_text(span.get("input"))
    return (
        f"{n}. {span.get('type')} `{span.get('name') or span.get('step_id') or ''}`: "
        f"{clip(body, TRUNC_STEP_LINE)}"
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
        user_in = request_for(root, trace_spans)
        answer = answer_for(root, trace_spans, TOOL, GENERATION, CHAIN)
        if user_in:
            lines.append(f"Turn {n} — user: {clip(user_in, TRUNC_TURN)}")
        if answer:
            lines.append(f"Turn {n} — agent: {clip(answer, TRUNC_TURN)}")
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
