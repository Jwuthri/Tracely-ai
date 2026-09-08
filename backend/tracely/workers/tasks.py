"""Celery tasks: thin dispatch into the service classes.

`tracely_workers.worker` imports this module to register the tasks on the shared Celery app.
"""

from __future__ import annotations

import structlog
from celery import signals as celery_signals

from tracely.config import settings
from tracely.infrastructure.db import repositories
from tracely.infrastructure.queue import eval_debounce
from tracely.infrastructure.queue.celery_app import celery_app
from tracely.services import quota_service
from tracely.services.evaluation_service import EvaluationService
from tracely.services.failure_intel_service import FailureIntelService
from tracely.services.ingestion_service import IngestionService

log = structlog.get_logger()


@celery_signals.worker_ready.connect
def _warm_pricing_on_boot(**_kw) -> None:
    """Load OpenRouter's price catalog once the worker is up.

    This process is the one that prices spans (`IngestionService._attach_costs`), so a cold cache
    means the first OTLP batch after every restart either waits on the fetch or — if it times
    out — is written with no cost at all and never revisited. Best-effort; never blocks the worker.
    """
    from tracely.infrastructure.llm.provider import warm_pricing_catalog

    warm_pricing_catalog()


@celery_app.task(name="tracely.ingest_otlp_blob", bind=True, max_retries=6, default_retry_delay=5)
def ingest_otlp_blob(self, project_id: str, key: str, content_type: str) -> dict:
    try:
        result = IngestionService().process_blob(project_id, key, content_type)
        # Online evaluation: debounce per trace so a run whose spans span several OTLP batches is
        # evaluated ONCE after it goes quiet — not once per batch (wasted judge spend) and not on a
        # partial trace. Each batch bumps the trace's generation; the scheduled eval runs only if its
        # generation is still the latest when it fires (see infrastructure/queue/eval_debounce.py).
        # Never evaluate a recording of Tracely's own work — that would record another
        # evaluation, which would be evaluated, forever (`domain/introspection.py`).
        internal = set(result.get("internal_trace_ids") or ())
        for trace_id in result.get("trace_ids", []):
            if trace_id in internal:
                continue
            gen = eval_debounce.bump(project_id, trace_id)
            evaluate_run_task.apply_async(
                (project_id, trace_id, gen), countdown=settings.eval_debounce_seconds
            )
    except Exception as exc:  # transient failures -> retry with backoff
        log.warning("ingest_failed", key=key, error=str(exc))
        raise self.retry(exc=exc)

    # Hosted-cloud quota: count this batch's never-seen traces into the project's month. AFTER
    # (and outside) the try above on purpose — `record_ingested_traces` swallows its own
    # failures, and a counting hiccup must never trigger the task retry: the Redis seen-set
    # would already hold these ids, so the retry would lose the count AND re-ingest the batch.
    # This task is the only counting site, so inline emissions (scenario turns, recordings —
    # which call `process_blob` directly) never consume quota. No-op unless BILLING_ENABLED.
    quota_service.record_ingested_traces(project_id, result.get("trace_ids", []), internal)
    return {"events": result.get("events", 0)}


# The conversation debounce shares the per-trace generation counter, namespaced so a thread and a
# trace of the same id can never collide.
_CONV_KEY = "conv:{thread}"


@celery_app.task(name="tracely.evaluate_run", bind=True, max_retries=3, default_retry_delay=3)
def evaluate_run_task(self, project_id: str, trace_id: str, gen: int = 0) -> dict:
    # Debounce: skip if a newer batch for this trace arrived after we were scheduled — the later
    # task will evaluate the settled trace. `gen=0` is the ungated sentinel (always runs).
    if not eval_debounce.is_latest(project_id, trace_id, gen):
        return {"skipped": "superseded", "trace_id": trace_id}
    try:
        # Batch turn/step columns run as soon as this trace settles. Sequential message columns
        # deliberately wait for the debounced whole-thread pass below: they need the preceding
        # turn's result, so treating each ingest task as an isolated run would silently turn them
        # into batch evaluators.
        result = EvaluationService().evaluate_trace(
            project_id, trace_id, skip_conversation=True, execution_mode="batch"
        )
    except Exception as exc:
        raise self.retry(exc=exc)
    # Real-time rolling summary: fold this turn into the thread's accumulating summary. Incremental
    # (only new spans are summarized) and best-effort — a summary failure must never fail the run.
    try:
        from tracely.services.rolling_summary_service import RollingSummaryService

        thread_id = result.get("thread_id") or trace_id
        RollingSummaryService().build_for_thread(project_id, thread_id, source="ingest")
    except Exception as exc:
        log.warning("rolling_summary_ingest_failed", trace_id=trace_id, error=str(exc))

    # The whole-thread pass (conversation columns + sequential message columns), debounced on the
    # THREAD: every turn schedules one and only the last one standing runs, so the thread is graded
    # once it stops growing. Same trailing-debounce mechanism as the per-trace one above, keyed by
    # thread. Skipped outright when the project has neither kind of column — `evaluate_trace`
    # already knows, and a task whose only job is to discover it has nothing to do is a queue hop
    # plus a DB round-trip on every message.
    if result.get("needs_thread_pass"):
        thread_id = result.get("thread_id") or trace_id
        cgen = eval_debounce.bump(project_id, _CONV_KEY.format(thread=thread_id))
        evaluate_conversation_task.apply_async(
            (project_id, thread_id, cgen), countdown=settings.eval_debounce_seconds
        )
    return result


@celery_app.task(
    name="tracely.evaluate_conversation", bind=True, max_retries=3, default_retry_delay=3
)
def evaluate_conversation_task(self, project_id: str, thread_id: str, gen: int = 0) -> dict:
    """Grade a settled thread's CONVERSATION columns and sequential message/step columns.

    The same thread pass gives sequential message evaluators a stable oldest→newest ordering, so
    turn N receives turn N-1's result. Batch columns already ran on their individual trace tasks.
    """
    if not eval_debounce.is_latest(project_id, _CONV_KEY.format(thread=thread_id), gen):
        return {"skipped": "superseded", "thread_id": thread_id}
    try:
        return EvaluationService().evaluate_thread(
            project_id, thread_id, execution_mode="sequential"
        )
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(name="tracely.rebuild_clusters", bind=True, max_retries=0)
def rebuild_clusters_task(self, project_id: str) -> dict:
    return FailureIntelService().rebuild_clusters(project_id)


@celery_app.task(name="tracely.run_scenario_gate", bind=True, max_retries=0)
def run_scenario_gate_task(
    self,
    project_id: str,
    agent_id: str,
    gate_run_id: str,
    env: str = "ci",
    git_ref: str = "",
    pr_number: int | None = None,
    min_pass_rate: float | None = None,
    case_ids: list[str] | None = None,
    scenario_ids: list[str] | None = None,
) -> dict:
    """Phase 1 of a simulated gate: replay the regression cases, then drive the conversations.

    Async because the scenario half makes real HTTP calls to the customer's agent — minutes, not
    milliseconds. `POST /api/gate/simulate` pre-creates the `GateRun` row (status RUNNING) so CI
    gets an id to poll immediately; `max_retries=0` because a half-driven conversation must not be
    silently re-sent to a live endpoint.

    Grading is a SEPARATE task, scheduled with a countdown. The agent's own spans arrive as
    ordinary OTLP and are ingested by Celery, so under the default `--pool=solo --concurrency=1`
    they cannot be processed while this task holds the only slot. Returning first lets that queue
    drain, so the grader actually sees the agent's tool calls instead of a trace containing only
    Tracely's turn spans.
    """
    from tracely.infrastructure.db.engine import SyncSessionLocal
    from tracely.infrastructure.db.models import GateRun
    from tracely.services.gate_service import GateService

    with SyncSessionLocal() as s:
        # `task_acks_late` redelivery: a run that already finalized must not be driven again.
        done = s.get(GateRun, gate_run_id)
        if done is not None and done.finished_at is not None:
            return {"gate_run_id": gate_run_id, "status": done.status}
        try:
            gate = GateService(s).run_gate(
                project_id,
                agent_id,
                env=env,
                git_ref=git_ref,
                pr_number=pr_number,
                with_scenarios=True,
                min_pass_rate=min_pass_rate,
                gate_run_id=gate_run_id,
                finalize=False,
                case_ids=case_ids,
                scenario_ids=scenario_ids,
            )
        except Exception:
            s.rollback()
            log.exception("scenario_gate_drive_failed", gate_run_id=gate_run_id)
            _mark_gate_error(s, gate_run_id)
            raise

    try:
        grade_scenario_gate_task.apply_async(
            (project_id, gate_run_id, min_pass_rate, scenario_ids),
            countdown=settings.gate_scenario_span_grace_s,
        )
    except Exception:
        # A broker blip here would otherwise leave the row RUNNING forever, with CI polling it.
        log.exception("scenario_gate_grade_enqueue_failed", gate_run_id=gate_run_id)
        with SyncSessionLocal() as s:
            _mark_gate_error(s, gate_run_id)
        raise
    return {"gate_run_id": gate.id, "status": "RUNNING"}


@celery_app.task(name="tracely.grade_scenario_gate", bind=True, max_retries=0)
def grade_scenario_gate_task(
    self,
    project_id: str,
    gate_run_id: str,
    min_pass_rate: float | None = None,
    scenario_ids: list[str] | None = None,
) -> dict:
    """Phase 2: grade the driven conversations and finalize the run.

    Runs after phase 1 released the worker, so the agent's own span-ingest tasks have had a chance
    to process and its tool calls are actually on the trace by now.
    """
    from tracely.infrastructure.db.engine import SyncSessionLocal
    from tracely.services.gate_service import GateService

    with SyncSessionLocal() as s:
        try:
            gate = GateService(s).grade_scenarios(
                gate_run_id, min_pass_rate, project_id=project_id, scenario_ids=scenario_ids
            )
        except Exception:
            s.rollback()
            log.exception("scenario_gate_grade_failed", gate_run_id=gate_run_id)
            _mark_gate_error(s, gate_run_id)
            raise
        if gate is None:
            return {"gate_run_id": gate_run_id, "status": "MISSING"}
        return {"gate_run_id": gate.id, "status": gate.status}


def _mark_gate_error(session, gate_run_id: str) -> None:
    """Leave a terminal record rather than a row stuck on RUNNING that CI polls forever."""
    from datetime import datetime, timezone

    from tracely.infrastructure.db.models import GateRun

    stuck = session.get(GateRun, gate_run_id)
    if stuck and stuck.finished_at is None:
        stuck.status = "ERROR"
        stuck.finished_at = datetime.now(timezone.utc)
        session.commit()


@celery_app.task(name="tracely.evaluate_monitors", bind=True, max_retries=0)
def evaluate_monitors_task(self) -> dict:
    """Fire the monitoring engine for every enabled monitor in every project — driven by Celery
    beat (`beat_schedule` in `celery_app.py`). Best-effort: one bad monitor is logged + skipped,
    transient errors are NOT retried (the next beat tick will pick up where we left off)."""
    import asyncio

    from tracely.services.monitoring_service import MonitoringService

    try:
        return asyncio.run(MonitoringService().evaluate_all())
    except Exception as exc:  # CH outage / Redis blip — the next tick will retry
        log.warning("evaluate_monitors_failed", error=str(exc))
        return {"monitors": 0, "fired": 0, "error": str(exc)}


@celery_app.task(name="tracely.prune_chats", bind=True, max_retries=0)
def prune_chats_task(self) -> dict:
    """Drop judge-conversation checkpoints nothing can read again (beat, nightly).

    LangGraph persists one checkpoint per step and each re-serializes the whole transcript, so a
    long sequential column grows its storage quadratically and never gives it back. Left alone it
    is the largest table in the deployment by an order of magnitude — a disk-full incident on the
    store that also holds the registry. `infrastructure/llm/checkpointer.py` has the detail."""
    from tracely.infrastructure.llm.checkpointer import prune

    try:
        return prune()
    except Exception as exc:  # noqa: BLE001 — a missed sweep costs disk, never a grade
        log.warning("prune_chats_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task(name="tracely.enforce_retention", bind=True, max_retries=0)
def enforce_retention_task(self) -> dict:
    """Delete traces past their plan's retention window (beat, nightly).

    A no-op unless billing is on — see `services/retention_service.py` for why that gate is not
    optional. Never retried: a missed night is a day of extra storage, and re-running a sweep
    that half-failed just re-issues the same DELETEs the next tick anyway."""
    from tracely.services.retention_service import enforce

    # Snapshot un-artifacted cases from their sources BEFORE anything is swept — the artifact is
    # what keeps a case executable once its source trace is gone. Runs whether or not billing
    # (and so the sweep) is on: a self-hosted ClickHouse TTL expires traces just the same.
    try:
        from tracely.infrastructure.db.engine import SyncSessionLocal
        from tracely.services.regression_service import RegressionService

        with SyncSessionLocal() as s:
            res = RegressionService(s).backfill_artifacts()
        if res["snapshotted"] or res["unrecoverable"]:
            log.info(
                "case_artifacts_backfilled",
                snapshotted=len(res["snapshotted"]),
                unrecoverable=len(res["unrecoverable"]),
            )
    except Exception as exc:
        log.warning("case_artifact_backfill_failed", error=str(exc))

    try:
        return enforce()
    except Exception as exc:  # noqa: BLE001 — a missed sweep costs disk, never a grade
        log.warning("enforce_retention_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task(name="tracely.selfcheck", bind=True, max_retries=0)
def selfcheck_task(self) -> dict:
    """Watch our own deployment (beat, every 5 min). Tracely's failure modes are quiet — a dead
    worker still 202s every ingest — so this is the thing that turns 'the numbers stopped moving'
    into a page. Best-effort by construction: it must never itself be the reason a beat tick dies."""
    import asyncio

    from tracely.services.selfcheck_service import run

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        log.warning("selfcheck_failed", error=str(exc))
        return {"degraded": None, "error": str(exc)}


@celery_app.task(name="tracely.run_scenario", bind=True, max_retries=0)
def run_scenario_task(
    self, project_id: str, scenario_id: str, conversation_id: str, env: str = "ci"
) -> dict:
    """Drive ONE scenario against its agent's endpoint — the Scenarios page's Run button.

    Async for the same reason the gate's phase 1 is: this makes real HTTP calls to the customer's
    agent, which takes minutes, not milliseconds. `max_retries=0` because a half-driven
    conversation must never be silently re-sent to a live endpoint.

    Grading is a SEPARATE task on a countdown, exactly as the gate does it. The agent's own spans
    arrive as ordinary OTLP and are ingested by Celery; under `--pool=solo --concurrency=1` they
    cannot be processed while this task holds the only slot, so returning first lets that queue
    drain and the grader sees the agent's tool calls instead of only Tracely's turn spans.
    """
    from tracely.infrastructure.db.engine import SyncSessionLocal
    from tracely.infrastructure.db.models import AgentEndpoint, Scenario
    from tracely.services.simulation_service import SimulationService

    with SyncSessionLocal() as s:
        scenario = s.get(Scenario, scenario_id)
        if scenario is None or scenario.project_id != project_id:
            return {"error": "scenario not found"}
        endpoint = s.get(AgentEndpoint, scenario.agent_id)
        if endpoint is None:
            return {"error": "no endpoint configured for this agent"}
        agent_slug = repositories.agent_slug(s, project_id, scenario.agent_id)
        try:
            result = SimulationService().run_scenario(
                project_id,
                agent_slug,
                scenario,
                endpoint,
                env=env,
                conversation_id=conversation_id,
            )
        except Exception as exc:
            # The drive crashed mid-run (blob store, mapper). Any turns that DID land are real
            # traces — still schedule grading so they don't sit ungraded forever, but without the
            # per-turn trace ids the grader falls back to grading the thread as-is.
            log.exception("scenario_run_failed", scenario_id=scenario_id)
            grade_scenario_turns_task.apply_async(
                (project_id, conversation_id), countdown=settings.gate_scenario_span_grace_s
            )
            return {"conversation_id": conversation_id, "error": f"{type(exc).__name__}: {exc}"}

    grade_scenario_turns_task.apply_async(
        (
            project_id,
            conversation_id,
            scenario_id,
            result.get("trace_ids") or [],
            result.get("error") or "",
        ),
        countdown=settings.gate_scenario_span_grace_s,
    )
    return {
        "conversation_id": conversation_id,
        "turns": len(result.get("turns") or []),
        "error": result.get("error") or "",
    }


@celery_app.task(name="tracely.grade_scenario_turns", bind=True, max_retries=0)
def grade_scenario_turns_task(
    self,
    project_id: str,
    conversation_id: str,
    scenario_id: str = "",
    trace_ids: list[str] | None = None,
    error: str = "",
) -> dict:
    """Phase 2 of a one-click run: grade the conversation the drive produced.

    A standalone run has no gate to grade it, and the turns were ingested inline (blob → mapper,
    NOT via Celery), so nothing scheduled an evaluation for them. Grading goes through the SAME
    path as the gate's phase 2 — the project's evaluators (targeting off), the authored
    expectations, and the attack judge for an ADVERSARIAL scenario. `evaluate_thread` alone
    skipped all of that, so a successful jailbreak run from the UI rendered green.

    The legacy `(project_id, conversation_id)` form (no scenario id — e.g. a task queued by an
    older backend, or a drive that crashed before returning trace ids) falls back to plain
    evaluation of whatever landed.
    """
    from tracely.infrastructure.db.engine import SyncSessionLocal
    from tracely.services.gate_service import GateService

    try:
        if scenario_id:
            with SyncSessionLocal() as s:
                return GateService(s).grade_standalone_scenario(
                    project_id, scenario_id, conversation_id, trace_ids or [], error=error
                )
        return EvaluationService().evaluate_thread(project_id, conversation_id)
    except Exception as exc:
        log.warning("scenario_grade_failed", conversation_id=conversation_id, error=str(exc))
        return {"scores": 0, "error": str(exc)}
