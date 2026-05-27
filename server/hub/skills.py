"""Skill registry: reusable patterns the LLM distils from successful
codegen-loop jobs.

The hub watches every codegen-loop SUCCESS and asks the LLM to
abstract the winning script into a generalisable "skill" -- a short
description + code template + LLM-facing instructions that future
jobs can pull in as additional context. Site-specific scraps (a
URL, a CSS selector, baked-in cookies) get filtered out by the
distillation prompt; only patterns that the LLM thinks will help
some other job survive into the registry.

Storage layout::

    {data_dir}/skills/auto/<slug>.json      # LLM-extracted, low trust
    {data_dir}/skills/curated/<slug>.json   # operator-promoted

The two tiers exist so the auto folder can be aggressive without
fearing noise -- the operator periodically grooms it (the Admin UI
``🛠 Skills`` tab) and promotes the keepers to ``curated/``. The
LLM retrieval path biases toward curated skills.

Slugs are kebab-case (``crawl-paginated-gallery``,
``bypass-age-gate-with-agent``). The filename mirrors the slug so
operator-side editing is straightforward.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

SkillTier = Literal["auto", "curated"]


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def normalise_slug(s: str) -> str:
    """Coerce arbitrary text to ``kebab-case-ascii`` (max 80 chars).

    Numbers and ``-`` survive; everything else collapses to ``-``.
    Leading/trailing ``-`` are stripped. Empty slug → ``unnamed``."""
    s = (s or "").strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    s = s[:80]
    return s or "unnamed"


@dataclass
class SkillRecord:
    """One reusable pattern. The split between ``code_template`` and
    ``llm_instructions`` is the same split Anthropic's skills use:

      * ``code_template`` -- compilable Python the operator can paste
        into Code mode (with placeholder URL / config bits). Drives
        the Submit tab's ``▸ load from skill`` dropdown.
      * ``llm_instructions`` -- prose the codegen-loop appends to its
        system prompt when this skill is judged relevant for a new
        job. Drives the auto-improvement loop.

    The two are independent: a skill can be useful for LLMs without
    being a complete script, or be a useful copy-paste without much
    LLM advice."""

    slug: str
    name: str
    description: str  # one-liner shown in lists + used for retrieval
    code_template: str  # full Python (paste-ready)
    llm_instructions: str  # prose injected into codegen system prompt
    applicable_when: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    auto_extracted: bool = True  # True for auto/, False for hand-written curated/
    extracted_from: list[str] = field(default_factory=list)  # job_ids
    tier: SkillTier = "auto"  # "auto" or "curated" (must match folder)
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> SkillRecord:
        # Be tolerant of older shapes (missing fields default to "" / []).
        return cls(
            slug=d.get("slug") or "",
            name=d.get("name") or d.get("slug") or "",
            description=d.get("description") or "",
            code_template=d.get("code_template") or "",
            llm_instructions=d.get("llm_instructions") or "",
            applicable_when=list(d.get("applicable_when") or []),
            tags=list(d.get("tags") or []),
            auto_extracted=bool(d.get("auto_extracted", True)),
            extracted_from=list(d.get("extracted_from") or []),
            tier=d.get("tier") or ("auto" if d.get("auto_extracted", True) else "curated"),
            use_count=int(d.get("use_count") or 0),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            last_used_at=d.get("last_used_at"),
        )


class SkillRegistry:
    """File-backed CRUD over per-tier skill stores. O(1) per record,
    O(N) for list (fine at paprika's scale -- tens, not millions)."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "skills"
        (self.root / "auto").mkdir(parents=True, exist_ok=True)
        (self.root / "curated").mkdir(parents=True, exist_ok=True)

    def _tier_dir(self, tier: SkillTier) -> Path:
        if tier not in ("auto", "curated"):
            raise ValueError(f"unknown tier: {tier!r}")
        return self.root / tier

    def _path(self, slug: str, tier: SkillTier) -> Path:
        return self._tier_dir(tier) / f"{normalise_slug(slug)}.json"

    # ----- read --------------------------------------------------------

    def list_all(self) -> list[SkillRecord]:
        """Both tiers. ``curated`` first, then ``auto``, each sorted by
        most-recently-updated."""
        out: list[SkillRecord] = []
        for tier in ("curated", "auto"):
            recs: list[SkillRecord] = []
            for p in self._tier_dir(tier).glob("*.json"):
                try:
                    recs.append(SkillRecord.from_json(json.loads(p.read_text(encoding="utf-8"))))
                except Exception:
                    # Corrupt file -- skip; operator will see the gap in
                    # the UI and can re-save manually.
                    pass
            recs.sort(key=lambda r: r.updated_at, reverse=True)
            out.extend(recs)
        return out

    def get(self, slug: str, tier: SkillTier | None = None) -> SkillRecord | None:
        """Look up by slug. If ``tier`` is None, ``curated`` is
        searched first (since promoted skills shadow their auto
        counterparts)."""
        slug = normalise_slug(slug)
        tiers: tuple[SkillTier, ...] = (tier,) if tier else ("curated", "auto")
        for t in tiers:
            p = self._path(slug, t)
            if p.exists():
                try:
                    return SkillRecord.from_json(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    return None
        return None

    # ----- write -------------------------------------------------------

    def upsert(
        self,
        slug: str,
        *,
        name: str,
        description: str,
        code_template: str,
        llm_instructions: str,
        applicable_when: list[str] | None = None,
        tags: list[str] | None = None,
        auto_extracted: bool = True,
        extracted_from: list[str] | None = None,
        tier: SkillTier = "auto",
    ) -> SkillRecord:
        slug = normalise_slug(slug)
        if not slug:
            raise ValueError("slug cannot be empty")
        existing = self.get(slug, tier=tier)
        now = _utcnow_iso()
        # Merge extracted_from across re-runs of the distiller -- we want
        # the provenance list to grow as the same pattern is rediscovered.
        merged_sources = list(extracted_from or [])
        if existing:
            for j in existing.extracted_from:
                if j and j not in merged_sources:
                    merged_sources.append(j)
        rec = SkillRecord(
            slug=slug,
            name=(name or slug)[:120],
            description=description or "",
            code_template=code_template or "",
            llm_instructions=llm_instructions or "",
            applicable_when=list(applicable_when or []),
            tags=list(tags or []),
            auto_extracted=auto_extracted,
            extracted_from=merged_sources,
            tier=tier,
            use_count=(existing.use_count if existing else 0),
            created_at=(existing.created_at if existing and existing.created_at else now),
            updated_at=now,
            last_used_at=(existing.last_used_at if existing else None),
        )
        self._write(rec)
        return rec

    def delete(self, slug: str, tier: SkillTier | None = None) -> bool:
        slug = normalise_slug(slug)
        tiers: tuple[SkillTier, ...] = (tier,) if tier else ("curated", "auto")
        any_removed = False
        for t in tiers:
            p = self._path(slug, t)
            if p.exists():
                try:
                    p.unlink()
                    any_removed = True
                except Exception:
                    pass
        return any_removed

    def promote(self, slug: str) -> SkillRecord | None:
        """Move a skill from ``auto/`` to ``curated/``. The promoted
        record loses its ``auto_extracted`` flag (since an operator
        signed off on it) and its ``tier`` flips. Returns the record
        in its new tier, or None if the slug isn't in auto/."""
        slug = normalise_slug(slug)
        src = self._path(slug, "auto")
        if not src.exists():
            return None
        try:
            rec = SkillRecord.from_json(json.loads(src.read_text(encoding="utf-8")))
        except Exception:
            return None
        rec.tier = "curated"
        rec.auto_extracted = False
        rec.updated_at = _utcnow_iso()
        self._write(rec)
        try:
            src.unlink()
        except Exception:
            pass
        return rec

    def demote(self, slug: str) -> SkillRecord | None:
        """Move a skill from ``curated/`` back to ``auto/``. Reverse
        of promote; useful when the operator regrets a promotion."""
        slug = normalise_slug(slug)
        src = self._path(slug, "curated")
        if not src.exists():
            return None
        try:
            rec = SkillRecord.from_json(json.loads(src.read_text(encoding="utf-8")))
        except Exception:
            return None
        rec.tier = "auto"
        rec.auto_extracted = True
        rec.updated_at = _utcnow_iso()
        self._write(rec)
        try:
            src.unlink()
        except Exception:
            pass
        return rec

    def bump_use(self, slug: str) -> SkillRecord | None:
        """Increment ``use_count`` + set ``last_used_at``. Called once
        per codegen-loop job per skill picked by the retriever."""
        rec = self.get(slug)
        if rec is None:
            return None
        rec.use_count += 1
        rec.last_used_at = _utcnow_iso()
        self._write(rec)
        return rec

    def _write(self, rec: SkillRecord) -> None:
        path = self._path(rec.slug, rec.tier)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(rec.to_json(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
