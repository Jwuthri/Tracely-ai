"""Catalog templates must be able to emit the verdict they claim to — and be runnable as shipped.

The library judges run on a decision model (TypeSafe Jev): a question plus labels instead of a
rubric plus a JSON schema (see `domain/evaluation/decision.py`). The old failure class — a
`threshold` with no score field to gate on, so a detector looked wired up and never produced a
verdict — has a decision-model twin: a classifier column with no `fail_options` is a label, not
a check. These tests pin which templates are checks and which are labels, so that can't drift.
"""

from __future__ import annotations

import pytest

from tracely.api.routers.evaluators import _validate_evaluator
from tracely.config import settings
from tracely.domain.evaluation import decision
from tracely.domain.evaluation.evaluators.catalog import TEMPLATES
from tracely.domain.evaluation.template_resolver import extract_template_variables

_JUDGES = [t for t in TEMPLATES if t["kind"] == "llm_judge"]
# Deliberately verdict-less: their output is a label the table shows, not a pass/fail.
_INFORMATIONAL = {"tracely.run.intent", "tracely.step.self_correction"}


def test_the_library_runs_on_the_decision_model():
    assert _JUDGES
    off = [
        t["score_name"]
        for t in _JUDGES
        if not decision.is_decision_output(t["config"].get("output_type"))
    ]
    assert off == [], f"library judges not on a decision model: {off}"


@pytest.mark.parametrize("t", TEMPLATES, ids=lambda t: t["score_name"])
def test_every_template_passes_the_routers_validation(t):
    """Seeding inserts configs verbatim, bypassing the router — so the router's rules are
    re-asserted here, or a template could ship a column the API would have rejected."""
    _validate_evaluator(t["kind"], t["level"], t["config"])


@pytest.mark.parametrize("t", _JUDGES, ids=lambda t: t["score_name"])
def test_checks_can_fail_and_labels_cannot(t):
    cfg = t["config"]
    ot = cfg["output_type"]
    if t["score_name"] in _INFORMATIONAL:
        assert ot == decision.MULTICLASS and not cfg.get("fail_options"), (
            "a label column must not gate"
        )
        return
    # binary always has a verdict; a multi-class/multi-label check needs fail labels to have one
    if ot != decision.BINARY:
        assert cfg.get("fail_options"), f"{t['score_name']} can never FAIL: no fail_options"


def test_the_gate_quality_judge_always_emits_a_verdict():
    """Regression gates read `gate_quality_score_name` by verdict; a verdict-less answer there
    reads as 'judge gave no verdict' → UNAVAILABLE on every case."""
    t = next(x for x in TEMPLATES if x["score_name"] == settings.gate_quality_score_name)
    assert t["recommended"] is True
    assert t["config"]["advisory"] is True
    assert t["config"].get("fail_options")


@pytest.mark.parametrize("t", _JUDGES, ids=lambda t: t["score_name"])
def test_labels_fit_the_table_cell(t):
    """A CATEGORICAL score headlines `string_value` in the cell; long labels get cut off."""
    if t["config"]["output_type"] == decision.BINARY:
        pytest.skip("binary has no labels")
    labels = list(t["config"]["criteria"])
    assert all(len(lb) <= 24 for lb in labels), [lb for lb in labels if len(lb) > 24]


@pytest.mark.parametrize("t", _JUDGES, ids=lambda t: t["score_name"])
def test_unbounded_state_has_a_context_fallback(t):
    """`@CURRENT_STEPS` has no total cap (each field is clipped, the count isn't), so a long turn
    can exceed the decision model's 32k tokens — those templates must not silently skip it."""
    refs = extract_template_variables(t["config"].get("prompt") or "")
    if not any(r.startswith("CURRENT_STEPS") for r in refs):
        pytest.skip("bounded state")
    assert t["config"].get("fallback_model")


# ── the intent column's label contract ───────────────────────────────────────────


def _intent_template():
    return next(t for t in TEMPLATES if t["score_name"] == "tracely.run.intent")


def test_intent_column_is_recommended_and_sequential():
    """Installed in every new workspace, and chained so each turn sees the earlier intents."""
    t = _intent_template()
    assert t["recommended"] is True
    assert t["config"]["execution_mode"] == "sequential"
    # the user's message alone: the agent's answer is the long half of the item, and reading it
    # makes the label follow what the agent did instead of what the user asked for
    assert t["config"]["include_answer"] is False


def test_intent_labels_are_the_intent_vocabulary():
    from tracely.domain.evaluation.evaluators.catalog import INTENTS

    assert list(_intent_template()["config"]["criteria"]) == INTENTS


# ── @VARIABLE templates must declare themselves advanced ─────────────────────────
# The seeder (`seeding_service` / `auth.provisioning`) inserts a template's config into the DB
# verbatim — unlike the API, it never runs `_stamp_advanced`. A template whose prompt holds
# `@VARIABLES` but no `is_advanced` therefore ships as a BASIC column whose rubric is the literal
# text "@CURRENT_STEPS.tool", and every grade it produces is nonsense.


@pytest.mark.parametrize("t", TEMPLATES, ids=lambda t: t["score_name"])
def test_a_template_with_variables_declares_is_advanced(t):
    from tracely.domain.evaluation.template_resolver import extract_template_variables

    config = t.get("config") or {}
    if not extract_template_variables(config.get("prompt") or ""):
        pytest.skip("no @VARIABLES — a plain rubric")
    assert config.get("is_advanced") is True, (
        f"{t['score_name']} uses @VARIABLES but doesn't set is_advanced; the seeder doesn't stamp it"
    )
