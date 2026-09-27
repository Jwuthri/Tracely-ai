"""The workspace's rolling-summary budget: validation, defaults, and the settings API."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from tracely.domain.evaluation.rolling_summary import budget_problem, summary_budget
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base

_TABLES = [
    models.Project.__table__, models.IngestKey.__table__, models.User.__table__,
    models.Membership.__table__, models.Organization.__table__, models.OrgMembership.__table__,
    models.Invitation.__table__, models.Evaluator.__table__,
]


@pytest_asyncio.fixture
async def engine(tmp_path):
    """File-backed SQLite so the admin router's sync sessionmaker sees the same database."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    sync_eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    maker = sessionmaker(sync_eng)
    import tracely.api.routers.admin as admin_router

    monkeypatch.setattr(admin_router, "SyncSessionLocal", maker)
    yield maker
    sync_eng.dispose()


def test_budget_falls_back_to_defaults_per_field():
    b = summary_budget({"max_tokens": 8000}, default_max=16000, default_step=512)
    assert (b.max_tokens, b.step_max_tokens) == (8000, 512)
    # an unusable stored value never breaks a build — it reads as the default
    b = summary_budget({"max_tokens": "lots", "step_max_tokens": 10}, default_max=16000, default_step=512)
    assert (b.max_tokens, b.step_max_tokens) == (16000, 512)
    assert summary_budget(None, default_max=16000, default_step=512).max_tokens == 16000


@pytest.mark.parametrize("raw, needle", [
    ({"max_tokens": 100}, "between 2000 and 64000"),
    ({"max_tokens": 10**6}, "between 2000 and 64000"),
    ({"step_max_tokens": 5000}, "between 64 and 4000"),
    ({"max_tokens": 2000, "step_max_tokens": 3000}, "cannot exceed"),
    ({"max_tokens": True}, "integer"),
    ({"budget": 1}, "unknown keys"),
])
def test_budget_validation(raw, needle):
    assert needle in (budget_problem(raw) or "")


def test_default_budget_leaves_room_in_a_32k_decision_model():
    from tracely.config import Settings

    assert Settings().rolling_summary_max_tokens <= 16000


async def _owner_token(client) -> str:
    r = await client.post("/auth/register", json={"email": "owner@x.test", "password": "hunter2-pw"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def test_settings_api_roundtrip(client, sync_db):
    tok = await _owner_token(client)
    h = {"Authorization": f"Bearer {tok}"}
    r = await client.get("/api/project/rolling-summary-config", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["custom"] is False and r.json()["config"] == r.json()["defaults"]

    r = await client.put("/api/project/rolling-summary-config", headers=h,
                         json={"max_tokens": 12000, "step_max_tokens": 256})
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {"max_tokens": 12000, "step_max_tokens": 256} and r.json()["custom"]

    bad = await client.put("/api/project/rolling-summary-config", headers=h, json={"max_tokens": 50})
    assert bad.status_code == 400 and "between" in bad.json()["detail"]

    # both fields omitted = back to the defaults
    r = await client.put("/api/project/rolling-summary-config", headers=h, json={})
    assert r.json()["custom"] is False
