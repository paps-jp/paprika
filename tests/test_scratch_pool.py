"""Regression: shared node-tmpfs scratch pool (server/worker/scratch_pool.py).

The pool is a tmpfs living on the Proxmox node, bind-mounted into EVERY worker
CT on that node. The CTs cannot see each other -- there is no lock server and
no hub involvement -- so the dangerous failures are all cross-worker:

  1. owner scoping   -- never delete a neighbour's live download
  2. claim accounting-- admission must see bytes promised-but-not-yet-written,
                        or two CTs both start a 1.8GB download into a 4GB pool
  3. self-healing    -- every pre-existing rmtree(workdir) call site stays
                        untouched, so a claim whose dir is gone must not leak
  4. inert by default-- no tmpfs mounted => byte-for-byte the old behaviour
                        (caller falls back to tempfile.mkdtemp on CT disk)

Property 4 is what lets the source roll out ahead of the infra.
"""

import os

import pytest

from server.worker import scratch_pool


MB = 1024 * 1024


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A 'mounted' pool at tmp_path: the tmpfs check is the only thing we
    cannot reproduce on a test box, so it is the only thing faked."""
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.delenv("PAPRIKA_SCRATCH_DISABLE", raising=False)
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: True)
    return tmp_path


def _free(monkeypatch, n_bytes):
    monkeypatch.setattr(scratch_pool, "free_bytes", lambda pool: n_bytes)


# --- 4. inert by default ---------------------------------------------------


def test_no_mount_means_no_pool(tmp_path, monkeypatch):
    """The whole point of the fallback: a worker whose CT has no mp0 yet must
    behave exactly as before, so source and infra can roll out separately."""
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    assert scratch_pool.pool_dir() is None
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 100 * MB) is None


def test_plain_directory_is_never_used(tmp_path, monkeypatch):
    """A real /var/paprika/dl that is NOT a tmpfs means the mp0 is missing.
    Writing there would put 2GB videos on the very disk this protects, under
    shared-pool naming -- strictly worse than the status quo."""
    monkeypatch.setenv("PAPRIKA_SCRATCH_POOL_DIR", str(tmp_path))
    monkeypatch.setattr(scratch_pool, "_MOUNTS_PATH", os.devnull)
    assert scratch_pool.pool_dir() is None


def test_kill_switch(pool, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_DISABLE", "1")
    assert scratch_pool.pool_dir() is None
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 1) is None


# --- naming contract -------------------------------------------------------


def test_dir_name_keeps_caller_prefix_contiguous(pool, monkeypatch):
    """_terminate_ytdlp_descendants_for_job finds a stuck download by
    searching argv for 'paprika-vid-<job_id>', and the tmp sweeper protects
    live work by matching ids in the name. Inserting the owner id in the
    middle would silently break force-complete."""
    _free(monkeypatch, 10_000 * MB)
    d = scratch_pool.acquire("paprika-vid-job42-", "w51175", 100 * MB)
    assert d is not None
    assert "paprika-vid-job42" in d.name
    assert "-w51175-" in d.name


# --- 2. claim accounting ---------------------------------------------------


def test_admission_counts_a_neighbours_unwritten_claim(pool, monkeypatch):
    """The failure this prevents: CT A is 30s into a 1.8GB download, so most
    of what it will consume is not yet on disk. Free space alone would let
    CT B start a second one and ENOSPC both."""
    _free(monkeypatch, 4000 * MB)
    a = scratch_pool.acquire("paprika-vid-j1-", "wA", 2600 * MB)
    assert a is not None
    # 4000 free - 2600 outstanding - 512 min-free < 2600 => refused.
    b = scratch_pool.acquire("paprika-vid-j2-", "wB", 2600 * MB)
    assert b is None


def test_claim_shrinks_as_bytes_land(pool, monkeypatch):
    """A claim reserves 'promised MINUS already written' -- otherwise a
    finished-but-not-yet-released download would double-count."""
    _free(monkeypatch, 4000 * MB)
    a = scratch_pool.acquire("paprika-vid-j1-", "wA", 1000 * MB)
    assert a is not None
    assert scratch_pool.outstanding_bytes(pool) == pytest.approx(
        1000 * MB, rel=0.01,
    )
    (a / "video.mp4").write_bytes(b"\0" * (4 * MB))
    assert scratch_pool.outstanding_bytes(pool) == pytest.approx(
        996 * MB, rel=0.01,
    )


def test_min_free_headroom_is_reserved(pool, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_MIN_FREE_MB", "512")
    _free(monkeypatch, 600 * MB)
    assert scratch_pool.acquire("paprika-vid-j1-", "wA", 200 * MB) is None
    assert scratch_pool.acquire("paprika-vid-j1-", "wA", 50 * MB) is not None


def test_stale_claim_stops_blocking_admission(pool, monkeypatch):
    """A wedged or killed neighbour must not reserve the pool forever; its
    claim ages out. (Its DIRECTORY still survives -- only its owner may
    delete that.)"""
    monkeypatch.setenv("PAPRIKA_SCRATCH_CLAIM_TTL_S", "60")
    _free(monkeypatch, 4000 * MB)
    a = scratch_pool.acquire("paprika-vid-j1-", "wA", 2600 * MB)
    assert a is not None
    claim = pool / scratch_pool._CLAIMS_DIRNAME / f"{a.name}.claim"
    old = os.stat(claim).st_mtime - 3600
    os.utime(claim, (old, old))
    assert scratch_pool.outstanding_bytes(pool) == 0
    assert scratch_pool.acquire("paprika-vid-j2-", "wB", 2600 * MB) is not None
    assert a.is_dir()


# --- 3. self-healing -------------------------------------------------------


def test_claim_is_dropped_when_someone_rmtrees_the_dir(pool, monkeypatch):
    """Several existing call sites rmtree the workdir directly (job finally,
    session_end). They must stay correct without knowing about the pool."""
    import shutil

    _free(monkeypatch, 4000 * MB)
    a = scratch_pool.acquire("paprika-j1-", "wA", 1000 * MB)
    assert a is not None
    shutil.rmtree(a)
    assert scratch_pool.outstanding_bytes(pool) == 0
    assert not (pool / scratch_pool._CLAIMS_DIRNAME / f"{a.name}.claim").exists()


def test_release_is_safe_on_non_pool_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(scratch_pool, "_is_tmpfs", lambda p: False)
    d = tmp_path / "local"
    d.mkdir()
    scratch_pool.release(d)
    assert not d.exists()
    scratch_pool.release(None)


# --- 1. owner scoping ------------------------------------------------------


def test_purge_own_leaves_neighbours_alone(pool, monkeypatch):
    """Startup purge. Ours are from a dead prior process; theirs may be a
    live 2-hour download in another CT."""
    _free(monkeypatch, 10_000 * MB)
    mine = scratch_pool.acquire("paprika-vid-j1-", "w51175", 10 * MB)
    theirs = scratch_pool.acquire("paprika-vid-j2-", "w51176", 10 * MB)
    n, _freed = scratch_pool.purge_own("w51175")
    assert n == 1
    assert not mine.exists()
    assert theirs.is_dir()


def test_sweep_is_owner_scoped_and_respects_live_tokens(pool, monkeypatch):
    _free(monkeypatch, 10_000 * MB)
    live = scratch_pool.acquire("paprika-vid-jLIVE-", "w1", 10 * MB)
    stale = scratch_pool.acquire("paprika-vid-jOLD-", "w1", 10 * MB)
    neighbour = scratch_pool.acquire("paprika-vid-jN-", "w2", 10 * MB)
    for d in (live, stale, neighbour):
        old = os.stat(d).st_mtime - 7200
        os.utime(d, (old, old))

    n, _freed = scratch_pool.sweep_own("w1", {"jLIVE"}, min_age_s=1800)

    assert n == 1
    assert live.is_dir()        # protected by the live-job token
    assert not stale.exists()   # ours, old, not live -> gone
    assert neighbour.is_dir()   # never ours to delete


def test_touch_own_only_refreshes_our_claims(pool, monkeypatch):
    _free(monkeypatch, 10_000 * MB)
    mine = scratch_pool.acquire("paprika-vid-j1-", "w1", 10 * MB)
    theirs = scratch_pool.acquire("paprika-vid-j2-", "w2", 10 * MB)
    claims = pool / scratch_pool._CLAIMS_DIRNAME
    for d in (mine, theirs):
        c = claims / f"{d.name}.claim"
        os.utime(c, (1_000_000, 1_000_000))

    assert scratch_pool.touch_own("w1") == 1
    assert os.stat(claims / f"{mine.name}.claim").st_mtime > 1_000_000
    assert os.stat(claims / f"{theirs.name}.claim").st_mtime == 1_000_000


# --- pool-file tuning ------------------------------------------------------


def test_pool_file_overrides_env_reserve(pool, monkeypatch):
    """The reserve is a worst-case guess (p90 x merge) while real usage is an
    order of magnitude smaller -- measured live, 11 concurrent downloads held
    28.6GB of claims for 1.7GB of bytes. Retuning it must not require
    re-creating a container on every CT, so the pool carries the number."""
    monkeypatch.setenv("PAPRIKA_SCRATCH_VIDEO_RESERVE_MB", "2600")
    assert scratch_pool.video_reserve_bytes() == 2600 * MB
    (pool / ".reserve_mb").write_text("800")
    assert scratch_pool.video_reserve_bytes() == 800 * MB
    (pool / ".min_free_mb").write_text("1024")
    assert scratch_pool.min_free_bytes() == 1024 * MB


def test_garbage_pool_file_falls_back_to_env(pool, monkeypatch):
    monkeypatch.setenv("PAPRIKA_SCRATCH_VIDEO_RESERVE_MB", "2600")
    for junk in ("", "  ", "abc", "-5", "0"):
        (pool / ".reserve_mb").write_text(junk)
        assert scratch_pool.video_reserve_bytes() == 2600 * MB


def test_smaller_reserve_admits_more(pool, monkeypatch):
    """The point of the knob: same pool, more concurrent downloads."""
    _free(monkeypatch, 8000 * MB)
    (pool / ".reserve_mb").write_text("800")
    got = [
        scratch_pool.acquire(f"paprika-vid-j{i}-", "wA",
                             scratch_pool.video_reserve_bytes())
        for i in range(8)
    ]
    assert all(g is not None for g in got)   # 8 x 800MB fits; 8 x 2600MB never would


# --- 5. cgroup axis (loft 2026-08-06) --------------------------------------
#
# The pool's free space says the NODE can take the bytes. It says nothing about
# whether the calling CT's memory cgroup can -- and tmpfs pages ARE charged to
# the CT that writes them, which is what crushed loft's page cache to 0.6MB and
# turned a write problem into a 270MB/s read storm. Both axes must pass.


def _cgroup(monkeypatch, *, current_mb, limit_mb=None, ok=True):
    """Declare a cgroup state: what we've spent, and the cap someone told us."""
    monkeypatch.delenv("PAPRIKA_WORKER_MEM_LIMIT_MB", raising=False)
    monkeypatch.delenv("PAPRIKA_SCRATCH_CGROUP_RESERVE_MB", raising=False)
    monkeypatch.setattr(
        scratch_pool.cgroup_mem, "sample",
        lambda: scratch_pool.cgroup_mem.MemSample(ok=ok, current=current_mb * MB),
    )
    if limit_mb is not None:
        (scratch_pool.configured_dir() / ".cgroup_limit_mb").write_text(str(limit_mb))


def test_cgroup_axis_is_inert_without_a_declared_limit(pool, monkeypatch):
    """No limit declared => the axis has no opinion and the old behaviour
    stands. This is what lets the source ship before any node is configured."""
    _free(monkeypatch, 10_000 * MB)
    _cgroup(monkeypatch, current_mb=7_500)          # would be way over any cap
    assert scratch_pool.cgroup_headroom_bytes(pool, "w1") is None
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 2600 * MB) is not None


def test_cgroup_axis_is_inert_when_the_controller_is_unreadable(pool, monkeypatch):
    """cgroup v1 host, or no controller: ok=False must mean 'no signal', never
    'no headroom'. Guessing here would push every download onto the disk."""
    _free(monkeypatch, 10_000 * MB)
    _cgroup(monkeypatch, current_mb=0, limit_mb=8192, ok=False)
    assert scratch_pool.cgroup_headroom_bytes(pool, "w1") is None
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 2600 * MB) is not None


def test_cgroup_axis_rejects_when_the_ct_budget_is_full(pool, monkeypatch):
    """The loft case: pool has plenty of room, the CT does not."""
    _free(monkeypatch, 60_000 * MB)                 # node is fine
    _cgroup(monkeypatch, current_mb=6_000, limit_mb=8192)
    # 8192 - 6000 - 1536 reserve = 656MB of headroom
    assert scratch_pool.cgroup_headroom_bytes(pool, "w1") == 656 * MB
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 2600 * MB) is None


def test_cgroup_axis_admits_when_the_ct_has_room(pool, monkeypatch):
    """Same node, same download, cap raised 8G -> 12G: now it fits. The knob
    has to be continuous like this, not a cliff."""
    _free(monkeypatch, 60_000 * MB)
    _cgroup(monkeypatch, current_mb=3_000, limit_mb=12288)
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 2600 * MB) is not None


def test_cgroup_axis_keeps_a_page_cache_floor(pool, monkeypatch):
    """Without the reserve the CT would be admitted right up to its cap, which
    is precisely the state that leaves 0.6MB of active_file."""
    _free(monkeypatch, 60_000 * MB)
    _cgroup(monkeypatch, current_mb=4_000, limit_mb=8192)
    # 4192MB nominally free, but 1536 is reserved -> 2656 usable
    assert scratch_pool.cgroup_headroom_bytes(pool, "w1") == 2656 * MB
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 3000 * MB) is None
    assert scratch_pool.acquire("paprika-vid-j2-", "w1", 2000 * MB) is not None


def test_cgroup_axis_ignores_a_neighbours_claim(pool, monkeypatch):
    """A neighbour's promised bytes land in the NEIGHBOUR's cgroup. Counting
    them against us would be the pool axis over again, and would starve a CT
    that has room purely because its node-mates are busy."""
    _free(monkeypatch, 60_000 * MB)
    _cgroup(monkeypatch, current_mb=1_000, limit_mb=12288)
    neighbour = scratch_pool.acquire("paprika-vid-j9-", "w2", 5000 * MB)
    assert neighbour is not None
    assert scratch_pool.cgroup_headroom_bytes(pool, "w1") == (12288 - 1000 - 1536) * MB
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 2600 * MB) is not None


def test_cgroup_axis_counts_our_own_outstanding_claim(pool, monkeypatch):
    """Two downloads starting back-to-back on ONE worker: the first has written
    nothing yet, so memory.current cannot see it. Without subtracting our own
    claim the same headroom is handed out twice and both land in the cgroup."""
    _free(monkeypatch, 60_000 * MB)
    _cgroup(monkeypatch, current_mb=1_000, limit_mb=8192)
    # headroom 5656MB: one 3000MB download fits, two do not
    assert scratch_pool.acquire("paprika-vid-j1-", "w1", 3000 * MB) is not None
    assert scratch_pool.acquire("paprika-vid-j2-", "w1", 3000 * MB) is None


def test_cgroup_limit_pool_file_beats_env(pool, monkeypatch):
    """Same precedence as the other two knobs: env is baked into the container
    at create time, the pool file is one echo away."""
    _free(monkeypatch, 60_000 * MB)
    _cgroup(monkeypatch, current_mb=1_000, limit_mb=12288)
    monkeypatch.setenv("PAPRIKA_WORKER_MEM_LIMIT_MB", "8192")
    assert scratch_pool.cgroup_limit_bytes() == 12288 * MB
    (scratch_pool.configured_dir() / ".cgroup_limit_mb").unlink()
    assert scratch_pool.cgroup_limit_bytes() == 8192 * MB


# --- ENOSPC classifier (the local-disk retry trigger) ----------------------


@pytest.mark.parametrize("msg,expected", [
    ("ERROR: unable to write data: [Errno 28] No space left on device", True),
    ("OSError: [Errno 28] ENOSPC", True),
    ("ERROR: HTTP Error 403: Forbidden", False),
    ("", False),
    (None, False),
])
def test_enospc_classifier(msg, expected):
    from server.worker.agent._mix_jobexec import _looks_like_enospc

    assert _looks_like_enospc(msg) is expected
