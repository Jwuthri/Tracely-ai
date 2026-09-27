"""LLM-as-judge evaluator — one class, three granularities, every call through LangChain's
`create_agent` on OpenRouter (see `infrastructure.llm.provider`).

The judge's rubric prompt becomes the agent's SYSTEM prompt and the graded ITEM is the user
message — the item alone, with nothing bundled alongside it, so what the judge reads is exactly
what the level names (every string it sends is rendered in `prompts.py`, which documents the
system-vs-user contract):

- level CONVERSATION  → the whole `[user, ai, user, ai …]` transcript, once
- level AGENT_RUN     → one `[user, ai]` pair, per message
- level SPAN/TOOL/GENERATION → one step — every event inside the message (tool call, thinking,
                        retrieval, agent hand-off), the message root excluded

This module owns everything that is NOT prompt text: the response schemas, the level dispatch,
the sequential continuity policy (`_chat_id`), and output→`EvalResult` conversion. A failed
grade returns None → NO score is written; the failure is logged and visible in the eval
recording, never persisted as a fake verdict.

`config.output_type` picks the structured response schema the agent must return:

Output types → persisted score shape:
  score (default) → NUMERIC value 0..1, PASS/FAIL via `threshold`
  number          → NUMERIC value (any range), PASS/FAIL only when `threshold` is set
  boolean         → BOOLEAN 1/0, PASS/FAIL
  text            → TEXT string_value (+ optional reason), no verdict
  json            → the user's `config.output_schema` compiled to a pydantic contract (enums
                    become Literal constraints). Exactly the user's fields — nothing appended. A
                    numeric `score` field (if present) drives the 0..1 `value` + PASS/FAIL via
                    `threshold`; a `reason`/`reasoning`/`summary` field becomes the comment. With
                    neither, the column is informational (no value, no verdict).

Execution mode (`config.execution_mode`, default "batch"):
- batch      → every item graded independently, nothing carried between them.
- sequential → items graded in order as ONE conversation with the model: the rubric is the system
               prompt, then item → verdict → item → verdict (a durable LangGraph thread; see
               `infrastructure/llm/checkpointer.py`). A step judge's conversation spans the steps
               of one MESSAGE (reset and rebuilt on every pass over that trace); a message judge's
               spans the TURNS of one thread, extended incrementally by the settled-thread pass
               and rebuilt when the turn order changes or a full run is forced — a lone
               mid-thread re-grade never extends it. Cross-turn, a metric's previous
               result is seeded into the first item (`CFG_PREVIOUS`, injected by
               the service). Without a reachable checkpointer the conversation degrades to
               pasting the run-so-far into each item's own prompt. CONVERSATION level has one
               item, so the mode changes nothing there.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field

from tracely.domain.evaluation.evaluators.base import (
    CFG_CHAIN_PASS,
    CFG_DEPENDENCIES,
    CFG_PREVIOUS,
    CONVERSATION,
    SPAN,
    STEP_LEVELS,
    Evaluator,
    default_registry,
)
from tracely.domain.evaluation.evaluators import prompts
from tracely.domain.evaluation.evaluators.prompts import ADVANCED_SYSTEM
from tracely.domain.evaluation.evaluators.catalog import DEFAULT_JUDGE_PROMPT
from tracely.domain.evaluation import budget, decision
from tracely.domain.evaluation.output_schema import model_from_json_schema
from tracely.domain.evaluation.results import EvalResult, RunContext, chain_payload
from tracely.domain.evaluation.template_resolver import (
    build_context,
    extract_template_variables,
    template_resolver,
)
from tracely.domain import introspection
from tracely.infrastructure.llm import provider

log = structlog.get_logger()

OUTPUT_TYPES = ("score", "number", "boolean", "text", "json", *decision.DECISION_OUTPUT_TYPES)

_DEFAULT_MAX_SPANS = 30  # cost guard for per-step judges


# ── structured response schemas (create_agent response_format) ───────────────


class ScoreVerdict(BaseModel):
    """Quality grade with a normalized score."""

    score: float = Field(description="0..1 quality score: 0 = total failure, 0.5 = mediocre, 1 = flawless")
    reason: str = Field(default="", description="one or two sentences citing the decisive evidence")


class NumberVerdict(BaseModel):
    """Numeric measurement (any range)."""

    value: float = Field(description="the numeric evaluation result")
    reason: str = Field(default="", description="one or two sentences justifying the number")


class PassFailVerdict(BaseModel):
    """Binary check outcome."""

    passed: bool = Field(description="true when the graded content passes this check")
    reason: str = Field(default="", description="the strongest signal behind the decision")


class TextVerdict(BaseModel):
    """Free-text observation."""

    text: str = Field(description="the observation — a short, specific paragraph")
    reason: str = Field(default="", description="one sentence on why this observation matters")


def _parse_json_object(text: str) -> dict:
    """Best-effort strict-JSON parse of a model reply (tolerates ```json fences)."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    parsed = json.loads(t)
    if not isinstance(parsed, dict):
        raise ValueError(f"judge returned non-object JSON: {type(parsed).__name__}")
    return parsed


def _deps(config: dict, span_id: str | None = None) -> dict:
    """Dependency results (`CFG_DEPENDENCIES`) flattened to `{score_name: payload | [payloads]}`.

    `span_id=None` — a trace/conversation grade: one item, so every result of each dependency
    applies. With a `span_id` — one step: each dependency's result for this exact span, falling
    back to its trace/conversation-level result (recorded under span_id ""), so a step-level
    composite lines up step N's prerequisite verdicts while a dependency on a per-turn metric is
    shared across the run's steps."""
    raw = config.get(CFG_DEPENDENCIES) or {}
    out: dict = {}
    for name, records in raw.items():
        if not isinstance(records, list) or not records:
            continue
        match = records
        if span_id is not None:
            match = [rec for rec in records if rec.get("span_id") == span_id]
            if not match:  # trace/conversation-level dependency → applies to every step
                match = [rec for rec in records if not rec.get("span_id")]
        if match:
            out[name] = match[0].get("payload") if len(match) == 1 else [m.get("payload") for m in match]
    return out


def _is_sequential(config: dict) -> bool:
    return str(config.get("execution_mode") or "batch") == "sequential"


def _chat_id(ctx: RunContext, config: dict, score_name: str, level: str) -> str | None:
    """The conversation this column holds with the model, or None for a batch column.

    Batch means the items are independent, so each one is a fresh question — sharing a transcript
    would be exactly the contamination `batch` promises not to have. Sequential means the opposite,
    and gets one thread per (workspace, column, subject): the rubric and every earlier item→verdict
    pair stay on it, so a later item sends only itself and the provider serves the rest from its
    prompt cache. The subject matches the level's sequence:

    - STEP levels → the TRACE. A step judge chains the steps of one message; its `_run_steps` call
      is always a complete ordered pass, which resets the conversation before item 1.
    - AGENT_RUN → the THREAD, and only inside a whole-thread pass (`__chain_pass__`, stamped by
      `evaluate_thread`, which resets the conversation first). A lone re-grade of one mid-thread
      turn must NOT extend the durable conversation — it would append its item out of order — so
      it falls back to pasting the earlier turns into its own prompt.
    - CONVERSATION → never. The thread is one item; there is no sequence to hold a conversation
      about, and chaining re-grades would show the judge a stale copy of the whole transcript per
      settle.

    Returns None when the checkpointer can't be reached, and that is load-bearing: the callers
    stop pasting the history in once a chat exists, so answering "yes" on a thread that will not
    actually persist would degrade every sequential column to batch — silently, and only in the
    incident where the database is already unhappy.
    """
    if not _is_sequential(config) or level == CONVERSATION:
        return None
    if decision.is_decision_output(config.get("output_type")):
        # A decision model has no conversation to hold: every call is one state + one question.
        # None here makes the callers paste the run-so-far into the item instead — sequential
        # decision columns still see their history, as state.
        return None
    if level in STEP_LEVELS:
        subject = ctx.trace_id
    elif config.get(CFG_CHAIN_PASS):
        subject = ctx.thread_id or ctx.trace_id
    else:
        return None
    from tracely.infrastructure.llm.checkpointer import chat_id, get_checkpointer

    if not subject or get_checkpointer() is None:
        return None
    return chat_id(ctx.project_id, score_name, subject)


def _previous_from_config(config: dict) -> dict | None:
    """The cross-item seed for sequential mode (set by EvaluationService for thread runs)."""
    if not _is_sequential(config):
        return None
    prev = config.get(CFG_PREVIOUS)
    return prev if isinstance(prev, dict) else None


@default_registry.register
class LLMJudgeEvaluator(Evaluator):
    """Runs the configured rubric through `create_agent` with a structured response schema.
    Skipped entirely when no LLM credential is configured so the rest of the pipeline runs
    unchanged."""

    kind: ClassVar[str] = "llm_judge"

    def run(self, ctx: RunContext, params: dict) -> list[EvalResult]:
        if not provider.llm_enabled():
            return []
        config = params  # the full evaluator config — flat, see `base.dispatch`
        if config.get("is_advanced"):
            return self._run_advanced(ctx, config)
        if self.level == CONVERSATION:
            return self._run_conversation(ctx, config)
        if self.level in STEP_LEVELS:
            return self._run_steps(ctx, config)
        return self._run_message(ctx, config)

    # ── per-level runners ────────────────────────────────────────────────────

    def _run_message(self, ctx: RunContext, config: dict) -> list[EvalResult]:
        """One grade for the message (level AGENT_RUN, one trace): user request vs final answer.

        Only a run with neither a request nor an answer is skipped — an agent that crashed into
        no answer still gets graded (see `_message_body`); "not graded yet" for the worst outcome
        there is was the old bug."""
        body = prompts.message_body(ctx.spans, include_answer=config.get("include_answer", True) is not False)
        if not body:
            return []
        # Sequential: the earlier turns are the context this message is judged in. Batch grades the
        # message alone. That is the ONLY difference between the modes at this level — the item
        # itself is `[user, ai]` either way, with no tool dump, no catalog, nothing else bundled
        # in. A judge that needs the tool results should read them at step level, where the tool
        # call IS the item. On a chain pass the earlier turns are already on the column's durable
        # conversation (verdicts included), so only the fallback path pastes them in — but they ARE
        # what the model is reading, so the recording shows them (`Recording.context`).
        chat = _chat_id(ctx, config, self.score_name, self.level)
        history = ""
        if _is_sequential(config) and not chat and ctx.thread_spans:
            lines = prompts.turn_lines(ctx.thread_spans, stop_before=ctx.trace_id)
            if lines:
                history = (
                    "Conversation so far (earlier turns, oldest first):\n"
                    + prompts.clip("\n".join(lines), prompts.TRUNC_TRAJECTORY) + "\n\n"
                )
        rec = introspection.active()
        if rec and chat:
            rec.context = prompts.earlier_messages(ctx.thread_spans, ctx.trace_id)
        result = self._grade(
            config, history + body,
            # On a chain pass the transcript carries the earlier verdicts in the judge's own
            # words; pasting the seed too would say it twice.
            previous=None if chat else _previous_from_config(config),
            chat_id=chat,
        )
        return [result] if result else []

    def _step_candidates(self, ctx: RunContext, config: dict) -> tuple[list[dict], int]:
        """The spans a step-level judge grades, with I/O, capped at `max_spans`. Shared by the
        basic and advanced step paths. Returns `(candidates, eligible)` where `eligible` is the
        pre-cap count so callers can surface partial coverage.

        A step is EVERY event inside the message — tool calls, thinking, retrieval, the hand-offs
        between agents. Level SPAN takes them all (narrow it with `config.span_types`);
        TOOL/GENERATION/CHAIN take exactly that type. The message root is never a step: it is the
        message, and grading it here would duplicate the message level.
        """
        wanted = (
            {str(t).upper() for t in (config.get("span_types") or [])}
            if self.level == SPAN
            else {self.level}
        )
        root_id = (ctx.root or {}).get("span_id", "")
        candidates = [
            s for s in ctx.spans
            if (not wanted or s.get("type") in wanted)
            and s.get("span_id") != root_id
            and (s.get("input") or s.get("output"))
        ]
        eligible = len(candidates)
        max_spans = int(config.get("max_spans") or _DEFAULT_MAX_SPANS)
        if eligible > max_spans:
            log.info(
                "llm_judge_step_cap", trace_id=ctx.trace_id, candidates=eligible, cap=max_spans
            )
            candidates = candidates[:max_spans]
        return candidates, eligible

    def _run_steps(self, ctx: RunContext, config: dict) -> list[EvalResult]:
        """One grade per step. Level SPAN grades `config.span_types` (default TOOL+GENERATION);
        a TOOL/GENERATION/CHAIN-level judge grades exactly that span type."""
        candidates, eligible = self._step_candidates(ctx, config)
        # Honest partial-coverage signal: a capped run grades only the first `len(candidates)` of
        # `eligible` steps. Surface it on each result's comment instead of dropping the rest silently.
        coverage = (
            f" [coverage: graded {len(candidates)} of {eligible} steps; "
            f"{eligible - len(candidates)} not evaluated (max_spans cap)]"
            if eligible > len(candidates)
            else ""
        )
        sequential = _is_sequential(config)
        previous = _previous_from_config(config)
        chat = _chat_id(ctx, config, self.score_name, self.level)
        if chat:
            # This call is a complete ordered pass over the message's steps, and the conversation
            # is valid for exactly one pass: a re-graded trace (late spans, an on-demand re-run)
            # must rebuild it from step 1, not append a second copy of every step to it.
            from tracely.infrastructure.llm.checkpointer import reset_chat

            reset_chat(chat)
        out: list[EvalResult] = []
        for i, s in enumerate(candidates):
            # Name the recorded call for the span it judges. Without this a step-level column
            # grading 10 spans records 10 identical rows named after the model, and "which step
            # did it fail on?" is unanswerable — the one question step-level grading exists for.
            rec = introspection.active()
            if rec:
                rec.target = f"{s.get('type')} {s.get('name') or s.get('step_id') or i + 1}"
                # On a chat thread the earlier steps are already on the conversation, so they do
                # not go over the wire again — but they ARE what the model is reading, and a row
                # that shows only step N makes sequential indistinguishable from batch. Record
                # them (display only; the wire prompt below is unchanged).
                rec.context = (
                    "".join(f"{prompts.step_body(candidates, n)}\n\n[user]\n" for n in range(i))
                    if sequential and i and chat
                    else ""
                )
            # Sequential means the run so far is context: the tool call is judged knowing what the
            # model was thinking when it chose it. Batch grades each step on its own — that is the
            # whole difference between the two modes.
            #
            # On a chat thread the earlier steps ARE the earlier user messages, so re-rendering
            # them here would send the same text twice and break the cached prefix. Only the
            # fallback path (no checkpointer) pastes them in.
            trajectory = (
                "Steps already taken in this message (oldest first):\n"
                + prompts.clip("\n".join(prompts.step_line(p, n) for n, p in enumerate(candidates[:i], start=1)),
                        prompts.TRUNC_TRAJECTORY)
                + "\n\n"
                if sequential and i and not chat
                else ""
            )
            body = trajectory + prompts.step_body(candidates, i)
            result = self._grade(
                config, body,
                # With a chat the transcript carries each step's verdict, so the seed is pasted
                # only where the fresh conversation has nothing yet: the first step, whose
                # `previous` is the previous TURN's result of this metric (`__previous_result__`).
                previous=previous if (not chat or i == 0) else None,
                deps=_deps(config, s.get("span_id", "")),
                chat_id=chat,
            )
            if result:
                result.target_span_id = s.get("span_id", "")
                if coverage:
                    result.comment = (result.comment or "") + coverage
                out.append(result)
                if sequential:
                    previous = chain_payload(value=result.value, verdict=result.verdict, comment=result.comment, string_value=result.string_value)
        return out

    def _run_conversation(self, ctx: RunContext, config: dict) -> list[EvalResult]:
        """One grade for the whole thread: a turn-by-turn transcript (ctx.spans spans every
        trace in the thread; each span carries its trace_id)."""
        lines = prompts.turn_lines(ctx.spans)
        if not lines:
            return []
        turns = sum(1 for line in lines if " — user: " in line) or len(lines)
        transcript = prompts.clip("\n".join(lines), 8000)
        # The messages, and nothing else. A conversation-level judge is asked to read a
        # conversation; a tool catalog stapled underneath made it grade tool choice instead, and
        # marked a plain greeting down for not calling an identification tool.
        body = f"Full conversation ({turns} turn{'s' if turns != 1 else ''}):\n{transcript}"
        # Never a chat_id here: the thread is one item, and each settle-pass re-grades it whole —
        # a durable conversation would only show the judge stale copies of the same transcript.
        result = self._grade(config, body)
        return [result] if result else []

    # ── advanced (template) grading ──────────────────────────────────────────

    def _history_override(self, ctx: RunContext, wanted: list[str]) -> str | None:
        """The rolling summary for this thread, when one exists — it backs `@ROLLING_SUMMARY` and
        is a compact, prefix-stable substitute for the raw transcript at `@HISTORY`/`@MESSAGES`.
        None (the default) leaves the raw transcript in place, so behavior is unchanged when no
        summary has been generated."""
        names = {w.split(".", 1)[0] for w in (wanted or [])}
        if not ({"HISTORY", "MESSAGES", "ROLLING_SUMMARY"} & names):
            return None
        try:
            from tracely.services.rolling_summary_service import RollingSummaryService

            return RollingSummaryService.history_override(
                ctx.project_id,
                ctx.thread_id or ctx.trace_id,
                # message/step judges read the summary as of THEIR turn, not the thread's end
                through_trace_id="" if self.level == CONVERSATION else ctx.trace_id,
            )
        except Exception:  # a cache miss/error must never block a grade
            return None

    def _declared_agents(self, ctx: RunContext, wanted: list[str]) -> list[dict] | None:
        """The user-declared agent catalog for `@LIST_AGENT`, when one was sent for this thread —
        a richer (description + tool params) substitute for the spans-derived agent list."""
        names = {w.split(".", 1)[0] for w in (wanted or [])}
        if "LIST_AGENT" not in names:
            return None
        try:
            from tracely.services.conversation_agents_service import ConversationAgentsService

            return ConversationAgentsService.for_thread(
                ctx.project_id, ctx.thread_id or ctx.trace_id
            )
        except Exception:
            return None

    def _run_advanced(self, ctx: RunContext, config: dict) -> list[EvalResult]:
        """Advanced judge: resolve the user's `@VARIABLE` template against the trace/thread, then
        grade. Mirrors the basic per-level dispatch — but the user controls the context. Uses
        `ctx.thread_spans` (the whole thread, set by the service when conversation-scoped vars are
        referenced) for @HISTORY/@PREVIOUS_*, falling back to the current trace's `spans`."""
        template = config.get("prompt") or ""
        wanted = config.get("template_variables") or extract_template_variables(template)
        thread_spans = ctx.thread_spans or ctx.spans
        history_override = self._history_override(ctx, wanted)
        declared_agents = self._declared_agents(ctx, wanted)
        if self.level in STEP_LEVELS:
            return self._run_advanced_steps(
                ctx, config, template, wanted, thread_spans, history_override, declared_agents
            )
        context = build_context(
            self.level,
            thread_spans=thread_spans,
            current_trace_id=ctx.trace_id,
            metric_previous_result=_previous_from_config(config),
            wanted_vars=wanted,
            history_override=history_override,
            declared_agents=declared_agents,
        )
        result = self._grade_resolved(config, template, context)
        return [result] if result else []

    def _run_advanced_steps(
        self,
        ctx: RunContext,
        config: dict,
        template: str,
        wanted: list[str],
        thread_spans: list[dict],
        history_override: str | None = None,
        declared_agents: list[dict] | None = None,
    ) -> list[EvalResult]:
        """One advanced grade per qualifying step (reusing the basic candidate selection + cap),
        threading the previous result into `@METRIC_PREVIOUS_RESULT` in sequential mode."""
        sequential = _is_sequential(config)
        previous = _previous_from_config(config)
        candidates, eligible = self._step_candidates(ctx, config)
        coverage = (
            f" [coverage: graded {len(candidates)} of {eligible} steps; "
            f"{eligible - len(candidates)} not evaluated (max_spans cap)]"
            if eligible > len(candidates)
            else ""
        )
        out: list[EvalResult] = []
        for s in candidates:
            context = build_context(
                self.level,
                thread_spans=thread_spans,
                current_trace_id=ctx.trace_id,
                current_span_id=s.get("span_id"),
                metric_previous_result=previous,
                wanted_vars=wanted,
                history_override=history_override,
                declared_agents=declared_agents,
            )
            result = self._grade_resolved(config, template, context)
            if result:
                result.target_span_id = s.get("span_id", "")
                if coverage:
                    result.comment = (result.comment or "") + coverage
                out.append(result)
                if sequential:
                    previous = chain_payload(value=result.value, verdict=result.verdict, comment=result.comment, string_value=result.string_value)
        return out

    def _grade_resolved(self, config: dict, template: str, context) -> EvalResult | None:
        """Resolve the rubric's `@VARIABLE`s and grade — unless NOTHING resolved.

        A template whose every variable soft-missed has no subject: the judge would read a rubric
        made entirely of `[No CURRENT_MESSAGE.output available]` and, having been asked for a
        verdict, invent one. Real exporters ship orphan fragments constantly (a tool/chain span
        whose turn root never arrived, carrying no input and no output on any span), and grading
        them manufactured a "the agent provided no answer at all" failure cluster out of pure
        ingestion noise — plus one LLM call each. No score is written instead, which is what
        `_run_message` has always done for an empty message body.

        Deliberately "none resolved", not "any missing": a turn with a request and no answer is a
        REAL failure and still gets graded — its `@CURRENT_MESSAGE.input` resolved. A rubric with
        no variables at all (a static prompt) has nothing to miss and is never skipped."""
        resolved = template_resolver.resolve(template, context)
        if resolved.variables_missing and not resolved.variables_used:
            return None
        return self._grade_advanced(config, resolved.resolved_text)

    # ── grading + output-type handling ───────────────────────────────────────

    def _grade(
        self,
        config: dict,
        body: str,
        previous: dict | None = None,
        deps: dict | None = None,
        chat_id: str | None = None,
    ) -> EvalResult | None:
        """Basic grade: assemble the prompt (rubric system prompt + auto deps context), then hand
        off to the shared model-call tail.

        `chat_id` puts this item into the column's running conversation with the model instead of
        asking a fresh question. Callers decide what rides along: they pass `previous` only when
        the transcript does NOT already carry it (no chat, or the first item of a fresh chat).
        """
        rubric = config.get("prompt") or DEFAULT_JUDGE_PROMPT
        if previous:
            body += (
                "\n\nPrevious result of this metric (the preceding item in the sequence — use "
                "it for continuity and comparison):\n"
                + prompts.clip(json.dumps(previous, ensure_ascii=False), 1500)
            )
        if deps is None:  # trace/conversation grades use every dependency result
            deps = _deps(config)
        if deps:
            dep_lines = "\n".join(
                f"- {name}: {prompts.clip(json.dumps(v, ensure_ascii=False), 600)}"
                for name, v in deps.items()
            )
            body += (
                "\n\nResults from prerequisite evaluations (use these as additional context):\n"
                + dep_lines
            )
        return self._call_and_build(config, system_prompt=rubric, body=body, chat_id=chat_id)

    def _grade_advanced(self, config: dict, resolved_text: str) -> EvalResult | None:
        """Advanced grade: the resolved `@VARIABLE` template IS the prompt — the user's
        placeholders already carry every piece of context they chose, so it goes in the human
        message and nothing is auto-appended (`@METRIC_PREVIOUS_RESULT` handles sequential
        chaining inside the template).

        It used to be passed as BOTH the system prompt and the human message. That doubled the
        input tokens of every advanced grade — an `@HISTORY` template is 8k characters — and
        repeating a rubric verbatim measurably degrades instruction-following on several models.
        Sent once now, under a fixed preamble that only says what kind of job this is.

        **No `chat_id` here, on purpose — do not "fix" this.** Basic columns hold a durable
        conversation with the model because their rubric is the system prompt and each item is a
        short human turn, so the prefix repeats byte for byte. An advanced template is the
        opposite shape: the rubric and its resolved context (`@HISTORY` is routinely 8k) are one
        blob in the HUMAN message. Chaining those would store that blob once per item inside the
        transcript — the conversation carried N times in a conversation — which costs more than
        the one-shot it replaced. Sequential advanced columns chain through
        `@METRIC_PREVIOUS_RESULT` instead, which is the author's explicit choice about what
        carries over.
        """
        return self._call_and_build(config, system_prompt=ADVANCED_SYSTEM, body=resolved_text)

    def _call_and_build(
        self, config: dict, *, system_prompt: str, body: str, chat_id: str | None = None
    ) -> EvalResult | None:
        """The model call + output-type→result tail shared by basic and advanced grading —
        everything after the prompt is assembled.

        One context gate sits in front of every call (`domain/evaluation/budget.py`): the item is
        estimated against the column model's context, and one that does not fit is graded by the
        column's `fallback_model` or — with none set, or when it does not fit either — recorded
        as a visible, verdict-less skip. Nothing is truncated to fit. A provider that rejects the
        call for length anyway (`ContextOverflowError` — the estimate is only an estimate) takes
        the same path."""
        output_type = str(config.get("output_type") or "score").lower()
        if output_type not in OUTPUT_TYPES:
            output_type = "score"
        primary = config.get("model") or None
        is_decision = decision.is_decision_output(output_type)
        if is_decision and not provider.is_decision_model(primary):
            # A decision column whose model was swapped for an LLM outside the validated router:
            # answer the same question through the LLM rather than write nothing.
            is_decision = False
        attempts = [primary] + ([config["fallback_model"]] if config.get("fallback_model") else [])
        last_overflow = ""
        for i, model in enumerate(attempts):
            info = provider.model_info(model)
            decision_call = is_decision and i == 0
            if decision_call:
                questions = decision.build_questions(config)
                tokens = budget.estimate_tokens(decision.request_text(body, questions))
            elif decision.is_decision_output(output_type):
                tokens = budget.estimate_tokens(decision.fallback_system_prompt(config), body)
            else:
                tokens = budget.estimate_tokens(system_prompt, body)
            reserve = 0 if decision_call else provider.settings.llm_max_output_tokens
            if not budget.fits(info, tokens, reserve):
                last_overflow = budget.overflow_note(info["id"], tokens, info)
                log.info("llm_judge_over_context", evaluator=self.score_name, model=info["id"], tokens=tokens)
                continue
            try:
                if decision_call:
                    result = self._decision_call(config, body, questions, model=info["id"])
                else:
                    result = self._llm_call(
                        config, output_type, system_prompt=system_prompt, body=body,
                        model=model, chat_id=chat_id if i == 0 else None,
                    )
            except provider.ContextOverflowError:
                limit = f" {budget.k(info['context_tokens'])}" if info.get("context_tokens") else ""
                last_overflow = (
                    f"{info['id']} rejected the input as longer than its{limit} context. "
                    "Set a fallback model on this column to grade long items."
                )
                log.info("llm_judge_context_rejected", evaluator=self.score_name, model=info["id"])
                continue
            except Exception as exc:
                log.warning("llm_judge_failed", evaluator=self.score_name, level=self.level, error=str(exc))
                return None
            if result is not None and i > 0:
                note = f"[graded by fallback {info['id']}: input exceeded {attempts[0] or 'the default model'}'s context]"
                result.comment = f"{note} {result.comment}".strip()[:500]
            return result
        return self._skipped(last_overflow, has_fallback=len(attempts) > 1)

    def _skipped(self, note: str, *, has_fallback: bool) -> EvalResult:
        """A neutral, visible score for an item no configured model could read in full. Neutral
        on purpose: the verdict policy fails only on FAIL, so a too-long item never turns a gate
        red — but the cell says why it was not graded instead of staying mysteriously empty."""
        if has_fallback:
            note += " The fallback model's context is too small as well."
        return EvalResult(
            "", self.level, "", data_type="TEXT",
            string_value="Skipped — input too long", comment=note[:500],
        )

    def _decision_call(self, config: dict, state: str, questions: dict, *, model: str) -> EvalResult:
        usage: dict = {}
        answers = provider.run_decision(state, questions, model=model, on_usage=usage.update)
        return self._attach_usage(self._decision_result(decision.answer_fields(config, answers)), usage)

    def _decision_result(self, fields: dict) -> EvalResult:
        return EvalResult(
            "", self.level, fields["verdict"], data_type=fields["data_type"], value=fields["value"],
            string_value=fields["string_value"], comment=fields["comment"][:500],
        )

    def _llm_call(
        self, config: dict, output_type: str, *, system_prompt: str, body: str,
        model: str | None, chat_id: str | None,
    ) -> EvalResult | None:
        usage: dict = {}
        if decision.is_decision_output(output_type):
            # An LLM answering a decision column's question (its context fallback): constrained
            # to the same answer space, reshaped into the same answer, mapped by the same code.
            reply = provider.run_structured_agent(
                body,
                response_format=decision.fallback_schema(config),
                system_prompt=decision.fallback_system_prompt(config),
                model=model,
                on_usage=usage.update,
            )
            fields = decision.answer_fields(config, decision.fallback_answers(config, reply))
            return self._attach_usage(self._decision_result(fields), usage)
        if output_type == "json":
            schema_model = model_from_json_schema(config.get("output_schema"))
            if schema_model is not None:
                verdict = provider.run_structured_agent(
                    body,
                    response_format=schema_model,
                    system_prompt=system_prompt,
                    model=model,
                    on_usage=usage.update,
                    chat_id=chat_id,
                )
                return self._attach_usage(self._json_result(config, verdict.model_dump()), usage)
            # no usable schema: free-form strict JSON (the rubric defines the shape)
            text = provider.run_text_agent(
                body + "\n\nRespond with ONE strict JSON object shaped exactly as your "
                "instructions describe.",
                system_prompt=system_prompt,
                model=model,
                on_usage=usage.update,
            )
            return self._attach_usage(self._json_result(config, _parse_json_object(text)), usage)
        verdict = provider.run_structured_agent(
            body,
            response_format=self._response_format(output_type, config),
            system_prompt=system_prompt,
            model=model,
            on_usage=usage.update,
            chat_id=chat_id,
        )
        return self._attach_usage(self._to_result(config, output_type, verdict), usage)

    @staticmethod
    def _attach_usage(result: EvalResult | None, usage: dict) -> EvalResult | None:
        """Stamp this grade's LLM token usage onto the result (for per-evaluator cost)."""
        if result is not None and usage:
            result.usage = dict(usage)
        return result

    @staticmethod
    def _response_format(output_type: str, config: dict) -> type[BaseModel]:
        if output_type == "number":
            return NumberVerdict
        if output_type == "boolean":
            return PassFailVerdict
        if output_type == "text":
            return TextVerdict
        return ScoreVerdict

    def _to_result(self, config: dict, output_type: str, verdict: Any) -> EvalResult:
        reason = str(getattr(verdict, "reason", ""))[:500]
        if output_type == "number":
            value = float(verdict.value)
            v = ""
            if config.get("threshold") is not None:
                v = "PASS" if value >= float(config["threshold"]) else "FAIL"
            return EvalResult(
                "", self.level, v, data_type="NUMERIC", value=value, comment=reason,
            )
        if output_type == "boolean":
            ok = bool(verdict.passed)
            return EvalResult(
                "", self.level, "PASS" if ok else "FAIL", value=1.0 if ok else 0.0, comment=reason,
            )
        if output_type == "text":
            return EvalResult(
                "", self.level, "", data_type="TEXT", string_value=str(verdict.text)[:2000],
                comment=reason,
            )
        # default: score
        score_val = min(max(float(verdict.score), 0.0), 1.0)
        threshold = float(config.get("threshold", 0.6))
        ok = score_val >= threshold
        return EvalResult(
            "", self.level, "PASS" if ok else "FAIL", data_type="NUMERIC", value=score_val, comment=reason,
        )

    def _json_result(self, config: dict, parsed: dict) -> EvalResult:
        """Persist a custom-schema result — exactly the user's fields, nothing appended. If the
        schema carries a numeric `score`/`overall_score`, it drives the normalized 0..1 `value`
        (PASS/FAIL via `threshold`); a `reason`/`reasoning`/`summary` string becomes the comment.
        With neither, the column is informational (no value, no verdict). All fields stay in
        string_value so the user sees exactly what they defined."""
        parsed = dict(parsed)
        candidate = parsed.get("score", parsed.get("overall_score"))
        raw_score = candidate if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) else None
        reason = parsed.get("reason") or parsed.get("reasoning") or parsed.get("summary")
        value = min(max(float(raw_score), 0.0), 1.0) if raw_score is not None else None
        verdict = ""
        if value is not None and config.get("threshold") is not None:
            verdict = "PASS" if value >= float(config["threshold"]) else "FAIL"
        return EvalResult(
            "", self.level, verdict, data_type="TEXT", value=value,
            string_value=json.dumps(parsed, ensure_ascii=False)[:4000],
            comment=str(reason or "")[:500],
        )
