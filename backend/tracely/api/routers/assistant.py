"""The dashboard's chat widget: conversations, one turn at a time, plus attachments.

Thin by the book — validate, call `assistant_service`, shape the reply. Conversations are scoped
to the project AND to the caller (`Principal.user_id`; None for ingest keys and dev mode, which
then share the project's chats).

A turn streams. The assistant is an agent now: it reads traces, writes evaluators, runs scenarios,
and a turn that does three of those takes the better part of a minute. `POST /assistant/chat`
answers `text/event-stream` (the same `data: <json>` / `[DONE]` protocol as `/evaluations/run`)
so the widget can show the work as it happens:

    {"type": "tool",      "name": str, "args": {...}}   the agent is calling a tool
    {"type": "tool_done", "name": str, "ok": bool}      that tool came back
    {"type": "delta",     "text": str}                  a piece of the answer
    {"type": "done",      "chat_id", "title", "reply"}  saved; `reply` is authoritative
    {"type": "disabled"}                                no LLM key on this deployment
    {"type": "over_budget", "spent_usd", "budget_usd"}  this conversation spent its allowance
    {"type": "error",     "detail": str}                the turn failed; nothing was stored

The tools run as the CALLER: the request's own credential headers are forwarded into them
(`internal_client.auth_headers_from`), so the agent's reach is the user's reach.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_principal
from tracely.api.internal_client import auth_headers_from
from tracely.auth import Principal
from tracely.infrastructure.blob import s3
from tracely.infrastructure.db import repositories as repo
from tracely.infrastructure.db.engine import SyncSessionLocal
from tracely.services import assistant_service

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 5
MAX_CONTEXT_CHARS = 20_000
_ID_RE = re.compile(r"^[0-9a-f]{32}$")  # what we mint — and the only thing we'll look up


class Attachment(BaseModel):
    id: str = Field(max_length=32)
    name: str = Field(max_length=200)
    mime: str = Field(default="application/octet-stream", max_length=100)
    size: int = 0


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    chat_id: str | None = Field(default=None, max_length=36)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    path: str = Field(default="", max_length=200)  # the page the user is looking at
    # What that page chose to share about itself (the alert editor's unsaved rule). Capped so a
    # page can't paste a workspace into every turn.
    context: dict | None = None

    @field_validator("context")
    @classmethod
    def _small(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v, default=str)) > MAX_CONTEXT_CHARS:
            raise ValueError(f"context exceeds {MAX_CONTEXT_CHARS} chars")
        return v


class MessageOut(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    attachments: list[Attachment] = Field(default_factory=list)


def _summary(row) -> dict:
    msgs = row.messages or []
    return {
        "id": row.id,
        "title": row.title or assistant_service.title_for(
            next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
        ),
        "messages": len(msgs),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# How long a silent stream may stay silent. `run_scenario`, `replay_case` and `run_evaluation`
# each make real calls that take minutes, and a turn spending them emits nothing in between —
# long enough for a proxy to decide the connection is dead and close it, which the browser sees
# as a chat that stopped mid-answer. An SSE comment is the protocol's own way to say "still here":
# parsers ignore it, so it costs one line and changes nothing downstream.
PING_EVERY_S = 15.0


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@router.post("/assistant/chat")
async def chat(
    body: ChatBody, request: Request, principal: Principal = Depends(get_principal)
) -> StreamingResponse:
    """One assistant turn, streamed. See the module docstring for the frame protocol."""
    stream = assistant_service.answer_stream(
        principal.project_id,
        principal.user_id,
        chat_id=body.chat_id,
        message=body.message,
        attachments=[a.model_dump() for a in body.attachments],
        path=body.path,
        context=body.context,
        headers=auth_headers_from(request.headers),
    )

    async def gen():
        # ONE task drains the turn, and this generator only reads its queue. Draining it directly
        # — a task per `__anext__`, to time out for a keep-alive — ran each step of the agent in a
        # different copied Context, which breaks every contextvar the turn depends on: the LLM key
        # scope, and the introspection recording, whose token then cannot be reset in the context
        # that set it. Same shape as `/evaluations/run`, for the same reason.
        queue: asyncio.Queue = asyncio.Queue()
        finished = object()

        async def pump():
            try:
                async for event in stream:
                    await queue.put(event)
            except Exception as exc:
                # A dead provider is a chat bubble, not a 500 in the console — and once the first
                # frame is out the status code is already 200, so a failure has to BE a frame.
                log.warning(
                    "assistant_chat_failed", project_id=principal.project_id, error=str(exc)
                )
                await queue.put({"type": "error", "detail": f"the model call failed: {exc}"[:300]})
            finally:
                await queue.put(finished)

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=PING_EVERY_S)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is finished:
                    break
                yield _sse(event)
        finally:
            # Client gone mid-turn (they closed the panel, or hit Stop): stop the agent rather
            # than letting it keep calling tools and spending on an answer nobody will read.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/assistant/chats")
async def list_chats(principal: Principal = Depends(get_principal)) -> list[dict]:
    """This caller's conversations, newest first — what the history panel lists."""

    def work():
        with SyncSessionLocal() as s:
            rows = repo.assistant_chat_list(s, principal.project_id, principal.user_id)
            return [_summary(r) for r in rows]

    return await run_in_threadpool(work)


@router.get("/assistant/chats/{chat_id}")
async def get_chat(chat_id: str, principal: Principal = Depends(get_principal)) -> dict:
    """One conversation in full — what reopening it from history loads."""

    def work():
        with SyncSessionLocal() as s:
            row = repo.assistant_chat_get(s, principal.project_id, principal.user_id, chat_id)
            if row is None:
                return None
            return {**_summary(row), "messages": row.messages or []}

    out = await run_in_threadpool(work)
    if out is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return out


@router.delete("/assistant/chats/{chat_id}")
async def delete_chat(chat_id: str, principal: Principal = Depends(get_principal)) -> dict:
    def work():
        with SyncSessionLocal() as s:
            return repo.assistant_chat_delete(s, principal.project_id, principal.user_id, chat_id)

    if not await run_in_threadpool(work):
        raise HTTPException(status_code=404, detail="conversation not found")
    return {"deleted": True}


@router.post("/assistant/upload")
async def upload(
    file: UploadFile = File(...), principal: Principal = Depends(get_principal)
) -> dict:
    """Store one attachment and hand back the metadata the next message echoes. The bytes go
    under the project's blob prefix, so deleting the workspace takes them along."""
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )
    attachment_id = uuid.uuid4().hex
    mime = file.content_type or "application/octet-stream"
    await run_in_threadpool(
        s3.put_blob, s3.assistant_blob_key(principal.project_id, attachment_id), raw, mime
    )
    return {
        "id": attachment_id,
        "name": (file.filename or "file")[:200],
        "mime": mime[:100],
        "size": len(raw),
    }


@router.get("/assistant/files/{attachment_id}")
async def get_file(attachment_id: str, principal: Principal = Depends(get_principal)) -> Response:
    """Serve an attachment back — the thumbnail in the transcript, and the download link.

    Only ever renders images inline. Anything else is handed back as an opaque download: this
    endpoint sits on our own origin, and serving a user-uploaded `text/html` inline with its own
    content type is stored XSS with extra steps.
    """
    if not _ID_RE.match(attachment_id):  # also what keeps a crafted id out of another prefix
        raise HTTPException(status_code=404, detail="not found")
    try:
        raw, mime = await run_in_threadpool(
            s3.get_blob_typed, s3.assistant_blob_key(principal.project_id, attachment_id)
        )
    except Exception:
        raise HTTPException(status_code=404, detail="not found") from None
    inline = mime.startswith("image/") and not mime.endswith("svg+xml")  # SVG executes script
    return Response(
        content=raw,
        media_type=mime if inline else "application/octet-stream",
        headers={
            "Content-Disposition": "inline" if inline else "attachment",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )
