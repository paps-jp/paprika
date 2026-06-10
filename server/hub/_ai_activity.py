# -*- coding: utf-8 -*-
"""Lightweight in-memory live-activity tracking for the #ai 稼働中 tab.

Two cheap primitives, all in-process (NO DB, NO I/O) so the live view costs
essentially nothing:

  * in-flight gauges -- ``track()`` context manager wrapped around each AI
    LLM call (judge / distiller / codegen). Mirrors
    ``perception_llm._VisionGauge``; sync __enter__/__exit__ around an await
    is fine on the single hub event loop. Vision/perception keeps its own
    gauge in perception_llm (read separately by the endpoint).
  * a recent-events ring (capped deque) -- ``record_event()`` called at the
    few meaningful AI-loop moments (distill / perceive / escalate / recipe).

Read back via ``inflight_snapshot()`` / ``recent_events()`` from the
``/ai/activity`` endpoint. Everything is best-effort and must NEVER disrupt
the AI flow.
"""
from __future__ import annotations

import time
from collections import deque

_active: dict[str, int] = {}
_total: dict[str, int] = {}
_peak: dict[str, int] = {}
_active_slug: dict[str, int] = {}   # per-ENGINE in-flight (for 稼働中 status)
_events: "deque[dict]" = deque(maxlen=80)


class _Gauge:
    __slots__ = ("k", "slug")

    def __init__(self, k: str, slug: str = "") -> None:
        self.k = k
        self.slug = slug or ""

    def __enter__(self) -> "_Gauge":
        try:
            n = _active.get(self.k, 0) + 1
            _active[self.k] = n
            _total[self.k] = _total.get(self.k, 0) + 1
            if n > _peak.get(self.k, 0):
                _peak[self.k] = n
            if self.slug:
                _active_slug[self.slug] = _active_slug.get(self.slug, 0) + 1
        except Exception:
            pass
        return self

    def __exit__(self, *exc) -> bool:
        try:
            _active[self.k] = max(0, _active.get(self.k, 0) - 1)
            if self.slug:
                _active_slug[self.slug] = max(0, _active_slug.get(self.slug, 0) - 1)
        except Exception:
            pass
        return False


def track(kind: str, slug: str = "") -> _Gauge:
    """Count one in-flight AI call of ``kind`` (and, if given, the engine
    ``slug``) for the duration of the block.

    Usage::  with track("judge", slug=tgt.engine_slug): resp = await client.post(...)
    """
    return _Gauge(kind, slug)


def active_engine_slugs() -> list:
    """Engine slugs with >=1 in-flight AI call right now (for 稼働中 status)."""
    try:
        return [s for s, n in _active_slug.items() if n > 0]
    except Exception:
        return []


def inflight_snapshot() -> dict:
    """``{kind: {active,total,peak}}`` for every kind seen since hub start."""
    out: dict[str, dict] = {}
    for k in set(_active) | set(_total) | set(_peak):
        out[k] = {
            "active": int(_active.get(k, 0)),
            "total": int(_total.get(k, 0)),
            "peak": int(_peak.get(k, 0)),
        }
    return out


def record_event(kind: str, summary: str = "", *, host: str = "", job_id: str = "") -> None:
    """Append one AI-loop event to the recent-events ring (best-effort).

    ``kind`` is a short tag (distill / perceive / escalate / recipe / ...).
    ``at`` is a unix ts (the client renders "Ns ago"); the hub clock is UTC.
    """
    try:
        _events.appendleft({
            "at": time.time(),
            "kind": str(kind or "")[:24],
            "summary": str(summary or "")[:160],
            "host": str(host or "")[:120],
            "job_id": str(job_id or "")[:64],
        })
    except Exception:
        pass


def recent_events(n: int = 50) -> list:
    try:
        return list(_events)[: max(0, int(n))]
    except Exception:
        return []
