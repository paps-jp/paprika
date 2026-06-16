"""Nightly read-only review of yesterday's per-host signals.

A scheduled task that runs once per day and, for each host with notable
activity (failures / escalations / new barriers) in the last 24h, asks
the reasoning engine to write a fresh strategy digest into
``host_strategy``. The digest is what the operator reads as a VISION.md
for that host, and what ``distiller_r1`` reads as standing context at
the start of every escalation.

Design:

* **Read-only effects**: never writes to skills, conventions,
  fetch_recipes, HostKnowledge.per_page, etc. The only side effect is
  ``host_strategy_upsert(updated_by='nightly_review')``. Operator-edited
  digests (``updated_by='operator'``) are preserved unless the operator
  themselves blanks them.
* **Cross-hub safe**: an advisory ``redis`` lease keeps only ONE hub
  per fleet from running the task on a given day. Lease is per-day,
  so if the elected hub dies mid-pass another picks it up next day.
* **Failure-bounded budget**: caps the number of hosts handled per
  night to keep token spend predictable (``PAPRIKA_NIGHTLY_REVIEW_MAX``).
* **Opt-in**: ``nightly_review_enabled`` setting, default False. Hour
  configurable via ``nightly_review_hour_utc`` (default 16 = 01:00 JST).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# Operator-tunable knobs (env defaults; Settings overrides at runtime).
_DEFAULT_HOUR_UTC = int(os.environ.get("PAPRIKA_NIGHTLY_REVIEW_HOUR_UTC", "16"))
_DEFAULT_MAX_HOSTS = int(os.environ.get("PAPRIKA_NIGHTLY_REVIEW_MAX", "30"))
_MIN_SIGNAL = int(os.environ.get("PAPRIKA_NIGHTLY_REVIEW_MIN_SIGNAL", "3"))

# Redis lease so only one hub runs the pass per day.
_LEASE_TTL_S = 23 * 3600  # ~23h, expires before the next run window
_LEASE_KEY_PREFIX = "paprika:nightly_review:lease:"


_SYSTEM_PROMPT = """You write a SHORT per-host strategy digest (VISION.md
style) for a web scraping fleet. The operator (and another reasoning AI)
reads this each morning to understand the host's quirks.

Output ONLY Markdown. NO preamble, NO JSON. About 6-12 bullet lines.

Structure your digest with these headings (omit a heading when there's
nothing to say for it -- don't pad):

## 何を取りに行くか
1-2 lines on the actual content type (videos, articles, images,
listings) and whether download_video applies.

## 効いた手 / 効かなかった手
What's been observed to work or fail on this host. Cite SPECIFICS:
"age gate dismissed by clicking #age-yes", "yt-dlp blocked, switched
to direct manifest fetch", etc. Pull from the signals below.

## 既知の壁
List the barriers we know exist: age gates, login walls, geoblock,
captchas, rate limits, overlay popups. One line each.

## 次に試すこと
1-3 concrete next moves. Be specific ("try requesting m3u8 manifest
URL directly" beats "look for alternative endpoints").

Tone: terse, operator-readable, no hedging. Skip "consider" / "might
be useful" -- give direct guidance.
"""


def _host_of(url: str) -> str:
    """Normalised hostname from a URL ('' on parse error).
    The jobs table holds URLs not hosts, so we derive the host here and
    group in Python instead of SQL."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        h = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h


async def _scan_24h_signals(pool: Any, *, since_ts: float) -> dict[str, dict]:
    """ONE query: pull every job created in the last 24h + its url/status/error.
    Group by host in Python (the jobs table has no host column).

    Returns ``{host: {failed_jobs, review_jobs, completed_jobs, common_errors}}``
    with errors already bucketed (top 5 per host). URL-template stats are
    pulled separately per-host since host_url_history HAS a host column.
    """
    sigs: dict[str, dict] = {}
    raw_errs: dict[str, dict[str, int]] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT url, status, error FROM jobs
                   WHERE created_at >= FROM_UNIXTIME(%s)""",
                (since_ts,),
            )
            rows = await cur.fetchall()
    for row in rows:
        if not row:
            continue
        url, status, error = row[0], row[1], row[2] if len(row) > 2 else None
        host = _host_of(str(url or ""))
        if not host:
            continue
        sig = sigs.setdefault(host, {
            "host": host,
            "failed_jobs": 0, "review_jobs": 0, "completed_jobs": 0,
            "escalated_jobs": 0,
            "common_errors": [], "url_templates": [],
        })
        if status == "failed":
            sig["failed_jobs"] += 1
            if error:
                bkt = raw_errs.setdefault(host, {})
                k = str(error)[:60].strip()
                bkt[k] = bkt.get(k, 0) + 1
        elif status == "review":
            sig["review_jobs"] += 1
        elif status == "completed":
            sig["completed_jobs"] += 1
    # Fold the error buckets into each host's sig (top 5 by frequency).
    for host, bkt in raw_errs.items():
        sigs[host]["common_errors"] = sorted(
            bkt.items(), key=lambda x: -x[1]
        )[:5]
    return sigs


async def _attach_url_templates(pool: Any, sigs: dict, *, since_ts: float) -> None:
    """For each host already in ``sigs``, pull its top URL templates from
    host_url_history (which DOES have a host column). Mutates sigs in place.
    Best-effort: a per-host failure leaves an empty list and continues."""
    for host, sig in sigs.items():
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT template, COUNT(*) FROM host_url_history
                           WHERE host=%s AND last_seen_at >= FROM_UNIXTIME(%s)
                             AND template IS NOT NULL
                           GROUP BY template ORDER BY COUNT(*) DESC LIMIT 8""",
                        (host, since_ts),
                    )
                    sig["url_templates"] = [
                        {"template": str(r[0]), "hits": int(r[1])}
                        for r in await cur.fetchall() if r
                    ]
        except Exception:
            sig["url_templates"] = []


def _pick_flagged_hosts(sigs: dict, *, limit: int, min_signal: int) -> list[str]:
    """Top hosts by (failed_jobs + review_jobs), filtered by min_signal."""
    flagged = []
    for host, sig in sigs.items():
        n = int(sig.get("failed_jobs", 0) or 0) + int(sig.get("review_jobs", 0) or 0)
        if n >= min_signal:
            flagged.append((host, n))
    flagged.sort(key=lambda x: -x[1])
    return [h for h, _ in flagged[:limit]]


def _format_signal(sig: dict, existing_strategy: str) -> str:
    """Render the gathered signal into the LLM user message."""
    parts: list[str] = []
    parts.append(f"HOST\n{sig['host']}\n")
    parts.append(
        "ACTIVITY (last 24h)\n"
        f"  completed_jobs: {sig.get('completed_jobs', 0)}\n"
        f"  failed_jobs:    {sig.get('failed_jobs', 0)}\n"
        f"  review_jobs:    {sig.get('review_jobs', 0)}\n"
    )
    if sig.get("common_errors"):
        parts.append(
            "TOP ERRORS (failed jobs)\n"
            + "\n".join(f"  [{n}] {e}" for e, n in sig["common_errors"])
            + "\n"
        )
    if sig.get("url_templates"):
        parts.append(
            "URL TEMPLATES (24h, top hits)\n"
            + "\n".join(f"  {t['template']}  ({t['hits']}x)" for t in sig["url_templates"])
            + "\n"
        )
    if existing_strategy and existing_strategy.strip():
        parts.append(
            "EXISTING STRATEGY (most-recent digest)\n"
            + existing_strategy.strip()[:1800]
            + "\n\n"
            "Refresh it: keep what's still true, add what's new from\n"
            "the signals above, remove what's stale. If the existing\n"
            "digest is already current, return it nearly unchanged.\n"
        )
    parts.append("Produce the digest Markdown now (no JSON, no fences).")
    return "\n".join(parts)


async def _ask_llm_for_digest(sig: dict, existing: str, target) -> str | None:
    """Call the reasoning engine. Returns the digest Markdown, or None on
    error / empty / engine unavailable."""
    user_msg = _format_signal(sig, existing)
    body = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.4,
        "max_tokens": 1200,
    }
    try:
        from server.hub.codegen import adapt_chat_body, record_engine_usage
        body = adapt_chat_body(target, body)
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=getattr(target, "timeout", 90.0)) as client:
            r = await client.post(target.url, json=body, headers=target.headers)
            if r.status_code >= 400:
                _log.info(
                    "[nightly-review] LLM %d from %s: %s",
                    r.status_code, target.url, r.text[:200],
                )
                return None
            payload = r.json()
        try:
            from server.hub.codegen import record_engine_usage
            record_engine_usage(target, payload.get("usage") or {})
        except Exception:
            pass
    except Exception as e:
        _log.info("[nightly-review] LLM call failed: %s: %s", type(e).__name__, e)
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    raw = msg.get("content") or ""
    # Reasoning models sometimes wrap their answer in <think>...</think>.
    try:
        from server.hub.judge_llm import _strip_think_block
        raw = _strip_think_block(raw)
    except Exception:
        pass
    raw = (raw or "").strip()
    if not raw:
        return None
    # Strip markdown code fences if the model insisted on them.
    if raw.startswith("```"):
        import re as _re
        raw = _re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = _re.sub(r"\n?```\s*$", "", raw).strip()
    return raw or None


async def _try_acquire_lease(state, day_str: str) -> bool:
    """Best-effort cross-hub lease so only ONE hub runs the pass per day.
    Returns True iff we got the lease.

    Uses the existing redis connection (state.redis). When redis isn't
    configured we just say "yes" -- single-hub deployments don't need
    coordination, and multi-hub installs should have redis."""
    redis = getattr(state, "redis", None)
    if redis is None:
        return True
    key = _LEASE_KEY_PREFIX + day_str
    hub_id = str(getattr(state, "hub_id", "")) or "hub-?"
    try:
        ok = await redis.set(key, hub_id, ex=_LEASE_TTL_S, nx=True)
        return bool(ok)
    except Exception as e:
        _log.info("[nightly-review] lease check failed (assume taken): %s", e)
        return False


async def _resolve_reasoning_target():
    """Reasoning engine target for the digest (distiller role preferred,
    falls back to judge role, then env default). Returns None when none
    of them resolve to an accepting engine."""
    try:
        from server.hub._state import state
        from server.hub._roles import resolve_role_engine_slug
        from server.hub.codegen import resolve_engine_target
        slug = await resolve_role_engine_slug("distiller")
        if not slug:
            slug = await resolve_role_engine_slug("judge")
        if not slug:
            slug = os.environ.get("PAPRIKA_R1_DISTILLER_ENGINE", "deepseek-r1")
        if state.engines is None:
            return None
        return resolve_engine_target(slug, state.engines)
    except Exception as e:
        _log.info("[nightly-review] target resolve failed: %s", e)
        return None


async def run_once() -> dict:
    """Execute one nightly review pass: gather signals, write digests.
    Returns a small stats dict suitable for logging. Safe to call
    on-demand (e.g. via an admin trigger) in addition to the scheduled
    invocation."""
    stats = {"started_at": time.time(), "hosts_considered": 0,
             "hosts_updated": 0, "hosts_skipped": 0, "elapsed_s": 0.0}
    try:
        from server.hub._state import state
    except Exception:
        return stats
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        _log.info("[nightly-review] no MariaDB pool, skipping")
        return stats

    # Cross-hub lease.
    day_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    have_lease = await _try_acquire_lease(state, day_str)
    if not have_lease:
        _log.info("[nightly-review] another hub holds the day's lease; skipping")
        return stats

    # Settings: cap, etc.
    max_hosts = _DEFAULT_MAX_HOSTS
    try:
        if state.settings is not None:
            max_hosts = int(state.settings.get("nightly_review_max_hosts", _DEFAULT_MAX_HOSTS))
    except Exception:
        pass

    target = await _resolve_reasoning_target()
    if target is None:
        _log.info("[nightly-review] no reasoning engine accepting; aborting")
        return stats

    since_ts = time.time() - 24 * 3600
    # Single pass over the 24h jobs window -> per-host signal dict. Then
    # attach url-template stats from host_url_history (which DOES have
    # a host column). Avoids the N+1 per-host queries the old code did,
    # AND fixes a bug where the old SQL assumed a non-existent jobs.host
    # column (jobs only carries url; host is derived in Python).
    sigs = await _scan_24h_signals(pool, since_ts=since_ts)
    hosts = _pick_flagged_hosts(sigs, limit=max_hosts, min_signal=_MIN_SIGNAL)
    stats["hosts_considered"] = len(hosts)
    _log.info("[nightly-review] %d host(s) flagged for digest update", len(hosts))
    # Trim sigs to just the flagged hosts before attaching templates so
    # we don't run an O(all-hosts) query.
    sigs = {h: sigs[h] for h in hosts if h in sigs}
    await _attach_url_templates(pool, sigs, since_ts=since_ts)

    from server.hub.mariadb import host_strategy_get, host_strategy_upsert
    for host in hosts:
        try:
            existing_rec = await host_strategy_get(pool, host)
            existing = (existing_rec.get("summary_md") or "") if existing_rec else ""
            # Operator-edited digests are preserved: nightly review never
            # clobbers them. Operator can explicitly delete the digest
            # to opt back in to auto-roll-up.
            if existing_rec and existing_rec.get("updated_by") == "operator":
                stats["hosts_skipped"] += 1
                continue
            sig = sigs.get(host) or {"host": host}
            digest = await _ask_llm_for_digest(sig, existing, target)
            if not digest:
                stats["hosts_skipped"] += 1
                continue
            await host_strategy_upsert(pool, host, digest, "nightly_review")
            stats["hosts_updated"] += 1
        except Exception as e:
            _log.info(
                "[nightly-review] host=%s failed (non-fatal): %s: %s",
                host, type(e).__name__, e,
            )
            stats["hosts_skipped"] += 1

    stats["elapsed_s"] = round(time.time() - stats["started_at"], 1)
    _log.info(
        "[nightly-review] complete: considered=%d updated=%d skipped=%d elapsed=%.1fs",
        stats["hosts_considered"], stats["hosts_updated"],
        stats["hosts_skipped"], stats["elapsed_s"],
    )
    return stats


async def scheduler_loop() -> None:
    """Long-running loop: sleep until the configured hour, run the pass,
    sleep until the next day. Designed to be created as a background
    asyncio task from the app lifespan."""
    while True:
        try:
            from server.hub._state import state
            enabled = False
            hour_utc = _DEFAULT_HOUR_UTC
            try:
                if state.settings is not None:
                    enabled = bool(state.settings.get("nightly_review_enabled", False))
                    hour_utc = int(state.settings.get("nightly_review_hour_utc", _DEFAULT_HOUR_UTC))
            except Exception:
                pass
            now = datetime.now(timezone.utc)
            target_dt = now.replace(hour=hour_utc % 24, minute=5, second=0, microsecond=0)
            if target_dt <= now:
                # already past today's slot -> aim for tomorrow's
                from datetime import timedelta
                target_dt = target_dt + timedelta(days=1)
            sleep_s = max(60.0, (target_dt - now).total_seconds())
            await asyncio.sleep(sleep_s)
            if not enabled:
                # Setting may have changed during sleep; re-check.
                try:
                    if state.settings is not None:
                        enabled = bool(state.settings.get("nightly_review_enabled", False))
                except Exception:
                    enabled = False
                if not enabled:
                    continue
            await run_once()
        except asyncio.CancelledError:
            return
        except Exception as e:
            _log.info(
                "[nightly-review] scheduler tick failed (non-fatal): %s: %s",
                type(e).__name__, e,
            )
            await asyncio.sleep(300.0)
