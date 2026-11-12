"""The learner's status document (learner/status.py) and its hub reader.

The status is for a person who looks in once in a while. What is pinned
down: the numbers it shows are the ones the transitions are decided from,
the budget counts what the scheduler counts, and neither a broken status
nor a dead learner can pass for a healthy one.
"""
import asyncio
import datetime as dt
import json
import random
import time

from learner import autopilot as A
from learner import playbook as P
from learner import status as ST
from server.hub.routes import learn as HL

NOW = dt.datetime(2026, 11, 20, 12, 0, 0)
TODAY = NOW.date()


def _pb(id, state, **kw):
    base = dict(id=id, host="x.example", template="/v/{int}/", intent="video", state=state,
                actions=[{"kind": "click", "selector": "button.yes"}], apply_pct=20, holdout_pct=5,
                state_changed_at=NOW - dt.timedelta(hours=id), created_at=NOW - dt.timedelta(days=1),
                provenance={"klass": "overlay_wall"})
    base.update(kw)
    return P.Playbook(**base)


def _c(success=0, fail=0, na=0):
    return {"n": success + fail + na, "n_success": success, "n_fail": fail, "n_na": na}


# ---- playbooks ---------------------------------------------------------------

def test_trial_row_compares_trial_with_its_control_group():
    row = ST.playbook_row(_pb(1, P.TRIAL), {"trial": _c(30, 10, na=5), "pb_control": _c(4, 36)})
    assert (row["applied_arm"], row["compare_arm"]) == ("trial", "pb_control")
    # Jobs the Verifier could not score (n/a) are in n but not in the rate.
    assert row["applied"]["n"] == 45 and row["applied"]["scored"] == 40
    assert row["applied"]["rate"] == 0.75 and row["compare"]["rate"] == 0.1
    assert row["applied"]["lo"] < 0.75 < row["applied"]["hi"]
    assert row["klass"] == "overlay_wall" and row["n_actions"] == 1


def test_active_row_compares_with_the_holdout():
    row = ST.playbook_row(_pb(2, P.ACTIVE, promoted_diff=0.4),
                          {"active": _c(80, 20), "pb_holdout": _c(1, 9), "trial": _c(30, 10)})
    assert (row["applied_arm"], row["compare_arm"]) == ("active", "pb_holdout")
    assert row["applied"]["rate"] == 0.8 and row["promoted_diff"] == 0.4


def test_row_without_any_job_has_no_rate():
    row = ST.playbook_row(_pb(3, P.TRIAL), None)
    assert row["applied"] == {"n": 0, "scored": 0, "success": 0, "rate": None, "lo": None, "hi": None}


def test_retired_row_shows_the_pair_it_was_judged_on():
    was_trial = ST.playbook_row(_pb(4, P.RETIRED, reason="trial_no_gain"),
                                {"trial": _c(2, 50), "pb_control": _c(3, 40)})
    assert was_trial["applied_arm"] == "trial"
    was_active = ST.playbook_row(_pb(5, P.RETIRED, reason="active_decayed"),
                                 {"active": _c(5, 60), "pb_holdout": _c(2, 9), "trial": _c(30, 10)})
    assert was_active["applied_arm"] == "active"


def test_playbooks_section_counts_every_state_and_caps_retired():
    pbs = [_pb(1, P.TRIAL), _pb(2, P.ACTIVE), _pb(3, P.CANDIDATE)]
    pbs += [_pb(100 + i, P.RETIRED) for i in range(ST.MAX_RETIRED + 5)]
    sec = ST.playbooks_section(pbs, {})
    assert sec["counts"] == {"candidate": 1, "trial": 1, "active": 1, "retired": ST.MAX_RETIRED + 5}
    states = [it["state"] for it in sec["items"]]
    assert states[:3] == ["active", "trial", "candidate"]
    assert states.count("retired") == ST.MAX_RETIRED
    # The retired ones kept are the most recently retired.
    assert sec["items"][3]["id"] == 100


# ---- solver ------------------------------------------------------------------

def test_solver_budget_does_not_count_infra_runs():
    rows = [(TODAY, "gave_up", 18, 35000), (TODAY, "infra", 6, 17000), (TODAY, "solved", 2, 9000),
            (TODAY - dt.timedelta(days=1), "replay_fail", 8, 36000)]
    sec = ST.solver_section(rows, [], today=TODAY, daily_runs=40, daily_tokens=2_000_000, enabled=True)
    assert sec["today"]["runs"] == 20                       # infra consumed no decision
    assert sec["today"]["tokens"] == 61000                  # but its tokens were spent
    assert sec["today"]["by_result"] == {"gave_up": 18, "infra": 6, "solved": 2}
    assert (sec["today"]["budget_runs"], sec["today"]["budget_tokens"]) == (40, 2_000_000)
    assert [d["day"] for d in sec["days"]] == ["2026-11-20", "2026-11-19"]


def test_solver_day_without_runs_still_shows_the_budget():
    sec = ST.solver_section([], [], today=TODAY, daily_runs=40, daily_tokens=1000, enabled=False)
    assert sec["enabled"] is False
    assert sec["today"] == {"day": "2026-11-20", "runs": 0, "tokens": 0, "by_result": {},
                            "budget_runs": 40, "budget_tokens": 1000}


def test_recent_runs_are_json_ready():
    run = (NOW, "x.example", "/v/{int}/", "overlay_wall", "https://x.example/v/1/", "gave_up",
           "not a video page", 3, 1900, 41.5, None)
    sec = ST.solver_section([], [run], today=TODAY, daily_runs=40, daily_tokens=1, enabled=True)
    assert sec["recent"][0]["ts"] == "2026-11-20T12:00:00Z"
    assert sec["recent"][0]["reason"] == "not a video page" and sec["recent"][0]["pb_id"] is None
    json.dumps(sec)


# ---- keys / outcomes ---------------------------------------------------------

def test_keys_section_totals():
    sec = ST.keys_section([("overlay_wall", 40, 46, 0, 38), ("video_not_captured", 3, 3, 1, None)])
    assert (sec["keys"], sec["dormant"], sec["runs"], sec["solved"]) == (43, 38, 49, 1)
    assert sec["by_class"][0]["klass"] == "overlay_wall"


def test_outcomes_rate_is_over_scored_jobs_only():
    # (day, intent, n, success, partial, fail, blocked, dead, infra, na, unknown)
    rows = [(TODAY, "video", 1000, 100, 100, 250, 50, 0, 0, 480, 20),
            (TODAY, "page", 400, 360, 0, 40, 0, 0, 0, 0, 0)]
    sec = ST.outcomes_section(rows, [("video", "video_not_captured", 240)])
    video = next(d for d in sec["days"] if d["intent"] == "video")
    assert video["scored"] == 500 and video["rate"] == 0.2 and video["na"] == 480
    assert next(d for d in sec["days"] if d["intent"] == "page")["rate"] == 0.9
    assert sec["fail_reasons_24h"] == [{"intent": "video", "reason": "video_not_captured", "n": 240}]


# ---- the autopilot writes it ---------------------------------------------------

class _Redis:
    def __init__(self, fail=False):
        self.kv, self.ex, self.fail = {}, {}, fail

    async def set(self, k, v, ex=None):
        if self.fail and k == ST.REDIS_KEY:
            raise RuntimeError("redis down")
        self.kv[k], self.ex[k] = v, ex


class _Store:
    def __init__(self):
        self.playbooks = [_pb(1, P.TRIAL), _pb(2, P.RETIRED, reason="trial_no_gain")]
        self.counts = {1: {"trial": _c(30, 10), "pb_control": _c(4, 36)}}

    async def load_playbooks(self, include_retired=False):
        return [p for p in self.playbooks if include_retired or p.state != P.RETIRED]

    async def playbook_counts(self, ids, since_days=None):
        return {i: self.counts[i] for i in ids if i in self.counts}

    async def last_applied_days(self, ids):
        return {}

    async def key_base_counts(self, host, template, intent, days=7):
        return {"n_success": 0, "n_fail": 50}

    async def solver_days(self, days=7):
        return [(dt.datetime.now(dt.timezone.utc).date(), "gave_up", 3, 900)]

    async def recent_runs(self, limit=30):
        return []

    async def key_summary(self):
        return [("overlay_wall", 3, 3, 0, 3)]

    async def outcome_days(self, days=7):
        return []

    async def fail_reasons(self, hours=24, limit=12):
        return []


class _LLM:
    model = "test-model"


def _pilot(redis):
    ap = A.Autopilot(store=_Store(), hub=None, llm=_LLM(), redis=redis,
                     cfg=A.AutopilotConfig(solver_enabled=True, lifecycle_interval_s=0.0),
                     rng=random.Random(1))
    ap.consumer_stats = lambda: {"read": 10, "flushed": 10}
    return ap


def test_publish_status_writes_one_expiring_document():
    r = _Redis()
    doc = asyncio.run(_pilot(r).publish_status())
    stored = json.loads(r.kv[ST.REDIS_KEY])
    assert r.ex[ST.REDIS_KEY] == ST.TTL_S                    # a dead learner's status goes away
    assert stored["v"] == ST.STATUS_VERSION and abs(stored["ts"] - time.time()) < 5
    assert stored["playbooks"]["counts"] == {"candidate": 0, "trial": 1, "active": 0, "retired": 1}
    assert stored["playbooks"]["items"][0]["applied"]["rate"] == 0.75
    assert stored["solver"]["today"]["runs"] == 3 and stored["solver"]["enabled"] is True
    assert stored["consumer"] == {"read": 10, "flushed": 10}
    assert stored["manifest"]["model"] == "test-model"
    assert stored["config"]["daily_runs"] == 40
    assert doc["keys"]["keys"] == 3


def test_a_failing_status_write_does_not_stop_the_lifecycle_loop():
    r = _Redis(fail=True)
    ap = _pilot(r)
    ap.store.counts = {}          # no evidence yet: the tick has no transition to make

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ap._lifecycle_loop(stop))
        for _ in range(200):
            await asyncio.sleep(0)
            if ap.stats["lifecycle_ticks"] >= 2:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    assert ap.stats["lifecycle_ticks"] >= 2
    assert P.REDIS_KEY in r.kv and ST.REDIS_KEY not in r.kv   # playbooks still published


# ---- the hub reads it ----------------------------------------------------------

HUB = {"playbooks_enabled": True, "playbooks": {"n": 0}}


def test_hub_reports_a_learner_that_never_published():
    out = HL.shape(None, now=1000.0, hub=HUB)
    assert out == {"available": False, "stale": False, "age_s": None, "hub": HUB, "status": None}


def test_hub_passes_a_fresh_document_through():
    raw = json.dumps({"v": 1, "ts": 940.0, "playbooks": {"counts": {"trial": 1}}}).encode()
    out = HL.shape(raw, now=1000.0, hub=HUB)
    assert out["available"] and not out["stale"] and out["age_s"] == 60.0
    assert out["status"]["playbooks"]["counts"] == {"trial": 1}


def test_hub_flags_a_document_the_learner_stopped_refreshing():
    raw = json.dumps({"v": 1, "ts": 1000.0 - HL.STALE_AFTER_S - 1})
    assert HL.shape(raw, now=1000.0, hub=HUB)["stale"] is True


def test_hub_survives_a_garbage_document():
    for raw in ("not json", "[1, 2]", '{"ts": "x"}', bytes([255, 254])):
        out = HL.shape(raw, now=1000.0, hub=HUB)
        assert out["available"] is False and out["status"] is None


def test_hub_and_learner_agree_on_the_key():
    assert HL.STATUS_KEY == ST.REDIS_KEY
