"""An ingest key reads and writes traces for ONE workspace. It must not be able to wipe that
workspace, change its secrets, or delete what people built in it — the key lives in CI logs and
`.env` files, and a leak should cost traces, not the product."""

from __future__ import annotations

import pytest

from tracely.auth import resolve_principal
from tracely.config import settings

DESTRUCTIVE = [
    ("DELETE", "/api/project/data", {"confirm": "DELETE"}),
    ("PUT", "/api/project/llm-key", {"api_key": "sk-or-leaked"}),
    ("DELETE", "/api/project/llm-key", None),
    ("POST", "/api/project/agents/prune", None),
    ("POST", "/api/project/seed", None),
    ("DELETE", "/api/sessions", {"threads": ["t1"]}),
    ("DELETE", "/api/clusters", {"cluster_ids": ["c1"]}),
    ("DELETE", "/api/cases/c1", None),
    ("POST", "/api/clusters/c1/unpromote", None),
    ("DELETE", "/api/evaluators/e1", None),
    ("DELETE", "/api/scenarios/s1", None),
    ("DELETE", "/api/meta-analyses/m1", None),
    ("POST", "/api/monitors", {"name": "m", "condition": {"type": "trace_failure_rate", "threshold": 0.5}}),
    ("PATCH", "/api/monitors/m1", {"enabled": False}),
    ("DELETE", "/api/monitors/m1", None),
    ("POST", "/api/monitors/m1/test", None),
    ("PUT", "/api/agents/planner/endpoint", {"url": "https://8.8.8.8/chat"}),
]


@pytest.mark.parametrize("method,path,body", DESTRUCTIVE)
async def test_ingest_key_is_refused(client, make_workspace, method, path, body):
    await make_workspace("ws", "tk_ci_key", "o@x.test")
    r = await client.request(
        method, path, json=body, headers={"Authorization": "Bearer tk_ci_key"}
    )
    assert r.status_code == 403, (path, r.text)


async def test_the_same_key_still_reads_and_ingests(client, make_workspace):
    """The narrowing is on destruction, not on what the key is for."""
    await make_workspace("ws", "tk_ci_key", "o@x.test")
    r = await client.post(
        "/auth/projects", json={"name": "x"}, headers={"Authorization": "Bearer tk_ci_key"}
    )
    assert r.status_code == 403  # unchanged: workspace creation was already owner/admin-only
    r = await client.get("/auth/me", headers={"Authorization": "Bearer tk_ci_key"})
    assert r.status_code == 200 and r.json()["role"] is None


async def test_dev_mode_key_acts_as_owner(session, make_workspace, monkeypatch):
    """AUTH_MODE=dev has no humans: the dashboard itself runs on the ingest key, so its
    Settings page must keep working. Prod refuses to boot in this mode."""
    await make_workspace("ws", "tk_dev", "o@x.test")
    monkeypatch.setattr(settings, "auth_mode", "dev")
    p = await resolve_principal(token="tk_dev", x_project=None, session=session)
    assert p.role == "OWNER" and p.user_id is None
    monkeypatch.setattr(settings, "auth_mode", "local")
    p = await resolve_principal(token="tk_dev", x_project=None, session=session)
    assert p.role is None
