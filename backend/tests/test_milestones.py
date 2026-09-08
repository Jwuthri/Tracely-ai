"""Activation milestones (W6): durable, idempotent, sample-aware, and only from real outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.regression.outcome import Execution, evaluate_contract
from tracely.domain.trajectory import build_trajectory
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import case_grading, gate_service, milestones

PROJECT = "p1"


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[models.Project.__table__, models.ProjectMilestone.__table__])
    with Session(eng) as s:
        s.add(models.Project(id=PROJECT, name="p", slug="p"))
        s.commit()
        yield s
    eng.dispose()


def test_record_is_first_wins_and_measures_elapsed(db):
    assert milestones.record(db, PROJECT, "first_trace_received", integration="tracely") is True
    assert milestones.record(db, PROJECT, "first_trace_received") is False  # idempotent
    first = db.get(models.ProjectMilestone, (PROJECT, "first_trace_received"))
    first.first_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()
    assert milestones.record(db, PROJECT, "candidate_verified", sample=True) is True
    row = db.get(models.ProjectMilestone, (PROJECT, "candidate_verified"))
    assert row.sample is True and row.elapsed_ms >= 5 * 60 * 1000 - 1000
    with pytest.raises(ValueError):
        milestones.record(db, PROJECT, "clicked_a_button")


def test_for_project_lists_every_milestone_in_order(db):
    milestones.record(db, PROJECT, "first_trace_received")
    items = milestones.for_project(db, PROJECT)
    assert [i["name"] for i in items] == list(milestones.MILESTONES)
    assert items[0]["first_at"] and items[1]["first_at"] is None


def test_funnel_counts_real_vs_sample_and_drop_off(db):
    db.add(models.Project(id="p2", name="q", slug="q"))
    db.commit()
    milestones.record(db, PROJECT, "first_trace_received")
    milestones.record(db, PROJECT, "first_check_completed")
    milestones.record(db, "p2", "first_trace_received", sample=True)
    f = milestones.funnel(db)
    by = {s["name"]: s for s in f["steps"]}
    assert by["first_trace_received"] == {"name": "first_trace_received", "real": 1, "sample": 1, "drop_off": None}
    assert by["first_check_completed"]["real"] == 1 and by["first_check_completed"]["drop_off"] == 0
    assert by["source_failure_confirmed"]["drop_off"] == 1
    assert f["projects_started"] == 1


def test_sample_and_integration_detection():
    assert milestones.is_sample([{"metadata": {"tracely.sample": "true"}}])
    assert milestones.is_sample([{"env": "demo", "metadata": {}}])
    assert not milestones.is_sample([{"env": "prod", "metadata": {}}])
    assert milestones.integration_of([{"scope_name": "openinference.instrumentation.openai"}]) == "openinference.instrumentation.openai"
    assert milestones.integration_of([{"telemetry_sdk_language": "go"}]) == "otlp:go"


# ── the hooks fire only on confirmed outcomes ─────────────────────────────────


def _spans(level="DEFAULT", mode="recorded"):
    return [
        {"trace_id": "t", "span_id": "a", "parent_span_id": "", "type": "AGENT", "name": "planner",
         "level": level, "status_message": "", "agent_run_id": "r", "is_app_root": True,
         "tool_call_names": [], "output": "x", "input": "hi", "metadata": {"tracely.replay.mode": mode}},
    ]


class _Case:
    project_id = PROJECT
    source_trace_id = "src"


def _outcome(verdict, mode="recorded", complete=True):
    ex = Execution(mode=mode, problem="" if complete else "replay error: x")
    traj = build_trajectory(_spans("ERROR" if verdict == "FAIL" else "DEFAULT"))
    return evaluate_contract({"no_error": True, "required_tools": []}, "superset", traj, execution=ex)


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(milestones, "record_own", lambda pid, name, **kw: seen.append((name, kw)) or True)
    return seen


def test_case_reproduced_needs_recorded_complete_fail_on_a_candidate(calls):
    case_grading.record_case_milestones(None, _Case(), _outcome("FAIL"), "cand", _spans(), verified=False)
    assert [c[0] for c in calls] == ["case_reproduced"]
    calls.clear()
    case_grading.record_case_milestones(None, _Case(), _outcome("FAIL"), "src", _spans(), verified=False)  # the source
    case_grading.record_case_milestones(None, _Case(), _outcome("FAIL", mode="live"), "cand", _spans(), verified=False)
    case_grading.record_case_milestones(None, _Case(), _outcome("PASS", complete=False), "cand", _spans(), verified=False)
    assert calls == []
    case_grading.record_case_milestones(None, _Case(), _outcome("PASS"), "cand", _spans(), verified=True)
    assert [c[0] for c in calls] == ["candidate_verified"]


def test_ci_check_completed_needs_a_real_scoped_graded_gate(calls):
    svc = gate_service.GateService.__new__(gate_service.GateService)
    g = models.GateRun(id="g", project_id=PROJECT, agent_id="a", run_id="run-1", total=2, status="FAIL")
    svc._milestone_ci(g, [_spans()])
    assert calls[0][0] == "ci_check_completed" and calls[0][1]["meta"] == {"gate_id": "g", "status": "FAIL"}
    calls.clear()
    for bad in (
        models.GateRun(id="g", project_id=PROJECT, agent_id="a", run_id="", total=2, status="PASS"),   # unscoped
        models.GateRun(id="g", project_id=PROJECT, agent_id="a", run_id="r", total=0, status="PASS"),  # empty suite
        models.GateRun(id="g", project_id=PROJECT, agent_id="a", run_id="r", total=2, status="NO_COVERAGE"),
        models.GateRun(id="g", project_id=PROJECT, agent_id="a", run_id="r", total=2, status="INCOMPLETE"),
    ):
        svc._milestone_ci(bad, [_spans()])
    assert calls == []
