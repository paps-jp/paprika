"""Compiler: a Solver trace -> deterministic playbook actions (design §9.2).

The Solver clicks elements by their per-observation number (``@N``), which
means nothing on the next page load. The Compiler turns each click that
was actually carried out into a target description that survives a reload
and a different URL of the same template:

    1. up to 4 CSS selectors that matched exactly that element, most
       stable first (id / data-* / name / aria-label / classes), with
       anything that looks URL-specific (long digit or hex runs) dropped;
    2. the element's visible text as a last resort.

The emitted actions use only what the worker's recipe player runs
(server/worker/agent/recipe.py -- ``evaluate`` / ``click`` / ``wait``):

    evaluate   find the target by (1) then (2) and mark it
    click      ``el.click()`` on the marked element
    wait       let the page react

The video player is the exception. It is found by size, not by name, and
clicked with a real mouse click at a point inside its box (``"mouse":
true`` with ``dx`` / ``dy``, pixels from its middle): the only click that is
a user gesture and the only one that reaches a player inside an embedded
frame. A worker too old
to know ``mouse`` falls back to ``el.click()``.

If the target is not on the page the ``click`` fails with "not found",
which the worker reports in ``facts.recipe`` -- a cache miss, and the job
simply continues as a plain fetch.

Everything here is pure. The JavaScript is data: it is built here and run
by the hub session API (Solver) or the worker recipe player (replay).
"""
from __future__ import annotations

import json
import re

COMPILER_VERSION = 2
MAX_CLICKS = 6
MAX_SELECTORS = 4
WAIT_AFTER_CLICK_S = 1.5
WAIT_BEFORE_FIRST_S = 1.0
#: A wait the model chose is replayed only when it is long enough to be a
#: decision ("let the advertisement finish"), not a pause between looks.
MIN_REPLAYED_WAIT_S = 5.0
MAX_REPLAYED_WAIT_S = 90.0
MAX_PLAYER_CLICKS = 3
PLAYER_MARK = "player"

_UNSTABLE_RE = re.compile(r"\d{3,}|[0-9a-f]{8,}", re.I)


def describe_js(ref: int) -> str:
    """JS returning a JSON description of outline element ``@ref`` (or
    ``null``): tag, text, candidate selectors, and the two things the
    Solver must refuse to click -- an editable field, an off-site link."""
    return _DESCRIBE_JS.replace("__N__", str(int(ref)))


_DESCRIBE_JS = r"""
(() => {
  const el = document.querySelector('[data-paprika-id="__N__"]');
  if (!el) return JSON.stringify(null);
  const esc = (v) => (window.CSS && CSS.escape) ? CSS.escape(v) : String(v).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  const q = (v) => '"' + String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const tag = el.tagName.toLowerCase();
  const uniq = (sel) => {
    try { const m = document.querySelectorAll(sel); return m.length === 1 && m[0] === el; }
    catch (_) { return false; }
  };
  const cands = [];
  const push = (sel) => { if (sel && cands.length < 8 && !cands.includes(sel) && uniq(sel)) cands.push(sel); };
  if (el.id) push('#' + esc(el.id));
  for (const a of ['data-testid', 'data-test', 'data-qa', 'data-action', 'data-role', 'name', 'aria-label', 'title', 'value']) {
    const v = el.getAttribute(a);
    if (v && v.length <= 80) push(tag + '[' + a + '=' + q(v) + ']');
  }
  const cls = [...el.classList].filter(c => c.length <= 40);
  if (cls.length) {
    push(tag + '.' + cls.slice(0, 3).map(esc).join('.'));
    push(tag + '.' + esc(cls[0]));
  }
  const p = el.parentElement;
  if (p && p.id) push('#' + esc(p.id) + ' > ' + tag);
  if (p && p.classList.length) push(p.tagName.toLowerCase() + '.' + esc(p.classList[0]) + ' > ' + tag);
  const text = (el.getAttribute('aria-label') || el.innerText || el.value || '')
    .replace(/\s+/g, ' ').trim().slice(0, 80);
  let external = false, href = '';
  if (tag === 'a') {
    try {
      const u = new URL(el.href, location.href);
      href = u.href.slice(0, 200);
      external = u.origin !== location.origin;
    } catch (_) {}
  }
  const type = (el.getAttribute('type') || '').toLowerCase();
  const editable = (tag === 'input' && !['button', 'submit', 'checkbox', 'radio', 'image', 'reset'].includes(type || 'text'))
    || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
  const r = el.getBoundingClientRect();
  return JSON.stringify({
    tag, role: el.getAttribute('role') || '', type, text, cands, href, external, editable,
    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
  });
})()
""".strip()


_TAG_JS = r"""
(() => {
  const sels = __SELS__, text = __TEXT__, mark = __MARK__;
  const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll('[data-pb-t="' + mark + '"]').forEach(e => e.removeAttribute('data-pb-t'));
  let el = null;
  for (const s of sels) {
    try { const m = [...document.querySelectorAll(s)].filter(vis); if (m.length) { el = m[0]; break; } }
    catch (_) {}
  }
  if (!el && text) {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const want = norm(text);
    for (const c of document.querySelectorAll('a,button,input[type=button],input[type=submit],[role=button],[onclick]')) {
      if (!vis(c)) continue;
      if (norm(c.getAttribute('aria-label') || c.innerText || c.value) === want) { el = c; break; }
    }
  }
  if (el) el.setAttribute('data-pb-t', mark);
  return !!el;
})()
""".strip()


def is_stable_selector(sel: str) -> bool:
    """False for selectors that are likely specific to one URL / one page
    load (ids with counters, hashed class names, our own per-observation
    numbering)."""
    if not isinstance(sel, str) or not sel or len(sel) > 200:
        return False
    if "data-paprika-id" in sel or "data-pb-t" in sel:
        return False
    return not _UNSTABLE_RE.search(sel)


def stable_selectors(desc: dict) -> list[str]:
    out: list[str] = []
    for s in (desc or {}).get("cands") or []:
        if is_stable_selector(s) and s not in out:
            out.append(s)
        if len(out) >= MAX_SELECTORS:
            break
    return out


def stable_text(desc: dict) -> str:
    t = str((desc or {}).get("text") or "").strip()
    # A label with a long number in it (a view count, a price, an id) will
    # not be the same on another URL of the template.
    if not t or len(t) > 60 or _UNSTABLE_RE.search(t):
        return ""
    return t


def tag_js(selectors: list[str], text: str, mark: str) -> str:
    return (
        _TAG_JS.replace("__SELS__", json.dumps(list(selectors), ensure_ascii=False))
        .replace("__TEXT__", json.dumps(text or "", ensure_ascii=False))
        .replace("__MARK__", json.dumps(str(mark)))
    )


def click_actions(desc: dict, mark: str) -> list[dict] | None:
    """The three actions for one click, or None when the element cannot be
    described in a way that would survive a reload."""
    sels = stable_selectors(desc)
    text = stable_text(desc)
    if not sels and not text:
        return None
    return [
        {"kind": "evaluate", "expression": tag_js(sels, text, mark)},
        {"kind": "click", "selector": f'[data-pb-t="{mark}"]'},
        {"kind": "wait", "seconds": WAIT_AFTER_CLICK_S},
    ]


def spot_offset(value) -> int:
    """A distance in pixels from the middle of the player (anything
    unusable: 0, the middle itself).

    Pixels, not a share of the box: a player's buttons keep their size and
    their distance from its middle when the player is drawn larger or
    smaller, and it is drawn at a different size from one page load to the
    next (seen live 2026-11-14: the same embedded player 587, 840 and 988 px
    high, its play button 162 px below the middle every time)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if f != f:  # NaN
        return 0
    return int(round(min(4000.0, max(-4000.0, f))))


_PLAYER_JS = r"""
(() => {
  const mark = __MARK__, want = __WANT__, hint = __HINT__;
  let dx = __DX__, dy = __DY__;
  const largest = (sel, lo, hi) => {
    let best = null, area = 0;
    for (const e of document.querySelectorAll(sel)) {
      const r = e.getBoundingClientRect();
      if (r.width < 320 || r.height < 180) continue;
      const ratio = r.width / r.height;
      if (ratio < lo || ratio > hi) continue;
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const a = r.width * r.height;
      if (a > area) { best = e; area = a; }
    }
    return best;
  };
  const media = largest('video, iframe', 0.5, 2.6);
  const box = (() => {
    // Wrappers around a player are named like one too, and their size
    // changes with whatever else they hold. Of the boxes that are not
    // small next to the largest (a related-video thumbnail is), take the
    // one shaped most like a screen: that is the player itself.
    const all = [];
    for (const e of document.querySelectorAll('[id*="player" i], [class*="player" i], [id*="video" i], [class*="video" i]')) {
      if (e.tagName === 'VIDEO' || e.tagName === 'IFRAME') continue;
      const r = e.getBoundingClientRect();
      if (r.width < 320 || r.height < 180 || r.width > innerWidth || r.height > innerHeight * 1.1) continue;
      const ratio = r.width / r.height;
      if (ratio < 1.2 || ratio > 2.4) continue;
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      all.push({e: e, area: r.width * r.height, off: Math.abs(ratio - 16 / 9)});
    }
    const most = Math.max(0, ...all.map(c => c.area));
    let best = null;
    for (const c of all) {
      if (c.area < most * 0.25) continue;
      if (!best || c.off < best.off - 0.02 || (Math.abs(c.off - best.off) <= 0.02 && c.area < best.area)) best = c;
    }
    return best ? best.e : null;
  })();
  const inside = (e, p) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    return p[0] >= r.left - 4 && p[0] <= r.right + 4 && p[1] >= r.top - 4 && p[1] <= r.bottom + 4;
  };
  let best = want === 'media' ? media : want === 'box' ? box : (media || box);
  let hinted = false;
  if (hint) {
    const h = inside(media, hint) ? media : inside(box, hint) ? box : null;
    if (h) {
      const r = h.getBoundingClientRect();
      best = h; hinted = true;
      dx = Math.round(hint[0] - (r.left + r.width / 2));
      dy = Math.round(hint[1] - (r.top + r.height / 2));
    }
  }
  document.querySelectorAll('[data-pb-t="' + mark + '"]').forEach(e => e.removeAttribute('data-pb-t'));
  if (!best) return JSON.stringify({found: false});
  try { best.scrollIntoView({block: 'center', behavior: 'instant'}); } catch (e) {}
  const r = best.getBoundingClientRect();
  const px = Math.min(Math.max(r.left + r.width / 2 + dx, r.left + 1), r.right - 1);
  const py = Math.min(Math.max(r.top + r.height / 2 + dy, r.top + 1), r.bottom - 1);
  const top = document.elementFromPoint(px, py) || best;
  const link = top.closest ? top.closest('a[href]') : null;
  let external = false;
  if (link) {
    try {
      const h = new URL(link.href, location.href).hostname.split('.').slice(-2).join('.');
      external = !!h && h !== location.hostname.split('.').slice(-2).join('.');
    } catch (e) {}
  }
  if (!external) best.setAttribute('data-pb-t', mark);
  const frame = best.tagName === 'IFRAME';
  const vid = best.tagName === 'VIDEO' ? best : (frame ? null : best.querySelector('video'));
  return JSON.stringify({
    found: true, hinted: hinted, external: external,
    kind: frame ? 'iframe' : (best.tagName === 'VIDEO' ? 'video' : 'box'), pick: best === media ? 'media' : 'box',
    src: frame ? (best.src || '').slice(0, 200) : '',
    playing: !!vid && !vid.paused && !vid.ended && vid.currentTime > 0,
    tag: top.tagName.toLowerCase(), text: (top.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
    vw: innerWidth, vh: innerHeight, dx: dx, dy: dy,
  });
})()
""".strip()

PLAYER_PICKS = ("media", "box")


def player_tag_js(mark: str = PLAYER_MARK, dx: float = 0, dy: float = 0, *, want: str = "",
                  hint: tuple | None = None) -> str:
    """JS that finds the page's main video player, marks it, and reports as
    a JSON string what a click (dx, dy) pixels from its middle would hit.

    Players rarely have a stable name, so the player is found by what it
    is. Two kinds of element can be one, and each kind has ONE candidate,
    the largest that is big enough (320x180) and shaped like a player:

      media   a ``<video>`` or an embedded frame. The player inside a frame
              has no ``<video>`` the page can see at all.
      box     an element named like a player (``player`` / ``video`` in its
              id or class) and shaped like a screen. Many players have
              neither a ``<video>`` nor a frame until they are clicked:
              only a poster in such a box. Of several, the one closest to
              16:9 is taken, not the largest: wrappers are named like a
              player too, and a point measured from the middle of a wrapper
              is somewhere else on the next page load.

    The size and shape rules are what keeps advertising out: a banner that
    plays a video is 300x250 or a wide strip, never a 16:9 box.

    Which candidate is used: ``want`` ("media" / "box") names the kind;
    without it, the media candidate if there is one, else the box. With
    ``hint`` -- a point on the screen, in pixels -- the candidate that point
    lies in is used and (dx, dy) become that point; a hint in neither
    candidate is ignored (``hinted`` false in the report).

    The MARK goes on the player itself; the click is then a real mouse
    click at that point of its box, which lands on whatever is drawn there
    -- a play button, an overlay, the content of the frame. The player is
    left unmarked (so the click finds nothing) when that point is covered
    by a link to another site."""
    hint_js = "null"
    if hint is not None:
        hint_js = json.dumps([spot_offset(hint[0]), spot_offset(hint[1])])
    return (
        _PLAYER_JS.replace("__MARK__", json.dumps(str(mark)))
        .replace("__WANT__", json.dumps(want if want in PLAYER_PICKS else ""))
        .replace("__HINT__", hint_js)
        .replace("__DX__", repr(spot_offset(dx)))
        .replace("__DY__", repr(spot_offset(dy)))
    )


def player_click_actions(mark: str, dx: float = 0, dy: float = 0, want: str = "") -> list[dict]:
    dx, dy = spot_offset(dx), spot_offset(dy)
    return [
        {"kind": "evaluate", "expression": player_tag_js(mark, dx, dy, want=want)},
        {"kind": "click", "selector": f'[data-pb-t="{mark}"]', "mouse": True, "dx": dx, "dy": dy},
        {"kind": "wait", "seconds": WAIT_AFTER_CLICK_S},
    ]


def _target_id(desc: dict) -> tuple:
    return (tuple(stable_selectors(desc)), stable_text(desc))


def _replayable(s: dict) -> str:
    """'click' / 'player' / 'wait' for a step worth replaying, else ''."""
    if not isinstance(s, dict):
        return ""
    act = s.get("action")
    if act == "click" and s.get("ok") and isinstance(s.get("desc"), dict):
        return "click"
    if act == "click_player" and s.get("performed"):
        return "player"
    if act == "wait" and s.get("ok") and float(s.get("seconds") or 0) >= MIN_REPLAYED_WAIT_S:
        return "wait"
    return ""


def compile_trace(trace: list[dict]) -> tuple[list[dict] | None, str]:
    """(actions, reason). ``actions`` is None when nothing replayable came
    out; ``reason`` says why.

    Kept, in the order they happened: clicks on a listed element that did
    something, clicks on the video player, and long waits that did something
    (an advertisement ending). Looking, scrolling and short pauses are things
    a plain fetch does by itself. Clicking the same target twice in a row is
    kept once -- except the player, where the first click often only opens
    an advertisement and the second one starts the video.

    A player click that changed nothing is kept only when a later step did
    change something: it may have been the necessary first click.
    """
    steps = [(k, s) for s in (trace or []) for k in [_replayable(s)] if k]
    # Drop trailing player clicks without effect: nothing came of them.
    while steps and steps[-1][0] == "player" and not steps[-1][1].get("ok"):
        steps.pop()
    if not steps:
        return None, "no_click_in_trace"
    actions: list[dict] = [{"kind": "wait", "seconds": WAIT_BEFORE_FIRST_S}]
    prev = None
    n = players = 0
    for kind, s in steps:
        if kind == "wait":
            actions.append({"kind": "wait", "seconds": min(float(s["seconds"]), MAX_REPLAYED_WAIT_S)})
            prev = None
            continue
        if kind == "player":
            players += 1
            if players > MAX_PLAYER_CLICKS:
                continue
            spot = s.get("spot") if isinstance(s.get("spot"), dict) else {}
            actions.extend(player_click_actions(f"p{players}", spot.get("dx", 0), spot.get("dy", 0),
                                                str(spot.get("pick") or "")))
            prev = None
            continue
        tid = _target_id(s["desc"])
        if tid == prev:
            continue
        prev = tid
        n += 1
        if n > MAX_CLICKS:
            return None, "too_many_clicks"
        acts = click_actions(s["desc"], f"s{n}")
        if acts is None:
            return None, "unstable_target"
        actions.extend(acts)
    return actions, "ok"


#: The wait added when replayed steps start a player but only a short clip
#: is saved: long enough for a 30-second advertisement to play out.
AD_WAIT_S = 40.0


def has_long_wait(actions: list[dict] | None, at_least_s: float = 30.0) -> bool:
    for a in actions or []:
        try:
            if a.get("kind") == "wait" and float(a.get("seconds") or 0) >= at_least_s:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def with_ad_wait(actions: list[dict]) -> list[dict]:
    """The same steps, followed by one wait for an advertisement to end."""
    return list(actions) + [{"kind": "wait", "seconds": AD_WAIT_S}]


MANUAL_KINDS = ("wait", "click", "evaluate")
MAX_MANUAL_ACTIONS = 20
MAX_MANUAL_BYTES = 8000


def check_manual_actions(actions) -> str:
    """'' when ``actions`` is a list of steps a person may hand in, else what
    is wrong with it. The same three kinds the Compiler itself emits -- so
    whatever is accepted here the worker's recipe player already runs."""
    if not isinstance(actions, list) or not actions:
        return "actions must be a non-empty list"
    if len(actions) > MAX_MANUAL_ACTIONS:
        return f"more than {MAX_MANUAL_ACTIONS} actions"
    if len(json.dumps(actions, ensure_ascii=False)) > MAX_MANUAL_BYTES:
        return "actions too large"
    for i, a in enumerate(actions, 1):
        if not isinstance(a, dict) or a.get("kind") not in MANUAL_KINDS:
            return f"action {i}: kind must be one of {', '.join(MANUAL_KINDS)}"
        if a["kind"] == "wait":
            try:
                if not 0 < float(a.get("seconds") or 0) <= MAX_REPLAYED_WAIT_S:
                    return f"action {i}: wait needs 0 < seconds <= {MAX_REPLAYED_WAIT_S:.0f}"
            except (TypeError, ValueError):
                return f"action {i}: wait needs a number of seconds"
        if a["kind"] == "click" and not str(a.get("selector") or "").strip():
            return f"action {i}: click needs a selector"
        if a["kind"] == "click":
            for k, lo, hi in (("fx", 0.0, 1.0), ("fy", 0.0, 1.0), ("dx", -4000.0, 4000.0), ("dy", -4000.0, 4000.0)):
                if a.get(k) is None:
                    continue
                try:
                    if not lo <= float(a[k]) <= hi:
                        return f"action {i}: {k} must be between {lo:g} and {hi:g}"
                except (TypeError, ValueError):
                    return f"action {i}: {k} must be a number between {lo:g} and {hi:g}"
        if a["kind"] == "evaluate" and not str(a.get("expression") or "").strip():
            return f"action {i}: evaluate needs an expression"
    return ""


def summarize(actions: list[dict]) -> list[dict]:
    """A compact, human-readable form of compiled actions for the audit
    trail (the JS is recoverable from the playbook row itself)."""
    out = []
    for a in actions or []:
        if a.get("kind") == "evaluate":
            if "largest('video, iframe'" in (a.get("expression") or ""):
                out.append({"find": "the video player"})
                continue
            m = re.search(r"const sels = (\[.*?\]), text = (\".*?\"), mark = ", a.get("expression") or "", re.S)
            if m:
                try:
                    out.append({"find": json.loads(m.group(1)), "text": json.loads(m.group(2))})
                    continue
                except Exception:
                    pass
            out.append({"evaluate": str(a.get("expression") or "")[:80]})
        elif a.get("kind") == "click" and a.get("mouse"):
            out.append({"mouse_click": a.get("selector"),
                        "from_middle": [spot_offset(a.get("dx")), spot_offset(a.get("dy"))]})
        elif a.get("kind") == "click":
            out.append({"click": a.get("selector")})
        elif a.get("kind") == "wait" and float(a.get("seconds") or 0) >= MIN_REPLAYED_WAIT_S:
            out.append({"wait_s": a.get("seconds")})
    return out
