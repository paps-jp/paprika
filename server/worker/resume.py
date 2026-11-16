"""Pick a deferred video download up again after the worker restarts.

Why
---
A worker recycles itself every ``WORKER_RECYCLE_AFTER_JOBS`` jobs, and the
video downloads running behind it die with the process.  Putting the recycle
off (see ``_maybe_begin_recycle``) only changes WHICH downloads are cut: the
worker keeps taking jobs while it waits, so new downloads keep starting, and
there are always a few in flight when it finally exits.  Measured 2026-11-15
over 150 minutes, fleet-wide: 686 downloads cut, 310 GiB already fetched and
thrown away.

Nothing about the bytes requires that.  The container that comes back is the
same container, so both places a download writes to -- its own ``/tmp`` and
the node's shared ramdisk -- still hold the ``.part``; and yt-dlp, run again
with the same arguments on the same directory, carries on where it stopped
(``Resuming download at byte N`` for a plain file, the fragment index in the
``.ytdl`` file for HLS).  82% of the cut downloads were one of those two kinds.

What was missing is the memory of what the download was FOR: which job, which
targets, which referer and cookies, and the result the fetch had already
produced.  That is the manifest this module keeps, one small JSON file per
running download.

Only an exit that says so is resumed
------------------------------------
A manifest is written when the download starts, but it is honoured after a
restart only if the exiting process stamped it (:func:`mark_ok`) -- which the
recycle does and nothing else does.  A crash, an OOM kill or a memory-guard
exit leaves the stamp off and the next start purges the directory exactly as
before.  That is deliberate for the memory guard: a download on the ramdisk is
charged to this CT's memory cgroup for as long as its file exists, so keeping
it across the restart would bring the pressure straight back.

The stamp also goes stale (``PAPRIKA_RESUME_MAX_AGE_S``): a CT that was cloned
or restored carries somebody else's ``/tmp``, and a worker that stayed down
for an hour has no job left to resume for.

Where the files live
--------------------
In the container's own temp dir, not on the shared ramdisk: the manifest
holds the job assignment and the path of a cookies file, and the ramdisk is
visible to every other worker CT on the node.

Env
---
``PAPRIKA_RESUME_DOWNLOADS``   0 = off (kill switch); default on
``PAPRIKA_RESUME_MAX_AGE_S``   how old a stamp may be at startup (default 900)
``PAPRIKA_RESUME_MAX``         how many restarts one download may ride out
                               (default 3)
``PAPRIKA_RESUME_DIR``         manifest directory (default <tmp>/paprika-resume)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Directory name under the temp dir. The worker's tmp sweepers match
#: ``paprika-*`` and must leave this one alone (see WorkerAgent
#: ``_TMP_SWEEP_PROTECTED`` and the startup wipe in ``server/__main__.py``).
DIRNAME = "paprika-resume"

_VERSION = 1


def _num_env(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return (os.environ.get("PAPRIKA_RESUME_DOWNLOADS") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def max_age_s() -> float:
    return _num_env("PAPRIKA_RESUME_MAX_AGE_S", 900.0)


def max_resumes() -> int:
    return int(_num_env("PAPRIKA_RESUME_MAX", 3))


def manifest_dir() -> Path:
    raw = os.environ.get("PAPRIKA_RESUME_DIR")
    return Path(raw) if raw else Path(tempfile.gettempdir()) / DIRNAME


def _path(job_id: str) -> Path:
    # A job id is hex, but this name reaches the filesystem: keep it to the
    # characters a job id can have rather than trusting the caller.
    safe = "".join(c for c in str(job_id) if c.isalnum() or c in "-_")
    return manifest_dir() / f"{safe}.json"


def save(job_id: str, data: dict) -> bool:
    """Write (or replace) the manifest of one download. Never raises: losing
    the ability to resume must not cost the download itself."""
    try:
        d = manifest_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = dict(data)
        body["version"] = _VERSION
        body["job_id"] = job_id
        tmp = d / f".{os.getpid()}.{time.monotonic_ns()}.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f)
        os.replace(str(tmp), str(_path(job_id)))
        return True
    except Exception as e:
        log.info("resume: could not write the manifest of %s (%s)", job_id, e)
        return False


def load(job_id: str) -> dict | None:
    try:
        with open(_path(job_id), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("version") != _VERSION:
        return None
    return m


def load_all() -> list[dict]:
    try:
        names = sorted(p.stem for p in manifest_dir().glob("*.json"))
    except OSError:
        return []
    return [m for m in (load(n) for n in names) if m is not None]


def remove(job_id: str) -> None:
    try:
        _path(job_id).unlink()
    except OSError:
        pass


def mark_ok(job_id: str) -> bool:
    """Stamp a manifest as "the process is leaving on purpose; pick this up".

    Called on the way out of a recycle, so it has to be quick and must not
    raise. False when there is no manifest to stamp."""
    m = load(job_id)
    if m is None:
        return False
    m["resume_ok_at"] = time.time()
    return save(job_id, m)


def _has_bytes(dirs: list[str]) -> bool:
    """True when at least one of the directories still holds a non-empty file."""
    for d in dirs:
        try:
            for p in Path(d).iterdir():
                try:
                    if p.is_file() and p.stat().st_size > 0:
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def why_not(m: dict, worker_id: str | None = None, now: float | None = None) -> str:
    """Why this manifest must NOT be resumed, or "" when it may be."""
    now = time.time() if now is None else now
    if not enabled():
        return "resuming is switched off"
    ok_at = m.get("resume_ok_at")
    if not isinstance(ok_at, (int, float)) or ok_at <= 0:
        return "the last exit did not hand it over"
    age = now - ok_at
    if age < -60 or age > max_age_s():
        return f"handed over {age:.0f}s ago"
    if worker_id and m.get("worker_id") and m.get("worker_id") != worker_id:
        return f"belongs to worker {m.get('worker_id')}"
    if int(m.get("resumes") or 0) >= max_resumes():
        return f"already resumed {int(m.get('resumes') or 0)} time(s)"
    if not m.get("targets") or not m.get("assign"):
        return "incomplete manifest"
    if not _has_bytes(list(m.get("dl_dirs") or [])):
        return "nothing left on disk"
    # Video is never written to a disk. A download an older version left on
    # the CT disk is not carried on with: skipping it here also takes its
    # directory out of keep_dirs(), so the startup wipe removes the bytes.
    from . import scratch_pool

    if not all(scratch_pool.video_dir_ok(d) for d in (m.get("dl_dirs") or [])):
        return "its bytes are on a disk, not the ramdisk"
    return ""


def resumable(worker_id: str | None = None, now: float | None = None) -> list[dict]:
    return [m for m in load_all() if not why_not(m, worker_id, now)]


def keep_dirs(worker_id: str | None = None, now: float | None = None) -> set[str]:
    """Directories the startup purges must leave alone (resolved paths)."""
    out: set[str] = set()
    for m in resumable(worker_id, now):
        for d in m.get("dl_dirs") or []:
            try:
                out.add(str(Path(d).resolve()))
            except OSError:
                out.add(str(d))
    return out


def discard(m: dict, *, rmdirs: bool = True) -> None:
    """Forget a download: its manifest, its cookies file and (by default) its
    bytes. For the shared ramdisk the caller releases the claim instead and
    passes ``rmdirs=False``."""
    ck = m.get("cookies_file")
    if ck:
        try:
            Path(ck).unlink()
        except OSError:
            pass
    if rmdirs:
        for d in m.get("dl_dirs") or []:
            shutil.rmtree(d, ignore_errors=True)
    jid = m.get("job_id")
    if jid:
        remove(str(jid))


def prune(worker_id: str | None = None, now: float | None = None) -> tuple[int, int]:
    """Startup: drop every manifest that will not be resumed.

    Their directories are NOT removed here -- the startup wipe and the
    scratch-pool purge that follow own that, exactly as before this module
    existed -- only the manifest and the cookies file, which nothing else
    would collect. Returns ``(kept, dropped)``."""
    kept = dropped = 0
    try:
        for p in manifest_dir().glob("*.json"):
            if load(p.stem) is None:        # unreadable / another format
                p.unlink()
                dropped += 1
    except OSError:
        pass
    for m in load_all():
        reason = why_not(m, worker_id, now)
        if not reason:
            kept += 1
            continue
        log.info(
            "resume: not picking up %s after the restart: %s",
            m.get("job_id"), reason,
        )
        discard(m, rmdirs=False)
        dropped += 1
    return kept, dropped
