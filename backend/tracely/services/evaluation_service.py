"""Run a project's evaluators on traces/threads and persist the resulting `Scores`.

Score ids are deterministic per `(trace, evaluator, target span)` — and per `(thread, evaluator)`
for CONVERSATION-level evaluators — (see `ScoreWriter`) so re-evaluating as spans arrive across
batches, or re-running on demand from the UI, replaces rather than duplicates via
ReplacingMergeTree.

Entry points:
- `evaluate_trace`  — the ingest path (Celery) AND the on-demand path for one trace/turn.
  Runs every applicable evaluator: trace+span-level on the trace, conversation-level on the
  trace's thread (unless `skip_conversation`).
- `evaluate_thread` — the on-demand path for a whole conversation row: every turn, then the
  conversation-level evaluators once.

`on_result` (optional) fires once per persisted score with a JSON-ready dict — the SSE run
endpoint streams these straight into the grid.

Cluster-on-failure runs immediately after a trace's eval batch — the cheap structural signature
clustering, NOT the embedding rebuild (that's on-demand via `FailureIntelService`).
"""

from __future__ import annotations

import json
from typing import Callable

import structlog

from tracely.config import settings
from tracely.domain import introspection
from tracely.domain.evaluation.evaluators import EvalResult, RunContext, default_registry
from tracely.domain.evaluation.evaluators.base import (
    CFG_CHAIN_PASS,
    CFG_DEPENDENCIES,
    CFG_PREVIOUS,
    CONVERSATION,
    RUN,
)
from tracely.domain.evaluation.results import chain_payload
from tracely.domain.evaluation.targeting import spec_applies
from tracely.domain.evaluation.template_resolver import references_conversation_scope
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.clickhouse.score_writer import ScoreWriter
from tracely.infrastructure.clickhouse.trace_reader import TraceReader
from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.llm import provider
from tracely.services.introspection_service import record
from tracely.services.structural_clustering_service import StructuralClusteringService

log = structlog.get_logger()

OnResult = Callable[[dict], None]


def _execution_mode(spec: dict) -> str:
    """The evaluator's item-ordering mode. Unknown/legacy values are safely batch."""
    mode = str((spec.get("config") or {}).get("execution_mode") or "batch").lower()
    return "sequential" if mode == "sequential" else "batch"


# The table calls its three levels conversation / message / step, so the recording does too — a
# recording that says "turn" or "AGENT_RUN" for the row the table labels `M` makes the reader
# translate between two vocabularies for the same thing.
_LEVEL_NAME = {
    "CONVERSATION": "conv",
    "AGENT_RUN": "msg",
    "SPAN": "step",
    "TOOL": "step",
    "GENERATION": "step",
    "CHAIN": "step",
}


def level_name(level: str) -> str:
    return _LEVEL_NAME.get(str(level).upper(), str(level or "?").lower())


def _spec_json(spec: dict) -> str:
    """What an evaluator column IS, as an object — the recording's input for that column.

    An object rather than the old `llm_judge · msg · basic` line for the sake of the checks that
    make no LLM call: a structural column has no nested prompt/reply to open, so this descriptor is
    the only thing on its row that explains what ran. The table renders anything that parses as
    JSON, so both kinds of column now read the same way.
    """
    cfg = spec.get("config") or {}
    out: dict[str, object] = {
        "evaluator": spec.get("score_name", ""),
        "kind": spec.get("kind", ""),
        "level": level_name(spec.get("level", "")),
        "mode": _execution_mode(spec),
    }
    if spec.get("kind") == "llm_judge":
        out["prompt"] = "advanced" if cfg.get("is_advanced") else "basic"
        out["output_type"] = str(cfg.get("output_type") or "score")
        if cfg.get("model"):
            out["model"] = cfg["model"]
    elif cfg.get("check"):
        out["check"] = cfg["check"]
    if cfg.get("threshold") is not None:
        out["threshold"] = cfg["threshold"]
    if cfg.get("advisory"):
        out["advisory"] = True
    if cfg.get("depends_on"):
        out["depends_on"] = cfg["depends_on"]
    return json.dumps(out, indent=2, ensure_ascii=False)


def _results_json(results: list[EvalResult]) -> str:
    """A column's verdict(s) as an object — an array when a step-level column graded several spans.

    `string_value` is re-parsed rather than embedded as a string so a `json` judge's own schema
    shows as nested fields instead of escaped quotes.
    """
    items = []
    for r in results:
        item: dict[str, object] = {
            "verdict": r.verdict or None,
            "value": r.value,
            "reason": r.comment or None,
            "span_id": r.target_span_id or None,
            "output": introspection.json_value(r.string_value) if r.string_value else None,
            "usage": r.usage or None,
        }
        items.append({k: v for k, v in item.items() if v is not None})
    if not items:
        return ""
    return json.dumps(items[0] if len(items) == 1 else items, indent=2, ensure_ascii=False)


def _subject_label(ctx) -> str:
    """What is being graded, in words — the recording's title line. Falls back to the id, but the
    id alone was the whole complaint: a row reading `0000…1f1ffee` tells you nothing."""
    from tracely.domain.evaluation.text import content_text

    text = (content_text(ctx.root.get("input")) or "").strip()
    where = "conversation" if not ctx.trace_id else "message"
    subject = ctx.trace_id or ctx.thread_id
    return f"grading {where} {subject}" + (f"\n\n{text[:600]}" if text else "")


def _chain_payload(score: dict) -> dict:
    """A persisted score row → the compact context that seeds the NEXT turn in sequential mode
    (one rendering for the whole chain: `results.chain_payload`)."""
    return chain_payload(
        value=score.get("value"), verdict=score.get("verdict") or "",
        comment=score.get("comment") or "", string_value=score.get("string_value") or "",
    )


def _with_previous(spec: dict, chain: dict[str, dict]) -> dict:
    """A copy of `spec` carrying the previous turn's result of the same metric (no-op when the
    metric hasn't produced one yet — the first item simply grades without chain context)."""
    prev = chain.get(spec["score_name"])
    if prev is None:
        return spec
    return {**spec, "config": {**(spec.get("config") or {}), CFG_PREVIOUS: prev}}


def _topo_sort(specs: list[dict]) -> list[dict]:
    """Reorder specs so `depends_on` dependencies run before their dependents.
    Falls back to original order when a cycle is detected (logged as a warning)."""
    from collections import deque
    by_name = {s["score_name"]: s for s in specs}
    dependents: dict[str, list[str]] = {s["score_name"]: [] for s in specs}
    in_degree: dict[str, int] = {}
    for s in specs:
        deps = [d for d in ((s.get("config") or {}).get("depends_on") or []) if d in by_name]
        in_degree[s["score_name"]] = len(deps)
        for dep in deps:
            dependents[dep].append(s["score_name"])
    queue = deque(s for s in specs if in_degree[s["score_name"]] == 0)
    ordered: list[dict] = []
    while queue:
        spec = queue.popleft()
        ordered.append(spec)
        for dep_name in dependents[spec["score_name"]]:
            in_degree[dep_name] -= 1
            if in_degree[dep_name] == 0:
                queue.append(by_name[dep_name])
    if len(ordered) != len(specs):
        log.warning("eval_dependency_cycle", evaluators=[s["score_name"] for s in specs])
        return specs
    return ordered


def _inject_dependencies(spec: dict, completed: dict[str, list[dict]]) -> dict:
    """Return a copy of spec with its declared dependencies' results injected as
    `config.__dependencies__` — `{score_name: [{span_id, payload}, ...]}`. Only deps that have
    already produced results are included; the evaluator resolves the per-span slice it needs."""
    depends_on = (spec.get("config") or {}).get("depends_on") or []
    deps = {name: completed[name] for name in depends_on if name in completed}
    if not deps:
        return spec
    return {**spec, "config": {**(spec.get("config") or {}), CFG_DEPENDENCIES: deps}}


def _needs_thread_context(specs: list[dict]) -> bool:
    """True when a message/step-level eval needs the WHOLE thread fetched (one extra read), which
    is two cases:

    - an advanced llm_judge referencing a conversation-scoped variable (`@HISTORY`/`@MESSAGES`/
      `@PREVIOUS_*`/`@GOAL`/`@LIST_AGENT`). Purely step-local advanced columns (only
      `@CURRENT_STEP.*` / `@METRIC_PREVIOUS_RESULT`) pay nothing;
    - a sequential MESSAGE-level judge, which grades this message in the light of the earlier
      turns — without the thread it would fall back to grading it alone, i.e. batch. (A
      sequential STEP judge chains within its own message and never reads the thread.)
    """
    return any(
        s.get("kind") == "llm_judge"
        and (
            (_execution_mode(s) == "sequential" and s.get("level") == RUN)
            or (
                (s.get("config") or {}).get("is_advanced")
                and references_conversation_scope((s.get("config") or {}).get("template_variables"))
            )
        )
        for s in specs
    )


class EvaluationService:
    """Online evaluation orchestrator. Stateless across calls; lazy-constructs the trace
    reader / score writer / structural clusterer."""

    def __init__(
        self,
        trace_reader: TraceReader | None = None,
        score_writer: ScoreWriter | None = None,
        registry=default_registry,
    ) -> None:
        self.trace_reader = trace_reader or TraceReader()
        self._score_writer = score_writer
        self.registry = registry

    # Lazy on purpose: building the writer reads `trace_reader.client`, and *that* property is what
    # opens the ClickHouse socket. Doing it in __init__ made merely constructing the service a
    # connection attempt, so every unit test that instantiates it (directly or via a Celery task)
    # needed a live ClickHouse — defeating TraceReader's deliberate laziness.
    @property
    def score_writer(self) -> ScoreWriter:
        if self._score_writer is None:
            self._score_writer = ScoreWriter(self.trace_reader.client)
        return self._score_writer

    @score_writer.setter
    def score_writer(self, writer: ScoreWriter) -> None:
        self._score_writer = writer

    def evaluate_trace(
        self,
        project_id: str,
        trace_id: str,
        specs: list[dict] | None = None,
        on_result: OnResult | None = None,
        skip_conversation: bool = False,
        thread_spans: list[dict] | None = None,
        execution_mode: str | None = None,
        apply_targeting: bool = False,
    ) -> dict:
        spans = self.trace_reader.read_spans(project_id, trace_id)
        if not spans:
            return {"scores": 0}
        # Second guard on the infinite loop (the first is at ingest, which never schedules these).
        # Both are needed: a monitor, a manual re-run, or a backfill can call this directly, and
        # grading a recording of a grading would record another one, without end.
        if any(s.get("internal_kind") for s in spans):
            return {"scores": 0, "skipped": "internal"}
        root = root_span(spans)
        agent_run_id = root.get("agent_run_id") or trace_id
        thread_id = next((s.get("conversation_id") for s in spans if s.get("conversation_id")), "") or trace_id
        if specs is None:
            # Auto (on-ingest) run: honor each evaluator's targeting + sampling. An explicit
            # on-demand run passes `specs` and always grades (the user chose them).
            specs = self._apply_targeting(
                project_id, self.load_enabled_evaluators(project_id), root, trace_id
            )
        elif apply_targeting:
            # `evaluate_thread` reuses an already-loaded list to avoid treating its automatic
            # settled-thread pass like an on-demand run. Keep the same targeting guarantee that
            # the ordinary ingest path has, but decide it against this individual trace.
            specs = self._apply_targeting(project_id, specs, root, trace_id)
        trace_specs = [s for s in specs if s["level"] != CONVERSATION]
        if execution_mode is not None:
            trace_specs = [s for s in trace_specs if _execution_mode(s) == execution_mode]
        conv_specs = [] if skip_conversation else [s for s in specs if s["level"] == CONVERSATION]

        # Advanced judges that read @HISTORY/@PREVIOUS_* need the whole thread (one extra read,
        # gated). `evaluate_thread` passes `thread_spans` in so the per-turn loop fetches once.
        if thread_spans is None and thread_id != trace_id and _needs_thread_context(trace_specs):
            thread_spans = self.trace_reader.read_thread_spans(project_id, thread_id)

        ctx = RunContext(
            project_id, trace_id, agent_run_id, spans, root,
            thread_id=thread_id, thread_spans=thread_spans,
        )
        results = self._dispatch_specs(trace_specs, ctx)
        if results:
            self.score_writer.write_eval_scores(
                project_id, trace_id, agent_run_id, results, thread_id=thread_id
            )
            self._emit(on_result, results, trace_id=trace_id, thread_id=thread_id)
            self._milestone_first_check(project_id, spans)

        fail_results = [r for r in results if r.verdict == "FAIL"]
        if fail_results and root.get("agent_id"):
            self._cluster_failure(project_id, root["agent_id"], trace_id, fail_results, spans)
        if fail_results:
            self._notify_trace_failed(project_id, trace_id, fail_results)

        conv_count = 0
        if conv_specs:
            conv_count = self._evaluate_conversation(project_id, thread_id, conv_specs, on_result)

        log.info(
            "evaluated", trace_id=trace_id, scores=len(results) + conv_count, failures=len(fail_results)
        )
        return {
            "scores": len(results) + conv_count,
            "failures": len(fail_results),
            "thread_id": thread_id,
            # Is the deferred whole-thread pass worth scheduling at all? It exists for two kinds of
            # column — CONVERSATION-level ones, and sequential ones that need the turns in order —
            # so it is only wasted work when the project has neither. The specs are already loaded
            # here, so answering costs nothing and saves a queued task plus its DB round-trip on
            # every message of the common batch-only setup.
            "needs_thread_pass": any(
                s["level"] == CONVERSATION or _execution_mode(s) == "sequential" for s in specs
            ),
        }

    def evaluate_thread(
        self,
        project_id: str,
        thread_id: str,
        specs: list[dict] | None = None,
        on_result: OnResult | None = None,
        execution_mode: str | None = None,
    ) -> dict:
        """Evaluate a whole conversation row: turns with the trace/span-level evaluators, then
        the conversation-level evaluators once across the full thread.

        Metrics with `config.execution_mode == "sequential"` chain across the turns: each
        trace's run receives the previous turn's result of the SAME metric (injected as
        `CFG_PREVIOUS`; within a turn the judge chains its own steps).

        Sequential grading is INCREMENTAL on the automatic (ingest-debounced) pass: each metric's
        progress through the thread is persisted (`eval_chain_progress`), so a settle only grades
        the turns that arrived since the last pass, appending them to the column's durable
        conversation. The stored prefix no longer matching the thread's turn order (a
        late-arriving trace re-sorted the turns) resets the conversation and rebuilds from
        turn 1. An explicit run (the UI's Play button, specs passed in) always rebuilds — its
        intent is "re-grade everything now".

        The whole sequential pass holds a per-thread Redis lock so the debounced task and an
        on-demand run can't interleave one conversation; see `queue/thread_lock.py`.
        """
        automatic = specs is None
        if specs is None:
            specs = self.load_enabled_evaluators(project_id)
        trace_specs = [s for s in specs if s["level"] != CONVERSATION]
        if execution_mode is not None:
            trace_specs = [s for s in trace_specs if _execution_mode(s) == execution_mode]
        conv_specs = [s for s in specs if s["level"] == CONVERSATION]
        total = failures = 0
        if trace_specs:
            sequential_names = {
                s["score_name"] for s in trace_specs if _execution_mode(s) == "sequential"
            }
            if sequential_names:
                from tracely.infrastructure.queue.thread_lock import thread_pass_lock

                with thread_pass_lock(project_id, thread_id):
                    total, failures = self._run_turn_pass(
                        project_id, thread_id, trace_specs, sequential_names,
                        on_result, automatic,
                    )
            else:
                total, failures = self._run_turn_pass(
                    project_id, thread_id, trace_specs, set(), on_result, automatic
                )
        if conv_specs:
            total += self._evaluate_conversation(
                project_id, thread_id, conv_specs, on_result, apply_targeting=automatic
            )
        return {"scores": total, "failures": failures}

    def _run_turn_pass(
        self,
        project_id: str,
        thread_id: str,
        trace_specs: list[dict],
        sequential_names: set[str],
        on_result: OnResult | None,
        automatic: bool,
    ) -> tuple[int, int]:
        """The ordered pass over a thread's turns — the one context where a message-level judge
        may extend its durable conversation (`CFG_CHAIN_PASS`; a lone re-grade of one mid-thread
        turn appending out of order is exactly what the marker forbids).

        Each sequential metric starts at its persisted progress (incremental) or at turn 1 after
        a reset (first run, order change, or a forced full run); batch specs run on every turn.
        """
        turn_ids = self.trace_reader.thread_trace_ids(project_id, thread_id)
        if not turn_ids:
            return 0, 0
        starts: dict[str, int] = {}
        chain: dict[str, dict] = {}
        if sequential_names:
            trace_specs = [
                {**s, "config": {**(s.get("config") or {}), CFG_CHAIN_PASS: True}}
                if s["score_name"] in sequential_names
                else s
                for s in trace_specs
            ]
            run_level = {
                s["score_name"] for s in trace_specs
                if s["score_name"] in sequential_names and s["level"] == RUN
            }
            starts = self._chain_starts(
                project_id, thread_id, sequential_names, run_level, turn_ids, chain,
                incremental=automatic,
            )

        def capture(score: dict) -> None:
            if score["name"] in sequential_names:
                chain[score["name"]] = _chain_payload(score)
            if on_result is not None:
                on_result(score)

        # Fetch the thread's spans ONCE (not once per turn) when an advanced judge needs the
        # full transcript — `evaluate_trace` reuses what we pass instead of re-reading.
        thread_spans = (
            self.trace_reader.read_thread_spans(project_id, thread_id)
            if _needs_thread_context(trace_specs) else None
        )
        total = failures = 0
        for i, tid in enumerate(turn_ids):
            # A sequential metric joins at its start index; everything else runs on every turn.
            due = [
                s for s in trace_specs
                if s["score_name"] not in sequential_names or starts[s["score_name"]] <= i
            ]
            if not due:
                continue
            staged = [_with_previous(s, chain) for s in due]
            r = self.evaluate_trace(
                project_id, tid, specs=staged, on_result=capture, skip_conversation=True,
                thread_spans=thread_spans, apply_targeting=automatic,
            )
            total += r.get("scores", 0)
            failures += r.get("failures", 0)
            # Record progress per graded turn (not per pass) so a crashed pass resumes from the
            # last turn it finished instead of re-appending everything to the conversation.
            graded = [n for n in sequential_names if starts[n] <= i]
            if graded:
                try:
                    with SyncSessionLocal() as sess:
                        for name in graded:
                            repositories.chain_progress_set(
                                sess, project_id, name, thread_id,
                                turn_ids[: i + 1], chain.get(name),
                            )
                except Exception as exc:  # progress is an optimization, never a blocker
                    log.warning("chain_progress_write_failed", thread_id=thread_id, error=str(exc))
        return total, failures

    def _chain_starts(
        self,
        project_id: str,
        thread_id: str,
        sequential_names: set[str],
        run_level: set[str],
        turn_ids: list[str],
        chain: dict[str, dict],
        incremental: bool,
    ) -> dict[str, int]:
        """Where each sequential metric's pass begins, resetting whatever can't be continued.

        A metric continues (start = however many turns its durable conversation already holds,
        `chain` seeded with its last payload) iff we're on the automatic incremental path and its
        stored turn list is a prefix of the thread's current turn order. Anything else — first
        run, a late-arriving trace re-sorting the turns, or a forced full run — starts at 0 with
        a fresh conversation: message-level chats reset here, step-level chats reset per trace
        inside `_run_steps`, and the stale progress rows are cleared."""
        progress: dict[str, dict] = {}
        if incremental:
            try:
                with SyncSessionLocal() as sess:
                    progress = repositories.chain_progress_load(sess, project_id, thread_id)
            except Exception as exc:  # no progress ⇒ full rebuild, which is always safe
                log.warning("chain_progress_load_failed", thread_id=thread_id, error=str(exc))
        from tracely.infrastructure.llm.checkpointer import chat_id, reset_chat

        starts: dict[str, int] = {}
        stale: list[str] = []
        for name in sorted(sequential_names):
            done = (progress.get(name) or {}).get("turn_ids") or []
            if incremental and done and done == turn_ids[: len(done)]:
                starts[name] = len(done)
                payload = (progress.get(name) or {}).get("last_payload")
                if isinstance(payload, dict):
                    chain[name] = payload
                continue
            starts[name] = 0
            if done or not incremental:
                stale.append(name)
            if name in run_level:
                reset_chat(chat_id(project_id, name, thread_id))
        if stale:
            try:
                with SyncSessionLocal() as sess:
                    repositories.chain_progress_clear(sess, project_id, thread_id, stale)
            except Exception as exc:
                log.warning("chain_progress_clear_failed", thread_id=thread_id, error=str(exc))
        return starts

    def _apply_targeting(
        self, project_id: str, specs: list[dict], root: dict, trace_id: str
    ) -> list[dict]:
        """Drop evaluators whose target_agent/target_env don't match this trace, then roll each
        one's sampling die. The agent slug is resolved only when some evaluator actually targets
        an agent (avoids a DB hit on the common no-targeting path)."""
        agent_id = root.get("agent_id") or ""
        env = root.get("env") or ""
        agent_slug = ""
        if agent_id and any((s.get("target_agent") or "").strip() for s in specs):
            try:
                with SyncSessionLocal() as sess:
                    agent_slug = repositories.agent_slug(sess, project_id, agent_id)
            except Exception as exc:  # slug lookup must never break evaluation
                log.warning("agent_slug_lookup_failed", agent_id=agent_id, error=str(exc))
        return [
            s
            for s in specs
            if spec_applies(
                s, agent_id=agent_id, agent_slug=agent_slug, env=env, trace_id=trace_id
            )
        ]

    # ── answer-quality grading for the CI gate (non-persisting) ─────────────────

    def quality_specs(
        self, project_id: str, only_names: list[str] | None = None
    ) -> list[dict]:
        """The answer-quality judge(s) the gate replays — enabled AGENT_RUN-level LLM judges.

        Narrowed (in priority order) to: the explicit `only_names` a case recorded it was promoted
        from; else the single canonical answer-quality judge (`settings.gate_quality_score_name`)
        so the gate enforces "the answer is wrong/unfaithful", NOT every AGENT_RUN judge (some are
        signal detectors with inverted thresholds — blocking on those is noise). Blank config →
        every AGENT_RUN llm_judge (legacy broad behavior)."""
        specs = [
            s
            for s in self.load_enabled_evaluators(project_id)
            if s["kind"] == "llm_judge" and s["level"] == RUN
        ]
        if only_names:
            wanted = set(only_names)
            specs = [s for s in specs if s["score_name"] in wanted]
        elif settings.gate_quality_score_name:
            specs = [s for s in specs if s["score_name"] == settings.gate_quality_score_name]
        return specs

    def grade_trace_quality(
        self,
        project_id: str,
        spans: list[dict],
        only_names: list[str] | None = None,
        specs: list[dict] | None = None,
    ) -> list[EvalResult]:
        """Run answer-quality judge(s) on an arbitrary set of spans (a promote source trace or a
        replayed gate trace) WITHOUT persisting — returns the `EvalResult`s so the caller can
        gate on them. Empty when there are no quality judges, no spans, or grading fails (a
        missing LLM key degrades to structural-only, never a false fail)."""
        if specs is None:
            specs = self.quality_specs(project_id, only_names)
        if not spans or not specs:
            return []
        root = root_span(spans)
        ctx = RunContext(
            project_id,
            root.get("trace_id", "") or "",
            root.get("agent_run_id", "") or "",
            spans,
            root,
        )
        return self._dispatch_specs(specs, ctx)

    # ── internals ─────────────────────────────────────────────────────────────

    def evaluate_conversation(
        self,
        project_id: str,
        thread_id: str,
        on_result: OnResult | None = None,
        apply_targeting: bool = True,
    ) -> dict:
        """Grade a thread with its CONVERSATION-level evaluators, once.

        `apply_targeting=False` grades with every enabled column regardless of target/sampling —
        for explicit runs (the CI gate) where thinning the suite would silently weaken it.

        Split out of `evaluate_trace` because it must NOT run per turn: the ingest path calls
        `evaluate_trace` for every turn, so an inline conversation pass re-graded the whole thread
        on each one — the same judge, the same transcript, N times, N times the spend, N identical
        rows in the recording. Callers run this once after the thread settles (the ingest hop
        debounces it per thread; the gate calls it after driving every turn).
        """
        specs = [
            s for s in self.load_enabled_evaluators(project_id) if s["level"] == CONVERSATION
        ]
        if not specs:
            return {"scores": 0}
        return {
            "scores": self._evaluate_conversation(
                project_id, thread_id, specs, on_result, apply_targeting=apply_targeting
            )
        }

    def _evaluate_conversation(
        self,
        project_id: str,
        thread_id: str,
        specs: list[dict],
        on_result: OnResult | None,
        apply_targeting: bool = False,
    ) -> int:
        """Run CONVERSATION-level specs over every span in the thread; persist thread-scoped."""
        spans = self.trace_reader.read_thread_spans(project_id, thread_id)
        if not spans:
            return 0
        if apply_targeting:
            specs = self._apply_conversation_targeting(project_id, specs, spans, thread_id)
        if not specs:
            return 0
        ctx = RunContext(project_id, "", "", spans, root_span(spans), thread_id=thread_id)
        results = self._dispatch_specs(specs, ctx)
        # This pass writes with NO trace_id, so only CONVERSATION-level results are addressable
        # (readers find them by session_id). A span-scoped evaluator misconfigured to CONVERSATION
        # level — `tool_success` is one, its results stay TOOL-level — would otherwise be written
        # with an empty trace_id: rows no query in the system can reach, silently. Drop and say so.
        keep = [r for r in results if r.level == CONVERSATION]
        if len(keep) != len(results):
            log.warning(
                "conversation_pass_dropped_span_scoped_results",
                thread_id=thread_id,
                evaluators=sorted({r.name for r in results if r.level != CONVERSATION}),
            )
        if keep:
            self.score_writer.write_eval_scores(project_id, "", "", keep, thread_id=thread_id)
            self._emit(on_result, keep, trace_id="", thread_id=thread_id)
        return len(keep)

    def _apply_conversation_targeting(
        self, project_id: str, specs: list[dict], spans: list[dict], thread_id: str
    ) -> list[dict]:
        """Target a whole-thread evaluator once, deterministically.

        A conversation can include several agent runs. Agent/environment targeting therefore
        matches when *any* turn belongs to the selected target, while sampling is keyed by the
        thread id because the conversation — not an arbitrary turn — is the subject being scored.
        """
        by_trace: dict[str, list[dict]] = {}
        for span in spans:
            by_trace.setdefault(span.get("trace_id") or "", []).append(span)
        roots = [root_span(trace_spans) for trace_spans in by_trace.values()]
        slug_by_agent: dict[str, str] = {}

        def slug_for(agent_id: str) -> str:
            if not agent_id:
                return ""
            if agent_id not in slug_by_agent:
                try:
                    with SyncSessionLocal() as sess:
                        slug_by_agent[agent_id] = repositories.agent_slug(sess, project_id, agent_id)
                except Exception as exc:
                    log.warning("agent_slug_lookup_failed", agent_id=agent_id, error=str(exc))
                    slug_by_agent[agent_id] = ""
            return slug_by_agent[agent_id]

        applicable: list[dict] = []
        for spec in specs:
            target_only = {**spec, "sampling": 1.0}
            target_matches = any(
                spec_applies(
                    target_only,
                    agent_id=root.get("agent_id") or "",
                    agent_slug=slug_for(root.get("agent_id") or ""),
                    env=root.get("env") or "",
                    trace_id=thread_id,
                )
                for root in roots
            )
            if not target_matches:
                continue
            # Reuse the canonical deterministic sampler after separating it from target matching.
            sample_only = {**spec, "target_agent": "", "target_env": ""}
            if spec_applies(sample_only, agent_id="", agent_slug="", env="", trace_id=thread_id):
                applicable.append(spec)
        return applicable

    def _dispatch_specs(self, specs: list[dict], ctx: RunContext) -> list[EvalResult]:
        specs = _topo_sort(specs)
        # Each completed evaluator's results, kept per `span_id` so a dependent grading at step
        # level can line up THIS step's prerequisite verdicts (a trace/conversation result lands
        # under span_id "" — see `_inject_dependencies` / the judge's per-span resolution).
        completed: dict[str, list[dict]] = {}
        results: list[EvalResult] = []
        subject = ctx.trace_id or ctx.thread_id
        # THE chokepoint every eval path funnels through (on-ingest, on-demand run, gate quality
        # grading) — scoping here once covers every llm_judge call this dispatch makes, and the
        # recording captures each of those calls' prompt and reply without per-evaluator wiring.
        # The three eval levels land on the traces table's three levels: every recording about
        # one conversation shares a namespaced conversation id, so a 3-turn thread shows as ONE
        # row with 4 runs (turn 1, 2, 3, and the conversation-level pass) rather than 4 orphans.
        thread = ctx.thread_id or ctx.trace_id
        with provider.use_project_key(ctx.project_id), record(
            introspection.EVAL, subject,
            # `{level}`/`{n}` are filled per emitted trace: one dispatch grades message columns
            # AND step columns, and they land as two rows, each titled for what it actually did.
            "eval · {level} · {n} column(s)", project_id=ctx.project_id,
            subject_label=_subject_label(ctx),
            conversation_id=f"eval:{thread}" if thread else "",
            # A recording REPLACES the last one about the same (subject, level) rather than
            # stacking beside it. Every eval path re-runs: the conversation pass re-grades the
            # thread each time it grows, ingest re-grades a turn whose spans arrived late, and the
            # UI's "Run evals" re-grades on demand. Without this a 3-turn conversation showed 9
            # step runs and the newest verdict was whichever row you happened to open.
            stable=True,
        ) as rec:
            for spec in specs:
                spec = _inject_dependencies(spec, completed)
                if rec:
                    # One span per evaluator, named for the column, describing WHICH kind at
                    # WHAT level — "why did this column say that" starts with knowing what it is.
                    rec.label = spec.get("score_name") or spec.get("kind") or "evaluator"
                    rec.describe(input=_spec_json(spec), meta={
                        "evaluator": spec.get("score_name", ""),
                        # The table's word for it (msg/step/conv), not the enum — and the key the
                        # recording splits its traces on, so a row is one level throughout.
                        "level": level_name(spec.get("level", "")),
                        "kind": spec.get("kind", ""),
                    })
                try:
                    new_results = self.registry.dispatch(
                        spec["kind"], spec["config"], spec["score_name"], spec["level"], ctx
                    )
                    if rec:
                        # The verdict lands ON the evaluator's own span rather than in a child
                        # "verdict" event — one row per evaluator, not two.
                        rec.describe(
                            output=_results_json(new_results)
                            or "(no result — not applicable to this trace)"
                        )
                    results.extend(new_results)
                    if new_results:
                        completed[spec["score_name"]] = [
                            {
                                "span_id": r.target_span_id,
                                "payload": chain_payload(
                                    value=r.value, verdict=r.verdict,
                                    comment=r.comment, string_value=r.string_value,
                                ),
                            }
                            for r in new_results
                        ]
                except Exception as exc:  # one bad evaluator must not sink the rest
                    if rec:
                        # As the output too, not only the span's error status: the status colours
                        # the row, the output is the cell you actually read.
                        rec.describe(
                            output=json.dumps({"error": str(exc)[:2000]}, indent=2, ensure_ascii=False),
                            error=str(exc)[:500],
                        )
                    log.warning(
                        "evaluator_failed", evaluator=spec.get("score_name", "?"), error=str(exc)
                    )
        return results

    @staticmethod
    def _emit(
        on_result: OnResult | None, results: list[EvalResult], *, trace_id: str, thread_id: str
    ) -> None:
        if on_result is None:
            return
        for r in results:
            try:
                on_result({
                    "name": r.name,
                    "evaluation_level": r.level,
                    "observation_id": r.target_span_id or None,
                    "value": r.value,
                    "string_value": r.string_value,
                    "verdict": r.verdict,
                    "comment": r.comment,
                    "data_type": r.data_type,
                    "trace_id": None if r.level == CONVERSATION else trace_id,
                    "session_id": thread_id or None,
                })
            except Exception as exc:  # a slow/broken consumer must not sink the run
                log.warning("eval_emit_failed", error=str(exc))

    @staticmethod
    def load_enabled_evaluators(
        project_id: str, evaluator_ids: list[str] | None = None
    ) -> list[dict]:
        """The evaluators to run: the project's enabled `Evaluator` records (optionally narrowed
        to `evaluator_ids`). With none configured, online evaluation is a no-op — evaluators are
        opt-in, not auto-run."""
        try:
            with SyncSessionLocal() as s:
                return repositories.evaluator_enabled_specs(s, project_id, evaluator_ids)
        except Exception as exc:  # table missing / DB hiccup -> no evals
            log.warning("evaluator_load_failed", error=str(exc))
            return []

    @staticmethod
    def _milestone_first_check(project_id: str, spans: list[dict]) -> None:
        """`first_check_completed` — a verdict was actually written for a real trace."""
        try:
            from tracely.services import milestones

            with SyncSessionLocal() as s:
                milestones.record(
                    s, project_id, "first_check_completed",
                    sample=milestones.is_sample(spans), integration=milestones.integration_of(spans),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("milestone_first_check_failed", error=str(exc))

    @staticmethod
    def _notify_trace_failed(project_id: str, trace_id: str, fail_results: list[EvalResult]) -> None:
        """Fire the "a conversation just broke" event monitors (`/settings/alerts`).

        Advisory evaluators are excluded for the same reason they never flip a verdict
        (`domain/evaluation/verdict.py`): an advisory FAIL is not a failing turn, and paging on one
        would make the alert disagree with the badge. Best-effort in its own session — a flaky
        Slack webhook must not fail the eval that just wrote the score."""
        try:
            from tracely.services.alert_events import trace_event
            from tracely.services.monitoring_service import notify_event

            with SyncSessionLocal() as s:
                advisory = set(repositories.advisory_score_names(s, project_id))
                failing = [
                    {"name": r.name, "comment": r.comment}
                    for r in fail_results
                    if r.name not in advisory
                ]
                if not failing:
                    return
                # One builder, shared with the `/test` path — a test that assembled its own
                # context would be a test of the wrong thing.
                notify_event(project_id, trace_event(s, project_id, trace_id, failing=failing))
        except Exception as exc:
            log.warning("monitor_notify_failed", trace_id=trace_id, error=str(exc))

    @staticmethod
    def _cluster_failure(
        project_id: str,
        agent_id: str,
        trace_id: str,
        fail_results: list[EvalResult],
        spans: list[dict],
    ) -> None:
        """Cheap structural clustering — runs in its own session so a clustering hiccup never
        breaks the eval insert. Exceptions are swallowed (logged) for the same reason."""
        try:
            with SyncSessionLocal() as s:
                StructuralClusteringService(s).cluster_failure(
                    project_id, agent_id, trace_id, fail_results, spans
                )
        except Exception as exc:
            log.warning("cluster_failed", trace_id=trace_id, error=str(exc))
