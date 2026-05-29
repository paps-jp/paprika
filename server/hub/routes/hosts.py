"""Per-host cookie + auto-login + visited-URL routes.

Three logical sub-feature sets share the ``/hosts/{host}/...`` URL
namespace:

* Cookie registry (auto-injected into HubSessionStart): list / GET /
  PUT / DELETE / Netscape export.
* Auto re-login recipe: PUT login_recipe, POST relogin. The actual
  ``_ensure_host_login`` helper stays in app.py for now (it's also
  called from the job runner's pre-fetch hook); this module's
  ``relogin`` route lazy-imports it to avoid the cycle.
* Per-host visited URL set: GET / POST / DELETE / match_counts.

``_require_hosts`` is exposed at module scope so app.py and (until
#2B-F migrates them) session routes can re-export it via plain import.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from server.hub._state import config, get_storage_dir, state
from server.hub.host_visited import HostVisitedRegistry
from server.hub.hosts import HostRegistry, _normalise_host

log = logging.getLogger(__name__)

router = APIRouter(tags=["Hosts"])


# ----------------------------------------------------------------------------
# Helpers (re-exported from app.py for backwards compat with session-route
# code that still references _require_hosts directly).
# ----------------------------------------------------------------------------


def _require_hosts() -> HostRegistry:
    if state.hosts is None:
        raise HTTPException(503, "host registry not initialised")
    return state.hosts


def _require_host_visited() -> HostVisitedRegistry:
    if state.host_visited is None:
        raise HTTPException(503, "host visited registry not initialised")
    return state.host_visited


def _host_to_dict(rec, *, include_visited_count: bool = False) -> dict:
    """Render a HostRecord for the API. ``cookie_count`` saves the UI a
    map+len when listing dozens of hosts."""
    d = {
        "host": rec.host,
        "cookies": rec.cookies,
        "cookie_count": len(rec.cookies or []),
        "notes": rec.notes,
        "recrawl_patterns": list(rec.recrawl_patterns or []),
        "popup_policy": rec.popup_policy or "kill",
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
        "last_used_at": rec.last_used_at,
        # Auto re-login recipe (login_goal omitted from listings to
        # avoid spraying credentials across logs / the admin UI; the
        # has_login_recipe flag is enough to show "configured").
        "has_login_recipe": rec.has_login_recipe,
        "login_url": rec.login_url,
        "login_check": rec.login_check,
        "login_refresh_ttl_s": rec.login_refresh_ttl_s,
        "last_login_at": rec.last_login_at,
        # Pre-baked per-host Fetch playbooks (HostRecord.fetch_recipes).
        # Each entry: {pattern, description, actions, ...}. See
        # server/hub/hosts.py:HostRecipe. Empty list when none.
        "fetch_recipes": [
            r.to_json() if hasattr(r, "to_json") else r
            for r in (rec.fetch_recipes or [])
        ],
    }
    if include_visited_count and state.host_visited is not None:
        try:
            d["visited_count"] = state.host_visited.count(rec.host)
        except Exception:
            d["visited_count"] = 0
    return d


# ----------------------------------------------------------------------------
# Cookie registry CRUD
# ----------------------------------------------------------------------------


@router.get("/hosts")
async def list_hosts(
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """List registered hosts with optional substring search and
    pagination.

    Query params:
      * ``q``       -- case-insensitive substring match against host
                       name and notes. Empty = no filter.
      * ``offset``  -- skip this many entries (default 0).
      * ``limit``   -- max entries to return (default 50, max 500).

    Returns ``{total, count, offset, limit, q, hosts}``. The cookies
    array is omitted on each item to keep the response small.
    """
    reg = _require_hosts()
    all_recs = reg.list_all()
    if q:
        ql = q.lower()
        all_recs = [
            r for r in all_recs if ql in (r.host or "").lower() or ql in (r.notes or "").lower()
        ]
    total = len(all_recs)
    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or 50), 500))
    page = all_recs[off : off + lim]
    items = []
    for r in page:
        d = _host_to_dict(r, include_visited_count=True)
        d.pop("cookies", None)
        items.append(d)
    return {
        "total": total,
        "count": len(items),
        "offset": off,
        "limit": lim,
        "q": q or "",
        "hosts": items,
    }


@router.get("/hosts/{host}")
async def get_host(host: str) -> dict:
    reg = _require_hosts()
    rec = reg.get(host)
    if rec is None:
        raise HTTPException(404, f"host '{host}' not registered")
    return _host_to_dict(rec)


# v2 Phase 2: HostKnowledge read-only endpoints.
# Files are produced by scripts/migrate_to_v2.py and (later) updated by
# the R1 Distiller. Until Phase 4 plumbs them into actual job execution
# they are useful for operators to verify the migration outcome and to
# build admin-UI tiles around them.

@router.get("/host_knowledge")
async def list_host_knowledge() -> dict:
    """List all hosts that have an applied HostKnowledge file.

    Returns ``{"count": N, "hosts": ["example.com", ...]}``. Skips
    hosts that exist only in the v1 ``hosts/`` tree but had no
    meaningful knowledge to migrate (operator can still see them
    via ``/hosts``).
    """
    knowledge_dir = config.data_dir / "host_knowledge"
    if not knowledge_dir.is_dir():
        return {"count": 0, "hosts": []}
    hosts: list[str] = []
    for f in sorted(knowledge_dir.glob("*.json")):
        hosts.append(f.stem)
    return {"count": len(hosts), "hosts": hosts}


@router.get("/hosts/{host}/knowledge")
async def get_host_knowledge(host: str) -> dict:
    """Return the v2 HostKnowledge object for ``host``.

    404 if no migrated/learned knowledge exists yet (the host may still
    be visible via ``/hosts/{host}`` for its cookies/recipes).

    The response is the raw JSON file on disk; it conforms to the
    ``core.host_knowledge.HostKnowledge`` schema. Read-only at this
    phase -- Phase 4 will introduce PUT/PATCH when R1 starts writing.
    """
    import json as _json
    normalised = _normalise_host(host)
    knowledge_dir = config.data_dir / "host_knowledge"
    path = knowledge_dir / f"{normalised}.json"
    if not path.is_file():
        raise HTTPException(
            404,
            f"host_knowledge for '{normalised}' not found "
            f"(run scripts/migrate_to_v2.py --apply if v1 data exists)",
        )
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"host_knowledge file unreadable: {e}")


# v2 Phase 7: Plugin Registry endpoints.
# Lets operators see what tools are installed, what capabilities they
# advertise, and invoke them manually for testing.  Write operations
# (install / uninstall / disable) come later -- for now the registry
# is operator-edited on disk (data/tools/installed/) and the hub just
# reads it.


@router.get("/admin/plugins")
async def list_plugins_endpoint() -> dict:
    """List all installed plugins with their declared actions."""
    from server.hub.plugins import list_plugins, TOOLS_DIR
    return {
        "tools_dir": str(TOOLS_DIR),
        "plugins": list_plugins(),
    }


@router.get("/admin/plugin_catalog")
async def get_plugin_catalog() -> dict:
    """Return the merged plugin catalog (catalog.json + install status).

    Drives the Plugins admin tab's "All plugins" view. Each entry carries
    both the catalog metadata (summary / category / source / capabilities)
    AND the live install state (installed: bool, installed_version, actions).
    Plugins installed but not in catalog.json are appended with
    in_catalog: false so operator-side experiments still show up.
    """
    from server.hub.plugins import load_catalog, merged_catalog, TOOLS_DIR
    cat = load_catalog()
    return {
        "tools_dir":  str(TOOLS_DIR),
        "version":    cat.get("version"),
        "updated_at": cat.get("updated_at"),
        "plugins":    merged_catalog(),
    }


@router.post("/admin/plugins/{name}/invoke")
async def invoke_plugin_endpoint(name: str, body: dict) -> dict:
    """Manually invoke a plugin action. Body::

        { "action": "get_cookies",
          "params": { "url": "...", "delay_s": 10 } }

    Returns the plugin's structured result, or HTTP 4xx/5xx on
    PluginNotAvailable / PluginInvocationError.
    """
    from server.hub.plugins import (
        invoke_plugin,
        PluginNotAvailable,
        PluginInvocationError,
    )
    action = body.get("action") or ""
    params = body.get("params") or {}
    if not action:
        raise HTTPException(400, "'action' is required")
    try:
        result = await invoke_plugin(
            name, action, params,
            audit_context={"source": "admin_ui"},
        )
        return {"ok": True, "result": result}
    except PluginNotAvailable as e:
        raise HTTPException(404, str(e))
    except PluginInvocationError as e:
        raise HTTPException(500, str(e))


@router.get("/admin/plugins/invocations")
async def list_plugin_invocations(limit: int = 50) -> dict:
    """Return the tail of invocations.jsonl for the admin audit view."""
    import json as _json
    from server.hub.plugins import TOOLS_DIR
    path = TOOLS_DIR / "invocations.jsonl"
    if not path.is_file():
        return {"count": 0, "invocations": []}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        raise HTTPException(500, f"could not read invocations.jsonl: {e}")
    tail = lines[-max(1, int(limit)):]
    entries: list[dict] = []
    for ln in reversed(tail):  # newest first
        try:
            entries.append(_json.loads(ln))
        except Exception:
            continue
    return {"count": len(entries), "invocations": entries}


@router.delete("/admin/plugins/invocations")
async def clear_plugin_invocations() -> dict:
    """Wipe the invocations.jsonl audit log.

    Used by the Plugins admin tab's "delete all" button when the
    operator wants to start fresh. The file is truncated (not removed)
    so subsequent writes don't need to recreate parent dirs.
    """
    from server.hub.plugins import TOOLS_DIR
    path = TOOLS_DIR / "invocations.jsonl"
    if not path.is_file():
        return {"deleted": 0}
    try:
        # Count first so the response is meaningful for the operator.
        n = sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
        path.write_text("", encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"could not clear invocations.jsonl: {e}")
    return {"deleted": n}


# Judge comparison analysis.
# Aggregates default judge.json vs reasoning judge verdicts across all
# codegen-loop attempts so operators can see how often the two agree,
# and inspect the disagreement cases. Used while running in shadow mode
# to decide when (or whether) to switch to primary.


@router.get("/admin/judge_comparisons")
async def get_judge_comparisons(limit: int = 50) -> dict:
    """Aggregate default vs reasoning judge verdicts across all jobs.

    Walks ``data/jobs/*/attempts/*/`` and pairs up ``judge.json``
    (default) with ``judge_reasoning.json`` or ``judge_r1.json``
    (reasoning judge). Returns:

      * counts:       agree / disagree / r1_only / legacy_only / both_missing
      * disagree_rate: float (when both ran)
      * samples:      up to ``limit`` recent disagreements with the
                       two verdicts side-by-side, for human inspection

    Read-only. Cheap (just scans files); no LLM calls.
    """
    import json as _json
    from datetime import datetime
    jobs_dir = get_storage_dir()
    if not jobs_dir.is_dir():
        return {
            "counts": {},
            "disagree_rate": 0.0,
            "samples": [],
        }

    agree = 0
    disagree = 0
    r1_only = 0
    legacy_only = 0
    both_missing = 0
    disagreements: list[dict] = []

    for job_path in jobs_dir.iterdir():
        if not job_path.is_dir():
            continue
        attempts_dir = job_path / "attempts"
        if not attempts_dir.is_dir():
            continue
        job_id = job_path.name
        for att_path in attempts_dir.iterdir():
            if not att_path.is_dir():
                continue
            legacy_file = att_path / "judge.json"
            # Support both new and legacy reasoning judge filenames
            r1_file = att_path / "judge_reasoning.json"
            if not r1_file.is_file():
                r1_file = att_path / "judge_r1.json"
            has_legacy = legacy_file.is_file()
            has_r1 = r1_file.is_file()
            if has_legacy and has_r1:
                try:
                    lj = _json.loads(legacy_file.read_text(encoding="utf-8"))
                    rj = _json.loads(r1_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                lsat = bool(lj.get("satisfied"))
                rsat = bool(rj.get("satisfied"))
                if lsat == rsat:
                    agree += 1
                else:
                    disagree += 1
                    # capture the disagreement; we'll trim to `limit`
                    # below.  Use job_path.stat().st_mtime as a rough
                    # "recency" proxy.
                    try:
                        mtime = r1_file.stat().st_mtime
                    except Exception:
                        mtime = 0
                    disagreements.append({
                        "job_id": job_id,
                        "attempt": att_path.name,
                        "mtime": mtime,
                        "legacy": {
                            "satisfied": lsat,
                            "reason": (lj.get("reason") or "")[:240],
                            "model":  lj.get("model"),
                        },
                        "r1": {
                            "satisfied": rsat,
                            "reason": (rj.get("reason") or "")[:240],
                            "model":  rj.get("model"),
                        },
                    })
            elif has_r1:
                r1_only += 1
            elif has_legacy:
                legacy_only += 1
            else:
                both_missing += 1

    disagreements.sort(key=lambda d: -d["mtime"])
    samples = disagreements[: max(0, int(limit))]
    # Drop the internal mtime field from the response.
    for d in samples:
        d.pop("mtime", None)

    total_paired = agree + disagree
    disagree_rate = (disagree / total_paired) if total_paired else 0.0

    return {
        "counts": {
            "agree":         agree,
            "disagree":      disagree,
            "r1_only":       r1_only,
            "legacy_only":   legacy_only,
            "both_missing":  both_missing,
            "total_paired":  total_paired,
        },
        "disagree_rate": round(disagree_rate, 4),
        "samples": samples,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


@router.put("/hosts/{host}")
async def put_host(host: str, body: dict) -> dict:
    """Create or update the record for ``host``. Body::

        {
          "cookies": [ {name, value, domain, path, ...}, ... ],
          "notes": "optional operator memo",
          "recrawl_patterns": ["https://www.example.com/index*", ...]
        }

    Hostname is normalised (lowercase + strip ``www.``) so
    ``example.com`` and ``www.example.com`` map to the same record.

    ``recrawl_patterns`` (optional) is a list of fnmatch / glob-style
    URL patterns (``*`` = any chars, ``?`` = one char) that the
    walker treats as "always re-crawl, ignore the visited set". Use
    for frontier pages (index, category listings, sitemaps).
    Omitting the field preserves the existing patterns; passing
    ``[]`` clears them.

    Returns the saved HostRecord."""
    reg = _require_hosts()
    body = body or {}
    cookies = body.get("cookies")
    notes = body.get("notes")
    recrawl_patterns = body.get("recrawl_patterns")
    popup_policy = body.get("popup_policy")
    fetch_recipes = body.get("fetch_recipes")
    if cookies is not None and not isinstance(cookies, list):
        raise HTTPException(400, "'cookies' must be a list of CDP CookieParam-shaped dicts")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(400, "'notes' must be a string")
    if recrawl_patterns is not None:
        if not isinstance(recrawl_patterns, list) or not all(
            isinstance(p, str) for p in recrawl_patterns
        ):
            raise HTTPException(400, "'recrawl_patterns' must be a list of strings")
    if popup_policy is not None:
        if not isinstance(popup_policy, str) or popup_policy not in ("kill", "follow"):
            raise HTTPException(400, "'popup_policy' must be 'kill' or 'follow'")
    if fetch_recipes is not None:
        if not isinstance(fetch_recipes, list) or not all(
            isinstance(r, dict) for r in fetch_recipes
        ):
            raise HTTPException(
                400,
                "'fetch_recipes' must be a list of recipe dicts "
                "(each shaped {pattern, actions, description, ...})",
            )
    try:
        rec = reg.upsert(
            host=host,
            cookies=cookies or [],
            notes=notes,
            recrawl_patterns=recrawl_patterns,
            popup_policy=popup_policy,
            fetch_recipes=fetch_recipes,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _host_to_dict(rec, include_visited_count=True)


@router.post("/hosts/{host}/recipes")
async def append_host_recipe(host: str, body: dict) -> dict:
    """Append a single :class:`HostRecipe` to ``host``'s recipe list.

    Body shape (everything except ``actions`` is optional; ``actions``
    or ``code`` or ``goal`` should be provided -- the recipe is
    useless without at least one of them, but the server doesn't
    enforce that, leaving room for placeholder/disabled recipes)::

        {
          "pattern":       "/frame*",
          "description":   "kick play button",
          "actions":       [...],
          "code":          "...",
          "goal":          "...",
          "created_from_job": "<jobid>",
          "created_by":    "ai" | "operator"
        }

    Creates the host record if it doesn't exist yet -- an AI-
    investigated host might not be registered when the recipe lands.

    Returns the updated host record (same shape as GET /hosts/{host}).
    """
    body = body or {}
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    # Minimal sanity: a recipe with no actions / code / goal is OK
    # (operator might be staging a placeholder) but actions must be
    # well-shaped if present so the worker doesn't crash on dispatch.
    actions = body.get("actions")
    if actions is not None:
        if not isinstance(actions, list) or not all(
            isinstance(a, dict) for a in actions
        ):
            raise HTTPException(
                400,
                "'actions' must be a list of action dicts",
            )
    reg = _require_hosts()
    try:
        rec = reg.append_recipe(host, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _host_to_dict(rec, include_visited_count=True)


@router.delete("/hosts/{host}")
async def delete_host(host: str) -> dict:
    reg = _require_hosts()
    ok = reg.delete(host)
    if not ok:
        raise HTTPException(404, f"host '{host}' not registered")
    # Also wipe the visited URL file -- keeping it after the host
    # record is gone would just produce orphan state.
    if state.host_visited is not None:
        try:
            state.host_visited.delete_host(host)
        except Exception:
            pass
    return {"host": _normalise_host(host), "deleted": True}


# ----------------------------------------------------------------------------
# Auto re-login (recipe CRUD + force-trigger)
# ----------------------------------------------------------------------------


@router.put("/hosts/{host}/login_recipe")
async def put_host_login_recipe(host: str, body: dict) -> dict:
    """Configure (or clear) the auto re-login recipe for a host.

    Body::

        {
          "login_url":   "https://market.laxd.com/item/XXX/",
          "login_goal":  "Enter email ... and password ..., click login.",
          "login_check": "login.",      // substring => still logged OUT
          "login_refresh_ttl_s": 900    // re-login before a fetch only when
                                        // the last login is older than this
        }

    Pass ``"login_goal": ""`` to clear the recipe (disables auto-login).
    The recipe is stored plaintext alongside the cookies (LAN-trusted).
    """
    body = body or {}
    reg = _require_hosts()
    rec = reg.get(host)
    cookies = list(rec.cookies) if rec else []
    try:
        rec = reg.upsert(
            host=host,
            cookies=cookies,
            login_url=body.get("login_url"),
            login_goal=body.get("login_goal"),
            login_check=body.get("login_check"),
            login_refresh_ttl_s=body.get("login_refresh_ttl_s"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "host": rec.host,
        "login_url": rec.login_url,
        "login_check": rec.login_check,
        "login_refresh_ttl_s": rec.login_refresh_ttl_s,
        "has_login_recipe": rec.has_login_recipe,
        "last_login_at": rec.last_login_at,
    }


@router.post("/hosts/{host}/relogin")
async def relogin_host(host: str) -> dict:
    """Force an immediate re-login for ``host`` (ignores the TTL gate).
    Use to set up / test a recipe, or wire to a cron to keep a session
    warm. Returns the _ensure_host_login status dict.
    """
    if state.hosts is None or state.hosts.get(_normalise_host(host)) is None:
        raise HTTPException(404, f"host '{host}' not registered")
    # Lazy import: _ensure_host_login still lives in app.py because the
    # job runner's pre-fetch hook also calls it. Once that hook moves
    # (#2B-G), the function can migrate here too.
    from server.hub.app import _ensure_host_login

    return await _ensure_host_login(host, force=True)


@router.get("/hosts/{host}/cookies.txt")
async def get_host_cookies_netscape(host: str):
    """Return the host's stored cookies as a Netscape cookies.txt.

    Used by the worker's ``download_video`` handler to authenticate
    yt-dlp against sites whose video manifest is login-gated (X /
    Twitter being the motivating case). The worker GETs this for
    the target video URL's host, writes it to a temp file, and
    passes ``--cookies <file>`` to yt-dlp.

    Why this path instead of ``--cookies-from-browser``: the
    operator's real cookies are pushed to the registry as plaintext
    via the Paprika Bridge extension (chrome.cookies API), which
    works regardless of Chrome 127+ App-Bound (v20) encryption.
    Reading the worker's on-disk Chrome cookies directly would hit
    the same v20 wall.

    Returns an empty body (200) when the host has no cookies, so the
    worker can treat "no cookies" and "host not registered" the same
    (= run yt-dlp without auth).
    """
    reg = _require_hosts()
    rec = reg.get(host)
    if rec is None or not rec.cookies:
        return PlainTextResponse("", media_type="text/plain")
    from server.hub.hosts import cookies_to_netscape

    body = cookies_to_netscape(rec.cookies, fallback_host=_normalise_host(host))
    try:
        reg.touch_used(_normalise_host(host))
    except Exception:
        pass
    return PlainTextResponse(body, media_type="text/plain")


# ----------------------------------------------------------------------------
# Per-host visited URL set
# ----------------------------------------------------------------------------


@router.get("/hosts/{host}/visited")
async def list_host_visited(
    host: str,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Page through the host's visited URL set with optional
    substring search.

    Returns ``{host, total, count, offset, limit, q, urls}`` where
    ``urls`` is a list of ``{url, hash}`` (use ``hash`` for the
    DELETE-by-id endpoint).
    """
    reg = _require_host_visited()
    return reg.page(host, q=q, offset=offset, limit=limit)


@router.post("/hosts/{host}/visited")
async def add_host_visited(host: str, body: dict) -> dict:
    """Append URL(s) to the host's visited set. Body accepts either::

        {"url": "https://..."}              # single
        {"urls": ["https://...", ...]}       # batch

    Idempotent: duplicates within the body and against the existing
    set are dropped. Returns ``{host, added, total}``.
    """
    reg = _require_host_visited()
    body = body or {}
    single = body.get("url")
    many = body.get("urls")
    if single is None and many is None:
        raise HTTPException(400, "body must contain 'url' or 'urls'")
    if single is not None:
        if not isinstance(single, str):
            raise HTTPException(400, "'url' must be a string")
        urls_in = [single]
    else:
        if not isinstance(many, list) or not all(isinstance(u, str) for u in many):
            raise HTTPException(400, "'urls' must be a list of strings")
        urls_in = many
    added = reg.add_many(host, urls_in)
    return {
        "host": _normalise_host(host),
        "added": added,
        "total": reg.count(host),
    }


@router.delete("/hosts/{host}/visited")
async def clear_host_visited(host: str) -> dict:
    """Wipe the host's visited set. Returns ``{host, cleared}``."""
    reg = _require_host_visited()
    n = reg.clear(host)
    return {"host": _normalise_host(host), "cleared": n}


@router.delete("/hosts/{host}/visited/{sha}")
async def remove_host_visited(host: str, sha: str) -> dict:
    """Remove one URL by its SHA hash (use the value returned by
    GET /hosts/{host}/visited)."""
    reg = _require_host_visited()
    removed = reg.remove_by_hash(host, sha)
    if removed is None:
        raise HTTPException(404, f"no visited URL with hash {sha!r}")
    return {
        "host": _normalise_host(host),
        "removed": removed,
        "total": reg.count(host),
    }


@router.post("/hosts/{host}/visited/match_counts")
async def host_visited_match_counts(host: str, body: dict) -> dict:
    """For each pattern in ``body["patterns"]``, return how many of
    the host's visited URLs would match it (glob with ``*``/``?``).

    Used by the admin UI's pattern editor to live-flag patterns that
    don't actually match anything in the visited set (typo warning).

    Returns ``{patterns, counts, total}`` where ``total`` is the
    host's overall visited URL count.
    """
    reg = _require_host_visited()
    body = body or {}
    patterns = body.get("patterns") or []
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise HTTPException(400, "'patterns' must be a list of strings")
    counts = reg.match_counts(host, patterns)
    return {
        "host": _normalise_host(host),
        "patterns": patterns,
        "counts": counts,
        "total": reg.count(host),
    }


# ============================================================================
# Auto re-login helpers (#2B-H)
# ----------------------------------------------------------------------------
# Used by the /hosts/{host}/relogin route above (lazy-imports the
# implementation from app.py historically; now resolves locally) AND by
# the create_job pre-fetch hook in routes/jobs.py (also via app.py's
# re-export chain). Both keep working unchanged thanks to the
# re-export.
# ============================================================================

import asyncio

from fastapi import HTTPException

from server.hub.routes.sessions import (
    _send_session_action,
    close_session,
    create_session,
    session_agent,
    session_save_cookies_to_host,
)

# ---- auto re-login -------------------------------------------------------


async def _session_state_dict(session_id: str) -> dict:
    """Fetch a session's current {url, title, ...} via the worker."""
    try:
        out = await _send_session_action(
            session_id,
            {"kind": "state"},
            timeout=15.0,
        )
        r = out.get("result")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


async def _ensure_host_login(host: str, *, force: bool = False) -> dict:
    """Re-authenticate ``host`` if its stored login session looks dead.

    Drives the host's login recipe in a throwaway session:
      1. open a session to ``login_url`` (current cookies auto-injected)
      2. read the landing URL/title -- if ``login_check`` is NOT present
         we're still logged in, just refresh cookies + stamp.
      3. otherwise run ``page.agent(login_goal)``, re-check, and on
         success save the full cookie jar + stamp ``last_login_at``.
    Always tears the session down.

    Returns a status dict: ``{host, relogin: ok|skipped|failed, ...}``.
    Never raises -- callers (pre-fetch hook, cron) treat a failure as
    "proceed with whatever cookies we have".
    """
    h = _normalise_host(host)
    rec = state.hosts.get(h) if state.hosts is not None else None
    if rec is None or not rec.has_login_recipe:
        return {"host": h, "relogin": "skipped", "reason": "no login recipe"}
    if not force and not state.hosts.is_login_stale(h):
        return {"host": h, "relogin": "skipped", "reason": "fresh (within ttl)"}

    login_url = (rec.login_url or "").strip() or f"https://{h}/"
    check = (rec.login_check or "").strip().lower()

    def _logged_out(st: dict) -> bool:
        # No check configured -> be conservative and (re)login anyway.
        if not check:
            return True
        url = (st.get("url") or "").lower()
        title = (st.get("title") or "").lower()
        return (check in url) or (check in title)

    sid = None
    try:
        sess = await create_session({"initial_url": login_url, "idle_ttl_s": 300})
        sid = sess.get("session_id")
        if not sid:
            return {"host": h, "relogin": "failed", "reason": "session open returned no id"}
        # Give the SPA a beat to settle / redirect to the login page.
        await asyncio.sleep(2.0)
        st = await _session_state_dict(sid)
        if _logged_out(st):
            try:
                await session_agent(
                    sid,
                    {"goal": rec.login_goal, "max_steps": 8, "engine": "auto"},
                )
            except HTTPException as e:
                return {"host": h, "relogin": "failed", "reason": f"agent: {e.detail}"}
            await asyncio.sleep(1.5)
            st = await _session_state_dict(sid)
            if _logged_out(st):
                return {
                    "host": h,
                    "relogin": "failed",
                    "reason": "still logged out after login agent",
                    "url": st.get("url"),
                    "title": st.get("title"),
                }
        # Logged in -> persist the full jar + stamp.
        saved = await session_save_cookies_to_host(
            sid,
            {"host": h, "all_cookies": True, "notes": (rec.notes or f"auto-login {h}")},
        )
        state.hosts.touch_login(h)
        log.info(
            "auto-login %s: ok (saved %s cookie(s), url=%s)",
            h,
            saved.get("saved_count"),
            st.get("url"),
        )
        return {
            "host": h,
            "relogin": "ok",
            "saved_count": saved.get("saved_count"),
            "url": st.get("url"),
            "title": st.get("title"),
        }
    except HTTPException as e:
        return {"host": h, "relogin": "failed", "reason": str(e.detail)}
    except Exception as e:
        return {"host": h, "relogin": "failed", "reason": f"{type(e).__name__}: {e}"}
    finally:
        if sid:
            try:
                await close_session(sid)
            except Exception:
                pass
