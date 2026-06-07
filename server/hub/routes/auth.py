"""Auth routes: UI login + API-key / user management.

Part of the "LAN-trust → multi-user / public" hardening. These endpoints
are the operator-facing front of :mod:`server.hub.auth`:

  * ``GET  /login``         — minimal login page (also the enforce-mode redirect target)
  * ``POST /auth/login``    — verify credentials → set a signed session cookie
  * ``POST /auth/logout`` / ``GET /logout`` — clear the session cookie
  * ``GET  /auth/me``       — the caller's resolved principal + active auth mode
  * ``POST /auth/users``    — create a user (admin/off)
  * ``GET  /auth/users``    — list users (admin/off)
  * ``POST /auth/keys``     — mint an API key (plaintext returned exactly once)
  * ``GET  /auth/keys``     — list keys (own; admin sees all)
  * ``DELETE /auth/keys/{id}`` — revoke a key

Authorization here is deliberately light for Phase 1: management endpoints
require an admin principal, but in ``off`` mode every request is the SYSTEM
principal (admin-equivalent) so the operator can bootstrap keys/users locally
before turning auth on. Data-ownership scoping is Phase 2.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from server.hub import auth as _auth
from server.hub._state import state

router = APIRouter(tags=["Auth"])


def _principal(request: Request) -> "_auth.Principal":
    return getattr(request.state, "principal", _auth.ANONYMOUS)


def _require_manage(request: Request) -> None:
    """Gate user/all-key management to admins. In ``off`` mode the principal is
    SYSTEM (admin-equivalent) so local bootstrap works without enabling auth."""
    if not _principal(request).is_admin:
        raise HTTPException(403, "admin privilege required")


def _cookie_secure() -> bool:
    """Send the session cookie with ``Secure`` only when configured (prod
    behind TLS). Default off so dev over plain http still logs in."""
    return (os.environ.get("PAPRIKA_COOKIE_SECURE") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _set_session_cookie(response: Response, principal: "_auth.Principal") -> None:
    token = _auth.sign_session(
        {"uid": principal.id, "email": principal.email, "role": principal.role}
    )
    response.set_cookie(
        _auth.SESSION_COOKIE, token,
        httponly=True, samesite="lax", secure=_cookie_secure(),
        max_age=7 * 24 * 3600, path="/",
    )


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@router.post("/auth/login")
async def auth_login(body: dict, response: Response) -> dict:
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    principal = await state.auth.verify_login(email, password)
    if principal is None:
        raise HTTPException(401, "invalid credentials")
    _set_session_cookie(response, principal)
    return {"ok": True, "user": {
        "id": principal.id, "email": principal.email, "role": principal.role,
    }}


@router.post("/auth/logout")
async def auth_logout(response: Response) -> dict:
    response.delete_cookie(_auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/logout")
async def logout_redirect() -> RedirectResponse:
    r = RedirectResponse("/login", status_code=302)
    r.delete_cookie(_auth.SESSION_COOKIE, path="/")
    return r


@router.get("/auth/me")
async def auth_me(request: Request) -> dict:
    p = _principal(request)
    return {
        "kind": p.kind, "id": p.id, "email": p.email, "role": p.role,
        "is_admin": p.is_admin, "is_authenticated": p.is_authenticated,
        "auth_mode": _auth.current_mode(),
    }


# ---------------------------------------------------------------------------
# Users (admin / off)
# ---------------------------------------------------------------------------

@router.post("/auth/users")
async def create_user(body: dict, request: Request) -> dict:
    _require_manage(request)
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    try:
        u = await state.auth.create_user(
            body.get("email", ""), body.get("password", ""),
            role=(body.get("role") or "user"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "user": u}


@router.get("/auth/users")
async def list_users(request: Request) -> dict:
    _require_manage(request)
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    users = await state.auth.list_users()
    return {"count": len(users), "users": users}


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

@router.post("/auth/keys")
async def create_api_key(body: dict, request: Request) -> dict:
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    p = _principal(request)
    user_id = (body.get("user_id") or "").strip()
    if not user_id:
        if p.kind == "user":
            user_id = p.id
        else:
            # off-mode SYSTEM / anonymous: there's no self user to own the key.
            raise HTTPException(400, "user_id required (no user principal)")
    elif not p.is_admin and user_id != p.id:
        raise HTTPException(403, "cannot mint keys for another user")
    try:
        rec, secret = await state.auth.create_api_key(
            user_id,
            name=(body.get("name") or ""),
            scopes=tuple(body.get("scopes") or ()),
            expires_at=(body.get("expires_at") or ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # ``secret`` is the full plaintext key — returned exactly once, never stored.
    return {"ok": True, "key": rec, "secret": secret}


@router.get("/auth/keys")
async def list_api_keys(request: Request) -> dict:
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    p = _principal(request)
    if p.is_admin:
        keys = await state.auth.list_api_keys(None)
    elif p.kind == "user":
        keys = await state.auth.list_api_keys(p.id)
    else:
        keys = []
    return {"count": len(keys), "keys": keys}


@router.delete("/auth/keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request) -> dict:
    if state.auth is None:
        raise HTTPException(503, "auth store not ready")
    p = _principal(request)
    scope_user = None if p.is_admin else (p.id if p.kind == "user" else "\0none")
    ok = await state.auth.revoke_api_key(key_id, user_id=scope_user)
    if not ok:
        raise HTTPException(404, "key not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Paprika · ログイン</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: #1b1b1b; color: #ddd;
    font: 15px/1.6 -apple-system, "Segoe UI", sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    width: 340px; max-width: 92vw;
    background: #242424; border: 1px solid #333; border-radius: 12px;
    padding: 2rem 1.8rem; box-shadow: 0 8px 28px rgba(0,0,0,.5);
  }
  .brand { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.4rem; }
  .brand img { width: 2rem; height: 2rem; }
  .brand h1 { font-size: 1.2rem; margin: 0; font-weight: 600; }
  label { display: block; font-size: .8rem; opacity: .8; margin: .9rem 0 .3rem; }
  input {
    width: 100%; padding: .6rem .7rem; font: inherit;
    background: #1b1b1b; border: 1px solid #3a3a3a; border-radius: 6px; color: #fff;
  }
  input:focus { outline: none; border-color: #c0392b; }
  button {
    width: 100%; margin-top: 1.4rem; padding: .65rem;
    background: #c0392b; color: #fff; border: 0; border-radius: 6px;
    font: inherit; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #a93226; }
  button:disabled { opacity: .6; cursor: default; }
  .err { color: #ff8a80; font-size: .82rem; min-height: 1.2em; margin-top: .8rem; }
</style>
</head>
<body>
  <form class="card" id="f">
    <div class="brand"><img src="/icon.svg" alt=""><h1>Paprika</h1></div>
    <label for="email">メールアドレス</label>
    <input id="email" type="email" autocomplete="username" required autofocus>
    <label for="password">パスワード</label>
    <input id="password" type="password" autocomplete="current-password" required>
    <button id="b" type="submit">ログイン</button>
    <div class="err" id="e"></div>
  </form>
<script>
  const params = new URLSearchParams(location.search);
  const next = params.get('next') || '/';
  const f = document.getElementById('f');
  const e = document.getElementById('e');
  const b = document.getElementById('b');
  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    e.textContent = ''; b.disabled = true;
    try {
      const r = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('email').value,
          password: document.getElementById('password').value,
        }),
      });
      if (r.ok) { location.href = next; return; }
      const j = await r.json().catch(() => ({}));
      e.textContent = j.detail || 'ログインに失敗しました';
    } catch (err) {
      e.textContent = '通信エラー: ' + err.message;
    } finally {
      b.disabled = false;
    }
  });
</script>
</body>
</html>
"""


@router.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return _LOGIN_HTML
