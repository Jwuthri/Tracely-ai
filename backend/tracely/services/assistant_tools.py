"""The tools the in-app assistant drives Tracely with.

Every tool is a thin wrapper over one of our own HTTP endpoints (`api/internal_client`), carrying
the credentials of the person chatting. So the agent reaches exactly the workspace that person can
reach, and every write it makes goes through the same validation the UI does. No tool touches the
database, none of them knows what a project id is, and adding one is about eight lines.

Two rules make this work, both learned the hard way:

1. **Failures are returned, not raised.** langgraph's ToolNode re-raises anything that isn't a
   bad-arguments error, which kills the stream mid-answer and leaves the user with a dead chat.
   Handing the model `error: Tracely API 422: level must be one of …` instead lets it read the
   router's own complaint and correct itself — the validation we already wrote for the UI becomes
   the agent's feedback loop, for free.
2. **Results are clipped.** One `export_conversations` call can be megabytes; unclipped, a single
   tool result evicts the conversation it was meant to inform.

3. **No tool argument may be called `config`.** `StructuredTool._arun` takes its own keyword-only
   `config` (the `RunnableConfig`), so an argument of that name is swallowed there and never
   reaches the function — which fails at call time, deep in a tool loop, not at import. Hence
   `evaluator_config`. The MCP server's twin of this tool can and does call it `config`; FastMCP
   has no such reserved name.

Deletes are here (the product decision was full access), but the system prompt requires the agent
to ask before calling one. That rail is a prompt, not an enforcement — a deployment that wants
deletes truly gone should drop them from `build_tools` rather than trust the wording.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from tracely.api.internal_client import api_call

# One tool result's ceiling. Large enough for a cluster with its members or a trace with its
# spans; small enough that three of them still leave room to think.
MAX_RESULT_CHARS = 15_000
# Whole conversations are the heaviest thing here — every turn, every span, every score.
EXPORT_MAX = 10
# A backfill re-grades with real judge calls, on OUR event loop, inside one chat turn.
EVAL_TARGET_MAX = 20
# What a tool call stores on its recorded span — for a human reading the trace, not the model.
RECORD_CHARS = 4_000
# Driving a scenario or replaying a case makes real HTTP calls to the customer's agent.
SLOW_TIMEOUT_S = 300.0


def _clip(value: Any) -> Any:
    """Keep one tool result from eating the context. Oversized results come back as text with a
    note telling the model how to ask for less, which it can act on — unlike a silent truncation."""
    text = json.dumps(value, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return value
    return (
        text[:MAX_RESULT_CHARS] + f"\n… [truncated: {len(text)} chars total. Narrow the request — "
        "a smaller `limit`, one agent, a shorter time window — to see the rest.]"
    )


async def _api(headers: dict[str, str], method: str, path: str, **kw: Any) -> Any:
    """One endpoint call on the caller's behalf, with both rules from the module docstring applied."""
    try:
        return _clip(await api_call(headers, method, path, **kw))
    except Exception as exc:  # including ValueError carrying the router's own 4xx body
        return f"error: {exc}"[:1000]


def _params(**kw: Any) -> dict[str, Any]:
    """Query params, minus the ones the caller left empty — so a blank optional argument means
    'don't filter' rather than 'filter on the empty string'."""
    return {k: v for k, v in kw.items() if v not in ("", None)}


def _recorded(tool_obj):
    """File this tool's call into the active introspection Recording, if there is one.

    Wrapping the tool object rather than each function keeps the recording out of thirty
    docstrings — and, more usefully, captures the ARGUMENTS and the RESULT, which is what makes a
    recorded turn answer "did it call the right tool with the right ids" instead of just naming it.
    Costs nothing when no recording is open, which is every call outside the in-app assistant.
    """
    from tracely.domain import introspection

    inner = tool_obj.coroutine

    async def traced(**kwargs):
        rec = introspection.active()
        if rec is None:
            return await inner(**kwargs)
        start_ns = time.time_ns()
        try:
            result = await inner(**kwargs)
        except Exception as exc:  # a tool that raises is a bug, but it must still be legible
            rec.add(
                tool_obj.name, obs_type="TOOL", start_ns=start_ns,
                input=_clip_text(json.dumps(kwargs, default=str)), error=str(exc)[:500],
            )
            raise
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        rec.add(
            tool_obj.name, obs_type="TOOL", start_ns=start_ns,
            input=_clip_text(json.dumps(kwargs, default=str)),
            output=_clip_text(text),
            # `error:` is how a tool reports failure here (see the module docstring), so the
            # recording has to read the text to know a step went wrong.
            error=text[:500] if isinstance(result, str) and result.startswith("error:") else "",
        )
        return result

    tool_obj.coroutine = traced
    return tool_obj


def _clip_text(text: str) -> str:
    """What gets stored on the span. Smaller than `MAX_RESULT_CHARS`: the model needs the whole
    result to reason with, a person reading the trace needs enough to recognise it."""
    return text if len(text) <= RECORD_CHARS else text[:RECORD_CHARS] + f"… [{len(text)} chars]"


def build_tools(headers: dict[str, str], path: str = "") -> list:
    """The assistant's toolbox, bound to one caller's credentials.

    Built per turn rather than once at import: the headers are the caller's, and a tool that
    outlived the request that made it would be a tool pointed at the wrong workspace. `path` is
    the page the user is on — `draft_alert` is only useful while the alert editor is open.
    """
    from langchain_core.tools import tool

    async def call(method: str, path: str, **kw: Any) -> Any:
        return await _api(headers, method, path, **kw)

    # ── reading: traces and conversations ────────────────────────────────────

    @tool
    async def list_traces(limit: int = 20) -> Any:
        """Recent traces (newest first) with their verdict, agent, latency, cost and token counts.

        One row per agent run. Start here for "what is my agent doing" or "what is failing".
        """
        return await call("GET", "/api/traces", params={"limit": limit})

    @tool
    async def get_trace(trace_id: str) -> Any:
        """One trace in full: every span (nested by parent), its inputs/outputs, and every
        evaluator score attached to it. This is the evidence for why a run passed or failed —
        read it before explaining any individual failure."""
        return await call("GET", f"/api/traces/{trace_id}")

    @tool
    async def search_traces(query: str) -> Any:
        """Find conversations whose user message contains `query` (min 2 characters). Also
        matches agents, cases and clusters by name. The way in when the user describes a
        problem in words rather than giving you an id."""
        return await call("GET", "/api/search", params={"q": query})

    @tool
    async def list_conversations(
        limit: int = 20, agent: str = "", sort: str = "recent", evals: bool = False
    ) -> Any:
        """Conversations (threads of turns), newest first: first user message, last answer, turn
        count, tokens, cost, status, and the conversation-level evaluator scores.

        `sort` is recent | started | duration | tokens. `evals=true` also lists Tracely's own
        internal runs, which are hidden by default — you rarely want them.
        """
        return await call(
            "GET", "/api/sessions", params=_params(limit=limit, agent=agent, sort=sort, evals=evals)
        )

    @tool
    async def get_conversation(thread_id: str) -> Any:
        """One conversation: every turn with its input, output and scores, plus the
        conversation-level scores. Use this to answer "why did this conversation fail" — then
        `get_trace` on the failing turn for the span-level detail."""
        return await call("GET", f"/api/sessions/{thread_id}")

    @tool
    async def export_conversations(
        limit: int = 5, meta: str = "", from_ts: str = "", to_ts: str = ""
    ) -> Any:
        """Whole conversations, newest first — turns, the spans behind each turn, and scores at
        both levels. `list_conversations` answers "what ran"; this answers "what was actually
        said, step by step", across several conversations at once.

        `meta` filters on span metadata as `key=value` (e.g. `business_id=2a73…`) — how you pull
        one tenant's traffic out of a shared workspace. `from_ts`/`to_ts` are ISO-8601 UTC bounds.
        Heavy: prefer a narrower window over a bigger `limit`.
        """
        return await call(
            "GET",
            "/api/export",
            ndjson=True,
            params=_params(limit=max(1, min(limit, EXPORT_MAX)), meta=meta, from_ts=from_ts, to_ts=to_ts),
        )

    # ── reading: failure intelligence, trends, CI ────────────────────────────

    @tool
    async def list_clusters(limit: int = 20, min_size: int = 0) -> Any:
        """Failure clusters (biggest first): recurring failure modes grouped by semantic
        similarity, each with a title, status and member count. The fastest read on what is
        actually broken in this workspace."""
        return await call("GET", "/api/clusters", params=_params(limit=limit, min_size=min_size or None))

    @tool
    async def get_cluster(cluster_id: str, members: int = 25) -> Any:
        """One failure cluster in detail: its analysis, its proposed fix, its severity, and the
        traces belonging to it — the raw material for a regression case."""
        return await call("GET", f"/api/clusters/{cluster_id}", params={"members": members})

    @tool
    async def get_trends(days: int = 14) -> Any:
        """Daily traces vs failures over `days` (1–90), plus gate and cluster roll-ups. Use this
        for "is it getting better or worse"."""
        return await call("GET", "/api/trends", params={"days": days})

    @tool
    async def get_ops_metrics(days: int = 14) -> Any:
        """Latency (p50/p95), throughput, token and cost roll-up over `days` (1–90)."""
        return await call("GET", "/api/ops", params={"days": days})

    @tool
    async def list_cases(limit: int = 50) -> Any:
        """The regression cases in this workspace — the frozen failures that gate a pull
        request, each with its agent, status and last replay result."""
        return await call("GET", "/api/cases", params={"limit": limit})

    @tool
    async def list_gates(limit: int = 20) -> Any:
        """Recent CI gate runs, newest first: which commit/PR, which agent, pass rate, verdict."""
        return await call("GET", "/api/gates", params={"limit": limit})

    @tool
    async def get_gate(gate_id: str) -> Any:
        """One gate run in detail, including the per-case results behind its verdict — what to
        read when someone asks why their PR was blocked."""
        return await call("GET", f"/api/gates/{gate_id}")

    # ── evaluators: the columns of the traces table ──────────────────────────

    @tool
    async def list_evaluators() -> Any:
        """Every evaluator configured here — id, kind, level, score_name, enabled, full config.
        Each one is a column in the traces table."""
        return await call("GET", "/api/evaluators")

    @tool
    async def list_evaluator_templates() -> Any:
        """The catalog of ready-made evaluators, each with a proven `config` and a flag for
        whether this workspace already installed it. Prefer copying a template's config into
        `create_evaluator` over inventing a rubric from scratch."""
        return await call("GET", "/api/evaluators/templates")

    @tool
    async def create_evaluator(
        name: str,
        evaluator_config: dict,
        kind: str = "llm_judge",
        level: str = "AGENT_RUN",
        description: str = "",
        score_name: str = "",
        enabled: bool = True,
    ) -> Any:
        """Add a new evaluator — a new column on the traces table.

        kind="llm_judge" — `evaluator_config` takes `prompt` (the rubric), `output_type`
        ("score" | "boolean" | "json"), `threshold` (for "score"), and optional `advisory`
        (true = a FAIL is recorded but does not flip the trace's verdict, which is what
        subjective-quality columns should be). `level` is one of CONVERSATION, AGENT_RUN, SPAN,
        TOOL, GENERATION, CHAIN.

        kind="structural" — `evaluator_config` takes `check`, one of run_outcome (AGENT_RUN),
        tool_success (TOOL), tool_consistency (AGENT_RUN), latency (AGENT_RUN, plus `budget_ms`),
        required_tools (AGENT_RUN, plus `tools`). The level is fixed per check, as noted.

        A new evaluator grades traces ingested from now on — call `run_evaluation` to backfill it
        over conversations that already exist.
        """
        return await call(
            "POST",
            "/api/evaluators",
            json={
                "name": name,
                "description": description,
                "kind": kind,
                "level": level,
                "enabled": enabled,
                "config": evaluator_config,
                "score_name": score_name,
            },
        )

    @tool
    async def update_evaluator(
        evaluator_id: str,
        name: str = "",
        description: str = "",
        level: str = "",
        enabled: bool | None = None,
        evaluator_config: dict | None = None,
        target_agent: str = "",
        target_env: str = "",
        sampling: float | None = None,
    ) -> Any:
        """Patch an evaluator — only the fields you pass change. `enabled=false` retires a column
        without losing the scores it already produced, which is nearly always the right move
        instead of deleting it. `sampling` (0.0–1.0) grades a deterministic fraction of traces;
        `target_agent` / `target_env` scope the column to one agent or environment."""
        patch = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "level": level,
                "enabled": enabled,
                "config": evaluator_config,
                "target_agent": target_agent,
                "target_env": target_env,
                "sampling": sampling,
            }.items()
            if v not in ("", None)
        }
        return await call("PATCH", f"/api/evaluators/{evaluator_id}", json=patch)

    @tool
    async def run_evaluation(
        thread_ids: list[str] | None = None,
        trace_ids: list[str] | None = None,
        evaluator_ids: list[str] | None = None,
    ) -> Any:
        """Grade existing conversations or turns now — the backfill after adding a column, or a
        re-grade after editing a rubric.

        Pass `thread_ids` (whole conversations) and/or `trace_ids` (single turns); leave
        `evaluator_ids` empty to run every enabled evaluator. Real judge calls, so it is slow and
        capped at 20 targets per call — do a representative sample, not the whole workspace.
        """
        threads = [t for t in (thread_ids or []) if t][:EVAL_TARGET_MAX]
        traces = [t for t in (trace_ids or []) if t][:EVAL_TARGET_MAX]
        if not threads and not traces:
            return "error: pass at least one thread_id or trace_id"
        frames = await call(
            "POST",
            "/api/evaluations/run",
            sse=True,
            timeout=SLOW_TIMEOUT_S,
            json={
                "thread_ids": threads,
                "trace_ids": traces,
                "evaluator_ids": [e for e in (evaluator_ids or []) if e],
            },
        )
        if not isinstance(frames, list):
            return frames  # an error string from `_api`
        return {
            "targets": len(threads) + len(traces),
            "scores_written": sum(1 for f in frames if f.get("type") == "result"),
            "errors": [f for f in frames if f.get("type") in ("target_error", "error")],
        }

    # ── alerts: tell me without me looking ──────────────────────────────────

    @tool
    async def list_alerts() -> Any:
        """The workspace's alerts, with each one's current state (`last_fired_at`,
        `last_fired_summary`). An alert pushes one Tracely signal to Slack, email or a webhook."""
        return await call("GET", "/api/monitors")

    @tool
    async def create_alert(
        name: str,
        trigger: str,
        destination: str,
        action: str = "slack",
        agent: str = "",
        contains: str = "",
        score_name: str = "",
        env: str = "",
        message: str = "",
        threshold: float = 0.0,
        window_minutes: int = 60,
        min_samples: int = 20,
        min_interval_seconds: int = 900,
    ) -> Any:
        """Create an alert: a trigger plus a one-step flow that notifies `destination`.

        `action` is `slack` (an incoming-webhook URL), `email` (an address) or `webhook` (your own
        endpoint, POSTed a JSON body). `message` is the notification text and may use Jinja
        variables — `{alert.summary}`, `{alert.url}`, `{failure_reason}`, `{agent.slug}`,
        `{gate.status}`, `{cluster.label}` — each wrapped in DOUBLE braces. Leave it empty for a
        sensible default.

        Event triggers fire the moment the thing happens; filters are optional and ANDed:
        - `gate_failed` — a CI gate finished FAIL or NO_COVERAGE. Filters: `env`, `contains`.
        - `trace_failed` — a live turn failed a non-advisory evaluator. Filters: `score_name`,
          `contains` (case-insensitive substring of the evaluator names AND the judge's reason —
          this is how "tell me when a conversation leaks PII" is expressed).
        - `cluster_new` — a failure signature nothing has produced before. Filter: `contains`.

        Threshold triggers are evaluated over a sliding window every 5 minutes and need
        `threshold` (0..1) plus `window_minutes` / `min_samples`:
        - `fail_rate_over` / `score_below` — one evaluator (`score_name` required).
        - `trace_failure_rate` — every trace in the workspace.

        `agent` scopes to one agent (slug); `min_interval_seconds` rate-limits (0 = every time).
        Confirm the destination with the user first — this sends real messages. The user can then
        open the rule in Settings → Alerts and draw more steps onto it (conditions, an LLM step, a
        webhook with its own headers).
        """
        condition: dict[str, Any] = {"type": trigger}
        if contains:
            condition["contains"] = contains
        if score_name:
            condition["score_name"] = score_name
        if env:
            condition["env"] = env
        if trigger in ("fail_rate_over", "score_below", "trace_failure_rate"):
            condition["threshold"] = threshold
            condition["window_minutes"] = window_minutes
            condition["min_samples"] = min_samples

        text = message or "🚨 {{ alert.name }}\n{{ alert.summary }}\n{{ alert.url }}"
        if action == "email":
            step_config: dict[str, Any] = {
                "to_template": destination,
                "subject_template": "[Tracely] {{ alert.name }}",
                "body_template": text,
            }
            step_type = "send_email"
        elif action == "webhook":
            step_config = {
                "url": destination,
                "method": "POST",
                "headers": [],
                "body_template": '{"summary": "{{ alert.summary }}", "url": "{{ alert.url }}"}',
            }
            step_type = "webhook"
        else:
            step_config = {"url": destination, "text_template": text}
            step_type = "slack"

        # A step id is a global primary key (it doubles as the canvas node id), so it has to be
        # unique per rule — a constant collided on the second alert the assistant ever made.
        step_id = f"s-{uuid4()}"
        return await call(
            "POST",
            "/api/monitors",
            json={
                "name": name,
                "target_agent": agent,
                "condition": condition,
                "steps": [
                    {"id": step_id, "order_index": 0, "name": f"Notify {action}",
                     "step_type": step_type, "config": step_config}
                ],
                "flow_layout": {
                    "nodes": [
                        {"id": "__rule_trigger__", "type": "trigger",
                         "position": {"x": 60, "y": 140}, "data": {"label": trigger}},
                        {"id": step_id, "type": "ruleStep", "position": {"x": 360, "y": 140},
                         "data": {"name": f"Notify {action}", "step_type": step_type}},
                    ],
                    "edges": [
                        {"id": f"e-__rule_trigger__-{step_id}", "source": "__rule_trigger__",
                         "target": step_id}
                    ],
                },
                "min_interval_seconds": min_interval_seconds,
            },
        )

    @tool
    async def draft_alert(
        name: str,
        trigger: str,
        steps: list[dict[str, Any]],
        description: str = "",
        target_agent: str = "",
        contains: str = "",
        score_name: str = "",
        env: str = "",
        threshold: float = 0.0,
        window_minutes: int = 60,
        min_samples: int = 20,
    ) -> Any:
        """Draw a whole alert rule — trigger, filters and a wired chain of steps — onto the alert
        editor the user has open (/settings/alerts/new or /settings/alerts/{id}). Nothing is
        saved: the canvas redraws and the user presses Save. Use it for anything richer than one
        notification; `create_alert` is for a one-step rule from any page.

        The page's current rule arrives as page state with the user's message; when they ask
        for a change, pass the whole rule back with only that change made, step names intact.

        `trigger` and filters are as in `create_alert`. `steps` run in order, each
        `{"name": str, "step_type": str, "config": {...}}`; the config shapes:
          condition          {"expression": "{{ jinja }}"}  — falsy stops the flow
          slack              {"url": "", "text_template": "…"}
          send_email         {"to_template": "", "subject_template": "…", "body_template": "…"}
          webhook            {"url": "", "method": "POST", "headers": [{"key": "Authorization",
                              "value": "Bearer "}], "body_template": "{\\"k\\": \\"{{ v }}\\"}"}
          llm_prompt         {"model": "", "system_prompt": "…", "user_prompt_template": "…",
                              "temperature": 0, "output_schema": [{"name": "…", "type": "string",
                              "description": "…"}]}
          python_expression  {"expression": "len(failing_evaluators)"} — names directly, no {{ }}
        Every string is a Jinja template over the trigger's variables ({{ alert.summary }},
        {{ trace.url }}, {{ failure_reason }}, {{ failing_evaluators }}, {{ gate.status }},
        {{ cluster.label }}); an upstream step is positional — `{{ steps[0].result }}` is the
        first step before this one, and an llm_prompt with an output_schema exposes
        `steps[i].result.<field>`, otherwise `steps[i].result.text`.

        NEVER invent a URL, an email address or a model id: leave `url` / `to_template` / `model`
        empty (the user picks them in the step's inspector) and say so.
        Prefer a trigger filter to a condition step when the trigger can express it — a filter
        stops the flow from running at all. Add an llm_prompt step only when they asked for
        written or classified output.
        """
        from tracely.domain.monitoring.conditions import EVENT_TYPES, POLLED_TYPES
        from tracely.services.alert_flow_service import STEP_TYPES

        if not path.startswith("/settings/alerts/"):
            return (
                "error: the user is not on the alert editor, so there is no canvas to draw on. "
                "Use create_alert for a one-step rule, or send them to /settings/alerts/new and "
                "draft it there."
            )
        if trigger not in EVENT_TYPES | POLLED_TYPES:
            return f"error: unknown trigger {trigger!r}; one of {sorted(EVENT_TYPES | POLLED_TYPES)}"
        bad = [s.get("step_type") for s in steps if s.get("step_type") not in STEP_TYPES]
        if bad or not steps:
            return f"error: steps must be non-empty with step_type in {sorted(STEP_TYPES)}; got {bad}"
        # The browser applies the ARGUMENTS of this call (it sees them on the stream), so a
        # rejected draft never reaches the canvas and an accepted one is exactly what was sent.
        return {
            "drawn": True,
            "steps": len(steps),
            "note": "on the canvas now; the user must paste destinations and press Save",
        }

    # ── scenarios: emulated conversations that gate a PR ─────────────────────

    @tool
    async def list_scenarios(agent: str = "") -> Any:
        """The scenarios configured here, optionally for one agent. A scenario is a conversation
        Tracely drives against the customer's agent endpoint — scripted turns, or an adversarial
        goal an attacker model pursues."""
        return await call("GET", "/api/scenarios", params=_params(agent=agent))

    @tool
    async def create_scenario(
        agent: str,
        kind: str = "SCRIPTED",
        title: str = "",
        turns: list[dict] | None = None,
        goal: str = "",
        max_turns: int = 0,
    ) -> Any:
        """Author a scenario for `agent` (its slug or id).

        kind="SCRIPTED" needs `turns`: a list of `{"message": "...", "expect": "...",
        "tools": ["..."]}` — `expect` and `tools` are optional per turn, and with neither the turn
        is graded only by the workspace's own evaluators.

        kind="ADVERSARIAL" needs `goal` instead: what an attacker model should try to make the
        agent do. The goal being ACHIEVED is a FAIL — that is the point of an adversarial run.

        `max_turns` (1–30) bounds the conversation. Prefer `import_scenario` when a real
        conversation already shows the behaviour you want to pin down.
        """
        return await call(
            "POST",
            "/api/scenarios",
            json={
                "agent": agent,
                "kind": kind,
                "title": title,
                "turns": turns or [],
                "goal": goal,
                **({"max_turns": max_turns} if max_turns else {}),
            },
        )

    @tool
    async def import_scenario(thread_id: str, agent: str, title: str = "") -> Any:
        """Turn a real conversation into a scenario: its user messages are kept verbatim, so the
        conversation that broke in production becomes the one that gates the PR claiming to fix
        it. Capped at 30 turns."""
        return await call(
            "POST",
            "/api/scenarios/import",
            json=_params(thread_id=thread_id, agent=agent, title=title),
        )

    @tool
    async def run_scenario(scenario_id: str, env: str = "") -> Any:
        """Drive one scenario against its agent's endpoint now. Returns a conversation id
        immediately; the run itself takes minutes and fills that conversation in as it goes, so
        tell the user where to watch rather than waiting for a verdict here."""
        return await call(
            "POST",
            f"/api/scenarios/{scenario_id}/run",
            timeout=SLOW_TIMEOUT_S,
            json=_params(env=env),
        )

    # ── regression cases ─────────────────────────────────────────────────────

    @tool
    async def promote_trace(trace_id: str) -> Any:
        """Freeze one failing trace into a regression case: its inputs and tool trajectory become
        a fixture that replays on every PR. The source trace must currently FAIL — a case that
        passed at birth would gate nothing, so it stays a DRAFT instead."""
        return await call("POST", f"/api/traces/{trace_id}/promote", timeout=SLOW_TIMEOUT_S)

    @tool
    async def promote_cluster(cluster_id: str) -> Any:
        """Turn a whole failure cluster into a regression case, using its most representative
        member. The usual path from "this keeps happening" to "this can't happen again"."""
        return await call("POST", f"/api/clusters/{cluster_id}/promote", timeout=SLOW_TIMEOUT_S)

    @tool
    async def replay_case(case_id: str, candidate_trace_id: str = "") -> Any:
        """Replay one regression case now and report PASS/FAIL — how you check whether a fix
        actually landed. Defaults to replaying the case's own source trace."""
        return await call(
            "POST",
            f"/api/cases/{case_id}/replay",
            timeout=SLOW_TIMEOUT_S,
            json=_params(candidate_trace_id=candidate_trace_id),
        )

    # ── deletes: ask the user first, every time ──────────────────────────────

    @tool
    async def delete_evaluator(evaluator_id: str) -> Any:
        """Delete an evaluator column permanently. Ask the user to confirm before calling this,
        and offer `update_evaluator(enabled=false)` first — retiring a column keeps the scores it
        already produced, deleting it does not."""
        return await call("DELETE", f"/api/evaluators/{evaluator_id}")

    @tool
    async def delete_scenario(scenario_id: str) -> Any:
        """Delete a scenario permanently. Ask the user to confirm before calling this."""
        return await call("DELETE", f"/api/scenarios/{scenario_id}")

    @tool
    async def delete_case(case_id: str) -> Any:
        """Delete a regression case permanently — the PR gate stops checking it. Ask the user to
        confirm before calling this."""
        return await call("DELETE", f"/api/cases/{case_id}")

    @tool
    async def delete_clusters(cluster_ids: list[str]) -> Any:
        """Delete failure clusters. Clusters are derived from traces, so a later Analyze re-forms
        any issue whose failing traces still exist — this prunes noise, it does not fix anything.
        Ask the user to confirm before calling this."""
        return await call("DELETE", "/api/clusters", json={"cluster_ids": cluster_ids})

    @tool
    async def delete_conversations(thread_ids: list[str]) -> Any:
        """Delete whole conversations and their scores — the underlying trace data, permanently
        and unrecoverably. This is the most destructive tool here: always quote back exactly what
        will be deleted and get an explicit yes before calling it."""
        return await call("DELETE", "/api/sessions", json={"threads": thread_ids})

    return [_recorded(t) for t in (
        list_traces, get_trace, search_traces, list_conversations, get_conversation,
        export_conversations, list_clusters, get_cluster, get_trends, get_ops_metrics,
        list_cases, list_gates, get_gate,
        list_evaluators, list_evaluator_templates, create_evaluator, update_evaluator,
        run_evaluation,
        list_scenarios, create_scenario, import_scenario, run_scenario,
        list_alerts, create_alert, draft_alert,
        promote_trace, promote_cluster, replay_case,
        delete_evaluator, delete_scenario, delete_case, delete_clusters, delete_conversations,
    )]
