#!/bin/sh
# Worker egress firewall.
#
# Drops outbound connections from the worker container to private
# IP ranges (RFC1918, link-local incl. cloud metadata, loopback,
# IPv6 ULA) EXCEPT for the explicitly-allowed hub + agent
# destinations the worker actually needs.
#
# This is the "deep" defence layer in paprika's SSRF protection.
# The hub already validates submitted URLs at POST time (see
# server/hub/url_safety.py) but a malicious script inside the
# runner sandbox or a redirect / window.location / fetch() call
# inside the rendered page can navigate to private IPs that the
# hub never saw. iptables in the OUTPUT chain catches all of
# those because the kernel drops the SYN before any data leaves
# the container.
#
# Opt-in via env var so existing deployments aren't broken:
#
#   PAPRIKA_WORKER_EGRESS_FIREWALL=1  -> install + enforce
#   PAPRIKA_WORKER_EGRESS_FIREWALL=0  -> skip (default; backwards
#                                       compatible)
#
# Requires:
#   * iptables binary in the image  (Dockerfile adds it)
#   * --cap-add=NET_ADMIN on the container  (compose adds it)
#
# Failure modes:
#   * iptables not installed -> exit 1, refuse to start the worker
#     (operator opted in but the image isn't ready)
#   * NET_ADMIN missing      -> iptables -A returns nonzero; we
#     exit 1 with a clear hint about cap_add
#   * hub/agent hostname doesn't resolve -> log warning, continue
#     anyway. Worker will fail to register over WS, which is the
#     normal "hub not reachable" error path -- operator sees the
#     same symptom they'd see without the firewall.

set -eu

# ----------------------------------------------------------------------
# Opt-out short-circuit. Default is OFF for backward compatibility:
# existing deployments don't get a behaviour change unless they
# explicitly enable it (typically via the demo / public-facing hub
# stack).
# ----------------------------------------------------------------------
if [ "${PAPRIKA_WORKER_EGRESS_FIREWALL:-0}" != "1" ]; then
  exit 0
fi

# ----------------------------------------------------------------------
# Sanity checks before we touch iptables.
# ----------------------------------------------------------------------
if ! command -v iptables >/dev/null 2>&1; then
  echo "[egress-firewall] iptables not found in PATH; image needs apt install iptables" >&2
  echo "[egress-firewall] PAPRIKA_WORKER_EGRESS_FIREWALL=1 was requested but cannot be honoured" >&2
  exit 1
fi

# A quick capability probe: try to list OUTPUT chain. If we lack
# NET_ADMIN, this fails with "Permission denied" and we can surface a
# friendly hint instead of a cryptic permission error mid-rule.
if ! iptables -L OUTPUT -n >/dev/null 2>&1; then
  echo "[egress-firewall] iptables -L OUTPUT failed -- container probably missing CAP_NET_ADMIN" >&2
  echo "[egress-firewall] Add  cap_add: ['NET_ADMIN']  to the worker service in docker-compose.yml" >&2
  exit 1
fi

# Flush any prior rules so successive entrypoint runs (e.g. after a
# version-mismatch self-restart) don't pile up duplicates.
iptables -F OUTPUT 2>/dev/null || true

# Allow lo (loopback) -- inter-process talk inside the container,
# Chrome remote-debugging on 127.0.0.1, etc.
iptables -A OUTPUT -o lo -j ACCEPT

# Allow ALREADY-established connections to return data. Without this
# every TCP handshake would race the firewall (we'd drop the ACK).
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

# Docker's embedded resolver lives on 127.0.0.11 -- needed for hub /
# agent hostname lookup AND for any DNS the worker's Chrome does
# (almost everything). Allow explicitly.
iptables -A OUTPUT -d 127.0.0.11 -j ACCEPT

# Allow well-known public DNS resolvers too, in case the container
# is configured to bypass the docker resolver (custom resolv.conf
# / --dns flags). 8.8.8.8 / 1.1.1.1 / 9.9.9.9 covers the usual set.
for dns in 8.8.8.8 8.8.4.4 1.1.1.1 1.0.0.1 9.9.9.9; do
  iptables -A OUTPUT -d "$dns" -p udp --dport 53 -j ACCEPT
  iptables -A OUTPUT -d "$dns" -p tcp --dport 53 -j ACCEPT
done

# Resolve hub / agent / any operator-specified hostnames + allow them.
# These are the legitimate private-IP destinations -- they live on
# the docker network and the worker MUST reach them.
ALLOW_HOSTS="hub agent ${PAPRIKA_FIREWALL_ALLOW_HOSTS:-}"
for h in $ALLOW_HOSTS; do
  # getent walks /etc/hosts then DNS. Comments out IPv6 line if the
  # resolver chokes -- 'echo' suppresses the propagation of getent's
  # exit code when the host is missing.
  for ip in $(getent ahosts "$h" 2>/dev/null | awk 'NF>=1 {print $1}' | sort -u); do
    [ -z "$ip" ] && continue
    echo "[egress-firewall] allow $h -> $ip"
    iptables -A OUTPUT -d "$ip" -j ACCEPT
  done
done

# Operator-specified raw IPs (CIDR or single addr) get an explicit
# ACCEPT rule above the DROP block. Comma-separated.
if [ -n "${PAPRIKA_FIREWALL_ALLOW_IPS:-}" ]; then
  echo "$PAPRIKA_FIREWALL_ALLOW_IPS" | tr ',' '\n' | while read -r cidr; do
    [ -z "$cidr" ] && continue
    echo "[egress-firewall] allow CIDR $cidr"
    iptables -A OUTPUT -d "$cidr" -j ACCEPT
  done
fi

# DROP private + loopback + link-local ranges. Order matters: these
# come AFTER the allow-list above so legitimate hub / agent traffic
# slips through first. Anything not matched by the explicit allows
# above and that falls inside one of these CIDRs is killed.
DROP_CIDRS="
  10.0.0.0/8
  172.16.0.0/12
  192.168.0.0/16
  169.254.0.0/16
  127.0.0.0/8
  100.64.0.0/10
"
for cidr in $DROP_CIDRS; do
  echo "[egress-firewall] drop $cidr"
  iptables -A OUTPUT -d "$cidr" -j DROP
done

# Default policy stays ACCEPT so public-internet traffic isn't
# blocked. The explicit DROPs above handle the private ranges.
iptables -P OUTPUT ACCEPT

# IPv6: same story. Skip silently if ip6tables isn't usable
# (e.g. IPv6 disabled in the kernel -- common on cloud VMs).
if command -v ip6tables >/dev/null 2>&1 && ip6tables -L OUTPUT >/dev/null 2>&1; then
  ip6tables -F OUTPUT 2>/dev/null || true
  ip6tables -A OUTPUT -o lo -j ACCEPT
  ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
  # Drop ULA / link-local / loopback.
  for cidr in fc00::/7 fe80::/10 ::1/128; do
    echo "[egress-firewall] drop IPv6 $cidr"
    ip6tables -A OUTPUT -d "$cidr" -j DROP
  done
  ip6tables -P OUTPUT ACCEPT
fi

echo "[egress-firewall] active. Rule summary:"
iptables -L OUTPUT -n --line-numbers 2>&1 | head -30 >&2
