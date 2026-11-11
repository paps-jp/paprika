"""Ledger aggregation + stream consumer (learner/ledger.py, learner/consumer.py).

The ledger is the only place counters live, so the properties that matter
are: keys cannot be single-URL (the 64,856-recipe failure mode), counts are
additive deltas, and nothing is acked before it is durable.
"""
import asyncio
import json

from learner import ledger as L
from learner import verify as V
from learner.consumer import Consumer, decode


def _ev(job_id="j1", url="https://ex.com/video/123/abc-def-ghi/", **kw):
    base = {
        "job_id": job_id, "ts": 1794420000.0, "url": url, "host": "ex.com",
        "status": "completed", "download_video": True, "capture_assets": True,
        "assets": {"n_video": 1}, "ytdlp": {}, "video_seen": {}, "occlusion": {},
        "facts": {"media": [{"name": "v.mp4", "bytes": 9e7, "probed": True,
                             "playable": True, "duration_s": 900.0}]},
    }
    base.update(kw)
    return base


def test_template_key_folds_ids_so_keys_cannot_be_single_url():
    a = L.template_of("https://ex.com/thread-3085848-1-1.html")
    b = L.template_of("https://ex.com/thread-9999999-1-1.html")
    assert a == b
    assert L.template_of("https://ex.com/video/28624/x-y-z/") == \
        L.template_of("https://ex.com/video/50654/q-r-s/")


def test_aggregator_counts_by_key_and_drains_deltas():
    agg = L.DailyAggregator()
    ok = _ev("a")
    agg.add(ok, V.verify(ok))
    ok2 = _ev("b", url="https://ex.com/video/999/zzz-yyy-xxx/")
    agg.add(ok2, V.verify(ok2))
    bad = _ev("c", assets={"n_video": 0}, facts={}, video_seen={"urls": 1})
    agg.add(bad, V.verify(bad))
    daily, detail = agg.drain()
    assert len(daily) == 1                      # same host+template+intent+day+arm
    key, counts = daily[0]
    assert key.intent == "video" and key.version == V.VERSION and key.arm == "control"
    assert (counts["n"], counts["n_success"], counts["n_fail"]) == (3, 2, 1)
    assert L.vsr(counts) == 2 / 3
    assert agg.drain() == ([], [])              # drained -> deltas, not totals


def test_detail_is_a_bounded_sample_not_a_copy_of_the_jobs_table():
    agg = L.DailyAggregator()
    for i in range(2000):
        ok = _ev(f"ok{i}")
        agg.add(ok, V.verify(ok))
        bad = _ev(f"bad{i}", assets={"n_video": 0}, facts={}, video_seen={"urls": 1})
        agg.add(bad, V.verify(bad))
    daily, detail = agg.drain()
    assert daily[0][1]["n"] == 4000             # counting stays exact
    n_ok = sum(1 for d in detail if d["outcome"] == V.SUCCESS)
    n_bad = sum(1 for d in detail if d["outcome"] == V.FAIL)
    assert 3 <= n_ok <= 60                      # ~1%
    assert 120 <= n_bad <= 300                  # ~10%


def test_arms_are_separate_rows_of_the_same_key():
    agg = L.DailyAggregator()
    r = _ev("r1", recipe_applied=True, arm="recipe", recipe_pattern="/video/*")
    h = _ev("h1", recipe_applied=False, arm="holdout", recipe_pattern="/video/*",
            assets={"n_video": 0}, facts={}, video_seen={"urls": 1})
    c = _ev("c1")
    for ev in (r, h, c):
        agg.add(ev, V.verify(ev))
    daily, detail = agg.drain()
    by_arm = {k.arm: cnt for k, cnt in daily}
    assert set(by_arm) == {"recipe", "holdout", "control"}
    assert len({(k.host, k.template, k.intent, k.day) for k, _ in daily}) == 1
    assert by_arm["recipe"]["n_success"] == 1 and by_arm["holdout"]["n_fail"] == 1
    # every scored holdout job is kept in detail, with the withheld pattern
    hd = [d for d in detail if d["arm"] == "holdout"]
    assert len(hd) == 1 and hd[0]["recipe_pattern"] == "/video/*"


def test_arm_of():
    assert L.arm_of({"recipe_applied": True, "arm": "recipe"}) == "recipe"
    assert L.arm_of({"recipe_applied": True}) == "recipe"          # pre-arm hub
    assert L.arm_of({"recipe_applied": False, "arm": "holdout"}) == "holdout"
    assert L.arm_of({"recipe_applied": True, "arm": "holdout"}) == "recipe"   # facts win
    assert L.arm_of({}) == "control"
    assert L.arm_of({"escalated_from": "x", "arm": "holdout"}) == "solver"


def test_infra_and_na_do_not_enter_vsr():
    counts = {"n": 10, "n_success": 2, "n_fail": 2, "n_infra": 4, "n_na": 2}
    assert L.vsr(counts) == 0.5
    assert L.vsr({"n": 3, "n_infra": 3}) is None


def test_sampling_is_deterministic():
    ids = [f"job{i}" for i in range(5000)]
    picked = [i for i in ids if L.in_sample(i)]
    assert picked == [i for i in ids if L.in_sample(i)]
    assert 10 <= len(picked) <= 120             # ~1%


def test_upsert_sql_is_additive():
    sql = L.daily_upsert_sql()
    assert "n_success = n_success + VALUES(n_success)" in sql
    assert sql.count("%s") == len(L.daily_params(
        L.Key("h", "/", "video", "2026-11-12", 1, "holdout"), {}, __import__("datetime").datetime(2026, 1, 1)))
    assert "arm" in sql.split("VALUES")[0]


class _FakeRedis:
    def __init__(self, entries):
        self._new = list(entries)
        self.acked = []

    async def xgroup_create(self, *a, **k):
        raise Exception("BUSYGROUP Consumer Group name already exists")

    async def xreadgroup(self, group, name, streams, count=None, block=None):
        start = next(iter(streams.values()))
        if start != ">" or not self._new:
            await asyncio.sleep(0)
            return []
        batch, self._new = self._new[:count], self._new[count:]
        return [("s", batch)]

    async def xautoclaim(self, *a, **k):
        return ["0-0", [], []]

    async def xack(self, key, group, *ids):
        self.acked.extend(ids)

    async def xdel(self, key, *ids):
        self.deleted = getattr(self, "deleted", []) + list(ids)


class _FakeLedger:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    async def write(self, daily, detail):
        if self.fail:
            raise RuntimeError("db down")
        self.writes.append((daily, detail))


def _entry(i, ev):
    return (f"{i}-0", {"d": json.dumps(ev)})


def test_consumer_acks_only_after_durable_write():
    entries = [_entry(1, _ev("a")), _entry(2, _ev("b")), ("3-0", {"d": "not json"})]
    r, led = _FakeRedis(entries), _FakeLedger()
    c = Consumer(r, led, name="t")
    for eid, f in entries:
        c.handle(eid, f)
    asyncio.run(c.flush())
    assert r.acked == ["1-0", "2-0", "3-0"]
    assert r.deleted == r.acked                # durable -> dropped from the buffer
    assert c.stats["read"] == 2 and c.stats["bad"] == 1
    assert led.writes[0][0][0][1]["n"] == 2


def test_consumer_does_not_ack_when_the_ledger_write_fails():
    r, led = _FakeRedis([]), _FakeLedger(fail=True)
    c = Consumer(r, led, name="t")
    c.handle("1-0", {"d": json.dumps(_ev("a"))})
    asyncio.run(c.flush())
    assert r.acked == [] and c.stats["flush_errors"] == 1
    assert not getattr(r, "deleted", [])       # never delete what is not durable
    # the deltas were dropped with the failed flush: a later reclaim re-reads
    # the entry, so keeping them would double count.
    led.fail = False
    asyncio.run(c.flush())
    assert led.writes == []


def test_decode_tolerates_bytes_and_garbage():
    assert decode({b"d": b'{"job_id":"x"}'}) == {"job_id": "x"}
    assert decode({"d": "[1,2]"}) is None
    assert decode({"other": "1"}) is None
