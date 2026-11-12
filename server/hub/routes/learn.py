"""``GET /learn/status`` -- what the out-of-process learner is doing.

The learner (``learner/``) publishes one JSON status document under a Redis
key (``learner/status.py``). This route is the hub's whole share: read that
document and hand it to the admin UI, together with this hub's own view of
the playbook mechanism (is the setting on, how old is the set it applies).

Read-only, one Redis GET, no job-table query. The hub never writes the
document and nothing here feeds dispatch.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter

from server.hub import _playbooks

router = APIRouter(tags=["Learn"])

#: Must equal ``learner.status.REDIS_KEY`` (the hub does not import the
#: learner package: it is not part of the hub image).
STATUS_KEY = "paprika:learn:status"
#: The learner rewrites its status every few minutes. Older than this and
#: the page says so instead of presenting stale numbers as current.
STALE_AFTER_S = 900.0


def shape(raw: str | bytes | None, *, now: float, hub: dict) -> dict:
    """The response for a raw Redis value. Pure.

    ``available`` is False when the learner has never published (or its
    document expired, or is not valid JSON); ``stale`` is True when a
    document exists but the learner stopped refreshing it.
    """
    out = {"available": False, "stale": False, "age_s": None, "hub": hub, "status": None}
    if raw is None:
        return out
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        doc = json.loads(raw)
        ts = float(doc.get("ts") or 0.0)
    except Exception:
        return out
    if not isinstance(doc, dict):
        return out
    age = max(0.0, now - ts)
    out.update(available=True, stale=age > STALE_AFTER_S, age_s=round(age, 1), status=doc)
    return out


@router.get("/learn/status")
async def learn_status() -> dict:
    hub = {"playbooks_enabled": _playbooks.enabled(), "playbooks": _playbooks.stats()}
    raw = None
    try:
        r = _playbooks._redis()
        if r is not None:
            raw = await asyncio.wait_for(r.get(STATUS_KEY), timeout=3.0)
    except Exception:
        raw = None
    return shape(raw, now=time.time(), hub=hub)
