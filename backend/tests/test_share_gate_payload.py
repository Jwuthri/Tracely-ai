"""What a shared gate verdict says out loud.

The URL this payload backs is designed to be pasted into a public pull request, so the test that
matters is not "does it render" but "what did it leak". `_public_gate` is a pure shaper, so it is
checked directly — no DB, no ClickHouse.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tracely.api.routers.share import _public_gate
from tracely.infrastructure.db.models import GateCase, GateRun


def _gate(**over) -> GateRun:
    g = GateRun(
        id="gate-1",
        project_id="proj-secret",
        agent_id="agent-secret",
        env="ci",
        git_ref="deadbeefcafe",
        pr_number=42,
        status="FAIL",
        total=2,
        passed=1,
        failed=1,
        skipped=0,
        latency_ms=1234.0,
        total_tokens=9000,
        warnings=["endpoint was slow"],
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
        finished_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    for k, v in over.items():
        setattr(g, k, v)
    return g


def _case(detail: dict) -> tuple[GateCase, str]:
    gc = GateCase(
        id="c1",
        gate_run_id="gate-1",
        evaluation_case_id="case-internal-id",
        scenario_id="scenario-internal-id",
        candidate_trace_id="trace-nobody-may-read",
        verdict="FAIL",
        detail=detail,
    )
    return gc, "refund beyond the 30-day window"


def test_the_verdict_is_public_and_the_scope_is_not():
    out = _public_gate(_gate(), "planner", [_case({"failed_expectations": ["no refund offered"]})])

    assert out["status"] == "FAIL"
    assert out["passed"] == 1 and out["failed"] == 1
    assert out["agent"] == "planner"
    assert out["cases"][0]["title"] == "refund beyond the 30-day window"
    assert out["cases"][0]["detail"]["failed_expectations"] == ["no refund offered"]

    # Nothing that would widen the link past this one verdict.
    flat = repr(out)
    for secret in ("proj-secret", "agent-secret", "trace-nobody-may-read", "case-internal-id",
                   "scenario-internal-id"):
        assert secret not in flat, f"{secret} leaked into a public share payload"


def test_case_detail_is_an_allowlist_not_a_blocklist():
    """The gate pipeline keeps adding keys to `detail`. A new one must be invisible until someone
    opts it in — otherwise the next feature silently publishes trace ids."""
    out = _public_gate(
        _gate(),
        "planner",
        [_case({
            "reason": "no candidate trace",
            "trace_ids": ["t1", "t2"],
            "candidate_trace_id": "t1",
            "some_future_field": "whatever ships next",
        })],
    )
    assert out["cases"][0]["detail"] == {"reason": "no candidate trace"}
