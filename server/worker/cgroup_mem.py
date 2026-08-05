"""Worker-side cgroup v2 memory sampler (the signal the memory guard runs on).

Why not /proc/meminfo
---------------------
``_sample_resources`` in ``agent/_mix_run.py`` has always derived ``mem_pct``
from ``/proc/meminfo``, documented as "CT-local (cgroup memory)".  On the
production fleet that is **false**, and measurably so: the workers run as a
docker container INSIDE an LXC CT, and while lxcfs virtualises ``/proc/meminfo``
for the CT, the container mounts a fresh procfs that shows the bare kernel's
numbers.  Read from inside ``paprika-worker-1`` on boiler CT 382::

    MemTotal:  395,718,540 kB   <- 377GB = the Proxmox NODE, not the 8GB CT

So every worker on a node reports that node's free memory, identically, and a
CT can be seconds from OOM while ``mem_pct`` reads 15%.  That is why the
2026-08-02 refault storm was invisible in the admin Workers tab.

``/sys/fs/cgroup`` is NOT virtualised away: with cgroup namespaces the
container sees its OWN cgroup as the root, so ``memory.current``,
``memory.stat`` and ``memory.pressure`` are all real and correctly scoped.

Why three signals
-----------------
``MemAvailable``-shaped metrics cannot see the failure we care about.  In the
incident the CT sat at ``memory.current == memory.max == 8G`` with only ~1.2G
anon: the rest was page cache being refaulted in a loop (executables and
libraries evicted, then immediately read back).  Page cache counts as
*available*, so a percentage-of-limit gauge reads healthy while the box does
24k read IOPS of 2KB random IO and its heartbeat starves.  The three signals
here are chosen so each catches a failure the others miss:

``anon``
    The leak.  Monotonic growth of anonymous memory -- the known worker-python
    RSS leak ([[worker-python-rss-leak-refault-storm]]).  Measured healthy
    baseline on boiler: 1.6-2.1GB, climbing 78-215MB per 31s under load.

``pgmajfault`` rate
    The thrash, measured where it happens.  A major fault is by definition a
    page that had to come off disk.  Healthy baseline measured across three
    boiler CTs: **41-75 faults/s**.  CT356 during the storm had accumulated
    18.4 MILLION.  This is the most reliable of the three because faults are
    charged to the cgroup that takes them, regardless of where reclaim ran.

``memory.pressure`` (PSI)
    The thrash, measured as what the operator actually cares about: wall-clock
    time tasks spent stalled on memory.  Healthy baseline: 0.00 across all
    windows.  Cheap and unambiguous when it moves.

Everything degrades to "no signal": a missing file, a cgroup v1 host, or an
unparseable field yields ``ok=False`` and the guard stays inert rather than
recycling workers on a guess.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

#: cgroup v2 mount point as seen from inside the container.
CGROUP_ROOT = "/sys/fs/cgroup"

_MB = 1024 * 1024


@dataclass
class MemSample:
    """One point-in-time read of this cgroup's memory state.

    ``ok=False`` means "no usable cgroup v2 memory controller here" -- every
    consumer must treat that as "do nothing", never as "healthy" or "zero".
    """

    ok: bool = False
    #: Bytes charged to this cgroup (anon + page cache + kernel).
    current: int = 0
    #: Hard limit in bytes, or 0 when unlimited/unknown. Inside the container
    #: this is normally 0: the docker container has no limit of its own, the
    #: 8GB cap lives on the parent (CT) cgroup, which a cgroup namespace hides.
    #: Hence the guard thresholds below are ABSOLUTE, not percentages.
    limit: int = 0
    anon: int = 0
    file: int = 0
    shmem: int = 0
    #: Cumulative counters. Only their RATE is meaningful (see MemRates).
    pgmajfault: int = 0
    refault_file: int = 0
    #: PSI "some"/"full" averages, percent of wall-clock time stalled.
    psi: dict[str, float] = field(default_factory=dict)

    @property
    def limit_pct(self) -> float | None:
        """Percent of the cgroup's own limit, or None when there is no limit.

        Deliberately returns None rather than 0.0 so a caller can't mistake
        "unlimited" for "empty" -- the exact confusion that made the old
        ``mem_pct`` misleading.
        """
        if not self.ok or self.limit <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * self.current / self.limit))

    @property
    def psi_some_avg60(self) -> float:
        return self.psi.get("some_avg60", 0.0)

    @property
    def psi_full_avg60(self) -> float:
        return self.psi.get("full_avg60", 0.0)


def _read_text(name: str) -> str | None:
    try:
        with open(os.path.join(CGROUP_ROOT, name), "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _read_int(name: str) -> int:
    """Parse a single-value cgroup file. ``max`` (= unlimited) reads as 0."""
    raw = _read_text(name)
    if raw is None:
        return 0
    raw = raw.strip()
    if not raw or raw == "max":
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _read_stat() -> dict[str, int]:
    raw = _read_text("memory.stat")
    if raw is None:
        return {}
    out: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def _read_pressure() -> dict[str, float]:
    """Parse ``memory.pressure`` into ``{some_avg10, some_avg60, ...}``.

    Format (one line per kind)::

        some avg10=0.00 avg60=0.00 avg300=0.03 total=2547568
        full avg10=0.00 avg60=0.00 avg300=0.03 total=2547533
    """
    raw = _read_text("memory.pressure")
    if raw is None:
        return {}
    out: dict[str, float] = {}
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind not in ("some", "full"):
            continue
        for kv in parts[1:]:
            key, _, val = kv.partition("=")
            if not val:
                continue
            try:
                out[f"{kind}_{key}"] = float(val)
            except ValueError:
                continue
    return out


def _env_limit_bytes() -> int:
    """Operator-declared cgroup limit for this worker, in MB.

    The CT's 8GB cap is invisible from inside the container (see MemSample.limit),
    so an operator who wants a percentage in the admin UI can declare it. Purely
    cosmetic: the guard's own thresholds never depend on it.
    """
    try:
        v = int(os.environ.get("PAPRIKA_WORKER_MEM_LIMIT_MB") or 0)
    except (TypeError, ValueError):
        return 0
    return v * _MB if v > 0 else 0


def available() -> bool:
    """True when this process can read a cgroup v2 memory controller."""
    return _read_text("memory.current") is not None


def sample() -> MemSample:
    """Read the cgroup once. Never raises."""
    cur_raw = _read_text("memory.current")
    if cur_raw is None:
        return MemSample(ok=False)
    try:
        current = int(cur_raw.strip())
    except ValueError:
        return MemSample(ok=False)
    st = _read_stat()
    return MemSample(
        ok=True,
        current=current,
        limit=_read_int("memory.max") or _env_limit_bytes(),
        anon=st.get("anon", 0),
        file=st.get("file", 0),
        shmem=st.get("shmem", 0),
        pgmajfault=st.get("pgmajfault", 0),
        refault_file=st.get("workingset_refault_file", 0),
        psi=_read_pressure(),
    )


def majfault_rate(prev: MemSample, cur: MemSample, dt_s: float) -> float:
    """Major faults per second between two samples.

    Returns 0.0 on a counter reset (container restarted under us), on a
    non-positive interval, or when either sample is unusable -- all cases where
    a computed rate would be fiction. Never negative.
    """
    if not (prev.ok and cur.ok) or dt_s <= 0:
        return 0.0
    delta = cur.pgmajfault - prev.pgmajfault
    if delta < 0:
        return 0.0
    return delta / dt_s


def refault_rate(prev: MemSample, cur: MemSample, dt_s: float) -> float:
    """Page-cache refaults per second between two samples. Same contract as
    ``majfault_rate``: this is the "evicted then immediately read back" counter,
    the cleanest signature of cache thrash as opposed to genuine first reads."""
    if not (prev.ok and cur.ok) or dt_s <= 0:
        return 0.0
    delta = cur.refault_file - prev.refault_file
    if delta < 0:
        return 0.0
    return delta / dt_s


def status_line(s: MemSample) -> str:
    """One-line human summary for the startup / trigger log."""
    if not s.ok:
        return "cgroup memory: unavailable (no cgroup v2 controller)"
    pct = s.limit_pct
    lim = f"{s.limit // _MB} MB ({pct:.0f}%)" if pct is not None else "unlimited"
    return (
        f"cgroup memory: current {s.current // _MB} MB / limit {lim}, "
        f"anon {s.anon // _MB} MB, file {s.file // _MB} MB, "
        f"shmem {s.shmem // _MB} MB, "
        f"PSI some avg60 {s.psi_some_avg60:.2f}"
    )
