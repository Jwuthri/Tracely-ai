#!/usr/bin/env python3
"""Deploy Tracely to Coolify as one resource per component — the Railway layout, self-hosted.

Creates (or reuses, by name) seven resources in one Coolify project environment, each with its
own logs, metrics, CPU/RAM limits and redeploy button:

    <prefix>-postgres    database   pgvector/pgvector:pg17
    <prefix>-clickhouse  database   clickhouse/clickhouse-server:24.8
    <prefix>-redis       database   redis
    <prefix>-minio       service    pgsty/minio (S3 blob store)
    <prefix>-backend     app        Dockerfile.backend, TRACELY_ROLE=api     → --api-domain
    <prefix>-worker      app        Dockerfile.backend, TRACELY_ROLE=worker  (private)
    <prefix>-frontend    app        frontend/Dockerfile.railway              → --app-domain

The three apps build from a GitHub repo through a GitHub App you have already connected in
Coolify (Sources), so every push to the branch redeploys only the apps whose files changed.
Secrets are generated on the first run and read back from Coolify on later runs, so re-running
is safe. Standard library only — no pip install.

    export COOLIFY_TOKEN=...   # Keys & Tokens → API tokens, permission "root"
    python3 deploy/coolify/setup.py --coolify-url https://coolify.example.com \\
        --app-domain tracely.example.com --api-domain tracely-api.example.com

See deploy/coolify/README.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BACKEND_WATCH = "backend/**\nworkers/**\nsdk/**\nscripts/**\npyproject.toml\nuv.lock\nDockerfile.backend"
FRONTEND_WATCH = "frontend/**"

MINIO_COMPOSE = """\
services:
  minio:
    # MinIO stopped publishing images; pgsty/minio is the maintained drop-in build.
    image: pgsty/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: {user}
      MINIO_ROOT_PASSWORD: {password}
    volumes:
      - minio-data:/data
"""


class Coolify:
    def __init__(self, url: str, token: str) -> None:
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            sys.exit(f"Coolify API {method} {path} → {e.code}: {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            sys.exit(f"Can't reach Coolify at {self.base}: {e.reason}")
        return json.loads(raw) if raw else None


def secret(n: int = 32) -> str:
    return secrets.token_hex(n)


def repo_from_git() -> str | None:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def pick(items: list[dict], wanted: str | None, what: str, label=lambda i: i["name"]) -> dict:
    if wanted:
        for i in items:
            if wanted in (i.get("uuid"), i.get("name")):
                return i
        sys.exit(f"No {what} named or with uuid {wanted!r}. Found: {[label(i) for i in items]}")
    if len(items) != 1:
        sys.exit(f"Found {len(items)} {what}s — pick one with the matching flag: {[label(i) for i in items]}")
    return items[0]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--coolify-url", default=os.environ.get("COOLIFY_URL"), help="e.g. https://coolify.example.com (or $COOLIFY_URL)")
    p.add_argument("--app-domain", required=True, help="public host of the UI, e.g. tracely.example.com")
    p.add_argument("--api-domain", required=True, help="public host of the API/OTLP ingest, e.g. tracely-api.example.com")
    p.add_argument("--project", default="tracely", help="Coolify project (created if missing; default: tracely)")
    p.add_argument("--environment", default="production")
    p.add_argument("--server", help="server name or uuid (default: the only one)")
    p.add_argument("--github-app", help="Coolify GitHub App source, name or uuid (default: the only private one)")
    p.add_argument("--repo", default=repo_from_git(), help="owner/name (default: this checkout's origin)")
    p.add_argument("--branch", default="main")
    p.add_argument("--prefix", default="tracely", help="resource name prefix (default: tracely)")
    p.add_argument(
        "--tls-upstream",
        action="store_true",
        help="TLS ends before Coolify (Cloudflare Tunnel, another proxy): route plain http:// in Coolify, "
        "public URLs stay https://. Without it Coolify serves https:// with Let's Encrypt.",
    )
    p.add_argument("--no-deploy", action="store_true", help="create/update everything but don't deploy")
    args = p.parse_args()

    token = os.environ.get("COOLIFY_TOKEN")
    if not token:
        sys.exit("Set COOLIFY_TOKEN (Coolify → Keys & Tokens → API tokens, permission root).")
    if not args.coolify_url:
        sys.exit("Pass --coolify-url or set COOLIFY_URL.")
    if not args.repo:
        sys.exit("Pass --repo owner/name (couldn't read it from git).")

    c = Coolify(args.coolify_url, token)
    route_scheme = "http" if args.tls_upstream else "https"
    app_url, api_url = f"https://{args.app_domain}", f"https://{args.api_domain}"
    names = {k: f"{args.prefix}-{k}" for k in ("postgres", "clickhouse", "redis", "minio", "backend", "worker", "frontend")}

    # ── where ────────────────────────────────────────────────────────────
    server = pick(c.call("GET", "/servers"), args.server, "server")
    gh = pick([g for g in c.call("GET", "/github-apps") if not g.get("is_public")], args.github_app, "GitHub App")
    project = next((x for x in c.call("GET", "/projects") if x["name"] == args.project), None)
    if not project:
        project = c.call("POST", "/projects", {"name": args.project})
        print(f"+ project {args.project}")
    env = c.call("GET", f"/projects/{project['uuid']}/{args.environment}")
    where = {"server_uuid": server["uuid"], "project_uuid": project["uuid"], "environment_name": args.environment}
    existing = {r["name"]: r for r in c.call("GET", "/resources") if r.get("environment_id") == env["id"]}

    def find(key: str) -> dict | None:
        return existing.get(names[key])

    def app_env(uuid: str, key: str) -> str | None:
        for e in c.call("GET", f"/applications/{uuid}/envs"):
            if e["key"] == key and not e.get("is_preview"):
                return e.get("value")
        return None

    # ── data stores ─────────────────────────────────────────────────────
    if r := find("postgres"):
        pg = c.call("GET", f"/databases/{r['uuid']}")
        print(f"= {names['postgres']}")
    else:
        pg = c.call("POST", "/databases/postgresql", {
            **where, "name": names["postgres"], "image": "pgvector/pgvector:pg17",
            "postgres_user": "tracely", "postgres_password": secret(), "postgres_db": "tracely",
            "instant_deploy": True,
        })
        pg = c.call("GET", f"/databases/{pg['uuid']}")
        print(f"+ {names['postgres']}")

    if r := find("clickhouse"):
        ch = c.call("GET", f"/databases/{r['uuid']}")
        print(f"= {names['clickhouse']}")
    else:
        ch = c.call("POST", "/databases/clickhouse", {
            **where, "name": names["clickhouse"], "image": "clickhouse/clickhouse-server:24.8",
            "clickhouse_admin_user": "tracely", "clickhouse_admin_password": secret(),
            "instant_deploy": True,
        })
        ch = c.call("GET", f"/databases/{ch['uuid']}")
        print(f"+ {names['clickhouse']}")

    if r := find("redis"):
        rd = c.call("GET", f"/databases/{r['uuid']}")
        print(f"= {names['redis']}")
    else:
        rd = c.call("POST", "/databases/redis", {**where, "name": names["redis"], "redis_password": secret(), "instant_deploy": True})
        rd = c.call("GET", f"/databases/{rd['uuid']}")
        print(f"+ {names['redis']}")

    if r := find("minio"):
        mn = c.call("GET", f"/services/{r['uuid']}")
        raw = mn.get("docker_compose_raw") or ""
        minio_user = re.search(r"MINIO_ROOT_USER:\s*(\S+)", raw).group(1)
        minio_password = re.search(r"MINIO_ROOT_PASSWORD:\s*(\S+)", raw).group(1)
        print(f"= {names['minio']}")
    else:
        minio_user, minio_password = "tracely", secret()
        compose = MINIO_COMPOSE.format(user=minio_user, password=minio_password)
        mn = c.call("POST", "/services", {
            **where, "name": names["minio"], "instant_deploy": True,
            "docker_compose_raw": base64.b64encode(compose.encode()).decode(),
            # join Coolify's shared network so the apps reach it as minio-<uuid>
            "connect_to_docker_network": True,
        })
        print(f"+ {names['minio']}")

    # Databases sit on Coolify's network under their uuid; the service container is minio-<uuid>.
    db = f"tracely:{pg['postgres_password']}@{pg['uuid']}:5432/tracely"
    redis_url = rd["internal_db_url"]  # redis://default:<password>@<uuid>:6379/0 (the API hides the bare password)

    # ── apps ────────────────────────────────────────────────────────────
    backend_alias = f"{args.prefix}-backend-api"  # stable private name (container names change per deploy)
    common = {**where, "github_app_uuid": gh["uuid"], "git_repository": args.repo, "git_branch": args.branch,
              "build_pack": "dockerfile", "instant_deploy": False, "autogenerate_domain": False}

    def ensure_app(key: str, spec: dict) -> dict:
        if r := find(key):
            c.call("PATCH", f"/applications/{r['uuid']}", {k: v for k, v in spec.items() if k not in where})
            print(f"= {names[key]}")
            return r
        a = c.call("POST", "/applications/private-github-app", {**common, "name": names[key], **spec})
        print(f"+ {names[key]}")
        return a

    backend = ensure_app("backend", {
        "dockerfile_location": "/Dockerfile.backend", "ports_exposes": "8000",
        "domains": f"{route_scheme}://{args.api_domain}", "watch_paths": BACKEND_WATCH,
        "custom_network_aliases": backend_alias,
    })
    worker = ensure_app("worker", {
        "dockerfile_location": "/Dockerfile.backend", "ports_exposes": "8000", "domains": "",
        "watch_paths": BACKEND_WATCH,
    })
    frontend = ensure_app("frontend", {
        "dockerfile_location": "/frontend/Dockerfile.railway", "ports_exposes": "3000",
        "domains": f"{route_scheme}://{args.app_domain}", "watch_paths": FRONTEND_WATCH,
    })

    # Secrets shared by API + worker: reuse what the backend already has, else generate.
    session_secret = (find("backend") and app_env(backend["uuid"], "SESSION_SECRET")) or secret()
    encryption_key = (find("backend") and app_env(backend["uuid"], "SECRETS_ENCRYPTION_KEY")) or secret()

    shared = {
        "TRACELY_ENV": "prod", "AUTH_MODE": "local",
        "SESSION_SECRET": session_secret, "SECRETS_ENCRYPTION_KEY": encryption_key,
        "DATABASE_URL": f"postgresql+asyncpg://{db}", "ALEMBIC_DATABASE_URL": f"postgresql+psycopg://{db}",
        "CLICKHOUSE_HOST": ch["uuid"], "CLICKHOUSE_PORT": "8123", "CLICKHOUSE_USER": "tracely",
        "CLICKHOUSE_PASSWORD": ch["clickhouse_admin_password"], "CLICKHOUSE_DATABASE": "tracely",
        "CLICKHOUSE_ASYNC_INSERT": "1", "CH_SYSTEM_LOG_TTL_DAYS": "3",
        "REDIS_URL": redis_url,
        "S3_ENDPOINT_URL": f"http://minio-{mn['uuid']}:9000", "S3_REGION": "us-east-1",
        "S3_ACCESS_KEY_ID": minio_user, "S3_SECRET_ACCESS_KEY": minio_password,
        "S3_BUCKET": "tracely-events", "S3_EVENT_PREFIX": "events/",
        "APP_BASE_URL": app_url, "FRONTEND_ORIGIN": app_url,
        "CELERY_POOL": "solo", "CELERY_CONCURRENCY": "1",
    }
    envs = {
        backend["uuid"]: {**shared, "TRACELY_ROLE": "api", "PORT": "8000", "INTERNAL_API_URL": "http://localhost:8000"},
        worker["uuid"]: {**shared, "TRACELY_ROLE": "worker", "CELERY_BEAT": "true",
                         "INTERNAL_API_URL": f"http://{backend_alias}:8000"},
        frontend["uuid"]: {
            "TRACELY_API": f"http://{backend_alias}:8000", "PORT": "3000",
            "NODE_OPTIONS": "--dns-result-order=ipv4first",
            # inlined into the client bundle at `next build`
            "NEXT_PUBLIC_AUTH_MODE": "local", "NEXT_PUBLIC_TRACELY_PUBLIC_API": api_url,
        },
    }
    build_time = {"NEXT_PUBLIC_AUTH_MODE", "NEXT_PUBLIC_TRACELY_PUBLIC_API"}
    for uuid, values in envs.items():
        c.call("PATCH", f"/applications/{uuid}/envs/bulk", {"data": [
            {"key": k, "value": v, "is_buildtime": k in build_time, "is_runtime": True, "is_literal": True}
            for k, v in values.items()
        ]})
    print("  environment variables set on backend, worker, frontend")

    if args.no_deploy:
        print("\nSkipped deploy (--no-deploy).")
    else:
        uuids = ",".join([backend["uuid"], worker["uuid"], frontend["uuid"]])
        c.call("POST", "/deploy", query={"uuid": uuids})
        print("  deploy queued: backend, worker, frontend (first build takes a few minutes)")

    print(f"""
Done. In Coolify → {args.project} → {args.environment} you now have one card per component.
  UI   {app_url}   (the first account you create becomes the owner)
  API  {api_url}   (SDK / OTLP endpoint; ingest key under Settings → API keys)
Never regenerate SECRETS_ENCRYPTION_KEY: it decrypts the OpenRouter keys workspaces save.""")


if __name__ == "__main__":
    main()
