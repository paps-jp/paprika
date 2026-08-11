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

# Refuse to pretend on a read-only rootfs. When the thin pool fills, ext4 flips
# the CT to emergency_ro and every delete below fails silently -- fstrim then
# reports GiB "trimmed" while the pool does not move, because there is nothing
# to release. On foyer 2026-08-11 the fleet guard ran this every 15 minutes for
# three hours against ten read-only CTs and logged 0% reclaimed each time,
# which read as "there is nothing to clean" instead of "I cannot clean".
#
# The flag is the tell, and it is easy to misread: ext4 LEAVES rw at the front
# and appends emergency_ro, so `grep ^rw` says the volume is healthy. Match the
# suffix. Testing an actual write also catches ro remounts that predate us.
root_opts=$(awk '$2=="/" {print $4; exit}' /proc/mounts 2>/dev/null)
case "$root_opts" in
    *emergency_ro*|ro,*|*,ro)
        log "CRITICAL: rootfs is READ-ONLY ($root_opts) -- refusing to run."
        log "CRITICAL: nothing can be deleted and fstrim will free nothing until"
        log "CRITICAL: the CT is stopped and e2fsck'd from the node. See"
        log "CRITICAL: internal/ops/thinpool-guard.sh for the runbook."
        exit 1
        ;;
esac

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

# In-container /tmp scratch cleanup. The bulk of long-term CT bloat is NOT
# host-side -- it's scratch leaking inside the docker container's overlay,
# left behind whenever a parent SIGKILLs its child (= every lane swap / Xvfb
# restart / container SIGTERM in our pipeline). Nothing owns it afterwards.
#
# The patterns are workload-specific and they go stale. The original three were
# raw-Chrome era; by the time foyer was measured on 2026-08-11 the fleet had
# moved to undetected-chromedriver plus paprika's own profile management, and
# `scoped_dir*` matched ZERO entries across all twenty CTs while the guard
# reported 0% reclaimed and everyone read that as "clean". Measured occupancy
# that day, per CT:
#
#   paprika-profile-*  80M each x 66..181   <- dominant by far
#   paprika-vid-*      up to 2.4G           <- present on a quarter of the fleet
#   uc_*               12k..14.8k entries   <- only ~50M, but murder on inodes
#   scoped_dir*        0                    <- the one pattern we were matching
#
# Re-measure before trusting this list again. `du -sh $T/* | sort -hr | head`
# inside the upper layer is the whole method.
#
# Two names in that namespace are NOT scratch and must never be swept:
# paprika-profile-cache is the shared profile every worker seeds from, and
# paprika-extensions lives in a committed image layer -- deleting it corrupts
# the image. This is why the globs stay explicit and nobody widens them to
# `paprika-*`.
#
# -mmin +60 keeps us from racing live Chrome holding an fd on a current entry.
TMP_SCRATCH_GLOBS=(
    '.com.google.Chrome.*'
    'com.google.Chrome.*'
    'scoped_dir*'
    'paprika-profile-*'
    'paprika-vid-*'
    'uc_*'
)
KEEP_GLOBS=( 'paprika-profile-cache' 'paprika-extensions' )

# Build the shared find(1) fragments once so the docker-exec and containerd
# fallback paths cannot drift apart -- they did before, and the fallback was
# the one that mattered during an outage.
#
# The parens are backslash-escaped because this string is consumed by `eval`
# and by `sh -c` inside the container -- a bare ( is a shell syntax error in
# both, and the failure is invisible on the containerd path where stderr goes
# to /dev/null. Caught in a fixture before deploy; do not "clean up" the
# backslashes.
_name_expr() {
    local first=1 g
    printf '\\('
    for g in "${TMP_SCRATCH_GLOBS[@]}"; do
        [ "$first" = 1 ] || printf ' -o'
        printf ' -name "%s"' "$g"
        first=0
    done
    printf ' \\)'
    for g in "${KEEP_GLOBS[@]}"; do printf ' ! -name "%s"' "$g"; done
}
NAME_EXPR="$(_name_expr)"

# rm via xargs, not `-exec rm -rf {} +`. The + form batches every path into a
# single rm that runs LAST, so a timeout kills the whole thing and deletes
# nothing while -print still reports thousands "found" (2026-08-10: "removed=188"
# with zero bytes actually freed). Chunked xargs frees incrementally instead.
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^paprika-worker-1$'; then
    # Count first, then delete. Counting the deletion stream in-flight needs
    # process substitution that /bin/sh inside the container does not have, and
    # the entries are stale by construction (>60 min) so nothing appears between
    # the two passes that we would want to keep.
    n=$(docker exec paprika-worker-1 sh -c \
        "find /tmp -maxdepth 1 -mmin +60 $NAME_EXPR 2>/dev/null | wc -l" 2>/dev/null || echo 0)
    docker exec paprika-worker-1 sh -c \
        "find /tmp -maxdepth 1 -mmin +60 $NAME_EXPR -print0 2>/dev/null \
         | xargs -0 -r -n 40 rm -rf 2>/dev/null" >/dev/null 2>&1 || true
    log "container /tmp scratch: removed $n entr(ies)"
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
    FIND_ARGS="-mindepth 4 -maxdepth 4 -path '*/fs/tmp/*' -mmin +60 $NAME_EXPR"
    nfb=$(eval "find '$SNAPS' $FIND_ARGS" 2>/dev/null | wc -l)
    eval "find '$SNAPS' $FIND_ARGS -print0" 2>/dev/null | xargs -0 -r -n 40 rm -rf 2>/dev/null || true
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
