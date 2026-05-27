"""Hub-managed Chrome extension registry.

Operators upload Chrome extensions (uBlock Origin Lite, AdGuard, paywall
bypassers, custom test extensions, ...) to the hub. Each worker pulls
the current set on connect, extracts to a local cache, and the lane
launch passes those paths to ``--load-extension`` so every Chrome lane
boots with the operator's preferred extension set already installed.

Why this exists separately from the Profile registry:

* A Chrome profile carries cookies, login state, autofill — operator-
  identity-shaped data that often shouldn't be shared across jobs
  ("use my prod gmail vs. my staging gmail").
* Extensions are app-shaped — they don't carry operator identity and
  should usually be loaded on every lane (an ad blocker is desirable
  for nearly every job). Keeping them in their own registry lets the
  admin UI manage them with a different mental model: profiles are
  picked per job, extensions are universal.

Plus: extensions delivered via Chrome's sync mechanism break a few
hours after a profile is uploaded (Google's anti-fraud invalidates the
session, "Sync is paused" appears, sync-installed extensions get
greyed out). Loading via ``--load-extension`` from a hub-managed cache
sidesteps Chrome sync entirely.

Storage layout::

    {data_dir}/extensions/<slug>.tar.gz       # the packed unpacked-extension dir
    {data_dir}/extensions/<slug>.meta.json    # operator-friendly metadata

The tarball contains a single top-level directory the worker extracts
verbatim; the resulting path is what gets passed to ``--load-extension``.
We don't store the original ``.crx`` because Chrome can't use a ``.crx``
with ``--load-extension`` (it only accepts unpacked dirs) -- the upload
endpoint normalises ``.zip`` and ``.crx`` uploads into the tarball form.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# kebab-case slug, file-safe + URL-safe.
_SLUG_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and bool(_SLUG_RE.match(slug))


def normalise_slug(slug: str) -> str:
    """Lowercase + collapse weird chars to '-'. Used to be lenient when
    the operator names a new extension via the UI."""
    s = (slug or "").strip().lower()
    s = re.sub(r"[^a-z0-9._\-]+", "-", s).strip("-")
    return s[:64]


@dataclass
class ExtensionMeta:
    """Operator-facing metadata for one uploaded extension."""

    slug: str
    name: str = ""  # human-readable; defaults to slug
    description: str = ""  # one-line note from manifest or operator
    version: str = ""  # from manifest.json
    extension_id: str = ""  # Chrome's deterministic ID from manifest.key
    size_bytes: int = 0
    enabled: bool = True  # if False, workers skip it
    uploaded_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    note: str = ""  # free-text operator note (separate
    # from description which often comes
    # from the manifest)

    def to_json(self) -> dict:
        d = asdict(self)
        d["uploaded_at"] = self.uploaded_at.isoformat() + "Z"
        d["updated_at"] = self.updated_at.isoformat() + "Z"
        return d

    @classmethod
    def from_json(cls, d: dict) -> ExtensionMeta:
        def _ts(s: str | None) -> datetime:
            if not s:
                return datetime.utcnow()
            return datetime.fromisoformat(s.rstrip("Z"))

        return cls(
            slug=d["slug"],
            name=d.get("name") or "",
            description=d.get("description") or "",
            version=d.get("version") or "",
            extension_id=d.get("extension_id") or "",
            size_bytes=int(d.get("size_bytes") or 0),
            enabled=bool(d.get("enabled", True)),
            uploaded_at=_ts(d.get("uploaded_at")),
            updated_at=_ts(d.get("updated_at")),
            note=d.get("note") or "",
        )


class ExtensionRegistry:
    """On-disk extension store.

    All methods are sync — filesystem IO is fast and the registry is
    small (typically <10 extensions). The hub wraps writes in a single
    asyncio.Lock so concurrent uploads can't corrupt each other.
    """

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "extensions"
        self.root.mkdir(parents=True, exist_ok=True)

    # ----- path helpers -----------------------------------------------

    def _tarball_path(self, slug: str) -> Path:
        return self.root / f"{slug}.tar.gz"

    def _meta_path(self, slug: str) -> Path:
        return self.root / f"{slug}.meta.json"

    # ----- read -------------------------------------------------------

    def exists(self, slug: str) -> bool:
        return self._tarball_path(slug).exists()

    def etag(self, slug: str) -> str | None:
        """Opaque cache key for the tarball; workers compare it to
        decide whether their local extraction is still current."""
        p = self._tarball_path(slug)
        if not p.exists():
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        return f"{st.st_size}-{st.st_mtime_ns}"

    def get_meta(self, slug: str) -> ExtensionMeta | None:
        p = self._meta_path(slug)
        if not p.exists():
            tar = self._tarball_path(slug)
            if not tar.exists():
                return None
            # Synth a bare meta from the tarball alone -- happens if
            # someone scp'd one in by hand.
            return ExtensionMeta(slug=slug, size_bytes=tar.stat().st_size)
        try:
            return ExtensionMeta.from_json(json.loads(p.read_text("utf-8")))
        except Exception:
            return None

    def get_tarball_path(self, slug: str) -> Path | None:
        p = self._tarball_path(slug)
        return p if p.exists() else None

    def list(self, *, include_disabled: bool = True) -> list[ExtensionMeta]:
        out: list[ExtensionMeta] = []
        for tar in sorted(self.root.glob("*.tar.gz")):
            slug = tar.name[: -len(".tar.gz")]
            m = self.get_meta(slug)
            if m is None:
                continue
            if not include_disabled and not m.enabled:
                continue
            out.append(m)
        out.sort(key=lambda x: x.updated_at, reverse=True)
        return out

    # ----- write ------------------------------------------------------

    def save(
        self,
        slug: str,
        *,
        upload_bytes: bytes,
        filename: str = "",
        note: str | None = None,
    ) -> ExtensionMeta:
        """Persist an extension upload.

        ``upload_bytes`` is the raw HTTP body. ``filename`` is the
        original upload name (e.g. ``ublock-lite.zip`` /
        ``adguard.crx``) -- used only to pick the unpacking path.
        Returns the saved ExtensionMeta.

        The extension is always stored as a *tarball of an unpacked
        directory* on disk, regardless of upload format. This is
        because Chrome's ``--load-extension`` only accepts unpacked
        dirs, so we may as well normalise at write time and avoid
        re-extracting on every worker.
        """
        if not is_valid_slug(slug):
            raise ValueError(f"invalid extension slug: {slug!r}")

        # Decide the unpacking strategy by filename suffix.
        suffix = ""
        if filename:
            n = filename.lower()
            if n.endswith(".zip"):
                suffix = ".zip"
            elif n.endswith(".crx"):
                suffix = ".crx"
            elif n.endswith(".tar.gz") or n.endswith(".tgz"):
                suffix = ".tar.gz"
        # Fall back to magic-byte sniff if filename was ambiguous.
        if not suffix:
            if upload_bytes[:4] == b"Cr24":
                suffix = ".crx"
            elif upload_bytes[:2] == b"PK":
                suffix = ".zip"
            elif upload_bytes[:2] == b"\x1f\x8b":
                suffix = ".tar.gz"
            else:
                raise ValueError(
                    "unrecognised upload format -- expected .zip, "
                    ".crx, or .tar.gz (containing the unpacked "
                    "extension directory)"
                )

        # Stage the unpacked content under a temp dir, then tar it up.
        with tempfile.TemporaryDirectory(prefix="paprika-ext-") as td_str:
            td = Path(td_str)
            unpack_dir = td / slug
            unpack_dir.mkdir()
            self._unpack_into(upload_bytes, suffix, unpack_dir)
            self._validate_unpacked(unpack_dir)
            # Read manifest fields for the operator UI before we tar.
            manifest_path = unpack_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text("utf-8", errors="replace"))
            except Exception:
                manifest = {}
            ext_name = str(manifest.get("name") or slug)
            ext_version = str(manifest.get("version") or "")
            ext_desc = str(manifest.get("description") or "")
            ext_id = ""  # populated lazily if the manifest carries a "key"

            # Tar up the unpack_dir (parent is td) so the archive's
            # top-level entry is the slug-named dir. Workers extract
            # straight into /tmp/paprika-extensions/, ending up with
            # /tmp/paprika-extensions/<slug>/manifest.json.
            tar_path = self._tarball_path(slug)
            tar_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(unpack_dir, arcname=slug)

        # Build / update meta.
        size = tar_path.stat().st_size
        prev = self.get_meta(slug)
        if prev is not None:
            uploaded_at = prev.uploaded_at
            enabled = prev.enabled
            saved_note = note if note is not None else prev.note
        else:
            uploaded_at = datetime.utcnow()
            enabled = True
            saved_note = note or ""
        meta = ExtensionMeta(
            slug=slug,
            name=ext_name,
            description=ext_desc[:200],
            version=ext_version[:32],
            extension_id=ext_id,
            size_bytes=size,
            enabled=enabled,
            uploaded_at=uploaded_at,
            updated_at=datetime.utcnow(),
            note=saved_note[:500],
        )
        self._meta_path(slug).write_text(json.dumps(meta.to_json(), indent=2), encoding="utf-8")
        return meta

    def set_enabled(self, slug: str, enabled: bool) -> ExtensionMeta | None:
        meta = self.get_meta(slug)
        if meta is None:
            return None
        meta.enabled = bool(enabled)
        meta.updated_at = datetime.utcnow()
        self._meta_path(slug).write_text(json.dumps(meta.to_json(), indent=2), encoding="utf-8")
        return meta

    def set_note(self, slug: str, note: str) -> ExtensionMeta | None:
        meta = self.get_meta(slug)
        if meta is None:
            return None
        meta.note = (note or "")[:500]
        meta.updated_at = datetime.utcnow()
        self._meta_path(slug).write_text(json.dumps(meta.to_json(), indent=2), encoding="utf-8")
        return meta

    def delete(self, slug: str) -> bool:
        ok = False
        for p in (self._tarball_path(slug), self._meta_path(slug)):
            if p.exists():
                try:
                    p.unlink()
                    ok = True
                except OSError:
                    pass
        return ok

    # ----- unpacking helpers -----------------------------------------

    def _unpack_into(self, blob: bytes, suffix: str, dest: Path) -> None:
        """Drop the unpacked extension files into ``dest``. Handles
        .zip / .crx / .tar.gz; raises ValueError on unknown format."""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmpf:
            tmpf.write(blob)
            tmpf.flush()
            tmp_path = Path(tmpf.name)
        try:
            if suffix == ".crx":
                # .crx = "Cr24" magic + signature header + ZIP payload.
                # Strip the header so zipfile sees a clean PK signature.
                self._unpack_crx(tmp_path, dest)
            elif suffix == ".zip":
                with zipfile.ZipFile(tmp_path) as zf:
                    self._safe_extract_zip(zf, dest)
            elif suffix == ".tar.gz":
                with tarfile.open(tmp_path, "r:gz") as tf:
                    self._safe_extract_tar(tf, dest)
            else:
                raise ValueError(f"unsupported suffix: {suffix}")
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        # If the archive wrapped everything in a single top-level dir
        # (common with .crx -> .zip -> single-dir layout), flatten it
        # so manifest.json ends up at the top of ``dest``.
        self._flatten_single_top_dir(dest)

    def _unpack_crx(self, crx_path: Path, dest: Path) -> None:
        """Parse the .crx header (v2 or v3), then extract the embedded
        ZIP. Reference:
          v2: 'Cr24' + version=2 + pubkeylen + siglen + payload
          v3: 'Cr24' + version=3 + headerlen + payload
        """
        data = crx_path.read_bytes()
        if data[:4] != b"Cr24":
            raise ValueError("not a .crx file (missing Cr24 magic)")
        import struct

        version = struct.unpack("<I", data[4:8])[0]
        if version == 2:
            pubkey_len = struct.unpack("<I", data[8:12])[0]
            sig_len = struct.unpack("<I", data[12:16])[0]
            zip_start = 16 + pubkey_len + sig_len
        elif version == 3:
            header_len = struct.unpack("<I", data[8:12])[0]
            zip_start = 12 + header_len
        else:
            raise ValueError(f"unsupported .crx version: {version}")
        zip_blob = data[zip_start:]
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
            zf.write(zip_blob)
            zf.flush()
            zip_path = Path(zf.name)
        try:
            with zipfile.ZipFile(zip_path) as z:
                self._safe_extract_zip(z, dest)
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass

    def _safe_extract_zip(self, zf: zipfile.ZipFile, dest: Path) -> None:
        """Like ``zf.extractall`` but blocks path traversal (../foo,
        absolute paths, symlinks) so an evil upload can't write
        outside ``dest``."""
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + ""):
                # Resolve the suspicious entry once more to be sure;
                # rejects "../" exits and absolute names.
                try:
                    target.relative_to(dest.resolve())
                except ValueError:
                    raise ValueError(f"refusing to extract path-traversal entry: {info.filename!r}")
            zf.extract(info, dest)

    def _safe_extract_tar(self, tf: tarfile.TarFile, dest: Path) -> None:
        for m in tf.getmembers():
            if m.issym() or m.islnk():
                continue
            target = (dest / m.name).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError:
                raise ValueError(f"refusing to extract path-traversal entry: {m.name!r}")
        tf.extractall(dest)

    def _flatten_single_top_dir(self, dest: Path) -> None:
        """If ``dest`` contains exactly one subdirectory (and no
        files / other dirs), move that subdir's contents up into
        ``dest``. Handles uploads where the operator zipped a whole
        ``my-extension/`` folder instead of zipping its contents."""
        entries = [p for p in dest.iterdir()]
        if len(entries) != 1:
            return
        sole = entries[0]
        if not sole.is_dir():
            return
        # Skip the flatten when manifest.json already lives at the top.
        if (dest / "manifest.json").exists():
            return
        if not (sole / "manifest.json").exists():
            return
        # Move children up, then drop the now-empty wrapper dir.
        for child in list(sole.iterdir()):
            shutil.move(str(child), str(dest / child.name))
        try:
            sole.rmdir()
        except OSError:
            pass

    def _validate_unpacked(self, dest: Path) -> None:
        """Reject uploads that aren't an unpacked Chrome extension --
        the canonical signal is a manifest.json at the top level."""
        manifest = dest / "manifest.json"
        if not manifest.exists():
            raise ValueError(
                "upload does not contain a manifest.json at the top "
                "level -- expected an unpacked Chrome extension "
                "directory (zip / crx / tar.gz of the unpacked dir)"
            )
        try:
            json.loads(manifest.read_text("utf-8", errors="replace"))
        except Exception as e:
            raise ValueError(f"manifest.json is not valid JSON: {e}")
