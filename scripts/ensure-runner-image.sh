#!/bin/bash
# ensure-runner-image.sh — guarantee every production hub has the
# `paprika-runner:latest` image the codegen-loop sandbox needs.
#
#   RUN THIS ON 10.10.50.34  (the SoT / control host — same place as
#   deploy-from-34.sh; it has key-only SSH to every hub).
#
# Why this exists
# ---------------
# codegen-loop / rerun jobs are HUB-orchestrated: the hub spawns one
# ephemeral `docker run paprika-runner:latest` container per attempt
# (server/hub/runner.py::execute_in_sandbox) to run the LLM-generated
# paprika-client script. That image is a BUILD-ONLY compose service
# (docker-compose.yml `runner`, profile build-only) — it is never pushed
# to a registry, so `docker run` can't pull it. It must already exist on
# each hub host's docker daemon.
#
# deploy-from-34.sh only rsyncs server/ + core/ and restarts containers —
# it NEVER (re)builds or distributes this image. So a hub that is new,
# rebuilt, or was simply missed by the original one-off manual build ends
# up WITHOUT the image, and every codegen-loop attempt it orchestrates
# dies with:
#     Unable to find image 'paprika-runner:latest' locally
#     docker: Error response from daemon: pull access denied ...
#     -> exit_code 125, actions_count 0
# (2026-07-27: this bit hub-41 — the known orphan hub — and failed job
#  d4dc663b9833. Six of seven hubs had the image; hub-41 did not.)
#
# What this does
# --------------
# Idempotent convergence: read every hub's current runner image id, pick
# the fleet "golden" id (the one the most hubs already run), and replicate
# it (docker save | gzip | docker load) onto any hub that is MISSING it or
# running a DIFFERENT id. No build needed on the target — parity with the
# rest of the fleet is preserved byte-for-byte. A no-op (seconds) once the
# fleet is converged, so it is safe to call on every deploy.
#
# Cold start (NO hub has the image at all — only the very first deploy):
# builds once on .34, which is the only host that carries the build
# context (docker/runner/Dockerfile + client/python), then fans out.
#
# Flags (env): DRY_RUN=1   (report only, no save/load/build)
#              RUNNER_IMAGE=paprika-runner:latest   (override tag)
set -uo pipefail

IMAGE="${RUNNER_IMAGE:-paprika-runner:latest}"
SSHO=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)
say(){ printf '%s\n' "$*"; }

# Hub discovery — identical source of truth to deploy-from-34.sh: the
# Redis hub-presence registry UNION the static core, minus .34.
_disc_hubs() {
  docker exec paprika-redis-1 redis-cli --scan --pattern 'paprika:hubs:*' 2>/dev/null | grep -vE ':index$' | while read -r _k; do
    docker exec paprika-redis-1 redis-cli GET "$_k" 2>/dev/null | grep -oE '"ip"[[:space:]]*:[[:space:]]*"10\.10\.50\.[0-9]+"' | grep -oE '10\.10\.50\.[0-9]+'
  done
}
HUBS=($(printf '%s\n' 10.10.50.35 10.10.50.36 10.10.50.37 10.10.50.38 10.10.50.39 10.10.50.40 10.10.50.41 $(_disc_hubs) | grep -E '^10\.10\.50\.[0-9]+$' | grep -vx 10.10.50.34 | sort -u))
[ "${#HUBS[@]}" -eq 0 ] && { say "!! no hubs discovered — aborting"; exit 1; }

say "==> ensure '$IMAGE' on ${#HUBS[@]} hub(s): ${HUBS[*]}${DRY_RUN:+  [DRY-RUN]}"

# -- 1) inventory: current image id per hub ------------------------------------
declare -A HAVE          # ip -> image id ("" if missing/unreachable)
declare -A IDCOUNT       # image id -> how many hubs run it
for H in "${HUBS[@]}"; do
  id=$(ssh "${SSHO[@]}" "root@$H" "docker image inspect -f '{{.Id}}' '$IMAGE' 2>/dev/null" 2>/dev/null | tr -d '\r')
  HAVE[$H]="$id"
  if [ -n "$id" ]; then
    IDCOUNT[$id]=$(( ${IDCOUNT[$id]:-0} + 1 ))
    short=${id#sha256:}; say "    $H : ${short:0:16}"
  else
    say "    $H : (missing)"
  fi
done

# -- 2) pick the golden id (held by the most hubs) -----------------------------
GOLDEN=""; GOLDEN_N=0
for id in "${!IDCOUNT[@]}"; do
  if [ "${IDCOUNT[$id]}" -gt "$GOLDEN_N" ]; then GOLDEN="$id"; GOLDEN_N="${IDCOUNT[$id]}"; fi
done

# Cold start: nobody has it -> build once on .34 (only host with build context).
if [ -z "$GOLDEN" ]; then
  say "==> no hub has '$IMAGE' — cold-start build on .34 (build context lives here)"
  if [ -n "${DRY_RUN:-}" ]; then
    say "    [DRY-RUN] would: cd /opt/paprika && docker build -f docker/runner/Dockerfile -t '$IMAGE' ."
    exit 0
  fi
  if [ ! -f /opt/paprika/docker/runner/Dockerfile ] || [ ! -d /opt/paprika/client/python ]; then
    say "!! build context missing on .34 (docker/runner/Dockerfile + client/python). Build manually on a host that has it, then re-run."
    exit 1
  fi
  ( cd /opt/paprika && docker build -f docker/runner/Dockerfile -t "$IMAGE" . ) \
    || { say "!! cold-start build failed on .34"; exit 1; }
  GOLDEN=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null | tr -d '\r')
  # .34 is the source now; treat it as an implicit golden holder below.
  SRC_HOST=".34-local"
  say "    built golden ${GOLDEN#sha256:}"
fi

# Find a hub that already holds golden (the replication source). If we
# cold-built, source is .34-local (handled specially in the copy step).
SRC_HOST="${SRC_HOST:-}"
if [ -z "$SRC_HOST" ]; then
  for H in "${HUBS[@]}"; do
    [ "${HAVE[$H]}" = "$GOLDEN" ] && { SRC_HOST="$H"; break; }
  done
fi
say "==> golden = ${GOLDEN#sha256:} (on $GOLDEN_N hub(s)); replication source = $SRC_HOST"

# -- 3) converge: replicate golden onto every missing/mismatched hub -----------
changed=0; failed=0
for H in "${HUBS[@]}"; do
  [ "${HAVE[$H]}" = "$GOLDEN" ] && continue   # already golden
  reason="missing"; [ -n "${HAVE[$H]}" ] && reason="mismatch (${HAVE[$H]#sha256:})"
  say "==> $H needs golden ($reason) -> replicating from $SRC_HOST"
  if [ -n "${DRY_RUN:-}" ]; then changed=$((changed+1)); continue; fi
  if [ "$SRC_HOST" = ".34-local" ]; then
    docker save "$IMAGE" | gzip -1 | ssh "${SSHO[@]}" "root@$H" "gunzip | docker load" >/dev/null 2>&1
  else
    ssh "${SSHO[@]}" "root@$SRC_HOST" "docker save '$IMAGE' | gzip -1" \
      | ssh "${SSHO[@]}" "root@$H" "gunzip | docker load" >/dev/null 2>&1
  fi
  newid=$(ssh "${SSHO[@]}" "root@$H" "docker image inspect -f '{{.Id}}' '$IMAGE' 2>/dev/null" 2>/dev/null | tr -d '\r')
  if [ "$newid" = "$GOLDEN" ]; then
    say "    OK — $H now runs golden"; changed=$((changed+1))
  else
    say "    !! FAILED — $H still not golden (got '${newid:-none}')"; failed=$((failed+1))
  fi
done

say "==> done: $changed converged, $failed failed, $(( ${#HUBS[@]} - changed - failed )) already-golden"
[ "$failed" -eq 0 ]
