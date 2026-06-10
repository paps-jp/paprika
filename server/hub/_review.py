"""Classify a COMPLETED fetch as 課題 ("review") -- content blocked by a
full-screen login / age / consent / paywall overlay.

WHY
===
A ``mode=fetch`` job is marked ``completed`` whenever the worker returns a
``FetchResult`` (it raised no exception). But "the page rendered" is not "the
content was captured": an auth / age / cookie / paywall modal can cover the
whole viewport, so the job *looks* successful while really it only grabbed a
wall. Those used to hide among the clean successes.

This module buckets them apart into a distinct terminal status
(``JobStatus.review``, shown as "課題" in the admin UI's #jobs sub-tab) WITHOUT
discarding the result -- the operator can still inspect the captured wall,
and the existing AI escalation (``_escalate.py``) still runs on the same
completion.

HOW (no hardcoding)
===================
The worker's ``core/fetcher.probe_occlusion`` measures the page *structurally*
from the live DOM -- it asks the browser's own hit-testing what is painted on
top across a grid of viewport points, plus background scroll-lock, ARIA / top-
layer modal markers, a login password field, and visible-image / text
scarcity. No site-specific URLs or keywords. This module only applies a
conservative threshold to that report.

GATES
  * Settings ``review_flag_enabled`` (default ON) + env kill-switch
    ``PAPRIKA_REVIEW_DISABLE=1``.
  * Only ``mode=fetch`` (codegen-loop / rerun manage their own success).
  * Conservative by design (the operator chose "ON, conservative threshold"):
    requires a full-viewport overlay AND content scarcity AND an explicit
    modal/login/scroll-lock marker, so a normal page with a header login
    link, a hero image, or a single positioned app wrapper does NOT trip it.

Everything is best-effort: a crash here must never affect job completion.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


_DISABLE = _flag("PAPRIKA_REVIEW_DISABLE", False)

# Thresholds (env-tunable). Defaults are deliberately conservative.
#   COVERAGE  -- one painted layer must cover >= this fraction of the viewport.
#   DOMINANCE -- ... AND own >= this fraction of the grid hit-test points.
#   IMG/TEXT  -- "content scarce": few visible images AND little visible text.
_MIN_COVERAGE = _num("PAPRIKA_REVIEW_MIN_COVERAGE", 0.85)
_MIN_DOMINANCE = _num("PAPRIKA_REVIEW_MIN_DOMINANCE", 0.6)
_MAX_VIS_IMAGES = int(_num("PAPRIKA_REVIEW_MAX_VISIBLE_IMAGES", 3))
_MAX_TEXT_LEN = int(_num("PAPRIKA_REVIEW_MAX_TEXT_LEN", 1200))


def _feature_on() -> bool:
    if _DISABLE:
        return False
    try:
        from server.hub._state import state

        if state.settings is not None:
            return bool(state.settings.get("review_flag_enabled", True))
    except Exception:
        pass
    return True


def classify_review(info, result) -> str | None:
    """Return a human-readable 課題 reason for a completed fetch, or None.

    ``info`` is the JobInfo, ``result`` the JobResult the worker sent. Pure +
    synchronous (only dict / attribute reads), safe to call inline from the
    WS handler. Returns None unless there is strong, multi-signal evidence
    that a full-screen overlay blocked the content.
    """
    try:
        opts = getattr(info, "options", None)
        if ((opts.mode if opts else None) or "fetch") != "fetch":
            return None
        if not _feature_on():
            return None

        # Operator marked this host 対象外 (HostRecord.excluded = "do nothing"):
        # never bucket its fetches as 課題(review).
        try:
            from server.hub._escalate import _host_excluded, _host_of
            if _host_excluded(_host_of(getattr(info, "url", ""))):
                return None
        except Exception:
            pass

        occ = getattr(result, "occlusion", None) or {}
        if not occ or occ.get("error"):
            return None

        coverage = float(occ.get("coverage") or 0.0)
        dominance = float(occ.get("dominance") or 0.0)
        scroll_lock = bool(occ.get("scrollLock"))
        aria_modal = bool(occ.get("ariaModal"))
        has_pw = bool(occ.get("hasPassword"))
        vis_img = int(occ.get("visibleImages") or 0)
        text_len = int(occ.get("textLen") or 0)

        # A single painted layer covers most of the viewport AND owns most of
        # the hit-test grid -> a full-screen overlay, not just a banner.
        full_overlay = coverage >= _MIN_COVERAGE and dominance >= _MIN_DOMINANCE
        if not full_overlay:
            return None

        # The wall is actually BLOCKING content: few VISIBLE images AND little
        # VISIBLE text behind the overlay. NB: we deliberately do NOT gate on
        # the saved-asset count -- a real login / age wall still ships a logo +
        # a few icons, so an `assets == 0` gate never fired in practice
        # (validated live 2026-06-09: github.com/login -> 4 assets but visImg=0
        # textLen=282; pinterest -> 17 assets but visImg=2 textLen=814; both are
        # genuine login walls). The occlusion probe's live visImg / textLen is
        # the right "is content actually showing" signal -- Instagram's
        # content-behind-modal (visImg=19, 71 assets) is correctly NOT flagged.
        content_scarce = vis_img <= _MAX_VIS_IMAGES and text_len < _MAX_TEXT_LEN
        if not content_scarce:
            return None

        # Require at least one explicit "this is a modal/login/locked" marker
        # so a full-bleed splash / single-image page doesn't trip it.
        if not (aria_modal or scroll_lock or has_pw):
            return None

        bits: list[str] = []
        if has_pw:
            bits.append("ログインフォーム")
        if aria_modal:
            bits.append("モーダルダイアログ")
        if scroll_lock:
            bits.append("背景スクロールロック")
        bits.append(f"オーバーレイがビューポートの{int(coverage * 100)}%を占有")
        return "全面オーバーレイでコンテンツが取得できていません (" + ", ".join(bits) + ")"
    except Exception:
        log.debug(
            "review classify crashed for %s",
            getattr(info, "job_id", "?"),
            exc_info=True,
        )
        return None
