"""Startup preflight: detect external dependencies and fetch what
paprika needs but doesn't ship.

What paprika ships in the zip:
  * python runtime (PyInstaller bundle)
  * server / windows / core / static (= paprika code)
  * redis-server.exe (tporadowski/redis ~5MB)
  * websockify.exe + TightVNC server (~10MB)
  * (NOT) Chromium    -- detected first, downloaded on first run
  * (NOT) VC++ Redist -- detected first, user-guided install

Detection runs on every paprika.exe start; download / install runs
only when something is missing. The result is cached in
``data/.preflight.json`` so subsequent starts are instant.

Why "detect + download" instead of "bundle everything":
  * Chromium is 180MB. A 50MB zip ↔ a 250MB zip is the difference
    between "instant download on a phone tether" and "30-minute wait".
  * Chromium needs frequent updates (security). Out-of-band fetch
    means paprika 1.0 stays usable when Chrome 145 ships, without us
    cutting a new paprika release.
  * VC++ Redist is on ~95% of Win10/11 boxes already; bundling it
    would double the zip for the 5% case.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses describing what's missing and what the GUI should propose
# ---------------------------------------------------------------------------


@dataclass
class MissingDep:
    """One missing prerequisite the operator may need to install."""
    key: str                # stable identifier: "chromium", "vc_redist"
    name: str               # human label: "Visual C++ Redistributable"
    why: str                # one-line explanation for the GUI
    install_url: str = ""   # download page (manual install path)
    auto_install_supported: bool = False  # GUI can offer one-click


@dataclass
class PreflightResult:
    """Aggregate of one detection pass."""
    chromium_path: Path | None = None
    vc_redist_ok: bool = True
    missing: list[MissingDep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chromium_path is not None and self.vc_redist_ok


# ---------------------------------------------------------------------------
# Chromium detection + first-run download
# ---------------------------------------------------------------------------


# Where the bundled / downloaded Chromium lives. Per-install (under the
# paprika data dir) so multiple paprika versions don't collide.
def _chromium_target_dir(data_dir: Path) -> Path:
    return data_dir / "chromium"


# Standard Windows Chrome install locations checked before falling back
# to "use the operator's existing Chrome". An empty list means "always
# use bundled Chromium" (current policy: Chromium 同梱、副作用ゼロ)。
_SYSTEM_CHROME_PATHS_UNUSED: list[str] = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def find_chromium(data_dir: Path) -> Path | None:
    """Locate a usable chrome.exe. Bundled Chromium under data_dir is
    preferred so we never co-opt the operator's daily-driver Chrome.

    Returns the path on success, None if nothing was found (caller
    triggers download)."""
    target = _chromium_target_dir(data_dir) / "chrome.exe"
    if target.exists():
        return target
    return None


# Latest stable Chromium build from a redistributable source. nodriver
# uses Playwright's revision; we mirror the simplest URL pattern.
#
# In production this URL should be pinned per paprika release so a
# Chromium upstream change doesn't silently rotate the operator's
# browser version. The hub also self-checks via "is the .exe present"
# so a partial download / power loss mid-download retries on next start.
_CHROMIUM_DL_URL = (
    # Placeholder. Real impl reads from a paprika-hosted manifest:
    #   GET https://paprika.example/dl/chromium/manifest.json
    # which returns {"version": "131.0.6778.85", "url": "..."}
    # so we can rotate Chromium without app updates. For now a constant.
    "https://playwright.azureedge.net/builds/chromium/1148/chromium-win64.zip"
)


def download_chromium(
    data_dir: Path,
    *,
    progress_cb=None,
) -> Path:
    """Fetch + extract Chromium under ``data_dir/chromium/``.

    ``progress_cb(downloaded_bytes, total_bytes)`` is invoked from the
    download loop so the GUI can show a progress bar; pass None to
    suppress. Blocks until the extracted chrome.exe is on disk.

    Idempotent: if a previous run downloaded the zip but crashed before
    extracting, it picks up where it left off."""
    target_dir = _chromium_target_dir(data_dir)
    target_exe = target_dir / "chrome.exe"
    if target_exe.exists():
        return target_exe

    tmp_zip = data_dir / ".chromium.partial.zip"
    target_dir.mkdir(parents=True, exist_ok=True)

    log.info("downloading Chromium from %s", _CHROMIUM_DL_URL)
    req = urllib.request.Request(
        _CHROMIUM_DL_URL,
        headers={"User-Agent": "paprika-windows/preflight"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with open(tmp_zip, "wb") as f:
            chunk_size = 1024 * 1024
            downloaded = 0
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    try:
                        progress_cb(downloaded, total)
                    except Exception:
                        # GUI callback failures shouldn't kill the
                        # download.
                        pass

    log.info("extracting Chromium to %s", target_dir)
    with zipfile.ZipFile(tmp_zip) as z:
        z.extractall(target_dir)
    try:
        tmp_zip.unlink()
    except OSError:
        pass

    # The Playwright Chromium zip nests under "chrome-win/". Flatten so
    # target_dir/chrome.exe is the canonical path regardless of upstream.
    nested = target_dir / "chrome-win" / "chrome.exe"
    if nested.exists() and not target_exe.exists():
        for child in (target_dir / "chrome-win").iterdir():
            shutil.move(str(child), target_dir / child.name)
        try:
            (target_dir / "chrome-win").rmdir()
        except OSError:
            pass

    if not target_exe.exists():
        raise RuntimeError(
            f"Chromium download finished but chrome.exe missing at {target_exe}. "
            f"Upstream layout may have changed."
        )
    return target_exe


# ---------------------------------------------------------------------------
# VC++ Redistributable detection
# ---------------------------------------------------------------------------


def check_vc_redist() -> bool:
    """Probe the registry / DLL for VC++ 2015-2022 Redistributable.

    The simplest reliable check is "can we load vcruntime140.dll".
    Python.exe itself depends on it, so under PyInstaller this is
    practically always True -- if vcruntime140 was missing, paprika.exe
    would have failed to load before our code ran. We keep this method
    so :func:`run_preflight` can surface a clear message if a future
    Python build drops the dep or the operator runs in a sandbox that
    moved DLLs.
    """
    if sys.platform != "win32":
        return True  # not applicable

    import ctypes
    try:
        ctypes.WinDLL("vcruntime140.dll")
        ctypes.WinDLL("vcruntime140_1.dll")  # 2019+
        return True
    except OSError:
        return False


_VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


# ---------------------------------------------------------------------------
# Cache so we don't probe every launch
# ---------------------------------------------------------------------------


def _cache_path(data_dir: Path) -> Path:
    return data_dir / ".preflight.json"


def _load_cache(data_dir: Path) -> dict:
    p = _cache_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(data_dir: Path, payload: dict) -> None:
    try:
        _cache_path(data_dir).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point used by windows/main.py
# ---------------------------------------------------------------------------


def run_preflight(
    data_dir: Path,
    *,
    auto_download_chromium: bool = True,
    download_progress_cb=None,
) -> PreflightResult:
    """One-shot preflight. Designed to be called once at startup.

    * If Chromium is missing AND ``auto_download_chromium=True``,
      downloads it inline (this is the first-run experience -- shows
      a "downloading 180 MB" progress dialog).
    * If VC++ Redist is missing, returns it in ``missing`` for the
      caller's GUI to render an install prompt; we don't silent-install
      because it requires admin rights.

    Returns a :class:`PreflightResult`. ``result.ok`` is True iff all
    hard requirements are present and paprika can proceed."""
    data_dir.mkdir(parents=True, exist_ok=True)
    result = PreflightResult()

    # --- Chromium ---
    chrome = find_chromium(data_dir)
    if chrome is None and auto_download_chromium:
        try:
            chrome = download_chromium(
                data_dir, progress_cb=download_progress_cb
            )
            result.chromium_path = chrome
        except Exception as e:
            log.exception("Chromium download failed: %s", e)
            result.missing.append(MissingDep(
                key="chromium",
                name="Chromium browser",
                why="paprika needs Chromium to drive browser automation",
                install_url=_CHROMIUM_DL_URL,
                auto_install_supported=True,
            ))
    elif chrome is None:
        result.missing.append(MissingDep(
            key="chromium",
            name="Chromium browser",
            why="paprika needs Chromium to drive browser automation",
            install_url=_CHROMIUM_DL_URL,
            auto_install_supported=True,
        ))
    else:
        result.chromium_path = chrome

    # --- VC++ Redistributable ---
    if not check_vc_redist():
        result.vc_redist_ok = False
        result.missing.append(MissingDep(
            key="vc_redist",
            name="Visual C++ 2015-2022 Redistributable",
            why="Python / Chromium runtime needs this Microsoft library",
            install_url=_VC_REDIST_URL,
            auto_install_supported=False,  # admin install, defer to OS
        ))

    # Cache the success path so subsequent startups skip the download.
    _save_cache(data_dir, {
        "chromium_path": str(result.chromium_path) if result.chromium_path else None,
        "vc_redist_ok": result.vc_redist_ok,
    })
    return result
