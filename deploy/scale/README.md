# Multi-hub scale-out (foundation — NOT enabled)

Target for stably running ~200 workers behind **nginx + Hub×N + Redis**.
See `internal/200-worker-target-architecture.html` for the full design.

## Status

This directory is the **foundation**, not a turnkey deployment. The
production stack still runs a single hub (`docker-compose.yml`). The
code changes that make multi-hub *possible* have landed; the pieces that
make it *correct* have not.

### Done (in the running code today)

- **`HUB_ID`** — each hub process has a stable id (`config.hub_id`,
  defaults to `$HUB_ID` else the container hostname).
- **WS ownership in Redis** — `WorkerRegistry` writes
  `paprika:worker:{id}:owner = <hub_id>` (TTL-refreshed on register +
  heartbeat, compare-and-deleted on disconnect).
- **Session Map in Redis** — `SessionRegistry` mirrors
  `paprika:session:{sid} = {worker_id, hub}` (write-only, TTL'd).
- **Object-store mirror plumbing** (`server/hub/objstore.py`) — job
  assets (`/assets`, `page.html`, `log.txt`, screenshots, per-attempt
  files) are written through to an S3/MinIO bucket and served with a
  local-first / object-store-fallback read path. **Gated by
  `PAPRIKA_S3_ENABLED`, default OFF**; all bucket IO runs in
  `asyncio.to_thread` so it can't block the hub loop.

All of the above is **dormant for a single hub**: the Redis keys are
written but nothing reads them back, and the object-store mirror is off
unless `PAPRIKA_S3_ENABLED` is set, so single-hub behaviour is
unchanged.

### Not done (required before enabling this)

1. **Hub→Hub forwarding** (control-plane *phase 3*) — **DONE.** When a
   `/sessions/*` request lands on a hub that doesn't hold the session,
   the hub looks it up in the Redis Session Map
   (`paprika:session:{id} = {worker_id, hub}`) and forwards to the
   owning hub, which runs it against its live WS:

   - **actions** (everything via `_send_session_action`: click, fill,
     navigate, outline, evaluate, screenshot, cookies, …) forward as an
     action dict to `POST /internal/sessions/{id}/action` (worker-secret
     guarded, OpenAPI-hidden, local-only on receipt).
   - **close** (`DELETE /sessions/{id}`) and **status**
     (`GET /sessions/{id}`) reverse-proxy the raw request to the owner
     hub (it holds the WS and does the cookie-save / video-drain /
     parent-job cascade). A one-hop loop guard (`X-Paprika-Hub-Forwarded`)
     stops a stale map from bouncing a request between hubs.
   - **noVNC** — HTTP viewer assets reverse-proxy to the owner hub (same
     loop guard); the `websockify` WS bridges operator ⇄ this-hub ⇄
     owner-hub ⇄ worker, guarded against bounce by a `?_fwd=1` marker.

   Dormant on a single hub: a locally-held session short-circuits before
   any Redis lookup. Forward target defaults to `http://{hub_id}:8000`
   (override via `PAPRIKA_HUB_INTERNAL_FMT`), matching the hub-a/b/c
   service names below.

   **Minor follow-up:** `GET /sessions` is per-hub (lists only this
   hub's sessions); cross-hub aggregation is unbuilt. Not a correctness
   issue — individual session ops all resolve correctly via forwarding.
2. **Shared object storage** (*phase 2 / MinIO*). The mirror code has
   **landed but ships OFF** (`server/hub/objstore.py`, gated by
   `PAPRIKA_S3_ENABLED`). To enable: set the `PAPRIKA_S3_*` env (see
   below) on every hub so uploads write through to the shared bucket
   and reads fall back to it. Until enabled, each hub still writes
   assets only to its own local `/data/jobs` and the others 404 them.

   ```
   PAPRIKA_S3_ENABLED=1
   PAPRIKA_S3_ENDPOINT=http://<minio-host>:9000
   PAPRIKA_S3_BUCKET=paprika
   PAPRIKA_S3_PREFIX=jobs
   PAPRIKA_S3_ACCESS_KEY=<key>      # secret — env only, never commit
   PAPRIKA_S3_SECRET_KEY=<secret>   # secret — env only, never commit
   PAPRIKA_S3_REGION=us-east-1
   ```
3. **Redis HA + lease TTL** (*phase 4*) — **DONE (ships OFF).** A single
   Redis is the new SPOF; the code now addresses both halves, and both
   stay dormant for a single hub.

   - **Redis HA** — the client (`server/store.py:make_redis_client`,
     used by both the job store and the live-log pub/sub) understands
     `redis+sentinel://` URLs and returns a master connection that
     follows Sentinel failover automatically. Plain `redis://` is
     unchanged. To enable, point every hub's `--redis-url` at a Sentinel
     pool:

     ```
     redis+sentinel://sentinel-a:26379,sentinel-b:26379,sentinel-c:26379/paprika
     ```

     where the last path segment (`paprika`) is the Sentinel-monitored
     master name (`mymaster` if omitted). Or just use a managed Redis
     and keep a plain `redis://` URL.
   - **Job leases** (`server/hub/_leases.py`, gated by
     `PAPRIKA_JOB_LEASE_ENABLED`, default OFF). Each hub writes a
     TTL'd lease (`paprika:job:{id}:lease = {hub, requeues}`) for every
     **hub-orchestrated** job it's running (codegen-loop + rerun) and
     refreshes it on a timer. If a hub dies it stops refreshing; once the
     lease expires a surviving hub atomically re-claims (`SET NX`) and
     re-dispatches the job — shared object storage (phase 2) lets the new
     hub read the orphaned job's prior attempts. A durable requeue
     counter caps re-dispatches (`PAPRIKA_JOB_LEASE_MAX_REQUEUES`,
     default 1) so a poison job can't bounce the fleet forever; once
     exhausted (or for an unrecoverable rerun) the job is failed out.

     ```
     PAPRIKA_JOB_LEASE_ENABLED=1
     PAPRIKA_JOB_LEASE_TTL_S=90          # lease lifetime without refresh
     PAPRIKA_JOB_LEASE_REFRESH_S=30      # owner re-write cadence (< ttl)
     PAPRIKA_JOB_LEASE_MAX_REQUEUES=1    # re-dispatch budget per job
     ```

     Dormant on a single hub: with the flag OFF the lease loop returns
     immediately, no keys are written, and the existing startup
     "mark orphaned running jobs failed" recovery is unchanged. When ON,
     that startup recovery defers to the lease reaper (so a restarting
     hub never fails a live peer's jobs). **Not covered:** worker-
     dispatched *fetch* jobs — those live on a worker that re-homes to
     another hub on its own, so they don't need hub-side re-dispatch.

## Files

- `nginx.conf` — sticky (consistent-hash by `worker_id`) for
  `/workers/{id}/link`, round-robin for everything else; WebSocket
  upgrade + long read timeouts tuned against the 30s/120s hub↔worker
  ping settings.
- `docker-compose.scale.yml` — nginx + `hub-a/b/c` (distinct `HUB_ID`) +
  shared Redis. The hub env block is a **minimal skeleton**: copy the
  full environment from the root `docker-compose.yml` before real use.

## When ready

```bash
docker compose -f deploy/scale/docker-compose.scale.yml up -d --build
```

Then point workers at the nginx address (`HUB_URL=ws://<nginx-host>:8000`)
and scale up gradually (24 → 50 → 100 → 200), watching 1011 disconnect
rate, lease expiry, and Redis latency at each step.
