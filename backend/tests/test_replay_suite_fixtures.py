"""`GET /gate/suite` fixture handling (W1): a recording that cannot be loaded is an explicit
execution problem on the case, never an empty bundle that would replay as "nothing recorded" or
fall back to live calls."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.regression.fixtures import FixtureBundle
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import gate_service as gs

PROJECT = "p1"


@pytest.fixture
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng, tables=[models.Agent.__table__, models.EvaluationCase.__table__]
    )
    with Session(eng) as s:
        yield s
    eng.dispose()


class _Reader:
    def read_spans(self, project_id, trace_id):
        return [{"input": "hello"}]


def _case(db, key: str | None) -> models.EvaluationCase:
    agent = models.Agent(id="a1", project_id=PROJECT, slug="planner", display_name="planner")
    db.add(agent)
    c = models.EvaluationCase(
        id=str(uuid.uuid4()), project_id=PROJECT, agent_id="a1", level="AGENT_RUN",
        title="t", input_digest="d", status="PROMOTED", origin="MANUAL",
        source_trace_id="src", source_span_id="s", fixture_bundle_s3_key=key,
        assertions={}, match_mode="superset", version=1, created_by="ui",
    )
    db.add(c)
    db.commit()
    return c


def _suite(db, monkeypatch, blob):
    monkeypatch.setattr(gs.blobstore, "get_blob", blob)
    svc = gs.GateService(db, trace_reader=_Reader(), eval_service=object())
    return svc.replay_suite(PROJECT, "a1")


def test_loaded_bundle_is_returned(db, monkeypatch):
    _case(db, "fixtures/p1/d.json")
    raw = FixtureBundle(tools=[], llm=[]).encode()
    [row] = _suite(db, monkeypatch, lambda key: raw)
    assert row["fixtures"] == json.loads(raw) and row["fixture_error"] is None


def test_unreadable_bundle_propagates_as_fixture_error(db, monkeypatch):
    _case(db, "fixtures/p1/d.json")

    def boom(key):
        raise OSError("NoSuchKey")

    [row] = _suite(db, monkeypatch, boom)
    assert row["fixtures"] is None
    assert "NoSuchKey" in row["fixture_error"] and "fixtures/p1/d.json" in row["fixture_error"]


def test_case_without_recording_is_flagged(db, monkeypatch):
    _case(db, None)
    [row] = _suite(db, monkeypatch, lambda key: pytest.fail("must not read the blob store"))
    assert row["fixtures"] is None and "no fixture bundle" in row["fixture_error"]


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]"])
def test_decode_rejects_garbage(raw):
    with pytest.raises(ValueError):
        FixtureBundle.decode(raw)


def test_decode_round_trips():
    b = FixtureBundle.capture([{"type": "TOOL", "name": "t", "input": '{"a": 1}', "output": "o"}])
    assert FixtureBundle.decode(b.encode())["tools"][0]["args"] == '{"a": 1}'
