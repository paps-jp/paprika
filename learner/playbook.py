"""Playbooks: verified, replayable steps with a life cycle (design §7).

A playbook is a short list of deterministic actions for one
(host, URL template, intent). It is knowledge only in so far as it can be
replayed and measured:

    candidate   the Solver found it, and replaying it (no LLM) succeeded on
                the URL it was found on AND on other URLs of the template
    trial       replayed on ``apply_pct`` % of the matching real jobs; the
                rest of those jobs is the comparison group
    active      replayed on all matching jobs except a small holdout
    retired     no longer applied (with the reason recorded)

Every transition is decided by ``decide`` -- a pure function of the
playbook and its measured counts. No human is in the loop, and no LLM
opinion is an input: the only evidence is Verifier outcomes of real jobs.

This module is pure (no I/O). The SQL lives in ``learner/store.py``; the
hub-side reader of ``published_doc`` is ``server/hub/_playbooks.py``.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from learner.report import wilson
from learner.verify import SCORED, SUCCESS

CANDIDATE = "candidate"
TRIAL = "trial"
ACTIVE = "active"
RETIRED = "retired"
STATES = (CANDIDATE, TRIAL, ACTIVE, RETIRED)

#: The document the hub reads. Bump only on an incompatible change.
PUBLISH_VERSION = 1
REDIS_KEY = "paprika:learn:playbooks"
MAX_ACTIONS = 40          # must not exceed server/hub/_playbooks.MAX_ACTIONS

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS learn_playbooks (
        id BIGINT NOT NULL AUTO_INCREMENT,
        host VARCHAR(190) NOT NULL,
        template VARCHAR(300) NOT NULL,
        intent VARCHAR(16) NOT NULL,
        version INT NOT NULL DEFAULT 1,
        state VARCHAR(16) NOT NULL,
        apply_pct INT NOT NULL DEFAULT 20,
        holdout_pct INT NOT NULL DEFAULT 5,
        promoted_diff DOUBLE NULL,
        actions JSON NOT NULL,
        provenance JSON NULL,
        reason VARCHAR(64) NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        state_changed_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        KEY idx_key_state (host, template, intent, state),
        KEY idx_state (state)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    # One row per (host, template, intent) the scheduler has ever tried:
    # how often the Solver ran there, how it went, and until when the key
    # is left alone (design §8.2 dormancy).
    """
    CREATE TABLE IF NOT EXISTS learn_key_state (
        host VARCHAR(190) NOT NULL,
        template VARCHAR(300) NOT NULL,
        intent VARCHAR(16) NOT NULL,
        klass VARCHAR(32) NOT NULL DEFAULT '',
        runs INT NOT NULL DEFAULT 0,
        solved INT NOT NULL DEFAULT 0,
        fail_streak INT NOT NULL DEFAULT 0,
        dormant_until DATETIME NULL,
        last_run_at DATETIME NULL,
        last_result VARCHAR(32) NOT NULL DEFAULT '',
        PRIMARY KEY (host, template, intent)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
    # One row per Solver run, with its trace: the audit trail of every
    # LLM-driven action the learner ever took.
    """
    CREATE TABLE IF NOT EXISTS learn_solver_runs (
        id BIGINT NOT NULL AUTO_INCREMENT,
        ts DATETIME NOT NULL,
        host VARCHAR(190) NOT NULL,
        template VARCHAR(300) NOT NULL,
        intent VARCHAR(16) NOT NULL,
        klass VARCHAR(32) NOT NULL DEFAULT '',
        url VARCHAR(600) NOT NULL DEFAULT '',
        result VARCHAR(32) NOT NULL,
        reason VARCHAR(120) NOT NULL DEFAULT '',
        steps INT NOT NULL DEFAULT 0,
        llm_calls INT NOT NULL DEFAULT 0,
        tokens INT NOT NULL DEFAULT 0,
        duration_s DOUBLE NOT NULL DEFAULT 0,
        pb_id BIGINT NULL,
        manifest JSON NULL,
        trace JSON NULL,
        PRIMARY KEY (id),
        KEY idx_ts (ts),
        KEY idx_key_ts (host, template, intent, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
    """,
)


@dataclass
class Playbook:
    id: int
    host: str
    template: str
    intent: str
    state: str
    actions: list
    version: int = 1
    apply_pct: int = 20
    holdout_pct: int = 5
    promoted_diff: float | None = None
    state_changed_at: _dt.datetime | None = None
    created_at: _dt.datetime | None = None
    reason: str = ""
    provenance: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.host, self.template, self.intent)


@dataclass(frozen=True)
class LifecycleConfig:
    trial_pct: int = 20
    #: When the comparison group essentially never succeeds there is little
    #: to lose, so the trial takes half the traffic and finishes sooner.
    trial_pct_hopeless: int = 50
    hopeless_vsr: float = 0.02
    holdout_pct: int = 5
    min_n: int = 20               # scored jobs before a trial is judged
    promote_margin: float = 0.05  # trial's lower bound must beat control by this
    give_up_n: int = 50           # scored trial jobs without promotion -> retire
    trial_max_days: float = 14.0  # a trial that never gets traffic is dropped
    decay_min_n: int = 50         # recent scored active jobs before decay is judged
    decay_min_holdout_n: int = 10
    decay_window_days: int = 3
    unused_days: float = 30.0


@dataclass(frozen=True)
class Decision:
    state: str                    # the state to move to
    reason: str
    promoted_diff: float | None = None


def scored(counts: dict | None) -> tuple[int, int]:
    """(successes, scored n) of a counts dict with ``n_<outcome>`` keys."""
    c = counts or {}
    n = sum(int(c.get("n_" + o, 0) or 0) for o in SCORED)
    return int(c.get("n_" + SUCCESS, 0) or 0), n


def rate(counts: dict | None) -> float | None:
    s, n = scored(counts)
    return (s / n) if n else None


def decide(pb: Playbook, arms: dict, *, now: _dt.datetime,
           cfg: LifecycleConfig | None = None, recent: dict | None = None,
           last_applied_days: float | None = None) -> Decision | None:
    """The next state for ``pb``, or None to leave it where it is.

    ``arms``    all-time counts per arm: {"trial": {...}, "pb_control": {...},
                "active": {...}, "pb_holdout": {...}}
    ``recent``  the same over the last ``decay_window_days`` (active only)
    ``last_applied_days``  days since a job last ran the playbook
                (None = never; the time in the state is used instead)
    """
    cfg = cfg or LifecycleConfig()
    age_days = _age_days(pb.state_changed_at, now)

    if pb.state == TRIAL:
        ts, tn = scored(arms.get("trial"))
        cs, cn = scored(arms.get("pb_control"))
        if tn >= cfg.min_n and cn >= cfg.min_n:
            t_lo, t_hi = wilson(ts, tn)
            c_rate = cs / cn
            if t_lo > c_rate + cfg.promote_margin:
                return Decision(ACTIVE, "trial_beats_control", promoted_diff=(ts / tn) - c_rate)
            if t_hi < c_rate:
                return Decision(RETIRED, "trial_worse_than_control")
        if tn >= cfg.give_up_n and cn >= cfg.min_n:
            return Decision(RETIRED, "trial_no_gain")
        if age_days is not None and age_days > cfg.trial_max_days:
            return Decision(RETIRED, "trial_no_traffic")
        return None

    if pb.state == ACTIVE:
        win = recent if recent is not None else arms
        as_, an = scored(win.get("active"))
        hs, hn = scored(win.get("pb_holdout"))
        if an >= cfg.decay_min_n and hn >= cfg.decay_min_holdout_n:
            floor = (hs / hn) + max(0.0, (pb.promoted_diff or 0.0)) / 2.0
            if (as_ / an) < floor:
                # The site changed (or the gain was never real): the
                # playbook no longer earns its place. The key becomes
                # eligible for a fresh Solver run.
                return Decision(RETIRED, "active_decayed")
        idle = last_applied_days if last_applied_days is not None else age_days
        if idle is not None and idle > cfg.unused_days:
            return Decision(RETIRED, "active_unused")
        return None

    return None


def trial_pct_for(control_vsr: float | None, cfg: LifecycleConfig | None = None) -> int:
    cfg = cfg or LifecycleConfig()
    if control_vsr is not None and control_vsr <= cfg.hopeless_vsr:
        return cfg.trial_pct_hopeless
    return cfg.trial_pct


def admit_candidates(pbs: list[Playbook]) -> list[Playbook]:
    """Candidates that may start their trial now: the key has no trial yet
    and only the oldest candidate of a key goes (one trial per key)."""
    busy = {p.key for p in pbs if p.state == TRIAL}
    out = []
    for p in sorted((p for p in pbs if p.state == CANDIDATE), key=lambda p: p.id):
        if p.key in busy:
            continue
        busy.add(p.key)
        out.append(p)
    return out


def published_doc(pbs: list[Playbook], *, now_ts: float, rev: int) -> dict:
    """The document the hubs read: applicable playbooks only."""
    items = []
    for p in sorted(pbs, key=lambda p: p.id):
        if p.state not in (TRIAL, ACTIVE):
            continue
        if not isinstance(p.actions, list) or not p.actions or len(p.actions) > MAX_ACTIONS:
            continue
        items.append({
            "id": p.id, "ver": p.version, "host": p.host, "template": p.template,
            "intent": p.intent, "state": p.state,
            "apply_pct": p.apply_pct if p.state == TRIAL else 100,
            "holdout_pct": p.holdout_pct if p.state == ACTIVE else 0,
            "actions": p.actions,
        })
    return {"v": PUBLISH_VERSION, "rev": int(rev), "ts": float(now_ts), "items": items}


def _age_days(since: _dt.datetime | None, now: _dt.datetime) -> float | None:
    if since is None:
        return None
    return (now - since).total_seconds() / 86400.0
