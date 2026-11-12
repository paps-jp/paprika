"""Compiler: a Solver trace -> deterministic playbook actions (design §9.2).

The Solver clicks elements by their per-observation number (``@N``), which
means nothing on the next page load. The Compiler turns each click that
was actually carried out into a target description that survives a reload
and a different URL of the same template:

    1. up to 4 CSS selectors that matched exactly that element, most
       stable first (id / data-* / name / aria-label / classes), with
       anything that looks URL-specific (long digit or hex runs) dropped;
    2. the element's visible text as a last resort.

The emitted actions use only what the worker's existing recipe player
already runs (server/worker/agent/recipe.py -- ``evaluate`` / ``click`` /
``wait``), so a playbook needs no worker change:

    evaluate   find the target by (1) then (2) and mark it
    click      a real click on the marked element
    wait       let the page react

If the target is not on the page the ``click`` fails with "not found",
which the worker reports in ``facts.recipe`` -- a cache miss, and the job
simply continues as a plain fetch.

Everything here is pure. The JavaScript is data: it is built here and run
by the hub session API (Solver) or the worker recipe player (replay).
"""
from __future__ import annotations

import json
import re

COMPILER_VERSION = 1
MAX_CLICKS = 6
MAX_SELECTORS = 4
WAIT_AFTER_CLICK_S = 1.5
WAIT_BEFORE_FIRST_S = 1.0

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


def _target_id(desc: dict) -> tuple:
    return (tuple(stable_selectors(desc)), stable_text(desc))


def compile_trace(trace: list[dict]) -> tuple[list[dict] | None, str]:
    """(actions, reason). ``actions`` is None when nothing replayable came
    out; ``reason`` says why.

    Only clicks that were actually performed are kept; looking, scrolling
    and waiting are things a plain fetch does by itself. Clicking the same
    target twice in a row is kept once.
    """
    clicks = [
        s for s in (trace or [])
        if isinstance(s, dict) and s.get("action") == "click" and s.get("ok")
        and isinstance(s.get("desc"), dict)
    ]
    if not clicks:
        return None, "no_click_in_trace"
    actions: list[dict] = [{"kind": "wait", "seconds": WAIT_BEFORE_FIRST_S}]
    prev = None
    n = 0
    for s in clicks:
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


def summarize(actions: list[dict]) -> list[dict]:
    """A compact, human-readable form of compiled actions for the audit
    trail (the JS is recoverable from the playbook row itself)."""
    out = []
    for a in actions or []:
        if a.get("kind") == "evaluate":
            m = re.search(r"const sels = (\[.*?\]), text = (\".*?\"), mark = ", a.get("expression") or "", re.S)
            if m:
                try:
                    out.append({"find": json.loads(m.group(1)), "text": json.loads(m.group(2))})
                    continue
                except Exception:
                    pass
            out.append({"evaluate": True})
        elif a.get("kind") == "click":
            out.append({"click": a.get("selector")})
    return out
