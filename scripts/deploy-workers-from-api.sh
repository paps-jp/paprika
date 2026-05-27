#!/bin/bash
# Fleet deploy driven by the LIVE worker inventory (GET /workers/hosts)
# instead of the static scripts/workers.env. Run this ON the hub host.
#
# Why: workers.env drifts -- a worker host added to the fleet but not to
# the file silently misses every deploy (that's how a feature shipped to
# 1 of 25 hosts and looked broken). The hub already knows every connected
# worker's IP (ConnectedWorker.client_address), exposed at /workers/hosts;
# this script uses that as the source of truth so the deploy can't miss a
# host that's actually running.
#
# For each worker host it:
#   1. rsyncs the hub's working tree (server/ core/ VERSION) to
#      /opt/paprika on the host
#   2. clears stale __pycache__
#   3. `docker compose restart worker` (NOT `up -d` -- a bind-mount-only
#      source change doesn't trigger `up -d` to recreate, so the process
#      keeps running old code; restart forces a reload)
#
# Usage (on the hub host):
#   ./scripts/deploy-workers-from-api.sh
#   HUB_URL=http://localhost:8000 ./scripts/deploy-workers-from-api.sh
#   SSH_USER=root ./scripts/deploy-workers-from-api.sh
#   DRY_RUN=1 ./scripts/deploy-workers-from-api.sh    # print plan only
#
# Requires: curl, jq, rsync, ssh (key-based auth to the worker hosts).
set -euo pipefail

HUB_URL="${HUB_URL:-http://localhost:8000}"
SSH_USER="${SSH_USER:-root}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/paprika}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

echo "==> querying live worker inventory: $HUB_URL/workers/hosts"
HOSTS_JSON="$(curl -fsS "$HUB_URL/workers/hosts")"
# Parse with python3 (always present on the hub) so we don't depend on
# jq being installed. Falls back to grep if python3 is somehow missing.
_extract_hosts() {
  if command -v python3 >/dev/null 2>&1; then
    echo "$HOSTS_JSON" | python3 -c \
      'import sys,json; print("\n".join(h["address"] for h in json.load(sys.stdin).get("hosts",[]) if h.get("address")))'
  elif command -v jq >/dev/null 2>&1; then
    echo "$HOSTS_JSON" | jq -r '.hosts[].address'
  else
    echo "$HOSTS_JSON" | grep -oE '"address":[[:space:]]*"[^"]+"' \
      | sed -E 's/.*"address":[[:space:]]*"([^"]+)".*/\1/'
  fi
}
mapfile -t HOSTS < <(_extract_hosts | sort -u)

if [ "${#HOSTS[@]}" -eq 0 ]; then
  echo "!! no worker hosts returned -- is the hub up and are workers connected?"
  exit 1
fi

echo "==> ${#HOSTS[@]} worker host(s):"
printf '    %s\n' "${HOSTS[@]}"

if [ -n "${DRY_RUN:-}" ]; then
  echo "==> DRY_RUN set -- not deploying."
  exit 0
fi

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo
  echo "==> [$ip] rsync + restart worker"
  if rsync -az --delete \
        --exclude '__pycache__' --exclude '*.pyc' \
        ./server ./core ./VERSION \
        "${SSH_USER}@${ip}:${REMOTE_ROOT}/" \
     && ssh -o ConnectTimeout=10 "${SSH_USER}@${ip}" \
        "cd ${REMOTE_ROOT} && \
         find server core -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; \
         docker compose restart worker" ; then
    echo "    [$ip] OK"
  else
    echo "    [$ip] FAILED"
    FAILED+=("$ip")
  fi
done

echo
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "✓ deployed to all ${#HOSTS[@]} worker host(s)"
else
  echo "!! deployed with ${#FAILED[@]} failure(s): ${FAILED[*]}"
  exit 1
fi
