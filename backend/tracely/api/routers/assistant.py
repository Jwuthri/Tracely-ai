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
import time
import uuid
from collections import deque
from typing import Literal

import httpx

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from tracely.api.auth import get_principal
from tracely.config import settings
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
        "title": row.title
        or assistant_service.title_for(
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


# ── Voice mode ────────────────────────────────────────────────────────────────
# Speech-to-speech for the same widget: the browser talks to the voice provider DIRECTLY
# (WebRTC for OpenAI, WebSocket for xAI/Grok) using a short-lived ephemeral token minted here,
# so the real keys never leave the server. Like the text assistant, voice runs on OUR keys —
# the same CLAUDE.md server-key seam, spoken instead of typed. These calls don't go through
# `infrastructure/llm/provider.py` on purpose: a realtime session is not a chat completion, and
# routing it through LangChain has nothing to route.
#
# The voice model gets ONE tool, `ask_tracely(question)`. The browser forwards it to
# POST /assistant/chat — the regular assistant agent, running as the caller — and speaks the
# reply. So voice grants no new reach into the workspace; it is a microphone on the agent above.

VOICE_INSTRUCTIONS = (
    "You are the Tracely assistant, speaking out loud inside the Tracely dashboard. "
    "Tracely is trace-native CI/CD for AI agents: production traces are graded by evaluators, "
    "failures are clustered, clusters become regression cases, and cases gate pull requests. "
    "For ANY question about this workspace's data — traces, failures, evaluators, clusters, "
    "cases, gates, trends, alerts — call the ask_tracely tool and summarize its answer aloud; "
    "never guess at numbers or verdicts yourself. Keep replies short and conversational: one or "
    "two spoken sentences, no markdown, no lists, and never read ids or URLs out loud."
)

ASK_TRACELY_TOOL = {
    "type": "function",
    "name": "ask_tracely",
    "description": (
        "Ask the Tracely workspace agent anything about this workspace: traces, conversations, "
        "failures, evaluators, clusters, regression cases, CI gates, trends, alerts. It can also "
        "create and change things (evaluators, scenarios, alerts) when asked to. Returns a text "
        "answer to summarize aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The user's request, in full."}
        },
        "required": ["question"],
    },
}

# OpenAI's realtime voices are a fixed set (the API error enumerates them); xAI's grow with
# releases, so those are fetched live below and this is only the fallback.
OPENAI_VOICES = [
    "marin",
    "cedar",
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
]
XAI_VOICES_FALLBACK = [
    "ara",
    "eve",
    "leo",
    "rex",
    "sal",
    "altair",
    "atlas",
    "aurora",
    "carina",
    "castor",
    "celeste",
    "cosmo",
    "helios",
    "helix",
    "iris",
    "kepler",
    "liora",
    "lumen",
    "luna",
    "lux",
    "naksh",
    "orion",
    "perseus",
    "rigel",
    "sirius",
    "ursa",
    "zagan",
    "zenith",
]
_XAI_VOICES_TTL_S = 3600.0
_xai_voices_cache: tuple[float, list[str]] = (0.0, [])

# ponytail: in-process fixed-window limiter, per project. Multi-replica prod rate-limits per
# replica; move to Redis if minted-session abuse ever shows up on the invoice.
_session_mints: dict[str, deque] = {}


def _rate_limited(project_id: str) -> bool:
    now = time.monotonic()
    q = _session_mints.setdefault(project_id, deque())
    while q and now - q[0] > 60.0:
        q.popleft()
    if len(q) >= max(1, settings.voice_sessions_per_minute):
        return True
    q.append(now)
    return False


async def _xai_voices() -> list[str]:
    """xAI's current voice roster (28 at last count, and growing) — cached for an hour."""
    global _xai_voices_cache
    ts, voices = _xai_voices_cache
    if voices and time.monotonic() - ts < _XAI_VOICES_TTL_S:
        return voices
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.x.ai/v1/tts/voices",
                headers={"Authorization": f"Bearer {settings.xai_api_key}"},
            )
            r.raise_for_status()
            voices = [v["voice_id"] for v in r.json().get("voices", []) if v.get("voice_id")]
    except Exception as exc:
        log.warning("xai_voices_fetch_failed", error=str(exc))
        voices = []
    if voices:
        _xai_voices_cache = (time.monotonic(), voices)
        return voices
    return XAI_VOICES_FALLBACK


class VoiceSessionBody(BaseModel):
    provider: Literal["openai", "xai"]
    voice: str = Field(default="", max_length=64)


@router.get("/assistant/voice/config")
async def voice_config(principal: Principal = Depends(get_principal)) -> dict:
    """Which speech providers this deployment can offer, and every voice each one has.
    A provider with no key configured is simply absent — the widget hides the mode entirely
    when the list is empty."""
    providers = []
    if settings.openai_api_key:
        providers.append(
            {
                "id": "openai",
                "label": "OpenAI",
                "model": settings.voice_openai_model,
                "voices": OPENAI_VOICES,
                "default_voice": "marin",
            }
        )
    if settings.xai_api_key:
        providers.append(
            {
                "id": "xai",
                "label": "Grok",
                "model": settings.voice_xai_model,
                "voices": await _xai_voices(),
                "default_voice": "ara",
            }
        )
    return {"providers": providers}


@router.post("/assistant/voice/session")
async def voice_session(
    body: VoiceSessionBody, principal: Principal = Depends(get_principal)
) -> dict:
    """Mint an ephemeral client token for one voice session and hand back everything the
    browser needs to connect on its own. The reply's `connection.type` tells it which
    transport: `webrtc` (OpenAI — POST the SDP offer to `connection.url`) or `websocket`
    (xAI — connect to `connection.url` and send `connection.session_update` first)."""
    if _rate_limited(principal.project_id):
        raise HTTPException(status_code=429, detail="too many voice sessions — wait a minute")
    ttl = settings.voice_token_ttl_seconds

    if body.provider == "openai":
        if not settings.openai_api_key:
            raise HTTPException(status_code=400, detail="OpenAI voice is not configured")
        voice = body.voice or "marin"
        if voice not in OPENAI_VOICES:
            raise HTTPException(status_code=400, detail=f"unknown OpenAI voice: {voice}")
        # The whole session config is baked into the minted secret server-side — instructions,
        # tools, VAD — so the browser only ever exchanges SDP.
        payload = {
            "expires_after": {"anchor": "created_at", "seconds": ttl},
            "session": {
                "type": "realtime",
                "model": settings.voice_openai_model,
                "instructions": VOICE_INSTRUCTIONS,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": {"type": "semantic_vad"},
                    },
                    "output": {"voice": voice},
                },
                "tools": [ASK_TRACELY_TOOL],
            },
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.openai.com/v1/realtime/client_secrets",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
        if r.status_code != 200:
            log.warning(
                "voice_session_mint_failed",
                provider="openai",
                status=r.status_code,
                body=r.text[:300],
            )
            raise HTTPException(status_code=502, detail="couldn't start an OpenAI voice session")
        data = r.json()
        return {
            "provider": "openai",
            "token": data["value"],
            "expires_at": data.get("expires_at"),
            "voice": voice,
            "connection": {"type": "webrtc", "url": "https://api.openai.com/v1/realtime/calls"},
        }

    if not settings.xai_api_key:
        raise HTTPException(status_code=400, detail="Grok voice is not configured")
    voices = await _xai_voices()
    voice = body.voice or ("ara" if "ara" in voices else voices[0])
    # Validated here because xAI doesn't reject a bad voice — it silently drops the whole
    # audio config from session.update, and the session runs on the default voice.
    if voice not in voices:
        raise HTTPException(status_code=400, detail=f"unknown Grok voice: {voice}")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.x.ai/v1/realtime/client_secrets",
            headers={"Authorization": f"Bearer {settings.xai_api_key}"},
            json={"expires_after": {"seconds": ttl}},
        )
    if r.status_code != 200:
        log.warning(
            "voice_session_mint_failed", provider="xai", status=r.status_code, body=r.text[:300]
        )
        raise HTTPException(status_code=502, detail="couldn't start a Grok voice session")
    data = r.json()
    # xAI configures the session over the socket, so the client is handed the exact
    # `session.update` to send — it fills in the sample rate its AudioContext actually got.
    return {
        "provider": "xai",
        "token": data["value"],
        "expires_at": data.get("expires_at"),
        "voice": voice,
        "connection": {
            "type": "websocket",
            "url": "wss://api.x.ai/v1/realtime",
            "session_update": {
                "instructions": VOICE_INSTRUCTIONS,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": "grok-stt-latest"},
                    },
                    "output": {"voice": voice, "format": {"type": "audio/pcm", "rate": 24000}},
                },
                "turn_detection": {"type": "server_vad"},
                "tools": [ASK_TRACELY_TOOL],
                "tool_choice": "auto",
            },
        },
    }
