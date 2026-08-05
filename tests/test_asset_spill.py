"""Regression: pre-emptive RAM-disk spill-over (2026-07-20 incident).

The image RAM-disk MinIO filled and the whole HOST stopped answering --
kernel alive and TCP connect instant, but /minio/health/live took 8-15s and
sshd could not finish a banner exchange. Crucially **writes never returned an
error**; they hung. So the fallback has to key off a free-space threshold
sampled ahead of time, never off a failed write.

These tests pin the four properties the fix rests on:
  1. hysteresis      -- switch out at high_pct, return only under low_pct
  2. fail-safe       -- an unmeasurable tier spills (that IS the incident)
  3. per-job pinning -- one job's assets never split across two stores
  4. inert by default-- asset_spill_enabled off => byte-for-byte old routing

See server/hub/_spill.py for the mechanism and the reasoning behind each.
"""

import asyncio

import pytest

from server.hub import _spill, objstore


# --- helpers ---------------------------------------------------------------


class _FakeSettings:
    """Stands in for the live SettingsRegistry."""

    def __init__(self, **vals):
        self._v = dict(vals)

    def get(self, key, default=None):
        return self._v.get(key, default)

    def set(self, key, value):
        self._v[key] = value


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Isolate module-global caches between tests."""
    from server.hub import _state

    _spill._LOCAL_STATE.clear()
    _spill._PIN_CACHE.clear()
    monkeypatch.setattr(
        _state.state,
        "settings",
        _FakeSettings(
            asset_spill_enabled=True,
            asset_spill_high_pct=80.0,
            asset_spill_low_pct=60.0,
        ),
        raising=False,
    )
    yield
    _spill._LOCAL_STATE.clear()
    _spill._PIN_CACHE.clear()


def _cap(total, used, healthy=True, note=""):
    from server.hub._storage_metrics import MinioCapacity

    return MinioCapacity(
        total_bytes=total,
        used_bytes=used,
        free_bytes=max(0, total - used),
        bucket_usage_bytes=used,
        bucket_object_count=0,
        healthy=healthy,
        note=note,
    )


def _sample(monkeypatch, pct, prev_spill, *, healthy=True, note="", bucket_ok=True):
    """Run one _sample_tier pass at ``pct`` used, given the previous state."""
    total = 107_400_000_000  # .47 image RAM disk
    used = int(total * pct / 100.0)

    async def _fake_fetch(endpoint, access, secret, bucket="paprika", timeout_s=10.0):
        return _cap(total, used, healthy=healthy, note=note)

    async def _fake_ensure(tier):
        return bucket_ok

    monkeypatch.setattr(_spill, "_tier_probe_cfg", lambda t: ("http://ram:9000", "a", "b", "paprika"))
    monkeypatch.setattr("server.hub._storage_metrics.fetch_minio_capacity", _fake_fetch)
    monkeypatch.setattr(objstore, "ensure_ram_bucket", _fake_ensure)
    prev = {"spill": prev_spill}
    return asyncio.run(_spill._sample_tier(_spill.TIER_NONVIDEO, prev))


# --- 1. hysteresis ---------------------------------------------------------


def test_below_high_stays_on_ram(monkeypatch):
    assert _sample(monkeypatch, 79.9, prev_spill=False)["spill"] is False


def test_reaching_high_spills(monkeypatch):
    # 80% of 107.4GB leaves ~21GB -- at the measured peak +400MB/min that is
    # ~54 minutes of headroom, so the switch has time to take effect.
    assert _sample(monkeypatch, 80.0, prev_spill=False)["spill"] is True
    assert _sample(monkeypatch, 92.0, prev_spill=False)["spill"] is True


def test_hysteresis_does_not_return_at_the_high_mark(monkeypatch):
    """Once spilling, dropping just under 80% must NOT flip back -- that is
    the flap that scatters one job's assets over two stores."""
    for pct in (79.9, 70.0, 60.0):
        assert _sample(monkeypatch, pct, prev_spill=True)["spill"] is True, pct


def test_returns_to_ram_only_below_low(monkeypatch):
    assert _sample(monkeypatch, 59.9, prev_spill=True)["spill"] is False


def test_low_pct_forced_below_high(monkeypatch):
    """A mis-set pair (low >= high) must not collapse the hysteresis band."""
    from server.hub import _state

    _state.state.settings.set("asset_spill_low_pct", 95.0)
    assert _spill.low_pct() < _spill.high_pct()


# --- 2. fail-safe: unmeasurable => spill -----------------------------------


def test_unmeasurable_tier_spills(monkeypatch):
    """The incident signature: the host answers too slowly to measure. We must
    treat that as over-threshold, not as 'unknown, keep writing to RAM'."""
    got = _sample(monkeypatch, 10.0, prev_spill=False, healthy=False, note="timeout")
    assert got["spill"] is True
    assert "unmeasurable" in got["note"]


def test_stale_shared_state_spills():
    """If the sampler stopped updating, the decision must decay to 'spill'."""
    import time

    _spill._LOCAL_STATE["nonvideo"] = {
        "spill": False,
        "pct": 10.0,
        "ts": time.time() - 10_000,  # far older than stale_after_s
        "note": "",
    }
    assert _spill._is_spilling_now("nonvideo") is True


def test_missing_state_spills():
    assert _spill._is_spilling_now("nonvideo") is True


def test_fresh_state_is_respected():
    import time

    _spill._LOCAL_STATE["nonvideo"] = {
        "spill": False,
        "pct": 16.0,
        "ts": time.time(),
        "note": "",
    }
    assert _spill._is_spilling_now("nonvideo") is False


# --- 3. per-job pinning ----------------------------------------------------


def test_pin_survives_a_mid_job_threshold_flip():
    """A job that started on RAM keeps writing to RAM even after the tier
    flips -- otherwise its assets split and the consumer pays a lookup miss
    per asset."""
    import time

    _spill._LOCAL_STATE["nonvideo"] = {"spill": False, "pct": 10.0, "ts": time.time(), "note": ""}
    _spill._cache_pin("job-a", {"nonvideo": False, "primary": False})

    # tier flips to spill mid-job
    _spill._LOCAL_STATE["nonvideo"] = {"spill": True, "pct": 85.0, "ts": time.time(), "note": ""}

    assert _spill.is_spilling("nonvideo", "job-a") is False   # pinned
    assert _spill.is_spilling("nonvideo", "job-new") is True  # unpinned
    assert _spill.is_spilling("nonvideo", None) is True


def test_pin_encode_decode_roundtrip():
    pin = {"nonvideo": True, "primary": False}
    assert _spill._decode_pin(_spill._encode_pin(pin)) == pin
    assert _spill._decode_pin(_spill._encode_pin(pin).encode()) == pin


def test_pin_cache_is_bounded():
    for i in range(_spill._PIN_CACHE_MAX + 50):
        _spill._cache_pin("j%d" % i, {"nonvideo": False, "primary": False})
    assert len(_spill._PIN_CACHE) <= _spill._PIN_CACHE_MAX


# --- 2b. RAM bucket persistence (a reboot wipes the bucket, not just data) --


def test_missing_ram_bucket_spills(monkeypatch):
    """A rebooted RAM MinIO loses its bucket; while it can't be recreated the
    tier is un-writable and must spill (writes would else fail, not hang)."""
    got = _sample(monkeypatch, 10.0, prev_spill=False, bucket_ok=False)
    assert got["spill"] is True
    assert "bucket" in got["note"]


def test_recreated_ram_bucket_returns_to_ram(monkeypatch):
    """Once the bucket is (re)created and usage is low, writes go back to RAM."""
    got = _sample(monkeypatch, 10.0, prev_spill=True, bucket_ok=True)
    assert got["spill"] is False


class _FakeS3:
    """Minimal boto3-ish client recording bucket ops."""

    def __init__(self, exists, create_raises=None):
        self._exists = exists
        self._create_raises = create_raises
        self.created = False

    def head_bucket(self, Bucket):
        if not self._exists:
            raise RuntimeError("NoSuchBucket")

    def create_bucket(self, Bucket):
        if self._create_raises:
            raise self._create_raises
        self.created = True
        self._exists = True


def test_ensure_bucket_noop_when_present():
    c = _FakeS3(exists=True)
    assert objstore.ensure_bucket(c, "paprika") is True
    assert c.created is False


def test_ensure_bucket_creates_when_missing():
    c = _FakeS3(exists=False)
    assert objstore.ensure_bucket(c, "paprika") is True
    assert c.created is True


def test_ensure_bucket_tolerates_race():
    c = _FakeS3(exists=False, create_raises=RuntimeError("BucketAlreadyOwnedByYou"))
    assert objstore.ensure_bucket(c, "paprika") is True


def test_ensure_bucket_false_when_unrecoverable():
    c = _FakeS3(exists=False, create_raises=RuntimeError("Connection timeout"))
    assert objstore.ensure_bucket(c, "paprika") is False


def test_ensure_bucket_false_without_client():
    assert objstore.ensure_bucket(None, "paprika") is False


# --- 3b. snapshot / loop tick ----------------------------------------------


def test_snapshot_reports_effective_routing_when_disabled():
    """With the feature off nothing can divert, so /health must not claim a
    spill just because the fail-safe has no sample yet."""
    from server.hub import _state

    _state.state.settings.set("asset_spill_enabled", False)
    snap = _spill.snapshot()
    assert snap["enabled"] is False
    assert snap["tiers"]["nonvideo"]["spill"] is False
    assert snap["tiers"]["primary"]["spill"] is False


def test_snapshot_reports_spill_when_enabled_and_unmeasured():
    snap = _spill.snapshot()          # enabled via the fixture, no sample yet
    assert snap["enabled"] is True
    assert snap["tiers"]["nonvideo"]["spill"] is True


def test_tick_when_disabled_clears_state_and_returns():
    """The disabled path must not spin or raise -- it runs on every hub."""
    from server.hub import _state

    _spill._LOCAL_STATE["nonvideo"] = {"spill": True, "pct": None, "ts": 0, "note": ""}
    _state.state.settings.set("asset_spill_enabled", False)
    asyncio.run(_spill._tick())
    assert _spill._LOCAL_STATE == {}


# --- 4. key -> job_id (what makes the pin reachable from the sync path) ----


def test_job_id_from_key(monkeypatch):
    monkeypatch.setattr(objstore, "_prefix", lambda: "jobs")
    # The key scheme the consumer depends on -- must not change.
    assert objstore.job_id_from_key("jobs/abc123/assets/a.jpg") == "abc123"
    assert objstore.job_id_from_key("jobs/abc123/page.html") == "abc123"
    assert objstore.job_id_from_key("profiles/default.tar.gz") == "profiles"
    assert objstore.job_id_from_key("") is None


def test_job_id_from_key_empty_prefix(monkeypatch):
    monkeypatch.setattr(objstore, "_prefix", lambda: "")
    assert objstore.job_id_from_key("abc123/assets/a.jpg") == "abc123"


# --- 5. routing ------------------------------------------------------------


def _wire(monkeypatch, *, nv_spill, primary_spill, feature_on=True):
    """Point every tier at a distinguishable sentinel client."""
    monkeypatch.setattr(objstore, "enabled", lambda: True)
    monkeypatch.setattr(objstore, "_prefix", lambda: "jobs")
    monkeypatch.setattr(objstore, "_spill_feature_on", lambda: feature_on)
    monkeypatch.setattr(objstore, "_nonvideo_endpoint", lambda: "http://ram-img:9000")
    monkeypatch.setattr(objstore, "_nonvideo_bucket", lambda: "paprika")
    monkeypatch.setattr(objstore, "_bucket", lambda: "paprika")
    monkeypatch.setattr(objstore, "_spill_endpoint", lambda: "http://big-vid:9100")
    monkeypatch.setattr(objstore, "_nv_spill_endpoint", lambda: "http://pantry:9000")
    monkeypatch.setattr(objstore, "_get_client", lambda: "RAM_VID")
    monkeypatch.setattr(objstore, "_get_nv_client", lambda: "RAM_IMG")
    monkeypatch.setattr(objstore, "_get_spill_client", lambda: "SPILL_VID")
    monkeypatch.setattr(objstore, "_get_nv_spill_client", lambda: "SPILL_IMG")
    monkeypatch.setattr(
        objstore,
        "_spilling",
        lambda tier, key: nv_spill if tier == "nonvideo" else primary_spill,
    )


def test_routes_to_ram_when_under_threshold(monkeypatch):
    _wire(monkeypatch, nv_spill=False, primary_spill=False)
    assert objstore._write_cb("jobs/j1/assets/a.jpg")[0] == "RAM_IMG"
    assert objstore._write_cb("jobs/j1/assets/v.mp4")[0] == "RAM_VID"


def test_image_diverts_to_pantry_when_over_threshold(monkeypatch):
    _wire(monkeypatch, nv_spill=True, primary_spill=False)
    assert objstore._write_cb("jobs/j1/assets/a.jpg")[0] == "SPILL_IMG"
    # video is a separate tier and must be unaffected
    assert objstore._write_cb("jobs/j1/assets/v.mp4")[0] == "RAM_VID"


def test_video_diverts_independently(monkeypatch):
    _wire(monkeypatch, nv_spill=False, primary_spill=True)
    assert objstore._write_cb("jobs/j1/assets/a.jpg")[0] == "RAM_IMG"
    assert objstore._write_cb("jobs/j1/assets/v.mp4")[0] == "SPILL_VID"


def test_feature_off_is_byte_for_byte_old_routing(monkeypatch):
    """Default state: even with both tiers 'over threshold', nothing diverts."""
    _wire(monkeypatch, nv_spill=True, primary_spill=True, feature_on=False)
    assert objstore._write_cb("jobs/j1/assets/a.jpg")[0] == "RAM_IMG"
    assert objstore._write_cb("jobs/j1/assets/v.mp4")[0] == "RAM_VID"


def test_reads_cover_every_tier(monkeypatch):
    """Spilled objects must stay readable with no migration."""
    _wire(monkeypatch, nv_spill=False, primary_spill=False)
    img = [c for c, _ in objstore._read_cbs("jobs/j1/assets/a.jpg")]
    assert "RAM_IMG" in img and "SPILL_IMG" in img and "RAM_VID" in img and "SPILL_VID" in img


def test_delete_unions_every_tier(monkeypatch):
    """DELETE /jobs/{id} must purge spilled objects too, or they orphan."""
    _wire(monkeypatch, nv_spill=False, primary_spill=False)
    cbs = [c for c, _ in objstore._all_cbs()]
    assert set(cbs) == {"RAM_VID", "SPILL_VID", "RAM_IMG", "SPILL_IMG"}


def test_dedup_when_spill_points_at_an_existing_tier(monkeypatch):
    """Misconfiguring a spill target to an already-used endpoint must not make
    LIST/DELETE visit the same bucket twice."""
    _wire(monkeypatch, nv_spill=False, primary_spill=False)
    monkeypatch.setattr(objstore, "_get_spill_client", lambda: "RAM_VID")
    monkeypatch.setattr(objstore, "_spill_bucket", lambda: "paprika")
    cbs = objstore._all_cbs()
    assert len(cbs) == len(set((c, b) for c, b in cbs))
