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
from pathlib import Path

from server.hub._state import get_storage_dir, state


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
    key = _key_for(path)
    if key is None:
        return

    def _put() -> None:
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
        await asyncio.to_thread(_put)
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
        try:
            for p in root.rglob("*"):
                try:
                    if not p.is_file():
                        continue
                    key = _key_for(p)
                    if key is None:
                        continue
                    client.upload_file(str(p), _bucket(), key)
                    n += 1
                except Exception:
                    continue
        except Exception:
            return n
        return n

    try:
        return await asyncio.to_thread(_put_all)
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
        return await asyncio.to_thread(_get)
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
        return await asyncio.to_thread(_list)
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
        return await asyncio.to_thread(_list)
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
        return await asyncio.to_thread(_head)
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
        return await asyncio.to_thread(_open)
    except Exception:
        return None


__all__ = [
    "enabled",
    "mirror_file",
    "mirror_dir",
    "ensure_local",
    "list_dir",
    "list_tree",
    "prefix_exists",
    "open_object",
]
