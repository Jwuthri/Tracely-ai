"""The shared case-outcome contract (W2): checks, execution evidence, and the two policy tables.

Pure — no DB, no ClickHouse. What is pinned:
- PASS needs every required check to have RUN and passed; an expected judge with no result is
  UNAVAILABLE and makes the case INCOMPLETE (blocking), never a pass.
- A behavioural FAIL outranks an execution problem, so the two stay distinguishable.
- Advisory checks are visible but never decide.
- An execution that did not complete makes the structural checks UNAVAILABLE.
- The gate policy table: empty suite, partial coverage, all-skipped, mixed, incomplete.
"""

from __future__ import annotations

import pytest

from tracely.domain.regression.outcome import (
    Check,
    Execution,
    aggregate,
    evaluate_contract,
    execution_evidence,
    gate_status,
    worst_gate_status,
)
from tracely.domain.trajectory import build_trajectory


def _span(span_id, type_, name, *, parent="", level="DEFAULT", status_message="", metadata=None):
    return {
        "trace_id": "t", "span_id": span_id, "parent_span_id": parent, "type": type_,
        "name": name, "level": level, "agent_run_id": "r", "is_app_root": parent == "",
        "tool_call_names": [], "output": None, "status_message": status_message,
        "metadata": metadata or {},
    }


def _traj(*spans):
    return build_trajectory([_span("a", "AGENT", "planner"), *spans])


CLEAN = [_span("t", "TOOL", "get_weather", parent="a")]
ASSERTIONS = {"no_error": True, "required_tools": ["get_weather"], "match_mode": "superset"}
QUALITY = {**ASSERTIONS, "quality": {"score_names": ["tracely.run.quality"]}}


def _q(verdict, comment="", value=None):
    return {"score_name": "tracely.run.quality", "verdict": verdict, "value": value, "comment": comment}


# ── aggregation policy ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("checks", "execution", "expected"),
    [
        ([Check("tools", True, "PASS")], Execution(), "PASS"),
        ([Check("tools", True, "FAIL")], Execution(), "FAIL"),
        ([Check("tools", True, "UNAVAILABLE")], Execution(), "INCOMPLETE"),
        # advisory checks never decide, whatever they say
        ([Check("tools", True, "PASS"), Check("quality:q", False, "FAIL")], Execution(), "PASS"),
        ([Check("tools", True, "PASS"), Check("quality:q", False, "UNAVAILABLE")], Execution(), "PASS"),
        # behavioural FAIL outranks an unavailable check / execution problem — still distinguishable
        ([Check("tools", True, "FAIL"), Check("quality:q", True, "UNAVAILABLE")], Execution(), "FAIL"),
        ([Check("tools", True, "PASS")], Execution(problem="replay error: x"), "INCOMPLETE"),
        # nothing required at all + complete execution = PASS (vacuous, the caller decides)
        ([], Execution(), "PASS"),
    ],
)
def test_aggregate_policy(checks, execution, expected):
    assert aggregate(checks, execution) == expected


# ── evaluate_contract ─────────────────────────────────────────────────────────


def test_structural_pass_without_quality_expectation():
    out = evaluate_contract(ASSERTIONS, "superset", _traj(*CLEAN))
    assert out.verdict == "PASS"
    assert [c.name for c in out.checks] == ["tools", "no_error"]
    assert out.detail["policy"] and out.detail["quality_checked"] is False


def test_expected_judge_with_no_result_is_incomplete_not_pass():
    """THE W2 bug: a quality-only case used to PASS when the judge silently returned nothing."""
    out = evaluate_contract(QUALITY, "superset", _traj(*CLEAN), quality=[])
    assert out.verdict == "INCOMPLETE"
    q = next(c for c in out.checks if c.name == "quality:tracely.run.quality")
    assert q.required and q.status == "UNAVAILABLE" and "no result" in q.reason
    assert out.detail["quality_checked"] is False


def test_partial_results_do_not_imply_complete_evaluation():
    a = {**ASSERTIONS, "quality": {"score_names": ["q1", "q2"]}}
    quality = [{"score_name": "q1", "verdict": "PASS", "value": 1, "comment": ""}]
    out = evaluate_contract(a, "superset", _traj(*CLEAN), quality=quality)
    assert out.verdict == "INCOMPLETE"
    assert {c.name: c.status for c in out.checks}["quality:q2"] == "UNAVAILABLE"


def test_failed_required_judge_fails_the_case():
    out = evaluate_contract(QUALITY, "superset", _traj(*CLEAN), quality=[_q("FAIL", "hallucinated", 0.2)])
    assert out.verdict == "FAIL"
    assert out.detail["quality_pass"] is False and out.detail["quality_reason"] == "hallucinated"
    assert out.detail["quality_score"] == 0.2


def test_advisory_judge_is_visible_but_never_decides():
    missing = evaluate_contract(QUALITY, "superset", _traj(*CLEAN), quality=[], quality_blocks=False)
    failed = evaluate_contract(QUALITY, "superset", _traj(*CLEAN), quality=[_q("FAIL")], quality_blocks=False)
    assert missing.verdict == "PASS" and failed.verdict == "PASS"
    assert next(c for c in missing.checks if c.name.startswith("quality")).status == "UNAVAILABLE"
    assert failed.detail["quality_pass"] is False  # still reported


def test_quality_cannot_rescue_a_structural_fail():
    out = evaluate_contract(QUALITY, "superset", _traj(), quality=[_q("PASS")])
    assert out.verdict == "FAIL"
    assert {c.name: c.status for c in out.checks}["tools"] == "FAIL"
    assert "missing tools: get_weather" in out.detail["checks"][0]["reason"]


def test_execution_problem_makes_structural_checks_unavailable():
    out = evaluate_contract(
        ASSERTIONS, "superset", _traj(), execution=Execution(mode="recorded", problem="replay error: fixtures unavailable")
    )
    assert out.verdict == "INCOMPLETE"
    assert all(c.status == "UNAVAILABLE" for c in out.checks)
    assert out.detail["execution"] == {
        "mode": "recorded", "complete": False, "problem": "replay error: fixtures unavailable", "diverged": [],
    }


def test_judge_identity_is_stored_beside_the_result():
    judges = {"tracely.run.quality": {"evaluator_id": "e1", "config_digest": "abc", "model": "gpt-4o"}}
    out = evaluate_contract(QUALITY, "superset", _traj(*CLEAN), quality=[_q("PASS")], judges=judges)
    assert out.detail["judges"] == judges and out.verdict == "PASS"


# ── execution evidence off the trace ──────────────────────────────────────────


def test_execution_evidence_reads_replay_stamps():
    spans = [
        _span("a", "AGENT", "planner", level="ERROR", status_message="replay error: no recorded call for tools:x",
              metadata={"tracely.replay.mode": "recorded"}),
        _span("g", "GENERATION", "gpt-4o", parent="a", metadata={"tracely.replay.divergence": "input"}),
    ]
    ex = execution_evidence(spans)
    assert ex.mode == "recorded" and not ex.complete and ex.diverged == ["gpt-4o"]


def test_agent_raised_is_behavioural_not_an_execution_problem():
    spans = [_span("a", "AGENT", "planner", level="ERROR", status_message="agent raised: KeyError")]
    assert execution_evidence(spans).complete
    assert execution_evidence([]) == Execution()


# ── the gate policy table ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("passed", "failed", "skipped", "incomplete", "total", "full", "expected"),
    [
        (0, 0, 0, 0, 0, False, "PASS"),  # empty suite: vacuous, not a first test
        (3, 0, 0, 0, 3, False, "PASS"),
        (2, 0, 1, 0, 3, False, "PASS"),  # partial coverage tolerated by default
        (2, 0, 1, 0, 3, True, "NO_COVERAGE"),  # …unless full coverage is required
        (0, 0, 3, 0, 3, False, "NO_COVERAGE"),  # all skipped
        (1, 1, 1, 0, 3, False, "FAIL"),  # mixed
        (2, 0, 0, 1, 3, False, "INCOMPLETE"),  # a case could not be checked
        (0, 0, 2, 1, 3, False, "INCOMPLETE"),  # incomplete outranks no-coverage
        (1, 1, 0, 1, 3, False, "FAIL"),  # …but a real failure outranks incomplete
    ],
)
def test_gate_policy_table(passed, failed, skipped, incomplete, total, full, expected):
    assert gate_status(passed, failed, skipped, incomplete, total, require_full_coverage=full) == expected


def test_worst_gate_status_ordering():
    assert worst_gate_status("PASS", "NO_COVERAGE") == "NO_COVERAGE"
    assert worst_gate_status("NO_COVERAGE", "INCOMPLETE") == "INCOMPLETE"
    assert worst_gate_status("INCOMPLETE", "FAIL") == "FAIL"
    assert worst_gate_status("PASS", "UNKNOWN") == "UNKNOWN"  # unknown = treated as worst


def test_execution_problem_makes_quality_unavailable_even_with_a_failing_judge_result():
    """A replay that could not be served answers with an error string; the judge fails that —
    but it is an execution problem, not a behavioural failure: INCOMPLETE, never FAIL."""
    out = evaluate_contract(
        QUALITY, "superset", _traj(), quality=[_q("FAIL", "no answer at all")],
        execution=Execution(mode="recorded", problem="replay error: no recorded call for tools:get_weather"),
    )
    assert out.verdict == "INCOMPLETE"
    assert all(c.status == "UNAVAILABLE" for c in out.checks)
