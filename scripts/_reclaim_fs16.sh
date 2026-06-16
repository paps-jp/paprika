#!/bin/sh
# Runs ON .16 (ATTIC, busybox-ish sh). Expects /tmp/live_ids.txt (live DB job-ids)
# already in place. Computes orphan job dirs = (bucket dirs) - (live) - (modified
# in the last day). $1 = "exec" launches a DETACHED rm; otherwise dry-run (count).
JOBS="/Volume1/@usb/usbshare_sda1/paprika-minio/data/paprika/jobs"
cd "$JOBS" || { echo "no jobs dir"; exit 1; }

ls | grep -E '^[0-9a-f]+$' | sort > /tmp/all_ids.txt
sort /tmp/live_ids.txt > /tmp/live_s.txt
comm -23 /tmp/all_ids.txt /tmp/live_s.txt > /tmp/orphans_raw.txt
# recent dirs (mtime < 1 day) -> protect, in case the DB read missed a new job
find . -maxdepth 1 -type d -mtime -1 2>/dev/null | sed 's|^\./||' | grep -E '^[0-9a-f]+$' | sort > /tmp/recent.txt
comm -23 /tmp/orphans_raw.txt /tmp/recent.txt > /tmp/orphans.txt

echo "ALL=$(wc -l < /tmp/all_ids.txt) LIVE=$(wc -l < /tmp/live_s.txt) ORPHANS_RAW=$(wc -l < /tmp/orphans_raw.txt) RECENT_EXCL=$(wc -l < /tmp/recent.txt) DELETABLE=$(wc -l < /tmp/orphans.txt)"
echo "sample-deletable:"; head -4 /tmp/orphans.txt

if [ "$1" = "exec" ]; then
  # final safety: never delete if the live set looks empty/broken
  if [ "$(wc -l < /tmp/live_s.txt)" -lt 1000 ]; then echo "ABORT: live set too small"; exit 1; fi
  echo "df BEFORE:"; df -h "$JOBS" | tail -1
  : > /tmp/rm.log
  nohup sh -c "cd '$JOBS' && xargs -P 8 rm -rf < /tmp/orphans.txt; echo RM_DONE >> /tmp/rm.log; df -h '$JOBS' | tail -1 >> /tmp/rm.log" >> /tmp/rm.log 2>&1 &
  echo "rm launched DETACHED (pid $!). monitor: cat /tmp/rm.log ; df -h $JOBS"
fi
