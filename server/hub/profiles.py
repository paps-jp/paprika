"""Operator Chrome profile registry.

Stores Chrome ``User Data``-shaped tarballs the operator uploaded
from their own machine (via ``paprika-client upload-profile``) so the
worker fleet can launch Chrome with ``--user-data-dir=<extracted>``
and start a session that is already logged in to the operator's
sites, with their cookies, localStorage, IndexedDB, autofill, etc.

The local equivalent (``options.clone_chrome_profile``) only works
when the hub itself runs on the operator's machine -- on a Linux
worker fleet there is no operator Chrome to clone from. This
registry is the worker-fleet bridge: operator uploads once
(`POST /profiles/{name}`), every subsequent job that sets
`options.use_profile = "{name}"` gets a fresh extraction of that
tarball as its `--user-data-dir`.

Storage layout::

    {data_dir}/profiles/<safe-name>.tar.gz   # the tarball
    {data_dir}/profiles/<safe-name>.meta.json # uploaded_at, size, etc

One file per profile. The same profile can back many jobs in
parallel -- each worker extracts its own copy to a per-job scratch
dir before launching Chrome, so the tarball stays read-only and
Chrome's profile-lock can't fight across workers.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Profile names go straight into URLs and filenames, so keep the
# allowed character set tight. We accept ASCII alphanumeric + a few
# separators; everything else is rejected at the API boundary (rather
# than silently rewritten) so the operator notices typos.
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


def is_valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name))


@dataclass
class ProfileMeta:
    """Metadata about an uploaded profile. Persisted next to the
    tarball as ``<name>.meta.json``. Includes only operator-friendly
    fields -- file paths / checksums / etc. are NOT stored here, the
    filesystem is the source of truth for those."""

    name: str
    size_bytes: int
    uploaded_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    source_machine: str | None = None  # informational only
    chrome_profile_name: str | None = None  # "Default" / "Profile 1" etc.
    note: str | None = None  # free-text operator note

    def to_json(self) -> dict:
        d = asdict(self)
        # Stringify datetimes for JSON. Suffix with Z so JS / curl
        # operators see they're UTC.
        d["uploaded_at"] = self.uploaded_at.isoformat() + "Z"
        d["updated_at"] = self.updated_at.isoformat() + "Z"
        return d

    @classmethod
    def from_json(cls, d: dict) -> ProfileMeta:
        def _parse_ts(s: str | None) -> datetime:
            if not s:
                return datetime.utcnow()
            return datetime.fromisoformat(s.rstrip("Z"))

        return cls(
            name=d["name"],
            size_bytes=int(d.get("size_bytes") or 0),
            uploaded_at=_parse_ts(d.get("uploaded_at")),
            updated_at=_parse_ts(d.get("updated_at")),
            source_machine=d.get("source_machine"),
            chrome_profile_name=d.get("chrome_profile_name"),
            note=d.get("note"),
        )


class ProfileRegistry:
    """On-disk profile-tarball store.

    All methods are sync (filesystem IO is fast). The hub wraps the
    write methods in a single asyncio.Lock to serialise concurrent
    uploads.
    """

    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "profiles"
        self.root.mkdir(parents=True, exist_ok=True)

    # ----- path helpers -----------------------------------------------

    def _tarball_path(self, name: str) -> Path:
        return self.root / f"{name}.tar.gz"

    def _meta_path(self, name: str) -> Path:
        return self.root / f"{name}.meta.json"

    # The "default" profile is the one the hub auto-applies when a
    # job is submitted without an explicit ``options.use_profile``.
    # Stored as a single-line file holding the name (or absent /
    # empty when no default is set). Kept separate from the per-
    # profile meta.json so it stays a single source of truth that
    # can't go inconsistent across multiple meta files.
    _DEFAULT_FILE = "_default.txt"

    def _default_path(self) -> Path:
        return self.root / self._DEFAULT_FILE

    # ----- default --------------------------------------------------

    def get_default(self) -> str | None:
        """Return the name of the profile marked as default, or None.

        Self-heals: if the default points at a profile that no
        longer exists (operator deleted it without unsetting), we
        return None and silently scrub the stale file so a stale
        default can't pin the dispatch path forever.
        """
        p = self._default_path()
        if not p.exists():
            return None
        try:
            name = p.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not name or not self.exists(name):
            try:
                p.unlink()
            except OSError:
                pass
            return None
        return name

    def set_default(self, name: str | None) -> None:
        """Mark ``name`` as the auto-applied default. Pass None to
        clear. Validates that the profile exists; raises ValueError
        otherwise so the API can return a 404.
        """
        p = self._default_path()
        if name is None or name == "":
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            return
        if not is_valid_name(name):
            raise ValueError(f"invalid profile name: {name!r}")
        if not self.exists(name):
            raise ValueError(f"profile '{name}' not found")
        p.write_text(name, encoding="utf-8")

    # ----- read -------------------------------------------------------

    def exists(self, name: str) -> bool:
        return self._tarball_path(name).exists()

    def etag(self, name: str) -> str | None:
        """Opaque cache key for the tarball. Workers compare this to
        decide whether their cached extraction is still valid.

        Derived from (size, mtime_ns) of the tarball so any re-upload
        of the same name produces a fresh etag. Cheaper than a content
        hash and good enough for "did the operator re-upload?".
        Returns None when the profile doesn't exist.
        """
        p = self._tarball_path(name)
        if not p.exists():
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        return f"{st.st_size}-{st.st_mtime_ns}"

    def get_meta(self, name: str) -> ProfileMeta | None:
        p = self._meta_path(name)
        if not p.exists():
            # Synthesise from filesystem if .meta.json went missing
            # (e.g. operator scp'd a tarball in by hand). The tarball
            # itself is the authoritative artifact.
            tar = self._tarball_path(name)
            if not tar.exists():
                return None
            return ProfileMeta(name=name, size_bytes=tar.stat().st_size)
        try:
            return ProfileMeta.from_json(json.loads(p.read_text("utf-8")))
        except Exception:
            return None

    def get_tarball_path(self, name: str) -> Path | None:
        p = self._tarball_path(name)
        return p if p.exists() else None

    def list(self) -> list[ProfileMeta]:
        out: list[ProfileMeta] = []
        for tar in sorted(self.root.glob("*.tar.gz")):
            name = tar.name[: -len(".tar.gz")]
            m = self.get_meta(name)
            if m is not None:
                out.append(m)
        # Most-recent first matches the UX in /hosts and /jobs.
        out.sort(key=lambda x: x.updated_at, reverse=True)
        return out

    # ----- write ------------------------------------------------------

    def save(
        self,
        name: str,
        *,
        tarball_bytes: bytes | None = None,
        tarball_src: Path | None = None,
        source_machine: str | None = None,
        chrome_profile_name: str | None = None,
        note: str | None = None,
    ) -> ProfileMeta:
        """Persist a profile. Either ``tarball_bytes`` (in-memory) or
        ``tarball_src`` (path to a temp file the caller streamed to)
        must be provided -- the second is preferred for large uploads
        so we don't double the memory hit.

        Overwrites any existing profile of the same name. The new
        ``uploaded_at`` mirrors ``updated_at`` only on first save; on
        re-upload ``uploaded_at`` stays as it was so operators can
        see "first registered N days ago".
        """
        if not is_valid_name(name):
            raise ValueError(f"invalid profile name: {name!r}")
        tar_path = self._tarball_path(name)
        # Atomic move so a half-written tarball is never observable.
        tmp_path = tar_path.with_suffix(tar_path.suffix + ".tmp")
        if tarball_src is not None:
            shutil.move(str(tarball_src), str(tmp_path))
        elif tarball_bytes is not None:
            tmp_path.write_bytes(tarball_bytes)
        else:
            raise ValueError("save() requires tarball_bytes or tarball_src")
        tmp_path.replace(tar_path)

        # Merge metadata: preserve original uploaded_at across re-upload.
        prev = self.get_meta(name)
        meta = ProfileMeta(
            name=name,
            size_bytes=tar_path.stat().st_size,
            uploaded_at=(prev.uploaded_at if prev else datetime.utcnow()),
            updated_at=datetime.utcnow(),
            source_machine=source_machine or (prev.source_machine if prev else None),
            chrome_profile_name=chrome_profile_name or (prev.chrome_profile_name if prev else None),
            note=note if note is not None else (prev.note if prev else None),
        )
        self._meta_path(name).write_text(
            json.dumps(meta.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return meta

    def remove(self, name: str) -> bool:
        """Delete the tarball + metadata. Returns True if anything was
        removed, False if the name didn't exist. Also clears the
        default-profile pointer if it was pointing at ``name``."""
        if not is_valid_name(name):
            return False
        removed = False
        for p in (self._tarball_path(name), self._meta_path(name)):
            if p.exists():
                try:
                    p.unlink()
                    removed = True
                except OSError:
                    pass
        # If the default was pointing here, scrub it -- otherwise the
        # next dispatch would try to use a profile that no longer
        # exists. get_default() would self-heal but doing it here
        # makes the state correct even if get_default isn't called
        # before the next list().
        p = self._default_path()
        if p.exists():
            try:
                if p.read_text(encoding="utf-8").strip() == name:
                    p.unlink()
            except OSError:
                pass
        return removed
