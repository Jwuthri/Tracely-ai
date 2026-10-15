#!/bin/sh
# Creates tracely-secrets once with fresh random values. Never rotates: Postgres keeps the
# password it was initialised with, so a new one would lock the app out of its own volume.
set -eu
K="kubectl ${KUBE_CONTEXT:+--context $KUBE_CONTEXT} -n tracely"   # KUBE_CONTEXT unset = current context
if $K get secret tracely-secrets >/dev/null 2>&1; then echo "tracely-secrets exists, leaving it"; exit 0; fi

r() { openssl rand -hex 32; }
PG=$(r); RD=$(r)
$K create secret generic tracely-secrets \
  --from-literal=POSTGRES_PASSWORD="$PG" \
  --from-literal=DATABASE_URL="postgresql+asyncpg://tracely:$PG@postgres:5432/tracely" \
  --from-literal=ALEMBIC_DATABASE_URL="postgresql+psycopg://tracely:$PG@postgres:5432/tracely" \
  --from-literal=CLICKHOUSE_PASSWORD="$(r)" \
  --from-literal=REDIS_PASSWORD="$RD" \
  --from-literal=REDIS_URL="redis://:$RD@redis:6379/0" \
  --from-literal=S3_SECRET_ACCESS_KEY="$(r)" \
  --from-literal=SESSION_SECRET="$(r)" \
  --from-literal=SECRETS_ENCRYPTION_KEY="$(r)"
