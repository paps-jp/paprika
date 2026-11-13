"""Solver: look, take one action, look again (design §9.1).

Replaces "write a whole Python script, run it for up to an hour, grade it
afterwards". Each step the model sees what is on the page right now and
proposes ONE action from a small closed set; the Solver carries it out
through the hub's session API and records what changed.

Scope of this first version (design §15.4 G3): pages where a video is
behind something that a click gets past -- an age confirmation, a consent
or advertising overlay, a play button. So the action set is deliberately
tiny: click a listed element, scroll, wait, done, give up. The Solver
never types, never logs in, and refuses to click editable fields or links
that leave the site.

The limits are enforced HERE, in code, not by asking the model nicely. A
live run on 2026-11-12 showed why: told never to log in, the 4B model
clicked "ログイン" and then "Continue with Google" and landed on a
third-party sign-in page. So, regardless of what the model proposes:

  * a click on anything labelled as logging in, signing up, paying or
    installing is refused;
  * a click on an input field or on a link to another site is refused;
  * if a click takes the browser to another site, or makes a password
    field appear, the run ends there and its trace is discarded.

What the Solver returns is a TRACE, not a verdict. Whether the trace is
worth anything is decided afterwards by replaying it without a model and
judging those jobs with the Verifier (learner/autopilot.py). The model
saying "done" only ends the session.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from learner.compiler import MAX_PLAYER_CLICKS, PLAYER_MARK, describe_js, player_tag_js
from learner.hubapi import HubError, HubUnavailable
from learner.llm import LLMBadOutput, LLMUnavailable, start_meter

PROMPT_VERSION = 4

#: result values
TRACE = "trace"                # at least one click was carried out
NO_ACTION = "no_action"        # the model found nothing to click
GAVE_UP = "gave_up"            # the model named a barrier it must not climb
EXHAUSTED = "exhausted"        # step / time / token limit, no click made
INFRA = "infra"                # hub or engine unavailable: not a signal
BAD_OUTPUT = "llm_bad_output"  # the model would not produce the JSON asked for

#: barriers a give_up may name; these make the key dormant for a long time
HARD_BARRIERS = ("login", "paywall", "captcha", "region")
#: barriers that are about the ONE URL that was opened, not about the key:
#: another URL of the same template may be fine, so the autopilot moves on
#: to the next URL instead of giving the key up (learner/autopilot.py).
#: "redirected" is never named by the model: the Solver sets it when the
#: page it was sent to had already left for another site before any click
#: (an advertising redirect), so whatever the model saw was not the key.
URL_BARRIERS = ("gone", "redirected")
BARRIERS = HARD_BARRIERS + ("gone", "no_video", "other")

SYSTEM_PROMPT = """You operate a web browser. Your goal: get the main video of the page to load and play.

Each turn you see the page state and a numbered list of visible interactive elements, written as [@N] tag "text".
Reply with ONE JSON object and nothing else:

  {"action": "click", "ref": N, "why": "..."}     click element @N
  {"action": "scroll", "why": "..."}              scroll down one screen
  {"action": "click_player", "why": "..."}        click the middle of the video player
  {"action": "wait", "seconds": N, "why": "..."}  wait N seconds (2 to 60)
  {"action": "done", "why": "..."}                the video is loading / playing and nothing covers it
  {"action": "give_up", "barrier": "login|paywall|captcha|region|gone|no_video|other", "why": "..."}

What you may click:
  - an age confirmation ("I am 18", "Enter", "Yes", "はい", "入場")
  - accepting or closing a cookie / consent notice
  - closing a popup or an overlay that covers the page (x, Close, Skip, 閉じる)
  - the play button of the video

Never click: log in, sign up, register, buy, subscribe, download-app, advertisements, or links to other sites.
You cannot type. If the page needs a login, a payment, a captcha or is blocked in this region, give_up with that barrier.
Two barriers must be told apart:
  - "gone": THIS page's video was removed, deleted, made private or inactive, or the page says it was not found. Other pages of the site may still have their video.
  - "no_video": this KIND of page never has a video (a shop item, an article, a form, a list, a profile, a search result).
If the page HAS a video player but you cannot get it to start, that is "other", not "no_video".
If a click did not change anything, do not repeat it; try something else or give_up.
Use click_player when the video is not playing and no listed element starts it. The first click on a player
often only opens an advertisement: clicking the player again is allowed.
If a short advertisement is playing in the player before the main video, wait for it to end with ONE long wait
(for a 30-second advertisement: {"action": "wait", "seconds": 40}), then read "longest" in VIDEO ELEMENTS again.
Prefer the smallest number of clicks."""

_PROBE_JS = r"""
(() => {
  const vids = [...document.querySelectorAll('video')].slice(0, 4).map(v => ({
    src: (v.currentSrc || v.src || '').slice(0, 100),
    dur: isFinite(v.duration) ? Math.round(v.duration) : 0,
    paused: !!v.paused, w: v.clientWidth, h: v.clientHeight,
  }));
  const W = innerWidth || 1, H = innerHeight || 1;
  let cover = 0, coverText = '';
  let seen = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (++seen > 6000) break;
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
    if ((parseInt(cs.zIndex) || 0) < 1) continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
    const c = (w * h) / (W * H);
    if (c > cover) { cover = c; coverText = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200); }
  }
  return JSON.stringify({
    url: location.href, title: (document.title || '').slice(0, 120), videos: vids,
    iframes: document.querySelectorAll('iframe').length,
    overlay: {cover: Math.round(cover * 100) / 100, text: coverText},
    // Only a password field the user can SEE: most sites keep a hidden
    // login form in every page, which says nothing about a wall.
    password_field: [...document.querySelectorAll('input[type=password]')].some(e => {
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';
    }),
  });
})()
""".strip()


@dataclass(frozen=True)
class SolverConfig:
    max_steps: int = 12
    max_seconds: float = 900.0   # a thinking model takes ~30 s per step
    max_tokens: int = 60_000
    max_bad_outputs: int = 2
    #: Steps in a row that change nothing (a wait, a scroll, a refused or
    #: repeated click) before the run is ended: a model that is stuck does
    #: not get to spend the whole step budget finding that out.
    max_idle_steps: int = 3
    max_wait_total_s: float = 150.0   # all waits of one run together
    outline_chars: int = 5000
    settle_s: float = 2.5


@dataclass
class SolveResult:
    result: str
    reason: str = ""
    barrier: str = ""
    trace: list = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    duration_s: float = 0.0

    @property
    def clicks(self) -> int:
        return sum(1 for s in self.trace if s.get("action") == "click" and s.get("ok"))

    @property
    def replayable(self) -> int:
        """Steps a playbook could be made of: effective clicks, player
        clicks and long waits (``learner/compiler.py``)."""
        from learner.compiler import _replayable
        return sum(1 for s in self.trace if _replayable(s) and s.get("ok"))


def media_state(probe: dict | None) -> dict:
    """The few numbers a step's effect is described with."""
    p = probe if isinstance(probe, dict) else {}
    vids = [v for v in (p.get("videos") or []) if isinstance(v, dict)]
    return {
        "videos": len(vids),
        "with_src": sum(1 for v in vids if v.get("src")),
        "playing": sum(1 for v in vids if v.get("src") and not v.get("paused")),
        "longest_s": max((int(v.get("dur") or 0) for v in vids), default=0),
        "overlay": float((p.get("overlay") or {}).get("cover") or 0.0),
        "url": str(p.get("url") or ""),
        "password": bool(p.get("password_field")),
    }


NOTHING = "nothing changed"

#: Labels the Solver never clicks, whatever the model says. Deliberately
#: broad: a wrongly refused click costs one step, a wrongly allowed one
#: walks into an account flow.
FORBIDDEN_LABEL_RE = re.compile(
    r"\b(log[\s-]?in|sign[\s-]?in|sign[\s-]?up|register|registration|subscribe|subscription|"
    r"create (an )?account|my account|continue with|connect with|buy|purchase|checkout|"
    r"add to cart|premium|upgrade|install|get the app|download (the )?app)\b"
    r"|ログイン|ログオン|サインイン|サインアップ|会員登録|新規登録|無料登録|登録する|アカウント"
    r"|購入|課金|カートに|プレミアム|有料|インストール|アプリを|アプリで|パスワード"
    r"|登录|登入|注册|购买|下载app",
    re.I,
)

_SECOND_LEVEL = {"co", "com", "ne", "or", "ac", "go", "net", "org", "gov", "edu"}


def site_of(url: str) -> str:
    """The registrable part of a URL's host (crude eTLD+1): enough to tell
    "still on this site" from "somewhere else"."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def gone_js(ref: int) -> str:
    """JS: is outline element ``@ref`` no longer there / no longer visible?"""
    return _GONE_JS.replace("__N__", str(int(ref)))


_GONE_JS = r"""
(() => {
  const el = document.querySelector('[data-paprika-id="__N__"]');
  if (!el) return true;
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return true;
  const cs = getComputedStyle(el);
  return cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0;
})()
""".strip()


def _target_key(desc: dict) -> tuple:
    return (str(desc.get("tag") or ""), str(desc.get("text") or ""), tuple(desc.get("cands") or []),
            str(desc.get("href") or ""))


def describe_effect(before: dict, after: dict) -> str:
    parts = []
    if after["url"] != before["url"]:
        parts.append(f"page changed to {after['url'][:100]}")
    if abs(after["overlay"] - before["overlay"]) >= 0.05:
        parts.append(f"overlay {before['overlay']:.2f} -> {after['overlay']:.2f}")
    if after["with_src"] != before["with_src"]:
        parts.append(f"videos with a source {before['with_src']} -> {after['with_src']}")
    if after["playing"] != before["playing"]:
        parts.append(f"playing {before['playing']} -> {after['playing']}")
    if after["longest_s"] != before["longest_s"]:
        parts.append(f"longest video {before['longest_s']}s -> {after['longest_s']}s")
    return "; ".join(parts) or NOTHING


def build_user_prompt(url: str, probe: dict | None, outline: str, history: list[str],
                      *, notes: str = "", outline_chars: int = 5000) -> str:
    p = probe if isinstance(probe, dict) else {}
    ms = media_state(p)
    ov = p.get("overlay") or {}
    lines = [
        f"TARGET URL: {url}",
        f"CURRENT URL: {p.get('url') or '?'}",
        f"TITLE: {p.get('title') or ''}",
        f"VIDEO ELEMENTS: {ms['videos']} (with a source: {ms['with_src']}, playing: {ms['playing']}, "
        f"longest: {ms['longest_s']}s)   IFRAMES: {p.get('iframes') or 0}",
        f"LARGEST OVERLAY: covers {ms['overlay']:.0%} of the screen"
        + (f', text: "{ov.get("text")}"' if ov.get("text") else ""),
    ]
    if p.get("password_field"):
        lines.append("NOTE: the page contains a password field.")
    if notes:
        lines.append(f"WHAT IS KNOWN ABOUT THIS KIND OF PAGE: {notes[:600]}")
    if history:
        lines.append("YOUR STEPS SO FAR:")
        lines.extend(f"  {i}. {h}" for i, h in enumerate(history, 1))
    lines.append("")
    lines.append("ELEMENTS:")
    lines.append((outline or "(none)")[:outline_chars])
    return "\n".join(lines)


def parse_decision(obj: dict) -> dict:
    """Normalise the model's reply to one of the allowed actions.
    Raises LLMBadOutput for anything else."""
    if not isinstance(obj, dict):
        raise LLMBadOutput("reply is not an object")
    action = str(obj.get("action") or "").strip().lower()
    why = str(obj.get("why") or "")[:200]
    if action == "click":
        try:
            ref = int(str(obj.get("ref")).lstrip("@"))
        except (TypeError, ValueError):
            raise LLMBadOutput(f"click without a numeric ref: {obj.get('ref')!r}")
        if ref < 1:
            raise LLMBadOutput(f"click ref out of range: {ref}")
        return {"action": "click", "ref": ref, "why": why}
    if action in ("scroll", "done", "click_player"):
        return {"action": action, "why": why}
    if action == "wait":
        try:
            seconds = float(obj.get("seconds") or 2)
        except (TypeError, ValueError):
            seconds = 2.0
        return {"action": "wait", "seconds": max(2.0, min(60.0, seconds)), "why": why}
    if action == "give_up":
        barrier = str(obj.get("barrier") or "other").strip().lower()
        return {"action": "give_up", "barrier": barrier if barrier in BARRIERS else "other", "why": why}
    raise LLMBadOutput(f"unknown action {action!r}")


async def solve(hub, llm, url: str, *, cfg: SolverConfig | None = None, notes: str = "",
                sleep=asyncio.sleep) -> SolveResult:
    """Explore ``url`` in one browser session and return the trace."""
    cfg = cfg or SolverConfig()
    t0 = time.monotonic()
    meter = start_meter()
    trace: list[dict] = []
    history: list[str] = []

    def _out(result: str, reason: str = "", barrier: str = "") -> SolveResult:
        return SolveResult(
            result=result, reason=reason[:120], barrier=barrier, trace=trace,
            llm_calls=meter.calls, tokens=meter.tokens,
            duration_s=round(time.monotonic() - t0, 1),
        )

    try:
        sid = await hub.open_session(url)
    except HubUnavailable as e:
        return _out(INFRA, f"open_session: {e}")
    except HubError as e:
        return _out(GAVE_UP, f"open_session rejected: {e}", "other")

    bad = 0
    waited = 0.0                   # seconds spent in "wait" so far
    player_clicks = 0
    idle = 0                       # consecutive steps without any effect
    dead: set = set()              # targets whose click changed nothing
    try:
        await sleep(cfg.settle_s)
        probe = await hub.evaluate(sid, _PROBE_JS)
        # Where the page settled after loading is "the site": a redirect on
        # load is the site's own doing, a change after OUR click is not.
        home = site_of(media_state(probe)["url"]) or site_of(url)
        elsewhere = home if home != site_of(url) else ""
        for _step in range(cfg.max_steps):
            if time.monotonic() - t0 > cfg.max_seconds or meter.tokens > cfg.max_tokens:
                break
            if idle >= cfg.max_idle_steps:
                trace.append({"action": "stopped", "why": "no_progress"})
                break
            outline = await hub.outline(sid)
            before = media_state(probe if isinstance(probe, dict) else None)
            user = build_user_prompt(url, probe if isinstance(probe, dict) else None, outline,
                                     history, notes=notes, outline_chars=cfg.outline_chars)
            try:
                decision = parse_decision(await llm.chat_json(SYSTEM_PROMPT, user))
            except LLMBadOutput as e:
                bad += 1
                trace.append({"action": "bad_output", "detail": str(e)[:120]})
                if bad > cfg.max_bad_outputs:
                    return _out(BAD_OUTPUT, str(e))
                continue

            act = decision["action"]
            if act == "done":
                trace.append({"action": "done", "why": decision["why"]})
                break
            if act == "give_up":
                trace.append({"action": "give_up", "barrier": decision["barrier"], "why": decision["why"]})
                if elsewhere:
                    # The model judged another site's page. Whatever it found
                    # there -- no video, a login wall -- says nothing about
                    # the key, and would shelve it for a month.
                    return _out(GAVE_UP, f"loaded {elsewhere} instead: {decision['why']}", "redirected")
                return _out(GAVE_UP, decision["why"], decision["barrier"])
            if act == "wait":
                seconds = min(decision["seconds"], max(2.0, cfg.max_wait_total_s - waited))
                waited += seconds
                await sleep(seconds)
                probe = await hub.evaluate(sid, _PROBE_JS)
                effect = describe_effect(before, media_state(probe))
                trace.append({"action": "wait", "seconds": seconds, "ok": effect != NOTHING, "effect": effect})
                idle = 0 if effect != NOTHING else idle + 1
                if waited >= cfg.max_wait_total_s:
                    idle = cfg.max_idle_steps          # no more waiting in this run
                history.append(f"waited {seconds:.0f}s: " + effect)
                continue
            if act == "click_player":
                if player_clicks >= MAX_PLAYER_CLICKS:
                    idle += 1
                    trace.append({"action": "click_player", "ok": False, "refused": "too_many"})
                    history.append("click_player: REFUSED, the player was clicked "
                                   f"{MAX_PLAYER_CLICKS} times already")
                    continue
                pdesc = await hub.evaluate(sid, player_tag_js())
                if not isinstance(pdesc, dict) or not pdesc.get("found"):
                    idle += 1
                    trace.append({"action": "click_player", "ok": False, "refused": "no_player"})
                    history.append("click_player: REFUSED, there is no video player on the page")
                    continue
                if pdesc.get("external") or FORBIDDEN_LABEL_RE.search(str(pdesc.get("text") or "")):
                    idle += 1
                    trace.append({"action": "click_player", "ok": False, "refused": "covered", "desc": pdesc})
                    history.append("click_player: REFUSED, the middle of the player is covered by a link to "
                                   "another site or an account button")
                    continue
                player_clicks += 1
                try:
                    await hub.click(sid, f'[data-pb-t="{PLAYER_MARK}"]')
                    ok = True
                except HubError as e:
                    ok = False
                    history.append(f"click_player: FAILED ({str(e)[:60]})")
                await sleep(cfg.settle_s)
                probe = await hub.evaluate(sid, _PROBE_JS)
                after = media_state(probe if isinstance(probe, dict) else None)
                effect = describe_effect(before, after)
                left = bool(after["url"]) and site_of(after["url"]) != home
                login_form = after["password"] and not before["password"]
                if ok and (left or login_form):
                    trace.append({"action": "click_player", "ok": False, "performed": True, "desc": pdesc,
                                  "why": decision["why"], "effect": effect})
                    trace.append({"action": "aborted", "why": "left_site" if left else "login_form_appeared"})
                    return _out(GAVE_UP, f"a click led to {after['url'][:80]}" if left
                                else "a click opened a login form", "other" if left else "login")
                changed = ok and effect != NOTHING
                idle = 0 if changed else idle + 1
                trace.append({"action": "click_player", "ok": changed, "performed": ok, "desc": pdesc,
                              "why": decision["why"], "effect": effect})
                if ok:
                    history.append(f"clicked the player ({pdesc.get('tag')}): {effect}")
                continue
            if act == "scroll":
                await hub.scroll(sid, "down")
                await sleep(1.0)
                probe = await hub.evaluate(sid, _PROBE_JS)
                trace.append({"action": "scroll"})
                effect = describe_effect(before, media_state(probe))
                idle = 0 if effect != NOTHING else idle + 1
                history.append("scrolled down: " + effect)
                continue

            # click
            ref = decision["ref"]
            desc = await hub.evaluate(sid, describe_js(ref))
            if not isinstance(desc, dict):
                idle += 1
                trace.append({"action": "click", "ref": ref, "ok": False, "refused": "no_such_element"})
                history.append(f"click @{ref}: REFUSED, there is no element @{ref}")
                continue
            label = f'@{ref} {desc.get("tag")} "{str(desc.get("text") or "")[:40]}"'
            if desc.get("editable"):
                idle += 1
                trace.append({"action": "click", "ref": ref, "ok": False, "refused": "editable", "desc": desc})
                history.append(f"click {label}: REFUSED, it is an input field and you cannot type")
                continue
            if desc.get("external"):
                idle += 1
                trace.append({"action": "click", "ref": ref, "ok": False, "refused": "external", "desc": desc})
                history.append(f"click {label}: REFUSED, it links to another site")
                continue
            if FORBIDDEN_LABEL_RE.search(str(desc.get("text") or "")):
                idle += 1
                trace.append({"action": "click", "ref": ref, "ok": False, "refused": "forbidden_label", "desc": desc})
                history.append(f"click {label}: REFUSED, logging in, signing up, paying or installing is not allowed")
                continue
            target = _target_key(desc)
            if target in dead:
                idle += 1
                trace.append({"action": "click", "ref": ref, "ok": False, "refused": "no_effect_before", "desc": desc})
                history.append(f"click {label}: REFUSED, you already clicked it and nothing changed")
                continue
            try:
                await hub.click(sid, f'[data-paprika-id="{ref}"]')
                ok = True
            except HubError as e:
                ok = False
                history.append(f"click {label}: FAILED ({str(e)[:60]})")
            await sleep(cfg.settle_s)
            gone = (await hub.evaluate(sid, gone_js(ref))) is True if ok else False
            probe = await hub.evaluate(sid, _PROBE_JS)
            after = media_state(probe if isinstance(probe, dict) else None)
            effect = describe_effect(before, after)
            if gone and after["url"] == before["url"]:
                effect = "the clicked element disappeared" + ("" if effect == NOTHING else "; " + effect)
            left = bool(after["url"]) and site_of(after["url"]) != home
            login_form = after["password"] and not before["password"]
            if ok and (left or login_form):
                # Not ours to continue, and nothing from this run may be
                # replayed: the whole trace is discarded by returning a
                # non-trace result.
                why = "left_site" if left else "login_form_appeared"
                trace.append({"action": "click", "ref": ref, "ok": False, "performed": True, "desc": desc,
                              "why": decision["why"], "effect": effect})
                trace.append({"action": "aborted", "why": why})
                return _out(GAVE_UP, f"a click led to {after['url'][:80]}" if left
                            else "a click opened a login form", "other" if left else "login")
            changed = ok and effect != NOTHING
            idle = 0 if changed else idle + 1
            if not changed:
                dead.add(target)
            # ``ok`` means "performed AND it did something": a click with no
            # visible effect is not worth replaying, so the Compiler never
            # sees it.
            trace.append({"action": "click", "ref": ref, "ok": changed, "performed": ok, "desc": desc,
                          "why": decision["why"], "effect": effect})
            if ok:
                history.append(f"clicked {label}: {effect}")
    except HubUnavailable as e:
        return _out(INFRA, f"session: {e}")
    except LLMUnavailable as e:
        return _out(INFRA, f"llm: {e}")
    except HubError as e:
        # The session died under us (worker restart, TTL): infrastructure.
        return _out(INFRA, f"session: {e}")
    finally:
        await hub.close_session(sid)

    res = _out(TRACE)
    if res.replayable > 0:
        return res
    ended_by_model = any(s.get("action") == "done" for s in trace)
    return _out(NO_ACTION if ended_by_model else EXHAUSTED,
                "model saw nothing to click" if ended_by_model else "limit reached without a click")
