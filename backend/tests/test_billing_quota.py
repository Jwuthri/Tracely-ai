"""Monthly trace quota: exact counting in the worker, fail-open enforcement at the OTLP edge.

Counting invariants pinned here, all of which favor the customer:
- a trace counts ONCE per month, however many batches (or task retries) carry its spans;
- Tracely's own recordings (`internal_kind`) never count;
- any Redis/Postgres failure means "don't count" and never an exception — a billing hiccup must
  not re-run `process_blob` (that would double the ingest work AND lose the count);
- `BILLING_ENABLED=false` (the self-host default) touches nothing, not even Redis.

Enforcement: over the plan's cap → 429 + Retry-After before the body is read; under → the
request proceeds; infra down → allow (a quota is a product limit, not a security boundary).

Quota pools per ORGANIZATION: the org's plan sets the cap and its workspaces share it, so
creating workspaces never mints quota and a paying team isn't charged per workspace.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tracely.config import settings
from tracely.domain.billing import current_period, plan_for_subscription_status, trace_limit_for
from tracely.infrastructure.db import models, repositories
from tracely.infrastructure.db.base import Base
from tracely.services import quota_service


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeSyncRedis:
    """Real SADD semantics — the dedupe the counter leans on."""

    def __init__(self) -> None:
        self.sets: dict[str, set] = {}
        self.ttls: dict[str, int] = {}

    def sadd(self, key, *ids):
        s = self.sets.setdefault(key, set())
        new = [i for i in ids if i not in s]
        s.update(new)
        return len(new)

    def expire(self, key, ttl):
        self.ttls[key] = ttl


class _BoomSyncRedis:
    def __getattr__(self, name):
        def _boom(*a, **kw):
            raise ConnectionError("redis down")

        return _boom


class _FakeAsyncRedis:
    def __init__(self, store: dict | None = None) -> None:
        self.store = store or {}

    async def get(self, key):
        v = self.store.get(key)
        return v.encode() if isinstance(v, str) else v

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _BoomAsyncRedis:
    def __getattr__(self, name):
        async def _boom(*a, **kw):
            raise ConnectionError("redis down")

        return _boom


@pytest.fixture
def sync_db(monkeypatch):
    """In-memory sync SQLite behind `SyncSessionLocal` for the counter's Postgres half."""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng, tables=[models.Project.__table__, models.UsageCounter.__table__]
    )
    maker = sessionmaker(eng)
    import tracely.infrastructure.db.engine as engine_module

    monkeypatch.setattr(engine_module, "SyncSessionLocal", maker)
    yield maker
    eng.dispose()


@pytest.fixture
def billing_on(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)


# ── domain helpers ────────────────────────────────────────────────────────────


def test_trace_limit_per_plan():
    assert trace_limit_for("free", 20_000, 1_000_000) == 20_000
    assert trace_limit_for("pro", 20_000, 1_000_000) == 1_000_000
    assert trace_limit_for("unlimited", 20_000, 1_000_000) is None
    # a typo'd plan must fall back to the free cap, not to uncapped
    assert trace_limit_for("porn", 20_000, 1_000_000) == 20_000


def test_subscription_status_mapping():
    for paid in ("active", "trialing", "past_due"):  # dunning must not downgrade mid-cycle
        assert plan_for_subscription_status(paid) == "pro"
    for gone in ("canceled", "unpaid", "incomplete_expired", "", None):
        assert plan_for_subscription_status(gone) == "free"


# ── counting ──────────────────────────────────────────────────────────────────


def test_counts_each_trace_once_across_batches_and_retries(sync_db, billing_on, monkeypatch):
    fake = _FakeSyncRedis()
    monkeypatch.setattr(quota_service, "sync_redis", lambda: fake)

    assert quota_service.record_ingested_traces("p1", ["t1", "t2"], []) == 2
    # the retry / late-batch case: same ids again, plus one genuinely new
    assert quota_service.record_ingested_traces("p1", ["t1", "t2", "t3"], []) == 1

    with sync_db() as s:
        assert repositories.usage_traces(s, "p1", current_period()) == 3


def test_internal_recordings_never_count(sync_db, billing_on, monkeypatch):
    monkeypatch.setattr(quota_service, "sync_redis", lambda: _FakeSyncRedis())
    n = quota_service.record_ingested_traces("p1", ["t1", "ev1"], ["ev1"])
    assert n == 1
    with sync_db() as s:
        assert repositories.usage_traces(s, "p1", current_period()) == 1


def test_disabled_touches_nothing(monkeypatch):
    def _no_redis():
        raise AssertionError("billing off must not touch Redis")

    monkeypatch.setattr(settings, "billing_enabled", False)
    monkeypatch.setattr(quota_service, "sync_redis", _no_redis)
    assert quota_service.record_ingested_traces("p1", ["t1"], []) == 0


def test_redis_down_counts_nothing_and_never_raises(sync_db, billing_on, monkeypatch):
    monkeypatch.setattr(quota_service, "sync_redis", lambda: _BoomSyncRedis())
    assert quota_service.record_ingested_traces("p1", ["t1"], []) == 0
    with sync_db() as s:
        assert repositories.usage_traces(s, "p1", current_period()) == 0


def test_pg_failure_after_sadd_is_swallowed(sync_db, billing_on, monkeypatch):
    """The ids are already in the seen-set when the upsert fails — the delta is deliberately
    lost (undercount) rather than raising into the task's retry path."""
    monkeypatch.setattr(quota_service, "sync_redis", lambda: _FakeSyncRedis())
    monkeypatch.setattr(
        repositories, "usage_increment",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("pg down")),
    )
    assert quota_service.record_ingested_traces("p1", ["t1"], []) == 0


def test_usage_increment_upserts(sync_db):
    with sync_db() as s:
        repositories.usage_increment(s, "p1", "2026-08", 5)
        repositories.usage_increment(s, "p1", "2026-08", 7)
        repositories.usage_increment(s, "p1", "2026-09", 1)  # new month = new row
        s.commit()
        assert repositories.usage_traces(s, "p1", "2026-08") == 12
        assert repositories.usage_traces(s, "p1", "2026-09") == 1


# ── the OTLP edge gate (end-to-end through the app) ───────────────────────────


async def _workspace_over_quota(client, make_workspace, session, traces: int):
    proj, _, key = await make_workspace("quota-ws", "key-quota", "q@x.test")
    session.add(models.UsageCounter(project_id=proj.id, period=current_period(), traces=traces))
    await session.commit()
    return proj, key


async def _sibling_project(
    session, organization_id: str | None, *, traces: int = 0, slug: str = "sib",
    key: str | None = None,
):
    """Another workspace in the same organization — the fan-out the pool exists to close."""
    proj = models.Project(
        id=str(uuid4()), slug=slug, name=slug, source="local",
        organization_id=organization_id,
    )
    session.add(proj)
    await session.flush()
    if traces:
        session.add(
            models.UsageCounter(project_id=proj.id, period=current_period(), traces=traces)
        )
    k = None
    if key:
        k = models.IngestKey(id=str(uuid4()), project_id=proj.id, key=key)
        session.add(k)
    await session.commit()
    return proj, k


async def test_over_quota_returns_429_with_retry_after(
    client, make_workspace, session, monkeypatch
):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())  # PG fallback
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(settings, "stripe_price_pro", "price_x")
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(
        otlp_router, "ingest_otlp",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("gate must reject first")),
    )
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=10)

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 429
    assert r.headers.get("retry-after") == "3600"
    assert "quota" in r.json()["detail"] and "/settings/billing" in r.json()["detail"]


async def test_over_quota_without_stripe_does_not_send_anyone_to_a_dead_upgrade_page(
    client, make_workspace, session, monkeypatch
):
    """BILLING_ENABLED can be on while Stripe is not configured — checkout answers 501 there. The
    cap is still real, so the 429 has to say who can lift it instead of linking to a page where
    the customer physically cannot pay."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "stripe_price_pro", "")
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=10)

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert "quota" in detail
    assert "/settings/billing" not in detail
    assert "operator" in detail


async def test_the_gate_fails_open_when_postgres_is_down(
    client, make_workspace, session, monkeypatch
):
    """Deliberate, and the reason it is deliberate is worth a test: a quota is a product limit,
    not a security boundary. If our DB blinks we let the customer's traces through and undercount
    — never the reverse. This is the ONLY fail-open on the enforcement path."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 1)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    monkeypatch.setattr(
        quota_service, "_snapshot_from_pg",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("pg down")),
    )
    ingested = []
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a, **kw: ingested.append(a) or "b1")
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=10**9)

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200, "a DB outage must not drop a paying customer's traces"
    assert ingested, "the trace was accepted, not silently discarded"


async def test_under_quota_ingests_normally(client, make_workspace, session, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    calls = []
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: calls.append(a) or "batch-1")
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=9)

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200
    assert len(calls) == 1


async def test_gate_reads_the_cache_before_postgres(
    client, make_workspace, session, monkeypatch
):
    """A cached 'over' verdict rejects without a DB read (the DB says under)."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=0)
    proj_id = (await client.get("/api/billing/usage", headers={"Authorization": f"Bearer {key.key}"})).json()
    cache = {
        f"tracely:quota:gate:{key.project_id}:{current_period()}": "10:10"
    }
    monkeypatch.setattr(quota_service, "async_redis", lambda: _FakeAsyncRedis(cache))
    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 429
    assert proj_id["traces_used"] == 0  # the DB genuinely said under — the cache decided


async def test_billing_disabled_means_no_gate(client, make_workspace, session, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", False)

    def _no_redis():
        raise AssertionError("disabled gate must not touch Redis")

    monkeypatch.setattr(quota_service, "async_redis", _no_redis)
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: "batch-1")
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=10**9)

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200


async def test_unlimited_plan_is_never_gated(client, make_workspace, session, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: "batch-1")
    proj, key = await _workspace_over_quota(client, make_workspace, session, traces=10**6)
    org = await session.get(models.Organization, proj.organization_id)
    org.plan = "unlimited"
    await session.commit()

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200


async def test_quota_pools_across_the_organizations_workspaces(
    client, make_workspace, session, monkeypatch
):
    """6 traces here + 5 in a second workspace of the same org = one exhausted 10-pool."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(
        otlp_router, "ingest_otlp",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("gate must reject first")),
    )
    proj, key = await _workspace_over_quota(client, make_workspace, session, traces=6)
    await _sibling_project(session, proj.organization_id, traces=5, slug="pool-sib")

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 429


async def test_another_organization_never_shares_the_pool(
    client, make_workspace, session, monkeypatch
):
    """Tenancy: a different org's usage is invisible here, however large."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: "batch-1")
    _, key = await _workspace_over_quota(client, make_workspace, session, traces=2)
    other, _, _ = await make_workspace("other-ws", "key-other", "other@x.test")
    await _sibling_project(session, other.organization_id, traces=10**6, slug="other-sib")

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200  # 2/10 of our own org's pool


async def test_upgrading_the_org_lifts_every_workspace(
    client, make_workspace, session, monkeypatch
):
    """One subscription covers the whole account — a team doesn't pay per workspace."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(settings, "pro_trace_limit", 10_000)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: "batch-1")
    proj, key = await _workspace_over_quota(client, make_workspace, session, traces=50)
    _, sib_key = await _sibling_project(
        session, proj.organization_id, slug="pro-sib", key="key-pro-sib"
    )
    org = await session.get(models.Organization, proj.organization_id)
    org.plan = "pro"
    await session.commit()

    for k in (key.key, sib_key.key):
        r = await client.post(
            "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {k}"}
        )
        assert r.status_code == 200  # 50 used of the org's 10k, in both workspaces


async def test_orgless_project_keeps_per_workspace_accounting(
    client, make_workspace, session, monkeypatch
):
    """CLI-seeded / dev projects (organization_id NULL) must never pool with each other."""
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 10)
    monkeypatch.setattr(quota_service, "async_redis", lambda: _BoomAsyncRedis())
    import tracely.api.routers.otlp as otlp_router

    monkeypatch.setattr(otlp_router, "ingest_otlp", lambda *a: "batch-1")
    proj, key = await _workspace_over_quota(client, make_workspace, session, traces=5)
    proj.organization_id = None
    await _sibling_project(session, None, traces=999, slug="orphan-sib")

    r = await client.post(
        "/v1/traces", content=b"{}", headers={"Authorization": f"Bearer {key.key}"}
    )
    assert r.status_code == 200  # 5/10 on its own counter; the other orphan's 999 is unrelated


async def test_usage_endpoint_reports_the_snapshot(client, make_workspace, session, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_trace_limit", 20_000)
    proj, key = await _workspace_over_quota(client, make_workspace, session, traces=1234)
    await _sibling_project(session, proj.organization_id, traces=234, slug="usage-sib")

    r = await client.get("/api/billing/usage", headers={"Authorization": f"Bearer {key.key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["billing_enabled"] is True
    assert body["plan"] == "free"
    assert body["traces_used"] == 1234 + 234  # the account pool, not this workspace alone
    assert body["trace_limit"] == 20_000
    assert body["period"] == current_period()
    assert body["quota_scope"] == "account"
