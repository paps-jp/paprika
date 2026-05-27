"""Desktop UI shell: pywebview window + System tray.

paprika.exe の "顔" の部分。中身 (hub + worker + Redis) は別モジュール
が起動済の前提で、ここはユーザに見える UI だけを担当する。

設計:
  * **pywebview** で独自ウィンドウに http://localhost:{port}/ を表示
    (= Claude Desktop / Notion Desktop と同じスタイル)。WebView2
    (Windows 11 標準) を使うので Chromium ランタイム同梱不要。
  * **pystray** で System tray アイコン。右クリックで
    [開く] [設定] [終了] / 左クリックで [開く]。
  * **ウィンドウを閉じても backend は継続**: 「× ボタン」で
    ウィンドウだけ消す → tray アイコンから再表示できる。
    バックグラウンドジョブが走り続ける用途に合う。
  * **[終了] でだけ全停止**: tray menu の "終了" が唯一の本物の
    シャットダウントリガ。

依存:
  * pywebview        -- ウィンドウ
  * pystray          -- tray icon
  * Pillow           -- アイコン画像 load
  * pyobject / pythonnet は不要 (WebView2 経由なので)
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class UiShell:
    """Owns the pywebview window + tray icon for one paprika instance.

    Usage::

        shell = UiShell(
            hub_url="http://127.0.0.1:8000",
            on_quit=lambda: backend.shutdown(),
        )
        shell.run()   # blocks until [Quit] picked from tray
    """

    def __init__(
        self,
        *,
        hub_url: str,
        on_quit: Callable[[], None],
        icon_path: Path | None = None,
        window_title: str = "paprika",
        window_size: tuple[int, int] = (1280, 800),
    ) -> None:
        self.hub_url = hub_url
        self.on_quit = on_quit
        self.icon_path = icon_path
        self.window_title = window_title
        self.window_size = window_size
        self._window = None        # pywebview Window
        self._tray = None          # pystray Icon
        self._tray_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Open the window + start the tray. Blocks until on_quit fires."""
        self._start_tray()
        self._open_window()  # blocks on pywebview event loop
        # When the user closes the LAST window AND the tray decided to
        # quit, control returns here. The tray callback already called
        # on_quit.
        log.info("UiShell.run returning")

    def show_window(self) -> None:
        """Re-open the window if it was closed (from tray menu)."""
        if self._window is None:
            self._open_window()
        else:
            try:
                self._window.show()
            except Exception:
                # pywebview may have torn down the underlying webview;
                # fall back to opening a fresh one.
                self._open_window()

    def quit(self) -> None:
        """Tear down tray + window + call the operator's on_quit hook.
        Idempotent."""
        log.info("UiShell.quit invoked")
        try:
            if self._tray is not None:
                self._tray.stop()
        except Exception:
            pass
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
        try:
            self.on_quit()
        except Exception:
            log.exception("on_quit hook raised")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_window(self) -> None:
        """Open the pywebview window pointing at the hub URL.

        Imported lazily so dev mode (= no pywebview installed) can run
        with --no-ui and the import error doesn't kill startup."""
        try:
            import webview  # pywebview
        except ImportError:
            log.warning(
                "pywebview not installed; falling back to system browser"
            )
            webbrowser.open(self.hub_url)
            return

        w, h = self.window_size
        # JS-bridge API: catches ``<a target="_blank">`` clicks in the
        # admin UI and re-routes them to a new pywebview window
        # (= keeps everything inside paprika; no jump to the OS default
        # Chrome). Bound to ``window.pywebview.api`` automatically.
        api = _PaprikaJsApi(self)
        self._window = webview.create_window(
            title=self.window_title,
            url=self.hub_url,
            width=w,
            height=h,
            min_size=(800, 600),
            resizable=True,
            confirm_close=False,
            js_api=api,
        )
        # After the page loads, inject a one-shot click hook that
        # captures every ``a[target=_blank]`` and forwards the href
        # to Python. admin.js is a SPA so the same handler stays
        # live for the whole session (no per-navigation re-injection
        # needed).
        try:
            self._window.events.loaded += self._on_window_loaded
        except Exception:
            log.debug("loaded event not supported", exc_info=True)
        # The first call to webview.start() blocks until ALL windows
        # close. Subsequent show_window() calls reuse the same event
        # loop. start() must NOT be called twice in one process; we
        # only ever call it on the first run.
        #
        # ``icon=`` paints the Window's title-bar / taskbar icon
        # (Windows reads ICO; multi-resolution preferred). Without
        # this the operator sees the WebView2 default globe icon.
        start_kwargs = {}
        if self.icon_path and self.icon_path.exists():
            start_kwargs["icon"] = str(self.icon_path)
        webview.start(**start_kwargs)

    def _on_window_loaded(self) -> None:
        """Page finished loading -- inject the link-intercept JS so
        ``target=_blank`` opens a new pywebview window instead of
        the OS default browser."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(_LINK_INTERCEPT_JS)
        except Exception:
            log.exception("failed to inject link-intercept JS")

    def open_new_window(self, url: str) -> None:
        """Spawn another pywebview window pointing at ``url``.
        Called from the JS bridge when admin UI tries to open a
        target=_blank link (= screencast viewer, etc.)."""
        try:
            import webview
        except ImportError:
            webbrowser.open(url)
            return
        # The new window inherits the same icon and gets a default
        # 1280x900 size (good for screencast viewer aspect ratio).
        # We don't track these windows in self._window (only the
        # primary window). pywebview closes them when their X is
        # clicked; that's fine -- they're disposable viewers.
        log.info("opening new pywebview window for %s", url[:80])
        try:
            webview.create_window(
                title="paprika",
                url=url,
                width=1280,
                height=900,
                min_size=(640, 480),
                resizable=True,
            )
        except Exception:
            log.exception("create_window failed; falling back to system browser")
            webbrowser.open(url)


    def _start_tray(self) -> None:
        """Spin up pystray on a background thread. The main thread is
        owned by pywebview's event loop on Windows, so the tray HAS to
        live on its own thread.

        Falls back silently if pystray is not installed (= dev mode)."""
        try:
            import pystray  # noqa: F401
        except ImportError:
            log.warning("pystray not installed; tray icon disabled")
            return

        t = threading.Thread(target=self._tray_loop, daemon=True, name="paprika-tray")
        t.start()
        self._tray_thread = t

    def _tray_loop(self) -> None:
        """Pystray event loop. Runs on a background thread."""
        try:
            from PIL import Image
            import pystray
        except ImportError:
            return

        # Tray icon: 16x16 paprika icon. Falls back to a solid color so
        # we don't crash if the asset is missing in a hand-rolled dev
        # checkout.
        try:
            if self.icon_path and self.icon_path.exists():
                image = Image.open(self.icon_path)
            else:
                image = Image.new("RGB", (16, 16), color=(200, 60, 60))
        except Exception:
            image = Image.new("RGB", (16, 16), color=(200, 60, 60))

        menu = pystray.Menu(
            pystray.MenuItem(
                "開く",
                lambda _icon, _item: self.show_window(),
                default=True,  # left-click opens the window
            ),
            pystray.MenuItem(
                "設定 (Settings)",
                lambda _icon, _item: self._open_settings(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "終了 (Quit)",
                lambda _icon, _item: self.quit(),
            ),
        )
        self._tray = pystray.Icon(
            "paprika",
            image,
            "paprika",
            menu=menu,
        )
        # run() blocks the tray thread until icon.stop() is called.
        self._tray.run()

    def _open_settings(self) -> None:
        """Open the admin UI's Settings tab specifically.

        admin.js reads ``location.hash`` at startup and routes
        ``#settings`` to the Settings tab. We just point pywebview at
        the URL with that hash; if the window already exists, navigate.
        """
        url = self.hub_url.rstrip("/") + "/#settings"
        if self._window is not None:
            try:
                self._window.load_url(url)
                self._window.show()
                return
            except Exception:
                pass
        webbrowser.open(url)


# Injected into the admin UI page after every full load. Captures
# every link click with ``target="_blank"`` and pipes the href to
# Python via the pywebview JS bridge.
#
# We use the capture phase (= 3rd ``true`` arg) so the listener fires
# BEFORE the admin UI's own click handlers can call
# ``window.open()``. Without that, admin UI's bare
# ``window.open(url, '_blank')`` calls also need to be hooked, which
# is trickier. The target="_blank" anchor case covers the screencast
# viewer + every other "open externally" link admin UI uses today.
_LINK_INTERCEPT_JS = """
(function() {
  if (window.__paprikaLinkHook) return;
  window.__paprikaLinkHook = true;
  function isApiReady() {
    return window.pywebview && window.pywebview.api
      && typeof window.pywebview.api.open_new_window === 'function';
  }
  document.addEventListener('click', function(e) {
    var a = e.target && e.target.closest ? e.target.closest('a[target="_blank"]') : null;
    if (!a || !a.href) return;
    if (!isApiReady()) return;  // bridge missing -> let default fire
    e.preventDefault();
    e.stopPropagation();
    window.pywebview.api.open_new_window(a.href);
  }, true);
  // Also redirect window.open() calls (= what admin UI sometimes
  // does without an <a> tag, e.g. for noVNC autoconnect links).
  var origOpen = window.open;
  window.open = function(url) {
    if (url && isApiReady()) {
      window.pywebview.api.open_new_window(url);
      return null;
    }
    return origOpen.apply(this, arguments);
  };
})();
"""


class _PaprikaJsApi:
    """Methods on this class are exposed as ``window.pywebview.api.X``
    in the admin UI's JavaScript. Used by ``_LINK_INTERCEPT_JS`` to
    forward target=_blank clicks back to Python."""

    def __init__(self, shell: "UiShell") -> None:
        self._shell = shell

    def open_new_window(self, url: str) -> None:
        # pywebview executes JS-bridge calls on a worker thread;
        # webview.create_window needs to run on the main UI thread.
        # UiShell.open_new_window calls webview.create_window which
        # internally posts to the main loop, so this is safe to call
        # from here.
        try:
            self._shell.open_new_window(str(url))
        except Exception:
            log.exception("open_new_window bridge failed")
