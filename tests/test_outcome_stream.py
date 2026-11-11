"""Hub -> learner coupling (server/hub/_outcome_stream.py).

The hub's whole contribution to the learning system is this one event, so:
it must ship dark (off by default), never raise into the WS handler, and
carry the facts the Verifier needs without the bulk it does not.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from server.hub import _outcome_stream as OS


def _info(**kw):
    t0 = datetime(2026, 11, 12, 3, 0, 0)
    opts = NS(mode="fetch", download_video=True, capture_assets=True,
              fetch_strategy="recipe", escalated_from=None,
              fetch_recipe={"pattern": "/video/*", "description": "auto: age_gate barrier"})
    base = dict(job_id="abc123", url="https://Ex.com/video/1/x-y/", status=NS(value="completed"),
                options=opts, worker_id="w5185", started_at=t0,
                completed_at=t0 + timedelta(seconds=42), error=None)
    base.update(kw)
    return NS(**base)


def _result(**kw):
    assets = [
        NS(name="a.jpg", size=100_000, mime="image/jpeg"),
        NS(name="logo.svg", size=900, mime="image/svg+xml"),
        NS(name="big.mp4", size=90_000_000, mime="video/mp4"),
        NS(name="small.webm", size=1_000_000, mime=None),
    ]
    base = dict(assets=assets, assets_failed=1,
                ytdlp_results=[NS(ok=False, message="HTTP Error 403: Forbidden " + "x" * 300),
                               NS(ok=True, message="ok")],
                video_urls_seen=["u1", "u2"], video_detection={"videos": [1], "iframes": [1, 2]},
                occlusion={"coverage": 0.2, "hasPassword": False, "dump": {"big": "x" * 5000}},
                review_reason=None, partial=False,
                facts={"media": [{"name": "big.mp4", "probed": True, "playable": True}],
                       "recipe": {"ran": 1, "ok": 1}})
    base.update(kw)
    return NS(**base)


def test_build_event_carries_verifier_inputs_compactly():
    ev = OS.build_event(_info(), _result())
    assert ev["v"] == OS.SCHEMA_VERSION and ev["ev"] == "job_terminal"
    assert ev["host"] == "ex.com" and ev["status"] == "completed" and ev["dur_s"] == 42.0
    assert ev["download_video"] is True and ev["recipe_applied"] is True
    assert ev["recipe_pattern"] == "/video/*"
    a = ev["assets"]
    assert (a["n"], a["n_video"], a["n_image"], a["n_decor"]) == (4, 2, 1, 1)
    assert a["videos"][0] == {"name": "big.mp4", "size": 90_000_000}
    assert ev["ytdlp"] == {"n": 2, "fail": 1, "msgs": [ev["ytdlp"]["msgs"][0]]}
    assert len(ev["ytdlp"]["msgs"][0]) <= 120
    assert ev["video_seen"] == {"urls": 2, "videos": 1, "iframes": 2}
    assert "dump" not in ev["occlusion"] and ev["occlusion"]["coverage"] == 0.2
    assert ev["facts"]["media"][0]["playable"] is True
    assert len(json.dumps(ev)) < 2000


def test_build_event_reports_the_experiment_arm():
    info = _info()
    info.options.fetch_recipe = None
    info.options.learn = {"arm": "holdout", "pattern": "/video/*", "pct": 20}
    ev = OS.build_event(info, _result())
    assert ev["arm"] == "holdout" and ev["recipe_applied"] is False
    assert ev["recipe_pattern"] == "/video/*"          # the WITHHELD pattern
    assert OS.build_event(_info(), _result())["arm"] == ""   # pre-arm options


def test_build_event_for_a_failure_without_result():
    ev = OS.build_event(_info(status=NS(value="failed")), None, error="worker w1 disconnected " * 40)
    assert ev["status"] == "failed" and len(ev["error"]) <= 300
    assert ev["assets"]["n"] == 0 and ev["facts"] == {}


def test_event_round_trips_through_the_verifier():
    from learner import verify as V
    ev = json.loads(json.dumps(OS.build_event(_info(), _result(
        facts={"media": [{"name": "big.mp4", "bytes": 9e7, "probed": True,
                          "playable": True, "duration_s": 1500.0}]}))))
    assert V.verify(ev).outcome == V.SUCCESS


class _R:
    def __init__(self, boom=False):
        self.calls, self.boom = [], boom

    async def xadd(self, key, fields, maxlen=None, approximate=None):
        if self.boom:
            raise RuntimeError("redis down")
        self.calls.append((key, fields, maxlen, approximate))


def _run_emit(monkeypatch, *, on, redis):
    monkeypatch.setattr(OS, "enabled", lambda: on)
    monkeypatch.setattr(OS, "_redis", lambda: redis)

    async def go():
        OS.emit_job_terminal(_info(), _result())
        await asyncio.sleep(0.01)
    asyncio.run(go())


def test_emit_is_dark_by_default(monkeypatch):
    monkeypatch.delenv("PAPRIKA_OUTCOME_STREAM", raising=False)
    monkeypatch.setattr(OS.state, "settings", None, raising=False)
    assert OS.enabled() is False
    r = _R()
    _run_emit(monkeypatch, on=False, redis=r)
    assert r.calls == []


def test_emit_writes_a_capped_stream_entry(monkeypatch):
    r = _R()
    _run_emit(monkeypatch, on=True, redis=r)
    (key, fields, maxlen, approx), = r.calls
    assert key == OS.STREAM_KEY and maxlen == OS.MAXLEN and approx is True
    assert json.loads(fields["d"])["job_id"] == "abc123"


def test_emit_never_raises(monkeypatch):
    _run_emit(monkeypatch, on=True, redis=_R(boom=True))      # redis error
    _run_emit(monkeypatch, on=True, redis=None)               # no redis
    monkeypatch.setattr(OS, "enabled", lambda: True)
    OS.emit_job_terminal(None)                                # no info
    OS.emit_job_terminal(_info(), _result())                  # no running loop


def test_settings_toggle_enables(monkeypatch):
    monkeypatch.delenv("PAPRIKA_OUTCOME_STREAM", raising=False)
    monkeypatch.setattr(OS.state, "settings", NS(get=lambda k, d=None: True), raising=False)
    assert OS.enabled() is True
