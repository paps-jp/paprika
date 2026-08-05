#!/bin/bash
#
# paprika worker housekeep: prune accumulated containerd snapshots + stale
# resources so the worker CT's root filesystem never fills up.
#
# Background: each worker self-update (HubExpectedVersion -> pull new layer)
# leaves the previous image's containerd snapshot in /var/lib/containerd
# uncollected. Over weeks this builds to 30G+ per CT (each Chrome layer ~262M,
# libLLVM ~124M, plus base layers). When /var fills to 100%, Xvfb can't write
# its lockfile -- the worker enters a multi-thousand-restart loop without ever
# being able to log a useful error (2026-06-06: w11 with 543 restarts, w18
# with 1979 restarts; both unrecoverable without manual disk cleanup).
#
# Pruning is safe: --filter "until=72h" excludes anything used by a running
# container, AND only touches images last referenced >72h ago. The active
# worker image (referenced now) is never eligible.
#
# Installed as a systemd timer (see scripts/install-worker-housekeep.sh).
# Run frequency: hourly. Log: journalctl -u paprika-worker-housekeep.
#
# Why hourly and not daily: measured leak on loft 2026-08-05 is ~19G/day/CT
# against a 32G rootfs, so a single CT walks from 15% to 78% between two daily
# runs. And the CT's own df is the WRONG gauge anyway -- 20 CTs share one
# LVM-thin pool, so the fleet allocates far faster than any single CT looks
# full. This script cannot see the pool; scripts/thinpool-guard.sh (installed
# on the PVE node, not the CT) is the layer that can.

set -euo pipefail

LOG_PREFIX="[paprika-housekeep]"
log() { echo "$LOG_PREFIX $*"; }

before_used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
before_free=$(df -h --output=avail / | tail -1 | tr -d ' ')
log "before: disk ${before_used}% used, ${before_free} free"

# Prune dangling images / build cache / stopped containers / unused networks.
# --filter "until=72h" keeps anything touched in the last 3 days (covers a
# typical deploy + warm-up window). Errors are tolerated so a transient docker
# hiccup doesn't break the timer.
docker image prune -af --filter "until=72h" 2>&1 | tail -3 || log "image prune skipped (docker not ready?)"
docker builder prune -af --filter "until=72h" 2>&1 | tail -3 || true
docker container prune -f --filter "until=72h" 2>&1 | tail -3 || true

# Rotate docker container json logs that exceeded 100M (the daemon's per-file
# cap is configurable but most worker CTs were provisioned without log-opts,
# so a single worker can accumulate hundreds of MB of stdout per week).
find /var/lib/docker/containers -name '*-json.log' -size +100M 2>/dev/null | while read -r f; do
    log "truncating oversized log: $f ($(du -h "$f" | cut -f1))"
    : > "$f"
done

# In-container Chrome /tmp leak cleanup. The bulk of long-term CT bloat
# (2026-06-06 measurement: ~9G per worker after 5 days of jobs) is NOT
# host-side -- it's Chrome's own /tmp scratch leaking inside the docker
# container's overlay. Two patterns dominate:
#   .com.google.Chrome.*  ~9.7M each, one per crashed renderer
#   scoped_dir*           ~52M each, one per killed lane reload
# Chrome doesn't clean these on exit when a parent SIGKILLs it (= every
# lane swap / Xvfb restart / container SIGTERM in our pipeline). They
# accumulate indefinitely with no owner. Match outside-mtime-60-min so we
# never race with active Chrome holding an fd on the current entry.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^paprika-worker-1$'; then
    nfiles=$(docker exec paprika-worker-1 sh -c \
        'find /tmp -maxdepth 1 -name ".com.google.Chrome.*" -type f -mmin +60 -delete -print 2>/dev/null | wc -l' \
        2>/dev/null || echo 0)
    ndirs=$(docker exec paprika-worker-1 sh -c \
        'find /tmp -maxdepth 1 -name "scoped_dir*" -type d -mmin +60 -exec rm -rf {} + -print 2>/dev/null | wc -l' \
        2>/dev/null || echo 0)
    # Chrome also leaves NON-dotted com.google.Chrome.XXXXXX *directories* (the
    # SingletonCookie/SingletonSocket holders). Individually tiny -- 31M total on
    # a CT carrying 3G of leak -- but neither pattern above ever matched them, so
    # they were the one class that survived every housekeep run.
    ncdirs=$(docker exec paprika-worker-1 sh -c \
        'find /tmp -maxdepth 1 -name "com.google.Chrome.*" -type d -mmin +60 -exec rm -rf {} + -print 2>/dev/null | wc -l' \
        2>/dev/null || echo 0)
    log "container /tmp Chrome leak: removed $nfiles file(s) + $ndirs scoped_dir(s) + $ncdirs chrome dir(s)"
else
    # The docker-exec path above silently no-ops whenever dockerd is unreachable
    # -- which is exactly what happens once the CT rootfs has gone emergency_ro,
    # i.e. the moment this cleanup matters most. On loft 2026-08-05 all 20 worker
    # CTs were wedged by a full LVM-thin pool and every housekeep run for the
    # preceding day logged nothing but "cleanup skipped" while the leak that
    # caused it kept growing. containerd keeps the container running under the
    # moby namespace regardless of dockerd, so reach into the writable layer
    # directly. Same 60-min guard so we never race live Chrome.
    SNAPS=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots
    nfb=$(find "$SNAPS" -maxdepth 4 -path '*/fs/tmp/*' -mmin +60 \
            \( -name 'scoped_dir*' -o -name 'com.google.Chrome.*' -o -name '.com.google.Chrome.*' \) \
            -prune -exec rm -rf {} + -print 2>/dev/null | wc -l)
    log "container /tmp cleanup via containerd upper layer (docker unreachable): removed $nfb entr(ies)"
fi

# Return the freed blocks to the LVM-thin pool. Everything above only frees
# ext4 blocks; without a discard the thin pool still counts them as allocated,
# so on a shared pool the deletes above buy the FLEET nothing. Two passes: on
# loft 2026-08-05 a single pass under pool pressure released about a quarter of
# what it reported, and the second pass took the volumes from 67.9% to 36.1%.
# Cheap on a 32G rootfs and harmless if the CT is unprivileged (fstrim just
# fails and we carry on).
for _pass in 1 2; do
    fstrim / >/dev/null 2>&1 || { [ "$_pass" = 1 ] && log "fstrim / not permitted (unprivileged CT?) -- pool will not shrink from here"; break; }
done

after_used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
after_free=$(df -h --output=avail / | tail -1 | tr -d ' ')
log "after:  disk ${after_used}% used, ${after_free} free  (reclaimed: $((before_used - after_used))pp)"

# Emergency back-stop: if we're STILL >90% after cleanup, the bloat isn't
# from images -- something else is wrong (a job dump? a runaway log?).
# Surface that loudly so admin sees it instead of letting the worker enter
# a silent restart loop.
if [ "$after_used" -ge 90 ]; then
    log "WARNING: disk still ${after_used}% after housekeep -- inspect /var manually"
    du -h -d 1 /var 2>/dev/null | sort -rh | head -5 | sed "s/^/$LOG_PREFIX top-var: /"
fi
