"""Migration script: v1 stores → v2 HostKnowledge.

Reads existing paprika data:

  * ``data/hosts/{host}.json``           → HostKnowledge skeleton
  * ``data/conventions/{tier}/*.json``   → HostKnowledge (site-specific) OR
                                           leave for GenericPattern (generic)
  * ``data/skills/{tier}/*.json``        → same routing as conventions

Writes to:

  * ``data/host_knowledge_preview/{host}.json``  (one file per host)
  * ``data/migration_report.md``                  (human-readable diff /
                                                   review queue)

The v1 originals are NEVER touched. Operator confirms by reviewing the
preview, then runs ``--apply`` to swap in the new files (with a backup
copy of v1 retained for half a year).

Usage:

    # 1. Dry-run (default): write preview + report, leave v1 alone.
    python scripts/migrate_to_v2.py --dry-run

    # 2. After reviewing migration_report.md and the preview tree:
    python scripts/migrate_to_v2.py --apply

    # Override the data root:
    PAPRIKA_DATA_DIR=/path/to/data python scripts/migrate_to_v2.py --dry-run

Phase 2 of the v2 architecture; see ``internal/v2-architecture.html``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add project root to sys.path so ``import core.host_knowledge`` works
# when the script is launched directly.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.host_knowledge import (  # noqa: E402
    AuthInfo,
    BarrierKnowledge,
    ContentExtraction,
    HostKnowledge,
    LoginCheck,
    NavigationHints,
    Provenance,
    Stats,
    Strategy,
    StrategyStep,
)


_log = logging.getLogger("migrate_to_v2")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = Path(
    os.environ.get("PAPRIKA_DATA_DIR", "/data/jobs"),
)


# ---------------------------------------------------------------------------
# Migration Report -- collects per-host & global stats for human review.
# ---------------------------------------------------------------------------


class MigrationReport:
    """Accumulates per-host migration outcomes; emits markdown at the end."""

    def __init__(self) -> None:
        self.hosts_ok: list[str] = []
        self.hosts_empty: list[str] = []  # successfully migrated but trivially empty
        self.hosts_failed: list[tuple[str, str]] = []  # (host, error)
        self.conventions_site_specific: list[tuple[str, str]] = []  # (slug, host)
        self.conventions_generic: list[str] = []  # slug
        self.conventions_review: list[tuple[str, str]] = []  # (slug, reason)
        self.skills_site_specific: list[tuple[str, str]] = []
        self.skills_generic: list[str] = []
        self.skills_review: list[tuple[str, str]] = []

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Migration Report (v1 → v2)")
        lines.append("")
        lines.append(f"Generated at: {datetime.utcnow().isoformat()}Z")
        lines.append("")

        lines.append("## Summary")
        lines.append("")
        lines.append(f"- Hosts migrated successfully: **{len(self.hosts_ok)}**")
        lines.append(f"- Hosts migrated (empty / placeholder): **{len(self.hosts_empty)}**")
        lines.append(f"- Hosts FAILED: **{len(self.hosts_failed)}**")
        lines.append(f"- Conventions → site-specific: **{len(self.conventions_site_specific)}**")
        lines.append(f"- Conventions → generic: **{len(self.conventions_generic)}**")
        lines.append(f"- Conventions → review queue: **{len(self.conventions_review)}**")
        lines.append(f"- Skills → site-specific: **{len(self.skills_site_specific)}**")
        lines.append(f"- Skills → generic: **{len(self.skills_generic)}**")
        lines.append(f"- Skills → review queue: **{len(self.skills_review)}**")
        lines.append("")

        if self.hosts_failed:
            lines.append("## ❌ Hosts FAILED")
            lines.append("")
            for host, err in self.hosts_failed:
                lines.append(f"- `{host}` — {err}")
            lines.append("")

        if self.conventions_site_specific:
            lines.append("## 🏷 Conventions routed to site-specific HostKnowledge")
            lines.append("")
            for slug, host in self.conventions_site_specific:
                lines.append(f"- `{slug}` → `{host}`")
            lines.append("")

        if self.skills_site_specific:
            lines.append("## 🏷 Skills routed to site-specific HostKnowledge")
            lines.append("")
            for slug, host in self.skills_site_specific:
                lines.append(f"- `{slug}` → `{host}`")
            lines.append("")

        if self.conventions_generic or self.skills_generic:
            lines.append("## 🌐 Generic patterns (stay as GenericPattern, not migrated to HostKnowledge)")
            lines.append("")
            for slug in self.conventions_generic:
                lines.append(f"- convention: `{slug}`")
            for slug in self.skills_generic:
                lines.append(f"- skill: `{slug}`")
            lines.append("")

        if self.conventions_review or self.skills_review:
            lines.append("## ⚠️ Review queue (operator decision required)")
            lines.append("")
            for slug, reason in self.conventions_review:
                lines.append(f"- convention `{slug}` — {reason}")
            for slug, reason in self.skills_review:
                lines.append(f"- skill `{slug}` — {reason}")
            lines.append("")

        if self.hosts_empty:
            lines.append("## ℹ️ Hosts migrated as placeholders (no meaningful v1 data)")
            lines.append("")
            for host in self.hosts_empty:
                lines.append(f"- `{host}`")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pass 1: hosts.json → HostKnowledge skeleton
# ---------------------------------------------------------------------------


def _parse_iso(s: Any) -> datetime | None:
    """Best-effort ISO-8601 parser. Accepts trailing Z, returns None on garbage."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _recipe_to_content_extraction(recipe: dict) -> ContentExtraction | None:
    """Convert a v1 fetch_recipe into a v2 ContentExtraction.

    v1 fetch_recipe shape (observed in real data):
        {
          "pattern": "/*",
          "description": "...",
          "actions": [{"kind": "goto"/"click"/...., "args": [...], ...}, ...],
          "goal": "...",
          "code": "<python>",
          "engine": "auto",
          ...
        }

    The migration faithfully preserves the click sequence from ``actions``
    as a v2 sequence-strategy. The full Python ``code`` is dropped from
    HostKnowledge (it's site+job-specific; v2 generates code on demand
    via R1/codegen). Operators can find the original in ``_v1_backup/``
    if they need it.
    """
    pattern = recipe.get("pattern") or ""
    if not pattern:
        return None

    actions = recipe.get("actions") or []
    strategy_steps: list[StrategyStep] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        kind = a.get("kind") or ""
        if kind == "click":
            sel = None
            args = a.get("args") or []
            if args and isinstance(args[0], str):
                sel = args[0]
            strategy_steps.append(StrategyStep(action="click", selector=sel))
        elif kind == "goto":
            args = a.get("args") or []
            url = args[0] if args and isinstance(args[0], str) else None
            strategy_steps.append(StrategyStep(action="navigate", url=url))
        elif kind == "wait":
            args = a.get("args") or []
            ms = None
            if args and isinstance(args[0], (int, float)):
                ms = int(float(args[0]) * 1000)  # v1 used seconds
            strategy_steps.append(StrategyStep(action="wait", ms=ms))
        elif kind == "scroll":
            strategy_steps.append(StrategyStep(action="scroll"))
        elif kind:
            # Preserve unknown action kinds verbatim so nothing is silently lost.
            strategy_steps.append(StrategyStep(action=kind))

    if not strategy_steps:
        return None

    strategy = Strategy(kind="sequence", steps=strategy_steps)

    notes_parts: list[str] = []
    desc = recipe.get("description")
    if desc:
        notes_parts.append(f"description: {desc}")
    goal = recipe.get("goal")
    if goal:
        notes_parts.append(f"goal: {goal}")
    src_job = recipe.get("created_from_job")
    if src_job:
        notes_parts.append(f"created_from_job: {src_job}")

    return ContentExtraction(
        url_pattern=pattern,
        page_kind="unknown",  # v1 didn't classify -- leave for distiller to learn
        strategy=strategy,
        notes=" / ".join(notes_parts) if notes_parts else None,
    )


def migrate_host(record: dict) -> HostKnowledge:
    """Convert one v1 HostRecord dict to a v2 HostKnowledge.

    Cookies are NOT copied -- they remain in the v1 HostRecord (operator-
    edited, separate update sycle). HostKnowledge.auth.profile_slug
    references the cookies indirectly when needed.
    """
    host = record.get("host") or "unknown.invalid"
    knowledge = HostKnowledge(host=host)

    # ---- timestamps ------------------------------------------------------
    created = _parse_iso(record.get("created_at"))
    updated = _parse_iso(record.get("updated_at"))
    if created:
        knowledge.created_at = created
    if updated:
        knowledge.updated_at = updated

    # ---- navigation_hints ------------------------------------------------
    notes = (record.get("notes") or "").strip()
    if notes:
        # Filter out auto-generated noise: "auto-saved by fetch job X" carries
        # no semantic value.
        if not notes.startswith("auto-saved by fetch job"):
            knowledge.per_page.navigation_hints.common_observations.append(notes)

    popup_policy = record.get("popup_policy") or record.get("popup_behavior")
    if popup_policy in ("kill", "follow", "ignore"):
        knowledge.per_page.navigation_hints.popup_policy = popup_policy  # type: ignore[assignment]

    # ---- site_structure --------------------------------------------------
    recrawl = record.get("recrawl_patterns") or []
    if isinstance(recrawl, list):
        knowledge.site_structure.frontier_patterns = [
            p for p in recrawl if isinstance(p, str) and p
        ]

    # ---- auth ------------------------------------------------------------
    requires_login = bool(record.get("login_url") or record.get("login_check"))
    knowledge.auth = AuthInfo(
        requires_login=requires_login,
        profile_slug=None,  # v1 didn't track this directly
        login_check=(
            LoginCheck(
                url=record.get("login_url"),
                expect_text=(record.get("login_check") or {}).get("expect_text")
                if isinstance(record.get("login_check"), dict)
                else None,
            )
            if requires_login
            else None
        ),
    )

    # ---- per_page.content_extraction (from fetch_recipes) ---------------
    fetch_recipes = record.get("fetch_recipes") or []
    if isinstance(fetch_recipes, list):
        for r in fetch_recipes:
            if not isinstance(r, dict):
                continue
            ce = _recipe_to_content_extraction(r)
            if ce is not None:
                knowledge.per_page.content_extraction.append(ce)

    # ---- stats (initial) -------------------------------------------------
    last_used = _parse_iso(record.get("last_used_at"))
    if last_used:
        knowledge.stats.last_success_at = last_used
        # Pass 3 (stats computation) will refine total_jobs/success_rate
        # from job history; this is a conservative seed.
        knowledge.stats.total_jobs = 1
        knowledge.stats.successful_jobs = 1
        knowledge.stats.success_rate = 1.0

    # ---- provenance ------------------------------------------------------
    knowledge.provenance = Provenance(
        last_updated_by="migrate_to_v2 Pass 1",
        last_updated_at=datetime.utcnow(),
    )

    return knowledge


def _is_meaningfully_populated(k: HostKnowledge) -> bool:
    """True if the migration produced more than an empty skeleton.

    A "meaningful" migration has any of: navigation hints, frontier
    patterns, recipes, auth, or non-default popup policy. Hosts whose
    v1 record had only auto-save noise count as empty.
    """
    pp = k.per_page
    if pp.content_extraction:
        return True
    if pp.navigation_hints.common_observations:
        return True
    if pp.navigation_hints.popup_policy and pp.navigation_hints.popup_policy != "kill":
        return True
    if k.site_structure.frontier_patterns:
        return True
    if k.auth.requires_login:
        return True
    return False


# ---------------------------------------------------------------------------
# Pass 2: conventions / skills routing
# ---------------------------------------------------------------------------


# Placeholder hosts that should never be treated as "the target site"
# of a convention/skill. RFC 2606 reserves example.com / example.org /
# example.net for documentation use; code templates and tutorials lace
# them everywhere, so naive substring matching produces false positives.
_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "localhost",
}


def _detect_site_for_pattern(pattern: dict, known_hosts: set[str]) -> str | None:
    """Heuristic: scan the pattern's text fields for a known host name.

    Scoring (per candidate host):
      * slug:             +10
      * name:             +5
      * tags:             +5
      * applicable_when:  +3
      * description:      +2
      * advice / rationale / llm_instructions: +2
      * bad/good_example: +1
      * code_template:    +1

    Multiple appearances accumulate. Placeholder hosts (example.com etc)
    are excluded entirely -- they're tutorial filler, not real targets.

    Returns the highest-scoring host whose score >= 3, or None.
    """
    eligible = {h for h in known_hosts if h and h.lower() not in _PLACEHOLDER_HOSTS}
    if not eligible:
        return None

    field_weights: list[tuple[str, int, bool]] = [
        # (field_name, weight, is_list)
        ("slug", 10, False),
        ("name", 5, False),
        ("tags", 5, True),
        ("applicable_when", 3, True),
        ("description", 2, False),
        ("advice", 2, False),
        ("rationale", 2, False),
        ("llm_instructions", 2, False),
        ("bad_example", 1, False),
        ("good_example", 1, False),
        ("code_template", 1, False),
    ]

    scores: dict[str, int] = {h: 0 for h in eligible}

    for field, weight, is_list in field_weights:
        v = pattern.get(field)
        if is_list:
            if not isinstance(v, list):
                continue
            text = "\n".join(s for s in v if isinstance(s, str)).lower()
        else:
            if not isinstance(v, str):
                continue
            text = v.lower()
        for host in eligible:
            h = host.lower()
            if h in text:
                scores[host] += weight

    best_host = max(scores, key=lambda h: scores[h])
    if scores[best_host] < 3:
        return None
    return best_host


def classify_pattern(
    pattern: dict,
    known_hosts: set[str],
) -> tuple[str, str | None]:
    """Decide if a convention/skill is site-specific or generic.

    Returns ``(verdict, host_or_reason)`` where verdict is one of:
      * "site_specific" -- host is the second element
      * "generic"       -- second element is None
      * "review"        -- second element is the reason for review
    """
    host = _detect_site_for_pattern(pattern, known_hosts)
    if host:
        return ("site_specific", host)

    # No host mentioned -- but is it a CLEAR generic pattern, or
    # ambiguous? Heuristic: a clear generic has tags or applicable_when
    # that talk about technique, not site.
    aw = pattern.get("applicable_when")
    if isinstance(aw, list) and aw:
        return ("generic", None)

    # Sparse metadata, no host -- send to review queue.
    return ("review", "no applicable_when or recognizable host")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_migration(
    *,
    data_dir: Path,
    dry_run: bool,
    report: MigrationReport,
) -> None:
    hosts_dir = data_dir / "hosts"
    preview_dir = data_dir / "host_knowledge_preview"
    out_dir = data_dir / "host_knowledge"
    target_dir = preview_dir if dry_run else out_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    # --- Pass 1: hosts ----------------------------------------------------
    known_hosts: set[str] = set()
    if hosts_dir.is_dir():
        for hf in sorted(hosts_dir.glob("*.json")):
            try:
                record = json.loads(hf.read_text(encoding="utf-8"))
            except Exception as e:
                report.hosts_failed.append((hf.stem, f"unreadable: {e}"))
                continue
            try:
                knowledge = migrate_host(record)
            except Exception as e:
                report.hosts_failed.append((hf.stem, f"migrate_host crashed: {e}"))
                continue
            known_hosts.add(knowledge.host)

            is_populated = _is_meaningfully_populated(knowledge)

            # Skip writing a preview file for trivially-empty hosts -- they
            # exist only because paprika saved their cookies. Phase 2's goal
            # is to migrate *knowledge*; an empty skeleton can be auto-
            # created on first job under v2 just as well. Track them in
            # the report so the operator can spot anything mis-classified.
            if not is_populated:
                report.hosts_empty.append(knowledge.host)
                continue

            target = target_dir / f"{knowledge.host}.json"
            try:
                target.write_text(
                    knowledge.model_dump_json(indent=2, exclude_none=False),
                    encoding="utf-8",
                )
            except Exception as e:
                report.hosts_failed.append((knowledge.host, f"write failed: {e}"))
                continue

            report.hosts_ok.append(knowledge.host)

    # --- Pass 2: conventions ---------------------------------------------
    for tier in ("curated", "auto"):
        cdir = data_dir / "conventions" / tier
        if not cdir.is_dir():
            continue
        for f in sorted(cdir.glob("*.json")):
            try:
                pattern = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                report.conventions_review.append((f.stem, f"unreadable: {e}"))
                continue
            verdict, info = classify_pattern(pattern, known_hosts)
            slug = pattern.get("slug") or f.stem
            if verdict == "site_specific" and info:
                report.conventions_site_specific.append((slug, info))
                # NOTE: actual merge into the HostKnowledge file is left
                # for Pass 2b -- this dry-run just classifies. Merging
                # would require deciding which section (navigation_hints?
                # barriers?) the rule belongs to, which needs R1 in v2
                # Phase 4. For now we just track routing.
            elif verdict == "generic":
                report.conventions_generic.append(slug)
            else:
                report.conventions_review.append(
                    (slug, info or "could not classify"),
                )

    # --- Pass 2 (skills) -------------------------------------------------
    for tier in ("curated", "auto"):
        sdir = data_dir / "skills" / tier
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.glob("*.json")):
            try:
                pattern = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                report.skills_review.append((f.stem, f"unreadable: {e}"))
                continue
            verdict, info = classify_pattern(pattern, known_hosts)
            slug = pattern.get("slug") or f.stem
            if verdict == "site_specific" and info:
                report.skills_site_specific.append((slug, info))
            elif verdict == "generic":
                report.skills_generic.append(slug)
            else:
                report.skills_review.append((slug, info or "could not classify"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default: write preview tree and report, leave v1 alone.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to data/host_knowledge/ (active) instead of preview.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Override paprika data root (default: {DEFAULT_DATA_DIR}).",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to write migration_report.md (default: <data-dir>/migration_report.md).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    dry_run = not args.apply

    if not args.data_dir.is_dir():
        print(f"ERROR: data dir not found: {args.data_dir}", file=sys.stderr)
        return 1

    report = MigrationReport()
    run_migration(data_dir=args.data_dir, dry_run=dry_run, report=report)

    report_path = args.report or (args.data_dir / "migration_report.md")
    try:
        report_path.write_text(report.to_markdown(), encoding="utf-8")
    except Exception as e:
        print(f"ERROR: could not write report to {report_path}: {e}", file=sys.stderr)
        return 1

    mode = "DRY-RUN" if dry_run else "APPLIED"
    target_dir = (
        args.data_dir / "host_knowledge_preview"
        if dry_run
        else args.data_dir / "host_knowledge"
    )
    print(f"[{mode}] migration done.")
    print(f"  hosts OK: {len(report.hosts_ok)}")
    print(f"  hosts empty (placeholder): {len(report.hosts_empty)}")
    print(f"  hosts FAILED: {len(report.hosts_failed)}")
    print(f"  conventions site-specific: {len(report.conventions_site_specific)}")
    print(f"  conventions generic: {len(report.conventions_generic)}")
    print(f"  conventions review: {len(report.conventions_review)}")
    print(f"  skills site-specific: {len(report.skills_site_specific)}")
    print(f"  skills generic: {len(report.skills_generic)}")
    print(f"  skills review: {len(report.skills_review)}")
    print(f"  output dir: {target_dir}")
    print(f"  report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
