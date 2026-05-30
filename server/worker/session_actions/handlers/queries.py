"""Session-action handlers (queries). Auto-registered into
_SESSION_ACTIONS via the @_session_action decorator."""
from __future__ import annotations

from server.worker.session_actions._registry import _session_action, _ActionCtx
from server.worker import browser_ops
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
)


@_session_action("outline", read_only=True)
async def _act_outline(agent, ctx: "_ActionCtx") -> None:
    ctx.reply.result = await browser_ops.outline(
        ctx.tab,
        visited_urls=ctx.state.visited_urls,
    )


@_session_action("visited", read_only=True)
async def _act_visited(agent, ctx: "_ActionCtx") -> None:
    ctx.reply.result = list(ctx.state.visited_urls_ordered)


@_session_action("last_response", read_only=True)
async def _act_last_response(agent, ctx: "_ActionCtx") -> None:
    # Most recent main-document HTTP response observed on this
    # session (goto / back / forward / reload / click-navigation),
    # updated by the passive tracker installed at session_start.
    # None until a document response has been seen.
    ctx.reply.result = ctx.state.last_response


@_session_action("network", read_only=True)
async def _act_network(agent, ctx: "_ActionCtx") -> None:
    # Session network traffic log for the Live panel "Network" tab.
    # ``since`` enables incremental polling (only newer entries).
    since_ts = float(ctx.action.get("since", 0) or 0)
    entries = ctx.state.network_log
    if since_ts:
        entries = [e for e in entries if e.get("timestamp", 0) > since_ts]
    ctx.reply.result = {
        "count": len(ctx.state.network_log),
        "entries": entries,
    }


@_session_action("state", read_only=True)
async def _act_state(agent, ctx: "_ActionCtx") -> None:
    try:
        title = await ctx.tab.evaluate("document.title")
    except Exception:
        title = ""
    ctx.reply.result = {
        "url": ctx.cur,
        "title": title or "",
        "lane_idx": ctx.state.lane.lane_idx,
        "visited_count": len(ctx.state.visited_urls),
    }


@_session_action("links", read_only=True)
async def _act_links(agent, ctx: "_ActionCtx") -> None:
    # Every <a href> on the page resolved to absolute URLs. The JS
    # lives in module-scope _LINKS_EXTRACT_JS (shared with the
    # session-end dump). nodriver returns arrays as a JSON string for
    # non-scalars, so JSON.stringify on the JS side + json.loads here.
    raw_str = None
    try:
        raw_str = await ctx.tab.evaluate(_LINKS_EXTRACT_JS)
    except Exception as e:
        ctx.reply.status = f"ERR: links eval failed: {e}"
    items: list = []
    if isinstance(raw_str, str) and raw_str:
        import json as _json

        try:
            parsed = _json.loads(raw_str)
            if isinstance(parsed, list):
                items = parsed
        except Exception:
            pass
    elif isinstance(raw_str, list):
        # Some nodriver versions auto-decode JSON; accept that too.
        items = raw_str
    ctx.reply.result = {
        "current_url": ctx.cur or "",
        "count": len(items),
        "links": items,
    }


@_session_action("exists", read_only=True)
async def _act_exists(agent, ctx: "_ActionCtx") -> None:
    # CSS selector exists check -- cheap, deterministic; used by
    # macros / scripts for if/else branching without an LLM.
    selector = ctx.action.get("selector") or ""
    status, found = await browser_ops.exists(ctx.tab, selector, ctx.slog)
    ctx.reply.status = status
    ctx.reply.result = bool(found)


@_session_action("get_cookies", read_only=True)
async def _act_get_cookies(agent, ctx: "_ActionCtx") -> None:
    # Dump cookies via CDP Network.getAllCookies (or getCookies when
    # ``urls`` narrows it). Used by the "save cookies to host" button.
    from nodriver import cdp as _cdp

    urls = ctx.action.get("urls")
    if urls:
        cookies = await ctx.tab.send(_cdp.network.get_cookies(urls=list(urls)))
    else:
        cookies = await ctx.tab.send(_cdp.network.get_all_cookies())
    # Project CDP Cookie objects to plain dicts the host registry accepts.
    out: list[dict] = []
    for c in cookies or []:
        try:
            d = c.to_json() if hasattr(c, "to_json") else dict(vars(c))
        except Exception:
            d = {}
        if not d:
            continue
        out.append(d)
    ctx.reply.result = {
        "current_url": ctx.cur or "",
        "count": len(out),
        "cookies": out,
    }
