"""System / probe routes: /health, /info, /icon.svg, /favicon.ico.

The smallest endpoints the hub exposes -- monitoring probes plus the
SVG logo every HTML surface references. Kept separate from the admin
UI shell route (``/`` -> /static/admin.js) which stays in app.py for
now because of the inline _ADMIN_HTML template (planned for extraction
to a Jinja2/StaticFiles template in a later round).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from server.hub._state import config, state


router = APIRouter(tags=["System"])


# Inline SVG for the paprika logo. Served from /icon.svg so every HTML
# surface (admin dashboard, /screenshots, /jobs/*/log, per-job
# galleries) references one URL instead of duplicating markup. Also
# used as the favicon via ``<link rel="icon" type="image/svg+xml">``.
_PAPRIKA_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1254 1254">
  <defs><style>
    .st0 { fill: #5c9138; }
    .st1 { fill: #f5b800; }
  </style></defs>
  <path class="st1" d="M486.2,486.1c68.1-6.4,95,45.1,159.4,45,56.2-.1,81.5-41.9,134.4-45.5,117.4-7.9,145.8,135.5,143.3,225.8-3.5,123.4-50,342.8-129.3,439.3-80,97.3-221.9,89.2-299.5-6.7-76.3-94.2-125.2-307.3-129.3-427.4-2.7-80.1,20.5-221.3,121-230.7Z"/>
  <path class="st1" d="M843.7,1155.2c-4.7-3.4,5.9-14.6,8.2-18.5,42.9-70.2,69.3-160.6,86.7-240.9,25.8-119.3,55.3-321.8-45.9-413.5-3.8-3.5-24.7-17.4-25.2-18.9-1.2-3.6,8.3-17,10.1-22.8,4-12.6,4.2-27.3.8-40-4.9-18.2-16.7-16.6,1.6-32.1,42.3-36,108-49.4,161.5-36.4,190.6,46.2,117.1,355,76.5,486.2-33.9,109.3-113.6,302.8-236.1,333.9-7.1,1.8-33.1,6.8-38.3,3.1Z"/>
  <path class="st1" d="M278.5,327.6c42.1-3.4,106.4,10.8,136.7,42.2,7.1,7.3-2.3,13.5-5.8,20.8-8.8,18.5-9.4,41.1,0,59.5,2.2,4.3,10.2,11.8,8.9,15.9s-21.2,17.4-25.2,21.5c-97.7,101.7-69.3,293.7-40.8,419.1,17.4,76.7,43.3,157.4,84.3,224.7,4.5,7.3,20.5,22.5,6.2,25.1-33.3,6.1-90.2-29.8-114.2-51.6-112-101.4-181.3-341.5-198.9-488.2-13.7-114.3,3.3-277.2,148.7-289.1Z"/>
  <path class="st0" d="M479.5,372.1c3.2-3.3,0-12.9.7-18.6,4.1-30.7,46.3-42.4,72.1-35.5,6.7,1.8,12.9,7.4,18,8.7s2.1,1.1,3.4-.8c2.5-3.7,3.9-25.9,5.1-32.2,17.6-95.9,76.8-212.3,183.1-231.1,48.4-8.5,88.8,31.3,63.1,76.4-16.6,29.3-44.8,21.9-69.9,38-36.3,23.2-61,93.4-58.2,135,1.6,24.5,6.3,13.8,21.4,8,25.3-9.8,66.2-3.8,75.5,25.6,2.3,7.2.4,19.8,2.9,23.7s12.6,5.9,17,8.3c23.6,12.9,36.2,46.1,13.8,66.2s-16.7,3.6-28.6,3.2c-23-.8-46.6.6-68.2,9.1-29.3,11.6-44.9,33.1-79.6,34.9-46.4,2.5-66.7-27-107-38.3-18.5-5.2-39-6.7-58.2-5.7s-18.3,6.6-30.6-2.7c-23.6-17.7-12.2-51.2,9.6-65,3.3-2.1,13.1-5.5,14.7-7.2Z"/>
  <path class="st1" d="M1053.8,307.2c-68.5-18.8-139.1-4.2-195,38.7-2.8,2.2-8.3,9.8-11.8,9.1s-12.5-9-12.9-9.7c-1.8-2.9-2.6-11.8-4.6-16.7-12.3-30.3-46.8-45.3-77.4-49.1-3.7-.5-15.5,1.2-16.3-2.3-.7-2.9,8.7-28.5,10.7-33,3.3-7.5,15.7-30.3,22-33.9s22.5-6.6,29.5-7.8c81.6-13.7,197.1,20,247.9,88.2,3.6,4.9,8.6,10,8,16.6Z"/>
  <path class="st1" d="M546.3,280.4c-37.6-2.5-83.2,15.3-96.8,53s-1.1,19.2-9,15.1-16.8-13-24.7-17.9c-49.2-30.3-107.4-38.3-163.7-24.8-.9-5.8,3.6-9.7,6.7-13.9,24.1-33.1,68.1-59.9,106.1-73.6,60.9-22,136.2-28.7,196.1-1l-14.8,63.1Z"/>
</svg>
"""


def _hub_version() -> str:
    """Lazy lookup back into app.py for the running version string.

    Lives there because the value is derived from /app/VERSION + a
    cached read; centralising the read on app.py keeps the disk
    access in one place. Imported lazily so this module can be
    imported before app.py has finished loading.
    """
    from server.hub._version import _hub_version as _v
    return _v()


def _static_asset_version() -> str:
    """Cache-buster hash that covers BOTH .py source AND static assets.

    ``_hub_version()`` only hashes .py files (it must match the
    worker-side version for fleet handshake). For the ``?v=`` tag on
    admin.js / admin.css we need a hash that also changes when those
    static files change, otherwise the browser serves stale JS/CSS
    after a JS-only edit + container restart.

    Falls back to ``_hub_version()`` if the static dir is missing.
    """
    import hashlib
    from pathlib import Path

    base = _hub_version()
    h = hashlib.sha256(base.encode())
    static_dir = Path("/app/server/hub/static")
    if static_dir.is_dir():
        for p in sorted(static_dir.rglob("*")):
            if p.is_file() and p.suffix in (".js", ".css"):
                try:
                    h.update(p.name.encode())
                    h.update(b"\0")
                    h.update(p.read_bytes())
                except Exception:
                    continue
    return h.hexdigest()[:12]


@router.get("/icon.svg")
async def paprika_icon():
    """Serve the paprika logo SVG. Referenced by ``<link rel="icon">``
    + ``<img class="logo">`` in every HTML surface. Cached for a day
    so the browser stops re-fetching."""
    return Response(
        content=_PAPRIKA_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
async def favicon_redirect():
    """Browsers default to /favicon.ico. Redirect to the SVG so we
    only maintain one logo file. Modern browsers accept SVG favicons
    via the /icon.svg ``<link rel="icon" type="image/svg+xml">`` too;
    this catches older / inflexible clients."""
    return RedirectResponse(url="/icon.svg", status_code=302)


@router.get("/info", response_class=PlainTextResponse)
async def info_text() -> str:
    """Plain-text equivalent of the old /, kept for terminal users."""
    nstats = state.registry.stats() if state.registry else {"count": 0}
    novnc_lines = ""
    if state.registry:
        for w in state.registry.connections.values():
            nv = w.capabilities.novnc_url
            if nv:
                sep = "&" if "?" in nv else "?"
                full = (f"{nv}{sep}autoconnect=1&resize=scale&reconnect=1"
                        if "autoconnect" not in nv else nv)
                novnc_lines += f"    {w.worker_id:<24} {full}\n"
    return (
        "paprika hub\n"
        f"  data dir   : {config.data_dir.resolve()}\n"
        f"  store      : {state.store_kind}\n"
        f"  workers    : {nstats['count']} connected\n"
        f"  max local  : {config.max_concurrent_jobs}\n"
        "\n"
        "  Client API:\n"
        "    POST /jobs                       submit\n"
        "    GET  /jobs                       list\n"
        "    GET  /jobs/{id}                  status (+worker_id, +novnc_url)\n"
        "    GET  /jobs/{id}/result           final result\n"
        "    GET  /jobs/{id}/page.html / /log.txt / /assets/{f}\n"
        "    WS   /jobs/{id}/events           live log stream\n"
        "    DELETE /jobs/{id}                remove\n"
        "    GET  /https://...                URL pass-through (same as POST /jobs)\n"
        "\n"
        "  Worker API:\n"
        "    WS   /workers/{worker_id}/link\n"
        "    POST /jobs/{id}/assets\n"
        "    POST /jobs/{id}/files/{kind}\n"
        "    GET  /workers\n"
        + (f"\n  noVNC viewers:\n{novnc_lines}" if novnc_lines else "")
    )


@router.get("/health")
async def health() -> dict:
    """The probe every operational sidecar reads. ``version`` surfaces
    the source hash so external monitoring can spot fleet drift --
    compare to each worker's reported version via /workers to see
    which ones haven't auto-updated yet."""
    nstats = state.registry.stats() if state.registry else {"count": 0}
    return {
        "status": "ok",
        "store": state.store_kind,
        "workers": nstats["count"],
        "version": _hub_version(),
    }



# ============================================================================
# Admin UI shell + screenshots page (#2B-G3-partial)
# ============================================================================

# Admin UI HTML shell. Extracted to server/hub/static/admin.html -- this
# was the last big HTML-in-Python blob (a ~3000-line r"""...""" literal
# here; the JS/CSS were already external static files). Loaded once at
# import. The @@PAPRIKA_VERSION@@ cache-bust token is substituted per
# request in admin_ui().
from pathlib import Path as _Path

_ADMIN_HTML = (
    _Path(__file__).resolve().parent.parent / "static" / "admin.html"
).read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def admin_ui() -> HTMLResponse:
    # JS / CSS are served from /static (mounted above) and tagged with
    # ``?v={hub_version}`` so a fresh deploy invalidates browser caches
    # without us having to fight ETags. The shell HTML itself is small
    # enough that no-cache on it is cheap and avoids stale-version-tag
    # foot-guns.
    html = _ADMIN_HTML.replace("@@PAPRIKA_VERSION@@", _static_asset_version())
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# ----------------------------------------------------------------------------
# /screenshots — standalone fullscreen-friendly live preview grid
# (URL retained as ``/screenshots`` for the bookmark-compat; the page
# content + endpoints inside it all use the new "preview" naming)
# ----------------------------------------------------------------------------



_SCREENSHOTS_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Paprika · live preview</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: #1b1b1b; color: #ddd; font: 14px/1.5 -apple-system,"Segoe UI",sans-serif; }
  header {
    display: flex; align-items: center; gap: 1.2rem;
    padding: .7rem 1.2rem;
    background: #c0392b; color: #fff;
    position: sticky; top: 0; z-index: 10;
    box-shadow: 0 2px 6px rgba(0,0,0,.4);
  }
  header h1 { margin: 0; font-size: 1.05rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.5rem; }
  header h1 .logo { width: 1.5em; height: 1.5em; vertical-align: middle; flex-shrink: 0; }
  header h1 small { font-weight: 400; opacity: .8; margin-left: .4rem; }
  .ctrl { display: flex; align-items: center; gap: .8rem; margin-left: auto; font-size: .85rem; }
  .ctrl label { display: flex; align-items: center; gap: .35rem; }
  .ctrl input[type=number] {
    width: 56px; padding: 2px 6px;
    background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.35);
    border-radius: 4px; color: #fff; font: inherit;
  }
  .ctrl select {
    padding: 2px 6px;
    background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.35);
    border-radius: 4px; color: #fff; font: inherit;
  }
  .ctrl select option { color: #222; }
  .ctrl a { color: #ffe; text-decoration: none; opacity: .85; }
  .ctrl a:hover { opacity: 1; text-decoration: underline; }
  main { padding: 1rem; }
  #status { font-size: .82rem; opacity: .8; margin-bottom: .8rem; }
  .grid {
    display: grid;
    grid-template-columns: var(--ss-cols, repeat(auto-fill, minmax(var(--tile-min, 380px), 1fr)));
    gap: .8rem;
  }
  .tile {
    background: #000; border-radius: 8px; overflow: hidden;
    position: relative; aspect-ratio: 16/9;
    box-shadow: 0 4px 12px rgba(0,0,0,.5);
    display: block; text-decoration: none; color: inherit;
    transition: transform .15s, box-shadow .15s, outline-color .15s;
    outline: 2px solid transparent;
  }
  a.tile { cursor: pointer; }
  a.tile:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.6); outline-color: #c0392b; }
  a.tile:hover .open { opacity: 1; }
  .tile img { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
  .tile .lbl {
    position: absolute; top: 6px; left: 8px;
    font-size: .78rem; padding: 2px 8px;
    background: rgba(0,0,0,.6); color: #fff; border-radius: 4px;
    backdrop-filter: blur(2px); pointer-events: none;
  }
  .tile .open {
    position: absolute; top: 6px; right: 8px;
    font-size: .72rem; padding: 2px 8px;
    background: rgba(192,57,43,.85); color: #fff; border-radius: 4px;
    opacity: 0; transition: opacity .15s; pointer-events: none;
  }
  .tile .err {
    position: absolute; bottom: 8px; left: 8px; right: 8px;
    font-size: .76rem; color: #ffb4b4;
    padding: 4px 8px; background: rgba(120,0,0,.75); border-radius: 4px;
    pointer-events: none;
  }
  .tile.busy { outline-color: #c0392b; box-shadow: 0 0 0 1px rgba(192,57,43,.45), 0 0 14px rgba(192,57,43,.4); }
  .tile.idle { opacity: .78; }
  .tile .badge {
    position: absolute; bottom: 8px; right: 8px;
    font-size: .72rem; font-weight: 600; color: #fff;
    padding: 2px 9px; border-radius: 10px;
    display: flex; align-items: center; gap: 5px;
    pointer-events: none;
  }
  .tile .badge.running   { background: rgba(192,57,43,.85); }
  .tile .badge.keepalive { background: rgba(217,127,38,.9); }
  .tile .badge.idle      { background: rgba(80,80,90,.7); }
  .tile .badge .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: currentColor; display: inline-block;
  }
  .tile .badge.running .dot   { animation: paprikaSsPulse2 1.2s infinite; }
  .tile .badge.keepalive .dot { animation: paprikaSsPulse2 2.4s infinite; }
  @keyframes paprikaSsPulse2 { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  .spin { animation: paprikaSpinCW 1s linear infinite; display:inline-block; }
  @keyframes paprikaSpinCW { to { transform: rotate(360deg); } }
  /* Loading overlay: striped backdrop + spinner. Shown on first
     image load only (class is removed by JS in the 'load' / 'error'
     handlers below). Polling refreshes do not re-add the class so
     subsequent updates silently swap pixels without flicker. */
  .tile.loading::before {
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(135deg, #1a1a22 25%, #222 25%, #222 50%, #1a1a22 50%, #1a1a22 75%, #222 75%);
    background-size: 18px 18px;
    z-index: 1;
  }
  .tile.loading::after {
    content: '';
    position: absolute;
    top: 50%; left: 50%;
    width: 34px; height: 34px;
    margin: -17px 0 0 -17px;
    border: 3px solid rgba(255,255,255,.15);
    border-top-color: #c0392b;
    border-radius: 50%;
    animation: paprikaSsLoad2 0.8s linear infinite;
    z-index: 2;
    pointer-events: none;
  }
  @keyframes paprikaSsLoad2 { to { transform: rotate(360deg); } }
  .tile.loading .open { display: none; }
  .tile .sub {
    position: absolute; top: 28px; left: 8px; right: 8px;
    font-size: .68rem; color: #fff;
    padding: 1px 6px; background: rgba(0,0,0,.55);
    border-radius: 3px; pointer-events: none;
    text-overflow: ellipsis; overflow: hidden; white-space: nowrap;
  }
  .empty { padding: 2rem; text-align: center; opacity: .6; }
</style>
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:8px;" title="ホーム (Submit form) に戻る"><img src="/icon.svg" alt="paprika" class="logo"> Paprika</a> <small>live preview</small></h1>
  <span class="ctrl">
    <label>every <input type="number" id="ssInterval" value="5" min="1" max="60"> s</label>
    <label><input type="checkbox" id="ssEnabled" checked> on</label>
    <label>size
      <select id="ssSize">
        <option value="180">XS</option>
        <option value="280">S</option>
        <option value="380" selected>M</option>
        <option value="520">L</option>
        <option value="720">XL</option>
      </select>
    </label>
    <label>cols
      <select id="ssCols">
        <option value="auto" selected>auto</option>
        <option value="2">2</option>
        <option value="3">3</option>
        <option value="4">4</option>
        <option value="5">5</option>
        <option value="6">6</option>
        <option value="8">8</option>
        <option value="10">10</option>
        <option value="12">12</option>
      </select>
    </label>
    <a href="/" title="back to admin UI">← admin</a>
  </span>
</header>
<main>
  <div id="status">connecting…</div>
  <div id="grid" class="grid"></div>
</main>
<script>
const ssTiles = new Map();
let ssTimer = null;

function ssKey(w, s) { return w + '/' + s; }

async function syncGrid() {
  let data;
  let jobsData = [];
  try {
    const [r1, r2] = await Promise.all([
      fetch('/workers'),
      fetch('/jobs?limit=200'),
    ]);
    data = await r1.json();
    const jResp = await r2.json().catch(() => ({}));
    jobsData = (jResp && jResp.jobs) || [];
  } catch (e) {
    document.getElementById('status').textContent = 'fetch failed: ' + e.message;
    return;
  }
  const workers = data.workers || [];
  // Build "busy" lookup: ``worker_id|lane`` -> running job. The
  // standalone page mirrors the in-app Live preview panel; both
  // surfaces drive the RUNNING / IDLE badge from the same data.
  const busy = new Map();
  for (const j of jobsData) {
    if (j.status !== 'running') continue;
    if (j.worker_id == null || j.lane_idx == null) continue;
    busy.set(ssKey(j.worker_id, j.lane_idx), j);
  }
  const busyCount = busy.size;
  document.getElementById('status').textContent =
    `${workers.length} worker(s) · ${[...ssTiles.keys()].length} tile(s) · ${busyCount} running`;
  const want = new Set();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    for (let i = 0; i < cap; i++) want.add(ssKey(w.worker_id, i));
  }
  for (const [k, t] of [...ssTiles.entries()]) {
    if (!want.has(k)) { t.wrap.remove(); ssTiles.delete(k); }
  }
  const grid = document.getElementById('grid');
  if (want.size === 0 && ssTiles.size === 0) {
    grid.innerHTML = '<div class="empty">no workers connected</div>';
    return;
  }
  const ph = grid.querySelector('.empty');
  if (ph) ph.remove();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    const urls = w.lane_novnc_urls || w.slot_novnc_urls || [];
    for (let i = 0; i < cap; i++) {
      const key = ssKey(w.worker_id, i);
      if (ssTiles.has(key)) continue;
      const novncUrl = urls[i];
      const wrap = document.createElement(novncUrl ? 'a' : 'div');
      // Same as the in-app Live preview tile: start in 'loading'
      // state, drop the class on first 'load' / 'error'. CSS overlay
      // shows a diagonal stripe + spinner so the tile doesn't look
      // broken during the 1-2 s cold-start fetch.
      wrap.className = 'tile loading';
      if (novncUrl) {
        let u = novncUrl;
        if (!u.includes('autoconnect')) {
          u += (u.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
        }
        wrap.href = u; wrap.target = '_blank'; wrap.rel = 'noopener';
        wrap.title = 'Open noVNC viewer in a new tab';
      }
      const img = document.createElement('img');
      img.alt = key; img.loading = 'lazy';
      const lbl = document.createElement('span');
      lbl.className = 'lbl';
      lbl.textContent = w.worker_id + ' #' + i;
      const sub = document.createElement('span');
      sub.className = 'sub';
      sub.style.display = 'none';
      const badge = document.createElement('span');
      badge.className = 'badge idle';
      badge.innerHTML = '<span class="dot"></span><span class="badge-text">IDLE</span>';
      const open = document.createElement('span');
      open.className = 'open'; open.textContent = '↗ noVNC';
      const err = document.createElement('span');
      err.className = 'err'; err.style.display = 'none';
      wrap.appendChild(img); wrap.appendChild(lbl); wrap.appendChild(sub);
      if (novncUrl) wrap.appendChild(open);
      wrap.appendChild(badge);
      wrap.appendChild(err);
      wrap.classList.add('idle');
      grid.appendChild(wrap);
      img.addEventListener('error', () => {
        err.textContent = 'capture failed (worker offline or lane not ready)';
        err.style.display = 'block';
        wrap.classList.remove('loading');
      });
      img.addEventListener('load', () => {
        err.style.display = 'none';
        wrap.classList.remove('loading');
      });
      ssTiles.set(key, { wrap, img, err, sub, badge });
    }
  }
  // Flip RUNNING / KEEPALIVE / IDLE for every tile based on the
  // jobs snapshot. KEEPALIVE = crawl finished but session is alive
  // for the operator (= keep_session Fetch jobs + post-detach
  // codegen-loop sessions).
  for (const [key, tile] of ssTiles) {
    const job = busy.get(key);
    if (job) {
      const isKeepalive = !!(
        job.progress && job.progress.phase === 'keepalive'
      );
      tile.wrap.classList.add('busy');
      tile.wrap.classList.remove('idle');
      tile.badge.className = isKeepalive ? 'badge keepalive' : 'badge running';
      const txt = tile.badge.querySelector('.badge-text');
      if (txt) txt.textContent = isKeepalive ? 'KEEPALIVE' : 'RUNNING';
      if (tile.sub) {
        tile.sub.textContent = job.url || `(job ${job.job_id})`;
        tile.sub.style.display = '';
      }
    } else {
      tile.wrap.classList.add('idle');
      tile.wrap.classList.remove('busy');
      tile.badge.className = 'badge idle';
      const txt = tile.badge.querySelector('.badge-text');
      if (txt) txt.textContent = 'IDLE';
      if (tile.sub) { tile.sub.textContent = ''; tile.sub.style.display = 'none'; }
    }
  }
}

function refreshImages() {
  if (!document.getElementById('ssEnabled').checked) return;
  const t = Date.now();
  // Match the size shown in the grid so we don't ship more pixels than
  // we'll display. The browser still scales to the tile, but smaller
  // requests = less ffmpeg work + smaller JPEG over the wire.
  const w = parseInt(document.getElementById('ssSize').value, 10) || 380;
  const px = Math.min(1920, Math.max(160, w * 2));  // 2x for crisp on hi-dpi
  // Pair the larger pixel size with mid-range JPEG quality. Standalone
  // monitor view is "operator wants to read the screen" so we trade
  // a bit more bandwidth for less compression artefacts vs the inline
  // tile grid (quality=30).
  const q = 45;
  for (const [key, tile] of ssTiles) {
    if (tile._loading) continue;
    const [wid, lane] = key.split('/');
    const url =
      `/workers/${encodeURIComponent(wid)}/lanes/${encodeURIComponent(lane)}/preview`
      + `?width=${px}&quality=${q}&t=${t}`;
    // Double-buffer: preload off-screen, swap only after decode.
    const probe = new Image();
    tile._loading = true;
    probe.onload = () => {
      tile.img.src = probe.src;
      tile._loading = false;
    };
    probe.onerror = () => {
      tile.img.src = url;
      tile._loading = false;
    };
    probe.src = url;
  }
}

function resetTimer() {
  if (ssTimer) clearInterval(ssTimer);
  const sec = Math.max(1, parseInt(document.getElementById('ssInterval').value, 10) || 5);
  ssTimer = setInterval(refreshImages, sec * 1000);
  refreshImages();
}

function applySize() {
  const w = parseInt(document.getElementById('ssSize').value, 10) || 380;
  document.documentElement.style.setProperty('--tile-min', w + 'px');
}
function applyCols() {
  const v = document.getElementById('ssCols').value;
  const grid = document.getElementById('grid');
  if (v === 'auto') {
    grid.style.removeProperty('--ss-cols');
  } else {
    grid.style.setProperty('--ss-cols', `repeat(${parseInt(v,10)}, 1fr)`);
  }
}

document.getElementById('ssInterval').addEventListener('change', resetTimer);
document.getElementById('ssEnabled').addEventListener('change', () => {
  if (document.getElementById('ssEnabled').checked) resetTimer();
  else if (ssTimer) { clearInterval(ssTimer); ssTimer = null; }
});
document.getElementById('ssSize').addEventListener('change', () => {
  applySize(); refreshImages();
});
document.getElementById('ssCols').addEventListener('change', () => {
  applyCols(); refreshImages();
});

applySize();
applyCols();
syncGrid().then(resetTimer);
setInterval(syncGrid, 5000);
</script>
</body>
</html>
"""


@router.get("/screenshots", response_class=HTMLResponse)
async def screenshots_page() -> str:
    return _SCREENSHOTS_HTML

