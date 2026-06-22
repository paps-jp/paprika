"""Browser fetch core.

Used by both the CLI (`fetch_html.py`) and the WebAPI (`server/`).

The public surface is:

- `FetchOptions`: dataclass with every knob the CLI exposes.
- `FetchResult`: structured output (html + saved assets + video detection + yt-dlp results).
- `async fetch(opts) -> FetchResult`: the main worker.

Helpers (`clone_chrome_profile`, `run_ytdlp`, etc.) are also exported
so the CLI can call them directly during option resolution.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# websockets.connect open_timeout bump (CDP handshake on loaded workers)
# ---------------------------------------------------------------------------
# nodriver (0.50.x) connects to Chrome's DevTools (CDP) WebSocket via
# ``websockets.connect(url, ping_timeout=..., max_size=...)`` -- WITHOUT an
# ``open_timeout`` arg, so the library default (10s in websockets ≥10) gates
# whether Chrome's CDP listener has materialised. On heavy workers (observed
# load1=15+ on a 2-lane host) Chrome spends >10s coming up before it accepts
# the WS upgrade; the handshake then raised "TimeoutError: timed out during
# opening handshake" out of uc.start() and the fetch failed without a single
# captured byte (job bb1e3e673407, 2026-06-16).
#
# Raise the default to 30s by subclassing ``websockets.connect``. Explicit
# callers (worker<->hub WS in server/worker/agent/_mix_run.py, hub<->worker
# ones) that pass open_timeout=... still win -- only the unspecified case
# moves. Env override: ``PAPRIKA_WS_OPEN_TIMEOUT`` (seconds, float).
# Idempotent: re-importing fetcher.py does not stack wrappers.
try:
    import websockets as _ws_pkg
    if not getattr(_ws_pkg.connect, "_paprika_open_timeout_patched", False):
        _PAPRIKA_WS_OPEN_TIMEOUT_S = float(
            os.environ.get("PAPRIKA_WS_OPEN_TIMEOUT", "30") or 30
        )
        _OrigConnect = _ws_pkg.connect

        class _PaprikaConnect(_OrigConnect):
            def __init__(self, *args, open_timeout=_PAPRIKA_WS_OPEN_TIMEOUT_S, **kwargs):
                super().__init__(*args, open_timeout=open_timeout, **kwargs)

        _PaprikaConnect._paprika_open_timeout_patched = True
        _ws_pkg.connect = _PaprikaConnect
except Exception:
    # websockets not installed (extremely unlikely in this codebase) or the
    # library shape changed in a future version: leave the default alone
    # rather than crashing fetcher.py at import time.
    pass


# ---------------------------------------------------------------------------
# Worker egress proxy (target-site plane only)
# ---------------------------------------------------------------------------
# A worker can route its TARGET-site fetch traffic out through a remote box
# (e.g. a per-拠点 proxy) so sites see that box's residential IP instead of
# the fleet's shared egress IP -- the worker-side half of IP-block avoidance.
#
# Deliberately env-scoped to the fetch/target plane (Chrome + yt-dlp), NOT a
# process-wide HTTP_PROXY: worker<->hub control traffic (make_async_client)
# must stay direct, or a down proxy would cut the worker off from the hub.
_EGRESS_PROXY_RESOLVED = False
_EGRESS_PROXY_CACHE = ""
# Pool pushed by the hub (Settings.proxy_pool, via HubProxyPoolSync). ``None``
# = the hub hasn't spoken yet -> fall back to the env vars below. A non-None
# value is AUTHORITATIVE: ``[]`` means the operator cleared the pool, so the
# worker egresses directly (no env fallback).
_EGRESS_POOL_OVERRIDE = None


def set_egress_pool(pool) -> None:
    """Install the hub-broadcast egress proxy pool (Settings.proxy_pool).

    Called by the worker on HubProxyPoolSync (live edit) and once on
    connect (catch-up). Forces a re-pick on the next access so a Settings
    edit is adopted -- but a *running* Chrome keeps its launch-time proxy
    (proxy is a launch flag), so a fresh pick only takes effect for
    Chrome / yt-dlp processes that START after this. Takes precedence over
    the ``PAPRIKA_WORKER_PROXY*`` env vars.
    """
    global _EGRESS_POOL_OVERRIDE, _EGRESS_PROXY_RESOLVED, _EGRESS_PROXY_CACHE
    _EGRESS_POOL_OVERRIDE = [str(p).strip() for p in (pool or []) if str(p).strip()]
    _EGRESS_PROXY_RESOLVED = False
    _EGRESS_PROXY_CACHE = ""


def _worker_egress_proxy() -> str:
    """Proxy URL for target-site egress, or ``""`` for direct.

    Pool source, in precedence order:
      * hub-broadcast pool (``set_egress_pool`` from Settings.proxy_pool) --
        authoritative once received; ``[]`` = egress direct.
      * ``PAPRIKA_WORKER_PROXY_POOL`` env -- list (comma / whitespace /
        newline separated). Fallback before the hub has pushed a pool.
      * ``PAPRIKA_WORKER_PROXY`` env -- a single proxy URL.

    Each entry is a full URL with scheme, e.g. ``http://10.20.0.5:3128``
    or ``socks5://10.20.0.5:1080``. Empty everywhere = no proxy (default;
    fully no-op so prod behaviour is unchanged until opted in).

    One pick is **cached for the worker process** (re-picked only when the
    hub pushes a new pool). Why one exit per worker, not per call: a single
    job touches several egress surfaces -- the browser fetch and the yt-dlp
    download of the same page. Token / IP-bound HLS manifests reject a
    segment request from a different IP than the page fetch, so all of a
    job's surfaces MUST share one exit IP. Rotation therefore happens
    ACROSS workers (each picks its own random exit).
    """
    global _EGRESS_PROXY_RESOLVED, _EGRESS_PROXY_CACHE
    if _EGRESS_PROXY_RESOLVED:
        return _EGRESS_PROXY_CACHE
    if _EGRESS_POOL_OVERRIDE is not None:
        # Hub-authoritative: empty list = direct (no env fallback).
        chosen = random.choice(_EGRESS_POOL_OVERRIDE) if _EGRESS_POOL_OVERRIDE else ""
    else:
        pool = [p for p in re.split(r"[,\s]+", os.environ.get("PAPRIKA_WORKER_PROXY_POOL", "")) if p]
        if pool:
            chosen = random.choice(pool)
        else:
            chosen = os.environ.get("PAPRIKA_WORKER_PROXY", "").strip()
    _EGRESS_PROXY_CACHE = chosen
    _EGRESS_PROXY_RESOLVED = True
    return chosen


def _worker_proxy_bypass() -> str:
    """Chrome ``--proxy-bypass-list``: never send loopback / LAN through the
    egress proxy (hub, redis, noVNC, devtools all live on the LAN)."""
    default = (
        "localhost;127.0.0.1;10.0.0.0/8;172.16.0.0/12;"
        "192.168.0.0/16;<-loopback>"
    )
    extra = os.environ.get("PAPRIKA_WORKER_PROXY_BYPASS", "").strip()
    return f"{default};{extra}" if extra else default


# CDP wire format is camelCase but nodriver's CookieParam dataclass uses
# snake_case field names. Browser cookies (Network.getAllCookies) and
# operator-pasted JSON come in camelCase. This mapping bridges them so
# we can `CookieParam(**translated)` cleanly. Unknown / None fields are
# dropped to avoid TypeErrors from the dataclass.
_COOKIE_CAMEL_TO_SNAKE = {
    "httpOnly": "http_only",
    "sameSite": "same_site",
    "sameParty": "same_party",
    "sourceScheme": "source_scheme",
    "sourcePort": "source_port",
    "partitionKey": "partition_key",
}
# Fields nodriver's CookieParam accepts (after key translation). Keeps
# us from passing through future CDP additions that aren't supported by
# our pinned nodriver version.
_COOKIE_PARAM_FIELDS = {
    "name", "value", "url", "domain", "path",
    "secure", "http_only", "same_site",
    "expires", "priority", "same_party",
    "source_scheme", "source_port", "partition_key",
}


def _to_cdp_cookie_param(d: dict):
    """Translate a CDP-wire (camelCase) cookie dict into the kwargs
    nodriver's ``cdp.network.CookieParam`` constructor expects, and
    return the constructed CookieParam. Returns ``None`` if the input
    can't be coerced (missing name/value).

    Drops fields nodriver doesn't model (e.g. ``size``, ``session``
    which appear on response Cookie objects but aren't valid for
    setCookies), drops None values, translates the complex
    ``partition_key`` struct via the matching CookiePartitionKey
    dataclass when available, and wraps primitive-typed nodriver
    fields (TimeSinceEpoch, enums) so that the wire serialiser doesn't
    crash with ``AttributeError: 'float' object has no attribute
    'to_json'`` etc.
    """
    import nodriver as _nd
    cdp = _nd.cdp
    if not isinstance(d, dict):
        return None
    if not d.get("name") or "value" not in d:
        return None

    # Cache nodriver's wrapper types so we can coerce raw primitives
    # into the dataclass-y shape its to_json() walker expects.
    NW = cdp.network
    TimeSinceEpoch = getattr(NW, "TimeSinceEpoch", None)
    SameSite = getattr(NW, "CookieSameSite", None)
    SourceScheme = getattr(NW, "CookieSourceScheme", None)
    Priority = getattr(NW, "CookiePriority", None)
    PartitionKey = getattr(NW, "CookiePartitionKey", None)

    def _coerce_enum(EnumCls, val):
        """Accept either an Enum member, or a string matching .value /
        .name. Returns None on no match (so the field gets dropped)."""
        if EnumCls is None or val is None:
            return None
        if isinstance(val, EnumCls):
            return val
        # Try .value match first (CDP wire strings: "Lax", "Strict", ...)
        try:
            return EnumCls(val)
        except Exception:
            pass
        # Try by name (uppercase)
        try:
            return EnumCls[str(val).upper()]
        except Exception:
            return None

    kwargs: dict = {}
    for k, v in d.items():
        if v is None:
            continue
        new_k = _COOKIE_CAMEL_TO_SNAKE.get(k, k)
        if new_k not in _COOKIE_PARAM_FIELDS:
            continue
        if new_k == "partition_key" and isinstance(v, dict):
            try:
                if PartitionKey is None:
                    continue
                pk_kwargs = {
                    "top_level_site": v.get("topLevelSite") or v.get("top_level_site"),
                    "has_cross_site_ancestor": v.get("hasCrossSiteAncestor")
                                              if "hasCrossSiteAncestor" in v
                                              else v.get("has_cross_site_ancestor"),
                }
                pk_kwargs = {pk: pv for pk, pv in pk_kwargs.items() if pv is not None}
                kwargs[new_k] = PartitionKey(**pk_kwargs)
            except Exception:
                continue
            continue
        if new_k == "expires" and TimeSinceEpoch is not None:
            # nodriver subclasses float for TimeSinceEpoch but the
            # JSON walker calls .to_json() unconditionally -- raw
            # floats blow up. Wrap.
            try:
                kwargs[new_k] = TimeSinceEpoch(float(v))
            except Exception:
                continue
            continue
        if new_k == "same_site":
            coerced = _coerce_enum(SameSite, v)
            if coerced is None:
                continue
            kwargs[new_k] = coerced
            continue
        if new_k == "source_scheme":
            coerced = _coerce_enum(SourceScheme, v)
            if coerced is None:
                continue
            kwargs[new_k] = coerced
            continue
        if new_k == "priority":
            coerced = _coerce_enum(Priority, v)
            if coerced is None:
                continue
            kwargs[new_k] = coerced
            continue
        kwargs[new_k] = v
    try:
        return cdp.network.CookieParam(**kwargs)
    except Exception:
        # Last-ditch: keep only the truly indispensable fields.
        minimal = {k: kwargs[k] for k in ("name", "value", "domain", "path", "url", "secure") if k in kwargs}
        try:
            return cdp.network.CookieParam(**minimal)
        except Exception:
            return None


def _to_cdp_cookie_params(cookies):
    """Map a list of cookie dicts to CookieParam objects, dropping any
    that fail to convert. Returns a list (possibly empty)."""
    out = []
    for c in cookies or []:
        p = _to_cdp_cookie_param(c)
        if p is not None:
            out.append(p)
    return out


async def _force_single_page_target(browser, log=None) -> int:
    """Close every ``page`` target except the first, via CDP.

    Lane Chrome instances stay alive across many fetch jobs; without
    this, popups / ad windows / left-over tabs from a previous job
    pile up and the operator sees the noVNC viewer crowded with stale
    tabs. Done via Target.getTargets + Target.closeTarget rather than
    nodriver's tab list because the latter can lag CDP state."""
    try:
        import nodriver as uc
        cdp_mod = uc.cdp
        targets = await browser.send(cdp_mod.target.get_targets()) or []
    except Exception as e:
        if log:
            log(f"  !! tab cleanup: get_targets failed: {e}")
        return 0
    pages = [t for t in targets if getattr(t, "type_", None) == "page"]
    if len(pages) <= 1:
        return 0
    keep_tid = getattr(pages[0], "target_id", None)
    closed = 0
    for t in pages[1:]:
        tid = getattr(t, "target_id", None)
        if not tid:
            continue
        try:
            await browser.send(cdp_mod.target.close_target(target_id=tid))
            closed += 1
        except Exception as e:
            if log:
                log(f"  !! tab cleanup: close {tid[:8]}.. failed: {e}")
    if log and closed:
        log(f"  ... tab cleanup: closed {closed} extra tab(s)")
    return closed

import nodriver as uc
from nodriver import cdp

# Real User-Agent obtained from the Chrome instance at startup via
# cdp.browser.get_version().  Populated once inside fetch() and reused by
# helper functions (_hls_is_live, fallback HTTP downloads) so that every
# outgoing request carries the same UA the browser sends.
_BROWSER_USER_AGENT: Optional[str] = None

_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


def _get_user_agent() -> str:
    """Return the real Chrome UA if available, else a static fallback."""
    return _BROWSER_USER_AGENT or _FALLBACK_USER_AGENT


# ----------------------------------------------------------------------------
# Logging callback
# ----------------------------------------------------------------------------

LogFn = Callable[[str], None]


def default_log(msg: str) -> None:
    """Default logger: write to stderr (matches old CLI behavior)."""
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# Chrome profile helpers
# ----------------------------------------------------------------------------

def default_chrome_user_data_dir() -> Optional[Path]:
    """Default Chrome 'User Data' root for this OS."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return Path(local) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path.home() / ".config/google-chrome"


_CLONE_ROOT_FILES = ("Local State",)
_CLONE_PROFILE_ITEMS = (
    "Cookies", "Cookies-journal",
    "Login Data", "Login Data-journal",
    "Preferences", "Secure Preferences",
    "Network", "Web Data", "Web Data-journal",
    "Local Storage", "Session Storage",
    "IndexedDB",
    # Chrome extensions -- mirrors the client-side list at
    # client/python/paprika_client/_chrome_local.py. Including
    # extensions in the clone lets paprika fetch jobs run with
    # the operator's adblocker / password manager / etc.
    "Extensions",
    "Local Extension Settings",
    "Sync Extension Settings",
    "Managed Extension Settings",
    "Extension State",
    "Extension Rules",
    "Extension Scripts",
)


def clone_chrome_profile(
    profile_name: str = "Default",
    log: LogFn = default_log,
) -> Path:
    """Copy a Chrome profile to a temp dir so nodriver can use it
    without conflicting with a running Chrome (which locks the original).

    Returns the new 'User Data'-equivalent root path.
    """
    src_root = default_chrome_user_data_dir()
    if not src_root or not src_root.exists():
        raise FileNotFoundError(f"Chrome User Data dir not found: {src_root}")
    src_profile = src_root / profile_name
    if not src_profile.exists():
        raise FileNotFoundError(
            f"Chrome profile '{profile_name}' not found in {src_root}"
        )

    dst_root = Path(tempfile.mkdtemp(prefix="nodriver_chrome_"))
    dst_profile = dst_root / "Default"
    dst_profile.mkdir(parents=True)

    def safe_copy(src: Path, dst: Path) -> bool:
        try:
            if src.is_file():
                shutil.copy2(src, dst)
            elif src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            return True
        except (PermissionError, OSError):
            return False

    copied, skipped = [], []
    for name in _CLONE_ROOT_FILES:
        src = src_root / name
        if src.exists():
            (safe_copy(src, dst_root / name) and copied.append(name)) \
                or skipped.append(name)
    for name in _CLONE_PROFILE_ITEMS:
        src = src_profile / name
        if src.exists():
            (safe_copy(src, dst_profile / name) and copied.append(name)) \
                or skipped.append(name)

    log(
        f"  ... cloned Chrome profile '{profile_name}' -> {dst_root}\n"
        f"      copied: {', '.join(copied) if copied else '(none)'}"
        + (f"\n      locked/skipped: {', '.join(skipped)}" if skipped else "")
    )
    return dst_root


# ----------------------------------------------------------------------------
# Video site detection / yt-dlp wrapper
# ----------------------------------------------------------------------------

# Note: the hardcoded video-site whitelist (VIDEO_SITE_PATTERN /
# _STATUS_VIDEO_PATTERN / is_video_site) that used to live here was
# dropped on 2026-05-28. yt-dlp target collection is now driven by:
#   * iframe-generic regex (player|embed|video|stream|watch|hub in the
#     iframe URL) -- catches embedded YouTube / Vimeo / etc. without an
#     allowlist,
#   * network-stream passive capture (HLS .m3u8 / DASH .mpd) -- catches
#     any provider that streams via standard manifest formats,
#   * HostRecipe (server.hub.hosts) -- per-host playbook for the rare
#     "direct page URL on a known video site" case (the page-url branch
#     this whitelist used to cover).
# Adding a new video site no longer needs a code edit; if a specific
# site needs the page URL fed to yt-dlp directly, register a recipe.


def _fmp4_box_type(path: Path) -> bytes:
    """Return the 4-byte box type of the first MP4 box, or b'' on error."""
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        if len(header) == 8:
            return header[4:8]
    except Exception:
        pass
    return b""


def merge_fmp4_fragments(assets_dir: Path, log: LogFn = default_log) -> list[Path]:
    """Scan *assets_dir* for fMP4 init+segment groups and merge them into
    standalone playable MP4 files.

    When yt-dlp downloads individual CMAF / HLS-fMP4 segment URLs it saves
    each segment as a separate file: one ``*_init_*.mp4`` (FTYP+MOOV only,
    no video data) and one or more ``*_{seq}_*.mp4`` fragments (MOOF+MDAT
    only, no codec header).  Neither file is playable on its own; browsers
    need the init box before they can decode any fragment.

    Detection:
      • init segment  – first MP4 box is ``ftyp`` AND file is < 100 KB
      • media segment – first MP4 box is ``moof`` or ``sidx``
                        (``sidx`` = Segment Index Box, precedes ``moof`` in
                        some CMAF streams)

    Grouping: files whose stem starts with the same leading numeric video-ID
    (e.g. ``117832863``) are treated as belonging to the same video.  Segments
    are merged in alphabetical order (which matches the HLS sequence number
    order in the yt-dlp output-template filenames).

    Returns a list of newly created merged files.
    """
    mp4_files = sorted(f for f in assets_dir.iterdir()
                       if f.is_file() and f.suffix.lower() == ".mp4")
    if not mp4_files:
        return []

    # Classify each file.
    init_map: dict[str, Path] = {}     # vid_id -> init Path
    seg_map: dict[str, list[Path]] = {}  # vid_id -> [segment Paths]

    for p in mp4_files:
        btype = _fmp4_box_type(p)
        if not btype:
            continue

        # Extract leading numeric video-ID from stem (e.g. "117832863_480p…")
        import re as _re2
        m = _re2.match(r"^(\d+)_", p.stem)
        if not m:
            continue
        vid_id = m.group(1)

        if btype == b"ftyp" and p.stat().st_size < 100_000:
            # Init segment: ftyp box, very small (no media data)
            init_map[vid_id] = p
        elif btype in (b"moof", b"sidx", b"styp"):
            # Media fragment (moof = Movie Fragment; sidx = Segment Index,
            # precedes moof in some CMAF streams; styp = Segment Type)
            seg_map.setdefault(vid_id, []).append(p)

    merged: list[Path] = []
    for vid_id, init_path in init_map.items():
        segs = seg_map.get(vid_id)
        if not segs:
            continue
        segs_sorted = sorted(segs, key=lambda p: p.name)
        out_path = assets_dir / f"{vid_id}_merged.mp4"
        if out_path.exists():
            continue
        log(
            f"  🔗 fMP4 merge: {init_path.name} + "
            f"{len(segs_sorted)} segment(s) → {out_path.name}"
        )
        try:
            with open(out_path, "wb") as fout:
                fout.write(init_path.read_bytes())
                for seg in segs_sorted:
                    fout.write(seg.read_bytes())
            size_kb = out_path.stat().st_size / 1024
            log(f"  ✅ fMP4 merged: {out_path.name} ({size_kb:.1f} KB)")

            # ------------------------------------------------------------------
            # PTS normalization: CMAF/DASH segments carry absolute timestamps
            # from the origin stream (e.g. 486 s into a live broadcast).
            # Browsers seek to that offset on load and find nothing, making the
            # file appear un-playable.  Re-mux with ffmpeg to shift all
            # timestamps to start at 0 and write a faststart moov atom.
            # Falls back silently to the raw concat if ffmpeg is absent.
            # ------------------------------------------------------------------
            _pts_tmp = out_path.with_suffix(".pts_tmp.mp4")
            try:
                _ffmpeg = shutil.which("ffmpeg")
                if _ffmpeg:
                    _rr = subprocess.run(
                        [
                            _ffmpeg, "-y",
                            "-i", str(out_path),
                            "-c", "copy",
                            "-avoid_negative_ts", "make_zero",
                            "-movflags", "+faststart",
                            str(_pts_tmp),
                        ],
                        capture_output=True,
                        timeout=120,
                    )
                    if (
                        _rr.returncode == 0
                        and _pts_tmp.exists()
                        and _pts_tmp.stat().st_size > 0
                    ):
                        _pts_tmp.replace(out_path)
                        log(f"  🕐 PTS normalized: {out_path.name}")
                    else:
                        _pts_tmp.unlink(missing_ok=True)
                        log(
                            f"  !! PTS normalize failed (rc={_rr.returncode})"
                            f" — keeping raw concat"
                        )
                else:
                    log("  ⚠ ffmpeg not found — skipping PTS normalize")
            except Exception as _pts_exc:
                _pts_tmp.unlink(missing_ok=True)
                log(f"  !! PTS normalize error: {_pts_exc} — keeping raw concat")

            merged.append(out_path)
        except Exception as exc:
            log(f"  !! fMP4 merge failed for video {vid_id}: {exc}")
            out_path.unlink(missing_ok=True)

    return merged


# ---------------------------------------------------------------------------
# yt-dlp plugin adapter loader
# ---------------------------------------------------------------------------

_YTDLP_ADAPTER: Any = None  # module object, loaded on first use


def _load_ytdlp_adapter() -> Any:
    """Load the paprika-ytdlp plugin adapter (lazy, cached).

    Tries the canonical container path first (/data/tools), then falls
    back to a path relative to this file for local dev. Returns the
    module object on success, or None if the adapter is not available.
    """
    global _YTDLP_ADAPTER
    if _YTDLP_ADAPTER is not None:
        return _YTDLP_ADAPTER
    import importlib.util
    candidates = [
        Path("/data/tools/installed/paprika-ytdlp/adapter.py"),
        Path(__file__).resolve().parents[1] / "data/tools/installed/paprika-ytdlp/adapter.py",
    ]
    for p in candidates:
        if p.is_file():
            try:
                spec = importlib.util.spec_from_file_location("paprika_ytdlp_adapter", p)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                _YTDLP_ADAPTER = mod
                return mod
            except Exception:
                pass
    return None


def _hls_is_live(url: str, referer: Optional[str] = None) -> Optional[bool]:
    """Fetch the HLS manifest and check for liveness.

    Returns:
        True   – live stream (explicit PLAYLIST-TYPE:EVENT, or short
                 media playlist with no #EXT-X-ENDLIST)
        False  – VOD / finite recording
        None   – not HLS, master playlist, or couldn't determine
    """
    if not re.search(r"\.m3u8($|\?)", url, re.I):
        return None
    import urllib.request as _ur
    try:
        headers: dict[str, str] = {"User-Agent": _get_user_agent()}
        if referer:
            headers["Referer"] = referer
        req = _ur.Request(url, headers=headers)
        # Read up to 256 KB so we don't truncate long VOD variant
        # playlists. A 30-minute VOD at 10 s segments has ~180 #EXTINF
        # lines + URLs ≈ 20-40 KB; 256 KB safely covers 4-hour movies.
        # 8 KB used to mis-classify these as live because
        # #EXT-X-ENDLIST sits at the end of the file.
        with _ur.urlopen(req, timeout=8) as resp:
            content = resp.read(262144).decode("utf-8", errors="replace")
    except Exception:
        return None
    # Master playlists (multi-variant) list sub-streams via
    # EXT-X-STREAM-INF but never contain EXT-X-ENDLIST.  They are
    # NOT live — yt-dlp resolves variants itself.  Returning True
    # here would inject --hls-use-mpegts / --download-sections flags
    # that break ffmpeg on CDNs with JPEG thumbnails in the variant
    # manifest (e.g. surrit.com).
    if "#EXT-X-STREAM-INF" in content:
        return None
    if "#EXT-X-ENDLIST" in content:
        return False
    if "#EXT-X-PLAYLIST-TYPE:VOD" in content:
        return False
    if "#EXT-X-PLAYLIST-TYPE:EVENT" in content:
        return True
    # No ENDLIST after 256 KB read. Distinguish very-long VOD
    # (hundreds of #EXTINF entries) from a sliding-window live
    # stream (usually <10 segments visible at any moment).
    if content.count("#EXTINF") >= 50:
        return False
    return True


def run_ytdlp(
    url: str,
    output_dir: Path,
    referer: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    timeout: int = 600,
    log: LogFn = default_log,
    cookies_file: Optional[Path] = None,
    *,
    is_protected: Optional[Callable[[], bool]] = None,
) -> tuple[bool, str]:
    """Shell out to yt-dlp. Returns (success, last lines of log).

    Delegates to the paprika-ytdlp plugin adapter when available
    (``/data/tools/installed/paprika-ytdlp/adapter.py``).  Falls back
    to the inline implementation when the adapter file is absent (e.g.
    during local dev or on a worker that hasn't received the bind mount).

    ``cookies_file`` (Netscape cookies.txt) is the auth path for
    sites like X / Twitter where the video manifest is behind a
    login. Preferred over ``cookies_from_browser`` because the
    latter reads the worker's on-disk Chrome cookies, which on
    Chrome 127+ are App-Bound-encrypted (v20) and undecryptable.
    The cookies.txt is built hub-side from the /hosts/{host}
    registry (plaintext cookies pushed via the Paprika Bridge
    extension), so it sidesteps all browser-cookie decryption.

    ``is_protected``: optional callable returning True if the stall +
    min-rate kill gates should DEFER killing for the next progress-
    line cycle. Used to honour noVNC operator interaction: when the
    operator is actively driving the lane via noVNC, the agent's
    callback returns True so we reset the stall / rate timers instead
    of killing yt-dlp. Forwarded to the adapter via ``_is_protected_fn``.
    None == legacy behaviour (gates kill on the regular timers).
    """
    # ------------------------------------------------------------------
    # Plugin-adapter path (preferred)
    # ------------------------------------------------------------------
    _has_curl_cffi = False
    try:
        import curl_cffi  # noqa: F401
        _has_curl_cffi = True
    except ImportError:
        pass

    adapter = _load_ytdlp_adapter()
    if adapter is not None:
        kwargs: dict[str, Any] = dict(
            url=url,
            output_dir=str(output_dir),
            referer=referer,
            cookies_file=str(cookies_file) if cookies_file else None,
            cookies_from_browser=cookies_from_browser,
            timeout=timeout,
            user_agent=_BROWSER_USER_AGENT,
            impersonate="chrome" if _has_curl_cffi else None,
            _log_fn=log,
            _is_protected_fn=is_protected,
        )
        try:
            result = adapter.download(**kwargs)
        except TypeError:
            # Older adapter without user_agent / impersonate /
            # _is_protected_fn params (remote workers auto-update core/
            # but data/tools/ may lag behind during rolling upgrades).
            # Strip the newest kwargs in reverse age order and retry.
            kwargs.pop("_is_protected_fn", None)
            try:
                result = adapter.download(**kwargs)
            except TypeError:
                kwargs.pop("user_agent", None)
                kwargs.pop("impersonate", None)
                result = adapter.download(**kwargs)
        ok, msg = result["ok"], result["message"]
        # If the adapter failed with a Cloudflare anti-bot error and
        # curl_cffi is available, fall through to the inline path
        # which passes --impersonate directly to the yt-dlp CLI.
        # The adapter may not support the impersonate kwarg yet.
        if ok or not _has_curl_cffi:
            return ok, msg
        if "cloudflare" not in msg.lower():
            return ok, msg
        log("  [ytdlp] adapter hit Cloudflare — retrying with "
            "--impersonate via inline fallback")

    # ------------------------------------------------------------------
    # Inline fallback (no plugin adapter found)
    # ------------------------------------------------------------------
    if adapter is None:
        log("  [ytdlp] plugin adapter not found — using inline fallback")
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return False, "yt-dlp not found on PATH (try: pip install yt-dlp)"

    # Detect live HLS streams first so the output template + merge
    # format match what yt-dlp will actually produce.  --hls-use-mpegts
    # forces a TS stream; saving as .mp4 produces files browsers can't
    # play.
    _live = _hls_is_live(url, referer)
    _live_flags: list[str] = []
    if _live is True:
        _live_record_s = int(os.environ.get("PAPRIKA_LIVE_HLS_RECORD_S", "30"))
        if _live_record_s <= 0:
            log(
                "  ⏭ live HLS stream detected (no #EXT-X-ENDLIST) — "
                "skipping yt-dlp (PAPRIKA_LIVE_HLS_RECORD_S=0)"
            )
            return False, "live stream skipped"
        log(
            f"  🔴 live HLS stream detected — recording first "
            f"{_live_record_s}s (PAPRIKA_LIVE_HLS_RECORD_S={_live_record_s}, container=.ts)"
        )
        _live_flags = [
            "--no-live-from-start",
            "--download-sections", f"*0-{_live_record_s}",
            "--hls-use-mpegts",
        ]

    if _live_flags:
        output_template = str(output_dir / "%(title).80s [%(id)s].ts")
        merge_format = "mpegts"
    else:
        output_template = str(output_dir / "%(title).80s [%(id)s].%(ext)s")
        merge_format = "mp4"

    cmd = [
        ytdlp,
        "-f", "bv*+ba/b",
        "--merge-output-format", merge_format,
        "--no-playlist",
        "--no-warnings",
        "--no-overwrites",
        "-o", output_template,
    ]
    # Route the video download out through the same egress proxy as the
    # browser fetch, so segment/CDN requests share the page's exit IP
    # (token/IP-bound manifests reject a mismatched download IP).
    _egress_proxy = _worker_egress_proxy()
    if _egress_proxy:
        cmd += ["--proxy", _egress_proxy]
    # Impersonate a real browser to bypass Cloudflare / anti-bot
    # challenges.  Requires curl_cffi (pip install curl_cffi).
    # yt-dlp auto-selects the best target; we just need to enable it.
    try:
        import curl_cffi  # noqa: F401
        cmd += ["--impersonate", "chrome"]
    except ImportError:
        pass
    if referer:
        cmd += ["--referer", referer]
    if cookies_file:
        cmd += ["--cookies", str(cookies_file)]
    elif cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    extras = []
    if referer:
        extras.append(f"referer={referer}")
    if cookies_file:
        extras.append(f"cookies={cookies_file}")
    elif cookies_from_browser:
        extras.append(f"cookies-from-browser={cookies_from_browser}")
    extra_log = f" ({', '.join(extras)})" if extras else ""
    log(f"  $ yt-dlp ... {url}{extra_log}")
    if _live_flags:
        cmd += _live_flags
    # Append the URL LAST, behind a ``--`` option terminator so a URL
    # beginning with ``-`` can never be misread by yt-dlp as a flag
    # (e.g. ``--exec`` = arbitrary shell command). url_safety already
    # requires an http(s) host before dispatch; this is cheap
    # defense-in-depth at the exact point the URL enters the argv.
    cmd += ["--", url]
    lines: list[str] = []
    deadline = time.monotonic() + timeout

    # ------------------------------------------------------------------
    # Stall / slow-download early-kill knobs.
    # ------------------------------------------------------------------
    # Observed on 10.10.50.143 / .152: yt-dlp dribbling a 34 MB mp4 at
    # 20 KiB/s -> ETA 24 minutes, asyncio thread pool stuck the whole
    # time, worker heartbeat blocked, hub TTL'd the worker. The plain
    # ``timeout`` (1 hour) doesn't trip because yt-dlp KEEPS emitting
    # progress lines -- it's not hung, just glacial. Need stall AND
    # min-rate gates to abort early.
    #
    # PAPRIKA_YTDLP_NO_PROGRESS_S
    #   Kill if the download percentage has not advanced (by >=0.1%)
    #   for this long. Catches a yt-dlp that's stuck on a CDN that
    #   accepted the request but stopped serving bytes -- the rate
    #   gate below won't fire (rate doesn't change either) so the
    #   stall gate is the canonical "yt-dlp is alive but the bytes
    #   aren't moving" detector. Default 90s.
    #
    # PAPRIKA_YTDLP_MIN_RATE_KIBS
    #   Minimum acceptable download rate in KiB/s. Default 50.
    #
    # PAPRIKA_YTDLP_MIN_RATE_GRACE_S
    #   Don't kill on low rate until the rate has been below the floor
    #   for at least this many continuous seconds. A short blip on a
    #   wobbly link shouldn't abort. Default 60s.
    #
    # All three are disabled by setting to 0 (= old behaviour: only
    # the wall-clock ``timeout`` kills).
    _no_progress_s = float(os.environ.get("PAPRIKA_YTDLP_NO_PROGRESS_S", "90"))
    _min_rate_kibs = float(os.environ.get("PAPRIKA_YTDLP_MIN_RATE_KIBS", "50"))
    _min_rate_grace_s = float(os.environ.get("PAPRIKA_YTDLP_MIN_RATE_GRACE_S", "60"))

    _last_pct: Optional[float] = None
    _last_pct_at = time.monotonic()
    _slow_rate_since: Optional[float] = None

    returncode = -1
    try:
        with subprocess.Popen(
            cmd + ["--newline"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                lines.append(line)
                log(line)
                now_m = time.monotonic()

                # Wall-clock timeout (the original gate).
                if now_m > deadline:
                    proc.kill()
                    return False, f"timeout after {timeout}s"

                # Parse progress + rate from this line, if any.
                pct, rate_kibs = _parse_ytdlp_progress_line(line)

                # noVNC operator priority: a callback returning True
                # means a human is actively driving this session via
                # noVNC; defer kill, reset both gate timers, keep going.
                # Evidence preservation > throughput when somebody is
                # literally watching.
                _is_protected_now = False
                try:
                    if is_protected is not None and is_protected():
                        _is_protected_now = True
                except Exception:
                    pass

                # ---- Stall gate (percentage didn't advance) ----
                if _no_progress_s > 0 and pct is not None:
                    if _last_pct is None or pct - _last_pct >= 0.1:
                        _last_pct = pct
                        _last_pct_at = now_m
                    elif now_m - _last_pct_at > _no_progress_s:
                        if _is_protected_now:
                            # Operator interacting -- reset the stall
                            # anchor and continue. Log once at INFO so
                            # the operator can see protection engaged.
                            log(
                                f"  -- stall gate: deferred kill "
                                f"({_last_pct:.1f}% for "
                                f"{now_m - _last_pct_at:.0f}s) — "
                                f"noVNC operator is interacting"
                            )
                            _last_pct_at = now_m
                        else:
                            proc.kill()
                            return False, (
                                f"stalled: download stuck at {_last_pct:.1f}% "
                                f"for {_no_progress_s:.0f}s "
                                f"(PAPRIKA_YTDLP_NO_PROGRESS_S)"
                            )

                # ---- Min-rate gate (download too slow for too long) ----
                if _min_rate_kibs > 0 and rate_kibs is not None:
                    if rate_kibs < _min_rate_kibs:
                        if _slow_rate_since is None:
                            _slow_rate_since = now_m
                        elif now_m - _slow_rate_since > _min_rate_grace_s:
                            if _is_protected_now:
                                log(
                                    f"  -- rate gate: deferred kill "
                                    f"({rate_kibs:.1f} KiB/s for "
                                    f"{now_m - _slow_rate_since:.0f}s) — "
                                    f"noVNC operator is interacting"
                                )
                                _slow_rate_since = now_m
                            else:
                                proc.kill()
                                return False, (
                                    f"too slow: {rate_kibs:.1f} KiB/s < "
                                    f"{_min_rate_kibs:.0f} KiB/s for "
                                    f"{_min_rate_grace_s:.0f}s "
                                    f"(PAPRIKA_YTDLP_MIN_RATE_KIBS / GRACE_S)"
                                )
                    else:
                        # Rate recovered above the floor; reset the grace.
                        _slow_rate_since = None
            proc.wait()
            returncode = proc.returncode
    except Exception as e:
        return False, f"failed to spawn yt-dlp: {e}"

    if returncode == 0:
        last = lines[-1:] or ["(ok)"]
        return True, last[0]
    err_tail = lines[-3:]
    return False, "\n".join(err_tail) if err_tail else f"exit={returncode}"


# Progress line parser used by the inline run_ytdlp stall / rate gates.
# Matches yt-dlp's standard --newline progress format::
#
#   [download]  45.2% of  1.20GiB at  5.00MiB/s ETA 00:30
#   [download]  20.3% of   34.48MiB at   21.67KiB/s ETA 21:38
#
# Returns ``(percent, rate_in_KiB_per_second)`` -- both ``None`` on a
# line that isn't a progress marker. Rate is normalised to KiB/s
# regardless of yt-dlp's reported unit (B / KiB / MiB / GiB).
_YTDLP_PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+"
    r"[\d.]+\s*[KMGT]?i?B(?:[^\s]*)\s+at\s+"
    r"([\d.]+)\s*([KMGT]?)i?B/s"
)
_RATE_UNIT_TO_KIBS: dict[str, float] = {
    "":  1.0 / 1024.0,  # bytes/s -> KiB/s
    "K": 1.0,
    "M": 1024.0,
    "G": 1024.0 * 1024.0,
    "T": 1024.0 * 1024.0 * 1024.0,
}


def _parse_ytdlp_progress_line(line: str) -> tuple[Optional[float], Optional[float]]:
    """Extract ``(percent, rate_KiB/s)`` from a yt-dlp progress line.

    Returns ``(None, None)`` for non-progress lines so the caller can
    use plain ``is not None`` checks. Defensive: any parsing error
    falls back to ``(None, None)`` rather than raising.
    """
    try:
        m = _YTDLP_PROGRESS_RE.search(line)
        if not m:
            return None, None
        pct = float(m.group(1))
        rate_val = float(m.group(2))
        unit = m.group(3)
        rate_kibs = rate_val * _RATE_UNIT_TO_KIBS.get(unit, 1.0)
        return pct, rate_kibs
    except Exception:
        return None, None


def pick_stream_urls(urls: list[str]) -> list[str]:
    """All unique HLS/DASH/direct video URLs (.ts segments filtered out).

    When an HLS manifest (.m3u8) is present from a CDN host, individual
    .mp4 fragment files from that same host are excluded.  Modern adaptive
    streaming CDNs (Akamai, Fastly, custom HLS origins) serve short-lived
    fMP4 segments whose signed URLs expire within seconds; by the time
    yt-dlp tries them they 404, and the .m3u8 yt-dlp call already covers
    the full stream.

    NB: this is the SYNC entry point kept for backward-compatibility. The
    async variant :func:`pick_stream_urls_async` additionally fetches each
    m3u8 to detect master-playlist relationships and skip variant children
    (= different bitrates / resolutions of the same content), avoiding the
    "yt-dlp ran twice for the same video at 720p and 1080p" duplication.
    """
    hls = [u for u in urls if re.search(r"\.m3u8($|\?)", u, re.I)]
    dash = [u for u in urls if re.search(r"\.mpd($|\?)", u, re.I)]
    direct = [
        u for u in urls
        if re.search(r"\.(mp4|webm|mov|m4v)($|\?)", u, re.I)
    ]
    # Build a set of hosts that already have an HLS manifest; any direct
    # .mp4/.mov/etc. from those hosts are almost certainly HLS segments,
    # not standalone files, and should not be queued separately.
    hls_hosts: set[str] = set()
    for u in hls:
        try:
            h = urlparse(u).hostname or ""
            if h:
                hls_hosts.add(h)
        except Exception:
            pass
    if hls_hosts:
        direct = [
            u for u in direct
            if (urlparse(u).hostname or "") not in hls_hosts
        ]
    return list(dict.fromkeys(hls + dash + direct))


# Regex over an m3u8's first few KB. A master playlist (Multivariant Playlist
# per RFC 8216) declares its variants with ``#EXT-X-STREAM-INF:`` lines, each
# immediately followed by the variant's URI on the next non-comment line. A
# media playlist (the actual segment list a single variant points at) instead
# contains ``#EXTINF:`` segment-duration tags. We classify by the FIRST
# tag we see.
_HLS_STREAM_INF_RE = re.compile(
    r"^#EXT-X-STREAM-INF:[^\n]*\n([^\s#][^\n]*)", re.M
)
_HLS_EXTINF_RE = re.compile(r"^#EXTINF:", re.M)


async def _peek_m3u8(url: str, client) -> str:
    """Fetch the first ~4KB of an m3u8 URL. Used by
    :func:`_detect_hls_variant_children` to classify master vs media playlist.
    Returns the text body (best-effort -- any error => empty string).

    4KB easily covers up to ~30 variants in a typical master playlist; we
    cap with ``Range`` to keep the probe cheap even when the actual playlist
    is much larger (a long-form HLS media playlist can be 50KB+).
    """
    try:
        # Don't follow redirects manually; httpx follow_redirects=True is the
        # caller's responsibility.
        resp = await client.get(
            url,
            headers={"Range": "bytes=0-4095"},
            timeout=10.0,
        )
        # Range may be honoured (206) or ignored (200); both fine.
        return resp.text or ""
    except Exception:
        return ""


async def _detect_hls_variant_children(
    hls_urls: list[str], client,
) -> set[str]:
    """Return the subset of ``hls_urls`` that are variant CHILDREN of another
    URL in the same list (= a master playlist).

    A master playlist whose variants are also captured by the network sniffer
    triggers redundant downloads in the current code path: paprika feeds
    EACH captured ``.m3u8`` to yt-dlp separately, even when they're the same
    video at different bitrates. By peeking each m3u8 and reading its
    ``#EXT-X-STREAM-INF`` lines, we can mark the variant URIs as "yt-dlp
    will already get these via the master" and drop them from the dispatch
    list. yt-dlp's native variant selection picks the best quality from the
    master, so the operator still gets the highest-resolution download.

    Best-effort: any HTTP / parse failure for a given URL is silently
    treated as "not a master", so the caller falls back to the existing
    no-dedup behavior (= still works, just emits the dup downloads).
    """
    from urllib.parse import urljoin

    # Parallel peek with a per-fetch 10s timeout. ``return_exceptions=True``
    # so one stuck CDN can't drop the whole detection.
    peeks = await asyncio.gather(
        *(_peek_m3u8(u, client) for u in hls_urls),
        return_exceptions=True,
    )
    bodies: dict[str, str] = {}
    for u, body in zip(hls_urls, peeks):
        if isinstance(body, str):
            bodies[u] = body
        else:
            bodies[u] = ""

    # Canonical form for matching: strip query string so a master's variant
    # URI matches a captured URL that picked up a cache-bust ``?t=NNN``.
    def _canon(u: str) -> str:
        return u.split("?", 1)[0]

    captured_canon = {_canon(u): u for u in hls_urls}
    children: set[str] = set()
    for master_url, body in bodies.items():
        if not body:
            continue
        # Classify: must have at least one #EXT-X-STREAM-INF (master marker).
        # We deliberately tolerate also seeing #EXTINF later (some playlist
        # tools generate hybrids), but a body with ONLY #EXTINF is a media
        # playlist and we leave it alone.
        m_iter = list(_HLS_STREAM_INF_RE.finditer(body))
        if not m_iter:
            continue
        # For each variant URI under #EXT-X-STREAM-INF, resolve relative to
        # the master URL and check whether it's in our captured set.
        for m in m_iter:
            variant_uri = m.group(1).strip()
            if not variant_uri:
                continue
            absolute = urljoin(master_url, variant_uri)
            target = captured_canon.get(_canon(absolute))
            if target and target != master_url:
                children.add(target)
    return children


async def pick_stream_urls_async(urls: list[str], client=None) -> list[str]:
    """Async variant of :func:`pick_stream_urls` that ALSO dedups HLS
    variants. Drops captured m3u8 URLs that are children of a captured
    master playlist (same video at lower bitrate) so yt-dlp is invoked
    ONCE per logical video instead of once per quality.

    Passes ``client`` (an httpx.AsyncClient) for the master-detection
    fetches. If ``client`` is None, behaves identically to the sync version
    (no dedup, safe fallback). The first-stage host-level segment filter
    from the sync version is preserved.
    """
    base = pick_stream_urls(urls)
    if client is None:
        return base
    # Only m3u8 URLs are relevant for master detection. Anything else
    # (DASH, direct mp4) passes through unchanged.
    hls = [u for u in base if re.search(r"\.m3u8($|\?)", u, re.I)]
    if len(hls) < 2:
        return base
    try:
        children = await _detect_hls_variant_children(hls, client)
    except Exception:
        return base
    if not children:
        return base
    return [u for u in base if u not in children]


# ----------------------------------------------------------------------------
# Asset save helpers
# ----------------------------------------------------------------------------

# Passive CDP capture saves every response whose MIME starts with one
# of these prefixes. Video is deliberately EXCLUDED -- modern streaming
# sites serve MSE/DASH fMP4 fragments, so a single playing video emits
# hundreds of 100-200KB ".mp4" segments that aren't independently
# playable. Those segments flood the gallery, waste disk, and produce
# no useful artefact (you'd need yt-dlp / ffmpeg to remux them).
#
# For real video capture, scripts MUST call ``page.download_video()``
# which shells out to yt-dlp and produces a single playable .mp4
# (including for direct-served single-file mp4s -- yt-dlp handles both
# streamed and static URLs).
#
# ``video_urls_seen`` (collected separately by on_response) still
# captures the URL list for diagnostics; only the save step is gated.
SAVE_MIME_PREFIXES = ("image/", "audio/")


# Extension → MIME fallback for the (surprisingly common) case where the
# server returns a binary image with NO Content-Type header at all.
# Cloudflare-fronted WordPress with newer image formats was the canonical
# trigger (job 4b9aff01bc6f: 8 .avif URLs in the page, 0 captured because
# the .avif response had no Content-Type, and our prefix matcher saw
# "".startswith("image/") = False).
#
# Kept tight: only formats we'd actually want to save. The matcher uses
# the URL path's basename suffix so a URL with a query string (.../foo.avif?ver=1)
# still matches.
_EXT_TO_MIME = {
    "avif":  "image/avif",
    "webp":  "image/webp",
    "jpg":   "image/jpeg",
    "jpeg":  "image/jpeg",
    "png":   "image/png",
    "gif":   "image/gif",
    "svg":   "image/svg+xml",
    "bmp":   "image/bmp",
    "ico":   "image/x-icon",
    "tif":   "image/tiff",
    "tiff":  "image/tiff",
    "jxl":   "image/jxl",
    "heic":  "image/heic",
    "heif":  "image/heif",
    # audio
    "mp3":   "audio/mpeg",
    "m4a":   "audio/mp4",
    "aac":   "audio/aac",
    "ogg":   "audio/ogg",
    "oga":   "audio/ogg",
    "wav":   "audio/wav",
    "flac":  "audio/flac",
    "opus":  "audio/opus",
}


def _mime_from_url(url: str) -> str:
    """Guess a content-type from the URL's path extension. Returns an
    empty string when the extension isn't on our save-worthy list.

    Used as a fallback when the server response has no Content-Type
    header (mainly Cloudflare-fronted WordPress sites that serve
    AVIF / WebP without setting the header). Robust to query strings
    and fragments: only the path basename is inspected."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path
    except Exception:
        return ""
    if "." not in path:
        return ""
    ext = path.rsplit(".", 1)[-1].lower()
    return _EXT_TO_MIME.get(ext, "")


def _effective_mime(server_mime: str, url: str) -> str:
    """The MIME we should use for filter + save decisions.

    Prefers the server-provided Content-Type when it's non-empty and
    not generic (``application/octet-stream`` is generic enough to
    warrant the URL-based fallback). Otherwise falls back to the URL
    extension.
    """
    m = (server_mime or "").strip().lower()
    # Generic blobs: treat as missing and let URL extension decide.
    if not m or m in ("application/octet-stream", "binary/octet-stream"):
        return _mime_from_url(url)
    return m


def _unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


# File extensions we trust as the actual content type so the gallery /
# desktop file managers can render them. Anything NOT in this set is
# treated as a non-extension suffix (e.g. xhamster's CDN serves URLs
# like ``…/1280x720.c.jpg.v1699771613`` where ``.v1699771613`` is a
# version tag, not an extension — without intervention the asset would
# land on disk as ``…v1699771613`` and the gallery would skip it).
_KNOWN_MEDIA_EXTS = frozenset({
    # images
    "jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "svg", "ico",
    "tiff", "tif", "heic", "jfif",
    # video
    "mp4", "webm", "mov", "m4v", "mkv", "avi", "ts", "m3u8", "mpd", "flv", "ogv",
    # audio
    "mp3", "m4a", "ogg", "wav", "flac", "aac", "opus",
    # docs / data
    "pdf", "html", "htm", "json", "txt", "md", "xml", "csv",
    # web assets
    "css", "js", "mjs", "wasm", "woff", "woff2", "ttf", "eot", "otf",
    # archives
    "zip", "gz", "tar", "7z",
})


def _filename_from(url: str, mime: str, fallback: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or fallback
    parts = name.split(".")
    # 1. Already ends with a recognised media ext -> leave alone.
    if len(parts) >= 2 and parts[-1].lower() in _KNOWN_MEDIA_EXTS:
        pass
    # 2. A recognised ext sits SOMEWHERE in the middle of the basename
    #    (e.g. "1280x720.c.jpg.v1699771613" has ".jpg" buried before the
    #    ".v1699771613" version tag). Move the LAST such ext to the end
    #    so file managers / the gallery treat the asset as that media
    #    type. Preserves original casing of the moved segment.
    elif len(parts) >= 3 and any(p.lower() in _KNOWN_MEDIA_EXTS for p in parts[:-1]):
        for i in range(len(parts) - 2, -1, -1):
            if parts[i].lower() in _KNOWN_MEDIA_EXTS:
                ext = parts.pop(i)
                parts.append(ext)
                break
        name = ".".join(parts)
    # 3. No recognised ext anywhere -> derive one from MIME (legacy
    #    behaviour, unchanged for the unnamed-blob case).
    elif "." not in name:
        ext = mime.split(";")[0].split("/")[-1] or "bin"
        name = f"{name}.{ext}"
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:180]


# ----------------------------------------------------------------------------
# In-page JS payloads
# ----------------------------------------------------------------------------

_DETECT_VIDEO_JS = r"""
JSON.stringify((() => {
    const HOSTS = /youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|tiktok\.com|jwplayer|wistia|brightcove|streamable/i;
    const videos = [...document.querySelectorAll('video')].map(v => ({
        src: v.currentSrc || v.src || null,
        sources: [...v.querySelectorAll('source')].map(s => ({
            src: s.src || null,
            type: s.type || null,
        })),
        poster: v.poster || null,
        duration: isNaN(v.duration) ? null : v.duration,
        autoplay: v.autoplay,
        muted: v.muted,
    }));
    const iframes = [...document.querySelectorAll('iframe')]
        .filter(f => f.src && HOSTS.test(f.src))
        .map(f => {
            let host = '';
            try { host = new URL(f.src).hostname; } catch (e) {}
            return { src: f.src, host };
        });
    return { videos, iframes };
})())
"""

_TRIGGER_VIDEOS_JS = r"""
JSON.stringify((() => {
    const HOSTS = /youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|tiktok\.com|jwplayer|wistia|brightcove|streamable/i;
    const out = { played: [], to_click: [], total_video: 0, total_iframe: 0 };
    for (const el of document.querySelectorAll('video, iframe')) {
        try {
            const isVideo = el.tagName === 'VIDEO';
            const isVideoIframe = el.tagName === 'IFRAME' && el.src && HOSTS.test(el.src);
            if (!isVideo && !isVideoIframe) continue;

            if (isVideo) out.total_video++;
            else out.total_iframe++;

            if (isVideo && !el.dataset._played) {
                try {
                    el.muted = true;
                    el.playsInline = true;
                    if (typeof el.preload === 'string') el.preload = 'auto';
                    try { el.load(); } catch (e) {}
                    const p = el.play();
                    if (p && typeof p.catch === 'function') p.catch(() => {});
                    el.dataset._played = '1';
                    out.played.push(el.currentSrc || el.src || '(no src)');
                } catch (e) {}
            }

            if (el.dataset._clicked) continue;
            const r = el.getBoundingClientRect();
            const visible = r.width >= 20 && r.height >= 20
                          && r.bottom > 0 && r.top < window.innerHeight
                          && r.right > 0 && r.left < window.innerWidth;
            if (!visible) continue;

            el.dataset._clicked = '1';
            out.to_click.push({
                tag: el.tagName.toLowerCase(),
                src: el.currentSrc || el.src || '(no src)',
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top + r.height / 2),
            });
        } catch (e) {}
    }
    return out;
})())
"""

_FALLBACK_CLICK_JS = r"""
JSON.stringify((() => {
    if (window.__centerClickedOnce) return null;
    if (document.querySelector('video')) return null;
    window.__centerClickedOnce = true;
    return {
        x: Math.round(window.innerWidth / 2),
        y: Math.round(window.innerHeight / 2),
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight
    };
})())
"""


async def detect_videos(tab) -> dict:
    raw = await tab.evaluate(_DETECT_VIDEO_JS)
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"videos": [], "iframes": []}


# Pick the page's "representative image" from the LIVE DOM -- the cover /
# hero image a human associates with the page, NOT the site logo. Runs
# the same priority cascade as server.hub.meta but with one signal a
# static HTML parse can't have: the TRUE decoded size of every <img>
# (naturalWidth*naturalHeight), so the largest-image fallback (priority
# 5) is exact rather than guessed from declared width/height attributes.
#
# Priority: 1 OGP -> 2 Twitter Card -> 3 JSON-LD (image / thumbnailUrl /
# thumbnail; @graph + nesting + arrays; value may be a string,
# {url|contentUrl|@id}, or a list) -> 4 <link rel=image_src> -> 5 biggest
# <img> by naturalWidth*naturalHeight (currentSrc preferred), with
# logo / icon / sprite / pixel URLs and implausible aspect ratios
# filtered out. Returns a JSON string {url, source, width?, height?,
# area?} or {url:null, source:null}.
_PICK_REPRESENTATIVE_IMAGE_JS = r"""
JSON.stringify((() => {
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch (e) { return null; } };
  const clean = (u) => {
    if (!u || typeof u !== 'string') return null;
    u = u.trim();
    if (!u) return null;
    const low = u.toLowerCase();
    if (low.startsWith('data:') || low.startsWith('blob:') ||
        low.startsWith('javascript:') || low.startsWith('about:')) return null;
    return abs(u);
  };
  const metaC = (sel) => { const el = document.querySelector(sel); return el ? clean(el.getAttribute('content')) : null; };

  // 1. OGP
  let u = metaC('meta[property="og:image:secure_url"]')
       || metaC('meta[property="og:image:url"]')
       || metaC('meta[property="og:image"]')
       || metaC('meta[name="og:image"]');
  if (u) return { url: u, source: 'og:image' };

  // 2. Twitter Card
  u = metaC('meta[name="twitter:image"]')
   || metaC('meta[name="twitter:image:src"]')
   || metaC('meta[property="twitter:image"]')
   || metaC('meta[property="twitter:image:src"]');
  if (u) return { url: u, source: 'twitter:image' };

  // 3. JSON-LD (recursive)
  const KEYS = ['image', 'thumbnailurl', 'thumbnail'];
  const urlFromVal = (v) => {
    if (!v) return null;
    if (typeof v === 'string') return clean(v);
    if (Array.isArray(v)) { for (const it of v) { const r = urlFromVal(it); if (r) return r; } return null; }
    if (typeof v === 'object') {
      for (const k in v) {
        const kl = k.toLowerCase();
        if (kl === 'url' || kl === 'contenturl' || kl === '@id') { const r = clean(v[k]); if (r) return r; }
      }
    }
    return null;
  };
  const walk = (node) => {
    if (!node || typeof node !== 'object') return null;
    if (Array.isArray(node)) { for (const it of node) { const r = walk(it); if (r) return r; } return null; }
    const lower = {};
    for (const k in node) lower[k.toLowerCase()] = k;
    for (const want of KEYS) { if (want in lower) { const r = urlFromVal(node[lower[want]]); if (r) return r; } }
    if ('@graph' in lower) { const r = walk(node[lower['@graph']]); if (r) return r; }
    for (const k in node) { const v = node[k]; if (v && typeof v === 'object') { const r = walk(v); if (r) return r; } }
    return null;
  };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    let data; try { data = JSON.parse(s.textContent); } catch (e) { continue; }
    const r = walk(data);
    if (r) return { url: r, source: 'json-ld' };
  }

  // 4. <link rel="image_src">
  { const el = document.querySelector('link[rel="image_src"]');
    if (el) { const r = clean(el.getAttribute('href')); if (r) return { url: r, source: 'image_src' }; } }

  // 5. Largest <img> by TRUE naturalWidth*naturalHeight (currentSrc preferred)
  const BAD = /sprite|logo|favicon|\/icons?[\/_-]|[\/_-]icon|avatar|blank|spacer|1x1|pixel|placeholder|loader|loading|emoji|\/flag|badge|[\/_-]btn|button|rating|[\/_-]star|watermark/i;
  let best = null, bestArea = 0;
  for (const img of document.images) {
    const w = img.naturalWidth | 0, h = img.naturalHeight | 0;
    if (!w || !h) continue;                 // not decoded / broken
    if (w < 100 || h < 100) continue;       // ignore icons / thumbs
    const ar = w / h;
    if (ar > 8 || ar < 0.125) continue;     // skip banners / rules
    const cand = img.currentSrc || img.src || '';
    const c = clean(cand);
    if (!c) continue;
    if (BAD.test(cand)) continue;
    const area = w * h;
    if (area > bestArea) { bestArea = area; best = { url: c, width: w, height: h }; }
  }
  if (best) return { url: best.url, source: 'img', width: best.width, height: best.height, area: bestArea };

  return { url: null, source: null };
})())
"""


async def pick_representative_image(tab) -> dict:
    """Pick the page's representative (cover/hero) image from the live DOM.

    Runs the OGP -> Twitter -> JSON-LD -> image_src -> largest-<img>
    cascade. The win over a static page.html parse is priority 5: the
    biggest <img> is chosen by TRUE naturalWidth*naturalHeight (only
    knowable in a live browser after image decode), with logos / icons /
    sprites / tracking pixels filtered out.

    Returns {url, source, width?, height?, area?} or {} when nothing
    qualified or the probe failed. ``source`` is one of og:image /
    twitter:image / json-ld / image_src / img."""
    try:
        raw = await tab.evaluate(_PICK_REPRESENTATIVE_IMAGE_JS)
    except Exception:
        return {}
    try:
        out = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(out, dict) or not out.get("url"):
        return {}
    return out


# Structural "is the content blocked by a full-screen overlay?" probe. The
# crux: we do NOT parse z-index or match site-specific keywords/URLs. We ask
# the browser's own hit-testing (elementFromPoint) what is painted on top at a
# grid of viewport points, then read resolved layout + ARIA standards. That
# makes it generic across sites (login walls, age gates, cookie/consent
# modals, paywalls) with zero hardcoding. Everything is wrapped so a probe
# failure returns {error:...} and never breaks the fetch.
_DETECT_OVERLAY_JS = r"""
JSON.stringify((() => {
  try {
    const W = Math.max(1, window.innerWidth || 0);
    const H = Math.max(1, window.innerHeight || 0);
    const A = W * H;
    // (1) viewport grid hit-test -> which painted "layer root" owns each point.
    // Climb each hit element to its nearest positioned (fixed/absolute/sticky)
    // ancestor: that is the resolved stacking layer the browser drew on top,
    // so z-index / transforms / stacking contexts are all already accounted
    // for by hit-testing -- we never interpret z-index ourselves.
    const layerRoot = (el) => {
      let r = el;
      while (r && r.parentElement) {
        let pos = '';
        try { pos = getComputedStyle(r).position; } catch (e) {}
        if (pos === 'fixed' || pos === 'absolute' || pos === 'sticky') break;
        r = r.parentElement;
      }
      return r || el;
    };
    const COLS = 9, ROWS = 9;
    const hits = new Map();
    let samples = 0;
    for (let i = 1; i < COLS; i++) {
      for (let j = 1; j < ROWS; j++) {
        const el = document.elementFromPoint(W * i / COLS, H * j / ROWS);
        if (!el) continue;
        samples++;
        const root = layerRoot(el);
        hits.set(root, (hits.get(root) || 0) + 1);
      }
    }
    let top = null, topN = 0;
    for (const [el, c] of hits) { if (c > topN) { topN = c; top = el; } }
    let coverage = 0, topTag = '', topRole = '', topPos = '';
    if (top) {
      try {
        const r = top.getBoundingClientRect();
        coverage = Math.min(1, (Math.max(0, r.width) * Math.max(0, r.height)) / A);
      } catch (e) {}
      topTag = (top.tagName || '').toLowerCase();
      try { topRole = top.getAttribute('role') || ''; } catch (e) {}
      try { topPos = getComputedStyle(top).position; } catch (e) {}
    }
    const dominance = samples ? topN / samples : 0;
    // (2) background scroll lock -- modals almost always pin the page.
    let scrollLock = false;
    try {
      const bo = getComputedStyle(document.body).overflowY;
      const ho = getComputedStyle(document.documentElement).overflowY;
      scrollLock = (bo === 'hidden' || bo === 'clip' || ho === 'hidden' || ho === 'clip');
    } catch (e) {}
    // (3) standards-based modal markers (ARIA + top-layer pseudo) -- web
    // standards, not site keywords. Each guarded: invalid selectors throw.
    let ariaModal = false;
    try {
      ariaModal = !!document.querySelector('dialog[open], [aria-modal="true"], [role="dialog"], [role="alertdialog"]');
    } catch (e) {}
    if (!ariaModal) {
      try { ariaModal = !!document.querySelector(':modal, [popover]:popover-open'); } catch (e) {}
    }
    let inertMain = false;
    try { inertMain = !!document.querySelector('[inert], [aria-hidden="true"]'); } catch (e) {}
    // (4) login password field -- a standard element, far stronger than the
    // word "login" appearing somewhere on the page. ONLY counts if the field is
    // ACTUALLY VISIBLE (not a hidden header-dropdown login form, which many
    // Chinese aggregator sites stash in the DOM for a future "登录" click).
    // Pre-fix that flagged every page with a header login form as 課題 even
    // when the article body rendered fine (false positive on fld777.com /
    // 福利岛 articles, post-mortem 2026-06-15). "Visible" =
    //   * non-zero bounding box (display:none -> 0x0 rect, eliminated),
    //   * intersects the current viewport (off-screen hidden tabs eliminated),
    //   * computed style not display:none / visibility:hidden / opacity:0.
    let hasPassword = false;
    try {
      for (const inp of document.querySelectorAll('input[type="password"]')) {
        const b = inp.getBoundingClientRect();
        if (b.width <= 0 || b.height <= 0) continue;  // display:none
        if (b.bottom <= 0 || b.top >= H || b.right <= 0 || b.left >= W) continue;  // off-screen
        const cs = window.getComputedStyle(inp);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        if (parseFloat(cs.opacity || '1') <= 0.01) continue;
        hasPassword = true;
        break;
      }
    } catch (e) {}
    // (5) visible-image scarcity (the operator's intuition), counted on
    // actually-rendered, in-viewport images only.
    let visibleImages = 0;
    try {
      for (const im of document.images) {
        const b = im.getBoundingClientRect();
        if (b.width > 32 && b.height > 32 && b.bottom > 0 && b.top < H && b.right > 0 && b.left < W) {
          visibleImages++;
        }
      }
    } catch (e) {}
    // (6) visible text length (content scarcity).
    let textLen = 0;
    try { textLen = ((document.body && document.body.innerText) || '').trim().length; } catch (e) {}
    return {
      coverage: +coverage.toFixed(3),
      dominance: +dominance.toFixed(3),
      scrollLock: scrollLock,
      ariaModal: ariaModal,
      inertMain: inertMain,
      hasPassword: hasPassword,
      visibleImages: visibleImages,
      textLen: textLen,
      samples: samples,
      topTag: topTag,
      topRole: topRole || '',
      topPos: topPos || '',
    };
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
})())
"""


async def capture_scrolling_screenshot(
    tab,
    *,
    max_height_px: int = 3000,
    quality: int = 50,
) -> bytes:
    """Capture a 'full-page-scroll' screenshot of the live tab, capped at
    ``max_height_px``.

    Uses CDP ``Page.captureScreenshot`` with ``captureBeyondViewport=True``
    + a ``clip`` rectangle so Chrome renders the area beyond the current
    viewport INTERNALLY -- the page is NOT actually scrolled, so the
    script's DOM state (focused element, hover, current scroll position,
    open menus) survives untouched. This matters for the codegen polling
    path where capture happens mid-script.

    Width  = the page's CSS layout width (css_content_size.width).
    Height = ``min(document_full_height, max_height_px)``. Infinite-scroll
             pages (where documentHeight grows on every scroll) are bounded
             by the cap so the JPEG never explodes.

    Returns raw JPEG bytes. On ANY CDP failure (rare; e.g. tab navigated
    mid-capture) we fall back to a plain viewport JPEG so callers always
    get something to save.
    """
    try:
        metrics = await tab.send(cdp.page.get_layout_metrics())
        # 6-tuple: (layoutVP, visualVP, contentSize, cssLayoutVP, cssVisualVP, cssContentSize)
        css_layout = metrics[3]
        css_content = metrics[5]
        # cssContentSize is a dom.Rect with width/height in CSS px.
        w = float(getattr(css_content, "width", 0) or 0)
        if w <= 0:
            w = float(getattr(css_layout, "client_width", 0) or 0)
        h_full = float(getattr(css_content, "height", 0) or 0)
        if h_full <= 0:
            h_full = float(getattr(css_layout, "client_height", 0) or 0)
        h = min(h_full, float(max_height_px))
        if w > 0 and h > 0:
            clip = cdp.page.Viewport(
                x=0.0, y=0.0, width=w, height=h, scale=1.0,
            )
            b64 = await tab.send(cdp.page.capture_screenshot(
                format_="jpeg",
                quality=int(quality),
                clip=clip,
                capture_beyond_viewport=True,
            ))
            return base64.b64decode(b64)
    except Exception:
        pass
    # Fallback: plain viewport JPEG (legacy behavior).
    b64 = await tab.send(cdp.page.capture_screenshot(
        format_="jpeg", quality=int(quality),
    ))
    return base64.b64decode(b64)


async def probe_occlusion(tab) -> dict:
    """Probe the live DOM for a full-screen blocking overlay (login / age /
    consent / paywall modal). Returns a small structural report:

      {coverage, dominance, scrollLock, ariaModal, inertMain, hasPassword,
       visibleImages, textLen, samples, topTag, topRole, topPos}

    or ``{"error": ...}`` / ``{}`` on failure. The hub turns this into the
    "課題" (review) classification (server/hub/_review.py); this function only
    measures. Best-effort -- never raises."""
    try:
        raw = await tab.evaluate(_DETECT_OVERLAY_JS)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        out = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


async def trigger_videos(tab) -> dict:
    """Find videos, play() new <video>s, click the center of visible ones.
    If nothing detected, click viewport center once."""
    raw = await tab.evaluate(_TRIGGER_VIDEOS_JS)
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"played": [], "to_click": [], "clicked": [],
                "total_video": 0, "total_iframe": 0,
                "fallback_click": None}

    clicked = []
    for t in result.get("to_click", []):
        try:
            await tab.mouse_click(t["x"], t["y"])
            clicked.append(t)
        except Exception as e:
            t["click_error"] = str(e)
            clicked.append(t)
    result["clicked"] = clicked

    if (result.get("total_video", 0) == 0
            and result.get("total_iframe", 0) == 0):
        try:
            raw_fb = await tab.evaluate(_FALLBACK_CLICK_JS)
            fb = json.loads(raw_fb) if raw_fb and raw_fb != "null" else None
            if fb:
                await tab.mouse_click(fb["x"], fb["y"])
                result["fallback_click"] = fb
        except Exception as e:
            result["fallback_error"] = str(e)

    return result


# Force every lazy-loaded image to fetch NOW, before the scroll loop
# starts. Many sites (WordPress with image-optimisation plugins, AMP
# pages, custom React layouts) wrap their AVIF/WebP/JPEG into one of
# these patterns:
#
#   <img src="/blank.gif" data-src="/real.avif" loading="lazy">
#   <img data-srcset="/sm.avif 360w, /lg.avif 720w">
#   <source data-srcset="/real.avif" type="image/avif">
#   <div data-src="/real.avif" class="lazyload">
#
# The browser never fires the GET for the real URL until a JS lazy-load
# library swaps ``data-src`` → ``src`` on intersection. Our 50px scroll
# loop is fast enough that the IntersectionObserver callbacks haven't
# fired by the time we move past the element, so the AVIF / new-format
# images never appear in the network listener (job 4b9aff01bc6f was the
# canonical case: 8 .avif URLs in the HTML, 0 captured).
#
# This snippet walks every commonly-used lazy-load attribute name and
# promotes it to the real one, then strips the native loading="lazy"
# hint so Chrome's own lazy-load doesn't re-defer it. It also flips
# ``content-visibility:auto`` (the modern CSS way to defer offscreen
# painting) back to visible so the layout commits and CSS background-
# images render. Idempotent: running it twice is a no-op since the
# attributes get cleared after promotion.
#
# Trade-off: forcing every image to load EAGERLY bloats memory on
# infinite-scroll feeds (Twitter / Reddit) and triggers data caps on
# image-heavy galleries. Opt-out via env var when this hurts more
# than it helps -- see _FORCE_LAZY_LOAD_ENABLED below.
_FORCE_LAZY_LOAD_JS = r"""
(() => {
  const promoted = {imgSrc: 0, imgSrcset: 0, sourceSrcset: 0, bg: 0};
  // 1) <img> / <source> with data-src / data-srcset / data-lazy-src
  //    / data-original (common across lazyload libraries).
  const dataAttrs = ['data-src', 'data-lazy-src', 'data-original',
                     'data-lazy', 'data-image', 'data-img'];
  document.querySelectorAll('img, source').forEach(el => {
    for (const a of dataAttrs) {
      const v = el.getAttribute(a);
      if (v && el.tagName === 'IMG' && !el.src.endsWith(v)) {
        el.src = v;
        promoted.imgSrc++;
        break;
      }
    }
    const ss = el.getAttribute('data-srcset') || el.getAttribute('data-lazy-srcset');
    if (ss) {
      el.srcset = ss;
      if (el.tagName === 'SOURCE') promoted.sourceSrcset++;
      else promoted.imgSrcset++;
    }
    // Strip the native lazy hint -- Chrome's own scheduler would
    // otherwise still defer until the element scrolls in.
    if (el.getAttribute('loading') === 'lazy') {
      el.removeAttribute('loading');
    }
  });
  // 2) Background-image lazy patterns: data-bg / data-background-image.
  document.querySelectorAll('[data-bg], [data-background-image], [data-background]').forEach(el => {
    const v = el.getAttribute('data-bg') ||
              el.getAttribute('data-background-image') ||
              el.getAttribute('data-background');
    if (v) {
      el.style.backgroundImage = `url('${v}')`;
      promoted.bg++;
    }
  });
  // 3) Modern CSS content-visibility:auto defers paint -> defers
  //    background-image fetch. Flip to visible so the layout commits.
  document.querySelectorAll('[style*="content-visibility"]').forEach(el => {
    el.style.contentVisibility = 'visible';
  });
  // 4) Force-fetch images the browser won't auto-request on a passive
  //    fetch: CSS background-image on offscreen / unpainted elements
  //    (e.g. a jwplayer ".jw-preview" poster set via inline style),
  //    <meta og:image/twitter:image> (metadata -- never fetched), and
  //    poster / data-poster attrs. new Image() fires a real GET so the
  //    network listener captures + saves it; the asset idle-wait below
  //    keeps the capture window open until these land. Same-origin GETs
  //    carry the page as referer (what cover CDNs expect).
  const _abs = (u) => { try { return new URL(u, location.href).href; } catch (_) { return null; } };
  const _urls = new Set();
  const _add = (u) => { const a = _abs(u); if (a && /^https?:/i.test(a)) _urls.add(a); };
  // 4a) computed background-image on every element (inline jw-preview
  //     poster + any CSS-class background-image). Capped so a giant DOM
  //     can't stall the fetch.
  try {
    let _n = 0;
    for (const el of document.querySelectorAll('*')) {
      if (++_n > 8000) break;
      let bg = '';
      try { bg = getComputedStyle(el).backgroundImage || ''; } catch (_) { continue; }
      if (!bg || bg === 'none') continue;
      const re = /url\((["']?)([^"')]+)\1\)/gi;
      let m;
      while ((m = re.exec(bg)) !== null) { if (m[2]) _add(m[2]); }
    }
  } catch (_) {}
  // 4b) og:image / twitter:image meta (cover poster; metadata-only).
  document.querySelectorAll(
    'meta[property="og:image"], meta[property="og:image:url"], ' +
    'meta[name="twitter:image"], meta[name="twitter:image:src"]'
  ).forEach(m => _add(m.getAttribute('content') || ''));
  // 4c) poster / data-poster attributes (<video poster>, lazy posters).
  document.querySelectorAll('[poster], [data-poster]').forEach(el => {
    _add(el.getAttribute('poster') || '');
    _add(el.getAttribute('data-poster') || '');
  });
  let forcedBg = 0;
  for (const u of _urls) {
    try { const im = new Image(); im.src = u; forcedBg++; } catch (_) {}
  }
  promoted.forcedBg = forcedBg;
  return JSON.stringify(promoted);
})()
"""


# Env-var opt-out for the lazy-load force-fire. Enabled by default
# because the common case (operator wants every asset on the page,
# even the lazy ones) is what fetch mode is FOR. Disable when the
# operator hits a site whose lazy-loading semantics break under
# eager promotion (rare, but e.g. paywall sites whose lazyload also
# enforces a click-to-load gate).
_FORCE_LAZY_LOAD_ENABLED = os.environ.get(
    "PAPRIKA_FORCE_LAZY_LOAD", "1",
).strip().lower() not in ("0", "false", "no", "off", "")


# Read the live hls.js / Plyr player instance to recover the AUTHORITATIVE
# HLS manifest + every quality variant. Modern JAV/streaming sites hand
# hls.js a master playlist and play through a blob: URL; the network only
# shows whatever variant the player happened to fetch (often a low-quality
# muted-autoplay PREVIEW from a decoy CDN), so the passive network sniff
# misses the real master and the top quality. hls.js has already parsed
# the master into ``instance.url`` (master) and ``instance.levels[]``
# (each variant's ``.url`` + resolution), so reading the live instance is
# the canonical, CORS-free way to enumerate every stream. Generalised:
# probes window.hls, Plyr (window.player.hls), any global that quacks like
# an Hls instance (has .url + .levels[]), plus <video>/<source> currentSrc.
# Only http(s) manifest/video URLs are returned (blob: excluded), so the
# results drop straight into video_urls_seen -> pick_stream_urls -> yt-dlp.
_HLS_INSTANCE_JS = r"""
JSON.stringify((() => {
  const out = [];
  const _ok = u => typeof u === 'string'
    && /^https?:/.test(u)
    && /\.(m3u8|mpd|mp4|webm|m4v|mov)(\?|$)/i.test(u);
  const push = u => { if (_ok(u)) out.push(u); };
  const harvest = h => {
    if (!h || typeof h !== 'object') return;
    try { push(h.url); } catch (_) {}
    try { (h.levels || []).forEach(l => { if (l) push(l.url); }); } catch (_) {}
  };
  try {
    const cands = [];
    try { if (window.hls) cands.push(window.hls); } catch (_) {}
    try { if (window.player && window.player.hls) cands.push(window.player.hls); } catch (_) {}
    // Scan globals for anything that quacks like an Hls instance.
    for (const k of Object.getOwnPropertyNames(window)) {
      try {
        const v = window[k];
        if (v && typeof v === 'object'
            && typeof v.url === 'string'
            && Array.isArray(v.levels)) {
          cands.push(v);
        }
      } catch (_) {}
    }
    cands.forEach(harvest);
  } catch (_) {}
  // Direct <video>/<source> http manifests (blob: is excluded by _ok).
  try {
    document.querySelectorAll('video, source').forEach(v => {
      push(v.currentSrc || ''); push(v.src || '');
    });
  } catch (_) {}
  return [...new Set(out)];
})())
"""

_HLS_INSTANCE_PROBE_ENABLED = os.environ.get(
    "PAPRIKA_HLS_INSTANCE_PROBE", "1",
).strip().lower() not in ("0", "false", "no", "off", "")


async def _scroll_page(
    tab,
    step_px: int,
    max_px: int,
    delay: float,
    log: LogFn,
) -> None:
    total = 0
    while total < max_px:
        at_bottom = await tab.evaluate(
            "(window.innerHeight + window.scrollY) "
            ">= (document.documentElement.scrollHeight - 1)"
        )
        if at_bottom:
            log(f"  ... scrolled {total}px, reached bottom.")
            return
        await tab.evaluate(f"window.scrollBy(0, {step_px})")
        total += step_px
        await asyncio.sleep(delay)
    log(f"  ... scrolled {total}px (max-px cap reached).")


def _format_video_url(url: Optional[str]) -> str:
    if not url:
        return "(no src)"
    if url.startswith("blob:"):
        return f"{url}  [BLOB - MSE stream, use yt-dlp]"
    return url


def _format_video_report(data: dict, network_video_urls: list[str]) -> list[str]:
    """Return the report as lines (caller decides how to emit)."""
    lines: list[str] = []
    videos = data.get("videos", [])
    iframes = data.get("iframes", [])

    if not videos and not iframes and not network_video_urls:
        lines.append("\n=== Video detection: none found ===")
        return lines

    lines.append("\n=== Video detection ===")
    if videos:
        lines.append(f"  <video> tags: {len(videos)}")
        for i, v in enumerate(videos, 1):
            dur = f"{v['duration']:.1f}s" if v.get("duration") else "?"
            lines.append(
                f"  [{i}] src={_format_video_url(v['src'])}  "
                f"duration={dur}  autoplay={v['autoplay']}"
            )
            for s in v.get("sources", []):
                lines.append(
                    f"      <source> {_format_video_url(s['src'])}  ({s['type']})"
                )
    if iframes:
        lines.append(f"  embedded players (iframe): {len(iframes)}")
        for i, f in enumerate(iframes, 1):
            lines.append(f"  [{i}] {f['src']}  [{f['host']}]")
        lines.append(
            "  -> embedded players: use yt-dlp on the iframe src directly"
        )
    if network_video_urls:
        lines.append(
            f"  video network requests captured: {len(network_video_urls)}"
        )
        for url in network_video_urls[:10]:
            lines.append(f"      {url}")
        if len(network_video_urls) > 10:
            lines.append(
                f"      ... and {len(network_video_urls) - 10} more"
            )
    return lines


# ----------------------------------------------------------------------------
# Public API: FetchOptions, FetchResult, fetch()
# ----------------------------------------------------------------------------

@dataclass
class FetchOptions:
    """All knobs exposed by the CLI/API, with reasonable defaults."""
    url: str
    wait_seconds: int = 20
    settle_seconds: float = 0.0
    idle_seconds: float = 3.0
    max_wait_seconds: float = 60.0
    scroll: bool = False
    scroll_step: int = 50
    scroll_max: int = 3000
    scroll_early_after: float = 5.0
    post_click_seconds: float = 5.0
    # When True, install iframe + nested-iframe deep network trace via
    # CDP Target.setAutoAttach so cross-origin video players' HLS/DASH
    # manifest URLs land in this fetch's network_log. Mirrors the
    # session-mode flag of the same name. Plumbed from
    # JobOptions.download_video at the /jobs Fetch dispatch site.
    download_video: bool = False
    # When True (worker fetch path), DETECT video streams during capture
    # but do NOT run the (often 10+ min) yt-dlp download inline. The
    # chosen targets are returned on FetchResult.deferred_video_targets
    # so the caller can release the lane and run the download in a
    # detached background task (the job's "downloading" phase). Default
    # False keeps the legacy inline-download behaviour.
    defer_video_download: bool = False
    cookies_from: Optional[str] = None
    referer: Optional[str] = None
    user_data_dir: Optional[Path] = None
    attach_host: Optional[str] = None
    attach_port: Optional[int] = None
    # When attaching, open the page in a new tab (True, default — safe for
    # user-attached Chrome that may have other tabs) or reuse the existing
    # first tab (False — set by lane-pool mode where the Chrome is dedicated).
    attach_new_tab: bool = True
    keep_open: bool = False
    headless: bool = False
    assets_dir: Optional[Path] = None
    # Logger: called for each user-facing log line. Server can override.
    log: LogFn = field(default=default_log)
    # Cookies to install via CDP Network.setCookies BEFORE navigating
    # to ``url``. List of CDP CookieParam dicts (name/value/domain/path
    # /expires/secure/httpOnly/sameSite/...). When empty/None the step
    # is skipped silently. Used by the hub's per-host registry path:
    # if the operator has saved cookies for the URL's host, they ride
    # along on the JobAssign so the first request carries them.
    cookies_to_install: Optional[list[dict]] = None
    # Drop captured assets smaller than this many bytes (0 = no
    # filter). Useful for skipping decorative icons / 1px trackers /
    # CSS sprite slivers without manual curation. The check happens
    # right after the response body is fetched, so the bytes don't
    # land on disk and don't fire ``on_saved`` upload callbacks.
    min_asset_size_bytes: int = 0
    # Asset URL blacklist (V): substring deny list. Any media response
    # whose URL contains one of these is silently dropped (not saved,
    # not yt-dlp'd). Match is case-insensitive substring. Same source
    # as HubAssignJob.asset_url_blacklist on the worker session path.
    asset_url_blacklist: list[str] = field(default_factory=list)
    # Async callback fired right before fetch returns, given the full
    # post-fetch cookie jar (CDP Cookie dicts). Used by the worker to
    # auto-upsert any cookies the page set during this fetch back into
    # the host registry. None disables the dump step.
    on_complete_dump_cookies: Optional[Any] = None  # Awaitable cb
    # Async callback fired once the browser is attached and the tab is
    # set up (network.enable done, cookies installed) but BEFORE the
    # first navigation. Receives ``(browser, tab)``. Used by the
    # worker to register this fetch as a read-only session in
    # ``self._sessions`` so /sessions/{id}/{cookies,outline,...}
    # works while the fetch is running. Errors are swallowed; a
    # callback failure must NOT cancel the fetch.
    on_browser_ready: Optional[Any] = None  # Awaitable cb(browser, tab)
    # Async callback fired right after the initial Page.navigate() has
    # resolved and the document is in a ready state, but BEFORE the
    # scroll / pre-video-trigger / asset-capture phase. Used by the
    # worker to apply HostRecord.fetch_recipes (cookie-banner clicks,
    # age-gate dismissal, play-button kick) so subsequent steps see the
    # page in its "operator-prepared" state. Receives ``(tab)``.
    # Errors are swallowed; a callback failure must NOT cancel the
    # fetch.
    on_after_navigate: Optional[Any] = None  # Awaitable cb(tab)
    # Async callback fired in the ``finally`` block, just before
    # ``browser.stop()``. Used by the worker to unregister the
    # fetch-owned session from ``self._sessions`` so subsequent
    # /sessions/{id}/* requests get a clean 404 instead of operating
    # on a soon-to-be-disconnected tab. No args.
    on_browser_closing: Optional[Any] = None  # Awaitable cb()
    # Shared list that the fetch fills with one dict per media response
    # seen on the wire: {url, mime, size, saved, document_url, timestamp}.
    # The worker passes the same list to the inspect SessionState so the
    # Live-panel "Network" tab can display traffic in real time. When
    # None the fetch simply skips the bookkeeping.
    network_log: Optional[list] = None
    # Async (or sync) callback fired once per asset, right after it has
    # been written to ``assets_dir``. Receives ``(path: Path, info: dict)``
    # where ``info`` matches the FetchResult.assets_saved entry
    # (name/path/size/url/mime). The worker uses this to upload each
    # captured asset to the hub *incrementally* so a mid-fetch failure
    # (worker disconnect, crash, hub restart) doesn't discard everything
    # captured up to that point -- the legacy behaviour was a single
    # batch upload only after fetch() returned successfully. Best-effort:
    # a callback error is logged and swallowed; it must never cancel the
    # fetch. None disables incremental upload.
    on_asset_saved: Optional[Any] = None  # cb(path, info) -> None | Awaitable


@dataclass
class FetchResult:
    """Everything the caller might want after a fetch."""
    html: str
    assets_saved: list[dict] = field(default_factory=list)
    assets_failed: int = 0
    video_detection: dict = field(default_factory=lambda: {"videos": [], "iframes": []})
    # Representative image picked from the LIVE DOM at capture time
    # (OGP -> Twitter -> JSON-LD -> image_src -> largest <img> by true
    # naturalWidth*naturalHeight). Shipped to the hub as a meta.json
    # sidecar so /jobs/{id}/meta can return a real cover image instead
    # of the site logo. {} when nothing qualified / the probe failed.
    representative_image: dict = field(default_factory=dict)
    video_urls_seen: list[str] = field(default_factory=list)
    ytdlp_results: list[dict] = field(default_factory=list)
    # The expanded list of all iframe srcs we found (for diagnostics).
    iframe_srcs: list[str] = field(default_factory=list)
    # Populated only when FetchOptions.defer_video_download is True: the
    # video targets that WOULD have been downloaded inline, as a list of
    # {url, referer, label}. The caller (worker) runs these in a detached
    # background task after releasing the lane.
    deferred_video_targets: list[dict] = field(default_factory=list)
    # Occlusion / overlay report from the LIVE DOM at capture time: a grid
    # hit-test (what fraction of the viewport one top layer covers + owns),
    # background scroll-lock, standards-based modal markers (ARIA / top-layer),
    # a login password field, and visible-image / text scarcity. Purely
    # OBSERVATIONAL here -- the hub classifies it into the "課題" (review)
    # bucket when a full-screen login / age / modal wall blocked the content
    # (server/hub/_review.py). Structural + standards-based, NO site-specific
    # hardcoding. {} when the probe failed or wasn't run.
    occlusion: dict = field(default_factory=dict)
    # ② v2 eye: a single small viewport JPEG captured at end-of-fetch so the
    # hub's perception (vision LLM) has an image even on fetches that saved no
    # assets (auth wall / video-DL-fail). Raw bytes (core has no workdir); the
    # worker writes it to {job}/final.jpg. b"" when capture was off / failed.
    screenshot: bytes = b""


async def fetch(opts: FetchOptions) -> FetchResult:
    """The main worker. Drives nodriver, captures assets, runs yt-dlp if applicable."""

    log = opts.log
    url = opts.url
    assets_dir = opts.assets_dir
    referer = opts.referer
    keep_open = opts.keep_open

    attaching = opts.attach_host is not None and opts.attach_port is not None
    if attaching:
        # nodriver.Config.__init__ unconditionally calls find_chrome_executable()
        # which raises FileNotFoundError when Chrome isn't installed locally.
        # In attach mode we never spawn a Chrome — pass a placeholder so the
        # lookup is skipped. (Browser.start() also skips the executable
        # existence check when host/port are set, so this value is never run.)
        start_kwargs: dict[str, Any] = dict(
            host=opts.attach_host, port=opts.attach_port,
            browser_executable_path=sys.executable,
        )
        log(
            f"  ... ATTACH mode: connecting to existing Chrome at "
            f"{opts.attach_host}:{opts.attach_port}"
        )
        keep_open = True  # never close someone else's browser
    else:
        _spawn_args = ["--window-size=1920,1080", "--lang=ja-JP"]
        _egress_proxy = _worker_egress_proxy()
        if _egress_proxy:
            _spawn_args += [
                f"--proxy-server={_egress_proxy}",
                f"--proxy-bypass-list={_worker_proxy_bypass()}",
                # Stop WebRTC from leaking the real egress IP around the proxy.
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
            log(f"  ... egress via proxy {_egress_proxy}")
        start_kwargs = dict(
            headless=opts.headless,
            browser_args=_spawn_args,
        )
        if opts.user_data_dir is not None:
            start_kwargs["user_data_dir"] = str(opts.user_data_dir)
            log(f"  ... using Chrome user-data-dir: {opts.user_data_dir}")
    browser = await uc.start(**start_kwargs)

    # Grab the real User-Agent from the running Chrome so every HTTP
    # request we make outside the browser (HLS probes, fallback asset
    # downloads, etc.) carries the same UA string.
    global _BROWSER_USER_AGENT
    try:
        _ver = await browser.send(cdp.browser.get_version())
        # cdp.browser.get_version() returns a 5-tuple in this nodriver build
        # (protocolVersion, product, revision, userAgent, jsVersion) -- NOT an
        # object -- so `_ver.user_agent` raised AttributeError and we silently
        # fell back to a default UA. Token+UA-gated video CDNs then 403 every
        # fragment (the av01.media failure). Handle both shapes.
        _ua = getattr(_ver, "user_agent", None)
        if _ua is None and isinstance(_ver, (tuple, list)) and len(_ver) > 3:
            _ua = _ver[3]
        if _ua:
            _BROWSER_USER_AGENT = _ua
            log(f"  ... Chrome UA: {_BROWSER_USER_AGENT}")
        else:
            log(f"  !! Chrome UA unavailable (get_version -> {type(_ver).__name__})")
    except Exception as _e:
        log(f"  !! could not read Chrome UA: {_e}")

    # Chrome reports `ws://localhost:9222/...` in its /json/version response
    # regardless of how the client reached it. When attaching across
    # containers (or any non-loopback path), that hostname is wrong for the
    # caller. Rewrite the websocket URL on the browser before any WS opens.
    if attaching:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(browser.websocket_url)
            if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
                new_netloc = f"{opts.attach_host}:{opts.attach_port}"
                browser.websocket_url = browser.websocket_url.replace(
                    parsed.netloc, new_netloc, 1
                )
                if hasattr(browser, "info") and browser.info is not None:
                    try:
                        browser.info.webSocketDebuggerUrl = browser.websocket_url
                    except Exception:
                        pass
                log(
                    f"  ... rewrote browser.websocket_url to "
                    f"ws://{new_netloc}/..."
                )
        except Exception as e:
            log(f"  !! could not rewrite websocket_url: {e}")

    result = FetchResult(html="")

    async def _fire_on_saved(entry: dict) -> None:
        """Notify the caller that one asset just landed on disk, so it
        can upload it to the hub incrementally. Best-effort: never let a
        callback error abort the capture loop."""
        cb = opts.on_asset_saved
        if cb is None:
            return
        try:
            res = cb(Path(entry["path"]), entry)
            if inspect.isawaitable(res):
                await res
        except Exception as _cb_exc:
            log(
                f"  (on_asset_saved cb failed for {entry.get('name')}: "
                f"{type(_cb_exc).__name__}: {_cb_exc})"
            )

    try:
        if attaching:
            if opts.attach_new_tab:
                tab = await browser.get("about:blank", new_tab=True)
                log(
                    "  ... opened a new tab in the attached browser "
                    "(your existing tabs are untouched)"
                )
            else:
                # Lane-pool mode: reuse the dedicated Chrome's existing tab.
                # Aggressively close every OTHER tab first so popups /
                # ad windows / stale tabs from previous jobs on this
                # lane don't accumulate in the noVNC viewer.
                try:
                    await _force_single_page_target(browser, log=log)
                except Exception as e:
                    log(f"  !! pre-fetch tab cleanup failed: {e}")
                tab = await browser.get("about:blank", new_tab=False)
                log(
                    "  ... reusing lane's existing tab "
                    "(navigation replaces previous job's page)"
                )
        else:
            tab = await browser.get("about:blank")

        metadata: dict = {}
        in_flight = 0
        last_activity = time.monotonic()
        # Network-log bookkeeping: shared with the inspect SessionState
        # so the Live-panel "Network" tab can display traffic in real
        # time while the fetch is running.
        _net_log: list = opts.network_log if opts.network_log is not None else []
        _net_logged_urls: set = set()

        # URL blacklist (V + Y): operator-managed deny list. Supports
        # substring / glob / regex (see core/url_blacklist.py for syntax).
        # Compiled once before the on_response hot path.
        from core.url_blacklist import (
            compile_blacklist as _compile_blacklist,
            is_manifest_url as _is_manifest_url,
            pattern_targets_manifests as _pattern_targets_manifests,
        )
        _fetch_bl_matcher = _compile_blacklist(opts.asset_url_blacklist or ())

        def _fetch_blacklisted(u: str) -> str | None:
            return _fetch_bl_matcher.match(u)

        async def on_response(event: cdp.network.ResponseReceived):
            nonlocal in_flight, last_activity
            server_mime = (event.response.mime_type or "").lower()
            evt_url = event.response.url or ""
            # Skip non-HTTP(S) URLs entirely. data: and blob: URIs surface
            # via iframe deep-trace (Chrome fires ResponseReceived for them
            # too) but they can't be fetched separately and their base64
            # payloads would bloat _net_log and the metadata dict.
            if not evt_url.startswith(("http://", "https://")):
                return
            # Blacklist gate: drop matching URLs before any save/yt-dlp
            # decision. Logged once via the network log entry below would
            # leak the URL into operator view, so silent skip is correct.
            #
            # Manifest passthrough (2026-06-14): a GENERAL pattern like
            # ``*.saawsedge.com*`` (intended for .ts/.mp4 segment noise)
            # would otherwise silently drop the main video's .m3u8
            # manifest -- yt-dlp then fell back to iframe-generic ad URLs
            # and produced no video (job 63f9bf436c2f post-mortem).
            # Manifest URLs (.m3u8/.mpd) are tracked into
            # video_urls_seen even when blacklisted by a general pattern;
            # a manifest-specific pattern (literal ``.m3u8``/``.mpd``)
            # still wins so ``*/trailer*.m3u8`` keeps blocking.
            _bl_pat = _fetch_blacklisted(evt_url)
            if _bl_pat is not None:
                if (_is_manifest_url(evt_url)
                        and not _pattern_targets_manifests(_bl_pat)):
                    if evt_url not in result.video_urls_seen:
                        result.video_urls_seen.append(evt_url)
                    return
                log(f"  BLOCK (blacklist={_bl_pat!r}) {evt_url[:120]}")
                return
            # _effective_mime falls back to URL extension when the
            # server returns no / generic Content-Type -- Cloudflare-
            # fronted WordPress is the canonical AVIF case.
            mime = _effective_mime(server_mime, evt_url)
            if mime.startswith("video/") or re.search(
                r"\.(mp4|webm|m3u8|mpd|mov|m4v|ts)(\?|$)", evt_url, re.I
            ):
                if evt_url not in result.video_urls_seen:
                    result.video_urls_seen.append(evt_url)
            # Append to network_log for the Live-panel Network tab.
            is_interesting = any(
                mime.startswith(p)
                for p in ("image/", "audio/", "video/", "font/")
            )
            if is_interesting and evt_url not in _net_logged_urls:
                _net_logged_urls.add(evt_url)
                content_length = None
                try:
                    for h in (event.response.headers or {}):
                        if h.lower() == "content-length":
                            content_length = int(event.response.headers[h])
                            break
                except Exception:
                    pass
                _net_log.append({
                    "url": evt_url,
                    "mime": mime,
                    "size": content_length,
                    "saved": False,
                    "document_url": "",
                    "timestamp": time.time(),
                })
            if any(mime.startswith(p) for p in SAVE_MIME_PREFIXES):
                metadata[event.request_id] = {
                    "url": event.response.url,
                    "mime": mime,
                }
                in_flight += 1
                last_activity = time.monotonic()

        async def on_finished(event: cdp.network.LoadingFinished):
            nonlocal in_flight, last_activity
            info = metadata.pop(event.request_id, None)
            if info is None:
                return
            try:
                body, is_b64 = await tab.send(
                    cdp.network.get_response_body(event.request_id)
                )
            except Exception as e:
                # CDP -32000 "No resource with given identifier found" means
                # the browser tracked the request but its response body was
                # already evicted from Chrome's internal cache.  This is
                # common for resources loaded inside iframes: the outer CDP
                # target observes the ResponseReceived event but the body
                # lives in the iframe's browsing context and is unreachable
                # via the main target's getResponseBody. Fall back to a
                # direct HTTP fetch so we still save the asset.
                err_str = str(e)
                _is_cache_miss = (
                    (
                        "-32000" in err_str
                        or "No resource with given identifier" in err_str
                    )
                    and info["url"].startswith(("http://", "https://"))
                )
                if assets_dir is not None and _is_cache_miss:
                    try:
                        from core.httpclient import make_async_client

                        # Pull cookies for the resource's domain out of
                        # Chrome so authenticated/region-locked assets work.
                        _jar: dict[str, str] = {}
                        try:
                            _raw_cookies = await tab.send(
                                cdp.network.get_cookies(
                                    urls=[info["url"]]
                                )
                            )
                            for _c in (_raw_cookies or []):
                                try:
                                    _jar[_c.name] = _c.value
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        _fb_headers = {
                            "User-Agent": _get_user_agent(),
                            "Referer": url,
                            "Accept": (
                                "image/avif,image/webp,image/apng,"
                                "image/svg+xml,image/*,*/*;q=0.8"
                            ),
                        }
                        async with make_async_client(
                            follow_redirects=True,
                            timeout=30.0,
                            headers=_fb_headers,
                            cookies=_jar,
                        ) as _client:
                            _resp = await _client.get(info["url"])
                        if _resp.status_code == 200:
                            _data = _resp.content
                            if (
                                opts.min_asset_size_bytes
                                and len(_data) < opts.min_asset_size_bytes
                            ):
                                log(
                                    f"  SKIP {info['url']}: "
                                    f"{len(_data)/1024:.1f}KB < min "
                                    f"{opts.min_asset_size_bytes/1024:.1f}KB"
                                )
                                return
                            _fb_mime = info["mime"] or (
                                _resp.headers.get("content-type", "").split(";")[0].strip()
                            )
                            _fb_name = _filename_from(
                                info["url"], _fb_mime,
                                f"resource_{len(result.assets_saved)}"
                            )
                            _fb_path = _unique_path(assets_dir, _fb_name)
                            await asyncio.to_thread(_fb_path.write_bytes, _data)
                            result.assets_saved.append({
                                "name": _fb_path.name,
                                "path": str(_fb_path.resolve()),
                                "size": len(_data),
                                "url": info["url"],
                                "mime": _fb_mime,
                            })
                            for _entry in reversed(_net_log):
                                if _entry["url"] == info["url"]:
                                    _entry["size"] = len(_data)
                                    _entry["saved"] = True
                                    break
                            log(
                                f"  SAVED (http-fallback) "
                                f"[{len(_data)/1024:>8.1f} KB] "
                                f"{_fb_path.resolve()}"
                            )
                            await _fire_on_saved(result.assets_saved[-1])
                            return
                        else:
                            log(
                                f"  SKIP {info['url']}: "
                                f"CDP evicted + fallback HTTP {_resp.status_code}"
                            )
                            result.assets_failed += 1
                            return
                    except Exception as _fb_exc:
                        log(
                            f"  SKIP {info['url']}: "
                            f"CDP evicted + fallback failed "
                            f"({type(_fb_exc).__name__}: {_fb_exc})"
                        )
                        result.assets_failed += 1
                        return
                log(f"  SKIP {info['url']}: {e}")
                result.assets_failed += 1
                return
            finally:
                in_flight = max(0, in_flight - 1)
                last_activity = time.monotonic()

            data = base64.b64decode(body) if is_b64 else body.encode("utf-8")
            # Min-size filter -- drop "decorative" assets the operator
            # never cares about (1px trackers, tiny CSS sprite
            # slivers, icon SVGs). 0 disables. Counts the decoded
            # size, not the encoded transfer size, so a small body
            # gzip'd in transit is still skipped if its real bytes
            # don't make the threshold.
            if (
                opts.min_asset_size_bytes
                and len(data) < opts.min_asset_size_bytes
            ):
                log(
                    f"  SKIP {info['url']}: "
                    f"{len(data)/1024:.1f}KB < min "
                    f"{opts.min_asset_size_bytes/1024:.1f}KB"
                )
                return
            name = _filename_from(
                info["url"], info["mime"],
                f"resource_{len(result.assets_saved)}"
            )
            path = _unique_path(assets_dir, name)  # type: ignore[arg-type]
            await asyncio.to_thread(path.write_bytes, data)
            result.assets_saved.append({
                "name": path.name,
                "path": str(path.resolve()),
                "size": len(data),
                "url": info["url"],
                "mime": info["mime"],
            })
            # Update network_log entry with actual body size + saved flag.
            for entry in reversed(_net_log):
                if entry["url"] == info["url"]:
                    entry["size"] = len(data)
                    entry["saved"] = True
                    break
            log(f"  SAVED [{len(data)/1024:>8.1f} KB] {path.resolve()}")
            await _fire_on_saved(result.assets_saved[-1])

        async def on_failed(event: cdp.network.LoadingFailed):
            nonlocal in_flight, last_activity
            info = metadata.pop(event.request_id, None)
            if info is None:
                return
            log(f"  SKIP {info['url']}: load failed ({event.error_text})")
            result.assets_failed += 1
            in_flight = max(0, in_flight - 1)
            last_activity = time.monotonic()

        if assets_dir is not None:
            assets_dir.mkdir(parents=True, exist_ok=True)
            tab.handlers[cdp.network.ResponseReceived].append(on_response)
            tab.handlers[cdp.network.LoadingFinished].append(on_finished)
            tab.handlers[cdp.network.LoadingFailed].append(on_failed)
            await tab.send(cdp.network.enable(
                max_total_buffer_size=1536 * 1024 * 1024,
                max_resource_buffer_size=512 * 1024 * 1024,
            ))
            # iframe + nested-iframe deep network trace. ON whenever we
            # are capturing assets (assets_dir is not None, guaranteed
            # here) so cross-origin iframe resources -- thumbnail images,
            # preview screenshots from DMM litevideo, etc. -- surface via
            # on_response just like main-frame assets.
            # Previously gated on download_video=True only; broadened so
            # plain asset-capture fetches also see sub-frame network events.
            # The HTTP-fallback in on_finished handles the -32000 "body
            # evicted from cache" case that sub-frame responses often hit.
            try:
                from server.worker.browser_ops import (
                    install_iframe_deep_trace as _install_iframe_deep_trace,
                )
                await _install_iframe_deep_trace(tab, log=log)
            except Exception as e:
                log(
                    f"  !! iframe deep-trace install failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )
            # Same-origin iframe fetch/XHR hook -- complements the CDP
            # deep-trace above for cross-origin iframes.  Some sites
            # (e.g. 7mmtv.sx → play.php iframe with hls.js) hide their
            # HLS manifest fetch inside a same-origin iframe whose XHR
            # doesn't surface in Network.responseReceived. The hook
            # captures every fetch/XHR URL into a global bucket which
            # the poller below reads + feeds into _net_log so the
            # Live-panel Network tab shows them.
            try:
                from server.worker.browser_ops import (
                    install_url_capture_hook as _install_url_capture_hook,
                    read_url_capture as _read_url_capture,
                )
                await _install_url_capture_hook(tab, log=log)
            except Exception as e:
                log(
                    f"  !! url-capture hook install failed "
                    f"(non-fatal): {type(e).__name__}: {e}"
                )
                _read_url_capture = None  # type: ignore
            # Background poller -- mirrors the session-mode poller in
            # browser_ops.install_session_asset_capture.
            _hook_seen_urls: set = set()
            _hook_poll_n = [0]
            _hook_total = [0]
            # Cursor into _net_log: index of the first entry not yet
            # streamed to the Live panel's Network tab (see netcap emit
            # below). Covers BOTH capture surfaces (on_response + this
            # hook) since both append to the shared _net_log.
            _net_emit_idx = [0]

            async def _fetch_url_capture_poller():
                await asyncio.sleep(2.0)
                while True:
                    try:
                        captured = await _read_url_capture(tab)
                    except Exception as _e:
                        log(f"  [url-capture] poller exiting: {_e}")
                        return
                    _hook_poll_n[0] += 1
                    if captured:
                        _hook_total[0] += len(captured)
                        log(
                            f"  [url-capture] poll #{_hook_poll_n[0]}: "
                            f"+{len(captured)} URL(s) (total: {_hook_total[0]})"
                        )
                        for _ent in captured[:5]:
                            log(
                                f"    [url-capture] "
                                f"{_ent.get('api','?')} "
                                f"{(_ent.get('url') or '')[:160]}"
                            )
                    elif _hook_poll_n[0] in (5, 20, 40):
                        installs = getattr(
                            _read_url_capture, "_last_installs", "?"
                        )
                        log(
                            f"  [url-capture] poll #{_hook_poll_n[0]}: "
                            f"alive, bucket empty "
                            f"(hook_installs in page: {installs})"
                        )
                    for entry in captured:
                        u = entry.get("url") or ""
                        if not u or u in _hook_seen_urls:
                            continue
                        _hook_seen_urls.add(u)
                        # Blacklist gate (Y bugfix): the JS fetch/XHR
                        # hook is a separate capture surface that
                        # bypasses the CDP on_response gate. Without
                        # this check, `https://*.saawsedge.com*` etc.
                        # are still leaked into network_log + video_urls_seen
                        # via this poller (job 9dc8d38174e4 post-mortem).
                        # Manifest passthrough (2026-06-14): see the
                        # on_response gate above; identical rationale.
                        _bl_hit = _fetch_blacklisted(u)
                        if _bl_hit is not None:
                            if (_is_manifest_url(u)
                                    and not _pattern_targets_manifests(_bl_hit)):
                                if u not in result.video_urls_seen:
                                    result.video_urls_seen.append(u)
                                continue
                            log(f"  [url-capture] BLOCK (blacklist={_bl_hit!r}) {u[:120]}")
                            continue
                        # Record to network_log so the Live panel
                        # shows the URL (mirrors on_response).
                        if u not in _net_logged_urls:
                            _net_logged_urls.add(u)
                            _net_log.append({
                                "url": u,
                                "mime": "",
                                "size": None,
                                "saved": False,
                                "document_url": "",
                                "source": "iframe_xhr_hook",
                                "timestamp": time.time(),
                            })
                        # Track video URLs the same way on_response does
                        # so download_video heuristics can use them.
                        if re.search(
                            r"\.(mp4|webm|m3u8|mpd|mov|m4v|ts)(\?|$)", u, re.I,
                        ):
                            if u not in result.video_urls_seen:
                                result.video_urls_seen.append(u)
                    # Stream the batch of newly-captured network entries to
                    # the Live panel's Network tab as an EPHEMERAL delta that
                    # rides the WorkerJobLog channel (hub broadcasts, does not
                    # persist). One emit per poll cycle keeps it cheap and
                    # replaces the page.network() pull that 504s on streaming
                    # pages. Literal prefix must match
                    # server.protocol.NET_CAPTURE_MARKER.
                    if len(_net_log) > _net_emit_idx[0]:
                        _net_delta = _net_log[_net_emit_idx[0]:]
                        _net_emit_idx[0] = len(_net_log)
                        try:
                            log("[[paprika:netcap]] " + json.dumps(
                                {"net": [
                                    {
                                        "url": _e.get("url", ""),
                                        "mime": _e.get("mime", ""),
                                        "size": _e.get("size"),
                                        "saved": bool(_e.get("saved")),
                                        "source": _e.get("source", ""),
                                    }
                                    for _e in _net_delta
                                    if isinstance(_e, dict) and _e.get("url")
                                ]},
                                ensure_ascii=False,
                            ))
                        except Exception:
                            pass
                    try:
                        await asyncio.sleep(1.5)
                    except asyncio.CancelledError:
                        return

            _fetch_url_capture_task = None
            if _read_url_capture is not None:
                try:
                    _fetch_url_capture_task = asyncio.create_task(
                        _fetch_url_capture_poller()
                    )
                except Exception as e:
                    log(f"  !! url-capture poller spawn failed: {e}")
            if referer:
                await tab.send(cdp.network.set_extra_http_headers(
                    cdp.network.Headers({"Referer": referer})
                ))
                log(f"  ... custom Referer set: {referer}")
            # Auto-injected host cookies (hub-side host registry path).
            # MUST run after Network.enable() and BEFORE Page.navigate()
            # so the first request from this navigation already carries
            # the session. Best-effort: an injection failure shouldn't
            # nuke the fetch -- the page may still load anonymously.
            if opts.cookies_to_install:
                try:
                    params = _to_cdp_cookie_params(opts.cookies_to_install)
                    if params:
                        await tab.send(cdp.network.set_cookies(cookies=params))
                        log(
                            f"  ... installed {len(params)} cookie(s) "
                            f"from host registry before navigation "
                            f"({len(opts.cookies_to_install) - len(params)} dropped as invalid)"
                            if len(params) < len(opts.cookies_to_install)
                            else
                            f"  ... installed {len(params)} cookie(s) "
                            f"from host registry before navigation"
                        )
                    else:
                        log("  !! all auto-inject cookies were invalid; continuing without")
                except Exception as e:
                    log(
                        f"  !! cookie auto-injection failed "
                        f"({type(e).__name__}: {e}); continuing without"
                    )
            # Browser is attached, tab is hooked, cookies are installed.
            # Let the caller register this run as an inspectable session
            # BEFORE we kick off the navigation, so a fast operator hitting
            # /sessions/{id}/cookies while the first request is in flight
            # already gets a valid response.
            if opts.on_browser_ready is not None:
                try:
                    await opts.on_browser_ready(browser, tab)
                except Exception as e:
                    log(
                        f"  !! on_browser_ready callback failed "
                        f"({type(e).__name__}: {e}); continuing"
                    )
            await tab.send(cdp.page.navigate(url))
        else:
            if referer or opts.cookies_to_install:
                try:
                    await tab.send(cdp.network.enable())
                except Exception:
                    pass
            if referer:
                try:
                    await tab.send(cdp.network.set_extra_http_headers(
                        cdp.network.Headers({"Referer": referer})
                    ))
                except Exception:
                    pass
            if opts.cookies_to_install:
                try:
                    params = _to_cdp_cookie_params(opts.cookies_to_install)
                    if params:
                        await tab.send(cdp.network.set_cookies(cookies=params))
                        log(
                            f"  ... installed {len(params)} cookie(s) "
                            f"from host registry before navigation "
                            f"({len(opts.cookies_to_install) - len(params)} dropped as invalid)"
                            if len(params) < len(opts.cookies_to_install)
                            else
                            f"  ... installed {len(params)} cookie(s) "
                            f"from host registry before navigation"
                        )
                    else:
                        log("  !! all auto-inject cookies were invalid; continuing without")
                except Exception as e:
                    log(
                        f"  !! cookie auto-injection failed "
                        f"({type(e).__name__}: {e}); continuing without"
                    )
            # Mirror of the assets-dir branch: tell the caller the
            # browser is ready to be inspected as a session.
            if opts.on_browser_ready is not None:
                try:
                    await opts.on_browser_ready(browser, tab)
                except Exception as e:
                    log(
                        f"  !! on_browser_ready callback failed "
                        f"({type(e).__name__}: {e}); continuing"
                    )
            await tab.get(url)

        ready_start = time.monotonic()
        ready_deadline = ready_start + opts.wait_seconds
        while time.monotonic() < ready_deadline:
            ready = await tab.evaluate("document.readyState")
            if ready == "complete":
                break
            if opts.scroll and opts.scroll_early_after > 0:
                elapsed = time.monotonic() - ready_start
                if elapsed >= opts.scroll_early_after:
                    scrollable = await tab.evaluate(
                        "document.documentElement.scrollHeight "
                        "> window.innerHeight"
                    )
                    if scrollable:
                        log(
                            f"  ... document not complete after {elapsed:.1f}s "
                            f"but page is scrollable; starting scroll early."
                        )
                        break
            await asyncio.sleep(0.1)

        if opts.settle_seconds > 0:
            log(
                f"  ... document ready. holding for {opts.settle_seconds:.1f}s "
                f"before checking idle."
            )
            await asyncio.sleep(opts.settle_seconds)

        # ---- per-host recipe (HostRegistry.fetch_recipes) ----
        # Runs deterministic action playbooks right after navigation
        # so cookie-banner / age-gate / play-button preparation is
        # already done by the time the scroll + capture loop starts.
        # Best-effort: a recipe crash must NOT kill the fetch.
        if opts.on_after_navigate is not None:
            try:
                await opts.on_after_navigate(tab)
            except Exception as e:
                log(
                    f"  !! on_after_navigate callback failed "
                    f"({type(e).__name__}: {e}); continuing"
                )

        def _emit_trigger(label: str, r: dict) -> bool:
            played = r.get("played", [])
            clicked = r.get("clicked", [])
            fb = r.get("fallback_click")
            if not played and not clicked and not fb:
                return False
            log(
                f"  ... {label}: played={len(played)} clicked={len(clicked)} "
                f"(found video={r.get('total_video', 0)} "
                f"iframe={r.get('total_iframe', 0)})"
            )
            for src in played:
                log(f"      PLAY  {src[:90]}")
            for t in clicked:
                if "click_error" in t:
                    log(
                        f"      CLICK [{t['tag']}] FAILED "
                        f"({t['click_error']}) {t['src'][:80]}"
                    )
                else:
                    log(
                        f"      CLICK [{t['tag']}] ({t['x']},{t['y']}) "
                        f"{t['src'][:80]}"
                    )
            if fb:
                log(
                    f"      CLICK [viewport-center] ({fb['x']},{fb['y']}) "
                    f"— no <video>/iframe found, kicking the player area"
                )
            return True

        # Promote every lazy-loaded image (data-src / data-srcset /
        # loading="lazy" / etc.) to the eager src so the network
        # listener sees the real URLs. Runs whether or not scroll is
        # on, because plenty of pages defer images that are already in
        # the viewport (think: lazy-load libraries that wait for
        # 'DOMContentLoaded + 200ms' rather than intersection). See
        # _FORCE_LAZY_LOAD_JS docstring for rationale + opt-out.
        if _FORCE_LAZY_LOAD_ENABLED:
            try:
                raw = await tab.evaluate(_FORCE_LAZY_LOAD_JS)
                # Parse the stats so we can surface them in the log --
                # operators care about "did this hurt the fetch?".
                import json as _json
                stats = _json.loads(raw or '{}') if raw else {}
                if any(stats.values()):
                    log(
                        f"  ... lazy-load force: img.src={stats.get('imgSrc',0)} "
                        f"img.srcset={stats.get('imgSrcset',0)} "
                        f"source.srcset={stats.get('sourceSrcset',0)} "
                        f"bg={stats.get('bg',0)} "
                        f"forced-bg/og/poster={stats.get('forcedBg',0)}"
                    )
            except Exception as e:
                log(f"  ... lazy-load force skipped ({type(e).__name__}: {e})")

        if opts.scroll:
            log(
                f"  ... scrolling page in {opts.scroll_step}px steps "
                f"(cap: {opts.scroll_max}px)"
            )
            await _scroll_page(
                tab, opts.scroll_step, opts.scroll_max, 0.1, log
            )
            # Second pass after scroll: many lazy-load libraries bind
            # their observers AFTER first DOMContentLoaded, so elements
            # the observer attached to during scroll only realise they
            # were intersected once we re-promote.
            if _FORCE_LAZY_LOAD_ENABLED:
                try:
                    await tab.evaluate(_FORCE_LAZY_LOAD_JS)
                except Exception:
                    pass

        if assets_dir is not None:
            log(
                f"  ... waiting up to {opts.max_wait_seconds:.0f}s "
                f"for assets to finish (idle threshold: "
                f"{opts.idle_seconds:.1f}s)"
            )
            deadline = time.monotonic() + opts.max_wait_seconds
            while time.monotonic() < deadline:
                idle_for = time.monotonic() - last_activity
                if in_flight == 0 and idle_for >= opts.idle_seconds:
                    log(f"  ... network idle for {idle_for:.1f}s, finishing.")
                    break
                await asyncio.sleep(0.5)
            else:
                log(
                    f"  ... reached max-wait ({opts.max_wait_seconds:.0f}s) "
                    f"with {in_flight} request(s) still in flight."
                )

        # tab.get_content() runs DOM.getDocument(depth=-1, pierce=True), making
        # Chrome CBOR-serialize the ENTIRE deeply-nested DOM tree. On
        # pathologically deep DOMs (e.g. www.bodogblog.com) Chrome's CBOR encoder
        # overflows its stack ("CBOR: stack limit exceeded" / -32000) and this
        # await -- the one finishing step that used to be UNWRAPPED -- crashed
        # the whole fetch, discarding every asset already captured above
        # (assets_saved=0 -> Fail). Make HTML capture best-effort like the other
        # finishing steps: on failure fall back to reading outerHTML as a STRING
        # via Runtime.evaluate (no node-tree serialization -> immune to the
        # depth/stack limit), and never let an HTML-capture error fail an
        # otherwise-successful fetch.
        try:
            result.html = await tab.get_content()
        except Exception as _gc_exc:
            log(
                f"  ... get_content() failed "
                f"({type(_gc_exc).__name__}: {str(_gc_exc)[:140]}); "
                f"falling back to outerHTML via evaluate"
            )
            try:
                _oh = await tab.evaluate("document.documentElement.outerHTML")
                result.html = _oh if isinstance(_oh, str) else (result.html or "")
            except Exception as _oh_exc:
                log(
                    f"  ... outerHTML fallback also failed "
                    f"({type(_oh_exc).__name__}); continuing without page.html"
                )
                result.html = result.html or ""

        # Pick the representative (cover/hero) image while the DOM is
        # still live -- this is the only point where true
        # naturalWidth*naturalHeight is readable for the largest-image
        # fallback. Best-effort: a failure here never fails the fetch.
        try:
            result.representative_image = await pick_representative_image(tab)
            _rep = result.representative_image or {}
            if _rep.get("url"):
                log(f"  representative image: {_rep['url']} (via {_rep.get('source')})")
        except Exception as e:
            log(f"  (representative-image pick failed: {e})")

        # 課題(review) signal: probe the live DOM for a full-screen blocking
        # overlay (login / age / consent / paywall modal). Structural +
        # standards-based (hit-test coverage + scroll-lock + ARIA modal +
        # visible-content scarcity) -- no site-specific hardcoding. Purely
        # observational here; the hub classifies + buckets the job. The DOM
        # is final at this point (settled, scrolled, recipe-applied), so any
        # auto-dismissed consent banner is already gone and won't false-fire.
        # Best-effort: a probe failure never fails the fetch.
        try:
            result.occlusion = await probe_occlusion(tab)
            _o = result.occlusion or {}
            if _o and not _o.get("error"):
                log(
                    f"  occlusion: coverage={_o.get('coverage')} "
                    f"dominance={_o.get('dominance')} lock={_o.get('scrollLock')} "
                    f"modal={_o.get('ariaModal')} pw={_o.get('hasPassword')} "
                    f"visImg={_o.get('visibleImages')} textLen={_o.get('textLen')}"
                )
        except Exception as e:
            log(f"  (occlusion probe failed: {type(e).__name__}: {e})")

        # ② v2 eye: capture a FULL-PAGE-SCROLL JPEG now, while tab is live
        # and the DOM is final (settled / scrolled / recipe-applied -- same
        # point as the occlusion probe). Stored on FetchResult.screenshot;
        # the worker persists it as {job}/final.jpg for the hub's perception
        # AND for the operator's "what did this page look like" view.
        # Capped at PAPRIKA_FETCH_SCREENSHOT_MAX_HEIGHT (default 3000 px) to
        # bound infinite-scroll pages. Best-effort + env kill-switch.
        if (os.environ.get("PAPRIKA_FETCH_SCREENSHOT", "1") or "1").strip().lower() not in ("0", "false", "no", "off"):
            try:
                _max_h = int(os.environ.get("PAPRIKA_FETCH_SCREENSHOT_MAX_HEIGHT", "3000") or 3000)
            except Exception:
                _max_h = 3000
            try:
                result.screenshot = await capture_scrolling_screenshot(
                    tab, max_height_px=_max_h, quality=50,
                )
            except Exception as e:
                log(f"  (screenshot capture failed: {type(e).__name__}: {e})")

        if assets_dir is not None:
            log(
                f"\n=> {len(result.assets_saved)} assets saved to: "
                f"{assets_dir.resolve()}  ({result.assets_failed} failed)"
            )

        try:
            result.video_detection = await detect_videos(tab)
        except Exception as e:
            log(f"  (video detection failed: {e})")
            result.video_detection = {"videos": [], "iframes": []}

        # Recover the authoritative HLS manifest + every quality variant
        # from the live hls.js / Plyr instance (see _HLS_INSTANCE_JS). The
        # passive network sniff only sees whatever variant the player
        # fetched -- often a low-quality decoy preview -- so this is what
        # gets the real master + top quality into the yt-dlp pipeline.
        if _HLS_INSTANCE_PROBE_ENABLED:
            try:
                raw_hls = await tab.evaluate(_HLS_INSTANCE_JS)
                hls_urls = json.loads(raw_hls) if raw_hls else []
                added = 0
                for u in hls_urls:
                    if u and u not in result.video_urls_seen:
                        result.video_urls_seen.append(u)
                        added += 1
                if hls_urls:
                    log(
                        f"  ... hls.js instance probe: {len(hls_urls)} "
                        f"manifest URL(s) ({added} new) from live player"
                    )
            except Exception as e:
                log(f"  ... hls.js instance probe skipped "
                    f"({type(e).__name__}: {e})")

        for line in _format_video_report(
            result.video_detection, result.video_urls_seen
        ):
            log(line)

        if assets_dir is not None:
            ytdlp_targets: list[tuple[str, Optional[str], str]] = []
            seen_targets: set[str] = set()

            def add_target(u: str, ref: Optional[str], lbl: str):
                if u and u not in seen_targets:
                    seen_targets.add(u)
                    ytdlp_targets.append((u, ref, lbl))

            # The old "page-url" branch (whitelist match on the fetched
            # URL itself) and "iframe" branch (whitelist match on
            # network-detected iframe srcs) were dropped along with
            # VIDEO_SITE_PATTERN; iframe-generic regex below + the
            # network-stream pass below + HostRecipe per-host targets
            # cover the same ground without a static site list.

            try:
                all_iframe_srcs = await tab.evaluate(
                    "JSON.stringify("
                    "[...document.querySelectorAll('iframe[src]')]"
                    ".map(f => f.src)"
                    ".filter(s => s && /^https?:/.test(s))"
                    ")"
                )
                iframe_srcs = json.loads(all_iframe_srcs) if all_iframe_srcs else []
            except Exception:
                iframe_srcs = []
            result.iframe_srcs = list(iframe_srcs)
            try:
                page_host = urlparse(url).hostname or ""
            except Exception:
                page_host = ""
            for src in iframe_srcs:
                try:
                    src_host = urlparse(src).hostname or ""
                except Exception:
                    continue
                if src_host and src_host != page_host:
                    if re.search(
                        r"(player|frame|embed|video|stream|watch|hub)",
                        src, re.I,
                    ):
                        add_target(src, url, "iframe-generic")

            # HLS master/variant dedup: when two captured m3u8 URLs are
            # actually the same video at different bitrates (a master and one
            # of its variants both surfaced through the network sniffer), feed
            # ONLY the master to yt-dlp. yt-dlp picks the best variant itself.
            # ``make_async_client`` honours the same proxy / TLS / DNS knobs
            # the rest of the worker uses; the peek is HEAD-like (Range 0-4095)
            # and best-effort, so a stuck CDN can't block fetch completion.
            _dedup_targets: list[str]
            try:
                from core.httpclient import make_async_client as _mac
                # 10s overall (connect + read) per peek; the inner gather()
                # parallelises so total wall time stays bounded by the slowest
                # single response, not the sum.
                async with _mac(
                    timeout=10.0,
                    follow_redirects=True,
                ) as _peek_client:
                    _dedup_targets = await pick_stream_urls_async(
                        result.video_urls_seen, _peek_client,
                    )
            except Exception:
                # Fallback to sync path on any setup error -- no dedup but no
                # functional regression either.
                _dedup_targets = pick_stream_urls(result.video_urls_seen)
            for s in _dedup_targets:
                add_target(s, url, "network-stream")

            if ytdlp_targets and opts.defer_video_download:
                # Detect-only: hand the targets back to the caller, which
                # releases the lane and downloads them in a detached
                # background task (job phase = "downloading").
                result.deferred_video_targets = [
                    {"url": u, "referer": ref, "label": lbl}
                    for (u, ref, lbl) in ytdlp_targets
                ]
                log(
                    f"\n=== {len(ytdlp_targets)} video target(s) detected; "
                    f"deferring download to background (lane released) ==="
                )
                for u, _ref, lbl in ytdlp_targets:
                    log(f"     [{lbl}] deferred: {u}")
            elif ytdlp_targets:
                if not shutil.which("yt-dlp"):
                    log(
                        f"\n!! {len(ytdlp_targets)} video URL(s) detected "
                        f"but yt-dlp not installed. Run: pip install yt-dlp"
                    )
                    for u, _ref, lbl in ytdlp_targets:
                        log(f"     [{lbl}] would download: {u}")
                else:
                    log(
                        f"\n=== Auto yt-dlp "
                        f"({len(ytdlp_targets)} URL(s)) ==="
                    )
                    ok_count = fail_count = 0
                    # Bounce log lines back from the worker thread to the
                    # async loop. The upstream ``log`` callback (in
                    # server/worker/agent.py) ends with
                    # ``asyncio.ensure_future(self._send(...))`` which
                    # requires the running event loop. Running it directly
                    # from a thread raises ``RuntimeError: no running event
                    # loop``. ``call_soon_threadsafe`` schedules the call
                    # on the loop's queue from any thread.
                    _ytdlp_loop = asyncio.get_event_loop()
                    def _ytdlp_safe_log(line: str) -> None:
                        try:
                            _ytdlp_loop.call_soon_threadsafe(log, line)
                        except Exception:
                            # Last-ditch: print to stderr so the line is
                            # not lost even if the loop is unavailable.
                            try:
                                import sys as _sys
                                print(line, file=_sys.stderr)
                            except Exception:
                                pass
                    # File-name → source URL mapping. yt-dlp の各実行前後で
                    # assets_dir を diff し、新規ファイルを呼び出し URL に紐づける。
                    # 後段の `assets_saved` 登録時に `url` として書き戻して
                    # `.meta` sidecar 経由で `assets.json` の source_url まで届く。
                    # これがないと crawl.py 等の下流が source_url 空のため動画を
                    # 完全に取りこぼす (= face 抽出 0 件)。
                    _ytdlp_url_for_file: dict[str, str] = {}

                    # fetch-mode の wall-clock timeout (default run_ytdlp=600s)。
                    # 600s だと 800 KiB/s で ~470 MB の VOD でギリギリ間に合わない
                    # ケース (99.3% で kill されて .mp4.part 残し) が発生した。
                    # env で延長可能にする (default 30 分 = 0.83 MB/s で 1.5 GB)。
                    # PAPRIKA_YTDLP_FETCH_TIMEOUT_S=0 で旧 600s に戻る。
                    _fetch_yt_timeout = int(
                        os.environ.get("PAPRIKA_YTDLP_FETCH_TIMEOUT_S", "1800")
                    ) or 600
                    # rate-gate / stall-gate kill 後の自動 retry 回数 (= 0 で無効)。
                    # yt-dlp は default で `--continue` 相当の挙動なので、 既存 `.part`
                    # から resume されて 2 回目で完走することが多い (= CDN 一時不調や
                    # PEER 切断のケース)。
                    _yt_retry_max = int(
                        os.environ.get("PAPRIKA_YTDLP_RETRY_ON_STALL", "1")
                    )

                    for u, ref, lbl in ytdlp_targets:
                        _attempt = 0
                        ok = False
                        msg = ""
                        while True:
                            _attempt += 1
                            log(f"  [{lbl}] attempt {_attempt}")
                            try:
                                _before_files = {
                                    p.name for p in assets_dir.iterdir() if p.is_file()
                                } if assets_dir and assets_dir.exists() else set()
                            except Exception:
                                _before_files = set()
                            # yt-dlp shells out synchronously (subprocess.run);
                            # offload to a worker thread so the asyncio loop
                            # keeps pumping the worker's heartbeat + WS ping
                            # response while the download runs.
                            ok, msg = await asyncio.to_thread(
                                run_ytdlp,
                                u, assets_dir,
                                referer=ref,
                                cookies_from_browser=opts.cookies_from,
                                timeout=_fetch_yt_timeout,
                                log=_ytdlp_safe_log,
                            )
                            try:
                                _after_files = {
                                    p.name for p in assets_dir.iterdir() if p.is_file()
                                } if assets_dir and assets_dir.exists() else set()
                            except Exception:
                                _after_files = set()
                            for _nf in (_after_files - _before_files):
                                _ytdlp_url_for_file[_nf] = u
                            if ok:
                                break
                            # 0 B/s / stall / rate-gate kill のときだけ retry。
                            # wall-clock timeout / Unsupported URL / 404 等は retry しない
                            # (= 同じ結果になるか、 ファイル巨大すぎてどうにもならない)。
                            _ml = (msg or "").lower()
                            _is_stall = (
                                "too slow" in _ml
                                or "no progress" in _ml
                                or "min_rate" in _ml
                            )
                            if _attempt > _yt_retry_max or not _is_stall:
                                break
                            log(f"  ↻ retry after stall-gate kill ({msg[:80]})")

                        result.ytdlp_results.append({
                            "url": u, "label": lbl,
                            "referer": ref, "ok": ok, "message": msg,
                            "attempts": _attempt,
                        })
                        if ok:
                            log(f"  OK   {u}\n       {msg}")
                            ok_count += 1
                        else:
                            log(f"  FAIL {u} (after {_attempt} attempt(s))")
                            for line in msg.splitlines():
                                log(f"       {line}")
                            fail_count += 1
                    log(f"=> yt-dlp: {ok_count} ok, {fail_count} failed")

                    # merged file / direct-output file の url 解決ヘルパ:
                    # 1) ファイル名そのままで _ytdlp_url_for_file を引く (= yt-dlp が
                    #    最終 .mp4 を 1 ファイルで吐く典型ケース)
                    # 2) miss → fragment 名から merged_path が推定できないので、
                    #    成功した yt-dlp の最後の URL に振る (= 1-target ケースで正確
                    #    & multi-target ケースでもまず妥当な近似)
                    def _resolve_ytdlp_src_url(_fname: str) -> str | None:
                        u = _ytdlp_url_for_file.get(_fname)
                        if u:
                            return u
                        for _r in reversed(result.ytdlp_results):
                            if _r.get("ok") and _r.get("url"):
                                return _r["url"]
                        return None
                    # ----- .mp4.part 救出 -----
                    # yt-dlp が timeout / stall / 強制 kill で中断されたケース。
                    # `.mp4.part` はほぼ完全な mp4 (= 末尾 moov atom のみ欠落) が
                    # 残るので、 ffprobe で再生可能性を確認して `.mp4` として
                    # 採用する。 こうしないと 99% で kill された動画が完全に
                    # 失われ、 storage に塩漬けされるだけになる。
                    # コピー (rename ではない) で yt-dlp の resume を壊さない。
                    _part_recover_min = int(os.environ.get(
                        "PAPRIKA_YTDLP_PART_RECOVER_MIN_MB", "50"
                    )) * 1024 * 1024
                    if assets_dir and assets_dir.exists():
                        for _part_file in sorted(assets_dir.glob("*.mp4.part")):
                            try:
                                if not _part_file.is_file():
                                    continue
                                if _part_file.stat().st_size < _part_recover_min:
                                    log(f"  📦 .part skip (<{_part_recover_min//1024//1024}MB): {_part_file.name[:60]}")
                                    continue
                                _mp4_path = _part_file.with_suffix("")  # strip .part
                                if _mp4_path.exists():
                                    continue
                                # ffprobe で再生可能 + video stream あり確認
                                _probe = subprocess.run(
                                    ["ffprobe", "-v", "error", "-print_format", "json",
                                     "-show_format", "-show_streams", str(_part_file)],
                                    capture_output=True, text=True, timeout=30,
                                )
                                if _probe.returncode != 0:
                                    log(f"  📦 .part unplayable: {_part_file.name[:60]}")
                                    continue
                                _info = json.loads(_probe.stdout or "{}")
                                _has_vid = any(
                                    s.get("codec_type") == "video"
                                    for s in (_info.get("streams") or [])
                                )
                                _dur = float((_info.get("format") or {}).get("duration") or 0)
                                if not _has_vid or _dur <= 0:
                                    log(f"  📦 .part no video / dur=0: {_part_file.name[:60]}")
                                    continue
                                # copy (not rename) so yt-dlp's --continue resumes if retried
                                import shutil as _sh
                                _sh.copy2(_part_file, _mp4_path)
                                _sz = _mp4_path.stat().st_size
                                result.assets_saved.append({
                                    "name": _mp4_path.name,
                                    "path": str(_mp4_path.resolve()),
                                    "size": _sz,
                                    "url": _resolve_ytdlp_src_url(_mp4_path.name),
                                    "mime": "video/mp4",
                                })
                                log(
                                    f"  📦 .part RECOVERED: {_part_file.name[:50]} "
                                    f"→ {_mp4_path.name[:50]} "
                                    f"({_sz / 1_048_576:.0f} MB, dur={_dur:.0f}s)"
                                )
                            except Exception as _re:
                                log(f"  📦 .part recovery error: {_re}")

                    # Post-process: if yt-dlp downloaded individual fMP4
                    # segments (init + numbered fragments), merge them into
                    # a single playable MP4 and register each merged file
                    # as an asset.
                    merged_paths = merge_fmp4_fragments(assets_dir, log)
                    for mp in merged_paths:
                        result.assets_saved.append({
                            "name": mp.name,
                            "path": str(mp.resolve()),
                            "size": mp.stat().st_size,
                            "url": _resolve_ytdlp_src_url(mp.name),
                            "mime": "video/mp4",
                        })

                    # Register any video files yt-dlp wrote directly into
                    # assets_dir that aren't already in the gallery (e.g. a
                    # 30s live-HLS recording or a VOD download).  Skip bare
                    # fMP4 fragments (init segments and moof-only shards) –
                    # those are either already merged above or not playable.
                    _VID_EXTS = {
                        ".mp4": "video/mp4",
                        ".mkv": "video/x-matroska",
                        ".webm": "video/webm",
                        ".ts":  "video/MP2T",
                        ".m4v": "video/mp4",
                        ".mov": "video/quicktime",
                        ".avi": "video/x-msvideo",
                        ".flv": "video/x-flv",
                    }
                    _in_gallery = {a["path"] for a in result.assets_saved}
                    if assets_dir and assets_dir.exists():
                        for _vf in sorted(assets_dir.iterdir()):
                            if not _vf.is_file():
                                continue
                            _mime_v = _VID_EXTS.get(_vf.suffix.lower())
                            if not _mime_v:
                                continue
                            if str(_vf.resolve()) in _in_gallery:
                                continue
                            # Skip bare fMP4 fragments — not independently
                            # playable (init-only or moof/sidx/styp shards).
                            _bt = _fmp4_box_type(_vf)
                            if _bt in (b"moof", b"sidx", b"styp"):
                                continue
                            if _bt == b"ftyp" and _vf.stat().st_size < 100_000:
                                continue
                            # yt-dlp with --hls-use-mpegts writes MPEG-TS
                            # into a .mp4 file.  VideoRemuxer skips the
                            # remux because the extension is already ".mp4".
                            # Detect by MPEG-TS sync byte (0x47) and remux
                            # with ffmpeg so the browser can play the file.
                            if _vf.suffix.lower() == ".mp4":
                                try:
                                    _magic = _vf.read_bytes()[:1]
                                except Exception:
                                    _magic = b""
                                if _magic == b"\x47":
                                    _tmp = _vf.with_suffix(".remux.mp4")
                                    log(f"  🔄 MPEG-TS→MP4 remux: {_vf.name}")
                                    try:
                                        _rr = subprocess.run(
                                            [
                                                "ffmpeg", "-y",
                                                "-i", str(_vf),
                                                "-c", "copy",
                                                "-movflags", "+faststart",
                                                str(_tmp),
                                            ],
                                            capture_output=True,
                                            timeout=120,
                                        )
                                        if (
                                            _rr.returncode == 0
                                            and _tmp.exists()
                                            and _tmp.stat().st_size > 0
                                        ):
                                            _vf.unlink()
                                            _tmp.rename(_vf)
                                            log(f"  ✅ remux OK: {_vf.name}")
                                        else:
                                            _tmp.unlink(missing_ok=True)
                                            log(
                                                f"  !! remux failed "
                                                f"(rc={_rr.returncode}): "
                                                + (_rr.stderr or b"")
                                                .decode(errors="replace")
                                                .strip()[-200:]
                                            )
                                    except Exception as _re:
                                        log(f"  !! remux error: {_re}")
                            _sz = _vf.stat().st_size
                            result.assets_saved.append({
                                "name": _vf.name,
                                "path": str(_vf.resolve()),
                                "size": _sz,
                                "url": _resolve_ytdlp_src_url(_vf.name),
                                "mime": _mime_v,
                            })
                            log(
                                f"  📼 video→gallery: {_vf.name} "
                                f"({_sz / 1_048_576:.1f} MB)"
                            )

        # Auto-save back: dump the full cookie jar from the (now
        # warmed-up) browser and hand it to the worker callback, which
        # will host-filter and PUT to the registry. Best-effort; a
        # dump failure must NOT cancel the otherwise-successful fetch.
        if opts.on_complete_dump_cookies is not None:
            try:
                jar = await tab.send(cdp.network.get_all_cookies())
                dumped: list[dict] = []
                for c in jar or []:
                    try:
                        d = c.to_json() if hasattr(c, "to_json") else dict(vars(c))
                    except Exception:
                        d = {}
                    if d:
                        dumped.append(d)
                log(f"  ... dumped {len(dumped)} cookie(s) from browser for host-registry save")
                await opts.on_complete_dump_cookies(dumped)
            except Exception as e:
                log(
                    f"  !! cookie auto-save failed "
                    f"({type(e).__name__}: {e}); continuing"
                )

        return result
    finally:
        # Cancel the URL-capture poller if it was started.  Otherwise
        # it keeps looping on a torn-down tab and logs benign errors.
        try:
            t = locals().get("_fetch_url_capture_task")
            if t is not None and not t.done():
                t.cancel()
        except Exception:
            pass
        # Last chance to unregister any inspectable-session bookkeeping
        # the caller put on this fetch -- MUST run before browser.stop()
        # so subsequent /sessions/{id}/* requests get a clean 404 and
        # don't try to ride a half-disconnected tab.
        if opts.on_browser_closing is not None:
            try:
                await opts.on_browser_closing()
            except Exception as e:
                log(
                    f"  !! on_browser_closing callback failed "
                    f"({type(e).__name__}: {e})"
                )
        # Reduce to one tab so the next operator (next fetch / human in
        # noVNC) sees a clean window. Best-effort; cleanup failure must
        # not mask the actual fetch result.
        #
        # NEVER do this in ATTACH mode: we connected to the operator's
        # OWN running Chrome (attach_host/port), so reducing it to a
        # single tab would close every other tab they had open. Tab
        # cleanup only makes sense for a browser this fetch owns.
        if not attaching:
            try:
                await _force_single_page_target(browser, log=log)
            except Exception as e:
                log(f"  !! post-fetch tab cleanup failed: {e}")
        if keep_open:
            log(
                "  ... browser left open (--keep-open). "
                "Close it manually when done."
            )
        else:
            browser.stop()
