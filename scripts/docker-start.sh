#!/bin/sh
# Default command of the Dockerfile.backend image: TRACELY_ROLE picks the process, so one image
# serves the API, the worker and the one-shot migrate job on hosts that run each as its own
# service (Coolify's per-service deploy in deploy/coolify/). docker-compose.yml, Railway and EKS
# set their own commands and never reach this script.
#
#   TRACELY_ROLE=api      (default) migrations, then uvicorn on $PORT (8000)
#   TRACELY_ROLE=worker   Celery worker; CELERY_BEAT=true embeds beat — run exactly ONE beat
#   TRACELY_ROLE=migrate  migrations only, then exit
#
# Migrations are idempotent: ClickHouse DDL → alembic → LangGraph checkpointer → seed default
# project → ensure the S3 bucket. The API runs them on every start (TRACELY_MIGRATE_ON_START=false
# skips that) because a host's pre-deploy hook isn't guaranteed to run on the very first deploy.
set -e

migrate() {
  python -m tracely.infrastructure.clickhouse.migrations
  (cd backend && alembic upgrade head)
  python -m tracely.infrastructure.llm.checkpointer
  python -m tracely.services.seeding_service
  python -c 'import tracely.infrastructure.blob.s3 as s; s.ensure_bucket()'
}

case "${TRACELY_ROLE:-api}" in
  api)
    if [ "${TRACELY_MIGRATE_ON_START:-true}" = "true" ]; then
      migrate
    fi
    exec uvicorn tracely.api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    beat=""
    if [ "${CELERY_BEAT:-false}" = "true" ]; then
      beat="--beat"
    fi
    exec celery -A tracely_workers.worker worker --pool="${CELERY_POOL:-solo}" \
      --concurrency="${CELERY_CONCURRENCY:-1}" $beat --loglevel=info
    ;;
  migrate)
    migrate
    ;;
  *)
    echo "docker-start.sh: unknown TRACELY_ROLE '${TRACELY_ROLE}' (api | worker | migrate)" >&2
    exit 64
    ;;
esac
