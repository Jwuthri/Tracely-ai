"""On-demand evaluation runs, streamed over SSE.

POST /api/evaluations/run drives the same engine as the ingest path, but interactively:
the UI's per-column / per-row / run-all Play buttons send the target threads (conversation
rows) and/or traces (turn rows) plus an optional evaluator filter, and per-score `result`
frames stream back into the exact grid cells as they're persisted.

Frame protocol (`data: <json>\n\n`, terminated by `data: [DONE]\n\n`):
  {"type":"start","targets":N,"evaluators":M}
  {"type":"result","score":{name, evaluation_level, observation_id, value, string_value,
                            verdict, comment, data_type, trace_id, session_id}}
  {"type":"target_done","target":"<id>","scores":n} | {"type":"target_error","target":"<id>",...}
  {"type":"done"}

Execution: bounded-concurrency (3) over targets, each target evaluated in a worker thread
(the engine is sync). Re-runs are idempotent — score ids are deterministic, so ReplacingMergeTree
swaps values in place. A client disconnect stops the stream; in-flight targets finish writing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_project_id
from tracely.domain import introspection
from tracely.infrastructure.clickhouse import async_reader
from tracely.services.evaluation_service import EvaluationService

log = structlog.get_logger()

router = APIRouter(prefix="/api")

MAX_TARGETS = 200
CONCURRENCY = 3
QUEUE_TIMEOUT_S = 600


class RunRequest(BaseModel):
    evaluator_ids: list[str] = Field(default_factory=list)  # empty → all enabled evaluators
    thread_ids: list[str] = Field(default_factory=list)  # conversation rows (full subtree)
    trace_ids: list[str] = Field(default_factory=list)  # turn rows (trace + its spans)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/evaluations/run")
async def run_evaluations(
    body: RunRequest, project_id: str = Depends(get_project_id)
) -> StreamingResponse:
    threads = [t for t in dict.fromkeys(body.thread_ids) if t][:MAX_TARGETS]
    traces = [t for t in dict.fromkeys(body.trace_ids) if t][:MAX_TARGETS]
    log.info(
        "eval_run_clicked",
        project_id=project_id,
        evaluator_ids=body.evaluator_ids or "all",
        threads=len(threads),
        traces=len(traces),
    )
    if not threads and not traces:
        raise HTTPException(status_code=400, detail="no targets: pass thread_ids and/or trace_ids")

    specs = await run_in_threadpool(
        EvaluationService.load_enabled_evaluators, project_id, body.evaluator_ids or None
    )
    if not specs:
        raise HTTPException(status_code=400, detail="no enabled evaluators match the request")

    queue: asyncio.Queue[dict] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(payload: dict) -> None:  # called from worker threads
        loop.call_soon_threadsafe(queue.put_nowait, payload)

    targets = [("thread", t) for t in threads] + [("trace", t) for t in traces]

    async def run_target(kind: str, target_id: str) -> None:
        svc = EvaluationService()
        on_result = lambda score: emit({"type": "result", "score": score})  # noqa: E731

        def work() -> dict:
            if kind == "thread":
                return svc.evaluate_thread(project_id, target_id, specs=specs, on_result=on_result)
            return svc.evaluate_trace(
                project_id, target_id, specs=specs, on_result=on_result, skip_conversation=True
            )

        try:
            r = await run_in_threadpool(work)
            emit({"type": "target_done", "target": target_id, "scores": r.get("scores", 0)})
        except Exception as exc:
            log.warning("eval_run_target_failed", target=target_id, error=str(exc))
            emit({"type": "target_error", "target": target_id, "detail": str(exc)[:300]})

    async def run_all() -> None:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def guarded(kind: str, target_id: str) -> None:
            async with sem:
                await run_target(kind, target_id)

        await asyncio.gather(*(guarded(k, t) for k, t in targets))
        queue.put_nowait({"type": "__complete__"})

    async def gen():
        task = asyncio.create_task(run_all())
        results = errors = 0
        try:
            yield _sse({"type": "start", "targets": len(targets), "evaluators": len(specs)})
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=QUEUE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    yield _sse({"type": "error", "detail": "evaluation run timed out"})
                    break
                if msg.get("type") == "__complete__":
                    break
                if msg.get("type") == "result":
                    results += 1
                elif msg.get("type") == "target_error":
                    errors += 1
                yield _sse(msg)
            log.info(
                "eval_run_finished",
                project_id=project_id,
                targets=len(targets),
                results=results,
                target_errors=errors,
            )
            yield _sse({"type": "done"})
            yield "data: [DONE]\n\n"
        finally:
            # Client gone or stream finished — stop driving new targets. In-flight worker
            # threads can't be interrupted; they finish their current target and persist.
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/evaluations/prompt")
async def evaluation_prompt(
    subject: str,
    level: str,
    name: str,
    step: str = "",
    project_id: str = Depends(get_project_id),
) -> dict:
    """The LLM calls behind one score cell: the judge's prompt and what came back.

    No pointer is stored on the score — the eval recording's trace id IS the identity of
    (project, eval, subject, level), so it is re-derived here. `subject` is the trace id for a
    msg/step score and the thread id for a conv score (what `_dispatch_specs` recorded under).

    `step` narrows a step-level column to the one span the cell is about: a judge grading 10 spans
    records 10 calls under the same evaluator, and a TOOL row must not show the other nine. It is
    matched against the recorded call's name (`llm_judge` names each one `"<TYPE> <span name>"`);
    an unmatched value falls back to every call rather than to nothing, so a drift in that label
    costs precision, not the panel.

    A structural evaluator made no call and returns no steps.
    """
    trace_id = introspection.stable_trace_id(project_id, introspection.EVAL, subject, level)
    spans = await async_reader.trace_spans(project_id, trace_id)
    calls = [s for s in spans if s["step_name"] == name and s["type"] == "GENERATION"]
    if step:
        calls = [s for s in calls if s["name"] == step] or calls
    return {
        "trace_id": trace_id,
        "steps": [
            {
                "name": s["name"],
                "model": s["model_id"],
                "input": _clip(s["input"]),
                "output": _clip(s["output"]),
            }
            for s in calls
        ],
    }


# A judge prompt is a few KB; a judge prompt that pasted a 2MB tool result is not something a
# popover can show, and shipping it to the browser helps no one. The full text is always one
# click away in the eval trace itself.
PROMPT_CHARS = 20_000


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= PROMPT_CHARS else text[:PROMPT_CHARS] + "\n… (truncated)"
