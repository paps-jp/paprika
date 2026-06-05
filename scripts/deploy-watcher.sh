#!/bin/bash
# paprika deploy-watcher — Phase 2 of the .34-as-SoT deploy model.
#
#   RUNS ON 10.10.50.34. Watches /opt/paprika/{server,core}; when the source
#   CONTENT changes and then stays stable for DEBOUNCE_S (= "the edit is
#   finished"), it runs deploy-from-34.sh to propagate to all 3 hubs and
#   converge the worker fleet. Debounced (no mid-edit deploys), flock-guarded
#   (never overlaps a manual deploy), and content-based (mtime-only touches are
#   ignored — same logic as deploy-from-34.sh's --checksum).
#
#   Enable:  systemctl enable --now paprika-deploy-watcher
#   Pause:   systemctl stop  paprika-deploy-watcher     # back to manual deploys
#   Logs:    journalctl -u paprika-deploy-watcher -f
#
# Workflow once this is live: edit /opt/paprika/{server,core} ON .34 ONLY
# (never edit the hubs directly), and the change auto-deploys ~DEBOUNCE_S later.
set -uo pipefail
SRC=/opt/paprika
INTERVAL_S="${INTERVAL_S:-15}"     # source-hash poll cadence
DEBOUNCE_S="${DEBOUNCE_S:-30}"     # require the hash stable this long before deploying
DEPLOY="$SRC/scripts/deploy-from-34.sh"
LOCK=/run/paprika-deploy.lock

srchash(){ cd "$SRC" 2>/dev/null && find server core -name '*.py' 2>/dev/null | sort | xargs md5sum 2>/dev/null | md5sum | cut -d' ' -f1; }
log(){ printf '%s deploy-watcher: %s\n' "$(date -u +%FT%TZ)" "$*"; }

[ -x "$DEPLOY" ] || { log "FATAL: $DEPLOY missing/not executable"; exit 1; }
last="$(srchash)"; dirty=0
log "armed (baseline=$last interval=${INTERVAL_S}s debounce=${DEBOUNCE_S}s). Edit /opt/paprika/{server,core} on THIS host; changes auto-propagate."
while true; do
  sleep "$INTERVAL_S"
  cur="$(srchash)"
  [ -z "$cur" ] && { log "hash compute failed; skipping this cycle"; continue; }
  if [ "$cur" != "$last" ]; then
    log "change detected: $last -> $cur (debouncing ${DEBOUNCE_S}s)"
    last="$cur"; dirty=$(date +%s); continue
  fi
  if [ "$dirty" != 0 ] && [ $(( $(date +%s) - dirty )) -ge "$DEBOUNCE_S" ]; then
    log "source stable ${DEBOUNCE_S}s at $cur -> running deploy-from-34.sh"
    flock -n "$LOCK" bash "$DEPLOY" 2>&1 | sed 's/^/  | /'
    rc=${PIPESTATUS[0]:-?}
    log "deploy finished (rc=$rc); new baseline=$(srchash)"
    last="$(srchash)"; dirty=0
  fi
done
