"""Hub side of learned playbooks (server/hub/_playbooks.py).

The hub's whole part is: read the one document the learner publishes, put
each matching job into an experiment arm from its id alone, and stamp the
playbook's actions for the worker's existing recipe player. It must ship
dark, never raise into dispatch, and never steer traffic from a stale set.
"""
import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

from learner import playbook as P
from server.hub import _outcome_stream as OS
from server.hub import _playbooks as PB

URL = "https://www.example.com/video/12345/some-title/"
ACTIONS = [{"kind": "wait", "seconds": 1.0}, {"kind": "click", "selector": "button.yes"}]


def _item(**kw):
    base = dict(id=7, ver=1, host="www.example.com", template="/video/{int}/some-title/",
                intent="video", state="trial", apply_pct=20, holdout_pct=0, actions=ACTIONS)
    base.update(kw)
    return base


def _doc(*items, ts=None):
    return {"v": 1, "rev": 3, "ts": time.time() if ts is None else ts, "items": list(items)}


def _opts(**kw):
    base = dict(fetch_recipe=None, learn=None, download_video=True, fetch_strategy="recipe", mode="fetch")
    base.update(kw)
    return NS(**base)


@pytest.fixture
def live(monkeypatch):
    """Playbooks enabled, one trial published, no background task."""
    from server.hub._page_role import templatize

    tpl = templatize(URL)
    monkeypatch.setattr(PB, "enabled", lambda: True)
    monkeypatch.setattr(PB, "ensure_refresher", lambda: None)
    monkeypatch.setattr(PB, "_index", PB.build_index(_doc(_item(template=tpl))))
    return tpl


def _job_in(arm_wanted, slot):
    for i in range(5000):
        got = PB.assign(f"job{i}", slot)
        if got and got[0] == arm_wanted:
            return f"job{i}"
    raise AssertionError(f"no job lands in {arm_wanted}")


def test_dark_by_default_changes_nothing(monkeypatch):
    monkeypatch.setattr(PB.state, "settings", None, raising=False)
    monkeypatch.setattr(PB, "_index", PB.build_index(_doc(_item())))
    assert PB.enabled() is False
    o = _opts()
    assert PB.apply_playbook_arm(o, "j1", URL) is None
    assert o.fetch_recipe is None and o.learn is None


def test_enabled_but_nothing_published_changes_nothing(monkeypatch):
    monkeypatch.setattr(PB, "enabled", lambda: True)
    monkeypatch.setattr(PB, "ensure_refresher", lambda: None)
    monkeypatch.setattr(PB, "_index", {})
    assert PB.apply_playbook_arm(_opts(), "j1", URL) is None


def test_trial_job_gets_the_actions_and_control_gets_nothing(live):
    slot = PB.lookup(URL, "video")
    o = _opts()
    assert PB.apply_playbook_arm(o, _job_in("trial", slot), URL) == "trial"
    assert o.fetch_recipe["actions"] == ACTIONS and o.fetch_recipe["created_by"] == "learner"
    assert o.learn == {"arm": "trial", "pb": 7, "ver": 1}

    c = _opts()
    assert PB.apply_playbook_arm(c, _job_in("pb_control", slot), URL) == "pb_control"
    assert c.fetch_recipe is None                      # plain fetch: the comparison group
    assert c.learn == {"arm": "pb_control", "pb": 7, "ver": 1}


def test_trial_share_matches_apply_pct_and_is_stable_per_job(live):
    slot = PB.lookup(URL, "video")
    arms = [PB.assign(f"job{i}", slot)[0] for i in range(5000)]
    assert 0.17 < arms.count("trial") / len(arms) < 0.23
    assert set(arms) == {"trial", "pb_control"}
    for i in range(300):                               # a redriven job keeps its arm
        assert PB.assign(f"job{i}", slot)[0] == arms[i]


def test_active_playbook_keeps_a_holdout():
    slot = {"active": dict(_item(state="active", apply_pct=100, holdout_pct=5))}
    arms = [PB.assign(f"job{i}", slot)[0] for i in range(6000)]
    assert 0.03 < arms.count("pb_holdout") / len(arms) < 0.07
    assert set(arms) == {"active", "pb_holdout"}


def test_trial_next_to_an_active_is_compared_with_it():
    slot = {"trial": _item(id=9), "active": _item(id=7, state="active", holdout_pct=5)}
    seen = {}
    for i in range(4000):
        arm, pb, vs = PB.assign(f"job{i}", slot)
        seen.setdefault(arm, (pb["id"], vs["id"] if vs else None))
    assert seen["trial"] == (9, None)
    assert seen["active"] == (7, 9)        # an active job doubles as the trial's control
    assert seen["pb_holdout"] == (7, None)
    assert "pb_control" not in seen


def test_jobs_that_must_not_be_touched(live):
    slot = PB.lookup(URL, "video")
    jid = _job_in("trial", slot)
    own = _opts(fetch_recipe={"pattern": "*", "actions": [{"kind": "wait"}]})
    assert PB.apply_playbook_arm(own, jid, URL) is None and own.fetch_recipe["actions"] == [{"kind": "wait"}]
    assert PB.apply_playbook_arm(_opts(fetch_strategy="normal"), jid, URL) is None
    assert PB.apply_playbook_arm(_opts(learn={"arm": "solver"}), jid, URL) is None
    # a page job is a different intent: the video playbook does not apply
    assert PB.apply_playbook_arm(_opts(download_video=False), jid, URL) is None
    # another template on the same host
    assert PB.apply_playbook_arm(_opts(), jid, "https://www.example.com/tag/abc/") is None


def test_never_raises_into_dispatch(live, monkeypatch):
    monkeypatch.setattr(PB, "lookup", lambda *a, **k: 1 / 0)
    o = _opts()
    assert PB.apply_playbook_arm(o, "j1", URL) is None
    assert o.fetch_recipe is None and o.learn is None


def test_stale_or_malformed_documents_apply_nothing():
    assert PB.build_index(_doc(_item(), ts=time.time() - PB.MAX_AGE_S - 5)) == {}
    assert PB.build_index("nope") == {}
    assert PB.build_index({"items": [_item()]}) == {}          # no timestamp = stale
    idx = PB.build_index(_doc(
        _item(id=1, state="candidate"), _item(id=2, state="retired"),
        _item(id=3, actions=[]), _item(id=4, actions=[{"kind": "wait"}] * (PB.MAX_ACTIONS + 1)),
        {"id": "x"}, "junk",
    ))
    assert idx == {}


def test_duplicate_trial_on_a_key_does_not_flip_flop():
    idx = PB.build_index(_doc(_item(id=12), _item(id=5), _item(id=9)))
    (slot,) = idx.values()
    assert slot["trial"]["id"] == 5


def test_learner_document_round_trips_to_the_hub_index():
    pbs = [
        P.Playbook(id=1, host="a.com", template="/v/{id}/", intent="video", state=P.TRIAL,
                   actions=ACTIONS, apply_pct=50),
        P.Playbook(id=2, host="b.com", template="/w/{id}/", intent="video", state=P.ACTIVE,
                   actions=ACTIONS, holdout_pct=5),
        P.Playbook(id=3, host="c.com", template="/", intent="video", state=P.CANDIDATE, actions=ACTIONS),
        P.Playbook(id=4, host="d.com", template="/", intent="video", state=P.RETIRED, actions=ACTIONS),
    ]
    doc = json.loads(json.dumps(P.published_doc(pbs, now_ts=time.time(), rev=1)))
    idx = PB.build_index(doc)
    assert set(idx) == {("a.com", "/v/{id}/", "video"), ("b.com", "/w/{id}/", "video")}
    assert idx[("a.com", "/v/{id}/", "video")]["trial"]["apply_pct"] == 50
    assert idx[("b.com", "/w/{id}/", "video")]["active"]["holdout_pct"] == 5
    assert P.REDIS_KEY == PB.REDIS_KEY and P.MAX_ACTIONS <= PB.MAX_ACTIONS


class _R:
    def __init__(self, value):
        self.value = value

    async def get(self, key):
        assert key == PB.REDIS_KEY
        return self.value


def test_refresh_reads_the_published_key(monkeypatch):
    monkeypatch.setattr(PB, "_index", {})
    monkeypatch.setattr(PB, "_redis", lambda: _R(json.dumps(_doc(_item())).encode()))
    assert asyncio.run(PB.refresh_once()) is True
    assert len(PB._index) == 1 and PB.stats()["rev"] == 3
    # the learner removed the key -> nothing is applied any more
    monkeypatch.setattr(PB, "_redis", lambda: _R(None))
    assert asyncio.run(PB.refresh_once()) is False
    assert PB._index == {}


def test_outcome_event_names_the_playbook():
    info = NS(job_id="j", url=URL, status=NS(value="completed"), worker_id="w1",
              started_at=None, completed_at=None, error=None,
              options=NS(mode="fetch", download_video=True, capture_assets=True,
                         fetch_strategy="recipe", escalated_from=None,
                         fetch_recipe={"pattern": "*", "actions": ACTIONS},
                         learn={"arm": "active", "pb": 7, "ver": 2, "vs": 9}))
    ev = OS.build_event(info, None)
    assert (ev["arm"], ev["pb"], ev["pb_ver"], ev["pb_vs"]) == ("active", 7, 2, 9)
    info.options.learn = {"arm": "recipe", "pattern": "/x/*"}
    ev = OS.build_event(info, None)
    assert ev["pb"] is None and ev["pb_vs"] is None
