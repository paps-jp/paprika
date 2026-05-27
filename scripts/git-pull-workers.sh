#!/bin/bash
# SSH into every worker host, `git pull` the configured branch, and
# restart the worker container.
#
# Each worker hits GitHub independently -- use this when you want
# workers' on-disk source to match a published commit exactly (e.g.
# after merging to main). For a deploy that doesn't touch GitHub and
# can include uncommitted local edits, use ./sync-workers.sh instead.
#
# Configuration:
#   By default the worker list is fetched from the hub's live inventory
#   (GET ${HUB_URL}/workers/hosts -- HUB_URL defaults to localhost:8000).
#
#   Manual override:
#       WORKERS='root@host1 root@host2' ./scripts/git-pull-workers.sh
#
# Env knobs (all optional):
#   COMPOSE_FILE=docker-compose-worker.yml  default
#   RESTART_SERVICE=worker                  default
#   REMOTE_PATH=/opt/paprika                default
#   BRANCH=main                             which branch to pull
#   SSH_USER=root                           when building WORKERS from API
#   HUB_URL=http://localhost:8000
#   REBUILD=1                               full `up -d --build` instead of
#                                           plain `restart` (only needed when
#                                           Dockerfile or requirements.txt
#                                           changed -- Python edits ride the
#                                           bind mount and don't need rebuild)
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-worker.yml}"
RESTART_SERVICE="${RESTART_SERVICE:-worker}"
REMOTE_PATH="${REMOTE_PATH:-/opt/paprika}"
BRANCH="${BRANCH:-main}"
SSH_USER="${SSH_USER:-root}"
HUB_URL="${HUB_URL:-http://localhost:8000}"

# Auto-discover the worker fleet from the hub when the caller didn't
# hand us one. Single source of truth = hub's live inventory.
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
    echo "error: hub returned 0 connected worker hosts. Nothing to pull." >&2
    exit 2
  fi
  WORKERS=""
  for ip in $IPS; do
    WORKERS="${WORKERS}${SSH_USER}@${ip} "
  done
  WORKERS="${WORKERS% }"
fi

echo "==> branch  : $BRANCH"
echo "==> workers : $WORKERS"
echo "==> compose : $COMPOSE_FILE (service: $RESTART_SERVICE)"
if [ -n "${REBUILD:-}" ]; then
  echo "==> REBUILD mode: will run docker compose up -d --build"
fi
echo

for W in $WORKERS; do
  echo "=== $W ==="
  if [ -n "${REBUILD:-}" ]; then
    ssh "$W" "set -e; \
      cd '$REMOTE_PATH'; \
      git fetch origin; \
      git checkout '$BRANCH'; \
      git pull --ff-only origin '$BRANCH'; \
      docker compose -f '$COMPOSE_FILE' up -d --build '$RESTART_SERVICE'"
  else
    ssh "$W" "set -e; \
      cd '$REMOTE_PATH'; \
      git fetch origin; \
      git checkout '$BRANCH'; \
      git pull --ff-only origin '$BRANCH'; \
      docker compose -f '$COMPOSE_FILE' restart '$RESTART_SERVICE'"
  fi
done

echo
echo "==> done: pulled '$BRANCH' on $(echo "$WORKERS" | wc -w) host(s)"
