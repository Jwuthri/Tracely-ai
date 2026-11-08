# Deploy Tracely to Coolify

[Coolify](https://coolify.io) is a self-hosted, Railway-style PaaS. There are two ways to run
Tracely on it:

| | **A. One card per component** (recommended) | **B. One compose bundle** |
|---|---|---|
| What you get | 7 resources, as on Railway: Postgres, ClickHouse, Redis, MinIO, backend, worker, frontend | 1 resource that holds all 7 containers |
| Per-component logs / metrics / CPU & RAM limits / redeploy | yes | only for the whole bundle |
| Database backups and a public port toggle | yes, on each database card | no |
| Push redeploys | only the apps whose files changed | everything |
| Setup | `setup.py`, one command | a few clicks |

Both need a Coolify server and the repo connected through a GitHub App
(Coolify → **Sources** → **+ Add** → GitHub App). Fork the repo first if you want to deploy your own
changes.

## A. One card per component — `setup.py`

1. In Coolify, go to **Settings → Advanced** and turn on **API access**. Then go to
   **Keys & Tokens → API tokens** and create a token with the **root** permission.
2. Point two DNS names at your Coolify server, one for the UI and one for the API.
3. From a checkout of this repo (standard-library Python 3.9+, nothing to install), run:

```bash
export COOLIFY_TOKEN=<token>
python3 deploy/coolify/setup.py \
  --coolify-url https://coolify.example.com \
  --app-domain tracely.example.com \
  --api-domain tracely-api.example.com
```

It creates the project `tracely` (change it with `--project`) and these resources:

| Resource | Type | Notes |
|---|---|---|
| `tracely-postgres` | Database | `pgvector/pgvector:pg17` |
| `tracely-clickhouse` | Database | `clickhouse/clickhouse-server:24.8` |
| `tracely-redis` | Database | Celery broker and result backend |
| `tracely-minio` | Service | `pgsty/minio`, the S3 blob store. MinIO no longer publishes images; this is the maintained drop-in build. |
| `tracely-backend` | App, `Dockerfile.backend` | `TRACELY_ROLE=api`. Runs the idempotent migrations and bucket setup, then the API. Public on `--api-domain`. |
| `tracely-worker` | App, `Dockerfile.backend` | `TRACELY_ROLE=worker`, `CELERY_BEAT=true` (exactly one beat). Private. |
| `tracely-frontend` | App, `frontend/Dockerfile.railway` | Public on `--app-domain`. Reaches the API over Coolify's private network. |

The script wires everything together:
- **Secrets:** it generates the database passwords, `SESSION_SECRET` and `SECRETS_ENCRYPTION_KEY`.
- **Settings:** it sets every environment variable, including the `NEXT_PUBLIC_*` values the frontend needs at build time.
- **Watch paths:** each app only redeploys when its own files change.
- **Deploy:** it starts the first deploy.

Re-running it is safe: existing resources are reused by name and their secrets are kept.

Useful flags:
- `--tls-upstream`: TLS ends before Coolify (a Cloudflare Tunnel or another proxy), so Coolify routes plain `http://` while the public URLs stay `https://`.
- `--project` and `--environment`: where the resources go.
- `--prefix`: resource names.
- `--server`, `--github-app`, `--repo` and `--branch`: chosen automatically when there's only one, or from this checkout's `origin` for the repo.
- `--no-deploy`: create and update everything without deploying.

After it finishes:
- **First account:** open the UI and create your workspace. The first account becomes the OWNER, and everyone after that joins by invite.
- **Ingest key:** it's under Settings → API keys.
- **Resources:** adjust CPU and RAM on each card under **Resource Limits**.
- **Backups:** schedule them on the Postgres card (Backups → an S3 destination).
- **Token:** revoke the API token if you created it just for this.

> Never regenerate `SECRETS_ENCRYPTION_KEY` (it's on the backend and worker cards). It decrypts
> the OpenRouter keys that workspaces save, so changing it orphans them.

## B. One compose bundle — `docker-compose.coolify.yml`

Project → **+ New** → *Git Repository (with GitHub App)* → this repo, then:

- Build pack: **Docker Compose**
- Docker Compose file: `/docker-compose.coolify.yml`. Type the path, because the picker only lists `docker-compose.yml`.
- Skip the `.env.example` import. It holds local dev values.

Coolify generates every secret on first save (the `SERVICE_PASSWORD_*` and `SERVICE_USER_*` variables).

Next, give exactly two services a domain:
- `frontend` on port **3000**
- `backend` on port **8000**

Use `http://` behind a TLS-terminating proxy, `https://` otherwise. If your hosts aren't
`tracely.scribely.xyz` and `tracely-api.scribely.xyz`, set these to their public `https://` origins:
- `APP_BASE_URL`: the UI
- `TRACELY_PUBLIC_API`: the API

Then deploy. On the first deploy, `migrate` runs once and exits, and `backend` and `worker` start
after it succeeds.

## Pointing the SDK at it

```python
tracely.init(endpoint="https://tracely-api.example.com", api_key="<ingest-key>")
```
