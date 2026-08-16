"""Shared node-tmpfs scratch pool for worker downloads.

Why
---
What fills a worker CT's LVM-thin rootfs is the video download: for every job
in the ``downloading`` phase yt-dlp writes a 1-2GB ``.mp4.part`` under
``/tmp`` -- which inside the container is the overlay, i.e. the CT rootfs, i.e.
the node's thin-pool (66 such downloads were in flight fleet-wide when this
was written).  Thin-pool exhaustion is what previously put a worker CT's ext4
into ``emergency_ro``, starving its heartbeat and cascading into ghost
workers.

This module moves those writes onto a **tmpfs that lives on the Proxmox node
and is bind-mounted into every worker CT** (``mp0: /ram/pdl,mp=/var/paprika/dl``).
Two properties make the shared-pool shape the right one:

  * the pool is shared by every CT on the node, so a single 1.8GB download can
    use space the other, idle CTs aren't using.  A per-CT private ramdisk of
    the same total size cannot: sized 1GB it holds neither the median (270MB
    with the x1.3 merge headroom) reliably nor the p90 (1.8GB) at all.

The memory the pool costs, and who pays it
------------------------------------------
This module was written believing that a host-mounted tmpfs is charged to the
host, leaving the CT's memory cgroup untouched.  **That is false**, and loft
proved it on 2026-08-06: cgroup v2 charges a shmem page to the cgroup that
first faults it in, and the process doing the faulting is yt-dlp inside the CT.
So a 5.8GB download lands 5.8GB against that CT's 8GB ``memory.max``.

The CTs run ``swap: 0``, so shmem cannot be reclaimed.  The only reclaimable
thing left is page cache, which was duly crushed from 6GB to 0.6MB -- after
which every executable and shared library the container touched had to be read
back off the disk on every access.  The node did 270MB/s of reads against
1.2MB/s of writes, ``io.pressure`` sat at 99% and load hit 397: a module that
exists to spare the disk had turned a write problem into a 15x larger read
problem.

Hence the second admission axis below.  The pool's own free space says whether
the NODE can take the bytes; it says nothing about whether the calling CT's
memory budget can.  Both must be true, or the download goes to disk -- which
costs one video's worth of writes and is unambiguously the cheaper failure.

Sharing is also the hazard.  The CTs are mutually invisible -- no lock server,
no hub involvement -- so this module NEVER deletes or accounts away another
worker's data.  Every directory it creates embeds the owner ``worker_id``, and
both the startup purge and the periodic sweep are owner-scoped.  Cross-worker
admission is coordinated through the pool itself: a claim file per live
directory, so a worker can see how many bytes its neighbours have promised but
not yet written.

Everything degrades to "behave exactly as before".  When the mount is absent
(``pool_dir()`` -> None), when the pool is short on space, or when
``PAPRIKA_SCRATCH_DISABLE=1``, ``acquire()`` returns None and the caller falls
back to ``tempfile.mkdtemp()`` on the CT disk -- the pre-existing code path.
That also means this module is inert on a worker whose CT has no ``mp0`` yet,
so the source rollout and the infra rollout are independent.

Sizing arithmetic (why the defaults are what they are)
------------------------------------------------------
One video costs ``size * 1.3`` (yt-dlp downloads video+audio, then muxes).
``PAPRIKA_SCRATCH_VIDEO_RESERVE_MB`` (2600) covers the measured p90 of 1.8GB.
With a 4GB pool and ``PAPRIKA_SCRATCH_MIN_FREE_MB`` 512 that admits exactly
ONE concurrent download per node; everything else falls back to disk.  The
lever for a higher hit rate is the pool size, and a tmpfs can be grown with no
downtime and without touching the CT or the container::

    mount -o remount,size=32G /ram/pdl

Env
---
``PAPRIKA_SCRATCH_POOL_DIR``       pool mountpoint (default /var/paprika/dl;
                                   falls back to PAPRIKA_DOWNLOAD_DIR when set)
``PAPRIKA_SCRATCH_DISABLE``        1 = never use the pool (kill switch)
``PAPRIKA_SCRATCH_MIN_FREE_MB``    headroom never handed out (default 512)
``PAPRIKA_SCRATCH_VIDEO_RESERVE_MB``  per-video claim (default 2600)

Both of those are node policy rather than per-worker config, so they can also
be set from the pool itself and take effect immediately, with no container
re-creation::

    echo 800 > /ram/pdl/.reserve_mb      # per-video claim
    echo 512 > /ram/pdl/.min_free_mb     # headroom

The cgroup axis is configured the same way.  It is INERT until the limit is
supplied, because the container cannot discover it: ``memory.max`` inside the
container reads ``max`` (the CT's cap lives on a parent cgroup that the cgroup
namespace hides), while ``/proc/meminfo`` shows the whole Proxmox node.  Only
``memory.current`` is real and correctly scoped.  The cap is uniform across a
node's worker CTs, so it is node policy like the two knobs above::

    echo 12288 > /ram/pdl/.cgroup_limit_mb    # = pct set <id> -memory
    echo 1536  > /ram/pdl/.cgroup_reserve_mb  # page cache floor + CT overhead

``PAPRIKA_WORKER_MEM_LIMIT_MB`` is honoured as a fallback for the limit.

Pool file wins over env, which wins over the default.
``PAPRIKA_SCRATCH_JOB``            1 = also pool per-job workdirs (default 0)
``PAPRIKA_SCRATCH_JOB_RESERVE_MB`` per-job-workdir claim (default 256)
``PAPRIKA_SCRATCH_CLAIM_TTL_S``    a claim not refreshed for this long stops
                                   counting against admission (default 10800)
"""
from __future__ import annotations

import logging
import os
import re as _re
import shutil
import tempfile
import time
from pathlib import Path

from . import cgroup_mem

log = logging.getLogger("paprika.worker.scratch")

#: Where the shared host-tmpfs pool is bind-mounted inside the CT. Matches
#: ``mp0: /ram/pdl,mp=/var/paprika/dl`` in the (uniform) worker CT config.
_DEFAULT_POOL_DIR = "/var/paprika/dl"

#: Claim files live here, one per live directory. Kept inside the pool on
#: purpose: the tmpfs IS the coordination medium between CTs that cannot see
#: each other.
_CLAIMS_DIRNAME = ".paprika-claims"

#: Overridable for tests.
_MOUNTS_PATH = "/proc/self/mounts"

_MB = 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name) or 0)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _pool_override_mb(name: str) -> int | None:
    """Read a tuning knob from a file at the pool root, e.g. ``.reserve_mb``.

    The pool is shared by every CT on the node, so these two numbers are
    NODE policy, not per-worker config -- and they are the numbers an
    operator actually needs to move: the video reserve is a worst-case guess
    (p90 x merge) while real usage is an order of magnitude smaller, so the
    right value is only knowable from live traffic.

    Putting them in the pool means ``echo 800 > /ram/pdl/.reserve_mb`` retunes
    every worker on the node instantly. The env vars are baked into the
    container at create time, so changing THOSE would mean re-creating 14
    containers to change one integer.

    Precedence: pool file > env > default. Unreadable/garbage -> ignored.
    """
    try:
        pool = configured_dir()
        raw = (pool / name).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def min_free_bytes() -> int:
    mb = _pool_override_mb(".min_free_mb")
    if mb is None:
        mb = _env_int("PAPRIKA_SCRATCH_MIN_FREE_MB", 512)
    return mb * _MB


def video_reserve_bytes() -> int:
    mb = _pool_override_mb(".reserve_mb")
    if mb is None:
        mb = _env_int("PAPRIKA_SCRATCH_VIDEO_RESERVE_MB", 2600)
    return mb * _MB


def cgroup_limit_bytes() -> int:
    """This CT's ``memory.max``, or 0 when nobody has told us.

    Not discoverable from inside the container -- see the module docstring --
    so it must be declared.  0 means "no signal", and every caller must treat
    that as "do not gate", never as "no headroom": an undeclared limit is the
    default state of a node that has not been configured yet, and gating on a
    guess would silently push every download onto the disk this module exists
    to protect.
    """
    mb = _pool_override_mb(".cgroup_limit_mb")
    if mb is None:
        mb = _env_int("PAPRIKA_WORKER_MEM_LIMIT_MB", 0)
    return max(0, mb) * _MB


def cgroup_reserve_bytes() -> int:
    """Bytes that must stay unspent inside the cgroup after a download lands.

    Two things have to fit in it:

    * a **page cache floor**.  The whole failure this axis prevents is page
      cache being reclaimed to zero, so the budget has to keep some.  Healthy
      CTs across the fleet hold 700-820MB of ``active_file``; 1GB covers that.
    * **CT-level overhead the container cannot see**.  ``memory.current`` here
      is the docker container's cgroup, but the cap is on the CT, which also
      carries systemd, dockerd and the lane processes -- a few hundred MB that
      would otherwise be double-spent.

    Default 1536MB = 1GB floor + 512MB overhead.
    """
    mb = _pool_override_mb(".cgroup_reserve_mb")
    if mb is None:
        mb = _env_int("PAPRIKA_SCRATCH_CGROUP_RESERVE_MB", 1536)
    return mb * _MB


def cgroup_headroom_bytes(pool: Path, worker_id: str) -> int | None:
    """Bytes this CT can still absorb as shmem, or None when the axis is inert.

    None (no declared limit, or no readable cgroup v2 controller) means "this
    axis has no opinion" and admission falls back to the pool check alone --
    exactly the behaviour that shipped before this axis existed.

    Our own outstanding claims are subtracted because they are bytes we have
    promised to write but have not written yet: they are not in
    ``memory.current`` and would otherwise be handed out twice by two
    concurrent downloads on the same worker.  Neighbours' claims are NOT
    subtracted -- they are charged to their own cgroups, not ours.  That
    asymmetry is the whole point of having two axes.
    """
    limit = cgroup_limit_bytes()
    if limit <= 0:
        return None
    s = cgroup_mem.sample()
    if not s.ok:
        return None
    spent = s.current + outstanding_bytes(pool, owner=worker_id)
    return max(0, limit - spent - cgroup_reserve_bytes())


def job_reserve_bytes() -> int:
    return _env_int("PAPRIKA_SCRATCH_JOB_RESERVE_MB", 256) * _MB


def claim_ttl_s() -> int:
    return _env_int("PAPRIKA_SCRATCH_CLAIM_TTL_S", 10800)


def job_workdirs_enabled() -> bool:
    """Per-job workdirs (assets/page.html/log) on the pool. Default OFF: on a
    4GB pool the video reserve is the whole budget, and a workdir can itself
    receive an inline video download. Flip this on once the pool is grown."""
    return _truthy("PAPRIKA_SCRATCH_JOB")


def _is_tmpfs(p: Path) -> bool:
    """True when *p* is (under) a mounted tmpfs/ramfs.

    Deliberately strict: without this check a missing ``mp0`` would leave a
    plain directory on the CT rootfs, and we would happily write 2GB videos to
    the very disk this module exists to protect -- with shared-pool semantics
    on top, which is strictly worse than the status quo.
    """
    try:
        target = os.path.realpath(str(p))
    except OSError:
        return False
    best_len = -1
    best_fstype = ""
    try:
        with open(_MOUNTS_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                # /proc mounts octal-escape spaces etc. as \040.
                mnt = parts[1].replace("\\040", " ")
                fstype = parts[2]
                if target == mnt or target.startswith(
                    mnt if mnt.endswith("/") else mnt + "/"
                ):
                    if len(mnt) > best_len:
                        best_len = len(mnt)
                        best_fstype = fstype
    except OSError:
        return False
    return best_fstype in ("tmpfs", "ramfs")


def configured_dir() -> Path:
    """The pool path we WOULD use (regardless of whether it is mounted)."""
    v = (
        os.environ.get("PAPRIKA_SCRATCH_POOL_DIR")
        or os.environ.get("PAPRIKA_DOWNLOAD_DIR")
        or _DEFAULT_POOL_DIR
    )
    return Path(v.strip() or _DEFAULT_POOL_DIR)


def pool_dir() -> Path | None:
    """The usable pool, or None when it is disabled / absent / not a tmpfs.

    Cheap enough to call per job (one small /proc read + a stat), and NOT
    cached on purpose: the mount can appear or disappear under a running
    worker (node maintenance, ``remount``), and a stale "yes" would send a
    2GB download to the CT disk under a name only the pool sweeper knows.
    """
    if _truthy("PAPRIKA_SCRATCH_DISABLE"):
        return None
    p = configured_dir()
    try:
        if not p.is_dir():
            return None
    except OSError:
        return None
    if not _is_tmpfs(p):
        return None
    return p


def free_bytes(pool: Path) -> int:
    try:
        st = os.statvfs(str(pool))
        return int(st.f_bavail) * int(st.f_frsize)
    except OSError:
        return 0


def total_bytes(pool: Path) -> int:
    try:
        st = os.statvfs(str(pool))
        return int(st.f_blocks) * int(st.f_frsize)
    except OSError:
        return 0


def _claims_dir(pool: Path) -> Path:
    return pool / _CLAIMS_DIRNAME


def _dir_used_bytes(d: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(str(d)):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def outstanding_bytes(pool: Path, owner: str | None = None) -> int:
    """Bytes other workers (and we) have claimed but not yet written.

    With *owner* set, only that worker's claims are counted -- used by the
    cgroup axis, where a neighbour's promise is irrelevant because it will be
    charged to the neighbour's cgroup, not ours.

    ``free`` alone is not a safe admission signal on a shared pool: a
    neighbouring CT may be 30 seconds into a 1.8GB download, so most of what
    it will consume is not yet visible.  A claim records the promised total;
    what remains outstanding is ``promised - already written``.

    Self-healing by construction: a claim whose directory is gone is stale
    (the directory is always created BEFORE its claim), so it is unlinked and
    ignored.  That is what lets every pre-existing ``rmtree(workdir)`` call
    site stay untouched -- deleting a pooled directory without telling us can
    never leak accounting.
    """
    claims = _claims_dir(pool)
    ttl = claim_ttl_s()
    now = time.time()
    total = 0
    try:
        entries = list(claims.iterdir())
    except OSError:
        return 0
    token = _owner_token(owner) if owner else None
    for c in entries:
        if c.suffix != ".claim":
            continue
        if token is not None and token not in c.stem:
            continue
        d = pool / c.stem
        try:
            if not d.is_dir():
                c.unlink(missing_ok=True)
                continue
            if now - c.stat().st_mtime > ttl:
                # Owner is gone or wedged. Stop reserving space for it; the
                # directory itself is only ever removed by its own owner.
                continue
            need = int((c.read_text(encoding="utf-8") or "0").strip() or 0)
        except (OSError, ValueError):
            continue
        total += max(0, need - _dir_used_bytes(d))
    return total


def _owner_token(worker_id: str) -> str:
    return f"-{worker_id}-"


#: Worker ids are ``w<3rd octet><4th octet>`` (server/worker/agent/workerid.py)
#: with a container-hash fallback. Anything outside this shape is not an owner
#: id we put there, and a directory we cannot attribute is never reclaimed.
_OWNER_RE = _re.compile(r"^[A-Za-z0-9_]{2,64}$")


def orphan_min_age_s() -> int:
    """How long a directory AND its claim must have gone untouched before
    another worker on the node may reclaim it.

    Deliberately far longer than anything a live owner can be quiet for: the
    longest download is ``PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S`` (2h) and the
    owner refreshes its claims every sweep (30 min), so 6h of silence on BOTH
    signals means the owner is not running.
    """
    return _env_int("PAPRIKA_SCRATCH_ORPHAN_MIN_AGE_S", 21600)


def _dir_owner(name: str) -> str | None:
    """The worker id embedded in a pool directory name, or None.

    ``acquire`` builds ``<caller prefix><worker_id>-<mkdtemp suffix>``. Neither
    the job id (hex) nor mkdtemp's suffix (letters/digits/underscore) contains
    a dash, so the owner is the second-to-last dash-separated field.

    The ``paprika-`` gate is what keeps this from attributing an owner to
    something we did not create: every caller of :func:`acquire` passes a
    ``paprika-...`` prefix, and a name we cannot attribute must never become
    a reclaim candidate.
    """
    if not name.startswith("paprika-"):
        return None
    parts = name.rsplit("-", 2)
    if len(parts) != 3:
        return None
    owner = parts[1]
    return owner if owner and _OWNER_RE.match(owner) else None


def sweep_orphans(worker_id: str, min_age_s: float | None = None) -> tuple[int, int]:
    """Reclaim pool directories whose owner no longer exists.

    Every other sweep here is owner-scoped, and that is normally the right
    contract -- the CTs on a node cannot see each other's processes, so only
    the owner can prove its own directory is dead. But it leaves one class of
    garbage with no collector at all: when a worker is deleted, renamed or
    re-provisioned (a CT rebuild, a changed ``WORKER_ID``), its directories
    stay on the node tmpfs forever because the one process allowed to remove
    them is gone. Measured 2026-08-16: ``w51180`` and ``w5143``, both absent
    from the fleet, between them held 15 directories -- one set 76 hours old --
    on RAM that the surviving CTs on those nodes were competing for.

    Reclaiming another CT's data needs evidence that cannot be faked by a
    quiet-but-live neighbour, so THREE things must all hold:

      * the directory itself has not been written to for ``min_age_s``;
      * its claim file has not been refreshed for ``min_age_s`` either --
        ``touch_own`` runs every sweep, so a live owner cannot be silent for
        6h while holding a download that caps out at 2h;
      * that owner has no OTHER claim in the pool that IS fresh, which is what
        rules out an owner whose claim file was lost rather than abandoned.

    Deliberately does NOT consult the hub's worker list: ``/workers`` is
    served per-hub and a degraded hub answers with a fraction of the fleet
    (observed: 16 of 211), which as an input here would mean deleting live
    workers' downloads. The pool speaks for itself.

    Returns ``(removed, freed_bytes)``. Set ``PAPRIKA_SCRATCH_ORPHAN_DISABLE``
    to turn it off.
    """
    pool = pool_dir()
    if pool is None or _truthy("PAPRIKA_SCRATCH_ORPHAN_DISABLE"):
        return (0, 0)
    age = float(orphan_min_age_s() if min_age_s is None else min_age_s)
    if age <= 0:
        return (0, 0)
    now = time.time()

    claim_mtime: dict[str, float] = {}
    fresh_owners: set[str] = set()
    try:
        claim_entries = list(_claims_dir(pool).iterdir())
    except OSError:
        claim_entries = []
    for c in claim_entries:
        if c.suffix != ".claim":
            continue
        try:
            m = c.stat().st_mtime
        except OSError:
            continue
        claim_mtime[c.stem] = m
        owner = _dir_owner(c.stem)
        if owner and now - m < age:
            fresh_owners.add(owner)

    removed = 0
    freed = 0
    try:
        entries = list(pool.iterdir())
    except OSError:
        return (0, 0)
    for d in entries:
        if d.name == _CLAIMS_DIRNAME or d.name.startswith("."):
            continue
        owner = _dir_owner(d.name)
        if not owner or owner == worker_id or owner in fresh_owners:
            continue
        try:
            if not d.is_dir():
                continue
            if now - d.stat().st_mtime < age:
                continue
        except OSError:
            continue
        m = claim_mtime.get(d.name)
        if m is not None and now - m < age:
            continue
        size = _dir_used_bytes(d)
        release(d)
        removed += 1
        freed += size
        log.info(
            "scratch pool: reclaimed orphan %s (owner %s gone, idle %.1fh, "
            "%d MB)", d.name, owner, (now - m if m else age) / 3600.0,
            size // _MB,
        )
    return (removed, freed)


def acquire(prefix: str, worker_id: str, need_bytes: int) -> Path | None:
    """Reserve ``need_bytes`` and return a fresh directory in the pool.

    ``prefix`` is the caller's usual mkdtemp prefix (e.g.
    ``paprika-vid-<job_id>-``); the owner id is appended to it, so the final
    name is ``paprika-vid-<job_id>-<worker_id>-<rand>``.  Keeping the caller's
    prefix CONTIGUOUS matters: ``_terminate_ytdlp_descendants_for_job`` finds
    a stuck download by searching argv for ``paprika-vid-<job_id>``, and the
    tmp sweeper protects live work by matching job/session ids in the name.

    Returns None whenever the pool cannot take the job -- the caller must fall
    back to ``tempfile.mkdtemp()`` on local disk.
    """
    pool = pool_dir()
    if pool is None:
        return None
    need = max(0, int(need_bytes))
    try:
        claims = _claims_dir(pool)
        claims.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    floor = min_free_bytes()
    if free_bytes(pool) - outstanding_bytes(pool) - floor < need:
        return None

    # Second axis: can OUR cgroup absorb these bytes as shmem? The pool check
    # above only established that the node can. Skipped entirely when the axis
    # is inert (no declared limit) -- see cgroup_headroom_bytes.
    headroom = cgroup_headroom_bytes(pool, worker_id)
    if headroom is not None and headroom < need:
        log.info(
            "scratch pool: %s needs %d MB but cgroup headroom is %d MB "
            "(limit %d MB, reserve %d MB); using local disk",
            prefix, need // _MB, headroom // _MB,
            cgroup_limit_bytes() // _MB, cgroup_reserve_bytes() // _MB,
        )
        return None

    try:
        d = Path(tempfile.mkdtemp(prefix=f"{prefix}{worker_id}-", dir=str(pool)))
    except OSError as e:
        log.info("scratch pool: mkdtemp failed (%s); using local disk", e)
        return None
    claim = claims / f"{d.name}.claim"
    try:
        claim.write_text(str(need), encoding="utf-8")
    except OSError:
        shutil.rmtree(d, ignore_errors=True)
        return None

    # Double-check after publishing our claim. Two CTs can pass the check
    # above at the same instant; whoever notices the over-subscription backs
    # off to local disk. Both backing off is fine (safe direction) -- a
    # needless disk write is cheap, an ENOSPC 90 minutes into a download is
    # not.
    if free_bytes(pool) - outstanding_bytes(pool) < floor:
        release(d)
        return None
    return d


def release(path: Path | str | None) -> None:
    """Drop a directory and its claim. Safe on non-pool paths and on None."""
    if not path:
        return
    p = Path(path)
    pool = pool_dir()
    if pool is not None:
        try:
            if p.parent.resolve() == pool.resolve():
                (_claims_dir(pool) / f"{p.name}.claim").unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(p, ignore_errors=True)


def touch_own(worker_id: str) -> int:
    """Refresh our claims so they keep counting. Called from the maintenance
    loop; a download can run for 2h (``PAPRIKA_VIDEO_DOWNLOAD_TIMEOUT_S``),
    far longer than any sane TTL, and an unrefreshed claim would let the
    node over-admit while we are still writing."""
    pool = pool_dir()
    if pool is None:
        return 0
    tok = _owner_token(worker_id)
    n = 0
    try:
        entries = list(_claims_dir(pool).iterdir())
    except OSError:
        return 0
    now = time.time()
    for c in entries:
        if c.suffix != ".claim" or tok not in c.stem:
            continue
        try:
            if not (pool / c.stem).is_dir():
                continue
            os.utime(c, (now, now))
            n += 1
        except OSError:
            continue
    return n


def purge_own(worker_id: str) -> tuple[int, int]:
    """Remove every directory this worker owns. Startup only.

    Safe wholesale precisely because it is owner-scoped AND runs before any
    job: our own leftovers are from a dead prior process, while a neighbour's
    directory (same pool, different ``worker_id``) is never touched.
    """
    pool = pool_dir()
    if pool is None:
        return (0, 0)
    tok = _owner_token(worker_id)
    removed = 0
    freed = 0
    try:
        entries = list(pool.iterdir())
    except OSError:
        return (0, 0)
    for d in entries:
        if d.name == _CLAIMS_DIRNAME or tok not in d.name:
            continue
        try:
            if not d.is_dir():
                continue
            freed += _dir_used_bytes(d)
        except OSError:
            continue
        release(d)
        removed += 1
    return (removed, freed)


def sweep_own(
    worker_id: str, keep_tokens: set[str], min_age_s: float,
) -> tuple[int, int]:
    """Owner-scoped periodic sweep, mirroring the /tmp sweeper's contract.

    ``keep_tokens`` are live session / job ids: a directory whose name embeds
    one is preserved regardless of age.  That protection is what keeps a
    2-hour single-file download alive -- its directory mtime never moves once
    yt-dlp has created the ``.part``, so age alone would sweep it out from
    under the running process.
    """
    pool = pool_dir()
    if pool is None:
        return (0, 0)
    tok = _owner_token(worker_id)
    now = time.time()
    removed = 0
    freed = 0
    try:
        entries = list(pool.iterdir())
    except OSError:
        return (0, 0)
    for d in entries:
        if d.name == _CLAIMS_DIRNAME or tok not in d.name:
            continue
        try:
            if not d.is_dir():
                continue
            if now - d.stat().st_mtime < min_age_s:
                continue
        except OSError:
            continue
        if any(t and t in d.name for t in keep_tokens):
            continue
        freed += _dir_used_bytes(d)
        release(d)
        removed += 1
    # Claims orphaned by someone else's rmtree cost nothing but confuse the
    # accounting read; outstanding_bytes() unlinks them as it goes.
    try:
        outstanding_bytes(pool)
    except Exception:
        pass
    return (removed, freed)


def status_line() -> str:
    """One-line human summary for the startup log."""
    if _truthy("PAPRIKA_SCRATCH_DISABLE"):
        return "scratch pool: DISABLED (PAPRIKA_SCRATCH_DISABLE=1)"
    p = configured_dir()
    pool = pool_dir()
    if pool is None:
        return (
            f"scratch pool: not active at {p} (no tmpfs mounted) -- "
            f"downloads stay on local disk"
        )
    return (
        f"scratch pool: {pool} tmpfs {total_bytes(pool) // _MB} MB "
        f"({free_bytes(pool) // _MB} MB free), "
        f"video reserve {video_reserve_bytes() // _MB} MB, "
        f"min-free {min_free_bytes() // _MB} MB, "
        f"job workdirs {'on' if job_workdirs_enabled() else 'off'}"
    )
