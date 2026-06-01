"""Storage for operator-recorder demonstrations (programming-by-
demonstration MVP1, M2: persistence layer).

Each "demonstration" is one ended-and-saved recording session: the
operator clicked through a flow, the agent extension captured the
events + bbox clips, the hub verbalised them via Qwen3-VL, and the
operator (via the admin UI) chose to keep the recording as a reusable
example.

File layout under ``<data>/oprec/``:

    index.jsonl                 # one demo per line, light metadata only
    demos/<id>.json             # the actual demonstration (events + clips)

The index is append-only JSONL because that lets us:

  * write a new line without touching everything else (O(1), safe
    against concurrent saves -- POSIX append on a single file)
  * tail / grep cheaply when looking up demos for a host
  * convert later to MariaDB without schema change (each line is
    already a row)

Deletes are TOMBSTONED in the index (line with ``deleted: true``) so
the file stays append-only; a future compaction can rewrite if the
file ever grows beyond comfort. Per-demo JSON files are removed from
disk immediately on delete.

Privacy: the events arrive with password fields already redacted by
content.js; we don't re-inspect on save. operator-provided note /
title are stored verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


log = logging.getLogger(__name__)


# Cap per demo to keep the JSON file under a couple of MB. clip data
# URLs are ~10-30 KB each, so 200 events = ~5 MB worst case. Reading +
# parsing that on every detail view is fine; if a demo exceeds the cap
# we just truncate at save time and note it in the metadata.
MAX_EVENTS_PER_DEMO = int(os.environ.get("OPREC_MAX_EVENTS_PER_DEMO", "200"))

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_HOST_STRIP = re.compile(r"^www\.")


def _normalise_host(host: str) -> str:
    """Lowercase + drop leading www. so example.com == www.example.com.

    Same convention as ``server.hub.hosts`` so a demo saved against
    www.example.com surfaces when an operator filters by example.com.
    """
    h = (host or "").strip().lower()
    return _HOST_STRIP.sub("", h)


def _host_of_url(url: str) -> str:
    if not url:
        return ""
    try:
        return _normalise_host(urlparse(url).hostname or "")
    except Exception:
        return ""


def _new_id() -> str:
    """Sortable id: ``oprec_<unix-ms>_<6chars>``. The timestamp prefix
    keeps natural file ordering chronological."""
    return f"oprec_{int(time.time() * 1000)}_{secrets.token_urlsafe(4)[:6]}"


@dataclass
class DemoIndex:
    """One line in index.jsonl. The demo body is in demos/<id>.json."""
    id: str
    host: str
    start_url: str
    title: str = ""
    note: str = ""
    event_count: int = 0
    clip_count: int = 0
    created_at: int = 0     # unix ms
    updated_at: int = 0     # unix ms
    deleted: bool = False   # tombstone marker

    def to_jsonl(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, default=str)

    @classmethod
    def from_jsonl(cls, line: str) -> "DemoIndex | None":
        line = line.strip()
        if not line:
            return None
        try:
            d = json.loads(line)
        except Exception:
            return None
        return cls(
            id=str(d.get("id") or ""),
            host=str(d.get("host") or ""),
            start_url=str(d.get("start_url") or ""),
            title=str(d.get("title") or ""),
            note=str(d.get("note") or ""),
            event_count=int(d.get("event_count") or 0),
            clip_count=int(d.get("clip_count") or 0),
            created_at=int(d.get("created_at") or 0),
            updated_at=int(d.get("updated_at") or 0),
            deleted=bool(d.get("deleted") or False),
        )


@dataclass
class DemoBody:
    """The actual events + metadata. Serialised to demos/<id>.json."""
    id: str
    host: str
    start_url: str
    title: str
    note: str
    created_at: int
    events: list[dict] = field(default_factory=list)

    def event_count(self) -> int:
        return len(self.events)

    def clip_count(self) -> int:
        return sum(1 for e in self.events if e and e.get("clip"))

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "start_url": self.start_url,
            "title": self.title,
            "note": self.note,
            "created_at": self.created_at,
            "events": self.events,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class OpRecStore:
    """File-backed demonstration store.

    Thread-safe via a single lock on the index write path. Reads are
    lock-free; readers may briefly see an inconsistent view (post-
    crash, mid-write) but the JSONL format auto-skips a partial last
    line.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.index_path = self.root / "index.jsonl"
        self.demos_dir = self.root / "demos"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.demos_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.index_path.touch()

    # ----- index r/w ------------------------------------------------------
    def _append_index(self, entry: DemoIndex) -> None:
        with self._lock:
            with self.index_path.open("a", encoding="utf-8") as f:
                f.write(entry.to_jsonl())
                f.write("\n")

    def _scan_index(self) -> dict[str, DemoIndex]:
        """Return id -> latest DemoIndex entry. Later lines win
        (deletes / updates layered on top of older inserts)."""
        out: dict[str, DemoIndex] = {}
        try:
            with self.index_path.open("r", encoding="utf-8") as f:
                for line in f:
                    e = DemoIndex.from_jsonl(line)
                    if e is None or not e.id:
                        continue
                    out[e.id] = e
        except FileNotFoundError:
            pass
        return out

    # ----- body r/w -------------------------------------------------------
    def _body_path(self, id_: str) -> Path:
        return self.demos_dir / f"{id_}.json"

    def _write_body(self, body: DemoBody) -> None:
        p = self._body_path(body.id)
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(body.to_json(), f, ensure_ascii=False, indent=2)
        tmp.replace(p)  # atomic on POSIX

    def _read_body(self, id_: str) -> DemoBody | None:
        p = self._body_path(id_)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            return None
        return DemoBody(
            id=str(d.get("id") or id_),
            host=str(d.get("host") or ""),
            start_url=str(d.get("start_url") or ""),
            title=str(d.get("title") or ""),
            note=str(d.get("note") or ""),
            created_at=int(d.get("created_at") or 0),
            events=list(d.get("events") or []),
        )

    # ----- public API -----------------------------------------------------
    def save(
        self,
        *,
        events: list[dict],
        start_url: str,
        title: str = "",
        note: str = "",
    ) -> DemoIndex:
        """Persist a new demonstration. Returns the index entry."""
        # Bound the events array so the JSON file stays a sensible size.
        truncated = False
        if len(events) > MAX_EVENTS_PER_DEMO:
            events = events[:MAX_EVENTS_PER_DEMO]
            truncated = True
        now = int(time.time() * 1000)
        host = _host_of_url(start_url)
        demo_id = _new_id()
        if not title:
            # Default title: <host> · <event count> events
            title = f"{host or '(no host)'} · {len(events)} events"
        if truncated:
            note = (note + "\n" if note else "") + (
                f"[truncated to {MAX_EVENTS_PER_DEMO} events at save time]"
            )
        body = DemoBody(
            id=demo_id,
            host=host,
            start_url=start_url,
            title=title,
            note=note,
            created_at=now,
            events=list(events),
        )
        self._write_body(body)
        idx = DemoIndex(
            id=demo_id,
            host=host,
            start_url=start_url,
            title=title,
            note=note,
            event_count=body.event_count(),
            clip_count=body.clip_count(),
            created_at=now,
            updated_at=now,
        )
        self._append_index(idx)
        return idx

    def list(
        self,
        *,
        host: str | None = None,
        limit: int = 50,
    ) -> list[DemoIndex]:
        """Return up to ``limit`` non-deleted demos, newest first.
        Optional ``host`` filter is matched on the normalised host
        (lowercase, leading www stripped)."""
        idx = self._scan_index()
        wanted_host = _normalise_host(host) if host else None
        out = [
            e for e in idx.values()
            if not e.deleted
            and (wanted_host is None or e.host == wanted_host)
        ]
        out.sort(key=lambda e: e.created_at, reverse=True)
        return out[:limit]

    def get(self, demo_id: str) -> DemoBody | None:
        # Honour tombstones: a deleted demo's index entry exists but
        # we treat get() as 404 to match the public contract.
        idx = self._scan_index().get(demo_id)
        if idx is None or idx.deleted:
            return None
        return self._read_body(demo_id)

    def get_index(self, demo_id: str) -> DemoIndex | None:
        e = self._scan_index().get(demo_id)
        if e is None or e.deleted:
            return None
        return e

    def patch(
        self,
        demo_id: str,
        *,
        title: str | None = None,
        note: str | None = None,
    ) -> DemoIndex | None:
        """Update operator-editable fields. Other fields are immutable."""
        cur = self.get_index(demo_id)
        if cur is None:
            return None
        body = self._read_body(demo_id)
        if body is None:
            return None
        now = int(time.time() * 1000)
        if title is not None:
            cur.title = str(title)
            body.title = str(title)
        if note is not None:
            cur.note = str(note)
            body.note = str(note)
        cur.updated_at = now
        self._write_body(body)
        self._append_index(cur)
        return cur

    def delete(self, demo_id: str) -> bool:
        cur = self.get_index(demo_id)
        if cur is None:
            return False
        cur.deleted = True
        cur.updated_at = int(time.time() * 1000)
        self._append_index(cur)
        # Remove the body file -- saves disk; the index keeps the
        # tombstone so list() naturally hides the deleted demo.
        try:
            self._body_path(demo_id).unlink()
        except FileNotFoundError:
            pass
        return True


# ---------------------------------------------------------------------------
# Module-level singleton (initialised lazily by the route)
# ---------------------------------------------------------------------------
_STORE: OpRecStore | None = None


def get_store() -> OpRecStore:
    global _STORE
    if _STORE is None:
        from server.hub._state import get_storage_dir
        _STORE = OpRecStore(get_storage_dir() / "oprec")
    return _STORE
