"""The MCP server mounted at `/mcp`, driven by a real MCP client over an in-process ASGI transport.

Proves the three things that break silently: the handshake works at `/mcp` itself (no redirect),
the caller's ingest key is forwarded into the routers (so tools are project-scoped), and a tool
call reaches the real endpoint — `create_evaluator` here lands an actual row that
`list_evaluators` reads back.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from tracely.api.main import app
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base

_TABLES = [
    models.Organization.__table__,
    models.OrgMembership.__table__,
    models.Project.__table__,
    models.IngestKey.__table__,
    models.User.__table__,
    models.Membership.__table__,
    models.Invitation.__table__,
    models.Evaluator.__table__,
]


@pytest_asyncio.fixture
async def engine(tmp_path):
    """File-backed (overrides conftest's :memory:) so the evaluators router's SYNC sessionmaker
    sees the same database — same reason as test_evaluators_api."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    import tracely.api.routers.evaluators as evaluators_router

    monkeypatch.setattr(
        evaluators_router,
        "SyncSessionLocal",
        sessionmaker(create_engine(f"sqlite:///{tmp_path}/test.db")),
    )


@asynccontextmanager
async def mcp_session(key: str | None):
    """An MCP client talking to our mounted server without a socket.

    The transport is rebuilt per session: `lifespan` isn't active under ASGITransport, so the
    session manager has to be run here, it refuses to run twice, and its task group must live in
    the same task as the test (a fixture spanning `yield` is torn down in a different one). The
    mounted route looks the manager up per request, so it follows along.
    """
    from tracely.api import mcp_server

    mcp_server.mcp._session_manager = None
    mcp_server.mcp.streamable_http_app()
    async with mcp_server.mcp.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {key}"} if key else {},
        ) as http:
            async with streamable_http_client("http://test/mcp", http_client=http) as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    yield session


def payload(result):
    """FastMCP only derives an output schema from a parameterized return type, so a `-> dict`
    tool answers with a text block while `-> list[dict]` fills `structuredContent` (and an empty
    list has no text blocks at all, which is the wrong thing to assert on)."""
    assert not result.isError, result.content[0].text if result.content else "(no content)"
    if result.structuredContent is not None:
        return result.structuredContent["result"]
    return json.loads(result.content[0].text)


async def test_tools_are_listed(client, sync_db, make_workspace):
    await make_workspace("mcp-list", "mcp_key_list", "list@x.test")
    async with mcp_session("mcp_key_list") as s:
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"list_traces", "get_trace", "list_evaluators", "create_evaluator"} <= names
    assert {"list_audits", "create_audit", "generate_audit_seeds", "run_audit", "get_audit_run", "get_audit_rollout"} <= names
    # Read-mostly by design: no delete/admin/ingest surface.
    assert not [n for n in names if "delete" in n]


async def test_create_then_list_evaluator(client, sync_db, make_workspace):
    """The whole point: a tool call goes through the real router and mutates real state."""
    await make_workspace("mcp-crud", "mcp_key_crud", "crud@x.test")
    async with mcp_session("mcp_key_crud") as s:
        created = payload(
            await s.call_tool(
                "create_evaluator",
                {
                    "name": "Refund policy",
                    "kind": "llm_judge",
                    "level": "AGENT_RUN",
                    "config": {
                        "prompt": "Did the agent follow the refund policy?",
                        "threshold": 0.6,
                    },
                },
            )
        )
        assert created["name"] == "Refund policy"
        listed = payload(await s.call_tool("list_evaluators", {}))
    assert created["id"] in {e["id"] for e in listed}


async def test_router_validation_reaches_the_caller(client, sync_db, make_workspace):
    """A 400 from the router surfaces as a tool error, not a silent success."""
    await make_workspace("mcp-bad", "mcp_key_bad", "bad@x.test")
    async with mcp_session("mcp_key_bad") as s:
        res = await s.call_tool(
            "create_evaluator", {"name": "Nope", "kind": "structural", "config": {"check": "bogus"}}
        )
    assert res.isError
    assert "unknown structural check" in res.content[0].text


async def test_key_is_required_and_scoped(client, sync_db, make_workspace):
    """No key → the routers' own 401. Another workspace's key → only its own rows."""
    await make_workspace("mcp-a", "mcp_key_a", "a@x.test")
    await make_workspace("mcp-b", "mcp_key_b", "b@x.test")

    async with mcp_session(None) as s:
        assert (await s.call_tool("list_evaluators", {})).isError

    async with mcp_session("mcp_key_a") as s:
        payload(
            await s.call_tool("create_evaluator", {"name": "A only", "config": {"prompt": "x"}})
        )
    async with mcp_session("mcp_key_b") as s:
        assert payload(await s.call_tool("list_evaluators", {})) == []


async def test_export_conversations_parses_ndjson_and_caps_the_limit(
    client, sync_db, make_workspace, monkeypatch
):
    """The export endpoint streams NDJSON, so the tool must not hand it to `r.json()`. The cap is
    the other half: a whole workspace would evict the caller's context."""
    from tracely.api import mcp_server
    from tracely.api.routers import sessions as sessions_router

    asked: list[int] = []

    async def fake_overview(project_id, limit, offset, *a, **kw):
        asked.append(limit)
        if offset:
            return []
        # already-parsed dict — the shape `sessions_overview` really returns
        return [{"thread": f"t-{i}", "metadata": {"business_id": "A"}} for i in range(40)]

    async def fake_turns(project_id, thread_id, advisory):
        return [{"trace_id": f"{thread_id}-tr", "input": "hi", "output": "yo"}]

    monkeypatch.setattr(sessions_router.async_reader, "sessions_overview", fake_overview)
    monkeypatch.setattr(sessions_router.async_reader, "session_turns", fake_turns)
    monkeypatch.setattr(sessions_router.async_reader, "thread_spans_full", lambda *a: _empty([]))
    monkeypatch.setattr(sessions_router.async_reader, "conversation_scores", lambda *a: _empty([]))
    monkeypatch.setattr(sessions_router.async_reader, "scores_by_trace", lambda *a: _empty({}))
    monkeypatch.setattr(sessions_router, "advisory_score_names", lambda p: _empty([]))

    await make_workspace("mcp-export", "mcp_key_export", "export@x.test")
    async with mcp_session("mcp_key_export") as s:
        convs = payload(
            await s.call_tool("export_conversations", {"limit": 999, "meta": "business_id=A"})
        )
    # 40 conversations matched, but a tool call may not pull more than _EXPORT_MAX of them
    assert len(convs) == mcp_server._EXPORT_MAX
    assert [c["thread_id"] for c in convs] == [f"t-{i}" for i in range(mcp_server._EXPORT_MAX)]
    assert convs[0]["messages"][0]["input"] == "hi"  # NDJSON parsed, not handed to r.json()


async def _empty(value):
    return value
