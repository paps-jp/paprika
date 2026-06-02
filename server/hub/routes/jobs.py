"""Per-job HTTP routes: file serving, asset gallery, link/network reads.

Twenty routes covering the read-only + worker-dump endpoints under
``/jobs/{id}/...`` (and the ``/ui/assets/{id}`` HTML gallery surface
that shares the gallery JSON helpers). Helpers ``_safe_job_file``,
``_require_job_info``, ``_soft_resolve_job``, ``_asset_href``,
``_human_size`` and the ``_IMG/VIDEO/AUDIO_EXTS`` constants still live
in app.py (used by the create_job / cancel_job / WS routes that haven't
migrated yet) and are imported back here.

Not in this module (yet):
  * POST /jobs (create_job) -- 440-line dispatch + lifespan-touching
  * WS /jobs/{id}/events     -- live log stream
  * GET /jobs list, /{id}, /{id}/result, /{id}/visited
  * cancel / delete / cleanup / /admin/cleanup_jobs
  * POST /jobs/{id}/screenshot / /assets / /assets/from_url / /files/{kind}
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from server.hub._state import config, get_storage_dir, state

log = logging.getLogger(__name__)

# _safe_job_file (job-dir path sanitiser) stays in app.py -- it's used
# by other code paths that haven't migrated yet. The rest of the
# helpers (_require_job_info, _soft_resolve_job, _asset_href,
# _human_size, asset-mime constants) live in THIS module (defined
# below) and are re-exported from app.py for the /jobs routes still
# there (GET /jobs / {id} / result / cancel / delete / cleanup,
# POST /jobs/{id}/screenshot / /assets, etc).
from server.hub import objstore
from server.hub._helpers import _safe_job_file
from server.hub.routes.novnc import _proxy_session_dict
from server.hub.routes.sessions import (
    _novnc_autoconnect,
    _route_to_page,
    _send_session_action,
)
from server.protocol import JobInfo

router = APIRouter(tags=["Jobs"])


# ----------------------------------------------------------------------------
# Per-job static file serving
# ----------------------------------------------------------------------------


@router.get("/jobs/{job_id}/page.html")
async def get_page_html(job_id: str):
    # Multi-hub read-fallback: pull from shared object storage if a
    # different hub wrote it (no-op locally / single-hub).
    await objstore.ensure_local(get_storage_dir() / job_id / "page.html")
    return FileResponse(_safe_job_file(job_id, "page.html"), media_type="text/html")


# ----------------------------------------------------------------------------
# /jobs/{id}/links -- extract <a href> from the saved page.html
#
# Counterpart to ``/sessions/{sid}/links`` (which queries a live browser
# tab via the worker). This one works post-mortem: the session is long
# gone, but the rendered DOM dump is still on disk. Parses it with the
# stdlib HTMLParser, resolves relative URLs against the job's start
# URL, dedupes, applies the same protocol filter the live endpoint uses
# (javascript:/mailto:/tel:/blob:/data:/about:), and returns the same
# JSON shape so clients can use either endpoint interchangeably.
# ----------------------------------------------------------------------------


def _extract_links_from_html(
    raw_html: str,
    base_url: str,
) -> list[dict]:
    """Pull every <a href> out of an HTML document.

    Mirrors the worker's live ``document.links`` JS implementation:
      * resolve relative -> absolute against ``base_url`` (or against
        <base href> if the document declares one)
      * skip javascript: / mailto: / tel: / blob: / data: / about:
      * dedupe by absolute href
      * truncate visible text to ~120 chars

    Returns ``[{href, text, target, rel}, ...]``.
    """
    import html.parser as _hparser
    from urllib.parse import urljoin as _urljoin

    _SKIP = ("javascript:", "mailto:", "tel:", "blob:", "data:", "about:")

    class _LinkExtractor(_hparser.HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.base = base_url
            self.seen: set[str] = set()
            self.out: list[dict] = []
            self._stack: list[dict] = []  # active <a> frames
            self._text_buf: list[str] = []

        def handle_starttag(self, tag, attrs):
            t = tag.lower()
            if t == "base":
                # First <base href> wins, per spec.
                for k, v in attrs:
                    if k.lower() == "href" and v and self.base == base_url:
                        self.base = _urljoin(base_url, v)
                        break
            elif t == "a":
                href = ""
                target = ""
                rel = ""
                for k, v in attrs:
                    lk = k.lower()
                    if lk == "href":
                        href = v or ""
                    elif lk == "target":
                        target = v or ""
                    elif lk == "rel":
                        rel = v or ""
                self._stack.append(
                    {
                        "href": href,
                        "target": target,
                        "rel": rel,
                        "text_start": len(self._text_buf),
                    }
                )

        def handle_endtag(self, tag):
            if tag.lower() != "a" or not self._stack:
                return
            frame = self._stack.pop()
            href_raw = (frame["href"] or "").strip()
            if not href_raw:
                return
            lc = href_raw.lower()
            if any(lc.startswith(p) for p in _SKIP):
                return
            try:
                abs_href = _urljoin(self.base, href_raw)
            except Exception:
                return
            if not abs_href or abs_href in self.seen:
                return
            self.seen.add(abs_href)
            text = " ".join(self._text_buf[frame["text_start"] :])
            text = " ".join(text.split())
            if len(text) > 120:
                text = text[:119] + "…"
            self.out.append(
                {
                    "href": abs_href,
                    "text": text,
                    "target": frame["target"],
                    "rel": frame["rel"],
                }
            )

        def handle_data(self, data):
            if self._stack:
                self._text_buf.append(data)

    parser = _LinkExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        # Malformed HTML -- return whatever we collected so far.
        pass
    return parser.out


@router.get("/jobs/{job_id}/links")
async def get_job_links(job_id: str) -> dict:
    """Return all <a href> from the job's saved page.html, resolved to
    absolute URLs.

    Companion to ``/sessions/{sid}/links``. The session endpoint queries
    a live browser; this one parses the persisted HTML so it keeps
    working long after the job and its session are gone.

    For fetch-mode jobs the HTML is the post-render DOM dump (so SPA
    routes that fill in client-side ARE captured). For agent-mode jobs
    it's the last page snapshot the agent wrote. Empty list is a valid
    answer (e.g. the page never finished loading, or it's a binary
    asset response saved as page.html).

    Same shape as ``/sessions/{sid}/links`` so scripts can fall back
    from live -> stored without reshaping the result.
    """
    info = await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    current_url = info.url if info is not None else ""

    # Preferred source: the rendered HTML dump written by fetcher-style
    # jobs at completion. Keeps the legacy behaviour intact.
    page_path = job_dir / "page.html"
    if page_path.exists():
        try:
            raw = page_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(500, f"failed to read page.html: {e}")
        links = _extract_links_from_html(raw, current_url)
        return {
            "job_id": job_id,
            "current_url": current_url,
            "count": len(links),
            "links": links,
        }

    # Session-end snapshot fallback. Session-based jobs (cli.session,
    # codegen-loop runner sessions, the face_search crawler) don't
    # write page.html; the worker dumps the final-page links here
    # instead via POST /jobs/{id}/links_snapshot. Multiple sessions
    # under the same parent_job_id append (one JSON object per line),
    # so we flatten + dedupe by href, taking the LAST snapshot's
    # current_url as the page reference (most-recent wins).
    snapshot_path = job_dir / "links_snapshot.jsonl"
    if snapshot_path.exists():
        seen: set[str] = set()
        flat: list[dict] = []
        snap_current_url = current_url
        try:
            for line in snapshot_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("current_url"):
                    snap_current_url = obj["current_url"]
                for lk in obj.get("links") or []:
                    href = (lk.get("href") or "").strip()
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    flat.append(
                        {
                            "href": href,
                            "text": lk.get("text") or "",
                            "target": lk.get("target") or "",
                            "rel": lk.get("rel") or "",
                        }
                    )
        except Exception as e:
            raise HTTPException(500, f"failed to read links snapshot: {e}")
        return {
            "job_id": job_id,
            "current_url": snap_current_url,
            "count": len(flat),
            "links": flat,
        }

    # Neither source: job exists but never produced link data (still
    # running, download-only job, or session crashed before dumping).
    # Return empty so callers can poll without special-casing.
    return {
        "job_id": job_id,
        "current_url": current_url,
        "count": 0,
        "links": [],
    }


# ---------------------------------------------------------------------------
# Session-end snapshots. Worker POSTs these as the last act before
# tearing down a session whose ``asset_upload_base`` points at us, so
# the Live panel's Network / Links tabs still have data after the
# session closes. See server/worker/agent.py:_dump_session_to_parent_job.
# ---------------------------------------------------------------------------


def _consult_host_knowledge(url: str, options) -> list[str]:
    """v2 Phase 5: read HostKnowledge for this URL's host and apply hints.

    Mutates ``options`` in place when a learned hint should override an
    operator-unspecified default. Returns a list of human-readable
    consultation log lines (empty when no knowledge exists).

    Today's hints (lightweight):
      * navigation_hints.popup_policy → JobOptions.popup_policy (when
        operator didn't set one explicitly).
      * navigation_hints.lazy_load_trigger_needed → log only (the
        existing fetcher already runs the lazy-load JS unconditionally,
        so no opt-in is needed yet).
      * stats.overall_confidence → log only (informational).

    Future hints will inject barrier strategies and tool selection.
    Read-only at this phase -- no updates to HostKnowledge happen here
    (that's the Distiller's job after the job completes).
    """
    import json as _json
    from urllib.parse import urlparse as _up

    try:
        host = (_up(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return []
    if not host:
        return []

    knowledge_path = config.data_dir / "host_knowledge" / f"{host}.json"
    if not knowledge_path.is_file():
        return []

    try:
        k = _json.loads(knowledge_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"==> HostKnowledge consult: read failed for {host}: {e}"]

    log_lines: list[str] = [
        f"==> HostKnowledge consult: applying knowledge for '{host}'"
    ]

    # ---- navigation_hints.popup_policy -----------------------------------
    nh = (k.get("per_page") or {}).get("navigation_hints") or {}
    pp = nh.get("popup_policy")
    if pp in ("kill", "follow", "ignore"):
        # JobOptions.popup_policy default in protocol.py is None / unset.
        # Only override when caller didn't set a custom value.
        current = getattr(options, "popup_policy", None)
        if not current or current == "kill":
            try:
                setattr(options, "popup_policy", pp)
                log_lines.append(f"    popup_policy: {pp} (from HostKnowledge)")
            except Exception:
                pass

    # ---- navigation_hints.lazy_load_trigger_needed -----------------------
    if nh.get("lazy_load_trigger_needed"):
        log_lines.append("    lazy_load_trigger_needed: yes (informational)")

    # ---- per_page.barriers (informational for now) ----------------------
    barriers = (k.get("per_page") or {}).get("barriers") or {}
    active = [bk for bk, bv in barriers.items() if (bv or {}).get("present")]
    if active:
        log_lines.append(
            f"    known barriers: {', '.join(active)} "
            f"(strategies registered, will be auto-applied in a future phase)"
        )

    # ---- content_extraction (informational) -----------------------------
    ce = (k.get("per_page") or {}).get("content_extraction") or []
    if ce:
        patterns = [c.get("url_pattern") for c in ce if isinstance(c, dict)]
        log_lines.append(
            f"    content_extraction patterns: {len(patterns)} "
            f"({', '.join(p for p in patterns[:3] if p)}{'...' if len(patterns) > 3 else ''})"
        )

    # ---- stats / confidence ---------------------------------------------
    stats = k.get("stats") or {}
    n = stats.get("total_jobs") or 0
    sr = stats.get("success_rate") or 0.0
    tier = stats.get("overall_confidence") or "low"
    log_lines.append(
        f"    stats: {n} prior job(s), success_rate={sr:.0%}, confidence={tier}"
    )

    return log_lines


# ---------------------------------------------------------------------------
# v2 Phase 7c: pre-flight plugin auto-invocation.
#
# When HostKnowledge records that a barrier on this host is best cleared
# by a specific plugin (BarrierKnowledge.suggested_tool), the dispatcher
# invokes that plugin BEFORE handing the job to a Worker, and merges
# any cookies it returns into the HostRecord. The Worker then picks up
# those cookies via the existing rec.cookies path -- no new wiring on
# the Worker side.
#
# Today's recipe:
#   per_page.barriers.cloudflare_challenge.subtype = "js_challenge" | "turnstile"
#     → suggested_tool = "paprika-flare"   (Worker Chrome session, IP-matched)
#   per_page.barriers.cloudflare_challenge.subtype = "ip_banned"
#     → suggested_tool = "paprika-proxy-fetch" (proxied egress; less useful
#       since cookies are not IP-matched to the Worker, but the response
#       body confirms whether the host is still IP-banned).
#
# The pre-flight is best-effort: any failure (plugin not installed, proxy
# not configured, Worker pool exhausted, plugin error) is logged but
# never blocks job dispatch. The job still goes through; it just goes
# without the cf_clearance cookie and the Worker takes its chances.
# ---------------------------------------------------------------------------


# Pre-flight plugins we are willing to auto-invoke. Lock this list down
# so a hallucinated HostKnowledge.suggested_tool can't spawn arbitrary
# plugins (e.g. yt-dlp on every navigation).
_PREFLIGHT_ALLOWED_PLUGINS = frozenset({
    "paprika-flare",
    "paprika-proxy-fetch",
})


def _cookies_dict_to_records(
    cookies: dict, *, host: str,
) -> list[dict]:
    """Convert ``{name: value}`` to the CDP-cookie record shape that
    HostRecord stores. Domain defaults to the dot-prefixed host (matches
    Cloudflare's actual cookie scope)."""
    out: list[dict] = []
    if not isinstance(cookies, dict):
        return out
    domain = f".{host}" if host and not host.startswith(".") else host
    for name, value in cookies.items():
        if not name or value is None:
            continue
        out.append({
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": "/",
        })
    return out


async def _preflight_cf_plugin(url: str, job_id: str) -> list[str]:
    """Pre-flight any plugin HostKnowledge has tagged for this host's barriers.

    Reads ``per_page.barriers`` from HostKnowledge, finds entries with
    ``present=true`` AND ``suggested_tool`` in the allow-list, invokes
    the plugin's ``get_cookies`` action, and merges returned cookies
    into the HostRecord. Returns log lines to append to the job log.

    Best-effort: any exception is swallowed and reported as a log line;
    the dispatcher continues without the plugin's contribution.
    """
    import json as _json
    from urllib.parse import urlparse as _up

    try:
        host = (_up(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return []
    if not host:
        return []

    knowledge_path = config.data_dir / "host_knowledge" / f"{host}.json"
    if not knowledge_path.is_file():
        return []
    try:
        k = _json.loads(knowledge_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    barriers = (k.get("per_page") or {}).get("barriers") or {}
    if not isinstance(barriers, dict):
        return []

    # Find the first barrier with a suggested_tool we trust.
    chosen_kind = None
    chosen_barrier = None
    for bkind, bval in barriers.items():
        if not isinstance(bval, dict):
            continue
        if not bval.get("present"):
            continue
        tool = bval.get("suggested_tool")
        if not tool or tool not in _PREFLIGHT_ALLOWED_PLUGINS:
            continue
        chosen_kind = bkind
        chosen_barrier = bval
        break

    if chosen_barrier is None:
        return []

    tool = chosen_barrier.get("suggested_tool")
    subtype = chosen_barrier.get("subtype") or "?"
    tool_params = dict(chosen_barrier.get("tool_params") or {})
    # Plugins all take a ``url`` arg; override / inject it from the job URL.
    tool_params["url"] = url

    log_lines: list[str] = [
        f"==> pre-flight plugin: {tool} for barrier "
        f"{chosen_kind}/{subtype} on {host}"
    ]

    try:
        from server.hub.plugins import (
            invoke_plugin,
            PluginNotAvailable,
            PluginInvocationError,
        )
    except Exception as e:
        log_lines.append(f"    plugin module unavailable ({e}); skipping pre-flight")
        return log_lines

    try:
        result = await invoke_plugin(
            tool,
            "get_cookies",
            tool_params,
            audit_context={"job_id": job_id, "host": host, "trigger": "preflight"},
        )
    except PluginNotAvailable as e:
        log_lines.append(f"    plugin not available: {e}; skipping pre-flight")
        return log_lines
    except PluginInvocationError as e:
        log_lines.append(f"    plugin failed: {str(e)[:240]}; continuing without cookies")
        return log_lines
    except Exception as e:
        log_lines.append(
            f"    plugin crashed ({type(e).__name__}: {str(e)[:200]}); "
            f"continuing without cookies"
        )
        return log_lines

    cookies = (result or {}).get("cookies") or {}
    n_cookies = len(cookies) if isinstance(cookies, dict) else 0
    elapsed_ms = (result or {}).get("elapsed_ms") or 0
    log_lines.append(
        f"    plugin returned {n_cookies} cookie(s) in {elapsed_ms} ms"
    )

    if n_cookies == 0:
        return log_lines

    # Merge into HostRecord. The Worker dispatch path below reads
    # rec.cookies AFTER us, so the fresh cf_clearance is picked up.
    cookie_records = _cookies_dict_to_records(cookies, host=host)
    if not cookie_records:
        return log_lines

    if state.hosts is None:
        log_lines.append("    host registry not available; cookies not persisted")
        return log_lines

    try:
        existing = state.hosts.get(host)
        # Merge: new names overwrite, old-but-still-relevant names survive.
        # This is critical -- a CF pre-flight only sets cf_clearance and
        # __cf_bm, but the host may also have a login session cookie set
        # earlier that we must NOT wipe.
        existing_records = list(existing.cookies) if existing and existing.cookies else []
        new_names = {c["name"] for c in cookie_records}
        merged = [c for c in existing_records if c.get("name") not in new_names] + cookie_records
        state.hosts.upsert(host, merged)
        log_lines.append(
            f"    merged into HostRecord ({host}): "
            f"{', '.join(sorted(new_names))}"
        )
    except Exception as e:
        log_lines.append(
            f"    HostRecord merge failed ({type(e).__name__}: {str(e)[:200]})"
        )

    return log_lines


async def _require_job_info(job_id: str) -> JobInfo:
    """Hard-404 lookup: the operation REQUIRES a registered JobInfo
    record (e.g. log writes, finalization, anything that needs the
    job's worker_id or status). Use this when accepting a session-
    routed parent_job_id wouldn't make sense for the action.
    """
    assert state.store is not None
    info = await state.store.get_job_info(job_id)
    if info is None:
        raise HTTPException(404, f"job '{job_id}' not found")
    return info


async def _soft_resolve_job(
    job_id: str,
    require_subdir: str = "",
) -> JobInfo | None:
    """Soft-404 lookup: accept the request when a JobInfo record exists
    OR a session-routed parent_job_id's directory was pre-created by
    create_session. Returns the JobInfo when registered, ``None`` when
    only the on-disk dir exists.

    ``require_subdir`` is the relative path inside ``data/jobs/{id}/``
    that must exist for the soft-accept path -- usually ``"assets"`` to
    match the dir create_session pre-creates. Pass ``""`` (default) to
    accept the bare job dir.

    Centralises the pattern that grew across /assets, /assets.json,
    /ui/assets, /jobs/{id}/links, /jobs/{id}/network, and the
    session-end POST endpoints. Six call sites at the time of
    extraction; deferring this DRY made the asset-gallery debugging
    saga ~one commit longer than it needed to be."""
    assert state.store is not None
    info = await state.store.get_job_info(job_id)
    if info is not None:
        return info
    check_dir = get_storage_dir() / job_id
    if require_subdir:
        check_dir = check_dir / require_subdir
    if not check_dir.is_dir():
        raise HTTPException(404, f"job '{job_id}' not found")
    return None


def _append_network_jsonl(job_dir: Path, entries: list, sid: str) -> int:
    """Blocking JSONL append for network logs. Runs in a worker thread
    (see ``asyncio.to_thread`` call site) so slow storage IO never stalls
    the hub event loop."""
    job_dir.mkdir(parents=True, exist_ok=True)
    out_path = job_dir / "network.jsonl"
    written = 0
    with out_path.open("a", encoding="utf-8") as f:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Stamp the session_id into each entry so the Live panel UI
            # can show "which tab/session made this request" when the
            # parent job had multiple sessions.
            entry_out = dict(entry)
            entry_out.setdefault("session_id", sid)
            f.write(json.dumps(entry_out, ensure_ascii=False))
            f.write("\n")
            written += 1
    return written


def _append_line(job_dir: Path, filename: str, line: str) -> None:
    """Blocking single-line append. Runs in a worker thread so slow
    storage IO never stalls the hub event loop."""
    job_dir.mkdir(parents=True, exist_ok=True)
    out_path = job_dir / filename
    with out_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")


@router.post("/jobs/{job_id}/network")
async def upload_session_network(job_id: str, body: dict) -> dict:
    """Worker -> hub. Append a session's network log to
    ``data/jobs/{id}/network.jsonl``. JSONL is line-oriented so concurrent
    sessions under the same parent_job_id can append safely (write(2) on
    POSIX is atomic for sub-page payloads).

    Body::

        {
          "secret":     "...",                  # worker_secret, optional
          "session_id": "ses_...",
          "entries":    [{url, mime, size, ...}, ...]
        }
    """
    if config.worker_secret:
        if str((body or {}).get("secret") or "") != config.worker_secret:
            raise HTTPException(401, "bad secret")
    sid = str((body or {}).get("session_id") or "")
    entries = (body or {}).get("entries") or []
    if not isinstance(entries, list):
        raise HTTPException(400, "entries must be a list")

    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id

    # Offload the blocking open()/write() (incl. mkdir/stat) to a worker
    # thread so a slow storage backend (e.g. SMB/CIFS mount) cannot stall
    # the single hub event loop and starve every worker's heartbeat/pong.
    written = await asyncio.to_thread(_append_network_jsonl, job_dir, entries, sid)
    return {"ok": True, "job_id": job_id, "session_id": sid, "written": written}


@router.get("/jobs/{job_id}/network")
async def get_job_network(job_id: str) -> dict:
    """Read back the session-end network dump. Same shape as
    ``/sessions/{sid}/network`` so the Live panel can swap the source
    transparently."""
    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    log_path = job_dir / "network.jsonl"
    entries: list[dict] = []
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            raise HTTPException(500, f"failed to read network.jsonl: {e}")
    return {
        "job_id": job_id,
        "count": len(entries),
        "entries": entries,
    }


@router.post("/jobs/{job_id}/links_snapshot")
async def upload_session_links_snapshot(job_id: str, body: dict) -> dict:
    """Worker -> hub. Append one session's final-page link list to
    ``data/jobs/{id}/links_snapshot.jsonl`` (one JSON object per line,
    one line per session). Read by the extended ``GET /jobs/{id}/links``
    when ``page.html`` is absent (= session-based jobs).

    Body::

        {
          "secret":      "...",
          "session_id":  "ses_...",
          "current_url": "https://...",
          "links":       [{href, text, target, rel}, ...]
        }
    """
    if config.worker_secret:
        if str((body or {}).get("secret") or "") != config.worker_secret:
            raise HTTPException(401, "bad secret")
    sid = str((body or {}).get("session_id") or "")
    links = (body or {}).get("links") or []
    if not isinstance(links, list):
        raise HTTPException(400, "links must be a list")
    current_url = str((body or {}).get("current_url") or "")

    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    line = json.dumps(
        {
            "session_id": sid,
            "current_url": current_url,
            "links": [
                {
                    "href": (lk or {}).get("href") or "",
                    "text": (lk or {}).get("text") or "",
                    "target": (lk or {}).get("target") or "",
                    "rel": (lk or {}).get("rel") or "",
                }
                for lk in links
                if isinstance(lk, dict)
            ],
        },
        ensure_ascii=False,
    )
    # Offload the blocking open()/write() (incl. mkdir/stat) to a worker
    # thread so a slow storage backend cannot stall the hub event loop.
    await asyncio.to_thread(_append_line, job_dir, "links_snapshot.jsonl", line)
    return {"ok": True, "job_id": job_id, "session_id": sid, "count": len(links)}


@router.get("/jobs/{job_id}/meta")
async def get_page_meta(job_id: str) -> dict:
    """Pull the rendered page's ``<title>`` / description / thumbnail
    out of the saved ``page.html``.

    Response shape::

        {
          "job_id":        "...",
          "url":           "https://example.com/page",
          "title":         "Example -- Welcome",        # or null
          "description":   "An example page.",          # or null
          "thumbnail_url": "https://example.com/og.png",# or null
          "source":        "page.html"
        }

    Extraction is "first non-empty" across (in order):
      * title:         <title> -> og:title -> twitter:title
      * description:   <meta name=description> -> og:description -> twitter:description
      * thumbnail_url: og:image{,:url,:secure_url} -> twitter:image -> apple-touch-icon -> icon

    Returns 404 only when the job itself doesn't exist. When the
    job exists but page.html was never saved (still running, or a
    download-only / video-only job), the title/description/
    thumbnail fields are null and the caller can poll again later
    -- same pattern as /jobs/{id}/links.
    """
    info = await _require_job_info(job_id)
    page_path = get_storage_dir() / job_id / "page.html"
    if not page_path.exists():
        return {
            "job_id": job_id,
            "url": info.url or "",
            "title": None,
            "description": None,
            "thumbnail_url": None,
            "source": "page.html",
        }
    try:
        raw = page_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, f"failed to read page.html: {e}")
    from server.hub.meta import extract_meta

    meta = extract_meta(raw, base_url=info.url or "")
    return {
        "job_id": job_id,
        "url": info.url or "",
        "title": meta.get("title"),
        "description": meta.get("description"),
        "thumbnail_url": meta.get("thumbnail_url"),
        "source": "page.html",
    }


@router.get("/jobs/{job_id}/log.txt")
async def get_log(job_id: str):
    await objstore.ensure_local(get_storage_dir() / job_id / "log.txt")
    return FileResponse(_safe_job_file(job_id, "log.txt"), media_type="text/plain")


@router.get("/jobs/{job_id}/script.py")
async def get_script(job_id: str):
    """The final generated script for codegen-loop jobs. For fetch jobs
    this 404s unless a template-style .py has been emitted (future)."""
    return FileResponse(
        _safe_job_file(job_id, "script.py"),
        media_type="text/x-python",
    )


@router.get("/jobs/{job_id}/actions.json")
async def get_actions_json(job_id: str):
    """The winning attempt's mutating Page-call trace, as a JSON list of
    ``{kind, args, kwargs, elapsed_ms, ok}`` entries (Phase 2b).

    Returns ``[]`` for jobs that ran no Page actions (e.g. an LLM-failure
    attempt that never reached a script) but never 404s if the job dir
    exists -- the file is always written. 404 when the job itself is
    unknown.
    """
    return FileResponse(
        _safe_job_file(job_id, "actions.json"),
        media_type="application/json",
    )


@router.get("/jobs/{job_id}/recipe_suggestion")
async def get_recipe_suggestion(job_id: str) -> dict:
    """Aggregate everything the 'Save as HostRecipe' UI needs into one
    response (Phase 2c).

    Combines: the job's starting URL, a vendor-neutral host + pattern
    guess, the winning attempt's action trace, the generated script,
    the operator's goal text, and the job's success flag.

    The UI prefills a modal from this, lets the operator edit, then
    POSTs to /hosts/{host}/recipes. Operators are expected to verify
    the pattern in particular -- the heuristic is conservative but
    won't always pick the right segment to wildcard.
    """
    job_dir = get_storage_dir() / job_id
    if not job_dir.exists():
        raise HTTPException(404, f"job '{job_id}' not found")

    # Pull JobInfo for the url + goal (codegen-loop options live there).
    info = await state.store.get_job_info(job_id) if state.store else None
    url = info.url if info else ""
    goal = ""
    if info and info.options:
        goal = (info.options.goal or "").strip()

    actions: list = []
    try:
        actions = json.loads(
            (job_dir / "actions.json").read_text(encoding="utf-8")
        )
        if not isinstance(actions, list):
            actions = []
    except Exception:
        actions = []

    # Top-level /jobs/{id}/script.py is written by iterative_codegen's
    # _persist_outcome() AFTER the codegen-loop finishes. If the job
    # was killed mid-attempt (hub restart / worker crash / cancel) the
    # outcome never gets persisted and the top-level script.py is
    # missing -- but per-attempt scripts at /jobs/{id}/attempts/N/script.py
    # are still there. Fall back to the LATEST attempt's script.py so
    # the recipe save modal isn't empty for failed jobs.
    code = ""
    try:
        code = (job_dir / "script.py").read_text(encoding="utf-8")
    except Exception:
        attempts_dir = job_dir / "attempts"
        if attempts_dir.is_dir():
            try:
                numeric_attempts = sorted(
                    (p for p in attempts_dir.iterdir()
                     if p.is_dir() and p.name.isdigit()),
                    key=lambda p: int(p.name),
                    reverse=True,
                )
                for ap in numeric_attempts:
                    sp = ap / "script.py"
                    try:
                        c = sp.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if c.strip():
                        code = c
                        break
            except Exception:
                pass

    outcome: dict = {}
    try:
        outcome = json.loads(
            (job_dir / "outcome.json").read_text(encoding="utf-8")
        )
    except Exception:
        pass

    # Host + pattern derivation. Both are vendor-neutral: host is the
    # bare netloc minus a "www." prefix (matching HostRegistry's own
    # normaliser), pattern is the path-glob heuristic from
    # hosts.pattern_from_url.
    from server.hub.hosts import pattern_from_url, _normalise_host
    host = ""
    pattern = "*"
    if url:
        try:
            from urllib.parse import urlparse
            host = _normalise_host(urlparse(url).hostname or "")
            pattern = pattern_from_url(url)
        except Exception:
            pass

    return {
        "job_id": job_id,
        "url": url,
        "host": host,
        "pattern": pattern,
        "description": f"AI調査 by job {job_id}",
        "goal": goal,
        "actions": actions,
        "code": code,
        "success": bool(outcome.get("success")),
        "created_from_job": job_id,
        "created_by": "ai",
    }


@router.get("/jobs/{job_id}/plan.json")
async def get_plan(job_id: str):
    """The planner's goal decomposition (codegen-loop only).

    Written once at the start of run_iterative_codegen -- the
    planner LLM turns the operator's goal into 3-7 sub-steps + a
    success criterion. Operators inspect this to see how the
    decomposition was framed; the Judge also uses the success
    criterion as the bar for verdict.
    """
    return FileResponse(
        _safe_job_file(job_id, "plan.json"),
        media_type="application/json",
    )


@router.get("/jobs/{job_id}/attempts")
async def list_attempts(job_id: str) -> dict:
    """List all codegen-loop attempts for a job (codegen-loop mode only)."""
    job_dir = get_storage_dir() / job_id
    if not job_dir.exists():
        raise HTTPException(404, f"job '{job_id}' not found")
    attempts_dir = job_dir / "attempts"
    if not attempts_dir.exists():
        return {"job_id": job_id, "count": 0, "attempts": []}
    rows: list[dict] = []
    for sub in sorted(attempts_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        if not sub.is_dir() or not sub.name.isdigit():
            continue
        try:
            result = json.loads((sub / "result.json").read_text(encoding="utf-8"))
        except Exception:
            result = {}
        # Include LLM metadata in the row if we captured it (added when
        # the orchestrator started persisting prompt/response per attempt).
        llm_meta: dict = {}
        if (sub / "llm_meta.json").exists():
            try:
                llm_meta = json.loads((sub / "llm_meta.json").read_text(encoding="utf-8"))
            except Exception:
                pass
        rows.append(
            {
                "n": int(sub.name),
                **result,
                "script_href": f"/jobs/{job_id}/attempts/{sub.name}/script.py",
                "stdout_href": f"/jobs/{job_id}/attempts/{sub.name}/stdout.log",
                "stderr_href": f"/jobs/{job_id}/attempts/{sub.name}/stderr.log",
                "prompt_href": (
                    f"/jobs/{job_id}/attempts/{sub.name}/prompt.txt"
                    if (sub / "prompt.txt").exists()
                    else None
                ),
                "llm_response_href": (
                    f"/jobs/{job_id}/attempts/{sub.name}/llm_response.txt"
                    if (sub / "llm_response.txt").exists()
                    else None
                ),
                "llm": llm_meta or None,
            }
        )
    return {"job_id": job_id, "count": len(rows), "attempts": rows}


@router.get("/jobs/{job_id}/sessions")
async def list_job_sessions(job_id: str) -> dict:
    """Sessions currently owned by this Job (codegen-loop pinned them
    via PAPRIKA_JOB_ID -> parent_job_id). Used by the admin UI's live
    panel to render noVNC iframes for whatever lanes the runner has
    open right now.

    Each session dict's ``novnc_url`` is rewritten to the session-rooted
    hub-proxy URL so iframes embed via the hub (= no worker LAN IP
    leakage)."""
    if state.sessions is None:
        return {"job_id": job_id, "count": 0, "sessions": []}
    matches = [s.to_json() for s in state.sessions.all() if s.job_id == job_id]
    for m in matches:
        _proxy_session_dict(m)
        m["novnc_url_autoconnect"] = _novnc_autoconnect(m.get("novnc_url"))
    return {"job_id": job_id, "count": len(matches), "sessions": matches}


@router.post("/jobs/{job_id}/refresh")
async def refresh_job_from_session(job_id: str) -> dict:
    """For a job whose session is still alive (keep_session=True Fetch
    jobs, or any codegen-loop session pinned to this job), push the
    current browser state back into the job directory:

      * capture the current page HTML and overwrite
        ``data/jobs/{job_id}/page.html`` (so /jobs/{id}/links
        re-extracts against whatever URL the operator just landed on
        via noVNC),
      * flush every file in the worker tempdir that hasn't been
        uploaded yet (videos the operator manually played, images
        revealed by clicks, etc.).

    Use case: operator opens a keep_session fetch job in noVNC, plays
    a video on the page → HLS segments stream into the worker's
    capture dir but never get shipped (the fetcher's "I'm done"
    moment has already passed). One POST here drags those into the
    gallery and the Links tab.

    Returns the per-action result from the worker (current_url,
    html_uploaded, added=[asset names], added_count). 404 if the job
    doesn't exist or has no live session; 502 if the worker session
    action errors / times out.
    """
    info = await _require_job_info(job_id)
    if state.sessions is None:
        raise HTTPException(
            404,
            f"job {job_id}: no session registry available (hub started without session support)",
        )
    # Resolve the session id. Two paths:
    #   1. Fetch keep_session: JobInfo.session_id was set at dispatch
    #      time by the /jobs handler.
    #   2. codegen-loop / rerun / Code mode: the script itself opens
    #      the session via paprika-client at runtime. JobInfo.session_id
    #      stays None; the link is the OTHER direction
    #      (SessionInfo.job_id == this job_id), set by the runner
    #      orchestrator injecting PAPRIKA_JOB_ID env into the script.
    # Either path eventually yields a SessionInfo; refresh works the
    # same way from there.
    sid: str | None = getattr(info, "session_id", None)
    if sid and state.sessions.get(sid) is not None:
        # Path 1: stored session_id is still alive. Use it.
        pass
    else:
        # Path 2 (or path 1 with a dead session): scan the registry
        # for live sessions linked to this job. Prefer detach()-ed
        # sessions (operator-managed, the typical refresh target);
        # fall back to the most-recently-active one when nothing's
        # been formally detached.
        candidates = [s for s in state.sessions.all() if s.job_id == job_id]
        if not candidates:
            raise HTTPException(
                404,
                f"job {job_id} has no live session "
                f"(closed, TTL-reaped, or worker disconnected). "
                f"For Fetch jobs, submit with keep_session=true; "
                f"for Code / codegen-loop jobs, the script must call "
                f"await sess.detach() before exiting.",
            )
        # Sort: detached first, then by last_active_at (newest first).
        candidates.sort(
            key=lambda s: (not s.detached, -s.last_active_at.timestamp()),
        )
        sid = candidates[0].session_id
    reply = await _send_session_action(
        sid,
        {"kind": "fetch_refresh"},
        timeout=60.0,
    )
    if reply.get("status", "").startswith("ERR:"):
        raise HTTPException(502, reply["status"])
    return {
        "job_id": job_id,
        "session_id": sid,
        "result": reply.get("result") or {},
    }


@router.post("/jobs/{job_id}/download-video")
async def download_video_for_job(
    job_id: str,
    body: dict | None = None,
) -> dict:
    """Shell to ``yt-dlp`` on the live session bound to this job and
    upload the resulting video file(s) to the job's /assets directory.

    Body (all optional)::

        {
          "url":       "https://...",   # target page URL; default = the
                                        # session's current page URL
          "referer":   "https://...",   # passed as yt-dlp --referer
          "timeout_s": 1800             # yt-dlp subprocess timeout (sec)
        }

    Unlike ``/jobs/{id}/refresh`` -- which flushes passively-captured
    HLS / segment files (= ``.ts`` fragments not directly playable) --
    this endpoint runs yt-dlp end-to-end so the gallery gets a single
    combined .mp4. Use it when the operator clicks "play" in noVNC and
    wants the resulting video stored as one playable file.

    Resolves the session the same way /refresh does: tries
    ``info.session_id`` first (Fetch keep_session jobs), then scans
    ``SessionInfo.job_id`` (codegen-loop / Code-mode jobs with
    detached sessions).

    Returns the per-action result: ``{ok, url, message, files,
    file_count}``. 404 if no session is bound to the job; 502 on
    worker error / timeout. The HTTP timeout is set to ``timeout_s +
    120`` (yt-dlp subprocess can be slow on big videos).
    """
    info = await _require_job_info(job_id)
    if state.sessions is None:
        raise HTTPException(
            404,
            f"job {job_id}: no session registry available (hub started without session support)",
        )
    # Same dual-path resolution as /refresh -- see refresh_job_from_session
    # for the rationale.
    sid: str | None = getattr(info, "session_id", None)
    if sid and state.sessions.get(sid) is not None:
        pass
    else:
        candidates = [s for s in state.sessions.all() if s.job_id == job_id]
        if not candidates:
            raise HTTPException(
                404,
                f"job {job_id} has no live session (closed, TTL-reaped, or worker disconnected).",
            )
        candidates.sort(
            key=lambda s: (not s.detached, -s.last_active_at.timestamp()),
        )
        sid = candidates[0].session_id

    body = body or {}
    url = body.get("url")
    referer = body.get("referer")
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
    # Forward candidate-discovery + media-oracle controls (iframe_walk,
    # min/expected duration, perceptual-hash reference) to the worker.
    for _k in (
        "iframe_walk", "min_duration_s", "expected_duration_s",
        "duration_tolerance", "reference_phash", "phash_max_distance",
    ):
        if body.get(_k) is not None:
            action[_k] = body[_k]
    # Route to a specific tab if the caller asked. Without this the
    # worker dispatcher uses state.default_page_id, which is whatever
    # the last switch_page targeted -- NOT necessarily what the
    # operator sees in noVNC (Chrome focus and worker state can
    # drift; clicking the in-browser tab bar in noVNC doesn't sync
    # back to the worker). Operator-facing button uses this to pin
    # yt-dlp on the chosen tab from a multi-tab picker.
    action = _route_to_page(action, body)
    # +120s buffer over the subprocess timeout (uploads happen AFTER
    # yt-dlp completes, plus WS round-trip overhead).
    reply = await _send_session_action(
        sid,
        action,
        timeout=float(timeout_s) + 120.0,
    )
    if reply.get("status", "").startswith("ERR:"):
        raise HTTPException(502, reply["status"])
    return {
        "job_id": job_id,
        "session_id": sid,
        "result": reply.get("result") or {},
    }


@router.get("/jobs/{job_id}/attempts/{n}/{filename}")
async def get_attempt_file(job_id: str, n: int, filename: str):
    allowed = {
        "script.py": "text/x-python",
        "stdout.log": "text/plain; charset=utf-8",
        "stderr.log": "text/plain; charset=utf-8",
        "result.json": "application/json",
        # LLM call artefacts -- the goal + retry context that went in,
        # the raw response that came out, and metadata (model, tokens,
        # latency). Lets operators rerun bad prompts without re-running
        # the whole job.
        "prompt.txt": "text/plain; charset=utf-8",
        "llm_response.txt": "text/plain; charset=utf-8",
        "llm_meta.json": "application/json",
        # Judge LLM verdict, written by iterative_codegen.py when the
        # heuristic-success gate calls judge_attempt(). Has the
        # satisfied/reason/hint shape -- operator inspects to see why
        # an exit-0 attempt was rejected (or accepted).
        "judge.json": "application/json",
        # Final-frame screenshot of the lane after the script exited,
        # captured before orphan-session cleanup so the judge LLM
        # can SEE the page state. Operator can inspect to verify the
        # judge's verdict against the actual visual outcome.
        "final_screenshot.jpg": "image/jpeg",
        # Phase 2b: per-attempt action trace (mutating Page calls
        # captured via the __PAPRIKA_ACTION__ stdout sentinel).
        # Empty array when the attempt didn't run any traceable
        # actions. Mirror of the top-level /jobs/{id}/actions.json
        # for the winning attempt.
        "actions.json": "application/json",
        # Reasoning judge verdict, written next to legacy judge.json
        # when reasoning_judge_mode is shadow / primary. Same shape as
        # judge.json plus "mode" and "engine" fields.
        "judge_reasoning.json": "application/json",
        # Legacy name (backward compat for existing job data).
        "judge_r1.json": "application/json",
        # Per-attempt PerceptionResult (vision LLM observation of the
        # attempt's final screenshot). Used by reasoning judge and
        # distiller. Read-only artefact.
        "perception.json": "application/json",
    }
    if filename not in allowed:
        raise HTTPException(400, "invalid attempt file name")
    await objstore.ensure_local(
        get_storage_dir() / job_id / "attempts" / str(n) / filename
    )
    return FileResponse(
        _safe_job_file(job_id, "attempts", str(n), filename),
        media_type=allowed[filename],
    )


# v2 Phase 1: end-of-job PerceptionResult (saved by save_perception_for_job
# after the job completes). Single file per job at workdir root.
@router.get("/jobs/{job_id}/perception")
async def get_job_perception(job_id: str):
    await objstore.ensure_local(get_storage_dir() / job_id / "perception.json")
    return FileResponse(
        _safe_job_file(job_id, "perception.json"),
        media_type="application/json",
    )


@router.get("/jobs/{job_id}/assets/{filename:path}")
async def get_asset(job_id: str, filename: str):
    """Serve an asset file. ``filename`` may include forward slashes for
    nested paths (e.g. ``post_verification/post_verification.png`` from
    ``page.capture(label=...)`` output). Path traversal is blocked via
    resolve+relative_to against the job's assets/ root."""
    if not filename or "\\" in filename or filename.startswith("/"):
        raise HTTPException(400, "invalid path")
    # Reject any segment that's empty / "." / ".." so the resolve check
    # below has well-formed input.
    parts = filename.split("/")
    for seg in parts:
        if seg in ("", ".", ".."):
            raise HTTPException(400, "invalid path component")
    job_dir = get_storage_dir() / job_id
    if not job_dir.exists():
        raise HTTPException(404, f"job '{job_id}' not found")
    assets_root = (job_dir / "assets").resolve()
    target = (job_dir / "assets" / filename)
    try:
        target.resolve().relative_to(assets_root)
    except ValueError:
        raise HTTPException(400, "path escapes assets dir")
    # Multi-hub read-fallback: pull from shared object storage if a
    # different hub produced this asset (no-op locally / single-hub).
    await objstore.ensure_local(target)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"file not found: {filename}")
    return FileResponse(target)


# ----------------------------------------------------------------------------
# /ui/assets/{id} — visual browser of captured assets (images / videos)
# (renamed from /gallery -> /screenshots -> /ui/attempts -> /ui/assets;
#  old paths retained as legacy aliases)
# ----------------------------------------------------------------------------

_IMG_EXTS = {"png", "jpg", "jpeg", "webp", "avif", "gif", "svg", "bmp", "ico"}
_VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "mkv"}
_AUDIO_EXTS = {"mp3", "ogg", "wav", "m4a", "aac", "flac", "opus"}


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def _asset_href(job_id: str, filename: str) -> str:
    """Build a URL-safe ``/jobs/{job_id}/assets/{filename}`` path.

    Asset filenames come from external sources (downloaded video
    titles, captured page titles, operator uploads) and routinely
    contain characters that break a bare-string URL: CJK / emoji
    (handled by browsers but ugly), and -- the bug-prone one --
    ``#``, which the browser treats as the fragment separator and
    SILENTLY DROPS from the request path. A 2026-05 X video crawl
    landed files like ``...治愈之音！ #廈門六中合唱團 ...[id].mp4`` whose
    bare hrefs 404'd because the server only saw the substring before
    the ``#``. Quote with safe="" so EVERY non-alphanumeric becomes
    percent-encoded; the file route handler decodes back to the
    original filename automatically.
    """
    from urllib.parse import quote

    # safe='/' preserves directory separators in nested paths
    # (e.g. "post_verification/post_verification.png" stays readable),
    # while still percent-encoding every other special character.
    # The asset route accepts {filename:path} so slashes match the
    # directory hierarchy.
    return f"/jobs/{quote(job_id, safe='')}/assets/{quote(filename, safe='/')}"


@router.get("/jobs/{job_id}/assets.json")
async def job_assets_json(job_id: str) -> dict:
    """JSON view of captured assets -- powers the inline live panel's
    thumbnail strip. Lighter than the full HTML gallery; just enough for
    the admin UI to render tiles.

    Each item also carries ``source_url`` and ``mime`` when the upload
    came with that metadata (session captures emit it via the passive
    CDP listener). The admin UI's click-through popup shows them.

    Note: the legacy ``/jobs/{id}/gallery.json`` path is kept as an alias
    below for older integrations -- prefer ``assets.json`` going forward.
    """
    await _soft_resolve_job(job_id, require_subdir="assets")
    assets_dir = get_storage_dir() / job_id / "assets"
    meta_dir = assets_dir / ".meta"
    items: list[dict] = []
    if assets_dir.exists():
        for p in sorted(assets_dir.iterdir(), key=lambda p: p.name.lower()):
            if not p.is_file():
                continue
            # Skip "screenshot-*" entries -- those are intentional
            # captures (manual Screenshot button) and live exclusively
            # in the Screenshot tab via /jobs/{id}/screenshots.json.
            # Operator preference: don't duplicate them into the asset
            # gallery. Page-downloaded images (logo.png etc) without the
            # prefix continue to show here.
            if p.name.lower().startswith("screenshot-"):
                continue
            ext = p.suffix.lower().lstrip(".")
            kind = "other"
            if ext in _IMG_EXTS:
                kind = "image"
            elif ext in _VIDEO_EXTS:
                kind = "video"
            elif ext in _AUDIO_EXTS:
                kind = "audio"
            sz = p.stat().st_size
            # Pull sidecar metadata if it exists. The fetch path saves
            # source URLs straight onto the asset row; session captures
            # use the .meta/ sidecar minted by upload_asset above.
            source_url = None
            mime = None
            page_url = None
            meta_path = meta_dir / f"{p.name}.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    source_url = meta.get("source_url")
                    mime = meta.get("mime")
                    page_url = meta.get("page_url")
                except Exception:
                    pass
            items.append(
                {
                    "name": p.name,
                    "href": _asset_href(job_id, p.name),
                    "size": sz,
                    "size_h": _human_size(sz),
                    "ext": ext,
                    "kind": kind,
                    "source_url": source_url,
                    "page_url": page_url,
                    "mime": mime,
                }
            )
    return {"job_id": job_id, "count": len(items), "items": items}


# Backwards-compatible alias for the old name. The endpoint was renamed
# from ``gallery.json`` to ``assets.json`` in 2026-05; external clients
# that hard-coded the old URL keep working but should migrate.
@router.get("/jobs/{job_id}/gallery.json", include_in_schema=False)
async def job_gallery_json(job_id: str) -> dict:
    return await job_assets_json(job_id)


@router.get("/jobs/{job_id}/screenshots.json")
async def job_screenshots_json(job_id: str) -> dict:
    """List every screenshot-like asset under this job, regardless of
    depth. Powers the Live panel's Screenshot tab viewer (operator-driven
    captures, ``page.screenshot()`` SDK calls, ``page.capture(label=...)``
    PNG dumps from codegen-loop / vision-agent attempts, and Fetch
    mode's passive PNG/JPG capture).

    The plain ``assets.json`` endpoint only enumerates the TOP-LEVEL
    assets/ directory, which misses ``page.capture()`` output that
    lands at ``assets/<label>/<label>.png`` and per-attempt screenshots
    at ``assets/.../final_screenshot.jpg``. This endpoint walks
    recursively and filters to image extensions, so the operator sees
    a single chronological stream of "what the browser looked like"
    regardless of which code path saved each PNG.

    Each item carries:
      * ``name``      -- filename only (no path)
      * ``path``      -- relative path under assets/ (e.g. "screenshot-...",
                          "post_verification/post_verification.png")
      * ``href``      -- absolute URL to fetch the PNG
      * ``size``      -- bytes
      * ``mtime``     -- file mtime as POSIX seconds (float)
      * ``label``     -- subdirectory the file lives in, or "" for top
                          level. Useful to group AI capture() output.

    Sorted by mtime ASCENDING so the array index lines up with
    chronology (UI defaults to showing the latest at the end).
    """
    await _soft_resolve_job(job_id, require_subdir="assets")
    assets_dir = get_storage_dir() / job_id / "assets"
    items: list[dict] = []
    # Classify which image files are "screenshots" (taken intentionally
    # by API / client / AI / operator) vs "page assets" (downloaded by
    # the browser as part of the crawled page itself, e.g. logo.png,
    # banner.gif). The latter belong in the asset gallery only.
    #
    # Heuristic:
    #   * Top-level image whose name starts with "screenshot-" -- the
    #     manual /screenshot endpoint's output. INCLUDE.
    #   * Image in a SUBDIRECTORY of assets/ -- output of
    #     ``page.capture(label="...")`` (saves <label>/<label>.png +
    #     .html + .axtree.json) and per-attempt final_screenshot.jpg
    #     under attempts/N/. INCLUDE.
    #   * Other top-level images (no "screenshot-" prefix) -- assumed
    #     page-downloaded asset. EXCLUDE.
    if assets_dir.exists():
        for p in assets_dir.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in _IMG_EXTS:
                continue
            try:
                rel = p.relative_to(assets_dir).as_posix()
            except Exception:
                rel = p.name
            in_subdir = "/" in rel
            is_named_screenshot = rel.lower().startswith("screenshot-")
            if not (in_subdir or is_named_screenshot):
                continue  # page-downloaded asset, not a screenshot
            try:
                st = p.stat()
            except Exception:
                continue
            label = rel.rsplit("/", 1)[0] if "/" in rel else ""
            items.append({
                "name": p.name,
                "path": rel,
                "href": _asset_href(job_id, rel),
                "size": st.st_size,
                "size_h": _human_size(st.st_size),
                "ext": ext,
                "mtime": st.st_mtime,
                "label": label,
            })
    items.sort(key=lambda d: (d["mtime"], d["path"]))
    return {"job_id": job_id, "count": len(items), "items": items}


@router.get("/ui/assets/{job_id}", response_class=HTMLResponse)
async def job_assets(job_id: str) -> str:
    """HTML assets browser: every asset saved for this job, grouped
    by type (images / videos / audio / others). Linked from the admin
    UI Live panel and the Recent Jobs row.

    URL moved through ``/gallery`` -> ``/screenshots`` -> ``/ui/attempts``
    -> ``/ui/assets`` over the project's lifetime. Each old path stays
    accepted as an
    alias so bookmarks / external links don't break, but the admin
    UI links + Swagger schema only show the latest one."""
    info = await _soft_resolve_job(job_id, require_subdir="assets")
    # ``info`` is None for session-routed parent_job_ids whose dir was
    # pre-created by create_session but never registered as a JobInfo.
    # The gallery HTML still renders for those (the file listing comes
    # from disk, not info), so fall back to safe display values.
    _info_url = (info.url if info is not None else "") or ""
    try:
        _info_status = info.status.value if (info is not None and info.status is not None) else "?"
    except Exception:
        _info_status = "?"
    assets_dir = get_storage_dir() / job_id / "assets"

    files: list[Path] = []
    if assets_dir.exists():
        files = sorted(
            (p for p in assets_dir.iterdir() if p.is_file()),
            key=lambda p: (p.suffix.lower(), p.name.lower()),
        )

    buckets = {"images": [], "videos": [], "audios": [], "others": []}
    for p in files:
        # Skip manual Capture-button outputs -- they belong to the
        # Screenshot tab only (see assets_json filter above).
        if p.name.lower().startswith("screenshot-"):
            continue
        ext = p.suffix.lower().lstrip(".")
        info_d = {
            "name": p.name,
            "href": _asset_href(job_id, p.name),
            "size": p.stat().st_size,
            "size_h": _human_size(p.stat().st_size),
            "ext": ext,
        }
        if ext in _IMG_EXTS:
            buckets["images"].append(info_d)
        elif ext in _VIDEO_EXTS:
            buckets["videos"].append(info_d)
        elif ext in _AUDIO_EXTS:
            buckets["audios"].append(info_d)
        else:
            buckets["others"].append(info_d)

    import html as _html

    def img_tile(a):
        return (
            f'<a class="tile img" href="{_html.escape(a["href"])}" target="_blank" '
            f'title="{_html.escape(a["name"])} — {a["size_h"]}">'
            f'<img loading="lazy" src="{_html.escape(a["href"])}" alt="">'
            f'<span class="cap">{_html.escape(a["name"])}<br>{a["size_h"]}</span>'
            f"</a>"
        )

    def vid_tile(a):
        return (
            f'<div class="tile vid">'
            f'<video controls preload="metadata" src="{_html.escape(a["href"])}"></video>'
            f'<span class="cap">'
            f'<a href="{_html.escape(a["href"])}" download>{_html.escape(a["name"])}</a>'
            f" — {a['size_h']}</span></div>"
        )

    def aud_tile(a):
        return (
            f'<div class="tile aud">'
            f'<audio controls preload="metadata" src="{_html.escape(a["href"])}"></audio>'
            f'<span class="cap">'
            f'<a href="{_html.escape(a["href"])}" download>{_html.escape(a["name"])}</a>'
            f" — {a['size_h']}</span></div>"
        )

    def other_tile(a):
        return (
            f'<a class="tile other" href="{_html.escape(a["href"])}" download '
            f'title="{_html.escape(a["name"])}">'
            f'<span class="ext">.{_html.escape(a["ext"] or "bin")}</span>'
            f'<span class="cap">{_html.escape(a["name"])}<br>{a["size_h"]}</span></a>'
        )

    def section(title, items, renderer):
        if not items:
            return ""
        return (
            f"<section><h2>{title} ({len(items)})</h2>"
            f'<div class="grid {title.lower()}">'
            + "".join(renderer(a) for a in items)
            + "</div></section>"
        )

    body = (
        section("Images", buckets["images"], img_tile)
        + section("Videos", buckets["videos"], vid_tile)
        + section("Audios", buckets["audios"], aud_tile)
        + section("Others", buckets["others"], other_tile)
    )
    if not body:
        # Distinguish "still running" from "done, captured nothing" so the
        # user doesn't think the gallery is broken on minimal pages like
        # example.com (which legitimately has no images/scripts to capture).
        status = _info_status
        if status in ("completed", "succeeded", "failed"):
            body = (
                '<section><p class="empty">'
                f"Job <code>{_html.escape(status)}</code> — no assets were captured.<br>"
                "This page may have no images / videos / external resources "
                "(e.g. <code>example.com</code> is intentionally minimal).<br>"
                "Try a richer URL to see the gallery populate."
                "</p></section>"
            )
        else:
            body = (
                '<section><p class="empty">'
                f"No assets yet. Job is currently <code>{_html.escape(status)}</code>; "
                "refresh in a moment."
                "</p></section>"
            )

    total = sum(len(v) for v in buckets.values())
    return f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>gallery — {_html.escape(job_id)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; margin: 0; background: #f5f3ef; color: #222; }}
  header {{ background: #c0392b; color: #fff; padding: .8rem 1.5rem; }}
  header a {{ color: #fff; text-decoration: none; }}
  header a:hover {{ text-decoration: underline; }}
  header h1 {{ margin: 0; font-size: 1.2rem; display: flex; align-items: center; gap: 0.4rem; }}
  header h1 .logo {{ width: 1.4em; height: 1.4em; vertical-align: middle; }}
  header .sub {{ font-size: .85rem; opacity: .9; margin-top: .25rem; }}
  header .sub a {{ font-family: ui-monospace, Consolas, monospace; }}
  main {{ max-width: 1400px; margin: 0 auto; padding: 1rem; }}
  section {{ background: #fff; margin-bottom: 1rem; padding: .8rem 1rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.07); }}
  h2 {{ margin: 0 0 .6rem; font-size: 1rem; color: #444; border-bottom: 1px solid #eee; padding-bottom: .3rem; }}
  .grid {{ display: grid; gap: .6rem; }}
  .grid.images {{ grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
  .grid.videos {{ grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }}
  .grid.audios {{ grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }}
  .grid.others {{ grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }}
  .tile {{ display: block; background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: .4rem; text-decoration: none; color: inherit; overflow: hidden; }}
  .tile.img img {{ width: 100%; height: 140px; object-fit: contain; background: #f0eee9; border-radius: 4px; }}
  .tile.vid video {{ width: 100%; max-height: 220px; background: #000; border-radius: 4px; }}
  .tile.aud audio {{ width: 100%; }}
  .tile.other {{ display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 110px; text-align: center; }}
  .tile.other .ext {{ font-size: 1.6rem; font-weight: 700; color: #c0392b; font-family: ui-monospace, Consolas, monospace; }}
  .cap {{ display: block; margin-top: .35rem; font-size: .78rem; color: #555; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .cap a {{ color: #c0392b; text-decoration: none; }}
  .cap a:hover {{ text-decoration: underline; }}
  .empty {{ color: #999; font-style: italic; }}
</style>
</head>
<body>
<header>
  <h1><img src="/icon.svg" alt="paprika" class="logo"> Gallery: <a href="/jobs/{_html.escape(job_id)}/result"><code>{_html.escape(job_id)}</code></a></h1>
  <div class="sub">
    URL: <a href="{_html.escape(_info_url)}" target="_blank">{_html.escape(_info_url)}</a>
    &nbsp;|&nbsp; status: {_html.escape(_info_status)}
    &nbsp;|&nbsp; total: <strong>{total}</strong> files
    &nbsp;|&nbsp;
    <a href="/">← admin</a>
    &nbsp;
    <a href="/jobs/{_html.escape(job_id)}/page.html" target="_blank">page.html</a>
    &nbsp;
    <a href="/jobs/{_html.escape(job_id)}/log.txt" target="_blank">log.txt</a>
  </div>
</header>
<main>
{body}
</main>
</body></html>"""


# Compat alias for the /ui/attempts -> /ui/assets rename. Bookmarked
# admin UI links / external integrations get a transparent pass-through.
# Hidden from the OpenAPI schema so new integrations land on the
# canonical name (/ui/assets/{id}). Drop on next breaking-change release.
@router.get(
    "/ui/attempts/{job_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_assets_attempts_legacy(job_id: str) -> str:
    return await job_assets(job_id)


# Job-scoped convenience path for the asset gallery. Renamed from the
# old /jobs/{id}/gallery and /jobs/{id}/screenshots (which both served
# this same HTML) into the assets vocabulary. The canonical UI entry
# point is /ui/assets/{id}; this one is handy for job-scoped links.
@router.get(
    "/jobs/{job_id}/assets.html",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_assets_html(job_id: str) -> str:
    return await job_assets(job_id)


# ============================================================================
# /jobs lifecycle (#2B-G2) -- list / get / result / visited / cancel /
# delete / cleanup + the /admin/cleanup_jobs legacy alias.
#
# Skipped from this batch: POST /jobs (create_job, 442 lines, dispatches
# into the worker pool; lifespan-touching), WS /jobs/{id}/events (live
# log stream), POST /jobs/{id}/screenshot, POST /jobs/{id}/assets +
# /assets/from_url + /files/{kind} (worker-secret-auth uploads).
# ============================================================================

import os
import shutil
from datetime import datetime

from server.hub.routes.novnc import _proxy_info
from server.protocol import AssetInfo, JobResult, JobStatus
from server.runner import DONE_SENTINEL


@router.get("/jobs")
async def list_jobs(
    request: Request,
    offset: int = 0,
    limit: int = 0,
    status: str | None = None,
    mode: str | None = None,
    q: str | None = None,
) -> dict:
    """List jobs with optional server-side pagination and filtering.

    Query params:
      * ``offset`` -- skip this many entries (default 0).
      * ``limit``  -- max entries to return (default 0 = all, max 500).
                      **Recommended** for MCP / CLI callers: ``limit=50``.
      * ``status`` -- filter by status (``running``, ``completed``,
                      ``failed``, ``cancelled``, ``queued``).
                      Comma-separated for multiple: ``status=completed,failed``.
      * ``mode``   -- filter by job mode (``fetch``, ``codegen-loop``, etc.).
      * ``q``      -- case-insensitive substring match against URL.

    Returns a paginated envelope::

        {total, count, offset, limit, jobs: [...]}

    When ``limit=0`` (the default), **all** matching jobs are returned
    (``jobs`` is the full array) so existing callers that expect a bare
    list keep working -- they just need to read ``resp.jobs`` (or
    iterate the array when the response *is* a list for the legacy
    ``Accept: application/json`` path).

    .. versionchanged:: 2026-05-26
       Added pagination (offset/limit) and filters (status/mode/q).
       Response shape changed from ``list[JobInfo]`` to the envelope
       dict above.  The admin UI was updated in the same commit.
    """
    assert state.store is not None
    lim = max(0, min(int(limit or 0), 500))

    # When no filters and limit>0, push offset/limit into the store
    # to avoid fetching all IDs from Redis.
    has_filter = bool(status or mode or q)
    if has_filter or lim == 0:
        ids = await state.store.list_job_ids()
        total_in_store = len(ids)
    else:
        total_in_store = await state.store.count_jobs()
        ids = await state.store.list_job_ids(offset=offset, limit=lim)

    # Hydrate
    infos: list[JobInfo] = []
    for jid in ids:
        info = await state.store.get_job_info(jid)
        if info is not None:
            infos.append(_proxy_info(info, request))

    # Apply filters (post-hydration because we need fields)
    if status:
        allowed = {s.strip().lower() for s in status.split(",")}
        infos = [i for i in infos if i.status.value in allowed]
    if mode:
        allowed_modes = {m.strip().lower() for m in mode.split(",")}
        infos = [
            i for i in infos
            if (i.options.get("mode") if isinstance(i.options, dict)
                else getattr(i.options, "mode", "fetch") or "fetch"
               ).lower() in allowed_modes
        ]
    if q:
        ql = q.lower()
        infos = [i for i in infos if ql in (i.url or "").lower()]

    filtered_total = len(infos)

    # Paginate (only when filters were applied client-side)
    off = max(0, int(offset or 0))
    if has_filter:
        if lim > 0:
            page = infos[off : off + lim]
        else:
            page = infos[off:] if off else infos
    else:
        # Already sliced at the store level (or lim=0 → all)
        if lim == 0:
            page = infos[off:] if off else infos
        else:
            page = infos  # already sliced
            filtered_total = total_in_store

    return {
        "total": filtered_total,
        "count": len(page),
        "offset": off,
        "limit": lim,
        "jobs": page,
    }


@router.get("/jobs/{job_id}", response_model=JobInfo)
async def get_job(job_id: str, request: Request) -> JobInfo:
    info = await _require_job_info(job_id)
    # Rewrite novnc_url to point at the hub's noVNC proxy so external
    # clients don't need to reach individual worker LAN IPs. See
    # ``_hub_proxied_novnc_url`` and the /jobs/{id}/novnc/* endpoints
    # below for the proxy implementation.
    return _proxy_info(info, request)


def _backfill_asset_metadata(job_id: str, result: JobResult) -> JobResult:
    """For each ``AssetInfo`` in ``result.assets`` whose ``url`` / ``mime``
    / ``page_url`` is missing, try to fill it in from the on-disk
    ``.meta/<name>.json`` sidecar.

    Why this exists: older worker builds saved ``JobResult`` to Redis
    before the protocol gained the ``page_url`` field (and before the
    fetch-mode upload path even passed ``source_url`` along). Those
    historical entries are frozen in Redis with ``page_url=None``. But
    the asset upload endpoint always wrote a ``.meta/`` sidecar with
    the full metadata, so we can recover the missing fields on read
    instead of forcing a re-crawl.

    No write-back: we just patch the dict before returning. Idempotent
    on jobs that already had full metadata.
    """
    meta_dir = get_storage_dir() / job_id / "assets" / ".meta"
    if not meta_dir.is_dir():
        return result
    patched: list[AssetInfo] = []
    changed = False
    for a in result.assets:
        if a.url and a.page_url and a.mime:
            patched.append(a)
            continue
        meta_path = meta_dir / f"{a.name}.json"
        if not meta_path.exists():
            patched.append(a)
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            patched.append(a)
            continue
        new_url = a.url or meta.get("source_url")
        new_page_url = a.page_url or meta.get("page_url")
        new_mime = a.mime or meta.get("mime")
        if new_url != a.url or new_page_url != a.page_url or new_mime != a.mime:
            changed = True
            patched.append(
                a.model_copy(
                    update={
                        "url": new_url,
                        "page_url": new_page_url,
                        "mime": new_mime,
                    }
                )
            )
        else:
            patched.append(a)
    if not changed:
        return result
    return result.model_copy(update={"assets": patched})


@router.get("/jobs/{job_id}/result", response_model=JobResult)
async def get_job_result(job_id: str) -> JobResult:
    info = await _require_job_info(job_id)
    if info.status not in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        raise HTTPException(409, f"job not finished (status={info.status})")
    result = await state.store.get_job_result(job_id)
    if result is None:
        return JobResult(job_id=job_id, status=info.status, error=info.error)
    # Patch missing url / page_url / mime from on-disk .meta/ sidecars.
    # Covers jobs persisted before the protocol gained page_url.
    return _backfill_asset_metadata(job_id, result)


@router.get("/jobs/{job_id}/visited")
async def get_job_visited(job_id: str) -> dict:
    """Return the list of canonical URLs the agent visited during the job.

    Mostly empty for plain-fetch jobs; populated for agent-mode jobs
    (i.e. those launched with JobOptions.goal). Same data exposed in
    JobResult.visited_urls, broken out so dashboards / scripts can hit
    a stable JSON shape without parsing the full result object.
    """
    info = await _require_job_info(job_id)
    result = await state.store.get_job_result(job_id)
    urls = list(result.visited_urls) if result else []
    return {
        "job_id": job_id,
        "status": info.status,
        "count": len(urls),
        "visited_urls": urls,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Cancel an in-flight job. Cancels the orchestrator task (which
    propagates to execute_in_sandbox -> the docker runner subprocess
    gets killed), then marks the job as ``cancelled`` and broadcasts a
    final DONE_SENTINEL so /jobs/{id}/events subscribers unblock.

    Idempotent: cancelling an already-finished job is a no-op
    returning ``{cancelled: false}``."""
    info = await _require_job_info(job_id)
    if info.status not in (JobStatus.queued, JobStatus.running):
        return {
            "job_id": job_id,
            "cancelled": False,
            "reason": f"job already {info.status}",
        }
    t = state.local_tasks.pop(job_id, None)
    cancelled_task = False
    if t and not t.done():
        t.cancel()
        cancelled_task = True

    # Force a terminal state on the JobInfo so the admin UI flips the
    # badge to "cancelled" immediately, even before the orchestrator's
    # except path persists its own update.
    info.status = JobStatus.cancelled
    info.error = "cancelled by user"
    info.completed_at = datetime.utcnow()
    if info.progress is not None:
        info.progress.phase = "cancelled"
        info.progress.last_log = "[user] job cancelled"
    await state.store.save_job_info(info)
    try:
        await state.store.publish_log(job_id, "[user] job cancelled")
        await state.store.publish_log(job_id, DONE_SENTINEL)
    except Exception:
        pass
    # Best-effort: close any sessions this job still owns. _cleanup_
    # orphan_sessions is defined inside _run_codegen_loop_job's scope
    # so we replicate the minimal flow here.
    if state.sessions is not None and state.registry is not None:
        for sess in [s for s in state.sessions.all() if s.job_id == job_id]:
            sid = sess.session_id
            state.sessions.remove(sid)
            worker = state.registry.connections.get(sess.worker_id)
            if worker is None:
                continue
            try:
                await worker.end_session(sid, timeout=5.0)
            except Exception:
                pass
    return {"job_id": job_id, "cancelled": True, "task_was_running": cancelled_task}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    assert state.store is not None
    t = state.local_tasks.pop(job_id, None)
    if t and not t.done():
        t.cancel()
    deleted = await state.store.delete_job(job_id)
    job_dir = get_storage_dir() / job_id
    try:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass
    if not deleted:
        raise HTTPException(404, f"job '{job_id}' not found")
    return {"deleted": job_id}


def _job_dir_size_bytes(job_id: str) -> int:
    """Walk the job's data dir and sum file sizes. Best-effort -- a
    permission error or vanished file just stops counting that branch."""
    total = 0
    job_dir = get_storage_dir() / job_id
    if not job_dir.exists():
        return 0
    try:
        for root, _dirs, files in os.walk(job_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


@router.post("/jobs/cleanup")
async def cleanup_jobs(body: dict) -> dict:
    """Bulk delete old / large jobs to reclaim disk.

    Sits under ``/jobs`` (alongside the per-job verb actions
    ``cancel`` / ``screenshot``) so the Swagger ``Jobs`` section
    surfaces it where operators expect bulk maintenance to live.

    The legacy URL ``POST /admin/cleanup_jobs`` is retained as a
    hidden alias for one release cycle so existing cron scripts /
    admin-UI HTML keep working without a same-day flag day.

    Body knobs (all optional, AND-ed together when multiple are given):

      older_than_days: int
          Only candidates whose ``completed_at`` (or ``created_at``
          if completed_at is null) is older than N days.
      status_in: list[str]
          Only candidates whose ``status`` is in this list. Default
          ["completed", "failed", "cancelled"] -- in-flight jobs are
          NEVER cleaned even if explicitly requested.
      min_size_mb: int
          Only candidates whose on-disk size is at least N MiB.
      keep_last: int
          Always keep the N most-recently-created jobs regardless of
          age / size. Default 10 -- protects "show me the latest" UX.
      dry_run: bool
          If true, just return what WOULD be deleted. Default false.

    Returns ``{candidates: [...], deleted: [...], total_freed_bytes,
    skipped: [...], dry_run}``.
    """
    assert state.store is not None
    body = body or {}
    older_than_days = body.get("older_than_days")
    min_size_mb = body.get("min_size_mb")
    keep_last = int(body.get("keep_last") or 10)
    dry_run = bool(body.get("dry_run") or False)
    status_in = set(body.get("status_in") or ["completed", "failed", "cancelled"])

    # Enumerate all jobs with metadata.
    job_ids = await state.store.list_job_ids()
    rows: list[dict] = []
    for jid in job_ids:
        info = await state.store.get_job_info(jid)
        if info is None:
            continue
        size = _job_dir_size_bytes(jid)
        when = info.completed_at or info.created_at
        rows.append(
            {
                "job_id": jid,
                "status": str(info.status).split(".")[-1],
                "created_at": (info.created_at.isoformat() + "Z") if info.created_at else None,
                "completed_at": (info.completed_at.isoformat() + "Z")
                if info.completed_at
                else None,
                "size_bytes": size,
                "_when": when,
            }
        )

    # Sort newest first, reserve the keep_last "always keep" set.
    rows.sort(key=lambda r: r["_when"] or datetime.min, reverse=True)
    protected = {r["job_id"] for r in rows[: max(0, keep_last)]}

    now = datetime.utcnow()
    candidates: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        reason_keep: str | None = None
        if r["job_id"] in protected:
            reason_keep = f"protected by keep_last={keep_last}"
        elif r["status"] not in status_in:
            reason_keep = f"status={r['status']!r} not in delete set (probably still running)"
        elif older_than_days is not None:
            when = r["_when"]
            if when is None or (now - when).total_seconds() < int(older_than_days) * 86400:
                reason_keep = f"younger than {older_than_days} day(s)"
        if reason_keep is None and min_size_mb is not None:
            if r["size_bytes"] < int(min_size_mb) * 1024 * 1024:
                reason_keep = f"size {r['size_bytes']} < {int(min_size_mb)} MiB"
        if reason_keep:
            skipped.append({"job_id": r["job_id"], "reason": reason_keep})
        else:
            candidates.append(
                {
                    "job_id": r["job_id"],
                    "status": r["status"],
                    "size_bytes": r["size_bytes"],
                    "age_days": (now - r["_when"]).total_seconds() / 86400 if r["_when"] else None,
                }
            )

    deleted: list[str] = []
    total_freed = 0
    if not dry_run:
        for c in candidates:
            try:
                t = state.local_tasks.pop(c["job_id"], None)
                if t and not t.done():
                    t.cancel()
                await state.store.delete_job(c["job_id"])
                d = get_storage_dir() / c["job_id"]
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                deleted.append(c["job_id"])
                total_freed += c["size_bytes"]
            except Exception as e:
                skipped.append({"job_id": c["job_id"], "reason": f"delete failed: {e}"})

    return {
        "dry_run": dry_run,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "candidate_total_bytes": sum(c["size_bytes"] for c in candidates),
        "deleted": deleted,
        "total_freed_bytes": total_freed,
        "skipped": skipped,
        "protected_count": len(protected),
    }


# Legacy alias for the /admin/cleanup_jobs -> /jobs/cleanup rename.
# Hidden from OpenAPI so new integrations land on the canonical
# /jobs/cleanup endpoint; cron scripts and bookmarked admin UI keep
# working through one release cycle.
@router.post("/admin/cleanup_jobs", include_in_schema=False)
async def cleanup_jobs_legacy(body: dict) -> dict:
    return await cleanup_jobs(body)


# ============================================================================
# /jobs upload + screenshot endpoints (#2B-G2)
# ----------------------------------------------------------------------------
# POST /jobs/{id}/screenshot, /assets, /assets/from_url, /files/{kind}.
# These are the worker-secret-gated upload routes plus the operator-
# triggered "snapshot now" button. _ffmpeg_q_from_quality_pct stays in
# app.py (also used by worker_lane_preview which is still there).
# ============================================================================

import uuid


# _ffmpeg_q_from_quality_pct lives in app.py and is defined AFTER the
# include_router stanza for this module, so eager import would race
# the partial-app-load. Lazy-import at call time -- happy path, no
# perf hit (one attribute lookup per /jobs/{id}/screenshot request).
def _ffmpeg_q_from_quality_pct(pct: int) -> int:
    from server.hub._helpers import _ffmpeg_q_from_quality_pct as _impl

    return _impl(pct)


# ----------------------------------------------------------------------------
# /jobs/{id}/screenshot — take + save a screenshot now
# (URL renamed from /screenshot/capture; old path retained as alias)
# ----------------------------------------------------------------------------


@router.post("/jobs/{job_id}/screenshot")
async def take_job_screenshot(
    job_id: str,
    width: int = 1280,
    quality: int = 85,
) -> dict:
    """**SCREENSHOT** action: take a high-quality screenshot of the
    job's lane and save it as a JPEG asset.

    Returns ``{ok, name, size, mime, href}``. The saved asset shows
    up in ``/jobs/{id}/assets.json`` alongside everything else, so
    ``/ui/assets/{id}`` (the HTML asset gallery) + the per-job result
    page pick it up automatically.

    Used by the Submit-form Live panel's "Screenshot" button so an
    operator can manually snapshot the running browser without
    waiting for the script to call ``page.capture()``.

    Width / quality default to "good for human review" -- 1280px,
    quality=85 on the 0-100 perceptual scale (= ffmpeg q≈6, high
    quality). This is the opposite end of the cost spectrum from
    :func:`worker_lane_screenshot` (PREVIEW endpoint) which targets
    ~30-80 KB thumbnails.

    Note: previously ``quality`` was being passed raw to the worker's
    ffmpeg q:v parameter (2-31 inverted scale), so ``quality=80``
    actually produced the *worst* possible quality. Fixed in this
    revision; ``quality`` now consistently means "higher = better".
    """
    if state.store is None:
        raise HTTPException(503, "store not ready")
    info = await _require_job_info(job_id)
    worker_id = info.worker_id
    lane_idx = info.lane_idx
    if not worker_id or lane_idx is None:
        raise HTTPException(
            409,
            "job has no lane bound yet (worker_id or lane_idx missing)",
        )
    if state.registry is None:
        raise HTTPException(503, "registry not ready")
    worker = state.registry.connections.get(worker_id)
    if worker is None:
        raise HTTPException(
            502,
            f"worker '{worker_id}' is no longer connected",
        )
    ffmpeg_q = _ffmpeg_q_from_quality_pct(quality)
    try:
        reply = await worker.request_screenshot(
            int(lane_idx),
            max_width=int(width),
            quality=ffmpeg_q,
        )
    except TimeoutError:
        raise HTTPException(504, "screenshot timed out")
    except Exception as e:
        raise HTTPException(502, f"screenshot send failed: {e}")
    if reply.error:
        raise HTTPException(502, f"worker error: {reply.error}")

    import base64

    try:
        jpeg = base64.b64decode(reply.jpeg_b64)
    except Exception:
        raise HTTPException(502, "worker returned invalid base64")
    if not jpeg:
        raise HTTPException(502, "worker returned empty image")

    # Name the file by capture timestamp so the asset list reads in
    # chronological order. Use ``.jpg`` extension to match the wire
    # MIME (no client-side re-encode needed); ``screenshot-`` prefix
    # so the Screenshot-tab thumbnail strip can filter for them.
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    # Sub-second salt so back-to-back captures don't collide.
    salt = uuid.uuid4().hex[:4]
    name = f"screenshot-{ts}-{salt}.jpg"

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / name
    target_path.write_bytes(jpeg)

    # Sidecar metadata so the gallery popup shows context.
    try:
        meta_dir = target_dir / ".meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source": "manual-screenshot",
                    "page_url": info.url,
                    "mime": "image/jpeg",
                    "size": len(jpeg),
                    "captured_at": datetime.utcnow().isoformat() + "Z",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target_path)

    return {
        "ok": True,
        "name": name,
        "size": len(jpeg),
        "mime": "image/jpeg",
        "href": _asset_href(job_id, name),
    }


# Legacy alias for the /screenshot/capture -> /screenshot rename.
# Hidden from OpenAPI so new integrations land on the canonical name.
# Drop on next breaking-change release.
@router.post(
    "/jobs/{job_id}/screenshot/capture",
    include_in_schema=False,
)
async def take_job_screenshot_legacy(
    job_id: str,
    width: int = 1280,
    quality: int = 85,
) -> dict:
    return await take_job_screenshot(job_id=job_id, width=width, quality=quality)


# ----------------------------------------------------------------------------
# /jobs/{id}/assets — worker-side upload endpoint
# ----------------------------------------------------------------------------


@router.post("/jobs/{job_id}/assets")
async def upload_asset(
    job_id: str,
    file: UploadFile = File(...),
    asset_name: str | None = Form(None),
    secret: str | None = Form(None),
    source_url: str | None = Form(None),
    mime: str | None = Form(None),
    page_url: str | None = Form(None),
):
    """Receive a file uploaded by a worker. Stored under
    data/jobs/{job_id}/assets/{asset_name}.

    Optional metadata captured by the session-mode passive listener:
      - ``source_url``: the URL the browser fetched this resource from
      - ``mime``:       the response's content-type
      - ``page_url``:   the URL of the page that initiated the request
                        (CDP's Network.RequestWillBeSent.documentURL).
                        Lets the gallery popup answer "which page did
                        this image come from", even if the script
                        subsequently navigated away from that page.
    When provided, persisted to a JSON sidecar
    (``assets/.meta/<name>.json``) the gallery endpoint reads back.
    """
    if config.worker_secret and secret != config.worker_secret:
        raise HTTPException(401, "bad secret")
    # Soft 404 via the shared helper: accept session-routed
    # parent_job_ids whose assets dir create_session pre-made, so raw
    # cli.session(parent_job_id=...) callers (e.g. an external crawler
    # publishing page.screenshot(label=) frames) can populate a gallery
    # the admin UI shows at /ui/assets/<id> without first POSTing a
    # /jobs entry. The 404 still fires for genuinely-unknown ids.
    await _soft_resolve_job(job_id, require_subdir="assets")

    name = asset_name or file.filename or "unnamed"
    # sanitize
    import re

    name = re.sub(r'[<>:"/\\|?*]', "_", name)[:180]

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name

    # Stream the upload to disk in 1 MiB chunks instead of loading
    # the entire body into memory via ``await file.read()``. For a
    # large video (1+ GB iframe-mp4 captured by the passive m3u8 /
    # mp4 listener) the buffered form pushed the hub container past
    # its 2 GB cgroup memory cap and tripped the OOM killer mid-
    # upload -- the file was lost AND every other in-flight session
    # died because the hub process was replaced. Streaming write keeps
    # peak memory bounded at chunk size regardless of body length.
    # Use a .part suffix + atomic rename so readers never see a half-
    # written file under the final name.
    part = target.with_suffix(target.suffix + ".part")
    written = 0
    try:
        with open(part, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MiB
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
        part.replace(target)
    finally:
        # If the upload was interrupted, .part may still exist.
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

    # Sidecar metadata. Best-effort -- failure to write the .meta JSON
    # must not fail the asset upload (the file itself is the important
    # artefact; metadata is a UX nicety).
    if source_url or mime or page_url:
        try:
            meta_dir = target_dir / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / f"{name}.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "source_url": source_url or None,
                        "page_url": page_url or None,
                        "mime": mime or None,
                        "size": written,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    # Multi-hub foundation: mirror to shared object storage (no-op unless
    # PAPRIKA_S3_ENABLED). Local disk stays the source of truth.
    await objstore.mirror_file(target)

    return {"saved": str(target.resolve()), "size": written, "name": name}


@router.post("/jobs/{job_id}/assets/from_url")
async def save_asset_from_url(job_id: str, body: dict) -> dict:
    """Download a resource by URL and save it as a job asset.

    Used by the Live panel "Network" tab: the operator sees a media
    response in the traffic log and clicks "add to assets". The hub
    fetches the URL (server-side, so cookies/auth don't matter — the
    content is already in the browser's cache; we're using the URL
    as a cache key to re-fetch) and stores it under the job's assets.

    Body::

        {
          "url": "https://cdn.example.com/thumb.jpg",
          "mime": "image/jpeg",          // optional hint
          "page_url": "https://...",     // optional
        }
    """
    info = await _require_job_info(job_id)

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    import re as _re

    # Derive filename from URL.
    from urllib.parse import unquote, urlparse

    import httpx

    parsed = urlparse(url)
    raw_name = unquote(parsed.path.split("/")[-1] or "resource")
    name = _re.sub(r'[<>:"/\\|?*]', "_", raw_name)[:180]
    if not name or name == ".":
        name = "resource"

    target_dir = get_storage_dir() / job_id / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Dedup: if a file with the same name already exists, skip.
    target = target_dir / name
    if target.exists():
        return {
            "status": "already_exists",
            "name": name,
            "size": target.stat().st_size,
        }

    # Download from URL.
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
    except Exception as e:
        raise HTTPException(
            502,
            f"download failed: {type(e).__name__}: {e}",
        )

    target.write_bytes(content)

    # Sidecar metadata.
    mime = body.get("mime") or ""
    page_url = body.get("page_url") or ""
    try:
        meta_dir = target_dir / ".meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "source_url": url,
                    "page_url": page_url or None,
                    "mime": mime or None,
                    "size": len(content),
                    "source": "network-tab",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target)

    return {"status": "saved", "name": name, "size": len(content)}


# Special files (page.html, log.txt) — worker uploads via the same handler
# pattern. We accept any of "page.html" / "log.txt" / regular asset names.
@router.post("/jobs/{job_id}/files/{kind}")
async def upload_special(
    job_id: str,
    kind: str,
    file: UploadFile = File(...),
    secret: str | None = Form(None),
):
    """Upload a special file (currently 'page.html' or 'log.txt')."""
    if config.worker_secret and secret != config.worker_secret:
        raise HTTPException(401, "bad secret")
    if kind not in ("page.html", "log.txt"):
        raise HTTPException(400, "kind must be 'page.html' or 'log.txt'")
    await _require_job_info(job_id)
    target = get_storage_dir() / job_id / kind
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stream chunked (1 MiB) instead of buffering the whole body in
    # RAM. Page HTML can run 10+ MB on heavily-scripted sites; not as
    # catastrophic as the /assets video case but still wasteful.
    total = 0
    with open(target, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    # Mirror to shared object storage (dormant unless PAPRIKA_S3_ENABLED).
    await objstore.mirror_file(target)
    return {"saved": str(target.resolve()), "size": total}


# ============================================================================
# /jobs WS events (#2B-G2)
# ----------------------------------------------------------------------------
# Live log stream that the admin UI's Live panel subscribes to. Uses
# state.store.subscribe_log (Redis pub/sub) as the upstream feed.
# ============================================================================

from fastapi import WebSocket, WebSocketDisconnect

from server.protocol import Event

# ----------------------------------------------------------------------------
# Live log WebSocket (client-facing)
# ----------------------------------------------------------------------------


@router.websocket("/jobs/{job_id}/events")
async def job_events(ws: WebSocket, job_id: str, since: int = 0):
    """Live log stream for a job.

    Query parameter `since` is a 0-based line offset: the client passes
    the number of log lines it has already rendered, and the server skips
    that many before streaming the rest. This makes reconnects cheap and
    prevents the browser from re-painting the entire history every time
    the connection bounces (the classic "live log flicker" bug).
    """
    await ws.accept()
    assert state.store is not None

    info = await state.store.get_job_info(job_id)
    if info is None:
        await ws.send_json({"type": "error", "data": {"message": "job not found"}})
        await ws.close()
        return

    try:
        existing = await state.store.get_log_lines(job_id)
        # Skip what the client has already rendered.
        start = max(0, int(since or 0))
        for line in existing[start:]:
            await ws.send_json(
                Event(type="log", job_id=job_id, data={"line": line}).model_dump(mode="json")
            )
    except Exception:
        pass

    if info.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        await ws.send_json(
            Event(type="done", job_id=job_id, data={"status": info.status.value}).model_dump(
                mode="json"
            )
        )
        return

    try:
        async for line in state.store.subscribe_log(job_id):
            if line == DONE_SENTINEL:
                final = await state.store.get_job_info(job_id)
                await ws.send_json(
                    Event(
                        type="done",
                        job_id=job_id,
                        data={"status": (final.status.value if final else "unknown")},
                    ).model_dump(mode="json")
                )
                return
            await ws.send_json(
                Event(type="log", job_id=job_id, data={"line": line}).model_dump(mode="json")
            )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass


# ============================================================================
# Standalone live-log HTML viewer (#2B-G3-partial). Renders an HTML page
# that connects to /jobs/{id}/events (WS) and tails the log in real time.
# ============================================================================


_LIVE_LOG_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Paprika · live log</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: #0f0f10; color: #e5e5e5; font: 14px/1.5 -apple-system,"Segoe UI",sans-serif; display: flex; flex-direction: column; }
  header {
    display: flex; align-items: center; gap: 1rem;
    padding: .6rem 1.1rem;
    background: #c0392b; color: #fff;
    flex-shrink: 0;
    box-shadow: 0 2px 6px rgba(0,0,0,.4);
  }
  header h1 { margin: 0; font-size: 1rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.4rem; }
  header h1 .logo { width: 1.4em; height: 1.4em; vertical-align: middle; flex-shrink: 0; }
  header h1 .jid { font-family: ui-monospace,Consolas,monospace; background: rgba(0,0,0,.2); padding: 1px 8px; border-radius: 4px; font-size: .85rem; margin-left: .4rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: .75rem; font-weight: 600; margin-left: .4rem; }
  .badge.completed { background: #d4f5d8; color: #185c2c; }
  .badge.failed    { background: #fbe0e0; color: #8a1f1f; }
  .badge.running   { background: #fff2cc; color: #7a5a14; }
  .badge.queued    { background: #e6e6e6; color: #555; }
  .badge.cancelled { background: #e0e0e0; color: #777; }
  .ctrl { display: flex; align-items: center; gap: .8rem; margin-left: auto; font-size: .85rem; }
  .ctrl label { display: flex; align-items: center; gap: .35rem; }
  .ctrl a { color: #ffe; text-decoration: none; opacity: .85; }
  .ctrl a:hover { opacity: 1; text-decoration: underline; }
  .ctrl button {
    padding: 3px 10px; font: inherit; cursor: pointer;
    background: rgba(255,255,255,.15); color: #fff;
    border: 1px solid rgba(255,255,255,.35); border-radius: 4px;
  }
  .ctrl button:hover { background: rgba(255,255,255,.25); }
  main {
    flex: 1; overflow: auto;
    padding: .6rem 1rem;
    font-family: ui-monospace,Consolas,"Cascadia Mono",monospace;
    font-size: 13px; line-height: 1.45;
  }
  pre#log { margin: 0; white-space: pre-wrap; word-wrap: break-word; color: #d6d6d6; }
  .meta { color: #888; padding: 6px 0; font-style: italic; }
  .meta.err { color: #ff8b8b; }
  .meta.done { color: #6ee06e; }
</style>
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:8px;" title="ホーム (Submit form) に戻る"><img src="/icon.svg" alt="paprika" class="logo"> Paprika</a> · <span>live log</span> <span class="jid" id="jid"></span> <span class="badge" id="badge">…</span></h1>
  <span class="ctrl">
    <label><input type="checkbox" id="follow" checked> auto-scroll</label>
    <button id="clearBtn" title="Clear screen (doesn't affect the stored log)">clear</button>
    <a href="" id="rawLink" target="_blank" title="Open the raw log.txt">↗ raw</a>
    <a href="/" title="back to admin UI">← admin</a>
  </span>
</header>
<main id="logBox">
  <pre id="log"></pre>
</main>
<script>
const JOB_ID = window.location.pathname.split('/')[2];
document.getElementById('jid').textContent = JOB_ID;
document.getElementById('rawLink').href = '/jobs/' + encodeURIComponent(JOB_ID) + '/log.txt';

const logEl = document.getElementById('log');
const boxEl = document.getElementById('logBox');
const badge = document.getElementById('badge');

// `seen` is our cursor into the server-side log: how many lines we've
// already rendered. On reconnect we pass `?since=seen` so the server
// only sends what we haven't seen -- no full-history re-dump, no
// flicker, no exponentially growing scroll buffer.
let seen = 0;
// True once we got the 'done' event and deliberately closed the socket.
// Stops the auto-reconnect loop that would otherwise re-fetch history
// every backoff window even though the job is already finished.
let finished = false;

// Coalesce many log lines arriving in one tick into a single DOM write
// (one layout + one scroll) using requestAnimationFrame. This is the
// pattern most heavy-traffic log viewers (CI dashboards etc.) use to
// stop the browser from thrashing when a worker dumps 1000 lines/sec.
const pending = [];
let flushScheduled = false;
function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  requestAnimationFrame(() => {
    flushScheduled = false;
    if (!pending.length) return;
    const frag = document.createDocumentFragment();
    for (const item of pending) {
      if (item.kind === 'line') {
        frag.appendChild(document.createTextNode(item.text + '\n'));
      } else {
        const div = document.createElement('div');
        div.className = 'meta' + (item.cls ? ' ' + item.cls : '');
        div.textContent = item.text;
        frag.appendChild(div);
      }
    }
    pending.length = 0;
    logEl.appendChild(frag);
    if (document.getElementById('follow').checked) {
      boxEl.scrollTop = boxEl.scrollHeight;
    }
  });
}
function appendLine(text) { pending.push({kind:'line', text}); seen++; scheduleFlush(); }
function appendMeta(text, cls) { pending.push({kind:'meta', text, cls}); scheduleFlush(); }

function setStatus(status) {
  // Only touch the DOM when the value actually changed -- otherwise the
  // periodic status poll causes a visible "blink" on the badge.
  const next = status || '—';
  if (badge.textContent === next) return;
  badge.className = 'badge ' + (status || '');
  badge.textContent = next;
}

async function refreshStatus() {
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(JOB_ID));
    if (!r.ok) return;
    const info = await r.json();
    setStatus(info.status);
  } catch (_) {}
}

document.getElementById('clearBtn').addEventListener('click', () => {
  // Clear the screen but don't reset `seen` -- we don't want a reconnect
  // to re-render lines the user just cleared.
  logEl.innerHTML = '';
});

// Open the WS log stream.
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
function buildUrl() {
  return `${proto}//${location.host}/jobs/${encodeURIComponent(JOB_ID)}/events?since=${seen}`;
}
let ws;
let backoff = 1000;
function connect() {
  if (finished) return;
  const url = buildUrl();
  ws = new WebSocket(url);
  ws.onopen = () => {
    backoff = 1000;
    appendMeta(seen === 0 ? '— connected' : `— reconnected (resuming from line ${seen})`);
    refreshStatus();
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { appendLine(e.data); return; }
    if (ev.type === 'log') {
      appendLine(ev.data && ev.data.line ? ev.data.line : '');
    } else if (ev.type === 'done') {
      const st = ev.data && ev.data.status;
      setStatus(st);
      appendMeta('— job ended: ' + st, 'done');
      finished = true;
      try { ws.close(); } catch (_) {}
    } else if (ev.type === 'error') {
      appendMeta('error: ' + (ev.data && ev.data.message), 'err');
    } else {
      appendLine(e.data);
    }
  };
  ws.onerror = () => { /* onclose will follow; handle there */ };
  ws.onclose = () => {
    if (finished) return;  // intentional close after 'done' -- don't reconnect
    appendMeta(`— disconnected; reconnecting in ${(backoff/1000)|0}s`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
}
connect();
// Status polling is cheap (one small JSON) but only updates the badge
// when the value actually changed, so 10s is plenty.
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""


@router.get("/ui/log/{job_id}", response_class=HTMLResponse)
async def job_live_log_page(job_id: str) -> str:
    """Standalone HTML viewer that tails /jobs/{id}/events in real time.

    URL renamed from ``/jobs/{id}/log`` to ``/ui/log/{id}`` so admin
    UI surfaces sit under a stable ``/ui/`` namespace (mirrors
    ``/ui/assets/{id}``). The old path stays accepted as a legacy
    alias just below.

    Note: ``/jobs/{id}/log.txt`` (the raw file download) is still
    served by ``get_log`` above; this route returns an HTML page
    instead.
    """
    # We don't 404 here even if the job is unknown -- the page reads the
    # job_id from window.location and the events WS already handles
    # "job not found" cleanly.
    _ = job_id  # job_id is taken from the URL on the client side
    return _LIVE_LOG_HTML


@router.get(
    "/jobs/{job_id}/log",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_live_log_page_legacy(job_id: str) -> str:
    return await job_live_log_page(job_id)


# ============================================================================
# POST /jobs -- create_job (#2B-G3). The 440-line dispatch that builds
# the JobInfo, applies per-host cookie auto-injection, picks the right
# mode (fetch / codegen-loop / rerun / vision-agent), and hands off to
# the appropriate orchestrator. Three orchestrator-side entry points
# (_run_codegen_loop_job, _run_rerun_loop_job, resolve_rerun_source)
# stay in app.py / iterative_codegen.py and are lazy-imported below.
# ============================================================================

import time

from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.iterative_codegen import resolve_rerun_source
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import (
    HubAssignJob,
    JobProgress,
    JobRequest,
)


# Orchestrator entry points + helpers that stay in app.py (lifespan-
# touching, called from outside create_job too). Lazy-import inside
# thin wrappers so we don't race the partial-app-load at boot time.
def _run_codegen_loop_job(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Thin wrapper that lazy-imports the orchestrator entry point so
    we don't race the partial-app-load at boot time. ``*args, **kwargs``
    so this layer doesn't have to track every signature change in
    ``server/hub/_jobrunner.py`` (the wrapper was previously hard-coded
    to ``(request, info)`` and silently broke when extra kwargs were
    added)."""
    from server.hub._jobrunner import _run_codegen_loop_job as _impl

    return _impl(*args, **kwargs)


def _run_rerun_loop_job(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Same lazy-import / pass-through pattern as above.

    ``_run_rerun_loop_job`` in _jobrunner.py takes
    ``(info, script_code, source_label, *, inherited_state_files=0,
    ...)``; this wrapper was previously declared as
    ``(request, info, source_jid)`` which made every rerun job 500 with
    "unexpected keyword argument 'inherited_state_files'"."""
    from server.hub._jobrunner import _run_rerun_loop_job as _impl

    return _impl(*args, **kwargs)


def _copy_session_state_dir(src_job_id: str, dst_job_id: str) -> int:
    from server.hub._jobrunner import _copy_session_state_dir as _impl

    return _impl(src_job_id, dst_job_id)


async def _ensure_host_login(host: str, *, force: bool = False) -> dict:
    from server.hub.routes.hosts import _ensure_host_login as _impl

    return await _impl(host, force=force)


def _hub_base_url(request) -> str:  # type: ignore[no-untyped-def]
    from server.hub._helpers import _hub_base_url as _impl

    return _impl(request)


def _asset_upload_url(base: str, job_id: str) -> str:
    from server.hub._helpers import _asset_upload_url as _impl

    return _impl(base, job_id)


# Constants from app.py; defined BEFORE the include_router stanza fires,
# so eager import is safe.
from server.hub.app import (  # noqa: E402
    _JOB_DISPATCH_POLL_S,
    JOB_DISPATCH_GRACE_S,
)

# ----------------------------------------------------------------------------
# /jobs endpoints (client-facing)
# ----------------------------------------------------------------------------


# state-model v1.1: queued-timeout guard.  A job that never leaves
# `queued` (no worker/lane, or a dispatch task that died) should resolve
# to a terminal state, not sit forever.  Implemented event-driven (one
# fire-once task per job) rather than polling the whole store.
_QUEUE_TIMEOUT_S = float(os.environ.get("PAPRIKA_QUEUE_TIMEOUT_S", "180"))
_QUEUE_GUARD_TASKS: set = set()


async def _queued_timeout_guard(job_id: str, deadline_s: float) -> None:
    try:
        await asyncio.sleep(deadline_s)
        jinfo = await state.store.get_job_info(job_id)
        if jinfo is None or jinfo.status != JobStatus.queued:
            return  # already dispatched / terminal -- nothing to do
        jinfo.status = JobStatus.failed
        jinfo.completed_at = datetime.utcnow()
        if jinfo.progress is not None:
            jinfo.progress.phase = "timed_out"
        jinfo.error = (
            f"queued for >{deadline_s:.0f}s without assignment "
            f"(no worker/lane available)"
        )
        await state.store.save_job_info(jinfo)
        try:
            await state.store.publish_log(job_id, "  !! " + jinfo.error)
            await state.store.publish_log(job_id, DONE_SENTINEL)
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def _spawn_queued_timeout_guard(job_id: str) -> None:
    try:
        t = asyncio.create_task(_queued_timeout_guard(job_id, _QUEUE_TIMEOUT_S))
        _QUEUE_GUARD_TASKS.add(t)
        t.add_done_callback(_QUEUE_GUARD_TASKS.discard)
    except RuntimeError:
        pass  # no running loop (shouldn't happen in request context)


@router.post("/jobs", response_model=JobInfo)
async def create_job(req: JobRequest, request: Request) -> JobInfo:
    if not req.url:
        raise HTTPException(400, "url is required")
    # SSRF guard: refuse loopback / RFC1918 / link-local (incl. cloud
    # metadata) / multicast hosts up front, before we hand the URL to
    # a worker Chrome. Bypass via env PAPRIKA_ALLOW_PRIVATE_URLS=1.
    # rerun mode gets the same check on req.url even though the
    # script may navigate elsewhere -- the initial nav is still us
    # dispatching, and an attacker who could pass an inline-code
    # script could just put http://10.0.0.5/ in page.goto() anyway,
    # so the URL check is just operator courtesy. The deeper defense
    # is the worker-side iptables egress firewall.
    from server.hub.url_safety import assert_public_url
    assert_public_url(req.url)
    assert state.store is not None and state.registry is not None

    # v2 Phase 5: HostKnowledge consultation.
    # If we have learned knowledge for this URL's host, apply hints
    # before the job is dispatched. Today this just tweaks JobOptions
    # (popup_policy from navigation_hints); future phases will inject
    # barrier strategies and content-extraction tool selection.
    # The consultation log goes into the job log so operators can see
    # what knowledge was applied.
    _hk_consultation = _consult_host_knowledge(req.url, req.options)

    job_id = uuid.uuid4().hex[:12]
    info = JobInfo(
        job_id=job_id,
        status=JobStatus.queued,
        url=req.url,
        options=req.options,
        created_at=datetime.utcnow(),
        progress=JobProgress(phase="queued"),
    )
    await state.store.save_job_info(info)

    # state-model v1.1: queued-timeout guard. Dispatch is normally
    # immediate (codegen/rerun create_task; fetch dispatches inline), so
    # this almost always no-ops -- but if a job is still `queued` after
    # the window (dispatch task died silently, or no worker/lane ever
    # picked it up), fail it as closed·timed_out instead of leaving it
    # stuck queued. Fires once; harmless once the status moved on.
    _spawn_queued_timeout_guard(job_id)

    # Persist the consultation summary to the job log for operator
    # visibility. ``append_log_line`` rpushes to the Redis list (and the
    # subscribe stream relays via the pubsub channel). Best-effort;
    # never blocks job dispatch.
    if _hk_consultation:
        try:
            for ln in _hk_consultation:
                await state.store.append_log_line(job_id, ln)
                try:
                    await state.store.publish_log(job_id, ln)
                except Exception:
                    pass
        except Exception:
            pass

    # v2 Phase 7c: pre-flight plugin auto-invocation.
    # If HostKnowledge declared a suggested_tool for a present barrier
    # (e.g. paprika-flare for cloudflare_challenge), run it now and
    # merge cookies into HostRecord BEFORE the worker dispatch reads
    # rec.cookies below. Best-effort: failures are logged, never raise.
    try:
        _preflight_lines = await _preflight_cf_plugin(req.url, job_id)
    except Exception as e:
        _preflight_lines = [
            f"==> pre-flight plugin crashed unexpectedly "
            f"({type(e).__name__}: {str(e)[:200]}); continuing without"
        ]
    if _preflight_lines:
        try:
            for ln in _preflight_lines:
                await state.store.append_log_line(job_id, ln)
                try:
                    await state.store.publish_log(job_id, ln)
                except Exception:
                    pass
        except Exception:
            pass

    (get_storage_dir() / job_id).mkdir(parents=True, exist_ok=True)
    (get_storage_dir() / job_id / "assets").mkdir(parents=True, exist_ok=True)

    # NOTE: the v1 "vision-agent" mode (CogAgent-driven pixel-space
    # action loop) was removed in the v2 cleanup. Pydantic now rejects
    # ``mode="vision-agent"`` at the protocol layer (see JobOptions),
    # so we never reach this point with that value.

    # ---- codegen-loop mode short-circuits the worker pipeline ----
    # The hub runs the LLM-generate -> sandbox-execute -> retry loop
    # itself; the generated script then opens its OWN /sessions/*
    # against this hub from inside the runner container, which routes
    # to a real worker. We don't dispatch a worker job here.
    if (req.options.mode or "fetch") == "codegen-loop":
        if not (req.options.goal or "").strip():
            raise HTTPException(400, "codegen-loop mode requires 'goal'")
        task = asyncio.create_task(
            _run_codegen_loop_job(request, info),
        )
        state.local_tasks[job_id] = task
        # novnc_url stays None at this point (lane not bound yet), so
        # the proxy rewrite is a no-op. Kept here for symmetry with the
        # other return paths so a future change that pre-binds lanes
        # doesn't accidentally surface a worker-direct URL.
        return _proxy_info(info, request)

    # ---- rerun mode: same pipeline as codegen-loop minus the LLM ----
    # Source: req.options.rerun_from (job/attempt ref on disk) or
    # req.options.code (inline). Resolved up-front so a 400 fires
    # synchronously if the source is missing/invalid.
    if (req.options.mode or "fetch") == "rerun":
        try:
            script_code, source_label, source_jid = resolve_rerun_source(
                get_storage_dir(),
                req.options.rerun_from,
                req.options.code,
            )
        except ValueError as e:
            raise HTTPException(400, f"rerun: {e}") from e
        # If we're rerunning from an existing job, inherit its walker
        # state (and any sibling per-parent state) so pap.walk() picks
        # up where the source left off rather than re-crawling from 0.
        # This is the kernel of the "▶ resume" UX: pause = cancel
        # (state stays on disk), resume = mode=rerun pointing at the
        # paused job's last attempt (state gets copied into the new
        # job's state dir before the sandbox starts).
        copied = 0
        if source_jid:
            try:
                copied = _copy_session_state_dir(source_jid, job_id)
            except Exception:
                copied = 0
        task = asyncio.create_task(
            _run_rerun_loop_job(info, script_code, source_label, inherited_state_files=copied),
        )
        state.local_tasks[job_id] = task
        return _proxy_info(info, request)

    # ---- resolve attach_to_job (Phase 4) ----
    # attach_to_job is best-effort: if the referenced job is gone (deleted,
    # expired, or just stale because the caller cached the id from a
    # previous session), or never used a lane pool, we *don't* fail the
    # request -- we fall back to plain "pick a free active worker" and
    # log the reason. Callers can pass attach_to_job optimistically
    # without having to first check whether the id still exists.
    lane_hint: int | None = None
    pinned_worker = None  # if attach_to_job: route to the same worker
    attach_fallback_reason: str | None = None
    if req.options.attach_to_job:
        prev = await state.store.get_job_info(req.options.attach_to_job)
        if prev is None:
            attach_fallback_reason = f"attach_to_job '{req.options.attach_to_job}' not found"
        elif prev.lane_idx is None:
            attach_fallback_reason = (
                f"attach_to_job '{req.options.attach_to_job}' had no lane_idx "
                f"(prior run did not use a lane pool)"
            )
        else:
            lane_hint = prev.lane_idx
            # Try to pin to the same worker so that lane exists on it. If
            # the worker has disconnected since then, fall back too.
            if prev.worker_id and prev.worker_id in state.registry.connections:
                pinned_worker = state.registry.connections[prev.worker_id]
            else:
                attach_fallback_reason = (
                    f"attach_to_job worker '{prev.worker_id}' no longer "
                    f"connected; routing as a fresh job"
                )
                lane_hint = None  # forget the hint, let the scheduler pick
        if attach_fallback_reason is not None:
            log.info(f"[hub] job {job_id}: {attach_fallback_reason}")
            # Record on the job so the operator can see why it didn't attach.
            info.progress.last_log = attach_fallback_reason
            await state.store.save_job_info(info)

    # Hub-managed min-size filter. Fill it in from the operator's
    # Settings default ONLY when the client omitted the field entirely
    # (e.g. a bare API call). Any value the client set explicitly --
    # including 0 ("capture everything") -- wins, so the Submit form is
    # authoritative (WYSIWYG) and can't be silently overridden.
    if state.settings is not None and "min_asset_size_bytes" not in req.options.model_fields_set:
        # Client didn't send the field at all -> use the operator's
        # Settings default. An explicit client value (including 0 =
        # "no filter") is left untouched so WYSIWYG holds for the form.
        try:
            req.options.min_asset_size_bytes = int(
                state.settings.get("min_asset_size_bytes", 0) or 0
            )
        except Exception:
            pass

    # Hub-managed Fetch defaults. For each fetch_* knob in Settings,
    # overlay onto JobOptions UNLESS the client explicitly set the
    # corresponding field (Pydantic's model_fields_set). That way an
    # operator can set "default scroll = True" once in Settings and
    # have every Fetch submit pick it up, but a one-off API caller
    # passing scroll=False explicitly still gets their value through.
    if state.settings is not None:
        try:
            explicit = req.options.model_fields_set
        except Exception:
            explicit = set()
        # Map Settings key -> JobOptions field name.
        _FETCH_DEFAULT_MAP = {
            "fetch_wait_seconds": "wait_seconds",
            "fetch_settle_seconds": "settle_seconds",
            "fetch_idle_seconds": "idle_seconds",
            "fetch_max_wait_seconds": "max_wait_seconds",
            "fetch_scroll": "scroll",
            "fetch_scroll_step": "scroll_step",
            "fetch_scroll_max": "scroll_max",
            "fetch_scroll_early_after": "scroll_early_after",
            "fetch_post_click_seconds": "post_click_seconds",
        }
        for setting_key, opt_field in _FETCH_DEFAULT_MAP.items():
            if opt_field in explicit:
                continue
            try:
                v = state.settings.get(setting_key)
                if v is None:
                    continue
                setattr(req.options, opt_field, v)
            except Exception:
                pass

    # Per-host cookie auto-injection + popup_policy lookup.
    #
    # Mirrors the session path: if the host of ``url`` has a record
    # in the registry, attach its cookies to the assign-job so the
    # worker CDP-installs them before navigation. The same host is
    # also echoed back as ``save_cookies_host`` so the worker dumps
    # the post-fetch jar back to /hosts/{host}, capturing any
    # session cookies the page set (and refreshing the existing
    # record's ``updated_at``).
    #
    # popup_policy is looked up for ANY worker-dispatched mode (fetch
    # OR vision-agent) because both run the tab-killer at the lane
    # boundary and need to know whether to follow popups (some video
    # sites open videos in new tabs etc.). Codegen-loop / rerun go
    # through /sessions instead and get it from that path.
    #
    # cookies + save_cookies_host stay fetch-only -- vision-agent
    # doesn't dump cookies on exit (no clean "fetch done" boundary
    # to hang the dump callback on; the loop just stops).
    auto_cookies: list[dict] | None = None
    auto_host: str | None = None
    auto_popup_policy: str = "kill"
    if state.hosts is not None and req.options.mode == "fetch":
        try:
            from urllib.parse import urlparse as _urlparse

            host_raw = _urlparse(req.url).hostname or ""
            auto_host = _normalise_host(host_raw)
            if auto_host:
                # Auto re-login gate: if this host has a login recipe
                # and its session is stale (last login older than the
                # configured TTL, or never), refresh it BEFORE reading
                # the cookies below. Keeps a login-gated fetch
                # (market.laxd.com etc.) working past the
                # session-cookie expiry without manual re-login. Only
                # for fetch mode -- the cookies are fetch-only too.
                # Best-effort: a failed re-login just proceeds with the
                # current (possibly stale) cookies.
                if req.options.mode == "fetch" and state.hosts.is_login_stale(auto_host):
                    try:
                        relog = await _ensure_host_login(auto_host)
                        log.info(
                            f"[hub] job {job_id}: pre-fetch auto-login "
                            f"{auto_host} -> {relog.get('relogin')}",
                        )
                    except Exception as e:
                        log.info(
                            f"[hub] job {job_id}: pre-fetch auto-login "
                            f"{auto_host} crashed: {type(e).__name__}: {e}",
                        )
                rec = state.hosts.get(auto_host)
                if rec:
                    auto_popup_policy = rec.popup_policy or "kill"
                    if rec.cookies and req.options.mode == "fetch":
                        auto_cookies = cookies_for_cdp(rec.cookies)
                    # Pick the best-matching pre-baked recipe (Phase 1)
                    # and stamp it onto JobOptions so the worker can
                    # run it right after navigation. Only for Fetch
                    # mode -- vision-agent / codegen-loop have their
                    # own LLM-driven flow and don't need the recipe.
                    if (
                        req.options.mode == "fetch"
                        and not req.options.fetch_recipe
                        and getattr(req.options, "fetch_strategy", "recipe") != "normal"
                    ):
                        try:
                            picked = rec.pick_recipe(req.url)
                            if picked is not None:
                                req.options.fetch_recipe = picked.to_json()
                                log.info(
                                    f"[hub] job {job_id}: matched "
                                    f"fetch_recipe pattern="
                                    f"{picked.pattern!r} for "
                                    f"host={auto_host!r}"
                                )
                        except Exception as e:
                            log.info(
                                f"[hub] job {job_id}: fetch_recipe "
                                f"lookup crashed "
                                f"({type(e).__name__}: {e}); "
                                f"continuing without recipe"
                            )
        except Exception:
            auto_cookies = None
            auto_host = None
            auto_popup_policy = "kill"

    # ---- GPU concurrency gate (codegen-loop only) ----
    # ぱっぷす環境では Qwen-VL を自前 GPU (RTX 6000 Pro Max-Q) で走らせるが
    # 1 枚を 24 ライン で奪い合うので、page.agent / observe / ask を呼び得る
    # codegen-loop ジョブが多数並ぶと GPU 飽和で全体が詰まる。
    # PAPRIKA_CODEGEN_LOOP_CONCURRENCY で同時実行数を絞り、上限に到達したら
    # grace window で他ジョブの完了を待つ。Pinned (attach_to_job) は対象外。
    _is_codegen_loop = (req.options.mode or "fetch") == "codegen-loop"
    if _is_codegen_loop and pinned_worker is None:
        from server.hub._gpu_gate import (
            codegen_loop_at_capacity,
            codegen_loop_in_flight,
            get_codegen_loop_limit,
        )
        if codegen_loop_at_capacity():
            _gpu_deadline = time.monotonic() + max(JOB_DISPATCH_GRACE_S, 5.0)
            _gpu_waited = False
            while codegen_loop_at_capacity() and time.monotonic() < _gpu_deadline:
                await asyncio.sleep(_JOB_DISPATCH_POLL_S)
                _gpu_waited = True
            if codegen_loop_at_capacity():
                log.info(
                    f"[hub] job {job_id}: codegen-loop GPU gate full "
                    f"({codegen_loop_in_flight()}/{get_codegen_loop_limit()}); "
                    f"refusing dispatch",
                )
                # Mark failed with a clear reason so the admin UI / SDK
                # can see "GPU gate" not "fleet at capacity". The job
                # never reached a worker -- no recovery work needed.
                info.status = JobStatus.failed
                info.error = (
                    f"GPU gate full ({codegen_loop_in_flight()}/"
                    f"{get_codegen_loop_limit()} codegen-loop already "
                    f"running); retry with backoff"
                )
                info.progress.phase = "failed"
                info.completed_at = datetime.utcnow()
                try:
                    await state.store.save_job_info(info)
                except Exception:
                    pass
                raise HTTPException(
                    503,
                    info.error,
                )
            if _gpu_waited:
                log.info(
                    f"[hub] job {job_id}: codegen-loop GPU gate freed during "
                    f"grace ({codegen_loop_in_flight()}/{get_codegen_loop_limit()})",
                )

    # ---- dispatch in priority order ----
    # 1) WebSocket-connected worker (pinned or any free).
    #
    # When not pinned and no worker currently has free capacity, poll
    # for up to JOB_DISPATCH_GRACE_S before giving up. This smooths
    # over the hub-restart reconnect window: right after a `docker
    # compose restart hub`, the WS registry is momentarily empty and a
    # job submitted in that gap used to instantly 503 ("fleet at
    # capacity") even though workers reconnect within a couple seconds
    # (job d435107ed59b hit exactly this). Pinned jobs (attach_to_job)
    # skip the grace loop -- they need one specific worker and the
    # assign below queues onto it regardless of its in_flight count.
    worker = pinned_worker
    if worker is None:
        worker = state.registry.pick_worker()
        if worker is None and JOB_DISPATCH_GRACE_S > 0:
            _grace_deadline = time.monotonic() + JOB_DISPATCH_GRACE_S
            _waited = False
            while worker is None and time.monotonic() < _grace_deadline:
                await asyncio.sleep(_JOB_DISPATCH_POLL_S)
                worker = state.registry.pick_worker()
                _waited = True
            if _waited and worker is not None:
                log.info(
                    f"[hub] job {job_id}: worker became available "
                    f"during dispatch grace window "
                    f"({worker.worker_id})",
                )
    if worker is not None:
        # Prefer the URL the worker actually dialled when it connected to
        # us (recorded on the WS handshake). Falls back to the operator
        # config / incoming HTTP request only when the worker connected
        # without a Host header.
        base = worker.public_base_url or _hub_base_url(request)

        # Allocate a session_id for this fetch so the admin UI can
        # inspect the running browser via /sessions/{sid}/* while the
        # fetch is in flight. The worker registers a read-only
        # SessionState under this id when the browser is attached and
        # tears it down right before browser.stop() (see fetch()'s
        # on_browser_ready / on_browser_closing callbacks). The
        # SessionInfo is removed here in the hub on WorkerJobComplete
        # / WorkerJobFailed; the id stays on JobInfo as a historical
        # reference but stops resolving once removed.
        fetch_sid: str | None = None
        if (req.options.mode or "fetch") == "fetch" and state.sessions is not None:
            fetch_sid = new_session_id()
            try:
                # keep_session: operator stays attached via noVNC and
                # the API. The noVNC proxy taps client-side RFB events
                # (mouse / key / clipboard) and touches the session's
                # last_active_at so the 60-second idle window is
                # genuinely "no operator activity for 60 s" rather
                # than "no API call". 60 s default chosen so a
                # forgotten / abandoned session doesn't hog a lane;
                # operator can override per-detach via
                # ``await sess.detach(idle_ttl_s=...)`` if they want
                # a longer leash. 24h absolute is the hard backstop.
                #
                # State machine after crawl ends:
                #   keepalive --(RFB activity)--> running
                #   running --(QUIET_S no RFB)--> keepalive
                #   keepalive --(idle_ttl_s no RFB)--> IDLE (= reaped)
                keep_session_req = bool(getattr(req.options, "keep_session", False))
                _idle_ttl_s = 60 if keep_session_req else 600
                _abs_ttl_s = 24 * 3600 if keep_session_req else 3600
                sinfo = SessionInfo(
                    session_id=fetch_sid,
                    worker_id=worker.worker_id,
                    initial_url=req.url,
                    idle_ttl_s=_idle_ttl_s,
                    absolute_ttl_s=_abs_ttl_s,
                    job_id=job_id,
                )
                sinfo.state = "fetch_running"
                state.sessions.add(sinfo)
                info.session_id = fetch_sid
            except Exception as e:
                log.info(
                    f"[hub] job {job_id}: could not register fetch "
                    f"session: {type(e).__name__}: {e}",
                )
                fetch_sid = None

        # Resolve ``options.use_profile`` (or fall back to the
        # operator-set default profile) to a profile_url the worker
        # can GET. We pass the URL (not just the name) so the worker
        # doesn't need to know hub-side path conventions and so the
        # tarball can in principle live on a different host later.
        # Reject explicit names that don't exist with a synchronous
        # 400; a missing default is silent (the job just runs with
        # the lane's stock profile, same as before defaults existed).
        _profile_url: str | None = None
        _profile_etag: str | None = None
        _profile_name = (req.options.use_profile or "").strip() or None
        _explicit = _profile_name is not None
        if _profile_name is None and state.profiles is not None:
            _profile_name = state.profiles.get_default()
        if _profile_name:
            if state.profiles is None or not state.profiles.exists(_profile_name):
                if _explicit:
                    raise HTTPException(
                        400,
                        f"use_profile: profile '{_profile_name}' not "
                        "found. Upload it first via POST /profiles/{name} "
                        "(paprika-client upload-profile).",
                    )
                # default went stale between get_default() and the
                # exists() recheck -- treat as "no default" rather
                # than failing the dispatch.
                _profile_name = None
        if _profile_name:
            _profile_url = f"{base}/profiles/{_profile_name}"
            # Etag lets the worker skip the download when its cache
            # already has this exact version (typical case after the
            # initial sync broadcast).
            _profile_etag = state.profiles.etag(_profile_name)

        # Asset URL blacklist (V): pull operator-managed list from Settings
        # and stamp onto every assignment so an admin UI edit takes effect
        # on the next dispatched job. Stored as newline-separated string;
        # split + trim + drop blanks here so the worker just iterates.
        _bl_raw = ""
        if state.settings is not None:
            try:
                _bl_raw = (state.settings.get("asset_url_blacklist", "") or "").strip()
            except Exception:
                _bl_raw = ""
        _asset_url_blacklist = [
            line.strip()
            for line in _bl_raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assign = HubAssignJob(
            job_id=job_id,
            url=req.url,
            options=req.options,
            asset_upload_base=_asset_upload_url(base, job_id),
            lane_hint=lane_hint,
            cookies=auto_cookies,
            save_cookies_host=auto_host if req.options.mode == "fetch" else None,
            session_id=fetch_sid,
            popup_policy=auto_popup_policy,
            profile_url=_profile_url,
            profile_name=_profile_name,
            profile_etag=_profile_etag,
            asset_url_blacklist=_asset_url_blacklist,
        )
        # Bump the registry's last_used_at so the Hosts tab reflects
        # that the cookies actually rode along on a real job.
        if auto_cookies and auto_host and state.hosts is not None:
            try:
                state.hosts.touch_used(auto_host)
            except Exception:
                pass
        ok = await state.registry.assign(worker, assign)
        if ok:
            # Record which worker + (if known) the noVNC URL so clients can
            # watch the job live.
            info.worker_id = worker.worker_id
            novnc = worker.capabilities.novnc_url
            if novnc:
                sep = "&" if "?" in novnc else "?"
                info.novnc_url = (
                    f"{novnc}{sep}autoconnect=1&resize=scale&reconnect=1"
                    if "autoconnect" not in novnc
                    else novnc
                )
            await state.store.save_job_info(info)
            # GPU gate: register the codegen-loop job so subsequent
            # submissions see the right in-flight count. Unregister
            # happens in workers.py when WorkerJobComplete / Failed lands.
            if _is_codegen_loop:
                try:
                    from server.hub._gpu_gate import register_codegen_loop
                    register_codegen_loop(job_id)
                except Exception:
                    pass
            log.info(
                f"[hub] job {job_id} → worker {worker.worker_id} "
                f"(in_flight={worker.in_flight}/"
                f"{worker.capabilities.max_concurrent})  "
                f"novnc={info.novnc_url or '(none)'}",
            )
            return _proxy_info(info, request)
        # If send failed, fall through to the 503 path below. Roll back
        # the SessionInfo we eagerly registered so it doesn't stick
        # around pointing at a worker that never accepted the job.
        if fetch_sid:
            try:
                state.sessions.remove(fetch_sid)
            except Exception:
                pass
            info.session_id = None
        log.info(f"!! failed to send job to worker {worker.worker_id}")

    # 2) No worker available -- reject with 503. The hub used to run an
    # in-process nodriver fallback here, but the hub container has no
    # Chrome installed, so that path failed with FileNotFoundError under
    # load (load test 2026-05: 48/100 jobs failed once 52 lanes were
    # saturated). Clients should retry with backoff; the operator UI
    # surfaces fleet capacity. Mark the JobInfo as failed so the admin
    # history shows the rejection rather than leaving a phantom queued
    # entry.
    info.status = JobStatus.failed
    info.error = "no worker available (fleet at capacity)"
    info.progress.phase = "failed"
    info.completed_at = datetime.utcnow()
    try:
        await state.store.save_job_info(info)
    except Exception:
        pass
    raise HTTPException(
        503,
        "no worker available (fleet at capacity); retry with backoff",
    )
