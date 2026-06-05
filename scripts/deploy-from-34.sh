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
HUBS=(10.10.50.35 10.10.50.36 10.10.50.37)
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

# -- 3) rolling hub restart (only if hub code changed / forced)
hubs_restarted=0
if [ "$hub_needed" = 1 ] && [ -z "${SKIP_HUBS:-}" ]; then
  say "==> [3] hub code changed -> rolling hub restart (sequential keeps 2/3 serving)"
  for H in "${HUBS[@]}"; do
    ssh "${SSHO[@]}" "root@$H" "docker restart -t 20 $HUB_CONTAINER >/dev/null 2>&1" && say "    restarted hub $H"
  done
  hubs_restarted=1
else
  say "==> [3] no hub restart (hubs auto re-hash served version within ~30s)"
fi

# -- 4) converge + rebalance workers (rolling, batched)
need_workers=0; [ "$worker_needed" = 1 ] && need_workers=1; [ "$hubs_restarted" = 1 ] && need_workers=1
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
else
  say "==> [4] no worker convergence/rebalance needed"
fi

# -- 5) verify
say "==> [5] verify"
declare -A seen=()
for H in "${HUBS[@]}"; do
  v=$(ssh "${SSHO[@]}" "root@$H" "curl -s -D - -o /dev/null http://localhost:8100/worker-source.tar.gz | tr -d '\r' | grep -i x-paprika-version | cut -d' ' -f2")
  say "    hub $H serves: $v"; seen[$v]=1
done
[ "${#seen[@]}" -gt 1 ] && say "    !! WARNING: hubs serve DIFFERENT versions (${!seen[*]}) — re-run with FORCE_HUBS=1"
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
