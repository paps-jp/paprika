"""AI engine registry routes: /engines/* (list, CRUD, promote/demote,
resolve, test).

Each EngineRecord is one LLM / VLM / VLA backend the operator has
wired in. Three seed entries (qwen / qwen-chat / cogagent) keep the
existing engine names working; new entries are how operators add
OpenAI / Claude (via LiteLLM proxy) / etc.

Secret-handling rules duplicated here so this module stays self-
contained:

* The literal ``api_key`` VALUE is NEVER returned by GET /engines or
  GET /engines/{slug} -- ``api_key_set: bool`` is all callers see.
* The ``api_key_env`` (the env-var NAME) IS surfaced, BUT if it looks
  like an API key (operator pasted secret into the wrong field --
  caught by ``_looks_like_secret``), the admin UI gets a redacted
  placeholder so the leak doesn't propagate.
* POST /engines/{slug}/resolve returns the resolved key + endpoint
  for worker-side use, gated by WORKER_SECRET when configured.
"""
from __future__ import annotations

import logging
import os
import time as _time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException

from server.hub._state import config, state
from server.hub.engines import (
    EngineRecord,
    EngineRegistry,
    normalise_slug as _engine_normalise_slug,
)


router = APIRouter(tags=["Engines"])
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MariaDB write-through (source-of-truth when configured)
# ---------------------------------------------------------------------------
#
# When MariaDB is hooked up, the file registry becomes a fast read
# cache and MariaDB owns the durable state. After each mutating
# operation in the routes we forward the change to MariaDB via these
# thin wrappers, so the next hub restart (which restores from MariaDB)
# sees the latest writes. Failures are logged but NOT raised -- a
# transient MariaDB outage shouldn't take down the admin UI, and the
# next startup ``auto_migrate_all`` push will heal the drift from the
# file side.

async def _mdb_upsert(rec: EngineRecord) -> None:
    pool = state.mariadb_pool
    if pool is None:
        return
    try:
        from server.hub.mariadb import upsert_engine_row
        await upsert_engine_row(pool, rec)
    except Exception as e:
        log.warning("mariadb upsert engine %s failed: %s", rec.slug, e)


async def _mdb_delete(slug: str) -> None:
    pool = state.mariadb_pool
    if pool is None:
        return
    try:
        from server.hub.mariadb import delete_engine_row
        await delete_engine_row(pool, slug)
    except Exception as e:
        log.warning("mariadb delete engine %s failed: %s", slug, e)


# ----------------------------------------------------------------------------
# Helpers (module-private; were inline in app.py before #2B-B)
# ----------------------------------------------------------------------------

def _require_engines() -> EngineRegistry:
    if state.engines is None:
        raise HTTPException(503, "engine registry not initialised")
    return state.engines


def _looks_like_secret(s: str) -> bool:
    """Heuristic: does this string look like an API key the operator
    mistakenly pasted into the env-var-NAME field?

    Real env var names are short, all-caps with underscores
    (e.g. ``OPENAI_API_KEY``). API keys are long random strings,
    often with a recognisable prefix.
    """
    if not s:
        return False
    s = s.strip()
    if len(s) > 40:
        return True
    # Common provider key prefixes.
    if s.startswith((
        "sk-", "sk_", "rk-", "rk_", "pk-", "pk_",
        "Bearer ", "anthropic-", "claude-",
        "ya29.", "ghp_", "gho_", "ghs_",
    )):
        return True
    return False


def _engine_to_dict(rec: EngineRecord) -> dict:
    """Project EngineRecord to the API shape.

    Secrets handling:
      * ``api_key`` (literal value) is NEVER returned. ``api_key_set``
        signals whether either auth source is configured.
      * ``api_key_env`` is the *name* of an env var and is normally
        safe to surface so the operator can confirm setup. But if the
        value looks like an API key (operator pasted secret into the
        wrong field -- a common UX trap), we redact it so the admin UI
        doesn't leak it back to anyone who can read /engines/.
    """
    direct_set = bool((rec.api_key or "").strip())
    env_set = False
    if rec.api_key_env:
        env_set = bool(os.environ.get(rec.api_key_env, "").strip())
    api_key_set = direct_set or env_set

    safe_env = rec.api_key_env
    if safe_env and _looks_like_secret(safe_env):
        safe_env = "***REDACTED*** (looks like an API key was pasted here; "\
                   "move it to the 'API key (direct)' field instead)"

    return {
        "slug": rec.slug,
        "name": rec.name,
        "kind": rec.kind,
        "protocol": rec.protocol,
        "endpoint": rec.endpoint,
        "model": rec.model,
        "api_key_env": safe_env,
        "api_key_set": api_key_set,
        "api_key_direct_set": direct_set,
        "headers": dict(rec.headers or {}),
        "timeout_s": rec.timeout_s,
        "promoted": rec.promoted,
        # Whether codegen will attach the OpenAI ``tools`` array (the
        # web_search tool) when routing through this engine. Operator
        # flips it off via the admin UI for endpoints that reject the
        # field.
        "supports_tools": rec.supports_tools,
        # Whether the Submit form's "コード生成 LLM" dropdown shows this
        # engine. Operator-driven opt-in; see EngineRecord.use_for_codegen.
        "use_for_codegen": rec.use_for_codegen,
        # Daily quota caps + today's usage. 0 = no cap on that limb.
        # Counters reset at UTC midnight. See EngineUsageRegistry.
        "daily_token_budget": rec.daily_token_budget,
        "daily_request_budget": rec.daily_request_budget,
        "usage_today": _usage_today_for(rec.slug),
        "notes": rec.notes,
        "builtin": rec.builtin,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


def _usage_today_for(slug: str) -> dict:
    """Read today's prompt/completion/request counters for ``slug``.
    Returns zero-valued dict when no data yet today or the usage
    registry isn't initialised."""
    reg = getattr(state, "engine_usage", None)
    if reg is None:
        return {"prompt": 0, "completion": 0, "requests": 0}
    try:
        return reg.get_today(slug)
    except Exception:
        return {"prompt": 0, "completion": 0, "requests": 0}


_OPENAI_ENDPOINT_SUFFIXES = (
    "/v1/chat/completions",
    "/v1/completions",
    "/chat/completions",
    "/v1",
)


def _normalise_engine_endpoint(endpoint: str, protocol: str) -> str:
    """Strip trailing path suffixes the worker code appends on its
    own, so an operator who pastes a full API URL from a vendor's
    docs doesn't end up with ``.../v1/chat/completions/v1/chat/completions``.

    Only applies to ``openai`` protocol -- agent-service / cogagent
    endpoints are paprika-internal and don't have this trap. No-op
    when the endpoint is already a clean base URL.
    """
    s = (endpoint or "").strip().rstrip("/")
    if protocol != "openai":
        return s
    for suffix in _OPENAI_ENDPOINT_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip("/")
            break
    return s


def _resolve_engine_payload(rec: EngineRecord) -> dict:
    """Project an EngineRecord to the dict workers use to actually
    talk to the backend. Differs from ``_engine_to_dict`` in two ways:

      * The real ``api_key`` value is included (workers need it to
        sign requests). This is why ``/resolve`` is auth-gated and
        ``/engines/{slug}`` is not.
      * Operator-only fields (``builtin``, ``notes``, timestamps) are
        omitted -- the worker just needs how-to-call-it.
    """
    api_key = ""
    if rec.api_key:
        api_key = rec.api_key.strip()
    elif rec.api_key_env:
        api_key = os.environ.get(rec.api_key_env, "").strip()
    return {
        "slug": rec.slug,
        "kind": rec.kind,
        "protocol": rec.protocol,
        "endpoint": rec.endpoint,
        "model": rec.model,
        "api_key": api_key,
        "api_key_source": (
            "direct" if rec.api_key
            else ("env" if rec.api_key_env and api_key else "")
        ),
        "headers": dict(rec.headers or {}),
        "timeout_s": rec.timeout_s,
    }


def _check_worker_secret(provided: str) -> None:
    """Reject unauthenticated callers when WORKER_SECRET is configured.

    No-op when the deployment hasn't set a secret (single-tenant LAN
    install). Mirrors the existing check the worker WS handshake uses.
    """
    if not config.worker_secret:
        return
    if (provided or "").strip() != config.worker_secret:
        raise HTTPException(401, "bad worker secret")


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@router.get("/engines")
async def list_engines() -> dict:
    """List every registered AI engine.

    Sorted: built-in first, then by kind (chat / vision-chat /
    gui-agent), then alphabetically by slug. API key VALUES are
    never returned -- only the env-var name and a flag indicating
    whether that env is currently set on the hub."""
    er = _require_engines()
    items = [_engine_to_dict(r) for r in er.list_all()]
    return {"count": len(items), "engines": items}


@router.get("/engines/{slug}")
async def get_engine(slug: str) -> dict:
    er = _require_engines()
    rec = er.get(slug)
    if rec is None:
        raise HTTPException(404, f"engine '{slug}' not found")
    return _engine_to_dict(rec)


@router.put("/engines/{slug}")
async def upsert_engine(slug: str, body: dict) -> dict:
    """Create or update an engine record.

    Built-in engines cannot have their ``slug`` / ``kind`` / ``protocol``
    field rewritten via this endpoint -- those are seed-controlled.
    Endpoint / model / api_key_env / headers / timeout_s / promoted /
    notes ARE editable so operators can point the seeds at their own
    LAN hosts."""
    er = _require_engines()
    body = body or {}
    slug = _engine_normalise_slug(slug)
    existing = er.get(slug)

    # ``api_key`` body convention:
    #   missing key     -> keep existing value (don't wipe by accident)
    #   "" empty string -> explicitly clear stored direct key
    #   non-empty       -> store the new value
    # The admin UI sends "" when the operator left the password field
    # blank on a NEW engine (= no direct key) but doesn't send the
    # field at all when editing without changing the key.
    if "api_key" in body:
        new_api_key = str(body.get("api_key") or "")
    elif existing is not None:
        new_api_key = existing.api_key
    else:
        new_api_key = ""

    # All engines are now operator-managed (the auto-seed of qwen /
    # qwen-chat / cogagent was removed). slug / kind / protocol are
    # fully editable; ``builtin`` is a legacy marker that no longer
    # restricts anything but is preserved on disk for backwards compat.
    rec = EngineRecord.from_json({
        **body, "slug": slug, "api_key": new_api_key,
    })
    if not rec.name.strip():
        rec.name = slug
    if rec.kind not in ("chat", "vision-chat", "reasoning"):
        raise HTTPException(
            400,
            "kind must be one of: chat, vision-chat, reasoning",
        )
    if rec.protocol not in (
        "openai", "anthropic", "agent-service",
    ):
        raise HTTPException(
            400,
            "protocol must be one of: openai, anthropic, agent-service",
        )
    if not rec.endpoint.strip():
        raise HTTPException(400, "endpoint cannot be empty")

    # Catch the most common UX trap: operator pasted a long API key
    # into the env-VAR-NAME field. Fail loudly instead of silently
    # writing the secret to disk.
    if rec.api_key_env and _looks_like_secret(rec.api_key_env):
        raise HTTPException(
            400,
            "api_key_env looks like an API key, not an env var name. "
            "Use the 'API key (direct)' field for the literal value, or "
            "the env-var name (e.g. OPENAI_API_KEY) for env reference.",
        )

    # Normalise endpoint: strip path suffixes the worker code adds on
    # its own (e.g. ``/v1/chat/completions``). Prevents the
    # ``.../v1/chat/completions/v1/chat/completions`` double-path
    # mistake when an operator pastes a full URL from the vendor's docs.
    rec.endpoint = _normalise_engine_endpoint(rec.endpoint, rec.protocol)
    if not rec.endpoint:
        raise HTTPException(400, "endpoint is empty after normalisation")

    saved = er.upsert(rec)
    await _mdb_upsert(saved)
    return _engine_to_dict(saved)


@router.delete("/engines/{slug}")
async def delete_engine(slug: str) -> dict:
    er = _require_engines()
    try:
        ok = er.delete(slug)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"engine '{slug}' not found")
    canonical = _engine_normalise_slug(slug)
    await _mdb_delete(canonical)
    return {"deleted": True, "slug": canonical}


@router.post("/engines/{slug}/promote")
async def promote_engine(slug: str) -> dict:
    """Mark this engine as the default for ``engine="auto"`` of its
    kind. Demotes any previously-promoted entry of the same kind so
    there's never more than one promoted per kind."""
    er = _require_engines()
    rec = er.get(slug)
    if rec is None:
        raise HTTPException(404, f"engine '{slug}' not found")
    # Demote any other promoted-of-same-kind first. Mirror each
    # demotion to MariaDB so the "one promoted per kind" invariant
    # holds even after a hub restart restores from MariaDB.
    for other in er.list_all():
        if other.slug != rec.slug and other.kind == rec.kind and other.promoted:
            demoted = er.set_promoted(other.slug, False)
            if demoted is not None:
                await _mdb_upsert(demoted)
    saved = er.set_promoted(rec.slug, True)
    await _mdb_upsert(saved)
    return _engine_to_dict(saved)


@router.post("/engines/{slug}/demote")
async def demote_engine(slug: str) -> dict:
    er = _require_engines()
    rec = er.get(slug)
    if rec is None:
        raise HTTPException(404, f"engine '{slug}' not found")
    saved = er.set_promoted(rec.slug, False)
    await _mdb_upsert(saved)
    return _engine_to_dict(saved)


@router.post("/engines/{slug}/resolve")
async def resolve_engine(slug: str, body: Optional[dict] = None) -> dict:
    """Worker-internal endpoint: return the full engine config with
    the API key resolved, ready to use.

    This is what worker code calls from ``page.ask()`` / future
    ``page.agent(engine=...)`` paths so the secret never leaves the
    hub except over an authenticated link. Gated by ``WORKER_SECRET``
    when one is configured.

    The public ``GET /engines/{slug}`` deliberately omits the API key
    value (only ``api_key_set: bool``); use this endpoint when you
    need to actually call the engine.

    body: ``{"secret": "..."}`` -- required iff hub has WORKER_SECRET.
    """
    body = body or {}
    _check_worker_secret(str(body.get("secret") or ""))
    er = _require_engines()
    rec = er.get(slug)
    if rec is None:
        raise HTTPException(404, f"engine '{slug}' not found")
    return _resolve_engine_payload(rec)


@router.post("/engines/auto/{kind}/resolve")
async def resolve_engine_auto(kind: str, body: Optional[dict] = None) -> dict:
    """Same shape as ``/engines/{slug}/resolve`` but picks the
    promoted engine of ``kind`` (or first non-promoted) automatically.

    Returns 404 when no engine of that kind exists. Workers use this
    for ``engine="auto"`` -- ``page.ask(engine="auto")`` resolves to
    the operator's chosen default chat engine.
    """
    body = body or {}
    _check_worker_secret(str(body.get("secret") or ""))
    if kind not in ("chat", "vision-chat", "reasoning"):
        raise HTTPException(
            400, "kind must be one of: chat, vision-chat, reasoning",
        )
    er = _require_engines()
    rec = er.pick_for_kind(kind)
    if rec is None:
        raise HTTPException(404, f"no engine of kind '{kind}' registered")
    return _resolve_engine_payload(rec)


@router.post("/engines/{slug}/test")
async def test_engine(slug: str) -> dict:
    """Lightweight connectivity check. Sends a tiny prompt to the
    engine's endpoint and reports whether we got a usable response
    back. Doesn't validate the model name or auth scopes -- just
    "can the worker reach this URL with these creds".

    Uses the same ``resolve_engine_target`` + ``adapt_chat_body``
    helpers as the codegen-loop, so the test path tolerates the
    same operator quirks (api_key pasted in api_key_env, gpt-5+
    parameter renames). Without this, the admin UI's Test button
    failed even when actual codegen jobs succeeded.
    """
    from server.hub.codegen import (
        adapt_chat_body as _adapt_chat_body,
        resolve_engine_target as _resolve_engine_target,
    )
    er = _require_engines()
    rec = er.get(slug)
    if rec is None:
        raise HTTPException(404, f"engine '{slug}' not found")

    t0 = _time.monotonic()
    # For openai-protocol engines, defer to the shared resolver --
    # it handles the literal-key-in-env-var-field heuristic, the
    # already-includes-chat-completions URL case, and the Bearer
    # header assembly. The other protocols (agent-service,
    # cogagent) keep the legacy path because they don't go
    # through the chat-completions API at all.
    if rec.protocol == "openai":
        try:
            tgt = _resolve_engine_target(slug, er)
        except Exception as e:
            return {
                "ok": False,
                "elapsed_ms": int((_time.monotonic() - t0) * 1000),
                "error": (
                    f"engine resolution failed: {type(e).__name__}: {e}"
                ),
            }
        # If the resolver couldn't recover an Authorization header
        # (no api_key + no resolvable api_key_env), surface the
        # specific reason instead of letting the API return 401.
        has_auth = any(
            k.lower() == "authorization" for k in (tgt.headers or {})
        )
        if not has_auth and rec.api_key_env and not rec.api_key:
            return {
                "ok": False,
                "elapsed_ms": int((_time.monotonic() - t0) * 1000),
                "error": (
                    f"api_key_env {rec.api_key_env!r} is not set on the "
                    f"hub; either define that env var + restart the hub, "
                    f"OR move the literal key from api_key_env into the "
                    f"'API key (direct)' field"
                ),
            }
        body = _adapt_chat_body(tgt, {
            "model": tgt.model or "gpt-3.5-turbo",
            "messages": [
                {"role": "user", "content": "Reply with the word pong only."},
            ],
            "max_tokens": 4,
            "temperature": 0,
        })
        try:
            async with httpx.AsyncClient(
                timeout=min(int(tgt.timeout or 60), 30),
            ) as cli:
                r = await cli.post(
                    tgt.url,
                    headers={**tgt.headers, "Content-Type": "application/json"},
                    json=body,
                )
        except Exception as e:
            return {
                "ok": False,
                "elapsed_ms": int((_time.monotonic() - t0) * 1000),
                "error": (
                    f"request failed: {type(e).__name__}: {e}"
                ),
            }
        elapsed_ms = int((_time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return {
                "ok": False,
                "elapsed_ms": elapsed_ms,
                "error": (
                    f"HTTP {r.status_code}: {r.text[:300]}"
                ),
            }
        # Best-effort response body sniff for the "reply" content.
        try:
            payload = r.json()
            content = (
                payload.get("choices", [{}])[0]
                .get("message", {})
                .get("content")
                or ""
            )
        except Exception:
            content = ""
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "reply": (content or "(empty)")[:120],
            "url": tgt.url,
            "model": tgt.model,
        }

    # Non-openai protocols (agent-service / cogagent / anthropic):
    # keep the legacy auth resolution path. They don't share the
    # chat-completions wire format so they can't reuse the codegen
    # adapter.
    api_key = ""
    if rec.api_key:
        api_key = rec.api_key.strip()
    elif rec.api_key_env:
        api_key = os.environ.get(rec.api_key_env, "").strip()
        if not api_key:
            return {
                "ok": False,
                "elapsed_ms": 0,
                "error": (
                    f"api_key_env '{rec.api_key_env}' is not set on the hub; "
                    f"set it in .env + restart the hub, or paste the key "
                    f"into the 'API key (direct)' field instead"
                ),
            }

    headers = dict(rec.headers or {})
    if api_key:
        # OpenAI / Anthropic / most APIs accept Bearer; Anthropic also
        # accepts x-api-key. We send Bearer by default and let the
        # operator add x-api-key via headers if needed.
        headers.setdefault("Authorization", f"Bearer {api_key}")

    base = rec.endpoint.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=min(rec.timeout_s, 15)) as cli:
            if rec.protocol == "openai":
                # Unreachable: handled by the resolver branch above.
                # Kept for code shape; protocol can only equal one.
                r = await cli.post(
                    f"{base}/v1/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": rec.model or "gpt-3.5-turbo",
                        "messages": [
                            {"role": "user", "content": "Reply with the word pong only."},
                        ],
                        "max_tokens": 4,
                        "temperature": 0,
                    },
                )
            elif rec.protocol == "anthropic":
                # Not natively supported yet -- v1 routes Claude
                # through LiteLLM (= openai protocol). Surface a
                # friendly error so the operator knows.
                return {
                    "ok": False,
                    "elapsed_ms": int((_time.monotonic() - t0) * 1000),
                    "error": (
                        "anthropic protocol is reserved for v2; "
                        "for now use protocol=openai with a LiteLLM "
                        "or other OpenAI-compat proxy"
                    ),
                }
            elif rec.protocol == "agent-service":
                # The bundled agent_service exposes /health.
                r = await cli.get(f"{base}/health", headers=headers)
            else:
                return {
                    "ok": False,
                    "elapsed_ms": int((_time.monotonic() - t0) * 1000),
                    "error": f"unknown protocol: {rec.protocol}",
                }
        elapsed = int((_time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return {
                "ok": False,
                "elapsed_ms": elapsed,
                "status_code": r.status_code,
                "error": f"HTTP {r.status_code}: {r.text[:300]}",
            }
        return {
            "ok": True,
            "elapsed_ms": elapsed,
            "status_code": r.status_code,
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed_ms": int((_time.monotonic() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }
