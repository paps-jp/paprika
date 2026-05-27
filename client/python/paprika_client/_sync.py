"""Synchronous facade over the async paprika_client API.

The SDK is async-first (``async with async_paprika.connect()``). Some
callers want a *blocking* API instead -- legacy sync code, quick
scripts, notebooks, or the face_search crawler ported from Selenium
(whose ``ScrapeNocloseClass`` is synchronous and stateful). This module
gives them one by running a private asyncio event loop on a dedicated
background thread and bridging every coroutine call onto it, so the
public surface is a 1:1 *synchronous* mirror of the async one::

    from paprika_client import sync_paprika

    # Form A: context manager (auto-closes the client + stops the loop)
    with sync_paprika.connect("http://paprika.lan:8000") as cli:
        with cli.session("https://example.com") as page:
            page.goto("https://news.ycombinator.com")
            page.locator(".titleline > a").first.click()
            print(page.state().url)
            page.screenshot(path="hn.png", label="hn")

    # Form B: manual lifetime
    cli = sync_paprika.connect("http://paprika.lan:8000")
    try:
        page = cli.open_session("https://example.com")  # no auto-close
        page.goto("https://example.org")
        page.close()
    finally:
        cli.close()

Design
------
A single generic proxy (:class:`_Sync`) wraps any async object
(client / session / page / locator / session-handle). Attribute access
returns:

* coroutine-returning methods  -> a sync function that runs the
  coroutine on the bridge loop and returns the (re-wrapped) result;
* sync methods that return another async object (``locator``, ``nth``,
  ``get_by_text`` ...) -> a sync function that wraps the result;
* plain values / properties    -> the value (re-wrapped if it is itself
  an async object).

Because the wrapping is generic, the sync API stays automatically in
lockstep with the async one: a new ``async def`` method on ``Page``
needs no change here. Only the genuinely special shapes are
hand-handled -- the dual-mode session handle (``inspect.iscoroutine``
guard so ``with cli.session(...)`` keeps its auto-close semantics) and
the Session sequence protocol (``sess[i]`` / ``len`` / iteration), which
lives on a Session-only subclass so ``bool(cli)`` etc. stay sane on the
other proxies.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any, Optional

from ._client import PaprikaClient, _SessionHandle
from ._page import Locator, Page, Session

# Instances of these types coming back out of a call get re-wrapped, so
# the entire reachable object graph stays synchronous. (Session must be
# tested before Page in _wrap since Session is-a Page.)
_WRAP_TYPES = (PaprikaClient, _SessionHandle, Page, Session, Locator)


class _Bridge:
    """A background thread running a private asyncio loop. Submit
    coroutines from the calling (sync) thread and block for the result.

    One bridge is created per :func:`connect` and shared by every
    session / page / locator opened through that client, so they all
    run on the same loop (sharing the one httpx connection pool).
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="paprika-sync-loop",
            daemon=True,
        )
        self._thread.start()

    def run(self, coro, timeout: Optional[float] = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def stop(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        except Exception:
            pass
        try:
            self._loop.close()
        except Exception:
            pass


def _unwrap(v: Any) -> Any:
    """Pass a wrapped proxy back to the async layer as its raw object,
    so callers can hand a sync Page to another sync method transparently."""
    return v._obj if isinstance(v, _Sync) else v


def _wrap(v: Any, bridge: "_Bridge") -> Any:
    if isinstance(v, _Sync):
        return v
    if isinstance(v, Session):          # Session is-a Page -- check first
        return _SyncSession(v, bridge)
    if isinstance(v, _WRAP_TYPES):
        return _Sync(v, bridge)
    if isinstance(v, list):
        return [_wrap(x, bridge) for x in v]
    if isinstance(v, tuple):
        return tuple(_wrap(x, bridge) for x in v)
    return v


class _Sync:
    """Synchronous proxy around one async paprika object."""

    __slots__ = ("_obj", "_bridge")

    def __init__(self, obj: Any, bridge: "_Bridge") -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_bridge", bridge)

    # -- attribute proxying -------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires when normal lookup misses, so _obj /
        # _bridge (real slots) are never routed through here.
        attr = getattr(self._obj, name)
        bridge = self._bridge
        if callable(attr) and not isinstance(attr, _WRAP_TYPES):
            def _method(*args, **kwargs):
                a = tuple(_unwrap(x) for x in args)
                kw = {k: _unwrap(x) for k, x in kwargs.items()}
                res = attr(*a, **kw)
                # Only true coroutines are awaited. _SessionHandle is
                # *awaitable* but NOT a coroutine -- awaiting it here
                # would open the session eagerly and discard the
                # context-manager auto-close, so leave it wrapped.
                if inspect.iscoroutine(res):
                    res = bridge.run(res)
                return _wrap(res, bridge)
            _method.__name__ = getattr(attr, "__name__", name)
            _method.__doc__ = getattr(attr, "__doc__", None)
            return _method
        return _wrap(attr, bridge)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._obj, name, _unwrap(value))

    def __repr__(self) -> str:
        return f"Sync<{self._obj!r}>"

    # -- context manager (drives __aenter__ / __aexit__) --------------------

    def __enter__(self):
        obj = self._obj
        if not hasattr(obj, "__aenter__"):
            raise TypeError(f"{type(obj).__name__} is not a context manager")
        return _wrap(self._bridge.run(obj.__aenter__()), self._bridge)

    def __exit__(self, exc_type, exc, tb):
        obj = self._obj
        if hasattr(obj, "__aexit__"):
            self._bridge.run(obj.__aexit__(exc_type, exc, tb))
        return False


class _SyncSession(_Sync):
    """Session proxy: adds the tab sequence protocol (``sess[i]`` /
    ``len(sess)`` / iteration). Kept off the generic proxy so that
    ``bool(cli)`` / ``bool(locator)`` don't accidentally route through a
    bogus ``__len__``."""

    __slots__ = ()

    def __getitem__(self, idx):
        return _wrap(self._obj[idx], self._bridge)

    def __len__(self):
        return len(self._obj)

    def __iter__(self):
        return (_wrap(x, self._bridge) for x in self._obj)


class _SyncClient(_Sync):
    """Client proxy that owns the bridge loop and the client's httpx
    lifetime. ``connect()`` returns one of these."""

    __slots__ = ("_entered",)

    def __init__(self, obj: PaprikaClient, bridge: "_Bridge") -> None:
        super().__init__(obj, bridge)
        object.__setattr__(self, "_entered", False)

    def _ensure(self) -> None:
        if not self._entered:
            self._bridge.run(self._obj.__aenter__())
            object.__setattr__(self, "_entered", True)

    def __enter__(self):
        # Idempotent: connect() may already have started us. Returning
        # self (not a fresh wrap) keeps `with` and manual use consistent.
        self._ensure()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self) -> None:
        """Close the underlying client and stop the background loop.
        Safe to call more than once."""
        try:
            if self._entered:
                self._bridge.run(self._obj.__aexit__(None, None, None))
        except Exception:
            pass
        finally:
            object.__setattr__(self, "_entered", False)
            self._bridge.stop()

    def __del__(self):
        try:
            if getattr(self, "_entered", False):
                self.close()
            else:
                self._bridge.stop()
        except Exception:
            pass


class _SyncPaprikaNamespace:
    """Module-level entry point: ``sync_paprika.connect(...)``.

    Mirrors :data:`async_paprika` but returns a blocking client. The
    hub URL resolves identically (explicit arg -> ``PAPRIKA_HUB`` env ->
    ``http://localhost:8000``)."""

    @staticmethod
    def connect(
        base_url: Optional[str] = None,
        *,
        token: Optional[str] = None,
        timeout: float = 180.0,
        auto_start: bool = True,
    ) -> "_SyncClient":
        """Create a synchronous paprika client.

        ``auto_start`` (default ``True``) enters the underlying httpx
        client immediately so the returned object is ready to use
        without a ``with`` block::

            cli = sync_paprika.connect("http://paprika.lan:8000")
            page = cli.open_session("https://example.com")
            ...
            cli.close()

        Using it as a context manager also works -- ``__enter__`` is
        idempotent::

            with sync_paprika.connect() as cli:
                ...
        """
        bridge = _Bridge()
        try:
            client = PaprikaClient(base_url, token=token, timeout=timeout)
            sc = _SyncClient(client, bridge)
            if auto_start:
                sc._ensure()
            return sc
        except Exception:
            bridge.stop()
            raise


sync_paprika = _SyncPaprikaNamespace()

# Public alias for type hints / isinstance checks.
SyncClient = _SyncClient

__all__ = ["sync_paprika", "SyncClient"]
