"""On-demand allocation-site attribution for the worker python RSS leak.

Why this exists
---------------
``worker-python-rss-leak-refault-storm``: the worker's anon memory climbs from
~400MB to 5.5-9.9GB and the CT gets OOM-killed.  Measured on garage 2026-08-13:
35-80 MB/min on a typical worker, 380 MB/min on a busy one, RSS ~100% anonymous
with ``[heap]`` dominant -- i.e. genuine Python object retention, not
fragmentation and not page cache.  The memory guard recycles the worker before
the kernel does (see ``_memory_guard_loop``), but that is a mitigation; nobody
has ever seen WHICH allocation site retains the objects.

Design constraints that shaped this
-----------------------------------
``tracemalloc`` cannot be left on: it stores a trace per live allocation, which
on a worker doing millions of small allocations is both CPU and memory the CT
does not have.  So it is armed only when a worker is already in trouble, kept
on for a bounded window, and stopped.  Starting late is not a problem *for this
leak*: it grows continuously, so a 2-minute delta at 35-80 MB/min attributes
70-160MB of fresh growth -- far above the noise floor -- without needing to have
traced the first five gigabytes.

Enabling must not require a container restart.  The env vars are baked into the
worker container at create time, so flipping one would mean re-creating every
container on the node.  Hence the same knob-file convention the scratch pool
already uses: ``touch /ram/pdl/.memtrace`` on the NODE arms every worker CT on
it, and ``rm`` disarms them, both instantly and with no restart.

Reading the output
------------------
The report is a delta against a baseline taken the moment tracing armed, sorted
by bytes gained, one line per source line::

    [memtrace] w51123 window 120s: +148.3MB total, top growers:
      +71.2MB  (+ 412331 blocks)  server/worker/agent/_mix_jobexec.py:884
      +38.9MB  (+   1204 blocks)  core/fetcher.py:2011
      ...

``blocks`` matters as much as bytes: a few huge blocks is a buffer nobody freed,
while hundreds of thousands of small ones is a container that keeps growing.
"""
from __future__ import annotations

import logging
import os
import time
import tracemalloc
from pathlib import Path

log = logging.getLogger("paprika.worker.memtrace")

#: Knob file at the pool root. Presence arms tracing; its contents are ignored.
KNOB_NAME = ".memtrace"

_MB = 1024.0 * 1024.0

# Module state. Only the memory-guard thread touches these, so no lock.
_armed_at: float = 0.0
_baseline: "tracemalloc.Snapshot | None" = None
_we_started_it: bool = False


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """True when the operator has armed leak tracing.

    Either ``PAPRIKA_MEMTRACE=1`` (baked into the container) or the knob file at
    the pool root (flippable live, node-wide). The file is checked on every call
    rather than cached so ``rm`` disarms a worker mid-window.
    """
    if _truthy(os.environ.get("PAPRIKA_MEMTRACE")):
        return True
    try:
        from server.worker import scratch_pool
        pool = scratch_pool.pool_dir()
        if pool is None:
            return False
        return (pool / KNOB_NAME).exists()
    except Exception:
        return False


def window_s() -> float:
    """How long to trace before reporting and stopping. Bounded on purpose:
    the trace table itself costs memory in a CT that is already short of it."""
    try:
        return max(30.0, float(os.environ.get("PAPRIKA_MEMTRACE_WINDOW_S") or 120.0))
    except (TypeError, ValueError):
        return 120.0


def _nframes() -> int:
    """Frames kept per allocation. 1 by default -- the file:line of the
    allocation is what identifies the leak, and each extra frame multiplies the
    trace table's own footprint."""
    try:
        return max(1, min(10, int(os.environ.get("PAPRIKA_MEMTRACE_FRAMES") or 1)))
    except (TypeError, ValueError):
        return 1


def arm(worker_id: str) -> bool:
    """Start tracing and take the baseline. Returns True if we armed now.

    Idempotent and never raises: a diagnostic that can break a worker is worse
    than no diagnostic. Already-armed, not-enabled, and "tracemalloc was already
    running for some other reason" all return False and change nothing.
    """
    global _armed_at, _baseline, _we_started_it
    if _armed_at or not enabled():
        return False
    try:
        if tracemalloc.is_tracing():
            # Someone else owns it; take a baseline but do not stop it later.
            _we_started_it = False
        else:
            tracemalloc.start(_nframes())
            _we_started_it = True
        _baseline = tracemalloc.take_snapshot()
        _armed_at = time.monotonic()
        cur, peak = tracemalloc.get_traced_memory()
        log.warning(
            "[memtrace] %s armed (%d frame(s), window %.0fs); baseline traced "
            "%.1fMB. Disarm with: rm %s",
            worker_id, _nframes(), window_s(), cur / _MB, KNOB_NAME,
        )
        return True
    except Exception:
        log.debug("[memtrace] arm failed", exc_info=True)
        _armed_at = 0.0
        _baseline = None
        return False


def due() -> bool:
    """True once the window has elapsed and a report is owed."""
    return bool(_armed_at) and (time.monotonic() - _armed_at) >= window_s()


def report(worker_id: str, limit: int = 20) -> None:
    """Log the top growers since ``arm`` and stop tracing. Never raises."""
    global _armed_at, _baseline, _we_started_it
    if not _armed_at:
        return
    elapsed = time.monotonic() - _armed_at
    try:
        snap = tracemalloc.take_snapshot()
        stats = snap.compare_to(_baseline, "lineno") if _baseline else snap.statistics("lineno")
        grew = [s for s in stats if getattr(s, "size_diff", s.size) > 0][:limit]
        total = sum(getattr(s, "size_diff", s.size) for s in stats)
        lines = [
            f"  {getattr(s, 'size_diff', s.size) / _MB:+9.1f}MB  "
            f"(blocks {getattr(s, 'count_diff', s.count):+8d})  {s.traceback[0]}"
            for s in grew
        ]
        log.warning(
            "[memtrace] %s window %.0fs: %+.1fMB traced growth, top %d growers:\n%s",
            worker_id, elapsed, total / _MB, len(lines), "\n".join(lines),
        )
    except Exception:
        log.debug("[memtrace] report failed", exc_info=True)
    finally:
        try:
            if _we_started_it and tracemalloc.is_tracing():
                tracemalloc.stop()
        except Exception:
            pass
        _armed_at = 0.0
        _baseline = None
        _we_started_it = False
