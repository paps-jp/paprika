"""Playbook life cycle (learner/playbook.py) and its counters in the ledger.

Every transition is automatic, so the rules are the product: a trial is
promoted only on measured evidence against a comparison group from the
same period, and an active playbook retires by itself when it stops
earning its place.
"""
import datetime as dt

from learner import ledger as L
from learner import playbook as P
from learner.verify import Verdict

NOW = dt.datetime(2026, 11, 20, 12, 0, 0)
ACTIONS = [{"kind": "click", "selector": "button.yes"}]


def _pb(state, **kw):
    base = dict(id=1, host="a.com", template="/v/{id}/", intent="video", state=state,
                actions=ACTIONS, state_changed_at=NOW - dt.timedelta(days=1))
    base.update(kw)
    return P.Playbook(**base)


def c(success, fail, **extra):
    d = {"n_success": success, "n_fail": fail}
    d.update(extra)
    return d


def test_trial_is_promoted_only_when_it_clearly_beats_control():
    d = P.decide(_pb(P.TRIAL), {"trial": c(18, 4), "pb_control": c(10, 90)}, now=NOW)
    assert (d.state, d.reason) == (P.ACTIVE, "trial_beats_control")
    assert abs(d.promoted_diff - (18 / 22 - 0.10)) < 1e-9
    # better on paper, but the interval still overlaps control + margin
    assert P.decide(_pb(P.TRIAL), {"trial": c(8, 14), "pb_control": c(25, 75)}, now=NOW) is None


def test_trial_waits_for_enough_scored_jobs_in_both_groups():
    assert P.decide(_pb(P.TRIAL), {"trial": c(19, 0), "pb_control": c(0, 80)}, now=NOW) is None
    assert P.decide(_pb(P.TRIAL), {"trial": c(30, 0), "pb_control": c(0, 10)}, now=NOW) is None
    # not-applicable / infra jobs are not evidence either way
    arms = {"trial": c(5, 0, n_na=500, n_infra=40), "pb_control": c(0, 80)}
    assert P.decide(_pb(P.TRIAL), arms, now=NOW) is None


def test_trial_is_retired_when_worse_or_when_it_never_pulls_ahead():
    d = P.decide(_pb(P.TRIAL), {"trial": c(1, 29), "pb_control": c(60, 40)}, now=NOW)
    assert (d.state, d.reason) == (P.RETIRED, "trial_worse_than_control")
    d = P.decide(_pb(P.TRIAL), {"trial": c(15, 40), "pb_control": c(50, 150)}, now=NOW)
    assert (d.state, d.reason) == (P.RETIRED, "trial_no_gain")
    stale = _pb(P.TRIAL, state_changed_at=NOW - dt.timedelta(days=20))
    assert P.decide(stale, {}, now=NOW).reason == "trial_no_traffic"


def test_active_retires_when_it_no_longer_beats_its_holdout():
    pb = _pb(P.ACTIVE, promoted_diff=0.6)
    healthy = {"active": c(70, 30), "pb_holdout": c(1, 14)}
    assert P.decide(pb, healthy, now=NOW, recent=healthy) is None
    # the site changed: the playbook now does no better than plain fetch
    decayed = {"active": c(8, 72), "pb_holdout": c(1, 14)}
    d = P.decide(pb, healthy, now=NOW, recent=decayed)
    assert (d.state, d.reason) == (P.RETIRED, "active_decayed")
    # too little recent data to call
    thin = {"active": c(2, 20), "pb_holdout": c(0, 3)}
    assert P.decide(pb, healthy, now=NOW, recent=thin) is None


def test_active_retires_when_unused():
    pb = _pb(P.ACTIVE, state_changed_at=NOW - dt.timedelta(days=90))
    assert P.decide(pb, {}, now=NOW, recent={}, last_applied_days=40).reason == "active_unused"
    assert P.decide(pb, {}, now=NOW, recent={}, last_applied_days=2) is None
    assert P.decide(pb, {}, now=NOW, recent={}).reason == "active_unused"   # never applied at all


def test_candidates_and_retired_are_left_alone():
    assert P.decide(_pb(P.CANDIDATE), {"trial": c(99, 0), "pb_control": c(0, 99)}, now=NOW) is None
    assert P.decide(_pb(P.RETIRED), {"trial": c(99, 0), "pb_control": c(0, 99)}, now=NOW) is None


def test_hopeless_keys_get_a_bigger_trial():
    assert P.trial_pct_for(0.0) == 50 and P.trial_pct_for(0.02) == 50
    assert P.trial_pct_for(0.3) == 20 and P.trial_pct_for(None) == 20


def test_one_trial_per_key():
    pbs = [
        _pb(P.CANDIDATE, id=5), _pb(P.CANDIDATE, id=3),                      # same key: oldest goes
        _pb(P.CANDIDATE, id=8, template="/w/{id}/"),                         # free key
        _pb(P.TRIAL, id=1, host="b.com"), _pb(P.CANDIDATE, id=9, host="b.com"),  # key already on trial
    ]
    assert [p.id for p in P.admit_candidates(pbs)] == [3, 8]


def test_published_doc_holds_only_applicable_playbooks():
    doc = P.published_doc(
        [_pb(P.TRIAL, id=2, apply_pct=50), _pb(P.ACTIVE, id=1, holdout_pct=5),
         _pb(P.CANDIDATE, id=3), _pb(P.RETIRED, id=4), _pb(P.TRIAL, id=6, actions=[])],
        now_ts=1000.0, rev=9,
    )
    assert (doc["v"], doc["rev"], doc["ts"]) == (P.PUBLISH_VERSION, 9, 1000.0)
    by = {i["id"]: i for i in doc["items"]}
    assert set(by) == {1, 2}
    assert (by[2]["apply_pct"], by[2]["holdout_pct"]) == (50, 0)
    assert (by[1]["apply_pct"], by[1]["holdout_pct"]) == (100, 5)


# ---- ledger: arms and per-playbook counters -------------------------------

def _ev(**kw):
    base = dict(job_id="j1", ts=1794430000.0, host="a.com", url="https://a.com/v/abc123/",
                download_video=True, status="completed", recipe_applied=True)
    base.update(kw)
    return base


def test_arm_of_recognises_playbook_and_solver_arms():
    assert L.arm_of(_ev(arm="trial", pb=4)) == "trial"
    assert L.arm_of(_ev(arm="pb_control", pb=4, recipe_applied=False)) == "pb_control"
    assert L.arm_of(_ev(arm="active", pb=4)) == "active"
    assert L.arm_of(_ev(arm="pb_holdout", pb=4, recipe_applied=False)) == "pb_holdout"
    assert L.arm_of(_ev(arm="solver")) == "solver"
    assert L.arm_of(_ev(arm="trial")) == "recipe"            # no playbook id: not ours
    assert L.arm_of(_ev(arm="recipe")) == "recipe"
    assert L.arm_of(_ev(arm="holdout", recipe_applied=False)) == "holdout"
    assert L.arm_of(_ev(arm="", recipe_applied=False)) == "control"


def test_playbook_counters_accumulate_per_arm_and_day():
    agg = L.DailyAggregator()
    ok = Verdict("success", "video_ok", "video")
    bad = Verdict("fail", "video_not_captured", "video")
    agg.add(_ev(job_id="a", arm="trial", pb=4), ok)
    agg.add(_ev(job_id="b", arm="trial", pb=4), bad)
    agg.add(_ev(job_id="c", arm="pb_control", pb=4, recipe_applied=False), bad)
    agg.add(_ev(job_id="d", arm="active", pb=7, pb_vs=4), ok)      # also control for trial #4
    agg.add(_ev(job_id="e", arm="recipe"), ok)                     # legacy recipe: no playbook row
    got = dict(agg.drain_playbooks())
    day = L.day_of(1794430000.0)
    assert got[(4, "trial", day)] == {"n": 2, "n_success": 1, "n_fail": 1}
    assert got[(4, "pb_control", day)] == {"n": 2, "n_fail": 1, "n_success": 1}
    assert got[(7, "active", day)] == {"n": 1, "n_success": 1}
    assert len(got) == 3
    assert agg.drain_playbooks() == []
    # every playbook-arm job keeps its detail row (decisions are auditable)
    _daily, detail = agg.drain()
    assert {r["job_id"] for r in detail} >= {"a", "b", "c", "d"}


def test_playbook_upsert_is_additive():
    sql = L.pb_upsert_sql()
    assert "learn_playbook_stats" in sql and "n_success = n_success + VALUES(n_success)" in sql
    now = dt.datetime(2026, 11, 20)
    params = L.pb_params((4, "trial", "2026-11-20"), {"n": 2, "n_success": 1}, now)
    assert params[:3] == (4, "trial", "2026-11-20") and params[3:5] == (2, 1) and params[-1] == now


def test_malformed_playbook_ids_are_ignored():
    assert L.pb_credits(_ev(arm="trial", pb="x"), "trial") == []
    assert L.pb_credits(_ev(arm="recipe"), "recipe") == []
