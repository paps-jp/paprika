// --- bulk cleanup (kept-last-N + age filter) ----------------------------
//   1) ask the user for "older than N days" + keep_last
//   2) POST dry_run=true to /jobs/cleanup, show preview
//   3) if confirmed, POST dry_run=false
async function bulkCleanup() {
  // Step 1: gather inputs.
  const olderRaw = prompt(
    "Delete completed jobs older than how many days? (blank = any age)\n"
    + "  - In-flight jobs are NEVER deleted.\n"
    + "  - The last 10 most-recent jobs are always kept (protected_count).",
    "7"
  );
  if (olderRaw === null) return;
  const older = olderRaw.trim() === '' ? null : parseInt(olderRaw, 10);
  if (olderRaw.trim() !== '' && !(older >= 0)) {
    alert('age must be a non-negative integer or blank');
    return;
  }
  const btn = document.getElementById('bulkCleanup');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> scanning…';
  // Step 2: dry-run preview.
  let preview;
  try {
    const body = {dry_run: true, keep_last: 10};
    if (older !== null) body.older_than_days = older;
    const r = await fetch('/jobs/cleanup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      alert('cleanup preview failed (HTTP ' + r.status + ')');
      return;
    }
    preview = await r.json();
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
  const n = preview.candidate_count;
  const totalMB = (preview.candidate_total_bytes / (1024*1024)).toFixed(1);
  if (n === 0) {
    alert(`No matching jobs found (skipped ${preview.skipped.length}). Nothing to clean.`);
    return;
  }
  if (!confirm(
    `${n} job(s) match (${totalMB} MiB total). Delete now?\n\n`
    + `Sample:\n` + preview.candidates.slice(0, 5).map(c =>
        `  ${c.job_id} · ${c.status} · ${(c.size_bytes/(1024*1024)).toFixed(1)} MiB`
        + (c.age_days ? ` · ${c.age_days.toFixed(1)}d old` : '')
      ).join('\n')
    + (n > 5 ? `\n  ...and ${n - 5} more` : '')
  )) return;
  // Step 3: actually delete.
  btn.disabled = true;
  btn.innerHTML = `<span class="ljp-spinner"></span> deleting ${n}…`;
  try {
    const body = {dry_run: false, keep_last: 10};
    if (older !== null) body.older_than_days = older;
    const r = await fetch('/jobs/cleanup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      alert('cleanup failed (HTTP ' + r.status + ')');
      return;
    }
    const result = await r.json();
    const freedMB = (result.total_freed_bytes / (1024*1024)).toFixed(1);
    alert(`Deleted ${result.deleted.length} job(s), freed ${freedMB} MiB.`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
    refresh();
  }
}

async function bulkDelete() {
  const raw = await fetch('/jobs').then(r => r.json());
  const jobs = Array.isArray(raw) ? raw : (raw.jobs || []);
  if (!jobs.length) return;
  if (!confirm(`Delete ALL ${jobs.length} jobs (with their files)?`)) return;
  const btn = document.getElementById('bulkDelete');
  const origLabel = btn.innerHTML;
  let done = 0;
  const total = jobs.length;
  btn.disabled = true;
  btn.style.background = '#f8e6c8';
  btn.style.borderColor = '#d99';
  btn.innerHTML = `<span class="ljp-spinner"></span> deleting 0 / ${total}…`;
  const updateLabel = () => {
    btn.innerHTML = `<span class="ljp-spinner"></span> deleting ${done} / ${total}…`;
  };
  // Limit concurrency so we don't fire 100+ DELETEs simultaneously --
  // each one does a large `rm -rf` server-side and can starve the disk.
  const concurrency = 6;
  let cursor = 0;
  async function worker() {
    while (cursor < jobs.length) {
      const j = jobs[cursor++];
      try {
        await fetch('/jobs/' + j.job_id, {method:'DELETE'});
      } catch (_) {}
      done += 1;
      updateLabel();
    }
  }
  await Promise.all(Array.from({length: concurrency}, () => worker()));
  btn.disabled = false;
  btn.style.background = '#eee';
  btn.style.borderColor = '#ccc';
  btn.innerHTML = origLabel;
  refresh();
}

// --- live preview -----------------------------------------------------------
const ssTiles = new Map();
let ssTimer = null;

// --- #screens pager (20 tiles / page) -----------------------------------
// Paging the live-preview grid does double duty under the push model: only the
// CURRENT page's tiles are polled, so only those ~20 workers get marked watched
// and self-capture + push -- the rest quiesce. _ssVisibleKeys (set by
// applyScreenshotPaging) is exactly the lane set refreshScreenshots fans out to.
const SS_PAGE_SIZE = 20;
let _ssPage = 0;
let _ssVisibleKeys = null;   // null = pre-init (poll all on the very first tick)

function ssKey(workerId, lane) { return workerId + '/' + lane; }

function buildTile(workerId, laneIdx, novncUrl) {
  // ALWAYS an <a> so a RUNNING tile can deep-link to its in-app Live Job
  // Panel (#live/<job>) even when the worker advertises no noVNC URL --
  // syncScreenshotBusyState sets the #live href on busy tiles regardless of
  // noVNC. (Previously a worker without a lane noVNC URL got a plain <div>, so
  // clicking its running preview navigated nowhere.) An idle tile with no
  // noVNC simply carries no href (not a link), which is fine.
  // The "↗ open" badge on the top-right hints that the tile is clickable.
  const wrap = document.createElement('a');
  // Start in 'loading' state -- the first /preview round-trip can be
  // 1-2 seconds when the worker's lane just woke up and ffmpeg hasn't
  // primed Xvfb's frame buffer yet. The CSS overlay (.ssitem.loading)
  // paints a diagonal stripe + spinner so the tile looks intentional
  // instead of "broken / black". Cleared on the first img 'load' or
  // 'error' event below.
  wrap.className = 'ssitem idle loading';
  wrap.dataset.sskey = ssKey(workerId, laneIdx);   // pager maps tile -> lane key
  if (novncUrl) {
    let url = novncUrl;
    if (!url.includes('autoconnect')) {
      url += (url.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
    }
    // Cache the LAN-direct URL on the element so syncScreenshotBusyState
    // can fall back to it when a lane is idle (no active session ->
    // nothing to hub-proxy). Operators occasionally want to peek at an
    // idle lane's fluxbox desktop directly for debugging.
    wrap.dataset.directUrl = url;
    wrap.href = url;
    wrap.target = '_blank';
    wrap.rel = 'noopener';
    wrap.title = 'Open noVNC viewer in a new tab';
  }
  const img = document.createElement('img');
  img.alt = workerId + ' #' + laneIdx; img.loading = 'lazy';
  const lbl = document.createElement('span');
  lbl.className = 'sslabel'; lbl.textContent = workerId + ' #' + laneIdx;
  // Sub-label for the running job's URL (set/cleared in
  // syncScreenshotBusyState).
  const sub = document.createElement('span');
  sub.className = 'sssub';
  sub.style.display = 'none';
  // RUNNING / IDLE badge, updated by syncScreenshotBusyState.
  const badge = document.createElement('span');
  badge.className = 'ssbadge idle';
  badge.innerHTML = '<span class="dot"></span><span class="ssbadge-text">IDLE</span>';
  const open = document.createElement('span');
  open.className = 'ssopen'; open.textContent = '↗ noVNC';
  const err = document.createElement('span');
  err.className = 'sserr'; err.style.display = 'none';
  wrap.appendChild(img); wrap.appendChild(lbl); wrap.appendChild(sub);
  if (novncUrl) wrap.appendChild(open);
  wrap.appendChild(badge);
  wrap.appendChild(err);
  img.addEventListener('error', () => {
    err.textContent = 'capture failed (worker offline or lane not ready)';
    err.style.display = 'block';
    // Even an error clears the loading state -- the err stripe at the
    // bottom is the operator's signal that something went wrong, the
    // spinning overlay just gets in the way.
    wrap.classList.remove('loading');
  });
  img.addEventListener('load', () => {
    err.style.display = 'none';
    // First successful frame -- drop the loading overlay so polling
    // refreshes silently swap pixels from here on out. Idempotent
    // (no-op if already removed) so re-firing on every poll is fine.
    wrap.classList.remove('loading');
    // Mark that this tile now has a real painted frame. Once set, the CSS
    // suppresses the OPAQUE loading stripe (.ssitem.loading:not(.has-frame))
    // so any later 'loading' state (e.g. a worker self-updating, or warming
    // between pushes) keeps the LAST good screenshot visible underneath and
    // only overlays the small spinner -- instead of blanking to the diagonal
    // stripe. The new frame still swaps in only once fully decoded (the probe
    // in ssApplyPreviewFrame), so the tile never flashes empty.
    wrap.classList.add('has-frame');
  });
  return { wrap, img, err, sub, badge };
}

function syncScreenshotGrid(workers) {
  const grid = document.getElementById('ssGrid');
  // Track workers mid self-update (pending_update_to set = draining to apply a
  // new version, then restarting). While restarting they can't capture, so the
  // grid shows a "更新中…" Loading state for them instead of the alarming
  // "warming…" dark box -- it's a normal rollout, not a fault. Refreshed every
  // tick from the live workers list; read in ssApplyPreviewFrame.
  window._ssUpdatingWorkers = new Set(
    (workers || []).filter(w => w && w.pending_update_to).map(w => w.worker_id)
  );
  const want = new Set();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    for (let i = 0; i < cap; i++) want.add(ssKey(w.worker_id, i));
  }
  for (const [k, t] of [...ssTiles.entries()]) {
    if (!want.has(k)) { t.wrap.remove(); ssTiles.delete(k); }
  }
  if (want.size === 0) {
    if (ssTiles.size === 0) {
      grid.innerHTML = '<div class="empty">no workers connected</div>';
    }
    return;
  }
  const placeholder = grid.querySelector('.empty');
  if (placeholder) placeholder.remove();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    // Prefer new field name; fall back to legacy alias.
    const urls = w.lane_novnc_urls || w.slot_novnc_urls || [];
    for (let i = 0; i < cap; i++) {
      const key = ssKey(w.worker_id, i);
      if (ssTiles.has(key)) continue;
      const tile = buildTile(w.worker_id, i, urls[i]);
      grid.appendChild(tile.wrap);
      ssTiles.set(key, tile);
    }
  }
}

// Update each tile's RUNNING / IDLE badge from the live jobs list.
// A lane is "busy" when there's at least one job with status=running
// whose worker_id + lane_idx point at it, OR when an active session
// is currently holding that lane. The session check matters for
// codegen-loop / vision-agent jobs where the JobInfo itself doesn't
// carry worker_id (the runner container drives via /sessions/*, and
// only the SessionInfo records the worker+lane assignment).
// Idle lanes get a quieter look so the eye lands on the active ones.
// Called from refresh() every 2s after the workers+jobs round-trip.
function syncScreenshotBusyState(jobs, sessions) {
  // worker_id|lane -> { job, session, label } (we keep the freshest
  // running mapping for the sub-label).
  const busy = new Map();
  for (const j of (jobs || [])) {
    if (j.status !== 'running') continue;
    if (j.worker_id == null) continue;
    // lane_idx absent or null -> can't map to a lane tile.
    if (j.lane_idx == null) continue;
    busy.set(ssKey(j.worker_id, j.lane_idx), { job: j, session: null });
  }
  // Sessions overlay -- a codegen-loop job's lane only shows up here.
  // If a job-driven entry already exists for this (worker, lane), keep
  // it (more informative label); otherwise synthesize a "session-only"
  // entry so the tile still flips to busy.
  for (const s of (sessions || [])) {
    if (!s) continue;
    if (s.worker_id == null || s.lane_idx == null) continue;
    const key = ssKey(s.worker_id, s.lane_idx);
    if (!busy.has(key)) {
      busy.set(key, { job: null, session: s });
    }
  }
  for (const [key, tile] of ssTiles) {
    const entry = busy.get(key);
    const job = entry && entry.job;
    const sess = entry && entry.session;
    if (entry) {
      // KEEPALIVE = crawl is done but the session is alive for the
      // operator to drive via noVNC. Detected via the job's
      // progress.phase set by WorkerJobComplete when keep_session=True.
      // Falls back to RUNNING for in-progress crawls + codegen-loop
      // sessions (which don't have the keepalive phase).
      const _phase = job && job.progress && job.progress.phase;
      const isKeepalive = _phase === 'keepalive';
      // "downloading": fetch finished, a detached yt-dlp download is
      // still uploading the video. Distinct orange-ish badge.
      const isDownloading = _phase === 'downloading';
      tile.wrap.classList.add('busy');
      tile.wrap.classList.remove('idle');
      tile.badge.className =
        (isKeepalive || isDownloading) ? 'ssbadge keepalive' : 'ssbadge running';
      const txt = tile.badge.querySelector('.ssbadge-text');
      if (txt) txt.textContent =
        isDownloading ? 'DOWNLOADING' : (isKeepalive ? 'KEEPALIVE' : 'RUNNING');
      // Title + sub-label: prefer the job URL when we have it, otherwise
      // fall back to the session's current_url / initial_url so codegen-
      // loop / vision-agent jobs still give the operator something
      // legible.
      const labelUrl = (job && job.url)
        || (sess && (sess.current_url || sess.initial_url))
        || '';
      const labelJobId = (job && job.job_id) || (sess && sess.job_id) || '';
      if (tile.sub) {
        tile.sub.textContent = labelUrl || `(job ${labelJobId})`;
        tile.sub.style.display = '';
      }
      // Click-through: when a job_id is bound to this lane, navigate to
      // the in-app Live Job Panel ("#live/<job_id>") in the SAME tab --
      // that gives the operator the full UI (logs / code / gallery /
      // noVNC) instead of a bare noVNC popup. Falls back to the noVNC
      // popup behaviour (separate tab) when only a session is bound
      // without a job_id (rare; codegen-loop sessions not yet linked
      // to a parent job).
      if (tile.wrap.tagName === 'A') {
        if (labelJobId) {
          tile.wrap.href = '#live/' + encodeURIComponent(labelJobId);
          tile.wrap.removeAttribute('target');
          tile.wrap.removeAttribute('rel');
          tile.wrap.title =
            `Open Live Job Panel for ${(labelJobId).slice(0, 12)}`
            + (labelUrl ? ` — ${labelUrl}` : '');
          // Refresh the on-tile open hint so the "↗ noVNC" badge reads
          // as "↗ live" -- it points at the in-app LJP now, not noVNC.
          const openEl = tile.wrap.querySelector('.ssopen');
          if (openEl) openEl.textContent = '↗ live';
        } else {
          // No job_id but a session exists -- give the operator the
          // noVNC popup as a fallback (session-rooted hub-proxy URL).
          const sid = sess && sess.session_id;
          let nextHref = tile.wrap.dataset.directUrl || '';
          if (sid) {
            nextHref =
              `/sessions/${encodeURIComponent(sid)}/novnc/` +
              `?path=sessions/${encodeURIComponent(sid)}/novnc/websockify` +
              `&autoconnect=1&resize=scale&reconnect=1`;
          }
          if (nextHref) tile.wrap.href = nextHref;
          tile.wrap.target = '_blank';
          tile.wrap.rel = 'noopener';
          tile.wrap.title =
            `Open noVNC viewer in a new tab` + (labelUrl ? ` — ${labelUrl}` : '');
          const openEl = tile.wrap.querySelector('.ssopen');
          if (openEl) openEl.textContent = '↗ noVNC';
        }
      }
    } else {
      tile.wrap.classList.add('idle');
      tile.wrap.classList.remove('busy');
      tile.badge.className = 'ssbadge idle';
      const txt = tile.badge.querySelector('.ssbadge-text');
      if (txt) txt.textContent = 'IDLE';
      if (tile.sub) {
        tile.sub.textContent = '';
        tile.sub.style.display = 'none';
      }
      // Reset title to its "open noVNC" hint when no job is running.
      if (tile.wrap.tagName === 'A') {
        tile.wrap.title = 'Open noVNC viewer in a new tab';
        // Lane is idle -> no session -> revert to the LAN-direct URL
        // cached at tile-build time. Idle lanes have no hub-proxy URL
        // (no session_id to use as the route key); operators clicking
        // an idle tile see the worker's fluxbox desktop directly.
        if (tile.wrap.dataset.directUrl) {
          tile.wrap.href = tile.wrap.dataset.directUrl;
        } else {
          // Idle + no noVNC URL -> not a link. Clear any stale #live href
          // left over from when this lane had a running job.
          tile.wrap.removeAttribute('href');
        }
        // Re-enable new-tab popup behaviour for the idle noVNC path
        // (the busy/job_id branch above strips target/rel to keep the
        // LJP navigation in-tab; restore here so the toggle is clean).
        tile.wrap.target = '_blank';
        tile.wrap.rel = 'noopener';
        const openEl = tile.wrap.querySelector('.ssopen');
        if (openEl) openEl.textContent = '↗ noVNC';
      } else {
        tile.wrap.title = '';
      }
    }
  }
}

// Sort tiles in the grid based on the operator's chosen order.
// Called after syncScreenshotBusyState so status classes are current.
// Mutates only DOM order; ssTiles Map stays intact.
function sortScreenshotGrid() {
  const grid = document.getElementById('ssGrid');
  if (!grid) return;
  const mode = (document.getElementById('ssSort') || {}).value || 'default';
  if (ssTiles.size > 0 && mode !== 'default') {
    // Build a sortable array of [key, tile, sortVal].
    const statusRank = (tile) => {
      if (tile.wrap.classList.contains('busy')) {
        // running (red) = 0, keepalive (orange) = 1
        return tile.badge.classList.contains('keepalive') ? 1 : 0;
      }
      return 2; // idle
    };
    const entries = [...ssTiles.entries()].map(([key, tile]) => {
      const [wid, lane] = key.split('/');
      return { key, tile, wid, lane: parseInt(lane, 10) || 0, status: statusRank(tile) };
    });
    if (mode === 'status') {
      // Running → Keepalive → Idle ; within same status: worker → lane
      entries.sort((a, b) =>
        (a.status - b.status)
        || a.wid.localeCompare(b.wid)
        || (a.lane - b.lane)
      );
    } else if (mode === 'worker') {
      entries.sort((a, b) =>
        a.wid.localeCompare(b.wid) || (a.lane - b.lane)
      );
    } else if (mode === 'worker-desc') {
      entries.sort((a, b) =>
        b.wid.localeCompare(a.wid) || (b.lane - a.lane)
      );
    }
    // Re-append in sorted order (moves existing DOM nodes, doesn't clone).
    for (const e of entries) grid.appendChild(e.tile.wrap);
  }
  // Slice the (now-sorted) grid into pages of SS_PAGE_SIZE.
  applyScreenshotPaging();
}

// Lazily build the pager bar (‹前 / "m–n / total 件" / 次›) above the grid.
function ssEnsurePager() {
  let pager = document.getElementById('ssPager');
  if (pager) return pager;
  const grid = document.getElementById('ssGrid');
  if (!grid || !grid.parentNode) return null;
  pager = document.createElement('div');
  pager.id = 'ssPager';
  pager.style.cssText =
    'display:none; align-items:center; gap:12px; justify-content:center;'
    + 'margin:8px 0; flex-wrap:wrap; font-size:13px;';
  const mkBtn = (id, txt) => {
    const b = document.createElement('button');
    b.id = id; b.type = 'button'; b.textContent = txt;
    b.style.cssText =
      'padding:4px 12px; border:1px solid #ccc; border-radius:6px;'
      + 'background:#f6f6f6; cursor:pointer;';
    return b;
  };
  const prev = mkBtn('ssPagerPrev', '‹ 前');
  const next = mkBtn('ssPagerNext', '次 ›');
  const label = document.createElement('span');
  label.id = 'ssPagerLabel';
  label.style.cssText = 'min-width:210px; text-align:center; color:#555;';
  pager.appendChild(prev); pager.appendChild(label); pager.appendChild(next);
  grid.parentNode.insertBefore(pager, grid);
  prev.addEventListener('click', () => {
    if (_ssPage > 0) { _ssPage--; applyScreenshotPaging(); refreshScreenshots(); }
  });
  next.addEventListener('click', () => {
    _ssPage++; applyScreenshotPaging(); refreshScreenshots();
  });
  return pager;
}

// Show only the current page's tiles (display:none on the rest) and record
// their keys in _ssVisibleKeys so the poll fans out to THIS page only.
function applyScreenshotPaging() {
  const grid = document.getElementById('ssGrid');
  if (!grid) return;
  const tiles = [...grid.querySelectorAll('.ssitem')];   // current (sorted) order
  const total = tiles.length;
  const pages = Math.max(1, Math.ceil(total / SS_PAGE_SIZE));
  if (_ssPage > pages - 1) _ssPage = pages - 1;
  if (_ssPage < 0) _ssPage = 0;
  const start = _ssPage * SS_PAGE_SIZE;
  const end = start + SS_PAGE_SIZE;
  const visible = new Set();
  tiles.forEach((el, idx) => {
    const show = idx >= start && idx < end;
    el.style.display = show ? '' : 'none';
    if (show && el.dataset.sskey) visible.add(el.dataset.sskey);
  });
  _ssVisibleKeys = visible;
  // Pager UI.
  ssEnsurePager();
  const label = document.getElementById('ssPagerLabel');
  const prev = document.getElementById('ssPagerPrev');
  const next = document.getElementById('ssPagerNext');
  const pager = document.getElementById('ssPager');
  if (label) {
    label.textContent = total === 0
      ? '0 件'
      : (start + 1) + '–' + Math.min(total, end) + ' / ' + total + ' 件'
        + '  (ページ ' + (_ssPage + 1) + '/' + pages + ')';
  }
  if (prev) { prev.disabled = _ssPage <= 0; prev.style.opacity = prev.disabled ? '0.45' : ''; }
  if (next) { next.disabled = _ssPage >= pages - 1; next.style.opacity = next.disabled ? '0.45' : ''; }
  if (pager) pager.style.display = pages <= 1 ? 'none' : 'flex';
}

// Resolve the operator's chosen tile size (width × quality) from the
// ssSize <select>. Falls back to the small preset if the control is
// missing or malformed. Persisted to localStorage so a refresh picks
// the same setting (cuts bandwidth when reloading the dashboard).
function ssCurrentSize() {
  const el = document.getElementById('ssSize');
  const raw = (el && el.value) || '320:30';
  const [w, q] = raw.split(':');
  return {
    width: Math.max(80, Math.min(1920, parseInt(w, 10) || 320)),
    quality: Math.max(0, Math.min(100, parseInt(q, 10) || 30)),
  };
}

// One batch is in flight at a time; AbortController lets us cancel the
// stream when the operator disables Live Preview (or the tab is left).
let _ssInFlight = false;
let _ssAbort = null;

// Apply one NDJSON frame record {wid, lane, jpeg_b64 | error} to its tile.
function ssApplyPreviewFrame(rec) {
  if (!rec || rec.wid == null || rec.lane == null) return;
  const tile = ssTiles.get(rec.wid + '/' + rec.lane);
  if (!tile) return;
  if (rec.error) {
    // "warming" is a NORMAL push-preview state, not a failure: the worker is
    // spinning up its self-capture loop (it pushes a frame within ~10s). Show
    // it neutrally -- and if we already have a (cached) frame, just keep it
    // rather than overlaying text, so the grid doesn't flash on every poll.
    const warming = rec.error === 'warming';
    // Worker is self-updating (drain -> restart -> new version): capture is
    // paused mid-restart, so show a Loading state ("更新中…") instead of
    // "warming…" / "capture failed". It's a normal rollout, not a fault, and
    // recovers on its own once the worker reconnects and resumes pushing.
    const updating = warming && window._ssUpdatingWorkers
      && window._ssUpdatingWorkers.has(rec.wid);
    if (updating) {
      if (tile.err) {
        tile.err.textContent = '更新中…';
        tile.err.style.color = '#9aa0a6';
        tile.err.style.display = 'block';
      }
      tile.wrap.classList.add('loading');   // diagonal stripe + spinner = Loading
      return;
    }
    if (tile.err) {
      if (warming && tile.img && tile.img.src) {
        tile.err.style.display = 'none';
      } else {
        tile.err.textContent = warming ? 'warming…' : ('capture failed (' + rec.error + ')');
        tile.err.style.color = warming ? '#9aa0a6' : '';
        tile.err.style.display = 'block';
      }
    }
    tile.wrap.classList.remove('loading');
    return;
  }
  if (!rec.jpeg_b64) return;
  const dataUrl = 'data:image/jpeg;base64,' + rec.jpeg_b64;
  // Double-buffer via an off-screen decode so the visible tile never
  // flashes to blank between frames (same intent as the old probe Image,
  // but a data URL has no network round-trip).
  const probe = new Image();
  probe.onload = () => {
    tile.img.src = dataUrl;        // already decoded -> instant swap
    if (tile.err) tile.err.style.display = 'none';
    tile.wrap.classList.remove('loading');
  };
  probe.onerror = () => {
    if (tile.err) {
      tile.err.textContent = 'capture failed (decode error)';
      tile.err.style.display = 'block';
    }
    tile.wrap.classList.remove('loading');
  };
  probe.src = dataUrl;
}

// Refresh every tile in ONE request. POST /workers/previews takes the whole
// lane set; the hub fans the screenshot RPCs out (bounded) and streams each
// frame back as an NDJSON line the moment it's ready, so tiles fill in
// progressively just like the old per-tile parallelism -- but over a single
// connection instead of ~20 (which on HTTP/1.1 starved the conn pool).
async function refreshScreenshots() {
  if (!document.getElementById('ssEnabled').checked) return;
  // One batch at a time: if the previous stream hasn't finished, skip this
  // tick rather than stacking a second request (replaces the per-tile
  // _loading guard, hoisted to the whole grid).
  if (_ssInFlight) return;
  const { width, quality } = ssCurrentSize();
  const lanes = [];
  // Push model + pager: poll ONLY the current page's tiles, so only those ~20
  // workers are marked watched -> only they self-capture + push (the rest
  // quiesce). _ssVisibleKeys is set by applyScreenshotPaging; null on the very
  // first tick -> poll all so the grid fills immediately.
  const keys = _ssVisibleKeys || ssTiles.keys();
  for (const key of keys) {
    const i = key.lastIndexOf('/');
    if (i < 0) continue;
    lanes.push({ wid: key.slice(0, i), lane: parseInt(key.slice(i + 1), 10) || 0 });
  }
  if (!lanes.length) return;

  _ssInFlight = true;
  _ssAbort = new AbortController();
  try {
    const resp = await fetch('/workers/previews', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ lanes, width, quality }),
      signal: _ssAbort.signal,
    });
    if (!resp.ok || !resp.body) return;  // grid-level failure; next tick retries
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let rec;
        try { rec = JSON.parse(line); } catch (_) { continue; }
        ssApplyPreviewFrame(rec);
      }
    }
  } catch (_) {
    // AbortError (tab-leave / disable) or a transient network blip --
    // swallow; the interval fires another batch next tick.
  } finally {
    _ssInFlight = false;
  }
}
function resetScreenshotTimer() {
  if (ssTimer) clearInterval(ssTimer);
  const sec = Math.max(1, parseInt(document.getElementById('ssInterval').value, 10) || 5);
  ssTimer = setInterval(refreshScreenshots, sec * 1000);
  refreshScreenshots();
}
function applyCols() {
  const v = document.getElementById('ssCols').value;
  const grid = document.getElementById('ssGrid');
  if (v === 'auto') {
    grid.style.setProperty('--ss-cols', 'repeat(auto-fill, minmax(260px, 1fr))');
  } else {
    grid.style.setProperty('--ss-cols', `repeat(${parseInt(v,10)}, 1fr)`);
  }
}

document.getElementById('ssInterval').addEventListener('change', resetScreenshotTimer);
document.getElementById('ssEnabled').addEventListener('change', () => {
  if (document.getElementById('ssEnabled').checked) resetScreenshotTimer();
  else {
    if (ssTimer) { clearInterval(ssTimer); ssTimer = null; }
    if (_ssAbort) { try { _ssAbort.abort(); } catch (_) {} }  // kill in-flight stream
  }
});
document.getElementById('ssCols').addEventListener('change', applyCols);
// Sort change: re-sort immediately, persist to localStorage.
(function wireSsSort() {
  const el = document.getElementById('ssSort');
  if (!el) return;
  try {
    const stored = localStorage.getItem('paprika.ssSort');
    if (stored) el.value = stored;
  } catch (_) {}
  el.addEventListener('change', () => {
    try { localStorage.setItem('paprika.ssSort', el.value); } catch (_) {}
    _ssPage = 0;              // jump back to page 1 on a sort change
    sortScreenshotGrid();
    refreshScreenshots();     // poll the new page-1 tiles immediately
  });
})();
// Size change: trigger an immediate re-render so the operator sees
// the new quality/width without waiting for the next polling tick.
// Persist to localStorage so the dashboard remembers across reloads.
(function wireSsSize() {
  const el = document.getElementById('ssSize');
  if (!el) return;
  try {
    const stored = localStorage.getItem('paprika.ssSize');
    if (stored) el.value = stored;
  } catch (_) {}
  el.addEventListener('change', () => {
    try { localStorage.setItem('paprika.ssSize', el.value); } catch (_) {}
    refreshScreenshots();
  });
})();
applyCols();
// Don't start the polling loop on page load -- setTab() will arm it
// when (and only when) the user opens the Live Preview tab. Without
// this guard, every page load (including direct-link to e.g.
// #submit) starts a screenshot poll that runs invisibly and
// degrades typing/clicking responsiveness in the active tab.
if ((location.hash || '').replace(/^#/, '').split('/')[0] === 'screens') {
  resetScreenshotTimer();
}

document.getElementById('bulkDelete').addEventListener('click', bulkDelete);
document.getElementById('bulkCleanup').addEventListener('click', bulkCleanup);
document.getElementById('openSessionBtn').addEventListener('click', openSessionInteractive);
document.getElementById('closeAllSessions').addEventListener('click', closeAllSessions);

// W: Recent jobs status filter tabs. Click handler delegates by
// data-jobs-status attribute, persists the choice in localStorage,
// updates the .active class for visual feedback, and triggers a
// refresh() so the table re-renders with the new filter immediately.
//
// UX note: even after the 2-second poll's render path was made
// signature-cached, the operator perceives a beat of dead time between
// click and table-swap because refresh() has to round-trip /jobs /
// /workers / /sessions before the new filter is applied. We paint a
// transient "更新中…" row into tbody synchronously so the click feels
// instantly acknowledged; the actual render that follows replaces it.
document.querySelectorAll('#jobsStatusTabs [data-jobs-status]').forEach(btn => {
  btn.addEventListener('click', () => {
    const val = btn.dataset.jobsStatus || 'all';
    _jobsStatusFilter = val;
    try { localStorage.setItem(JOBS_STATUS_FILTER_KEY, val); } catch (_) {}
    // Reset to page 1 so the operator sees results from the top.
    _jobsPage = 0;
    // Visual: toggle .active across siblings.
    document.querySelectorAll('#jobsStatusTabs [data-jobs-status]').forEach(b => {
      const sel = b.dataset.jobsStatus === val;
      b.classList.toggle('active', sel);
      b.setAttribute('aria-selected', sel ? 'true' : 'false');
    });
    // Immediate visual feedback: bust the render cache, paint a
    // loading row into the table body, and dim the table slightly so
    // the rerender lands with a visible swap. The next refresh()
    // tick's full rebuild branch replaces both naturally because the
    // filter is part of visSig (different value -> sig mismatch ->
    // rebuild).
    _jobsLastSig = '';
    const jt = document.getElementById('jobsTable');
    const tb = jt && jt.querySelector('tbody');
    if (tb) {
      tb.innerHTML =
        '<tr><td colspan="10" class="empty" style="text-align:center; color:#888;">' +
        '<iconify-icon icon="lucide:loader-circle" class="spin" style="vertical-align:middle;"></iconify-icon> ' +
        '<span data-i18n="jobs.loading">読み込み中…</span></td></tr>';
    }
    if (jt) {
      jt.classList.add('jobs-refreshing');
      // Auto-remove the dim after a beat regardless of refresh()'s
      // outcome -- worst case the table stays slightly dim until the
      // next tick, which still gets removed once new rows render.
      setTimeout(() => jt.classList.remove('jobs-refreshing'), 1200);
    }
    // Trigger immediate re-render via the next refresh tick. refresh()
    // is the routine that rebuilds the table; we don't have a
    // standalone renderJobs() here, so just force a poll.
    if (typeof refresh === 'function') refresh();
  });
});
// Restore the persisted .active state on page load (after the tab
// buttons exist in the DOM).
document.querySelectorAll('#jobsStatusTabs [data-jobs-status]').forEach(b => {
  const sel = b.dataset.jobsStatus === _jobsStatusFilter;
  b.classList.toggle('active', sel);
  b.setAttribute('aria-selected', sel ? 'true' : 'false');
});

// Default goal stuffed when LLM mode is picked with an empty Goal field.
// Tuned for a multi-hour to single-day crawl (target 10,000 pages). The
// LLM is told to use pap.walk() explicitly so it doesn't reach for a
// hand-rolled BFS loop -- the latter consistently miscounts (i++ on
// dedup skips) and trips UNDER-TARGET on long runs, while pap.walk
// handles dedup, dead-end filtering, and depth bound internally.
const DEFAULT_CRAWL_GOAL = (
  "このサイトのトップから辿れるページを順にクロールして。\n" +
  "ページ遷移で popup や age-gate が出たら page.agent() で処理して。\n" +
  "各ページで page.capture() を呼んで HTML+画像+outline を保存して。\n" +
  "動画が見つかったページは page.agent() で動画を取得して。\n" +
  "\n" +
  "ガードレール:\n" +
  "  - **pap.walk() を必ず使うこと** (自前 BFS ループは禁止)\n" +
  "  - 同じ URL は 2 回開かない (pap.walk が内部で dedup する)\n" +
  "  - 最大 10000 ページで停止 (target_pages=10000)\n" +
  "  - page.agent() の max_steps は 3\n" +
  "  - 進捗は print() で stdout に出力 ('[N/10000] visited https://...')\n"
);

// Read the currently-selected AI engine ("codegen" or "simple").
// Defaults to "codegen" if no radio is checked.
function currentAiEngine() {
  const checked = document.querySelector('input[name="aiEngine"]:checked');
  return (checked && checked.value) || 'codegen';
}

// Toggle the AI / Code options panels + .selected class on mode cards
// based on the currently-picked radio. Also flips between the two
// sub-areas (codegen Goal textarea vs simple macro builder) when
// the engine radio changes inside the AI panel.
function syncSubmitMode() {
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
  const engine = currentAiEngine();
  const fetchOpts = document.getElementById('fetchOptions');
  if (fetchOpts) fetchOpts.style.display = (mode === 'fetch') ? 'block' : 'none';
  document.getElementById('aiOptions').style.display   = (mode === 'ai')   ? 'block' : 'none';
  document.getElementById('codeOptions').style.display = (mode === 'code') ? 'block' : 'none';
  // Phase 2a: when Fetch becomes visible, re-sync the sub-mode area
  // (handles initial paint + mode-flip back to Fetch).
  if (mode === 'fetch' && typeof syncFetchSubMode === 'function') {
    syncFetchSubMode();
  }
  // 3-card model: the "Script" virtual card is selected when EITHER
  // mode=code (Script>Code sub-tab) OR mode=ai+engine=simple
  // (Script>Macro sub-tab). The Script sub-tab strip is shown only
  // while Script is active so operators see Code/Macro as siblings
  // of one mode rather than top-level cards.
  const isScriptActive = (mode === 'code') || (mode === 'ai' && engine === 'simple');
  const subTabs = document.getElementById('scriptSubTabs');
  if (subTabs) subTabs.style.display = isScriptActive ? '' : 'none';
  if (isScriptActive) {
    const activeKind = (mode === 'code') ? 'code' : 'macro';
    document.querySelectorAll('#scriptSubTabs .script-tab').forEach(t => {
      t.classList.toggle('selected', t.dataset.scriptKind === activeKind);
    });
  }
  document.querySelectorAll('.mode-card').forEach(card => {
    const cMode = card.dataset.mode;
    let sel = false;
    if (cMode === 'fetch') {
      sel = (mode === 'fetch');
    } else if (cMode === 'script') {
      sel = isScriptActive;
    } else if (cMode === 'ai') {
      // The AI card now exclusively means codegen-loop (the LLM
      // crawler). mode=ai+engine=simple is Script>Macro, not AI.
      sel = (mode === 'ai' && engine === 'codegen');
    }
    card.classList.toggle('selected', sel);
  });
  // Mirror selected card's title to the "選択中: …" header so the
  // operator has an unmissable confirmation of the current state.
  const curLabel = document.getElementById('modeCardsCurrentLabel');
  if (curLabel) {
    const selCard = document.querySelector('.mode-card.selected .mode-title');
    if (selCard) curLabel.textContent = selCard.textContent;
  }

  if (mode === 'ai') {
    const goalArea  = document.getElementById('aiGoalArea');
    const macroArea = document.getElementById('aiMacroArea');
    if (engine === 'simple') {
      goalArea.style.display = 'none';
      macroArea.style.display = 'block';
      // Render rows if not yet rendered.
      if (typeof renderSimpleRows === 'function') renderSimpleRows();
    } else {
      goalArea.style.display = 'block';
      macroArea.style.display = 'none';
    }
  }

  // URL becomes a hint-only field for Code mode (the script chooses its
  // own initial_url); ditto for AI since it gets the URL injected into
  // the goal. Don't actually disable -- still useful as metadata --
  // just relax the required attribute so the user can submit without it.
  const urlInput = document.getElementById('urlInput');
  if (mode === 'fetch') {
    urlInput.required = true;
    urlInput.placeholder = 'https://example.com';
  } else {
    urlInput.required = false;
    urlInput.placeholder = (mode === 'code')
      ? '(任意, 表示用にしか使われない)'
      : 'https://example.com';
  }
}

// 3-card model (Fetch / Script / AI). The Script card is a virtual
// mode -- it sets the real {mode, aiEngine} dispatch based on which
// sub-tab is active (Code -> mode=code, Macro -> mode=ai+simple).
// Hidden radios stay in sync so presetBuildPayload / submit code is
// unchanged.
function currentScriptKind() {
  const sel = document.querySelector('#scriptSubTabs .script-tab.selected');
  return (sel && sel.dataset.scriptKind) || 'code';
}

function _selectModeCard(card) {
  if (!card) return;
  const m = card.dataset.mode || 'fetch';
  if (m === 'script') {
    // Script card: route to the active sub-tab (Code or Macro).
    let kind = currentScriptKind();
    try {
      kind = localStorage.getItem('paprika.submit.scriptKind') || kind;
    } catch (_) {}
    if (kind === 'macro') {
      const r = document.querySelector('input[name="mode"][value="ai"]');
      if (r) r.checked = true;
      const e = document.querySelector('input[name="aiEngine"][value="simple"]');
      if (e) e.checked = true;
      try { localStorage.setItem('paprika.submit.aiEngine', 'simple'); } catch (_) {}
    } else {
      const r = document.querySelector('input[name="mode"][value="code"]');
      if (r) r.checked = true;
    }
  } else {
    const modeRadio = document.querySelector(`input[name="mode"][value="${m}"]`);
    if (modeRadio) modeRadio.checked = true;
    if (m === 'ai') {
      // The AI card is unambiguous now: it always means codegen-loop
      // (LLM writes a script). Macro lives under Script.
      const ce = card.dataset.aiEngine || 'codegen';
      const er = document.querySelector(`input[name="aiEngine"][value="${ce}"]`);
      if (er) {
        er.checked = true;
        try { localStorage.setItem('paprika.submit.aiEngine', ce); } catch (_) {}
      }
    }
  }
  syncSubmitMode();
}

// Script sub-tab click: switch between Code and Macro under the
// Script card, persist the choice, and re-dispatch via the Script
// card so all the panel toggling re-fires.
function _selectScriptKind(kind) {
  document.querySelectorAll('#scriptSubTabs .script-tab').forEach(t => {
    t.classList.toggle('selected', t.dataset.scriptKind === kind);
  });
  try { localStorage.setItem('paprika.submit.scriptKind', kind); } catch (_) {}
  const scriptCard = document.querySelector('.mode-card[data-mode="script"]');
  if (scriptCard) _selectModeCard(scriptCard);
}
document.querySelectorAll('#scriptSubTabs .script-tab').forEach(btn => {
  btn.addEventListener('click', () => _selectScriptKind(btn.dataset.scriptKind));
});
document.querySelectorAll('.mode-card').forEach(card => {
  card.addEventListener('click', (e) => {
    // Don't double-fire when the click lands on a hidden radio inside
    // the card (the historical Fetch/Code cards still wrap one).
    if (e.target && e.target.tagName === 'INPUT') return;
    _selectModeCard(card);
  });
});
for (const r of document.querySelectorAll('input[name="mode"]')) {
  r.addEventListener('change', syncSubmitMode);
}
// Phase 2a: fetchSubMode radio change wakes up the inline-goal toggle.
// syncFetchSubMode is defined in admin-joblist.js, which loads AFTER this
// file -- so naming it bare here at wiring time throws a ReferenceError
// that aborts the REST of this script (everything below this loop) and
// leaves the 取得方法 radios with no change handler. Reference it lazily
// from inside the handler instead: by the time a change actually fires,
// joblist.js has run and defined it.
for (const r of document.querySelectorAll('input[name="fetchSubMode"]')) {
  r.addEventListener('change', () => {
    if (typeof syncFetchSubMode === 'function') syncFetchSubMode();
  });
}

// X: 解析の目標プリセットボタン。クリックで textarea を GOAL_PRESETS の
// 文言で上書き。operator 編集中の内容は失われるが、それ以前にプリセット
// 文言から書き換えていないかは判別不能なので確認は省略 (誤クリックは
// Ctrl+Z で復活できる)。
for (const btn of document.querySelectorAll('.goal-preset[data-goal-preset]')) {
  btn.addEventListener('click', () => {
    const key = btn.dataset.goalPreset;
    const text = GOAL_PRESETS[key];
    if (!text) return;
    const ta = document.getElementById('fetchInvestigateGoal');
    if (ta) {
      ta.value = text;
      ta.focus();
      // カーソルを末尾に置いて operator が追記しやすくする。
      ta.selectionStart = ta.selectionEnd = ta.value.length;
    }
  });
}
// 動画をダウンロード <-> アセット保存 の相互制約 guard
{
  const _dv = document.getElementById('fetchDownloadVideo');
  if (_dv) _dv.addEventListener('change', syncFetchDlGuard);
}
// 初期描画時にも guard を効かせる (preset 復元など)
if (typeof syncFetchDlGuard === 'function') {
  try { syncFetchDlGuard(); } catch (_) {}
}
// Phase 2c: "Save as HostRecipe" modal logic. Opened from a button
// on the job detail panel for any completed codegen-loop / rerun job
// that captured actions.
window.openRecipeSaveModal = async function(jid) {
  if (!jid) return;
  const dlg = document.getElementById('recipeSaveModal');
  if (!dlg) {
    alert('recipe save modal element is missing');
    return;
  }
  const err = document.getElementById('recipeSaveError');
  if (err) { err.style.display = 'none'; err.textContent = ''; }
  // Fetch suggestion + prefill.
  let s;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(jid) + '/recipe_suggestion');
    if (!r.ok) {
      alert('recipe_suggestion fetch failed: HTTP ' + r.status);
      return;
    }
    s = await r.json();
  } catch (e) {
    alert('recipe_suggestion fetch crashed: ' + e);
    return;
  }
  if (!s.actions || s.actions.length === 0) {
    if (!confirm('この job は action trace を含みません。actions なしの recipe を保存しますか? (description / code のみ)')) {
      return;
    }
  }
  document.getElementById('recipeSaveHost').value = s.host || '';
  document.getElementById('recipeSavePattern').value = s.pattern || '*';
  document.getElementById('recipeSaveDescription').value = s.description || '';
  document.getElementById('recipeSaveActionCount').textContent = String((s.actions || []).length);
  document.getElementById('recipeSaveActionsPreview').textContent =
    JSON.stringify(s.actions || [], null, 2);
  document.getElementById('recipeSaveCodePreview').textContent = s.code || '(no script)';
  document.getElementById('recipeSaveGoalPreview').textContent = s.goal || '(no goal)';
  // Stash the full payload on the dialog so submit can read actions /
  // code / goal back without re-fetching.
  dlg.__suggestion = s;
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
};

(function _wireRecipeSaveModal() {
  const dlg = document.getElementById('recipeSaveModal');
  if (!dlg) return; // not on this page
  const form = document.getElementById('recipeSaveForm');
  const cancel = document.getElementById('recipeSaveCancel');
  if (cancel) {
    cancel.addEventListener('click', () => {
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
    });
  }
  if (!form) return;
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const host = document.getElementById('recipeSaveHost').value.trim();
    const pattern = document.getElementById('recipeSavePattern').value.trim() || '*';
    const description = document.getElementById('recipeSaveDescription').value.trim();
    const err = document.getElementById('recipeSaveError');
    err.style.display = 'none';
    if (!host) {
      err.textContent = 'host は必須です';
      err.style.display = 'block';
      return;
    }
    const s = dlg.__suggestion || {};
    const body = {
      pattern,
      description,
      actions: s.actions || [],
      code: s.code || null,
      goal: s.goal || null,
      created_from_job: s.created_from_job || null,
      created_by: s.created_by || 'ai',
    };
    try {
      const r = await fetch(
        '/hosts/' + encodeURIComponent(host) + '/recipes',
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        },
      );
      if (!r.ok) {
        const txt = await r.text();
        err.textContent = 'HTTP ' + r.status + ': ' + txt.slice(0, 400);
        err.style.display = 'block';
        return;
      }
      // Success: close modal + flash a toast.
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
      if (typeof toast === 'function') {
        toast('recipe を ' + host + ' に追加しました', 'ok');
      } else {
        alert('recipe を ' + host + ' に追加しました');
      }
    } catch (e) {
      err.textContent = '送信失敗: ' + e;
      err.style.display = 'block';
    }
  });
})();

// Engine radio: persist + re-sync labels. Use localStorage so the
// operator's last choice survives a page reload.
for (const r of document.querySelectorAll('input[name="aiEngine"]')) {
  r.addEventListener('change', () => {
    try {
      localStorage.setItem('paprika.submit.aiEngine', r.value);
    } catch (_) {}
    syncSubmitMode();
  });
}
// Track when the user types into the count/timeout inputs so we
// don't clobber their value on engine switch. (Otherwise switching
// LLM -> Vision -> LLM would reset to defaults every time.)
for (const id of ['maxAttempts', 'attemptTimeout']) {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', () => { el.dataset.userTouched = '1'; });
}
// Restore last AI engine choice. Legacy "vision" value gets migrated
// to "simple" (renamed in the macro-builder rework).
try {
  let saved = localStorage.getItem('paprika.submit.aiEngine');
  if (saved === 'vision') saved = 'simple';
  if (saved === 'codegen' || saved === 'simple') {
    const r = document.querySelector('input[name="aiEngine"][value="' + saved + '"]');
    if (r) r.checked = true;
  }
} catch (_) {}

// =========================================================================
