"""The evaluator config contract: one flat dict, end to end.

Legacy rows nested structural knobs under `config.params`. Dispatch used to narrow the config to
that inner dict when present — which silently stripped the runtime-injected `CFG_*` keys
(previous result, dependencies, chain-pass marker) for any row with the nesting. The nesting is
now folded away once, at spec load; dispatch passes the config through whole.
"""

from __future__ import annotations

from tracely.domain.evaluation.evaluators.base import (
    CFG_PREVIOUS,
    RUN,
    Evaluator,
    EvaluatorRegistry,
)
from tracely.domain.evaluation.results import EvalResult, RunContext, chain_payload
from tracely.infrastructure.db.repositories import _flat_config


def test_flat_config_folds_legacy_params_nesting():
    legacy = {"check": "latency", "params": {"budget_ms": 60000}}
    assert _flat_config(legacy) == {"check": "latency", "budget_ms": 60000}
    # nested knobs win over a same-named top-level key (dispatch used to pass ONLY the nested
    # dict, so the nested value is the one legacy rows actually ran with)
    shadowed = {"check": "latency", "budget_ms": 1, "params": {"budget_ms": 2}}
    assert _flat_config(shadowed)["budget_ms"] == 2
    # flat configs and empty rows pass through untouched
    assert _flat_config({"threshold": 0.6}) == {"threshold": 0.6}
    assert _flat_config(None) == {}
    assert _flat_config({"params": "not-a-dict"}) == {"params": "not-a-dict"}


def test_dispatch_passes_the_whole_config_to_the_evaluator():
    """The regression that motivated the flat contract: an injected runtime key must reach the
    evaluator even when the row (already flattened at load) carries arbitrary other knobs."""
    seen: dict = {}

    class Probe(Evaluator):
        kind = "probe"

        def run(self, ctx: RunContext, params: dict) -> list[EvalResult]:
            seen.update(params)
            return []

    registry = EvaluatorRegistry()
    registry.register(Probe)
    ctx = RunContext("p", "t1", "run-1", [], {})
    config = {"threshold": 0.6, CFG_PREVIOUS: {"verdict": "FAIL"}}
    registry.dispatch("probe", config, "probe.score", RUN, ctx)
    assert seen["threshold"] == 0.6
    assert seen[CFG_PREVIOUS] == {"verdict": "FAIL"}


def test_chain_payload_is_the_one_rendering_for_sequential_context():
    """Shared by the judge (chaining its own steps) and the service (seeding the next turn from
    the persisted score) — both sides of the chain must show the same shape."""
    # a `json` column keeps its schema shape, with the envelope re-attached where absent
    assert chain_payload(
        value=0.4, verdict="FAIL", comment="weak", string_value='{"intent": "complaint"}'
    ) == {"intent": "complaint", "score": 0.4, "verdict": "FAIL", "reason": "weak"}
    # schema fields are never overwritten by the envelope
    assert chain_payload(
        value=0.4, verdict="FAIL", comment="weak", string_value='{"score": 0.9}'
    )["score"] == 0.9
    # everything else collapses to the compact form, empties dropped
    assert chain_payload(value=0.8, verdict="PASS", comment="", string_value="") == {
        "value": 0.8, "verdict": "PASS",
    }
    # non-object JSON (a bare list/string) falls back to the compact form too
    assert chain_payload(value=None, verdict="", comment="", string_value="[1, 2]") == {}
    # a plain string is a decision column's label, carried as its own field
    assert chain_payload(value=0.96, verdict="", comment="order_status (confidence 0.96)",
                         string_value="order_status") == {
        "label": "order_status", "value": 0.96, "reason": "order_status (confidence 0.96)",
    }
