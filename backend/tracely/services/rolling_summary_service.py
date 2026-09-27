"""Rolling-summary generation: the per-span accumulate loop over a thread.

The summary is a flat JSON LIST of items — `[{role, type, content, …}, …]`:

  RULE 1 — represent the step: a step ≤ `step_max_tokens` (512) is appended VERBATIM to the list;
           a larger step is summarized to ~10-20 words by `rolling_summary_agent` first.
  RULE 2 — keep the whole summary under `max_tokens` (the workspace budget, default 16k): when the list exceeds the budget, fold
           the older items (everything but the last 2) into ONE compacted item with the
           `prev_summary` role (`{role:"prev_summary", type:"summary", content}`) at the front, and
           keep only the last 2 items verbatim. Recursive, progress-guarded.

So the recent turns stay structured/verbatim and only the distant past collapses into a single
`prev_summary` item. One row per span holds the full accumulated list at that point — conversation
view = last row, message view = its turn's last step, step view = that exact row. Idempotent +
incremental (one up-front read seeds the skip-cache), so the ingest hook re-runs cheaply.
`history_override` renders the list to the `@HISTORY` string. Legacy object-shaped rows are tolerated
via `as_summary_items`.

Sync (same `TraceReader` + `SyncSessionLocal` as the eval engine); the API/worker run it in a
threadpool / task.
"""

from __future__ import annotations

import structlog

from tracely.config import settings
from tracely.domain.evaluation.rolling_summary import (
    SummaryBudget,
    as_summary_items,
    compacted_item,
    components_token_total,
    decompose_turn_span,
    dedup_consecutive,
    format_summary_as_history,
    items_from_components,
    summary_budget,
    summary_token_total,
    user_input_for_turn,
)
from tracely.domain.traces.spans import root_span
from tracely.infrastructure.clickhouse.trace_reader import TraceReader
from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.infrastructure.llm import provider
from tracely.infrastructure.llm.rolling_summary_agent import compact_items, summarize_components

log = structlog.get_logger()

_KEEP_LAST = 2  # the most-recent items always stay verbatim during compaction
_COMPACT_MAX_PASSES = 6  # recursion backstop (a huge un-shrinkable tail can't loop forever)


def _group_turns(spans: list[dict]) -> list[tuple[str, list[dict]]]:
    """Spans grouped by trace_id in first-seen (start_time) order."""
    by_trace: dict[str, list[dict]] = {}
    order: list[str] = []
    for s in spans:
        tid = s.get("trace_id") or ""
        if tid not in by_trace:
            by_trace[tid] = []
            order.append(tid)
        by_trace[tid].append(s)
    return [(tid, by_trace[tid]) for tid in order]


def _naive_compact(text: str, budget_tokens: int) -> str:
    """Fallback compaction when no LLM is configured: bound the head to ~half the budget so the
    summary still shrinks (lossy, but keeps the budget invariant)."""
    max_chars = max(400, budget_tokens * 2)  # ≈ budget/2 tokens
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


class RollingSummaryService:
    def __init__(self, trace_reader: TraceReader | None = None) -> None:
        self.trace_reader = trace_reader or TraceReader()

    def build_for_thread(
        self, project_id: str, thread_id: str, *, force: bool = False, source: str = "on_demand"
    ) -> dict:
        """Generate (or refresh with `force`) the per-span rolling summary for a thread. Idempotent
        + incremental. Returns the final accumulated item list + counts."""
        spans = self.trace_reader.read_thread_spans(project_id, thread_id)
        if not spans:
            return {"thread_id": thread_id, "steps": 0, "items": [], "token_count": 0, "llm_steps": 0}

        # flatten to thread order, tagging each span's trace + whether it's the turn's root wrapper,
        # and resolving each turn's user request once (root input, or a GENERATION-input fallback)
        flat: list[tuple[str, dict, bool, bool]] = []
        user_by_trace: dict[str, object] = {}
        for trace_id, tspans in _group_turns(spans):
            root_id = root_span(tspans).get("span_id")
            user_by_trace[trace_id] = user_input_for_turn(tspans, root_id)
            multi = len(tspans) > 1
            for span in tspans:
                flat.append((trace_id, span, span.get("span_id") == root_id, multi))

        running: list[dict] = []
        llm_steps = 0
        with provider.use_project_key(project_id), SyncSessionLocal() as s:
            budget = self.budget_for(s, project_id)
            if force:
                repositories.rolling_summary_delete_for_thread(s, project_id, thread_id)
                existing: dict[str, list] = {}
            else:
                # one read up front → in-memory idempotent skip (no per-span query)
                existing = {
                    r.span_id: as_summary_items(r.summary)
                    for r in repositories.rolling_summary_list_for_thread(s, project_id, thread_id)
                }

            for step_order, (trace_id, span, is_root, multi) in enumerate(flat):
                span_id = span.get("span_id") or ""
                if not span_id:
                    continue
                if span_id in existing:  # already summarized → carry its accumulated state forward
                    running = existing[span_id]
                    continue

                # decompose: the root contributes the user request (its answer is captured by the
                # child generation) unless single-span; non-root framework wrappers contribute nothing.
                comps = decompose_turn_span(
                    span, is_root=is_root, multi=multi, user_comp=user_by_trace.get(trace_id)
                )

                verbatim = True
                if comps:
                    # RULE 1 — represent the step (verbatim ≤512 tok, else LLM-summarize)
                    summaries = None
                    if (
                        components_token_total(comps) > budget.step_max_tokens
                        and provider.llm_enabled()
                    ):
                        summaries = summarize_components(comps)
                        if summaries is not None:
                            verbatim = False
                            llm_steps += 1
                    new_items = [it.model_dump(exclude_none=True) for it in items_from_components(comps, summaries)]
                    running = running + dedup_consecutive(running[-1] if running else None, new_items)

                # RULE 2 — keep the whole summary under budget (fold older items into a prev_summary item)
                running, compactions = self._compact_to_budget(running, budget.max_tokens)
                llm_steps += compactions

                repositories.rolling_summary_create(
                    s,
                    project_id,
                    thread_id=thread_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    step_order=step_order,
                    summary=running,
                    token_count=summary_token_total(running),
                    meta={
                        "model": "" if verbatim else settings.rolling_summary_model,
                        "verbatim": verbatim,
                        "step_order": step_order,
                        "source": source,
                        # which budget built this row — a later budget change leaves older rows
                        # as they were until the thread is regenerated
                        "budget": budget.to_json(),
                    },
                )

        return {
            "thread_id": thread_id,
            "steps": len(flat),
            "items": running,
            "token_count": summary_token_total(running),
            "llm_steps": llm_steps,
        }

    @staticmethod
    def budget_for(s, project_id: str) -> SummaryBudget:
        """This workspace's budget: its own settings over the server defaults."""
        return summary_budget(
            repositories.project_rolling_summary_config_get(s, project_id),
            default_max=settings.rolling_summary_max_tokens,
            default_step=settings.rolling_summary_step_max_tokens,
        )

    def _compact_to_budget(self, running: list[dict], budget: int | None = None) -> tuple[list[dict], int]:
        """While the list exceeds the token budget, fold the older items (all but the last 2) into one
        `prev_summary` item at the front and keep only the last 2 items verbatim. The fold absorbs any
        existing prev_summary item (it's part of the head). Recursive with a progress guard + pass cap.
        Returns (items, number_of_compactions)."""
        budget = budget or settings.rolling_summary_max_tokens
        compactions = 0
        passes = 0
        while (
            summary_token_total(running) > budget
            and len(running) > _KEEP_LAST
            and passes < _COMPACT_MAX_PASSES
        ):
            passes += 1
            head, tail = running[:-_KEEP_LAST], running[-_KEEP_LAST:]
            head_text = format_summary_as_history(head, max_chars=0)
            content = compact_items(head_text) if provider.llm_enabled() else None
            if not content:
                content = _naive_compact(head_text, budget)
            candidate = [compacted_item(content)] + tail
            if summary_token_total(candidate) >= summary_token_total(running):
                break  # no progress (couldn't shrink, or the last 2 items alone exceed budget)
            running = candidate
            compactions += 1
        return running, compactions

    @staticmethod
    def get_for_thread(project_id: str, thread_id: str) -> dict:
        """The conversation-level rolling summary item list (+ its @HISTORY rendering), or an empty
        list when none has been generated."""
        with SyncSessionLocal() as s:
            rows = repositories.rolling_summary_list_for_thread(s, project_id, thread_id)
        latest = rows[-1] if rows else None
        items = as_summary_items(latest.summary) if latest else []
        return {
            "thread_id": thread_id,
            "steps": len(rows),
            "items": items,
            "token_count": latest.token_count if latest else 0,
            "history": format_summary_as_history(items, max_chars=0),
            "generated_at": (
                latest.updated_at.isoformat() if latest and latest.updated_at else None
            ),
        }

    @staticmethod
    def by_level(project_id: str, thread_id: str) -> dict:
        """Per-row rolling-summary item LISTS for the table's 3 levels — `conversation` (whole
        thread), `traces[trace_id]` (through that turn), and `spans[span_id]` (through that step).
        Returned in full (no truncation — the frontend renders the JSON); the budget's compaction bounds
        size."""
        with SyncSessionLocal() as s:
            rows = repositories.rolling_summary_list_for_thread(s, project_id, thread_id)
        if not rows:
            return {"thread_id": thread_id, "steps": 0, "conversation": None, "traces": {}, "spans": {}}
        spans: dict[str, list] = {}
        traces: dict[str, list] = {}
        for r in rows:  # ordered by step_order → traces[tid] ends at that trace's last step
            items = as_summary_items(r.summary)
            spans[r.span_id] = items
            traces[r.trace_id] = items
        return {
            "thread_id": thread_id,
            "steps": len(rows),
            "conversation": as_summary_items(rows[-1].summary),
            "traces": traces,
            "spans": spans,
        }

    @staticmethod
    def history_override(project_id: str, thread_id: str, through_trace_id: str = "") -> str | None:
        """The `@HISTORY` string rendered from the summary (bounded by the budget, not clipped), or
        None when no summary exists (caller falls back to the raw transcript). Guarded — a
        lookup/format failure never blocks a grade.

        `through_trace_id` cuts it at the end of that turn — what a message/step judge must read.
        The thread's LATEST summary covers every later turn too, and handing that to the judge of
        turn 2 grades it with hindsight. Without it (conversation level) the latest row is right.
        A turn with no summary row yet reads as no summary (raw transcript), never a later one."""
        if not thread_id:
            return None
        try:
            with SyncSessionLocal() as s:
                if through_trace_id:
                    rows = repositories.rolling_summary_list_for_thread(s, project_id, thread_id)
                    mine = [r for r in rows if r.trace_id == through_trace_id]  # ordered by step_order
                    row = mine[-1] if mine else None
                else:
                    row = repositories.rolling_summary_latest_for_thread(s, project_id, thread_id)
            if row and row.summary:
                return format_summary_as_history(row.summary, max_chars=0) or None
        except Exception as exc:  # never break the eval path on a cache miss/error
            log.warning("rolling_summary_history_override_failed", error=str(exc))
        return None
