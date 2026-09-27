"""How to write an evaluation column's config — the text agents read.

The in-app assistant (`services/assistant_tools.py`) and the MCP server (`api/mcp_server.py`) both
create and edit columns through the same router, so they get the same instructions: this module
renders the `create_evaluator` tool description for each (their config argument is named
differently — see `assistant_tools`' note on why it can't be `config` there). One source, so the
two agents can't drift from each other or from what `api/routers/evaluators._validate_evaluator`
actually accepts. Pure text; `tests/test_evaluator_guide.py` pins it to the validator.
"""

from __future__ import annotations

_GUIDE = """Add a new evaluator — a new column on the traces table. `level` is CONVERSATION (one grade per
thread), AGENT_RUN (per turn), or SPAN / TOOL / GENERATION / CHAIN (per step). New columns grade
traces ingested from now on; {backfill}

kind="llm_judge" has two engines. Prefer a DECISION column unless the metric needs prose.

1. DECISION (the default model, TypeSafe Jev — `model` may be omitted): a calibrated classifier
   that reads the item and answers ONE literal question; ~100x cheaper than an LLM judge, 32k-token
   input. `{arg}`:
   - output_type "decision_binary": `question`, `criteria` {{"true": what yes means, "false": what
     no means}}, `pass_when` "yes"|"no" (ask "Did the agent hallucinate?" with pass_when "no" —
     never invert the criteria), `threshold` P(yes) cutoff (default 0.5).
   - output_type "decision_multiclass": exactly one label. `criteria` {{label: description|null}}
     (2-255 snake_case labels ≤24 chars), `fail_options` [labels meaning FAIL] (empty = an
     informational label column).
   - output_type "decision_multilabel": any subset of 2-50 labels, same `criteria`/`fail_options`,
     `threshold` per label. FAILs when any fail label applies.
   Name the part of the item in backticks (`User request`, `Agent answer`, `Tool results`).
   Optional `fallback_model` (an LLM id) grades items too long for Jev; without it they show
   "Skipped — input too long".
2. LLM: set `model` to an LLM id (list them with the models endpoint; default text model
   openai/gpt-6-luna). `prompt` is the rubric; `output_type` "score" (0-1 + `threshold`),
   "boolean", "number", "text" (free-form — explanations), or "json" (+ `output_schema`).

Context: without @VARIABLES the level's item is sent (the request + answer / transcript / one
step). With them `prompt` becomes a template: @CURRENT_MESSAGE.input, @CURRENT_MESSAGE.output,
@CURRENT_STEPS (every step: inputs, outputs, errors), @CURRENT_STEPS.tool, @HISTORY (up to this
turn), @PREVIOUS_USER_MSG, @PREVIOUS_ASSISTANT_MSG, @GOAL, @DEPENDENCIES — for a decision column
the template is only headings + variables (the question lives in `question`).

Chaining columns:
- `depends_on` [score_names]: those columns run first, and their result for the same item
  ({{label, value, verdict, reason}}) is added to this column's context (@DEPENDENCIES in a template).
- `run_if` [conditions, ALL must hold]: grade an item only when its depends_on results match;
  other items show "Not run" (neutral, no spend). A condition is {{"column": score_name, "field":
  "label"|"verdict"|"value", "op": "in"|"not_in" (with "values": [...]) or
  "gte"|"gt"|"lte"|"lt" (with "value": number)}}. `label` matches a multi-class label or any
  multi-label label; `verdict` values are PASS / FAIL / NONE. Its columns join depends_on
  automatically and must already exist (list_evaluators for their score_names).
  Recipe — explain only the refund turns: {{"model": "openai/gpt-6-luna", "output_type": "text",
  "prompt": "Explain how the agent handled this refund request.", "run_if": [{{"column":
  "tracely.run.intent", "field": "label", "op": "in", "values": ["refund_cancellation"]}}]}}.
  The cheap-first pattern: a Jev column classifies every item; the LLM runs only where it flagged.

Also: `advisory` true = a FAIL is shown but doesn't fail the trace (subjective quality);
`execution_mode` "sequential" = items graded in order with the earlier ones as context.

kind="structural" — `{arg}` takes `check`: run_outcome (AGENT_RUN), tool_success (TOOL),
tool_consistency (AGENT_RUN), latency (AGENT_RUN, + `budget_ms`), required_tools (AGENT_RUN,
+ `tools`). The level is fixed per check.

Prefer a library template (list_evaluator_templates) when one fits. A 400 error names exactly what
to fix — correct the config and retry."""


def create_evaluator_description(config_arg: str, backfill: str) -> str:
    """The `create_evaluator` tool description for an agent whose config argument is `config_arg`."""
    return _GUIDE.format(arg=config_arg, backfill=backfill)
