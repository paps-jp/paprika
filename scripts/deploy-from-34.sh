#!/bin/bash
# deploy-from-34.sh — propagate the canonical paprika source from the .34
# control host to all 3 production hubs, then converge the worker fleet.
#
#   RUN THIS ON 10.10.50.34  (the SoT / control host: nginx front + redis +
#   reconciler live here; it has key-only SSH to every hub AND worker).
#
# Model: .34's /opt/paprika/{server,core} is the SINGLE canonical tree. This
# script pushes it to hub-35/36/37 ATOMICALLY (all files land before any
# restart -> no multi-hub version sawtooth, the documented footgun), then
# restarts only what the change requires.
#
# Change classification is CONTENT-hash based (NOT a fragile rsync-itemize
# parse), comparing .34 to a reference hub BEFORE the sync:
#   worker_hash = all .py EXCEPT server/hub/** + scheduler.py  (== _version.py
#                 worker hash). If it changed -> workers must self-update.
#   hub_hash    = all .py EXCEPT server/worker/**  (code the hub process runs).
#                 If it changed -> the hub must restart to re-import.
# A hub restart briefly drops its workers' WS and the nginx consistent-hash
# re-homes some elsewhere ("workerless hub -> 503"), so a rolling WORKER restart
# follows to both converge AND re-home evenly.
#
# Flags (env): DRY_RUN=1  SKIP_WORKERS=1  SKIP_HUBS=1  FORCE_HUBS=1  FORCE_REBALANCE=1
set -uo pipefail

SRC=/opt/paprika
# Deploy targets: AUTO-DISCOVERED from the Redis hub-presence registry
# (paprika:hubs:*, TTL-refreshed by each hub's _hubs.py -- the SAME source of
# truth the nginx upstream reconciler uses), UNION the static core. So a hub
# that auto-joins nginx ALSO auto-receives deploys, and a Redis blip can never
# drop a core hub or deploy to zero. Excludes .34 (SoT/router -- it appears in
# the presence payload's public_base, NOT as a hub ip). sort -u => HUBS[0] is a
# stable, low-numbered, in-sync hub for the classify reference below.
_disc_hubs() {
  docker exec paprika-redis-1 redis-cli --scan --pattern 'paprika:hubs:*' 2>/dev/null | grep -vE ':index$' | while read -r _k; do
    docker exec paprika-redis-1 redis-cli GET "$_k" 2>/dev/null | grep -oE '"ip"[[:space:]]*:[[:space:]]*"10\.10\.50\.[0-9]+"' | grep -oE '10\.10\.50\.[0-9]+'
  done
}
HUBS=($(printf '%s\n' 10.10.50.35 10.10.50.36 10.10.50.37 $(_disc_hubs) | grep -E '^10\.10\.50\.[0-9]+$' | grep -vx 10.10.50.34 | sort -u))
[ "${#HUBS[@]}" -eq 0 ] && HUBS=(10.10.50.35 10.10.50.36 10.10.50.37)
HUB_CONTAINER=hub-hub-a-1
FRONT=http://127.0.0.1:8000
SSHO=(-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)
BATCH="${BATCH:-5}"; BATCH_GAP="${BATCH_GAP:-16}"
say(){ printf '%s\n' "$*"; }

[ -d "$SRC/server" ] && [ -d "$SRC/core" ] || { say "!! $SRC/server|core missing — run this ON 10.10.50.34"; exit 1; }
SRC_HASH=$(cd "$SRC" && find server core -name '*.py' | sort | xargs md5sum | md5sum | cut -d' ' -f1)
say "==> canonical source (.34): server+core md5 = $SRC_HASH"

# -- 1) classify by category content-hash (.34 vs reference hub, pre-sync) ------
CATHASH_SH='cd /opt/paprika || exit 1
w=$(find server core -name "*.py" ! -path "server/hub/*" ! -name "scheduler.py" | sort | xargs md5sum | md5sum | cut -d" " -f1)
h=$(find server core -name "*.py" ! -path "server/worker/*" | sort | xargs md5sum | md5sum | cut -d" " -f1)
echo "$w $h"'
read -r W_NEW H_NEW < <(printf '%s\n' "$CATHASH_SH" | bash)
read -r W_OLD H_OLD < <(printf '%s\n' "$CATHASH_SH" | ssh "${SSHO[@]}" "root@${HUBS[0]}" bash)
worker_needed=0; hub_needed=0
[ "$W_NEW" != "$W_OLD" ] && worker_needed=1
[ "$H_NEW" != "$H_OLD" ] && hub_needed=1
[ -n "${FORCE_HUBS:-}" ] && hub_needed=1
[ -n "${FORCE_REBALANCE:-}" ] && worker_needed=1
say "==> [1] classify (vs ${HUBS[0]}):"
say "      worker_changed=$worker_needed   (worker_hash $W_OLD -> $W_NEW)"
say "      hub_changed=$hub_needed   (hub_hash    $H_OLD -> $H_NEW)"

# -- 2) rsync .34 -> all 3 hubs (parallel, content-based, atomic before restart)
RSYNC_OPTS=(-rlpz --checksum --delete --exclude=__pycache__/ --exclude='*.pyc' --exclude='*.log')
[ -n "${DRY_RUN:-}" ] && RSYNC_OPTS+=(--dry-run)
say "==> [2] rsync .34 -> ${HUBS[*]}$([ -n "${DRY_RUN:-}" ] && echo '  [DRY-RUN: no writes]')"
for H in "${HUBS[@]}"; do
  ( for sub in server core; do rsync "${RSYNC_OPTS[@]}" "$SRC/$sub/" "root@$H:/opt/paprika/$sub/" >/dev/null 2>&1; done; say "    synced -> $H" ) &
done
wait

if [ -n "${DRY_RUN:-}" ]; then say "DRY_RUN: stopping before any restart."; exit 0; fi
if [ "$worker_needed" = 0 ] && [ "$hub_needed" = 0 ]; then say "==> nothing changed; verifying only."; fi

# -- 3) rolling hub restart (only if hub code changed / forced). The hub's
#       uvicorn graceful-shutdown HANGS waiting for long-lived worker WS to
#       close, so force a quick SIGKILL (-t 8) and GATE on /health == 200
#       before the next hub. A hub that doesn't come back ABORTS the deploy --
#       never silently leave the fleet a hub short (this bit us: hub-35 hung in
#       "Waiting for connections to close" and the old `&& say` swallowed it).
hubs_restarted=0
if [ "$hub_needed" = 1 ] && [ -z "${SKIP_HUBS:-}" ]; then
  say "==> [3] hub code changed -> rolling hub restart (gated on /health)"
  for H in "${HUBS[@]}"; do
    say "    restarting hub $H ..."
    ssh "${SSHO[@]}" "root@$H" "docker restart -t 8 $HUB_CONTAINER >/dev/null 2>&1" \
      || say "    (docker restart returned non-zero on $H; checking /health anyway)"
    healthy=0
    for _ in $(seq 1 20); do
      h=$(ssh "${SSHO[@]}" "root@$H" "curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://localhost:8100/health" 2>/dev/null)
      [ "$h" = "200" ] && { healthy=1; break; }
      sleep 2
    done
    if [ "$healthy" = 1 ]; then say "    hub $H healthy"; else
      say "    !! hub $H NOT /health 200 after restart -- ABORTING. Fix $H ('ssh root@$H docker restart hub-hub-a-1') then re-run. Other hubs untouched."
      exit 1
    fi
  done
  hubs_restarted=1
else
  say "==> [3] no hub restart (hubs auto re-hash served version within ~30s)"
fi

# -- 4) converge + rebalance workers (rolling, batched)
# Worker-code-only changes (worker_needed=1, no hub restart) NO LONGER force a
# hard `docker restart` here -- that SIGKILLs workers mid-job and kills any
# in-flight fetch/session ("worker disconnected before the job finished"). The
# hub picks up the new worker source within ~30s (_hub_version mtime-TTL) and
# advertises it via HubExpectedVersion on heartbeat; each worker then runs its
# GRACEFUL rolling drain+self-update (drains in-flight up to
# PAPRIKA_DRAIN_DEADLINE_S=600s, then exit 42 -> supervisor relaunch). We only
# hard-restart workers to REBALANCE after a hub restart (hubs_restarted=1).
# Escape hatch: FORCE_WORKER_RESTART=1 restores the old immediate hard restart.
need_workers=0
[ "$hubs_restarted" = 1 ] && need_workers=1
[ -n "${FORCE_WORKER_RESTART:-}" ] && [ "$worker_needed" = 1 ] && need_workers=1
if [ "$need_workers" = 1 ] && [ -z "${SKIP_WORKERS:-}" ]; then
  say "==> [4] rolling-restart workers (converge + rebalance), batch=$BATCH gap=${BATCH_GAP}s"
  sleep 8
  IPS=$(curl -fsS --max-time 10 "$FRONT/workers/hosts" \
        | python3 -c 'import json,sys;print(" ".join(h["address"] for h in json.load(sys.stdin).get("hosts",[]) if h.get("address")))')
  n=0
  for ip in $IPS; do
    ssh "${SSHO[@]}" "root@$ip" "docker restart paprika-worker-1 >/dev/null 2>&1" &
    n=$((n+1)); if [ $((n % BATCH)) -eq 0 ]; then wait; say "    ... $n restarted"; sleep "$BATCH_GAP"; fi
  done
  wait
  say "    all $n workers restarted; waiting 100s for reconnect + lane respawn"; sleep 100
elif [ "$worker_needed" = 1 ]; then
  say "==> [4] worker code changed -> NO hard restart; graceful heartbeat self-update drains in-flight + converges in ~10min (FORCE_WORKER_RESTART=1 to override)"
else
  say "==> [4] no worker convergence/rebalance needed"
fi

# -- 5) verify -- POLL until settled (the fleet needs time to reconnect +
#       respawn lanes after a restart; a one-shot verify fires too early and
#       reports spurious 503s / empty versions).
say "==> [5] verify (polling until settled, up to ~2min)"
settled=0
for attempt in $(seq 1 12); do
  vers=""
  for H in "${HUBS[@]}"; do
    v=$(ssh "${SSHO[@]}" "root@$H" "curl -s --max-time 8 -D - -o /dev/null http://localhost:8100/worker-source.tar.gz | tr -d '\r' | grep -i x-paprika-version | cut -d' ' -f2" 2>/dev/null)
    vers="$vers ${v:-EMPTY}"
  done
  nuniq=$(printf '%s\n' $vers | sort -u | grep -c .)
  elig=$(curl -fsS --max-time 10 "$FRONT/workers" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);ws=d.get("workers") or [];L=lambda w:len(w.get("lane_novnc_urls") or []);print(sum(1 for w in ws if w.get("alive") and w.get("status")=="active" and (w.get("in_flight") or 0)<(w.get("capacity") or 0) and L(w)>0))' 2>/dev/null || echo 0)
  if [ "$nuniq" = 1 ] && ! printf '%s' "$vers" | grep -q EMPTY && [ "${elig:-0}" -ge 6 ]; then
    say "    settled: all hubs serve$(printf '%s' "$vers" | awk '{print " "$1}'), eligible=$elig"; settled=1; break
  fi
  say "    settling (attempt $attempt): hubs='$vers' eligible=$elig"; sleep 10
done
[ "$settled" = 1 ] || say "    !! did NOT settle in ~2min -- inspect hubs (served: $vers) + fleet manually"
curl -fsS --max-time 12 "$FRONT/workers" | python3 -c '
import sys,json
from collections import Counter
d=json.load(sys.stdin); ws=d.get("workers") or []
L=lambda w: len(w.get("lane_novnc_urls") or [])
elig=sum(1 for w in ws if w.get("alive") and w.get("status")=="active" and (w.get("in_flight") or 0)<(w.get("capacity") or 0) and L(w)>0)
print("    fleet: connected=%d alive=%d eligible=%d by_hub=%s"%(len(ws),sum(1 for w in ws if w.get("alive")),elig,dict(Counter(w.get("hub_id") for w in ws))))'
ok=0; bad=0
for i in 1 2 3 4; do
  out=$(curl -s --max-time 15 -w 'H:%{http_code}' -X POST "$FRONT/jobs" -H 'Content-Type: application/json' \
        -d '{"url":"https://example.com","options":{"mode":"fetch","scroll":false,"capture_assets":false,"max_wait_seconds":15}}')
  code=$(printf '%s' "$out" | grep -oE 'H:[0-9]+' | cut -d: -f2)
  jid=$(printf '%s' "$out" | python3 -c 'import sys,json;s=sys.stdin.read().split("H:")[0];print(json.loads(s).get("job_id","") if s.strip().startswith("{") else "")' 2>/dev/null || true)
  [ "$code" = 200 ] && ok=$((ok+1)) || bad=$((bad+1))
  [ -n "$jid" ] && curl -s -X POST "$FRONT/jobs/$jid/cancel" >/dev/null 2>&1
done
say "    submit probe: 200=$ok 503/err=$bad"
say "✓ deploy-from-34 complete (src $SRC_HASH)"
