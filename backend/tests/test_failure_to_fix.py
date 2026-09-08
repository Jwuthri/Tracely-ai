"""The failure-to-fix workspace's backend (W5): editable expectations that discriminate,
original-vs-candidate alignment, and exact candidate discovery."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tracely.domain.regression.compare import align_steps
from tracely.domain.regression.contract import check_tool_args, evaluate_assertions
from tracely.domain.regression.outcome import evaluate_contract
from tracely.domain.trajectory import build_trajectory
from tracely.infrastructure.db import models
from tracely.infrastructure.db.base import Base
from tracely.services import regression_service as rs

PROJECT = "p1"


def _span(sid, type_, name, *, parent="a", level="DEFAULT", input=None, output=None):
    return {"trace_id": "t", "span_id": sid, "parent_span_id": parent if sid != "a" else "",
            "type": type_, "name": name, "level": level, "status_message": "",
            "agent_id": "a1", "agent_run_id": "r", "is_app_root": sid == "a",
            "tool_call_names": [], "input": input, "output": output, "metadata": {}}


def _run(*tools, level="DEFAULT", text="refund order 42"):
    """agent root + tool steps; each tool is (name, args_json)."""
    spans = [_span("a", "AGENT", "planner", level=level, input=text, output="done")]
    spans += [_span(f"t{i}", "TOOL", n, input=a, output="ok") for i, (n, a) in enumerate(tools)]
    return spans


# ── contract: the new editable assertions ─────────────────────────────────────


def test_bad_argument_failure_becomes_a_discriminating_test():
    """The W5 headline: original passes wrong args → FAIL; the intended fix → PASS."""
    a = {"required_tools": ["lookup"], "tool_args": [{"tool": "lookup", "key": "order_id", "equals": "42"}]}
    buggy = build_trajectory(_run(("lookup", '{"order_id": "24"}')))
    fixed = build_trajectory(_run(("lookup", '{"order_id": "42"}')))
    assert evaluate_assertions(a, "superset", buggy)[0] == "FAIL"
    assert evaluate_assertions(a, "superset", fixed)[0] == "PASS"
    [v] = check_tool_args(a["tool_args"], buggy)
    assert v.startswith("lookup: 'order_id' was") and "expected" in v
    out = evaluate_contract(a, "superset", buggy)
    assert {c.name: c.status for c in out.checks}["tool_args"] == "FAIL"


def test_observe_style_kwargs_and_dotted_keys():
    a = [{"tool": "t", "key": "filters.city", "equals": "Paris"}]
    ok = build_trajectory(_run(("t", '{"kwargs": {"filters": {"city": "Paris"}}}')))
    missing = build_trajectory(_run(("t", '{"kwargs": {"filters": {}}}')))
    assert check_tool_args(a, ok) == [] and check_tool_args(a, missing) == ["t: argument 'filters.city' missing"]


def test_forbidden_and_bounded_calls():
    a = {"required_tools": ["lookup"], "forbidden_tools": ["delete_account"], "max_tool_calls": {"lookup": 1}}
    bad = build_trajectory(_run(("lookup", "{}"), ("lookup", "{}"), ("delete_account", "{}")))
    good = build_trajectory(_run(("lookup", "{}")))
    verdict, d = evaluate_assertions(a, "superset", bad)
    assert verdict == "FAIL" and d["forbidden_hit"] == ["delete_account"] and d["count_violations"] == ["lookup: called 2×, max 1"]
    assert evaluate_assertions(a, "superset", good)[0] == "PASS"
    checks = {c.name: c.status for c in evaluate_contract(a, "superset", bad).checks}
    assert checks["forbidden_tools"] == "FAIL" and checks["call_counts"] == "FAIL"


def test_harmless_alternative_path_passes_when_allowed():
    """superset: an extra, non-forbidden tool is fine; strict would reject it."""
    a = {"required_tools": ["lookup"], "match_mode": "superset"}
    alt = build_trajectory(_run(("check_policy", "{}"), ("lookup", "{}")))
    assert evaluate_assertions(a, "superset", alt)[0] == "PASS"
    assert evaluate_assertions({**a, "match_mode": "strict"}, "strict", alt)[0] == "FAIL"


# ── compare ───────────────────────────────────────────────────────────────────


def test_align_names_the_first_meaningful_divergence():
    ref = build_trajectory(_run(("lookup", '{"id": "24"}'), ("refund", '{"amount": 5}'), level="ERROR")).to_json()["steps"]
    cand = build_trajectory(_run(("lookup", '{"id": "42"}'), ("notify", "{}"))).to_json()["steps"]
    res = align_steps(ref, cand)
    assert res["first_divergence"] == 0
    assert res["rows"][0]["divergence"] == "arguments differ"
    assert res["rows"][1]["divergence"] == "refund → notify"
    assert res["rows"][2]["ref"]["name"] == "planner" and "not called" in res["rows"][2]["divergence"]  # the errored root


def test_align_identical_runs_have_no_divergence():
    steps = build_trajectory(_run(("lookup", '{"id": 1}'))).to_json()["steps"]
    res = align_steps(steps, steps)
    assert res["first_divergence"] is None and res["rows"][0]["op"] == "equal"


# ── service: edit expectations, discover candidates ───────────────────────────


class _Reader:
    def __init__(self):
        self.traces = {"src": _run(("lookup", '{"order_id": "24"}'), level="ERROR")}
        self.with_input: list[dict] = []

    def read_spans(self, project_id, trace_id):
        return self.traces.get(trace_id, [])

    def traces_with_input(self, project_id, agent_id, text, limit=20):
        return list(self.with_input)


class _NoJudge:
    def grade_trace_quality(self, *a, **k):
        return []

    def quality_specs(self, *a, **k):
        return []


class _NoWrite:
    def write_regression_verdict(self, *a, **k):
        pass


@pytest.fixture
def svc(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        models.Agent.__table__, models.EvaluationCase.__table__, models.EvaluationSuite.__table__,
        models.EvaluationSuiteCase.__table__, models.CaseReplay.__table__,
    ])
    store: dict[str, bytes] = {}
    monkeypatch.setattr(rs.blobstore, "put_blob", lambda k, b, ct="": store.__setitem__(k, b))
    monkeypatch.setattr(rs.blobstore, "get_blob", lambda k: store[k])
    s = Session(eng)
    s.add(models.Agent(id="a1", project_id=PROJECT, slug="planner", display_name="planner"))
    s.commit()
    service = rs.RegressionService(s, trace_reader=_Reader(), eval_service=_NoJudge())
    service.score_writer = _NoWrite()
    yield service
    s.close()
    eng.dispose()


def test_edit_expectations_bumps_version_resnapshots_and_revalidates(svc):
    case = svc.promote_trace(PROJECT, "src")
    v1_key, v1_digest = case.artifact_s3_key, case.artifact_digest
    assert case.status == "PROMOTED"  # the source errored → discriminates structurally

    # Make the source clean so only the argument predicate can discriminate.
    svc.trace_reader.traces["src"] = _run(("lookup", '{"order_id": "24"}'))
    case = svc.update_expectations(PROJECT, case.id, {"no_error": False})
    assert case.version == 2 and case.status == "DRAFT"  # source now passes → non-discriminating, honest
    case = svc.update_expectations(PROJECT, case.id, {"tool_args": [{"tool": "lookup", "key": "order_id", "equals": "42"}]})
    assert case.version == 3 and case.status == "PROMOTED" and case.fail_to_pass_validated
    assert case.artifact_s3_key.endswith("/v3.json") and case.artifact_digest != v1_digest
    assert case.artifact_s3_key != v1_key

    with pytest.raises(ValueError, match="unknown assertion"):
        svc.update_expectations(PROJECT, case.id, {"quality": {}})
    with pytest.raises(ValueError, match="match_mode"):
        svc.update_expectations(PROJECT, case.id, {"match_mode": "fuzzy"})
    with pytest.raises(rs.NotFound):
        svc.update_expectations("other", case.id, {"no_error": True})


def test_candidates_are_exact_input_matches_with_replay_state(svc):
    case = svc.promote_trace(PROJECT, "src")
    svc.trace_reader.traces["fix"] = _run(("lookup", '{"order_id": "42"}'))
    svc.trace_reader.with_input = [
        {"trace_id": "fix", "env": "ci", "ts": "2026-09-07T10:00:00", "run_id": "run-1", "level": "DEFAULT"},
        {"trace_id": "src", "env": "prod", "ts": "2026-09-06T10:00:00", "run_id": "", "level": "ERROR"},
    ]
    svc.replay_case(PROJECT, case.id, "fix")
    rows = svc.candidates(PROJECT, case.id)
    # the source's "replay" is the promote-time validation (it FAILs — that is the point)
    assert [(r["trace_id"], r["is_source"], r["replay_verdict"]) for r in rows] == [
        ("fix", False, "PASS"), ("src", True, "FAIL"),
    ]
    with pytest.raises(rs.NotFound):
        svc.candidates("other", case.id)


def test_compare_endpoint_shape(svc):
    case = svc.promote_trace(PROJECT, "src")
    svc.trace_reader.traces["fix"] = _run(("lookup", '{"order_id": "42"}'))
    res = svc.compare(PROJECT, case.id, "fix")
    assert res["structural_verdict"] == "PASS"
    assert res["rows"][0]["divergence"] == "arguments differ" and res["first_divergence"] == 0
    assert res["answers"]["candidate"] == "done"
    with pytest.raises(rs.NotFound):
        svc.compare(PROJECT, case.id, "nope")
