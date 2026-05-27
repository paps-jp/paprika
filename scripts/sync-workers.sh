#!/bin/bash
# Push the working tree of /opt/paprika to every worker host via rsync,
# write a VERSION stamp into the source root, then restart each
# worker container so the bind-mounted code is reloaded.
#
# Avoids hitting GitHub on every iteration and works without a
# container registry. Designed to run from the hub CT (or any host
# that has the latest source tree on disk and SSH access to the
# workers).
#
# Configuration:
#   By default the worker list is fetched from the hub's live inventory
#   (GET ${HUB_URL}/workers/hosts -- HUB_URL defaults to localhost:8000)
#   so a worker added to the fleet is automatically a deploy target.
#
#   Manual override (CI, partial deploy, "just these two hosts"):
#       WORKERS='root@host1 root@host2' ./scripts/sync-workers.sh
#
#   Optional knobs:
#       COMPOSE_FILE=docker-compose-worker.yml
#       RESTART_SERVICE=worker
#       REMOTE_PATH=/opt/paprika
#       SSH_USER=root              # used when building WORKERS from the API
#       HUB_URL=http://localhost:8000
#
# Usage:
#   ./scripts/sync-workers.sh           # rsync + restart everyone
#   DRY_RUN=1 ./scripts/sync-workers.sh # show what rsync would copy
#   NO_RESTART=1 ./scripts/sync-workers.sh  # rsync only, no docker restart
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SOURCE="${SOURCE:-$(cd "$HERE/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-worker.yml}"
RESTART_SERVICE="${RESTART_SERVICE:-worker}"
REMOTE_PATH="${REMOTE_PATH:-/opt/paprika}"
SSH_USER="${SSH_USER:-root}"
HUB_URL="${HUB_URL:-http://localhost:8000}"

# Auto-discover the worker fleet from the hub when the caller didn't
# hand us one. Single source of truth = hub's live inventory; no more
# workers.env file to drift out of sync with reality.
if [ -z "${WORKERS:-}" ]; then
  if ! HOSTS_JSON="$(curl -fsS --max-time 10 "${HUB_URL}/workers/hosts" 2>&1)"; then
    echo "error: WORKERS not set and ${HUB_URL}/workers/hosts unreachable:" >&2
    echo "  $HOSTS_JSON" >&2
    echo "  Set WORKERS='root@host1 root@host2' inline to bypass discovery." >&2
    exit 2
  fi
  IPS="$(echo "$HOSTS_JSON" | python3 -c '
import json, sys
print(" ".join(h["address"] for h in json.load(sys.stdin).get("hosts",[]) if h.get("address")))
')"
  if [ -z "$IPS" ]; then
    echo "error: hub returned 0 connected worker hosts. Nothing to deploy." >&2
    echo "  (override with WORKERS='root@host1 ...' if you have a target in mind.)" >&2
    exit 2
  fi
  WORKERS=""
  for ip in $IPS; do
    WORKERS="${WORKERS}${SSH_USER}@${ip} "
  done
  WORKERS="${WORKERS% }"
fi

# Stamp the source tree with a build identifier the bind-mounted
# workers will pick up at startup (admin UI surfaces this).
VERSION_SHA="$(cd "$SOURCE" && git rev-parse --short HEAD 2>/dev/null || echo dev)"
VERSION_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
VERSION_STR="${VERSION_SHA} ${VERSION_TS}"
printf '%s\n' "$VERSION_STR" > "$SOURCE/VERSION"

echo "==> source : $SOURCE"
echo "==> version: $VERSION_STR"
echo "==> workers: $WORKERS"
echo "==> compose: $COMPOSE_FILE (service: $RESTART_SERVICE)"
echo

RSYNC_OPTS=(
  -avz --delete
  --exclude=.git
  --exclude=data/
  --exclude=__pycache__/
  --exclude='*.pyc'
  --exclude=.venv/
  --exclude=node_modules/
  --exclude=.env.agent
  --exclude=.env
  --exclude='*.log'
)
if [ -n "${DRY_RUN:-}" ]; then
  RSYNC_OPTS+=(--dry-run)
  echo "(DRY_RUN=1; no files will be written or services restarted)"
fi

for W in $WORKERS; do
  echo "=== $W ==="
  rsync "${RSYNC_OPTS[@]}" "$SOURCE/" "$W:$REMOTE_PATH/"
  if [ -z "${DRY_RUN:-}" ] && [ -z "${NO_RESTART:-}" ]; then
    ssh "$W" "cd '$REMOTE_PATH' && docker compose -f '$COMPOSE_FILE' restart '$RESTART_SERVICE'"
  fi
done

echo
echo "==> done: $VERSION_STR pushed to $(echo "$WORKERS" | wc -w) host(s)"
