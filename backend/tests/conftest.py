"""Hermetic auth test harness: an in-memory SQLite DB (StaticPool so every connection shares it) with
only the registry/auth tables created, plus an ASGI httpx client with `get_session` overridden.

AUTH_MODE/SESSION_SECRET are set *before* importing the app so main.py mounts the local-mode routers.
Clerk-mode tests monkeypatch `settings.auth_mode` and call the resolver directly (no app rebuild)."""

from __future__ import annotations

import os
from uuid import uuid4

os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-at-least-32-chars-long")
# Hard-off, not setdefault: a dev .env with a real key made every invite test send a live Resend
# email to `@x.test`, i.e. a hard bounce per run. Env beats the dotenv file in pydantic-settings.
os.environ["RESEND_API_KEY"] = ""
# Same class of bug, same hard-off: recording is on by default in the product, so any test that
# runs an evaluation or drives a scenario wrote an internal trace into whatever ClickHouse the dev
# .env points at — i.e. the developer's own workspace, once per run. Tests assert on the recording
# payload (`domain/introspection.py`), which is pure; nothing here needs it emitted.
os.environ["INTROSPECTION_ENABLED"] = "false"
# Fourth time, same class of bug, and the one that actually reached CI red: `Settings` reads
# `.env`, so a developer with a real OpenRouter key ran a DIFFERENT suite from CI's. The assistant
# tests passed on every laptop and failed on GitHub — not because they call a model, but because
# building the tool-picker's client needs credentials, and constructing it is an argument to the
# stubbed call. Hard-off so "no key configured" is the tested default everywhere; the handful of
# tests that want a key set one on `settings` themselves.
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""
os.environ["LLM_JUDGE_API_KEY"] = ""
# Third time for the same class of bug: the judge's durable conversations live in Postgres, and a
# developer's machine has one listening on localhost — so the suite silently reached it, wrote
# checkpoint rows, and behaved differently there than in CI (where nothing is listening). Tests
# that want the chat path turn it on with a fake checkpointer.
os.environ["EVAL_CHAT_ENABLED"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from tracely.api.main import app  # noqa: E402
from tracely.auth import passwords  # noqa: E402
from tracely.config import settings  # noqa: E402
from tracely.infrastructure.db import models  # noqa: E402
from tracely.infrastructure.db.base import Base  # noqa: E402
from tracely.infrastructure.db.session import get_session  # noqa: E402

# Only the tables the auth flows touch — avoids the pgvector `Vector` column (failure_embeddings),
# which has no SQLite type compiler. Evaluators ride along because workspace provisioning seeds
# the recommended catalog (and the /api/evaluators CRUD tests need it).
_AUTH_TABLES = [
    models.Organization.__table__,
    models.OrgMembership.__table__,
    models.Project.__table__,
    models.IngestKey.__table__,
    models.User.__table__,
    models.Membership.__table__,  # legacy, still created so the table exists for rollback tests
    models.Invitation.__table__,
    models.PasswordReset.__table__,
    models.Evaluator.__table__,
    # The OTLP edge's quota gate reads it whenever a billing test flips BILLING_ENABLED on.
    models.UsageCounter.__table__,
]


@pytest.fixture(autouse=True)
def _no_demo_seed_subprocesses(monkeypatch):
    """Workspace creation spawns the demo seeder in production. Tests create workspaces
    constantly — without this every one forks a python process against a backend that isn't
    there."""
    monkeypatch.setattr(settings, "seed_new_workspaces", False)


@pytest.fixture(autouse=True)
def _no_live_posthog(monkeypatch):
    """A developer's `.env` may hold a real POSTHOG_API_KEY (the wizard writes one). Tests must
    never build a live client — they'd ship every fake LLM call to PostHog, and the handler's
    import is fragile under other tests' module patching."""
    from tracely.infrastructure.llm import provider as _prov

    monkeypatch.setattr(settings, "posthog_api_key", "")
    monkeypatch.setattr(_prov, "_posthog_client", None)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_AUTH_TABLES)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(sessionmaker):
    async with sessionmaker() as s:
        yield s


@pytest_asyncio.fixture
async def client(sessionmaker):
    async def _override():
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
def make_workspace(session):
    """An isolated tenant: organization + workspace + ingest key + user with `role` in the org.

    Mirrors what provisioning builds, so tests exercise the same derived-access path the app
    does (`kind="company"` by default — personal orgs refuse teammates)."""

    async def _make(
        slug: str,
        key: str,
        email: str,
        role: str = "OWNER",
        password: str = "pw-secret",
        kind: str = "company",
    ):
        org = models.Organization(id=str(uuid4()), name=slug, slug=f"org-{slug}", kind=kind)
        user = models.User(
            id=str(uuid4()),
            email=email,
            source="local",
            password_hash=passwords.hash_password(password),
        )
        session.add_all([org, user])
        await session.flush()
        proj = models.Project(
            id=str(uuid4()), slug=slug, name=slug, source="local", organization_id=org.id
        )
        session.add(proj)
        await session.flush()
        k = models.IngestKey(id=str(uuid4()), project_id=proj.id, key=key)
        m = models.OrgMembership(
            id=str(uuid4()), user_id=user.id, organization_id=org.id, role=role
        )
        session.add_all([k, m])
        await session.commit()
        return proj, user, k

    return _make
