"""The learner's status, as one document a person can glance at.

The learner runs unattended; this is how someone who looks in once in a
while sees what it is doing without a shell on its host: is it alive, what
playbooks exist and how their trials compare with the comparison group,
what the Solver spent its daily budget on, and how the fleet's real jobs
are being judged.

Same shape as the playbook publication (``learner/playbook.py``): the
learner writes ONE JSON document under a Redis key, the hubs only read it
(``server/hub/routes/learn.py``). Nothing here is an input to any decision
-- a wrong or missing status changes nothing the learner or the hubs do.

``build`` is pure; the SQL that feeds it lives in ``learner/store.py``.
"""
from __future__ import annotations

import datetime as _dt

from learner import playbook as P
from learner.report import wilson
from learner.verify import OUTCOMES

STATUS_VERSION = 1
REDIS_KEY = "paprika:learn:status"
#: The document expires on its own, so a dead learner cannot leave a status
#: that looks current. The reader also reports the document's age.
TTL_S = 3600
MAX_RETIRED = 20       # most recently retired playbooks kept in the document
MAX_RECENT_RUNS = 30

#: The arm a playbook is applied in, and the arm it is compared against.
_ARMS = {P.TRIAL: ("trial", "pb_control"), P.ACTIVE: ("active", "pb_holdout")}


def _iso(t: _dt.datetime | _dt.date | None) -> str | None:
    """UTC timestamp as text (the learner's DATETIME columns are naive UTC)."""
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        return t.replace(microsecond=0).isoformat() + "Z"
    return t.isoformat()


def _arm(counts: dict | None) -> dict:
    s, n = P.scored(counts)
    lo, hi = wilson(s, n)
    return {
        "n": int((counts or {}).get("n", 0) or 0), "scored": n, "success": s,
        "rate": (s / n) if n else None,
        "lo": lo if n else None, "hi": hi if n else None,
    }


def playbook_row(pb: P.Playbook, arms: dict | None) -> dict:
    """One playbook with the two arms its next transition is decided from.

    A retired playbook shows whichever pair it has numbers for (it was
    retired from trial or from active; only that pair has counts)."""
    arms = arms or {}
    applied_arm, compare_arm = _ARMS.get(pb.state) or (
        ("active", "pb_holdout") if arms.get("active") else ("trial", "pb_control")
    )
    return {
        "id": pb.id, "host": pb.host, "template": pb.template, "intent": pb.intent,
        "state": pb.state, "version": pb.version, "reason": pb.reason,
        "apply_pct": pb.apply_pct, "holdout_pct": pb.holdout_pct,
        "promoted_diff": pb.promoted_diff,
        "n_actions": len(pb.actions) if isinstance(pb.actions, list) else 0,
        "klass": str((pb.provenance or {}).get("klass") or ""),
        "created_at": _iso(pb.created_at), "state_changed_at": _iso(pb.state_changed_at),
        "applied_arm": applied_arm, "compare_arm": compare_arm,
        "applied": _arm(arms.get(applied_arm)), "compare": _arm(arms.get(compare_arm)),
    }


def playbooks_section(pbs: list[P.Playbook], counts: dict) -> dict:
    by_state = {s: 0 for s in P.STATES}
    for p in pbs:
        by_state[p.state] = by_state.get(p.state, 0) + 1
    order = {P.ACTIVE: 0, P.TRIAL: 1, P.CANDIDATE: 2}
    live = sorted((p for p in pbs if p.state != P.RETIRED),
                  key=lambda p: (order.get(p.state, 9), -p.id))
    retired = sorted((p for p in pbs if p.state == P.RETIRED),
                     key=lambda p: (p.state_changed_at or _dt.datetime.min, p.id),
                     reverse=True)[:MAX_RETIRED]
    return {
        "counts": by_state,
        "items": [playbook_row(p, counts.get(p.id)) for p in live + retired],
    }


def solver_section(day_rows: list[tuple], recent_rows: list[tuple], *, today: _dt.date,
                   daily_runs: int, daily_tokens: int, enabled: bool) -> dict:
    """``day_rows``: (day, result, runs, tokens). ``recent_rows``: see
    ``Store.recent_runs``. Infra results cost no run of the daily budget
    (same rule as ``Store.usage_today``)."""
    days: dict = {}
    for day, result, n, tokens in day_rows:
        d = days.setdefault(day, {"day": _iso(day), "runs": 0, "tokens": 0, "by_result": {}})
        d["by_result"][result] = d["by_result"].get(result, 0) + int(n or 0)
        d["tokens"] += int(tokens or 0)
        if result != "infra":
            d["runs"] += int(n or 0)
    t = days.get(today) or {"day": _iso(today), "runs": 0, "tokens": 0, "by_result": {}}
    return {
        "enabled": bool(enabled),
        "today": {**t, "budget_runs": int(daily_runs), "budget_tokens": int(daily_tokens)},
        "days": [days[d] for d in sorted(days, reverse=True)],
        "recent": [
            {"ts": _iso(r[0]), "host": r[1], "template": r[2], "klass": r[3], "url": r[4],
             "result": r[5], "reason": r[6], "steps": int(r[7] or 0), "tokens": int(r[8] or 0),
             "duration_s": float(r[9] or 0.0), "pb_id": r[10]}
            for r in recent_rows[:MAX_RECENT_RUNS]
        ],
    }


def keys_section(rows: list[tuple]) -> dict:
    """``rows``: (klass, keys, runs, solved, dormant now)."""
    by_class = [
        {"klass": r[0] or "", "keys": int(r[1] or 0), "runs": int(r[2] or 0),
         "solved": int(r[3] or 0), "dormant": int(r[4] or 0)}
        for r in rows
    ]
    by_class.sort(key=lambda c: -c["keys"])
    return {
        "keys": sum(c["keys"] for c in by_class), "dormant": sum(c["dormant"] for c in by_class),
        "runs": sum(c["runs"] for c in by_class), "solved": sum(c["solved"] for c in by_class),
        "by_class": by_class,
    }


def outcomes_section(day_rows: list[tuple], reason_rows: list[tuple]) -> dict:
    """``day_rows``: (day, intent, n, then one sum per ``OUTCOMES``).
    ``reason_rows``: (intent, reason, n) of failed jobs in the last day."""
    days = []
    for r in day_rows:
        c = {"n_" + o: int(v or 0) for o, v in zip(OUTCOMES, r[3:])}
        s, n = P.scored(c)
        days.append({
            "day": _iso(r[0]), "intent": r[1], "n": int(r[2] or 0), "scored": n,
            "success": s, "rate": (s / n) if n else None,
            **{o: c["n_" + o] for o in OUTCOMES},
        })
    days.sort(key=lambda d: (d["day"], d["intent"]), reverse=True)
    return {
        "days": days,
        "fail_reasons_24h": [
            {"intent": r[0], "reason": r[1], "n": int(r[2] or 0)} for r in reason_rows
        ],
    }


def build(*, now_ts: float, started_ts: float, manifest: dict, config: dict,
          consumer: dict | None, autopilot: dict | None,
          playbooks: dict, solver: dict, keys: dict, outcomes: dict) -> dict:
    return {
        "v": STATUS_VERSION, "ts": float(now_ts), "started_ts": float(started_ts),
        "manifest": dict(manifest or {}), "config": dict(config or {}),
        "consumer": dict(consumer or {}), "autopilot": dict(autopilot or {}),
        "playbooks": playbooks, "solver": solver, "keys": keys, "outcomes": outcomes,
    }
