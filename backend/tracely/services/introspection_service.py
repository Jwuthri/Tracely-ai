"""Turn a Recording into a trace.

One entry point — `record(...)` — wraps a piece of Tracely's own work (grading a trace, driving a
scenario) and, on the way out, ships what happened down the ordinary ingest path so it lands in
ClickHouse like any other trace.

Three rules this module exists to hold:

1. **It can never break the work it observes.** Every failure here is logged and swallowed. A
   judge that graded correctly must not report an error because its recording failed to save.
2. **No nesting.** A recording already in progress wins; an inner `record()` is a no-op that keeps
   filing steps into the outer one. Without this, grading inside a scenario run would fork the
   trace in two and lose the connection between them.
3. **Ingest is inline, not queued.** Same reason emulated turns are (see `simulation_service`):
   the caller often holds the worker's only slot under `--pool=solo --concurrency=1`, so an
   enqueued blob would sit behind work that is waiting on us.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import structlog
from starlette.concurrency import run_in_threadpool

from tracely.config import settings
from tracely.domain import introspection
from tracely.domain.introspection import Recording
from tracely.infrastructure.blob import s3 as blobstore
from tracely.infrastructure.clickhouse import deletes
from tracely.services.ingestion_service import IngestionService

log = structlog.get_logger(__name__)


@contextlib.contextmanager
def record(
    kind: str,
    subject_id: str,
    name: str,
    *,
    project_id: str,
    agent_slug: str = "",
    env: str = "prod",
    subject_label: str = "",
    conversation_id: str = "",
    turn_index: int = 0,
    stable: bool = False,
) -> Iterator[Recording | None]:
    """Record everything Tracely does inside this block as a trace about `subject_id`.

    Yields the `Recording` so callers can set `.label` (the group new steps file under) and add
    non-LLM steps; yields None when recording is off or already in progress, so callers must
    tolerate None — `rec and rec.add(...)`.
    """
    rec, token = _start(
        kind, subject_id, name, project_id=project_id, agent_slug=agent_slug, env=env,
        subject_label=subject_label, conversation_id=conversation_id, turn_index=turn_index,
        stable=stable,
    )
    if rec is None:
        yield None
        return
    try:
        yield rec
    finally:
        introspection._active.reset(token)
        _emit(rec)


@contextlib.asynccontextmanager
async def record_async(
    kind: str,
    subject_id: str,
    name: str,
    *,
    project_id: str,
    agent_slug: str = "",
    env: str = "prod",
    subject_label: str = "",
    conversation_id: str = "",
    turn_index: int = 0,
    stable: bool = False,
) -> AsyncIterator[Recording | None]:
    """`record`, for a caller on the event loop.

    Same contract, one difference that matters: the emit writes a blob and drives ingest, both
    blocking, and doing that inline would stall every other request on this worker while the
    assistant's turn is filed. The recording itself is a contextvar, which a threadpool call
    inherits, so nothing else changes.
    """
    rec, token = _start(
        kind, subject_id, name, project_id=project_id, agent_slug=agent_slug, env=env,
        subject_label=subject_label, conversation_id=conversation_id, turn_index=turn_index,
        stable=stable,
    )
    if rec is None:
        yield None
        return
    try:
        yield rec
    finally:
        introspection._active.reset(token)
        await run_in_threadpool(_emit, rec)


def _start(kind: str, subject_id: str, name: str, **kw) -> tuple[Recording | None, Any]:
    """Open a recording, or decline to (disabled, or one is already in progress — see rule 2)."""
    if not settings.introspection_enabled or introspection.active() is not None:
        return None, None
    rec = Recording(kind=kind, subject_id=subject_id, name=name, **kw)
    return rec, introspection._active.set(rec)


def _emit(rec: Recording) -> None:
    """Ship the recording as OTLP — one trace per evaluation level. Best-effort by design, see
    rule 1 in the module docstring."""
    try:
        import json

        for trace_id, body in introspection.payload(rec).items():
            if rec.stable:
                # Replace, not append: this recording re-runs over the same subject and reuses its
                # trace id, so the previous spans have to go. ReplacingMergeTree can't do it for
                # us — its sort key carries `start_time`, and this run has its own. Scoped to the
                # groups THIS recording carries: the trace is shared by every column graded at
                # that level, and a per-column run must not erase its siblings.
                deletes.delete_trace(rec.project_id, trace_id, introspection.step_names(body))
            raw = json.dumps(body).encode()
            key = blobstore.event_blob_key(rec.project_id, uuid.uuid4().hex, "application/json")
            blobstore.put_blob(key, raw, "application/json")
            IngestionService().process_blob(rec.project_id, key, "application/json")
    except Exception as exc:
        log.warning(
            "introspection_emit_failed",
            kind=rec.kind, subject_id=rec.subject_id, steps=len(rec.steps), error=str(exc),
        )
