"""Generate an evaluator config from a natural-language description ("Use AI" in Add Column).

One `create_agent` call (LangChain on OpenRouter, see `infrastructure.llm.provider`) with a
structured response schema. The output is a DRAFT: the UI pre-fills the manual form (and the
schema builder, for json outputs) with it for review/editing — generation never creates the
evaluator directly.

**Decision model first.** Most metrics are classifications (did X happen? which kind of Y?), and
those are drafted for the column default decision model (TypeSafe Jev — see
`domain/evaluation/decision.py`): a literal question plus labels, ~1/100th the cost of a judge.
Only a metric that genuinely needs generated text or an unbounded number gets an LLM rubric
column, pinned to the text default (`settings.llm_judge_model`). Without a reachable decision
model (no OpenRouter key in scope) every draft is an LLM column.

The generator writes ADVANCED templates by default: a rubric plus the `@VARIABLES` that carry
exactly the context it grades. That is not a stylistic preference — a basic column is fed one
fixed item per level (`evaluators/prompts.py`), so a metric that needs anything else, most often
the turn's tool results (`@CURRENT_STEPS.tool`), silently grades without the evidence it names
and marks correct answers unsupported. The variable catalog in the system prompt is RENDERED FROM
`TEMPLATE_VARIABLES`, so a new variable reaches the generator the moment it exists — the prompt
cannot drift from what the resolver actually supports. No `is_advanced` flag is generated: the
API stamps it from the `@VARIABLES` present in the prompt (`_stamp_advanced`).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from tracely.domain.evaluation.template_resolver import TEMPLATE_VARIABLES, catalog_level
from tracely.infrastructure.llm import provider

from tracely.domain.evaluation import decision as dec

LEVELS = ("CONVERSATION", "AGENT_RUN", "SPAN")
_LABEL_RE = re.compile(r"[^a-z0-9_]+")
OUTPUT_TYPES = ("score", "number", "boolean", "text", "json")
_FIELD_TYPES = ("string", "number", "boolean", "enum", "array")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DECISION_SECTION = """## Engine: DECISION (the default) vs LLM

Set engine="decision" unless the metric truly needs generated prose or an unbounded number. A
decision model is a calibrated classifier: it reads the item as its state and answers ONE typed
question. Pick the task:
- "binary": a yes/no question. Give yes_means and no_means, and pass_when: "yes" when yes is the
  good outcome, "no" when yes is the failure (e.g. "Did the agent hallucinate?" -> pass_when "no").
  Ask the question the natural way round; never invert the criteria to make yes the good answer.
- "multiclass": exactly one of 2-30 labels (quality categories, intent, failure type).
- "multilabel": any subset of 2-20 labels (which problems occurred, which topics apply).
Labels: snake_case, at most 24 characters, each with a one-line description; put the labels that
mean the item FAILED in fail_labels (leave it empty for a purely informational label column).

Write the question the way the model reads it — literally:
- One atomic judgment. Split "correct AND polite" into two metrics.
- Name the part of the item you mean in backticks, matching the headings it sees
  (`User request`, `Agent answer`, `Tool results`, `Step input`, `Step output`).
- State the exact condition and put boundary cases in the label descriptions.
- No numbers or counting — the model reasons about meaning, not arithmetic.

For a decision metric, `prompt` is NOT a rubric: leave it empty to send the level's default item,
or write a STATE TEMPLATE — only labelled headings and @VARIABLES, no instructions — when the
question needs other context (see the rules below; they apply to state templates too).

Use engine="llm" (then output_type is score/number/boolean/text/json and `prompt` is a rubric)
only for: a free-text observation, a numeric measurement outside 0..1, or a multi-field analysis
whose fields are numbers rather than labels.

"""

_LLM_ONLY_SECTION = """## Engine
Set engine="llm" — no decision model is available in this workspace.

"""

_SYSTEM = """You design evaluation metrics for an AI-agent observability platform.
A metric grades agent traces at exactly one level:
- "CONVERSATION": one grade for a whole multi-turn conversation (goal achievement, frustration, drift).
- "AGENT_RUN": one grade per agent run / assistant turn (answer quality, tone, hallucination).
- "SPAN": one grade per step inside a run — a tool call or model generation (tool choice, argument quality).

{engine}An LLM metric produces exactly one output type:
- "score": a 0..1 quality score with a pass threshold (most metrics).
- "boolean": a pass/fail judgment.
- "number": a raw numeric measurement (counts, magnitudes — when 0..1 doesn't fit).
- "text": a short free-text observation (no pass/fail).
- "json": a structured object — USE THIS for classifications and multi-dimensional analyses.
  Define 1-6 schema_fields that capture the signal: classifications get an "enum" field with the
  allowed labels; multi-dimensional grades get a few named number sub-scores. If the column
  should drive PASS/FAIL and gates, include a numeric "score" field (0-1) and a "reason" string;
  otherwise leave them out for an informational column. Field names must be snake_case
  identifiers.

Write the grading rubric like a strict senior reviewer would: second person, specific, naming
the failure modes to look for, and concrete about what earns a high vs low grade. Do NOT
mention JSON or output formatting in the rubric — the platform handles that.

## Choose the context: ADVANCED (default) vs BASIC

Each level auto-feeds a judge ONE fixed item, and nothing else:
- CONVERSATION -> the turn-by-turn transcript
- AGENT_RUN    -> the user request and the agent's final answer. NOT the tool calls, NOT the
                  retrieved documents, NOT the reasoning.
- SPAN         -> one step's input and output

Write an ADVANCED prompt — a rubric with `@VARIABLE` placeholders resolved against the real
trace — whenever the metric needs anything other than that exact item. This is the normal case:
faithfulness/hallucination needs `@CURRENT_STEPS.tool` (the evidence) next to the answer;
anything about earlier turns needs `@HISTORY` or `@PREVIOUS_ASSISTANT_MSG`; anything about the
user's goal needs `@GOAL`. Use BASIC (a rubric with NO variables) only when the level's default
item is already exactly what you would have asked for.

Rules for an advanced prompt, in order of what breaks when you get them wrong:
1. NOTHING is auto-injected. The template is the entire message the judge sees — if you don't
   reference the user's request, the judge never sees it. Include every variable it needs.
2. Use only variables listed for the level you chose; a variable from another level resolves to
   `[No X available]` and the judge grades on a blank.
3. Prefer the narrowest one that answers the question: `@CURRENT_STEPS.tool` over
   `@CURRENT_STEPS`, `@PREVIOUS_ASSISTANT_MSG` over `@HISTORY`. Wide variables are slower,
   dearer, and bury the signal.
4. Structure it: the instruction first, then each variable under its own labelled heading
   (`## Tool results` then `@CURRENT_STEPS.tool`), so the judge can tell the rubric from the data.
5. State what a missing value means. A variable can resolve to `[No CURRENT_STEPS.tool
   available]`; say whether that is a pass, a fail, or irrelevant.

## The variables, by level
{variables}

## Execution mode
"batch" (default) grades each item independently. Choose "sequential" ONLY when the grade
genuinely depends on the ones before it (drift, escalation, repetition, a running label): items
are then graded in order, and at AGENT_RUN level in advanced mode the previous item's result
reaches the judge ONLY through `@METRIC_PREVIOUS_RESULT` — reference it in the template or the
chain carries nothing."""


def _variable_catalog() -> str:
    """The `@VARIABLE` catalog, rendered per level from `TEMPLATE_VARIABLES`. Generated rather
    than hand-written so a new variable is offered to the generator the day it ships."""
    blocks = []
    for level in LEVELS:
        cl = catalog_level(level)
        lines = []
        for v in TEMPLATE_VARIABLES:
            if cl not in v.levels:
                continue
            # the bare ref works for every variable; props are the narrower forms
            refs = " ".join([f"@{v.name}"] + [f"@{v.name}.{p}" for p, _ in v.props])
            lines.append(f"  {refs} — {v.description}")
        blocks.append(f"{level}:\n" + "\n".join(lines))
    return "\n".join(blocks)


class GeneratedSchemaField(BaseModel):
    """One field of a json output schema."""

    name: str = Field(description="snake_case field name")
    type: str = Field(description='one of "string", "number", "boolean", "enum", "array"')
    description: str = Field(default="", description="what this field captures")
    required: bool = Field(default=True)
    enum_values: list[str] | None = Field(default=None, description='allowed labels, only for "enum"')


class GeneratedLabel(BaseModel):
    name: str = Field(description="snake_case label, at most 24 characters")
    description: str = Field(default="", description="one line: when this label applies")


class GeneratedEvaluatorDraft(BaseModel):
    """The structured response schema for metric generation."""

    name: str = Field(description="short metric name, 2-5 words")
    description: str = Field(default="", description="one sentence: what this metric checks")
    level: str = Field(description='one of "CONVERSATION", "AGENT_RUN", "SPAN"')
    engine: str = Field(default="llm", description='"decision" (a classifier question) or "llm" (a rubric)')
    # decision engine
    task: str | None = Field(default=None, description='decision only: "binary", "multiclass" or "multilabel"')
    question: str | None = Field(default=None, description="decision only: the one literal question")
    yes_means: str | None = Field(default=None, description="binary only: what a yes means")
    no_means: str | None = Field(default=None, description="binary only: what a no means")
    pass_when: str | None = Field(default=None, description='binary only: "yes" or "no"')
    labels: list[GeneratedLabel] | None = Field(default=None, description="multiclass/multilabel only")
    fail_labels: list[str] | None = Field(default=None, description="labels that mean the item failed")
    # llm engine (and a decision metric's optional state template in `prompt`)
    output_type: str = Field(default="score", description='llm only: one of "score", "number", "boolean", "text", "json"')
    prompt: str = Field(default="", description="llm: the grading rubric. decision: empty, or a state template of headings + @VARIABLES")
    threshold: float | None = Field(default=None, description='0..1 pass threshold, only for "score"')
    schema_fields: list[GeneratedSchemaField] | None = Field(
        default=None, description='the output object fields, only for "json"'
    )
    execution_mode: str = Field(
        default="batch", description='"batch", or "sequential" when the grade depends on the earlier items'
    )


def _schema_from_fields(fields: list[GeneratedSchemaField]) -> dict[str, Any] | None:
    """schema_fields → the stored JSON Schema (same shape the UI schema builder emits)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in fields:
        name = f.name.strip()
        if not _NAME_RE.match(name):
            continue
        ftype = f.type.lower().strip()
        if ftype == "enum":
            values = [str(v).strip() for v in (f.enum_values or []) if str(v).strip()]
            prop: dict[str, Any] = {"type": "string"}
            if values:
                prop["enum"] = values
        elif ftype in ("string", "number", "boolean", "array"):
            prop = {"type": ftype}
        else:
            prop = {"type": "string"}
        if f.description.strip():
            prop["description"] = f.description.strip()
        properties[name] = prop
        if f.required:
            required.append(name)
    if not properties:
        return None
    return {"type": "object", "properties": properties, "required": required}


def _label(name: str) -> str:
    return _LABEL_RE.sub("_", (name or "").strip().lower()).strip("_")[:24]


def _decision_config(draft: GeneratedEvaluatorDraft, model: str) -> dict[str, Any] | None:
    """A decision draft → a decision column config, or None when the draft is unusable as one
    (the caller then falls back to the LLM path rather than hand the user a broken form)."""
    task = (draft.task or "").strip().lower()
    question = (draft.question or "").strip()
    if task not in ("binary", "multiclass", "multilabel") or not question:
        return None
    config: dict[str, Any] = {"model": model, "question": question[:1000]}
    prompt = draft.prompt.strip()
    if prompt:
        config["prompt"] = prompt  # a state template; the API stamps is_advanced from its @VARIABLES
    if task == "binary":
        if not (draft.yes_means or "").strip() or not (draft.no_means or "").strip():
            return None
        config.update(
            output_type=dec.BINARY, threshold=dec.DEFAULT_THRESHOLD,
            pass_when="no" if (draft.pass_when or "").strip().lower() == "no" else "yes",
            criteria={"true": draft.yes_means.strip(), "false": draft.no_means.strip()},
        )
    else:
        criteria: dict[str, Any] = {}
        for lb in draft.labels or []:
            name = _label(lb.name)
            if name and name not in criteria:
                criteria[name] = lb.description.strip() or None
        fail = [n for n in dict.fromkeys(_label(f) for f in draft.fail_labels or []) if n in criteria]
        config.update(
            output_type=dec.MULTICLASS if task == "multiclass" else dec.MULTILABEL,
            criteria=criteria, fail_options=fail,
        )
        if task == "multilabel":
            config["threshold"] = dec.DEFAULT_THRESHOLD
        elif len(fail) == len(criteria):
            config["fail_options"] = fail[:-1]  # every label failing = a column that can't pass
    return config if dec.validate(config) is None else None


def generate_evaluator_config(description: str, project_id: str) -> dict[str, Any]:
    """Returns a normalized draft `{name, description, kind, level, config}` — a decision column
    when the metric is a classification and a decision model is reachable, an LLM rubric column
    on the text default otherwise. Raises on transport errors (caller maps to HTTP 502)."""
    with provider.use_project_key(project_id):
        column_model = provider.default_column_model_id()
        decision_ok = provider.is_decision_model(column_model)
        system = _SYSTEM.replace("{engine}", _DECISION_SECTION if decision_ok else _LLM_ONLY_SECTION)
        draft = provider.run_structured_agent(
            f"Design an evaluation metric for: {description.strip()[:2000]}",
            response_format=GeneratedEvaluatorDraft,
            system_prompt=system.replace("{variables}", _variable_catalog()),
        )
    level = draft.level.upper().strip()
    if level not in LEVELS:
        level = "AGENT_RUN"
    base = {
        "name": draft.name.strip()[:120] or "Custom metric",
        "description": draft.description.strip()[:400],
        "kind": "llm_judge",
        "level": level,
    }
    if decision_ok and draft.engine.strip().lower() == "decision":
        config = _decision_config(draft, column_model)
        if config is not None:
            if draft.execution_mode.lower().strip() == "sequential":
                config["execution_mode"] = "sequential"
            return {**base, "config": config}
    return {**base, "config": _llm_config(draft)}


def _llm_config(draft: GeneratedEvaluatorDraft) -> dict[str, Any]:
    output_type = draft.output_type.lower().strip()
    if output_type not in OUTPUT_TYPES:
        output_type = "score"
    config: dict[str, Any] = {
        # pinned, not implicit: "Default" in the form now means the decision model
        "model": provider.default_model_id(),
        "prompt": draft.prompt.strip() or (draft.question or "").strip(),
        "output_type": output_type,
    }
    if draft.execution_mode.lower().strip() == "sequential":
        config["execution_mode"] = "sequential"
    if output_type == "json":
        schema = _schema_from_fields(draft.schema_fields or [])
        if schema is not None:
            config["output_schema"] = schema
        else:
            # no usable fields → an empty object isn't a metric; fall back to a plain 0-1 score
            output_type = "score"
            config["output_type"] = "score"
    if output_type == "score":
        try:
            config["threshold"] = min(max(float(draft.threshold if draft.threshold is not None else 0.6), 0.0), 1.0)
        except (TypeError, ValueError):
            config["threshold"] = 0.6
    return config
