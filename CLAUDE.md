# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Tracely is trace-native CI/CD for AI agents: **production trace → failure detection → regression test → CI/CD gate**. The trace is the source of truth; evals, clusters, cases, gates and trends are all derived from it. There are no hand-authored datasets.

A `uv` workspace (`backend`, `workers`, `sdk`) plus a pnpm Next.js app (`frontend`) and a Nextra docs site (`docs`).

## Commands

```bash
make install                     # uv sync --all-packages --all-extras + pnpm install
make infra-up                    # clickhouse, postgres, redis, minio (docker)
make migrate                     # ClickHouse DDL runner + Alembic (Postgres)
make seed                        # default project + ingest key `tracely_dev_key`
make backend / workers / frontend    # three terminals: FastAPI :8000 · Celery · next dev
make demo                        # populate the whole product (traces, clusters, cases, gates)
```

Tests — no infra required, ~6s:

```bash
uv run pytest -q backend/tests sdk/tests        # what CI runs
uv run pytest -q backend/tests/test_gate_eval.py::test_name -x     # single test
uv run ruff check . && uv run ruff format .
cd frontend && pnpm test        # vitest; pnpm test:watch; pnpm build type-checks (tsc) + lints
```

Alembic: `cd backend && uv run alembic revision -m "…"` / `alembic upgrade head`. ClickHouse migrations are `*.up.sql` files in `backend/tracely/infrastructure/clickhouse/ddl/` applied by `python -m tracely.infrastructure.clickhouse.migrations`.

Whole stack in Docker: `docker compose up -d --build --wait` → UI on **:3001**, backend on **:8000** (remap with `TRACELY_WEB_PORT` / `TRACELY_BACKEND_PORT`). `make frontend` runs plain `next dev` (:3000) — use `cd frontend && pnpm dev -p 3001` to match Docker.

## Architecture

Write path (deliberately mirrors Langfuse, reimplemented in Python):

```
SDK/OTLP → POST /v1/traces → S3 blob (durable FIRST) → Redis/Celery
  → worker: otel/ mapping → registry upsert → ClickHouse events
  → evaluate_run_task (countdown=4, debounces late spans) → scores + structural clustering
```

Five stores: **ClickHouse** (`events` one row per span + `scores`, `ReplacingMergeTree`) · **Postgres + pgvector** (registry: projects, keys, agents, cases, gates, clusters, evaluators, users, monitors, annotations) · **S3/MinIO** (raw OTLP body = source of truth, plus regression fixture bundles) · **Redis** (Celery) · **pgvector** (failure embeddings).

`backend/tracely/` is one package with three roles (FastAPI app, shared domain, Celery tasks) and strict layering:

| Layer | Rule |
|---|---|
| `domain/` | Pure logic, **no I/O**. Stats, verdict policy, contracts, trajectories, template resolution. |
| `infrastructure/` | Every adapter: `clickhouse/`, `db/`, `blob/`, `queue/`, `llm/`, `notifications/`, `registry/`. |
| `services/` | Use-case orchestrator classes (`IngestionService`, `EvaluationService`, `GateService`, …). |
| `api/`, `workers/` | Thin. Routers shape HTTP; tasks dispatch into a service. `api/mcp_server.py` mounts an MCP server at `/mcp` whose tools call those routers in-process over an ASGI transport (forwarding the caller's key) — never the DB. |

`workers/` is a deployable shim that imports `tracely.workers.tasks`. `sdk/` is the instrumentation SDK **and** the `tracely simulate` / `gate` / `replay` CI CLI. `simulate` is the scenario path (one agent, `--agent a,b`, or `--all` = every agent with an enabled scenario); the fan-out is CLI-side — one `GateRun` per agent, aggregated into one commit status and one PR comment.

## Hard rules

- **No SQL in `api/routers/`.** ClickHouse reads go through `infrastructure/clickhouse/async_reader.py` (async, for the API) or `trace_reader.py` (sync, for workers/services); Postgres through `infrastructure/db/repositories.py`; auth lookups through `auth/queries.py`. Adding a query means adding a function there, not inlining SQL in a router. (`health.py`'s `SELECT 1` probe is the sole exception.)
- **Every LLM call goes through `infrastructure/llm/provider.py`** (`run_structured_agent` / `run_text_agent` — LangChain `create_agent` on OpenRouter, OpenAI as fallback). Never construct a client elsewhere. LLM modules are lazy-imported and `llm_enabled()` gates them: with no key the pipeline must still run (judge/FI/meta-analysis degrade, not crash). **Customers bring their own key**: inside `provider.use_project_key(project_id)` only that workspace's OpenRouter key applies — never the server-wide `OPENROUTER_API_KEY`/`LLM_JUDGE_API_KEY`/`OPENAI_API_KEY`. A project with no key configured is exactly an LLM-disabled deployment. Any new project-scoped LLM entry point wraps in `use_project_key` and checks `llm_enabled()` *inside* the wrap. The **one exception** is the in-app assistant (`services/assistant_service.py`): it explains and operates *our* product, so the model runs on *our* key via `provider.use_server_key()` — an explicit scope, not an unwrapped call, because under `REQUIRE_PROJECT_LLM_KEY` an unscoped call fails closed as a forgot-to-wrap bug. Anything else that spends the server key on a customer's behalf needs the same deliberate seam. Note the split: only the *model* is on our key. The assistant's **tools** re-enter our own routers carrying the caller's own credentials (`api/internal_client.py`), so we pay for tokens without widening anyone's reach.
- **Agents reach the product through the routers, never the DB.** Both the MCP server (`api/mcp_server.py`) and the in-app assistant's tools (`services/assistant_tools.py`) call `internal_client.api_call`, which re-issues the request against our own ASGI app with the caller's `authorization`/`x-tracely-key`/`x-tracely-project` headers. That leaves `get_project_id` as the only place scoping happens and the routers' own 4xx bodies as the only place a bad write is rejected — handed back to the agent, that error text is what lets it correct itself. Assistant tools **return** their failures as text (langgraph re-raises anything that isn't a bad-args error, which would kill the stream mid-answer), and no assistant tool argument may be named `config` (`StructuredTool._arun` swallows it).
- **Everything is scoped by `project_id`**, resolved from `Authorization: Bearer <ingest-key>` via `api/auth`. Every read and write takes it.
- **An ingest key ingests and reads; it never destroys or sets secrets.** Routes that wipe data, delete what people built, set the OpenRouter key or re-point an agent endpoint carry `dependencies=[Depends(require_user)]` (`api/auth.py`) — a machine principal has no role and is refused. Dev mode (no human auth, prod refuses to boot in it) is the one place an ingest key acts as OWNER. A leaked CI key must cost traces, not the workspace.
- **Every customer-supplied URL Tracely will call goes through `infrastructure/net.assert_public_url`** — at save time *and* right before the request. The worker shares a network with ClickHouse's HTTP port (which runs SQL from a POST body), so an unchecked agent endpoint or monitor webhook is a cross-tenant read. Off outside prod (self-hosters run agents on localhost) unless `ALLOW_PRIVATE_URLS` says otherwise.
- **Ending a session means bumping `users.token_version`.** Sessions are stateless JWTs carrying it as `tv`; `_resolve_local_jwt` compares the two. Password change and reset bump it (and hand back a fresh token, or the caller logs themselves out). Nothing else can revoke a session early.
- **An invite never logs anyone into an existing account.** The raw invite token is shown to the inviter; when the invited email already has an account, `accept_invitation` verifies that account's password before consuming the token.
- **Writes are idempotent.** Deterministic ids (score id = `uuid5(trace_id:name:span_id)`) + `ReplacingMergeTree` mean re-ingest/re-eval converges instead of duplicating. Anything sampled must be deterministic per `(trace_id, score_name)` for the same reason (`domain/evaluation/targeting.py`).
- **One verdict policy.** A trace/turn/session fails iff it has a `FAIL` on a **non-advisory** evaluator. Python: `domain/evaluation/verdict.py`. Its SQL twin lives in `async_reader` (`name NOT IN {adv:Array(String)}`), fed by `api/advisory.py`. Change both together or the badge, threads dot, and trends disagree.
- **Frontend fetch pattern:** Server Components (pages) call `app/lib/api.ts` directly; Client Components fetch a thin proxy under `app/api/*` which re-issues with the Bearer key server-side. `TRACELY_KEY` / `TRACELY_API` must never reach the browser — do not add an `env:` block to `next.config.mjs`.
- **New backend env var?** Add it to `config.py` (`Settings`) *and* the `x-app-env` anchor at the top of `docker-compose.yml` — `.env` alone won't reach the containers.
- **Emulated conversations have four non-obvious invariants.** Break any one and the gate goes quietly green instead of failing loudly:
  1. **Span ids are base64, never hex.** `parse_otlp_traces_json` runs protobuf's `json_format.Parse`, whose canonical mapping decodes `bytes` as base64 — hex does *not* raise, it silently yields a 24-byte id nothing can look up (`domain/simulation/emit.py`).
  2. **Turns are ingested and evaluated inline, not via Celery.** The gate task already owns the worker's only slot under `--pool=solo --concurrency=1`; enqueuing work and waiting for it deadlocks.
  3. **Driving and grading are separate tasks** (`run_scenario_gate` → countdown → `grade_scenario_gate`). The *customer's* spans arrive as ordinary OTLP, so their ingest is queued behind the gate — releasing the worker between phases is the only thing that lets them land before grading.
  4. **The attack judge is inverted.** For an `ADVERSARIAL` scenario, goal *achieved* = attack succeeded = **FAIL**. Without it the goal only generates turns and nothing judges the outcome, so a fully-successful attack passes.
- **Internal runs must never be evaluated.** Tracely records its own work — an evaluation, a scenario run — as a trace (`domain/introspection.py`), listed in the Traces tab behind the **Evals** filter chip. Every span carries `internal_kind` (`eval`|`sim`|`assistant` — the in-app agent records each of its turns, tools and all), and **three** things depend on it: the ingest hop skips scheduling evaluation for those trace ids, `EvaluationService.evaluate_trace` refuses them outright (monitors and manual re-runs don't go through ingest), and `_REAL` in `async_reader` keeps them out of every list, count and metric (the sole opt-in is `sessions_overview(include_internal=True)`, which the toggle asks for). Lose the first two and grading an eval run records another eval run, for ever; lose the third and a project's trace count doubles the day evaluators are switched on.
- **`SKIP` scores are dropped before the conversation roll-up.** `rollup_verdict` reads any score as evidence a run was graded, so a conversation whose only scores were skipped would report PASS having checked nothing (`domain/simulation/aggregate.py`).

## Gotchas

- The Celery **worker does not hot-reload**. After touching worker/eval/failure-intel/otel-mapping code: `docker compose restart worker` (backend/frontend are volume-mounted and do reload).
- Celery runs `--pool=solo --concurrency=1` locally on purpose — numba/UMAP/HDBSCAN in failure intelligence are fork-fragile.
- `AUTH_MODE` is `dev` (open) | `local` (email/password + JWT, needs `SESSION_SECRET`) | `clerk`. The frontend's `NEXT_PUBLIC_AUTH_MODE` must match the backend's. Prod refuses to boot with `AUTH_MODE=dev` or a seeded `tracely_dev_key`.
- OTLP arrives in three message conventions (structured / OpenInference-flattened / OpenLLMetry legacy); `otel/messages.py` normalizes them. Fix I/O rendering bugs there, not in the frontend renderers.
- `uv sync --frozen` in CI catches lock/extra drift — regenerate `uv.lock` when touching deps.

## Where the detail lives

Each folder has a thorough README; read it before changing that area rather than re-deriving from code.

| Path | Contents |
|---|---|
| `backend/README.md` | Full flow walkthroughs (ingest, eval, failure intel, regression, gate, auth, rolling summary, meta-analysis), module map, API surface, schema/migration list. |
| `frontend/README.md` | Data-flow rule, app shell, per-route fetch table, theme tokens. |
| `sdk/README.md` | Auto + manual instrumentation, the `tracely.*` attributes the backend indexes, hermetic replay seam, CLI. |
| `README.md`, `guides/OVERVIEW.md`, `guides/DEMO.md`, `guides/DEPLOY.md` | Quickstart / guided tour / 2-min demo / Railway runbook. |
| `design/` | The design dossier — the "why" behind every decision above. |
| `docs/` | The published SDK docs site (Nextra, `make docs` → :3002). |
