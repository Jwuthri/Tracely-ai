"""Manual replay and the CI gate grade a case through ONE path (W2 acceptance #1).

A quality-only case whose required judge cannot run must be INCOMPLETE from both the UI's
`replay_case` and the gate's `_record_gate_cases` — it can no longer pass manually while failing
(or vanishing) in CI. In-memory SQLite registry, fakes for ClickHouse and the judge."""

from __future__ import annotations


import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.evaluation.results import EvalResult
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services.gate_service import GateService
from tracely.services.regression_service import RegressionService

PROJECT = "p1"
SPANS = [
    {"trace_id": "cand", "span_id": "a", "parent_span_id": "", "type": "AGENT", "name": "planner",
     "level": "DEFAULT", "agent_run_id": "r", "is_app_root": True, "tool_call_names": [],
     "output": "answer", "input": "hi", "status_message": "", "metadata": {"tracely.replay.mode": "recorded"}},
    {"trace_id": "cand", "span_id": "t", "parent_span_id": "a", "type": "TOOL", "name": "get_weather",
     "level": "DEFAULT", "agent_run_id": "r", "is_app_root": False, "tool_call_names": [],
     "output": "sunny", "input": "{}", "status_message": "", "metadata": {}},
]


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        models.Evaluator.__table__, models.Agent.__table__, models.EvaluationCase.__table__,
        models.CaseReplay.__table__, models.GateRun.__table__, models.GateCase.__table__,
    ])
    with Session(eng) as s:
        s.add(models.Agent(id="a1", project_id=PROJECT, slug="planner", display_name="planner"))
        s.add(models.EvaluationCase(
            id="c1", project_id=PROJECT, agent_id="a1", level="AGENT_RUN", title="quality-only",
            input_digest="d", status="PROMOTED", origin="MANUAL", source_trace_id="src",
            source_span_id="a", fixture_bundle_s3_key="k", match_mode="superset", version=1,
            created_by="ui",
            assertions={"no_error": True, "required_tools": ["get_weather"], "match_mode": "superset",
                        "quality": {"score_names": ["tracely.run.quality"]}},
        ))
        s.commit()
        yield s
    eng.dispose()


class _Reader:
    def read_spans(self, project_id, trace_id):
        return SPANS if trace_id == "cand" else []

    def candidate_metrics(self, project_id, trace_ids):
        return 0.0, 0, {}


class _Judge:
    """The evaluation service as the grader sees it: a spec + whatever the judge returns."""

    def __init__(self, results):
        self.results = results

    def quality_specs(self, project_id, only_names=None):
        return [{"id": "e1", "score_name": "tracely.run.quality", "kind": "llm_judge",
                 "level": "AGENT_RUN", "config": {"model": "gpt-4o", "rubric": "faithful?"}}]

    def grade_trace_quality(self, project_id, spans, only_names=None, specs=None):
        return self.results


class _NoWrite:
    def write_regression_verdict(self, *a, **k):
        pass


def _manual(db, judge):
    svc = RegressionService(db, trace_reader=_Reader(), eval_service=judge)
    svc.score_writer = _NoWrite()
    return svc.replay_case(PROJECT, "c1", "cand")


def _gate(db, judge):
    svc = GateService(db, trace_reader=_Reader(), eval_service=judge)
    return svc.run_gate(PROJECT, "a1", candidates={"c1": "cand"})


def _gate_case(db):
    return db.execute(select(models.GateCase)).scalar_one()


def test_judge_unavailable_is_incomplete_in_both_paths(db):
    judge = _Judge([])  # no LLM key / judge disabled → nothing comes back
    replay = _manual(db, judge)
    gate = _gate(db, judge)
    assert replay.verdict == "INCOMPLETE"
    assert gate.status == "INCOMPLETE" and gate.incomplete == 1 and gate.failed == 0
    gc = _gate_case(db)
    assert gc.verdict == "INCOMPLETE"
    assert gc.detail["checks"] == replay.detail["checks"]  # literally the same evidence
    assert replay.detail["judges"]["tracely.run.quality"]["evaluator_id"] == "e1"
    assert replay.detail["execution"]["mode"] == "recorded"


def test_failed_judge_fails_in_both_paths(db):
    judge = _Judge([EvalResult(name="tracely.run.quality", level="AGENT_RUN", verdict="FAIL",
                               value=0.1, comment="made up a refund policy")])
    assert _manual(db, judge).verdict == "FAIL"
    gate = _gate(db, judge)
    assert gate.status == "FAIL" and _gate_case(db).detail["quality_reason"] == "made up a refund policy"


def test_passing_judge_passes_in_both_paths(db):
    judge = _Judge([EvalResult(name="tracely.run.quality", level="AGENT_RUN", verdict="PASS", value=0.9)])
    assert _manual(db, judge).verdict == "PASS"
    assert _gate(db, judge).status == "PASS"


def test_judge_is_not_called_when_the_execution_did_not_complete(db):
    class _Boom(_Judge):
        def grade_trace_quality(self, *a, **k):
            raise AssertionError("judge must not run on an incomplete execution")

    broken = [dict(SPANS[0], level="ERROR", status_message="replay error: no recorded call for tools:get_weather"), SPANS[1]]
    reader = _Reader()
    reader.read_spans = lambda p, t: broken if t == "cand" else []
    svc = RegressionService(db, trace_reader=reader, eval_service=_Boom([]))
    svc.score_writer = _NoWrite()
    assert svc.replay_case(PROJECT, "c1", "cand").verdict == "INCOMPLETE"
