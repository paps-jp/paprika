"""Session-action handlers (cloudflare). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations
import time

from server.worker.session_actions._registry import _session_action, _ActionCtx


@_session_action("solve_cloudflare", read_only=False)
async def _act_solve_cloudflare(agent, ctx: "_ActionCtx") -> None:
    tab = ctx.tab
    reply = ctx.reply
    action = ctx.action
    _slog = ctx.slog
    # Get past a Cloudflare "Just a moment..." challenge.
    #
    # Two phases:
    #   1. WAIT: nodriver is an undetected real Chrome, so
    #      the common *managed* challenge auto-passes
    #      within a few seconds of executing the
    #      challenge JS. Poll the title until the marker
    #      disappears.
    #   2. CLICK (opt-in, default on): if the wait times
    #      out the challenge probably wants an explicit
    #      Turnstile checkbox click. Use nodriver's
    #      verify_cf() -- it template-matches the
    #      checkbox in a screenshot (opencv) and clicks
    #      it by coordinate, since the widget lives in a
    #      cross-origin iframe / shadow DOM unreachable
    #      via the DOM. Then poll again.
    #
    # Body: {timeout_s?: float, click_checkbox?: bool}
    # Result: {cleared, title, waited_s, clicked_checkbox}
    #
    # IMPORTANT: verify_cf() clicks the best template
    # match unconditionally (no confidence threshold in
    # nodriver), so we ONLY invoke it while the title
    # still shows a challenge marker -- never on a
    # normal page, which would mis-click random content.
    import asyncio as _asyncio

    timeout_s = float(action.get("timeout_s") or 25.0)
    click_checkbox = action.get("click_checkbox", True)
    poll = 1.0
    start = time.time()

    # Language-independent challenge detection. The
    # inline challenge page sets ``window._cf_chl_opt``
    # (gold signal) + loads a /challenge-platform/
    # script + a challenges.cloudflare.com iframe. A
    # multilingual title-marker list is the fallback --
    # the JA challenge title is "しばらくお待ちください..."
    # which an English-only check missed (the bug that
    # made the first test falsely report cleared=True).
    # On a cleared page none of these are present.
    _CF_DETECT_JS = (
        "(function(){try{"
        "if(window._cf_chl_opt)return true;"
        "if(document.getElementById('challenge-running'))return true;"
        "if(document.querySelector('script[src*=\"challenge-platform\"]'))return true;"
        "if(document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]'))return true;"
        "var t=(document.title||'').toLowerCase();"
        "var m=['just a moment','checking your browser',"
        "'attention required','\\u3057\\u3070\\u3089\\u304f\\u304a\\u5f85\\u3061',"
        "'\\u5c11\\u3005\\u304a\\u5f85\\u3061','\\u304a\\u5f85\\u3061\\u304f\\u3060\\u3055\\u3044',"
        "'un momento','einen moment'];"
        "for(var i=0;i<m.length;i++){if(t.indexOf(m[i])>=0)return true;}"
        "return false;"
        "}catch(e){return true;}})()"
    )

    async def _title() -> str:
        try:
            return (await tab.evaluate("document.title")) or ""
        except Exception:
            return ""

    async def _challenged() -> bool:
        try:
            return bool(await tab.evaluate(_CF_DETECT_JS))
        except Exception:
            # Can't evaluate (navigation in flight etc.)
            # -> assume still challenged, keep waiting.
            return True

    # Locate the Turnstile checkbox by the CF iframe's
    # page-coordinate bounding box. Language-independent
    # (unlike verify_cf's English template match, which
    # mis-clicks the JA "私はロボットではありません" widget).
    # The checkbox sits at the iframe's left edge,
    # vertically centred -- ~30px in from the left.
    _CF_IFRAME_RECT_JS = (
        "(function(){var f=document.querySelector("
        "'iframe[src*=\"challenges.cloudflare.com\"]');"
        "if(!f)return null;var r=f.getBoundingClientRect();"
        "if(!r||!r.width||!r.height)return null;"
        "return {x:r.left,y:r.top,w:r.width,h:r.height};})()"
    )

    async def _click_cf_checkbox() -> bool:
        # Primary: click by iframe rect (any language).
        rect = None
        try:
            rect = await tab.evaluate(_CF_IFRAME_RECT_JS)
        except Exception:
            rect = None
        if isinstance(rect, dict) and rect.get("w"):
            x = float(rect.get("x") or 0)
            y = float(rect.get("y") or 0)
            w = float(rect.get("w") or 0)
            h = float(rect.get("h") or 0)
            cx = x + min(30.0, w * 0.12)
            cy = y + h / 2.0
            try:
                await tab.mouse_click(cx, cy)
                _slog(
                    f"solve_cloudflare: clicked CF iframe "
                    f"checkbox at ({cx:.0f},{cy:.0f})"
                )
                return True
            except Exception as e:
                _slog(
                    f"solve_cloudflare: iframe-rect "
                    f"click failed: {type(e).__name__}: {e}"
                )
        # Fallback: nodriver template match (EN only).
        try:
            await tab.verify_cf()
            _slog(
                "solve_cloudflare: verify_cf() template click attempted (fallback)"
            )
            return True
        except Exception as e:
            _slog(
                f"solve_cloudflare: verify_cf fallback "
                f"failed: {type(e).__name__}: {e}"
            )
            return False

    # Phase 1: passive wait (auto-pass window).
    cleared = False
    deadline = start + timeout_s
    while time.time() < deadline:
        if not await _challenged():
            cleared = True
            break
        await _asyncio.sleep(poll)

    # Phase 2: checkbox click. Only while still
    # challenged + opted in. Retry a couple times --
    # the Turnstile widget can take a beat to render
    # its clickable checkbox after the page settles.
    clicked = False
    if not cleared and click_checkbox:
        for _attempt in range(3):
            if not await _challenged():
                cleared = True
                break
            if await _click_cf_checkbox():
                clicked = True
            # Re-poll ~8s after each click attempt.
            post_deadline = time.time() + 8.0
            while time.time() < post_deadline:
                if not await _challenged():
                    cleared = True
                    break
                await _asyncio.sleep(poll)
            if cleared:
                break

    waited = round(time.time() - start, 1)
    last_title = await _title()
    reply.result = {
        "cleared": cleared,
        "title": last_title,
        "waited_s": waited,
        "clicked_checkbox": clicked,
    }
    _slog(
        f"solve_cloudflare: cleared={cleared} "
        f"clicked={clicked} title={last_title!r} "
        f"waited={waited}s"
    )
