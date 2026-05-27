# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for paprika.exe (Windows portable).

Build:
    pyinstaller windows/paprika.spec --noconfirm --clean

Output:
    dist/paprika/
      paprika.exe
      _internal/
        (python runtime, stdlib, server/, core/, windows/ ...)
      redis/
        redis-server.exe           (bundled)
      vnc/
        websockify.exe + TightVNC  (bundled, optional)
      static/
        admin.js + index.html      (admin UI)

Then zip dist/paprika/ → paprika-windows-vX.Y.Z.zip and publish.

Note on size: bundling all of server/ + core/ + admin UI + Redis comes
out to ~50MB. Chromium is NOT bundled (downloaded on first run by
windows/preflight.py) -- that keeps the zip small enough to host on
GitHub Releases and lets us rotate Chromium versions independently of
paprika releases.
"""

from pathlib import Path

# Resolve project root so PyInstaller picks up server/ and core/ at the
# right paths regardless of CWD at build time.
ROOT = Path.cwd()

# ---------------------------------------------------------------------------
# Data files: everything that isn't pure-python code and must travel
# alongside the bundled python runtime.
# ---------------------------------------------------------------------------

datas = []

# admin UI shell (HTML / JS / CSS / icons). Read by FastAPI's
# StaticFiles at runtime; paths are relative to the bundle root.
datas.append((str(ROOT / "server" / "hub" / "static"), "server/hub/static"))

# Bundled Redis. The build script (release/build-windows.ps1) downloads
# Redis-x64-5.0.14.zip from tporadowski/redis releases and extracts to
# windows/bin/redis/ before pyinstaller is invoked.
redis_dir = ROOT / "windows" / "bin" / "redis"
if redis_dir.exists():
    datas.append((str(redis_dir), "redis"))

# Bundled VNC server + websockify (worker noVNC bridge).
vnc_dir = ROOT / "windows" / "bin" / "vnc"
if vnc_dir.exists():
    datas.append((str(vnc_dir), "vnc"))

# Hub-wide VERSION file (used by /version endpoint + /health badge).
version_file = ROOT / "VERSION"
if version_file.exists():
    datas.append((str(version_file), "."))

# Paprika app icon (= same red/yellow pepper SVG admin UI uses).
# Bundled so runtime code (UiShell -> pywebview window icon +
# Pystray tray icon) can pick it up alongside the .exe's own
# Windows-shell icon (set via the EXE(icon=...) param below).
icon_file = ROOT / "windows" / "paprika.ico"
if icon_file.exists():
    datas.append((str(icon_file), "."))


# ---------------------------------------------------------------------------
# Hidden imports: modules pyinstaller's static analyser may miss because
# they're imported by string (uvicorn workers, importlib, etc.).
# ---------------------------------------------------------------------------

hiddenimports = [
    # FastAPI app loaded by uvicorn string ref "server.hub.app:app"
    "server.hub.app",
    # Router modules also load via string import / dynamic include
    "server.hub.routes.jobs",
    "server.hub.routes.sessions",
    "server.hub.routes.workers",
    "server.hub.routes.profiles",
    "server.hub.routes.extensions",
    "server.hub.routes.hosts",
    "server.hub.routes.presets",
    "server.hub.routes.skills",
    "server.hub.routes.conventions",
    "server.hub.routes.engines",
    "server.hub.routes.settings",
    "server.hub.routes.llm",
    "server.hub.routes.novnc",
    "server.hub.routes.system",
    "server.hub.routes.passthrough",
    # uvicorn dynamic loaders
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    # FastAPI + starlette transit deps
    "anyio._backends._asyncio",
    # tray icon + window
    "pystray._win32",
    "PIL.Image",
]


# ---------------------------------------------------------------------------
# Analysis: entry point + collected modules.
# ---------------------------------------------------------------------------

a = Analysis(
    [str(ROOT / "windows" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # We never need these in a Windows desktop build.
        "matplotlib",
        "numpy.testing",
        "pytest",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# --onedir layout (recommended over --onefile for paprika):
#   * startup is instant (no temp extract)
#   * bundled Redis / VNC binaries live alongside the exe so the user
#     can poke them with Explorer
#   * upgrades are "delete folder, unzip new"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="paprika",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX must be OFF -- it compresses the PE resource section and
    # the Windows shell then can't read the embedded icon (the .exe
    # falls back to PyInstaller's default bootloader icon). Size
    # penalty is ~3-5MB on a 14MB exe -- acceptable for keeping
    # the operator's "🌶 paprika" identity in Explorer / taskbar.
    upx=False,
    console=False,    # GUI mode -- no cmd window. Use --console flag for stderr stream.
    disable_windowed_traceback=False,
    icon=str(ROOT / "windows" / "paprika.ico") if (ROOT / "windows" / "paprika.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    # Same UPX-OFF reasoning as EXE() above. Compressing the
    # bundled DLLs also breaks resource lookup for some of them
    # (notably tk/tcl on the rare paths that load .dll resources
    # directly) and the zip-size win is marginal.
    upx=False,
    upx_exclude=[],
    name="paprika",
)
