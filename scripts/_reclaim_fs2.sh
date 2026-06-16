#!/bin/bash
# Runs ON .34. Takes the base live set from the prior full pagination
# (/tmp/live_ids_s.txt), AUGMENTS it with the NEWEST jobs (covers the
# pagination-skew gap = jobs added during the long pagination), ships the
# augmented live set to .16, and runs the no-find worker there which reuses the
# already-computed bucket ls (/tmp/all_ids.txt). EXECUTE=1 -> detached rm.
set -o pipefail
SP="sshpass -p Weare5814 ssh -p 9222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 paps@10.10.50.16"
ARG="dryrun"; [ "${EXECUTE:-0}" = "1" ] && ARG="exec"

BASE=$(wc -l < /tmp/live_ids_s.txt 2>/dev/null || echo 0)
echo "base live ids (prior full pagination): $BASE"
if [ "$BASE" -lt 1000 ]; then echo "ABORT: base live set missing/small ($BASE)"; exit 1; fi
cp /tmp/live_ids_s.txt /tmp/live_aug.txt

TOTAL=$(curl -s -m 20 "http://10.10.50.34:8000/jobs/summary" | python3 -c "import sys,json;print(json.load(sys.stdin).get('total') or 0)" 2>/dev/null)
echo "summary total now: $TOTAL"

# augment with the NEWEST jobs (offset 0.. = newest by created_at DESC)
got=0
for off in 0 1000 2000 3000 4000 5000 6000 7000 8000 9000; do
  n=""
  for try in 1 2 3 4 5 6; do
    page=$(curl -s -m 60 "http://10.10.50.34:8000/jobs?limit=1000&offset=$off")
    n=$(printf '%s' "$page" | python3 -c "import sys,json;d=json.load(sys.stdin);js=d.get('jobs',[]);open('/tmp/_pg','w').write('\n'.join(j['job_id'] for j in js if j.get('job_id')));print(len(js))" 2>/dev/null)
    [ -n "$n" ] && break; sleep 3
  done
  [ -z "$n" ] && { echo "WARN: newest page @ $off failed after retries; proceeding with what we have"; break; }
  cat /tmp/_pg >> /tmp/live_aug.txt; got=$((got+n))
  [ "$n" -lt 1 ] && break
done
echo "newest ids fetched: $got"

grep -E '^[0-9a-f]+$' /tmp/live_aug.txt | sort -u > /tmp/live_aug_s.txt
AUG=$(wc -l < /tmp/live_aug_s.txt)
echo "augmented live set (base + newest, uniq): $AUG"
if [ "$AUG" -lt 1000 ]; then echo "ABORT: augmented live set too small ($AUG)"; exit 1; fi

cat /tmp/live_aug_s.txt   | $SP "cat > /tmp/live_aug.txt"
cat /tmp/_reclaim_fs16b.sh | $SP "cat > /tmp/wb.sh"
echo "=== running .16 worker (mode=$ARG, reuses existing bucket ls) ==="
$SP "sh /tmp/wb.sh $ARG"
echo "=== driver done ==="
