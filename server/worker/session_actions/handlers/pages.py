"""Session-action handlers (pages). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations
import asyncio

from server.worker.session_actions._registry import _session_action, _ActionCtx


@_session_action("pages", read_only=True, session_level=True)
async def _act_pages(agent, ctx: "_ActionCtx") -> None:
    # List all tabs: {page_id, url, title, is_default}. URL / title
    # are best-effort (a just-navigated tab may not have them yet).
    items: list[dict] = []
    for pid, t in list(ctx.state.pages.items()):
        url = ""
        title = ""
        try:
            url = await t.evaluate("document.location.href") or ""
        except Exception:
            pass
        try:
            title = await t.evaluate("document.title") or ""
        except Exception:
            pass
        items.append(
            {
                "page_id": pid,
                "url": url,
                "title": title,
                "is_default": pid == ctx.state.default_page_id,
            }
        )
    ctx.reply.result = {
        "count": len(items),
        "default_page_id": ctx.state.default_page_id,
        "pages": items,
    }


@_session_action("new_page", read_only=False, session_level=True)
async def _act_new_page(agent, ctx: "_ActionCtx") -> None:
    # Open a new tab. ``url`` (default about:blank); ``switch`` flips
    # default_page_id to it so un-keyed primitives target the new tab.
    import uuid as _uuid

    new_url = (ctx.action.get("url") or "about:blank").strip()
    switch = bool(ctx.action.get("switch", False))
    browser_handle = ctx.state.browser
    if browser_handle is None:
        ctx.reply.status = "ERR: session has no browser handle"
    else:
        try:
            new_tab = await browser_handle.get(
                new_url,
                new_tab=True,
            )
        except Exception as e:
            ctx.reply.status = f"ERR: new_page failed: {type(e).__name__}: {e}"
        else:
            # ``browser.get(url, new_tab=True)`` returns as soon as
            # the target exists, NOT once Page.navigate ran. Poll
            # briefly so a follow-up state()/reload() doesn't sample
            # the tab while still on about:blank.
            if new_url and not new_url.startswith("about:"):
                for _ in range(30):  # ~3s ceiling
                    try:
                        cur = await new_tab.evaluate(
                            "document.location.href",
                        )
                    except Exception:
                        cur = None
                    if (
                        isinstance(cur, str)
                        and cur
                        and not cur.startswith("about:")
                    ):
                        break
                    await asyncio.sleep(0.1)
            pid = "p_" + _uuid.uuid4().hex[:8]
            ctx.state.pages[pid] = new_tab
            ctx.state.page_locks[pid] = asyncio.Lock()
            if switch or ctx.state.default_page_id is None:
                ctx.state.default_page_id = pid
            ctx.slog(f"new_page: opened {pid} -> {new_url} (switch={switch})")
            ctx.reply.result = {
                "page_id": pid,
                "url": new_url,
                "is_default": pid == ctx.state.default_page_id,
            }


@_session_action("close_page", read_only=False, session_level=True)
async def _act_close_page(agent, ctx: "_ActionCtx") -> None:
    # Close one tab (``page_id`` required). Closing the default page
    # is allowed iff another remains; default auto-moves to the
    # most-recently-added page.
    pid = ctx.action.get("page_id") or ""
    if not pid:
        ctx.reply.status = "ERR: close_page requires page_id"
    elif pid not in ctx.state.pages:
        ctx.reply.status = (
            f"ERR: unknown page_id {pid!r} (known: {sorted(ctx.state.pages.keys())})"
        )
    elif len(ctx.state.pages) <= 1:
        ctx.reply.status = (
            f"ERR: cannot close the last remaining "
            f"page ({pid}); end the session instead"
        )
    else:
        t = ctx.state.pages.pop(pid)
        ctx.state.page_locks.pop(pid, None)
        if pid == ctx.state.default_page_id:
            # Fall back to most-recently-added page.
            ctx.state.default_page_id = next(reversed(list(ctx.state.pages.keys())))
            ctx.slog(f"close_page: default moved to {ctx.state.default_page_id}")
        try:
            await t.close()
        except Exception as e:
            ctx.slog(
                f"close_page: tab.close raised "
                f"{type(e).__name__}: {e} (already gone?)"
            )
        ctx.slog(f"close_page: closed {pid}")
        ctx.reply.result = {
            "closed_page_id": pid,
            "default_page_id": ctx.state.default_page_id,
        }


@_session_action("switch_page", read_only=True, session_level=True)
async def _act_switch_page(agent, ctx: "_ActionCtx") -> None:
    # Change the default tab (where un-keyed primitives land).
    pid = ctx.action.get("page_id") or ""
    if not pid:
        ctx.reply.status = "ERR: switch_page requires page_id"
    elif pid not in ctx.state.pages:
        ctx.reply.status = (
            f"ERR: unknown page_id {pid!r} (known: {sorted(ctx.state.pages.keys())})"
        )
    else:
        ctx.state.default_page_id = pid
        # Best-effort: bring it to the visual front in noVNC.
        try:
            t = ctx.state.pages[pid]
            if hasattr(t, "activate"):
                await t.activate()
            elif hasattr(t, "bring_to_front"):
                await t.bring_to_front()
        except Exception:
            pass
        ctx.reply.result = {"default_page_id": pid}
