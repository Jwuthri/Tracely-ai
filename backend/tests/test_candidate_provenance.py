"""Exact candidate identity (W4): a gate result refers to THIS execution's traces.

Reader stand-in whose traces carry `tracely.replay.run_id`. Pins every deterministic outcome:
this run's trace → graded + candidate verification recorded; an older trace with the identical
input (a retry, a stale CI run) → INCOMPLETE, never paired; a missing / wrong-project id →
INCOMPLETE; a failed command → INCOMPLETE even though a matching trace exists; no run id →
legacy digest pairing, warned, never verified; source failure alone never shows as verified."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.traces.spans import input_digest
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services.gate_service import GateService
from tracely.services.regression_service import RegressionService

PROJECT = "p1"


def _trace(tid: str, run_id: str, text: str = "hi", *, level: str = "DEFAULT") -> list[dict]:
    meta = {"tracely.replay.run_id": run_id, "tracely.replay.mode": "recorded"} if run_id else {}
    return [
        {"trace_id": tid, "span_id": "a", "parent_span_id": "", "type": "AGENT", "name": "planner",
         "level": level, "status_message": "" if level == "DEFAULT" else "agent raised: x",
         "agent_id": "a1", "agent_run_id": "r", "is_app_root": True, "tool_call_names": [],
         "input": text, "output": "ok", "metadata": meta},
        {"trace_id": tid, "span_id": "t", "parent_span_id": "a", "type": "TOOL", "name": "get_weather",
         "level": "DEFAULT", "status_message": "", "agent_id": "a1", "agent_run_id": "r",
         "is_app_root": False, "tool_call_names": [], "input": "{}", "output": "sunny", "metadata": {}},
    ]


class _Reader:
    def __init__(self, traces: dict[str, list[dict]]):
        self.traces = traces

    def read_spans(self, project_id, trace_id):
        return self.traces.get(trace_id, []) if project_id == PROJECT else []

    def traces_for_run(self, project_id, agent_id, run_id, limit=500):
        return [t for t, spans in self.traces.items() if spans[0]["metadata"].get("tracely.replay.run_id") == run_id]

    def latest_traces_for_env(self, project_id, agent_id, env, limit=300):
        return list(self.traces)

    def candidate_metrics(self, project_id, trace_ids):
        return 0.0, 0, {}


class _NoJudge:
    def quality_specs(self, *a, **k):
        return []

    def grade_trace_quality(self, *a, **k):
        return []


class _NoWrite:
    def write_regression_verdict(self, *a, **k):
        pass


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        models.Agent.__table__, models.EvaluationCase.__table__, models.CaseReplay.__table__,
        models.GateRun.__table__, models.GateCase.__table__,
    ])
    with Session(eng) as s:
        s.add(models.Agent(id="a1", project_id=PROJECT, slug="planner", display_name="planner"))
        s.add(models.EvaluationCase(
            id="c1", project_id=PROJECT, agent_id="a1", level="AGENT_RUN", title="t",
            input_digest=input_digest(_trace("x", "")), status="PROMOTED", origin="MANUAL",
            source_trace_id="src", source_span_id="a", fixture_bundle_s3_key="k", match_mode="superset",
            version=1, created_by="ui", fail_to_pass_validated=True,
            assertions={"no_error": True, "required_tools": ["get_weather"], "match_mode": "superset"},
        ))
        s.commit()
        yield s
    eng.dispose()


def _gate(db, reader, **kw):
    return GateService(db, trace_reader=reader, eval_service=_NoJudge()).run_gate(PROJECT, "a1", **kw)


def _gc(db):
    return db.execute(select(models.GateCase)).scalar_one()


def _case(db):
    return db.get(models.EvaluationCase, "c1")


def test_this_runs_candidate_passes_and_verifies_the_case(db):
    reader = _Reader({"new": _trace("new", "run-1"), "old": _trace("old", "run-0")})
    g = _gate(db, reader, candidates={"c1": "new"}, run_id="run-1", execution_mode="recorded")
    assert g.status == "PASS" and g.run_id == "run-1" and g.execution_mode == "recorded"
    gc = _gc(db)
    assert gc.candidate_trace_id == "new" and gc.detail["provenance"] == {
        "run_id": "run-1", "pairing": "run", "case_version": 1, "artifact_digest": "",
    }
    c = _case(db)
    assert c.verified_candidate_trace_id == "new" and c.verified_case_version == 1
    assert c.verified_by == f"gate:{g.id}" and c.verified_at is not None
    assert not any("run id" in w for w in g.warnings)


def test_an_older_trace_with_identical_input_cannot_satisfy_this_run(db):
    reader = _Reader({"old": _trace("old", "run-0")})  # same input digest, previous run
    # explicit stale id
    g = _gate(db, reader, candidates={"c1": "old"}, run_id="run-1")
    assert g.status == "INCOMPLETE" and g.incomplete == 1
    assert "not produced by this run" in _gc(db).detail["reason"]
    assert _case(db).verified_candidate_trace_id == ""  # source failure alone ≠ verified
    db.execute(models.GateCase.__table__.delete())
    db.commit()
    # --cmd style: no explicit ids, digest matching is scoped to the run → nothing from run-1
    g = _gate(db, reader, run_id="run-1")
    assert g.status == "INCOMPLETE" and "no trace from this run" in _gc(db).detail["reason"]


def test_missing_and_wrong_project_candidates_are_incomplete(db):
    reader = _Reader({"new": _trace("new", "run-1")})
    g = _gate(db, reader, candidates={"c1": "nope"}, run_id="run-1")
    assert g.status == "INCOMPLETE" and "not found" in _gc(db).detail["reason"]


def test_a_wrong_input_candidate_is_incomplete(db):
    reader = _Reader({"other": _trace("other", "run-1", text="a different question")})
    g = _gate(db, reader, candidates={"c1": "other"}, run_id="run-1")
    assert g.status == "INCOMPLETE" and "input differs" in _gc(db).detail["reason"]


def test_a_failed_command_is_not_rescued_by_a_matching_trace(db):
    reader = _Reader({"new": _trace("new", "run-1")})  # a perfectly good trace from this run…
    g = _gate(db, reader, run_id="run-1", failed={"c1": "command exited 1"}, execution_mode="live")
    assert g.status == "INCOMPLETE"
    d = _gc(db).detail
    assert d["reason"] == "command failed: command exited 1" and d["execution"]["mode"] == "live"


def test_cmd_style_pairs_by_digest_within_the_run(db):
    reader = _Reader({"old": _trace("old", "run-0"), "new": _trace("new", "run-1")})
    g = _gate(db, reader, run_id="run-1")
    assert g.status == "PASS" and _gc(db).candidate_trace_id == "new"


def test_no_run_id_is_legacy_pairing_warned_and_never_verified(db):
    reader = _Reader({"old": _trace("old", "")})
    g = _gate(db, reader)
    assert g.status == "PASS" and _gc(db).detail["provenance"]["pairing"] == "digest-fallback"
    assert any("run id" in w for w in g.warnings)
    assert _case(db).verified_candidate_trace_id == ""


def test_a_failing_candidate_from_this_run_is_fail_not_verified(db):
    reader = _Reader({"bad": _trace("bad", "run-1", level="ERROR")})
    g = _gate(db, reader, candidates={"c1": "bad"}, run_id="run-1")
    assert g.status == "FAIL" and _case(db).verified_candidate_trace_id == ""


def test_manual_replay_of_a_candidate_verifies_but_the_source_does_not(db):
    reader = _Reader({"src": _trace("src", "", level="ERROR"), "fix": _trace("fix", "")})
    svc = RegressionService(db, trace_reader=reader, eval_service=_NoJudge())
    svc.score_writer = _NoWrite()
    assert svc.replay_case(PROJECT, "c1", "src").verdict == "FAIL"
    assert _case(db).verified_candidate_trace_id == ""
    assert svc.replay_case(PROJECT, "c1", "fix").verdict == "PASS"
    c = _case(db)
    assert c.verified_candidate_trace_id == "fix" and c.verified_by == "replay"


def test_verification_is_scoped_to_the_case_version(db):
    reader = _Reader({"fix": _trace("fix", "run-1")})
    _gate(db, reader, candidates={"c1": "fix"}, run_id="run-1")
    c = _case(db)
    c.version = 2  # the contract changed (recapture / edited expectations)
    db.commit()
    assert c.verified_case_version == 1 != c.version  # stale: the UI must not show "verified"
