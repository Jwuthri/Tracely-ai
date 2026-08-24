"""What a shared gate verdict says out loud.

The URL this payload backs is forwardable and lands in public pull requests that get crawled within
hours, so the test that matters is not "does it render" but "what did it leak". `_public_gate` is a
pure shaper, so it is checked directly — no DB, no ClickHouse.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tracely.api.routers.share import _failed_checks, _public_gate
from tracely.infrastructure.db.models import GateCase, GateRun


def _gate(**over) -> GateRun:
    g = GateRun(
        id="gate-internal-id",
        project_id="proj-secret",
        agent_id="agent-secret",
        env="staging-eu",
        git_ref="deadbee",
        pr_number=42,
        status="FAIL",
        total=2,
        passed=1,
        failed=1,
        skipped=0,
        latency_ms=1234.0,
        total_tokens=9000,
        warnings=["endpoint https://internal.acme.corp/agent was slow"],
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
        finished_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    for k, v in over.items():
        setattr(g, k, v)
    return g


def _case(detail: dict) -> tuple[GateCase, str]:
    gc = GateCase(
        id="c1",
        gate_run_id="gate-internal-id",
        evaluation_case_id="case-internal-id",
        scenario_id="scenario-internal-id",
        candidate_trace_id="trace-nobody-may-read",
        verdict="FAIL",
        detail=detail,
    )
    return gc, "refund beyond the 30-day window"


def test_it_shows_the_verdict_and_the_names_of_what_broke():
    out = _public_gate(
        _gate(), "planner", [_case({"failed_scores": ["no_pii_leak: the agent replied '…'"]})]
    )
    assert out["status"] == "FAIL"
    assert (out["passed"], out["failed"], out["skipped"]) == (1, 1, 0)
    assert out["agent"] == "planner"
    assert out["pr_number"] == 42
    assert out["cases"][0]["label"] == "refund beyond the 30-day window"
    assert out["cases"][0]["evaluators"] == ["no_pii_leak"]


def test_judge_rationale_never_reaches_the_page():
    """`failed_scores` is written as "name: comment", and the comment QUOTES the agent's answer —
    i.e. the customer's end-user PII, on a URL that gets crawled."""
    checks = _failed_checks({"failed_scores": ["no_pii_leak: the agent replied 'SSN 123-45-6789'"]})
    assert checks == ["no_pii_leak"]
    assert "123-45-6789" not in repr(checks)


def test_the_payload_is_an_allow_list_not_a_deny_list():
    out = _public_gate(
        _gate(),
        "planner",
        [
            _case(
                {
                    "reason": "no candidate trace",
                    "failed_expectations": ["the conversation errored mid-run: connection reset"],
                    "error": "POST https://internal.acme.corp/agent -> 502",
                    "quality_reason": "the answer invented a refund policy",
                    "trace_ids": ["t1", "t2"],
                    "some_future_field": "whatever ships next",
                }
            )
        ],
    )
    flat = repr(out)
    leaks = [
        "proj-secret",            # project scope
        "agent-secret",           # internal id
        "gate-internal-id",
        "case-internal-id",
        "scenario-internal-id",
        "trace-nobody-may-read",
        "t1",                     # detail.trace_ids
        "internal.acme.corp",     # detail.error / warnings — hostnames and endpoints
        "connection reset",       # failed_expectations text
        "invented a refund",      # judge rationale
        "no candidate trace",     # detail.reason
        "whatever ships next",    # a field nobody has thought of yet
        "staging-eu",             # env label
        "9000",                   # token spend
        "1234",                   # latency
    ]
    for secret in leaks:
        assert secret not in flat, f"{secret} leaked onto a public share page"

    # …while still naming the categories, which is what a reviewer actually needs.
    assert set(out["cases"][0]["evaluators"]) == {"scenario_expectation", "endpoint_error"}


def test_a_branch_name_is_not_a_sha():
    """`feat/acme-acquisition` on a public page leaks the roadmap. Only a commit SHA renders."""
    assert _public_gate(_gate(git_ref="feat/acme-acquisition"), "a", [])["sha"] is None
    assert _public_gate(_gate(git_ref="deadbeefcafe1234"), "a", [])["sha"] == "deadbee"
    assert _public_gate(_gate(git_ref=""), "a", [])["sha"] is None


def test_structural_failures_report_a_category_never_a_message():
    assert _failed_checks({"missing_tools": ["lookup_order"]}) == ["missing_tools"]
    assert _failed_checks({"tools_ok": False}) == ["tool_sequence"]
    assert _failed_checks({"quality_pass": False, "quality_reason": "wrong"}) == ["answer_quality"]
    assert _failed_checks({}) == []
    assert _failed_checks(None) == []


def test_the_same_evaluator_sinking_several_turns_is_named_once():
    assert _failed_checks({"failed_scores": ["tone: a", "tone: b", "grounded: c"]}) == [
        "tone",
        "grounded",
    ]
