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
from typing import Literal

from server.hub._jsonstore import TieredJsonRecordRegistry

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


class ConventionRegistry(TieredJsonRecordRegistry[ConventionRecord]):
    """File-backed CRUD over the two-tier convention store. Inherits the
    generic tiered list / get / delete / atomic-write from
    :class:`TieredJsonRecordRegistry`; only the convention-specific
    (de)serialisation + the upsert / promote / demote / bump_use helpers
    + the ``list_curated`` view live here."""

    subdir = "conventions"
    # ``curated`` shadows ``auto`` -- searched first, listed first.
    tiers = ("curated", "auto")
    _sort_reverse = True  # within each tier, most-recently-updated first

    # ---- TieredJsonRecordRegistry hooks -----------------------------------

    def _slug(self, key: str) -> str:
        return normalise_slug(key)

    def _key_of(self, rec: ConventionRecord) -> str:
        return rec.slug

    def _tier_of(self, rec: ConventionRecord) -> str:
        return rec.tier

    def _to_json(self, rec: ConventionRecord) -> dict:
        return rec.to_json()

    def _from_json(self, d: dict) -> ConventionRecord:
        return ConventionRecord.from_json(d)

    def _sort_key(self, rec: ConventionRecord):
        return rec.updated_at

    # ---- convention-specific helpers --------------------------------------

    def list_curated(self) -> list[ConventionRecord]:
        """Just the curated tier -- what gets injected into prompts."""
        return [c for c in self.list_all() if c.tier == "curated"]

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
