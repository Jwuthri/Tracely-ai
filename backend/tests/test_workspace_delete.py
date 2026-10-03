"""Deleting a workspace: who may, when it's refused, and what must survive it.

Three things make this safe rather than a footgun:
- owners/admins only, and only the workspace the caller is currently in (no id parameter, so it
  can't be aimed at another tenant);
- an organization's LAST workspace can't go — access is derived from the org, so an org with no
  workspaces locks every member out with a 403 and no route back;
- the surviving sibling INHERITS the usage counters, or "create workspace → burn quota → delete"
  would be an unlimited free tier.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from tracely.config import settings
from tracely.domain.billing import current_period
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base


# pgvector has no SQLite type compiler; render it as TEXT so every table can be created here and
# the repository's real DELETEs run against them (same shim as test_data_delete).
@compiles(Vector, "sqlite")
def _vector_as_text(element, compiler, **kw):  # noqa: ARG001
    return "TEXT"


@pytest_asyncio.fixture
async def engine(tmp_path):
    """Overrides conftest's in-memory engine: the delete spans BOTH sessions (async for auth,
    sync inside the router), so they have to share a file-backed database. Every table, because
    a full workspace delete touches nearly all of them."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch, engine):
    """Point the router's sync session at the same file, and stub the two external stores —
    ClickHouse and S3 aren't up in tests; the Postgres half is what's under test."""
    import tracely.api.routers.admin as admin

    sync_eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(admin, "SyncSessionLocal", sessionmaker(sync_eng))

    async def _no_events(project_id):
        return {"events": 0}

    monkeypatch.setattr(admin.deletes, "delete_project_events", _no_events)
    yield
    sync_eng.dispose()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, email: str, password: str = "pw-secret") -> str:
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def _second_workspace(session, project, slug="second") -> str:
    """Returns the id as a plain string — ORM instances go stale the moment the router's separate
    sync session commits underneath this one."""
    sib = models.Project(
        id=str(uuid4()), slug=slug, name=slug, source="local",
        organization_id=project.organization_id,
    )
    session.add(sib)
    await session.commit()
    return sib.id


async def test_admin_deletes_the_active_workspace(client, make_workspace, session, purges):
    proj, _user, _key = await make_workspace("acme", "tk_acme", "boss@x.test")
    sib_id = await _second_workspace(session, proj)
    proj_id = proj.id
    token = await _login(client, "boss@x.test")

    r = await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "acme"},
        headers={**_bearer(token), "X-Tracely-Project": proj_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["switch_to"] == sib_id  # the caller's cookie must move here
    # every blob of it (attachments too) — no cutoff, the workspace is gone — in the background
    assert purges == [(proj_id, None)]

    session.expire_all()
    assert await session.get(models.Project, proj_id) is None
    assert await session.get(models.Project, sib_id) is not None


async def test_the_only_workspace_cannot_be_deleted(client, make_workspace, session):
    """Otherwise the org has no workspaces and every member is locked out of the product."""
    proj, _user, _key = await make_workspace("solo", "tk_solo", "boss@x.test")
    proj_id = proj.id
    token = await _login(client, "boss@x.test")

    r = await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "solo"},
        headers={**_bearer(token), "X-Tracely-Project": proj_id},
    )
    assert r.status_code == 409 and "only workspace" in r.json()["detail"]
    session.expire_all()
    assert await session.get(models.Project, proj_id) is not None


async def test_wrong_confirmation_deletes_nothing(client, make_workspace, session):
    proj, _user, _key = await make_workspace("acme", "tk_acme", "boss@x.test")
    await _second_workspace(session, proj)
    proj_id = proj.id
    token = await _login(client, "boss@x.test")

    for bad in ("DELETE", "", "Acme"):  # not the exact name — including the wipe's confirm word
        r = await client.request(
            "DELETE",
            "/api/project",
            json={"confirm": bad},
            headers={**_bearer(token), "X-Tracely-Project": proj_id},
        )
        assert r.status_code == 400
    session.expire_all()
    assert await session.get(models.Project, proj_id) is not None


async def test_members_and_ingest_keys_cannot_delete(client, make_workspace, session):
    proj, _user, _key = await make_workspace("acme", "tk_acme", "m@x.test", role="MEMBER")
    await _second_workspace(session, proj)
    proj_id = proj.id
    token = await _login(client, "m@x.test")

    r = await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "acme"},
        headers={**_bearer(token), "X-Tracely-Project": proj_id},
    )
    assert r.status_code == 403

    # an SDK credential has role=None, so it can never delete the workspace it writes to
    r = await client.request(
        "DELETE",
        "/api/project", json={"confirm": "acme"}, headers=_bearer("tk_acme")
    )
    assert r.status_code == 403
    session.expire_all()
    assert await session.get(models.Project, proj_id) is not None


async def test_another_orgs_admin_cannot_reach_it(client, make_workspace, session):
    """The endpoint takes no id — an outsider can't even name the target, and switching to it
    is already 403 at the auth layer."""
    victim, _u, _k = await make_workspace("victim", "tk_victim", "victim@x.test")
    await _second_workspace(session, victim)
    victim_id = victim.id
    await make_workspace("other", "tk_other", "outsider@x.test")
    token = await _login(client, "outsider@x.test")

    r = await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "victim"},
        headers={**_bearer(token), "X-Tracely-Project": victim_id},
    )
    assert r.status_code == 403
    session.expire_all()
    assert await session.get(models.Project, victim_id) is not None


async def test_the_surviving_workspace_inherits_the_usage(client, make_workspace, session):
    """Otherwise: create a workspace, burn the org's quota in it, delete it, repeat — free."""
    monkey_period = current_period()
    proj, _user, _key = await make_workspace("acme", "tk_acme", "boss@x.test")
    sib_id = await _second_workspace(session, proj)
    proj_id = proj.id
    session.add_all([
        models.UsageCounter(project_id=proj_id, period=monkey_period, traces=8_000),
        models.UsageCounter(project_id=sib_id, period=monkey_period, traces=1_000),
    ])
    await session.commit()
    token = await _login(client, "boss@x.test")

    r = await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "acme"},
        headers={**_bearer(token), "X-Tracely-Project": proj_id},
    )
    assert r.status_code == 200

    session.expire_all()
    heir = await session.get(models.UsageCounter, (sib_id, monkey_period))
    assert heir.traces == 9_000  # the org's month is unchanged by the deletion


async def test_deleting_frees_a_workspace_slot(client, make_workspace, session, monkeypatch):
    """The cap counts live workspaces, so deleting one lets you create one again."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_workspace_limit", 2)
    proj, _user, _key = await make_workspace("acme", "tk_acme", "boss@x.test")
    await _second_workspace(session, proj)
    proj_id = proj.id
    token = await _login(client, "boss@x.test")

    at_cap = await client.post("/auth/projects", json={"name": "Third"}, headers=_bearer(token))
    assert at_cap.status_code == 409

    await client.request(
        "DELETE",
        "/api/project",
        json={"confirm": "acme"},
        headers={**_bearer(token), "X-Tracely-Project": proj_id},
    )
    assert (
        await client.post("/auth/projects", json={"name": "Third"}, headers=_bearer(token))
    ).status_code == 200
