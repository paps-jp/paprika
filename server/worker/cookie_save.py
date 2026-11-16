"""When a job's cookie save to the hub can be left out.

Every fetch ends by sending the host's cookies to the hub (``PUT
/hosts/{host}``). The hub writes the host's whole record to its disk, writes
it to MariaDB, and broadcasts it to every other hub, each of which writes the
whole record to its own disk as well. Measured on production, 2026-11-16:
about 900 saves a minute fleet-wide, so each of the 20 hubs rewrote ~895 host
files a minute (35 MB/min each, ~1.2 TB/day of SSD writes in all), while only
~200 different hosts were involved in any one minute -- the same host's record
was being rewritten several times a minute.

What changed between two consecutive saves of one host (1,526 compared):
a cookie's value 55%, only expiry/flags 37%, nothing but the timestamp 6.5%,
a cookie added or removed 1.8%.

So a save is left out when the hub already has the same set of cookies for
this host and that record was saved less than ``PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S``
ago. A cookie that appears or disappears (a login, a challenge clearance) is
always saved at once; a value that only rotated waits for the next save after
the interval. ``0`` turns this off and every job saves, as before.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_DEFAULT_MIN_INTERVAL_S = 600.0


def min_interval_s() -> float:
    try:
        v = float(os.environ.get("PAPRIKA_COOKIE_SAVE_MIN_INTERVAL_S") or _DEFAULT_MIN_INTERVAL_S)
    except (TypeError, ValueError):
        v = _DEFAULT_MIN_INTERVAL_S
    return max(0.0, v)


def _identity(cookie: dict) -> tuple[str, str, str]:
    """What makes two cookies "the same cookie": name, domain, path."""
    dom = str(cookie.get("domain") or "").lower().lstrip(".")
    return (str(cookie.get("name") or ""), dom, str(cookie.get("path") or "/"))


def _age_s(updated_at, now: datetime) -> float | None:
    """Seconds since the hub's ``updated_at`` (UTC, with or without ``Z``)."""
    if not updated_at or not isinstance(updated_at, str):
        return None
    try:
        t = datetime.fromisoformat(updated_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds()


def redundant_age_s(
    existing: dict | None,
    new_cookies: list[dict] | None,
    *,
    now: datetime | None = None,
    interval_s: float | None = None,
) -> float | None:
    """Age in seconds of the hub's record when saving ``new_cookies`` over it
    would add nothing worth a write; ``None`` when the save should go ahead.

    ``existing`` is the hub's ``GET /hosts/{host}`` reply (None when the host
    is not registered yet).
    """
    interval = min_interval_s() if interval_s is None else interval_s
    if interval <= 0 or not isinstance(existing, dict):
        return None
    age = _age_s(existing.get("updated_at"), now or datetime.now(timezone.utc))
    # A record stamped in the future (clock skew) is not evidence of a recent save.
    if age is None or age < 0 or age >= interval:
        return None
    try:
        old = {_identity(c) for c in (existing.get("cookies") or []) if isinstance(c, dict)}
        new = {_identity(c) for c in (new_cookies or []) if isinstance(c, dict)}
    except Exception:
        return None
    if old != new:
        return None
    return age
