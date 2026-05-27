"""Local Chrome profile helpers (operator-side).

Used by ``paprika-client upload-profile`` to snapshot the operator's
Chrome ``User Data`` into a tarball that gets POSTed to the hub. The
hub stores it under ``/data/profiles/{name}.tar.gz``; jobs / sessions
that opt into ``use_profile="{name}"`` get a fresh extraction as
their ``--user-data-dir`` so the worker browser starts logged in
to everything the operator's browser is logged in to.

Mirrors ``core.fetcher.clone_chrome_profile`` so the same set of
files lands in the snapshot whether you run the local CLI or the
``options.clone_chrome_profile`` server-side path. The list is
intentionally narrow -- Cache / Code Cache / GPUCache / Crash
Reports / etc. add hundreds of MB without any value for session
restoration. If you really want the kitchen sink, override
``items=`` on ``clone_local_chrome_profile()`` -- but the default
list already covers cookies + login + autofill + per-site storage.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional


def default_chrome_user_data_dir() -> Optional[Path]:
    """Default Chrome 'User Data' root for the current OS."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return Path(local) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


# These two lists are duplicated verbatim from core.fetcher because
# the client SDK is meant to be importable standalone (no server.*
# / core.* dependency). Keep them in sync if either side adds a new
# profile item -- e.g. Chrome ships a new "Some State" sub-file.
_CLONE_ROOT_FILES: tuple[str, ...] = ("Local State",)
_CLONE_PROFILE_ITEMS: tuple[str, ...] = (
    # Cookies + auth
    "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal",
    # User-visible preferences + extension enable/disable state.
    # Note: "Preferences" carries the extension list (id, version,
    # state, install source); without it Chrome wouldn't load
    # anything from Extensions/ at startup.
    "Preferences", "Secure Preferences",
    # Misc small data stores.
    "Network", "Web Data", "Web Data-journal",
    "Local Storage", "Session Storage",
    "IndexedDB",
    # Chrome extensions. Including the on-disk extension files +
    # all the auxiliary state Chrome keeps about each extension so
    # workers see the same set of extensions the operator has
    # installed (uBlock Origin, password managers, paprika's own
    # bridge, etc.). Some of these dirs can be large -- e.g. a
    # dictionary extension might be 50+ MB -- so the upload size
    # cap (PAPRIKA_PROFILE_MAX_BYTES) may need to be raised on
    # heavy installs.
    "Extensions",                 # the extension files themselves
    "Local Extension Settings",   # chrome.storage.local per extension
    "Sync Extension Settings",    # chrome.storage.sync per extension
    "Managed Extension Settings", # chrome.storage.managed (policy)
    "Extension State",            # extension service worker state
    "Extension Rules",            # declarativeNetRequest rules
    "Extension Scripts",          # registered content scripts
)


class ProfileCloneError(RuntimeError):
    """Raised when the local Chrome profile can't be cloned. The
    message includes a hint about the most likely cause (Chrome
    locked the file, profile name typo, etc.)."""


def clone_local_chrome_profile(
    profile_name: str = "Default",
    *,
    items: Optional[Iterable[str]] = None,
    extras: Optional[Iterable[str]] = None,
    dst_root: Optional[Path] = None,
) -> Path:
    """Copy a Chrome profile to a temp dir.

    Args:
      profile_name: Name of the Chrome profile under "User Data".
        Defaults to "Default"; check chrome://version's "Profile
        Path" field if you use multiple profiles.
      items: If set, override the default per-profile item list.
        Pass an empty iterable to skip the per-profile copy entirely
        (you'll still get Local State).
      extras: Additional per-profile entries to include on top of
        the defaults (e.g. ("Bookmarks", "History")).
      dst_root: Where to write the snapshot. If None, a fresh
        ``tempfile.mkdtemp`` is used. Caller is responsible for
        cleanup either way.

    Returns the snapshot root (a "User Data"-equivalent directory
    containing "Local State" + a subdirectory named ``profile_name``
    with the per-profile items). Suitable for tarballing.

    Safe to run while Chrome is open -- files that are locked are
    skipped with a stderr warning rather than aborting the whole
    clone. The "Cookies" / "Login Data" SQLite DBs are normally
    locked but recent Chrome versions use WAL journals, so the
    snapshot still captures the post-checkpoint state. If you
    notice "logged out" sessions after upload, close Chrome before
    re-running the CLI.

    Raises ProfileCloneError if the source profile doesn't exist.
    """
    src_root = default_chrome_user_data_dir()
    if not src_root or not src_root.exists():
        raise ProfileCloneError(
            f"Chrome 'User Data' directory not found at {src_root}. "
            f"Is Chrome installed? "
            f"(Looked under LOCALAPPDATA / ~/Library / ~/.config)"
        )
    src_profile = src_root / profile_name
    if not src_profile.exists():
        raise ProfileCloneError(
            f"Chrome profile {profile_name!r} not found in {src_root}. "
            f"Check chrome://version for the actual Profile Path."
        )

    if dst_root is None:
        dst_root = Path(tempfile.mkdtemp(prefix="paprika_profile_"))
    dst_profile = dst_root / "Default"  # always 'Default' inside the snapshot
    dst_profile.mkdir(parents=True, exist_ok=True)

    item_list = tuple(items) if items is not None else _CLONE_PROFILE_ITEMS
    if extras:
        item_list = item_list + tuple(extras)

    def safe_copy(src: Path, dst: Path) -> bool:
        try:
            if src.is_file():
                shutil.copy2(src, dst)
            elif src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            return True
        except (PermissionError, OSError) as e:
            print(
                f"  warn: could not copy {src.name}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return False

    # NOTE: the previous "(safe_copy(...) and copied.append(name)) or
    # skipped.append(name)" idiom was buggy -- list.append returns
    # None, so the AND result is always None (falsy), and the OR
    # branch ALWAYS ran. Result: every item appeared in BOTH
    # "copied" and "skipped" regardless of outcome. Fixed below
    # with explicit if/else.
    copied: list[str] = []
    skipped: list[str] = []
    for name in _CLONE_ROOT_FILES:
        src = src_root / name
        if src.exists():
            if safe_copy(src, dst_root / name):
                copied.append(name)
            else:
                skipped.append(name)
    for name in item_list:
        src = src_profile / name
        if src.exists():
            if safe_copy(src, dst_profile / name):
                copied.append(name)
            else:
                skipped.append(name)

    print(
        f"  cloned profile {profile_name!r} -> {dst_root}\n"
        f"    copied:  {', '.join(copied) if copied else '(none)'}",
        file=sys.stderr,
    )
    if skipped:
        print(
            f"    skipped: {', '.join(skipped)} (locked by Chrome?)",
            file=sys.stderr,
        )
    return dst_root
