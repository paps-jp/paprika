"""Shared core for the /jobs route package: router, asset/job helpers,
mime constants. Imported (``from ._base import *``) by every jobs.* route
sub-module and re-exported from jobs/__init__.py for external callers."""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from server.hub._state import config, get_storage_dir, state
from server.hub import objstore
from server.hub._helpers import _safe_job_file
from server.hub.routes.novnc import _proxy_session_dict
from server.hub.routes.sessions import (
    _novnc_autoconnect,
    _route_to_page,
    _send_session_action,
)
from server.protocol import JobInfo
import os
import shutil
from datetime import datetime
from server.hub.routes.novnc import _proxy_info
from server.protocol import AssetInfo, JobResult, JobStatus
from server.runner import DONE_SENTINEL
import uuid
from fastapi import WebSocket, WebSocketDisconnect
from server.protocol import Event
import time
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.iterative_codegen import resolve_rerun_source
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import (
    HubAssignJob,
    JobProgress,
    JobRequest,
)
from server.hub.app import (  # noqa: E402
    _JOB_DISPATCH_POLL_S,
    JOB_DISPATCH_GRACE_S,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["Jobs"])

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
    *,
    need_info: bool = True,
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
    /jobs/{id}/links, /jobs/{id}/network, and the
    session-end POST endpoints. Six call sites at the time of
    extraction; deferring this DRY made the asset-gallery debugging
    saga ~one commit longer than it needed to be."""
    assert state.store is not None
    # need_info=False (e.g. complete_asset_upload, thousands/min): the caller
    # only needs the soft-404 gate, not the JobInfo value. Use a cheap SELECT-1
    # existence check -- get_job_info's row->JobInfo pydantic build (json.loads
    # options/progress + model construction) was the top remaining MainThread
    # on-CPU cost (py-spy 2026-07-08). Falls back to get_job_info on stores
    # without job_exists (unchanged behaviour).
    if need_info:
        info = await state.store.get_job_info(job_id)
        if info is not None:
            return info
    else:
        _exists = getattr(state.store, "job_exists", None)
        if _exists is not None:
            if await _exists(job_id):
                return None
        elif await state.store.get_job_info(job_id) is not None:
            return None
    check_dir = get_storage_dir() / job_id
    if require_subdir:
        check_dir = check_dir / require_subdir
    if check_dir.is_dir():
        return None
    # S3-backed fallback: when the local dir is missing, accept the job
    # if the object store holds its artifacts (e.g. the local copy was
    # evicted but the S3 mirror still has it). Keeps the gallery
    # resolving without a local copy.
    if objstore.enabled() and await objstore.prefix_exists(job_id, require_subdir):
        return None
    raise HTTPException(404, f"job '{job_id}' not found")


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


# Raster (photo) image extensions -- the face-searchable set the downstream
# image pipeline can Image.open(). Kept separate from vector / icon so
# assets.json classifies them distinctly: SVG logos and favicons are
# decoration, not photos, and were choking the raster pipeline.
_RASTER_IMG_EXTS = {"png", "jpg", "jpeg", "webp", "avif", "gif", "bmp"}
_VECTOR_IMG_EXTS = {"svg"}
_ICON_IMG_EXTS = {"ico"}
# Union -- for "is this an image-ish file at all" checks (e.g. the
# screenshots.json listing). Value unchanged from before the split.
_IMG_EXTS = _RASTER_IMG_EXTS | _VECTOR_IMG_EXTS | _ICON_IMG_EXTS


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


async def _gather_assets(job_id: str) -> list[dict]:
    """The job's top-level assets as ``[{"name", "size"}]``, sourced from
    the object store *unioned* with any local copy -- so the gallery
    populates whether the files live on local disk, in S3, or both. This is
    what makes the gallery survive cache eviction: when the local
    dir is gone the S3 listing still supplies every asset.

    Excludes ``screenshot-*`` (those belong to the Screenshot tab) and the
    nested ``.meta/`` sidecar dir. Sorted case-insensitively by name. When
    S3 is disabled this is exactly the old local-only listing."""
    by_name: dict[str, int] = {}
    assets_dir = get_storage_dir() / job_id / "assets"
    if assets_dir.exists():
        for p in assets_dir.iterdir():
            if not p.is_file() or p.name.lower().startswith("screenshot-"):
                continue
            try:
                by_name[p.name] = p.stat().st_size
            except OSError:
                continue
    if objstore.enabled():
        for o in await objstore.list_dir(job_id, "assets"):
            n = o.get("name") or ""
            if not n or n.lower().startswith("screenshot-"):
                continue
            by_name.setdefault(n, int(o.get("size") or 0))  # local size wins
    return [
        {"name": n, "size": s}
        for n, s in sorted(by_name.items(), key=lambda kv: kv[0].lower())
    ]


_JOBS_SUMMARY_CACHE: dict = {"ts": 0.0, "value": None}


_JOBS_SUMMARY_TTL_S = 2.0


# L2 cache: the ``/jobs/summary`` result is also memoised in Redis so the
# ~350-500 ms full-table aggregation runs at most once per TTL FLEET-WIDE, not
# once per hub. The in-process L1 above (2 s) is defeated by nginx round-robin
# (each hub is only hit every ~n_hubs*poll seconds, so its cache is always
# cold); the shared L2 is what actually cuts the DB load. ``as_of`` in the
# cached value tells the operator how stale the counts are (<= TTL seconds).
_JOBS_SUMMARY_REDIS_KEY = "paprika:jobs_summary:v1"
_JOBS_SUMMARY_REDIS_TTL_S = 4


_JOBS_SUMMARY_RUNNING_PREVIEW = 5


_JOBS_SUMMARY_RECENT_WINDOWS_H = (1, 24)


async def _summary_python_count(
    created_after_ts: float | None,
) -> tuple[dict[str, int], dict[str, int], int]:
    """Hydrate-and-count fallback for stores without
    ``count_by_status_and_mode``. Linear in the number of jobs --
    fine at <10k, gets painful beyond that."""
    from datetime import datetime, timezone

    assert state.store is not None
    ids = await state.store.list_job_ids()
    by_status: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    cutoff = (
        datetime.fromtimestamp(created_after_ts, tz=timezone.utc)
        if created_after_ts is not None else None
    )
    for jid in ids:
        info = await state.store.get_job_info(jid)
        if info is None:
            continue
        if cutoff is not None:
            ca = getattr(info, "created_at", None)
            if ca is None or ca < cutoff:
                continue
        s = getattr(info.status, "value", None) or str(info.status)
        by_status[s] = by_status.get(s, 0) + 1
        opts = info.options
        if isinstance(opts, dict):
            mode = opts.get("mode") or "fetch"
        else:
            mode = getattr(opts, "mode", "fetch") or "fetch"
        by_mode[mode] = by_mode.get(mode, 0) + 1
    return by_status, by_mode, sum(by_status.values())


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


def _ffmpeg_q_from_quality_pct(pct: int) -> int:
    from server.hub._helpers import _ffmpeg_q_from_quality_pct as _impl

    return _impl(pct)


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


# Stale-while-revalidate cache for the peer-capacity lookup below.
# ``None`` means "never computed" (cold start); otherwise (expires_at, peer).
_PEER_CAP_TTL_S = float(os.environ.get("PAPRIKA_PEER_CAPACITY_TTL_S") or 2.0)
_peer_cap_cache: tuple[float, str | None] | None = None
_peer_cap_lock = asyncio.Lock()
_peer_cap_refresh: "asyncio.Task | None" = None


def _spawn_peer_cap_refresh() -> None:
    """Kick off a background refresh unless one is already running."""
    global _peer_cap_refresh
    t = _peer_cap_refresh
    if t is not None and not t.done():
        return
    try:
        _peer_cap_refresh = asyncio.create_task(_refresh_peer_cap())
    except RuntimeError:
        pass  # no running loop


async def _refresh_peer_cap() -> None:
    global _peer_cap_cache
    try:
        out = await _compute_peer_hub_with_spare_capacity()
    except Exception:
        # Keep serving the last good answer, but push the expiry out so a
        # persistently failing aggregation doesn't respawn on every submit.
        if _peer_cap_cache is not None:
            _peer_cap_cache = (
                time.monotonic() + _PEER_CAP_TTL_S, _peer_cap_cache[1]
            )
        return
    _peer_cap_cache = (time.monotonic() + _PEER_CAP_TTL_S, out)


async def _peer_hub_with_spare_capacity() -> str | None:
    """P1 cross-hub dispatch: return the peer hub_id (not us) with the MOST
    spare active-worker lane capacity per the shared cross-hub worker view,
    or None if no peer has a free lane. Job dispatch is otherwise per-hub, so
    a hub whose LOCAL workers are full 503s even while peers sit idle; this
    lets it forward the job to an idle peer instead.

    Cached, because the underlying ``stats_async()`` is a cross-hub Redis
    aggregation costing ~1.5s (p50, measured) and a locally-full hub calls
    this TWICE per submit: once in ``_fleet_has_spare_capacity`` (the
    admission gate) and again in ``_forward_to_peer_hub`` (the dispatch).
    That was ~3s of a 3.58s p50 -- 79% of all measured POST /jobs time on
    2026-08-14 -- to compute the same answer twice.

    STALE-WHILE-REVALIDATE, not a plain TTL. The first attempt used a 1s TTL
    with a single-flight lock and only got ``capacity`` from 1504ms to
    1309ms, because a TTL SHORTER than the 1.5s computation leaves no window
    where the entry is actually fresh: every caller either ran the
    aggregation or blocked on the lock waiting for someone else's. Converting
    "N parallel 1.5s calls" into "N callers waiting 1.5s" does not shorten
    any single request. (The second call in the same request did halve --
    1874ms to 900ms -- which is exactly the signature of that bug.)

    So: serve whatever we have IMMEDIATELY, even if stale, and refresh
    behind the response. Only a genuinely cold cache blocks a caller.

    Staleness is fine for what this returns: a coarse routing hint ("which
    peer has a lane"), at most ~TTL + one aggregation old. If the chosen peer
    filled up meanwhile it simply 503s and the caller falls back to local
    dispatch -- the same path a stale-by-milliseconds answer would take.

    Negative results are cached too: when the whole fleet is full, that is
    exactly when we least want every submit re-running the aggregation.
    """
    global _peer_cap_cache
    cached = _peer_cap_cache
    if cached is not None:
        expires, value = cached
        if time.monotonic() >= expires:
            _spawn_peer_cap_refresh()   # refresh behind the response
        return value
    # Cold start only: nothing to serve, so this caller waits. The lock keeps
    # the concurrent submits behind it from each starting their own run.
    async with _peer_cap_lock:
        if _peer_cap_cache is not None:
            return _peer_cap_cache[1]
        out = await _compute_peer_hub_with_spare_capacity()
        _peer_cap_cache = (time.monotonic() + _PEER_CAP_TTL_S, out)
        return out


async def _compute_peer_hub_with_spare_capacity() -> str | None:
    """Uncached form -- the cross-hub aggregation itself."""
    if state.registry is None or state.hubs is None:
        return None
    me = state.hubs.hub_id or ""
    try:
        snap = await state.registry.stats_async()
    except Exception:
        return None
    spare: dict[str, int] = {}
    for w in snap.get("workers", []):
        if not w.get("alive") or w.get("status") != "active":
            continue
        h = w.get("hub_id") or ""
        if not h or h == me:
            continue
        avail = (w.get("capacity") or 0) - (w.get("in_flight") or 0)
        if avail > 0:
            spare[h] = spare.get(h, 0) + avail
    if not spare:
        return None
    return max(spare, key=spare.get)


__all__ = [
    'router',
    '_extract_links_from_html',
    '_consult_host_knowledge',
    '_PREFLIGHT_ALLOWED_PLUGINS',
    '_cookies_dict_to_records',
    '_preflight_cf_plugin',
    '_require_job_info',
    '_soft_resolve_job',
    '_append_network_jsonl',
    '_append_line',
    '_IMG_EXTS',
    '_RASTER_IMG_EXTS',
    '_VECTOR_IMG_EXTS',
    '_ICON_IMG_EXTS',
    '_VIDEO_EXTS',
    '_AUDIO_EXTS',
    '_human_size',
    '_asset_href',
    '_gather_assets',
    '_JOBS_SUMMARY_CACHE',
    '_JOBS_SUMMARY_TTL_S',
    '_JOBS_SUMMARY_REDIS_KEY',
    '_JOBS_SUMMARY_REDIS_TTL_S',
    '_JOBS_SUMMARY_RUNNING_PREVIEW',
    '_JOBS_SUMMARY_RECENT_WINDOWS_H',
    '_summary_python_count',
    '_backfill_asset_metadata',
    '_job_dir_size_bytes',
    '_ffmpeg_q_from_quality_pct',
    '_run_codegen_loop_job',
    '_run_rerun_loop_job',
    '_copy_session_state_dir',
    '_ensure_host_login',
    '_hub_base_url',
    '_asset_upload_url',
    '_QUEUE_TIMEOUT_S',
    '_QUEUE_GUARD_TASKS',
    '_queued_timeout_guard',
    '_spawn_queued_timeout_guard',
    '_peer_hub_with_spare_capacity',
]
