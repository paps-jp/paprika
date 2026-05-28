"""Session API routes: /sessions/* (RFC-001).

A Session is a long-lived reservation of a Lane that the client drives
action by action over HTTP. POST /sessions opens one (the hub picks a
free Lane on some Worker); the client then hits
/sessions/{id}/click, /sessions/{id}/fill, etc.; DELETE /sessions/{id}
releases the lane.

Behaviourally the same browser_ops primitives are used as the agent
loop -- this surface just exposes them over HTTP for clients that
want deterministic, script-driven control instead of LLM-driven.

The 4 core helpers (_require_session_infra, _get_session_or_404,
_route_to_page, _send_session_action) plus the public route functions
(create_session, close_session, session_agent,
session_save_cookies_to_host) live here but are re-exported from
app.py so cross-cutting callers (auto re-login at L3069-3159, lifespan
cleanup at L332/389, pre-fetch hook at L3784/3878) keep working
unchanged until those modules also migrate.

noVNC HTTP proxy + WS proxy + worker-lane preview stay in app.py
until #2B-F-novnc lifts them out -- they pull in a 300-line WebSocket
proxy and depend on routing helpers that haven't moved yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from server.hub._state import config, get_storage_dir, state

log = logging.getLogger(__name__)

# _asset_upload_url is defined in app.py at L1334 -- import is safe at
# module top because that definition runs WAY before app.py reaches the
# include_router stanza for this module.
from server.hub.app import _asset_upload_url
from server.hub.codegen import (
    CODEGEN_LLM_URL,
    CODEGEN_MODEL_NAME,
    generate_script,
)
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.routes.hosts import _require_hosts
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL


def _proxy_session_dict(d: dict) -> dict:
    """Lazy-bridge wrapper around app.py's _proxy_session_dict.

    The real implementation lives in the noVNC subsection of app.py
    which is defined AFTER the include_router for this module, so we
    can't eager-import it. Wrap with a function-level import so the
    lookup happens at call time when app.py is fully loaded.
    """
    from server.hub.app import _proxy_session_dict as _impl

    return _impl(d)


def _disconnect_session_novnc_clients(session_id: str) -> None:
    """Lazy bridge to app.py's helper. Used by close_session to kick
    noVNC viewers off a session being torn down. The implementation
    lives in app.py's noVNC subsection (defined after our
    include_router stanza), so wrap with a function-level import."""
    from server.hub.app import _disconnect_session_novnc_clients as _impl

    return _impl(session_id)


router = APIRouter(tags=["Sessions"])


# ----------------------------------------------------------------------------
# Helpers + routes (extracted verbatim from app.py)
# ----------------------------------------------------------------------------


def _require_session_infra():
    if state.sessions is None or state.registry is None:
        raise HTTPException(503, "session registry not ready")


def _get_session_or_404(session_id: str) -> SessionInfo:
    _require_session_infra()
    info = state.sessions.get(session_id)
    if info is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    return info


def _route_to_page(
    action: dict,
    body: dict | None = None,
    page_id: str | None = None,
) -> dict:
    """Phase 2b per-tab routing helper.

    Looks for a page_id in either the POST body or an explicit
    query-param (for GET endpoints) and overlays it onto the action.
    Without this helper every primitive landed on
    state.default_page_id; with it, callers can target a specific tab
    in a multi-tab session via the SDK's ``page._page_id`` field.

    Body wins over the explicit ``page_id`` arg so a caller can pass
    both forms without the action turning into a confusing mix.

    Returns the (possibly-mutated) action dict for chaining.
    """
    pid = None
    if body is not None:
        pid = body.get("page_id")
    if not pid:
        pid = page_id
    if pid:
        action["page_id"] = pid
    return action


async def _send_session_action(session_id: str, action: dict, *, timeout: float = 30.0):
    """Look up the bound worker, forward an action, return the result.

    Serialises actions per-session via the session's lock, so two
    concurrent HTTP requests for the same session can't interleave
    CDP traffic on the same tab.
    """
    info = _get_session_or_404(session_id)
    worker = state.registry.connections.get(info.worker_id)
    if worker is None:
        raise HTTPException(
            502,
            f"session worker '{info.worker_id}' is no longer connected",
        )
    async with info.lock:
        info.state = "running"
        info.current_action = action.get("kind") or "?"
        # Refresh last_active_at at the START too, not just at the end.
        # The reaper also skips state=="running" sessions, but updating
        # the timestamp here belt-and-suspenders the case where a
        # worker drops mid-action and the state field stays stale
        # (drop_by_worker normally cleans up, but if it races a
        # reconnect we keep an accurate timestamp anyway).
        info.last_active_at = datetime.utcnow()
        try:
            reply = await worker.session_action(
                session_id,
                action,
                timeout=timeout,
            )
        except TimeoutError:
            raise HTTPException(504, "session action timed out")
        except Exception as e:
            raise HTTPException(502, f"session action send failed: {e}")
        finally:
            info.current_action = None
            info.state = "idle"
    info.last_active_at = datetime.utcnow()
    if reply.status and reply.status.startswith("ERR:"):
        # Pass-through error string so the client sees the browser-level
        # message but with a 502 to signal "the action failed".
        return {
            "status": reply.status,
            "elapsed_ms": reply.elapsed_ms,
            "result": reply.result,
        }
    return {
        "status": reply.status,
        "elapsed_ms": reply.elapsed_ms,
        "result": reply.result,
    }


@router.post("/sessions")
async def create_session(body: dict) -> dict:
    """Open a new session against a free Lane on some Worker.

    Body (all optional)::

        {
          "initial_url": "https://example.com",
          "worker_id": "worker-tokyo-1",     // pin to a specific worker
          "lane_hint": 0,                     // pin to a specific lane
          "idle_ttl_s": 300,
          "absolute_ttl_s": 3600
        }

    Returns 201 with ``{session_id, worker_id, lane_idx, novnc_url, ...}``.
    Returns 503 when no active Worker has a free Lane.
    """
    _require_session_infra()
    body = body or {}
    initial_url = body.get("initial_url")
    pin_worker = body.get("worker_id")
    lane_hint = body.get("lane_hint")

    # SSRF guard: reject loopback / RFC1918 / link-local hosts when
    # the operator passes initial_url. Bypass via
    # PAPRIKA_ALLOW_PRIVATE_URLS=1 on the hub. Subsequent
    # page.goto() calls aren't validated here -- the deeper defense
    # is the worker's iptables egress firewall.
    if initial_url:
        from server.hub.url_safety import assert_public_url
        assert_public_url(initial_url)

    if pin_worker:
        worker = state.registry.connections.get(pin_worker)
        if worker is None:
            raise HTTPException(404, f"worker '{pin_worker}' not connected")
        if worker.status != "active":
            raise HTTPException(409, f"worker '{pin_worker}' is {worker.status}")
    else:
        worker = state.registry.pick_worker()
        if worker is None:
            raise HTTPException(503, "no active worker available")

    sid = new_session_id()
    parent_jid = body.get("parent_job_id") or body.get("job_id") or None
    info = SessionInfo(
        session_id=sid,
        worker_id=worker.worker_id,
        initial_url=initial_url,
        idle_ttl_s=int(body.get("idle_ttl_s") or 300),
        absolute_ttl_s=int(body.get("absolute_ttl_s") or 3600),
        # Optional ownership: codegen-loop's paprika-runner tags every
        # session it opens with the parent job_id so the admin UI can
        # group "Live: job XXXX" pages with the right session noVNCs.
        job_id=parent_jid,
    )
    state.sessions.add(info)
    # When this session is owned by a parent job, point page.capture()
    # uploads at that job's existing /assets endpoint so its inline
    # gallery (and /ui/assets/{id}) actually shows the captures.
    # Without this, session captures stay in the worker's tempdir and
    # the parent job's gallery looks empty for codegen-loop runs.
    session_upload_base: str | None = None
    if parent_jid:
        # Use the worker's dialled base URL -- same logic as fetch jobs.
        # The worker reaches us via the host it WS-dialled in on, so
        # that URL works without needing PUBLIC_BASE_URL set globally.
        # Fall back to the configured public base if the worker hasn't
        # recorded a dial-in URL yet.
        base = worker.public_base_url or config.public_base_url
        if base:
            try:
                session_upload_base = _asset_upload_url(base, parent_jid)
                # Make sure the parent's assets dir exists on disk.
                (get_storage_dir() / parent_jid / "assets").mkdir(
                    parents=True,
                    exist_ok=True,
                )
            except Exception:
                session_upload_base = None

    # Per-host cookie auto-injection. If the operator has registered
    # cookies for the host of ``initial_url`` (e.g. example.com), pull
    # them out of the registry, sanitise them into CDP-shape, and hand
    # them to the worker so the very first request carries the session.
    # Skipping when there's no host or no record means non-cookie hosts
    # keep their zero-config behaviour.
    auto_cookies: list[dict] | None = None
    auto_host: str | None = None
    # Same host lookup also surfaces popup_policy -- "follow" tells the
    # worker's tab-killer to redirect the main tab to popup URLs even
    # across netlocs (sites like video-site.example whose video pages live on
    # an embed.* subdomain).
    auto_popup_policy: str = "kill"
    if initial_url and state.hosts is not None:
        try:
            from urllib.parse import urlparse as _urlparse

            host = _urlparse(initial_url).hostname or ""
            host = _normalise_host(host)
            if host:
                rec = state.hosts.get(host)
                if rec:
                    if rec.cookies:
                        auto_cookies = cookies_for_cdp(rec.cookies)
                        auto_host = host
                    auto_popup_policy = rec.popup_policy or "kill"
        except Exception:
            auto_cookies = None
            auto_host = None

    # Hub-managed min-size filter for the session's passive capture.
    # Pulled from Settings so the operator's "Asset capture" knob
    # also covers Code / LLM sessions opened by the runner.
    min_asset = 0
    if state.settings is not None:
        try:
            min_asset = int(state.settings.get("min_asset_size_bytes", 0) or 0)
        except Exception:
            min_asset = 0

    # Optional operator Chrome profile -- same shape as
    # JobOptions.use_profile but exposed on /sessions directly so SDK
    # callers (cli.session(use_profile=...)) get the same plumbing.
    # Falls back to the operator-set default profile when the call
    # doesn't specify one.
    profile_url: str | None = None
    profile_etag: str | None = None
    profile_name = (body.get("use_profile") or "").strip() or None
    _explicit_profile = profile_name is not None
    if profile_name is None and state.profiles is not None:
        profile_name = state.profiles.get_default()
    if profile_name:
        if state.profiles is None or not state.profiles.exists(profile_name):
            if _explicit_profile:
                state.sessions.remove(sid)
                raise HTTPException(
                    400,
                    f"use_profile: profile '{profile_name}' not found. "
                    "Upload it first via POST /profiles/{name} "
                    "(paprika-client upload-profile).",
                )
            # Stale default -- silently skip.
            profile_name = None
    if profile_name:
        # Use the worker-dialled base URL when available so a worker
        # behind NAT / on a different subnet still gets a reachable
        # URL. Falls back to PUBLIC_BASE_URL just like asset uploads.
        base = worker.public_base_url or config.public_base_url
        if base:
            profile_url = f"{base.rstrip('/')}/profiles/{profile_name}"
        profile_etag = state.profiles.etag(profile_name)

    try:
        ack = await worker.start_session(
            sid,
            initial_url=initial_url,
            lane_hint=lane_hint if isinstance(lane_hint, int) else None,
            asset_upload_base=session_upload_base,
            cookies=auto_cookies,
            min_asset_size_bytes=min_asset,
            popup_policy=auto_popup_policy,
            profile_url=profile_url,
            profile_name=profile_name,
            profile_etag=profile_etag,
            download_video=bool(body.get("download_video", False)),
            timeout=60.0,
        )
    except TimeoutError:
        state.sessions.remove(sid)

        # Tell the worker to release whatever it was building so the
        # lane comes back to the pool. session_start may still be in
        # flight worker-side -- our HubSessionEnd message either:
        #   (a) arrives after _sessions[sid] is populated -> normal
        #       teardown path picks it up + releases the lane.
        #   (b) arrives before _sessions[sid] exists -> worker parks
        #       sid in _aborted_sessions, and the in-flight start
        #       self-tears-down at its abort checkpoint right before
        #       sending the ack.
        # Fire-and-forget; we already failed the caller with 504.
        async def _cleanup_after_timeout():
            try:
                await worker.end_session(sid, timeout=20.0)
            except Exception:
                log.warning(
                    "session %s cleanup-on-timeout failed",
                    sid,
                    exc_info=True,
                )

        asyncio.create_task(_cleanup_after_timeout())
        raise HTTPException(504, "session start timed out")
    except Exception as e:
        state.sessions.remove(sid)
        raise HTTPException(502, f"session start send failed: {e}")
    if ack.error:
        state.sessions.remove(sid)
        raise HTTPException(502, f"worker rejected session: {ack.error}")
    info.lane_idx = ack.lane_idx
    info.novnc_url = ack.novnc_url

    # Cookies actually made it onto the wire -- bump last_used_at so the
    # admin UI shows "used 2 minutes ago" instead of "never". We only
    # touch the record after a clean ack to avoid bumping it for sessions
    # the worker rejected.
    if auto_host and auto_cookies and state.hosts is not None:
        try:
            state.hosts.touch_used(auto_host)
        except Exception:
            pass

    log.info(
        "session %s open on %s lane #%s%s",
        sid,
        worker.worker_id,
        ack.lane_idx,
        f" (+{len(auto_cookies)} cookies for {auto_host})" if auto_cookies else "",
    )
    # POST /sessions response: rewrite novnc_url to the session-rooted
    # hub proxy URL (same treatment as GET /sessions / /sessions/{sid}).
    # _proxy_session_dict mutates and returns the dict.
    d = _proxy_session_dict(info.to_json())
    d["novnc_url_autoconnect"] = _novnc_autoconnect(d.get("novnc_url"))
    return d


def _novnc_autoconnect(url: str | None) -> str | None:
    if not url:
        return None
    if "autoconnect" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}autoconnect=1&resize=scale&reconnect=1"


async def _auto_save_session_cookies(info) -> dict | None:
    """Pre-close hook: dump cookies from the session's tab and upsert
    them into the host registry under the host of ``info.initial_url``.
    Used by ``close_session`` so codegen-loop / rerun / direct
    ``cli.session`` opens get the same auto-save behaviour Fetch jobs
    already have. Best-effort -- never blocks the close path.

    Returns the registry response on success, ``None`` on skip or
    failure (and logs the reason).

    Skip conditions:
      * no ``initial_url`` to derive a host from
      * the session is fetch-owned (its on_browser_closing callback
        already does the save on the worker side -- avoid duplicates)
      * the host registry isn't initialised
    """
    if info is None or state.hosts is None:
        return None
    if (info.state or "") == "fetch_running":
        # Fetch session: the worker has its own dump+save flow already.
        return None
    initial = info.initial_url or ""
    if not initial:
        return None
    try:
        from urllib.parse import urlparse as _urlparse

        host = _urlparse(initial).hostname or ""
    except Exception:
        host = ""
    host = _normalise_host(host)
    if not host:
        return None
    # Get the live cookie jar from the worker via the existing
    # session_action. ``_send_session_action`` would raise on
    # transport / worker errors; wrap so we never block the close.
    try:
        out = await _send_session_action(
            info.session_id,
            {"kind": "get_cookies"},
            timeout=15.0,
        )
        result = out.get("result")
        if not isinstance(result, dict):
            log.warning(
                "session %s auto-save: worker returned non-dict (%r); skipping",
                info.session_id,
                out,
            )
            return None
        all_browser = result.get("cookies") or []
    except Exception:
        log.warning(
            "session %s auto-save: get_cookies failed; skipping",
            info.session_id,
            exc_info=True,
        )
        return None
    filtered = _filter_cookies_by_host(all_browser, host)
    # Look up existing record so we can honour "keep on no-match"
    # and preserve operator-edited notes. Apply the same logic as
    # the fetch worker callback: replace / keep-existing / marker.
    existing = state.hosts.get(host)
    if filtered:
        cookies_to_save = cookies_for_cdp(filtered)
        kind = (
            f"replaced ({len(filtered)} cookie(s))"
            if existing
            else f"created ({len(filtered)} cookie(s))"
        )
    elif existing and existing.cookies:
        cookies_to_save = list(existing.cookies)
        kind = (
            f"refreshed timestamp only "
            f"(kept {len(existing.cookies)} existing; "
            f"none matched in this session)"
        )
    else:
        cookies_to_save = []
        kind = "marker created (0 cookie(s) matched this host)"
    notes = (existing.notes if existing else None) or (
        f"auto-saved by session {info.session_id}"
        + (f" (job {info.job_id})" if info.job_id else "")
    )
    try:
        rec = state.hosts.upsert(
            host=host,
            cookies=cookies_to_save,
            notes=notes,
        )
    except Exception:
        log.warning(
            "session %s auto-save: upsert failed", info.session_id, exc_info=True
        )
        return None
    log.info(
        "session %s auto-save: PUT /hosts/%s -- %s", info.session_id, host, kind
    )
    return {
        "host": rec.host,
        "saved_count": len(cookies_to_save),
        "total_in_browser": len(all_browser),
        "kind": kind,
    }


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str) -> dict:
    """Release the Lane bound to ``session_id``.

    Before sending HubSessionEnd to the worker, the hub dumps the
    session's current cookie jar (filtered to cookies matching the
    host of ``initial_url``) and upserts it into the host registry.
    Mirrors Fetch's post-run auto-save: every host the operator
    explicitly opens a session against ends up in the Hosts tab,
    Cookie state preserved across script restarts.
    """
    _require_session_infra()
    # Mark as closing BEFORE removing from registry so list_sessions
    # mid-close shows the transition state.
    pre = state.sessions.get(session_id)
    if pre is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    pre.state = "closing"

    # Auto-save BEFORE we remove the SessionInfo from the registry --
    # _send_session_action looks the session up there to find the
    # owning worker. Best-effort; failures don't block the close.
    auto_save_result = None
    try:
        auto_save_result = await _auto_save_session_cookies(pre)
    except Exception:
        log.warning(
            "session %s auto-save crashed", session_id, exc_info=True
        )

    info = state.sessions.remove(session_id)
    # NOTE on noVNC disconnect ordering: we used to force-disconnect
    # noVNC bridges HERE (right after removing from the session
    # registry) so the operator's browser tab got an immediate
    # "disconnected" close frame instead of a frozen / phantom
    # viewer. That made sense when end_session returned in seconds.
    # With the worker-side video drain (passive m3u8 / mp4 listener
    # can be mid-download of a 1+ GB iframe video) the ack now
    # legitimately takes many MINUTES, and Chrome stays alive on
    # the lane for the entire drain window -- so disconnecting the
    # viewer up-front would hide the in-progress download from the
    # operator who specifically opened the noVNC to watch it.
    # The disconnect is deferred to AFTER end_session ack arrives
    # (see the post-ack block below).
    # Cascade: if this was a detached / keepalive session and its
    # parent job is still in "running" state, transition the job to
    # completed too. Otherwise the admin UI would show a phantom
    # "running" job whose underlying session is gone (= no noVNC, no
    # refresh, no nothing) until the operator manually cancelled it.
    # Also release worker.in_flight here -- for keepalive Fetch jobs
    # we deliberately SKIPPED that release in WorkerJobComplete so
    # the scheduler wouldn't over-dispatch while the lane was held
    # by the live session; closing the session is the right moment
    # to decrement.
    # Best-effort -- a save failure here MUST NOT block the lane
    # release / end_session below.
    try:
        if info is not None and info.detached and info.job_id and state.store is not None:
            job = await state.store.get_job_info(info.job_id)
            if job is not None and job.status == JobStatus.running:
                job.status = JobStatus.completed
                job.completed_at = datetime.utcnow()
                # phase: idle_timeout (= reaper-triggered) vs
                # keepalive_closed (= operator-triggered DELETE).
                # We can't easily tell from inside close_session,
                # so just use a neutral marker -- the reaper logs
                # its reason separately if interesting.
                job.progress.phase = "keepalive_closed"
                await state.store.save_job_info(job)
                # Release in_flight that WorkerJobComplete skipped.
                if state.registry is not None and job.worker_id:
                    try:
                        state.registry.release(job.worker_id, job.job_id)
                    except Exception:
                        pass
                try:
                    await state.store.publish_log(job.job_id, DONE_SENTINEL)
                except Exception:
                    pass
    except Exception:
        log.warning(
            "session %s parent-job cascade failed",
            session_id,
            exc_info=True,
        )

    worker = state.registry.connections.get(info.worker_id)
    if worker is None:
        return {
            "session_id": session_id,
            "closed": True,
            "warn": "worker was already disconnected",
            "cookie_save": auto_save_result,
        }
    # end_session timeout has to cover the worker-side video drain --
    # session_assets's passive m3u8 / mp4 listener may have spawned an
    # httpx stream for a 1+ GB iframe video, and _teardown_session_state
    # awaits drain BEFORE sending the ack. Capping the hub-side wait at
    # 20s used to return prematurely, which let the codegen-loop mark
    # the job "completed" and publish DONE_SENTINEL while the worker
    # was still mid-download -- the Live panel WS closed and the user
    # saw 'job ended' even though the file was 6% of the way to the
    # disk. Match the hub-side wait to PAPRIKA_VIDEO_DRAIN_HARD_S
    # (default 30 min) so the SDK's ``async with cli.session()`` block,
    # the runner sandbox script, the codegen-loop's post-script
    # judge/conventions phase, and finally DONE_SENTINEL all happen
    # AFTER the worker confirms drain completion -- which keeps the
    # Live panel open for the full download + upload window.
    import os as _os
    drain_hard = float(_os.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "1800.0"))
    # Add a small headroom so the worker's outer wait_for (hard+30s)
    # gets a chance to surface its own timeout cleanly before we
    # decide the lane is stuck.
    end_session_timeout = drain_hard + 60.0
    try:
        ack = await worker.end_session(session_id, timeout=end_session_timeout)
    except TimeoutError:
        # Even on timeout, kick the noVNC bridges -- they're connected
        # to a worker that may not respond cleanly; better a deliberate
        # disconnect now than a frozen viewer.
        try:
            await _disconnect_session_novnc_clients(session_id)
        except Exception:
            pass
        return {
            "session_id": session_id,
            "closed": True,
            "warn": (
                f"end_session timed out after {end_session_timeout:.0f}s "
                f"(lane may stay reserved on worker)"
            ),
            "cookie_save": auto_save_result,
        }
    except Exception as e:
        try:
            await _disconnect_session_novnc_clients(session_id)
        except Exception:
            pass
        return {
            "session_id": session_id,
            "closed": True,
            "warn": str(e),
            "cookie_save": auto_save_result,
        }
    # Drain done, worker acknowledged. NOW it's safe to tear down the
    # noVNC bridge -- the Chrome lane has finished its background work
    # (video downloads merged, network log dumped, browser reset) so
    # the viewer wouldn't be showing anything useful anyway. Without
    # this final disconnect the bridge would survive until websockify
    # noticed VNC server going away (= up to 30s of phantom frame).
    try:
        await _disconnect_session_novnc_clients(session_id)
    except Exception:
        pass
    return {
        "session_id": session_id,
        "closed": True,
        "error": ack.error if ack else None,
        "cookie_save": auto_save_result,
    }


@router.get("/sessions")
async def list_sessions() -> dict:
    """List active sessions on this hub. ``novnc_url`` is rewritten to
    the session-rooted hub proxy URL so admin UI tiles open via the
    hub (worker LAN IPs stay private)."""
    _require_session_infra()
    items = [_proxy_session_dict(s.to_json()) for s in state.sessions.all()]
    return {"count": len(items), "sessions": items}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Single-session details with ``novnc_url`` rewritten to the
    session-rooted hub proxy URL (see /sessions)."""
    info = _get_session_or_404(session_id)
    d = _proxy_session_dict(info.to_json())
    d["novnc_url_autoconnect"] = _novnc_autoconnect(d.get("novnc_url"))
    return d


# --- Inspection (read-only) -------------------------------------------------


@router.get("/sessions/{session_id}/state")
async def session_state(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    action = _route_to_page({"kind": "state"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    return out


@router.get("/sessions/{session_id}/outline")
async def session_outline(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    action = _route_to_page({"kind": "outline"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    return out


# ----------------------------------------------------------------------------
# Tab management (multi-tab Phase 2)
# Each session can hold N tabs ("pages"). One is the default (where
# un-keyed primitives land). These endpoints let scripts enumerate,
# spawn, close, and switch tabs without touching internals.
# ----------------------------------------------------------------------------


@router.get("/sessions/{session_id}/pages")
async def session_pages(session_id: str) -> dict:
    """List every tab in this session.

    Response shape::

        {
          "status": "OK",
          "count": 3,
          "default_page_id": "p_default",
          "pages": [
            {"page_id": "p_default", "url": "...", "title": "...", "is_default": true},
            {"page_id": "p_a1b2c3d4", "url": "...", "title": "...", "is_default": false},
            ...
          ]
        }

    Read-only, allowed even on fetch-owned sessions.
    """
    out = await _send_session_action(session_id, {"kind": "pages"})
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {"count": 0, "default_page_id": None, "pages": []}
    return {
        "status": out.get("status", "OK"),
        "count": int(result.get("count") or 0),
        "default_page_id": result.get("default_page_id"),
        "pages": result.get("pages") or [],
    }


@router.post("/sessions/{session_id}/pages")
async def session_new_page(session_id: str, body: dict) -> dict:
    """Open a new tab in this session.

    body::

        {
          "url":    "https://...",   // optional, defaults to about:blank
          "switch": true              // if true, also flip default_page_id
        }

    Response::

        {"status": "OK", "page_id": "p_a1b2c3d4", "url": "...", "is_default": false}

    Write action; rejected on fetch-owned sessions.
    """
    body = body or {}
    url = (body.get("url") or "about:blank").strip()
    switch = bool(body.get("switch", False))
    out = await _send_session_action(
        session_id,
        {"kind": "new_page", "url": url, "switch": switch},
        timeout=30.0,
    )
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "status": out.get("status", "OK"),
        "page_id": result.get("page_id"),
        "url": result.get("url"),
        "is_default": bool(result.get("is_default")),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.delete("/sessions/{session_id}/pages/{page_id}")
async def session_close_page(session_id: str, page_id: str) -> dict:
    """Close one tab. Cannot close the LAST remaining tab in the
    session (end the session with DELETE /sessions/{sid} instead).

    Closing the current default_page_id auto-rotates the default to
    the most-recently-added remaining tab; the new default is in the
    response.
    """
    out = await _send_session_action(
        session_id,
        {"kind": "close_page", "page_id": page_id},
        timeout=15.0,
    )
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "status": out.get("status", "OK"),
        "closed_page_id": result.get("closed_page_id"),
        "default_page_id": result.get("default_page_id"),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/resize")
async def session_resize_window(session_id: str, body: dict) -> dict:
    """Resize the Chrome OS window for this session.

    Body::

        {"width": 1280, "height": 720, "page_id": "p_default"}

    Used by the admin UI's "iframe サイズに合わせる" button to make
    the browser size match the noVNC viewport so the operator sees
    Chrome at the expected pixel ratio. The X display itself stays
    its native size; only Chrome's window is resized inside it.

    page_id is optional -- defaults to the session's foreground tab.
    Both width / height must be in [200, 4096].
    """
    body = body or {}
    try:
        w = int(body.get("width") or 0)
        h = int(body.get("height") or 0)
    except Exception:
        raise HTTPException(400, "width / height must be ints")
    if w < 200 or h < 200 or w > 4096 or h > 4096:
        raise HTTPException(400, "width / height must be in [200, 4096]")
    action = _route_to_page(
        {"kind": "resize_window", "width": w, "height": h},
        body,
    )
    return await _send_session_action(session_id, action, timeout=15.0)


@router.post("/sessions/{session_id}/pages/{page_id}/switch")
async def session_switch_page(session_id: str, page_id: str) -> dict:
    """Make the given ``page_id`` the session's default tab so
    subsequent un-keyed primitives (click / fill / outline / ...)
    target it. Also brings the tab to the visual front for noVNC
    viewers."""
    out = await _send_session_action(
        session_id,
        {"kind": "switch_page", "page_id": page_id},
        timeout=10.0,
    )
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "status": out.get("status", "OK"),
        "default_page_id": result.get("default_page_id"),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/keepalive")
async def session_keepalive(
    session_id: str,
    body: dict | None = None,
) -> dict:
    """Extend a session's TTLs so it survives long stretches of pure
    noVNC interaction (which doesn't touch the session-action timer).

    Body (all optional)::

        {"idle_ttl_s": 14400, "absolute_ttl_s": 86400}

    Both fields are clamped to [60, 7*86400] -- a week's worth of
    idle is the upper limit so a forgotten session can't pin a lane
    forever. Omitting a field leaves that TTL untouched.

    Used by ``Session.detach()`` in the SDK to hand a live browser
    over to the operator (noVNC + admin UI) with enough TTL headroom
    for actual human interaction. Returns the SessionInfo as JSON --
    same shape as ``GET /sessions/{sid}``.
    """
    _require_session_infra()
    sinfo = state.sessions.get(session_id)
    if sinfo is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    body = body or {}
    _MIN_TTL = 60
    _MAX_TTL = 7 * 86400  # 1 week
    if "idle_ttl_s" in body and body["idle_ttl_s"] is not None:
        try:
            v = int(body["idle_ttl_s"])
        except Exception:
            raise HTTPException(400, "idle_ttl_s must be an int")
        sinfo.idle_ttl_s = max(_MIN_TTL, min(_MAX_TTL, v))
    if "absolute_ttl_s" in body and body["absolute_ttl_s"] is not None:
        try:
            v = int(body["absolute_ttl_s"])
        except Exception:
            raise HTTPException(400, "absolute_ttl_s must be an int")
        sinfo.absolute_ttl_s = max(_MIN_TTL, min(_MAX_TTL, v))
    # Touch the timer too so the new idle window counts from now,
    # not from the last session_action (which may have been minutes
    # ago and immediately consume part of the bumped window).
    sinfo.last_active_at = datetime.utcnow()
    # Mark the session as operator-managed. Without this flag,
    # _cleanup_orphan_sessions (which fires when the parent codegen-
    # loop / rerun script exits) would yank the session back even
    # though the script explicitly handed it off via detach().
    # The only callers of /keepalive today are detach()-equivalent
    # SDK paths, so flipping it unconditionally is correct; any
    # future "just bump TTL without detaching" need would warrant a
    # ``detach=False`` body field.
    sinfo.detached = True
    out = sinfo.to_json()
    out["job_id"] = sinfo.job_id
    return out


@router.post("/sessions/{session_id}/exists")
async def session_exists(session_id: str, body: dict) -> dict:
    """CSS セレクタの一致要素が現在のページに存在するかを返す。

    body: ``{"selector": "..."}`` (str, required)
    response: ``{"status": "OK", "result": true|false, "elapsed_ms": int}``

    LLM を介さない決定的なチェック。Macro UI の ``If (CSS)`` 行と
    ``page.exists()`` SDK メソッドの裏側で叩かれる。
    """
    body = body or {}
    selector = (body.get("selector") or "").strip()
    if not selector:
        raise HTTPException(400, "missing 'selector'")
    action = _route_to_page(
        {"kind": "exists", "selector": selector},
        body,
    )
    out = await _send_session_action(session_id, action, timeout=15.0)
    return {
        "status": out.get("status", "OK"),
        "result": bool(out.get("result")),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/evaluate")
async def session_evaluate(session_id: str, body: dict) -> dict:
    """Evaluate a JS expression in the session tab's page context and
    return ``{status, result, elapsed_ms}`` where ``result`` is the
    expression's value (must be JSON-serialisable).

    Body: ``{"expression": "...", "await_promise": false, "page_id": "..."}``

    This is the low-level primitive the SDK builds Locator getters
    (``text_content`` / ``get_attribute`` / …), ``wait_for_selector``,
    and the JS-dispatched input helpers (``hover`` / ``select_option`` /
    …) on top of. LAN-trusted: arbitrary JS runs in the browser, same
    trust model as cookie injection / profile upload.
    """
    body = body or {}
    expr = (body.get("expression") or "").strip()
    if not expr:
        raise HTTPException(400, "missing 'expression'")
    action = _route_to_page(
        {
            "kind": "evaluate",
            "expression": expr,
            "await_promise": bool(body.get("await_promise")),
        },
        body,
    )
    return await _send_session_action(session_id, action, timeout=30.0)


@router.post("/sessions/{session_id}/set_input_files")
async def session_set_input_files(session_id: str, body: dict) -> dict:
    """Set the files on an ``<input type=file>`` matched by ``selector``.

    Body::

        {
          "selector": "input[type=file]",
          "files": [{"name": "photo.jpg", "content_b64": "..."}],
          "page_id": "..."        // optional, multi-tab
        }

    The worker materialises the base64 payloads in a tempdir and points
    the input at them via CDP ``DOM.setFileInputFiles`` (JS can't set a
    file input). Returns ``{status, result:{files, count}, elapsed_ms}``.
    """
    body = body or {}
    selector = (body.get("selector") or "").strip()
    if not selector:
        raise HTTPException(400, "missing 'selector'")
    files = body.get("files")
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "missing 'files' (non-empty list)")
    action = _route_to_page(
        {"kind": "set_input_files", "selector": selector, "files": files},
        body,
    )
    # File payloads can be sizeable; give the worker a generous window.
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/ask")
async def session_ask(session_id: str, body: dict) -> dict:
    """LLM に yes/no 質問を投げて bool を返す。

    body:
      ``{"question": "...", "engine": "auto"}``

    response: ``{"status": "OK", "result": true|false, "elapsed_ms": int}``

    ``engine`` で AI Engines 管理画面に登録した chat 系エンジンの
    slug を指定 (例: ``"chatgpt51"``, ``"qwen-chat"``, ``"claude"``)。
    省略 / ``"auto"`` は promoted な chat エンジンを採用 (operator
    が AI Engines タブで指定したデフォルト)。worker は hub の
    ``/engines/.../resolve`` を叩いて endpoint + model + API key を
    解決してから LLM を呼ぶので、worker のローカル env に API キー
    を撒く必要は無い。

    Worker 側で現在の outline + URL を prompt に入れて LLM に渡し、
    厳密な "yes" / "no" 1 ワード回答を引き出す。パース不能なら False
    (= 無作為に True に倒れない安全側) に倒す。Macro UI の
    ``If (Agent)`` 行と ``page.ask()`` SDK メソッドが裏で叩く。
    """
    body = body or {}
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "missing 'question'")
    engine = (body.get("engine") or "auto").lower()
    action = _route_to_page(
        {"kind": "ask", "question": question, "engine": engine},
        body,
    )
    out = await _send_session_action(session_id, action, timeout=45.0)
    return {
        "status": out.get("status", "OK"),
        "result": bool(out.get("result")),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/extract")
async def session_extract(session_id: str, body: dict) -> dict:
    """LLM-driven structured extraction (paprika-native).

    The SDK builds a JSON Schema from a Pydantic model and posts it
    here along with the natural-language instruction. The worker
    feeds the current page outline + the JSON Schema + the instruction
    to a chat engine, parses the JSON response, and returns it for
    SDK-side validation.

    body::

        {
          "instruction":  "<what to extract>",
          "schema_json":  "<JSON Schema string built by the SDK>",
          "engine":       "auto" | "<engine slug>",
          "context":      "outline" | "html",
          "max_chars":    12000,
          "variables":    {"name": "<value>", ...}  # optional
        }

    response::

        {"status": "OK", "result": <parsed JSON>, "elapsed_ms": int}

    The Pydantic validation step happens on the SDK side so the
    user's full type hint is honoured. The hub / worker layer here
    deliberately keeps the response shape as plain JSON so any
    future client (PHP, CLI, curl) can use the same endpoint.
    """
    body = body or {}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "missing 'instruction'")
    schema_json = (body.get("schema_json") or "").strip()
    engine = (body.get("engine") or "auto").lower()
    context = (body.get("context") or "outline").lower()
    if context not in ("outline", "html"):
        context = "outline"
    max_chars = int(body.get("max_chars") or 12000)
    action = _route_to_page(
        {
            "kind": "extract",
            "instruction": instruction,
            "schema_json": schema_json,
            "engine": engine,
            "context": context,
            "max_chars": max_chars,
            "variables": dict(body.get("variables") or {}),
        },
        body,
    )
    out = await _send_session_action(session_id, action, timeout=90.0)
    return {
        "status": out.get("status", "OK"),
        "result": out.get("result"),
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.post("/sessions/{session_id}/observe")
async def session_observe(session_id: str, body: dict) -> dict:
    """LLM-driven candidate enumeration (paprika-native).

    Ask the LLM to look at the outline and propose up to
    ``max_results`` elements matching the operator's intent. NOTHING
    is executed; the SDK reshapes the JSON into :class:`Candidate`
    objects that the script can inspect, then explicitly pass to
    ``page.click`` / ``page.fill``.

    body::

        {
          "intent":       "<natural-language description>",
          "engine":       "auto" | "<engine slug>",
          "max_results":  5,
          "variables":    {"name": "<value>", ...}  # optional
        }

    response::

        {
          "status": "OK",
          "result": [
            {"selector": "[data-paprika-id=\\"3\\"]",
             "description": "...", "method": "click",
             "arguments": null, "paprika_id": 3, "confidence": 0.92},
            ...
          ],
          "elapsed_ms": int
        }
    """
    body = body or {}
    intent = (body.get("intent") or "").strip()
    if not intent:
        raise HTTPException(400, "missing 'intent'")
    engine = (body.get("engine") or "auto").lower()
    max_results = int(body.get("max_results") or 5)
    action = _route_to_page(
        {
            "kind": "observe",
            "intent": intent,
            "engine": engine,
            "max_results": max_results,
            "variables": dict(body.get("variables") or {}),
        },
        body,
    )
    out = await _send_session_action(session_id, action, timeout=60.0)
    return {
        "status": out.get("status", "OK"),
        "result": out.get("result") or [],
        "elapsed_ms": int(out.get("elapsed_ms") or 0),
    }


@router.get("/sessions/{session_id}/links")
async def session_links(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    """Return all <a href> on the current page, resolved to absolute URLs.

    Used by:
      * the Live panel "Links" tab (polled while a job is running, so
        the operator can watch the page's outbound URLs as they
        change);
      * ``page.links()`` in paprika-client, so a script can crawl by
        URL list instead of by CSS selector;
      * future codegen-loop scripts that want "for each link on the
        page, do X".

    Result shape::

        {
          "session_id": "ses_...",
          "current_url": "https://example.com/foo",
          "count": 42,
          "links": [
            {"href": "https://...", "text": "anchor text", "target": "", "rel": ""},
            ...
          ]
        }

    Skipped protocols: javascript: / mailto: / tel: / blob: / data: /
    about: -- they're not navigatable in the page-action sense and
    just clutter the result. Deduped by href.
    """
    action = _route_to_page({"kind": "links"}, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "session_id": session_id,
        "current_url": result.get("current_url") or "",
        "count": int(result.get("count") or 0),
        "links": result.get("links") or [],
    }


@router.get("/sessions/{session_id}/last_response")
async def session_last_response(session_id: str) -> dict:
    """Return the most recent main-document HTTP response observed
    on this session.

    A passive Network CDP listener (installed at session_start) keeps
    the worker's ``state.last_response`` in sync with whatever the
    last top-level navigation returned -- whether that was
    ``page.goto`` / ``back`` / ``forward`` / ``reload`` /
    ``history_first`` OR a click that incidentally navigated
    (a link, a form submit, an in-page ``location.href = ...``).

    Used by ``page.last_response()`` in the SDK -- the click-induced
    nav case in particular has no per-call capture (the click action
    doesn't know whether it will navigate), so this stateful endpoint
    is the only way to read the response status after such a click.

    Returns ``{"response": {...} | None}``. The inner dict has the
    same shape as ``page.goto()``'s ``result["response"]``::

        {"url", "status", "status_text", "ok", "headers", "mime"}

    ``response`` is ``None`` when no document response has been
    observed yet on this session (fresh ``initial_url=about:blank``
    sessions, or sessions opened just moments before the call).
    """
    action = {"kind": "last_response"}
    out = await _send_session_action(session_id, action, timeout=10.0)
    return {"session_id": session_id, "response": out.get("result")}


@router.get("/sessions/{session_id}/network")
async def session_network(
    session_id: str,
    since: float = 0,
) -> dict:
    """Return media network traffic observed in this session.

    Used by the Live panel "Network" tab to show image/audio/video
    responses the browser loaded. The operator can inspect each item
    and cherry-pick ones to add to the job's asset gallery.

    ``since`` (UNIX timestamp, float) enables incremental polling:
    only entries newer than ``since`` are returned. Pass 0 to get all.

    Result shape::

        {
          "session_id": "ses_...",
          "count": 42,           // total entries on the worker
          "entries": [
            {"url": "https://...", "mime": "image/jpeg",
             "size": 123456, "saved": true,
             "document_url": "https://page.example.com",
             "timestamp": 1716300000.123},
            ...
          ]
        }
    """
    action = {"kind": "network", "since": since}
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "session_id": session_id,
        "count": int(result.get("count") or 0),
        "entries": result.get("entries") or [],
    }


@router.get("/sessions/{session_id}/visited")
async def session_visited(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    info = _get_session_or_404(session_id)
    # Pull the canonical list from the worker -- the hub's
    # SessionInfo.visited_urls is left empty until we wire up periodic
    # snapshots, but the worker has the authoritative ordered set.
    action = _route_to_page({"kind": "visited"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    urls = out.get("result") or []
    return {
        "session_id": session_id,
        "count": len(urls),
        "visited_urls": urls,
    }


@router.get("/sessions/{session_id}/screenshot")
async def session_screenshot(
    session_id: str,
    page_id: str | None = None,
    label: str | None = None,
):
    # ``label`` (optional): when set, the worker ALSO publishes this
    # frame to the parent job's gallery as screenshot-*.png (visible in
    # the Live tab's Screenshot sub-tab). Requires the session to be
    # bound to a parent job. The PNG bytes are still returned to the
    # caller regardless, so page.screenshot(path=...) keeps working.
    act: dict = {"kind": "screenshot"}
    if label:
        act["label"] = label
    action = _route_to_page(act, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=20.0)
    b64 = out.get("result") or ""
    if not isinstance(b64, str) or not b64:
        raise HTTPException(502, "worker returned no screenshot")
    import base64 as _b64

    try:
        png = _b64.b64decode(b64)
    except Exception:
        raise HTTPException(502, "worker returned invalid base64")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


def _filter_cookies_by_host(cookies: list, host: str) -> list:
    """Keep only cookies that would apply to ``host`` (host-only match
    or domain-match like ``.example.com`` / ``example.com``). Without
    this filter, a browser that's been used across many sites returns
    every cookie in its jar -- mostly third-party tracker noise -- and
    the operator has to manually prune them before saving."""
    if not host:
        return list(cookies or [])
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    out: list = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        dom = (c.get("domain") or "").lower().lstrip(".")
        if not dom:
            continue
        if dom.startswith("www."):
            dom = dom[4:]
        # Match the cookie domain to the requested host. A cookie set
        # for ``.foo.com`` applies to ``foo.com``, ``a.foo.com`` etc.
        # We treat the registry host as the eTLD+1 in spirit -- exact
        # match or suffix match.
        if dom == host or dom.endswith("." + host) or host.endswith("." + dom):
            out.append(c)
    return out


@router.get("/sessions/{session_id}/cookies")
async def session_cookies(
    session_id: str,
    host: str | None = None,
    all_cookies: bool = False,
    page_id: str | None = None,
) -> dict:
    """Dump the cookies the browser currently has for this session.

    Used by the admin UI's "save cookies → host" button: operator logs
    into a site once in the noVNC viewer, then this endpoint snapshots
    the cookie jar so the Host modal can pre-fill them. The returned
    cookies are CDP-shaped (name/value/domain/path/expires/secure/
    httpOnly/sameSite). ``current_url`` lets the UI infer which host
    to register them under.

    By default, results are filtered to cookies that match the host of
    ``current_url`` (or the explicit ``?host=`` query, if provided).
    Pass ``?all_cookies=true`` to bypass the filter and return every
    cookie in the browser jar (useful when third-party / cross-site
    cookies are what you want, e.g. an SSO provider hosted on a
    different domain).
    """
    action = _route_to_page({"kind": "get_cookies"}, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result")
    if not isinstance(result, dict):
        # Worker returned a status string (most likely "ERR: ...").
        raise HTTPException(502, f"worker reply: {out}")
    all_cookies_list = result.get("cookies") or []
    current_url = result.get("current_url") or ""
    if all_cookies:
        filtered = list(all_cookies_list)
        used_host = None
    else:
        used_host = (host or "").strip()
        if not used_host:
            try:
                from urllib.parse import urlparse as _urlparse

                used_host = _urlparse(current_url).hostname or ""
            except Exception:
                used_host = ""
        filtered = _filter_cookies_by_host(all_cookies_list, used_host)
    return {
        "current_url": current_url,
        "host_filter": used_host or None,
        "total_in_browser": len(all_cookies_list),
        "count": len(filtered),
        "cookies": filtered,
    }


@router.post("/sessions/{session_id}/save_cookies_to_host")
async def session_save_cookies_to_host(session_id: str, body: dict) -> dict:
    """Promote the session's current cookies to a Host registry entry.

    Body (all optional)::

        {
          "host": "example.com",   // omit -> infer from current_url
          "notes": "paps acct",
          "all_cookies": false     // when true, save EVERY cookie in
                                   // the browser jar (cross-site,
                                   // third-party). Default false:
                                   // only cookies whose domain matches
                                   // the resolved host.
        }

    Returns the saved HostRecord. The cookies are sanitised through
    ``cookies_for_cdp`` so unknown fields (size/session/...) are dropped
    before being persisted -- otherwise a future ``Network.setCookies``
    call would reject them.
    """
    body = body or {}
    reg = _require_hosts()
    out = await _send_session_action(
        session_id,
        {"kind": "get_cookies"},
        timeout=15.0,
    )
    result = out.get("result")
    if not isinstance(result, dict):
        raise HTTPException(502, f"worker reply: {out}")
    all_browser_cookies = result.get("cookies") or []
    current_url = result.get("current_url") or ""
    host = (body.get("host") or "").strip()
    if not host:
        # Infer from the tab's current URL.
        try:
            from urllib.parse import urlparse as _urlparse

            host = _urlparse(current_url).hostname or ""
        except Exception:
            host = ""
    if not host:
        raise HTTPException(
            400,
            "could not infer host from the session's current URL; pass 'host' in the request body",
        )
    save_all = bool(body.get("all_cookies"))
    cookies_to_save = (
        all_browser_cookies if save_all else _filter_cookies_by_host(all_browser_cookies, host)
    )
    notes = body.get("notes")
    rec = reg.upsert(
        host=host,
        cookies=cookies_for_cdp(cookies_to_save),
        notes=notes if isinstance(notes, str) and notes.strip() else None,
    )
    return {
        **{
            "host": rec.host,
            "cookies": rec.cookies,
            "cookie_count": len(rec.cookies or []),
            "notes": rec.notes,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "last_used_at": rec.last_used_at,
        },
        "current_url": current_url,
        "total_in_browser": len(all_browser_cookies),
        "saved_count": len(cookies_to_save),
        "filtered": not save_all,
    }


# --- Actions ----------------------------------------------------------------


@router.post("/sessions/{session_id}/navigate")
async def session_navigate(session_id: str, body: dict) -> dict:
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(400, "missing url")
    # SSRF guard: each navigation is its own chance to dial a private
    # IP, so we re-validate on every page.goto() call. The script
    # could still trigger in-browser navigations (window.location =
    # ..., JS redirects, fetch('http://10.0.0.5/')) which don't go
    # through this endpoint -- the worker iptables egress firewall is
    # the defense for those.
    from server.hub.url_safety import assert_public_url
    assert_public_url(url)
    action = _route_to_page({"kind": "navigate", "url": url}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/click")
async def session_click(session_id: str, body: dict) -> dict:
    sel = (body or {}).get("selector")
    if not sel:
        raise HTTPException(400, "missing selector")
    action = _route_to_page({"kind": "click", "selector": sel}, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/fill")
async def session_fill(session_id: str, body: dict) -> dict:
    body = body or {}
    sel = body.get("selector")
    val = body.get("value")
    if not sel:
        raise HTTPException(400, "missing selector")
    if val is None:
        raise HTTPException(400, "missing value")
    # Wire name is still `text` on the worker side -- browser_ops.execute
    # reads action.text and maps to fill(value=).
    payload = {"kind": "type", "selector": sel, "text": val}
    # ${name} placeholder substitution: when the SDK passes variables=
    # (page.fill(sel, "${pw}", variables={"pw": SECRET})) the dict travels
    # untouched to the worker, which substitutes at the CDP edge so the
    # real value never appears in hub logs or any LLM prompt.
    if body.get("variables"):
        payload["variables"] = dict(body["variables"])
    action = _route_to_page(payload, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/press")
async def session_press(session_id: str, body: dict) -> dict:
    """Press a key (or key combo) on the bound tab.

    Body::

        {
          "key": "Backspace",        # or "Ctrl+A", "Enter", "ArrowDown"...
          "count": 3,                # optional, default 1
          "modifiers": ["Ctrl"]      # optional; OR'd with anything
                                     # parsed from the combo string.
                                     # Accepts Ctrl/Shift/Alt/Meta and
                                     # common synonyms (Cmd, Option,
                                     # Control, Win, Super).
        }
    """
    body = body or {}
    key = body.get("key")
    if not key:
        raise HTTPException(400, "missing key")
    count = int(body.get("count") or 1)
    if count < 1 or count > 100:
        raise HTTPException(400, "count must be in [1, 100]")
    # Normalise modifiers list -> CDP bitfield. The worker also
    # supports raw int but we keep the wire format human-readable so
    # operators inspecting captured traffic can read it.
    _MOD_BITS = {
        "alt": 1,
        "option": 1,
        "opt": 1,
        "ctrl": 2,
        "control": 2,
        "meta": 4,
        "cmd": 4,
        "command": 4,
        "win": 4,
        "super": 4,
        "shift": 8,
    }
    mods = body.get("modifiers")
    bits: int | None = None
    if isinstance(mods, list):
        bits = 0
        for m in mods:
            if isinstance(m, str):
                bits |= _MOD_BITS.get(m.lower(), 0)
    elif isinstance(mods, int):
        bits = mods
    payload: dict = {"kind": "press_key", "key": key, "count": count}
    if bits:
        payload["modifiers"] = bits
    payload = _route_to_page(payload, body)
    return await _send_session_action(session_id, payload)


@router.post("/sessions/{session_id}/type")
async def session_type(session_id: str, body: dict) -> dict:
    """Insert text into the currently-focused element.

    Body::

        {"text": "hello world"}

    Uses CDP Input.insertText -- one shot, no per-character round
    trip, works for <input>/<textarea>/contenteditable. Caller must
    have already clicked / focused the target; this endpoint does
    NOT move focus.
    """
    body = body or {}
    text = body.get("text")
    if text is None or text == "":
        raise HTTPException(400, "missing 'text'")
    if not isinstance(text, str):
        raise HTTPException(400, "'text' must be a string")
    payload = {"kind": "type_text", "text": text}
    if body.get("variables"):
        # Same ${name} substitution semantics as /fill -- worker swaps
        # placeholders for real values at the CDP edge so secrets never
        # surface in hub logs / LLM prompts.
        payload["variables"] = dict(body["variables"])
    action = _route_to_page(payload, body)
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/scroll")
async def session_scroll(session_id: str, body: dict) -> dict:
    body = body or {}
    direction = body.get("direction") or "down"
    pixels = int(body.get("pixels") or body.get("amount") or 800)
    action = _route_to_page(
        {"kind": "scroll", "direction": direction, "amount": pixels},
        body,
    )
    return await _send_session_action(session_id, action)


@router.post("/sessions/{session_id}/back")
async def session_back(session_id: str, body: dict | None = None) -> dict:
    action = _route_to_page({"kind": "back"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/forward")
async def session_forward(session_id: str, body: dict | None = None) -> dict:
    """Browser の Forward ボタン相当。history の 1 つ先の entry に進む。
    既に末尾なら no-op で OK を返す。"""
    action = _route_to_page({"kind": "forward"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/history_first")
async def session_history_first(session_id: str, body: dict | None = None) -> dict:
    """履歴の 0 番目 (このセッションで最初に開いたページ) に戻る。
    既に 0 番目なら no-op。"""
    action = _route_to_page({"kind": "history_first"}, body)
    return await _send_session_action(session_id, action, timeout=60.0)


@router.post("/sessions/{session_id}/agent")
async def session_agent(session_id: str, body: dict) -> dict:
    """Run a localised LLM agent loop on an open session.

    Body::

        {"goal": "Dismiss any popups", "max_steps": 3}

    Returns ``{completed, steps_taken, summary, last_action, error}``.
    Implements ``page.agent(goal, max_steps)`` in the SDK; useful for
    hybrid scripts where most logic is deterministic but a few spots
    (age gates, login dialogs, "find the play button") need LLM
    judgement.
    """
    body = body or {}
    goal = (body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "missing 'goal'")
    max_steps = int(body.get("max_steps") or 5)
    if max_steps < 1 or max_steps > 30:
        raise HTTPException(400, "max_steps must be in [1, 30]")
    engine = (body.get("engine") or "auto").lower()
    if engine not in ("auto", "qwen", "cogagent"):
        raise HTTPException(
            400,
            "engine must be 'auto', 'qwen', or 'cogagent'",
        )
    info = _get_session_or_404(session_id)
    worker = state.registry.connections.get(info.worker_id)
    if worker is None:
        raise HTTPException(
            502,
            f"session worker '{info.worker_id}' is no longer connected",
        )
    async with info.lock:
        info.state = "running"
        info.current_action = f"agent({max_steps}, engine={engine})"
        # See _send_session_action for why we refresh here too.
        info.last_active_at = datetime.utcnow()
        try:
            reply = await worker.session_agent(
                session_id,
                goal,
                max_steps,
                engine=engine,
                # cogagent calls add ~2s, qwen ~3-8s; auto can chain
                # both. Give 60s/step headroom (+ initial 60s base).
                timeout=max(60.0, max_steps * 60.0),
            )
        except TimeoutError:
            raise HTTPException(504, "page.agent() timed out")
        except Exception as e:
            raise HTTPException(502, f"page.agent() send failed: {e}")
        finally:
            info.current_action = None
            info.state = "idle"
    info.last_active_at = datetime.utcnow()
    return {
        "completed": reply.completed,
        "steps_taken": reply.steps_taken,
        "summary": reply.summary,
        "last_action": reply.last_action,
        "error": reply.error,
        # Per-step trace -- the SDK prints these continuation lines
        # after the [paprika] action log so the job log shows what
        # the agent actually did. Empty when the worker emitted no
        # actions or when an older worker without the field replies.
        "steps": list(getattr(reply, "steps", None) or []),
    }


@router.post("/codegen")
async def codegen(body: dict) -> dict:
    """Generate a paprika-client script from a natural-language task.

    Body::

        {
          "goal": "Open HN, click each story link in order, capture each",
          "hub_url": "http://paprika.lan",     // optional
          "extra_context": "...",                    // optional
          "max_tokens": 2000,                        // optional
          "temperature": 0.1,                         // optional
          "engine": "chatgpt51"                       // optional, default env
        }

    Returns ``{code, raw, model, elapsed_ms, finish_reason, usage,
    tool_calls}``. ``tool_calls`` lists any web_search calls the model
    made -- empty when the engine doesn't speak OpenAI tools or didn't
    need to look anything up. Server-side execution is NOT performed --
    the operator copies the code out and runs it themselves.
    """
    goal = (body or {}).get("goal") or ""
    if not goal.strip():
        raise HTTPException(400, "missing 'goal'")
    hub_url = (body or {}).get("hub_url") or "http://hub:8000"
    extra = (body or {}).get("extra_context")
    # Optional engine routing. Lets the admin UI (and curl-from-the-
    # terminal smoke tests) target a specific registered engine instead
    # of always falling through to the env-default CODEGEN_LLM_URL.
    # Unknown slug -> resolve_engine_target falls back to env defaults
    # internally (with a stderr note), so a stale slug isn't fatal.
    from server.hub.codegen import resolve_engine_target as _resolve_engine

    engine_slug = ((body or {}).get("engine") or "").strip() or None
    llm_target = _resolve_engine(engine_slug, state.engines) if engine_slug else None
    try:
        out = await generate_script(
            goal,
            hub_url=hub_url,
            extra_context=extra,
            max_tokens=int((body or {}).get("max_tokens") or 2000),
            temperature=float((body or {}).get("temperature") or 0.1),
            target=llm_target,
            download_video=bool((body or {}).get("download_video", False)),
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"LLM call failed: {e}")
    return out


@router.get("/codegen/info")
async def codegen_info() -> dict:
    """Expose which LLM the hub will use so the UI can show it."""
    return {
        "llm_url": CODEGEN_LLM_URL,
        "model_name": CODEGEN_MODEL_NAME,
    }


def _state_key_safe(key: str) -> str:
    """Sanitise a state key so it can be used as a filename. Allows
    [A-Za-z0-9._-]; everything else becomes '_'. Capped at 80 chars."""
    return _re.sub(r"[^A-Za-z0-9._-]", "_", key or "default")[:80] or "default"


def _state_path(parent_job_id: str, key: str) -> Path:
    return get_storage_dir() / parent_job_id / "state" / f"{_state_key_safe(key)}.json"


@router.get("/sessions/{session_id}/state/{key}")
async def get_session_state(session_id: str, key: str) -> dict:
    """Read persistent key/value state for the session's parent job.

    State is stored under ``data/jobs/{parent_job_id}/state/<key>.json``
    so it survives across attempts of the same codegen-loop / rerun
    job. New session in the same parent job sees the same state --
    that's exactly what pap.walk()'s resume needs.

    Returns ``{key, data}`` (data may be any JSON value). 404 if no
    state was stored under that key. 400 if the session has no
    parent_job_id (state only makes sense bound to a job).
    """
    info = _get_session_or_404(session_id)
    parent_jid = info.job_id
    if not parent_jid:
        raise HTTPException(
            400,
            "session has no parent_job_id; state requires a job-bound "
            "session (set parent_job_id when opening the session, or "
            "use codegen-loop / rerun mode which sets it automatically)",
        )
    path = _state_path(parent_jid, key)
    if not path.exists():
        raise HTTPException(404, f"no state stored under key {key!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"state corrupt: {e}")
    return {"key": _state_key_safe(key), "data": data}


@router.put("/sessions/{session_id}/state/{key}")
async def put_session_state(session_id: str, key: str, body: dict) -> dict:
    """Write persistent state for the session's parent job (see
    GET counterpart for storage layout). Body must be JSON object
    ``{"data": <any JSON>}``."""
    info = _get_session_or_404(session_id)
    parent_jid = info.job_id
    if not parent_jid:
        raise HTTPException(
            400,
            "session has no parent_job_id; state requires a job-bound session",
        )
    body = body or {}
    if "data" not in body:
        raise HTTPException(400, "body must contain 'data' field")
    path = _state_path(parent_jid, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(body["data"], ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"state write failed: {e}")
    return {"key": _state_key_safe(key), "ok": True, "bytes": path.stat().st_size}


@router.post("/sessions/{session_id}/capture")
async def session_capture(session_id: str, body: dict) -> dict:
    body = body or {}
    label = body.get("label") or "capture"
    step = int(body.get("step") or 0)
    action = _route_to_page(
        {"kind": "capture", "label": label, "step": step},
        body,
    )
    return await _send_session_action(session_id, action, timeout=30.0)


@router.post("/sessions/{session_id}/solve_cloudflare")
async def session_solve_cloudflare(session_id: str, body: dict) -> dict:
    """Wait out a Cloudflare 'Just a moment...' managed challenge on
    the session's current page.

    Body (all optional): ``{timeout_s: float, page_id: str}``.
    Returns ``{status, result: {cleared, title, waited_s}}``.

    nodriver is an undetected real Chrome, so the common Cloudflare
    *managed* challenge auto-passes within a few seconds of loading
    -- this just polls the page title until the challenge marker is
    gone. A challenge that demands an explicit Turnstile checkbox
    click is NOT solved here (operator clicks it via noVNC; the
    resulting cf_clearance cookie auto-saves to /hosts/{host} and is
    reused on later sessions since the worker fleet shares an egress
    IP + Chrome UA).
    """
    body = body or {}
    timeout_s = float(body.get("timeout_s") or 25.0)
    if timeout_s < 1:
        timeout_s = 1.0
    if timeout_s > 180:
        timeout_s = 180.0
    action: dict = {"kind": "solve_cloudflare", "timeout_s": timeout_s}
    if "click_checkbox" in body:
        action["click_checkbox"] = bool(body["click_checkbox"])
    action = _route_to_page(action, body)
    return await _send_session_action(
        session_id,
        action,
        # +30 covers the post-click re-poll window (~12s) + verify_cf
        # screenshot/template work on top of the wait timeout.
        timeout=timeout_s + 30.0,
    )


@router.post("/sessions/{session_id}/download_video")
async def session_download_video(session_id: str, body: dict) -> dict:
    """Shell to yt-dlp against ``body["url"]`` (or the session's
    current page URL if omitted) and save the resulting video files
    to the parent job's /assets. Returns ``{ok, url, message, files,
    file_count}``.

    The worker-side timeout for the yt-dlp subprocess is controlled
    by ``body["timeout_s"]`` (default 1800s, up to ~10 days). The hub
    side waits ``timeout_s + 60`` for the worker's reply, so a long
    download won't trip the default 30s session_action timeout.
    """
    body = body or {}
    url = body.get("url")
    referer = body.get("referer")
    # Match the JobOptions.attempt_timeout_s cap (10 days). yt-dlp
    # itself respects this as its subprocess timeout.
    timeout_s = int(body.get("timeout_s") or 1800)
    if timeout_s < 30:
        timeout_s = 30
    if timeout_s > 864000:
        timeout_s = 864000
    action: dict = {"kind": "download_video", "timeout_s": timeout_s}
    if url:
        action["url"] = url
    if referer:
        action["referer"] = referer
    action = _route_to_page(action, body)
    return await _send_session_action(
        session_id,
        action,
        # Give the worker enough time for the subprocess + uploads,
        # plus a small buffer for the round-trip WS / multipart upload.
        timeout=timeout_s + 120.0,
    )
