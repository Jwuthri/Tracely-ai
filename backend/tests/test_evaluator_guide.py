"""The column guide both agents read (`domain/evaluation/guide.py`) must describe configs the
router actually accepts — an example that 400s teaches the agent to fail."""

from __future__ import annotations

import json
import re

from tracely.api.routers.evaluators import _validate_evaluator
from tracely.domain.evaluation import conditions, decision
from tracely.domain.evaluation.evaluators.catalog import INTENTS
from tracely.domain.evaluation.guide import create_evaluator_description


def test_the_recipe_in_the_guide_is_a_valid_config():
    text = create_evaluator_description("config", "backfill.")
    recipe = json.loads(re.search(r"explain only the refund turns: (\{.*?\]\}\]\})", text, re.S).group(1))
    _validate_evaluator("llm_judge", "AGENT_RUN", recipe)
    # it gates on a label the shipped intent column really produces
    assert recipe["run_if"][0]["values"][0] in INTENTS


def test_the_guide_names_every_output_type_and_condition_operator():
    text = create_evaluator_description("config", "")
    for name in (*decision.DECISION_OUTPUT_TYPES, *conditions.FIELDS, *conditions.SET_OPS, *conditions.NUM_OPS):
        assert f'"{name}"' in text, name


async def test_both_agents_carry_it():
    from tracely.api.mcp_server import mcp
    from tracely.services import assistant_tools

    [assistant] = [t for t in assistant_tools.build_tools({"authorization": "Bearer x"}) if t.name == "create_evaluator"]
    assert "run_if" in assistant.description and "`evaluator_config`" in assistant.description
    mcp_tool = next(t for t in await mcp.list_tools() if t.name == "create_evaluator")
    assert "run_if" in mcp_tool.description and "`config`" in mcp_tool.description
