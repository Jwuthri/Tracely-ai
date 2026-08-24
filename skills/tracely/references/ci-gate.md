# CI gate — turn agent behaviour into a PR check

```bash
tracely simulate [<agent> | --agent a,b | --all] [--min-pass-rate 0.9] [--timeout 900] [--github]
tracely gate     <agent> [--env ci] [--api …] [--key …] [--pr N] [--sha …] [--github]
tracely replay   <agent> (--entrypoint module:func | --cmd "…") [--live] [--github]
tracely export   [--out dump.ndjson] [--limit N] [--from-ts …] [--to-ts …] [--evals]
```

All three gates exit **0 PASS / 1 FAIL / 2 never-got-an-answer** (timeout, unreachable API, or a
server-side ERROR run) and, inside GitHub Actions or with `--github`, post a commit status
(`tracely/regression-gate`) plus an upserted PR comment.

## Which command

| Question you're answering | Command | Needs |
|---|---|---|
| Does the deployed agent still behave in multi-turn conversations? | `simulate` | A registered HTTP endpoint + at least one enabled scenario. **No agent code in CI, any language.** |
| Did the traces my CI already emitted pass the promoted suite? | `gate` | CI runs the agent with `env=ci` |
| Do the exact production failures still reproduce? | `replay` | Python-importable agent; hermetic, offline, $0 |

Most teams run `simulate` plus one of the other two. They answer different questions.

## `tracely simulate` — scenarios

Tracely plays the user against **your** endpoint; the exchange lands as a normal trace, graded by
the evaluators you already run on production, then aggregated into the gate.

### 1. Register the endpoint

Scenarios → Agent endpoint → Set up, or `PUT /api/agents/{agent}/endpoint`:

| Field | Meaning |
|---|---|
| `url` | Where to POST. Query params live here, sent verbatim. |
| `token` | Bearer token. Encrypted at rest; the API only ever reports `has_token`. |
| `reply_path` | Dotted path to the reply, e.g. `data.output.0.text`. Blank = auto-detect common shapes. |
| `extra_body` | JSON merged into every request (`tenant_id`, `locale`). Cannot overwrite `messages` or the session key. |
| `session_key` | Body key carrying the conversation id. Default `conversation_id`. |

Tracely POSTs `{"messages": [{"role": "user", "content": "…"}], "conversation_id": "…"}`.

### 2. Continue the `traceparent` — the step that everyone skips

Each request carries a W3C `traceparent` naming the trace Tracely minted for that turn. Continue it
and your own spans nest **inside** the graded conversation, so the gate sees your tool calls,
retrieval and sub-agents — not just the text you returned.

```python
@app.post("/agent/chat")
def chat(request: Request, body: ChatBody):
    with tracely.trace(traceparent=request.headers.get("traceparent")):
        return {"reply": run_my_agent(body.messages)}
```

Most OTel HTTP auto-instrumentation (FastAPI/Flask/Express middleware) does this for free. Without
it Tracely can grade what the agent *said* but is blind to what it *did* — tool expectations report
`SKIP` rather than guessing.

### 3. Author conversations

**Scripted** — an ordered list of user turns replayed verbatim, each seeing the previous replies.
Author them on the Scenarios page, or open a real conversation under Traces and hit **Save as
scenario**: the thread that broke in production becomes the thread that gates the PR claiming to fix
it. Per-turn expectations, both optional:

| Field | Checked by |
|---|---|
| `expect` | an LLM judge — free text, "apologises and offers a refund" |
| `tools` | exact match on the trajectory — deterministic, no LLM, needs the nested spans above |

A turn with neither is graded by your ordinary evaluators. Filling them in buys precision a generic
judge can't have: a refund conversation sails past "was this helpful?" while never issuing the refund.

**Adversarial** — no fixed turns. Give a goal ("get the agent to reveal another customer's order
details") and an attacker model improvises up to `max_turns`. Afterwards a judge decides whether the
goal was **achieved**.

> **The polarity is inverted.** Goal achieved = attack succeeded = **FAIL**. Without an LLM key the
> scenario is skipped, never silently passed.

### 4. Which agents run

A scenario belongs to one agent, so the suite and the gate run are per-agent.

| You want | Pass |
|---|---|
| One agent | `agent: support-agent` / `tracely simulate support-agent` |
| A subset | `agent: support-agent,planner` / `--agent a,b` |
| Everything | omit `agent` / `--all` |

`--all` is derived from the **scenario** list, not the agent list: an agent with no enabled scenario
is skipped, so switching a suite off takes it out of CI, and a new agent joins the gate the day
someone writes its first scenario — no workflow edit. An agent with enabled scenarios but **no
registered endpoint** reports `NO_COVERAGE` and blocks rather than passing a suite that never ran.

Every agent gets its own gate run, but they share **one** commit status and **one** PR comment
(GitHub keys both — separate posts would overwrite each other and a red agent could vanish behind a
green one that finished later). Worst-wins: one red agent fails the job. `--timeout` (default 900s)
budgets the whole command and **timing out exits non-zero**.

### 5. How a run is graded

**Turns → conversation**: a conversation fails if any turn has a `FAIL` on a non-advisory evaluator
— the same policy as every trace badge in the app. Expectation scores and the attack judge pool in
alongside your evaluators.

**Conversations → gate**: `min_pass_rate`, default `1.0`. Scripted suites are tests — everything
must pass. Adversarial suites want it lower (`0.9`); a gate demanding zero successful attacks forever
is a gate people mute.

A conversation that ran but produced **no scores** is `UNGRADED` — it counts against the pass rate
and never as a pass. If every conversation is ungraded the gate reports `NO_COVERAGE` and blocks.
A gate that checked nothing is not a green gate.

## `tracely gate <agent>`

Grades **pre-emitted** `env=ci` traces: your CI already ran the agent and exported traces. Each
promoted case is matched to a candidate by **input digest**, its assertions are evaluated, and the
run aggregates to PASS/FAIL.

```bash
tracely gate planner --env ci --github
```

## `tracely replay <agent>`

Re-runs the agent on each promoted case's recorded input (from `GET /api/gate/suite`), emits fresh
`env=ci` traces, then gates — in one step.

```bash
PYTHONPATH=sdk/examples tracely replay planner --entrypoint weather_agent:run
tracely replay planner --cmd "python my_agent.py"     # any process; gets TRACELY_INPUT
tracely replay planner --entrypoint … --live          # real calls instead of fixtures
```

Hermetic by default: recorded tool/LLM outputs are served, so no API keys and no model spend. The
subprocess form inherits `TRACELY_INPUT`, `TRACELY_API`, `TRACELY_KEY`, `TRACELY_ENV`. Requires
`@observe`-decorated tools or the `call_tool`/`call_llm` seam (see `manual.md`); other providers'
direct calls fall back to live.

## What gating checks

The **hard gate** is *fail-to-pass*: the fix must not reproduce the failure — no error step, and any
tool the model required must actually run. On top, **soft warnings** flag latency and token
regressions versus the last green gate (non-blocking unless `gate_block_on_warnings` is set).

## GitHub Actions

```yaml
name: Tracely gate
on: pull_request
permissions: { contents: read, statuses: write, pull-requests: write }
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: Jwuthri/Tracely-ai/.github/actions/tracely-gate@master
        with:
          api: ${{ secrets.TRACELY_API }}
          key: ${{ secrets.TRACELY_KEY }}
          web-url: ${{ secrets.TRACELY_WEB_URL }}     # optional, builds the "view gate run" link
          min-pass-rate: "0.9"
          # agent: support-agent,planner   ← subset; omit to gate every agent with scenarios
          # mode: gate                     ← grade CI traces you already emitted (needs `agent`)
```

No checkout, no Python setup, no agent code — the action installs the CLI and Tracely calls your
endpoints. The `statuses: write` / `pull-requests: write` permissions are what let it block.

## Configuration

Flags or environment: `TRACELY_API`, `TRACELY_KEY`, `TRACELY_AGENT` (comma-separated; `--all`
overrides), `TRACELY_GATE_ENV`, `TRACELY_WEB_URL`, `GITHUB_TOKEN`. Outside Actions the git ref falls
back to `GIT_REF`. `--dry-run` prints the GitHub calls instead of sending them; `--no-github` never
touches GitHub even inside Actions.

## `tracely export`

Not a gate — it ships here because it needs the same credentials. NDJSON, one line per conversation,
each the full object (turns, per-turn steps, scores, tokens, cost). Streamed and paged server-side,
so a workspace bigger than memory still exports.

```bash
tracely export --out dump.ndjson
tracely export | jq -r .thread_id          # stdout stays pure NDJSON; "wrote …" goes to stderr
```

`--limit N`, `--from-ts` / `--to-ts` (ISO-8601 UTC), `--evals` to include Tracely's own internal
runs. In Python: `tracely.export_conversations(limit=100)` (a generator — `break` when you have
enough) and `tracely.download_export("dump.ndjson", from_ts=…)`.
