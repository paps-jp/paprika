#!/bin/bash
#
# Install paprika-worker-housekeep on a worker CT (or many of them).
# Idempotent -- safe to re-run; replaces existing units in place.
#
# Usage:
#   # Install on a single worker:
#   bash scripts/install-worker-housekeep.sh 10.10.50.150
#
#   # Install on every connected worker (queries hub for the IP list):
#   bash scripts/install-worker-housekeep.sh --all
#
# Requires SSH access (id_paprika key, root login) -- the same path paprika
# already uses for worker code distribution.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOUSEKEEP_SH="$SCRIPT_DIR/worker-housekeep.sh"

# Default SSH key. Override with SSH_KEY=... env var.
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_paprika}"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 -i $SSH_KEY"

if [ ! -f "$HOUSEKEEP_SH" ]; then
    echo "missing: $HOUSEKEEP_SH" >&2
    exit 1
fi

install_one() {
    local ip="$1"
    echo "=== installing on $ip ==="

    # Push the housekeep script to /usr/local/sbin.
    scp $SSH_OPTS "$HOUSEKEEP_SH" "root@$ip:/usr/local/sbin/paprika-worker-housekeep" >/dev/null
    ssh $SSH_OPTS "root@$ip" "chmod 0755 /usr/local/sbin/paprika-worker-housekeep"

    # Write the systemd unit + timer in one shot. OnCalendar=daily picks a
    # randomized time within the day (RandomizedDelaySec) so the fleet doesn't
    # all prune at once and saturate the IO subsystem on each Proxmox node.
    ssh $SSH_OPTS "root@$ip" 'cat > /etc/systemd/system/paprika-worker-housekeep.service' <<'UNIT'
[Unit]
Description=Paprika worker disk housekeep (prune stale containerd snapshots)
Documentation=https://github.com/paps-jp/paprika

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/paprika-worker-housekeep
StandardOutput=journal
StandardError=journal
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
UNIT

    ssh $SSH_OPTS "root@$ip" 'cat > /etc/systemd/system/paprika-worker-housekeep.timer' <<'UNIT'
[Unit]
Description=Daily paprika worker disk housekeep
Documentation=https://github.com/paps-jp/paprika

[Timer]
OnBootSec=15min
OnCalendar=daily
RandomizedDelaySec=4h
Persistent=true
Unit=paprika-worker-housekeep.service

[Install]
WantedBy=timers.target
UNIT

    ssh $SSH_OPTS "root@$ip" '
systemctl daemon-reload
systemctl enable --now paprika-worker-housekeep.timer >/dev/null
echo "  installed. next run:"
systemctl list-timers paprika-worker-housekeep.timer --no-pager 2>&1 | head -3 | tail -1
'
}

if [ "${1:-}" = "--all" ]; then
    # Pull the live fleet IP list from the hub's /workers endpoint.
    hub="${PAPRIKA_HUB:-10.10.50.34:8000}"
    ips=$(curl -s "http://$hub/workers" | python3 -c '
import sys,json
for w in json.load(sys.stdin).get("workers",[]):
    print(w["address"])
' | sort -u)
    if [ -z "$ips" ]; then
        echo "no workers found via http://$hub/workers" >&2
        exit 1
    fi
    n=$(echo "$ips" | wc -l)
    echo "installing on $n workers..."
    for ip in $ips; do
        install_one "$ip" || echo "  WARN: install failed on $ip (continuing)"
    done
else
    if [ $# -eq 0 ]; then
        echo "usage: $0 <worker-ip> | --all" >&2
        exit 2
    fi
    for ip in "$@"; do
        install_one "$ip"
    done
fi

echo "done."
