"""The Verifier: one pure function that turns a job's FACTS into a verdict.

Design §5. This is deliberately the only definition of "did it work" in
the learning system. It reads measurements (ffprobe results, asset counts,
the occlusion probe, the error string) and never an LLM's or an agent's
own claim of success.

    verify(event) -> Verdict

``event`` is the dict the hub appends to the outcome stream
(server/hub/_outcome_stream.build_event). No I/O, no clock, no globals:
the same event always yields the same verdict for a given VERSION, so the
ledger can record which version judged each row and old rows are never
silently re-interpreted.

Outcomes
    success     the job got what it was for
    partial     something usable but not the goal (short clip / fragment)
    fail        the content was there to get and we did not get it
    blocked     a wall we do not try to climb (login / paywall / captcha /
                challenge) -- input to dormancy, not to retries.
                NB a full-screen overlay WITHOUT a credential form (age
                gate, consent, modal) is not this: it is a ``fail`` with
                reason ``overlay_wall`` -- exactly the kind a learned
                click sequence can get past.
    dead        the URL itself is gone (DNS / 404 / TLS) -- nothing to learn
    infra       our own machinery failed (worker lost, timeout of ours, no
                room in RAM to put the video) -- NEVER a learning signal
    na          the goal does not apply (video requested, page has none)
    unknown     the facts cannot decide; counted and monitored, and the only
                bucket an LLM judge may later settle
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

#: Bump on ANY change to the rules below. Stored with every ledger row.
#:   2  a video download the worker never attempted for want of room on the
#:      ramdisk is ``infra``/``no_ram``, not a failure of the fetch.
VERSION = 2

SUCCESS = "success"
PARTIAL = "partial"
FAIL = "fail"
BLOCKED = "blocked"
DEAD = "dead"
INFRA = "infra"
NA = "na"
UNKNOWN = "unknown"

OUTCOMES = (SUCCESS, PARTIAL, FAIL, BLOCKED, DEAD, INFRA, NA, UNKNOWN)

#: Outcomes that say something about whether the SITE can be fetched. VSR is
#: success / (these). infra / na / dead / unknown are excluded from the
#: denominator: none of them is evidence about our fetch strategy.
SCORED = (SUCCESS, PARTIAL, FAIL, BLOCKED)

INTENT_VIDEO = "video"
INTENT_PAGE = "page"


@dataclass(frozen=True)
class VerifyConfig:
    #: A playable video shorter than this is a preview/trailer/ad, not the
    #: goal. (Design §14 open question 2; per-template tuning comes later.)
    min_video_s: float = 60.0
    #: Below this a "video" is a stream segment or a UI clip.
    fragment_max_s: float = 15.0
    fragment_max_bytes: int = 2 * 1024 * 1024
    #: A probed-playable file with NO duration metadata still counts when it
    #: is at least this big (some containers omit duration).
    nodur_min_bytes: int = 5 * 1024 * 1024
    #: Page intent: non-decorative images+videos needed to call it content.
    min_content_assets: int = 1


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    intent: str
    version: int = VERSION
    verified_by: str = "rule"
    evidence: dict = field(default_factory=dict)

    @property
    def scored(self) -> bool:
        return self.outcome in SCORED

    def as_dict(self) -> dict:
        return asdict(self)


# ---- error-string classification -------------------------------------------
# Order matters: infra first. Every one of the 2,000 most recent failed fetch
# jobs sampled on 2026-11-12 was one of the first four shapes below -- i.e.
# our machinery, not the site.
_INFRA_RE = re.compile(
    r"worker \S+ (disconnected|restarted|no longer in the live fleet)"
    r"|is gone fleet-wide"
    r"|queued for >\d+s without assignment"
    r"|no free lane"
    r"|orphaned by a hub/worker restart"
    r"|lane (lost|pool)|chrome (crashed|exited|did not start)"
    r"|browser (crashed|closed unexpectedly)"
    r"|target (closed|crashed)|websocket (closed|connection)"
    r"|EngineThermalThrottled|ConnectError: All connection attempts failed"
    r"|out of memory|ENOSPC|No space left on device"
    r"|hub restart|self-update|cancelled by (hub|reaper)",
    re.I,
)
_DEAD_RE = re.compile(
    r"ERR_NAME_NOT_RESOLVED|host does not resolve|NXDOMAIN"
    r"|ERR_CONNECTION_REFUSED|ERR_ADDRESS_UNREACHABLE"
    r"|ERR_CERT_|ERR_SSL_|SSL: |certificate verify failed"
    r"|\bHTTP (404|410)\b|\b(404|410) (Not Found|Gone)\b"
    r"|ERR_TOO_MANY_REDIRECTS|ERR_INVALID_URL|blocked by url policy",
    re.I,
)
_BLOCKED_RE = re.compile(
    r"\bHTTP (401|403|407|451)\b|\b(401|403) (Unauthorized|Forbidden)\b"
    r"|cloudflare|cf-chall|just a moment|attention required"
    r"|captcha|recaptcha|hcaptcha|turnstile"
    r"|login required|sign in to|members only|paywall|subscribe to (view|watch)"
    r"|not available in your (country|region)|geo.?block",
    re.I,
)
_BLOCK_KIND_RE = (
    ("captcha", re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)),
    ("challenge", re.compile(r"cloudflare|cf-chall|just a moment|attention required", re.I)),
    ("region", re.compile(r"country|region|geo.?block|\b451\b", re.I)),
    ("paywall", re.compile(r"paywall|subscribe|premium|members only", re.I)),
    ("login", re.compile(r"login|log in|sign in|ログイン|password|401|403", re.I)),
)

_SEGMENT_NAME_RE = re.compile(
    r"(^|[_\-.])(seg|segment|chunk|frag|fragment|part)[_\-]?\d+"
    r"|\.(ts|m4s)$"
    r"|_\d{9,}\.(mp4|webm)$",   # ..._<epoch>.mp4 style stream slices
    re.I,
)


def classify_error(error: str) -> str | None:
    """infra / dead / blocked / None for a free-text error. Pure."""
    e = error or ""
    if not e:
        return None
    if _INFRA_RE.search(e):
        return INFRA
    if _DEAD_RE.search(e):
        return DEAD
    if _BLOCKED_RE.search(e):
        return BLOCKED
    return None


def _block_kind(text: str) -> str:
    for kind, rx in _BLOCK_KIND_RE:
        if rx.search(text or ""):
            return kind
    return "overlay"


def _is_fragment(m: dict, cfg: VerifyConfig) -> bool:
    dur = float(m.get("duration_s") or 0.0)
    size = int(m.get("bytes") or 0)
    name = str(m.get("name") or "")
    container = str(m.get("container") or "")
    if 0.0 < dur < cfg.fragment_max_s and size < cfg.fragment_max_bytes:
        return True
    if "mpegts" in container and dur < 4 * cfg.fragment_max_s:
        return True
    if _SEGMENT_NAME_RE.search(name) and dur < cfg.min_video_s:
        return True
    return False


def _video_verdict(ev: dict, cfg: VerifyConfig, blocked: str | None,
                   overlay: bool = False) -> Verdict:
    assets = ev.get("assets") or {}
    facts = ev.get("facts") or {}
    media = [m for m in (facts.get("media") or []) if isinstance(m, dict)]
    n_video = int(assets.get("n_video") or 0)
    seen = ev.get("video_seen") or {}
    ytd = ev.get("ytdlp") or {}
    deferred = facts.get("deferred") or {}

    probed = [m for m in media if m.get("probed")]
    playable = [m for m in probed if m.get("playable")]
    whole = [m for m in playable if not _is_fragment(m, cfg)]

    def _ev(**extra) -> dict:
        base = {
            "n_video": n_video, "probed": len(probed),
            "playable": len(playable), "whole": len(whole),
        }
        base.update(extra)
        return base

    # 1. A whole, long-enough, playable video is the goal.
    best = max((float(m.get("duration_s") or 0.0) for m in whole), default=0.0)
    for m in whole:
        dur = float(m.get("duration_s") or 0.0)
        if dur >= cfg.min_video_s:
            return Verdict(SUCCESS, "video_ok", INTENT_VIDEO, evidence=_ev(best_s=best))
    for m in whole:
        dur = float(m.get("duration_s") or 0.0)
        if dur <= 0.0 and int(m.get("bytes") or 0) >= cfg.nodur_min_bytes:
            return Verdict(SUCCESS, "video_ok_nodur", INTENT_VIDEO, evidence=_ev())

    # 1b. The worker found a video to download and had nowhere in RAM to put
    #     it (video is never written to disk), so the download was never
    #     attempted. Whatever else the job holds -- nothing, or a short clip
    #     caught on the way -- says nothing about whether the fetch works:
    #     this is our capacity, not the site. Counting it as a failure halved
    #     every measured rate and retired playbooks that still worked
    #     (2026-11-15 .. 11-19).
    if deferred.get("no_ram"):
        return Verdict(INFRA, "no_ram", INTENT_VIDEO,
                       evidence=_ev(targets=int(deferred.get("targets") or 0)))

    # 2. Something playable, but not the goal.
    if whole:
        return Verdict(PARTIAL, "video_short", INTENT_VIDEO, evidence=_ev(best_s=best))
    if playable:
        return Verdict(PARTIAL, "video_fragment", INTENT_VIDEO, evidence=_ev())

    # 3. Files exist and were measured, none plays.
    if probed and n_video > 0:
        if blocked:
            return Verdict(BLOCKED, f"blocked_{blocked}", INTENT_VIDEO, evidence=_ev())
        return Verdict(FAIL, "video_unplayable", INTENT_VIDEO, evidence=_ev())

    # 4. Files exist but nobody measured them (old worker / probe skipped):
    #    the facts cannot decide. Do NOT guess from size.
    if n_video > 0:
        return Verdict(UNKNOWN, "video_unprobed", INTENT_VIDEO, evidence=_ev())

    # 5. No video file at all.
    if blocked:
        return Verdict(BLOCKED, f"blocked_{blocked}", INTENT_VIDEO, evidence=_ev())
    if overlay:
        return Verdict(FAIL, "overlay_wall", INTENT_VIDEO, evidence=_ev())
    evidence_of_video = (
        int(seen.get("urls") or 0) > 0
        or int(seen.get("videos") or 0) > 0
        or int(ytd.get("n") or 0) > 0
        or int(deferred.get("targets") or 0) > 0
    )
    if evidence_of_video:
        return Verdict(
            FAIL, "video_not_captured", INTENT_VIDEO,
            evidence=_ev(ytdlp_fail=int(ytd.get("fail") or 0),
                         seen_urls=int(seen.get("urls") or 0)),
        )
    # A video was asked for and the page shows no trace of one. That is the
    # request not applying, not our fetch failing (same conservative rule
    # the old escalator used).
    return Verdict(NA, "no_video_evidence", INTENT_VIDEO, evidence=_ev())


def _page_verdict(ev: dict, cfg: VerifyConfig, blocked: str | None,
                  overlay: bool = False) -> Verdict:
    assets = ev.get("assets") or {}
    n_content = int(assets.get("n_image") or 0) + int(assets.get("n_video") or 0)
    evd = {"n_content": n_content, "n_decor": int(assets.get("n_decor") or 0)}
    if blocked:
        return Verdict(BLOCKED, f"blocked_{blocked}", INTENT_PAGE, evidence=evd)
    if overlay:
        # The hub's structural probe saw the content hidden behind a
        # full-screen layer; a few saved logo/icon assets do not change that.
        return Verdict(FAIL, "overlay_wall", INTENT_PAGE, evidence=evd)
    if not ev.get("capture_assets", True):
        # HTML-only capture: completing is all there is to verify.
        return Verdict(SUCCESS, "html_only", INTENT_PAGE, evidence=evd)
    if n_content >= cfg.min_content_assets:
        return Verdict(SUCCESS, "content_ok", INTENT_PAGE, evidence=evd)
    return Verdict(FAIL, "no_content", INTENT_PAGE, evidence=evd)


def intent_of(ev: dict) -> str:
    return INTENT_VIDEO if ev.get("download_video") else INTENT_PAGE


def verify(ev: dict, cfg: VerifyConfig | None = None) -> Verdict:
    """Facts -> verdict. Pure; never raises on a malformed event."""
    cfg = cfg or VerifyConfig()
    if not isinstance(ev, dict):
        return Verdict(UNKNOWN, "malformed_event", INTENT_PAGE)
    intent = intent_of(ev)
    status = str(ev.get("status") or "").lower()
    error = str(ev.get("error") or "")

    if status in ("failed", "cancelled"):
        kind = classify_error(error)
        if kind == INFRA:
            return Verdict(INFRA, "infra_worker", intent)
        if kind == DEAD:
            return Verdict(DEAD, "dead_url", intent)
        if kind == BLOCKED:
            return Verdict(BLOCKED, f"blocked_{_block_kind(error)}", intent)
        if status == "cancelled":
            return Verdict(INFRA, "cancelled", intent)
        return Verdict(UNKNOWN, "failed_unclassified", intent,
                       evidence={"error": error[:120]})

    if status == "downloading":
        # The fetch returned but a download is still running; a later event
        # for the same job settles it.
        return Verdict(UNKNOWN, "download_in_progress", intent)

    if status not in ("completed", "review"):
        return Verdict(UNKNOWN, f"status_{status or 'missing'}", intent)

    # A wall: the hub's structural occlusion classifier already bucketed the
    # job (status=review), or the worker's own log/error says so. A credential
    # form makes it a wall we do not climb; any other full-screen overlay is a
    # solvable failure.
    blocked: str | None = None
    overlay = False
    if status == "review":
        occ = ev.get("occlusion") or {}
        reason = str(ev.get("review_reason") or "")
        if occ.get("hasPassword") or "ログインフォーム" in reason:
            blocked = "login"
        else:
            overlay = True
    elif classify_error(error) == BLOCKED:
        blocked = _block_kind(error)

    if intent == INTENT_VIDEO:
        return _video_verdict(ev, cfg, blocked, overlay)
    return _page_verdict(ev, cfg, blocked, overlay)
