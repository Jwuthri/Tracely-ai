"""The assistant's speech mode: which providers are offered, what a minted session carries,
and the guards (voice validation, rate limit) in front of the mint.

The provider HTTP calls are faked at the `httpx.AsyncClient` seam — these tests assert what we
SEND (the session config baked into an OpenAI token, the `session.update` handed to a Grok
client) and how upstream failure surfaces, not the providers themselves.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from tracely.api.routers import assistant as r
from tracely.auth import Principal


def principal() -> Principal:
    return Principal(project_id="proj-1", user_id=None, role=None, kind="ingest")


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands in for `httpx.AsyncClient`; records the last request it saw."""

    calls: list[dict] = []
    response = FakeResponse()

    def __init__(self, *a, **kw): ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        return FakeClient.response

    async def get(self, url, headers=None):
        FakeClient.calls.append({"url": url, "headers": headers})
        return FakeClient.response


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    FakeClient.calls = []
    FakeClient.response = FakeResponse()
    monkeypatch.setattr(r.httpx, "AsyncClient", FakeClient)
    r._session_mints.clear()
    r._xai_voices_cache = (0.0, [])
    monkeypatch.setattr(r.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(r.settings, "xai_api_key", "xai-test")


async def test_config_lists_only_configured_providers(monkeypatch):
    monkeypatch.setattr(r.settings, "xai_api_key", "")
    out = await r.voice_config(principal())
    assert [p["id"] for p in out["providers"]] == ["openai"]
    assert out["providers"][0]["voices"] == r.OPENAI_VOICES

    monkeypatch.setattr(r.settings, "openai_api_key", "")
    monkeypatch.setattr(r.settings, "xai_api_key", "")
    assert (await r.voice_config(principal()))["providers"] == []


async def test_xai_voices_come_from_the_api_and_cache(monkeypatch):
    FakeClient.response = FakeResponse(
        payload={"voices": [{"voice_id": "ara"}, {"voice_id": "rex"}]}
    )
    assert await r._xai_voices() == ["ara", "rex"]
    n = len(FakeClient.calls)
    assert await r._xai_voices() == ["ara", "rex"]  # served from cache
    assert len(FakeClient.calls) == n


async def test_xai_voices_fall_back_when_the_fetch_fails():
    FakeClient.response = FakeResponse(status_code=500)
    assert await r._xai_voices() == r.XAI_VOICES_FALLBACK


async def test_openai_session_bakes_config_server_side():
    FakeClient.response = FakeResponse(payload={"value": "ek_test", "expires_at": 123})
    out = await r.voice_session(r.VoiceSessionBody(provider="openai", voice="cedar"), principal())
    assert out["token"] == "ek_test"
    assert out["connection"]["type"] == "webrtc"
    sent = FakeClient.calls[-1]["json"]["session"]
    assert sent["model"] == r.settings.voice_openai_model
    assert sent["audio"]["output"]["voice"] == "cedar"
    assert sent["instructions"] == r.VOICE_INSTRUCTIONS
    assert [t["name"] for t in sent["tools"]] == ["ask_tracely"]


async def test_xai_session_hands_the_client_its_session_update():
    FakeClient.response = FakeResponse(
        payload={"value": "xai_tok", "expires_at": 9, "voices": [{"voice_id": "eve"}]}
    )
    out = await r.voice_session(r.VoiceSessionBody(provider="xai", voice="eve"), principal())
    assert out["connection"]["type"] == "websocket"
    update = out["connection"]["session_update"]
    # xAI silently ignores a bad voice, so the session must carry the validated one explicitly.
    assert update["audio"]["output"]["voice"] == "eve"
    assert [t["name"] for t in update["tools"]] == ["ask_tracely"]


async def test_unknown_voice_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await r.voice_session(r.VoiceSessionBody(provider="openai", voice="nope"), principal())
    assert exc.value.status_code == 400


async def test_unconfigured_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(r.settings, "xai_api_key", "")
    with pytest.raises(HTTPException) as exc:
        await r.voice_session(r.VoiceSessionBody(provider="xai", voice="ara"), principal())
    assert exc.value.status_code == 400


async def test_upstream_failure_is_a_502_not_a_crash():
    FakeClient.response = FakeResponse(status_code=401, payload={"error": "bad key"})
    with pytest.raises(HTTPException) as exc:
        await r.voice_session(r.VoiceSessionBody(provider="openai", voice="marin"), principal())
    assert exc.value.status_code == 502


async def test_mints_are_rate_limited_per_project(monkeypatch):
    monkeypatch.setattr(r.settings, "voice_sessions_per_minute", 2)
    FakeClient.response = FakeResponse(payload={"value": "ek", "expires_at": 1})
    body = r.VoiceSessionBody(provider="openai", voice="marin")
    await r.voice_session(body, principal())
    await r.voice_session(body, principal())
    with pytest.raises(HTTPException) as exc:
        await r.voice_session(body, principal())
    assert exc.value.status_code == 429
    # a different project has its own window
    other = Principal(project_id="proj-2", user_id=None, role=None, kind="ingest")
    assert (await r.voice_session(body, other))["token"] == "ek"
