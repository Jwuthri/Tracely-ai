# Deploying Tracely to production

The local docker stack is the dev environment — it boots with `AUTH_MODE=dev` and seeds a published
`tracely_dev_key`. **Neither of those is safe in prod.** This document is the runbook for a real
deploy: required env vars, the guards that fail-fast if you miss one, the worker pool, backups,
and the post-deploy verification.

We target Railway (Postgres + ClickHouse + Redis + MinIO managed there), but the same checklist
works on any host running the same two containers.

> **Setting up on Railway from zero?** One click provisions all seven services pre-wired:
>
> [![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/n5n_LE?referralCode=WCq5Cn&utm_medium=integration&utm_source=template&utm_campaign=generic)
>
> To wire it by hand instead, follow [`deploy/railway/README.md`](../deploy/railway/README.md) —
> every env var is pre-written in
> [`.env.railway.example`](../deploy/railway/.env.railway.example). Either way, come back here to
> harden it for production.

> **Moving the site to a new domain?** The `tracely-studio.xyz` → `tracely-ai.com` move is
> written up as a reusable runbook in [`guides/DOMAIN_MIGRATION.md`](DOMAIN_MIGRATION.md) —
> hostname map, the 308 redirect that keeps the old backlinks alive, and the env vars to flip.

---

## 1. The prod refuse-to-boot guards

The backend fails fast at startup in these cases. **Don't bypass — fix them.**

| Guard | Where | Why |
| --- | --- | --- |
| `TRACELY_ENV=prod` + `AUTH_MODE=dev` → `ValueError` at config load | `tracely/config.py:_validate_auth` | Dev mode has no human auth; the ingest key is the only credential. Booting prod in dev mode is world-pwnable. |
| `TRACELY_ENV=prod` + `tracely_dev_key` still in `ingest_keys` → `RuntimeError` in `lifespan` | `api/main.py:_refuse_dev_key_in_prod` | The dev key is published in the docs. If the prod DB still has it (e.g. migrated from a dev snapshot), shut down before serving the first request. |
| `AUTH_MODE=local` + `SESSION_SECRET` shorter than 32 chars → `ValueError` | `tracely/config.py:_validate_auth` | Weak HS256 keys are forgeable. |
| `AUTH_MODE=clerk` + no `CLERK_ISSUER` → `ValueError` | same | Clerk verification needs the issuer to fetch JWKS. |

If `tracely_dev_key` is in your prod DB (a fresh prod deploy never seeds it — see
`seeding_service.py`), delete it before you boot:

```sql
DELETE FROM ingest_keys WHERE key = 'tracely_dev_key';
```

---

## 2. Required env vars

```dotenv
# Identity
TRACELY_ENV=prod                     # flips on the guards + tightens CORS + skips dev-key seeding

# Auth — pick ONE
AUTH_MODE=local                      # email/password owned by this backend
SESSION_SECRET=...                   # `openssl rand -hex 32` (>=32 chars required)
# …or
AUTH_MODE=clerk
CLERK_ISSUER=https://<slug>.clerk.accounts.dev
CLERK_AUDIENCE=...                   # optional; pins the JWT 'aud'

# Postgres + ClickHouse + Redis (Railway-injected URLs are fine)
DATABASE_URL=postgresql+asyncpg://...
ALEMBIC_DATABASE_URL=postgresql+psycopg://...
CLICKHOUSE_HOST=...
CLICKHOUSE_USER=...
CLICKHOUSE_PASSWORD=...
REDIS_URL=redis://...

# Object storage (event blobs)
S3_ENDPOINT_URL=...
S3_BUCKET=tracely-events
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...

# LLM (judge, failure-intel agents, rolling summary, meta-analysis).
# NOT used for customer workspaces — every project runs on its OWN OpenRouter key (Settings ->
# OpenRouter key) and a project without one simply gets no LLM features. These server-wide keys
# only apply outside a project scope (CLI scripts, local single-tenant use).
OPENROUTER_API_KEY=...
OPENAI_API_KEY=...                   # embeddings only

# REQUIRED in any multi-tenant deployment: encrypts each workspace's own OpenRouter key at rest
# (Settings -> OpenRouter key). `openssl rand -hex 32` (>=32 chars).
# Set the SAME value on the backend AND the worker: the API encrypts, the worker (which runs the
# on-ingest judge) decrypts. A mismatch means every stored key silently fails to decrypt, and
# since there is no fallback to OPENROUTER_API_KEY, every project's evals stop running.
# Unset, the feature refuses to store a key (HTTP 500 with a clear message) rather than
# persisting plaintext — which also means no workspace can enable LLM evaluation.
# Rotating it orphans already-stored workspace keys — each workspace must re-enter theirs.
SECRETS_ENCRYPTION_KEY=...

# Hosted frontend origin (CORS allow-list — wildcard localhost is OFF in prod)
FRONTEND_ORIGIN=https://app.your-domain.com
# Set on BOTH backend and worker: the backend builds invite links from it, and the worker builds
# monitor links (services/monitoring_service.py). Worker-only omission = alerts linking to localhost.
APP_BASE_URL=https://app.your-domain.com

# Transactional email (Resend) — BACKEND SERVICE ONLY; the frontend never sends mail, it proxies to
# the backend. WITHOUT RESEND_API_KEY invites are created but never emailed and the UI silently
# falls back to "share this link". EMAIL_FROM must be on a domain verified at resend.com/domains,
# otherwise every send is rejected.
RESEND_API_KEY=re_...
EMAIL_FROM=Tracely <invites@your-domain.com>

# Worker pool (real concurrency — see §3)
CELERY_POOL=prefork
CELERY_CONCURRENCY=4

# Optional: Sentry (no-op when DSN is unset; install sentry-sdk in the prod image to activate)
SENTRY_DSN=...
SENTRY_ENVIRONMENT=prod
```

---

## 2b. Hosted-cloud billing (optional — skip entirely when self-hosting)

Off by default; nothing below applies until you set `BILLING_ENABLED=true` on **both** the
backend and the worker (the quota is *counted* in the worker and *enforced* in the API — a
backend-only flag reads counters nobody writes, and the limit silently never fires).

```dotenv
BILLING_ENABLED=true
ALLOW_PUBLIC_SIGNUP=true           # multi-tenant signup; see "Accounts" below
REQUIRE_PROJECT_LLM_KEY=true       # server LLM keys never serve customer work — see below
ALLOW_PRIVATE_URLS=false           # agent endpoints / webhooks may not hit private IPs (prod default)
FREE_TRACE_LIMIT=20000             # per organization, per UTC month
PRO_TRACE_LIMIT=1000000
FREE_WORKSPACE_LIMIT=3             # per company org (personal accounts are always 1)
PRO_WORKSPACE_LIMIT=10
FREE_SEAT_LIMIT=3                  # members + pending invites
PRO_SEAT_LIMIT=10
MAX_ORGANIZATIONS_PER_USER=1       # see below
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...    # REQUIRED with the secret key — boot refuses without it
STRIPE_PRICE_PRO=price_...         # the Pro plan's monthly price id
```

**Accounts (the tenancy model).** Every user belongs to an **organization**, and a workspace is
reachable exactly when you're a member of the org that owns it — there is no way to be invited
into a single workspace, which is what keeps tenants apart:

| | Workspaces | Seats | Joinable |
| --- | --- | --- | --- |
| Personal (every signup) | 1 | 1 | never |
| Company, Free | `FREE_WORKSPACE_LIMIT` | `FREE_SEAT_LIMIT` | by invite |
| Company, Pro | `PRO_WORKSPACE_LIMIT` | `PRO_SEAT_LIMIT` | by invite |
| `unlimited` (operator) | ∞ | ∞ | by invite |

A solo user turns into a team by creating an organization (account menu → New organization) —
but **`MAX_ORGANIZATIONS_PER_USER` caps how many company orgs one user may BELONG to**, and
that cap is what makes the whole tier real: an organization is a quota pool, so uncapped org
creation would just be uncapped free quota one level up. It counts membership, not ownership —
being invited into a company uses up your one, so nobody runs a private org on the side of the
team they're in. **No plan lifts it**: the plan belongs to the org and buys workspaces, seats
and quota inside it, so a bigger account is a bigger org, never a second one.
`ALLOW_PUBLIC_SIGNUP=false` (the self-host default) keeps registration first-user-only, with
everyone else arriving by invite; set it to `true` for hosted cloud, where each signup gets its
own personal organization. Caps are only enforced when `BILLING_ENABLED=true`, so a self-hosted
deployment is never limited.

**What counts as a trace:** externally-POSTed OTLP traces, once per (project, month) however
many batches carry their spans. Tracely's own recordings (evaluations, scenario drives) never
count. Over the cap, `/v1/traces` answers **429 + Retry-After** until the month rolls over or
the plan changes; enforcement is fail-open (a Redis/Postgres outage admits traces, never drops
them). Redis sizing: one set per project-month of trace ids (≈ tens of MB per million traces) —
it shares the Celery broker, which runs `noeviction`, so give it headroom.

**The quota belongs to the organization, not the workspace.** Traces are counted per project
(exactly once each) but compared against the org's cap after summing across all of its
workspaces — so adding workspaces never adds quota, and a team on one Pro subscription can split
work across workspaces without paying per workspace. Projects with no organization (CLI-seeded,
dev mode) are counted on their own. Migration 0023 backfills organizations from existing
memberships, one per owner, preserving exactly the access people already had.

**Stripe setup (dashboard):** create the Pro product + monthly price → `STRIPE_PRICE_PRO`; add a
webhook endpoint at `https://<api-domain>/api/billing/webhook` with events
`checkout.session.completed`, `customer.subscription.updated`,
`customer.subscription.deleted` → `STRIPE_WEBHOOK_SECRET`. The webhook is signature-verified;
plan state only ever changes through it.

**Operator accounts:** the free cap applies to every organization the moment billing is enabled —
mark your own first:

```sql
UPDATE organizations SET plan='unlimited' WHERE slug IN ('default', 'demo');
```

`unlimited` is never touched by webhooks and is only settable via SQL. (Before 0023 this was a
column on `projects`; that column still exists but nothing reads it.)

**`REQUIRE_PROJECT_LLM_KEY`** is the hosted hard gate for AI features: with it on, the
server-wide `OPENROUTER_API_KEY`/`LLM_JUDGE_API_KEY`/`OPENAI_API_KEY` apply to *nothing* —
every judge, clustering run, meta-analysis, rolling summary and scenario gate runs exclusively
on the workspace's own OpenRouter key (Settings → OpenRouter key), and a workspace without one
gets structural checks only. Any future code path that forgets the per-project scoping fails
closed instead of billing you.

---

## 3. Celery worker pool

Local dev uses `--pool=solo --concurrency=1` because the failure-intelligence stack
(numba / UMAP / HDBSCAN) was historically fork-fragile. Prod sets:

```dotenv
CELERY_POOL=prefork
CELERY_CONCURRENCY=4   # tune to vCPU; the docker-compose worker reads both vars
```

If a numba/UMAP fork bug resurfaces under prefork, switch to `CELERY_POOL=threads` (same env var,
the rebuild-clusters task is mostly NumPy + I/O so threads are fine).

Run **at least two worker replicas** so a single hang doesn't drop ingestion. `task_acks_late=True`
+ `visibility_timeout=3h` mean an unacked task is redelivered after 3 hours — enough headroom for
the slowest cluster-rebuild without double-running fast ingest tasks.

### Beat: exactly one, and not zero

Monitors and the ops self-check are **beat-driven** — with no scheduler running they never fire,
silently (this is how it shipped locally for a while: the compose worker had no `--beat`, so the
monitoring engine had never once run). Local docker embeds it in the single worker. In prod run
**one** dedicated scheduler, never `--beat` on every replica or each one re-fires the schedule:

```bash
celery -A tracely_workers.worker beat --loglevel=info    # its own 1-replica service
```

Today's Railway deployment takes the shortcut instead: the worker is a single replica, so its
start command carries `--beat` and that one process *is* the scheduler.

> **The trap that comes with it:** the moment you scale the worker past one replica, every
> replica re-fires the whole schedule — including `enforce_retention`, which deletes traces.
> Scaling up means splitting beat into its own 1-replica service first (the command above) and
> dropping `--beat` from the worker.

Confirm it took: `/health/queue` reports `beat_age_s` — a null or a growing number means nothing
is scheduling. It read `null` in production until 2026-09-06, so `evaluate_monitors`,
`selfcheck` and `prune_chats` had never run there.

---

## 4. Backups (the only P0 we can't automate)

There is no automatic backup of Tracely's own data. Enable them in your provider's UI.

### Railway

- **Postgres** → service → *Backups* → toggle daily snapshots; pick a retention window (7-30 days).
- **ClickHouse** → service → *Backups* → same. ClickHouse snapshots include `events` and `scores`;
  point-in-time recovery isn't available, so daily is the granularity.
- Redis is **not** backup-critical (it holds the Celery queue; a queue replay = retry, not data loss).
- MinIO/S3 → enable versioning on the `tracely-events` bucket; OTLP blobs are immutable, so the
  cost is just one extra version per write.

### Restore test (do this at least once)

1. Take a fresh snapshot, then restore it into a `tracely-restore` service.
2. Point a throwaway backend at the restored URLs (`DATABASE_URL`, `CLICKHOUSE_HOST`).
3. Boot — the refuse-to-boot guard catches a forgotten dev key here. Open `/traces`.
4. Tear down. You now know your RTO.

---

## 5. Health probe

`GET /health` returns **200** only when ClickHouse and Postgres both answer; otherwise **503** with
a per-dep status payload. Wire your platform's liveness/readiness probes to it:

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
  interval: 10s
  timeout: 3s
  retries: 6
  start_period: 30s
```

A 503 means one of your dependencies is down; check the JSON body for which.

---

## 5b. The deployment watching itself

`/health` answers "can the API serve reads". It says nothing about the failure mode that actually
bites: **the worker dies and every client keeps getting 202s** while nothing lands in ClickHouse.
Traffic looks perfect, data stops.

`GET /health/queue` reports that shape — always **200**, because a backlog must make a human look,
not make your orchestrator kill the API:

```json
{"degraded": true,
 "problems": ["1,204 tasks queued and nothing has finished in 22m — the worker looks stuck"],
 "queue_depth": 1200, "unacked": 4, "last_task_age_s": 1337, "last_trace_age_s": 4210, "beat_age_s": 12}
```

`unacked` matters as much as `queue_depth`: prefetched tasks leave the Redis list, so a buried
worker shows `queue_depth: 0` — the depth is the **sum**.

The same check runs on beat every 5 minutes. It logs a structured `selfcheck` line every tick (a
heartbeat for log-based alerting) and, when `OPS_ALERT_WEBHOOK` is set, pushes to Slack or any
webhook — at most **once an hour** while degraded, because an alert that fires every 5 minutes gets
muted and then it may as well not exist.

```dotenv
OPS_ALERT_WEBHOOK=https://hooks.slack.com/services/…   # blank = log only
```

What it pages on (thresholds in `backend/tracely/domain/ops/selfcheck.py`, all deliberately
generous — a page nobody trusts is worse than no page):

| Signal | Meaning |
|---|---|
| deep queue **and** no task finished in 15m | worker stuck — the classic |
| deep queue, worker still finishing | draining but deep; capacity warning, not an outage |
| spans accepted but nothing stored in 1h | ingest path broken behind a green API |
| `beat_age_s` > 3h | scheduler dead — monitors and this check itself stopped |

**Sentry** (optional, `SENTRY_DSN`) now initializes in the worker as well as the API — set it on
both services. Exceptions inside tasks used to vanish into worker logs, which is exactly where
evaluation, clustering and gate runs break.

---

## 6. CORS

In prod `FRONTEND_ORIGIN` is the only allow-listed origin. Localhost is **not** allowed (controlled
by `settings.is_prod` in `api/main.py`). If you serve the frontend from multiple hosts (canary,
staging) set `FRONTEND_ORIGIN` to the user-facing one and use a same-origin proxy for the rest.

---

## 7. Post-deploy verification

After the deploy, run this from a workstation:

```bash
HOST=https://api.your-domain.com

# 1. /health should be 200 with both deps OK
curl -s "$HOST/health" | jq

# 2. The dev key MUST be invalid in prod
curl -s -w "\nHTTP %{http_code}\n" -H "Authorization: Bearer tracely_dev_key" "$HOST/api/sessions?limit=1"
# expect:  HTTP 401   (any 2xx response = you forgot the guard)

# 3. CORS must NOT allow localhost
curl -s -o /dev/null -w "%{http_code}\n" -H "Origin: http://localhost:3001" -H "Access-Control-Request-Method: GET" -X OPTIONS "$HOST/api/sessions"
# expect:  the response lacks Access-Control-Allow-Origin for localhost (compare against FRONTEND_ORIGIN)
```

If any of those three fail, **roll back** — don't try to patch a serving instance.
