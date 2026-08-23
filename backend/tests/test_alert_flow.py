"""Alert-rule flows: DAG resolution, the template namespace, and each step runner. No infra.

These are the invariants a visual builder rests on. Break one and the canvas lies: a step that
looks wired doesn't run, a chip resolves to the wrong upstream output, or a gate that should skip
the rule fires it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tracely.domain.alerting import (
    BASE_INPUTS,
    CYCLE_ERROR,
    TRIGGER_NODE_ID,
    ancestor_step_ids,
    build_context,
    catalog_for_trigger,
    declared_outputs,
    flow_layout_error,
    ordered_steps,
)
from tracely.services import alert_flow_service as engine


@dataclass
class Step:
    id: str
    order_index: int = 0
    name: str = ""
    step_type: str = "webhook"
    config: dict = field(default_factory=dict)


def edges(*pairs: tuple[str, str]) -> dict:
    return {"edges": [{"source": s, "target": t} for s, t in pairs], "nodes": [{"id": "x"}]}


T = TRIGGER_NODE_ID


# ── DAG resolution ────────────────────────────────────────────────────────────
def test_a_chain_runs_in_wired_order_not_saved_order():
    # order_index says c,b,a — the edges say a→b→c. The canvas wins.
    steps = [Step("a", 2), Step("b", 1), Step("c", 0)]
    ordered, err = ordered_steps(steps, edges((T, "a"), ("a", "b"), ("b", "c")))
    assert err is None
    assert [s.id for s in ordered] == ["a", "b", "c"]


def test_an_unreachable_step_is_parked_not_run():
    """An orphan node is saved and visible, and deliberately does NOT execute — otherwise a step
    someone dragged out of the flow keeps mailing customers."""
    steps = [Step("a"), Step("orphan")]
    ordered, err = ordered_steps(steps, edges((T, "a")))
    assert err is None
    assert [s.id for s in ordered] == ["a"]


def test_a_cycle_fails_the_whole_run_with_a_readable_error():
    steps = [Step("a"), Step("b")]
    ordered, err = ordered_steps(steps, edges((T, "a"), ("a", "b"), ("b", "a")))
    assert ordered == []
    assert err == CYCLE_ERROR


def test_no_edges_falls_back_to_saved_order():
    """What makes an API-created rule (or one from an older UI) still run."""
    steps = [Step("b", 1), Step("a", 0)]
    ordered, err = ordered_steps(steps, None)
    assert err is None and [s.id for s in ordered] == ["a", "b"]


def test_edges_that_reach_nothing_fall_back_too():
    steps = [Step("a", 0), Step("b", 1)]
    ordered, _ = ordered_steps(steps, edges(("a", "b")))  # nothing connected to the trigger
    assert [s.id for s in ordered] == ["a", "b"]


def test_diamond_order_is_deterministic():
    # a → {b, c} → d. Two processes must agree, so ready nodes pop in sorted-id order.
    steps = [Step(x) for x in ("a", "b", "c", "d")]
    layout = edges((T, "a"), ("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))
    first = [s.id for s in ordered_steps(steps, layout)[0]]
    assert first == ["a", "b", "c", "d"]
    assert first == [s.id for s in ordered_steps(list(reversed(steps)), layout)[0]]


def test_self_loops_and_unknown_targets_are_dropped():
    steps = [Step("a")]
    ordered, err = ordered_steps(steps, edges((T, "a"), ("a", "a"), ("a", "ghost")))
    assert err is None and [s.id for s in ordered] == ["a"]


# ── positional upstream outputs ───────────────────────────────────────────────
def test_ancestors_are_positional_per_branch():
    """`steps[0]` means "my first upstream step on THIS branch". Two parallel branches each see
    their own `steps[0]` — the canvas's Prior-steps chips are built from the same walk, and if the
    two drift every template silently reads the wrong value."""
    raw = [
        {"source": T, "target": "a"},
        {"source": "a", "target": "b"},
        {"source": "a", "target": "c"},
        {"source": "b", "target": "d"},
    ]
    order = ["a", "b", "c", "d"]
    assert ancestor_step_ids("a", raw, order) == []
    assert ancestor_step_ids("b", raw, order) == ["a"]
    assert ancestor_step_ids("c", raw, order) == ["a"]
    assert ancestor_step_ids("d", raw, order) == ["a", "b"]


def test_ancestors_without_edges_are_everything_before_it():
    assert ancestor_step_ids("c", None, ["a", "b", "c"]) == ["a", "b"]


# ── layout validation ─────────────────────────────────────────────────────────
def test_layout_is_opaque_except_for_edge_shape():
    assert flow_layout_error({"nodes": [{"anything": 1}], "edges": []}) is None
    assert flow_layout_error(None) is None
    assert "edges" in (flow_layout_error({"edges": "nope"}) or "")
    assert "source" in (flow_layout_error({"edges": [{"source": 1, "target": "a"}]}) or "")


# ── the template namespace ────────────────────────────────────────────────────
def test_every_catalog_path_exists_in_the_built_context():
    """The contract the chips advertise IS the contract the engine provides. This test is the only
    thing stopping the two from drifting into "renders empty and nobody knows why"."""
    ctx = build_context({"type": "trace_failed"})
    for row in BASE_INPUTS:
        path = row["path"]
        cur: Any = ctx
        for part in path.split("."):
            assert isinstance(cur, dict), f"{path} is not reachable in the context"
            assert part in cur, f"{path} missing from the context"
            cur = cur[part]


def test_context_is_populated_from_the_event_groups():
    ctx = build_context(
        {
            "type": "gate_failed",
            "gate": {"status": "FAIL", "failed": 3, "warnings": ["slow"]},
            "agent": {"slug": "support-bot"},
        }
    )
    assert ctx["gate"]["status"] == "FAIL" and ctx["gate"]["failed"] == 3
    assert ctx["trace"]["url"] == ""  # a gate alert has no trace — blank, never missing


def test_the_agent_slug_chip_is_filled_from_the_events_own_field():
    """`agent` is a bare slug on the event (that is what the matcher scopes on) and `agent.slug` in
    the namespace. They were two different shapes once, and every template that mentioned the agent
    rendered an empty string."""
    ctx = build_context({"type": "trace_failed", "agent": "support-bot"})
    assert ctx["agent"]["slug"] == "support-bot"
    # An explicit group still wins, and a missing one is blank rather than absent.
    assert build_context({"type": "gate_failed"})["agent"]["slug"] == ""


def test_catalog_is_filtered_by_trigger():
    gate_paths = {r["path"] for r in catalog_for_trigger("gate_failed")}
    assert "gate.status" in gate_paths and "trace.input" not in gate_paths
    trace_paths = {r["path"] for r in catalog_for_trigger("trace_failed")}
    assert "failure_reason" in trace_paths and "gate.status" not in trace_paths


def test_llm_outputs_follow_the_declared_schema():
    assert [o["name"] for o in declared_outputs("llm_prompt")] == ["text"]
    schema = [{"name": "severity", "type": "string", "description": "how bad"}]
    assert [o["name"] for o in declared_outputs("llm_prompt", {"output_schema": schema})] == ["severity"]
    assert {o["name"] for o in declared_outputs("condition")} == {"matched", "expression"}


# ── step runners ──────────────────────────────────────────────────────────────
def _ctx(**over: Any) -> dict:
    return build_context({"type": "trace_failed", **over})


@pytest.mark.parametrize(
    "rendered,expected",
    [("True", True), ("yes", True), ("0", False), ("False", False), ("None", False), ("", False), ("[]", False)],
)
def test_condition_falsiness_matches_what_jinja_produces(rendered: str, expected: bool):
    """Jinja hands you a STRING: `"False"` is truthy unless you check for it. Getting this wrong
    means a gate that should stop the rule lets it through."""
    out, err, _ = engine._step_condition({"expression": rendered}, _ctx())
    assert err is None and out["matched"] is expected


def test_condition_renders_against_the_context():
    ctx = _ctx(failing_evaluators=["a", "b"])
    out, err, rendered = engine._step_condition(
        {"expression": "{{ failing_evaluators | length > 1 }}"}, ctx
    )
    assert err is None and out["matched"] is True
    assert rendered["expression"] == "True"  # what the user sees in the run log


def test_webhook_sends_templated_headers_and_body(monkeypatch):
    """The bearer case: headers are `[{key, value}]` on the wire and a real header dict on the
    request, with templates rendered in both."""
    seen: dict[str, Any] = {}

    class Resp:
        status_code = 200
        text = "ok"

    def fake_request(method, url, headers=None, content=None, timeout=None):
        seen.update(method=method, url=url, headers=headers, content=content)
        return Resp()

    monkeypatch.setattr(engine.httpx, "request", fake_request)
    monkeypatch.setattr(engine, "assert_public_url", lambda url: None)
    ctx = _ctx(trace={"url": "https://app/traces/1"}, failure_reason="PII leaked")
    out, err, rendered = engine._step_webhook(
        {
            "url": "https://acme.test/hook",
            "method": "post",
            "headers": [{"key": "Authorization", "value": "Bearer sk-{{ alert.trigger }}"}],
            "body_template": '{"link": "{{ trace.url }}", "why": "{{ failure_reason }}"}',
        },
        ctx,
    )
    assert err is None and out["status"] == 200
    assert seen["method"] == "POST"
    assert seen["headers"]["Authorization"] == "Bearer sk-trace_failed"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert b"PII leaked" in seen["content"]
    assert rendered["body"] and "app/traces/1" in rendered["body"]


def test_webhook_url_is_checked_again_right_before_the_request(monkeypatch):
    """Save-time validation can't see a templated URL, so this is the check that actually protects
    the worker's network."""
    from tracely.infrastructure.net import UnsafeURL

    monkeypatch.setattr(
        engine, "assert_public_url", lambda url: (_ for _ in ()).throw(UnsafeURL("private address"))
    )
    called = {"n": 0}
    monkeypatch.setattr(engine.httpx, "request", lambda *a, **k: called.update(n=1))
    out, err, _ = engine._step_webhook({"url": "http://169.254.169.254/"}, _ctx())
    assert out is None and "private address" in (err or "")
    assert called["n"] == 0  # nothing left the box


def test_a_bad_template_is_a_step_error_not_a_crash():
    out, err, _ = engine._run_step("condition", {"expression": "{{ oops("}, _ctx(), project_id="p")
    assert out is None and "template error" in (err or "")


def test_python_expression_allows_arithmetic_and_blocks_imports():
    ctx = _ctx(failing_evaluators=["a", "b", "c"])
    out, err, _ = engine._step_python_expression({"expression": "len(failing_evaluators) * 2"}, ctx)
    assert err is None and out == 6
    _, err, _ = engine._step_python_expression({"expression": "__import__('os').system('ls')"}, ctx)
    assert err is not None  # simpleeval refuses; the message names the reason


def test_llm_step_needs_a_workspace_key(monkeypatch):
    """A workspace with no OpenRouter key gets a clear error, never Tracely's invoice."""
    from tracely.infrastructure.llm import provider

    monkeypatch.setattr(provider, "llm_enabled", lambda: False)
    out, err, rendered = engine._step_llm_prompt(
        {"user_prompt_template": "why did {{ trace.id }} fail?"}, _ctx(), project_id="p1"
    )
    assert out is None and "OpenRouter key" in (err or "")
    assert rendered["user_prompt"].startswith("why did")


def test_email_step_parses_both_recipient_shapes():
    # A template that renders a list variable produces a Python-list repr; a hand-typed field
    # produces a comma string. Both are real, both must work.
    assert engine._parse_recipients("['a@x.com', 'b@y.com']") == ["a@x.com", "b@y.com"]
    assert engine._parse_recipients("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]
    assert engine._parse_recipients("") == []
