#!/bin/sh
# Ship the current commit: build + push both app images, point the overlay at them, re-run
# migrations, roll out. Run from the repo root, logged in to your registry.
#   deploy/eks/redeploy.sh deploy/eks/overlays/<cluster>     (or an overlay dir in your infra repo)
# Env: KUBE_CONTEXT (default: current), PLATFORM (default linux/arm64),
#      PUBLIC_API = backend URL shown in Settings → API keys (baked into the frontend build).
set -eu
OVERLAY=${1:?usage: redeploy.sh <overlay-dir>}
K="kubectl ${KUBE_CONTEXT:+--context $KUBE_CONTEXT}"
TAG=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain)" ] || { echo "uncommitted changes: commit first so the tag means something" >&2; exit 1; }
# ponytail: registry read back from the overlay's tracely-backend line; one registry for both images
REG=$(sed -n 's#.*newName: \([^,]*\)/tracely-backend.*#\1#p' "$OVERLAY/kustomization.yaml")
[ -n "$REG" ] || { echo "no tracely-backend newName in $OVERLAY/kustomization.yaml" >&2; exit 1; }
# An overlay outside this repo pulls the base from git (`…//deploy/eks/base?ref=`): it moves to
# this commit with the images, so manifests and code never drift apart. That needs it pushed.
if grep -q '?ref=' "$OVERLAY/kustomization.yaml"; then
  git branch -r --contains HEAD | grep -q . || { echo "push $TAG first: the overlay fetches the base from the remote" >&2; exit 1; }
  REF=$(git rev-parse HEAD)
fi

docker buildx build --platform "${PLATFORM:-linux/arm64}" -f Dockerfile.backend \
  -t "$REG/tracely-backend:$TAG" --push .
docker buildx build --platform "${PLATFORM:-linux/arm64}" -f frontend/Dockerfile.railway \
  --build-arg NEXT_PUBLIC_AUTH_MODE=local --build-arg NEXT_PUBLIC_TRACELY_PUBLIC_API="${PUBLIC_API:-}" \
  -t "$REG/tracely-frontend:$TAG" --push .

# every newTag in the overlay is the app version (the MinIO entry carries none)
sed -i.bak "s/newTag: [^ ,}]*/newTag: $TAG/" "$OVERLAY/kustomization.yaml"
[ -z "${REF:-}" ] || sed -i.bak "s/?ref=[0-9A-Za-z._/-]*/?ref=$REF/" "$OVERLAY/kustomization.yaml"
rm -f "$OVERLAY/kustomization.yaml.bak"

$K -n tracely delete job migrate --ignore-not-found   # Job specs are immutable
$K apply -k "$OVERLAY"
$K -n tracely wait --for=condition=complete job/migrate --timeout=10m
for d in backend worker frontend; do $K -n tracely rollout status deploy/$d --timeout=5m; done
echo "deployed $TAG"
