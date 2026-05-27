#!/bin/bash
# paprika-worker.sh  — one-line Worker launcher for Linux / macOS
#
# Usage:
#   ./scripts/paprika-worker.sh
#
# Environment overrides (all optional):
#   HUB_URL=ws://192.168.1.10:8000 ./scripts/paprika-worker.sh
#   NOVNC_PUBLIC_HOST=myworker.example.com ./scripts/paprika-worker.sh
#   SLOT_POOL=4 ./scripts/paprika-worker.sh

set -e

IMAGE="${IMAGE:-paprika-worker:latest}"
NAME="${NAME:-paprika-worker}"
PORTS="${PORTS:-6080-6081:6080-6081}"

# Default NOVNC_PUBLIC_HOST to the host's hostname (FQDN if available)
: "${NOVNC_PUBLIC_HOST:=$(hostname -f 2>/dev/null || hostname)}"
export NOVNC_PUBLIC_HOST

# Stop+remove any existing one with the same name (idempotent)
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "Starting $NAME → $IMAGE  (NOVNC=$NOVNC_PUBLIC_HOST)"

docker run -d --name "$NAME" \
    --restart unless-stopped \
    --shm-size=2gb \
    -p "$PORTS" \
    -e HUB_URL="${HUB_URL:-}" \
    -e NOVNC_PUBLIC_HOST="$NOVNC_PUBLIC_HOST" \
    -e SLOT_POOL="${SLOT_POOL:-}" \
    -e LABELS="${LABELS:-}" \
    -e WORKER_ID="${WORKER_ID:-}" \
    -v paprika-worker-state:/root/.paprika \
    "$IMAGE"

echo "✓ paprika-worker started"
echo "  noVNC: http://$NOVNC_PUBLIC_HOST:6080/vnc_lite.html  (and 6081)"
echo "  Logs:  docker logs -f $NAME"
