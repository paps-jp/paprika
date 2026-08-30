"""Shared core for /sessions: router + cross-hub forwarding + session helpers."""

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

router = APIRouter(tags=["Sessions"])

def _proxy_session_dict(d: dict) -> dict:
    """Lazy-bridge wrapper around app.py's _proxy_session_dict.

    The real implementation lives in the noVNC subsection of app.py
    which is defined AFTER the include_router for this module, so we
    can't eager-import it. Wrap with a function-level import so the
    lookup happens at call time when app.py is fully loaded.
    """
    from server.hub.routes.novnc import _proxy_session_dict as _impl

    return _impl(d)


def _disconnect_session_novnc_clients(session_id: str) -> None:
    """Lazy bridge to app.py's helper. Used by close_session to kick
    noVNC viewers off a session being torn down. The implementation
    lives in app.py's noVNC subsection (defined after our
    include_router stanza), so wrap with a function-level import."""
    from server.hub.routes.novnc import _disconnect_session_novnc_clients as _impl

    return _impl(session_id)


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


def _hub_internal_url(hub_id: str, path: str) -> str:
    """Internal base URL of a sibling hub, for cross-host session / noVNC
    forwarding. Resolution order:

      1. ``PAPRIKA_HUB_INTERNAL_FMT`` -- explicit override (``{hub}`` placeholder).
      2. IP-encoded hub_id (``hub-36`` -> ``http://<subnet>.36:<port>``). The
         clone-safe scheme derives hub_id from the host LAN IP (see app.py
         ``_resolve_hub_id_from_host_ip``), so forwarding to a peer Just Works
         with no per-hub config / extra_hosts. Subnet + port via env.
      3. Legacy default ``http://{hub}:8000`` (in-compose service name)."""
    fmt = os.environ.get("PAPRIKA_HUB_INTERNAL_FMT")
    if fmt:
        return f"{fmt.format(hub=hub_id).rstrip('/')}{path}"
    m = _re.match(r"^hub-(\d{1,3})$", hub_id or "")
    if m:
        subnet = os.environ.get("PAPRIKA_HUB_SUBNET", "10.10.50")
        port = os.environ.get("PAPRIKA_HUB_INTERNAL_PORT", "8100")
        return f"http://{subnet}.{m.group(1)}:{port}{path}"
    return f"http://{hub_id}:8000{path}"


async def _forward_session_action(
    owner_hub: str,
    session_id: str,
    action: dict,
    timeout: float,
):
    """Forward an action to the hub that owns the session, return its
    JSON result verbatim. Raises HTTPException on transport failure so
    the caller sees a clean 502/504 instead of a raw exception."""
    url = _hub_internal_url(owner_hub, f"/internal/sessions/{session_id}/action")
    headers = {}
    if config.worker_secret:
        headers["X-Paprika-Worker-Secret"] = config.worker_secret
    # Allow a little slack over the action timeout for the extra hop.
    try:
        async with httpx.AsyncClient(timeout=timeout + 15.0) as client:
            r = await client.post(
                url,
                json={"action": action, "timeout": timeout},
                headers=headers,
            )
    except Exception as e:
        raise HTTPException(
            502,
            f"hub forward to '{owner_hub}' failed: {type(e).__name__}: {e}",
        )
    if r.status_code == 404:
        # Owner hub no longer has the session (closed / reaped / it
        # restarted). Surface as 404 -- the session is genuinely gone.
        raise HTTPException(404, f"session '{session_id}' not found")
    if r.status_code >= 500:
        raise HTTPException(r.status_code, r.text)
    try:
        return r.json()
    except Exception:
        raise HTTPException(502, "hub forward returned a non-JSON body")


_FWD_MARK = "X-Paprika-Hub-Forwarded"


async def _proxy_request_to_hub(owner_hub: str, request: Request, forward_timeout: float):
    """Reverse-proxy the *raw* incoming request (method, path, query,
    body, headers) to the hub that owns the session and return its
    response verbatim. Used for whole-request endpoints (close / status)
    where the owner hub must run the handler locally -- it holds the
    worker WS, the SessionInfo and does the cookie-save / drain / cascade
    work a non-owner hub structurally cannot."""
    body = await request.body()
    path = request.url.path
    url = _hub_internal_url(owner_hub, path)
    if request.url.query:
        url = f"{url}?{request.url.query}"
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    fwd_headers[_FWD_MARK] = config.hub_id or "1"
    if config.worker_secret:
        fwd_headers["X-Paprika-Worker-Secret"] = config.worker_secret
    try:
        async with httpx.AsyncClient(timeout=forward_timeout) as client:
            r = await client.request(
                request.method, url, content=body, headers=fwd_headers
            )
    except Exception as e:
        raise HTTPException(
            502,
            f"hub forward to '{owner_hub}' failed: {type(e).__name__}: {e}",
        )
    # Strip hop-by-hop / length headers; let Starlette recompute them.
    drop = {"content-length", "transfer-encoding", "connection"}
    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in drop}
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
        media_type=r.headers.get("content-type"),
    )


async def _maybe_forward_session(
    session_id: str, request: Request, *, forward_timeout: float = 60.0
):
    """If ``session_id`` isn't held by THIS hub but the Session Map says
    another hub owns it, reverse-proxy the request there and return that
    Response. Returns None to mean "handle locally" -- which is always
    the case on a single hub, when the session is unknown everywhere, or
    when the request was already forwarded once (loop guard)."""
    _require_session_infra()
    if state.sessions.get(session_id) is not None:
        return None  # local -- handle here (the only single-hub path)
    if request.headers.get(_FWD_MARK):
        return None  # already a forwarded hop; never bounce again
    owner = await state.sessions.lookup_owner(session_id)
    if owner is None:
        return None  # unknown -> let the local handler 404 as before
    _worker_id, owner_hub = owner
    if not owner_hub or owner_hub == config.hub_id:
        return None
    return await _proxy_request_to_hub(owner_hub, request, forward_timeout)


async def _send_session_action(session_id: str, action: dict, *, timeout: float = 30.0):
    """Route a session action to whichever hub owns the session.

    Local-first: when this hub holds the session, run it here (the only
    path that ever executes on a single hub). Otherwise consult the
    Redis Session Map and forward to the owning hub; fall through to the
    same 404 as before when the session is unknown everywhere.
    """
    _require_session_infra()
    if state.sessions.get(session_id) is not None:
        return await _send_session_action_local(session_id, action, timeout=timeout)
    # Not local -- maybe another hub owns it (multi-hub only).
    owner = await state.sessions.lookup_owner(session_id)
    if owner is not None:
        _worker_id, owner_hub = owner
        if owner_hub and owner_hub != config.hub_id:
            return await _forward_session_action(
                owner_hub, session_id, action, timeout
            )
    raise HTTPException(404, f"session '{session_id}' not found")


async def _send_session_action_local(
    session_id: str, action: dict, timeout: float = 30.0
):
    """Run an action against the worker WS held by THIS hub.

    Serialises actions per-session via the session's lock, so two
    concurrent HTTP requests for the same session can't interleave
    CDP traffic on the same tab. Raises 404 if the session is not held
    locally (the forwarding wrapper / internal endpoint guarantees it
    is before calling here).
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
            # Same reasoning as the per-job harvest: an automatic save must
            # not fsync on the event loop. Operator edits keep durable=True.
            durable=False,
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


async def _fetch_peer_sessions(owner_hub: str) -> list[dict]:
    """GET a peer hub's LOCAL session list for the cross-hub /sessions merge.
    The _FWD_MARK header makes the peer return only its own sessions (it skips
    its own fan-out) so there's no recursion. Best-effort: any failure yields
    [] so one slow / down peer never breaks the merge."""
    headers = {_FWD_MARK: config.hub_id or "1"}
    if config.worker_secret:
        headers["X-Paprika-Worker-Secret"] = config.worker_secret
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                _hub_internal_url(owner_hub, "/sessions"), headers=headers
            )
        if r.status_code == 200:
            return r.json().get("sessions") or []
    except Exception:
        pass
    return []


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


def _operator_trace_path(info: SessionInfo) -> Path | None:
    jid = getattr(info, "job_id", None)
    if not jid:
        return None
    return get_storage_dir() / jid / "operator_actions.json"


def _load_operator_trace(info: SessionInfo) -> list:
    p = _operator_trace_path(info)
    if not p or not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


async def _close_session_popups(session_id: str) -> dict:
    """Close every non-default tab in the session (ad popups / new
    windows spawned by clicks). Composite of the worker ``pages`` +
    ``close_page`` actions so the operator gets a single 'close popups'
    control. Returns {status, result:{closed:[...], count}}."""
    listing = await _send_session_action(
        session_id, {"kind": "pages"}, timeout=20.0,
    )
    res = listing.get("result") or {}
    pages = res.get("pages") or []
    closed: list = []
    for p in pages:
        if not isinstance(p, dict) or p.get("is_default"):
            continue
        pid = p.get("page_id")
        if not pid:
            continue
        try:
            r = await _send_session_action(
                session_id, {"kind": "close_page", "page_id": pid}, timeout=20.0,
            )
            if not str(r.get("status") or "").startswith("ERR:"):
                closed.append(pid)
        except Exception:
            pass
    return {"status": "OK", "elapsed_ms": 0,
            "result": {"closed": closed, "count": len(closed)}}


def _append_operator_trace(info: SessionInfo, entry: dict) -> int:
    p = _operator_trace_path(info)
    if not p:
        return 0
    trace = _load_operator_trace(info)
    trace.append(entry)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return len(trace)


def _state_key_safe(key: str) -> str:
    """Sanitise a state key so it can be used as a filename. Allows
    [A-Za-z0-9._-]; everything else becomes '_'. Capped at 80 chars."""
    return _re.sub(r"[^A-Za-z0-9._-]", "_", key or "default")[:80] or "default"


def _state_path(parent_job_id: str, key: str) -> Path:
    return get_storage_dir() / parent_job_id / "state" / f"{_state_key_safe(key)}.json"


__all__ = [
    'router',
    '_proxy_session_dict',
    '_disconnect_session_novnc_clients',
    '_require_session_infra',
    '_get_session_or_404',
    '_route_to_page',
    '_hub_internal_url',
    '_forward_session_action',
    '_FWD_MARK',
    '_proxy_request_to_hub',
    '_maybe_forward_session',
    '_send_session_action',
    '_send_session_action_local',
    '_novnc_autoconnect',
    '_auto_save_session_cookies',
    '_fetch_peer_sessions',
    '_filter_cookies_by_host',
    '_operator_trace_path',
    '_load_operator_trace',
    '_close_session_popups',
    '_append_operator_trace',
    '_state_key_safe',
    '_state_path',
]
