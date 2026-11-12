"""Applying learned playbooks at dispatch (design §7.2, §15.2, §15.4 G3).

The learner (out of process, ``learner/``) owns the playbooks. It publishes
the ones that are currently applicable -- ``trial`` and ``active`` -- as ONE
JSON document under a Redis key. This module is the hub's whole share of
the mechanism:

  * keep a local copy of that document (refreshed in the background),
  * at dispatch, look the job's (host, URL template, intent) up in it,
  * put the job into an experiment arm, deterministically from its id,
  * stamp the playbook's actions onto ``JobOptions.fetch_recipe`` (the
    worker's existing recipe player runs them; no worker change) and the
    arm onto ``JobOptions.learn`` (it travels to the learner in the
    outcome event).

Arms (one bucket 0..99 per job, ``crc32("pb:" + job_id) % 100``):

    trial        a trial playbook, applied            bucket <  apply_pct
    pb_control   a trial playbook, withheld           the rest (no active)
    active       an active playbook, applied
    pb_holdout   an active playbook, withheld         bucket >= 100 - holdout_pct

A job in ``pb_control`` / ``pb_holdout`` runs with NO replayed actions at
all (the legacy host recipe is skipped too), so the comparison is
"playbook vs plain fetch".

Ships dark: nothing happens unless Settings ``learn_playbooks_enabled`` is
true AND the learner has published something. Never raises into dispatch;
on any error the job is dispatched exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import zlib

from server.hub._state import state

log = logging.getLogger("paprika.hub.playbooks")

REDIS_KEY = "paprika:learn:playbooks"
REFRESH_INTERVAL_S = 30.0
#: A published set older than this is ignored: if the learner is dead its
#: trials must not keep steering traffic forever. The learner republishes
#: every few minutes even when nothing changed.
MAX_AGE_S = 1800.0
MAX_ACTIONS = 40

ARM_TRIAL = "trial"
ARM_CONTROL = "pb_control"
ARM_ACTIVE = "active"
ARM_HOLDOUT = "pb_holdout"
APPLIED_ARMS = (ARM_TRIAL, ARM_ACTIVE)

_index: dict[tuple[str, str, str], dict] = {}
_meta = {"rev": None, "ts": 0.0, "loaded_at": 0.0, "n": 0, "errors": 0}
_task: asyncio.Task | None = None


def enabled() -> bool:
    """Live, cross-hub setting ``learn_playbooks_enabled``."""
    try:
        if state.settings is not None:
            return bool(state.settings.get("learn_playbooks_enabled", False))
    except Exception:
        pass
    return False


def stats() -> dict:
    return dict(_meta)


def _redis():
    from server.hub._outcome_stream import _redis as _r
    return _r()


def build_index(doc: dict, *, now: float | None = None) -> dict:
    """Published document -> {(host, template, intent): {"trial": item, "active": item}}.

    Pure. A stale or malformed document yields an empty index (= no
    playbook is applied), which is the safe direction.
    """
    out: dict[tuple[str, str, str], dict] = {}
    if not isinstance(doc, dict):
        return out
    now = time.time() if now is None else now
    try:
        if now - float(doc.get("ts") or 0.0) > MAX_AGE_S:
            return out
    except Exception:
        return out
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        st = it.get("state")
        actions = it.get("actions")
        if st not in ("trial", "active") or not isinstance(actions, list) or not actions:
            continue
        if len(actions) > MAX_ACTIONS:
            continue
        try:
            item = {
                "id": int(it["id"]),
                "ver": int(it.get("ver") or 1),
                "state": st,
                "apply_pct": max(0, min(int(it.get("apply_pct") or 0), 100)),
                "holdout_pct": max(0, min(int(it.get("holdout_pct") or 0), 100)),
                "actions": actions,
            }
            key = (str(it["host"]).lower(), str(it["template"]), str(it["intent"]))
        except Exception:
            continue
        slot = out.setdefault(key, {})
        # One trial and one active per key; a duplicate from a learner bug
        # must not flip-flop, so the lowest id wins.
        cur = slot.get(st)
        if cur is None or item["id"] < cur["id"]:
            slot[st] = item
    return out


def bucket(job_id: str) -> int:
    """0..99, stable per job. Salted so it is independent of the recipe
    holdout bucket and of the learner's detail sample."""
    return zlib.crc32(("pb:" + (job_id or "")).encode("utf-8")) % 100


def assign(job_id: str, slot: dict) -> tuple[str, dict, dict | None] | None:
    """(arm, playbook, versus) for a job, or None. Pure.

    ``versus`` is the trial playbook an ``active`` job doubles as the
    comparison group for (only when a key has both).
    """
    trial = slot.get("trial")
    active = slot.get("active")
    b = bucket(job_id)
    if trial is not None and b < trial["apply_pct"]:
        return (ARM_TRIAL, trial, None)
    if active is not None:
        if b >= 100 - active["holdout_pct"]:
            return (ARM_HOLDOUT, active, None)
        return (ARM_ACTIVE, active, trial)
    if trial is not None:
        return (ARM_CONTROL, trial, None)
    return None


def lookup(url: str, intent: str) -> dict | None:
    if not _index:
        return None
    from server.hub._outcome_stream import _host
    from server.hub._page_role import templatize

    host = _host(url)
    if not host:
        return None
    return _index.get((host, templatize(url) or "/", intent))


def apply_playbook_arm(opts, job_id: str, url: str) -> str | None:
    """Assign the job to a playbook arm, if a playbook covers its URL.

    Returns the arm, or None when no playbook applies (the caller then
    continues with the legacy host-recipe pick). When an arm IS returned
    the caller must not attach a legacy recipe.
    """
    try:
        if not enabled():
            return None
        ensure_refresher()
        if not _index:
            return None
        if getattr(opts, "fetch_recipe", None):
            return None
        if getattr(opts, "fetch_strategy", "recipe") == "normal":
            return None
        prior = getattr(opts, "learn", None)
        if isinstance(prior, dict) and prior.get("arm") == "solver":
            return None  # the learner's own test job: it chose its recipe
        intent = "video" if getattr(opts, "download_video", False) else "page"
        slot = lookup(url, intent)
        if not slot:
            return None
        got = assign(job_id, slot)
        if got is None:
            return None
        arm, pb, versus = got
        learn = {"arm": arm, "pb": pb["id"], "ver": pb["ver"]}
        if versus is not None:
            learn["vs"] = versus["id"]
        if arm in APPLIED_ARMS:
            opts.fetch_recipe = {
                "pattern": "*",
                "description": f"playbook #{pb['id']} v{pb['ver']}",
                "actions": pb["actions"],
                "created_by": "learner",
            }
        opts.learn = learn
        return arm
    except Exception:
        _meta["errors"] += 1
        return None


async def refresh_once() -> bool:
    """Reload the published set. Returns True when a document was read."""
    global _index
    r = _redis()
    if r is None:
        return False
    raw = await asyncio.wait_for(r.get(REDIS_KEY), timeout=3.0)
    if raw is None:
        if _index:
            _index = {}
            _meta.update(rev=None, ts=0.0, n=0)
        _meta["loaded_at"] = time.time()
        return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    doc = json.loads(raw)
    idx = build_index(doc)
    _index = idx
    _meta.update(
        rev=doc.get("rev") if isinstance(doc, dict) else None,
        ts=float(doc.get("ts") or 0.0) if isinstance(doc, dict) else 0.0,
        n=sum(len(s) for s in idx.values()),
        loaded_at=time.time(),
    )
    return True


async def _refresh_loop() -> None:
    global _index
    while True:
        try:
            if enabled():
                await refresh_once()
            elif _index:
                _index = {}
        except asyncio.CancelledError:
            raise
        except Exception:
            _meta["errors"] += 1
        await asyncio.sleep(REFRESH_INTERVAL_S)


def ensure_refresher() -> None:
    """Start the background refresh on first use (needs a running loop)."""
    global _task
    if _task is not None and not _task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _task = loop.create_task(_refresh_loop(), name="playbooks-refresh")
