// --- inline live panel (log + noVNC) tied to the most-recently-submitted job
// Replaces the old "open /jobs/{id}/log in a new tab" UX. The panel lives
// in #liveJobPanel right under the Submit form.
// Sentinel prefix (must match server.protocol.JOB_PROGRESS_MARKER) for
// ephemeral per-download progress lines that drive the progress widget
// instead of being appended to the scroll log.
const LJP_PROGRESS_MARKER = '[[paprika:progress]] ';
// Sentinel prefix (must match server.protocol.NET_CAPTURE_MARKER) for
// ephemeral network-capture deltas that ride the log channel and feed the
// Network tab in real time -- replaces the page.network() pull that 504s.
const LJP_NETCAP_MARKER = '[[paprika:netcap]] ';
// Ephemeral "a thing landed, refresh it" signals (must match
// server.protocol). Event-driven refresh replaces the periodic gallery /
// links polling.
const LJP_ASSET_MARKER = '[[paprika:asset]] ';
const LJP_LINKS_MARKER = '[[paprika:links]] ';
const LJP = {
  jobId: null,
  ws: null,
  wsBackoff: 1000,
  seenLines: 0,
  // The most recent "[paprika] page.X(...)" call row that is still
  // awaiting its "  -> OK/ERR (Nms)" result line. When the result
  // arrives we append it to this row instead of starting a new line,
  // collapsing the 2-line call/result pair into one. Null when the
  // last row wasn't a pending call.
  _pendingCallEl: null,
  finished: false,
  pollTimer: null,
  statusTimer: null,
  codeTimer: null,
  // Map session_id -> the iframe wrapper element we mounted, so we can
  // diff against /jobs/{id}/sessions and only add/remove what changed.
  vncIframes: new Map(),
  // Map download-key -> {row, fill, stats, doneTimer} for the live
  // per-download progress bars (driven by [[paprika:progress]] markers).
  progress: new Map(),
  // state-model v1: true while any session for the attached job is in
  // "closing" (teardown). Drives the "closing" outer status label.
  _anyClosing: false,
  // -1 = not yet polled; only re-render the thumbnail strip when the
  // count actually changes (avoids flicker on each 2.5s poll).
  galleryLastCount: -1,
  // Content signature (joined asset names) for change-detection.
  // Pure count-based dedup misses the case where an upload races a
  // pre-existing eviction (e.g. a video lands while an old asset
  // gets cleaned up): count stays the same but the visible set
  // shifts. Hashing the names catches that too.
  gallerySignature: "",
  // After the job hits a terminal status, we do one final gallery sweep
  // and then stop polling -- assets stop arriving anyway.
  galleryStopped: false,
  // Sticky flag set once ljpRefreshStatus has observed a terminal
  // status AND the job is NOT a keep_session crawl. After this is true
  // the periodic status / sessions / code timers are torn down --
  // the underlying job state can't change anymore so further polls
  // are pure noise (and noticeable hub load when many tabs are open).
  // Reset to false on every fresh ljpAttach.
  _terminalStopped: false,
  // JobOptions.mode of the attached job; needed so ljpSetStatus knows
  // whether ▶ resume should be enabled (only codegen-loop / rerun
  // have a saved script to re-rerun). Stashed by ljpRefreshStatus.
  mode: null,
};

// Detect class from line content so the log pane can colour stdout
// (green-ish), stderr (red-ish), and meta lines (blue, italic) without
// the caller having to know. ljpAppendLine still accepts an explicit
// override.
function ljpClassifyLine(text) {
  if (typeof text !== 'string') return null;
  // Orchestrator stamps these prefixes when streaming subprocess output.
  if (text.indexOf('  [stderr]') !== -1) return 'stderr';
  if (text.indexOf('  [stdout]') !== -1) return 'stdout';
  return null;
}
function ljpAppendLine(text, cls) {
  // --- ephemeral per-download progress marker -----------------------
  // These ride the log channel but are NOT log text: route them to the
  // progress widget and return WITHOUT touching the scroll log or the
  // seenLines cursor (the hub never persisted them, so they must not
  // count toward the replay offset).
  if (typeof text === 'string' && text.startsWith(LJP_PROGRESS_MARKER)) {
    try { ljpUpdateProgress(JSON.parse(text.slice(LJP_PROGRESS_MARKER.length))); }
    catch (_) { /* malformed marker -- ignore */ }
    return;
  }
  // --- ephemeral network-capture marker ------------------------------
  // Captured-URL deltas streamed from the fetch engine: feed the Network
  // tab cache + re-render. Like progress markers these are never persisted
  // and must NOT count toward the seenLines replay cursor.
  if (typeof text === 'string' && text.startsWith(LJP_NETCAP_MARKER)) {
    try { ljpNetIngest(JSON.parse(text.slice(LJP_NETCAP_MARKER.length))); }
    catch (_) { /* malformed marker -- ignore */ }
    return;
  }
  // Asset uploaded -> refresh the gallery RIGHT NOW (not on the next
  // status tick) so a final video that lands AFTER the job flipped to
  // 'completed' still reflects without a manual page reload. Also reopen
  // the polling window if it was closed by a prior terminal status tick
  // (galleryStopped) — a late-arriving asset must be visible. The dirty
  // flag is still set so a concurrent refresh-in-progress coalesces this
  // tick into its successor.
  if (typeof text === 'string' && text.startsWith(LJP_ASSET_MARKER)) {
    LJP._galleryDirty = true;
    LJP.galleryStopped = false;
    try { if (typeof ljpRefreshGallery === 'function') ljpRefreshGallery(); }
    catch (_) {}
    return;
  }
  // Page links captured -> refresh the Links tab once (links are a final
  // snapshot, so no periodic polling needed).
  if (typeof text === 'string' && text.startsWith(LJP_LINKS_MARKER)) {
    try { ljpLinksRefresh(); } catch (_) {}
    return;
  }

  const el = document.getElementById('ljpLog');

  // --- collapse the paprika action result onto its call line ---------
  // The client SDK emits two consecutive lines per action:
  //   [paprika] page.goto('...')
  //   [paprika]   -> OK (3012ms)        (or NO_MATCH / ERR: ... )
  // Append the result to the preceding call row so the log reads
  // "page.goto('...')  -> OK (3012ms)" on ONE line. The call row still
  // renders the instant it's emitted (so a long action like
  // download_video is visibly in-flight); the result is merged in when
  // it arrives. The multi-line "step N" agent traces are left as their
  // own rows (they're not a single call's result).
  const resultMatch = (typeof text === 'string')
    ? text.match(/\[paprika\]\s+(->\s.*)$/)
    : null;
  if (resultMatch && LJP._pendingCallEl && el.contains(LJP._pendingCallEl)) {
    LJP._pendingCallEl.textContent += '  ' + resultMatch[1];
    // A result delivered on stderr (NO_MATCH / ERR / exception) means
    // the action failed -- recolour the merged row so it reads red.
    if (ljpClassifyLine(text) === 'stderr') {
      LJP._pendingCallEl.className = 'stderr';
    }
    LJP._pendingCallEl = null;
    LJP.seenLines += 1;   // keep the server cursor in step
    const d = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (d < 40) el.scrollTop = el.scrollHeight;
    return;
  }

  const line = document.createElement('div');
  const c = cls || ljpClassifyLine(text);
  if (c) line.className = c;
  line.textContent = text;
  el.appendChild(line);
  // Auto-scroll to bottom unless the user has scrolled up.
  const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
  if (distFromBottom < 40) el.scrollTop = el.scrollHeight;
  LJP.seenLines += 1;

  // Remember this row IF it's a paprika action CALL awaiting a result
  // -- i.e. "[paprika] page.X(...)" but not a "-> result" row and not
  // an indented "step N" trace row. The next "-> ..." line merges in.
  const isPaprika = (typeof text === 'string') && text.indexOf('[paprika] ') !== -1;
  const isCall = isPaprika
    && !/\[paprika\]\s+->/.test(text)
    && !/\[paprika\]\s+step /.test(text);
  LJP._pendingCallEl = isCall ? line : null;
}
function ljpAppendMeta(text) {
  const el = document.getElementById('ljpLog');
  const line = document.createElement('div');
  line.className = 'meta';
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

// Upsert one per-download progress bar from a parsed [[paprika:progress]]
// marker payload: {key, label, state, pct?, speed?, eta?, size?, time?,
// detail?}.  state = start | downloading | muxing | done.  Rows live in
// #ljpProgress (above the panes) and auto-clear a few seconds after done.
function ljpUpdateProgress(p) {
  if (!p || !p.key) return;
  const cont = document.getElementById('ljpProgress');
  if (!cont) return;
  let row = LJP.progress.get(p.key);
  // A 'done' for a key we never opened a bar for -> nothing to show.
  if (p.state === 'done' && !row) return;

  if (!row) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin:4px 0;';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex; justify-content:space-between; gap:8px; font-size:11px; color:#cdd; font-family:monospace;';
    const lbl = document.createElement('span');
    lbl.style.cssText = 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:58%;';
    const stats = document.createElement('span');
    stats.style.cssText = 'color:#9ab; white-space:nowrap;';
    head.appendChild(lbl); head.appendChild(stats);
    const track = document.createElement('div');
    track.style.cssText = 'height:6px; background:#23233a; border-radius:3px; overflow:hidden; margin-top:2px;';
    const fill = document.createElement('div');
    fill.style.cssText = 'height:100%; width:0%; background:#4ea3ff; transition:width .3s ease;';
    track.appendChild(fill);
    wrap.appendChild(head); wrap.appendChild(track);
    cont.appendChild(wrap);
    row = { wrap, lbl, stats, fill, doneTimer: null };
    LJP.progress.set(p.key, row);
  }

  if (p.label) row.lbl.textContent = p.label;
  if (!row.lbl.textContent) row.lbl.textContent = p.key;
  if (row.doneTimer) { clearTimeout(row.doneTimer); row.doneTimer = null; }

  if (p.state === 'done') {
    row.fill.style.width = '100%';
    row.fill.style.background = '#3ec46d';
    row.stats.textContent = '✓ 完了';
    row.doneTimer = setTimeout(() => ljpRemoveProgress(p.key), 4000);
  } else if (p.state === 'muxing') {
    row.fill.style.width = '100%';
    row.fill.style.background = '#7a6ad8';
    let s = 'muxing';
    if (p.time) s += ' ' + p.time;
    if (p.speed) s += ' · ' + p.speed;
    if (p.size) s += ' · ' + p.size;
    row.stats.textContent = s;
  } else { // start | downloading
    if (typeof p.pct === 'number') {
      row.fill.style.width = Math.max(0, Math.min(100, p.pct)) + '%';
    }
    row.fill.style.background = '#4ea3ff';
    let s = '';
    if (typeof p.pct === 'number') s += p.pct + '%';
    if (p.detail) s += (s ? ' · ' : '') + p.detail;
    if (p.speed) s += (s ? ' · ' : '') + p.speed;
    if (p.eta) s += (s ? ' · ' : '') + 'ETA ' + p.eta;
    row.stats.textContent = s || '開始…';
  }

  cont.style.display = LJP.progress.size ? '' : 'none';
}

function ljpRemoveProgress(key) {
  const row = LJP.progress.get(key);
  if (!row) return;
  if (row.doneTimer) clearTimeout(row.doneTimer);
  if (row.wrap && row.wrap.parentNode) row.wrap.parentNode.removeChild(row.wrap);
  LJP.progress.delete(key);
  const cont = document.getElementById('ljpProgress');
  if (cont) cont.style.display = LJP.progress.size ? '' : 'none';
}

function ljpSetStatus(s, phase) {
  const el = document.getElementById('ljpStatus');
  // Surface running-phase detail as a distinct label so the operator can
  // tell WHAT a "running" job is actually doing:
  //   * "downloading" -- fetch finished, a detached yt-dlp download is
  //     still uploading the video.
  //   * "keepalive"   -- keep_session job: capture done, the browser is
  //     held open for the operator to drive (idle, NOT working).
  // Both keep the running palette; only the label changes.
  // state-model v1: derive the 2-level label "outer · inner" from
  // (status, phase).  Outer = queued / active / closing / closed.
  //   active  : status=running (inner: downloading / keepalive; else none)
  //   closing : teardown in progress (phase=="closing" or owning session
  //             is closing -- LJP._anyClosing, set by ljpRefreshSessions)
  //   closed  : terminal (inner: completed / failed / cancelled / timed_out)
  // ``running`` (plain), ``codegen-loop:start`` etc. all read as "active".
  let _outer = s || '…', _inner = '', cls = 'status-queued';
  if (s === 'queued') {
    _outer = 'queued'; cls = 'status-queued';
  } else if (s === 'running') {
    if (phase === 'closing' || LJP._anyClosing) {
      _outer = 'closing'; cls = 'status-running';
    } else {
      _outer = 'active'; cls = 'status-running';
      if (phase === 'downloading' || phase === 'keepalive') _inner = phase;
    }
  } else if (s === 'completed' || s === 'succeeded') {
    _outer = 'closed'; _inner = 'completed'; cls = 'status-completed';
  } else if (s === 'cancelled') {
    _outer = 'closed'; _inner = 'cancelled'; cls = 'status-cancelled';
  } else if (s === 'failed') {
    _outer = 'closed';
    _inner = (phase === 'timed_out') ? 'timed_out' : 'failed';
    cls = 'status-failed';
  }
  el.textContent = _inner ? (_outer + ' · ' + _inner) : _outer;
  el.classList.remove(
    'status-queued', 'status-running', 'status-completed',
    'status-failed', 'status-cancelled',
  );
  el.classList.add(cls);
  // Toggle the .running class on the header so the live-dot pulses
  // when (and only when) the job is in flight.
  const hdr = document.getElementById('ljpHeader');
  if (hdr) hdr.classList.toggle('running', s === 'running' || s === 'queued');
  // Pause / resume button enablement.
  //   pause  : while running/queued
  //   resume : after a terminal state, AND only for modes that have a
  //            saved script (codegen-loop / rerun). fetch jobs can't
  //            be resumed because there's no script to re-run.
  const cancellable = (s === 'running' || s === 'queued');
  const terminal    = (s === 'completed' || s === 'succeeded' || s === 'failed' || s === 'cancelled');
  const resumable   = terminal && (LJP.mode === 'codegen-loop' || LJP.mode === 'rerun');
  const stopBtn = document.getElementById('ljpStop');
  if (stopBtn) {
    stopBtn.disabled = !cancellable;
    stopBtn.style.opacity = cancellable ? '1' : '0.45';
    stopBtn.style.cursor = cancellable ? 'pointer' : 'not-allowed';
  }
  const resumeBtn = document.getElementById('ljpResume');
  if (resumeBtn) {
    resumeBtn.disabled = !resumable;
    resumeBtn.style.opacity = resumable ? '1' : '0.45';
    resumeBtn.style.cursor = resumable ? 'pointer' : 'not-allowed';
  }
  // Save-as-recipe: only show in AI-investigation mode (codegen-loop).
  // Other modes can still use Jobs-tab → "recipe として保存" if needed.
  const recipeBtn = document.getElementById('ljpSaveRecipe');
  if (recipeBtn) {
    recipeBtn.style.display = (LJP.mode === 'codegen-loop') ? '' : 'none';
  }
}

// Start a new rerun-mode job from the last attempt of this job. State
// (pap.walk() visited / queue / etc.) is copied server-side so the new
// run resumes from where this one stopped. After submitting, the Live
// panel auto-attaches to the new job_id.
async function ljpResumeJob() {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpResume');
  if (!btn || btn.disabled) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> resuming…';
  try {
    // Find the latest attempt number to point rerun_from at.
    const attemptsResp = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/attempts');
    if (!attemptsResp.ok) {
      alert('cannot list attempts for resume (HTTP ' + attemptsResp.status + ')');
      return;
    }
    const att = await attemptsResp.json();
    if (!att.attempts || !att.attempts.length) {
      alert('no attempts found on this job; nothing to resume from.');
      return;
    }
    const lastN = att.attempts[att.attempts.length - 1].n;

    // Pull URL + attempt_timeout_s from the previous job so the new
    // one keeps the same shape.
    const infoResp = await fetch('/jobs/' + encodeURIComponent(LJP.jobId));
    const info = infoResp.ok ? await infoResp.json() : {};
    const prevOpts = (info.options || {});
    const prevUrl = info.url || 'about:blank';
    const prevTimeout = prevOpts.attempt_timeout_s || 180;

    const body = {
      url: prevUrl,
      options: {
        mode: 'rerun',
        rerun_from: `${LJP.jobId}/attempts/${lastN}`,
        attempt_timeout_s: prevTimeout,
      },
    };
    const r = await fetch('/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      const detail = err && (Array.isArray(err.detail) ? err.detail.map(d => d.msg).join('\n') : err.detail);
      alert('resume failed (' + r.status + '): ' + (detail || r.statusText));
      return;
    }
    const created = await r.json().catch(() => null);
    if (created && created.job_id) ljpAttach(created.job_id);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Stop the in-flight job. Confirms before firing so an accidental click
// doesn't nuke a long-running crawl. The hub-side cancel marks the job
// "cancelled", kills the runner subprocess, and force-ends any held
// sessions.
async function ljpStopJob() {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpStop');
  if (!btn || btn.disabled) return;
  if (!confirm(
    `Cancel job ${LJP.jobId}?\n\n`
    + `This stops the running sandbox immediately and closes any open `
    + `sessions. In-flight work is lost; assets already captured are kept.`
  )) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> stopping…';
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/cancel', {
      method: 'POST',
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('cancel failed (' + r.status + '): ' + (err && err.detail || r.statusText));
      return;
    }
    const result = await r.json();
    if (!result.cancelled) {
      alert('Job was already ' + (result.reason || 'finished') + '; nothing to cancel.');
    }
  } finally {
    btn.innerHTML = orig;
    // Status will refresh on the next /jobs/{id} poll (which fires the
    // done event over WS shortly after), flipping the badge + disabling
    // the button automatically. No manual state reset needed here.
  }
}

function ljpCloseWs() {
  if (LJP.ws) {
    try { LJP.ws.close(); } catch (_) {}
    LJP.ws = null;
  }
}
function ljpStopTimers() {
  if (LJP.pollTimer) { clearInterval(LJP.pollTimer); LJP.pollTimer = null; }
  if (LJP.statusTimer) { clearInterval(LJP.statusTimer); LJP.statusTimer = null; }
  if (LJP.codeTimer) { clearInterval(LJP.codeTimer); LJP.codeTimer = null; }
}

function ljpOpenWs() {
  if (!LJP.jobId || LJP.finished) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/jobs/${encodeURIComponent(LJP.jobId)}/events?since=${LJP.seenLines}`;
  const ws = new WebSocket(url);
  LJP.ws = ws;
  ws.onopen = () => {
    LJP.wsBackoff = 1000;
    ljpAppendMeta(LJP.seenLines === 0 ? '— connected' : `— reconnected (line ${LJP.seenLines})`);
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { ljpAppendLine(e.data); return; }
    if (ev.type === 'log') {
      ljpAppendLine(ev.data && ev.data.line ? ev.data.line : '');
    } else if (ev.type === 'done') {
      const st = ev.data && ev.data.status;
      ljpSetStatus(st);
      ljpAppendMeta('— job ended: ' + st);
      LJP.finished = true;
      try { ws.close(); } catch (_) {}
      // Final session sweep so the user sees the last state of the
      // session(s) the runner had open at the end.
      ljpRefreshSessions();
    } else if (ev.type === 'error') {
      ljpAppendMeta('error: ' + (ev.data && ev.data.message));
    } else {
      ljpAppendLine(e.data);
    }
  };
  ws.onclose = () => {
    if (LJP.finished || !LJP.jobId) return;
    ljpAppendMeta(`— disconnected; reconnecting in ${(LJP.wsBackoff/1000)|0}s`);
    setTimeout(ljpOpenWs, LJP.wsBackoff);
    LJP.wsBackoff = Math.min(LJP.wsBackoff * 2, 15000);
  };
}

function ljpAutoconnect(url) {
  if (!url) return url;
  if (url.indexOf('autoconnect=') !== -1) return url;
  return url + (url.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
}

async function ljpRefreshStatus() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId));
    if (!r.ok) return;
    const info = await r.json();
    // Stash mode so ljpSetStatus can decide whether ▶ resume should be
    // enabled (codegen-loop / rerun have script; fetch doesn't).
    LJP.mode = (info.options || {}).mode || null;
    ljpSetStatus(info.status, info.progress && info.progress.phase);
    // Render the read-only "run config" mirror once (the JobInfo's
    // url + options don't change after submission). Gated by a per-
    // attach flag so subsequent /jobs/{id} polls don't rebuild this
    // tab's DOM 30 times.
    if (!LJP._runConfigRendered) {
      try { ljpRenderRunConfig(info); LJP._runConfigRendered = true; }
      catch (e) { /* leave the placeholder; don't break the rest of the poll */ }
    }
    // Also populate the LIVE Submit form on the "ジョブの実行" sub-tab
    // with this job's options, so the operator can review (or tweak +
    // re-submit) the exact settings that produced the job they're
    // viewing. Only fires when the URL field is empty (= fresh load
    // via #live/<id> hash), to avoid clobbering a draft the operator
    // was composing.
    if (!LJP._submitFormPopulated) {
      try {
        const urlEl = document.getElementById('urlInput');
        const cur = urlEl ? (urlEl.value || '').trim() : '';
        // Mirror THIS job's options into the "ジョブの実行" form. Overwrite
        // when the field is empty OR still holds the URL we populated from
        // the PREVIOUSLY-viewed job (a leftover, not a hand-typed draft).
        // The old check skipped whenever the URL was non-empty, so after
        // viewing one job the leftover URL kept the form frozen on the
        // previous job's settings -- the "実行 tab doesn't reflect this
        // job" bug. A genuine draft (a URL the operator typed that differs
        // from the last populated one) is still preserved.
        if (!cur || cur === (LJP._submitFormUrl || '')) {
          ljpPopulateSubmitForm(info);
          LJP._submitFormUrl = (info.url || '').trim();
        }
        LJP._submitFormPopulated = true;
      } catch (e) { /* don't break the rest of the poll */ }
    }
    // Asset counter -- visible as soon as the first asset lands.
    const saved = (info.progress && info.progress.assets_saved) || 0;
    const failed = (info.progress && info.progress.assets_failed) || 0;
    const pill = document.getElementById('ljpAssetCount');
    if (saved > 0 || failed > 0) {
      pill.style.display = '';
      pill.textContent = `${saved} assets` + (failed ? ` (${failed} failed)` : '');
    } else {
      pill.style.display = 'none';
    }
    // Persisted video-DL progress: drives a bar that SURVIVES reopen
    // (the ephemeral [[paprika:progress]] marker only replays via the
    // log stream, so a fresh page load wouldn't otherwise see history).
    // Hub persists download_pct/eta/speed on every yt-dlp tick (throttled
    // 5s). Renders under the same #ljpProgress strip as the live bar.
    try {
      const pct = info.progress && info.progress.download_pct;
      if (typeof pct === 'number') {
        const persistedKey = '__persisted_dl__';
        ljpUpdateProgress({
          key: persistedKey,
          label: 'video download',
          state: pct >= 100 ? 'done' : 'downloading',
          pct: pct,
          eta: info.progress.download_eta || undefined,
          speed: info.progress.download_speed || undefined,
        });
      }
    } catch (_) { /* never let a render bug break the status poll */ }
    // Fetch-mode jobs carry their noVNC URL directly on JobInfo.
    // noVNC is LIVE only when the hub rewrote novnc_url to its
    // session-rooted proxy form ("/sessions/{sid}/novnc/..."):
    // _proxy_info does that only while a real session is resolvable.
    // Once the session is gone -- a finished job, OR a keepalive job
    // whose session hit its idle/absolute TTL (the job then cascades to
    // "completed" but keep_session stays true) -- novnc_url falls back
    // to the raw ABSOLUTE worker URL (or null). So: a relative
    // ("/...") novnc_url == session alive; anything else == gone.
    // This is reliable where the old status/keep_session guess wasn't:
    // it kept a dead viewer up for reaped keepalive jobs (e.g. opening
    // #live/<id> for such a job still showed noVNC).
    const _novnc = info.novnc_url || '';
    const _vncLive = _novnc.charAt(0) === '/';
    // Status-aware iframe lifecycle. Removing the iframe based ONLY on
    // novnc_url's shape ran too eagerly: as soon as the SDK called
    // DELETE /sessions/{sid}, the hub did state.sessions.remove() and
    // _find_active_session_id stopped returning the relative proxy URL
    // (because the session is no longer in the registry) -- but the
    // worker's noVNC bridge is still ALIVE for the entire drain window
    // (passive m3u8 / mp4 listener can take 5-20 min to finish a
    // multi-GB iframe video). Removing the iframe at that moment hid
    // the in-progress download from the operator who specifically
    // opened the panel to watch it.
    //
    // New rule: keep the iframe mounted while job.status is queued OR
    // running. Only force-unmount when status hits a terminal state
    // (completed / failed / cancelled / succeeded), at which point the
    // worker really has torn the lane down and the bridge is gone.
    const _statusTerminal = info.status === 'completed' || info.status === 'failed'
      || info.status === 'cancelled' || info.status === 'succeeded';
    // Prefer JobInfo.session_id (set by keep_session fetch / codegen-loop
    // / Code mode at dispatch time) as the iframe key over the synthetic
    // ``__job__`` placeholder. The real session_id key activates the
    // full operator UI: editable URL input, Go button, back/forward,
    // popup-close, AND the URL auto-refresh from /sessions/{sid}/pages.
    // The ``__job__`` placeholder shows only a read-only <code> URL.
    // ljpRefreshSessions also tries to discover sessions via
    // /jobs/{id}/sessions, but that requires SessionInfo.job_id ==
    // job_id which is only set for codegen-loop -- keep_session fetch
    // sessions wouldn't show up there. Using info.session_id covers
    // both paths.
    const _jobSid = info.session_id && _vncLive ? info.session_id : null;
    if (_vncLive && _jobSid && !LJP.vncIframes.has(_jobSid)) {
      // If we already have the placeholder up, ljpMountVncFrame's own
      // dedup logic will swap it out for the session_id-keyed version
      // (same canonical URL match path).
      ljpMountVncFrame(_jobSid, {
        novnc_url: info.novnc_url,
        novnc_url_autoconnect: ljpAutoconnect(info.novnc_url),
        label: _jobSid,
        initial_url: info.url || '',
      });
    } else if (_vncLive && !_jobSid && !LJP.vncIframes.has('__job__')) {
      // No session_id on JobInfo -- fall back to the synthetic key
      // (read-only display; ad-hoc fetch jobs go here).
      ljpMountVncFrame('__job__', {
        novnc_url: info.novnc_url,
        novnc_url_autoconnect: ljpAutoconnect(info.novnc_url),
        label: 'job ' + LJP.jobId.slice(0, 12),
      });
    } else if (_statusTerminal) {
      // Tear down whichever variant got mounted.
      if (LJP.vncIframes.has('__job__')) {
        ljpRemoveVncFrame('__job__', 'セッションは終了しました（noVNC は利用できません）');
      }
      if (_jobSid && LJP.vncIframes.has(_jobSid)) {
        ljpRemoveVncFrame(_jobSid, 'セッションは終了しました（noVNC は利用できません）');
      }
    }
    // Keep the session-keyed iframe's URL input in sync with the actual
    // current page URL (the polling done by ljpRefreshSessions ONLY
    // covers sessions that show up in /jobs/{id}/sessions; for
    // JobInfo.session_id-only paths we drive it from here).
    if (_jobSid && !_statusTerminal && LJP.vncIframes.has(_jobSid)) {
      try { ljpRefreshSessionUrl(_jobSid).catch(() => {}); } catch (_) {}
    }
    // Refresh the thumbnail strip past the queued phase -- but event-driven
    // now: only re-fetch when an asset actually landed (LJP._galleryDirty,
    // set by the [[paprika:asset]] marker) OR on the terminal final pass.
    // This replaces the every-tick /assets.json poll. _galleryDirty is set
    // true on attach for the initial load.
    if (info.status && info.status !== 'queued') {
      if (!LJP.galleryStopped) {
        const _galTerm = info.status === 'completed' || info.status === 'succeeded' || info.status === 'failed';
        if (LJP._galleryDirty || _galTerm) {
          LJP._galleryDirty = false;
          await ljpRefreshGallery();
        }
        if (_galTerm) {
          LJP.galleryStopped = true; // one more pass after terminal status, then stop
        }
      }
    }
    // Tear down the periodic status / sessions / code timers when the
    // job is fully terminal AND not a keep_session crawl. keep_session
    // jobs keep mutating session state (noVNC iframe lifecycle, cookie
    // dumps) until the operator closes the session, so we leave them
    // alone. Plain Fetch / codegen-loop / vision-agent jobs in a
    // terminal state can't change anymore -- continuing to poll wastes
    // ~3 req/2.5s per opened Live panel for nothing.
    const _isTerminal = info.status === 'completed' || info.status === 'succeeded'
      || info.status === 'failed' || info.status === 'cancelled';
    const _keepSession = !!(info.options && info.options.keep_session);
    if (_isTerminal && !_keepSession && !LJP._terminalStopped) {
      // One final sessions + code sweep so a last-second update doesn't
      // get lost, then halt.
      try { await ljpRefreshSessions(); } catch (_) {}
      try { await ljpRefreshCode(); } catch (_) {}
      ljpStopTimers();
      LJP._terminalStopped = true;
    }
  } catch (_) {}
}

// Pull the gallery JSON and render a thumbnail strip inside the panel.
// We re-use the gallery endpoint -- but to keep the inline view light we
// just parse the asset hrefs out of the rendered HTML rather than asking
// the server for a separate JSON shape.
async function ljpRefreshGallery() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/assets.json');
    if (!r.ok) return;
    const data = await r.json();
    const items = data.items || [];
    const grid = document.getElementById('ljpGalleryGrid');
    const cnt = document.getElementById('ljpGalleryCount');
    const empty = document.getElementById('ljpGalleryEmpty');
    cnt.textContent = String(items.length);
    // Mirror the count onto the header pill -- codegen-loop captures
    // don't increment JobProgress.assets_saved (that's a fetch-pipeline
    // counter), so without this fall-back the pill stays hidden even
    // when files actually landed via session uploads.
    const pill = document.getElementById('ljpAssetCount');
    if (items.length > 0) {
      pill.style.display = '';
      // Don't overwrite a richer label that ljpRefreshStatus may have
      // already set from progress.assets_saved.
      if (!pill.textContent.includes('failed')) {
        pill.textContent = `${items.length} assets`;
      }
    }
    // Only re-render if the asset set actually changed, to avoid
    // flicker on poll. Use a content signature (count + sorted name
    // list) so an addition AND a same-count swap both trigger a
    // re-render -- pure count-based dedup missed the case where a
    // 3 GB video upload landed while an old image was evicted: count
    // stayed at N but the user's tile for the video never appeared.
    const signature = items.length + '|' + items.map(a => a.name).join('\x1f');
    if (LJP.gallerySignature === signature) return;
    LJP.gallerySignature = signature;
    LJP.galleryLastCount = items.length;
    grid.innerHTML = '';
    if (items.length === 0) {
      empty.style.display = (LJP.galleryStopped ? '' : 'none');
      return;
    }
    empty.style.display = 'none';
    // Render tiles. Each tile's media area is wrapped in a fixed-height
    // <div> so the slot reserves space even before the <img>/<video>
    // has loaded -- without the wrapper, loading=lazy can collapse the
    // tile to ~25px (just the name/size text), which is what produced
    // the "300 grey bars and no images" screenshot.
    for (const a of items) {
      const tile = document.createElement('a');
      tile.href = a.href;       // fallback for middle-click / ctrl-click -> new tab
      tile.target = '_blank';
      tile.title = `${a.name} — ${a.size_h} — click for details`;
      // Plain click: open the detail modal instead of navigating.
      tile.addEventListener('click', (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return;
        ev.preventDefault();
        ljpOpenAssetModal(a);
      });
      tile.style.cssText = 'display:flex; flex-direction:column; background:#fff; border:1px solid #e5e5e5; border-radius:4px; padding:6px; text-decoration:none; color:inherit; overflow:hidden; box-sizing:border-box; cursor:pointer;';
      const mediaBox = 'flex:1 1 auto; min-height:0; display:flex; align-items:center; justify-content:center; border-radius:3px; overflow:hidden;';
      const captionStyle = 'display:block; font-size:11px; margin-top:5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
      const sizeStyle = 'display:block; font-size:10px; color:#888;';
      if (a.kind === 'image') {
        tile.innerHTML =
          `<div style="${mediaBox} background:#f0eee9;">` +
            `<img loading="lazy" src="${esc(a.href)}" alt="" style="max-width:100%; max-height:100%; object-fit:contain; display:block;">` +
          `</div>` +
          `<span style="${captionStyle}">${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      } else if (a.kind === 'video') {
        tile.innerHTML =
          `<div style="${mediaBox} background:#000;">` +
            `<video preload="none" src="${esc(a.href)}" muted style="max-width:100%; max-height:100%; object-fit:contain; display:block;"></video>` +
          `</div>` +
          `<span style="${captionStyle}">▶ ${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      } else {
        tile.innerHTML =
          `<div style="${mediaBox} background:#fafafa; color:#c0392b; font-family:monospace; font-weight:700; font-size:1.5em;">.${esc(a.ext || 'bin')}</div>` +
          `<span style="${captionStyle}">${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      }
      grid.appendChild(tile);
    }
  } catch (_) {}
}

// --- asset detail modal ---------------------------------------------------
function ljpOpenAssetModal(a) {
  const modal = document.getElementById('ljpAssetModal');
  document.getElementById('ljpAssetModalName').textContent = a.name || '';
  const preview = document.getElementById('ljpAssetModalPreview');
  preview.innerHTML = '';
  const src = a.href;
  // Build preview matching the kind. Set width/height after the
  // resource loads so we can populate the "寸法" row, too.
  const dims = document.getElementById('ljpAssetModalDims');
  dims.innerHTML = '<span style="color:#888;">(loading…)</span>';
  if (a.kind === 'image') {
    const img = document.createElement('img');
    img.src = src;
    img.alt = a.name || '';
    img.style.cssText = 'display:block; max-width:100%; max-height:60vh; object-fit:contain;';
    img.addEventListener('load', () => {
      dims.textContent = `${img.naturalWidth} × ${img.naturalHeight} px`;
    });
    img.addEventListener('error', () => {
      dims.innerHTML = '<span style="color:#c33;">(image failed to load)</span>';
    });
    preview.appendChild(img);
  } else if (a.kind === 'video') {
    const v = document.createElement('video');
    v.src = src;
    v.controls = true;
    v.preload = 'metadata';
    v.style.cssText = 'display:block; max-width:100%; max-height:60vh;';
    v.addEventListener('loadedmetadata', () => {
      const dur = isFinite(v.duration) ? v.duration.toFixed(1) + 's' : '?';
      dims.textContent = `${v.videoWidth || '?'} × ${v.videoHeight || '?'} px · duration ${dur}`;
    });
    preview.appendChild(v);
  } else if (a.kind === 'audio') {
    const audio = document.createElement('audio');
    audio.src = src;
    audio.controls = true;
    audio.preload = 'metadata';
    audio.style.cssText = 'display:block; width:90%;';
    audio.addEventListener('loadedmetadata', () => {
      const dur = isFinite(audio.duration) ? audio.duration.toFixed(1) + 's' : '?';
      dims.textContent = `duration ${dur}`;
    });
    preview.appendChild(audio);
  } else {
    // Other / unknown -- show a stylised extension placeholder.
    const ph = document.createElement('div');
    ph.style.cssText = 'color:#c0392b; font-family:monospace; font-weight:700; font-size:3em;';
    ph.textContent = `.${a.ext || 'bin'}`;
    preview.appendChild(ph);
    dims.innerHTML = '<span style="color:#888;">(n/a)</span>';
  }

  // Metadata rows
  const pageCell = document.getElementById('ljpAssetModalPage');
  if (a.page_url) {
    pageCell.innerHTML = `<a href="${esc(a.page_url)}" target="_blank" style="color:#06a;">${esc(a.page_url)}</a>`;
  } else {
    pageCell.innerHTML = '<span style="color:#888;">(no page URL recorded -- legacy asset or fetch-mode upload)</span>';
  }
  const srcCell = document.getElementById('ljpAssetModalSrc');
  if (a.source_url) {
    srcCell.innerHTML = `<a href="${esc(a.source_url)}" target="_blank" style="color:#06a;">${esc(a.source_url)}</a>`;
  } else {
    srcCell.innerHTML = '<span style="color:#888;">(no source URL recorded)</span>';
  }
  const hub = document.getElementById('ljpAssetModalHubLink');
  hub.href = src;
  hub.textContent = src;
  document.getElementById('ljpAssetModalSize').textContent = a.size_h ? `${a.size_h} (${a.size} bytes)` : `${a.size} bytes`;
  document.getElementById('ljpAssetModalMime').textContent =
    (a.mime ? a.mime : '(unknown)') + ` · .${a.ext || 'bin'}`;

  modal.style.display = 'flex';
}
function ljpCloseAssetModal() {
  const modal = document.getElementById('ljpAssetModal');
  modal.style.display = 'none';
  // Stop any playing media so audio doesn't leak after close.
  document.getElementById('ljpAssetModalPreview').innerHTML = '';
}

async function ljpRefreshSessions() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/sessions');
    if (!r.ok) return;
    const data = await r.json();
    const sessions = data.sessions || [];
    // state-model v1: surface the "closing" outer state. A session in
    // teardown reports state=="closing"; ljpSetStatus reads this flag to
    // show "closing" while a running job's session is being torn down.
    LJP._anyClosing = sessions.some((x) => x && x.state === 'closing');
    const seen = new Set();
    for (const s of sessions) {
      seen.add(s.session_id);
      if (!LJP.vncIframes.has(s.session_id)) {
        ljpMountVncFrame(s.session_id, {
          novnc_url: s.novnc_url,
          novnc_url_autoconnect: s.novnc_url_autoconnect,
          label: s.session_id,
          // Pre-fill the URL <input> with the session's initial URL so
          // the operator immediately sees "where we are" instead of a
          // bare placeholder. Subsequent navigates overwrite this via
          // the keydown handler in ljpMountVncFrame.
          initial_url: s.initial_url || '',
        });
      }
      // Reflect the actual current page URL into the address-bar input.
      // Fire-and-forget per session; throttled implicitly by this
      // function's 3 s setInterval. Skips if the operator is mid-edit.
      ljpRefreshSessionUrl(s.session_id).catch(() => {});
    }
    // Remove iframes for sessions that are no longer alive (closed by
    // worker / TTL). Keep the special '__job__' iframe (fetch mode);
    // its lifecycle is handled in ljpRefreshStatus.
    //
    // Multi-hub guard: /jobs/{id}/sessions is answered by whichever hub
    // nginx routed THIS poll to. A hub that doesn't own the session
    // reports it absent even while it's alive on a peer (the hub-side
    // fix forwards the keep_session-fetch case, but codegen-loop and a
    // momentarily-stale Session Map can still slip through). Don't tear
    // the viewer down on that false negative -- confirm via
    // /sessions/{sid} (which forwards to the owning hub) and only
    // unmount on a definitive 404.
    for (const sid of Array.from(LJP.vncIframes.keys())) {
      if (sid === '__job__') continue;
      if (!seen.has(sid)) {
        ljpConfirmSessionGoneThenRemove(sid);
      }
    }
    ljpUpdateVncCount();
    // Toggle the "↻ refresh" + "↓ video" buttons: shown iff ≥1 live
    // session is bound to this job. Covers both keep_session Fetch
    // jobs (where the session lingers past completion) and
    // codegen-loop / rerun jobs (which have sessions while attempts
    // are running). The buttons stay HIDDEN for plain Fetch jobs
    // without keep_session (no session after WorkerJobComplete ->
    // nothing for refresh / video download to act on).
    const sessionPresent = sessions.length > 0;
    const refreshBtn = document.getElementById('ljpRefresh');
    if (refreshBtn) {
      refreshBtn.style.display = sessionPresent ? '' : 'none';
    }
    const videoBtn = document.getElementById('ljpVideoDl');
    if (videoBtn) {
      videoBtn.style.display = sessionPresent ? '' : 'none';
    }
  } catch (_) {}
}

// Confirm a session is REALLY gone before unmounting its noVNC iframe.
// /jobs/{id}/sessions can come back empty from a non-owning hub under the
// multi-hub nginx round-robin even though the session is alive on a peer;
// /sessions/{sid} forwards to the owning hub, so a 404 there is
// authoritative. Anything else (200 = alive elsewhere, or a transient
// network blip) leaves the viewer mounted for the next poll to re-confirm
// -- so a momentary wrong-hub poll can't flap the live viewer.
async function ljpConfirmSessionGoneThenRemove(sid) {
  if (!sid || sid === '__job__') return;
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid));
    if (r.status === 404) {
      // Re-check it's still mounted -- a concurrent poll may have already
      // removed it -- then tear down for real.
      if (LJP.vncIframes.has(sid)) {
        ljpRemoveVncFrame(sid, 'セッションが終了しました');
        ljpUpdateVncCount();
      }
    }
    // 200 / any non-404 / parse: keep the iframe up; the session is alive
    // on a peer hub or the check was inconclusive.
  } catch (_) {
    // Network error -- inconclusive; leave the viewer mounted.
  }
}

// Pull the session's actual current page URL from the worker (via
// /sessions/{sid}/pages, which returns each tab + its URL + which is
// default) and reflect it into the noVNC header's URL <input>. Called
// once per session per ljpRefreshSessions cycle (= every 3 s).
//
// Two guards keep this from stomping operator input:
//   1. document.activeElement === inputEl -> operator is currently
//      typing; leave their unfinished URL alone.
//   2. inputEl.disabled -> a navigate is in flight (doNavigate sets
//      this); the post-navigate URL will come back on the next tick.
//
// Errors (closed session, worker unreachable, parse failure) are
// swallowed -- this is purely cosmetic feedback and any failure just
// means the URL stays at the last value the operator / initial_url
// pre-fill put there.
async function ljpRefreshSessionUrl(sid) {
  const wrap = LJP.vncIframes.get(sid);
  if (!wrap) return;
  const inputEl = wrap.querySelector('.ljp-vnc-url');
  if (!inputEl) return;
  if (document.activeElement === inputEl) return;
  if (inputEl.disabled) return;
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/pages');
    if (!r.ok) return;
    const d = await r.json();
    const pages = Array.isArray(d.pages) ? d.pages : [];
    if (pages.length === 0) return;
    // Default tab wins; fall back to the first listed tab.
    const def = pages.find(p => p && p.is_default) || pages[0];
    const url = def && def.url ? String(def.url) : '';
    if (!url) return;
    // Re-check focus/disabled after the await -- the operator may
    // have started typing while the fetch was in flight.
    if (document.activeElement === inputEl) return;
    if (inputEl.disabled) return;
    if (inputEl.value !== url) inputEl.value = url;
  } catch (_) {}
}

// Live noVNC zoom (CSS transform:scale on each iframe). The iframe
// renders at a fixed "logical" width × height (large enough to look
// good); zoom only changes the visual scale + the layout space we
// claim. Persisted across reloads as paprika.ljp.vncZoom.
// Base reference size at zoom=1.0 (= 100%). 1280x720 is a sensible
// "Live panel sits comfortably in a 1080p viewport" default. The
// actual Chrome window AND the iframe display size are now BOTH
// computed as round(base * zoom), so 100% means Chrome=1280x720 +
// iframe=1280x720 1:1, 50% means 640x360 both, etc. No CSS scale
// transform -- noVNC renders Chrome pixel-perfect into the iframe.
const LJP_VNC_BASE_W = 1280;
const LJP_VNC_BASE_H = 720;
// Chrome window resolution for the noVNC session. FIXED at 1.0 =
// 1280x720 (decoupled from the zoom dropdown, which now drives the
// in-browser PAGE zoom). The iframe itself fills the pane width and
// noVNC's resize=scale scales the 720p framebuffer to fit -- so the
// viewer always matches the panel width instead of leaving a black
// gap. The "↔ fit" button + per-mount resize use these dims.
function ljpVncZoom() {
  return 1.0;
}
// In-browser PAGE zoom (the dropdown's new job, 案A). Persisted under a
// dedicated key so it can't be confused with the old window-size value.
function ljpPageZoom() {
  try {
    const v = parseFloat(localStorage.getItem('paprika.ljp.pageZoom') || '1.0');
    if (v > 0.1 && v <= 5) return v;
  } catch (_) {}
  return 1.0;
}
// Real session_id for a vncIframes key (skip the synthetic '__job__'
// placeholder, which we can't address by session_id).
function ljpSessionKey(key) {
  return (key && key !== '__job__') ? key : null;
}
// Apply the current page zoom to ONE session via the dedicated /zoom
// API (worker CDP Emulation.setPageScaleFactor). This magnifies the
// actual paint output -- so it ALSO zooms full-viewport (100vw/100vh)
// cross-origin iframe players, which CSS `zoom` cannot. /zoom is
// allowed even on a fetch-owned session and is NOT recorded in the
// operator recipe trace (viewing aid only).
async function ljpApplyPageZoomToSession(sessionId) {
  const sid = ljpSessionKey(sessionId);
  if (!sid) return;
  const z = ljpPageZoom();
  try {
    await fetch('/sessions/' + encodeURIComponent(sid) + '/zoom', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({factor: z}),
    });
  } catch (_) { /* best-effort */ }
}
async function ljpApplyPageZoomAll() {
  if (!LJP || !LJP.vncIframes) return;
  await Promise.all([...LJP.vncIframes.keys()].map(ljpApplyPageZoomToSession));
}
// Forward an operator control action to the recording endpoint so the
// step lands in operator_actions.json (learn-from-operator). `action`
// is a {kind, ...} dict; `label` is the human tag stored in the trace.
async function ljpOpAction(sessionKey, action, label, opts) {
  const sid = ljpSessionKey(sessionKey);
  if (!sid) { alert('この pane はセッションIDが不明なため操作できません'); return null; }
  opts = opts || {};
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/operator_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        action,
        label: label || action.kind,
        screenshot: opts.screenshot !== false,  // default: capture before-shot
      }),
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert('操作失敗 (' + r.status + '): ' + (out.detail || r.statusText));
      return null;
    }
    return out;
  } catch (e) {
    alert('操作失敗: ' + e);
    return null;
  }
}
function ljpVncZoomDims() {
  const z = ljpVncZoom();
  return {
    w: Math.round(LJP_VNC_BASE_W * z),
    h: Math.round(LJP_VNC_BASE_H * z),
    z,
  };
}
// Apply the current zoom to one iframe's wrapper. No CSS scale: the
// iframe takes its actual pixel size, and Chrome's OS window is
// resized in parallel via POST /sessions/{sid}/resize (handled by
// ljpResizeChromeForSession). The net effect is pixel-perfect 1:1
// rendering at every zoom level instead of the previous
// "blurry / aliased noVNC image" produced by CSS transform: scale.
function ljpApplyVncZoomToBox(box) {
  const f = box.querySelector('iframe');
  if (!f) return;
  // Fit the noVNC ENTIRELY inside the pane height so nothing -- incl.
  // the remote page's bottom horizontal scrollbar -- is cut off (the
  // previous width:100% sizing overflowed the ~720px pane vertically
  // and hid the bottom). Size by HEIGHT with a 16:9 box, centered;
  // noVNC (resize=scale) scales the 1280x720 framebuffer to fit. The
  // dropdown drives the in-browser page zoom, not this display size.
  f.style.height = '100%';
  f.style.width = 'auto';
  f.style.aspectRatio = '16 / 9';
  f.style.transform = '';
  f.style.transformOrigin = '';
  const scaleBox = f.parentElement;
  // 684 ≈ grid height (720) minus the wrap header + borders, so the
  // full viewer (and any bottom scrollbar) stays on screen.
  scaleBox.style.height = '684px';
  scaleBox.style.width = '100%';
  scaleBox.style.display = 'flex';
  scaleBox.style.alignItems = 'center';
  scaleBox.style.justifyContent = 'center';
  scaleBox.style.overflow = 'hidden';
  scaleBox.style.background = '#000';
}
function ljpApplyVncZoom() {
  document.querySelectorAll('#ljpVncGrid > div').forEach(ljpApplyVncZoomToBox);
}

// Push the current zoom-derived dimensions to the worker so Chrome's
// OS window matches the iframe pixel-for-pixel. Iterates every
// mounted noVNC iframe; the '__job__' fetch-fallback iframe is
// skipped because we don't know its session_id locally.
async function ljpResizeChromeForSession(sessionId) {
  if (!sessionId || sessionId === '__job__') return;
  const {w, h} = ljpVncZoomDims();
  try {
    await fetch(
      '/sessions/' + encodeURIComponent(sessionId) + '/resize',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({width: w, height: h}),
      },
    );
  } catch (_) { /* best-effort */ }
}
async function ljpResizeAllVncChrome() {
  if (!LJP || !LJP.vncIframes) return;
  await Promise.all(
    [...LJP.vncIframes.keys()].map(ljpResizeChromeForSession),
  );
}

function ljpMountVncFrame(key, s) {
  // Hard guard: a session can appear in /jobs/{id}/sessions before the
  // worker has assigned a lane (novnc_url=null). Mounting an iframe
  // with src="" produces an empty pane AND an "↗ open" link that
  // navigates to "/" (the admin UI) -- exactly the bug the user hit.
  // Skip until the URL is populated; the next poll will retry.
  const src = s.novnc_url_autoconnect || s.novnc_url;
  if (!src) return;

  // URL-based dedup. A single worker lane often appears under TWO
  // identifiers in our polling loops:
  //   * the synthetic '__job__' key (from JobInfo.novnc_url, mounted
  //     by ljpRefreshStatus)
  //   * the real session_id key (from /jobs/{id}/sessions, mounted
  //     by ljpRefreshSessions)
  // For fetch-mode jobs only the first exists; for codegen-loop /
  // rerun jobs BOTH exist and point at the same URL, which produced
  // two identical noVNC panes ("job xxxxxx" + "ses_xxxxxx") stacked
  // on top of each other. Canonicalise to the URL's pathname (no
  // origin, no query, no hash) so a relative src from JobInfo and the
  // absolute iframe.src the browser has already resolved still
  // compare equal. Special case: if a real session arrives AFTER the
  // __job__ placeholder was mounted, swap the placeholder out so the
  // more informative session_id label wins.
  const canonOf = (u) => {
    try { return new URL(u, window.location.origin).pathname; }
    catch (_) { return (u || '').split('?')[0].split('#')[0]; }
  };
  const canon = canonOf(src);
  for (const [otherKey, existing] of LJP.vncIframes.entries()) {
    const f = existing.querySelector('iframe');
    if (!f || !f.src) continue;
    if (canonOf(f.src) !== canon) continue;
    if (key !== '__job__' && otherKey === '__job__') {
      if (existing.parentNode) existing.parentNode.removeChild(existing);
      LJP.vncIframes.delete('__job__');
      break;
    }
    return;  // duplicate -- keep the iframe already on screen
  }

  const grid = document.getElementById('ljpVncGrid');
  // Drop the placeholder on first mount.
  const empty = grid.querySelector('.empty');
  if (empty) empty.remove();
  const wrap = document.createElement('div');
  // No border / border-radius on the wrapper -- the only horizontal rule
  // we want is `.ljp-vnc-head { border-bottom }` as the head/iframe seam.
  // (Previously this wrapper had `border:1px solid #ccc; border-radius:6px`
  // which produced rounded corners + a left/top/right outline around the
  // head bar; operator feedback was that the side+top borders and the
  // rounding looked out of place.) `overflow:hidden` is kept so any iframe
  // scrollbar gutter doesn't poke past the wrapper edges.
  wrap.style.cssText = 'overflow:hidden; background:#000;';
  const head = document.createElement('div');
  // Light Chrome-chrome bar: matches the LJP top-header pill aesthetic
  // (cream/beige .pill + --la-* accent) instead of the previous dark
  // Chrome-tab bar. Sits above the dark noVNC iframe so the contrast
  // reads as "window chrome above viewport". The ``ljp-vnc-head`` class
  // hooks a scoped CSS rule (admin.css) that mirrors the LJP-top pill
  // behaviour -- per-button --la-bg accent applied at rest, gentle
  // lift on hover -- so the global .pill red-fill hover doesn't dominate.
  head.className = 'ljp-vnc-head';
  // Inline styling kept minimal -- visual identity now lives in CSS
  // (.ljp-vnc-head) so this bar matches #liveJobPanel h2 / .ljp-actions-group
  // exactly (same background gradient, same pill rules).
  head.style.cssText = 'display:flex; align-items:center; gap:6px;';
  // Operator control buttons (learn-from-operator Phase 1). Shown only
  // for real sessions (not the synthetic '__job__' fetch placeholder).
  // Each press is forwarded to /operator_action which executes it AND
  // records it to the per-job trace for later recipe distillation.
  const _opSid = ljpSessionKey(key);
  // Per-button accent: blueish for benign nav (戻る/進む/reload), red-ish
  // for destructive (popup close), green for affirmative (URL go).
  // Matches the LJP top-header convention (--la-bg / --la-bd / --la-fg
  // custom props applied by inline style; the .pill class reads them
  // on hover via the LJP override, with the global .pill as fallback
  // for visible default fill).
  // Per-button accent custom properties. Trailing "opacity:1; cursor:pointer;"
  // mirrors the LJP top-header button style block (e.g. #ljpStop) so the
  // markup is byte-for-byte interchangeable.
  const _navAccent   = '--la-bg: #eef0ff; --la-bd: #9bf; --la-fg: #0a4a7e; opacity: 1; cursor: pointer;';
  const _popupAccent = '--la-bg: #fde6e6; --la-bd: #d68080; --la-fg: #8a1d1d; opacity: 1; cursor: pointer;';
  const _shotAccent  = '--la-bg: #fff7e6; --la-bd: #e8c97a; --la-fg: #7a5a14; opacity: 1; cursor: pointer;';
  const _goAccent    = '--la-bg: #e6f6e6; --la-bd: #7fc77f; --la-fg: #1a5a1a; opacity: 1; cursor: pointer;';
  const _rightAccent = '--la-bg: #eef0f6; --la-bd: #bbc; --la-fg: #333; opacity: 1; cursor: pointer;';
  // Left-side nav cluster: 戻る / 進む / reload only. URL entry moved
  // to the dedicated <input> in the centre; popup-close moved to the
  // right next to fit / open since it's used less often than nav.
  // Each button uses the same structure as #ljpStop / #ljpResume in
  // the LJP top header: <button class="pill" data-i18n-title=... title=...
  // style="--la-bg/--la-bd/--la-fg; opacity:1; cursor:pointer;">
  // <iconify-icon>icon</iconify-icon> <span data-i18n=...>label</span>
  // </button>. Icon-only buttons omit the trailing span.
  const navBtns = _opSid ? (
    `<button class="pill ljp-op-back" data-i18n-title="ljp.vnc.back.title" title="戻る (記録)" style="${_navAccent}"><iconify-icon icon="lucide:chevron-left"></iconify-icon> <span data-i18n="ljp.vnc.back">戻る</span></button>` +
    `<button class="pill ljp-op-fwd" data-i18n-title="ljp.vnc.fwd.title" title="進む (記録)" style="${_navAccent}"><iconify-icon icon="lucide:chevron-right"></iconify-icon> <span data-i18n="ljp.vnc.fwd">進む</span></button>` +
    `<button class="pill ljp-vnc-reload" data-i18n-title="ljp.vnc.reload.title" title="このフレームを再読み込み" style="${_navAccent}"><iconify-icon icon="lucide:rotate-cw"></iconify-icon></button>`
  ) : (
    `<button class="pill ljp-vnc-reload" data-i18n-title="ljp.vnc.reload.title" title="このフレームを再読み込み" style="${_navAccent}"><iconify-icon icon="lucide:rotate-cw"></iconify-icon></button>`
  );
  // Centre: URL <input>. Operator types a URL + Enter to navigate via
  // /sessions/{sid}/operator_action {kind:navigate, url:...}. Pre-fills
  // with the session's initial_url so the operator sees where they
  // landed. Read-only synthetic placeholder ('__job__') sessions get
  // their session label instead since there's no real session to
  // navigate.
  // URL input / read-only label both stretch via flex:1; visual styling
  // (height, border, font) comes from .ljp-vnc-head CSS so the input
  // lines up with the .pill buttons.
  const _initialVal = _opSid ? (s.initial_url || s.label || '') : (s.label || '');
  const urlInput = _opSid ?
    `<input class="ljp-vnc-url" type="text" placeholder="https://example.com (Enter または → で移動)" value="${esc(_initialVal)}" style="flex:1; min-width:200px;" autocomplete="off" spellcheck="false">` :
    `<code style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; background:#fff; color:#5a5a68; padding:6px 10px; border:1px solid #d4cfca; border-radius:7px; font-size:.78em; font-family:ui-monospace,Consolas,monospace;" title="${esc(s.label)}">${esc(s.label)}</code>`;
  // Go button (icon-only "→"): same navigation action as pressing Enter
  // in the URL input. Sits immediately to the right of the input so it
  // reads as the input's submit affordance. Only shown for real
  // sessions (no point on the read-only '__job__' fetch placeholder).
  const goBtn = _opSid ?
    `<button class="pill ljp-vnc-go" data-i18n-title="ljp.vnc.go.title" title="URL へ移動" style="${_goAccent}"><iconify-icon icon="lucide:arrow-right"></iconify-icon></button>` :
    '';
  // Right cluster: screenshot, popup-close, zoom, fit, open.
  // Screenshot button keeps icon-only structure (no label span); popup
  // / fit / open follow the LJP top-header button structure with both
  // iconify-icon + <span data-i18n>.
  const shotBtn =
    `<button class="pill ljp-vnc-screenshot" data-i18n-title="ljp.vnc.screenshot.title" title="スクリーンショット撮影" style="${_shotAccent}"><iconify-icon icon="lucide:camera"></iconify-icon></button>`;
  const popupBtn = _opSid ?
    `<button class="pill ljp-op-popups" data-i18n-title="ljp.vnc.popups.title" title="広告などのポップアップ・別タブを閉じる (記録)" style="${_popupAccent}"><iconify-icon icon="lucide:x"></iconify-icon> <span data-i18n="ljp.vnc.popups">popup</span></button>` :
    '';
  // Zoom select: styling comes from .ljp-vnc-head select.ljp-vnc-zoom
  // in CSS (height/border/font aligned with the .pill row).
  const zoomSelect =
    `<select class="ljp-vnc-zoom" title="ページズーム (Ctrl+/Ctrl- 相当)">` +
      `<option value="0.5">50%</option>` +
      `<option value="0.75">75%</option>` +
      `<option value="1.0" selected>100%</option>` +
      `<option value="1.25">125%</option>` +
      `<option value="1.5">150%</option>` +
      `<option value="2.0">200%</option>` +
    `</select>`;
  head.innerHTML =
    navBtns +
    urlInput +
    goBtn +
    shotBtn +
    popupBtn +
    zoomSelect +
    `<button class="pill ljp-vnc-fit" data-i18n-title="ljp.vnc.fit.title" title="Chrome のウィンドウサイズを現在の zoom 設定に再同期する" style="${_rightAccent}"><iconify-icon icon="lucide:maximize"></iconify-icon> <span data-i18n="ljp.vnc.fit">fit</span></button>` +
    `<a class="pill ljp-vnc-open" href="${esc(src)}" target="_blank" data-i18n-title="ljp.vnc.open.title" title="新しいタブで開く" style="${_rightAccent}"><iconify-icon icon="lucide:external-link"></iconify-icon> <span data-i18n="ljp.vnc.open">open</span></a>`;
  // The iframe lives inside a transform-scale-box so the layout
  // reserves the *visually-scaled* size, not the logical size.
  const scaleBox = document.createElement('div');
  scaleBox.style.cssText = 'background:#000; position:relative;';
  const frame = document.createElement('iframe');
  frame.src = src;
  frame.style.cssText = 'display:block; border:0; background:#000;';
  scaleBox.appendChild(frame);
  wrap.appendChild(head);
  wrap.appendChild(scaleBox);
  grid.appendChild(wrap);
  ljpApplyVncZoomToBox(wrap);
  LJP.vncIframes.set(key, wrap);
  ljpUpdateVncCount();
  // Wire the reload button: re-assign frame.src to itself to force a
  // fresh load (a cache-buster query param would also work but noVNC
  // is sensitive to URL changes -- the autoconnect/reconnect query
  // params must stay verbatim).
  const reloadBtn = head.querySelector('.ljp-vnc-reload');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => {
      const cur = frame.src;
      frame.src = 'about:blank';
      // 50ms gap so the browser actually tears down the old viewer
      // before reattaching to the same URL.
      setTimeout(() => { frame.src = cur; }, 50);
    });
  }
  // Wire operator control buttons (recorded via /operator_action).
  if (_opSid) {
    const _flash = (btn, ok) => {
      const t = btn.textContent;
      btn.textContent = ok ? '✓' : '✕';
      setTimeout(() => { btn.textContent = t; }, 1200);
    };
    const backBtn = head.querySelector('.ljp-op-back');
    if (backBtn) backBtn.addEventListener('click', async () => {
      backBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'back'}, '戻る');
      backBtn.disabled = false; _flash(backBtn, !!r);
    });
    const fwdBtn = head.querySelector('.ljp-op-fwd');
    if (fwdBtn) fwdBtn.addEventListener('click', async () => {
      fwdBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'forward'}, '進む');
      fwdBtn.disabled = false; _flash(fwdBtn, !!r);
    });
    const popupsBtn = head.querySelector('.ljp-op-popups');
    if (popupsBtn) popupsBtn.addEventListener('click', async () => {
      popupsBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'close_popups'}, 'ポップアップ閉じる');
      popupsBtn.disabled = false; _flash(popupsBtn, !!r);
    });
    // URL input: Enter (or the adjacent → Go button) to navigate.
    // Replaces the old prompt()-driven .ljp-op-url button (button
    // removed from the header markup, the <input> sits in its place +
    // lets the operator see / edit the current URL inline like a real
    // browser address bar).
    const urlInputEl = head.querySelector('.ljp-vnc-url');
    const goBtnEl    = head.querySelector('.ljp-vnc-go');
    if (urlInputEl) {
      // Shared submit handler -- Enter keypress and Go click both end
      // up here. Disables the input while the navigate is in flight so
      // the operator can see the action is being processed, then
      // flashes ✓/✕ on the input itself for feedback.
      const doNavigate = async () => {
        const url = (urlInputEl.value || '').trim();
        if (!url) return;
        urlInputEl.disabled = true;
        if (goBtnEl) goBtnEl.disabled = true;
        try {
          const r = await ljpOpAction(_opSid, {kind: 'navigate', url: url}, 'URL移動: ' + url);
          _flash(urlInputEl, !!r);
        } finally {
          urlInputEl.disabled = false;
          if (goBtnEl) goBtnEl.disabled = false;
        }
      };
      urlInputEl.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        doNavigate();
      });
      if (goBtnEl) {
        goBtnEl.addEventListener('click', () => { doNavigate(); });
      }
    }
    // Screenshot button: hits POST /jobs/{id}/screenshot which captures
    // the current frame, saves it to data/jobs/{id}/assets/screenshot-*
    // (= filtered into the Screenshot sub-tab via screenshots.json),
    // and refreshes the Screenshot tab viewer so the new shot appears.
    const shotBtnEl = head.querySelector('.ljp-vnc-screenshot');
    if (shotBtnEl) {
      shotBtnEl.addEventListener('click', async () => {
        if (!LJP.jobId) { _flash(shotBtnEl, false); return; }
        shotBtnEl.disabled = true;
        let ok = false;
        try {
          const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/screenshot', { method: 'POST' });
          ok = r.ok;
        } catch (_) { ok = false; }
        shotBtnEl.disabled = false;
        _flash(shotBtnEl, ok);
        // Refresh the Screenshot tab viewer so the new shot shows up
        // without waiting for the next 5s poll.
        if (ok && typeof ljpShotRefreshScreenshots === 'function') {
          try { ljpShotRefreshScreenshots(); } catch (_) {}
        }
      });
    }
    // Apply the current page zoom to this freshly-mounted session
    // (best-effort; runs after noVNC has had a moment to connect).
    setTimeout(() => { ljpApplyPageZoomToSession(_opSid); }, 1500);
  }
  // Wire the "↔ fit" button -- POST /sessions/{sid}/resize with the
  // iframe's logical width/height. Only meaningful when `key` is a
  // real session_id (not the '__job__' placeholder used for fetch).
  const fitBtn = head.querySelector('.ljp-vnc-fit');
  if (fitBtn) {
    if (!key || key === '__job__') {
      // Fetch-mode synthetic iframe: we don't know the session_id
      // here (the URL has it but parsing is fragile). Hide the fit
      // button rather than firing unrouted requests.
      fitBtn.style.display = 'none';
    } else {
      fitBtn.addEventListener('click', async () => {
        const original = fitBtn.textContent;
        fitBtn.disabled = true;
        fitBtn.textContent = '↔ …';
        try {
          const {w, h} = ljpVncZoomDims();
          const r = await fetch(
            '/sessions/' + encodeURIComponent(key) + '/resize',
            {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({width: w, height: h}),
            },
          );
          if (!r.ok) {
            const detail = await r.json().catch(() => ({}));
            alert('resize failed (' + r.status + '): ' +
                  (detail.detail || r.statusText));
          } else {
            fitBtn.textContent = '↔ ✓';
            setTimeout(() => { fitBtn.textContent = original; }, 1500);
          }
        } catch (e) {
          alert('resize failed: ' + e);
        } finally {
          fitBtn.disabled = false;
          if (fitBtn.textContent === '↔ …') fitBtn.textContent = original;
        }
      });
    }
  }
  // Auto-fit on first mount: schedule a resize after noVNC has had
  // a moment to connect (the websocket handshake + initial RFB
  // exchange typically takes ~1 sec; firing CDP setWindowBounds
  // before that just gets queued, but doing it early-ish lets the
  // operator see the resized Chrome from the moment the screen
  // becomes visible). Skip for the '__job__' iframe (fetch fallback)
  // -- no session_id to target.
  if (key && key !== '__job__') {
    setTimeout(() => {
      ljpResizeChromeForSession(key);
    }, 1500);
  }
}

// Remove one noVNC iframe by key. When the grid empties out, restore a
// placeholder so the pane shows a message instead of a blank black box.
function ljpRemoveVncFrame(key, emptyMsg) {
  const wrap = LJP.vncIframes.get(key);
  if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
  LJP.vncIframes.delete(key);
  const grid = document.getElementById('ljpVncGrid');
  if (grid && LJP.vncIframes.size === 0 && !grid.querySelector('.empty')) {
    grid.innerHTML = '<div class="empty" style="padding:20px; text-align:center; '
      + 'color:#888; border:1px dashed #444; border-radius:6px;">'
      + (emptyMsg || 'noVNC will appear once a session opens…')
      + '</div>';
  }
  ljpUpdateVncCount();
}

function ljpUpdateVncCount() {
  const n = LJP.vncIframes.size;
  document.getElementById('ljpVncCount').textContent = String(n);
}

// --- tab switching for the Live panel -------------------------------------
function ljpSetTab(name) {
  const all = ['log', 'vnc', 'screenshot', 'links', 'network', 'code', 'gallery', 'run-config'];
  if (!all.includes(name)) name = 'log';
  document.querySelectorAll('.ljp-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.ljpTab === name);
  });
  document.querySelectorAll('#liveJobPanel .ljp-pane').forEach(p => {
    p.style.display = (p.dataset.ljpPane === name) ? '' : 'none';
  });
  try { localStorage.setItem('paprika.ljp.activeTab', name); } catch (_) {}
  // Activate / deactivate the live-screenshot refresh timer when
  // entering / leaving the screenshot tab. We don't burn CPU
  // re-fetching frames the operator can't see.
  if (typeof ljpShotOnTabChange === 'function') ljpShotOnTabChange(name);
  // Same idea for the Links tab -- only poll /sessions/{sid}/links
  // while the operator is actually looking at the URL list.
  if (typeof ljpLinksOnTabChange === 'function') ljpLinksOnTabChange(name);
  // Network tab -- only poll while visible.
  if (typeof ljpNetOnTabChange === 'function') ljpNetOnTabChange(name);
}


// ===========================================================================
// Page-role pill + 訂正 modal (Live job panel)
// ===========================================================================
const _LJP_ROLE_LABEL = { detail: '詳細', listing: '一覧', category: 'カテゴリ', tag: 'タグ', top: 'トップ', error: 'エラー', unknown: '不明' };
const _LJP_ROLE_CHOICES = ['detail', 'listing', 'category', 'tag', 'top', 'error', 'unknown'];
let _ljpPageRoleState = null;

function _ljpRoleEsc(s) { return (s == null ? '' : ('' + s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

function _ljpRolePillRender(data) {
  const pill = document.getElementById('ljpRolePill');
  const wrap = document.getElementById('ljpRoleWrap');
  if (!pill || !wrap) return;
  if (!data || !data.value) {
    pill.className = 'role-pill role-unknown'; pill.textContent = '—';
    wrap.style.display = 'none'; return;
  }
  const v = data.value;
  const label = _LJP_ROLE_LABEL[v] || v;
  pill.className = 'role-pill role-' + _ljpRoleEsc(v);
  const overridden = !!data.job_override || (!!data.host_override && data.host_override === v);
  pill.innerHTML = (overridden ? '<iconify-icon icon="lucide:pin" style="vertical-align:-2px; font-size:.9em;"></iconify-icon> ' : '') + label;
  const conf = (data.confidence != null) ? Math.round(data.confidence * 100) + '%' : '';
  pill.title = v + (conf ? ' · ' + conf : '') + ' · ' + (data.reason || '') + (overridden ? '\n\n(訂正済み)' : '');
  wrap.style.display = '';
}

async function ljpLoadPageRole(jobId) {
  if (!jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(jobId) + '/page-role');
    if (!r.ok) { _ljpRolePillRender(null); return; }
    _ljpPageRoleState = await r.json();
    _ljpRolePillRender(_ljpPageRoleState);
  } catch (_e) { _ljpRolePillRender(null); }
}

function _ljpOpenPageRoleEdit() {
  const m = document.getElementById('pageRoleEditModal');
  if (!m || !_ljpPageRoleState) return;
  const $ = (id) => document.getElementById(id);
  const s = _ljpPageRoleState;
  $('pageRoleEditUrl').textContent = s.url || '';
  const autoLabel = _LJP_ROLE_LABEL[s.value] || s.value || '—';
  const conf = (s.confidence != null) ? ' (conf ' + Math.round(s.confidence * 100) + '%)' : '';
  $('pageRoleEditAuto').innerHTML = '<span class="role-pill role-' + _ljpRoleEsc(s.value) + '">' + _ljpRoleEsc(autoLabel) + '</span> ' + _ljpRoleEsc(s.reason || '') + _ljpRoleEsc(conf);
  $('pageRoleEditTplPreview').textContent = s.url_template || '(no template)';
  const current = s.job_override || s.host_override || s.value || '';
  const choicesHost = $('pageRoleEditChoices');
  choicesHost.dataset.selected = current;
  choicesHost.innerHTML = _LJP_ROLE_CHOICES.map(v => {
    const isSel = v === current;
    const style = isSel
      ? 'cursor:pointer; padding:5px 12px; background:#196b2c; color:#fff; border-color:#196b2c; font-weight:600;'
      : 'cursor:pointer; padding:5px 12px;';
    return '<button type="button" class="role-pill role-' + v + ' pre-edit-choice" data-val="' + v + '" style="' + style + '">' + _ljpRoleEsc(_LJP_ROLE_LABEL[v]) + '</button>';
  }).join('');
  choicesHost.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => {
      choicesHost.dataset.selected = b.dataset.val;
      choicesHost.querySelectorAll('button').forEach(bb => {
        bb.style.cssText = bb === b
          ? 'cursor:pointer; padding:5px 12px; background:#196b2c; color:#fff; border-color:#196b2c; font-weight:600;'
          : 'cursor:pointer; padding:5px 12px;';
      });
    });
  });
  $('pageRoleEditApplyHost').checked = true;
  $('pageRoleEditMsg').textContent = '';
  m.style.display = 'flex';
}

function _ljpClosePageRoleEdit() {
  const m = document.getElementById('pageRoleEditModal');
  if (m) m.style.display = 'none';
}

async function _ljpSavePageRoleEdit(clear) {
  if (!_ljpPageRoleState || !_ljpPageRoleState.job_id) return;
  const $ = (id) => document.getElementById(id);
  const msg = $('pageRoleEditMsg');
  msg.style.color = '#666'; msg.textContent = '保存中…';
  const value = clear ? '' : (($('pageRoleEditChoices').dataset.selected) || '');
  const applyHost = !clear && $('pageRoleEditApplyHost').checked;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(_ljpPageRoleState.job_id) + '/page-role-override', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value, apply_to_host: applyHost }),
    });
    if (!r.ok) {
      const t = await r.text();
      msg.style.color = '#933';
      msg.textContent = '保存に失敗 (HTTP ' + r.status + '): ' + t.slice(0, 200);
      return;
    }
    msg.style.color = '#196b2c';
    msg.textContent = clear ? '訂正を解除しました' : (applyHost ? '保存しました (同テンプレ全ジョブへ反映)' : '保存しました (このジョブのみ)');
    await ljpLoadPageRole(_ljpPageRoleState.job_id);
    setTimeout(_ljpClosePageRoleEdit, 800);
  } catch (e) {
    msg.style.color = '#933';
    msg.textContent = '通信に失敗: ' + (e && e.message ? e.message : e);
  }
}

(function wirePageRoleEdit() {
  function _wire() {
    const editBtn = document.getElementById('ljpRoleEdit');
    if (editBtn) editBtn.addEventListener('click', _ljpOpenPageRoleEdit);
    const closeBtn = document.getElementById('pageRoleEditClose');
    if (closeBtn) closeBtn.addEventListener('click', _ljpClosePageRoleEdit);
    const m = document.getElementById('pageRoleEditModal');
    if (m) m.addEventListener('click', (e) => { if (e.target === m) _ljpClosePageRoleEdit(); });
    const saveBtn = document.getElementById('pageRoleEditSave');
    if (saveBtn) saveBtn.addEventListener('click', () => _ljpSavePageRoleEdit(false));
    const clearBtn = document.getElementById('pageRoleEditClear');
    if (clearBtn) clearBtn.addEventListener('click', () => _ljpSavePageRoleEdit(true));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
  else _wire();
})();

