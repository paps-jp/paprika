#!/bin/bash
# Runs ON .34. Installs the reclaim script into the hub container and launches
# it DETACHED (survives ssh drops), logging to /tmp/reclaim.log in the container.
H=root@10.10.50.35
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6"
C=$(ssh $SSHO $H "docker ps --format '{{.Names}}' | grep -iE hub | grep -v runner | head -1")
echo "container=$C"
cat /tmp/_minio_reclaim.py | ssh $SSHO $H "docker exec -i $C sh -c 'cat > /tmp/reclaim.py'"
echo "script installed; bytes in container:"
ssh $SSHO $H "docker exec $C sh -c 'wc -c < /tmp/reclaim.py; rm -f /tmp/reclaim.log'"
ssh $SSHO $H "docker exec -d -e EXECUTE=${EXECUTE:-0} -e PYTHONPATH=/app $C sh -c 'cd /app && python3 /tmp/reclaim.py > /tmp/reclaim.log 2>&1'"
echo "launched DETACHED (EXECUTE=${EXECUTE:-0}). monitor: docker exec $C cat /tmp/reclaim.log"
