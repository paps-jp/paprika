"""Shared JSON-on-disk primitives for the hub's file-backed registries.

Every ``*Registry`` under ``server/hub/`` (hosts, profiles, engines,
presets, skills, conventions, extensions, settings, host_visited) keeps
its records as one JSON file per record under ``{data_dir}/<subdir>/``.
Historically each one re-implemented the write step, and they drifted:
some wrote atomically (``.tmp`` + rename), several used a plain
``path.write_text`` that leaves a truncated / corrupt file if the process
dies mid-write. :func:`atomic_write_json` is the single correct
implementation they should all call.

:class:`JsonRecordRegistry` is an optional generic base for NEW registries
(and for migrating the simpler existing ones): it provides the shared
``_path`` / ``list_all`` / ``get`` / ``delete`` / ``_write`` so a concrete
registry only supplies the record (de)serialisation + slug + sort key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")


def atomic_write_json(
    path: str | os.PathLike,
    data,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    The bytes are written to a sibling ``*.tmp`` file, flushed +
    ``fsync``-ed, then ``os.replace``-d over the target. ``os.replace``
    is atomic on the same filesystem on both POSIX and Windows, so a
    reader (or a crash) never observes a half-written record -- it sees
    either the old file or the complete new one. The parent directory is
    created if missing.

    Use this for every config-registry write so a power loss / OOM-kill
    mid-save can't corrupt the operator's hosts / engines / settings.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        # Best-effort cleanup of the temp file so a failed write doesn't
        # litter the directory with ``*.tmp`` debris.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class JsonRecordRegistry(Generic[T]):
    """Generic file-backed CRUD over ``{data_dir}/<subdir>/<slug>.json``.

    One JSON file per record. Subclasses configure the behaviour by
    setting :attr:`subdir` and supplying the (de)serialisation + key
    helpers; everything else (list / get / delete / atomic write) is
    inherited. This is the de-duplicated form of the ~10 hand-rolled
    registries -- new registries should extend this rather than copy a
    sibling.

    Concrete subclass contract::

        class FooRegistry(JsonRecordRegistry[FooRecord]):
            subdir = "foos"

            def _slug(self, key: str) -> str: ...        # filename stem
            def _key_of(self, rec: FooRecord) -> str: ... # rec -> key
            def _to_json(self, rec: FooRecord) -> dict: ...
            def _from_json(self, d: dict) -> FooRecord: ...
            # optional: override _sort_key for list ordering
    """

    subdir: str = ""

    def __init__(self, data_dir: str | os.PathLike) -> None:
        if not self.subdir:
            raise ValueError(
                f"{type(self).__name__} must set a class-level `subdir`"
            )
        self.dir = Path(data_dir) / self.subdir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- subclass hooks ---------------------------------------------------

    def _slug(self, key: str) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _key_of(self, rec: T) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _to_json(self, rec: T) -> dict:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _from_json(self, d: dict) -> T:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    # When True, :func:`list_all` sorts by ``_sort_key`` descending
    # (e.g. newest-first by a timestamp key). Default ascending.
    _sort_reverse: bool = False

    def _sort_key(self, rec: T):
        """Ordering for :func:`list_all`. Default: no sort (insertion /
        glob order). Override for a stable display order."""
        return 0

    # ---- generic CRUD -----------------------------------------------------

    def _path(self, key: str) -> Path:
        return self.dir / f"{self._slug(key)}.json"

    def list_all(self) -> list[T]:
        records: list[T] = []
        for p in sorted(self.dir.glob("*.json")):
            # Skip sentinel / helper files that share the registry
            # directory but are NOT records. Convention: any filename
            # starting with ``_`` is reserved for the registry's own
            # internal use (e.g. EngineRegistry's ``_usage.json``
            # counter file). Without this skip, those helpers got fed
            # through ``_from_json`` -- which usually tolerated the
            # bad shape via ``.get()`` defaults and produced a phantom
            # record (e.g. an empty engine that normalised to
            # slug="unnamed"), inflating list_all counts and confusing
            # downstream code that expected one record per real file.
            if p.name.startswith("_"):
                continue
            try:
                records.append(self._from_json(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                # Corrupt / unreadable file: skip so one bad record
                # doesn't blank the whole listing.
                continue
        try:
            records.sort(key=self._sort_key, reverse=self._sort_reverse)
        except Exception:
            pass
        return records

    def get(self, key: str) -> T | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return self._from_json(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        try:
            p.unlink()
            return True
        except Exception:
            return False

    def _write(self, rec: T) -> None:
        atomic_write_json(self._path(self._key_of(rec)), self._to_json(rec))


class TieredJsonRecordRegistry(Generic[T]):
    """Generic file-backed CRUD over *tiered* stores:
    ``{data_dir}/<subdir>/<tier>/<slug>.json``, one JSON file per record.

    For registries whose records live in priority-ordered tiers -- e.g.
    skills / conventions, which keep a ``curated`` tier that shadows an
    ``auto`` tier. ``list_all`` / ``get`` / ``delete`` walk :attr:`tiers`
    in order (first entry = highest priority, searched first). This is
    the de-duplicated form of the two hand-rolled two-tier registries.

    Concrete subclass contract::

        class FooRegistry(TieredJsonRecordRegistry[FooRecord]):
            subdir = "foos"
            tiers = ("curated", "auto")     # priority order

            def _slug(self, key): ...
            def _key_of(self, rec): ...
            def _tier_of(self, rec): ...     # which tier a record belongs in
            def _to_json(self, rec): ...
            def _from_json(self, d): ...
            # optional: _sort_key (within-tier) + _sort_reverse
    """

    subdir: str = ""
    tiers: tuple[str, ...] = ()
    _sort_reverse: bool = False

    def __init__(self, data_dir: str | os.PathLike) -> None:
        if not self.subdir:
            raise ValueError(
                f"{type(self).__name__} must set a class-level `subdir`"
            )
        if not self.tiers:
            raise ValueError(
                f"{type(self).__name__} must set a non-empty `tiers`"
            )
        self.root = Path(data_dir) / self.subdir
        for t in self.tiers:
            (self.root / t).mkdir(parents=True, exist_ok=True)

    # ---- subclass hooks ---------------------------------------------------

    def _slug(self, key: str) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _key_of(self, rec: T) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _tier_of(self, rec: T) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _to_json(self, rec: T) -> dict:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _from_json(self, d: dict) -> T:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _sort_key(self, rec: T):
        """Within-tier ordering for :func:`list_all`. Default: no sort."""
        return 0

    # ---- generic CRUD -----------------------------------------------------

    def _tier_dir(self, tier: str) -> Path:
        if tier not in self.tiers:
            raise ValueError(f"unknown tier: {tier!r}")
        return self.root / tier

    def _path(self, key: str, tier: str) -> Path:
        return self._tier_dir(tier) / f"{self._slug(key)}.json"

    def list_all(self) -> list[T]:
        """All records, tiers concatenated in :attr:`tiers` order; each
        tier independently sorted by ``_sort_key``."""
        out: list[T] = []
        for tier in self.tiers:
            recs: list[T] = []
            for p in self._tier_dir(tier).glob("*.json"):
                try:
                    recs.append(self._from_json(json.loads(p.read_text(encoding="utf-8"))))
                except Exception:
                    continue
            try:
                recs.sort(key=self._sort_key, reverse=self._sort_reverse)
            except Exception:
                pass
            out.extend(recs)
        return out

    def get(self, key: str, tier: str | None = None) -> T | None:
        """Look up by key. ``tier=None`` searches every tier in priority
        order and returns the first hit."""
        search = (tier,) if tier else self.tiers
        for t in search:
            p = self._path(key, t)
            if p.exists():
                try:
                    return self._from_json(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    return None
        return None

    def delete(self, key: str, tier: str | None = None) -> bool:
        """Delete from ``tier`` (or every tier when None). True iff any
        file was removed."""
        search = (tier,) if tier else self.tiers
        removed = False
        for t in search:
            p = self._path(key, t)
            if p.exists():
                try:
                    p.unlink()
                    removed = True
                except Exception:
                    pass
        return removed

    def _write(self, rec: T) -> None:
        atomic_write_json(
            self._path(self._key_of(rec), self._tier_of(rec)),
            self._to_json(rec),
        )


def iter_json_files(directory: Path) -> Iterable[Path]:
    """Sorted ``*.json`` files in a directory (skips ``*.tmp``)."""
    return sorted(directory.glob("*.json"))


def load_records(
    directory: Path,
    from_json: Callable[[dict], T],
) -> list[T]:
    """Load + parse every ``*.json`` in ``directory``, skipping corrupt
    files. Standalone helper for registries that can't (yet) extend
    :class:`JsonRecordRegistry`."""
    out: list[T] = []
    for p in iter_json_files(directory):
        try:
            out.append(from_json(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out
