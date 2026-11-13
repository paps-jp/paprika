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
TARGET_REASONS = ("overlay_wall", "video_not_captured", "video_short")
KNOWN_REASONS = ("overlay_wall", "video_not_captured", "video_short")
#: Which daily count holds the jobs of a target reason. "video_short" is a
#: PARTIAL outcome (a playable but short file -- mostly an advertisement
#: saved in place of the main video); the others are failures.
REASON_COUNT = {"video_short": "n_partial"}
TARGET_INTENT = "video"

#: Days a key is left alone after its 1st, 2nd, ... consecutive failed run.
#: Short: the engine is in-house, so a retry is cheap, and a retry opens
#: other URLs of the key than the last one did.
DORMANCY_DAYS = (0.25, 1, 3, 7)
HARD_BARRIER_DAYS = 30
#: Verdicts about the key (not one URL) that a later run will not change
#: soon. "not_video" is the Solver's "no_video" seen on two different URLs
#: of the key (learner/autopilot.py); a single sighting is not enough.
KEY_BARRIERS = ("not_video",)
MIN_URLS = 3


@dataclass(frozen=True)
class SchedulerConfig:
    target_reasons: tuple = TARGET_REASONS
    #: The engine is an in-house GPU, so these two are a runaway guard, not
    #: a cost budget (2026-11-14). What a run does cost is one browser lane
    #: of the fleet for about a minute, and three fetch jobs per replay.
    daily_runs: int = 1000          # Solver sessions per UTC day (infra results are free)
    daily_tokens: int = 50_000_000  # model tokens per UTC day
    min_fails_7d: int = 5           # do not bother below this
    #: Jobs of the key that produced a playable video in the last 7 days
    #: (Verifier success or partial). Below this the key is not worked on:
    #: a video is asked for on many pages that have none (shop items,
    #: contact forms, wikis), and no click turns those into a video page.
    #: 0 turns the check off.
    min_video_7d: int = 1
    rerun_after_h: float = 6.0      # never the same key twice within this
    prior_strength: float = 4.0     # how many pseudo-observations the class prior is worth
    default_prior: tuple[float, float] = (1.0, 3.0)


@dataclass
class KeyCandidate:
    host: str
    template: str
    intent: str
    klass: str                      # the dominant failure reason
    fails_7d: int = 0               # jobs of the target kind in 7 days (REASON_COUNT)
    scored_7d: int = 0
    success_7d: int = 0
    video_7d: int = 0               # success + partial: a playable video came out
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
    if c.video_7d < cfg.min_video_7d:
        return False, "no_video_seen"
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

    if barrier in HARD_BARRIERS or barrier in KEY_BARRIERS:
        return _dt.timedelta(days=HARD_BARRIER_DAYS)
    i = max(1, int(fail_streak)) - 1
    return _dt.timedelta(days=DORMANCY_DAYS[min(i, len(DORMANCY_DAYS) - 1)])


def budget_left(runs_today: int, tokens_today: int, cfg: SchedulerConfig) -> bool:
    return runs_today < cfg.daily_runs and tokens_today < cfg.daily_tokens
