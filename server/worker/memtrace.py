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


def _knob_text() -> str | None:
    """Contents of the knob file, or None when it is absent/unreadable.

    None means "not armed via the file" -- distinct from "" (armed, defaults),
    which is what ``touch`` produces.
    """
    try:
        from server.worker import scratch_pool
        pool = scratch_pool.pool_dir()
        if pool is None:
            return None
        return (pool / KNOB_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    except Exception:
        return None


def _knob(key: str) -> str | None:
    """One ``key=value`` setting from the knob file.

    The file is whitespace-separated ``key=value`` tokens, so the first round of
    tracing is ``touch .memtrace`` and the follow-up that needs call chains is::

        echo 'frames=5 window=90' > /ram/pdl/.memtrace

    Retuning has to work without a restart for the same reason arming does: the
    env vars are baked into the container at create time, and the first report
    is exactly what tells you the second run needs deeper frames. Restarting to
    change that would reset the very anon growth being measured.

    Unparseable tokens are ignored rather than fatal -- a typo in a knob must
    degrade to the default, never crash the guard thread that reads it.
    """
    raw = _knob_text()
    if not raw:
        return None
    for tok in raw.split():
        k, sep, v = tok.partition("=")
        if sep and k.strip() == key:
            return v.strip()
    return None


def enabled() -> bool:
    """True when the operator has armed leak tracing.

    Either ``PAPRIKA_MEMTRACE=1`` (baked into the container) or the knob file at
    the pool root (flippable live, node-wide). The file is checked on every call
    rather than cached so ``rm`` disarms a worker mid-window.
    """
    if _truthy(os.environ.get("PAPRIKA_MEMTRACE")):
        return True
    return _knob_text() is not None


def _tunable(knob_key: str, env_key: str, default: float, lo: float, hi: float) -> float:
    """Knob file > env > default, clamped. Never raises."""
    for raw in (_knob(knob_key), os.environ.get(env_key)):
        if raw is None or not str(raw).strip():
            continue
        try:
            return max(lo, min(hi, float(raw)))
        except (TypeError, ValueError):
            continue
    return default


def window_s() -> float:
    """How long to trace before reporting and stopping. Bounded on purpose:
    the trace table itself costs memory in a CT that is already short of it.

    Worth shortening when the leak bursts: a worker that goes from the 5500MB
    arm point to its 12GB cap in ~6 minutes can be killed -- by the kernel OOM
    killer or by the self-check's ``os._exit`` -- before a long window reports,
    and a report that never prints is a wasted run.
    """
    return _tunable("window", "PAPRIKA_MEMTRACE_WINDOW_S", 120.0, 30.0, 900.0)


def _nframes() -> int:
    """Frames kept per allocation.

    1 by default: the file:line of the allocation is the cheapest useful answer
    and each extra frame multiplies the trace table's own footprint. Raise it
    when the top growers are generic sites (``json/decoder.py``,
    ``httpx/_models.py``) that say WHERE bytes were allocated but not WHO keeps
    them alive -- the call chain is what names the retainer.
    """
    return int(_tunable("frames", "PAPRIKA_MEMTRACE_FRAMES", 1.0, 1.0, 10.0))


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
        # "traceback" groups by the whole call chain, "lineno" by the allocation
        # site alone. With one frame they are the same thing; with more, only
        # the former separates "json.loads called from the CDP reader" from
        # "json.loads called from the asset uploader" -- which is the entire
        # reason for asking for more frames.
        key = "traceback" if _nframes() > 1 else "lineno"
        snap = tracemalloc.take_snapshot()
        stats = snap.compare_to(_baseline, key) if _baseline else snap.statistics(key)
        grew = [s for s in stats if getattr(s, "size_diff", s.size) > 0][:limit]
        total = sum(getattr(s, "size_diff", s.size) for s in stats)
        lines = []
        for s in grew:
            # tracemalloc sorts a Traceback OLDEST-first since 3.7, so the
            # allocation site is traceback[-1] and traceback[0] is the outermost
            # frame (usually the event loop, identical for everything and
            # useless). Verified on 3.13.7. With nframes=1 only the most recent
            # frame is kept, which is why a one-frame report reads correctly off
            # traceback[0] and a deep one silently reads INVERTED -- print the
            # site explicitly rather than relying on the single-frame accident.
            tb = s.traceback
            head = (
                f"  {getattr(s, 'size_diff', s.size) / _MB:+9.1f}MB  "
                f"(blocks {getattr(s, 'count_diff', s.count):+8d})  {tb[-1]}"
            )
            # Callers, innermost first, indented under the allocation site. The
            # retainer is usually 2-3 frames up: the site is a library, the
            # keeper is ours.
            callers = [
                f"                                  <- {f}" for f in reversed(tb[:-1])
            ]
            lines.append("\n".join([head, *callers]))
        log.warning(
            "[memtrace] %s window %.0fs (%d frame(s)): %+.1fMB traced growth, "
            "top %d growers:\n%s",
            worker_id, elapsed, _nframes(), total / _MB, len(lines), "\n".join(lines),
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
