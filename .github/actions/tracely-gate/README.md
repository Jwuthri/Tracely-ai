# `tracely-gate` action

Gate a pull request on your agent's behaviour and turn the result into a blocking commit status
(`tracely/regression-gate`) plus an upserted PR comment.

Two modes:

- **`simulate` (default)** — Tracely drives multi-turn [scenarios](../../../docs/pages/scenarios.mdx)
  against the HTTP endpoint you registered for the agent. **Needs no agent code in CI**, so it
  works for a TypeScript, Go or Ruby service exactly as it does for Python.
- **`gate`** — grade the ci-tagged traces your workflow already emitted against the agent's
  promoted regression suite (production failures turned into fail-to-pass cases).

## Quick start (simulate)

```yaml
- uses: your-org/tracely/.github/actions/tracely-gate@main
  with:
    api: ${{ secrets.TRACELY_API }}
    key: ${{ secrets.TRACELY_KEY }}
    min-pass-rate: "0.9"   # adversarial suites land some probes by design
```

Register the endpoint first (Scenarios → Agent endpoint), and forward the `traceparent` header
through your tracer so the gate can see your agent's tool calls, not just its replies.

### Which agents get gated

A scenario belongs to one agent, so `agent` chooses the suites to run:

| You want | Pass |
|---|---|
| One agent | `agent: support-agent` |
| A subset | `agent: support-agent,planner` |
| Everything | **omit `agent`** |

Omitting it gates every agent with at least one *enabled* scenario — derived from the scenario
list, so switching a suite off removes it from CI and a new agent joins the day someone writes its
first scenario, with no workflow edit. Each agent runs as its own gate but they share one commit
status and one PR comment (GitHub keys both, so separate posts would overwrite each other). **One
red agent fails the job**, and `timeout` budgets the whole step rather than each agent.

An agent with enabled scenarios but no registered endpoint reports `NO_COVERAGE` and blocks —
never a green pass for a suite that never ran.

## Inputs

| input | required | default | description |
|---|---|---|---|
| `mode` | | `simulate` | `simulate` (drive scenarios against the endpoint) or `gate` (grade pre-emitted traces) |
| `agent` | for `gate` | `""` | agent slug, or several comma-separated. Blank + `simulate` = every agent with an enabled scenario |
| `min-pass-rate` | | server setting (`1.0`) | simulate only. Fraction of conversations that must PASS |
| `timeout` | | `900` | simulate only. Seconds to wait; timing out fails the job, never passes it |
| `api` | ✅ | — | Tracely API base URL |
| `key` | ✅ | — | Tracely API / ingest key (use a secret) |
| `web-url` | | `""` | Tracely web base URL, for the "view gate run" link |
| `env` | | `ci` | the `tracely.env` tag your CI traces were emitted with |
| `github-token` | | `${{ github.token }}` | token used to post the status + comment |
| `sdk-spec` | | `tracely-ai` | pip spec for the SDK/CLI |

### Regression suite: `tracely replay`

If you also keep a promoted regression suite, `replay` is the turnkey path for it — one CLI step
re-runs your agent on every promoted case's recorded input against recorded fixtures (no API keys,
no model spend) and gates the PR. It needs a Python-importable entrypoint, which is the tradeoff
`simulate` exists to avoid.

`tracely replay` fetches the promoted suite, re-runs your agent on each recorded input
(emitting ci traces), pairs each trace to its case, gates, and posts the check — in one step.

```yaml
name: Tracely regression gate
on: pull_request

permissions:
  contents: read
  statuses: write        # the blocking commit status
  pull-requests: write   # the results comment

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - env:
          TRACELY_API: ${{ secrets.TRACELY_API }}
          TRACELY_KEY: ${{ secrets.TRACELY_KEY }}
          TRACELY_WEB_URL: ${{ secrets.TRACELY_WEB_URL }}
        run: |
          pip install tracely-ai        # + your agent's deps
          # your agent as module:function, called with each case input:
          tracely replay planner --entrypoint my_agent:run
          # ...or a non-Python agent (gets the input in $TRACELY_INPUT):
          # tracely replay planner --cmd "node run-agent.js"
```

**Hermetic by default.** Replay is deterministic and offline: each promoted case ships the
tool/LLM calls recorded in production (in order, with their args and error status), and an agent
written with `tracely.call_tool(name, fn, args=...)` / `tracely.call_llm(model, fn)` serves those
instead of making the real call — so CI needs no model key, costs nothing, and never flakes.
Repeated calls get their own recorded outputs, and a call that errored in production replays as an
error (so the gate reproduces the failure). Pass `--live` to make real calls instead.

### DIY emit + gate action

If your CI already emits ci-tagged traces (your harness calls `tracely_sdk.init(env="ci")`),
use the composite action to gate them:

```yaml
      - name: Emit ci traces
        run: python ci/run_agent.py      # your harness
      - uses: your-org/tracely/.github/actions/tracely-gate@main
        with:
          mode: gate          # grade the traces the step above emitted
          agent: planner
          api: ${{ secrets.TRACELY_API }}
          key: ${{ secrets.TRACELY_KEY }}
          web-url: ${{ secrets.TRACELY_WEB_URL }}
```

Mark **`tracely/regression-gate`** a required status check (Settings → Branches) to actually
block merges on a red gate.

## Run it locally

All three commands run outside CI — they just skip the GitHub posting (or use `--dry-run` to preview it):

```bash
pip install ./sdk                                          # provides the `tracely` CLI
export TRACELY_API=http://localhost:8000
tracely simulate --all                    # or: --agent support-agent,planner
tracely replay planner --entrypoint my_agent:run
tracely gate planner --env ci
```

### Pairing candidates to *this* run (`mode=gate`)

The action exports `TRACELY_RUN_ID=<github.run_id>-<github.run_attempt>` to the gate step, and
the Tracely SDK stamps every span emitted under that variable with `tracely.replay.run_id`. The
gate then only accepts candidate traces carrying **this** run id: a trace from a previous attempt
with the identical input is reported as `INCOMPLETE — candidate trace was not produced by this
run`, not silently graded. For that to work, the step that runs *your* agent must export the same
value:

```yaml
- name: Run agent on the promoted inputs
  env:
    TRACELY_RUN_ID: ${{ github.run_id }}-${{ github.run_attempt }}
  run: python ci_run_agent.py
```

Without it the gate falls back to matching the latest `ci` trace by input digest and carries a
warning saying so — inspectable, but not verification of this execution. `tracely replay` needs
no setup: it mints a run id per invocation and stamps it itself.

### Make the check required

The action posts the commit status `tracely/regression-gate` and a PR comment with case-level
evidence (each case's checks, version and a link to its page). Neither blocks a merge on its own.
In the repository: **Settings → Branches → Branch protection rule → Require status checks to
pass → add `tracely/regression-gate`**. The comment repeats this reminder on every run because
Tracely cannot see branch protection: a green comment is not proof the check is required.
