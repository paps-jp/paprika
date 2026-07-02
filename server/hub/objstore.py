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
    PAPRIKA_S3_ENDPOINT     e.g. http://10.10.50.16:9000
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
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server.hub._state import get_storage_dir, state


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
    """Drop the cached boto3 client so the next call rebuilds it from the
    current Settings/env config. Called after the operator saves S3
    settings so endpoint / credential changes take effect immediately."""
    global _client
    with _client_lock:
        _client = None


# --- lazy boto3 client (created once, on first use) -------------------------
_client = None
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


async def mirror_file(local_path: Path | str) -> None:
    """Best-effort upload of a freshly-written local file to the bucket.

    No-op when disabled. Never raises: a mirror failure must not fail
    the upload that produced the file (local disk is the source of
    truth). Runs the blocking boto3 call in a worker thread.
    """
    if not enabled():
        return
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
        client = _get_client()
        if client is None:
            return
        try:
            if not path.is_file():
                return
            client.upload_file(str(path), _bucket(), key)
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

    def _put_all() -> int:
        client = _get_client()
        if client is None:
            return 0
        n = 0
        bucket = _bucket()
        try:
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    key = _key_for(p)
                    if key is None:
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
        client = _get_client()
        if client is None:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".s3part")
            client.download_file(_bucket(), key, str(tmp))
            tmp.replace(path)
            return True
        except Exception:
            try:
                tmp = path.with_suffix(path.suffix + ".s3part")
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
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
    path = Path(local_path)

    def _put() -> bool:
        client = _get_client()
        if client is None or not path.is_file():
            return False
        try:
            client.upload_file(str(path), _bucket(), key)
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

    def _sign() -> str | None:
        client = _get_client()
        if client is None:
            return None
        try:
            return client.generate_presigned_url(
                "put_object",
                Params={"Bucket": _bucket(), "Key": key},
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
        client = _get_client()
        if client is None:
            return False
        tmp = path.with_suffix(path.suffix + ".s3part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(_bucket(), key, str(tmp))
            tmp.replace(path)
            return True
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
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
        client = _get_client()
        if client is None:
            return False
        try:
            client.head_object(Bucket=_bucket(), Key=key)
            return True
        except Exception:
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
        client = _get_client()
        if client is None:
            return []
        out: list[dict] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=_bucket(), Prefix=prefix, Delimiter="/"
            ):
                for o in page.get("Contents", []):
                    name = o["Key"][len(prefix):]
                    if not name or "/" in name:
                        continue  # directory marker / nested -- skip
                    out.append({"name": name, "size": int(o.get("Size", 0))})
        except Exception:
            return []
        return out

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
        client = _get_client()
        if client is None:
            return []
        out: list[dict] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
                for o in page.get("Contents", []):
                    rel = o["Key"][len(prefix):]
                    if not rel:
                        continue
                    lm = o.get("LastModified")
                    try:
                        mtime = lm.timestamp() if lm is not None else 0.0
                    except Exception:
                        mtime = 0.0
                    out.append(
                        {"rel": rel, "size": int(o.get("Size", 0)), "mtime": mtime}
                    )
        except Exception:
            return []
        return out

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
        client = _get_client()
        if client is None:
            return {"objects": 0, "bytes": 0, "deleted": False}
        bucket = _bucket()
        n = 0
        total = 0
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
            return {"objects": n, "bytes": total, "deleted": False}
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
        client = _get_client()
        if client is None:
            return []
        out: list[str] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=_bucket(), Prefix=base, Delimiter="/"
            ):
                for cp in page.get("CommonPrefixes", []):
                    jid = cp["Prefix"][len(base):].rstrip("/")
                    if jid:
                        out.append(jid)
        except Exception:
            return out
        return out

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
        client = _get_client()
        if client is None:
            return False
        try:
            r = client.list_objects_v2(Bucket=_bucket(), Prefix=prefix, MaxKeys=1)
            return int(r.get("KeyCount", 0) or 0) > 0
        except Exception:
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
        client = _get_client()
        if client is None:
            return None
        kwargs = {"Bucket": _bucket(), "Key": key}
        if range_header:
            kwargs["Range"] = range_header
        try:
            obj = client.get_object(**kwargs)
        except Exception:
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
