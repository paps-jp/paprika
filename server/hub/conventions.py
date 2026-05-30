"""Convention registry: short LLM-facing rules distilled from
failure→success diffs.

A "convention" is a one-bullet rule about how to write paprika-client
scripts -- typically a foot-gun the LLM stepped on in attempt N and
fixed in attempt N+1 of the same codegen-loop job. Conventions are
*always* injected into the codegen system prompt (curated tier only;
auto tier is for review). They are deliberately compact (under ~200
chars of advice + tiny good/bad example) so we can ship 20+ of them
without bloating the prompt.

Two tiers, like the skill registry:

    {data_dir}/conventions/auto/<slug>.json      # LLM-suggested
    {data_dir}/conventions/curated/<slug>.json   # operator-approved

Curated conventions ride along on every codegen-loop attempt. Auto
conventions stay on disk for the operator to review and promote if
they look useful.

Convention vs Skill -- recap of the distinction:
  * Skill   = reusable PATTERN (full code + instructions; retrieved
              top-K per job by the skill retriever).
  * Convention = atomic RULE (1-3 lines of advice + before/after; always
                 injected for curated).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from server.hub._jsonstore import atomic_write_json

ConventionTier = Literal["auto", "curated"]


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def normalise_slug(s: str) -> str:
    """Coerce arbitrary text to kebab-case-ascii (max 80 chars)."""
    s = (s or "").strip().lower()
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    s = s[:80]
    return s or "unnamed"


@dataclass
class ConventionRecord:
    """One atomic rule for paprika-client codegen.

    Field shape is borrowed from how Anthropic's own "best-practices"
    notes are structured: a short advice line, a rationale, and a
    side-by-side bad/good code snippet so the model sees both shapes.
    """

    slug: str
    name: str  # short title (<= 60 chars)
    advice: str  # the rule, 1-2 sentences, imperative
    rationale: str  # 1-2 sentences: why this matters
    bad_example: str = ""  # 1-5 lines of Python the rule forbids
    good_example: str = ""  # 1-5 lines of Python that follow the rule
    applicable_when: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    extracted_from: list[str] = field(default_factory=list)  # job_ids
    tier: ConventionTier = "auto"
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> ConventionRecord:
        return cls(
            slug=d.get("slug") or "",
            name=d.get("name") or d.get("slug") or "",
            advice=d.get("advice") or "",
            rationale=d.get("rationale") or "",
            bad_example=d.get("bad_example") or "",
            good_example=d.get("good_example") or "",
            applicable_when=list(d.get("applicable_when") or []),
            tags=list(d.get("tags") or []),
            extracted_from=list(d.get("extracted_from") or []),
            tier=d.get("tier") or "auto",
            use_count=int(d.get("use_count") or 0),
            created_at=d.get("created_at") or "",
            updated_at=d.get("updated_at") or "",
            last_used_at=d.get("last_used_at"),
        )

    def render_for_prompt(self) -> str:
        """Compact multi-line rendering used when injecting into the
        codegen system prompt. Kept short on purpose -- we want many
        conventions to fit in a context window."""
        lines = [f"- {self.advice.strip()}"]
        if self.rationale.strip():
            lines.append(f"  (Why: {self.rationale.strip()})")
        if self.bad_example.strip():
            lines.append("  BAD:")
            for ln in self.bad_example.strip().splitlines():
                lines.append(f"    {ln}")
        if self.good_example.strip():
            lines.append("  GOOD:")
            for ln in self.good_example.strip().splitlines():
                lines.append(f"    {ln}")
        return "\n".join(lines)


class ConventionRegistry:
    """File-backed CRUD over the two-tier convention store. Mirrors
    SkillRegistry; could be unified later, but the per-record shape
    differs enough that the small duplication is cheaper than a
    premature abstraction."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "conventions"
        (self.root / "auto").mkdir(parents=True, exist_ok=True)
        (self.root / "curated").mkdir(parents=True, exist_ok=True)

    def _tier_dir(self, tier: ConventionTier) -> Path:
        if tier not in ("auto", "curated"):
            raise ValueError(f"unknown tier: {tier!r}")
        return self.root / tier

    def _path(self, slug: str, tier: ConventionTier) -> Path:
        return self._tier_dir(tier) / f"{normalise_slug(slug)}.json"

    def list_all(self) -> list[ConventionRecord]:
        out: list[ConventionRecord] = []
        for tier in ("curated", "auto"):
            recs: list[ConventionRecord] = []
            for p in self._tier_dir(tier).glob("*.json"):
                try:
                    recs.append(
                        ConventionRecord.from_json(json.loads(p.read_text(encoding="utf-8")))
                    )
                except Exception:
                    pass
            recs.sort(key=lambda r: r.updated_at, reverse=True)
            out.extend(recs)
        return out

    def list_curated(self) -> list[ConventionRecord]:
        """Just the curated tier -- what gets injected into prompts."""
        return [c for c in self.list_all() if c.tier == "curated"]

    def get(self, slug: str, tier: ConventionTier | None = None) -> ConventionRecord | None:
        slug = normalise_slug(slug)
        tiers: tuple[ConventionTier, ...] = (tier,) if tier else ("curated", "auto")
        for t in tiers:
            p = self._path(slug, t)
            if p.exists():
                try:
                    return ConventionRecord.from_json(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    return None
        return None

    def upsert(
        self,
        slug: str,
        *,
        name: str,
        advice: str,
        rationale: str = "",
        bad_example: str = "",
        good_example: str = "",
        applicable_when: list[str] | None = None,
        tags: list[str] | None = None,
        extracted_from: list[str] | None = None,
        tier: ConventionTier = "auto",
    ) -> ConventionRecord:
        slug = normalise_slug(slug)
        if not slug:
            raise ValueError("slug cannot be empty")
        existing = self.get(slug, tier=tier)
        now = _utcnow_iso()
        # Merge extracted_from across re-extractions so provenance
        # accumulates rather than getting overwritten on each retry-
        # discovery of the same rule.
        merged_sources = list(extracted_from or [])
        if existing:
            for j in existing.extracted_from:
                if j and j not in merged_sources:
                    merged_sources.append(j)
        rec = ConventionRecord(
            slug=slug,
            name=(name or slug)[:120],
            advice=advice or "",
            rationale=rationale or "",
            bad_example=bad_example or "",
            good_example=good_example or "",
            applicable_when=list(applicable_when or []),
            tags=list(tags or []),
            extracted_from=merged_sources,
            tier=tier,
            use_count=(existing.use_count if existing else 0),
            created_at=(existing.created_at if existing and existing.created_at else now),
            updated_at=now,
            last_used_at=(existing.last_used_at if existing else None),
        )
        self._write(rec)
        return rec

    def delete(self, slug: str, tier: ConventionTier | None = None) -> bool:
        slug = normalise_slug(slug)
        tiers: tuple[ConventionTier, ...] = (tier,) if tier else ("curated", "auto")
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

    def promote(self, slug: str) -> ConventionRecord | None:
        slug = normalise_slug(slug)
        src = self._path(slug, "auto")
        if not src.exists():
            return None
        try:
            rec = ConventionRecord.from_json(json.loads(src.read_text(encoding="utf-8")))
        except Exception:
            return None
        rec.tier = "curated"
        rec.updated_at = _utcnow_iso()
        self._write(rec)
        try:
            src.unlink()
        except Exception:
            pass
        return rec

    def demote(self, slug: str) -> ConventionRecord | None:
        slug = normalise_slug(slug)
        src = self._path(slug, "curated")
        if not src.exists():
            return None
        try:
            rec = ConventionRecord.from_json(json.loads(src.read_text(encoding="utf-8")))
        except Exception:
            return None
        rec.tier = "auto"
        rec.updated_at = _utcnow_iso()
        self._write(rec)
        try:
            src.unlink()
        except Exception:
            pass
        return rec

    def bump_use(self, slug: str) -> ConventionRecord | None:
        rec = self.get(slug)
        if rec is None:
            return None
        rec.use_count += 1
        rec.last_used_at = _utcnow_iso()
        self._write(rec)
        return rec

    def _write(self, rec: ConventionRecord) -> None:
        # Atomic write so a crash mid-save can't corrupt the record.
        atomic_write_json(self._path(rec.slug, rec.tier), rec.to_json())


def render_conventions_block(
    conventions: list[ConventionRecord],
) -> str:
    """Build the multi-line block that gets prepended to the codegen
    system prompt. Empty string when no curated conventions.

    Header + bullet list; each bullet is the short advice plus an
    optional rationale + before/after snippet. The whole block is
    expected to stay under a few KB so it can travel with every
    prompt without bloating context."""
    if not conventions:
        return ""
    parts = [
        "=== Local conventions (learned from prior runs; follow these) ===",
        "",
    ]
    for c in conventions:
        parts.append(c.render_for_prompt())
        parts.append("")
    return "\n".join(parts)
