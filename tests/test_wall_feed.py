"""Regression: /wall -- 取得結果の滝 (visitor-facing results waterfall).

The wall taps the ``final.jpg`` upload of every completed fetch. At
production throughput that tap fires ~10x/second on a fleet-wide basis, so
the properties that actually matter are the *bounding* ones:

  * admission is fleet-wide rate-capped, and a REJECTED page costs no disk
    read and no ffmpeg -- the gate is checked before either
  * the ring is trimmed, so a wall left running for hours cannot grow
  * ``feed(after=seq)`` is a real cursor (no duplicate cards on the wall)
  * a missing/blank Redis degrades to an empty feed, never an exception --
    ``offer_file`` sits on the upload path and must not be able to fail it
"""

import asyncio
import shutil
import subprocess

import pytest

from server.hub import wallfeed


# ---------------------------------------------------------------------------
# A Redis stub with just the commands wallfeed uses.


class _FakePipeline:
    """Queues calls, replays them on execute() -- and counts as ONE trip."""

    def __init__(self, r):
        self._r = r
        self._ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self
        return queue

    async def execute(self):
        self._r.trips += 1
        return [await getattr(self._r, n)(*a, **k) for (n, a, k) in self._ops]


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.counters: dict[str, int] = {}
        self.trips = 0

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    async def incr(self, k):
        self.counters[k] = self.counters.get(k, 0) + 1
        return self.counters[k]

    async def expire(self, k, ttl):
        return True

    async def set(self, k, v, ex=None):
        self.kv[k] = v
        return True

    async def get(self, k):
        return self.kv.get(k)

    async def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)
        return len(self.lists[k])

    async def ltrim(self, k, start, end):
        self.lists[k] = self.lists.get(k, [])[start:end + 1]
        return True

    async def lrange(self, k, start, end):
        return self.lists.get(k, [])[start:end + 1]

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def zcard(self, k):
        return 0


@pytest.fixture
def r(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(wallfeed, "_redis", lambda: fake)
    return fake


@pytest.fixture
def shot(tmp_path):
    """A synthetic 'full page screenshot': tall, portrait, real JPEG.

    Generated rather than committed -- production final.jpg files are crawled
    third-party pages and have no business in the repo.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    p = tmp_path / "final.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1200x2400:duration=1:rate=1",
         "-frames:v", "1", str(p)],
        check=True,
    )
    assert p.stat().st_size > 1024
    return p


# ---------------------------------------------------------------------------


def test_offer_then_feed_and_thumb(r, shot):
    asyncio.run(wallfeed.offer_file(
        "job1", shot,
        url="https://www.example.com/a/b?c=d",
        worker_id="w51100", lane_idx=1,
    ))
    out = asyncio.run(wallfeed.feed())
    assert out["enabled"] is True
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["job_id"] == "job1"
    # `www.` is stripped so the wall's label is the site, not the prefix.
    assert item["host"] == "example.com"
    assert item["worker_id"] == "w51100" and item["lane_idx"] == 1
    assert item["seq"] == out["seq"] >= 1

    jpeg = asyncio.run(wallfeed.thumb(item["seq"]))
    assert jpeg and jpeg[:2] == b"\xff\xd8"      # JPEG SOI
    # Thumbnailing is the whole point of the tap: a card must be a fraction
    # of the full-page shot, not a re-serve of it.
    assert len(jpeg) < shot.stat().st_size / 4


def test_bundle_is_one_round_trip(r, shot):
    """The route serves cards + stats from a SINGLE pipelined trip.

    A round-trip to the shared Redis measured ~300ms under crawl load, and the
    browser holds one poll in flight at a time -- three sequential trips put
    the feed at ~1s p50 and showed up on the wall as dead air followed by a
    clump of cards. Pin the trip count, not just the payload.
    """
    asyncio.run(wallfeed.offer_file("job1", shot, url="https://a.example/"))
    r.trips = 0
    out = asyncio.run(wallfeed.bundle(after=0, limit=10))
    assert r.trips == 1, f"bundle made {r.trips} round-trips, expected 1"
    assert len(out["items"]) == 1
    assert out["items"][0]["job_id"] == "job1"
    assert out["stats"]["source"] == "redis"
    assert out["enabled"] is True
    # And the cursor still works through the bundled path.
    assert asyncio.run(wallfeed.bundle(after=out["seq"]))["items"] == []


def test_cursor_does_not_repeat_cards(r, shot):
    asyncio.run(wallfeed.offer_file("job1", shot, url="https://a.example/"))
    first = asyncio.run(wallfeed.feed())
    again = asyncio.run(wallfeed.feed(after=first["seq"]))
    assert again["items"] == []
    assert again["seq"] == first["seq"]

    asyncio.run(wallfeed.offer_file("job2", shot, url="https://b.example/"))
    nxt = asyncio.run(wallfeed.feed(after=first["seq"]))
    assert [i["job_id"] for i in nxt["items"]] == ["job2"]


def test_rate_gate_is_fleet_wide_and_precedes_any_work(r, shot, monkeypatch):
    """Only PAPRIKA_WALL_MAX_PER_SEC pages per second are admitted, and the
    ones that aren't never reach the disk or ffmpeg."""
    monkeypatch.setenv("PAPRIKA_WALL_MAX_PER_SEC", "2")
    calls = []
    monkeypatch.setattr(
        wallfeed, "_thumbnail_sync",
        lambda b: (calls.append(len(b)), b"\xff\xd8fake")[1],
    )
    for i in range(6):
        asyncio.run(wallfeed.offer_file(f"job{i}", shot, url="https://x.example/"))
    out = asyncio.run(wallfeed.feed())
    assert len(out["items"]) == 2, "rate gate did not cap admissions"
    assert len(calls) == 2, "rejected pages still paid for a thumbnail"


def test_ring_is_trimmed(r, shot, monkeypatch):
    monkeypatch.setenv("PAPRIKA_WALL_RING", "8")
    monkeypatch.setenv("PAPRIKA_WALL_MAX_PER_SEC", "1000")
    monkeypatch.setattr(wallfeed, "_thumbnail_sync", lambda b: b"\xff\xd8fake")
    for i in range(25):
        asyncio.run(wallfeed.offer_file(f"job{i}", shot, url="https://x.example/"))
    assert len(r.lists[wallfeed.K_FEED]) == 8
    out = asyncio.run(wallfeed.feed(limit=100))
    assert len(out["items"]) == 8
    # Oldest-first: the browser appends in arrival order.
    seqs = [i["seq"] for i in out["items"]]
    assert seqs == sorted(seqs)
    assert [i["job_id"] for i in out["items"]][-1] == "job24"


def test_no_redis_degrades_quietly(monkeypatch, shot):
    monkeypatch.setattr(wallfeed, "_redis", lambda: None)
    # Must not raise: this runs on the final.jpg upload path.
    asyncio.run(wallfeed.offer_file("job1", shot, url="https://x.example/"))
    assert asyncio.run(wallfeed.feed())["items"] == []
    assert asyncio.run(wallfeed.thumb(1)) is None
    assert asyncio.run(wallfeed.stats())["source"] == "unavailable"


def test_disabled_switch_stops_everything(r, shot, monkeypatch):
    monkeypatch.setenv("PAPRIKA_WALL_ENABLED", "0")
    asyncio.run(wallfeed.offer_file("job1", shot, url="https://x.example/"))
    assert r.lists.get(wallfeed.K_FEED) is None
    out = asyncio.run(wallfeed.feed())
    assert out["items"] == [] and out["enabled"] is False


def test_elapsed_from_naive_started_at_is_read_as_utc():
    """Job rows store naive UTC (prod-clock-utc-vs-jst). Reading one as local
    time would print '32400s' fetches on a JST hub."""
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    started = (now - dt.timedelta(seconds=12)).replace(tzinfo=None)
    ms = wallfeed._elapsed_ms(started, now.timestamp())
    assert 11_000 <= ms <= 13_000
    assert wallfeed._elapsed_ms(None, now.timestamp()) is None
