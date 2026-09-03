# `scripts/` — dev & demo helpers

Small standalone scripts for exercising a running stack. Both read `TRACELY_API` (default `http://localhost:8000`) and `TRACELY_KEY` (default `tracely_dev_key`).

## `send_test_trace.py` — raw-OTLP sample sender

Posts a hand-built multi-span OTLP trace (**agent → llm → tool**) straight at `/v1/traces` — no SDK involved. It's the quickest way to prove the ingestion path end-to-end and to seed demo data with known failure shapes.

```bash
uv run python scripts/send_test_trace.py        # a failing run
make send-trace                                  # same, via the Makefile
```

Environment flags select the variant / environment (so you can seed a realistic mix):

| Var | Effect |
|---|---|
| _(none)_ | a failing run (tool error) |
| `FIXED=1` | the healthy/fixed version |
| `SILENT=1` | a silent failure (model "called" a tool that never ran → tool-consistency FAIL) |
| `HALLUCINATE=1` | an answer that contradicts the tool result (→ LLM-judge FAIL) |
| `RANDOM=1` | randomize ids so each run is a distinct trace |
| `ENV=ci` | emit with `tracely.env=ci` (for gating demos) |

```bash
# seed a spread of failures, then hit "Analyze failures" in the UI:
make demo-failures        # loops send_test_trace.py with RANDOM/SILENT/HALLUCINATE
```

For richer, agent-shaped demo data (multi-turn, multi-agent, thinking, multimodal, structured output) use the SDK seeder instead: [`sdk/examples/seed_conversations.py`](../sdk/examples/seed_conversations.py).

## `demo_fleet.py` — one cinematic conversation for the Fleet view

Sends a 2-turn, 3-agent conversation **through the SDK** (not raw OTLP) using every span type
the `/fleet` office renders: `thinking` (thought cloud), `skill` (read at the library),
`delegate` (walk to the callee's desk + speech bubble), action tools at the tool wall, a
knowledge tool at the library, and one failing tool (the red `!`). Prints the thread id — open
`/sessions/<thread>/fleet`.

```bash
uv run python scripts/demo_fleet.py
```

## `seed_demo.py` — the whole-product seeder (one command)

Populates **every** surface a visitor sees, in dependency order, so the differentiated Test/Ship half is never left empty:

1. **Observe / Triage** — rich conversations (`seed_conversations.py`): every trace shape + the failures (tool error, hallucination, silent requested-but-not-executed tool, guardrail block).
2. **Triage** — cluster those failures into issues (`POST /api/clusters/rebuild`).
3. **Test / Ship** — promote failing traces into regression cases + run red→green CI gates (`seed_regression.py`) — the differentiated half competitors don't have.

```bash
make demo                                              # local dev (backend + worker already up)
docker compose --profile demo up -d --build --wait     # Docker: the `demo` compose profile runs exactly this
docker compose exec backend python scripts/seed_demo.py   # seed an already-running stack
```

Idempotent — each phase is skipped when its data already exists (promote dedupes by input digest; deterministic trace ids replace in place), so it's safe to run on every `docker compose up`. Pass `--force` to re-run anyway. This is the script behind [`DEMO.md`](../guides/DEMO.md).

## `tracely_gate.py` — deprecated shim

The CI gate now lives in the SDK as the `tracely` CLI. This file just forwards `python scripts/tracely_gate.py <agent>` → `tracely gate --agent <agent>` so old callers keep working.

```bash
pip install ./sdk          # provides the `tracely` CLI
tracely gate <agent>       # new, canonical usage  (see sdk/README.md + .github/actions/tracely-gate)
```

## Why these exist

- **A raw protobuf sender** (no SDK) proves the OTLP endpoint accepts standard OpenTelemetry traffic from anything, and gives deterministic, labelled failure shapes for the clustering/eval demos.
- **The gate moved to the SDK** so it ships as an installable, GitHub-aware CLI usable from any CI; the shim is kept only for backward compatibility.
