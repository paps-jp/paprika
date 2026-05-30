"""Session-action handlers (files). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations
import os
import tempfile
import time

from server.worker.session_actions._registry import _session_action, _ActionCtx
from server.worker import browser_ops
from server.worker._browser_helpers import (
    _paprika_agent_run,
)


@_session_action("resize_window", read_only=False)
async def _act_resize_window(agent, ctx: "_ActionCtx") -> None:
    # Resize the Chrome OS window via CDP Browser.setWindowBounds.
    # The X display stays its native size; Chrome clamps edge cases.
    try:
        width = int(ctx.action.get("width") or 0)
        height = int(ctx.action.get("height") or 0)
    except Exception:
        width = height = 0
    if width < 200 or height < 200:
        ctx.reply.status = (
            f"ERR: resize_window: width / height must "
            f"be >= 200 (got {width}x{height})"
        )
    elif width > 4096 or height > 4096:
        ctx.reply.status = (
            f"ERR: resize_window: width / height must "
            f"be <= 4096 (got {width}x{height})"
        )
    else:
        try:
            from nodriver import cdp

            wfor = await ctx.tab.send(
                cdp.browser.get_window_for_target(),
            )
            # nodriver returns (window_id, bounds) tuple.
            if isinstance(wfor, tuple) and len(wfor) >= 1:
                window_id = wfor[0]
            else:
                window_id = getattr(wfor, "window_id", wfor)
            await ctx.tab.send(
                cdp.browser.set_window_bounds(
                    window_id=window_id,
                    bounds=cdp.browser.Bounds(
                        width=width,
                        height=height,
                        window_state=cdp.browser.WindowState.NORMAL,
                    ),
                ),
            )
            ctx.reply.result = {
                "width": width,
                "height": height,
                "window_id": int(window_id)
                if isinstance(window_id, (int, str)) and str(window_id).isdigit()
                else None,
            }
            ctx.slog(f"[resize_window] {width}x{height}")
        except Exception as e:
            ctx.reply.status = (
                f"ERR: resize_window CDP call failed: {type(e).__name__}: {e}"
            )


@_session_action("zoom", read_only=True)
async def _act_zoom(agent, ctx: "_ActionCtx") -> None:
    # In-browser PAGE zoom. Preferred: the Paprika Agent extension's
    # chrome.tabs.setZoom (genuine reflow zoom, works on cross-origin
    # iframe players). Fallback: CDP Emulation.setPageScaleFactor.
    try:
        z = float(ctx.action.get("factor") or 1.0)
    except Exception:
        z = 1.0
    if z < 0.25:
        z = 0.25
    elif z > 5.0:
        z = 5.0
    agent_out = None
    try:
        agent_out = await _paprika_agent_run(
            ctx.tab, "setZoom", {"factor": z},
            timeout=8.0, log=ctx.slog,
        )
    except Exception as e:
        ctx.slog(f"[zoom] agent path errored: {type(e).__name__}: {e}")
        agent_out = None
    if agent_out and agent_out.get("ok"):
        ctx.reply.result = {
            "factor": z,
            "method": "chrome.tabs.setZoom",
        }
        ctx.slog(f"[zoom] genuine zoom via agent = {z}")
    else:
        # Fallback: CDP pinch-zoom.
        try:
            from nodriver import cdp

            await ctx.tab.send(
                cdp.emulation.set_page_scale_factor(
                    page_scale_factor=z,
                ),
            )
            ctx.reply.result = {
                "factor": z,
                "method": "setPageScaleFactor(fallback)",
            }
            ctx.slog(
                f"[zoom] fallback setPageScaleFactor = {z} "
                f"(agent unavailable)"
            )
        except Exception as e:
            ctx.reply.status = (
                f"ERR: zoom failed (agent + CDP): "
                f"{type(e).__name__}: {e}"
            )


@_session_action("ext", read_only=True)
async def _act_ext(agent, ctx: "_ActionCtx") -> None:
    # Generic Paprika Agent extension command bus: relay cmd/args to
    # the extension service worker, return HANDLERS[cmd]'s result.
    # Vendor-neutral -- new capabilities never change this branch.
    cmd = ctx.action.get("cmd")
    cargs = ctx.action.get("args") or {}
    if not cmd:
        ctx.reply.status = "ERR: ext: missing 'cmd'"
    else:
        try:
            _to = float(ctx.action.get("timeout") or 8.0)
        except Exception:
            _to = 8.0
        # NOTE: reply.status defaults to "OK" (truthy), so gate on a
        # local flag -- not on `not reply.status`.
        out = None
        errored = False
        try:
            out = await _paprika_agent_run(
                ctx.tab, cmd, cargs, timeout=_to, log=ctx.slog,
            )
        except Exception as e:
            errored = True
            ctx.reply.status = (
                f"ERR: ext({cmd}): {type(e).__name__}: {e}"
            )
        if not errored:
            if out is None:
                ctx.reply.status = (
                    f"ERR: ext({cmd}): agent unreachable"
                )
            elif out.get("ok"):
                ctx.reply.result = out.get("result")
                ctx.slog(f"[ext] {cmd} ok")
            else:
                ctx.reply.status = (
                    f"ERR: ext({cmd}): {out.get('error')}"
                )


@_session_action("screenshot", read_only=True)
async def _act_screenshot(agent, ctx: "_ActionCtx") -> None:
    from nodriver import cdp

    png_b64 = await ctx.tab.send(
        cdp.page.capture_screenshot(format_="png"),
    )
    ctx.reply.result = png_b64
    # Optional: publish to the parent job's gallery when a ``label``
    # is given AND the session is job-bound. Keeps the plain
    # byte-return path untouched for callers that don't want it.
    label = ctx.action.get("label")
    if label and ctx.state.asset_upload_base is not None:
        try:
            import base64 as _b64

            ts = time.strftime("%Y%m%d-%H%M%S")
            # ms suffix so a sub-second burst doesn't collide.
            ms = int((time.time() % 1) * 1000)
            safe = browser_ops.safe_label(str(label)) or "shot"
            name = f"screenshot-{ts}-{ms:03d}-{safe}.png"
            shots_dir = ctx.state.assets_dir / "screenshots"
            shots_dir.mkdir(parents=True, exist_ok=True)
            png_path = shots_dir / name
            png_path.write_bytes(_b64.b64decode(png_b64))
            await agent._upload_one_session_asset(
                ctx.state,
                png_path,
                mime="image/png",
                asset_name=name,
            )
        except Exception as e:
            ctx.slog(f"screenshot gallery upload failed: {e}")


@_session_action("evaluate", read_only=False)
async def _act_evaluate(agent, ctx: "_ActionCtx") -> None:
    # Arbitrary JS in the tab's page context -- the keystone the SDK
    # builds Locator / wait_for_selector / hover / select_option on.
    # nodriver returns arrays/objects as RemoteObject descriptors, so
    # wrap as JSON.stringify(await (EXPR)) (a string crosses by value)
    # + json.loads here. Trailing ``;`` is stripped because the
    # wrapper needs a single expression (a ``;`` would null the result).
    import json as _json

    expr = ctx.action.get("expression") or ""
    expr = expr.strip()
    while expr.endswith(";"):
        expr = expr[:-1].rstrip()
    if not expr:
        ctx.reply.status = "ERR: evaluate failed: empty expression"
    else:
        wrapped = "(async()=>{return JSON.stringify(await (" + expr + "));})()"
        try:
            raw = await ctx.tab.evaluate(wrapped, await_promise=True)
            if isinstance(raw, str):
                try:
                    ctx.reply.result = _json.loads(raw)
                except Exception:
                    ctx.reply.result = raw
            else:
                # undefined / non-serialisable -> null
                ctx.reply.result = None
        except Exception as e:
            ctx.reply.status = f"ERR: evaluate failed: {browser_ops.short_error(e)}"


@_session_action("capture", read_only=False)
async def _act_capture(agent, ctx: "_ActionCtx") -> None:
    label = ctx.action.get("label") or "capture"
    step = int(ctx.action.get("step") or 0)
    snap = await browser_ops.capture(
        ctx.tab,
        label=label,
        step=step,
        assets_dir=ctx.state.assets_dir,
        log=ctx.slog,
    )
    # Upload the PNG only to the parent job's gallery (renamed to
    # screenshot-* for the Live filter). HTML / axtree stay local.
    if ctx.state.asset_upload_base is not None and snap.png_name:
        png_path = ctx.state.assets_dir / snap.label / snap.png_name
        if png_path.exists() and png_path.stat().st_size > 0:
            ts = time.strftime("%Y%m%d-%H%M%S")
            uploaded_name = f"screenshot-{ts}-{snap.label}.png"
            await agent._upload_one_session_asset(
                ctx.state,
                png_path,
                mime="image/png",
                page_url=snap.url or None,
                asset_name=uploaded_name,
            )
    ctx.reply.result = {
        "label": snap.label,
        "url": snap.url,
        "html_name": snap.html_name,
        "png_name": snap.png_name,
        "axtree_name": snap.axtree_name,
    }


@_session_action("set_input_files", read_only=False)
async def _act_set_input_files(agent, ctx: "_ActionCtx") -> None:
    # File upload: the client base64-encodes the file
    # bytes, we materialise them in a worker tempdir and
    # point the <input type=file> at the paths via CDP
    # DOM.setFileInputFiles (a JS expression can't set a
    # file input -- browsers forbid it). Chrome reads the
    # paths at form-submit time, so the temp files must
    # outlive this call; they're cleaned with the lane.
    import base64 as _b64

    from nodriver import cdp as _cdp

    selector = ctx.action.get("selector") or ""
    files = ctx.action.get("files") or []
    if not selector:
        ctx.reply.status = "ERR: set_input_files: empty selector"
    else:
        try:
            updir = tempfile.mkdtemp(prefix="paprika_upload_")
            paths: list[str] = []
            for f in files:
                name = (
                    os.path.basename(f.get("name") or "upload.bin") or "upload.bin"
                )
                data = _b64.b64decode(f.get("content_b64") or "")
                p = os.path.join(updir, name)
                with open(p, "wb") as fh:
                    fh.write(data)
                paths.append(p)
            doc = await ctx.tab.send(_cdp.dom.get_document())
            node_id = await ctx.tab.send(
                _cdp.dom.query_selector(
                    node_id=doc.node_id,
                    selector=selector,
                )
            )
            if not node_id:
                ctx.reply.status = "NO_MATCH"
            else:
                await ctx.tab.send(
                    _cdp.dom.set_file_input_files(
                        files=paths,
                        node_id=node_id,
                    )
                )
                ctx.reply.result = {
                    "files": [os.path.basename(p) for p in paths],
                    "count": len(paths),
                }
        except Exception as e:
            ctx.reply.status = (
                f"ERR: set_input_files failed: {browser_ops.short_error(e)}"
            )


@_session_action("fetch_refresh", read_only=False)
async def _act_fetch_refresh(agent, ctx: "_ActionCtx") -> None:
    # Operator-triggered refresh on a keep_session
    # post-fetch session. Captures the current page
    # HTML (the operator may have navigated via
    # noVNC) and pushes it to /jobs/{jid}/files/
    # page.html so /jobs/{jid}/links re-extracts
    # against the latest DOM. Then walks the worker
    # tempdir and uploads any files the passive CDP
    # listener wrote AFTER the original fetch returned
    # (e.g. .ts segments from a video the operator
    # played manually). Idempotent: re-running an
    # already-flushed refresh is cheap and just
    # returns added=[].
    state = ctx.state
    tab = ctx.tab
    added: list[str] = []
    html_uploaded = False
    current_url = ""
    try:
        current_url = (
            await tab.evaluate(
                "document.location.href",
            )
            or ""
        )
    except Exception:
        current_url = ""
    # ---- page.html refresh ----
    if state.asset_upload_base and state.job_id:
        try:
            html = await tab.evaluate(
                "document.documentElement.outerHTML",
            )
            if isinstance(html, str) and html:
                base = state.asset_upload_base.split("/jobs/", 1)[0]
                page_url = f"{base}/jobs/{state.job_id}/files/page.html"
                files = {
                    "file": (
                        "page.html",
                        html.encode("utf-8"),
                        "text/html",
                    )
                }
                data: dict[str, str] = {}
                if agent.worker_secret:
                    data["secret"] = agent.worker_secret
                r = await agent._http.post(
                    page_url,
                    files=files,
                    data=data,
                )
                r.raise_for_status()
                html_uploaded = True
        except Exception as e:
            ctx.slog(
                f"[fetch_refresh] page.html upload failed: {type(e).__name__}: {e}"
            )
    # ---- new-asset flush ----
    if state.assets_dir is not None:
        try:
            for p in sorted(state.assets_dir.rglob("*")):
                if not p.is_file():
                    continue
                if p.name in state.uploaded_assets:
                    continue
                ok = await agent._upload_one_session_asset(
                    state,
                    p,
                    page_url=current_url or None,
                )
                if ok:
                    added.append(p.name)
        except Exception as e:
            ctx.slog(f"[fetch_refresh] asset flush failed: {type(e).__name__}: {e}")
    ctx.slog(
        f"[fetch_refresh] current_url={current_url!r} "
        f"html_uploaded={html_uploaded} "
        f"added_assets={len(added)}"
    )
    ctx.reply.result = {
        "current_url": current_url,
        "html_uploaded": html_uploaded,
        "added": added,
        "added_count": len(added),
    }
