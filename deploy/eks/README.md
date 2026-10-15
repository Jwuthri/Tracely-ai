# Deploy Tracely to AWS EKS (or any Kubernetes cluster)

The docker-compose stack, translated to plain Kubernetes manifests in one `tracely` namespace.
Everything runs **inside the cluster**: Postgres, ClickHouse, Redis and MinIO each get a pod, and
the three stateful ones get an EBS volume. So the deploy needs no IAM role, no RDS, no S3 bucket. You need:

- `kubectl` access to a cluster with a default `StorageClass` (`gp3` on EKS),
- an image registry the nodes can pull from (ECR in the same account works with the default node role),
- `docker buildx`.

It is sized for a **trial or internal deployment**: single replicas, no backups, no ingress. See
[§7](#7-going-further) for what changes before real traffic.

```
  agents (in-cluster) ──OTLP──▶ backend:8000 ──▶ redis ──▶ worker (celery + beat)
  you ──port-forward──▶ frontend:3000 ──▶ backend           │
                                   │                         ▼
                        postgres (pgvector) · clickhouse · minio   ← EBS volumes
```

| Piece | Kind | Image | Storage |
|---|---|---|---|
| postgres | StatefulSet | `pgvector/pgvector:pg17` | 20Gi |
| clickhouse | StatefulSet | `clickhouse/clickhouse-server:24.8` | 50Gi (90-day TTL on events/scores) |
| minio | StatefulSet | `minio/minio` (mirror it, see §2) | 20Gi |
| redis | Deployment | `redis:7` | none (broker only) |
| migrate | Job | `tracely-backend` | bucket, ClickHouse DDL, Alembic, default project |
| backend | Deployment | `tracely-backend` | FastAPI, `/health` readiness |
| worker | Deployment | `tracely-backend` | Celery `solo/1` with embedded beat. Keep **one** replica |
| frontend | Deployment | `tracely-frontend` | Next.js production build |

Layout: [`base/`](base/) is the stack and is never edited per cluster. `overlays/<cluster>/` holds
the registry and scheduling for one cluster. Overlays are gitignored, except
[`overlays/example/`](overlays/example/).

---

## 1. Build and push the app images

Build for your nodes' CPU architecture (`kubectl get nodes -L kubernetes.io/arch`). The frontend
inlines `NEXT_PUBLIC_*` at build time, so the auth mode is a **build arg**. It must match
`AUTH_MODE` in [`base/tracely.yaml`](base/tracely.yaml) (`local`).

```bash
ECR=<account>.dkr.ecr.<region>.amazonaws.com
TAG=$(git rev-parse --short HEAD)
aws ecr create-repository --repository-name tracely-backend
aws ecr create-repository --repository-name tracely-frontend
aws ecr get-login-password | docker login --username AWS --password-stdin $ECR

docker buildx build --platform linux/arm64 -f Dockerfile.backend -t $ECR/tracely-backend:$TAG --push .
docker buildx build --platform linux/arm64 -f frontend/Dockerfile.railway \
  --build-arg NEXT_PUBLIC_AUTH_MODE=local -t $ECR/tracely-frontend:$TAG --push .
```

If `docker login` fails on macOS with *"The specified item already exists in the keychain"*, run
docker with a throwaway `DOCKER_CONFIG` that has no `credsStore`.

## 2. Mirror a MinIO image

Docker Hub and quay.io no longer serve `minio/minio`: a fresh pull fails with *"pull access
denied"*. Push a build you already have (any machine that has run the compose stack has one) to
your own registry:

```bash
aws ecr create-repository --repository-name tracely-minio
docker tag minio/minio:latest $ECR/tracely-minio:RELEASE.2025-09-07T16-13-09Z
docker push $ECR/tracely-minio:RELEASE.2025-09-07T16-13-09Z
```

Use the release your local image actually is (`docker image inspect minio/minio --format '{{index .Config.Labels "release"}}'`)
and set the same tag in the base or override it in the overlay.

## 3. Write your overlay

```bash
cp -r deploy/eks/overlays/example deploy/eks/overlays/my-cluster
```

Fill in the three image names and the tag. If your nodes are tainted (Karpenter pools often are), or
the cluster mixes architectures, uncomment the scheduling patch. Check the render before applying:

```bash
kubectl kustomize deploy/eks/overlays/my-cluster | grep image:
```

## 4. Apply

```bash
kubectl create namespace tracely
deploy/eks/create-secret.sh            # KUBE_CONTEXT=<ctx> to target a non-current context
kubectl apply -k deploy/eks/overlays/my-cluster
kubectl -n tracely wait --for=condition=complete job/migrate --timeout=10m
kubectl -n tracely rollout restart deploy/backend deploy/worker
```

[`create-secret.sh`](create-secret.sh) generates every password and key into `tracely-secrets`.
It uses `kubectl create`, not `apply`, so the plaintext never lands in a `last-applied-configuration`
annotation. It never rotates: Postgres keeps the password it was initialised with.

The backend crash-loops with `relation "ingest_keys" does not exist` until the migrate job finishes.
That is expected, and the restart above clears it.

Verify:

```bash
kubectl -n tracely exec deploy/backend -- python -c \
  "import urllib.request as u; print(u.urlopen('http://localhost:8000/health').read().decode())"
# {"status":"ok","dependencies":{"clickhouse":"ok","postgres":"ok"}}
```

## 5. Use it

```bash
kubectl -n tracely port-forward svc/frontend 3001:3000
```

Open http://localhost:3001 and **sign up immediately**. The deployment runs `TRACELY_ENV=prod`
with `ALLOW_PUBLIC_SIGNUP=false`, so the first account to register owns it and everyone after joins
by invite. Prod mode also means the published `tracely_dev_key` is never seeded, so create an
ingest key under Settings → API keys.

Agents in the same cluster send traces to the backend's service DNS name, with no exposure needed:

```python
tracely.init(endpoint="http://backend.tracely.svc.cluster.local:8000", api_key="<ingest key>")
```

An existing OTLP exporter works the same way: `POST {endpoint}/v1/traces` with
`Authorization: Bearer <ingest key>`. The CLI reads `TRACELY_API` / `TRACELY_KEY`.

LLM features (judge, failure intelligence) need an OpenRouter key per workspace, under Settings →
OpenRouter key. With none set, ingestion and structural clustering still run.

## 6. Upgrade

```bash
TAG=$(git rev-parse --short HEAD)    # rebuild + push both images (§1), bump newTag in the overlay
kubectl -n tracely delete job migrate   # Job specs are immutable; the new one re-runs migrations
kubectl apply -k deploy/eks/overlays/my-cluster
```

**A StatefulSet won't replace a pod that never became ready** (for example one stuck in
`ImagePullBackOff`). After fixing its spec, `kubectl -n tracely delete pod <name>-0`.

## 7. Going further

**Exposing the UI to a team.** Use an Ingress or a LoadBalancer with an **explicit** scheme
annotation. On EKS, an un-annotated `type: LoadBalancer` gets an *internet-facing* ELB. A common
pattern is an ALB Ingress (`alb.ingress.kubernetes.io/scheme`) with OIDC auth on the load balancer
(`auth-type: oidc`, e.g. Google Workspace) in front of Tracely's own login. Then set
`FRONTEND_ORIGIN` and `APP_BASE_URL` in the base ConfigMap to the public URL so invite links work.
OIDC on the ALB also blocks machine clients, so CI (`tracely gate`) and external SDKs need their own
route to the backend, or need to run inside the VPC.

**Private agent endpoints.** The base sets `ALLOW_PRIVATE_URLS=true` so scenarios can call agents
on cluster-internal addresses. That also lets any workspace aim Tracely's outbound requests (agent
endpoints, monitor webhooks) at anything on the cluster network. ClickHouse has a password, but
every other unauthenticated service the cluster runs becomes reachable. Only keep it while everyone
with a workspace is trusted.

**Managed stores.** Swap the in-cluster pods for RDS Postgres (with the `vector` extension),
ElastiCache Redis and S3 by changing the URLs in `tracely-secrets` / the ConfigMap. One caveat:
S3 through an IAM role (IRSA) does not work yet, because `backend/tracely/infrastructure/blob/s3.py`
always passes the static `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`.

**Throughput.** Run the worker as `CELERY_POOL=prefork`, `CELERY_CONCURRENCY>=2`, and move beat
to its own single-replica Deployment before scaling workers. Every worker started with `--beat`
re-fires the schedule. See [`guides/DEPLOY.md`](../../guides/DEPLOY.md) §3.
