"""Page and Locator -- Playwright-shape async API."""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from ._client import PaprikaClient


def response_of(reply: dict | None) -> dict:
    """Extract the Playwright-compatible HTTP Response from a nav reply.

    Nav-kind actions (``page.goto`` / ``back`` / ``forward`` / ``reload``
    / ``history_first``) return a reply whose ``result["response"]``
    carries the captured HTTP response. This helper smooths over the
    "result might be None / not-a-dict / missing the key" edge cases
    and always returns a dict with the standard fields::

        {"url": "", "status": 0, "status_text": "", "ok": False,
         "headers": {}, "mime": ""}

    Usage::

        reply = await page.goto(url)
        r = response_of(reply)
        if r["status"] == 404:
            return None
        if not r["ok"]:
            raise RuntimeError(f"fetch failed: HTTP {r['status']}")

    Always returns a dict (never None), so direct subscript access
    is safe. ``status == 0`` means "no Document response captured"
    (cache hit, naked-media URL, capture timeout, etc.).
    """
    if not isinstance(reply, dict):
        return _EMPTY_RESPONSE.copy()
    result = reply.get("result")
    if not isinstance(result, dict):
        return _EMPTY_RESPONSE.copy()
    resp = result.get("response")
    if not isinstance(resp, dict):
        return _EMPTY_RESPONSE.copy()
    # Fill in any missing keys so callers can subscript freely.
    out = _EMPTY_RESPONSE.copy()
    out.update(resp)
    return out


_EMPTY_RESPONSE: dict = {
    "url": "",
    "status": 0,
    "status_text": "",
    "ok": False,
    "headers": {},
    "mime": "",
}


@dataclass(frozen=True)
class HandoffInfo:
    """Returned by ``Page.detach()`` / ``Session.detach()``. Carries
    the URLs an operator (or a calling script) needs to find the
    handed-off session again.

    Fields:
      session_id     -- stable opaque id (also visible in admin UI)
      novnc_url      -- hub-proxied noVNC viewer (relative path)
      refresh_url    -- POST endpoint to flush new assets / links
                        from the live session into the parent job
                        (None if the session has no parent_job_id)
      end_url        -- DELETE endpoint to close the session manually
      idle_ttl_s     -- effective idle TTL after keepalive
      absolute_ttl_s -- effective absolute TTL after keepalive
    """
    session_id: str
    novnc_url: str
    refresh_url: Optional[str]
    end_url: str
    idle_ttl_s: int
    absolute_ttl_s: int


@dataclass(frozen=True)
class Candidate:
    """One LLM-proposed action returned by :meth:`Page.observe`.

    A Candidate is a *suggested* interaction with the page -- the LLM
    has looked at the outline and picked an element that matches the
    operator's intent, but has NOT executed anything. The script
    decides whether to act on the suggestion::

        cands = await page.observe("ログインボタンを探す")
        if cands:
            await page.click(cands[0])     # accepts a Candidate directly

    The dataclass is paprika-native:

      selector     stable CSS selector usable by ``page.click`` /
                   ``page.fill`` directly. For elements drawn from the
                   outline this is ``[data-paprika-id="N"]`` where N
                   matches the ``[@N]`` marker in ``page.outline()``.
      description  short human-readable label the LLM assigned (used
                   for logging / debug; not parsed further).
      method       proposed interaction kind ("click" / "fill" /
                   "press" / "type" / "hover" / "select_option" /
                   None). Advisory; the script is free to use any
                   action on the selector.
      arguments    proposed argument list when ``method`` needs one
                   (e.g. the value to fill, the key to press). May
                   contain ``${name}`` placeholders if the original
                   observe() call passed variables=; those resolve
                   on the worker just before CDP execution.
      paprika_id   the ``[@N]`` ordinal from the outline when the
                   selector points there; None for free-form CSS.
                   Used internally for invariance across re-renders.
      confidence   0-1, LLM self-reported. Treat as a hint; ``observe``
                   already filters out low-confidence picks.
    """
    selector: str
    description: str
    method: Optional[str] = None
    arguments: Optional[list] = None
    paprika_id: Optional[int] = None
    confidence: Optional[float] = None


# Module-internal: log-side helper for masking ${name} placeholders so
# we never accidentally echo secrets when an operator passes variables=.
# The worker does the real substitution at the CDP edge; this is purely
# defensive logging.
import re as _re
_VAR_PATTERN = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
def _mask_variables(text: str) -> str:
    if not text or "${" not in text:
        return text
    return _VAR_PATTERN.sub(r"${\1}", text)   # canonicalise; values never substituted client-side


class PaprikaActionError(Exception):
    """Raised when a browser_ops action returned a non-OK status string
    (e.g. ``NO_MATCH``, ``ERR: …``). The hub itself returned 200 -- this
    means the page-level action failed, not that the HTTP call did."""

    def __init__(self, status: str, *, elapsed_ms: int = 0):
        super().__init__(status)
        self.status = status
        self.elapsed_ms = elapsed_ms


def _check(reply: dict) -> dict:
    """Translate an action JSON reply into either a return value or a
    ``PaprikaActionError``. Treats ``"OK"`` as success; anything starting
    with ``"NO_MATCH"`` or ``"ERR:"`` raises."""
    status = (reply or {}).get("status") or ""
    if status == "OK":
        return reply
    raise PaprikaActionError(
        status,
        elapsed_ms=int((reply or {}).get("elapsed_ms") or 0),
    )


# ---------------------------------------------------------------------------
# Action logging
# ---------------------------------------------------------------------------
#
# Every Page action (click / fill / press / scroll / goto / capture / agent
# / download_video / ...) emits a one-line "what am I doing right now"
# message before the underlying HTTP call, plus a follow-up
# OK / NO_MATCH / ERR line with the elapsed time after it returns. The
# paprika-runner forwards both stdout and stderr to /jobs/<id>/log, so
# the Live Log panel finally shows the step-by-step progress of a
# Simple macro or code-mode script instead of going dark between
# captures.
#
# Stream conventions:
#   * stdout for the call line + OK result -- these are informational
#     traces of normal program execution, not errors. Tagged [stdout]
#     in the Live Log so they sit alongside the script's own print()s.
#   * stderr for NO_MATCH / ERR: / unexpected exception results --
#     these are diagnostic and operators commonly grep for [stderr]
#     when triaging a failed run.
#
# Read-only inspection methods (state / title / outline / get_state /
# set_state) intentionally STAY un-logged: the agent loop calls
# outline() many times per turn and flooding Live Log with read noise
# defeats the purpose.
#
# Toggle off with PAPRIKA_CLIENT_ACTION_LOG=0 if a particular script
# wants quiet output.

_ACTION_LOG_ENV = "PAPRIKA_CLIENT_ACTION_LOG"


def _action_log_enabled() -> bool:
    val = os.environ.get(_ACTION_LOG_ENV, "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _short_repr(x: Any, max_len: int = 100) -> str:
    """Compact repr for log lines. Long strings / dicts get a ``…`` tail
    so a fill() with a 5 KB blob doesn't blow up the log line."""
    try:
        s = repr(x)
    except Exception:
        s = f"<unreprable {type(x).__name__}>"
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


def _format_call(name: str, args: tuple, kwargs: dict) -> str:
    parts = [_short_repr(a) for a in args]
    parts.extend(f"{k}={_short_repr(v)}" for k, v in kwargs.items())
    return f"page.{name}({', '.join(parts)})"


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


# Phase 2b: methods whose successful calls get appended to the
# Page-level action trace AND emitted as __PAPRIKA_ACTION__ sentinels
# on stdout. Pick state-mutating primitives only -- queries (state,
# outline, screenshot) and LLM-driven helpers (agent, observe,
# download_video) are deliberately excluded:
#   * Queries: nothing to replay.
#   * LLM helpers: their internal steps are non-deterministic and
#     don't fit HostRecipe's "list of clicks" execution model.
_TRACE_KINDS = frozenset({
    "goto",
    "click",
    "fill",
    "press",
    "type",
    "scroll",
    "hover",
    "select_option",
    "wait_for_selector",
})


def _trace_arg(x: Any) -> Any:
    """Coerce a Page-method argument to a JSON-safe form for the action
    trace. Locator / Candidate objects collapse to their ``selector``
    string with a kind tag; anything exotic falls back to a truncated
    repr so the sentinel line never grows unbounded."""
    if isinstance(x, (str, int, float, bool)) or x is None:
        # Truncate long strings so a fill() with a giant blob doesn't
        # blow up the sentinel line (which has to survive being a
        # single stdout line read by the hub).
        if isinstance(x, str) and len(x) > 500:
            return x[:497] + "..."
        return x
    sel = getattr(x, "selector", None)
    if isinstance(sel, str):
        # Candidate / Locator / future selector-bearing objects: keep
        # just the selector + the class tag so the recipe replayer can
        # reconstruct a plain CSS click.
        return {"_kind": type(x).__name__, "selector": sel}
    if isinstance(x, (list, tuple)):
        return [_trace_arg(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _trace_arg(v) for k, v in x.items()}
    s = repr(x)
    return s if len(s) <= 200 else s[:197] + "..."


def _action_log(method: F) -> F:
    """Decorator: prefix-and-suffix log around an async Page action.

    Emits two lines per call::

        [paprika] page.click('.btn-primary')
        [paprika]   -> OK (45ms)

    On failure the suffix carries the action-error status::

        [paprika]   -> NO_MATCH (1500ms)

    Original method's return value, side-effects (e.g. ``goto`` updating
    ``self._url``), and exceptions all pass through unchanged.
    """

    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        if not _action_log_enabled():
            return await method(self, *args, **kwargs)
        sig = _format_call(method.__name__, args, kwargs)
        # Action logs are informational (the script's "I'm clicking X
        # now" trace), not errors -- write to stdout so the Live Log
        # panel tags them [stdout] like the script's own print()s.
        # Reserve stderr for the action failure path below: NO_MATCH /
        # ERR / unexpected exceptions are diagnostic and operators
        # filter for them by stream.
        print(f"[paprika] {sig}", file=sys.stdout, flush=True)
        t0 = time.monotonic()
        try:
            result = await method(self, *args, **kwargs)
        except PaprikaActionError as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            print(
                f"[paprika]   -> {e.status} ({elapsed}ms)",
                file=sys.stderr,
                flush=True,
            )
            raise
        except Exception as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            print(
                f"[paprika]   -> {type(e).__name__}: {e} ({elapsed}ms)",
                file=sys.stderr,
                flush=True,
            )
            raise
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"[paprika]   -> OK ({elapsed}ms)", file=sys.stdout, flush=True)
        # Phase 2b: structured trace. Append to the Page's trace list
        # AND emit a sentinel line so the hub-side codegen orchestrator
        # can collect actions across the sandbox boundary into
        # attempts/{n}/actions.json. Wrapped in try/except so a trace-
        # serialisation failure NEVER breaks the actual action.
        if method.__name__ in _TRACE_KINDS:
            try:
                entry = {
                    "kind": method.__name__,
                    "args": [_trace_arg(a) for a in args],
                    "kwargs": {k: _trace_arg(v) for k, v in kwargs.items()},
                    "elapsed_ms": elapsed,
                    "ok": True,
                }
                try:
                    self._action_trace.append(entry)
                except Exception:
                    pass
                # ensure_ascii=False keeps Japanese selectors readable
                # in the live log; default=str catches any oddball type
                # that slipped past _trace_arg (Path / datetime / ...).
                print(
                    "__PAPRIKA_ACTION__:" + json.dumps(
                        entry, ensure_ascii=False, default=str
                    ),
                    file=sys.stdout,
                    flush=True,
                )
            except Exception:
                pass
        # If the action returned a dict carrying a ``steps`` list (e.g.
        # page.agent() per-iteration trace), surface those as
        # continuation lines so the operator-facing log shows what the
        # agent actually did inside the localised observe/act loop --
        # previously these lived only in the worker's stderr (the
        # _slog channel) and never reached the hub's job log.
        if isinstance(result, dict):
            steps = result.get("steps")
            if isinstance(steps, list) and steps:
                for s in steps:
                    if not isinstance(s, dict):
                        continue
                    n = s.get("n")
                    engine = s.get("engine") or "?"
                    kind = s.get("kind") or "?"
                    outcome = s.get("outcome")
                    head = f"[paprika]      step {n} ({engine}) {kind}"
                    if outcome:
                        out = str(outcome).replace("\n", " ").strip()
                        if len(out) > 240:
                            out = out[:237] + "..."
                        head += f" -> {out}"
                    print(head, file=sys.stdout, flush=True)
        return result

    return wrapper  # type: ignore[return-value]


class Page:
    """One open paprika session, presented as a Playwright-shape Page.

    Most methods mirror Playwright's ``Page`` so existing
    browser-automation code reads naturally. Where paprika has a
    primitive Playwright doesn't (``outline``, ``visited_urls``,
    ``capture``), the method has a paprika-only name -- there are no
    Playwright methods that overlap in shape.

    Created by :meth:`PaprikaClient.open_session` or
    :meth:`PaprikaClient.session`; do NOT instantiate directly.
    """

    def __init__(
        self,
        client: "PaprikaClient",
        info: dict,
        *,
        page_id: str = "p_default",
    ) -> None:
        self._client = client
        self._info = info
        self._sid: str = info["session_id"]
        # Which tab in the session this Page represents. The instance
        # returned by ``cli.session(...)`` always wraps the default
        # tab (``"p_default"``). Additional tabs opened via
        # ``page.new_page(url)`` get fresh ids (``"p_<8hex>"``).
        # Phase 2b: every per-tab primitive forwards this id to the
        # worker via ``_pid_json`` (for POST bodies) / ``_pid_params``
        # (for GET query strings), so ``page_a.goto(...)`` and
        # ``page_b.goto(...)`` target their respective tabs in
        # parallel rather than both landing on the session default.
        self._page_id: str = page_id
        self._closed = False
        # Detached pages are operator-managed (= ctx manager exit will
        # NOT call close() on them). Set by detach(), checked by the
        # `cli.session()` ctx manager's __aexit__ AND by close() itself
        # so a stray close() call doesn't accidentally end a session
        # the operator was supposed to keep poking at via noVNC.
        self._detached = False
        # Back-reference to the owning Session, if any. Set by
        # Session.open() / Session.refresh() so a per-tab switch()
        # call can update the parent's front_idx tracking client-side.
        # ``None`` for stand-alone Page wrappers (uncommon).
        self._session: Optional["Session"] = None
        # Cached value updated by state() / goto() / click() so users
        # can read page.url without an extra round trip.
        self._url: str = info.get("initial_url") or ""
        # Phase 2b: per-Page action trace. Populated by the _action_log
        # decorator for every mutating Page method (see _TRACE_KINDS).
        # Mirrored to stdout as __PAPRIKA_ACTION__ sentinels for the
        # sandbox->hub crossing. Operator scripts can read this
        # directly via ``page.action_trace`` to e.g. dump it as a
        # bespoke recipe even when running outside codegen-loop.
        self._action_trace: list[dict] = []

    # -- internal: per-tab routing helpers ----------------------------------

    def _pid_json(self, body: Optional[dict] = None) -> dict:
        """Build the POST body for a per-tab session-action endpoint,
        with ``page_id`` injected so the hub routes the action to
        THIS Page's tab on the worker (rather than the session's
        default tab)."""
        out = dict(body or {})
        out["page_id"] = self._page_id
        return out

    def _pid_params(self) -> dict[str, str]:
        """Build the query-string params dict for a per-tab GET
        session-action endpoint (state, screenshot, outline, ...)."""
        return {"page_id": self._page_id}

    # -- inspection (sync properties, no I/O) -------------------------------

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def worker_id(self) -> str:
        return self._info.get("worker_id") or ""

    @property
    def lane_idx(self) -> Optional[int]:
        return self._info.get("lane_idx")

    @property
    def novnc_url(self) -> Optional[str]:
        return self._info.get("novnc_url_autoconnect") or self._info.get("novnc_url")

    @property
    def url(self) -> str:
        """Last known URL. Updated by goto / click / state. Call
        :meth:`state` for a fresh value."""
        return self._url

    @property
    def action_trace(self) -> list[dict]:
        """Return a copy of the in-script action trace (Phase 2b).

        Each entry is ``{"kind", "args", "kwargs", "elapsed_ms", "ok"}``
        and corresponds to one successful mutating Page method call.
        See ``_TRACE_KINDS`` for which methods qualify. Returns a copy
        so the caller can mutate the list without affecting subsequent
        appends.
        """
        return list(self._action_trace)

    # -- navigation ---------------------------------------------------------

    @_action_log
    async def goto(self, url: str) -> dict:
        """Playwright-style ``page.goto(url)``.

        Returns the action reply with a Playwright-compatible HTTP
        ``response`` object embedded::

            reply = await page.goto("https://example.com/missing")
            # reply["status"]             == "OK"       # CDP nav succeeded
            # reply["elapsed_ms"]         == 1234
            # reply["result"]["response"] == {
            #     "url":         "https://example.com/missing",  # final URL
            #     "status":      404,                            # HTTP code
            #     "status_text": "Not Found",
            #     "ok":          False,                          # 200-299
            #     "headers":     {"content-type": "text/html", ...},
            #     "mime":        "text/html",
            # }
            if not response_of(reply)["ok"]:
                raise RuntimeError("page missing")

        ``reply["result"]["response"]`` is ``None`` when the navigation
        produced no Document-type response we could correlate (cached
        page, redirect-to-non-HTTP target, response arrived after the 5s
        capture window, ...). Use :func:`response_of` for safe lookup.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/navigate",
            json=self._pid_json({"url": url}),
        )
        _check(reply)
        self._url = url
        return reply

    @_action_log
    async def back(self) -> dict:
        """``window.history.back()`` -- equivalent to the browser's Back
        button. Updates :attr:`url` on success.

        Returns the same reply shape as :meth:`goto` -- including
        ``result["response"]`` with the HTTP response info for the
        page navigated back to. Use :func:`response_of` to access it
        safely.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/back",
            json=self._pid_json(),
        )
        _check(reply)
        # Refresh cached URL.
        try:
            s = await self.state()
            self._url = s.get("url", "") or self._url
        except Exception:
            pass
        return reply

    async def go_back(self) -> dict:
        """Playwright alias for :meth:`back`."""
        return await self.back()

    @_action_log
    async def forward(self) -> dict:
        """``window.history.forward()`` -- equivalent to the browser's
        Forward button. Symmetric counterpart to :meth:`back`. No-op
        when there's no forward entry (returns OK).

        Reply shape matches :meth:`goto` -- ``result["response"]`` carries
        the HTTP status of the page navigated forward to. Access via
        :func:`response_of`.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/forward",
            json=self._pid_json(),
        )
        _check(reply)
        try:
            s = await self.state()
            self._url = s.get("url", "") or self._url
        except Exception:
            pass
        return reply

    async def go_forward(self) -> dict:
        """Playwright alias for :meth:`forward`."""
        return await self.forward()

    @_action_log
    async def history_first(self) -> dict:
        """Jump back to history entry 0 -- the first page opened in
        this session (typically the ``initial_url`` of
        :meth:`PaprikaClient.session`). Useful for "start over" macros
        that navigate through several pages and want to come back to
        the entry point without remembering the URL.

        Implemented via CDP ``Page.navigateToHistoryEntry``. No-op
        when already at index 0.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/history_first",
            json=self._pid_json(),
        )
        _check(reply)
        try:
            s = await self.state()
            self._url = s.get("url", "") or self._url
        except Exception:
            pass
        return reply

    async def reload(self) -> dict:
        return await self.goto(await self._fresh_url())

    async def _fresh_url(self) -> str:
        """Resolve the URL to navigate to when ``reload()`` is called.

        Prefers the tab's live URL (``state()``) so the reload reflects
        whatever URL the operator may have navigated to via noVNC.
        Defense in depth: if the live URL has an ``about:`` scheme but
        we have a real cached URL (e.g. a new tab whose navigation
        hadn't committed at the moment of state()), fall back to the
        cached one. Without this guard a too-soon reload after
        ``sess.open(...)`` could pin the tab to about:blank, which is
        what happened on job a26e4651d538 (operator clicked yt-dlp on
        the session and got ``Unsupported url scheme: "about"``).
        """
        s = await self.state()
        live = s.get("url") or ""
        if (
            live.startswith("about:")
            and self._url
            and not self._url.startswith("about:")
        ):
            return self._url
        return live or self._url

    # -- actions ------------------------------------------------------------

    @_action_log
    async def click(self, selector) -> dict:
        """Click an element. Accepts a CSS selector OR a
        :class:`Candidate` returned from :meth:`observe` (its
        ``selector`` field is used)."""
        sel = selector.selector if isinstance(selector, Candidate) else selector
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/click",
            json=self._pid_json({"selector": sel}),
        )
        _check(reply)
        return reply

    @_action_log
    async def fill(
        self,
        selector,
        value: str,
        *,
        variables: Optional[dict] = None,
    ) -> dict:
        """Set the value of an ``<input>`` / ``<textarea>`` and fire
        ``input``/``change`` events. Equivalent to Playwright's
        ``page.fill``.

        ``selector`` accepts a CSS string OR a :class:`Candidate`
        from :meth:`observe`. When ``value`` contains ``${name}``
        placeholders and ``variables`` is provided, the worker
        substitutes them at the CDP edge -- the real value never
        appears in this SDK's logs and (if the call originated from
        an LLM loop) never appears in any prompt either."""
        sel = selector.selector if isinstance(selector, Candidate) else selector
        body = {"selector": sel, "value": value}
        if variables:
            body["variables"] = dict(variables)
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/fill",
            json=self._pid_json(body),
        )
        _check(reply)
        return reply

    @_action_log
    async def press(
        self,
        key: str,
        *,
        count: int = 1,
        modifiers: Optional[list] = None,
    ) -> dict:
        """Press a key (or key combo).

        Examples::

            await page.press("Enter")
            await page.press("Backspace", count=3)        # rapid delete
            await page.press("Ctrl+A")                    # combo string
            await page.press("a", modifiers=["Ctrl"])     # equivalent
            await page.press("Ctrl+Shift+T")              # multiple mods
            await page.press("ArrowDown", count=5)        # scroll a list

        ``key`` is either a W3C key name (``"Enter"``, ``"Backspace"``,
        ``"ArrowDown"``, ``"F5"`` ...) or a combo string with one or
        more modifiers joined by ``+`` (case-insensitive).

        ``modifiers`` accepts a list of modifier names:
        ``Ctrl`` / ``Shift`` / ``Alt`` / ``Meta`` and their common
        aliases (``Cmd`` / ``Command`` / ``Option`` / ``Win``).
        Combined (OR'd) with anything parsed from the combo string.

        ``count`` repeats the press N times with a short inter-press
        delay (~50ms). Cap is 100 server-side.
        """
        body: dict[str, Any] = {"key": key}
        if count != 1:
            body["count"] = int(count)
        if modifiers:
            body["modifiers"] = list(modifiers)
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/press",
            json=self._pid_json(body),
        )
        _check(reply)
        return reply

    @_action_log
    async def type(self, text: str, *, variables: Optional[dict] = None) -> dict:
        """Insert ``text`` into whatever element is currently focused.

        Uses CDP Input.insertText: one shot, fires the same
        ``input`` / ``change`` events real typing would, works for
        ``<input>`` / ``<textarea>`` / contenteditable and most
        canvas-based editors.

        Does NOT change focus -- click / locator.click() the target
        first if needed. For "fill an input by selector" use
        ``page.fill(selector, value)`` instead (it handles focus + value
        + events as a single primitive)."""
        if not text:
            raise ValueError("text is empty")
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/type",
            json=self._pid_json({"text": str(text)}),
        )
        _check(reply)
        return reply

    @_action_log
    async def scroll(
        self,
        direction: str = "down",
        pixels: int = 800,
    ) -> dict:
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/scroll",
            json=self._pid_json({"direction": direction, "pixels": pixels}),
        )
        _check(reply)
        return reply

    @_action_log
    async def wait_for(
        self,
        *,
        seconds: Optional[float] = None,
        ms: Optional[int] = None,
    ) -> None:
        """Sleep for the requested duration.

        Convenience wrapper around ``asyncio.sleep`` so LLM-generated
        scripts that reach for ``await page.wait_for(seconds=3)`` (the
        Playwright-flavoured name they'd expect) don't crash with
        AttributeError. Both ``seconds=`` and ``ms=`` work; if both
        are given, ``seconds`` wins.

        Pure client-side -- no HTTP round-trip.

        For "wait for element to appear" semantics use a poll loop
        on ``page.outline()`` or ``page.click(selector)`` directly
        (the worker retries within bops.click for a short window).
        """
        delay: float = 0.0
        if seconds is not None:
            delay = float(seconds)
        elif ms is not None:
            delay = float(ms) / 1000.0
        if delay > 0:
            await asyncio.sleep(delay)

    @_action_log
    async def capture(self, label: str = "capture", *, step: int = 0) -> dict:
        """Persist HTML + screenshot + outline of the current page under
        the session's assets dir. Returns the snapshot metadata."""
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/capture",
            json=self._pid_json({"label": label, "step": step}),
        )
        _check(reply)
        return reply.get("result") or {}

    def _require_job_id(self, what: str) -> str:
        """Resolve this session's parent job id or raise a helpful error.
        Shared by the asset helpers, which all need a job dir to read."""
        job_id = self._info.get("job_id")
        if not job_id:
            raise PaprikaActionError(
                f"{what} needs a job-bound session (this session has no "
                "job_id). Open it with cli.session(..., "
                "parent_job_id='<id>') or run under paprika-runner "
                "(PAPRIKA_JOB_ID) so passively-captured assets have a "
                "job dir to land in."
            )
        return job_id

    @_action_log
    async def assets(
        self,
        *,
        kind: Optional[str] = "image",
        absolute: bool = True,
        refresh: bool = True,
        details: bool = False,
    ):
        """Return the assets captured during this session.

        paprika passively records every resource the page loads (images,
        video, …) — the same machinery Fetch mode uses. This flushes
        anything still buffered on the worker (``refresh=True``) and then
        lists the parent job's captured assets. The Playwright analog is
        wiring up ``page.on("response")`` yourself; here it's one call.

        Args:
          kind:     Filter by asset kind: ``"image"`` (default),
                    ``"video"``, ``"audio"``, ``"other"`` — or ``None``
                    for every kind.
          absolute: ``True`` (default) returns ready-to-GET URLs
                    (``<base>/jobs/<id>/assets/<name>``); ``False``
                    returns the hub-relative ``href``.
          refresh:  Flush newly-captured assets off the worker before
                    listing. Leave it ``True`` so the result reflects
                    images revealed by the latest clicks / scrolls.
          details:  ``False`` (default) -> ``list[str]`` of URLs;
                    ``True`` -> ``list[dict]`` with the full metadata
                    (``name`` / ``url`` / ``size`` / ``source_url`` /
                    ``page_url`` / ``mime`` / ``kind`` / ``ext``).

        Requires a job-bound session (same constraint as
        :meth:`get_state`): the passive capture needs a job dir to land
        in. ``cli.session(...)`` callers pass ``parent_job_id=...``;
        codegen-loop / rerun / watch-live sessions are bound for you.

        Example::

            async with cli.session(
                "https://example.com/article",
                parent_job_id="my-crawl",
            ) as page:
                await page.scroll()          # trigger lazy images
                srcs = await page.assets()   # -> [url, url, ...]
        """
        items = await self._asset_items(kind=kind, refresh=refresh)
        prefix = self._client.base_url if absolute else ""
        if details:
            out = []
            for it in items:
                row = dict(it)
                row["url"] = prefix + it["href"]
                out.append(row)
            return out
        return [prefix + it["href"] for it in items]

    @_action_log
    async def save_assets(
        self,
        dest_dir: str,
        *,
        kind: Optional[str] = "image",
        refresh: bool = True,
    ) -> list[str]:
        """Download this session's captured assets to ``dest_dir`` and
        return the list of written file paths.

        Thin wrapper over :meth:`assets`: lists the matching assets
        (flushing first when ``refresh``), then GETs each one and writes
        it under ``dest_dir`` using the asset's stored filename.

        Example::

            paths = await page.save_assets("out/images")
            print(len(paths), "files saved")
        """
        items = await self._asset_items(kind=kind, refresh=refresh)
        os.makedirs(dest_dir, exist_ok=True)
        paths: list[str] = []
        for it in items:
            blob = await self._client._bytes("GET", it["href"])
            dest = os.path.join(dest_dir, it["name"])
            with open(dest, "wb") as f:
                f.write(blob)
            paths.append(dest)
        return paths

    async def _asset_items(
        self, *, kind: Optional[str], refresh: bool,
    ) -> list[dict]:
        """Flush (optional) + fetch the parent job's assets.json, filtered
        by ``kind``. Shared by :meth:`assets` / :meth:`save_assets` so a
        download does exactly one refresh, not one per call."""
        job_id = self._require_job_id("page.assets()")
        if refresh:
            # Flush the worker's capture buffer into the job dir. Best-
            # effort: a handed-off / dead session can 404 here, but
            # assets already on disk are still listed below.
            try:
                await self._client._json("POST", f"/jobs/{job_id}/refresh")
            except Exception:
                pass
        data = await self._client._json("GET", f"/jobs/{job_id}/assets.json")
        return [
            it for it in (data.get("items") or [])
            if kind is None or it.get("kind") == kind
        ]

    @_action_log
    async def download_video(
        self,
        url: Optional[str] = None,
        *,
        referer: Optional[str] = None,
        timeout_s: int = 1800,
    ) -> dict:
        """Run yt-dlp against ``url`` (or the current page if omitted)
        and upload the resulting video file(s) to the parent job's
        /assets endpoint.

        Returns a dict::

            {
              "ok":         bool,        # yt-dlp succeeded
              "url":        str,         # the URL that was attempted
              "message":    str,         # yt-dlp's last log line / error tail
              "files":      list[str],   # saved FILENAMES (basenames). NOT
                                         # dicts -- each element is a plain
                                         # str like "clip.mp4". Combine with
                                         # the parent job_id to get the URL,
                                         # percent-encoding the name (titles
                                         # often contain ``#`` / spaces, which
                                         # a bare URL silently 404s on):
                                         #   from urllib.parse import quote
                                         #   f"/jobs/{job_id}/assets/{quote(name, safe='')}"
              "file_count": int,         # len(files); 0 = nothing extracted
            }

        Use this for streaming-video sites where the passive CDP
        listener only catches segment fragments (.ts / .m4s):
        yt-dlp will combine them into a single playable .mp4 and
        ship it to the gallery alongside the rest of the assets.

        Args:
          url: The video page URL. Defaults to the current page.
            yt-dlp recognises most video sites (YouTube, Twitch,
            Vimeo, Dailymotion, etc.) -- pass the page URL, not the
            internal stream URL.
          referer: Optional ``--referer`` to pass to yt-dlp. Some
            sites validate the referer header on stream requests.
          timeout_s: Max time for the yt-dlp subprocess. Default
            1800 (30 min). Capped at 864000 (10 days) server-side.

        Implementation note: this call overrides the underlying
        httpx client's per-request timeout to ``timeout_s + 120``
        so a long yt-dlp run doesn't blow up on the PaprikaClient's
        default 60s read timeout. Without this override, every
        download taking > 60s surfaced as
        ``PaprikaError: ... transport error:`` even though the
        worker was still happily downloading. (Bug discovered on
        job b3fe8743b99f attempt 1.)
        """
        body: dict = {"timeout_s": int(timeout_s)}
        if url:
            body["url"] = url
        if referer:
            body["referer"] = referer
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/download_video",
            json=self._pid_json(body),
            timeout=float(timeout_s) + 120.0,
        )
        _check(reply)
        return reply.get("result") or {}

    @_action_log
    async def solve_cloudflare(
        self, *, timeout_s: float = 25.0, click_checkbox: bool = True,
    ) -> dict:
        """Get past a Cloudflare "Just a moment..." challenge on the
        current page.

        Call this right after ``goto()`` on a Cloudflare-protected
        site. Two phases run server-side:

          1. **Wait**: nodriver (the worker's undetected Chrome)
             auto-passes the common *managed* challenge within a few
             seconds of executing the challenge JS. Polls the page
             title until the challenge marker disappears.
          2. **Checkbox click** (when ``click_checkbox=True``, the
             default): if the wait times out the challenge probably
             wants an explicit Turnstile checkbox click. The worker
             uses nodriver's ``verify_cf()`` (opencv template match
             on a screenshot -> coordinate click) since the widget
             lives in a cross-origin iframe unreachable via the DOM.
             Then re-polls for clearance.

        Returns::

            {
              "cleared":          bool,   # challenge gone, content loaded
              "title":            str,    # page title at the end
              "waited_s":         float,  # total time spent
              "clicked_checkbox": bool,   # whether verify_cf() was tried
            }

        ``cleared == False`` means the site is still gated even after
        the checkbox attempt. Recovery: open the session via noVNC
        and click the checkbox manually; the resulting
        ``cf_clearance`` cookie auto-saves to ``/hosts/{host}`` and
        is reused on later sessions (the worker fleet shares an
        egress IP + Chrome UA, so a clearance solved once validates
        fleet-wide).

        Pass ``click_checkbox=False`` to do the passive wait only
        (e.g. when you know the site uses a non-interactive JS
        challenge and want to avoid any chance of a stray click).

        Typical usage::

            await page.goto("https://www.javlibrary.com/ja/")
            r = await page.solve_cloudflare(timeout_s=20)
            if r["cleared"]:
                await page.capture("after-cf")
            else:
                print("still gated; hand to operator via noVNC")
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/solve_cloudflare",
            json=self._pid_json({
                "timeout_s": float(timeout_s),
                "click_checkbox": bool(click_checkbox),
            }),
            timeout=float(timeout_s) + 40.0,
        )
        _check(reply)
        return reply.get("result") or {}

    async def get_state(self, key: str = "default") -> Optional[dict]:
        """Read persistent state stored under this session's parent
        job. Returns the previously-set ``data`` (any JSON value), or
        None if nothing was stored under ``key`` yet.

        Useful for resuming long crawls across attempts: write progress
        in :meth:`set_state` on every iteration; read it back on the
        first iteration of a future attempt to pick up where the last
        one left off.

        Requires the session to be bound to a parent job (codegen-loop
        / rerun jobs do this automatically; raw ``cli.session(...)``
        callers need to pass ``parent_job_id=...``).
        """
        try:
            reply = await self._client._json(
                "GET", f"/sessions/{self._sid}/state/{key}",
            )
        except Exception as e:
            # Treat 404 ("no state stored") as "first time" -- normal.
            msg = str(e)
            if "404" in msg or "no state stored" in msg:
                return None
            raise
        return reply.get("data")

    async def set_state(self, key: str = "default", value=None) -> None:
        """Persist ``value`` under ``key`` for this session's parent
        job. ``value`` must be JSON-serialisable (dict / list / str /
        number / bool / None). Future sessions opened with the same
        parent_job_id (across attempts, rerun, watch-live) read the
        same state via :meth:`get_state`."""
        await self._client._json(
            "PUT", f"/sessions/{self._sid}/state/{key}",
            json={"data": value},
        )

    # -- cookies / network --------------------------------------------------

    @_action_log
    async def cookies(
        self,
        *,
        host: Optional[str] = None,
        all_cookies: bool = False,
    ) -> dict:
        """Dump the cookies the browser currently holds for this session.

        Returns ``{current_url, host_filter, total_in_browser, count,
        cookies}`` where ``cookies`` is CDP-shaped (name / value / domain
        / path / expires / secure / httpOnly / sameSite).

        Args:
          host:        Filter to cookies matching this host. Omit to
                       infer the host from the tab's current URL.
          all_cookies: ``True`` returns the entire jar (cross-site /
                       third-party included), bypassing the host filter
                       — handy for SSO flows whose cookies live on a
                       different domain.
        """
        params = self._pid_params()
        if host:
            params["host"] = host
        if all_cookies:
            params["all_cookies"] = "true"
        return await self._client._json(
            "GET", f"/sessions/{self._sid}/cookies", params=params,
        )

    @_action_log
    async def save_cookies_to_host(
        self,
        *,
        host: Optional[str] = None,
        notes: Optional[str] = None,
        all_cookies: bool = False,
    ) -> dict:
        """Promote the session's current cookies into the Host registry so
        future fetches / sessions for that host start logged-in.

        Returns the saved host record plus ``{saved_count,
        total_in_browser, current_url, filtered}``.

        Args:
          host:        Host to register under. Omit to infer from the
                       tab's current URL.
          notes:       Free-text note stored on the host record.
          all_cookies: ``True`` saves every cookie in the jar (cross-site
                       included); default saves only cookies matching the
                       resolved host.
        """
        body: dict[str, Any] = {"all_cookies": bool(all_cookies)}
        if host:
            body["host"] = host
        if notes:
            body["notes"] = notes
        return await self._client._json(
            "POST", f"/sessions/{self._sid}/save_cookies_to_host", json=body,
        )

    @_action_log
    async def last_response(self) -> dict:
        """Return the most recent main-document HTTP response observed
        on this session.

        A passive listener on the worker tracks every top-level
        navigation -- whether triggered by ``page.goto`` / ``back`` /
        ``forward`` / ``reload`` / ``history_first`` OR by a click
        that incidentally navigated (link, form submit, JS
        ``location.href = ...``). The most recent one is always
        available here::

            page.locator("a.title-link").first.click()
            r = await page.last_response()
            if r["status"] == 404:
                return None
            if not r["ok"]:
                raise RuntimeError(f"HTTP {r['status']}")

        Always returns the standard response dict (see :func:`response_of`).
        Fields are filled with defaults (``status: 0``, ``ok: False``,
        empty headers) when no document response has been observed yet
        on this session.

        Use this for click-induced navigations -- ``page.goto()`` and
        the explicit nav methods already return the response inline as
        ``reply["result"]["response"]``.
        """
        out = await self._client._json(
            "GET", f"/sessions/{self._sid}/last_response",
        )
        # Bake out into the response_of() shape directly so the caller
        # doesn't need an extra unwrap step.
        resp = (out or {}).get("response")
        result = _EMPTY_RESPONSE.copy()
        if isinstance(resp, dict):
            result.update(resp)
        return result

    @_action_log
    async def network(self, *, since: float = 0.0) -> dict:
        """Media network traffic observed in this session (images / audio
        / video responses the browser loaded).

        Returns ``{session_id, count, entries}`` where each entry is
        ``{url, mime, size, saved, document_url, timestamp}``.

        ``since`` (UNIX timestamp) enables incremental polling: only
        entries newer than ``since`` are returned. Pass 0 for everything.
        """
        return await self._client._json(
            "GET", f"/sessions/{self._sid}/network",
            params={"since": since},
        )

    # -- JS evaluation + DOM getters / inputs -------------------------------
    #
    # evaluate() is the keystone: the hub forwards the expression to the
    # worker's tab.evaluate(). Everything below (text_content, get_attribute,
    # wait_for_selector, hover, select_option, …) is built on top of it as
    # client-side JS, so the whole Playwright-shaped DOM surface needs only
    # the single /evaluate endpoint on the server.

    @_action_log
    async def evaluate(self, expression: str, *, await_promise: bool = False):
        """Evaluate a JS ``expression`` in the page context and return its
        value (must be JSON-serialisable — strings / numbers / bools /
        null / arrays / objects; DOM nodes and functions can't cross the
        wire).

        Mirrors Playwright's ``page.evaluate``. Pass ``await_promise=True``
        to await a Promise the expression resolves to::

            title = await page.evaluate("document.title")
            n = await page.evaluate("document.querySelectorAll('img').length")
            data = await page.evaluate("fetch('/api').then(r=>r.json())",
                                       await_promise=True)
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/evaluate",
            json=self._pid_json({
                "expression": expression,
                "await_promise": bool(await_promise),
            }),
        )
        _check(reply)
        return reply.get("result")

    async def _eval_el(self, selector: str, body: str, *, index: int = 0):
        """Run a JS expression ``body`` (using ``el``) against the
        ``index``-th match of ``selector``. Negative ``index`` counts from
        the end. Returns ``None`` if no such element exists."""
        expr = (
            "(()=>{const els=document.querySelectorAll(%s);"
            "const i=%d;const el=els[i<0?els.length+i:i];"
            "if(!el)return null;return (%s);})()"
            % (json.dumps(selector), int(index), body)
        )
        return await self.evaluate(expr)

    async def wait_for_selector(
        self,
        selector: str,
        *,
        state: str = "visible",
        timeout: float = 30.0,
        poll_interval: float = 0.4,
    ) -> bool:
        """Wait until ``selector`` reaches ``state``. Returns ``True`` once
        satisfied; raises ``PaprikaActionError`` on timeout.

        ``state`` is one of ``"attached"`` (in DOM), ``"detached"`` (gone),
        ``"visible"`` (default), or ``"hidden"``. Implemented as a
        client-side poll over :meth:`evaluate`, so no extra hub endpoint
        is needed.
        """
        if state not in ("attached", "detached", "visible", "hidden"):
            raise ValueError(f"invalid state {state!r}")
        sel = json.dumps(selector)
        check = (
            "(()=>{const el=document.querySelector(%s);"
            "if(!el)return 'detached';"
            "const s=getComputedStyle(el);"
            "const vis=(el.offsetWidth>0||el.offsetHeight>0||"
            "el.getClientRects().length>0)&&s.visibility!=='hidden'&&"
            "s.display!=='none';"
            "return vis?'visible':'hidden';})()" % sel
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            cur = await self.evaluate(check)
            ok = (
                (state == "attached" and cur in ("visible", "hidden"))
                or (state == "detached" and cur == "detached")
                or (state == "visible" and cur == "visible")
                or (state == "hidden" and cur in ("hidden", "detached"))
            )
            if ok:
                return True
            if loop.time() > deadline:
                raise PaprikaActionError(
                    f"wait_for_selector({selector!r}, state={state!r}) "
                    f"timed out after {timeout}s (last: {cur})"
                )
            await asyncio.sleep(poll_interval)

    # -- DOM getters (selector-based; first match unless index given) -------

    async def text_content(self, selector: str, *, index: int = 0):
        return await self._eval_el(selector, "el.textContent", index=index)

    async def inner_text(self, selector: str, *, index: int = 0):
        return await self._eval_el(selector, "el.innerText", index=index)

    async def inner_html(self, selector: str, *, index: int = 0):
        return await self._eval_el(selector, "el.innerHTML", index=index)

    async def input_value(self, selector: str, *, index: int = 0):
        return await self._eval_el(selector, "el.value", index=index)

    async def get_attribute(self, selector: str, name: str, *, index: int = 0):
        return await self._eval_el(
            selector, f"el.getAttribute({json.dumps(name)})", index=index,
        )

    async def count(self, selector: str) -> int:
        n = await self.evaluate(
            f"document.querySelectorAll({json.dumps(selector)}).length"
        )
        return int(n or 0)

    async def is_visible(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.offsetWidth>0||el.offsetHeight>0||el.getClientRects()"
            ".length>0)&&getComputedStyle(el).visibility!=='hidden'",
            index=index,
        ))

    async def is_checked(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(selector, "!!el.checked", index=index))

    async def is_enabled(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(selector, "!el.disabled", index=index))

    async def is_disabled(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(selector, "!!el.disabled", index=index))

    async def is_editable(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector, "!el.disabled&&!el.readOnly", index=index,
        ))

    # -- input devices (JS-dispatched; selector-based) ----------------------
    #
    # These dispatch synthetic DOM events rather than driving real OS
    # input. That covers the overwhelming majority of sites; the rare
    # page that gates on isTrusted events won't react (use page.agent()
    # / noVNC there). set_input_files needs CDP and is not yet wired.

    async def hover(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true})),"
            "el.dispatchEvent(new MouseEvent('mouseenter',{bubbles:true})),"
            "el.dispatchEvent(new MouseEvent('mousemove',{bubbles:true})),true)",
            index=index,
        ))

    async def dblclick(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.dispatchEvent(new MouseEvent('dblclick',"
            "{bubbles:true,cancelable:true})),true)",
            index=index,
        ))

    async def focus(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector, "(el.focus&&el.focus(),true)", index=index,
        ))

    async def scroll_into_view_if_needed(
        self, selector: str, *, index: int = 0,
    ) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.scrollIntoView({block:'center',inline:'center'}),true)",
            index=index,
        ))

    async def select_option(
        self, selector: str, value: str, *, index: int = 0,
    ) -> bool:
        """Select an ``<option>`` in a ``<select>`` by its value (sets
        ``el.value`` and fires ``input`` + ``change``)."""
        return bool(await self._eval_el(
            selector,
            "(el.value=%s,el.dispatchEvent(new Event('input',{bubbles:true})),"
            "el.dispatchEvent(new Event('change',{bubbles:true})),true)"
            % json.dumps(value),
            index=index,
        ))

    async def check(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.checked=true,el.dispatchEvent(new Event('input',"
            "{bubbles:true})),el.dispatchEvent(new Event('change',"
            "{bubbles:true})),true)",
            index=index,
        ))

    async def uncheck(self, selector: str, *, index: int = 0) -> bool:
        return bool(await self._eval_el(
            selector,
            "(el.checked=false,el.dispatchEvent(new Event('input',"
            "{bubbles:true})),el.dispatchEvent(new Event('change',"
            "{bubbles:true})),true)",
            index=index,
        ))

    async def set_input_files(self, selector: str, files) -> dict:
        """Set the file(s) on an ``<input type=file>`` matched by
        ``selector``. ``files`` is a path or a list of paths.

        The bytes are read locally, base64-encoded, and the worker points
        the input at them via CDP ``DOM.setFileInputFiles`` (JS can't set
        a file input). Returns ``{files, count}``::

            await page.set_input_files("input[type=file]", "photo.jpg")
            await page.set_input_files("#docs", ["a.pdf", "b.pdf"])
        """
        if isinstance(files, (str, os.PathLike)):
            files = [files]
        payload = []
        for fp in files:
            with open(fp, "rb") as fh:
                data = fh.read()
            payload.append({
                "name": os.path.basename(str(fp)),
                "content_b64": base64.b64encode(data).decode("ascii"),
            })
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/set_input_files",
            json=self._pid_json({"selector": selector, "files": payload}),
        )
        _check(reply)
        return reply.get("result") or {}

    # -- inspection (paprika extensions) ------------------------------------

    async def state(self) -> dict:
        """``{url, title, lane_idx, visited_count}``. Refreshes
        :attr:`url`."""
        reply = await self._client._json(
            "GET", f"/sessions/{self._sid}/state",
            params=self._pid_params(),
        )
        result = reply.get("result") or {}
        if isinstance(result, dict):
            self._url = result.get("url", "") or self._url
        return result

    async def title(self) -> str:
        return (await self.state()).get("title", "")

    async def outline(self) -> str:
        """Text outline of every visible interactive element on the
        page, each tagged with ``[@N]`` (resolvable as
        ``[data-paprika-id="N"]``). The same data the LLM agent sees."""
        reply = await self._client._json(
            "GET", f"/sessions/{self._sid}/outline",
            params=self._pid_params(),
        )
        return reply.get("result") or ""

    async def visited_urls(self) -> list[str]:
        reply = await self._client._json(
            "GET", f"/sessions/{self._sid}/visited",
            params=self._pid_params(),
        )
        return reply.get("visited_urls") or []

    async def links(self, *, urls_only: bool = False):
        """Return every ``<a href>`` on the current page, resolved to
        absolute URLs.

        Skipped protocols (`javascript:` / `mailto:` / `tel:` / `blob:`
        / `data:` / `about:`) are filtered out at the worker. Duplicates
        are deduped by href. The visible anchor text is trimmed and
        truncated to ~120 chars.

        Args:
          urls_only: when True, return a flat ``list[str]`` of absolute
            URLs -- a one-line convenience for crawl loops::

              for u in await page.links(urls_only=True):
                  ...

            Default False returns the full list of dicts with
            ``{href, text, target, rel}`` so the script can filter on
            anchor text or open-in-new-tab semantics.

        Read-only: safe to call during a running fetch job.
        """
        reply = await self._client._json(
            "GET", f"/sessions/{self._sid}/links",
            params=self._pid_params(),
        )
        items = reply.get("links") or []
        if urls_only:
            return [item.get("href") for item in items if isinstance(item, dict) and item.get("href")]
        return items

    @_action_log
    async def exists(self, selector: str) -> bool:
        """Return True iff ``document.querySelector(selector)`` matches
        at least one element on the current page.

        Cheap, deterministic CSS check -- no LLM. Use this when you
        can express the condition as a CSS selector::

            if await page.exists('input[name="password"]'):
                await page.fill('input[name="password"]', '...')

        For natural-language conditions (e.g. "is there an error
        banner on this page?") see :meth:`ask` instead.

        Read-only: safe to call during a running fetch job.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/exists",
            json=self._pid_json({"selector": selector}),
        )
        _check(reply)
        result = bool(reply.get("result"))
        # Surface the bool in the [paprika] action log so the operator
        # can read the branch decision without printing it themselves.
        print(f"[paprika]   exists -> {result}", flush=True)
        return result

    @_action_log
    async def ask(self, question: str, *, engine: str = "auto") -> bool:
        """Ask a chat LLM a yes/no question about the current page and
        return the parsed bool.

        Sends the page outline (accessibility tree text + current URL)
        as context plus the question, with a strict "answer yes or no"
        prompt. Unparseable / unsure answers fall to ``False`` -- this
        is intentional: the macro should default to the non-acting
        branch when the LLM can't decide.

        Args:
          question: yes/no question. JP / EN both work.
          engine: slug of an entry from the AI Engines admin tab.
            ``"auto"`` (default) uses the promoted chat engine on the
            hub -- typically what the operator wants. Pass a specific
            slug (e.g. ``"chatgpt51"``, ``"qwen-chat"``, ``"claude"``)
            to pin the call to a particular backend. Must resolve to
            an engine with ``protocol="openai"`` (OpenAI-compat chat).

        Examples::

            if await page.ask("ログイン画面が表示されているか?"):
                await page.fill("#user", "myname")
                ...

            # Pin to a specific engine, ignoring the operator default.
            if await page.ask("Has the cookie banner appeared?", engine="chatgpt51"):
                await page.click("button:has-text('Accept')")

        Read-only: safe to call during a running fetch job.
        Latency: ~1-3 seconds per call (LLM round trip).
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/ask",
            json=self._pid_json({"question": question, "engine": engine}),
        )
        _check(reply)
        result = bool(reply.get("result"))
        print(f"[paprika]   ask -> {result}", flush=True)
        return result

    # -- structured extraction (paprika-native) -----------------------------

    @_action_log
    async def extract(
        self,
        instruction: str,
        schema: Any,
        *,
        engine: str = "auto",
        context: str = "outline",
        variables: Optional[dict] = None,
        max_chars: int = 12000,
    ):
        """Ask a chat LLM to read the current page and return data
        matching ``schema``. Result is validated and (when ``schema``
        is a Pydantic model) returned as a typed instance.

        Args:
          instruction: what to extract, in plain language (JP / EN).
            May reference ${name} placeholders -- those stay visible
            to the LLM (NOT substituted client-side); pass real
            values via ``variables`` if you need late-binding at the
            CDP edge.
          schema: target shape. Accepted forms::

              # a Pydantic v2 model class
              class Product(BaseModel):
                  name: str; price: int
              p: Product = await page.extract("...", Product)

              # a list of models
              ps: list[Product] = await page.extract("...", list[Product])

              # a scalar
              n: int = await page.extract("商品数", int)

          engine: AI Engines slug or "auto" (promoted chat engine).
          context: page-state fed to the LLM. ``"outline"`` (default,
            compact -- paprika's [@N] annotated accessibility tree)
            or ``"html"`` (raw HTML excerpt; richer but more tokens).
          variables: passed through to the worker so any post-LLM
            CDP action edge can substitute ${name} -> value. Not
            usually needed for extract (read-only).
          max_chars: cap on the context blob the LLM sees.

        Raises ``PaprikaError`` (HTTP / LLM transport) or
        ``PaprikaActionError`` (LLM returned malformed JSON / failed
        schema validation).

        Latency: ~2-5 seconds per call (one LLM round trip).
        """
        # Build a JSON Schema string the LLM can target. Pydantic
        # models expose model_json_schema(); for builtins / list[X]
        # we hand-roll a minimal hint. The schema is advisory: the
        # SDK does the authoritative validation on the response.
        schema_json: str
        try:
            # Pydantic v2 BaseModel class
            if hasattr(schema, "model_json_schema") and callable(schema.model_json_schema):
                schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
            else:
                # builtins / typing.List[Model] / typing.Dict[...] / etc.
                # Fall back to a generic TypeAdapter when pydantic is present.
                try:
                    from pydantic import TypeAdapter  # type: ignore
                    schema_json = json.dumps(
                        TypeAdapter(schema).json_schema(), ensure_ascii=False,
                    )
                except Exception:
                    # Last resort: stringify the type. The LLM gets a
                    # less precise hint but the SDK validation still
                    # catches mismatch.
                    schema_json = json.dumps({"type": "value", "hint": str(schema)})
        except Exception as e:
            raise PaprikaActionError(f"ERR: extract: could not build JSON Schema from {schema!r}: {e}") from e

        body = {
            "instruction": instruction,
            "schema_json": schema_json,
            "engine": engine,
            "context": context,
            "max_chars": int(max_chars),
        }
        if variables:
            body["variables"] = dict(variables)
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/extract",
            json=self._pid_json(body),
        )
        _check(reply)
        raw = reply.get("result")
        # Validate / parse the LLM's JSON against the requested shape.
        try:
            if hasattr(schema, "model_validate") and callable(schema.model_validate):
                # Pydantic v2 model class
                return schema.model_validate(raw)
            try:
                from pydantic import TypeAdapter  # type: ignore
                return TypeAdapter(schema).validate_python(raw)
            except Exception:
                # No pydantic -- do best-effort coercion for builtins.
                if schema in (str, int, float, bool):
                    return schema(raw) if not isinstance(raw, schema) else raw
                return raw
        except Exception as e:
            raise PaprikaActionError(
                f"ERR: extract: schema validation failed: {type(e).__name__}: {e}",
            ) from e

    # -- candidate enumeration (paprika-native) -----------------------------

    @_action_log
    async def observe(
        self,
        intent: str,
        *,
        engine: str = "auto",
        max_results: int = 5,
        variables: Optional[dict] = None,
    ) -> "list[Candidate]":
        """Ask the LLM to identify up to ``max_results`` page elements
        matching ``intent`` and return them as :class:`Candidate`
        objects WITHOUT executing anything.

        This is the safe two-step pattern: the script can inspect
        what the LLM picked, decide whether to act, and pass the
        chosen candidate directly to ``page.click`` / ``page.fill``::

            cands = await page.observe("ログインフォームの送信ボタン")
            for c in cands:
                print(c.description, c.selector, c.confidence)
            if cands:
                await page.click(cands[0])

        Args:
          intent: natural-language description (JP / EN) of what to
            find. The LLM is given the current outline and asked to
            reference its [@N] markers.
          engine: AI Engines slug or "auto".
          max_results: cap on the number of candidates returned. The
            LLM is told this so it can rank top-N.
          variables: optional placeholder dict. Returned candidates
            may carry ${name} in their ``arguments``; the worker
            substitutes when the script eventually fires a CDP
            action with those args.

        Returns a list of :class:`Candidate`; possibly empty if the
        LLM finds nothing matching the intent.

        Read-only -- safe to call during a running fetch job.
        Latency: ~2-4 seconds (one LLM round trip).
        """
        body = {
            "intent": intent,
            "engine": engine,
            "max_results": int(max_results),
        }
        if variables:
            body["variables"] = dict(variables)
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/observe",
            json=self._pid_json(body),
        )
        _check(reply)
        rows = reply.get("result") or []
        out: list[Candidate] = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            try:
                out.append(Candidate(
                    selector=str(r.get("selector") or ""),
                    description=str(r.get("description") or ""),
                    method=(str(r["method"]) if r.get("method") else None),
                    arguments=(list(r["arguments"]) if r.get("arguments") else None),
                    paprika_id=(int(r["paprika_id"]) if r.get("paprika_id") is not None else None),
                    confidence=(float(r["confidence"]) if r.get("confidence") is not None else None),
                ))
            except Exception:
                # Skip malformed rows rather than fail the whole call.
                continue
        return out

    async def screenshot(
        self,
        *,
        path: Optional[str] = None,
        label: Optional[str] = None,
    ) -> bytes:
        """Return the current viewport as a PNG (Playwright-shape:
        keyword-only ``path``, returns ``bytes``).

        Args:
          path:  if given, also write the PNG to this local file
                 (like Selenium ``save_screenshot`` / Playwright
                 ``screenshot(path=...)``).
          label: if given AND the session is bound to a parent job,
                 ALSO publish this frame to that job's gallery as
                 ``screenshot-<ts>-<label>.png`` so it appears in the
                 admin UI's Live > Screenshot sub-tab. Pure byte-return
                 (no gallery) when omitted.
        """
        params = self._pid_params()
        if label:
            params["label"] = label
        png = await self._client._bytes(
            "GET", f"/sessions/{self._sid}/screenshot",
            params=params,
        )
        if path:
            with open(path, "wb") as f:
                f.write(png)
        return png

    # -- LLM-in-script primitive --------------------------------------------

    @_action_log
    async def agent(
        self,
        goal: Optional[str] = None,
        *,
        max_steps: int = 5,
        engine: str = "auto",
        # LLM-generated code often confuses ``goal`` with ``prompt`` /
        # ``task`` (since those are the conventional names in other
        # chat APIs). Accept them as aliases so a typo doesn't kill
        # the whole codegen attempt; the canonical name remains
        # ``goal``.
        prompt: Optional[str] = None,
        task: Optional[str] = None,
    ) -> dict:
        """Hand control to a vision/LLM agent for up to ``max_steps``
        observe-act cycles against this session's tab.

        Useful for localised unknowns inside an otherwise deterministic
        script: age-gates, cookie consent dialogs, "find and click the
        play button", "log in with these credentials", etc.

        ``engine`` selects the driver:

          - ``"auto"`` (default):  CogAgent first, fall back to Qwen-VL
                                   when CogAgent's output looks suspect
                                   (corner box, repeated box, ...). Good
                                   general choice.
          - ``"qwen"``:            Qwen-VL only. Emits CSS selectors
                                   against the DOM outline. Use when
                                   the target is a clean DOM element.
          - ``"cogagent"``:        CogAgent only. Emits pixel boxes
                                   against the screenshot. Use when
                                   the target is canvas/iframe content
                                   or you want to avoid the DOM round
                                   trip entirely.

        Goal is sent verbatim, but the worker auto-translates Japanese
        / Chinese goals to English before invoking either engine
        (CogAgent has known weakness with Japanese task descriptions).
        You don't need to translate yourself.

        Returns a dict::

            {
              "completed":   True if the model called done() within
                             max_steps, False if the budget was exhausted,
              "steps_taken": number of model turns consumed,
              "summary":     the model's done() summary (when completed),
              "last_action": the most recent action it executed,
              "error":       None on success; otherwise a short error string,
            }

        Does NOT raise on "completed=False" -- check the dict if you
        need to act on the outcome. Raises ``PaprikaError`` only on
        HTTP/transport failures.
        """
        # Resolve goal from any of the accepted parameter names. We
        # take ``goal`` first (canonical), then prompt, then task.
        # When more than one is given the canonical one wins.
        effective_goal = goal or prompt or task
        if not effective_goal or not str(effective_goal).strip():
            raise ValueError(
                "page.agent() requires a goal. Pass it as the first "
                "positional arg or as goal=..."
            )
        if engine not in ("auto", "qwen", "cogagent"):
            raise ValueError(
                f"engine must be 'auto', 'qwen', or 'cogagent'; got {engine!r}"
            )
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/agent",
            json={
                "goal": str(effective_goal),
                "max_steps": int(max_steps),
                "engine": engine,
            },
        )
        return reply

    # -- locators -----------------------------------------------------------

    def locator(self, selector: str) -> "Locator":
        """Playwright-style locator. CSS selectors only in V1; the
        ``data-paprika-id`` form returned by :meth:`outline` is the
        most robust choice -- use ``loc = page.locator(f'[data-paprika-id="{n}"]')``
        after parsing an outline.
        """
        return Locator(self, selector)

    def get_by_role(self, role: str, *, name: Optional[str] = None) -> "Locator":
        """Approximate Playwright's ``get_by_role``. In V1 we just build
        an attribute selector ``[role="..."]``; the ``name=`` parameter
        is not enforced (the hub doesn't yet expose accessible-name
        matching). Use :meth:`get_by_text` when you need text matching.
        """
        return Locator(self, f'[role="{role}"]')

    def get_by_text(self, text: str) -> "Locator":
        """Resolves to the first interactive element in the current
        page outline whose visible text equals ``text``. Lazy: the
        outline is fetched at action-time, so the result reflects the
        page state when you actually click."""
        return Locator(self, _ByText(text))

    # The remaining get_by_* helpers compile straight to a CSS attribute
    # selector -- no hub-side accessible-name engine needed, so they work
    # in V1. They cover the common, unambiguous attribute-based locators;
    # for fuzzy/visible-text matching use :meth:`get_by_text`.

    def get_by_test_id(self, test_id: str) -> "Locator":
        """``[data-testid="..."]``. Matches Playwright's default test-id
        attribute. (Playwright lets you reconfigure the attribute name;
        V1 fixes it to ``data-testid``.)"""
        return Locator(self, f'[data-testid="{test_id}"]')

    def get_by_placeholder(self, text: str) -> "Locator":
        """``[placeholder="..."]`` — target an input by its placeholder."""
        return Locator(self, f'[placeholder="{text}"]')

    def get_by_title(self, text: str) -> "Locator":
        """``[title="..."]`` — target an element by its ``title`` tooltip."""
        return Locator(self, f'[title="{text}"]')

    def get_by_alt_text(self, text: str) -> "Locator":
        """``[alt="..."]`` — target an image (or area/input) by alt text."""
        return Locator(self, f'[alt="{text}"]')

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        """Close this tab.

        If this Page wraps the only remaining tab in its session, the
        whole session is ended (= same behavior as the single-tab
        era's ``page.close()``). Otherwise just this tab is closed
        and the session keeps running on whichever tab the worker
        rotates to as the new default.

        No-op when this page has been ``detach()``-ed: a detached
        session is owned by the operator (noVNC + admin UI), not by
        the script anymore. The context manager's __aexit__ honours
        this so ``await cli.session(...)`` blocks naturally hand off
        to the operator without an explicit ``cli.open_session()``.
        """
        if self._closed or self._detached:
            return
        # Decide between "close this tab" vs "end session" based on
        # how many tabs are currently in the session. One round trip
        # to the hub, cheap.
        try:
            snapshot = await self._client._json(
                "GET", f"/sessions/{self._sid}/pages",
            )
            n_pages = int(snapshot.get("count") or 0)
        except Exception:
            # Hub unreachable or session already gone -> treat as
            # last-tab and try to end the session anyway.
            n_pages = 1

        self._closed = True
        # DELETE /sessions waits server-side for the worker to:
        #   * dump the session's network log to the parent job dir
        #   * drain any in-flight video downloads (passive m3u8 / mp4
        #     listener may have spawned a 1+ GB httpx stream during
        #     the session -- worker awaits idle-window + hard cap)
        #   * restore the lane's default profile, release the lane
        # The drain can legitimately take many minutes for a long
        # video; if we let the default 180s SDK timeout fire first,
        # the script's `async with cli.session()` exits, the runner
        # process terminates, and codegen-loop publishes
        # DONE_SENTINEL while the video is still streaming -- the
        # Live panel WS closes and the operator sees "job ended"
        # mid-download. Bump the timeout for THIS call to match the
        # worker's drain hard cap + headroom.
        long_close_timeout = 30 * 60.0 + 60.0  # PAPRIKA_VIDEO_DRAIN_HARD_S + slack
        if n_pages <= 1:
            # Last tab -> end the whole session.
            try:
                await self._client._json(
                    "DELETE",
                    f"/sessions/{self._sid}",
                    timeout=long_close_timeout,
                )
            except Exception:
                pass
        else:
            # Multi-tab session, close just this one. Per-page close
            # doesn't wait on drain (session stays alive on other
            # tabs), so the default timeout is fine.
            try:
                await self._client._json(
                    "DELETE",
                    f"/sessions/{self._sid}/pages/{self._page_id}",
                )
            except Exception:
                pass

    @_action_log
    async def resize_window(self, width: int, height: int) -> dict:
        """Resize the Chrome OS window. Useful for matching the noVNC
        iframe size so the viewer renders Chrome 1:1 without a scale
        transform stretching text.

        Args:
          width:  new window width in CSS pixels (200..4096)
          height: new window height in CSS pixels (200..4096)

        Returns the reply dict ``{status, result={width, height, ...}}``.
        Routes to this Page's tab via Phase 2b page_id plumbing -- the
        Chrome window is per-process so calling on any tab resizes the
        whole browser, but the page_id makes the action target this
        specific tab's CDP session for the get_window_for_target lookup.
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/resize",
            json=self._pid_json({"width": int(width), "height": int(height)}),
        )
        _check(reply)
        return reply

    async def switch(self) -> dict:
        """Make this tab the session's foreground (= the one the noVNC
        viewer follows). Equivalent to clicking the tab in a real
        browser. No-op when already the default.

        Symmetric form ``await sess.switch(idx)`` is provided on
        ``Session`` (which dispatches to ``sess[idx].switch()``).
        """
        reply = await self._client._json(
            "POST",
            f"/sessions/{self._sid}/pages/{self._page_id}/switch",
        )
        # Keep the parent Session's front_idx in sync so
        # ``sess.front`` / ``sess.front_index`` reflect the new state
        # immediately, regardless of whether the operator wrote
        # ``await sess[i].switch()`` or ``await sess.switch(i)``.
        sess = self._session
        if sess is not None:
            try:
                sess._front_idx = sess._pages.index(self)
            except ValueError:
                pass
        return reply

    async def keepalive(
        self,
        *,
        idle_ttl_s: int = 120,
        absolute_ttl_s: int = 24 * 3600,
    ) -> "HandoffInfo":
        """Switch the session into "operator keepalive" mode so it
        survives the current script's exit AND keeps its parent job
        in ``status=running`` until the session is closed.

        Mental model:
          The script's automated phase is done. The session (and
          parent job) live on in an "operator interactive" state
          until either (a) the idle timer expires, (b) the absolute
          timer expires, or (c) the operator explicitly calls
          DELETE /sessions/{sid}.

        Effects:
          * marks the page (and via ``Session.keepalive``, every
            sibling tab) so that close() / ctx-manager __aexit__
            become no-ops -- the operator owns lifetime from here on,
          * bumps the hub-side TTLs to a window suitable for human
            interaction (default 2 min idle / 24h absolute). Idle
            counts from the last operator activity -- noVNC mouse /
            key events and any /sessions/* API call reset the timer.
            If the operator goes AFK for 2 min, the session is
            auto-closed and the parent job transitions to completed.
          * for keep_session Fetch jobs: the parent job stays
            ``status=running`` with ``progress.phase=keepalive`` so
            the admin UI's LivePreview / standalone /screenshots
            tile keeps showing RUNNING (not IDLE) while the session
            is alive,
          * returns a ``HandoffInfo`` with the URLs the operator
            needs (noVNC viewer, /jobs/{id}/refresh, DELETE end).

        Use case::

            async with cli.session("https://example.com") as sess:
                # ... script does its automated work ...
                handoff = await sess.detach()
                print(f"noVNC: {handoff.novnc_url}")
            # ctx exit no longer closes the session; operator takes over.
        """
        # Server-side TTL bump. Best-effort; even if it fails the
        # client-side _detached flag still suppresses close().
        body: dict[str, int] = {
            "idle_ttl_s": int(idle_ttl_s),
            "absolute_ttl_s": int(absolute_ttl_s),
        }
        try:
            reply = await self._client._json(
                "POST", f"/sessions/{self._sid}/keepalive",
                json=body,
            )
        except Exception:
            reply = {}
        self._detached = True
        job_id = (reply or {}).get("job_id")
        return HandoffInfo(
            session_id=self._sid,
            novnc_url=f"/sessions/{self._sid}/novnc/",
            refresh_url=(f"/jobs/{job_id}/refresh" if job_id else None),
            end_url=f"/sessions/{self._sid}",
            idle_ttl_s=int(reply.get("idle_ttl_s") or idle_ttl_s),
            absolute_ttl_s=int(reply.get("absolute_ttl_s") or absolute_ttl_s),
        )

    # Backwards-compatible alias. The old name suggested "let the
    # session go off on its own"; the new ``keepalive`` matches the
    # actual mental model -- the parent Job stays ``status=running``
    # while the session is alive, and the operator ends both
    # together. Existing scripts using detach() keep working.
    detach = keepalive

    # -- multi-tab ----------------------------------------------------------

    @property
    def page_id(self) -> str:
        """The session-local id of the tab this Page wraps. Stable
        for the tab's lifetime; the default tab is always
        ``"p_default"``."""
        return self._page_id

    async def new_page(
        self,
        url: str = "about:blank",
        *,
        switch: bool = False,
    ) -> "Page":
        """Open a new tab in this session. Returns a fresh ``Page``
        wrapper bound to the new tab.

        Args:
          url:    Initial URL for the new tab. ``about:blank`` if
                  omitted; pass an http(s):// URL to navigate at
                  creation time.
          switch: If True, the new tab also becomes the session's
                  default (= where un-keyed primitives land). False
                  by default to match Playwright's pattern of not
                  auto-focusing popups.

        Example::

            page_b = await page.new_page("https://example.com")
            print(await page_b.outline())          # NOT YET -- Phase 2b
            await page_b.close()

            # Iterate every tab in the session.
            for p in await page.pages():
                print(p.page_id, p.url)
        """
        reply = await self._client._json(
            "POST", f"/sessions/{self._sid}/pages",
            json={"url": url, "switch": bool(switch)},
        )
        pid = reply.get("page_id") or ""
        new_p = Page(self._client, dict(self._info), page_id=pid)
        new_p._url = reply.get("url") or url
        print(
            f"[paprika] page.new_page(url={url!r}, switch={switch}) "
            f"-> {pid}", flush=True,
        )
        return new_p

    # Playwright/Puppeteer-style alias: most operators reach for "open
    # another tab in this session" -- ``page.open(url)`` reads more
    # naturally than ``new_page(url)`` in that context. Same wire call.
    open = new_page

    async def pages(self) -> list["Page"]:
        """Return a snapshot list of every tab currently open in this
        session, each wrapped as a ``Page``.

        The result is a plain ``list[Page]`` so it supports indexing,
        ``len()``, ``for`` iteration, and list comprehensions
        directly:

            tabs = await page.pages()
            print(f"{len(tabs)} tabs")
            for p in tabs:
                print(p.page_id, p.url)
            await tabs[-1].close()              # close the most-recent
            for p in tabs[1:]:
                await p.close()                 # close everything but #0
        """
        reply = await self._client._json(
            "GET", f"/sessions/{self._sid}/pages",
        )
        out: list[Page] = []
        for item in reply.get("pages") or []:
            p = Page(
                self._client, dict(self._info),
                page_id=item.get("page_id") or "",
            )
            p._url = item.get("url") or ""
            out.append(p)
        return out


# ---------------------------------------------------------------------------
# Session -- the user-visible top-level handle returned by cli.session().
#
# Models "1 Chrome + 1 lane + 1 noVNC + N tabs". Session IS-A Page so the
# common single-tab case ("open URL, capture, done") needs no awareness
# of the multi-tab abstraction at all -- the operator can pretend the
# session IS the page and call every primitive directly on it.
#
# For multi-tab work, Session adds:
#   * sequence protocol  -- sess[i] / len(sess) / for t in sess
#   * sess.open(url)     -- adds a tab and tracks it in the local cache
#   * sess.switch(idx)   -- symmetric form of sess[idx].switch()
#   * sess.front         -- whichever tab is currently the foreground
# ---------------------------------------------------------------------------


class Session(Page):
    """A live paprika session, exposed as a Page-shaped container of
    tabs. ``cli.session(url)`` returns one of these.

    The Session itself behaves as the default tab (the one that opens
    with the initial URL) -- every Page primitive you'd reach for on
    the single-tab API works directly on ``sess``::

        async with cli.session("https://example.com") as sess:
            await sess.capture("snap")          # acts on the default tab

    For multi-tab work, treat it like a Python sequence::

        async with cli.session("https://youtube.com") as sess:
            await sess.open("https://google.com")    # adds sess[1]

            len(sess)                                # 2
            for tab in sess:                         # iterates tabs
                print(tab.url)
            sess[1].url                              # indexed access

            await sess.switch(1)                     # foreground sess[1]
            sess.front.url                           # whichever is front

    Persistence handoff::

            handoff = await sess.detach()
            # ctx exit no longer closes; operator drives via noVNC
    """

    def __init__(self, client: "PaprikaClient", info: dict) -> None:
        # Initialise as the default tab. The session_start ack on the
        # worker always registers the initial tab under "p_default".
        super().__init__(client, info, page_id="p_default")
        # Local cache of tabs. Index 0 is `self`. Tabs added via
        # ``sess.open(url)`` are appended. The worker may spawn popup
        # tabs we don't see locally; call ``await sess.refresh()`` to
        # resync with the worker's authoritative list.
        self._pages: list[Page] = [self]
        # Track the foreground tab (= which one noVNC follows). Starts
        # at self (= sess[0]) -- session_start opens with that tab in
        # focus. switch() updates this.
        self._front_idx: int = 0
        # Back-ref so Page.switch() called on sess (itself the default
        # tab) updates front_idx via the same path as any other tab.
        self._session = self

    # ----- sequence protocol -------------------------------------------

    def __getitem__(self, idx: int) -> Page:
        """Return a Page handle for the tab at ``idx``.

        .. warning::
           The local ``_pages`` cache only tracks tabs opened via
           ``sess.open(...)`` or pulled in by ``sess.refresh()`` --
           popups spawned by worker-side clicks (e.g. ``page.agent()``
           clicking a thumbnail that opens a target=_blank link) are
           NOT auto-synced. That means after such a click ``sess[-1]``
           often still resolves to ``self`` (the Session), and
           ``await sess[-1].close()`` will invoke
           :meth:`Session.close` and destroy the entire session.

           To close popups safely, use ``await sess.close_popups()``.
           To get an accurate view of the tab list first call
           ``await sess.refresh()``.
        """
        return self._pages[idx]

    def __len__(self) -> int:
        return len(self._pages)

    def __iter__(self):
        return iter(self._pages)

    # ----- multi-tab convenience ---------------------------------------

    async def open(  # type: ignore[override]
        self,
        url: str = "about:blank",
        *,
        switch: bool = False,
    ) -> Page:
        """Open a new tab in this session and return its ``Page``.

        Also appends to the session's local cache so ``sess[i]`` / the
        sequence protocol see the new tab without an extra round trip.
        """
        new_p = await Page.open(self, url, switch=switch)
        # Wire the back-ref so the new tab's switch() / close() update
        # this session's tracking automatically.
        new_p._session = self
        self._pages.append(new_p)
        if switch:
            # The new tab took focus on the worker; mirror that here.
            self._front_idx = len(self._pages) - 1
        return new_p

    # ``new_page`` stays available too for users coming from the
    # earlier Phase 2a API. Forwards to the cache-aware path.
    new_page = open

    async def switch(  # type: ignore[override]
        self, idx: Optional[int] = None,
    ) -> dict:
        """Bring a tab to the foreground (= the tab the noVNC viewer
        follows and that ``sess.<primitive>()`` calls hit).

        Two equivalent forms::

            await sess.switch(1)        # by index
            await sess[1].switch()      # by Page

        Both end up calling
        ``POST /sessions/{sid}/pages/{pid}/switch`` for the chosen
        tab, so they're interchangeable.

        With no argument, switches to ``self`` (= sess[0]). This is
        useful after operating on a secondary tab when you want to
        return focus to the default.
        """
        target = self if idx is None else self._pages[idx]
        # Bypass Session.switch on the target (would recurse) by
        # calling Page.switch directly with the target as self.
        result = await Page.switch(target)
        # Update local front-idx tracking. We could re-derive from the
        # reply but the local index is faster + sufficient.
        if target is self:
            self._front_idx = 0
        else:
            try:
                self._front_idx = self._pages.index(target)
            except ValueError:
                pass
        return result

    @property
    def front(self) -> Page:
        """The tab currently in foreground (= what noVNC shows + what
        unqualified Session primitives target). Updated by every
        ``switch()`` call. Equivalent to ``sess[sess.front_index]``.
        """
        return self._pages[self._front_idx]

    # Operator-friendly alias matching the "current page" vocabulary
    # some browser tools use (Selenium, Puppeteer DevTools).
    current = front

    @property
    def front_index(self) -> int:
        """Index of the foreground tab in this session (0 .. len-1)."""
        return self._front_idx

    async def refresh(self) -> list[Page]:
        """Re-pull the tab list from the worker. Useful when the page
        may have spawned popup tabs the SDK doesn't know about yet
        (target=_blank links the JS rewrite missed, window.open() with
        a noopener policy that paprika hasn't intercepted, etc.).

        Replaces the local cache; existing Page references that
        survived the refresh keep working (same session_id /
        page_id), but ``sess[i]`` indexing reflects the new ordering.
        """
        live = await self.pages()
        # Keep `self` (= the default tab Page object) as sess[0] so
        # operators who stash `sess` keep getting consistent behaviour
        # for ``sess.<primitive>()`` -- those primitives use
        # self._page_id, which would silently change if we replaced
        # sess[0] with a fresh Page wrapper.
        new_pages: list[Page] = []
        for p in live:
            if p.page_id == self._page_id:
                new_pages.append(self)
            else:
                p._session = self      # wire back-ref so switch() can update us
                new_pages.append(p)
        self._pages = new_pages
        # Re-anchor front_idx; if the front tab disappeared, fall
        # back to self.
        try:
            self._front_idx = next(
                i for i, p in enumerate(self._pages) if p is self.front
            )
        except StopIteration:
            self._front_idx = 0
        return list(self._pages)

    # ----- close / detach ----------------------------------------------

    @_action_log
    async def close_popups(self) -> int:
        """Close every tab in this session except the default one.

        Refreshes the local tab list first, then ``DELETE`` s each
        non-default tab one by one. Returns the number of tabs that
        were actually closed (0 when no popups exist).

        Typical use: right after a ``page.agent()`` / ``page.click()``
        step that may have spawned a popup tab (target=_blank,
        window.open, photo-detail overlays on gallery sites, etc.).
        Calling this between iterations of a "click N items in a
        gallery" loop keeps the session pinned to its default tab
        without crashing if no popup opened::

            for i in range(N):
                await sess.agent(f"Click item {i+1}")
                await sess.capture(f"item-{i+1}")
                await sess.close_popups()   # idempotent

        .. warning::
           Do NOT write ``await sess[-1].close()`` to close a popup
           when the SDK's tab cache may be stale (i.e. anything
           that opens a tab outside of ``await sess.open(...)``).
           ``sess._pages`` only tracks tabs explicitly opened via
           ``sess.open()`` / ``sess.refresh()``, so ``sess[-1]``
           often resolves to ``sess`` itself, and ``Session.close()``
           is unconditional -- this silently kills the whole session
           and the next action gets ``HTTP 404 session not found``.
           ``close_popups()`` does the refresh + close-just-the-extras
           for you and is the only safe primitive for this pattern.
        """
        if self._closed or self._detached:
            return 0
        try:
            await self.refresh()
        except Exception:
            # Hub blip on the GET /sessions/{sid}/pages call. Without
            # a fresh tab list we can't tell popups apart from self,
            # and the whole point of this helper is to NEVER kill the
            # session by accident. Bail out -- the caller can retry.
            return 0
        closed = 0
        # Iterate over a snapshot (refresh() may rebuild _pages).
        # Skip index 0 -- that's the default tab (= self), which the
        # whole point of this method is to KEEP alive.
        for p in list(self._pages[1:]):
            if p is self:
                # Belt-and-suspenders: refresh() preserves self at
                # index 0, so this is unreachable, but if a future
                # refactor breaks that invariant we still won't
                # accidentally nuke the session.
                continue
            try:
                await p.close()
                closed += 1
            except Exception:
                # Best-effort: a popup that already self-closed (user
                # script raced us) or a hub blip shouldn't cascade
                # into the caller. Other popups still get a chance.
                pass
        # Resync the cache so subsequent ``len(sess)`` / ``sess[i]``
        # reflects the post-close state without forcing another
        # round trip.
        try:
            await self.refresh()
        except Exception:
            pass
        return closed

    async def close(self) -> None:  # type: ignore[override]
        """Close the entire session (= terminate Chrome, release the
        lane). Unlike ``Page.close()`` -- which smart-closes (only
        DELETEs the session when this was the last tab) -- the
        Session-level form is unconditional: closing the session
        closes ALL tabs.

        No-op when ``detach()``-ed: a detached session has been
        handed to the operator and only the operator (or the TTL
        reaper) should end it.

        .. warning::
           ``await sess[-1].close()`` is a common footgun: when the
           SDK's ``_pages`` cache still only has ``self`` in it (the
           usual case after a ``page.agent()`` that opens a popup
           the SDK doesn't track), ``sess[-1] is sess``, and this
           method fires -- killing the session you wanted to keep.
           To close popups, use ``await sess.close_popups()``.
        """
        if self._closed or self._detached:
            return
        # DELETE /sessions waits server-side for the worker's video
        # drain -- a 1+ GB passive download can take many minutes.
        # The default 180s SDK timeout cuts the close call off
        # mid-drain, the script exits, codegen-loop marks the job
        # complete, and the operator sees "job ended" while the
        # video was still streaming. Use the same long_close_timeout
        # as Page.close() so async-with-cli.session() callers
        # transparently get the drain wait.
        long_close_timeout = 30 * 60.0 + 60.0  # PAPRIKA_VIDEO_DRAIN_HARD_S + slack
        try:
            await self._client._json(
                "DELETE",
                f"/sessions/{self._sid}",
                timeout=long_close_timeout,
            )
        except Exception:
            pass
        self._closed = True
        for p in self._pages:
            p._closed = True

    async def keepalive(  # type: ignore[override]
        self,
        *,
        idle_ttl_s: int = 120,
        absolute_ttl_s: int = 24 * 3600,
    ) -> HandoffInfo:
        """Switch the whole session into operator keepalive mode.

        See ``Page.keepalive`` for the full semantics. The Session
        override additionally propagates the keepalive flag to every
        cached tab so a stray ``tab.close()`` won't yank one out
        from under the operator. The parent Job stays
        ``status=running`` (= LivePreview / standalone tile keep
        showing RUNNING) until the session itself is closed.
        """
        info = await Page.keepalive(
            self, idle_ttl_s=idle_ttl_s, absolute_ttl_s=absolute_ttl_s,
        )
        for p in self._pages:
            p._detached = True
        return info

    # Backwards-compatible alias (see Page.detach docstring).
    detach = keepalive


# ---------------------------------------------------------------------------
# Locator
# ---------------------------------------------------------------------------


class _ByText:
    """Marker class for get_by_text -- resolved lazily at action time."""
    def __init__(self, text: str):
        self.text = text


class Locator:
    """One element handle, resolved at action time.

    The selector is stored unresolved; each call (``click``, ``fill``,
    …) forwards to the corresponding Page method. This mirrors
    Playwright's "locator = recipe for finding an element" semantics
    rather than "locator = a fixed reference".
    """

    def __init__(self, page: Page, selector, index: int = 0):
        self._page = page
        self._selector = selector
        # Which match to act on when the selector resolves to several
        # elements. 0 = first (Playwright's default-ish). nth()/first/
        # last/all() return new Locators with this set.
        self._index = index

    def __repr__(self) -> str:
        if self._index:
            return f"Locator({self._selector!r}, index={self._index})"
        return f"Locator({self._selector!r})"

    async def _resolve(self) -> str:
        """Turn whatever was passed to ``page.locator(...)`` /
        ``get_by_text(...)`` into a concrete CSS selector that the
        worker can match. For plain strings this is a no-op."""
        sel = self._selector
        if isinstance(sel, str):
            return sel
        if isinstance(sel, _ByText):
            # Walk the current outline, find the first element whose
            # visible-text label matches, return its
            # `[data-paprika-id="N"]` selector. Cheap enough for
            # interactive flows.
            outline = await self._page.outline()
            needle = sel.text.strip()
            for line in outline.splitlines():
                line = line.strip()
                # outline line shape: [@N] tag "text" key=val ...
                # Match on the quoted text segment.
                if not line.startswith("[@"):
                    continue
                # Extract id between [@ and ]
                try:
                    rb = line.index("]")
                    idx = line[2:rb]
                except ValueError:
                    continue
                # Find the first "...". Case-insensitive equality.
                q1 = line.find('"')
                q2 = line.find('"', q1 + 1)
                if q1 == -1 or q2 == -1:
                    continue
                text = line[q1 + 1:q2]
                if text.strip().lower() == needle.lower():
                    return f'[data-paprika-id="{idx}"]'
            raise PaprikaActionError(f"NO_MATCH (no element with text {needle!r})")
        raise TypeError(f"unsupported selector spec: {sel!r}")

    async def click(self) -> dict:
        return await self._page.click(await self._resolve())

    async def fill(self, value: str) -> dict:
        return await self._page.fill(await self._resolve(), value)

    async def press(
        self,
        key: str,
        *,
        count: int = 1,
        modifiers: Optional[list] = None,
    ) -> dict:
        # press is page-level (not element-level) in our protocol --
        # focus the element first via click, then dispatch the key.
        await self._page.click(await self._resolve())
        return await self._page.press(key, count=count, modifiers=modifiers)

    async def type(self, text: str) -> dict:
        """Focus this element and type ``text`` into it."""
        await self._page.click(await self._resolve())
        return await self._page.type(text)

    # -- sub-locators (return new Locators; no I/O) -------------------------

    def nth(self, index: int) -> "Locator":
        """The ``index``-th match (0-based; negative counts from end)."""
        return Locator(self._page, self._selector, index=index)

    @property
    def first(self) -> "Locator":
        return self.nth(0)

    @property
    def last(self) -> "Locator":
        return self.nth(-1)

    async def count(self) -> int:
        """How many elements this locator currently matches."""
        return await self._page.count(await self._resolve())

    async def all(self) -> list["Locator"]:
        """A list of single-element Locators, one per current match."""
        n = await self.count()
        return [Locator(self._page, self._selector, index=i) for i in range(n)]

    # -- getters (delegate to Page, threading our index) --------------------

    async def text_content(self):
        return await self._page.text_content(await self._resolve(), index=self._index)

    async def inner_text(self):
        return await self._page.inner_text(await self._resolve(), index=self._index)

    async def inner_html(self):
        return await self._page.inner_html(await self._resolve(), index=self._index)

    async def input_value(self):
        return await self._page.input_value(await self._resolve(), index=self._index)

    async def get_attribute(self, name: str):
        return await self._page.get_attribute(
            await self._resolve(), name, index=self._index,
        )

    async def is_visible(self) -> bool:
        return await self._page.is_visible(await self._resolve(), index=self._index)

    async def is_checked(self) -> bool:
        return await self._page.is_checked(await self._resolve(), index=self._index)

    async def is_enabled(self) -> bool:
        return await self._page.is_enabled(await self._resolve(), index=self._index)

    async def is_disabled(self) -> bool:
        return await self._page.is_disabled(await self._resolve(), index=self._index)

    async def is_editable(self) -> bool:
        return await self._page.is_editable(await self._resolve(), index=self._index)

    # -- inputs (delegate to Page, threading our index) ---------------------

    async def hover(self) -> bool:
        return await self._page.hover(await self._resolve(), index=self._index)

    async def dblclick(self) -> bool:
        return await self._page.dblclick(await self._resolve(), index=self._index)

    async def focus(self) -> bool:
        return await self._page.focus(await self._resolve(), index=self._index)

    async def scroll_into_view_if_needed(self) -> bool:
        return await self._page.scroll_into_view_if_needed(
            await self._resolve(), index=self._index,
        )

    async def select_option(self, value: str) -> bool:
        return await self._page.select_option(
            await self._resolve(), value, index=self._index,
        )

    async def check(self) -> bool:
        return await self._page.check(await self._resolve(), index=self._index)

    async def uncheck(self) -> bool:
        return await self._page.uncheck(await self._resolve(), index=self._index)

    async def set_input_files(self, files) -> dict:
        """Set file(s) on this ``<input type=file>``. ``files`` is a path
        or list of paths."""
        return await self._page.set_input_files(await self._resolve(), files)

    async def wait_for(self, *, state: str = "visible", timeout: float = 30.0) -> bool:
        """Wait until this locator's selector reaches ``state``
        (``attached`` / ``detached`` / ``visible`` / ``hidden``)."""
        return await self._page.wait_for_selector(
            await self._resolve(), state=state, timeout=timeout,
        )
