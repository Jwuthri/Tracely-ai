"""The variable namespace an alert flow's templates render against — and the catalog that
advertises it.

This is the most important contract in the feature: `BASE_INPUTS` is what the chip panel shows,
what the assistant's `draft_alert` tool is told about, and what `build_context` actually produces.
`tests/test_alert_flow.py` pins the two together, because this is exactly the kind of
thing that drifts until a template silently renders an empty string.

Pure: `build_context` takes an already-gathered `subject` payload (the service does the ClickHouse
/ Postgres reads) and shapes it. Every key exists on every trigger — a chip that doesn't apply
renders empty rather than raising, so one bad path can't fail a run.

`triggers` on a row says where a value is actually populated, so the UI only offers `gate.*` chips
on a gate alert.
"""

from __future__ import annotations

from typing import Any

# Every trigger type, for rows that are always populated.
ALL_TRIGGERS = (
    "gate_failed",
    "trace_failed",
    "cluster_new",
    "fail_rate_over",
    "score_below",
    "trace_failure_rate",
)
_TRACE = ("trace_failed",)
_GATE = ("gate_failed",)
_CLUSTER = ("cluster_new",)
_METRIC = ("fail_rate_over", "score_below", "trace_failure_rate")

BASE_INPUTS: tuple[dict[str, Any], ...] = (
    # ── alert ────────────────────────────────────────────────────────────────
    {
        "path": "alert.name",
        "type": "string",
        "description": "Name of this alert",
        "example": "PII in production",
        "triggers": ALL_TRIGGERS,
    },
    {
        "path": "alert.trigger",
        "type": "string",
        "description": "What fired it (gate_failed, trace_failed, cluster_new, …)",
        "example": "trace_failed",
        "triggers": ALL_TRIGGERS,
    },
    {
        "path": "alert.summary",
        "type": "string",
        "description": "One-line summary Tracely wrote for this event",
        "example": "tracely.run.grounded FAILED on support-bot — PII leaked in the confirmation",
        "triggers": ALL_TRIGGERS,
    },
    {
        "path": "alert.url",
        "type": "string",
        "description": "Deep link to the thing that fired: the trace, gate run or cluster",
        "example": "https://app.tracely.dev/traces/2ac7f1c824b8",
        "triggers": ALL_TRIGGERS,
    },
    {
        "path": "alert.fired_at",
        "type": "string",
        "description": "When it fired (ISO 8601, UTC)",
        "example": "2026-08-21T09:14:02+00:00",
        "triggers": ALL_TRIGGERS,
    },
    {
        "path": "alert.project",
        "type": "string",
        "description": "Workspace name",
        "example": "Acme production",
        "triggers": ALL_TRIGGERS,
    },
    # ── agent ────────────────────────────────────────────────────────────────
    {
        "path": "agent.slug",
        "type": "string",
        "description": "Agent the event belongs to ('' when the event is workspace-wide)",
        "example": "support-bot",
        "triggers": ALL_TRIGGERS,
    },
    # ── the failing turn ─────────────────────────────────────────────────────
    {
        "path": "trace.id",
        "type": "string",
        "description": "Trace (one agent run / turn) id",
        "example": "2ac7f1c824b880c6e303bef094191ea7",
        "triggers": _TRACE,
    },
    {
        "path": "trace.url",
        "type": "string",
        "description": "Link to the trace in Tracely",
        "example": "https://app.tracely.dev/traces/2ac7f1c824b8",
        "triggers": _TRACE,
    },
    {
        "path": "trace.thread_id",
        "type": "string",
        "description": "Conversation this turn belongs to",
        "example": "thread-8841",
        "triggers": _TRACE,
    },
    {
        "path": "trace.input",
        "type": "string",
        "description": "The user message that started the turn",
        "example": "I still haven't got my refund for order 55192",
        "triggers": _TRACE,
    },
    {
        "path": "trace.output",
        "type": "string",
        "description": "What the agent answered",
        "example": "I've refunded £42.00 to the card ending 4242.",
        "triggers": _TRACE,
    },
    {
        "path": "trace.error",
        "type": "string",
        "description": "Span error text, when the run errored",
        "example": "card processor 503",
        "triggers": _TRACE,
    },
    {
        "path": "trace.latency_ms",
        "type": "number",
        "description": "Turn latency in milliseconds",
        "example": 4210,
        "triggers": _TRACE,
    },
    {
        "path": "trace.tokens",
        "type": "number",
        "description": "Total tokens for the turn",
        "example": 3120,
        "triggers": _TRACE,
    },
    {
        "path": "trace.cost_usd",
        "type": "number",
        "description": "Turn cost in USD",
        "example": 0.0084,
        "triggers": _TRACE,
    },
    # ── evaluation ───────────────────────────────────────────────────────────
    {
        "path": "failing_evaluators",
        "type": "array",
        "description": "Names of the non-advisory evaluators that FAILED",
        "example": ["tracely.run.grounded", "tracely.tool.success"],
        "triggers": _TRACE,
    },
    {
        "path": "failure_reason",
        "type": "string",
        "description": "What the judges said, joined — the human explanation of the failure",
        "example": "PII leaked in the confirmation message; issue_refund returned 503",
        "triggers": _TRACE,
    },
    {
        "path": "scores",
        "type": "array",
        "description": "Every score on the trace as {name, verdict, value, comment}",
        "example": [{"name": "tracely.run.grounded", "verdict": "FAIL", "value": 0.2}],
        "triggers": _TRACE,
    },
    # ── the CI gate ──────────────────────────────────────────────────────────
    {
        "path": "gate.id",
        "type": "string",
        "description": "Gate run id",
        "example": "299b4d84-1632-4666",
        "triggers": _GATE,
    },
    {
        "path": "gate.url",
        "type": "string",
        "description": "Link to the gate run",
        "example": "https://app.tracely.dev/gates/299b4d84",
        "triggers": _GATE,
    },
    {
        "path": "gate.status",
        "type": "string",
        "description": "FAIL or NO_COVERAGE (the suite that could not run)",
        "example": "FAIL",
        "triggers": _GATE,
    },
    {
        "path": "gate.env",
        "type": "string",
        "description": "Environment the gate ran in",
        "example": "ci",
        "triggers": _GATE,
    },
    {
        "path": "gate.git_ref",
        "type": "string",
        "description": "Branch or commit under test",
        "example": "feat/refund-flow",
        "triggers": _GATE,
    },
    {
        "path": "gate.pr_number",
        "type": "number",
        "description": "Pull request number, when CI passed one",
        "example": 482,
        "triggers": _GATE,
    },
    {
        "path": "gate.passed",
        "type": "number",
        "description": "Cases + scenarios that passed",
        "example": 9,
        "triggers": _GATE,
    },
    {
        "path": "gate.failed",
        "type": "number",
        "description": "Cases + scenarios that failed",
        "example": 3,
        "triggers": _GATE,
    },
    {
        "path": "gate.skipped",
        "type": "number",
        "description": "Cases with no candidate trace to replay against",
        "example": 1,
        "triggers": _GATE,
    },
    {
        "path": "gate.warnings",
        "type": "array",
        "description": "Soft warnings on the run (latency/token deltas, misconfiguration)",
        "example": ["p95 latency +38% vs baseline"],
        "triggers": _GATE,
    },
    # ── the new failure mode ─────────────────────────────────────────────────
    {
        "path": "cluster.id",
        "type": "string",
        "description": "Failure cluster id",
        "example": "51bf5c90-3f9d",
        "triggers": _CLUSTER,
    },
    {
        "path": "cluster.url",
        "type": "string",
        "description": "Link to the cluster",
        "example": "https://app.tracely.dev/clusters/51bf5c90",
        "triggers": _CLUSTER,
    },
    {
        "path": "cluster.label",
        "type": "string",
        "description": "Human label Tracely gave the failure mode",
        "example": "card processor <n> — retrying",
        "triggers": _CLUSTER,
    },
    {
        "path": "cluster.taxonomy",
        "type": "string",
        "description": "Failure taxonomy bucket",
        "example": "execution: error",
        "triggers": _CLUSTER,
    },
    # ── the threshold that tripped ───────────────────────────────────────────
    {
        "path": "metric.name",
        "type": "string",
        "description": "What was measured (the evaluator, or the trace failure rate)",
        "example": "tracely.run.quality",
        "triggers": _METRIC,
    },
    {
        "path": "metric.value",
        "type": "number",
        "description": "Measured value at fire time (a rate is 0..1)",
        "example": 0.6,
        "triggers": _METRIC,
    },
    {
        "path": "metric.threshold",
        "type": "number",
        "description": "The line it crossed",
        "example": 0.2,
        "triggers": _METRIC,
    },
    {
        "path": "metric.window_minutes",
        "type": "number",
        "description": "Length of the window it was measured over",
        "example": 60,
        "triggers": _METRIC,
    },
    {
        "path": "metric.sample_size",
        "type": "number",
        "description": "How many observations were in the window",
        "example": 25,
        "triggers": _METRIC,
    },
)

# Paths the engine builds as nested dicts, so `build_context` and the catalog agree on shape.
_GROUPS = ("alert", "agent", "trace", "gate", "cluster", "metric")


def catalog_for_trigger(trigger: str) -> list[dict[str, Any]]:
    """The chips worth offering for this trigger — a gate alert has no `trace.*` to read."""
    return [
        {k: v for k, v in row.items() if k != "triggers"}
        for row in BASE_INPUTS
        if trigger in row["triggers"]
    ]


def _blank_namespace() -> dict[str, Any]:
    """Every catalog path present and empty. A template referencing an unset chip renders "",
    which is the behaviour a Jinja user expects — not a run that dies on a KeyError."""
    ns: dict[str, Any] = {g: {} for g in _GROUPS}
    for row in BASE_INPUTS:
        path = row["path"]
        default: Any = [] if row["type"] == "array" else ("" if row["type"] == "string" else 0)
        if "." in path:
            group, key = path.split(".", 1)
            ns[group][key] = default
        else:
            ns[path] = default
    return ns


def build_context(event: dict[str, Any]) -> dict[str, Any]:
    """The Jinja namespace for one firing.

    `event` is what the pipeline reports (see `services/monitoring_service.notify_event`), already
    carrying whatever subject detail the service gathered: `trace`, `gate`, `cluster`, `metric`,
    `scores`. Unknown keys are ignored; missing ones stay blank.
    """
    ns = _blank_namespace()
    for group in _GROUPS:
        incoming = event.get(group)
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                if key in ns[group]:
                    ns[group][key] = value
    # `agent` is a plain slug string on the event (the matcher scopes on it), and a group with a
    # `slug` in the namespace. Without this the `agent.slug` chip renders empty on every event —
    # which is exactly the kind of silent blank this module exists to prevent.
    if isinstance(event.get("agent"), str) and event["agent"]:
        ns["agent"]["slug"] = event["agent"]
    # Defaults that are always knowable from the event itself, so a context is useful even
    # without the per-monitor `alert` group (the editor's sample preview builds one with no rule).
    alert = ns["alert"]
    if not alert.get("trigger"):
        alert["trigger"] = str(event.get("type") or "")
    if not alert.get("summary"):
        alert["summary"] = str(event.get("summary") or "")
    if not alert.get("url"):
        alert["url"] = str(event.get("url") or "")
    if isinstance(event.get("scores"), list):
        ns["scores"] = event["scores"]
    if isinstance(event.get("failing_evaluators"), list):
        ns["failing_evaluators"] = event["failing_evaluators"]
    if event.get("failure_reason"):
        ns["failure_reason"] = str(event["failure_reason"])
    # `steps` is filled per step by the engine (positional ancestor outputs), never by the caller.
    ns["steps"] = []
    return ns


# ── step output contract ──────────────────────────────────────────────────────

_TEXT_OUTPUT = ({"name": "text", "type": "string", "description": "Model output"},)

DECLARED_OUTPUTS: dict[str, tuple[dict[str, str], ...]] = {
    "webhook": (
        {"name": "status", "type": "number", "description": "HTTP status"},
        {"name": "text", "type": "string", "description": "Response body (truncated)"},
    ),
    "slack": (
        {"name": "status", "type": "number", "description": "HTTP status from Slack"},
        {"name": "text", "type": "string", "description": "Slack's response body"},
    ),
    "send_email": (
        {"name": "status", "type": "number", "description": "HTTP status from Resend"},
        {"name": "recipients", "type": "array", "description": "Addresses the mail went to"},
    ),
    "condition": (
        {"name": "matched", "type": "boolean", "description": "true / false — gate decision"},
        {"name": "expression", "type": "string", "description": "Rendered expression text"},
    ),
    "python_expression": (
        {"name": "result", "type": "string", "description": "Value the expression returned"},
    ),
}


def declared_outputs(step_type: str, config: dict | None = None) -> list[dict[str, str]]:
    """What a step of this type puts in `steps[i].result` — the right-hand Output panel, and what
    the assistant is told a step produces before it wires the next one."""
    if step_type == "llm_prompt":
        schema = (config or {}).get("output_schema")
        if isinstance(schema, list) and schema:
            rows = [
                {
                    "name": str(r["name"]),
                    "type": str(r.get("type") or "string"),
                    "description": str(r.get("description") or "LLM-generated field"),
                }
                for r in schema
                if isinstance(r, dict) and r.get("name")
            ]
            if rows:
                return rows
        return [dict(r) for r in _TEXT_OUTPUT]
    return [dict(r) for r in DECLARED_OUTPUTS.get(step_type, ())]
