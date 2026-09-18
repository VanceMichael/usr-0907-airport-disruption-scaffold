#!/bin/sh
# Docker self-test for the airport disruption API.
#
# Builds the service image from scratch, starts it with a fresh named
# volume, then verifies — entirely inside the container, no external
# services involved:
#   1. valid events (closure / extension / reopening / indefinite)
#   2. invalid events (stable structured errors, no partial writes)
#   3. idempotent replay of the same event_id
#   4. cross-midnight window computation
#   5. data persistence across a container restart
#
# Usage: scripts/selftest.sh   (requires only Docker on the host)
set -eu

IMAGE="disruption-api:selftest"
CONTAINER="disruption-selftest-$$"
VOLUME="disruption-selftest-data-$$"
REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_healthy() {
  attempt=0
  while [ "$attempt" -lt 60 ]; do
    if docker exec "$CONTAINER" python -c \
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=1)" \
        >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  echo "service did not become healthy in time" >&2
  docker logs "$CONTAINER" >&2 || true
  return 1
}

run_phase() {
  if ! docker exec "$CONTAINER" python /scripts/selftest_client.py \
      --base-url http://127.0.0.1:8080 --phase "$1"; then
    echo "--- container logs ---" >&2
    docker logs "$CONTAINER" >&2 || true
    echo "SELF-TEST FAILED in phase $1" >&2
    exit 1
  fi
}

echo "==> Building image $IMAGE"
docker build -f "$REPO_ROOT/Dockerfile" -t "$IMAGE" "$REPO_ROOT"

echo "==> Starting container $CONTAINER with fresh volume $VOLUME"
docker volume create "$VOLUME" >/dev/null
docker run -d --name "$CONTAINER" \
  -e PORT=8080 \
  -e DB_PATH=/data/disruptions.db \
  -v "$VOLUME":/data \
  -v "$REPO_ROOT/scripts:/scripts:ro" \
  "$IMAGE" >/dev/null

echo "==> Waiting for the service to become healthy"
wait_healthy

echo "==> Phase 1: valid/invalid events, idempotency, cross-midnight, queries"
run_phase before-restart

echo "==> Restarting the container (volume stays attached)"
docker restart "$CONTAINER" >/dev/null
wait_healthy

echo "==> Phase 2: persistence after restart"
run_phase after-restart

echo "SELF-TEST PASSED"
