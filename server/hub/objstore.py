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

from server.hub._state import get_storage_dir


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """True when the operator has switched on the object-store mirror."""
    return _env_flag("PAPRIKA_S3_ENABLED")


def _bucket() -> str:
    return os.environ.get("PAPRIKA_S3_BUCKET") or "paprika"


def _prefix() -> str:
    # Strip leading/trailing slashes so key joins stay clean.
    return (os.environ.get("PAPRIKA_S3_PREFIX") or "jobs").strip("/")


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
                endpoint_url=os.environ.get("PAPRIKA_S3_ENDPOINT") or None,
                aws_access_key_id=os.environ.get("PAPRIKA_S3_ACCESS_KEY") or None,
                aws_secret_access_key=os.environ.get("PAPRIKA_S3_SECRET_KEY") or None,
                region_name=os.environ.get("PAPRIKA_S3_REGION") or "us-east-1",
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


__all__ = ["enabled", "mirror_file", "ensure_local"]
