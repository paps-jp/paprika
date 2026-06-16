#!/bin/sh
# Runs ON .16. Detached, high-parallelism rm of the precomputed orphan dirs in
# /tmp/orphans2.txt (already validated by the dry-run). $1=parallelism (-P),
# $2=batch (-n). Higher -P overlaps the degraded-FS metadata latency.
J=/Volume1/@usb/usbshare_sda1/paprika-minio/data/paprika/jobs
P=${1:-32}; N=${2:-80}
DEL=$(wc -l < /tmp/orphans2.txt 2>/dev/null || echo 0)
[ "$DEL" -lt 100000 ] && { echo "ABORT: orphans2.txt missing/small ($DEL)"; exit 1; }
[ "$DEL" -gt 705000 ] && { echo "ABORT: orphans2.txt too big ($DEL)"; exit 1; }
: > /tmp/rm2.log
echo "launch P=$P N=$N over $DEL dirs" >> /tmp/rm2.log
df -h "$J" | tail -1 >> /tmp/rm2.log
nohup sh -c "cd '$J' && xargs -P $P -n $N rm -rf < /tmp/orphans2.txt; echo RM_DONE >> /tmp/rm2.log; df -h '$J' | tail -1 >> /tmp/rm2.log" >> /tmp/rm2.log 2>&1 &
echo "rm relaunched DETACHED pid $! (P=$P N=$N, $DEL dirs)"
