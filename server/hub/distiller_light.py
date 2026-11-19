"""Lightweight distiller — no LLM, just bookkeeping.

v2 Phase 5 helper. Updates ``HostKnowledge.stats`` after each completed
job so the maturity tier (low/medium/high/stale) reflects reality. Pure
heuristics: bumps total_jobs / successful_jobs / success_rate /
last_*_at, and recomputes overall_confidence via the maturity evaluator.

Deeper distillation -- "this attempt revealed a new barrier strategy,
write it into per_page.barriers" -- is the R1 Distiller's job (Phase 6
or later). This module is the cheap baseline that runs on every job so
the operator UI can show meaningful confidence numbers from day one.

Writes: a job outcome only moves counters, so rewriting the whole
knowledge file (with an fsync) for each one was most of what the hub
still wrote to its disk. Outcomes can instead be counted in memory and
added to the file at most once per host per interval -- see
``set_write_interval``. The result in the file is the same as applying
them one by one; what changes is when it gets there. With the interval
at 0 (the default until the hub's loop sets it) every outcome is written
at once, as before.

A host seen for the first time is always written at once, so its file
exists for the readers that skip a host without one.

Concurrent writes: two jobs against the same host can complete within
milliseconds of each other, on different threads of the hub process. One
lock serialises the read-modify-write and the file is replaced by
temp + rename, so a reader never sees a half-written file.

There is no per-outcome line in ``history.jsonl`` any more: nothing read
them, and they grew without bound (over 1 GB on one hub). The R1
distiller still records its own, far rarer, updates there.

Public API:
  * ``record_job_outcome(host, success, *, job_id, reason)``
    -- counts the outcome into ``data/host_knowledge/{host}.json``.
       Returns the new HostKnowledge dict when it was written at once,
       None when it is waiting in memory (or on failure).
  * ``set_write_interval(seconds)`` / ``flush(...)`` / ``pending_count()``
    -- how long outcomes may wait, and writing out the ones that have.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core.host_knowledge import HostKnowledge, Stats, evaluate_maturity


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


def _knowledge_root(data_dir: Path) -> Path:
    """``host_knowledge/`` is the hub's own state, not a job's file: it stays
    in ``config.data_dir`` even when job files live on a ramdisk."""
    from server.hub._state import state_dir_for
    return state_dir_for(data_dir) / "host_knowledge"


def _path_for(host: str, data_dir: Path) -> Path:
    return _knowledge_root(data_dir) / f"{host}.json"


def _history_path_for(host: str, data_dir: Path) -> Path:
    """Where the R1 distiller keeps a host's update history. This module no
    longer writes there (see the module docstring)."""
    d = _knowledge_root(data_dir) / host
    return d / "history.jsonl"


def _atomic_write(path: Path, content: str, *, durable: bool = True) -> None:
    """Write file atomically: temp + rename. ``durable`` adds an fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            if durable:
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


# ---------------------------------------------------------------------------
# Outcomes waiting in memory
# ---------------------------------------------------------------------------


@dataclass
class _Tally:
    """Job outcomes for one host that have not reached its file yet."""

    host: str
    path: Path
    jobs: int
    ok: int
    last_at: datetime                       # newest outcome
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_reason: str | None         # newest non-empty failure reason
    since: float                            # monotonic time of the oldest outcome

    @classmethod
    def one(cls, host: str, path: Path, success: bool, reason: str, now: datetime) -> "_Tally":
        return cls(
            host=host,
            path=path,
            jobs=1,
            ok=1 if success else 0,
            last_at=now,
            last_success_at=now if success else None,
            last_failure_at=None if success else now,
            last_failure_reason=(reason[:300] or None) if (reason and not success) else None,
            since=time.monotonic(),
        )

    def add(self, newer: "_Tally") -> None:
        """Fold outcomes that happened after this tally's into it."""
        self.jobs += newer.jobs
        self.ok += newer.ok
        self.last_at = newer.last_at
        if newer.last_success_at is not None:
            self.last_success_at = newer.last_success_at
        if newer.last_failure_at is not None:
            self.last_failure_at = newer.last_failure_at
        if newer.last_failure_reason:
            self.last_failure_reason = newer.last_failure_reason


_lock = threading.Lock()        # guards _held and _interval_s
_file_lock = threading.Lock()   # one read-modify-write of a knowledge file at a time
_held: dict[str, _Tally] = {}
_interval_s: float = 0.0


def write_interval_s() -> float:
    return _interval_s


def pending_count() -> int:
    with _lock:
        return len(_held)


def set_write_interval(seconds: float) -> int:
    """How long a host's outcomes may wait in memory before they are added
    to its file. 0 writes every outcome at once; switching to 0 writes out
    whatever is waiting and returns how many files that was."""
    global _interval_s
    with _lock:
        _interval_s = max(0.0, float(seconds))
        off = _interval_s <= 0
    return flush() if off else 0


def _apply(t: _Tally, *, durable: bool = True) -> dict:
    """Add a tally to its host's file. Same result as applying its outcomes
    one by one, in order."""
    with _file_lock:
        try:
            raw = t.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        except UnicodeDecodeError:
            raw = ""   # not text any more: treated like unparseable JSON below
        try:
            d = json.loads(raw) if raw.strip() else {}
        except Exception:
            d = {}

        # Bootstrap skeleton when this is the host's first ever job.
        if not isinstance(d, dict) or not d.get("host"):
            d = HostKnowledge(host=t.host).model_dump(mode="json")

        # ---- bump stats --------------------------------------------------
        stats = d.get("stats")
        if not isinstance(stats, dict):
            stats = {}
        total = int(stats.get("total_jobs") or 0) + t.jobs
        ok = int(stats.get("successful_jobs") or 0) + t.ok
        stats["total_jobs"] = total
        stats["successful_jobs"] = ok
        stats["success_rate"] = round(ok / max(total, 1), 4)
        if t.last_success_at is not None:
            stats["last_success_at"] = t.last_success_at.isoformat()
        if t.last_failure_at is not None:
            stats["last_failure_at"] = t.last_failure_at.isoformat()
            if t.last_failure_reason:
                stats["last_failure_reason"] = t.last_failure_reason
        d["stats"] = stats
        now_iso = t.last_at.isoformat()
        d["updated_at"] = now_iso

        # ---- recompute overall_confidence via maturity evaluator ---------
        # The tier depends on the stats alone, so only they are validated.
        # Validating the whole document made this fail for every host whose
        # per_page section predates the current schema -- most of them --
        # and their tier was never refreshed.
        try:
            k_obj = HostKnowledge(
                host=t.host,
                stats=Stats.model_validate(
                    {k: v for k, v in stats.items() if k != "overall_confidence"}
                ),
            )
            stats["overall_confidence"] = evaluate_maturity(k_obj, now=t.last_at)
        except Exception as e:
            _log.info(
                "[distiller-light] maturity evaluation failed for %s: %s",
                t.host,
                e,
            )

        # ---- provenance --------------------------------------------------
        d["provenance"] = {
            "last_updated_by": "distiller-light",
            "last_updated_at": now_iso,
        }

        _atomic_write(
            t.path, json.dumps(d, ensure_ascii=False, indent=2), durable=durable,
        )
    return d


def flush(
    *,
    only_due: bool = False,
    durable: bool = True,
    budget_s: float | None = None,
) -> int:
    """Add waiting outcomes to their files; returns how many files were
    written. ``only_due`` leaves the hosts that have not waited a full
    interval yet. ``budget_s`` stops early (shutdown must not hang on this);
    what is left stays in memory."""
    started = time.monotonic()
    with _lock:
        keys = [
            k for k, t in _held.items()
            if not only_due or started - t.since >= _interval_s
        ]
    written = 0
    for key in keys:
        if budget_s is not None and time.monotonic() - started > budget_s:
            break
        with _lock:
            t = _held.pop(key, None)
        if t is None:
            continue
        try:
            _apply(t, durable=durable)
            written += 1
        except Exception as e:
            _log.warning(
                "[distiller-light] could not write stats for %s (%d outcome(s) "
                "kept in memory): %s: %s",
                t.host, t.jobs, type(e).__name__, e,
            )
            # Keep the counts; try again after another full interval rather
            # than on every tick.
            t.since = time.monotonic()
            with _lock:
                newer = _held.get(key)
                if newer is not None:
                    t.add(newer)
                _held[key] = t
    return written


def record_job_outcome(
    *,
    host: str,
    success: bool,
    job_id: str,
    reason: str = "",
    data_dir: Path,
) -> dict | None:
    """Count a finished job into HostKnowledge.stats for ``host``.

    Creates the HostKnowledge file if it doesn't exist (so an unknown
    host on first visit becomes a known one with 1/0 stats); that first
    write is never delayed.

    Returns the updated HostKnowledge dict when the outcome was written at
    once, None when it is waiting in memory (``set_write_interval``).

    ``job_id`` is accepted for the callers' sake; it used to go into a
    per-outcome history line that nothing read.
    """
    if not host:
        return None
    path = _path_for(host, data_dir)
    one = _Tally.one(host, path, success, reason, datetime.utcnow())
    with _lock:
        if _interval_s > 0 and path.is_file():
            key = str(path)
            waiting = _held.get(key)
            if waiting is None:
                _held[key] = one
            else:
                waiting.add(one)
            return None
    return _apply(one)
