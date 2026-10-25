# Deploy Tracely to Coolify

The whole stack — Postgres (pgvector), ClickHouse, Redis, MinIO, the API, the Celery worker and
the Next.js UI — as **one** Coolify resource, from [`docker-compose.coolify.yml`](../../docker-compose.coolify.yml)
at the repo root.

## 1. Create the resource

Project → **+ New** → *Git Repository (with GitHub App)* → this repo, then:

- Build pack: **Docker Compose**
- Docker Compose location: `/docker-compose.coolify.yml`

Coolify generates every secret on first save (`SERVICE_PASSWORD_*` / `SERVICE_USER_*`):
`SESSION_SECRET`, `SECRETS_ENCRYPTION_KEY`, and the Postgres / ClickHouse / MinIO passwords.
Don't regenerate `SERVICE_PASSWORD_64_SECRETS` later — it decrypts the OpenRouter keys
workspaces have saved.

## 2. Domains

Give exactly two services a domain (the rest stay private on the compose network). Behind a
Cloudflare Tunnel use `http://` — Cloudflare terminates TLS:

| Service | Domain |
|---|---|
| `frontend` | `http://tracely.example.com:3000` |
| `backend` | `http://tracely-api.example.com:8000` |

Then set these environment variables to the matching **https** origins and deploy:

| Variable | Value |
|---|---|
| `APP_BASE_URL` | `https://tracely.example.com` (invite/alert links, CORS) |
| `TRACELY_PUBLIC_API` | `https://tracely-api.example.com` (shown in Settings → API keys; baked in at build) |

Optional: `OPENROUTER_API_KEY`, `RESEND_API_KEY` / `EMAIL_FROM`, `OPS_ALERT_WEBHOOK`,
`SENTRY_DSN`, `CELERY_POOL=prefork` + `CELERY_CONCURRENCY=4` once ingest volume grows.

## 3. First deploy

`migrate` runs ClickHouse DDL → alembic → checkpointer → seeding, then exits; `backend` and
`worker` start only after it succeeds. It runs as `TRACELY_ENV=prod` with `AUTH_MODE=local`,
so no dev ingest key is seeded and registration is first-user-only: open the frontend, create
your workspace (you're the OWNER), and find your ingest key under Settings → API keys.

Point the SDK at the backend domain:

```python
tracely.init(endpoint="https://tracely-api.example.com", api_key="<ingest-key>")
```
