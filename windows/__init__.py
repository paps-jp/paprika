"""Windows-only packaging + runtime helpers for paprika.

Everything Windows-specific lives here so the cross-platform server code
(server/) stays clean. ``paprika.exe`` (PyInstaller --onedir) launches
``windows.main:main`` which:

  1. Runs preflight checks (Chromium / VC++ Redist detection, first-run
     Chromium download if missing).
  2. Picks free ports for Redis + hub + lane noVNC.
  3. Spawns bundled redis-server.exe via :mod:`windows.redis_supervisor`.
  4. Starts the hub in-process (``server.hub.app:app`` via uvicorn).
  5. Starts the single-lane worker via :mod:`windows.worker_supervisor`.
  6. Opens the :mod:`windows.ui_shell` pywebview window pointing at
     http://localhost:{hub_port}/.
  7. On shutdown: gracefully stops worker, hub, Redis, removes
     temp dirs.

Lifecycle vs Linux fleet:

  * fleet 版: hub / worker / Redis それぞれ別 Docker container、互いに
    network 越し。
  * Windows 単機: 全部 1 つの paprika.exe プロセスツリー内。Redis は
    subprocess、hub は in-process uvicorn task、worker (1 lane) は別
    subprocess (Chrome / Xvfb の代わりに OS 物理 display 直接利用)。

UI:
  * pywebview で localhost:{hub_port} を独自ウィンドウ表示
    (Claude Desktop 風)
  * System tray アイコン (Pystray) で右クリックメニュー
    [開く] [設定] [終了]
  * ウィンドウ閉じても backend は継続 (常駐型)
"""
