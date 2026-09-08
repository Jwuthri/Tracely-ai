"""Durable, versioned case artifacts (W3).

Pins: a promoted case is snapshotted (input + fixtures + expectations + judge identity +
provenance) and still executes after its source spans are gone; a missing/corrupt artifact is an
explicit error on the suite, never an empty input; backfill snapshots what it can and names what
it cannot; recapture needs the same input digest and bumps the version; deleting a case deletes
its blobs while routine retention never does. In-memory SQLite + a dict for the blob store."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.regression.artifact import CaseArtifact, artifact_key
from tracely.domain.regression.fixtures import FixtureBundle
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.infrastructure.db import repositories as repo
from tracely.services import gate_service as gs
from tracely.services import regression_service as rs

PROJECT = "p1"


def _spans(trace_id: str = "src", input_text: str = "what's the weather in Paris?"):
    return [
        {"trace_id": trace_id, "span_id": "a", "parent_span_id": "", "type": "AGENT", "name": "planner",
         "level": "ERROR", "status_message": "boom", "agent_id": "a1", "agent_run_id": "r",
         "is_app_root": True, "tool_call_names": ["get_weather"], "input": input_text,
         "output": None, "metadata": {}},
        {"trace_id": trace_id, "span_id": "t", "parent_span_id": "a", "type": "TOOL", "name": "get_weather",
         "level": "DEFAULT", "status_message": "", "agent_id": "a1", "agent_run_id": "r",
         "is_app_root": False, "tool_call_names": [], "input": '{"city": "Paris"}',
         "output": '{"temp": 21}', "metadata": {}},
    ]


class _Reader:
    """ClickHouse stand-in whose traces can be 'retained away' mid-test."""

    def __init__(self):
        self.traces: dict[str, list[dict]] = {"src": _spans()}

    def read_spans(self, project_id, trace_id):
        return self.traces.get(trace_id, [])

    def candidate_metrics(self, project_id, trace_ids):
        return 0.0, 0, {}


class _NoJudge:
    def grade_trace_quality(self, *a, **k):
        return []

    def quality_specs(self, *a, **k):
        return []


class _NoWrite:
    def write_regression_verdict(self, *a, **k):
        pass


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        models.Agent.__table__, models.EvaluationCase.__table__, models.EvaluationSuite.__table__,
        models.EvaluationSuiteCase.__table__, models.CaseReplay.__table__, models.GateRun.__table__,
        models.GateCase.__table__,
    ])
    with Session(eng) as s:
        s.add(models.Agent(id="a1", project_id=PROJECT, slug="planner", display_name="planner"))
        s.commit()
        yield s
    eng.dispose()


@pytest.fixture
def blobs(monkeypatch):
    store: dict[str, bytes] = {}

    def put(key, body, content_type="application/octet-stream"):
        store[key] = body

    def get(key):
        if key not in store:
            raise OSError("NoSuchKey")
        return store[key]

    def delete_prefix(prefix):
        gone = [k for k in store if k.startswith(prefix)]
        for k in gone:
            del store[k]
        return len(gone)

    for mod in (rs.blobstore, gs.blobstore):
        monkeypatch.setattr(mod, "put_blob", put)
        monkeypatch.setattr(mod, "get_blob", get)
        monkeypatch.setattr(mod, "_delete_prefix", delete_prefix)
    return store


def _svc(db, reader):
    svc = rs.RegressionService(db, trace_reader=reader, eval_service=_NoJudge())
    svc.score_writer = _NoWrite()
    return svc


def _suite(db, reader):
    return gs.GateService(db, trace_reader=reader, eval_service=_NoJudge()).replay_suite(PROJECT, "a1")


# ── domain ────────────────────────────────────────────────────────────────────


def test_artifact_round_trips_and_digest_is_content_bound():
    art = CaseArtifact.build(
        case_id="c", case_version=1, input_text="hi", fixtures={"version": 2, "tools": [], "llm": []},
        assertions={"no_error": True}, match_mode="superset", evaluators={},
        provenance={"source_trace_id": "src", "captured_at": "2026-09-07T00:00:00+00:00"},
    )
    back = CaseArtifact.decode(art.encode())
    assert back.input_text == "hi" and back.digest() == art.digest()
    other = CaseArtifact.build(
        case_id="c", case_version=1, input_text="hi!", fixtures=art.fixtures, assertions={"no_error": True},
        match_mode="superset", evaluators={}, provenance=art.provenance,
    )
    assert other.digest() != art.digest()
    assert back.initial_state["captured"] is False  # the boundary is stated, not hidden


def test_artifact_never_invents_an_empty_input():
    with pytest.raises(ValueError, match="no executable input"):
        CaseArtifact.build(case_id="c", case_version=1, input_text="", fixtures={}, assertions={},
                           match_mode="superset", evaluators={}, provenance={})


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]", b'{"input": "x"}', b'{"input": "x", "fixtures": {}, "format_version": 99}'])
def test_artifact_decode_is_strict(raw):
    with pytest.raises(ValueError):
        CaseArtifact.decode(raw)


# ── promote → survives retention ──────────────────────────────────────────────


def test_promoted_case_executes_after_its_source_is_retained_away(db, blobs):
    reader = _Reader()
    case = _svc(db, reader).promote_trace(PROJECT, "src")
    assert case.status == "PROMOTED"
    key = artifact_key("events/", PROJECT, case.id, 1)
    assert case.artifact_s3_key == key and key in blobs and len(case.artifact_digest) == 64
    art = json.loads(blobs[key])
    assert art["input"] == {"type": "text", "value": "what's the weather in Paris?"}
    assert art["fixtures"]["tools"][0]["name"] == "get_weather"
    assert art["expectations"]["assertions"]["required_tools"] == ["get_weather"]
    assert art["provenance"]["source_trace_id"] == "src" and art["provenance"]["input_digest"] == case.input_digest

    reader.traces.clear()  # the retention path: the source spans are gone
    [row] = _suite(db, reader)
    assert row["fixture_error"] is None
    assert row["input"] == "what's the weather in Paris?"
    assert row["fixtures"]["tools"][0]["output"] == '{"temp": 21}'
    assert row["case_version"] == 1 and row["artifact_digest"] == case.artifact_digest


def test_corrupt_artifact_is_an_explicit_error_not_an_empty_case(db, blobs):
    reader = _Reader()
    case = _svc(db, reader).promote_trace(PROJECT, "src")
    blobs[case.artifact_s3_key] = b"garbage"
    reader.traces.clear()
    [row] = _suite(db, reader)
    assert row["fixtures"] is None and "could not be loaded" in row["fixture_error"]


def test_blob_store_down_fails_the_promote_whole(db, blobs, monkeypatch):
    def down(*a, **k):
        raise OSError("s3 down")

    monkeypatch.setattr(rs.blobstore, "put_blob", down)
    with pytest.raises(OSError):
        _svc(db, _Reader()).promote_trace(PROJECT, "src")
    assert repo.cases_count(db, PROJECT) == 0  # no half-promoted case that would not survive


# ── legacy cases: backfill + recapture ────────────────────────────────────────


def _legacy_case(db, blobs, *, with_fixtures=True):
    key = "events/fixtures/p1/d.json" if with_fixtures else ""
    if with_fixtures:
        blobs[key] = FixtureBundle.capture(_spans()).encode()
    c = models.EvaluationCase(
        id="legacy", project_id=PROJECT, agent_id="a1", level="AGENT_RUN", title="old",
        input_digest=rs.input_digest(_spans()), status="PROMOTED", origin="MANUAL",
        source_trace_id="src", source_span_id="a", fixture_bundle_s3_key=key,
        assertions={"no_error": True, "required_tools": ["get_weather"], "match_mode": "superset"},
        match_mode="superset", version=1, created_by="ui",
    )
    db.add(c)
    db.commit()
    return c


def test_legacy_case_without_artifact_reads_source_while_it_exists(db, blobs):
    _legacy_case(db, blobs)
    [row] = _suite(db, _Reader())
    assert row["fixture_error"] is None and row["input"] and row["artifact_digest"] == ""


def test_legacy_case_with_expired_source_is_unrecoverable_on_the_suite(db, blobs):
    _legacy_case(db, blobs)
    reader = _Reader()
    reader.traces.clear()
    [row] = _suite(db, reader)
    assert row["fixtures"] is None and row["input"] == ""
    assert "input unrecoverable" in row["fixture_error"] and "recapture" in row["fixture_error"]


def test_backfill_snapshots_from_source_and_names_the_unrecoverable(db, blobs):
    c = _legacy_case(db, blobs)
    reader = _Reader()
    res = _svc(db, reader).backfill_artifacts(PROJECT)
    assert res == {"snapshotted": ["legacy"], "unrecoverable": {}}
    db.refresh(c)
    assert c.artifact_s3_key and c.artifact_digest
    reader.traces.clear()
    assert _suite(db, reader)[0]["fixture_error"] is None  # now durable

    # a second case whose source is already gone: reported, never invented
    other = models.EvaluationCase(
        id="gone", project_id=PROJECT, agent_id="a1", level="AGENT_RUN", title="gone",
        input_digest="x", status="PROMOTED", origin="MANUAL", source_trace_id="expired",
        source_span_id="a", fixture_bundle_s3_key="", assertions={}, match_mode="superset",
        version=1, created_by="ui",
    )
    db.add(other)
    db.commit()
    res = _svc(db, reader).backfill_artifacts()
    assert res["snapshotted"] == [] and "recapture" in res["unrecoverable"]["gone"]
    assert _svc(db, reader).backfill_artifacts(PROJECT)["snapshotted"] == []  # idempotent


def test_recapture_requires_the_same_input_and_bumps_the_version(db, blobs):
    c = _legacy_case(db, blobs, with_fixtures=False)
    reader = _Reader()
    reader.traces = {"fresh": _spans("fresh"), "other": _spans("other", "a different question")}
    svc = _svc(db, reader)
    with pytest.raises(ValueError, match="input digest"):
        svc.recapture(PROJECT, "legacy", "other")
    with pytest.raises(rs.NotFound):
        svc.recapture("someone-else", "legacy", "fresh")  # scoped
    art = svc.recapture(PROJECT, "legacy", "fresh")
    db.refresh(c)
    assert c.version == 2 and art.case_version == 2
    assert c.artifact_s3_key == artifact_key("events/", PROJECT, "legacy", 2)
    assert c.fixture_bundle_s3_key.endswith(f"/{c.input_digest}.json")


# ── deletion is deliberate; retention is not ──────────────────────────────────


def test_deleting_a_case_removes_its_artifact_and_fixtures(db, blobs):
    case = _svc(db, _Reader()).promote_trace(PROJECT, "src")
    assert any(k.startswith(f"events/cases/{PROJECT}/{case.id}/") for k in blobs)
    assert repo.case_delete(db, PROJECT, case.id)
    assert not any(k.startswith("events/cases/") for k in blobs)
    assert not any(k.startswith("events/fixtures/") for k in blobs)


def test_gate_result_pins_case_version_and_artifact(db, blobs):
    reader = _Reader()
    case = _svc(db, reader).promote_trace(PROJECT, "src")
    reader.traces["cand"] = [{**s, "level": "DEFAULT", "status_message": ""} for s in _spans("cand")]
    gate = gs.GateService(db, trace_reader=reader, eval_service=_NoJudge()).run_gate(
        PROJECT, "a1", candidates={case.id: "cand"}
    )
    [gc] = [r for r in db.query(models.GateCase).all()]
    assert gate.status == "PASS"
    assert gc.detail["case_version"] == 1 and gc.detail["artifact_digest"] == case.artifact_digest
    assert gc.detail["expectations"]["assertions"]["required_tools"] == ["get_weather"]
