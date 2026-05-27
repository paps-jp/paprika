"""Profile registry routes: /profiles/* (list, CRUD, default, install).

Operators upload Chrome profile snapshots (cookies / login state /
extensions). Jobs opt in with ``options.use_profile = "<name>"`` and
the hub instructs the worker to lay the profile's tarball into the
lane's user-data-dir before Chrome starts.

This module owns the entire profile feature surface:

* CRUD routes (GET / PUT / POST / DELETE under /profiles/)
* Default-profile management (POST/DELETE /profiles/default,
  POST /profiles/{name}/default)
* The Paprika Bridge extension install page +
  ``cookie-pusher.zip`` / ``paprika-bridge.zip`` artefacts that the
  install page serves
* All the archive helpers (_archive_to_targz / _detect_profile_remap
  / _format_bytes) used by upload_profile
* The broadcast helpers (_broadcast_profile_sync,
  _broadcast_profile_delete, _sync_all_profiles_to_worker,
  _profile_url_for_worker) -- the first two are called by the routes;
  _sync_all_profiles_to_worker is also called from worker-connect code
  still in app.py (re-exported via ``from server.hub.routes.profiles
  import _sync_all_profiles_to_worker``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from server.hub._state import config, state
from server.hub.profiles import (
    ProfileRegistry,
)
from server.hub.profiles import (
    is_valid_name as _profile_name_valid,
)
from server.protocol import HubProfileDelete, HubProfileSync

log = logging.getLogger(__name__)
router = APIRouter(tags=["Profiles"])


# Per-profile upload size cap. Operators uploading a real Chrome
# user-data-dir routinely hit a few hundred MB once a few sites with
# heavy IndexedDB are involved (twitter / discord). 500 MB default,
# overridable via PAPRIKA_PROFILE_MAX_BYTES if the operator needs more.
_PROFILE_MAX_BYTES = int(os.environ.get("PAPRIKA_PROFILE_MAX_BYTES") or 500 * 1024 * 1024)


# HTML for the extension install page. Plain string so we don't pull a
# template engine just for one page. Hub's "look" (system font stack +
# muted typography) is intentionally lighter than the admin UI -- this
# is a one-page handoff, not a tab.
_PROFILE_EXTENSION_INSTALL_HTML = """<!doctype html>
<html lang="ja"><head>
<meta charset="utf-8" />
<title>Paprika Bridge -- Install</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     max-width:720px;margin:40px auto;padding:0 20px;color:#222;line-height:1.6;}
h1{font-size:24px;margin-bottom:0;}
.sub{color:#666;margin-top:4px;}
code,pre{background:#f5f5fa;padding:2px 6px;border-radius:4px;font-size:13px;}
pre{padding:12px;overflow-x:auto;line-height:1.4;}
ol li{margin:8px 0;}
.dl{display:inline-block;background:#fef5e7;border:1px solid #d4a13d;
    color:#8a5a00;padding:8px 16px;border-radius:6px;font-weight:600;
    text-decoration:none;margin:12px 0;}
.dl:hover{background:#fce5b2;}
.note{background:#fff8e1;border-left:4px solid #d4a13d;padding:10px 14px;
      margin:16px 0;font-size:14px;border-radius:0 6px 6px 0;}
</style>
</head><body>
<h1>Paprika Bridge</h1>
<p class="sub">
  Chrome と Paprika Hub をつなぐ拡張機能です。 現バージョンは「今ログインしているサイトのクッキーを
  ワンクリックで <code>/hosts/&lt;host&gt;</code> レジストリへ送る」機能を提供。
  以後ジョブで <code>options.cookies_from=&quot;example.com&quot;</code> を指定すれば
  そのログイン状態でクロールできます。 今後のバージョンで URL 転送、クリップボード共有、
  ジョブ状態取得などを追加予定。
</p>

<h2>1. ダウンロード</h2>
<a class="dl" href="/profiles/extension/paprika-bridge.zip">
  paprika-bridge.zip をダウンロード
</a>

<h2>2. Chrome に読み込ませる (Load unpacked)</h2>
<ol>
  <li>ダウンロードした <code>paprika-bridge.zip</code> を展開
       (Windows なら右クリック → すべて展開)。
       <code>paprika-bridge/</code> というフォルダができます。</li>
  <li>Chrome のアドレスバーに <code>chrome://extensions</code> を貼り付けて開く。</li>
  <li>右上の「<strong>デベロッパー モード</strong>」を ON。</li>
  <li>左上の「<strong>パッケージ化されていない拡張機能を読み込む</strong>」を押し、
       手順 1 で展開した <code>paprika-bridge/</code> フォルダを選ぶ。</li>
  <li>ツールバーに paprika ロゴが出れば成功。
       ピン留めしておくと押しやすいです。</li>
</ol>

<h2>3. 使い方</h2>
<ol>
  <li>ログインしておきたいサイトを Chrome で開いてログインしておく
       (普段使いの Chrome そのままで OK)。</li>
  <li>ツールバーの paprika アイコンをクリック → ポップアップ。</li>
  <li>初回は Hub URL を入力 (例: <code>http://paprika.lan</code>)。
       次回からは保存される。</li>
  <li>「Push cookies to hub」を押すと、対象ホスト (現在のタブのドメインを
       デフォルトとする) のクッキーがハブに送られる。</li>
  <li>送ったホストは admin UI の <a href="/#hosts">Hosts</a> タブに出る。
       <code>cookies_from</code> で参照可能。</li>
</ol>

<div class="note">
  <strong>制限事項 (0.2 現在):</strong>
  cookie 転送のみ動作します (Chrome 拡張 API の制約)。
  Login Data SQLite / IndexedDB / Local Storage は含まれません。
  実用上、cookie だけで 90% のログインサイトには再ログイン不要で入れます。
  完全な profile (autofill / passwords / Local Storage 含む) を持ち込みたい
  場合は <code>paprika-client upload-profile</code> CLI を使ってください。
</div>

<h2>関連リンク</h2>
<ul>
  <li><a href="/#profiles">Profiles タブに戻る</a></li>
  <li><a href="https://github.com/paps-jp/paprika">paprika ソース (GitHub)</a></li>
</ul>
</body></html>
"""


def _require_profiles() -> ProfileRegistry:
    if state.profiles is None:
        raise HTTPException(503, "profile registry not initialised")
    return state.profiles


def _profile_url_for_worker(worker, name: str) -> str | None:
    """Build the GET URL a worker should use to fetch the tarball.

    Same logic as the per-job profile_url assembly: prefer the URL
    the worker dialled in on (worker.public_base_url), fall back to
    PUBLIC_BASE_URL. Returns None if neither is known -- in that
    case the worker has no way to reach back, so we skip the sync.
    """
    base = worker.public_base_url or config.public_base_url
    if not base:
        return None
    return f"{base.rstrip('/')}/profiles/{name}"


async def _broadcast_profile_sync(name: str) -> None:
    """Tell every connected worker to (re)prefetch ``name`` into its
    cache. Called after POST /profiles/{name} succeeds. Best-effort:
    a worker that's offline now will get the sync when it next
    connects via the handshake re-sync path.

    The ``is_default`` flag is filled in from the current default-
    profile state so workers know whether to install this one as
    the ambient (= applied to all idle lanes' user-data-dir so
    noVNC viewers see the operator's logged-in Chrome immediately).
    """
    if state.profiles is None or state.registry is None:
        return
    etag = state.profiles.etag(name)
    meta = state.profiles.get_meta(name)
    if etag is None or meta is None:
        return
    is_default = state.profiles.get_default() == name
    for w in list(state.registry.connections.values()):
        url = _profile_url_for_worker(w, name)
        if not url:
            continue
        try:
            await w.send(
                HubProfileSync(
                    name=name,
                    url=url,
                    etag=etag,
                    size_bytes=meta.size_bytes,
                    is_default=is_default,
                )
            )
        except Exception:
            log.warning(
                "profile_sync %r -> %s failed",
                name,
                w.worker_id,
                exc_info=True,
            )


async def _broadcast_profile_delete(name: str) -> None:
    """Tell every connected worker to drop its cached copy of
    ``name``. Called after DELETE /profiles/{name}.
    """
    if state.registry is None:
        return
    for w in list(state.registry.connections.values()):
        try:
            await w.send(HubProfileDelete(name=name))
        except Exception:
            log.warning(
                "profile_delete %r -> %s failed",
                name,
                w.worker_id,
                exc_info=True,
            )


async def _sync_all_profiles_to_worker(worker) -> None:
    """On worker (re)connect, send a HubProfileSync for every
    currently-registered profile so it can prefetch / verify its
    cache against our authoritative state. The is_default flag is
    set on the broadcast for the active default so the worker
    installs the ambient on its idle lanes.
    """
    if state.profiles is None:
        return
    default_name = state.profiles.get_default()
    for meta in state.profiles.list():
        url = _profile_url_for_worker(worker, meta.name)
        if not url:
            continue
        etag = state.profiles.etag(meta.name)
        if etag is None:
            continue
        try:
            await worker.send(
                HubProfileSync(
                    name=meta.name,
                    url=url,
                    etag=etag,
                    size_bytes=meta.size_bytes,
                    is_default=(meta.name == default_name),
                )
            )
        except Exception:
            log.warning(
                "initial profile_sync %r -> %s failed",
                meta.name,
                worker.worker_id,
                exc_info=True,
            )


def _detect_profile_remap(top_entries: dict) -> tuple[str | None, str]:
    """Decide how to remap an archive's top-level layout into the
    "User Data" shape the worker expects: ``Default/`` (the
    profile) + optional ``Local State`` at the root.

    Returns ``(rename_top, wrap_in)``:
      * ``(top_dir, "")`` -- the archive has a single non-Default
        top-level directory whose content looks like a Chrome
        profile (Preferences / Cookies / etc. inside). Catches
        the common "I zipped my 'Profile 10' folder" mistake.
      * ``(None, "Default")`` -- the archive is flat (Preferences
        directly at root). Wrap everything under ``Default/``.
      * ``(None, "")`` -- archive is already in the right shape
        (``Default/`` + ``Local State`` at root).
    """
    PROFILE_MARKERS = ("Preferences", "Cookies", "History", "Bookmarks")
    USER_DATA_MARKERS = ("Local State",)
    file_names = {n for n, k in top_entries.items() if k == "file"}
    dir_names = {n for n, k in top_entries.items() if k == "dir"}
    # Already correct shape.
    if "Default" in dir_names and any(m in file_names for m in USER_DATA_MARKERS):
        return None, ""
    # Flat profile: Preferences sitting at root -> wrap.
    if any(m in file_names for m in PROFILE_MARKERS):
        return None, "Default"
    # Single named profile dir: rename top to Default.
    if len(dir_names) == 1 and not file_names:
        only = next(iter(dir_names))
        return only, ""
    return None, ""


async def _archive_to_targz(
    src_path: Path,
    *,
    format: str,
    max_bytes: int,
) -> Path:
    """Normalise an uploaded archive (gzip-tar or ZIP) into the
    canonical User Data tar.gz the worker expects.

    Walks the entries once to detect the operator's intended Chrome
    profile layout (single Profile X dir / flat / standard) via
    :func:`_detect_profile_remap`, then re-packs as a fresh tar.gz
    with the remap applied so the worker always sees::

        Default/Preferences
        Default/Extensions/<id>/<version>/...
        Local State                          (when present in source)

    The original ``src_path`` is removed on success.

    Defences:
      * Path-escape entries (Zip Slip / tarbomb) -> 400.
      * Uncompressed total > ``max_bytes * 4`` -> 413 (bomb).
    """
    import io
    import tarfile
    import tempfile
    import zipfile

    # ---- enumerate entries from the source archive ------------------
    if format == "zip":
        z = zipfile.ZipFile(src_path, "r")
        entries: list[tuple[str, int, callable]] = []
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
                z.close()
                raise HTTPException(
                    400,
                    f"archive entry refused (escaping path): {name!r}",
                )
            if info.is_dir():
                entries.append((name, -1, None))
            else:
                entries.append(
                    (
                        name,
                        info.file_size,
                        (lambda i=info: z.read(i)),
                    )
                )
        close_src = z.close
    else:
        t = tarfile.open(src_path, "r:gz")
        entries = []
        for m in t.getmembers():
            name = m.name
            if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
                t.close()
                raise HTTPException(
                    400,
                    f"archive entry refused (escaping path): {name!r}",
                )
            if m.isdir():
                entries.append((name.rstrip("/") + "/", -1, None))
            elif m.isfile():
                entries.append(
                    (
                        name,
                        m.size,
                        (lambda mm=m: t.extractfile(mm).read()),
                    )
                )
        close_src = t.close

    # ---- detect layout -----------------------------------------------
    top: dict[str, str] = {}
    for name, sz, _ in entries:
        first = name.split("/", 1)[0]
        rest = name[len(first) + 1 :].rstrip("/")
        if rest or name.endswith("/"):
            top.setdefault(first, "dir")
        else:
            top.setdefault(first, "file" if sz >= 0 else "dir")
    rename_top, wrap_in = _detect_profile_remap(top)

    def remap(name: str) -> str:
        if rename_top is not None:
            prefix = rename_top + "/"
            if name == rename_top or name == rename_top + "/":
                return "Default/"
            if name.startswith(prefix):
                return "Default/" + name[len(prefix) :]
            return name
        if wrap_in:
            ROOT_FILES = {"Local State", "First Run"}
            if name in ROOT_FILES:
                return name
            return wrap_in + "/" + name
        return name

    # ---- write normalised tar.gz -------------------------------------
    out_path = Path(
        tempfile.mkstemp(
            prefix="profile_repack_",
            suffix=".tar.gz.tmp",
        )[1]
    )
    uncompressed_total = 0
    try:
        with tarfile.open(out_path, "w:gz", compresslevel=6) as tf:
            seen_dirs: set[str] = set()
            for name, size, reader in entries:
                new_name = remap(name)
                if reader is None:
                    if not new_name.endswith("/"):
                        new_name += "/"
                    if new_name in seen_dirs:
                        continue
                    seen_dirs.add(new_name)
                    ti = tarfile.TarInfo(name=new_name)
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    tf.addfile(ti)
                    continue
                data = reader()
                uncompressed_total += len(data)
                if uncompressed_total > max_bytes * 4:
                    raise HTTPException(
                        413,
                        f"archive transcode aborted: uncompressed "
                        f"size exceeded {max_bytes * 4} bytes "
                        f"(bomb suspected).",
                    )
                ti = tarfile.TarInfo(name=new_name)
                ti.size = len(data)
                ti.mode = 0o644
                ti.mtime = 0
                tf.addfile(ti, io.BytesIO(data))
        action = "kept layout"
        if rename_top is not None:
            action = f"remapped top {rename_top!r} -> 'Default'"
        elif wrap_in:
            action = f"wrapped flat layout in {wrap_in!r}"
        log.info(
            "normalised %s upload -> tar.gz: %d bytes uncompressed -> "
            "%d bytes compressed (%s)",
            format,
            uncompressed_total,
            out_path.stat().st_size,
            action,
        )
    except HTTPException:
        try:
            out_path.unlink()
        except OSError:
            pass
        try:
            close_src()
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            out_path.unlink()
        except OSError:
            pass
        try:
            close_src()
        except Exception:
            pass
        raise HTTPException(
            400,
            f"archive transcode failed: {type(e).__name__}: {e}",
        )
    finally:
        try:
            close_src()
        except Exception:
            pass
        try:
            src_path.unlink()
        except OSError:
            pass
    return out_path


# Legacy name kept for any path that still calls it; new code
# should use _archive_to_targz with format= directly.
async def _zip_to_targz(zip_path: Path, *, max_bytes: int) -> Path:
    return await _archive_to_targz(
        zip_path,
        format="zip",
        max_bytes=max_bytes,
    )


def _profile_meta_to_dict(meta, *, default_name: str | None = None) -> dict:
    d = meta.to_json()
    # Mirror the convention used by other registries: surface the
    # human-readable size next to the byte count so the admin UI
    # doesn't have to format it.
    d["size_human"] = _format_bytes(d.get("size_bytes") or 0)
    # Flag the default profile in list responses so the UI can
    # highlight it without an extra round trip to GET /profiles/default.
    d["is_default"] = default_name is not None and meta.name == default_name
    return d


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n = n / 1024
    return f"{n} ?"


@router.get("/profiles")
async def list_profiles() -> dict:
    """List every uploaded Chrome profile (metadata only).

    The tarballs themselves are at ``GET /profiles/{name}`` but those
    are typically only fetched by workers when a job opts into a
    profile via ``options.use_profile``.

    Response shape::

        {
          "default": "mydefault" | null,    // auto-applied profile name
          "profiles": [{name, size_bytes, ..., is_default}, ...]
        }
    """
    reg = _require_profiles()
    default = reg.get_default()
    return {
        "default": default,
        "profiles": [_profile_meta_to_dict(m, default_name=default) for m in reg.list()],
    }


@router.get("/profiles/default")
async def get_default_profile() -> dict:
    """Return ``{name: "<profile>" | null}`` for the default profile.

    The default is auto-applied to any /jobs or /sessions request
    that doesn't set ``options.use_profile`` explicitly. None means
    no default is set -- jobs run with the lane's stock profile.
    """
    reg = _require_profiles()
    return {"name": reg.get_default()}


@router.post("/profiles/{name}/default")
async def set_default_profile(name: str) -> dict:
    """Mark ``{name}`` as the auto-applied default profile.

    Effect: subsequent /jobs and /sessions requests without an
    explicit ``options.use_profile`` will be dispatched with this
    profile in the user-data-dir. Override per-job by setting
    ``options.use_profile`` to a different name. Clear via
    ``DELETE /profiles/default``.

    Workers receive a HubProfileSync broadcast with is_default=True
    so they install this profile as the ambient -- noVNC viewers
    see the operator's logged-in Chrome on every idle lane, not
    just on lanes that happened to run a job. The previous default
    (if any) gets a is_default=False broadcast so workers clear it.

    Only one default at a time; setting a new one replaces the
    previous default.
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    prev = reg.get_default()
    try:
        reg.set_default(name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    log.info("profile %r set as default", name)
    # Re-broadcast the new default first so workers install it,
    # then demote the previous default. Order matters: if we
    # cleared the old one first, the workers would briefly revert
    # to stock between the two broadcasts (visible in noVNC as a
    # "logged-out flicker"). Installing-then-clearing avoids that.
    try:
        await _broadcast_profile_sync(name)
        if prev and prev != name:
            await _broadcast_profile_sync(prev)
    except Exception:
        log.warning("default-change broadcast failed", exc_info=True)
    return {"name": name, "previous": prev}


@router.delete("/profiles/default")
async def clear_default_profile() -> dict:
    """Unset the default profile. Subsequent jobs without an
    explicit ``options.use_profile`` run with the lane's stock
    profile (no extra cookies / login state). Workers also clear
    the ambient install on their idle lanes (noVNC viewers see
    fresh Chrome again).
    """
    reg = _require_profiles()
    prev = reg.get_default()
    reg.set_default(None)
    if prev:
        log.info("default profile cleared (was %r)", prev)
        # Re-broadcast the demoted name with is_default=False so
        # workers clear it from their idle lanes.
        try:
            await _broadcast_profile_sync(prev)
        except Exception:
            log.warning("default-clear broadcast failed", exc_info=True)
    return {"name": None, "previous": prev}


@router.get("/profiles/{name}")
async def download_profile(name: str):
    """Stream the tarball for ``{name}``. Used by workers when they
    receive a HubAssignJob whose ``profile_url`` points here.

    Returns ``application/gzip``. Content-Disposition is set so a
    curl ``--remote-name`` works for ad-hoc debugging too.
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    p = reg.get_tarball_path(name)
    if p is None:
        raise HTTPException(404, f"profile '{name}' not found")
    from fastapi.responses import FileResponse

    return FileResponse(
        path=str(p),
        media_type="application/gzip",
        filename=f"{name}.tar.gz",
    )


@router.get("/profiles/{name}/info")
async def get_profile_info(name: str) -> dict:
    """Metadata for ``{name}`` without downloading the tarball."""
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    meta = reg.get_meta(name)
    if meta is None:
        raise HTTPException(404, f"profile '{name}' not found")
    return _profile_meta_to_dict(meta)


@router.post("/profiles/{name}")
async def upload_profile(
    name: str,
    request: Request,
) -> dict:
    """Upload a Chrome profile tarball.

    The body is the raw gzipped tarball (Content-Type:
    ``application/gzip``). For multipart uploads (CLI convenience),
    use POST /profiles/{name}/multipart instead.

    The tarball should unpack to a single ``User Data``-shaped
    directory tree (the layout produced by
    ``core.fetcher.clone_chrome_profile``). The worker extracts it
    into a per-job scratch dir and points Chrome at it.

    Optional headers the CLI can set (informational, surfaced in the
    admin UI):

      * ``X-Paprika-Source-Machine``: hostname / OS info string
      * ``X-Paprika-Chrome-Profile``: e.g. "Default" or "Profile 1"
      * ``X-Paprika-Note``: free-text note
    """
    if not _profile_name_valid(name):
        raise HTTPException(
            400,
            "invalid profile name (allowed: A-Z a-z 0-9 . _ -, max 64 chars)",
        )
    reg = _require_profiles()
    lock = state.profiles_lock
    assert lock is not None

    source_machine = request.headers.get("x-paprika-source-machine")
    chrome_profile = request.headers.get("x-paprika-chrome-profile")
    note = request.headers.get("x-paprika-note")

    # Stream the body to a temp file rather than read() into memory --
    # operator profiles can hit ~100 MB compressed for heavy Chrome
    # users (lots of localStorage / IndexedDB).
    import tempfile

    tmp = Path(tempfile.mkstemp(prefix=f"profile_upload_{name}_", suffix=".tar.gz.tmp")[1])
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > _PROFILE_MAX_BYTES:
                    raise HTTPException(
                        413,
                        f"profile too large ({total} > "
                        f"{_PROFILE_MAX_BYTES} bytes). Raise "
                        f"PAPRIKA_PROFILE_MAX_BYTES on the hub or "
                        f"upload a slimmer profile.",
                    )
                f.write(chunk)
        # Sniff the magic bytes. We accept either:
        #   * gzip-wrapped tar (1f 8b)  -- canonical, save as-is
        #   * ZIP (PK\3\4 or PK\5\6)    -- Windows "Send to ->
        #                                  Compressed (zipped) folder"
        #                                  output. We unzip + retar+gzip
        #                                  so the on-disk format the
        #                                  worker fetches stays
        #                                  consistent regardless of
        #                                  upload origin.
        # Other formats (plain tar, JSON, garbage) get a targeted 400
        # with a hint about what the operator probably did wrong.
        with open(tmp, "rb") as f:
            magic = f.read(4)
        is_gzip = magic.startswith(b"\x1f\x8b")
        is_zip = magic.startswith(b"PK\x03\x04") or magic.startswith(b"PK\x05\x06")
        if not (is_gzip or is_zip):
            hint: str
            if magic[:5] == b"ustar" or (
                len(magic) >= 4 and magic[:4].isalnum() and (b"\x00" not in magic[:4])
            ):
                hint = (
                    "this looks like a plain tar archive (no gzip "
                    "wrapper). Use `tar czf ...` (note the `z`) "
                    "instead of `tar cf ...`, gzip the .tar first, "
                    "or just upload a .zip instead."
                )
            elif magic[:1] in (b"{", b"[", b"<"):
                hint = (
                    f"this looks like text content (starts with "
                    f"{magic[:1]!r}), not an archive. Did the "
                    "upload tool transcode to JSON / XML?"
                )
            else:
                hint = (
                    f"first bytes were {magic.hex()} -- expected 1f 8b for gzip or 50 4b for zip."
                )
            raise HTTPException(
                400,
                f"uploaded body is not a recognised archive: {hint}",
            )

        # ALWAYS normalise the archive layout to the worker's
        # expected "User Data" shape -- ZIP or tar.gz, regardless
        # of how the operator built it. Catches three common
        # mistakes in one place:
        #   1. ZIP from Windows Explorer "Send to -> Compressed
        #      (zipped) folder" (transcode + normalise)
        #   2. tar/zip of a non-Default profile dir like
        #      "Profile 10/" (rename top -> "Default")
        #   3. flat archive with Preferences at root (wrap in
        #      "Default/")
        # Already-correct uploads pass through as a no-op rename
        # but get re-packed for hash consistency.
        tmp = await _archive_to_targz(
            tmp,
            format=("zip" if is_zip else "gzip"),
            max_bytes=_PROFILE_MAX_BYTES,
        )

        async with lock:
            meta = reg.save(
                name,
                tarball_src=tmp,
                source_machine=source_machine,
                chrome_profile_name=chrome_profile,
                note=note,
            )
        log.info(
            "profile %r uploaded: %d bytes (machine=%r chrome_profile=%r)",
            name,
            meta.size_bytes,
            source_machine,
            chrome_profile,
        )
        # Pre-push to every connected worker so the next job that
        # uses this profile finds it already in the local cache.
        # Fire-and-forget; the on-demand fetch path still works for
        # workers that miss the broadcast.
        try:
            await _broadcast_profile_sync(name)
        except Exception:
            log.warning(
                "profile %r broadcast failed", name, exc_info=True
            )
        return _profile_meta_to_dict(meta)
    finally:
        # save() moves the tmp file on success; on failure remove it
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@router.get("/profiles/extension/install", include_in_schema=False)
async def profile_extension_install_page() -> HTMLResponse:
    """Landing page with the Paprika Bridge .zip + install instructions.

    Operator clicks the "Paprika Bridge" link in the admin UI's
    Profiles tab, lands here, downloads the .zip, follows the
    "Load unpacked" instructions. The extension itself is built on
    the fly from server/web/extensions/paprika-bridge/.
    """
    return HTMLResponse(_PROFILE_EXTENSION_INSTALL_HTML)


def _build_paprika_bridge_zip() -> Response:
    """Shared helper for both the new and the legacy zip URLs.

    The source lives in the hub bind-mount so a `git pull` is enough
    to refresh the served bundle -- no rebuild needed. If the
    directory isn't present (= older source tree), 404 so the
    install page surfaces a clear error.
    """
    import io
    import zipfile

    from fastapi.responses import Response

    src = Path(__file__).parent.parent / "web" / "extensions" / "paprika-bridge"
    if not src.exists():
        raise HTTPException(404, "extension source not bundled in this hub build")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(p.relative_to(src)))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="paprika-bridge.zip"',
        },
    )


@router.get("/profiles/extension/paprika-bridge.zip", include_in_schema=False)
async def profile_extension_download():
    """Build a fresh .zip of the Paprika Bridge extension on every
    download. See ``_build_paprika_bridge_zip``.
    """
    return _build_paprika_bridge_zip()


# Legacy URL (0.1 was distributed as paprika-cookie-pusher.zip).
# Kept around for one release cycle so operators who have the old
# URL bookmarked don't hit a 404 mid-upgrade. Drop after 0.3.
@router.get("/profiles/extension/cookie-pusher.zip", include_in_schema=False)
async def profile_extension_download_legacy():
    return _build_paprika_bridge_zip()


@router.delete("/profiles/{name}")
async def delete_profile(name: str) -> dict:
    """Remove the tarball + metadata for ``{name}``. In-flight jobs
    that already started extracting are unaffected (they hold their
    own scratch dir). Returns ``{deleted: bool}``.
    """
    if not _profile_name_valid(name):
        raise HTTPException(400, "invalid profile name")
    reg = _require_profiles()
    lock = state.profiles_lock
    assert lock is not None
    async with lock:
        ok = reg.remove(name)
    if ok:
        log.info("profile %r deleted", name)
        try:
            await _broadcast_profile_delete(name)
        except Exception:
            log.warning(
                "profile %r delete broadcast failed", name, exc_info=True
            )
    return {"deleted": ok}
