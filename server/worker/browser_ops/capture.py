"""Page capture + passive session asset capture. (browser_ops package; see _base.py for shared helpers)."""

from __future__ import annotations
import asyncio
import base64
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from nodriver import cdp

from ._base import *  # noqa: F401,F403
from ._base import LogFn, Snapshot
from .dom import outline
from .media import _AUTOPLAY_ENABLED, install_iframe_deep_trace, install_url_capture_hook, read_url_capture, trigger_autoplay

def safe_label(label: str) -> str:
    label = (label or "").strip().lower()
    return re.sub(r"[^a-z0-9._-]+", "-", label).strip("-")[:60]


async def capture(
    tab,
    label: str,
    step: int,
    assets_dir: Path,
    log: LogFn,
) -> Snapshot:
    """Persist the current page state (HTML + PNG + AX tree) under
    ``assets_dir/<label>/``. Returns the :class:`Snapshot` record.
    """
    label_safe = safe_label(label) or f"capture-{step}"
    dir_path = assets_dir / label_safe
    dir_path.mkdir(parents=True, exist_ok=True)

    # 1) HTML
    try:
        html = await tab.evaluate("document.documentElement && document.documentElement.outerHTML")
    except Exception as e:
        log(f"  [agent] capture {label_safe}: failed to read HTML ({e})")
        html = ""
    html_name = f"{label_safe}.html"
    (dir_path / html_name).write_text(html or "", encoding="utf-8")

    # 2) Screenshot
    png_name = f"{label_safe}.png"
    png_path = dir_path / png_name
    try:
        result = await tab.send(cdp.page.capture_screenshot(format_="png"))
        png_bytes = base64.b64decode(result)
        png_path.write_bytes(png_bytes)
    except Exception as e:
        log(f"  [agent] capture {label_safe}: screenshot failed ({e})")
        png_path.write_bytes(b"")

    # 3) Page outline (the same indexed text we'd ship to the LLM --
    #    useful for replay/debugging "what did the model see here?").
    ax_name = f"{label_safe}.axtree.txt"
    try:
        ax = await outline(tab)
    except Exception as e:
        ax = f"(error: {e})"
    (dir_path / ax_name).write_text(ax, encoding="utf-8")

    try:
        current_url = await tab.evaluate("document.location.href")
    except Exception:
        current_url = ""

    return Snapshot(
        label=label_safe,
        step=step,
        url=current_url or "",
        html_name=html_name,
        png_name=png_name,
        axtree_name=ax_name,
    )


SESSION_SAVE_MIME_PREFIXES = ("image/", "audio/")


_SESSION_EXT_TO_MIME = {
    "avif": "image/avif",
    "webp": "image/webp",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "jxl": "image/jxl",
    "heic": "image/heic",
    "heif": "image/heif",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "opus": "audio/opus",
}


def _session_effective_mime(server_mime: str, url: str) -> str:
    """Effective MIME for the save filter. Falls back to URL extension
    when the server returned empty / generic Content-Type."""
    m = (server_mime or "").strip().lower()
    if m and m not in ("application/octet-stream", "binary/octet-stream"):
        return m
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path
    except Exception:
        return ""
    if "." not in path:
        return ""
    ext = path.rsplit(".", 1)[-1].lower()
    return _SESSION_EXT_TO_MIME.get(ext, "")


def _session_filename(url: str, mime: str, fallback: str) -> str:
    """Mirror of core.fetcher._filename_from -- mint a usable filename
    from the response URL + mime. Kept inline to avoid pulling fetch's
    whole stack into the worker session path."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    name = Path(parsed.path).name or fallback
    if "." not in name:
        ext = (mime or "").split(";")[0].split("/")[-1] or "bin"
        name = f"{name}.{ext}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:180]


def _session_unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


async def install_session_asset_capture(
    tab,
    assets_dir: Path,
    on_saved=None,
    log: LogFn | None = None,
    seen_urls: set | None = None,
    min_asset_size_bytes: int = 0,
    extra_mime_prefixes: tuple = (),
    network_log: list | None = None,
    on_stream_detected=None,
    enable_iframe_deep_trace: bool = True,
    url_blacklist: tuple = (),
) -> None:
    """Hook CDP network listeners so every image/video/audio response
    the browser loads while this tab is alive lands in ``assets_dir``.

    This is the session-mode counterpart to what core.fetcher does in
    fetch mode: instead of trying to scrape ``<img src>``/``<video src>``
    after the fact (which misses lazy-loaded / scripted assets), we
    passively persist anything the browser already downloaded for us.

    ``on_saved(path, info)`` is called once per persisted file -- the
    worker uses this to immediately upload the file to the parent job's
    /assets endpoint.

    ``seen_urls`` is an external set (owned by the caller) used to
    dedup across page navigations -- a long-running session that
    revisits the same image URL across multiple pages won't end up
    with foo.png + foo_1.png + foo_2.png in the gallery. Caller is
    free to share this set with other components (e.g. SessionState
    can keep one set per session and pass it in here).

    ``extra_mime_prefixes`` widens the default filter
    (``image/`` + ``audio/``) with additional prefixes the caller
    wants to capture. vision-agent jobs pass ``("video/",)`` because
    their popup-follow flow lands on naked MP4 URLs; the
    ``min_asset_size_bytes`` filter is what keeps MSE/DASH fragment
    floods (~100-200KB per chunk) out of the gallery when video
    capture is enabled.

    ``on_stream_detected(url, referer)`` is an optional sync callable
    invoked from on_response whenever the response URL looks like an
    HLS / DASH playlist (``.m3u8`` / ``.mpd``). The session-wide
    caller wires this to the agent's ``maybe_download_video`` closure
    so yt-dlp fires the moment a playlist is observed -- WITHOUT
    waiting for the SDK / LLM to call ``page.download_video()``
    explicitly. The CDP listener captures the playlist URL into the
    network log regardless; the callback is the auto-download trigger.
    Idempotent on the same URL (the downloader's internal set
    short-circuits repeats).

    Idempotent on the same tab: hooking twice would duplicate every
    save, so callers should only invoke once at session_start.
    """
    # URL-based detector for video resources we want the
    # ``on_stream_detected`` callback to fire on:
    #
    #   * HLS / DASH playlists (.m3u8 / .mpd): yt-dlp merges
    #     segments into a single mp4 in the downloader.
    #   * Direct video files (.mp4 / .webm / .mov / .m4v / .mkv):
    #     the downloader fetches via httpx with proper referer
    #     handling.
    #
    # We match on URL shape (not MIME) because some CDNs serve these
    # with generic ``application/octet-stream`` -- relying on
    # Content-Type would miss the trigger on exactly the sites where
    # we need it most. The downstream maybe_download closure is
    # idempotent on the same URL, so repeat firings (e.g. an mp4
    # that's also matched by URL shape) are harmless.
    _STREAM_URL_RE = re.compile(
        r"\.(m3u8|mpd|mp4|webm|mov|m4v|mkv)($|\?)", re.I,
    )
    save_prefixes = tuple(SESSION_SAVE_MIME_PREFIXES) + tuple(extra_mime_prefixes)
    assets_dir = Path(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict = {}
    # URL blacklist (V + Y): operator-managed deny list with substring /
    # glob (*, ?, ^, $) / regex (/.../) syntaxes. Compiled once outside
    # the hot per-response path. See core/url_blacklist.py for syntax.
    from core.url_blacklist import (
        compile_blacklist as _compile_blacklist,
        is_manifest_url as _is_manifest_url,
        pattern_targets_manifests as _pattern_targets_manifests,
    )
    _bl_matcher = _compile_blacklist(url_blacklist or ())

    def _is_blacklisted(url: str) -> str | None:
        """Return the first matching pattern, or None when not blocked."""
        return _bl_matcher.match(url)
    # request_id -> document_url snapshot at the time the request was
    # issued. Populated by on_request (RequestWillBeSent) and consumed
    # by on_response so we know which page initiated each asset request.
    request_documents: dict = {}
    if seen_urls is None:
        seen_urls = set()
    # Per-(host, basename) collisions across different URLs are rare but
    # possible (think /a/img.png and /b/img.png). When the second URL
    # arrives, _session_unique_path mints foo_1.png. We keep that
    # fallback so we don't drop legitimately-different assets, but
    # same-URL repeats are short-circuited up front via seen_urls.

    async def on_request(event):
        # ``document_url`` is the URL of the document that initiated the
        # request, captured at request-issue time -- BEFORE any
        # subsequent navigation can clobber tab.url. That gives us the
        # "which page did this image come from" answer the gallery
        # popup wants to show.
        try:
            doc = getattr(event, "document_url", None) or ""
            if doc:
                request_documents[event.request_id] = doc
        except Exception:
            pass

    # Network log: every media response the browser loads. Each entry
    # is a dict with url/mime/size/saved/document_url/timestamp. The
    # Live panel "Network" tab reads this via session action so the
    # operator can inspect traffic and cherry-pick assets.
    _net_log = network_log if network_log is not None else []
    # Track which URLs we already appended to _net_log to avoid
    # duplicate rows when the same URL is re-encountered.
    _net_logged_urls: set = set()

    async def on_response(event):
        try:
            url = event.response.url or ""
            if url in seen_urls:
                return
            # Blacklist gate (V): drop matching URLs BEFORE mime/save
            # decisions, log once, and short-circuit so yt-dlp doesn't
            # fire either. Mark as seen so we don't re-log if the same
            # URL re-appears across navigations.
            #
            # Manifest passthrough (2026-06-14): a general host/path rule
            # like ``*.saawsedge.com*`` (intended for .ts segment noise)
            # would otherwise silently drop the main video's .m3u8
            # manifest -- on_stream_detected never fires + yt-dlp never
            # downloads (job 63f9bf436c2f post-mortem). Manifest URLs
            # bypass general patterns; manifest-specific patterns (with
            # literal ``.m3u8``/``.mpd``) still win.
            _bl_pat = _is_blacklisted(url)
            if _bl_pat is not None:
                seen_urls.add(url)
                if (_is_manifest_url(url)
                        and not _pattern_targets_manifests(_bl_pat)):
                    if on_stream_detected:
                        try:
                            on_stream_detected(url, "")
                        except Exception:
                            pass
                    return
                if log:
                    log(f"  [session-assets] BLOCK (blacklist={_bl_pat!r}) {url[:120]}")
                return
            server_mime = (event.response.mime_type or "").lower()
            # _session_effective_mime falls back to URL extension when
            # the response has no / generic Content-Type. Mainly for
            # Cloudflare-fronted WordPress AVIF (server returns no
            # Content-Type) -- see job 4b9aff01bc6f post-mortem.
            mime = _session_effective_mime(server_mime, url)
            # NB: compose passes SESSION_ASSETS_DEBUG=0 by default, and
            # the string "0" is truthy in Python -- so a bare
            # os.environ.get() check fired the debug log on every
            # response even when "disabled".  Treat 0/false/no/"" as off.
            if log and os.environ.get("SESSION_ASSETS_DEBUG", "").lower() not in ("", "0", "false", "no"):
                log(
                    f"  [session-assets DEBUG] resp server_mime="
                    f"{server_mime!r} effective={mime!r} url={url[:120]}"
                )
            # Record ALL media responses in the network log (before
            # the save-prefix filter) so the operator sees everything.
            is_media = any(mime.startswith(p) for p in save_prefixes)
            # Also log common media MIME types that the save filter
            # might not cover (e.g. video/* when extra_mime_prefixes
            # doesn't include it, or application/octet-stream for
            # binary downloads).
            is_interesting = is_media or any(
                mime.startswith(p) for p in ("image/", "audio/", "video/", "font/")
            )
            # HLS/DASH playlists have MIME application/vnd.apple.mpegurl
            # or application/dash+xml -- not image/audio/video, so the
            # filter above misses them. Force-log any URL that matches
            # the stream pattern so page.network() exposes .m3u8/.mpd
            # entries and codegen scripts can sniff + pass them to
            # page.download_video(url=...).
            if not is_interesting and _STREAM_URL_RE.search(url):
                is_interesting = True
            if is_interesting and url not in _net_logged_urls:
                _net_logged_urls.add(url)
                # Content-Length from response headers (if available).
                content_length = None
                try:
                    for h in event.response.headers or {}:
                        if h.lower() == "content-length":
                            content_length = int(event.response.headers[h])
                            break
                except Exception:
                    pass
                _net_log.append(
                    {
                        "url": url,
                        "mime": mime,
                        "size": content_length,
                        "saved": False,
                        "document_url": request_documents.get(event.request_id) or "",
                        "timestamp": time.time(),
                    }
                )

            # Auto-trigger yt-dlp the moment an HLS/DASH playlist is
            # seen -- don't wait for an explicit page.download_video()
            # call. The downloader closure is idempotent on the same
            # URL, so re-firing is harmless. Match on URL shape so
            # CDNs that serve playlists as application/octet-stream
            # still trip the hook.
            if on_stream_detected and _STREAM_URL_RE.search(url):
                try:
                    on_stream_detected(
                        url,
                        request_documents.get(event.request_id) or "",
                    )
                except Exception as e:
                    if log:
                        log(
                            f"  [session-assets] on_stream_detected "
                            f"failed: {type(e).__name__}: {e}"
                        )
            if not is_media:
                # Not a saveable media response -- drop any preliminary
                # doc URL we stashed so we don't accumulate garbage.
                request_documents.pop(event.request_id, None)
                return
            seen_urls.add(url)
            metadata[event.request_id] = {
                "url": url,
                "mime": mime,
                "document_url": request_documents.pop(event.request_id, None),
            }
        except Exception:
            pass

    async def on_finished(event):
        info = metadata.pop(event.request_id, None)
        if info is None:
            return
        try:
            body, is_b64 = await tab.send(cdp.network.get_response_body(event.request_id))
        except Exception as e:
            if log:
                log(f"  [session-assets] SKIP {info['url']}: {e}")
            return
        try:
            data = base64.b64decode(body) if is_b64 else body.encode("utf-8")
        except Exception:
            return
        actual_size = len(data)
        # Update network_log entry with actual body size.
        for entry in reversed(_net_log):
            if entry["url"] == info["url"]:
                entry["size"] = actual_size
                break
        # Min-size filter: drop assets smaller than the configured
        # threshold (default 0 = no filter). Matches the fetch-mode
        # behaviour in core.fetcher so the same Settings knob takes
        # effect across all capture modes.
        if min_asset_size_bytes and actual_size < min_asset_size_bytes:
            if log:
                log(
                    f"  [session-assets] SKIP {info['url']}: "
                    f"{actual_size / 1024:.1f}KB < min "
                    f"{min_asset_size_bytes / 1024:.1f}KB"
                )
            return
        name = _session_filename(
            info["url"],
            info["mime"],
            f"resource_{len(list(assets_dir.iterdir()))}",
        )
        path = _session_unique_path(assets_dir, name)
        try:
            path.write_bytes(data)
        except Exception as e:
            if log:
                log(f"  [session-assets] write failed: {e}")
            return
        # Mark as saved in network log.
        for entry in reversed(_net_log):
            if entry["url"] == info["url"]:
                entry["saved"] = True
                break
        if log:
            log(f"  [session-assets] SAVED [{actual_size / 1024:>8.1f} KB] {path.name}")
        if on_saved is not None:
            try:
                # ``on_saved`` may be sync or async; await if it's a coroutine.
                res = on_saved(path, info)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception as e:
                if log:
                    log(f"  [session-assets] on_saved failed: {e}")

    async def on_failed(event):
        # Don't release the URL from seen_urls -- if a load failed once,
        # a future success is rare enough not to merit a re-try slot.
        metadata.pop(event.request_id, None)
        request_documents.pop(event.request_id, None)

    tab.handlers[cdp.network.RequestWillBeSent].append(on_request)
    tab.handlers[cdp.network.ResponseReceived].append(on_response)
    tab.handlers[cdp.network.LoadingFinished].append(on_finished)
    tab.handlers[cdp.network.LoadingFailed].append(on_failed)

    # iframe deep-trace is gated on ``enable_iframe_deep_trace``. The
    # actual setup lives in the module-level ``install_iframe_deep_trace``
    # helper so the plain Fetch path (core/fetcher) can reuse it.
    # Idempotency is keyed on a flag stashed on the tab, so an early
    # session-start install + late page.download_video() trigger
    # collapse into one effective install.
    if enable_iframe_deep_trace:
        await install_iframe_deep_trace(tab, log=log)
    elif log:
        log(
            "  [session-assets] iframe deep-trace DEFERRED "
            "(download_video=False; will install on first "
            "page.download_video() call)"
        )

    # Generous buffers -- one session may run for minutes / many pages.
    # Main-session Network.enable is unconditional: the regular asset
    # capture (images / fonts / mp4 from the top frame) needs this
    # regardless of whether iframe deep-trace is on.
    await tab.send(
        cdp.network.enable(
            max_total_buffer_size=1536 * 1024 * 1024,
            max_resource_buffer_size=512 * 1024 * 1024,
        )
    )

    # Same-origin iframe XHR / fetch hook + poller. CDP's Network
    # domain on the parent target SHOULD surface same-origin iframe
    # requests, but in practice (observed on 7mmtv.sx → play.php iframe
    # hosting hls.js → streamsuperpro.com m3u8) the iframe's hls.js
    # XHRs don't appear in Network.responseReceived events for reasons
    # that look like a Chromium quirk. Inject a fetch/XHR monkey-patch
    # via Page.addScriptToEvaluateOnNewDocument and poll the result
    # bucket so the hidden URLs land in network_log + trigger
    # maybe_download_video the same way Network.responseReceived would.
    await install_url_capture_hook(tab, log=log)

    # Background poller. Reads window.top.__paprika_url_capture every
    # 1.5 s and feeds new URLs through the same code path as on_response.
    # Stops itself when the tab closes (evaluate throws); the outer
    # session teardown also cancels the asyncio task.
    _hook_seen: set = set()
    _hook_poll_n = [0]
    _hook_total_captured = [0]
    # Set once any stream URL is observed -- stops the early auto-play
    # attempts (the player is clearly already loading its manifest).
    _stream_captured = [False]

    async def _url_capture_poller():
        # Stagger the first poll so the page has time to load + the
        # hook to be applied to its initial document.
        await asyncio.sleep(2.0)
        while True:
            try:
                captured = await read_url_capture(tab)
            except Exception as _e:
                if log:
                    log(f"  [url-capture] poller exiting (eval error): {_e}")
                return
            _hook_poll_n[0] += 1
            if captured:
                _hook_total_captured[0] += len(captured)
                if log:
                    log(
                        f"  [url-capture] poll #{_hook_poll_n[0]}: "
                        f"{len(captured)} new URL(s) "
                        f"(total captured so far: {_hook_total_captured[0]})"
                    )
            elif _hook_poll_n[0] in (5, 20, 60):
                # Heartbeat at ~7.5 s / 30 s / 90 s so the operator
                # sees that the poller is alive even when no XHRs
                # have been observed yet.
                if log:
                    log(
                        f"  [url-capture] poll #{_hook_poll_n[0]}: "
                        f"alive, bucket empty"
                    )
            # Auto-play trigger.  For the first several polls (~2-10 s
            # after load), nudge the page to start playback so a
            # click-gated player begins fetching its real HLS/DASH
            # manifest -- which the hook above then captures.  Firing on
            # multiple polls lets trigger_autoplay land its one-shot
            # main-player trusted click once the player has laid out (it
            # self-dedupes via tab._paprika_autoplay_trusted_done, so
            # repeats don't toggle playback off).  Stops once a stream
            # URL is seen.
            if (
                _AUTOPLAY_ENABLED
                and not _stream_captured[0]
                and _hook_poll_n[0] <= 6
            ):
                try:
                    await trigger_autoplay(tab, log=log)
                except Exception as _ape:
                    if log:
                        log(f"  [url-capture] autoplay attempt "
                            f"failed: {_ape}")
            for entry in captured:
                url = entry.get("url") or ""
                if not url or url in _hook_seen:
                    continue
                _hook_seen.add(url)
                # Blacklist gate (Y bugfix): the JS fetch/XHR hook is a
                # separate capture surface from CDP Network — it has its
                # own bucket and its own poller, so the on_response gate
                # above misses these. Apply the same matcher here so a
                # `https://*.saawsedge.com*` rule blocks both the CDP-
                # observed playlist and the iframe-captured one. Pre-
                # blacklist log was leaking via this exact path
                # (job 9dc8d38174e4 / edge-hls.saawsedge.com).
                #
                # Manifest passthrough (2026-06-14): same rationale as
                # the on_response gate above. A general host pattern
                # caught a manifest -> let it fire on_stream_detected so
                # yt-dlp gets the real video source.
                _bl_hit = _is_blacklisted(url)
                if _bl_hit is not None:
                    if (_is_manifest_url(url)
                            and not _pattern_targets_manifests(_bl_hit)):
                        _stream_captured[0] = True
                        if on_stream_detected:
                            try:
                                on_stream_detected(url, "")
                            except Exception as e:
                                if log:
                                    log(
                                        f"  [url-capture] on_stream_detected "
                                        f"failed: {e}"
                                    )
                        continue
                    if log:
                        log(f"  [url-capture] BLOCK (blacklist={_bl_hit!r}) {url[:120]}")
                    continue
                # Mirror the on_response logic for stream URLs: log to
                # network_log + fire maybe_download_video. We skip the
                # image/audio mime save path -- that path needs the
                # response body, which we don't have here.
                if _STREAM_URL_RE.search(url):
                    _stream_captured[0] = True
                    if url not in _net_logged_urls:
                        _net_logged_urls.add(url)
                        _net_log.append({
                            "url": url,
                            "mime": "",
                            "size": None,
                            "saved": False,
                            "document_url": "",
                            "source": "iframe_xhr_hook",
                            "timestamp": time.time(),
                        })
                    if on_stream_detected:
                        try:
                            on_stream_detected(url, "")
                        except Exception as e:
                            if log:
                                log(
                                    f"  [url-capture] on_stream_detected "
                                    f"failed: {e}"
                                )
            try:
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                return

    try:
        _poller_task = asyncio.create_task(_url_capture_poller())
        # Stash on tab so session teardown can cancel it cleanly. The
        # task is otherwise fire-and-forget; an unhandled exception is
        # absorbed by the bare try/except inside the poller body.
        existing = getattr(tab, "_paprika_url_capture_tasks", None)
        if existing is None:
            existing = []
            setattr(tab, "_paprika_url_capture_tasks", existing)
        existing.append(_poller_task)
    except Exception as e:
        if log:
            log(f"  [url-capture] poller spawn failed: {e}")

