"""DELETE /api/cases/{id} and DELETE /api/project/data — the two destructive UI actions.

Same harness as `test_clusters_delete.py`: a real sync SQLite db behind the routers, so the
repository deletes (children first, project-scoped) are exercised rather than mocked. The
ClickHouse half of the project wipe is monkeypatched — everything else is real.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from tracely.config import settings
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base


# pgvector has no SQLite type compiler; render the column as TEXT so failure_embeddings can be
# created here and the wipe's real DELETE runs against it.
@compiles(Vector, "sqlite")
def _vector_as_text(element, compiler, **kw):  # noqa: ARG001
    return "TEXT"


@pytest_asyncio.fixture
async def engine(tmp_path):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with eng.begin() as conn:
        # EVERY table, not a hand-kept subset: this list used to mirror what the wipe
        # deletes, so adding `scenarios` to the schema left the test creating a database
        # the wipe's own DELETE couldn't run against. The Vector shim above is what made
        # the subset necessary in the first place.
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sync_db(tmp_path, monkeypatch, engine):
    sync_eng = create_engine(f"sqlite:///{tmp_path}/test.db")
    maker = sessionmaker(sync_eng)
    import tracely.api.routers.admin as admin_router
    import tracely.api.routers.cases as cases_router
    import tracely.api.routers.evaluators as evaluators_router

    monkeypatch.setattr(cases_router, "SyncSessionLocal", maker)
    monkeypatch.setattr(admin_router, "SyncSessionLocal", maker)
    monkeypatch.setattr(evaluators_router, "SyncSessionLocal", maker)  # workspace bootstrap
    yield maker
    sync_eng.dispose()


@pytest.fixture
def no_clickhouse(monkeypatch):
    """Stub the ClickHouse half of the wipe; returns what the real one would."""
    import tracely.api.routers.admin as admin_router

    calls: list[str] = []

    async def fake(project_id: str) -> dict[str, int]:
        calls.append(project_id)
        return {"events": 7, "scores": 3}

    monkeypatch.setattr(admin_router.deletes, "delete_project_events", fake)
    return calls


async def _owner(client) -> tuple[str, str]:
    r = await client.post("/auth/register", json={"email": "o@x.test", "password": "hunter2-pw"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"})
    return tok, me.json()["project_id"]


def _seed_case(maker, project_id: str, *, with_gate: bool = True) -> str:
    """A case with a replay and (optionally) a gate verdict pointing at it."""
    with maker() as s:
        agent = models.Agent(id=str(uuid4()), project_id=project_id, slug=f"a-{uuid4().hex[:6]}")
        case = models.EvaluationCase(
            id=str(uuid4()),
            project_id=project_id,
            agent_id=agent.id,
            title="refund timeout",
            source_trace_id="tr-1",
            input_digest="d1",
        )
        s.add_all([agent, case])
        s.flush()
        s.add(
            models.CaseReplay(
                id=str(uuid4()), case_id=case.id, candidate_trace_id="tr-1", verdict="FAIL"
            )
        )
        if with_gate:
            gate = models.GateRun(
                id=str(uuid4()), project_id=project_id, agent_id=agent.id, env="ci", status="FAIL"
            )
            s.add(gate)
            s.flush()
            s.add(
                models.GateCase(
                    id=str(uuid4()), gate_run_id=gate.id, evaluation_case_id=case.id, verdict="FAIL"
                )
            )
        s.commit()
        return case.id


# ── DELETE /api/cases/{id} ────────────────────────────────────────────────────


async def test_delete_case_removes_replays_and_gate_verdicts(client, sync_db):
    tok, project_id = await _owner(client)
    keep = _seed_case(sync_db, project_id)
    drop = _seed_case(sync_db, project_id)

    r = await client.delete(f"/api/cases/{drop}", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": drop}

    with sync_db() as s:
        assert [c.id for c in s.execute(select(models.EvaluationCase)).scalars()] == [keep]
        assert list(s.execute(select(models.CaseReplay.case_id)).scalars()) == [keep]
        assert list(s.execute(select(models.GateCase.evaluation_case_id)).scalars()) == [keep]
        # the gate RUN survives — history stays, it just lists one case fewer
        assert len(list(s.execute(select(models.GateRun)).scalars())) == 2


async def test_delete_case_is_project_scoped_and_404s_when_unknown(client, sync_db):
    tok, _ = await _owner(client)
    other = _seed_case(sync_db, "some-other-project")
    h = {"Authorization": f"Bearer {tok}"}

    assert (await client.delete(f"/api/cases/{other}", headers=h)).status_code == 404
    assert (await client.delete(f"/api/cases/{uuid4()}", headers=h)).status_code == 404

    with sync_db() as s:
        assert s.get(models.EvaluationCase, other) is not None


# ── GET /api/traces/{trace_id}/case ───────────────────────────────────────────


async def test_case_for_trace_lookup_is_scoped_and_tracks_delete(client, sync_db):
    tok, project_id = await _owner(client)
    case_id = _seed_case(sync_db, project_id, with_gate=False)  # source_trace_id="tr-1"
    _seed_case(sync_db, "some-other-project", with_gate=False)  # same trace id, other tenant
    h = {"Authorization": f"Bearer {tok}"}

    r = await client.get("/api/traces/tr-1/case", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == case_id

    assert (await client.get("/api/traces/tr-unknown/case", headers=h)).status_code == 404

    # removing the case flips the trace page back to "promote"
    assert (await client.delete(f"/api/cases/{case_id}", headers=h)).status_code == 200
    assert (await client.get("/api/traces/tr-1/case", headers=h)).status_code == 404


# ── DELETE /api/project/data ──────────────────────────────────────────────────


async def test_wipe_requires_exact_confirm(client, sync_db, no_clickhouse):
    tok, project_id = await _owner(client)
    case_id = _seed_case(sync_db, project_id)

    for body in ({}, {"confirm": "delete"}, {"confirm": "yes"}):
        r = await client.request(
            "DELETE", "/api/project/data", headers={"Authorization": f"Bearer {tok}"}, json=body
        )
        assert r.status_code == 400, r.text

    assert no_clickhouse == []  # guard runs before anything is touched
    with sync_db() as s:
        assert s.get(models.EvaluationCase, case_id) is not None


async def test_wipe_clears_derived_data_but_keeps_config(client, sync_db, no_clickhouse):
    tok, project_id = await _owner(client)
    _seed_case(sync_db, project_id)
    with sync_db() as s:
        agent_id = s.execute(select(models.Agent.id)).scalars().first()
        s.add_all(
            [
                models.AgentVersion(id=str(uuid4()), agent_id=agent_id, config_hash="h1"),
                models.FailureEmbedding(
                    project_id=project_id,
                    agent_id=agent_id,
                    trace_id="tr-1",
                    embedding=[0.0] * settings.embedding_dim,
                    summary="boom",
                ),
                models.MetaAnalysis(id=str(uuid4()), project_id=project_id, agent_id=agent_id),
                models.RollingSummary(
                    id=str(uuid4()),
                    project_id=project_id,
                    thread_id="c1",
                    span_id="sp-1",
                ),
                models.ConversationAgent(
                    id=str(uuid4()), project_id=project_id, thread_id="c1", agents=[]
                ),
                models.ScoreAnnotation(
                    id=str(uuid4()),
                    project_id=project_id,
                    trace_id="tr-1",
                    score_name="grounded",
                    judge_verdict="FAIL",
                    human_verdict="PASS",
                ),
                models.Monitor(id=str(uuid4()), project_id=project_id, name="fail rate"),
                # Looks trace-derived, must survive the wipe: it is the BILLING record, and
                # wiping it would make Data → wipe a self-serve monthly quota reset.
                models.UsageCounter(project_id=project_id, period="2026-08", traces=12345),
            ]
        )
        s.commit()
        evaluators_before = len(list(s.execute(select(models.Evaluator)).scalars()))
    assert evaluators_before > 0  # workspace bootstrap seeded the catalog

    r = await client.request(
        "DELETE",
        "/api/project/data",
        headers={"Authorization": f"Bearer {tok}"},
        json={"confirm": "DELETE"},
    )
    assert r.status_code == 200, r.text
    deleted = r.json()["deleted"]
    assert deleted["events"] == 7 and deleted["scores"] == 3  # ClickHouse half reported through
    assert deleted["evaluation_cases"] == 1 and deleted["agents"] == 1
    assert no_clickhouse == [project_id]

    with sync_db() as s:
        for model in (
            models.EvaluationCase,
            models.CaseReplay,
            models.GateRun,
            models.GateCase,
            models.Agent,
            models.AgentVersion,
            models.FailureEmbedding,
            models.MetaAnalysis,
            models.RollingSummary,
            models.ConversationAgent,
            models.ScoreAnnotation,
        ):
            assert list(s.execute(select(model)).scalars()) == [], model.__tablename__
        # configuration survives — the workspace is usable the moment the next trace lands
        assert len(list(s.execute(select(models.Evaluator)).scalars())) == evaluators_before
        assert len(list(s.execute(select(models.Monitor)).scalars())) == 1
        assert len(list(s.execute(select(models.IngestKey)).scalars())) > 0
        assert s.get(models.Project, project_id) is not None
        # …and so does the billing record: a wipe is not a quota reset
        assert s.get(models.UsageCounter, (project_id, "2026-08")).traces == 12345


async def test_wipe_is_project_scoped_and_idempotent(client, sync_db, no_clickhouse):
    tok, _ = await _owner(client)
    other = _seed_case(sync_db, "some-other-project")
    h = {"Authorization": f"Bearer {tok}"}

    for _ in range(2):  # second run has nothing left to do and still succeeds
        r = await client.request(
            "DELETE", "/api/project/data", headers=h, json={"confirm": "DELETE"}
        )
        assert r.status_code == 200, r.text

    with sync_db() as s:
        assert s.get(models.EvaluationCase, other) is not None  # another project is untouched
