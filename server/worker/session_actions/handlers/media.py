"""Session-action handlers (media). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import subprocess
from typing import Optional

from server.worker.session_actions._registry import _session_action, _ActionCtx, _logger
from server.worker import browser_ops
from server.worker._browser_helpers import (
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _enumerate_all_frames,
    _extract_dom_video_urls,
    _extract_dom_video_urls_in_frame,
    _looks_like_player_iframe,
    _sniff_stream_urls_from_log,
    _trigger_video_playback,
    _try_click_play_button,
    _try_click_play_button_in_frame,
)

# ---- media oracle (success-verification for download_video) ----
# A downloaded file is only a "win" if it is actually playable media of
# plausible length -- yt-dlp exiting 0 / a file appearing is NOT enough
# (it can be an HTML error page saved as .mp4, a 0-byte stub, a truncated
# segment, or a 6s ad). For an evidence-preservation mission a false
# "success" is worse than a retry, so the oracle errs strict.
_MIN_VALID_VIDEO_S = 1.0  # below this (with a known duration) = glitch/placeholder


def _probe_media_sync(path: str, min_dur: float) -> dict:
    """ffprobe one file -> media-validity facts. Pure/sync; run via
    ``asyncio.to_thread``. Never raises -- failures become ``valid=False``
    with a ``reason``. If ffprobe is absent the caller treats the result
    as "oracle unavailable" and falls back to the legacy ok=uploaded."""
    info = {
        "valid": False, "has_video": False, "duration_s": 0.0,
        "width": 0, "height": 0, "codec": "", "bytes": 0, "reason": "",
    }
    try:
        info["bytes"] = os.path.getsize(path)
    except OSError:
        info["bytes"] = 0
    if info["bytes"] <= 0:
        info["reason"] = "empty"
        return info
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        info["reason"] = "ffprobe_missing"
        return info
    except subprocess.TimeoutExpired:
        info["reason"] = "probe_timeout"
        return info
    except Exception as e:  # pragma: no cover - defensive
        info["reason"] = f"probe_error:{type(e).__name__}"
        return info
    if proc.returncode != 0:
        info["reason"] = "not_media"
        return info
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        info["reason"] = "unparseable_probe"
        return info
    streams = data.get("streams") or []
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    if vstreams:
        info["has_video"] = True
        v0 = vstreams[0]
        info["width"] = int(v0.get("width") or 0)
        info["height"] = int(v0.get("height") or 0)
        info["codec"] = v0.get("codec_name") or ""
    dur = 0.0
    try:
        dur = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0.0 and vstreams:
        try:
            dur = float(vstreams[0].get("duration") or 0.0)
        except (TypeError, ValueError):
            dur = 0.0
    info["duration_s"] = round(dur, 2)
    # A still-image "video" (mjpeg/png) or audio-only file is not a real
    # video capture; require a real video stream. Only reject on a KNOWN
    # short duration (dur>0) so containers that omit duration metadata
    # aren't false-negatived.
    if not info["has_video"]:
        info["reason"] = "no_video_stream"
    elif 0.0 < dur < min_dur:
        info["reason"] = "too_short"
    else:
        info["valid"] = True
        info["reason"] = "ok"
    return info


@_session_action("download_video", read_only=False)
async def _act_download_video(agent, ctx: "_ActionCtx") -> None:
    tab = ctx.tab
    sid = ctx.msg.session_id
    state = ctx.state
    reply = ctx.reply
    action = ctx.action
    _slog = ctx.slog
    # Late-enable iframe + nested-iframe deep network
    # trace, if the session was opened with
    # download_video=False. Cross-origin video players
    # live inside iframes; without this hook their HLS
    # / DASH manifest URLs never enter state.network_log
    # and the iframe-walk fallback below has nothing to
    # find. Idempotent (the helper short-circuits when
    # the tab is already marked traced).
    try:
        await browser_ops.install_iframe_deep_trace(
            tab,
            log=lambda s: _logger.info(f"[session {sid}] {s}"),
        )
    except Exception as e:
        _logger.info(
            f"[session {sid}] late iframe trace "
            f"enable failed (non-fatal): "
            f"{type(e).__name__}: {e}"
        )
    # Shell to yt-dlp against the requested URL (or the
    # current page URL if omitted), saving outputs to
    # state.assets_dir/videos/. Each newly-saved file is
    # then uploaded to the parent job's /assets via the
    # same path the passive CDP listener uses. This is
    # the bulk video pipeline: for streaming sites the
    # passive listener only catches m3u8/.ts fragments
    # whereas yt-dlp produces a single playable .mp4.
    #
    # Enhancement (job 2d2e99c3829c): many video sites
    # embed their player in a 3rd-party iframe whose
    # OUTER URL yt-dlp doesn't recognise (e.g.
    # bird.openhub.tv/frame?pi=<opaque-token>). The
    # actual HLS playlist lives INSIDE the iframe and
    # gets surfaced in this session's network_log when
    # playback fires. So: before falling back to yt-dlp
    # on the page URL, sniff network_log for any
    # .m3u8 / .mpd entry, nudge <video>/<audio> to
    # autoplay to populate it, and use the sniffed URL
    # as the higher-priority candidate. If sniff fails,
    # behaviour reverts to the original page-URL path.
    target_url = action.get("url") or ""
    user_pinned_url = bool(target_url)
    # ``iframe_walk`` controls Tier 4 below. Default True
    # for the SDK call (operators want the best-effort
    # fallback); explicit False lets a caller skip the
    # invasive navigation step.
    iframe_walk_enabled = bool(
        action.get("iframe_walk", True)
    )
    if not target_url:
        try:
            st = await tab.evaluate("document.location.href")
            target_url = st or ""
        except Exception:
            target_url = ""
    if not target_url:
        reply.status = "ERR: no url for download_video"
    else:
        from core.fetcher import run_ytdlp

        videos_dir = state.assets_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        timeout_s = int(action.get("timeout_s") or 1800)
        referer = action.get("referer")
        # Default referer to the current page URL when
        # user-pinned URL points at a different host
        # (e.g. m3u8 on a CDN). Many CDNs reject bare
        # requests without a plausible Referer.
        if not referer:
            try:
                referer = await tab.evaluate(
                    "document.location.href"
                )
            except Exception:
                pass

        # ---- candidate URL list (priority ordered) ----
        # Tier 1: user-pinned ``url=`` (caller knows best)
        # Tier 2: deterministic DOM/network discovery
        #         - <video src> / <source src>
        #         - .m3u8 / .mpd in network_log
        # Tier 3: trigger playback + re-sniff
        # Tier 4: iframe walk (navigate into player iframes)
        # Tier 5: page URL (original fallback)
        #
        # All heuristics are VENDOR-NEUTRAL -- URL shape
        # and DOM structure, no hostnames hardcoded.
        # See _looks_like_player_iframe / _PLAYER_PATH_KEYWORDS.
        candidates: list[dict] = []
        sniffed_stream: Optional[str] = None
        dom_video_urls: list[str] = []
        iframe_walk_done = False

        if user_pinned_url or _VIDEO_STREAM_RE.search(target_url) \
                or _VIDEO_DIRECT_RE.search(target_url):
            # Caller knows what they want -- skip discovery.
            candidates.append({
                "url": target_url,
                "referer": referer,
                "label": (
                    "user-pinned url" if user_pinned_url
                    else "page url (is a stream)"
                ),
            })
        else:
            # ---- Tier 2: cheap discovery (no waits / no nav) ----
            dom_video_urls = await _extract_dom_video_urls(tab)
            for u in dom_video_urls:
                candidates.append({
                    "url": u,
                    "referer": referer or target_url,
                    "label": "DOM <video|source>[src]",
                })
            for u in _sniff_stream_urls_from_log(
                state.network_log
            ):
                if not sniffed_stream:
                    sniffed_stream = u
                candidates.append({
                    "url": u,
                    "referer": referer or target_url,
                    "label": "network_log .m3u8/.mpd",
                })

            # ---- Tier 3: trigger playback, re-sniff ----
            # Only if Tier 2 yielded nothing; otherwise we
            # already have something to try. Modern
            # browsers block programmatic .play() without
            # a user gesture, so we ALSO synthesise a
            # click on the most play-like visible element
            # (vendor-neutral heuristic).
            if not candidates:
                await _trigger_video_playback(tab)
                clicked = await _try_click_play_button(tab)
                if clicked:
                    _slog(
                        "[download_video] tier3: clicked "
                        "play-like element"
                    )
                # Short wait -- the operator usually
                # navigated here ages ago; playback +
                # 3-5s is plenty to surface a playlist.
                await asyncio.sleep(
                    5.0 if clicked else 3.0
                )
                for u in _sniff_stream_urls_from_log(
                    state.network_log
                ):
                    if not sniffed_stream:
                        sniffed_stream = u
                    candidates.append({
                        "url": u,
                        "referer": referer or target_url,
                        "label": "post-play network sniff",
                    })

            # Last resort within the original page:
            # let yt-dlp try the page URL itself before
            # we go invasive (iframe walk). It works for
            # the many sites whose page IS a yt-dlp
            # extractor target.
            candidates.append({
                "url": target_url,
                "referer": referer,
                "label": "page url",
            })

        # ---- yt-dlp + upload + verify loop over candidates ----
        # Keep trying candidates / tiers until one yields a VALID video
        # that we preserved (see the media oracle below); a mere uploaded
        # file (which may be HTML-as-mp4 / 0-byte / a short ad) does NOT
        # stop the search. Each candidate gets its own cookies.txt
        # (host-scoped, see ``ask``).
        upload_timeout = 30 * 60.0
        uploaded: list[str] = []
        upload_errors: list[str] = []
        new_files_all: list[str] = []
        validations: list[dict] = []
        valid_uploaded: list[str] = []
        ok = False
        msg = ""
        tried_labels: list[str] = []
        # Media-oracle duration floor; a caller who knows the target is
        # long can tighten it with ``min_duration_s``.
        min_dur = _MIN_VALID_VIDEO_S
        try:
            if action.get("min_duration_s") is not None:
                min_dur = float(action.get("min_duration_s"))
        except (TypeError, ValueError):
            min_dur = _MIN_VALID_VIDEO_S

        async def _ingest(cand_new, source_url, page_url):
            """Upload + ffprobe-validate each freshly-downloaded file and
            record it. Appends to ``uploaded`` / ``validations`` and, for a
            valid + preserved file, ``valid_uploaded``. Returns True iff a
            valid video was just preserved -- so the candidate search stops
            on a real video, never on junk."""
            got_valid = False
            for name in cand_new:
                path = videos_dir / name
                mime = "video/mp4" if path.suffix == ".mp4" else None
                up_ok = False
                try:
                    up_ok = await agent._upload_one_session_asset(
                        state,
                        path,
                        mime=mime,
                        source_url=source_url,
                        page_url=page_url,
                        timeout=upload_timeout,
                    )
                    if up_ok:
                        uploaded.append(name)
                    else:
                        size_b = 0
                        try:
                            size_b = path.stat().st_size
                        except Exception:
                            pass
                        upload_errors.append(
                            f"{name} ({size_b // 1024} KB): upload did not "
                            f"complete (asset_upload_base missing, "
                            f"already-uploaded, or HTTP / timeout error -- "
                            f"see worker stderr)"
                        )
                except Exception as e:
                    upload_errors.append(f"{name}: {type(e).__name__}: {e}")
                    _slog(f"[download_video] upload {name} failed: {e}")
                # ffprobe every downloaded file (uploaded or not) so the
                # result reports real-video-ness even when upload failed or
                # there is no parent job; only a valid + preserved file lets
                # the search stop.
                v = await asyncio.to_thread(
                    _probe_media_sync, str(path), min_dur
                )
                v["name"] = name
                v["uploaded"] = up_ok
                validations.append(v)
                if v.get("valid") and up_ok:
                    valid_uploaded.append(name)
                    got_valid = True
            return got_valid

        for cand in candidates:
            cand_url = cand["url"]
            cand_ref = cand["referer"]
            label = cand["label"]
            tried_labels.append(label)
            before = {
                p.name for p in videos_dir.iterdir() if p.is_file()
            }
            cookies_file = await agent._fetch_cookies_txt_for(
                cand_url,
                state,
                _slog,
            )
            _slog(
                f"[download_video] yt-dlp [{label}] "
                f"{cand_url[:120]} "
                f"(timeout {timeout_s}s"
                + (", +cookies" if cookies_file else "")
                + ")"
            )
            # yt-dlp is sync (subprocess.run); offload to
            # a worker thread so the event loop keeps
            # pumping the WS heartbeat etc.
            ok, msg = await asyncio.to_thread(
                run_ytdlp,
                cand_url,
                videos_dir,
                cand_ref,
                None,  # cookies_from_browser
                timeout_s,
                _slog,
                cookies_file,  # cookies_file (Netscape)
            )
            if cookies_file:
                try:
                    cookies_file.unlink()
                except OSError:
                    pass
            after = {
                p.name for p in videos_dir.iterdir() if p.is_file()
            }
            cand_new = sorted(after - before)
            new_files_all.extend(cand_new)
            # Upload each new artefact to the parent job.
            # Per-file timeout = 30 min: yt-dlp output
            # for an HD video can be hundreds of MB and
            # the shared httpx client uses 60s by
            # default -- not nearly enough. Without this
            # override the upload silently ReadTimeouts
            # and the file is lost. (Job ad1846fbbcbc.)
            await _ingest(cand_new, cand_url, target_url)
            # First candidate that lands a file in the
            # gallery wins; skip remaining fallbacks.
            if valid_uploaded:
                break

        # ---- Tier 3.5: post-failure re-sniff ----
        # When every candidate so far returned "Unsupported
        # URL" (typical signature of yt-dlp probing a page
        # whose extractor it doesn't have) AND the user
        # didn't pin a URL, give the playlist a last chance
        # to surface. Two things happen during the
        # candidate loop that the original Tier 2/3 sniff
        # can't catch:
        #   1) yt-dlp's HTTP probe of the page URL often
        #      causes the page's player JS to start
        #      loading the real .m3u8 (analytics ping,
        #      autoplay kicks in after DOMContentLoaded).
        #   2) The user-gesture click in Tier 3 might
        #      only have effect after a few hundred ms
        #      of JS work that exceeded the original
        #      3-5s wait.
        # So: pause briefly to let the network log catch
        # up, re-sniff, and retry anything new.
        unsupported = "Unsupported URL" in (msg or "")
        if (
            not valid_uploaded
            and not user_pinned_url
            and unsupported
        ):
            tried_urls = {c["url"] for c in candidates}
            await asyncio.sleep(3.0)
            new_streams = [
                u for u in _sniff_stream_urls_from_log(
                    state.network_log
                )
                if u not in tried_urls
            ]
            if new_streams:
                _slog(
                    f"[download_video] post-failure re-sniff: "
                    f"{len(new_streams)} new stream URL(s) "
                    f"appeared after first pass exhausted with "
                    f"'Unsupported URL'"
                )
                # Bound the retry count -- if 3 attempts on
                # newly-discovered playlists still fail, the
                # site probably needs the iframe walk (Tier 4)
                # to enter the player frame proper.
                for stream_url in new_streams[:3]:
                    tried_urls.add(stream_url)
                    before = {
                        p.name for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cookies_file = (
                        await agent._fetch_cookies_txt_for(
                            stream_url, state, _slog,
                        )
                    )
                    _slog(
                        f"[download_video] yt-dlp "
                        f"[re-sniffed .m3u8/.mpd] "
                        f"{stream_url[:120]} "
                        f"(timeout {timeout_s}s"
                        + (", +cookies" if cookies_file else "")
                        + ")"
                    )
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp,
                        stream_url,
                        videos_dir,
                        referer or target_url,
                        None,
                        timeout_s,
                        _slog,
                        cookies_file,
                    )
                    if cookies_file:
                        try:
                            cookies_file.unlink()
                        except OSError:
                            pass
                    after = {
                        p.name for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cand_new = sorted(after - before)
                    new_files_all.extend(cand_new)
                    await _ingest(cand_new, stream_url, target_url)
                    tried_labels.append(
                        "re-sniffed .m3u8/.mpd"
                    )
                    if valid_uploaded:
                        break

        # ---- Tier 4: iframe walk (Phase 3a) ----
        # Two phases per frame:
        #
        #   Phase A (NEW, in-place CDP): for each frame,
        #     use Page.createIsolatedWorld(frameId) +
        #     Runtime.evaluate(contextId=...) to harvest
        #     <video>/<source> URLs AND synthesise a
        #     user-gesture play click WITHOUT replacing
        #     the top frame. Works on players that
        #     refuse to load when not framed (window.top
        #     === window.self refusal).
        #
        #   Phase B (legacy, full navigate): for any
        #     frame Phase A yielded nothing usable on,
        #     fall back to the existing
        #     ``page.navigate(iframe_src)`` approach so
        #     we don't lose ground on sites where the
        #     iframe REQUIRES top-level loading.
        #
        # Frames discovered via CDP Page.getFrameTree
        # (recursive, depth=3) so JS-injected and
        # nested iframes are also visited.
        # All heuristics vendor-neutral.
        if (
            not valid_uploaded
            and not user_pinned_url
            and iframe_walk_enabled
            and not iframe_walk_done
        ):
            iframe_walk_done = True
            try:
                all_frames = await _enumerate_all_frames(tab)
            except Exception as e:
                _slog(
                    f"[download_video] frame enumeration "
                    f"failed: {type(e).__name__}: {e}"
                )
                all_frames = []
            # Filter + prioritise: player-shaped URLs
            # first (heuristic match), then anything
            # else (catch-all in case the heuristic
            # underrates). Within each bucket, shallow
            # depth first.
            prio_frames: list[tuple[int, int, dict]] = []
            for fr in all_frames:
                bucket = (
                    0 if _looks_like_player_iframe(fr["url"])
                    else 1
                )
                prio_frames.append((bucket, fr["depth"], fr))
            prio_frames.sort(key=lambda t: (t[0], t[1]))
            if prio_frames:
                _slog(
                    f"[download_video] in-page candidates "
                    f"exhausted; entering iframe walk "
                    f"({len(prio_frames)} frame(s) total, "
                    f"{sum(1 for t in prio_frames if t[0] == 0)} "
                    f"player-shaped)"
                )
            # Capture original URL ONCE so Phase B can
            # restore the operator's view after a
            # fallback navigate (Phase A doesn't
            # navigate, so the restore is a no-op for
            # in-place hits).
            orig_url_for_restore = target_url
            try:
                orig_url_for_restore = (
                    await tab.evaluate("document.location.href")
                    or target_url
                )
            except Exception:
                pass

            # ---------- Phase A: in-place per-frame ----------
            # Don't navigate. Just probe each frame via
            # isolated worlds. If we get a usable URL,
            # try yt-dlp with the frame's URL as referer.
            phase_a_winners: set[str] = set()
            for bucket, depth, fr in prio_frames:
                if valid_uploaded:
                    break
                frame_id = fr["frame_id"]
                frame_url = fr["url"] or ""
                _slog(
                    f"[download_video] frame in-place "
                    f"@depth={depth} bucket={bucket}: "
                    f"{frame_url[:120]}"
                )
                # Snapshot network_log size BEFORE any
                # click so we can tell "this manifest
                # is from THIS frame's click attempt"
                # vs "manifest was already there".
                # Note: shared log, no per-frame split;
                # we just use the new entries as a
                # weak attribution signal.
                try:
                    log_size_before = len(state.network_log or [])
                except Exception:
                    log_size_before = 0
                in_place_cands: list[dict] = []
                # 1) DOM extraction inside the frame.
                try:
                    pre_click_dom = (
                        await _extract_dom_video_urls_in_frame(
                            tab, frame_id,
                        )
                    )
                except Exception as e:
                    _slog(
                        f"[download_video] frame DOM probe "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    pre_click_dom = []
                for u in pre_click_dom:
                    in_place_cands.append({
                        "url": u,
                        "referer": frame_url,
                        "label": (
                            f"frame[d{depth}] DOM in-place"
                        ),
                    })
                # 2) Try synthesising a user-gesture
                # click inside the frame. This is the
                # step that unlocks autoplay-blocked
                # HLS without replacing the top frame.
                try:
                    clicked = (
                        await _try_click_play_button_in_frame(
                            tab, frame_id,
                        )
                    )
                except Exception as e:
                    _slog(
                        f"[download_video] frame click "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    clicked = False
                if clicked:
                    _slog(
                        f"[download_video] frame in-place "
                        f"[d{depth}]: clicked play-like "
                        f"element"
                    )
                    await asyncio.sleep(5.0)
                    # 3) Re-extract after click in case
                    # the player added a <video> tag
                    # post-init.
                    try:
                        post_click_dom = (
                            await _extract_dom_video_urls_in_frame(
                                tab, frame_id,
                            )
                        )
                    except Exception:
                        post_click_dom = []
                    for u in post_click_dom:
                        if not any(c["url"] == u for c in in_place_cands):
                            in_place_cands.append({
                                "url": u,
                                "referer": frame_url,
                                "label": (
                                    f"frame[d{depth}] DOM "
                                    f"in-place (post-click)"
                                ),
                            })
                # 4) New network log entries since
                # before the click -- shared log, but
                # the temporal correlation is a useful
                # weak signal.
                try:
                    log_tail = (
                        (state.network_log or [])[log_size_before:]
                    )
                    fresh_sniffs = _sniff_stream_urls_from_log(
                        log_tail
                    )
                except Exception:
                    fresh_sniffs = []
                for u in fresh_sniffs:
                    if not any(c["url"] == u for c in in_place_cands):
                        in_place_cands.append({
                            "url": u,
                            "referer": frame_url,
                            "label": (
                                f"frame[d{depth}] sniff "
                                f"(after in-place click)"
                            ),
                        })
                # 5) Run yt-dlp on the in-place
                # candidates.
                for cand in in_place_cands:
                    cand_url = cand["url"]
                    cand_ref = cand["referer"]
                    label = cand["label"]
                    tried_labels.append(label)
                    before = {
                        p.name
                        for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cookies_file = await agent._fetch_cookies_txt_for(
                        cand_url, state, _slog,
                    )
                    _slog(
                        f"[download_video] yt-dlp "
                        f"[{label}] {cand_url[:120]}"
                    )
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp,
                        cand_url, videos_dir, cand_ref,
                        None, timeout_s, _slog, cookies_file,
                    )
                    if cookies_file:
                        try:
                            cookies_file.unlink()
                        except OSError:
                            pass
                    after = {
                        p.name
                        for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cand_new = sorted(after - before)
                    new_files_all.extend(cand_new)
                    await _ingest(cand_new, cand_url, orig_url_for_restore)
                    if valid_uploaded:
                        break

            # ---------- Phase B: legacy navigate ----------
            # For frames Phase A didn't crack, fall
            # back to the original "navigate top frame
            # to iframe URL" approach. Only do this
            # when nothing landed in uploaded yet.
            # Reuse the same frame ordering.
            phase_b_frames = [
                (b, d, fr)
                for (b, d, fr) in prio_frames
                if fr["frame_id"] not in phase_a_winners
                and _looks_like_player_iframe(fr["url"])
            ]
            for ifr_idx, (_b, _d, _fr) in enumerate(phase_b_frames, 1):
                if valid_uploaded:
                    break
                ifr_src = _fr["url"]
                if valid_uploaded:
                    break
                _slog(
                    f"[download_video] iframe walk Phase B "
                    f"[{ifr_idx}/{len(phase_b_frames)}]: "
                    f"{ifr_src[:120]}"
                )
                try:
                    from nodriver import cdp as _cdp_nav
                    # Spoof the Referer so iframe player
                    # endpoints that require the parent
                    # origin (typical 3rd-party players
                    # serve nothing without it) get one.
                    # Vendor-neutral: we pass the URL we
                    # navigated from, which is exactly
                    # what the browser would have sent
                    # if the iframe loaded normally.
                    try:
                        await tab.send(
                            _cdp_nav.network.set_extra_http_headers(
                                headers=_cdp_nav.network.Headers(
                                    {"Referer": orig_url_for_restore}
                                ),
                            )
                        )
                    except Exception as e:
                        _slog(
                            f"[download_video] iframe set "
                            f"Referer header failed: "
                            f"{type(e).__name__}: {e}"
                        )
                    await tab.send(
                        _cdp_nav.page.navigate(ifr_src)
                    )
                except Exception as e:
                    _slog(
                        f"[download_video] iframe nav "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    continue
                # Settle: HTTP + script load + initial
                # autoplay. 4s is a compromise between
                # "give HLS time" and "don't hang".
                await asyncio.sleep(4.0)
                await _trigger_video_playback(tab)
                # Modern players block autoplay without
                # a user gesture -- synthesise a click
                # on the most play-like visible element
                # (vendor-neutral). This is the key step
                # that unlocks the HLS manifest request
                # the iframe walk depends on.
                ifr_clicked = await _try_click_play_button(tab)
                if ifr_clicked:
                    _slog(
                        f"[download_video] iframe[{ifr_idx}]: "
                        f"clicked play-like element"
                    )
                # Longer wait when we clicked -- gives
                # the player time to initialise + load
                # the playlist before sniff.
                await asyncio.sleep(
                    6.0 if ifr_clicked else 3.0
                )
                # Re-gather candidates from inside the
                # iframe's now-main-tab context.
                iframe_cands: list[dict] = []
                seen_in_walk = set()
                for u in await _extract_dom_video_urls(tab):
                    if u in seen_in_walk:
                        continue
                    seen_in_walk.add(u)
                    iframe_cands.append({
                        "url": u,
                        "referer": ifr_src,
                        "label": (
                            f"iframe[{ifr_idx}] "
                            f"DOM <video|source>"
                        ),
                    })
                for u in _sniff_stream_urls_from_log(
                    state.network_log
                ):
                    if u in seen_in_walk:
                        continue
                    seen_in_walk.add(u)
                    if not sniffed_stream:
                        sniffed_stream = u
                    iframe_cands.append({
                        "url": u,
                        "referer": ifr_src,
                        "label": (
                            f"iframe[{ifr_idx}] "
                            f"network .m3u8/.mpd"
                        ),
                    })
                # Also try the iframe URL itself --
                # some hosts route yt-dlp recognisable
                # extractors at the player page.
                iframe_cands.append({
                    "url": ifr_src,
                    "referer": orig_url_for_restore,
                    "label": f"iframe[{ifr_idx}] url",
                })
                for cand in iframe_cands:
                    cand_url = cand["url"]
                    cand_ref = cand["referer"]
                    label = cand["label"]
                    tried_labels.append(label)
                    before = {
                        p.name
                        for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cookies_file = await agent._fetch_cookies_txt_for(
                        cand_url, state, _slog,
                    )
                    _slog(
                        f"[download_video] yt-dlp "
                        f"[{label}] {cand_url[:120]}"
                    )
                    ok, msg = await asyncio.to_thread(
                        run_ytdlp,
                        cand_url, videos_dir, cand_ref,
                        None, timeout_s, _slog, cookies_file,
                    )
                    if cookies_file:
                        try:
                            cookies_file.unlink()
                        except OSError:
                            pass
                    after = {
                        p.name
                        for p in videos_dir.iterdir()
                        if p.is_file()
                    }
                    cand_new = sorted(after - before)
                    new_files_all.extend(cand_new)
                    await _ingest(cand_new, cand_url, orig_url_for_restore)
                    if valid_uploaded:
                        break
            # Restore the operator's original view.
            # Best-effort: never fail the action if
            # this navigate-back errors (keep_session
            # users see the post-walk page in noVNC
            # which is acceptable). Also clear the
            # Referer override set during the walk so
            # subsequent operator browsing is normal.
            if iframe_walk_done and orig_url_for_restore:
                try:
                    from nodriver import cdp as _cdp_back
                    try:
                        await tab.send(
                            _cdp_back.network.set_extra_http_headers(
                                headers=_cdp_back.network.Headers({})
                            )
                        )
                    except Exception:
                        pass
                    await tab.send(
                        _cdp_back.page.navigate(orig_url_for_restore)
                    )
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        # Surface failed uploads in the reply message
        # so the operator UI can tell apart "yt-dlp
        # produced nothing" from "yt-dlp produced files
        # but they didn't ship".
        if upload_errors and ok:
            msg = msg + "\n[upload] " + "\n[upload] ".join(upload_errors)

        # ---- media oracle (⑤): ``ok`` is derived from ffprobe, which ran
        # inline as each candidate's files were ingested (see ``_ingest``),
        # so yt-dlp exit 0 / a file merely appearing (HTML-as-mp4 / 0-byte
        # / truncated / ad) can't fake success. ``valid_uploaded`` holds the
        # preserved valid videos; ``validations`` every probed file.
        valid_files = [v["name"] for v in validations if v.get("valid")]
        oracle_unavailable = any(
            v.get("reason") == "ffprobe_missing" for v in validations
        )
        if validations and not oracle_unavailable:
            # Honest success = we PRESERVED (uploaded) a playable video.
            ok = len(valid_uploaded) > 0
        else:
            # No probe possible (no ffprobe, or nothing downloaded) -- keep
            # the legacy meaning so we don't regress on such workers.
            ok = bool(uploaded)
        _slog(
            f"[download_video] done ok={ok} "
            f"candidates={len(candidates)} "
            f"tried={tried_labels} "
            f"new_files={len(new_files_all)} "
            f"uploaded={len(uploaded)} "
            f"valid={len(valid_files)} valid_uploaded={len(valid_uploaded)}"
            + ("" if not oracle_unavailable else " (oracle:ffprobe_missing)")
        )
        reply.result = {
            "ok": ok,
            "url": target_url,
            "message": msg,
            "files": uploaded,
            "file_count": len(uploaded),
            # Media oracle: per-file ffprobe validation. ``ok`` is True iff
            # a valid video was PRESERVED (valid_uploaded non-empty), or the
            # oracle was unavailable. ``valid_files`` lists every downloaded
            # file that is real playable video (even if its upload failed),
            # so the operator / codegen LLM can tell "preserved real video"
            # from "saved junk" from "fetched real video but upload failed".
            "valid_files": valid_files,
            "valid_file_count": len(valid_files),
            "valid_uploaded_count": len(valid_uploaded),
            "validations": validations,
            "oracle_available": (not oracle_unavailable) and bool(validations),
            # Diagnostic fields so the operator / codegen
            # LLM can see WHICH path produced the file
            # (or why it failed).
            "sniffed_stream": sniffed_stream,
            "dom_video_urls": dom_video_urls,
            "iframe_walk_done": iframe_walk_done,
            "candidates_tried": tried_labels,
        }
