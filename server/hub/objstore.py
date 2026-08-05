"""Optional S3/MinIO mirror for per-job assets (multi-hub foundation).

DORMANT by default. When ``PAPRIKA_S3_ENABLED`` is unset / false every
function here is a cheap no-op and the hub behaves exactly as the
single-hub, local-disk deployment always has. This is the same
"foundation, not enabled" posture as the rest of the multi-hub work
(HUB_ID, WS-ownership keys, the Session Map): the plumbing lands now,
inert, so a later phase can switch it on without a code change.

When enabled, job artifacts written under :func:`get_storage_dir` are
*mirrored* to an S3-compatible object store (MinIO) under a key derived
from the file's path relative to the storage root. Local disk stays the
source of truth and the write-through cache; the bucket is the shared
backing store so that -- once Hub->Hub request forwarding (phase 3)
lands -- any hub behind nginx can serve an asset a *different* hub
produced.

Read policy is local-first / object-store-fallback: if a file is absent
locally (because another hub wrote it) :func:`ensure_local` pulls it
into the local cache path before the caller serves it.

All network IO is pushed off the event loop with ``asyncio.to_thread``
so a slow bucket can never head-of-line-block the hub's single loop
(the exact failure mode the JSONL-append ``to_thread`` fix addressed).

Configuration (all via env; only ``PAPRIKA_S3_ENABLED`` is required to
turn it on):

    PAPRIKA_S3_ENABLED      "1"/"true"/"yes" to activate (default off)
    PAPRIKA_S3_ENDPOINT     e.g. http://192.168.50.16:9000
    PAPRIKA_S3_BUCKET       bucket name (default "paprika")
    PAPRIKA_S3_PREFIX       key prefix within the bucket (default "jobs")
    PAPRIKA_S3_ACCESS_KEY   access key id
    PAPRIKA_S3_SECRET_KEY   secret access key
    PAPRIKA_S3_REGION       region name (default "us-east-1")

Credentials are read from the environment only and are never written to
disk or logged here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server.hub._state import get_storage_dir, state

log = logging.getLogger("paprika.objstore")


# --- dedicated S3/MinIO IO thread pool --------------------------------------
#
# Every blocking boto3 call below runs off the event loop -- but it must NOT
# share the *default* asyncio.to_thread() executor (a single ThreadPoolExecutor,
# capped at min(32, cpu+4) = 32 threads on the 128-core hub hosts). The fleet's
# per-asset uploads (boto3 ``upload_file``, seconds each for large video assets
# to MinIO over LAN) would fill every default-pool thread during an upload
# storm -- and then ANY other to_thread() work queues behind them, most
# painfully ``/jobs`` row deserialisation (MariaDBJobStore hydrates rows via
# asyncio.to_thread). That's exactly the "#jobs が数十秒フリーズする" the
# operator hit: the SELECT is ~1.4ms but the request sat 9.6s waiting for a
# free thread (py-spy 2026-07-03: 22 of 32 default-pool threads parked in
# upload_file). Isolating S3 IO onto its OWN bounded pool keeps the default
# pool free for request handlers no matter how heavy the mirror traffic gets.
#
# Sized independently of cpu-count (this work is IO-bound, not CPU-bound);
# kept at/under ``max_pool_connections`` (50) so threads don't outrun the
# botocore connection pool. Env-tunable; lazily built on first use.
_S3_IO_EXECUTOR: "ThreadPoolExecutor | None" = None
_S3_IO_EXECUTOR_LOCK = threading.Lock()


def _s3_executor() -> "ThreadPoolExecutor":
    global _S3_IO_EXECUTOR
    if _S3_IO_EXECUTOR is not None:
        return _S3_IO_EXECUTOR
    with _S3_IO_EXECUTOR_LOCK:
        if _S3_IO_EXECUTOR is None:
            try:
                _n = int(os.environ.get("PAPRIKA_S3_IO_THREADS") or 40)
            except ValueError:
                _n = 40
            _S3_IO_EXECUTOR = ThreadPoolExecutor(
                max_workers=max(4, _n), thread_name_prefix="objstore-s3",
            )
        return _S3_IO_EXECUTOR


async def _run_io(fn):
    """Run a blocking S3/MinIO callable on the DEDICATED objstore pool
    (``_s3_executor``), never the shared default asyncio.to_thread() pool --
    so mirror-upload storms can't starve request handlers. Drop-in replacement
    for ``await asyncio.to_thread(fn)`` for the zero-arg closures below."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_s3_executor(), fn)


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _s3cfg(key: str, env_name: str, default: str = "") -> str:
    """Resolve an S3 string config: the live Settings registry first
    (SettingsRegistry.get already falls back to the PAPRIKA_S3_* env var
    through its schema), then the env directly when settings aren't bound
    yet (tests / early boot), then ``default``."""
    try:
        if state.settings is not None:
            v = state.settings.get(key, None)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    except Exception:
        pass
    return (os.environ.get(env_name) or default).strip()


def enabled() -> bool:
    """True when the operator has switched on the object-store mirror --
    via the admin Settings tab (``s3_enabled``) or PAPRIKA_S3_ENABLED env."""
    try:
        if state.settings is not None:
            return bool(state.settings.get("s3_enabled", False))
    except Exception:
        pass
    return _env_flag("PAPRIKA_S3_ENABLED")


def _bucket() -> str:
    return _s3cfg("s3_bucket", "PAPRIKA_S3_BUCKET", "paprika") or "paprika"


def _prefix() -> str:
    # Strip leading/trailing slashes so key joins stay clean.
    return _s3cfg("s3_prefix", "PAPRIKA_S3_PREFIX", "jobs").strip("/")


def reset_client() -> None:
    """Drop the cached boto3 clients so the next call rebuilds them from the
    current Settings/env config. Called after the operator saves S3
    settings so endpoint / credential changes take effect immediately.

    The old clients are CLOSED, not just dereferenced. Each owns a urllib3
    pool holding up to ``max_pool_connections`` sockets; dropping the last
    reference leaves them to the GC, and the fds outlive the TCP connection
    (they stop showing up in ``ss`` entirely -- on 2026-07-24 hub .40 held 967
    socket fds with only 255 sockets left in any TCP state, and died of
    EMFILE). This runs on EVERY settings invalidation, cross-hub, so an
    un-closed client here leaks 4 pools per settings change per hub.

    Closed outside the lock: teardown does IO, and holding ``_client_lock``
    across it would block every S3 caller in the process."""
    global _client, _nv_client, _spill_cli, _nv_spill_cli
    with _client_lock:
        stale = [_client, _nv_client, _spill_cli, _nv_spill_cli]
        _client = None
        _nv_client = None
        _spill_cli = None
        _nv_spill_cli = None
    for c in stale:
        if c is None:
            continue
        try:
            c.close()
        except Exception:
            pass


# --- lazy boto3 client (created once, on first use) -------------------------
_client = None
_nv_client = None
_spill_cli = None
_nv_spill_cli = None
_client_lock = threading.Lock()


def _get_client():
    """Return a process-wide boto3 S3 client, building it on first use.

    Synchronous + blocking; only ever called from inside ``to_thread``.
    Returns None if boto3 is unavailable or the client can't be built so
    callers degrade to local-only behaviour instead of raising.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            import boto3
            from botocore.config import Config as _BotoConfig

            _client = boto3.client(
                "s3",
                endpoint_url=_s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT") or None,
                aws_access_key_id=_s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY") or None,
                aws_secret_access_key=_s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY") or None,
                region_name=_s3cfg("s3_region", "PAPRIKA_S3_REGION", "us-east-1"),
                config=_BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},  # MinIO wants path-style
                    retries={"max_attempts": 2, "mode": "standard"},
                    # botocore's default pool is 10 -- exhausted under codegen
                    # asset-I/O bursts ("Connection pool is full, discarding
                    # connection: <minio>"), which serialises S3 calls and
                    # holds asyncio to_thread workers, stalling /jobs & /overview.
                    max_pool_connections=int(
                        os.environ.get("PAPRIKA_S3_MAX_POOL_CONNECTIONS") or 50
                    ),
                ),
            )
        except Exception:
            _client = None
        return _client


# --- video / non-video tier routing (2026-07-06) ----------------------------
# The primary client above (s3_endpoint) is the durable/HDD tier -- it holds
# ALL existing objects + video. When a non-video hot tier is configured
# (s3_nonvideo_endpoint, e.g. a fast SSD MinIO), NON-VIDEO objects (images /
# HTML / HAR / json / ...) are WRITTEN there and READ from there first, falling
# back to the primary for pre-split objects. Video always stays on the primary.
# Listing/delete union BOTH tiers. Off (no nonvideo endpoint) => single-tier,
# identical to before.
_VIDEO_EXTS = frozenset(
    {".mp4", ".webm", ".m3u8", ".ts", ".mkv", ".mov", ".avi", ".m4v", ".mpd", ".flv"}
)


def _is_video_key(key: str) -> bool:
    """True when the object key names a video artifact (-> primary/HDD tier)."""
    k = (key or "").lower()
    dot = k.rfind(".")
    return dot >= 0 and k[dot:] in _VIDEO_EXTS


def _nonvideo_endpoint() -> str:
    return _s3cfg("s3_nonvideo_endpoint", "PAPRIKA_S3_NONVIDEO_ENDPOINT", "")


def _nonvideo_bucket() -> str:
    # Blank -> reuse the primary bucket (the common case: same bucket, diff host).
    return _s3cfg("s3_nonvideo_bucket", "PAPRIKA_S3_NONVIDEO_BUCKET", "") or _bucket()


def _nonvideo_enabled() -> bool:
    """The non-video hot tier is active only when S3 is on AND an endpoint for
    it is configured (Settings/env). Otherwise everything stays single-tier."""
    return enabled() and bool(_nonvideo_endpoint())


def _get_nv_client():
    """Process-wide boto3 client for the non-video hot tier (built on first
    use). None when the tier isn't configured or boto3 is unavailable."""
    global _nv_client
    if _nv_client is not None:
        return _nv_client
    with _client_lock:
        if _nv_client is not None:
            return _nv_client
        ep = _nonvideo_endpoint()
        if not ep:
            return None
        try:
            import boto3
            from botocore.config import Config as _BotoConfig

            _nv_client = boto3.client(
                "s3",
                endpoint_url=ep or None,
                aws_access_key_id=_s3cfg(
                    "s3_nonvideo_access_key", "PAPRIKA_S3_NONVIDEO_ACCESS_KEY"
                ) or None,
                aws_secret_access_key=_s3cfg(
                    "s3_nonvideo_secret_key", "PAPRIKA_S3_NONVIDEO_SECRET_KEY"
                ) or None,
                region_name=_s3cfg(
                    "s3_nonvideo_region", "PAPRIKA_S3_NONVIDEO_REGION", "us-east-1"
                ),
                config=_BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 2, "mode": "standard"},
                    max_pool_connections=int(
                        os.environ.get("PAPRIKA_S3_MAX_POOL_CONNECTIONS") or 50
                    ),
                ),
            )
        except Exception:
            _nv_client = None
        return _nv_client


# --- RAM-disk spill targets (2026-07-21) ------------------------------------
# Both tiers above are RAM-disk MinIO instances (small, and volatile -- a
# reboot loses everything). When one crosses a free-space threshold, NEW
# assets are written to its large durable "spill" store instead. The decision
# is made in _spill.py from a periodic capacity sample -- pre-emptively, on a
# threshold, NOT by catching write errors: during the 2026-07-20 incident the
# RAM host stopped answering entirely, so writes hung instead of failing and
# an error-driven fallback would never have fired.
#
# Reads/lists/deletes union the spill stores too, so spilled objects stay
# visible to the hub with no migration.


def _spill_endpoint() -> str:
    return _s3cfg("s3_spill_endpoint", "PAPRIKA_S3_SPILL_ENDPOINT", "")


def _spill_bucket() -> str:
    return _s3cfg("s3_spill_bucket", "PAPRIKA_S3_SPILL_BUCKET", "") or _bucket()


def _nv_spill_endpoint() -> str:
    return _s3cfg("s3_nonvideo_spill_endpoint", "PAPRIKA_S3_NONVIDEO_SPILL_ENDPOINT", "")


def _nv_spill_bucket() -> str:
    return (
        _s3cfg("s3_nonvideo_spill_bucket", "PAPRIKA_S3_NONVIDEO_SPILL_BUCKET", "")
        or _nonvideo_bucket()
    )


def _spill_feature_on() -> bool:
    """Master switch (Settings ``asset_spill_enabled``). Off => the whole
    spill mechanism is inert and routing is exactly as it was before."""
    try:
        from server.hub import _spill

        return _spill.spill_enabled()
    except Exception:
        return False


def _spill_enabled() -> bool:
    """Primary(video) tier has somewhere to spill to."""
    return enabled() and _spill_feature_on() and bool(_spill_endpoint())


def _nv_spill_enabled() -> bool:
    """Non-video(image) hot tier has somewhere to spill to."""
    return (
        _nonvideo_enabled() and _spill_feature_on() and bool(_nv_spill_endpoint())
    )


def _build_s3_client(endpoint: str, ak: str, sk: str, region: str):
    """Build one MinIO-flavoured boto3 client, or None on any failure."""
    try:
        import boto3
        from botocore.config import Config as _BotoConfig

        return boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=ak or None,
            aws_secret_access_key=sk or None,
            region_name=region or "us-east-1",
            config=_BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 2, "mode": "standard"},
                max_pool_connections=int(
                    os.environ.get("PAPRIKA_S3_MAX_POOL_CONNECTIONS") or 50
                ),
            ),
        )
    except Exception:
        return None


def _get_spill_client():
    global _spill_cli
    if _spill_cli is not None:
        return _spill_cli
    with _client_lock:
        if _spill_cli is not None:
            return _spill_cli
        ep = _spill_endpoint()
        if not ep:
            return None
        _spill_cli = _build_s3_client(
            ep,
            _s3cfg("s3_spill_access_key", "PAPRIKA_S3_SPILL_ACCESS_KEY")
            or _s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY"),
            _s3cfg("s3_spill_secret_key", "PAPRIKA_S3_SPILL_SECRET_KEY")
            or _s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY"),
            _s3cfg("s3_spill_region", "PAPRIKA_S3_SPILL_REGION", "us-east-1"),
        )
        return _spill_cli


def _get_nv_spill_client():
    global _nv_spill_cli
    if _nv_spill_cli is not None:
        return _nv_spill_cli
    with _client_lock:
        if _nv_spill_cli is not None:
            return _nv_spill_cli
        ep = _nv_spill_endpoint()
        if not ep:
            return None
        _nv_spill_cli = _build_s3_client(
            ep,
            _s3cfg(
                "s3_nonvideo_spill_access_key", "PAPRIKA_S3_NONVIDEO_SPILL_ACCESS_KEY"
            )
            or _s3cfg("s3_nonvideo_access_key", "PAPRIKA_S3_NONVIDEO_ACCESS_KEY"),
            _s3cfg(
                "s3_nonvideo_spill_secret_key", "PAPRIKA_S3_NONVIDEO_SPILL_SECRET_KEY"
            )
            or _s3cfg("s3_nonvideo_secret_key", "PAPRIKA_S3_NONVIDEO_SECRET_KEY"),
            _s3cfg(
                "s3_nonvideo_spill_region",
                "PAPRIKA_S3_NONVIDEO_SPILL_REGION",
                "us-east-1",
            ),
        )
        return _nv_spill_cli


# --- bucket existence / persistence (2026-07-21) ----------------------------
# The RAM-disk MinIO tiers (.47 images, .48 video) lose ALL state on reboot --
# including the ``paprika`` bucket itself, not just the objects. A missing
# bucket makes every write fail with NoSuchBucket, and the spill switch is
# threshold-driven (not error-driven) so it would NOT catch that. So the hub
# idempotently (re)creates the bucket on each write endpoint, driven by the
# spill sampler's periodic pass -- practical "persistence" for a volatile
# store. When the bucket is missing AND cannot be recreated (endpoint wedged),
# the sampler treats the tier as un-writable and spills to the durable store.


def ensure_bucket(client, bucket: str) -> bool:
    """Idempotently make sure ``bucket`` exists on ``client``.

    SYNCHRONOUS + blocking; only ever called from inside the S3 IO executor.
    Returns True if the bucket exists or was just created, False if it could
    not be ensured (endpoint unreachable / create denied) -- which the caller
    reads as 'not writable right now -> spill'. Never raises.
    """
    if client is None or not bucket:
        return False
    try:
        client.head_bucket(Bucket=bucket)
        return True
    except Exception:
        pass  # missing, or a transient head failure -- try to (re)create
    try:
        client.create_bucket(Bucket=bucket)
        log.info("objstore: (re)created bucket %r on a write endpoint", bucket)
        return True
    except Exception as exc:
        tag = type(exc).__name__ + " " + str(exc)
        # A peer hub raced us to create it, or head was just flaky: it exists.
        if "BucketAlreadyOwnedByYou" in tag or "BucketAlreadyExists" in tag:
            return True
        log.warning("objstore: could not ensure bucket %r: %s", bucket, type(exc).__name__)
        return False


def _ram_client_bucket(tier: str):
    """(client, bucket) of the RAM store backing ``tier`` (not its spill)."""
    if tier == "nonvideo":
        return _get_nv_client(), _nonvideo_bucket()
    return _get_client(), _bucket()


async def ensure_ram_bucket(tier: str) -> bool:
    """Ensure the RAM tier's bucket exists; see :func:`ensure_bucket`. Called
    by the spill sampler each pass so a rebooted RAM MinIO self-heals within
    one interval. Returns False when the bucket is missing and unrecoverable
    (-> the tier is spilled)."""
    def _do() -> bool:
        c, b = _ram_client_bucket(tier)
        return ensure_bucket(c, b)

    try:
        return await _run_io(_do)
    except Exception:
        return False


async def ensure_spill_buckets() -> None:
    """Best-effort ensure the spill-TARGET buckets exist too. They're durable
    stores so this is defensive, but a missing target bucket would break the
    fallback. Non-fatal; never raises."""
    def _do() -> None:
        if _spill_enabled():
            ensure_bucket(_get_spill_client(), _spill_bucket())
        if _nv_spill_enabled():
            ensure_bucket(_get_nv_spill_client(), _nv_spill_bucket())

    try:
        await _run_io(_do)
    except Exception:
        pass


def job_id_from_key(key: str) -> str | None:
    """``{prefix}/{job_id}/...`` -> job_id. None for non-job keys (profiles/...).

    Lets the synchronous write path recover the job a key belongs to without
    threading job context through every caller signature -- which is what
    keeps one job's assets pinned to a single store.
    """
    k = (key or "").strip("/")
    p = _prefix()
    if p and k.startswith(p + "/"):
        k = k[len(p) + 1 :]
    seg = k.split("/", 1)[0].strip()
    return seg or None


def _spilling(tier: str, key: str) -> bool:
    try:
        from server.hub import _spill

        return _spill.is_spilling(tier, job_id_from_key(key))
    except Exception:
        return False


def _write_cb(key: str):
    """(client, bucket) to WRITE ``key`` to: non-video -> hot tier when
    configured, else primary; video -> primary. Each tier diverts to its
    spill store while that tier is over its free-space threshold (per-job
    pinned, so one job never splits). (None, bucket) when no client."""
    if not _is_video_key(key) and _nonvideo_enabled():
        if _nv_spill_enabled() and _spilling("nonvideo", key):
            c = _get_nv_spill_client()
            if c is not None:
                return c, _nv_spill_bucket()
        c = _get_nv_client()
        if c is not None:
            return c, _nonvideo_bucket()
    if _spill_enabled() and _spilling("primary", key):
        c = _get_spill_client()
        if c is not None:
            return c, _spill_bucket()
    return _get_client(), _bucket()


def _dedup_cbs(cbs: list) -> list:
    """Drop duplicate (client, bucket) pairs -- a spill target may be
    configured to the same endpoint as another tier, and LIST/DELETE must not
    visit it twice."""
    out: list = []
    seen: set = set()
    for c, b in cbs:
        if c is None:
            continue
        sig = (id(c), b)
        if sig in seen:
            continue
        seen.add(sig)
        out.append((c, b))
    return out


def _read_cbs(key: str) -> list:
    """[(client, bucket), ...] to try IN ORDER when READING ``key``. Non-video:
    hot tier first, then its spill, then primary (fallback for pre-split
    objects) and the primary's spill. Empty when no client is available."""
    out: list = []
    if not _is_video_key(key) and _nonvideo_enabled():
        out.append((_get_nv_client(), _nonvideo_bucket()))
        if _nv_spill_enabled():
            out.append((_get_nv_spill_client(), _nv_spill_bucket()))
    out.append((_get_client(), _bucket()))
    if _spill_enabled():
        out.append((_get_spill_client(), _spill_bucket()))
    return _dedup_cbs(out)


def _all_cbs() -> list:
    """All configured (client, bucket) tiers -- for prefix LIST / DELETE, which
    must union every tier because a job's assets are split video(primary) /
    non-video(hot), and either may additionally have spilled."""
    out: list = [(_get_client(), _bucket())]
    if _spill_enabled():
        out.append((_get_spill_client(), _spill_bucket()))
    if _nonvideo_enabled():
        out.append((_get_nv_client(), _nonvideo_bucket()))
        if _nv_spill_enabled():
            out.append((_get_nv_spill_client(), _nv_spill_bucket()))
    return _dedup_cbs(out)


def _key_for(local_path: Path) -> str | None:
    """Map an on-disk artifact path to its object key.

    Key = ``{prefix}/{path-relative-to-storage-root}`` using forward
    slashes. Returns None when the path is not under the storage root
    (e.g. hub-internal metadata in ``config.data_dir``), which signals
    the caller to skip mirroring.
    """
    try:
        rel = Path(local_path).resolve().relative_to(get_storage_dir().resolve())
    except (ValueError, OSError):
        return None
    rel_posix = rel.as_posix()
    p = _prefix()
    return f"{p}/{rel_posix}" if p else rel_posix


# --- public async API -------------------------------------------------------


async def warm_pin(job_id: str | None) -> None:
    """Resolve this job's store decision BEFORE dispatching a write into the
    executor thread.

    ``_write_cb`` runs inside the thread pool and so cannot await Redis; it
    reads a process-local cache instead. This warms that cache. Callers that
    know their job_id should call it -- without a pin the write falls back to
    the live threshold decision, which could split a job across two stores if
    the threshold happens to flip mid-job. Never raises."""
    if not job_id:
        return
    try:
        from server.hub import _spill

        await _spill.ensure_pin(job_id)
    except Exception:
        pass


async def mirror_file(local_path: Path | str, job_id: str | None = None) -> None:
    """Best-effort upload of a freshly-written local file to the bucket.

    No-op when disabled. Never raises: a mirror failure must not fail
    the upload that produced the file (local disk is the source of
    truth). Runs the blocking boto3 call in a worker thread.

    ``job_id`` is optional and only pins the spill decision (see warm_pin).
    """
    if not enabled():
        return
    await warm_pin(job_id)
    path = Path(local_path)

    def _put() -> None:
        # _key_for resolves the path (Path.resolve() = realpath = sync lstat
        # syscalls); run it in THIS worker thread, not on the event loop. Per-
        # asset uploads from the fleet made this realpath a recurring chunk of
        # the hub's on-CPU loop time (py-spy 2026-06-08), feeding the
        # intermittent multi-second admin-poll stalls.
        key = _key_for(path)
        if key is None:
            return
        client, bucket = _write_cb(key)
        if client is None:
            return
        try:
            if not path.is_file():
                return
            client.upload_file(str(path), bucket, key)
        except Exception:
            # Swallow: the local copy already satisfies single-hub serves.
            pass

    try:
        await _run_io(_put)
    except Exception:
        pass


async def mirror_dir(local_dir: Path | str) -> int:
    """Best-effort recursive mirror of an entire job dir to the bucket.

    Uploads every file under ``local_dir`` (key per :func:`_key_for`).
    Returns the count uploaded; 0 (no-op) when disabled / boto3 missing.
    Never raises. Idempotent -- re-sending a file the per-file mirror
    already uploaded just overwrites the same key. Called at job completion
    so artifacts the hub orchestrator writes directly (script.py, plan.json,
    actions.json, attempts/*) reach S3 too, not only worker-uploaded assets.
    Blocking IO runs in a worker thread."""
    if not enabled():
        return 0
    root = Path(local_dir)
    # Job dirs are {storage_root}/{job_id}; .name avoids a resolve() here.
    await warm_pin(root.name)

    def _put_all() -> int:
        n = 0
        try:
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    key = _key_for(p)
                    if key is None:
                        continue
                    client, bucket = _write_cb(key)  # video->primary, else hot tier
                    if client is None:
                        continue
                    # INCREMENTAL: skip files already in the bucket at the same
                    # size (the per-file mirror already sent most assets). This
                    # keeps mirror_dir cheap when called repeatedly -- e.g. the
                    # cache-evict loop, which would otherwise RE-upload every
                    # multi-GB video on each pass and never keep up. Only genuinely
                    # missing files (typically the .meta sidecars) are uploaded.
                    try:
                        sz = p.stat().st_size
                        h = client.head_object(Bucket=bucket, Key=key)
                        if int(h.get("ContentLength", -1)) == sz:
                            continue
                    except Exception:
                        pass  # not present / head failed -> upload below
                    client.upload_file(str(p), bucket, key)
                    n += 1
                except Exception:
                    continue
        except Exception:
            return n
        return n

    try:
        return await _run_io(_put_all)
    except Exception:
        return 0


async def ensure_local(local_path: Path | str) -> bool:
    """Guarantee ``local_path`` exists locally, pulling from the bucket if
    needed. Returns True when the file is present after the call.

    Local-first: if the file is already on disk this is a single
    ``os.path`` check and returns immediately (the common, single-hub
    case). When the file is missing *and* the mirror is enabled, it
    downloads the object into ``local_path`` (creating parent dirs)
    before returning. Blocking IO runs in a worker thread.
    """
    path = Path(local_path)
    if path.exists():
        return True
    if not enabled():
        return False
    key = _key_for(path)
    if key is None:
        return False

    def _get() -> bool:
        for client, bucket in _read_cbs(key):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".s3part")
                client.download_file(bucket, key, str(tmp))
                tmp.replace(path)
                return True
            except Exception:
                try:
                    tmp = path.with_suffix(path.suffix + ".s3part")
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                continue
        return False

    try:
        return await _run_io(_get)
    except Exception:
        return False


# --- explicit-key helpers (hub-internal blobs outside the storage root) -----
#
# mirror_file / ensure_local derive the object key from a path UNDER the per-job
# storage root (_key_for), so they silently no-op for hub-internal blobs that
# live in config.data_dir (e.g. profile tarballs). These take an EXPLICIT key
# instead, for the profiles-> MinIO migration (caller owns the key scheme, e.g.
# ``profiles/<name>.tar.gz``).


async def put_object(key: str, local_path: Path | str) -> bool:
    """Upload a local file to an explicit object key. Returns True on success;
    no-op→False when disabled / boto3 missing / file absent. Never raises."""
    if not enabled():
        return False
    await warm_pin(job_id_from_key(key))
    path = Path(local_path)

    def _put() -> bool:
        client, bucket = _write_cb(key)
        if client is None or not path.is_file():
            return False
        try:
            client.upload_file(str(path), bucket, key)
            return True
        except Exception:
            return False

    try:
        return await _run_io(_put)
    except Exception:
        return False


def asset_key(job_id: str, filename: str) -> str:
    """Object key for a job asset, matching ``_key_for``'s scheme so a
    presigned PUT writes to the SAME key ``get_asset`` / ``mirror_file`` read:
    ``{prefix}/{job_id}/assets/{filename}``."""
    rel = f"{job_id}/assets/{filename}"
    p = _prefix()
    return f"{p}/{rel}" if p else rel


async def presign_put(key: str, expires_in: int = 7200) -> str | None:
    """Presigned PUT URL for an explicit key: the worker uploads the asset
    BYTES straight to MinIO with a plain HTTP PUT (the hub never sees them;
    asset uploads are ~45% of hub load). The URL embeds a SigV4 signature
    scoped to THIS key + PUT + expiry; MinIO verifies it OFFLINE against the
    shared secret -- no hub->MinIO call, no credentials handed to the worker.
    ``generate_presigned_url`` is a local crypto op (no network). None when
    disabled / no client. Never raises."""
    if not enabled():
        return None
    # Must pin BEFORE signing: the URL bakes in one endpoint, so the worker's
    # direct PUT lands wherever this decision points.
    await warm_pin(job_id_from_key(key))

    def _sign() -> str | None:
        client, bucket = _write_cb(key)
        if client is None:
            return None
        try:
            return client.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=int(expires_in),
            )
        except Exception:
            return None

    try:
        return await _run_io(_sign)
    except Exception:
        return None


async def get_object(key: str, local_path: Path | str) -> bool:
    """Download an explicit object key to ``local_path`` (atomic via .s3part).
    Creates parent dirs. Returns True on success. Never raises."""
    if not enabled():
        return False
    path = Path(local_path)

    def _get() -> bool:
        tmp = path.with_suffix(path.suffix + ".s3part")
        for client, bucket in _read_cbs(key):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(tmp))
                tmp.replace(path)
                return True
            except Exception:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                continue
        return False

    try:
        return await _run_io(_get)
    except Exception:
        return False


async def head_object(key: str) -> bool:
    """True when the explicit object key exists in the bucket. Never raises."""
    if not enabled():
        return False

    def _head() -> bool:
        for client, bucket in _read_cbs(key):
            try:
                client.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                continue
        return False

    try:
        return await _run_io(_head)
    except Exception:
        return False


def _job_prefix(job_id: str, subdir: str = "") -> str:
    """Object-key prefix (with trailing slash) for a job's dir, or a
    ``subdir`` within it. Mirrors the layout :func:`_key_for` produces:
    ``{prefix}/{job_id}/{subdir}/``."""
    p = _prefix()
    base = f"{p}/{job_id}" if p else job_id
    if subdir:
        base = f"{base}/{subdir.strip('/')}"
    return base + "/"


async def list_dir(job_id: str, subdir: str = "assets") -> list[dict]:
    """List the immediate files under a job's ``{subdir}`` in the bucket.

    Returns ``[{"name", "size"}]`` for direct children only -- nested dirs
    (e.g. the ``.meta/`` sidecar dir) are excluded via the ``/`` delimiter,
    matching the non-recursive ``Path.iterdir()`` the local lister uses.
    ``[]`` when disabled, boto3 is missing, or on any error (callers fall
    back to the local listing)."""
    if not enabled():
        return []
    prefix = _job_prefix(job_id, subdir)

    def _list() -> list[dict]:
        seen: dict = {}
        for client, bucket in _all_cbs():
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(
                    Bucket=bucket, Prefix=prefix, Delimiter="/"
                ):
                    for o in page.get("Contents", []):
                        name = o["Key"][len(prefix):]
                        if not name or "/" in name:
                            continue  # directory marker / nested -- skip
                        seen.setdefault(name, int(o.get("Size", 0)))
            except Exception:
                continue
        return [{"name": n, "size": s} for n, s in seen.items()]

    try:
        return await _run_io(_list)
    except Exception:
        return []


async def list_tree(job_id: str, subdir: str = "assets") -> list[dict]:
    """Recursively list ALL files under a job's ``{subdir}`` in the bucket.

    Returns ``[{"rel", "size", "mtime"}]`` where ``rel`` is the path
    relative to ``{subdir}`` (forward slashes, may contain ``/``) and
    ``mtime`` is the object's LastModified as POSIX seconds. Unlike
    :func:`list_dir` there is no delimiter, so nested dirs are included --
    needed by the screenshot tab which walks ``assets/<label>/<file>``.
    ``[]`` when disabled / on error."""
    if not enabled():
        return []
    prefix = _job_prefix(job_id, subdir)

    def _list() -> list[dict]:
        seen: dict = {}
        for client, bucket in _all_cbs():
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for o in page.get("Contents", []):
                        rel = o["Key"][len(prefix):]
                        if not rel:
                            continue
                        lm = o.get("LastModified")
                        try:
                            mtime = lm.timestamp() if lm is not None else 0.0
                        except Exception:
                            mtime = 0.0
                        seen.setdefault(
                            rel, (int(o.get("Size", 0)), mtime)
                        )
            except Exception:
                continue
        return [{"rel": r, "size": v[0], "mtime": v[1]} for r, v in seen.items()]

    try:
        return await _run_io(_list)
    except Exception:
        return []


async def delete_prefix(job_id: str, *, dry_run: bool = False) -> dict:
    """Delete EVERY object under a job's dir in the bucket
    (``{prefix}/{job_id}/...``). Returns ``{"objects", "bytes", "deleted"}``.
    ``dry_run=True`` counts only (no delete).

    DESTRUCTIVE + permanent: the bucket is the durable store, so a job's assets
    are gone for good. Used by the job-cleanup / orphan-sweep path so deleting a
    job actually reclaims its storage instead of orphaning it. Returns
    ``objects=0`` when disabled / boto3 missing / nothing there."""
    if not enabled() or not job_id:
        return {"objects": 0, "bytes": 0, "deleted": False}
    prefix = _job_prefix(job_id)

    def _delete() -> dict:
        n = 0
        total = 0
        for client, bucket in _all_cbs():
            batch: list[dict] = []
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for o in page.get("Contents", []):
                        n += 1
                        total += int(o.get("Size", 0))
                        if not dry_run:
                            batch.append({"Key": o["Key"]})
                            if len(batch) >= 1000:  # S3 delete_objects caps at 1000
                                client.delete_objects(
                                    Bucket=bucket,
                                    Delete={"Objects": batch, "Quiet": True},
                                )
                                batch = []
                if not dry_run and batch:
                    client.delete_objects(
                        Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
                    )
            except Exception:
                continue
        return {"objects": n, "bytes": total, "deleted": not dry_run}

    try:
        return await _run_io(_delete)
    except Exception:
        return {"objects": 0, "bytes": 0, "deleted": False}


async def list_job_prefixes() -> list[str]:
    """Every top-level job-id prefix present in the bucket (one per job dir).
    A delimited top-level listing (not a full object walk). Used by the orphan
    sweep to find bucket data whose DB job row is gone. ``[]`` when disabled."""
    if not enabled():
        return []
    p = _prefix()
    base = (p + "/") if p else ""

    def _list() -> list[str]:
        seen: set = set()
        for client, bucket in _all_cbs():
            try:
                paginator = client.get_paginator("list_objects_v2")
                for page in paginator.paginate(
                    Bucket=bucket, Prefix=base, Delimiter="/"
                ):
                    for cp in page.get("CommonPrefixes", []):
                        jid = cp["Prefix"][len(base):].rstrip("/")
                        if jid:
                            seen.add(jid)
            except Exception:
                continue
        return list(seen)

    try:
        return await _run_io(_list)
    except Exception:
        return []


async def prefix_exists(job_id: str, subdir: str = "") -> bool:
    """True when the bucket holds at least one object under the job's dir
    (or ``{subdir}`` within it). Lets the soft-resolve gate accept a job
    whose local/NAS copy is missing but whose artifacts live in the bucket.
    False when disabled / on error."""
    if not enabled():
        return False
    prefix = _job_prefix(job_id, subdir)

    def _head() -> bool:
        for client, bucket in _all_cbs():
            try:
                r = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
                if int(r.get("KeyCount", 0) or 0) > 0:
                    return True
            except Exception:
                continue
        return False

    try:
        return await _run_io(_head)
    except Exception:
        return False


async def open_object(
    job_id: str, rel_path: str, range_header: str | None = None
):
    """Open a job artifact in the bucket for streaming, honouring an HTTP
    ``Range`` header so video seeks work without a local copy.

    ``rel_path`` is relative to the job dir, e.g. ``"assets/clip.mp4"``.
    Returns ``{"status", "headers", "iter"}`` (``iter`` is a 0-arg sync
    generator yielding byte chunks) or ``None`` when disabled, missing, or
    on error -- callers then fall back to local disk."""
    if not enabled():
        return None
    key = _job_prefix(job_id).rstrip("/") + "/" + rel_path.lstrip("/")

    def _open():
        obj = None
        for client, bucket in _read_cbs(key):
            kwargs = {"Bucket": bucket, "Key": key}
            if range_header:
                kwargs["Range"] = range_header
            try:
                obj = client.get_object(**kwargs)
                break
            except Exception:
                obj = None
                continue
        if obj is None:
            return None
        body = obj["Body"]
        content_range = obj.get("ContentRange")
        headers = {"Accept-Ranges": "bytes"}
        clen = obj.get("ContentLength")
        if clen is not None:
            headers["Content-Length"] = str(clen)
        if content_range:
            headers["Content-Range"] = content_range

        def _iter(chunk: int = 262144):
            try:
                while True:
                    data = body.read(chunk)
                    if not data:
                        break
                    yield data
            finally:
                try:
                    body.close()
                except Exception:
                    pass

        return {
            "status": 206 if content_range else 200,
            "headers": headers,
            "iter": _iter,
        }

    try:
        return await _run_io(_open)
    except Exception:
        return None


async def reachable() -> tuple[bool, str]:
    """Liveness probe for the admin Settings status banner: ``head_bucket``
    on the configured bucket with a short timeout. Returns ``(ok, reason)``
    -- ``(True, "")`` when reachable, ``(False, "<ExceptionName>")`` on
    failure, ``(False, "disabled")`` when the mirror is off. Uses a fresh
    short-timeout client (NOT the cached transfer client, whose long
    timeouts would let a dead endpoint hang the /settings load)."""
    if not enabled():
        return (False, "disabled")

    def _check() -> tuple[bool, str]:
        try:
            import boto3
            from botocore.config import Config as _BotoConfig
        except Exception:
            return (False, "boto3 unavailable")
        # This client is PER-CALL (unlike the cached transfer clients), so it
        # must be closed: a boto3 client owns a urllib3 pool, and dropping the
        # reference leaves its sockets to the GC -- they linger as CLOSE-WAIT
        # once MinIO times the keep-alive out. /settings calls this on every
        # load, so an admin tab left polling slowly ate the hub's fd budget
        # (2026-07-24 EMFILE outage). ``client`` is bound before the try so
        # ``finally`` can close it even if construction's follow-up call raises.
        client = None
        try:
            client = boto3.client(
                "s3",
                endpoint_url=_s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT") or None,
                aws_access_key_id=_s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY") or None,
                aws_secret_access_key=_s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY") or None,
                region_name=_s3cfg("s3_region", "PAPRIKA_S3_REGION", "us-east-1"),
                config=_BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 1, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=3,
                ),
            )
            client.head_bucket(Bucket=_bucket())
            return (True, "")
        except Exception as e:
            return (False, type(e).__name__)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    try:
        return await _run_io(_check)
    except Exception as e:
        return (False, type(e).__name__)


__all__ = [
    "enabled",
    "mirror_file",
    "mirror_dir",
    "reachable",
    "ensure_local",
    "put_object",
    "get_object",
    "head_object",
    "list_dir",
    "list_tree",
    "prefix_exists",
    "open_object",
]
