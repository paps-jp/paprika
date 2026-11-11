"""Experiment-arm assignment for learned fetch knowledge (design §7.2, §15.4 G2).

A recipe (later: a playbook) that is replayed unconditionally can never be
evaluated -- there is nothing to compare it with. So a deterministic slice
of the jobs that WOULD get one runs without it (the ``holdout`` arm), and
the learner's ledger compares the two arms on the same host/template over
the same period.

The decision is a pure function of the job id, so a redriven job lands in
the same arm every time and every hub agrees without coordination.

The arm is recorded on ``JobOptions.learn`` (free-form dict, persisted with
the job) and travels to the learner in the outcome event. The hub does not
interpret it beyond this module.
"""
from __future__ import annotations

import zlib

from server.hub._state import state

ARM_RECIPE = "recipe"
ARM_HOLDOUT = "holdout"


def holdout_pct() -> int:
    """Live, cross-hub setting ``learn_recipe_holdout_pct`` (0 = off)."""
    try:
        if state.settings is not None:
            v = int(state.settings.get("learn_recipe_holdout_pct", 0) or 0)
            return max(0, min(v, 100))
    except Exception:
        pass
    return 0


def bucket(job_id: str) -> int:
    """0..99, stable for a job id. Salted so it is independent of the
    learner's own 1% detail sample (which hashes the bare id)."""
    return zlib.crc32(("recipe-holdout:" + (job_id or "")).encode("utf-8")) % 100


def in_holdout(job_id: str, pct: int | None = None) -> bool:
    p = holdout_pct() if pct is None else pct
    return p > 0 and bucket(job_id) < p


def apply_recipe_arm(opts, job_id: str, picked) -> str | None:
    """Stamp the picked recipe -- or withhold it -- on ``opts``.

    ``picked`` is the HostRecipe ``pick_recipe`` matched (or None). Returns
    the arm, or None when no recipe matched (nothing to assign).
    Never raises: on any error the job simply gets the recipe, i.e. the
    pre-experiment behaviour.
    """
    if picked is None:
        return None
    try:
        pattern = str(getattr(picked, "pattern", "") or "")[:300]
        pct = holdout_pct()
        if in_holdout(job_id, pct):
            opts.fetch_recipe = None
            opts.learn = {"arm": ARM_HOLDOUT, "pattern": pattern, "pct": pct}
            return ARM_HOLDOUT
        opts.fetch_recipe = picked.to_json()
        opts.learn = {"arm": ARM_RECIPE, "pattern": pattern, "pct": pct}
        return ARM_RECIPE
    except Exception:
        try:
            opts.fetch_recipe = picked.to_json()
        except Exception:
            pass
        return ARM_RECIPE
