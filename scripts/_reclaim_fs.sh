#!/bin/bash
# Runs ON .34. Builds the live DB job-id set (with a completeness guard), ships
# it + the .16-worker to .16, and runs the worker. EXECUTE=1 -> worker launches a
# DETACHED rm of the orphan job dirs; otherwise dry-run (count only).
set -o pipefail
SP="sshpass -p Weare5814 ssh -p 9222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 paps@10.10.50.16"
ARG="dryrun"; [ "${EXECUTE:-0}" = "1" ] && ARG="exec"

TOTAL=$(curl -s -m 20 "http://10.10.50.34:8000/jobs/summary" | python3 -c "import sys,json;print(json.load(sys.stdin).get('total') or 0)")
echo "summary total: $TOTAL"

> /tmp/live_ids.txt; off=0
while :; do
  cnt=""
  for try in 1 2 3 4 5 6; do
    page=$(curl -s -m 60 "http://10.10.50.34:8000/jobs?limit=500&offset=$off")
    cnt=$(printf '%s' "$page" | python3 -c "import sys,json;d=json.load(sys.stdin);js=d.get('jobs',[]);open('/tmp/_pg','w').write('\n'.join(j['job_id'] for j in js if j.get('job_id')));print(len(js))" 2>/dev/null)
    [ -n "$cnt" ] && break
    sleep 3
  done
  [ -z "$cnt" ] && { echo "ABORT: /jobs page failed after retries @ offset $off"; exit 1; }
  cat /tmp/_pg >> /tmp/live_ids.txt; echo >> /tmp/live_ids.txt
  [ "${cnt:-0}" -lt 1 ] && break; off=$((off+500))
done
grep -E '^[0-9a-f]+$' /tmp/live_ids.txt | sort -u > /tmp/live_ids_s.txt
LIVE=$(wc -l < /tmp/live_ids_s.txt); echo "live ids collected: $LIVE"
if [ "$LIVE" -lt $((TOTAL - 300)) ]; then echo "ABORT: live $LIVE far below total $TOTAL"; exit 1; fi
if [ "$LIVE" -lt 1000 ]; then echo "ABORT: live set < 1000 (suspect)"; exit 1; fi

cat /tmp/live_ids_s.txt | $SP "cat > /tmp/live_ids.txt"
cat /tmp/_reclaim_fs16.sh | $SP "cat > /tmp/w.sh"
echo "=== running .16 worker (mode=$ARG) ==="
$SP "sh /tmp/w.sh $ARG"
echo "=== driver done ==="
