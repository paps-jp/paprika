"""Recipe holdout (server/hub/_learn_arm.py) + the arm comparison
(learner/report.py).

Until 2026-11 every matching job replayed its recipe, so 64,856 recipes
existed with no way to tell which ones helped. The holdout must be:
deterministic per job (redrives must not flip arms), off by default, and
fail-safe to "give the job its recipe".
"""
from types import SimpleNamespace as NS

from learner import report as R
from server.hub import _learn_arm as A


class _Recipe:
    pattern = "/video/*"

    def to_json(self):
        return {"pattern": self.pattern, "actions": [{"kind": "click", "selector": "a"}]}


def _opts():
    return NS(fetch_recipe=None, learn=None)


def _set_pct(monkeypatch, pct):
    monkeypatch.setattr(A.state, "settings", NS(get=lambda k, d=None: pct), raising=False)


def test_off_by_default_every_matching_job_gets_its_recipe(monkeypatch):
    monkeypatch.setattr(A.state, "settings", None, raising=False)
    assert A.holdout_pct() == 0
    for i in range(300):
        o = _opts()
        assert A.apply_recipe_arm(o, f"job{i}", _Recipe()) == "recipe"
        assert o.fetch_recipe["pattern"] == "/video/*" and o.learn["arm"] == "recipe"


def test_no_recipe_matched_means_no_arm():
    o = _opts()
    assert A.apply_recipe_arm(o, "j", None) is None
    assert o.fetch_recipe is None and o.learn is None


def test_holdout_withholds_the_recipe_for_about_pct(monkeypatch):
    _set_pct(monkeypatch, 20)
    arms = []
    for i in range(5000):
        o = _opts()
        arm = A.apply_recipe_arm(o, f"job{i}", _Recipe())
        arms.append(arm)
        if arm == "holdout":
            assert o.fetch_recipe is None
            assert o.learn == {"arm": "holdout", "pattern": "/video/*", "pct": 20}
        else:
            assert o.fetch_recipe is not None and o.learn["arm"] == "recipe"
    share = arms.count("holdout") / len(arms)
    assert 0.17 < share < 0.23


def test_assignment_is_deterministic_so_redrives_keep_their_arm(monkeypatch):
    _set_pct(monkeypatch, 20)
    for i in range(500):
        first = A.apply_recipe_arm(_opts(), f"job{i}", _Recipe())
        again = A.apply_recipe_arm(_opts(), f"job{i}", _Recipe())
        assert first == again
    # raising the percentage only ADDS jobs to the holdout, never swaps them
    at20 = {i for i in range(2000) if A.in_holdout(f"job{i}", 20)}
    at50 = {i for i in range(2000) if A.in_holdout(f"job{i}", 50)}
    assert at20 <= at50


def test_bad_setting_values_are_clamped(monkeypatch):
    _set_pct(monkeypatch, 500)
    assert A.holdout_pct() == 100
    _set_pct(monkeypatch, -3)
    assert A.holdout_pct() == 0
    _set_pct(monkeypatch, "junk")
    assert A.holdout_pct() == 0


def test_fail_safe_gives_the_recipe(monkeypatch):
    _set_pct(monkeypatch, 100)

    class Frozen:                        # an options model without `learn`
        __slots__ = ("fetch_recipe",)

        def __init__(self):
            self.fetch_recipe = None

    o = Frozen()
    assert A.apply_recipe_arm(o, "j", _Recipe()) == "recipe"
    assert o.fetch_recipe is not None


def test_job_options_carries_learn_and_round_trips():
    from server.protocol import JobOptions
    o = JobOptions()
    assert o.learn is None
    A_opts = JobOptions(learn={"arm": "holdout", "pattern": "/x/*", "pct": 20})
    assert JobOptions.model_validate_json(A_opts.model_dump_json()).learn["arm"] == "holdout"


# ---- arm comparison -------------------------------------------------------

def test_wilson_interval_basics():
    assert R.wilson(0, 0) == (0.0, 1.0)
    lo, hi = R.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    lo0, hi0 = R.wilson(0, 20)
    assert lo0 == 0.0 and 0.15 < hi0 < 0.17


def test_compare_arms_verdicts():
    def c(s, f):
        return {"n_success": s, "n_fail": f}
    assert R.compare_arms(c(80, 20), c(10, 90))["verdict"] == "helps"
    assert R.compare_arms(c(10, 90), c(80, 20))["verdict"] == "hurts"
    assert R.compare_arms(c(50, 50), c(48, 52))["verdict"] == "no_diff"
    assert R.compare_arms(c(5, 0), c(0, 5))["verdict"] == "thin"       # too few to call
    out = R.compare_arms(c(80, 20), c(10, 90))
    assert abs(out["diff"] - 0.7) < 1e-9 and out["recipe_n"] == 100


def test_na_and_infra_do_not_count_toward_either_arm():
    recipe = {"n_success": 30, "n_fail": 10, "n_na": 500, "n_infra": 99}
    assert R.scored_n(recipe) == 40


def test_recipe_report_groups_templates_into_hosts():
    rows = [
        {"host": "a.com", "template": "/v/{id}/", "intent": "video", "arm": "recipe",
         "n": 60, "n_success": 50, "n_fail": 10},
        {"host": "a.com", "template": "/w/{id}/", "intent": "video", "arm": "recipe",
         "n": 40, "n_success": 30, "n_fail": 10},
        {"host": "a.com", "template": "/v/{id}/", "intent": "video", "arm": "holdout",
         "n": 30, "n_success": 3, "n_fail": 27},
        {"host": "b.com", "template": "/", "intent": "page", "arm": "control",
         "n": 999, "n_success": 999},
    ]
    rep = R.recipe_report(rows, min_n=20)
    assert len(rep) == 1                                   # b.com has no recipe arm
    r = rep[0]
    assert r["key"] == {"host": "a.com", "intent": "video"}
    assert (r["recipe_n"], r["holdout_n"], r["verdict"]) == (100, 30, "helps")
