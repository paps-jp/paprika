#!/bin/bash
# One-shot deployer for paprika. Runs on the hub CT.
#
# What it does, in order:
#   1. git pull on the hub's working tree.
#   2. docker compose up -d for the hub stack (hub + agent + worker +
#      redis). `up -d` (not `restart`) so .env changes are actually
#      picked up; idempotent when nothing changed.
#   3. rsync the working tree to every worker host (discovered live
#      from the hub's /workers/hosts API) and `docker compose up -d`
#      the worker container there.
#   4. rsync the working tree to extra hosts listed in
#      scripts/extra-hosts.env (e.g. the GPU box running vLLM). No
#      service restart -- vLLM stays up.
#
# Usage:
#   ./scripts/deploy.sh                  rebuild only if compose/build
#                                        config changed (default)
#   REBUILD=1 ./scripts/deploy.sh        force --build on hub + workers
#                                        (needed when Dockerfile or
#                                        requirements.txt changed)
#   SKIP_WORKERS=1 ./scripts/deploy.sh   only update the hub stack
#   SKIP_HUB=1 ./scripts/deploy.sh       only fan out to workers
#   SKIP_EXTRA=1 ./scripts/deploy.sh     skip the GPU-box rsync
#
# Safe to re-run. Aborts on first error.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

# -- 1) Pull latest -----------------------------------------------------------
echo "==> [1/3] git pull"
PREV="$(git rev-parse --short HEAD)"
git pull --ff-only
NEXT="$(git rev-parse --short HEAD)"
if [ "$PREV" = "$NEXT" ]; then
  echo "    (already at $NEXT)"
else
  echo "    $PREV -> $NEXT"
fi

# -- 2) Hub stack -------------------------------------------------------------
#
# Order matters: hub MUST be refreshed BEFORE we fan out to workers.
# Reason: source code is bind-mounted (./server -> /app/server) so an
# edit on disk is visible to a fresh process the moment it imports,
# but the already-running hub keeps its boot-time _CACHED_WORKER_VERSION.
# `docker compose up -d` is a no-op when only .py files changed (compose
# config is unchanged) -- so without an explicit restart here, the hub
# stays on the old hash while every worker (which DOES restart in step
# 3) reports a fresh hash. That asymmetry is exactly what triggered
# the 2026-05-25 outage: 25 workers entered an exit-42 / restart /
# self-update loop trying to "fix" a mismatch the hub itself had
# manufactured. (server/worker/agent.py:default_worker_version has a
# 10s TTL safety net for the same reason; this restart is the
# deterministic-correctness version of that fix.)
if [ -z "${SKIP_HUB:-}" ]; then
  echo
  echo "==> [2/3] hub stack: docker compose up -d"
  if [ -n "${REBUILD:-}" ]; then
    docker compose up -d --build
  else
    docker compose up -d
  fi
  # `up -d` above is a no-op when only .py files changed. Always do
  # an explicit hub restart so the running process re-imports + re-
  # hashes /app/server. Cheap (~3s), and the worker fan-out in step
  # 3 will see the fresh "expected version" instead of yesterday's.
  echo "    restarting hub to refresh source-hash cache"
  docker compose restart hub
  # Wait for hub HTTP to come back so step 3's fan-out doesn't race
  # against a still-booting hub. Cap at 30s; if hub isn't up by then,
  # something is wrong and we should NOT proceed to mass-restart workers.
  #
  # Use GET /system (hub's own version) instead of workers[0].version so
  # we don't depend on any worker being connected at this moment -- right
  # after a restart no workers are visible yet.
  HUB_VER=""
  for i in $(seq 1 15); do
    sleep 2
    HUB_VER="$(curl -fsS --max-time 3 http://localhost:8000/system 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' \
      2>/dev/null || true)"
    if [ -n "$HUB_VER" ]; then
      echo "    hub up, version=$HUB_VER"
      break
    fi
  done
  if [ -z "$HUB_VER" ]; then
    echo "    !! hub not responding after 30s; aborting before worker fan-out"
    docker compose ps --format "table {{.Service}}\t{{.Status}}"
    exit 1
  fi
  docker compose ps --format "table {{.Service}}\t{{.Status}}"
else
  echo
  echo "==> [2/3] SKIP_HUB set, skipping hub stack"
  HUB_VER=""
fi

# -- 3) External workers ------------------------------------------------------
if [ -z "${SKIP_WORKERS:-}" ]; then
  echo
  echo "==> [3/4] external workers via sync-workers.sh"
  # sync-workers.sh defaults to `restart`; switch to `up -d` so any
  # .env/compose changes take effect on the workers too. We do this
  # by passing through the env knobs sync-workers.sh recognises plus
  # an inline command override.
  if [ -n "${REBUILD:-}" ]; then
    # Force a worker rebuild remotely. Use git-pull-workers.sh because
    # it has a REBUILD mode; sync-workers.sh just restarts.
    REBUILD=1 "$HERE/git-pull-workers.sh"
  else
    "$HERE/sync-workers.sh"
    # After rsync, also re-create the worker container so .env /
    # compose changes take effect (sync-workers.sh only `restart`s).
    # Re-use the same auto-discovery sync-workers.sh just did: pull
    # the live worker list from the hub, no workers.env needed.
    if HOSTS_JSON="$(curl -fsS --max-time 10 http://localhost:8000/workers/hosts 2>/dev/null)"; then
      IPS="$(echo "$HOSTS_JSON" | python3 -c '
import json, sys
print(" ".join(h["address"] for h in json.load(sys.stdin).get("hosts",[]) if h.get("address")))
')"
      SSH_USER_LOCAL="${SSH_USER:-root}"
      for ip in $IPS; do
        echo "    recreating worker on ${SSH_USER_LOCAL}@${ip}"
        ssh "${SSH_USER_LOCAL}@${ip}" \
          "cd '${REMOTE_PATH:-/opt/paprika}' && \
           docker compose -f '${COMPOSE_FILE:-docker-compose-worker.yml}' up -d"
      done
    else
      echo "    skipping per-worker 'up -d' (hub /workers/hosts unreachable)"
    fi
  fi
else
  echo
  echo "==> [3/4] SKIP_WORKERS set, skipping workers"
fi

# -- 3.5) Post-deploy version verify -----------------------------------------
#
# Two checks:
#   a) Hub's own /system version matches what it broadcasts in the tarball
#      (X-Paprika-Version header). If these differ, the running uvicorn
#      process is still on stale module cache -- the most common cause is
#      using `docker compose up -d` instead of `docker compose restart`.
#   b) Connected workers all report the same version as the hub.
#      Transient drift is normal right after a fan-out; we warn but don't
#      abort since workers self-update within the agent.py TTL.
if [ -z "${SKIP_HUB:-}" ]; then
  echo
  echo "==> [3.5] post-deploy version check"

  # (a) Hub self-consistency: /system == X-Paprika-Version on tarball.
  TARBALL_VER="$(curl -s -D - http://localhost:8000/worker-source.tar.gz -o /dev/null \
    | grep -i '^x-paprika-version:' | awk '{print $2}' | tr -d '[:space:]' || true)"
  if [ -n "$TARBALL_VER" ] && [ -n "$HUB_VER" ]; then
    if [ "$TARBALL_VER" = "$HUB_VER" ]; then
      echo "    ✓ hub self-consistent: /system == tarball X-Paprika-Version ($HUB_VER)"
    else
      echo "    !! hub module-cache mismatch:"
      echo "       /system version   : $HUB_VER"
      echo "       tarball X-Paprika : $TARBALL_VER"
      echo "    → run: docker compose restart hub"
    fi
  fi

  # (b) Worker fleet version drift.
  if [ -z "${SKIP_WORKERS:-}" ]; then
    for i in $(seq 1 15); do
      sleep 2
      DRIFT="$(curl -fsS --max-time 3 http://localhost:8000/workers 2>/dev/null \
        | python3 -c 'import json,sys
d=json.load(sys.stdin)
hub_v = "'"$HUB_VER"'"
bad = [w["worker_id"] for w in d.get("workers",[])
       if w.get("version") and hub_v and w["version"] != hub_v]
print("\n".join(bad))
' 2>/dev/null || true)"
      if [ -z "$DRIFT" ]; then
        echo "    ✓ all connected workers match hub version $HUB_VER"
        break
      fi
      if [ "$i" -eq 15 ]; then
        echo "    !! workers still on mismatched version after 30s:"
        printf "      %s\n" $DRIFT
        echo "    (most likely still self-updating; re-check /workers later)"
      fi
    done
  fi
fi

# -- 4) Extra hosts (e.g. GPU box running an external LLM server) ------------
# Rsync the working tree to hosts listed in scripts/extra-hosts.env so
# any helper / config files stay in sync. We DON'T restart anything
# automatically on these hosts -- pulling the rug out from under
# in-flight requests is too disruptive. Just ship the files; operator
# restarts whatever they need to manually.
EXTRA_HOSTS=""
EXTRA_REMOTE_PATH=""
if [ -f "$HERE/extra-hosts.env" ]; then
  # shellcheck source=/dev/null
  . "$HERE/extra-hosts.env"
fi
if [ -n "${EXTRA_HOSTS:-}" ] && [ -z "${SKIP_EXTRA:-}" ]; then
  echo
  echo "==> [4/4] extra hosts (code rsync only, no service restart)"
  for H in $EXTRA_HOSTS; do
    echo "    $H"
    rsync -avz --delete \
      --exclude=.git \
      --exclude=data/ \
      --exclude=__pycache__/ \
      --exclude='*.pyc' \
      --exclude=.venv/ \
      --exclude=node_modules/ \
      --exclude=.env \
      --exclude=.env.agent \
      --exclude='*.log' \
      "$ROOT/" "$H:${EXTRA_REMOTE_PATH:-/home/www/paprika}/"
  done
else
  echo
  echo "==> [4/4] no extra hosts configured (scripts/extra-hosts.env)"
fi

echo
echo "✓ deployed $NEXT"
