#!/bin/sh
# Runs ON .16 (ATTIC). Reuses /tmp/all_ids.txt (bucket ls, already produced) and
# /tmp/live_aug.txt (augmented live DB set = full pagination + newest jobs).
# Computes orphans = all_ids - live_aug  (NO slow find -mtime; recent jobs are
# protected by the newest-job augmentation done on the driver side).
# $1 = "exec" launches a DETACHED rm; otherwise dry-run (count only).
JOBS="/Volume1/@usb/usbshare_sda1/paprika-minio/data/paprika/jobs"
ALL=$(wc -l < /tmp/all_ids.txt 2>/dev/null || echo 0)
if [ "$ALL" -lt 100000 ]; then echo "ABORT: all_ids.txt missing/small ($ALL)"; exit 1; fi
sort -u /tmp/all_ids.txt   > /tmp/all_s.txt
sort -u /tmp/live_aug.txt  > /tmp/live_aug_s.txt
LIVE=$(wc -l < /tmp/live_aug_s.txt)
comm -23 /tmp/all_s.txt /tmp/live_aug_s.txt > /tmp/orphans2.txt
DEL=$(wc -l < /tmp/orphans2.txt)
echo "ALL=$ALL LIVE_AUG=$LIVE DELETABLE=$DEL"
echo "sample-deletable:"; head -3 /tmp/orphans2.txt

if [ "$1" = "exec" ]; then
  [ "$LIVE" -lt 1000 ]   && { echo "ABORT: live set too small ($LIVE)"; exit 1; }
  [ "$DEL"  -lt 100000 ] && { echo "ABORT: deletable <100k suspicious ($DEL)"; exit 1; }
  [ "$DEL"  -gt 705000 ] && { echo "ABORT: deletable >705k suspicious ($DEL)"; exit 1; }
  echo "df BEFORE:"; df -h "$JOBS" | tail -1
  : > /tmp/rm.log
  nohup sh -c "cd '$JOBS' && xargs -P 8 rm -rf < /tmp/orphans2.txt; echo RM_DONE >> /tmp/rm.log; df -h '$JOBS' | tail -1 >> /tmp/rm.log" >> /tmp/rm.log 2>&1 &
  echo "rm launched DETACHED (pid $!). monitor: tail -f /tmp/rm.log"
fi
