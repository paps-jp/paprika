"""Session lifecycle: create/close/list/get/pages/resize/switch/keepalive/exists/internal action.

Part of the sessions/ package; shared bits in _base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from server.hub._state import config, get_storage_dir, state
from server.hub._helpers import _asset_upload_url
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

log = logging.getLogger(__name__)

from server.hub.routes.sessions._base import *  # noqa: F401,F403


async def _session_visible_one(info, request) -> bool:
    """Phase 2b read-scope for a single session. A scoped caller (enforce,
    non-admin user) may see a session when they OWN it, or when its parent
    job (``job_id``) is theirs — auto-sessions from a job dispatch carry no
    owner and are judged by the job. off / optional / admin → always True
    (non-breaking)."""
    from server.hub.auth import owner_of, should_scope
    p = getattr(getattr(request, "state", None), "principal", None)
    if not should_scope(p):
        return True
    owner = owner_of(request)
    if str(getattr(info, "owner_id", "default") or "default") == owner:
        return True
    jid = getattr(info, "job_id", None)
    if jid and state.store is not None:
        try:
            ji = await state.store.get_job_info(jid)
        except Exception:
            ji = None
        if ji is not None and getattr(ji, "owner_id", "default") == owner:
            return True
    return False


async def _scope_session_items(items: list[dict], request) -> list[dict]:
    """Phase 2b read-scope for the /sessions list. Same rule as
    :func:`_session_visible_one` but batched: resolve the parent-job owner once
    per distinct job_id (the admin UI is never scoped, so this only runs for a
    non-admin user's own poll). off / optional / admin return ``items`` as-is."""
    from server.hub.auth import owner_of, should_scope
    p = getattr(getattr(request, "state", None), "principal", None)
    if not should_scope(p):
        return items
    owner = owner_of(request)
    # Only sessions NOT already owner-matched need a parent-job lookup.
    pending = {
        it.get("job_id")
        for it in items
        if it.get("job_id") and str(it.get("owner_id") or "default") != owner
    }
    owned_jobs: set[str] = set()
    if pending and state.store is not None:
        for jid in pending:
            try:
                ji = await state.store.get_job_info(jid)
            except Exception:
                ji = None
            if ji is not None and getattr(ji, "owner_id", "default") == owner:
                owned_jobs.add(jid)
    return [
        it for it in items
        if str(it.get("owner_id") or "default") == owner
        or (it.get("job_id") in owned_jobs)
    ]


@router.post("/internal/sessions/{session_id}/action", include_in_schema=False)
async def internal_session_action(session_id: str, body: dict, request: Request):
    """Hub→Hub forwarding sink (phase 3). A sibling hub that received a
    /sessions/* request it doesn't own POSTs the action here, to the hub
    that holds the worker WS. We run it LOCALLY (never re-forward, so a
    stale Session Map can't bounce a request between hubs) and return the
    same shape the public endpoints do.

    Internal-only: guarded by the worker secret (same shared secret the
    workers use) and never published in the OpenAPI schema. Not reachable
    from the public surface in the single-hub default.
    """
    if config.worker_secret:
        sent = request.headers.get("X-Paprika-Worker-Secret")
        if sent != config.worker_secret:
            raise HTTPException(401, "bad secret")
    action = body.get("action")
    if not isinstance(action, dict):
        raise HTTPException(400, "body.action must be an object")
    try:
        timeout = float(body.get("timeout") or 30.0)
    except (TypeError, ValueError):
        timeout = 30.0
    # Local-only: 404s here (rather than re-forwarding) when the session
    # isn't actually on this hub, so the origin hub surfaces a clean 404.
    return await _send_session_action_local(session_id, action, timeout=timeout)


@router.post("/sessions")
async def create_session(body: dict, request: Request = None) -> dict:
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
            # P1 cross-hub forward (mirror of POST /jobs at
            # server/hub/routes/jobs/lifecycle.py:1890): pick_worker only
            # consults THIS hub's local connections, so a hub whose local
            # workers happen to all be busy 503s even when peer hubs sit
            # with free lanes. Codegen-loop sandboxed scripts hit this
            # constantly because they cli.session() in a tight loop, and
            # the parent codegen-loop is pinned to the hub that owns the
            # parent JobInfo -- a bad scheduling-hash draw kept failing
            # 3 attempts in a row even with 50+ free fleet-wide lanes.
            # Try a peer with spare BEFORE returning 503. _FWD_MARK on the
            # request short-circuits the loop -- one hop max, no bounce.
            if request is not None and not request.headers.get(_FWD_MARK):
                from server.hub.routes.jobs._base import (
                    _peer_hub_with_spare_capacity,
                )
                _peer = await _peer_hub_with_spare_capacity()
                if _peer:
                    try:
                        _resp = await _proxy_request_to_hub(_peer, request, 30.0)
                    except Exception:
                        _resp = None
                    if (
                        _resp is not None
                        and getattr(_resp, "status_code", 503) != 503
                    ):
                        log.info(
                            "[hub] /sessions: no free local worker -> "
                            "forwarded to peer hub %s (cross-hub create)",
                            _peer,
                        )
                        return _resp
                    # peer also full / unreachable -> fall through to the local 503.
            raise HTTPException(503, "no active worker available")

    sid = new_session_id()
    parent_jid = body.get("parent_job_id") or body.get("job_id") or None
    # Phase 2b: stamp the tenant that opened this session (off/optional →
    # "default"). ``request`` is None for the in-process auto-relogin caller
    # (_ensure_host_login), which owner_of() maps to "default" — an internal
    # session, correctly the shared tenant.
    from server.hub.auth import owner_of
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
        owner_id=owner_of(request),
    )
    state.sessions.add(info)
    # When this session is owned by a parent job, point page.capture()
    # uploads at that job's existing /assets endpoint so its inline
    # gallery actually shows the captures.
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
                    auto_popup_policy = rec.popup_policy or "kill"
                    # Phase 2b: inject the host's cookies only when this
                    # session's owner may use them (shared / same tenant).
                    # auto_host (used for the post-close save-back) stays unset
                    # otherwise, so a scoped session can't clobber another
                    # tenant's host record. No-op under off/optional.
                    from server.hub.auth import owner_can_use
                    if rec.cookies and owner_can_use(
                        getattr(rec, "owner_id", "default"),
                        job_owner=info.owner_id,
                        shared=getattr(rec, "shared", True),
                    ):
                        auto_cookies = cookies_for_cdp(rec.cookies)
                        auto_host = host
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

    # URL blacklist (V): operator-managed substring deny list. Worker
    # drops matching URLs at the asset capture layer AND skips yt-dlp
    # for them. Same source as HubAssignJob.asset_url_blacklist.
    asset_bl: list[str] = []
    if state.settings is not None:
        try:
            _raw = (state.settings.get("asset_url_blacklist", "") or "").strip()
            asset_bl = [
                line.strip()
                for line in _raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
        except Exception:
            asset_bl = []

    # Optional operator Chrome profile -- same shape as
    # JobOptions.use_profile but exposed on /sessions directly so SDK
    # callers (cli.session(use_profile=...)) get the same plumbing.
    # Falls back to the operator-set default profile when the call
    # doesn't specify one.
    profile_url: str | None = None
    profile_etag: str | None = None
    profile_name = (body.get("use_profile") or "").strip() or None
    _explicit_profile = profile_name is not None
    # Profiles are shared across hubs (MariaDB metadata + MinIO bytes) -- resolve
    # the default + existence/etag from the shared view so a session on any hub
    # can use a profile uploaded on any other hub.
    from server.hub.routes.profiles import _shared_default, _shared_meta
    if profile_name is None:
        profile_name = await _shared_default()
    _smeta = None
    if profile_name:
        _smeta = await _shared_meta(profile_name)
        if _smeta is None:
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
        else:
            # Phase 2b tenancy: this session's owner may only use a profile
            # that is shared or theirs. An explicit borrow is rejected (403);
            # a non-visible default is silently skipped. No-op off/optional.
            from server.hub.auth import owner_can_use
            if not owner_can_use(
                _smeta.get("owner_id"),
                job_owner=info.owner_id,
                shared=bool(_smeta.get("shared", True)),
            ):
                if _explicit_profile:
                    state.sessions.remove(sid)
                    raise HTTPException(
                        403,
                        f"use_profile: profile '{profile_name}' is not "
                        "available to this account",
                    )
                profile_name = None  # non-visible default → stock profile
    if profile_name:
        # Use the worker-dialled base URL when available so a worker
        # behind NAT / on a different subnet still gets a reachable
        # URL. Falls back to PUBLIC_BASE_URL just like asset uploads.
        base = worker.public_base_url or config.public_base_url
        if base:
            profile_url = f"{base.rstrip('/')}/profiles/{profile_name}"
        profile_etag = (_smeta or {}).get("etag") or None

    try:
        ack = await worker.start_session(
            sid,
            initial_url=initial_url,
            lane_hint=lane_hint if isinstance(lane_hint, int) else None,
            asset_upload_base=session_upload_base,
            cookies=auto_cookies,
            min_asset_size_bytes=min_asset,
            asset_url_blacklist=asset_bl,
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


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str, request: Request = None) -> dict:
    """Release the Lane bound to ``session_id``.

    Before sending HubSessionEnd to the worker, the hub dumps the
    session's current cookie jar (filtered to cookies matching the
    host of ``initial_url``) and upserts it into the host registry.
    Mirrors Fetch's post-run auto-save: every host the operator
    explicitly opens a session against ends up in the Hosts tab,
    Cookie state preserved across script restarts.
    """
    # Multi-hub: a close that lands on a non-owner hub is forwarded to
    # the owning hub, which holds the worker WS + does the cookie-save /
    # video-drain / parent-job cascade. Generous timeout to cover the
    # worker-side drain window (PAPRIKA_VIDEO_DRAIN_HARD_S, default 30m).
    #
    # ``request is None`` => an INTERNAL caller (the TTL reaper's
    # close_session bridge), which only ever closes a session THIS hub
    # owns locally -- there's nothing to forward, and there's no HTTP
    # request to forward anyway. Skip straight to the local close.
    if request is not None:
        import os as _os_fwd
        _close_to = float(_os_fwd.environ.get("PAPRIKA_VIDEO_DRAIN_HARD_S", "1800.0")) + 120.0
        fwd = await _maybe_forward_session(session_id, request, forward_timeout=_close_to)
        if fwd is not None:
            return fwd
    _require_session_infra()
    # Mark as closing BEFORE removing from registry so list_sessions
    # mid-close shows the transition state.
    pre = state.sessions.get(session_id)
    if pre is None:
        raise HTTPException(404, f"session '{session_id}' not found")
    # Snapshot the entry state before we flip it to "closing": Fetch
    # sessions (worker-managed lifecycle) already capture their own
    # end-of-fetch screenshot, so we skip the final capture below for
    # them to avoid a duplicate (rare path: admin force-close on a
    # running fetch).
    pre_was_fetch = (pre.state or "") == "fetch_running"
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

    # End-of-session FULL-PAGE screenshot. Capped at 3000 px so an
    # infinite-scroll page can't blow up the JPEG. Goes through the
    # session_action path so CDP's captureBeyondViewport handles the
    # off-screen render without actually scrolling the page (script
    # state stays put). Publishes:
    #   * to the parent job's gallery as ``screenshot-final-<ts>.jpg``
    #     (visible in admin UI's Live > Screenshot tab) when the
    #     session is job-bound;
    #   * mirrored to ``attempts/<latest-N>/final_screenshot.jpg`` when
    #     a codegen-loop attempts dir exists, so the Judge LLM sees
    #     the FULL-PAGE final state instead of the last 5 s viewport
    #     poll on clean exits.
    # Skip when this was a fetch session (already captured by the
    # worker), the worker WS is gone, or the lane isn't bound.
    if (
        not pre_was_fetch
        and pre.lane_idx is not None
        and state.registry is not None
        and state.registry.connections.get(pre.worker_id) is not None
        and (os.environ.get("PAPRIKA_SESSION_FINAL_SCREENSHOT", "1") or "1").strip().lower()
            not in ("0", "false", "no", "off")
    ):
        try:
            import base64 as _b64fs
            import time as _time_fs
            _ts = _time_fs.strftime("%Y%m%d-%H%M%S")
            _act: dict = {
                "kind": "screenshot",
                "full_page": True,
                "max_height": 3000,
                "format": "jpeg",
                "quality": 50,
            }
            if pre.job_id:
                _act["label"] = f"final-{_ts}"
            _reply = await _send_session_action(session_id, _act, timeout=20.0)
            _b64_str = (_reply or {}).get("result") if isinstance(_reply, dict) else None
            if isinstance(_b64_str, str) and _b64_str and pre.job_id:
                _attempts_dir = get_storage_dir() / pre.job_id / "attempts"
                if _attempts_dir.is_dir():
                    _ns = sorted(
                        (
                            int(d.name) for d in _attempts_dir.iterdir()
                            if d.is_dir() and d.name.isdigit()
                        ),
                        reverse=True,
                    )
                    if _ns:
                        try:
                            _out = _attempts_dir / str(_ns[0]) / "final_screenshot.jpg"
                            _out.parent.mkdir(parents=True, exist_ok=True)
                            _out.write_bytes(_b64fs.b64decode(_b64_str))
                        except Exception:
                            log.info(
                                "session %s final-screenshot attempt-dir mirror failed",
                                session_id,
                                exc_info=True,
                            )
        except Exception:
            log.info(
                "session %s final-screenshot capture failed",
                session_id,
                exc_info=True,
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
                # state-model v1: a keep_session job whose held session is
                # closed (operator DELETE or idle/absolute TTL reap) is a
                # NORMAL completion -- the capture already succeeded, the
                # held browser just expired.  Phase mirrors status; the
                # old "keepalive_closed" marker (which nothing read) is
                # gone.
                job.progress.phase = "completed"
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
async def list_sessions(request: Request) -> dict:
    """List active sessions across the fleet. ``novnc_url`` is rewritten to
    the session-rooted hub proxy URL so admin UI tiles open via the hub
    (worker LAN IPs stay private).

    Multi-hub: the in-memory registry holds only THIS hub's sessions, so
    behind nginx a bare local list flickers in/out as the admin polls
    different hubs (sessions appear/disappear; live-preview tiles flip
    RUNNING/keepalive). Unless this call is already a forwarded hop, fan out
    to every live peer hub and merge (deduped by session_id) so any hub
    returns the same fleet-wide set."""
    _require_session_infra()
    items = [_proxy_session_dict(s.to_json()) for s in state.sessions.all()]
    if not request.headers.get(_FWD_MARK) and state.hubs is not None:
        try:
            hubs = await state.hubs.list_all()
        except Exception:
            hubs = []
        peers = [
            h.get("hub_id")
            for h in hubs
            if h.get("alive") and h.get("hub_id") and h.get("hub_id") != config.hub_id
        ]
        if peers:
            results = await asyncio.gather(
                *[_fetch_peer_sessions(hid) for hid in peers],
                return_exceptions=True,
            )
            seen = {it.get("session_id") for it in items}
            for res in results:
                if not isinstance(res, list):
                    continue
                for it in res:
                    sid = it.get("session_id")
                    if sid and sid not in seen:
                        seen.add(sid)
                        items.append(it)
    # Phase 2b: scope the merged fleet-wide list to the caller's tenant. Done
    # once on the aggregating hub (peers return their slice unscoped over the
    # worker-secret internal hop); no-op for off/optional/admin.
    items = await _scope_session_items(items, request)
    return {"count": len(items), "sessions": items}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    """Single-session details with ``novnc_url`` rewritten to the
    session-rooted hub proxy URL (see /sessions)."""
    # Multi-hub: forward to the owning hub when this hub doesn't hold it.
    fwd = await _maybe_forward_session(session_id, request, forward_timeout=15.0)
    if fwd is not None:
        return fwd
    info = _get_session_or_404(session_id)
    # Phase 2b: a scoped caller gets 404 for a session that isn't theirs (own
    # owner or parent job). Unguessable 128-bit ids already gate the action
    # endpoints; this hides existence on the read. No-op off/optional/admin.
    if not await _session_visible_one(info, request):
        raise HTTPException(404, f"session '{session_id}' not found")
    d = _proxy_session_dict(info.to_json())
    d["novnc_url_autoconnect"] = _novnc_autoconnect(d.get("novnc_url"))
    return d


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

