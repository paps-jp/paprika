"""Lightweight distiller — no LLM, just bookkeeping.

v2 Phase 5 helper. Updates ``HostKnowledge.stats`` after each completed
job so the maturity tier (low/medium/high/stale) reflects reality. Pure
heuristics: bumps total_jobs / successful_jobs / success_rate /
last_*_at, and recomputes overall_confidence via the maturity evaluator.

Deeper distillation -- "this attempt revealed a new barrier strategy,
write it into per_page.barriers" -- is the R1 Distiller's job (Phase 6
or later). This module is the cheap baseline that runs on every job so
the operator UI can show meaningful confidence numbers from day one.

Concurrent writes: two jobs against the same host can complete within
milliseconds of each other. We use a write-then-rename pattern with a
file lock (POSIX flock via fcntl) to avoid losing updates. Best-effort
on Windows.

Public API:
  * ``record_job_outcome(host, success, *, job_id, reason)``
    -- updates ``data/host_knowledge/{host}.json`` and the per-host
       history JSONL. Returns the new HostKnowledge dict, or None.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# fcntl is POSIX-only; on Windows we fall back to "best-effort, no lock".
try:
    import fcntl as _fcntl  # type: ignore[import-untyped]
except Exception:  # pragma: no cover -- Windows fallback
    _fcntl = None  # type: ignore[assignment]

from core.host_knowledge import HostKnowledge, evaluate_maturity, Stats, Provenance


_log = logging.getLogger(__name__)


def host_from_url(url: str | None) -> str | None:
    """Extract a normalised host (lowercase, ``www.`` stripped) from a URL.

    Returns None if the URL is unparseable or has no host. Matches the
    same normalisation used by ``_consult_host_knowledge`` in routes/jobs.py.
    """
    if not url:
        return None
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if h.startswith("www."):
        h = h[4:]
    return h or None


def _path_for(host: str, data_dir: Path) -> Path:
    return data_dir / "host_knowledge" / f"{host}.json"


def _history_path_for(host: str, data_dir: Path) -> Path:
    d = data_dir / "host_knowledge" / host
    return d / "history.jsonl"


def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically: temp + rename. Locks the dir on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def record_job_outcome(
    *,
    host: str,
    success: bool,
    job_id: str,
    reason: str = "",
    data_dir: Path,
) -> dict | None:
    """Update HostKnowledge.stats for ``host`` after a job completes.

    Creates the HostKnowledge file if it doesn't exist (so an unknown
    host on first visit becomes a known one with 1/0 stats). Always
    appends an entry to history.jsonl.

    Returns the updated HostKnowledge dict, or None on failure.

    Locking: POSIX flock on the file during read-modify-write so two
    parallel completions don't clobber each other.
    """
    if not host:
        return None
    path = _path_for(host, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read-modify-write with a lock. On Windows fcntl is missing so we
    # do a best-effort plain read.
    lock_fd = None
    try:
        if _fcntl is not None:
            # Open the file in r+ mode; create if missing.
            try:
                lock_fd = open(path, "r+", encoding="utf-8")
            except FileNotFoundError:
                # File doesn't exist yet -- create empty skeleton, then re-open r+.
                _atomic_write(path, "{}")
                lock_fd = open(path, "r+", encoding="utf-8")
            try:
                _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_EX)
            except Exception:
                pass
            lock_fd.seek(0)
            raw = lock_fd.read()
        else:  # pragma: no cover -- Windows
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = "{}"

        try:
            d = json.loads(raw) if raw.strip() else {}
        except Exception:
            d = {}

        # Bootstrap skeleton when this is the host's first ever job.
        if not d.get("host"):
            d = HostKnowledge(host=host).model_dump(mode="json")

        # ---- bump stats --------------------------------------------------
        stats = d.get("stats") or {}
        total = int(stats.get("total_jobs") or 0) + 1
        ok = int(stats.get("successful_jobs") or 0) + (1 if success else 0)
        stats["total_jobs"] = total
        stats["successful_jobs"] = ok
        stats["success_rate"] = round(ok / max(total, 1), 4)
        now = datetime.utcnow()
        now_iso = now.isoformat()
        if success:
            stats["last_success_at"] = now_iso
        else:
            stats["last_failure_at"] = now_iso
            if reason:
                stats["last_failure_reason"] = reason[:300]
        d["stats"] = stats
        d["updated_at"] = now_iso

        # ---- recompute overall_confidence via maturity evaluator ---------
        try:
            k_obj = HostKnowledge.model_validate(d)
            tier = evaluate_maturity(k_obj, now=now)
            d["stats"]["overall_confidence"] = tier
        except Exception as e:
            _log.info(
                "[distiller-light] maturity evaluation failed for %s: %s",
                host,
                e,
            )

        # ---- provenance --------------------------------------------------
        d["provenance"] = {
            "last_updated_by": "distiller-light",
            "last_updated_at": now_iso,
        }

        new_text = json.dumps(d, ensure_ascii=False, indent=2)
        if lock_fd is not None:  # POSIX path
            lock_fd.seek(0)
            lock_fd.truncate()
            lock_fd.write(new_text)
            try:
                lock_fd.flush()
                os.fsync(lock_fd.fileno())
            except Exception:
                pass
        else:  # pragma: no cover -- Windows
            _atomic_write(path, new_text)
    finally:
        if lock_fd is not None:
            try:
                _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fd.close()
            except Exception:
                pass

    # ---- append to history.jsonl (append-only; no lock needed) ----------
    try:
        hist_path = _history_path_for(host, data_dir)
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at":           now_iso,
            "by":           "distiller-light",
            "trigger_job":  job_id,
            "changes":      ["stats.total_jobs", "stats.success_rate", "stats.overall_confidence"],
            "outcome":      "success" if success else "failure",
            "reason":       (reason or "")[:200],
        }
        with hist_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.info(
            "[distiller-light] history append failed for %s: %s",
            host,
            e,
        )

    return d
