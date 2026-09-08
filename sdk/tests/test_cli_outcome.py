"""CLI half of the W2 contract: INCOMPLETE ranks, blocks, exits 2, and is explained per case."""

from __future__ import annotations

from tracely_sdk import cli


def test_incomplete_ranks_between_no_coverage_and_fail():
    rows = [{"status": s} for s in ("PASS", "NO_COVERAGE", "INCOMPLETE")]
    assert cli.worst_status(rows) == "INCOMPLETE"
    assert cli.worst_status(rows + [{"status": "FAIL"}]) == "FAIL"


def test_exit_codes_split_no_verdict_from_no():
    assert cli.exit_code("PASS") == 0
    assert cli.exit_code("FAIL") == 1 and cli.exit_code("NO_COVERAGE") == 1
    assert cli.exit_code("INCOMPLETE") == 2 and cli.exit_code("ERROR") == 2


def test_case_reason_names_the_unavailable_required_check():
    detail = {
        "checks": [
            {"name": "tools", "required": True, "status": "PASS", "reason": ""},
            {"name": "quality:tracely.run.quality", "required": True, "status": "UNAVAILABLE",
             "reason": "judge returned no result"},
            {"name": "quality:extra", "required": False, "status": "UNAVAILABLE", "reason": "x"},
        ],
        "execution": {"mode": "recorded", "complete": True, "problem": ""},
    }
    r = cli.case_reason(detail)
    assert "unavailable: quality:tracely.run.quality — judge returned no result" in r
    assert "quality:extra" not in r  # advisory: visible in the UI, not a blocking reason


def test_case_reason_leads_with_the_execution_problem():
    detail = {"execution": {"problem": "replay error: fixtures unavailable"}, "checks": []}
    assert cli.case_reason(detail).startswith("replay error: fixtures unavailable")


def test_markdown_explains_incomplete():
    md = cli.render_markdown(
        {"status": "INCOMPLETE", "agent": "a", "env": "ci", "passed": 1, "failed": 0,
         "skipped": 0, "incomplete": 1, "cases": []}, "", "abc1234",
    )
    assert "**INCOMPLETE**" in md and "could not be fully" in md


def test_markdown_carries_case_level_evidence_and_the_manifest():
    data = {
        "status": "FAIL", "agent": "planner", "env": "ci", "passed": 0, "failed": 1, "skipped": 0,
        "incomplete": 0, "run_id": "run-abc", "execution_mode": "recorded",
        "cases": [{
            "title": "Refund flow", "verdict": "FAIL", "evaluation_case_id": "case-1",
            "candidate_trace_id": "t1",
            "detail": {
                "case_version": 3,
                "checks": [
                    {"name": "tools", "required": True, "status": "PASS", "reason": ""},
                    {"name": "quality:q", "required": True, "status": "FAIL", "reason": "made it up"},
                    {"name": "quality:extra", "required": False, "status": "UNAVAILABLE", "reason": ""},
                ],
                "quality_pass": False, "quality_reason": "made it up",
            },
        }],
    }
    md = cli.render_markdown(data, "https://tracely.test", "abc1234")
    assert "run `run-abc` · recorded" in md
    assert "[Refund flow](https://tracely.test/cases/case-1) <sub>v3</sub>" in md
    assert "`✓tools ✗quality:q ?quality:extraᵃ`" in md
    assert "required status check" in md and "does not verify branch protection" in md
    # a public share link keeps strangers off the login wall: no case link then
    md2 = cli.render_markdown({**data, "share_url": "https://tracely.test/share/x"}, "https://tracely.test", "abc1234")
    assert "/cases/case-1" not in md2


def test_checks_summary_is_empty_without_the_contract():
    assert cli.checks_summary({}) == "" and cli.checks_summary({"checks": []}) == ""
