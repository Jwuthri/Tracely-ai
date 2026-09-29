# Evaluators — the columns of the trace table

An evaluator **is** a column. It grades traces as they land (debounced ~4s after ingest) and writes
its verdict into the grid. Scores stream in live over SSE.

## Two kinds

| Kind | Cost | Use for |
|---|---|---|
| `structural` | free, deterministic, never flakes | anything checkable from the span tree |
| `llm_judge` | a model call per item | anything that needs reading the text |

**Always exhaust structural checks first.** A judge that re-derives "did a tool error" is spend and
variance for a fact already in the trace.

### Structural checks

| `config.check` | Fixed level | Fails when | Extra config |
|---|---|---|---|
| `run_outcome` | `AGENT_RUN` | any step in the run errored | — |
| `tool_success` | `TOOL` | a tool call errored | — |
| `tool_consistency` | `AGENT_RUN` | the model requested a tool that never executed (silent failure) | — |
| `latency` | `AGENT_RUN` | the run exceeds the budget | `budget_ms` (default 60000) |
| `required_tools` | `AGENT_RUN` | specific tools weren't called | `tools: [...]` |

The level is **fixed per check** — the API rejects a mismatch, because a TOOL score with no
observation id has nowhere to render.

## Levels

| Level | One result per | The judge reads |
|---|---|---|
| `CONVERSATION` | thread | the whole transcript, turn by turn |
| `AGENT_RUN` (message) | turn | that turn's user request and agent answer |
| `SPAN` / `TOOL` / `GENERATION` / `CHAIN` (step) | span | that step's input and output |

A `TOOL` / `GENERATION` / `CHAIN` column grades exactly that span type. A `SPAN` column grades every
step in the turn, narrowed by `config.span_types` — which is how you grade the newer types:
`span_types: ["SKILL"]` grades each named capability, `["DELEGATE"]` grades each routing decision.

Choosing the level decides what the column *can* catch. "Did the agent ever issue the refund" is a
conversation question; a message-level column will miss it on every turn individually.

## Output types

`score` (0–1 with a pass/fail `threshold`), `number`, `boolean`, `text`, or `json` with your own
schema. Enum fields are enforced on the model; a numeric `score` field in a JSON schema drives the
pass/fail verdict via the threshold.

## The verdict policy — one rule, everywhere

> A trace / turn / session fails **iff** it has a `FAIL` on a **non-advisory** evaluator.

`config.advisory: true` records the verdict and shows the pill but does **not** flip the roll-up.
That's what subjective-quality judges should be — otherwise one opinionated rubric turns the whole
workspace red and people mute the gate. The shipped "Answer quality · LLM judge" is advisory for
exactly this reason.

A run with **no scores at all** has no badge — ungraded is not passing.

## Batch vs sequential

- **Batch** (default) grades every item independently. Two grades of the same item are directly
  comparable. This is what you want for most metrics.
- **Sequential** grades items in order as one running conversation with the judge: rubric as system
  prompt, then item → verdict → item → verdict. A step column chains steps within a message; a
  message column chains turns of a thread. Incremental — new turns are appended, not re-graded;
  re-running from the UI re-grades from the start. Meaningless at conversation level (one item).

Use sequential when a verdict genuinely depends on what came before (drift, escalating frustration,
self-correction). Otherwise batch is cheaper and more stable.

## Basic vs advanced prompts

**Basic**: write the rubric, context is auto-injected (request / answer / tool results / transcript /
step I/O).

**Advanced**: any prompt containing an `@VARIABLE`. The resolved template becomes the **whole**
prompt — nothing is added around it, so you decide exactly what the judge reads. An unresolvable
reference becomes the literal `[No @REF available]` (soft miss, never blocks a grade).

> Sequential + advanced behaves differently: there is no running judge conversation. Chaining
> happens only through `@METRIC_PREVIOUS_RESULT`. A template that doesn't reference it behaves
> exactly like batch.

### `@VARIABLE` catalog

All levels:

| Variable | Resolves to |
|---|---|
| `@HISTORY` | full formatted conversation history |
| `@ROLLING_SUMMARY` | accumulated compact summary of the thread so far (empty if none yet) |
| `@GOAL` | the user's overall goal — first request in the thread |
| `@AGENTS` | this conversation's agents and their tools — declared, offered to the model, or seen being called (`.tools` every tool, `.called` only the used ones) |

Conversation level:

`@MESSAGES` · `@USER_MESSAGES` · `@ASSISTANT_MESSAGES` · `@FIRST_USER_MSG` · `@LAST_USER_MSG` ·
`@LAST_ASSISTANT_MSG`

Message level (step inherits):

| Variable | Resolves to |
|---|---|
| `@PREVIOUS_USER_MSG` / `@PREVIOUS_ASSISTANT_MSG` | the previous turn's request / answer |
| `@CURRENT_MESSAGE.input` / `.output` / `.role` | the turn under evaluation |
| `@CURRENT_STEPS` | all steps of the turn, formatted |
| `@CURRENT_STEPS.tool` / `.retriever` / `.generation` / `.thinking` / `.chain` / `.skill` / `.delegate` | just that step type — how a message-level rubric asks for the evidence it grades against |
| `@CURRENT_STEPS_COUNT` | number of steps in the turn |

Step level only:

| Variable | Resolves to |
|---|---|
| `@CURRENT_STEP` / `@PREVIOUS_STEP` | the step under evaluation / the one before it |
| `.tool_call` · `.tool_result` · `.thinking` · `.output_content` · `.output_structured` | props on either |
| `@STEP_NUMBER` | 1-indexed position |

Sequential mode only: `@METRIC_PREVIOUS_RESULT` (message + step).

Case matters — `@foo` and `email@x.com` are not variables. Only referenced variables are
materialized, so an unused `@HISTORY` costs nothing.

### A grounded advanced rubric

```
You grade whether the assistant's answer is supported by the tools it actually called.

User asked: @CURRENT_MESSAGE.input
Assistant answered: @CURRENT_MESSAGE.output
Tool calls and their results this turn:
@CURRENT_STEPS.tool

Return a score 0-1. Score 0 if the answer states any specific fact (number, date, name,
availability) that the tool results do not contain. Ignore style.
```

That beats a generic "was this helpful?" because it names the evidence.

## Controlling judge spend

Three knobs, applied on the **automatic** (on-ingest) run only — an explicit re-run from the UI
always grades:

| Field | Effect |
|---|---|
| `target_agent` | matches the trace's agent id **or** slug; empty = any |
| `target_env` | `prod` / `staging` / `ci` / `dev`; empty = any |
| `sampling` | 0.0–1.0. Deterministic per `(trace_id, score_name)` — a re-ingested trace makes the same keep/drop decision, so scores converge instead of flickering |

"Grade 10% of prod traces with the expensive judge, 100% of staging" is `sampling: 0.1` +
`target_env: prod` on one column and a second column for staging.

## Creating a column from a coding agent (MCP)

```
create_evaluator(
  name="Refund actually issued",
  kind="llm_judge",
  level="CONVERSATION",
  config={
    "prompt": "...rubric, may use @VARIABLES...",
    "output_type": "score",
    "threshold": 0.6,
    "advisory": False,
    "execution_mode": "batch",
  },
)
```

- `score_name` is the stable key results are stored under; leave it blank to have one derived.
- `update_evaluator(evaluator_id, enabled=False)` **retires** a column and keeps its history.
  Nothing deletes.
- New evaluators grade traces ingested **from now on** — backfill from the Evaluations UI.
- `list_evaluator_templates()` is a catalog worth copying from before writing a rubric from scratch:
  goal achievement, user frustration, conversation efficiency, hallucination, helpfulness, tone, PII
  leakage, intent, re-ask, user correction, sycophancy, trajectory quality, intent drift,
  comprehensive safety, tool-choice quality, self-correction awareness, step analysis.

## Before you let a judge gate a release

Label its verdicts against human review on the **Calibration** page. You get per-evaluator agreement
plus `false_pass` (missed failures) and `false_fail` (over-flagging). An over-flagging judge that
blocks PRs gets muted within a week, which is worse than not having it.

## No LLM key?

Structural checks still run. Judges, failure analysis and summaries **degrade gracefully** — they
switch off rather than crash. Each workspace brings its own OpenRouter key (Settings → OpenRouter
key); a project with no key is exactly an LLM-disabled deployment.
