#!/usr/bin/env bash
# End-to-end Docker self-test for the airport disruption service.
#
# In a clean environment this script:
#   1. builds the service image from scratch,
#   2. starts the stack and waits for the health check,
#   3. verifies valid events, invalid events, idempotent replay, and
#      cross-midnight impact computation (phase 1),
#   4. recreates the service container on the same persistent volume,
#   5. verifies that events, impacts, and idempotency survived (phase 2).
#
# Usage: scripts/docker_selftest.sh
# Env:   KEEP_STACK=1   leave the stack running afterwards (default: tear down)
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE="disruption-service"
CLIENT_CONTAINER_PATH="/tmp/selftest_client.py"

log() { printf '\n==> %s\n' "$*"; }

if ! docker compose version >/dev/null 2>&1; then
  echo "error: 'docker compose' (v2) is required but not available" >&2
  exit 1
fi

cleanup() {
  if [ "${KEEP_STACK:-0}" != "1" ]; then
    log "Tearing down stack and removing volumes"
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  else
    log "KEEP_STACK=1 set; leaving the stack running"
  fi
}
trap cleanup EXIT

log "Resetting any previous stack state (containers and volumes)"
docker compose down -v --remove-orphans >/dev/null 2>&1 || true

log "Building service image"
docker compose build "$SERVICE"

log "Starting stack and waiting for health checks"
docker compose up -d --wait

log "Verifying scaffold fixture checks still pass"
docker compose exec -T scaffold sh scaffold/validate_inputs.sh

log "Copying self-test client into the service container"
docker compose cp scripts/selftest_client.py "$SERVICE:$CLIENT_CONTAINER_PATH"

log "Phase 1: valid/invalid events, idempotency, cross-midnight computation"
docker compose exec -T "$SERVICE" python "$CLIENT_CONTAINER_PATH" --phase 1

log "Recreating the service container (persistent volume retained)"
docker compose up -d --wait --force-recreate "$SERVICE"

log "Phase 2: data retention after container recreation"
docker compose exec -T "$SERVICE" python "$CLIENT_CONTAINER_PATH" --phase 2

log "Confirming the SQLite database file persists in the named volume"
docker compose exec -T "$SERVICE" test -s /data/disruptions.db \
  && echo "  [PASS] /data/disruptions.db exists and is non-empty"

log "Docker self-test completed successfully"
