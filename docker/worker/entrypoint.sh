#!/bin/sh
set -e

# Egress firewall first, before any python / chrome process can dial
# outward. Opt-in via PAPRIKA_WORKER_EGRESS_FIREWALL=1. No-op when
# disabled. Sourced rather than exec'd so a failure (e.g. missing
# CAP_NET_ADMIN when the operator requested the feature) kills the
# worker boot rather than silently proceeding.
if [ -x /entrypoint-egress-firewall.sh ]; then
  /entrypoint-egress-firewall.sh
fi

# Auto-detect NOVNC_PUBLIC_HOST if unset or "auto":
# - with --network host:  hostname returns the host machine's name
# - with --hostname X:    returns X
# - otherwise:            returns the container id hash → not useful externally,
#                         caller should pass NOVNC_PUBLIC_HOST explicitly
if [ -z "$NOVNC_PUBLIC_HOST" ] || [ "$NOVNC_PUBLIC_HOST" = "auto" ]; then
  NOVNC_PUBLIC_HOST=$(hostname 2>/dev/null || echo localhost)
fi

# Honour LANE_POOL (new) with SLOT_POOL kept as a deprecated alias so
# existing .env files keep working through the Slot -> Lane rename.
N_LANES="${LANE_POOL:-${SLOT_POOL:-2}}"

echo "[entrypoint] HUB_URL=$HUB_URL  NOVNC_PUBLIC_HOST=$NOVNC_PUBLIC_HOST  LANE_POOL=$N_LANES"

exec python -m server --mode worker \
  --hub-url "${HUB_URL:-ws://paprika.lan:8000}" \
  ${WORKER_ID:+--worker-id "$WORKER_ID"} \
  --lane-pool "$N_LANES" \
  --max-concurrent "${MAX_CONCURRENT:-2}" \
  --novnc-public-host "$NOVNC_PUBLIC_HOST" \
  --novnc-base-port "${NOVNC_BASE_PORT:-6080}" \
  ${LABELS:+--labels "$LABELS"}
