"""Plan retention: the free tier keeps a week, the ClickHouse TTL keeps the rest.

The invariants that matter, all of which favor the customer:
- self-hosted (`BILLING_ENABLED=false`) is never swept — every org there sits on the default
  `free` plan, so a sweep would delete the operator's own traces;
- workspaces are grouped by the window their ORG's plan buys, so one DELETE covers the tier;
- `unlimited` is never swept, and Pro's window equals the table TTL (nothing to cut).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tracely.config import settings
from tracely.domain.billing import retention_days_for
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import retention_service


@pytest.fixture
def sync_db(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng, tables=[models.Project.__table__, models.Organization.__table__])
    maker = sessionmaker(eng)
    import tracely.infrastructure.db.engine as engine_module

    monkeypatch.setattr(engine_module, "SyncSessionLocal", maker)
    monkeypatch.setattr(retention_service, "SyncSessionLocal", maker)
    yield maker
    eng.dispose()


@pytest.fixture
def swept(monkeypatch):
    """Record the (project_ids, days) each sweep would issue instead of hitting ClickHouse."""
    calls: list[tuple[list[str], int]] = []

    def _fake(project_ids, days):
        calls.append((sorted(project_ids), days))
        return len(project_ids)

    monkeypatch.setattr(retention_service, "delete_expired", _fake)
    return calls


def _org(s, org_id: str, plan: str) -> None:
    s.add(models.Organization(id=org_id, name=org_id, slug=org_id, plan=plan))


def _project(s, project_id: str, org_id: str | None) -> None:
    s.add(models.Project(id=project_id, slug=project_id, name=project_id, organization_id=org_id))


def test_retention_per_plan():
    assert retention_days_for("free", 7, 90) == 7
    assert retention_days_for("pro", 7, 90) == 90
    assert retention_days_for("unlimited", 7, 90) is None
    # a typo'd plan gets the free window, same fail-toward-the-cap rule as the trace limit
    assert retention_days_for("gold", 7, 90) == 7


def test_self_hosted_is_never_swept(sync_db, swept, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", False)
    with sync_db() as s:
        _org(s, "o1", "free")
        _project(s, "p1", "o1")
        s.commit()

    assert retention_service.enforce()["skipped"] == "billing_disabled"
    assert swept == []


def test_free_workspaces_are_swept_together_at_one_week(sync_db, swept, monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "free_retention_days", 7)
    monkeypatch.setattr(settings, "pro_retention_days", 90)
    with sync_db() as s:
        _org(s, "free_org", "free")
        _org(s, "pro_org", "pro")
        _org(s, "op_org", "unlimited")
        _project(s, "f1", "free_org")
        _project(s, "f2", "free_org")
        _project(s, "pro1", "pro_org")
        _project(s, "op1", "op_org")
        _project(s, "orphan", None)  # dev-mode / CLI seed: free window
        s.commit()

    out = retention_service.enforce()

    assert (["f1", "f2", "orphan"], 7) in swept
    assert (["pro1"], 90) in swept
    assert not [c for c in swept if "op1" in c[0]]  # unlimited: the table TTL is all it gets
    assert out["spans"] == 4
