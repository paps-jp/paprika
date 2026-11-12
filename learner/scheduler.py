"""Improvement scheduler: where is a Solver run worth the most? (design §8)

The old system reacted to each failed job ("a fetch failed -> hand it to
the AI"), which spent 48% of its runs on hosts that never once succeeded.
This one decides in batches, from the ledger, under a fixed budget:

    value(key) = failures_7d(key) x P_solvable(key)

``failures_7d`` is exact (the daily table). ``P_solvable`` is not known,
so it is a Beta distribution per key -- prior from how the Solver has done
on that CLASS of failure so far, updated by its results on the key -- and
each round draws one sample from it (Thompson sampling). Keys the Solver
keeps failing on go dormant for 1, 3, 7, then 30 days by themselves.

Pure functions only; the SQL that feeds them is in ``learner/store.py``.
"""
from __future__ import annotations

import datetime as _dt
import random
from dataclasses import dataclass, field

#: The failure class this version of the Solver is built for (design §15.4
#: G3): a video is wanted, a full-screen layer hides the page, and there is
#: no credential form -- an age confirmation, a consent or advertising
#: overlay. ``video_not_captured`` (the video is there but the download got
#: nothing; mostly players inside cross-origin iframes) is ~25x larger but
#: is not something a click on the top document fixes; it is opened up by
#: configuration once the Solver can do more (SchedulerConfig.target_reasons).
TARGET_REASONS = ("overlay_wall",)
KNOWN_REASONS = ("overlay_wall", "video_not_captured")
TARGET_INTENT = "video"

DORMANCY_DAYS = (1, 3, 7, 30)
HARD_BARRIER_DAYS = 30
MIN_URLS = 3


@dataclass(frozen=True)
class SchedulerConfig:
    target_reasons: tuple = TARGET_REASONS
    daily_runs: int = 40            # Solver runs per UTC day (infra results are free)
    daily_tokens: int = 2_000_000   # model tokens per UTC day
    min_fails_7d: int = 5           # do not bother below this
    rerun_after_h: float = 20.0     # at most one run per key per day
    prior_strength: float = 4.0     # how many pseudo-observations the class prior is worth
    default_prior: tuple[float, float] = (1.0, 3.0)


@dataclass
class KeyCandidate:
    host: str
    template: str
    intent: str
    klass: str                      # the dominant failure reason
    fails_7d: int = 0
    scored_7d: int = 0
    success_7d: int = 0
    urls: list = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.host, self.template, self.intent)

    @property
    def vsr(self) -> float | None:
        return (self.success_7d / self.scored_7d) if self.scored_7d else None


@dataclass
class KeyState:
    runs: int = 0
    solved: int = 0
    fail_streak: int = 0
    dormant_until: _dt.datetime | None = None
    last_run_at: _dt.datetime | None = None


def class_prior(class_stats: dict, klass: str, cfg: SchedulerConfig) -> tuple[float, float]:
    """Beta prior for a key of failure class ``klass``.

    ``class_stats``: {klass: (solved, runs)} over all keys so far. With few
    runs the default prior (pessimistic: 1 in 4) is used.
    """
    solved, runs = class_stats.get(klass, (0, 0))
    if runs < 10:
        return cfg.default_prior
    p = max(0.02, min(0.98, solved / runs))
    return (1.0 + p * cfg.prior_strength, 1.0 + (1.0 - p) * cfg.prior_strength)


def posterior(prior: tuple[float, float], st: KeyState | None) -> tuple[float, float]:
    if st is None:
        return prior
    return (prior[0] + st.solved, prior[1] + max(0, st.runs - st.solved))


def eligible(c: KeyCandidate, st: KeyState | None, busy: set, now: _dt.datetime,
             cfg: SchedulerConfig) -> tuple[bool, str]:
    if c.intent != TARGET_INTENT:
        return False, "intent"
    if c.key in busy:
        return False, "has_playbook"
    if c.fails_7d < cfg.min_fails_7d:
        return False, "low_traffic"
    if len(set(c.urls)) < MIN_URLS:
        return False, "too_few_urls"
    if st is not None:
        if st.dormant_until is not None and st.dormant_until > now:
            return False, "dormant"
        if st.last_run_at is not None and (now - st.last_run_at).total_seconds() < cfg.rerun_after_h * 3600:
            return False, "ran_recently"
    return True, ""


def rank(cands: list[KeyCandidate], states: dict, class_stats: dict, busy: set, *,
         now: _dt.datetime, cfg: SchedulerConfig | None = None,
         rng: random.Random | None = None) -> list[tuple[float, KeyCandidate]]:
    """Eligible keys, most valuable first. One Thompson draw per key."""
    cfg = cfg or SchedulerConfig()
    rng = rng or random.Random()
    out = []
    for c in cands:
        st = states.get(c.key)
        ok, _why = eligible(c, st, busy, now, cfg)
        if not ok:
            continue
        a, b = posterior(class_prior(class_stats, c.klass, cfg), st)
        out.append((c.fails_7d * rng.betavariate(a, b), c))
    out.sort(key=lambda t: -t[0])
    return out


def pick_urls(urls: list, n: int = MIN_URLS) -> list[str]:
    """``n`` distinct URLs, in the order given (newest first): the first is
    explored, the others test whether the result generalises."""
    out: list[str] = []
    for u in urls:
        if isinstance(u, str) and u and u not in out:
            out.append(u)
        if len(out) >= n:
            break
    return out


def dormancy(fail_streak: int, barrier: str = "") -> _dt.timedelta:
    """How long to leave a key alone after its ``fail_streak``-th
    consecutive failed run."""
    from learner.solver import HARD_BARRIERS

    if barrier in HARD_BARRIERS:
        return _dt.timedelta(days=HARD_BARRIER_DAYS)
    i = max(1, int(fail_streak)) - 1
    return _dt.timedelta(days=DORMANCY_DAYS[min(i, len(DORMANCY_DAYS) - 1)])


def budget_left(runs_today: int, tokens_today: int, cfg: SchedulerConfig) -> bool:
    return runs_today < cfg.daily_runs and tokens_today < cfg.daily_tokens
