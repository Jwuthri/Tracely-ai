"""POST /api/clusters/{id}/promote — promoting a cluster whose oldest trace is gone.

The medoid is the cluster's FIRST member, so it is the first trace a delete or the 90-day events
TTL takes out. Keying promote only on the medoid made the button 404 on clusters that still had
perfectly good newer traces — and the UI swallowed it, so it read as "nothing happens".

Same harness as `test_clusters_delete`: real sync SQLite behind the router, with the trace read
(ClickHouse) and the promote itself stubbed — this pins the member-fallback, not the promotion.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services.regression_service import NotFound

_TABLES = [
    models.Project.__table__,
    models.IngestKey.__table__,
    models.User.__table__,
    models.Membership.__table__,
    models.Organization.__table__,
    models.OrgMembership.__table__,
    models.Invitation.__table__,
    models.Evaluator.__table__,
    models.Agent.__table__,
    models.FailureCluster.__table__,
    models.ClusterMember.__table__,
    # unpromote deletes the promoted case — everything case_delete touches must exist
    models.EvaluationCase.__table__,
    models.CaseReplay.__table__,
    models.EvaluationSuite.__table__,
    models.EvaluationSuiteCase.__table__,
    models.GateRun.__table__,
    models.GateCase.__table__,
]


@pytest_asyncio.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    sync_eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    maker = sessionmaker(sync_eng)
    import tracely.api.routers.clusters as clusters_router
    import tracely.api.routers.evaluators as evaluators_router

    monkeypatch.setattr(clusters_router, "SyncSessionLocal", maker)
    monkeypatch.setattr(evaluators_router, "SyncSessionLocal", maker)
    yield maker
    sync_eng.dispose()


@pytest.fixture
def promotable(monkeypatch):
    """Stub RegressionService.promote_trace: only trace ids in the returned set still exist."""
    import tracely.api.routers.clusters as clusters_router

    alive: set[str] = set()
    tried: list[str] = []

    def fake_promote(self, project_id, trace_id, title=None):
        tried.append(trace_id)
        if trace_id not in alive:
            raise NotFound("trace not found")
        return type("Case", (), {"id": f"case-for-{trace_id}"})()

    monkeypatch.setattr(clusters_router.RegressionService, "promote_trace", fake_promote)
    return alive, tried


async def _owner(client) -> tuple[str, str]:
    tok = (
        await client.post("/auth/register", json={"email": "o@x.test", "password": "hunter2-pw"})
    ).json()["token"]
    pid = (await client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})).json()[
        "project_id"
    ]
    return tok, pid


def _seed(maker, project_id: str, trace_ids: list[str]) -> str:
    """A cluster whose members are `trace_ids` in order — the first is the medoid."""
    with maker() as s:
        agent = models.Agent(id=str(uuid4()), project_id=project_id, slug=f"a-{uuid4().hex[:6]}")
        cl = models.FailureCluster(
            id=str(uuid4()),
            project_id=project_id,
            agent_id=agent.id,
            cluster_key=uuid4().hex[:16],
            label="BadRequestResponseError(...)",
            count=len(trace_ids),
            status="OPEN",
        )
        s.add_all([agent, cl])
        s.flush()
        for i, tid in enumerate(trace_ids):
            s.add(models.ClusterMember(cluster_id=cl.id, trace_id=tid, is_medoid=(i == 0)))
        s.commit()
        return cl.id


async def test_falls_back_when_the_medoid_trace_is_gone(client, sync_db, promotable):
    """The reported bug: histogram renders (newer traces exist) but promote 404s on the medoid."""
    alive, tried = promotable
    tok, pid = await _owner(client)
    cid = _seed(sync_db, pid, ["gone-1", "gone-2", "still-here"])
    alive.add("still-here")

    r = await client.post(
        f"/api/clusters/{cid}/promote", headers={"Authorization": f"Bearer {tok}"}
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"case_id": "case-for-still-here", "cluster_status": "PROMOTED"}
    assert tried == ["gone-1", "gone-2", "still-here"]  # medoid first, in member order
    with sync_db() as s:
        cl = s.get(models.FailureCluster, cid)
        assert (cl.status, cl.candidate_case_id) == ("PROMOTED", "case-for-still-here")


async def test_medoid_still_wins_when_it_is_alive(client, sync_db, promotable):
    alive, tried = promotable
    tok, pid = await _owner(client)
    cid = _seed(sync_db, pid, ["medoid", "other"])
    alive.update({"medoid", "other"})

    r = await client.post(
        f"/api/clusters/{cid}/promote", headers={"Authorization": f"Bearer {tok}"}
    )

    assert r.json()["case_id"] == "case-for-medoid"
    assert tried == ["medoid"]  # stops at the first that works


async def test_all_traces_gone_explains_why(client, sync_db, promotable):
    alive, _ = promotable  # nothing alive
    tok, pid = await _owner(client)
    cid = _seed(sync_db, pid, ["gone-1", "gone-2"])

    r = await client.post(
        f"/api/clusters/{cid}/promote", headers={"Authorization": f"Bearer {tok}"}
    )

    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "2 traces are still stored" in detail and "TTL" in detail
    with sync_db() as s:
        assert s.get(models.FailureCluster, cid).status == "OPEN"  # not half-promoted


async def test_unpromote_deletes_the_case_and_reopens_the_cluster(client, sync_db):
    tok, pid = await _owner(client)
    cid = _seed(sync_db, pid, ["t-1"])
    with sync_db() as s:
        cl = s.get(models.FailureCluster, cid)
        case = models.EvaluationCase(
            id=str(uuid4()),
            project_id=pid,
            agent_id=cl.agent_id,
            title=cl.label,
            source_trace_id="t-1",
            input_digest="d1",
        )
        s.add(case)
        s.flush()
        s.add(
            models.CaseReplay(
                id=str(uuid4()), case_id=case.id, candidate_trace_id="t-1", verdict="FAIL"
            )
        )
        cl.status, cl.candidate_case_id = "PROMOTED", case.id
        s.commit()
        case_id = case.id

    h = {"Authorization": f"Bearer {tok}"}
    r = await client.post(f"/api/clusters/{cid}/unpromote", headers=h)

    assert r.status_code == 200, r.text
    assert r.json() == {"status": "OPEN"}
    with sync_db() as s:
        cl = s.get(models.FailureCluster, cid)
        assert (cl.status, cl.candidate_case_id) == ("OPEN", None)
        assert s.get(models.EvaluationCase, case_id) is None
        assert list(s.execute(select(models.CaseReplay)).scalars()) == []

    # already-gone case (deleted from the cases page first) is a no-op, not an error
    with sync_db() as s:
        cl = s.get(models.FailureCluster, cid)
        cl.status, cl.candidate_case_id = "PROMOTED", "case-already-deleted"
        s.commit()
    assert (await client.post(f"/api/clusters/{cid}/unpromote", headers=h)).status_code == 200

    assert (await client.post(f"/api/clusters/{uuid4()}/unpromote", headers=h)).status_code == 404
