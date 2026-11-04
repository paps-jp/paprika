// --- #jobs "ライブ" console -------------------------------------------------
//
// An append-only job feed, in the shape of `tail -f` rather than a table the
// UI keeps re-rendering.
//
// WHY this exists (measured on prod 2026-09-11, 16.4M rows / +88k jobs per
// hour / ~24 jobs a second):
//
//   * `GET /jobs?limit=1` took **3.36s** -- and `limit=500` took 5.79s. The
//     ~3.4s was FIXED cost: the unfiltered `SELECT COUNT(*) FROM jobs` behind
//     the pager's total, walking the whole index every poll. The UI then threw
//     the number away in favour of /jobs/summary's Redis-cached badge counts.
//   * The 2s poll had no in-flight guard, so a 3.5s response piled requests up
//     until the browser's 6-connections-per-host limit stalled EVERYTHING
//     (same endpoint measured at ~61s from inside the page).
//   * Any single row changing status rebuilt the entire tbody -- and every row
//     eagerly rendered a 12-item actions dropdown, so a 1000-row page meant
//     ~20k <iconify-icon> custom elements.
//
// At 24 jobs/s a "sorted, paginated, counted" view of `all` is the wrong
// shape anyway: page 1 is a different set every second and the total is
// meaningless. So this view instead:
//
//   * pulls ONLY what finished since its watermark, via the keyset endpoint
//     `GET /jobs/completes?completed_after=<cursor>&slim=1` -- O(log N + limit)
//     on idx_completed_at_status, no COUNT, no OFFSET. Measured 0.18-0.30s for
//     500 rows regardless of table size (and ~8x smaller with slim=1).
//   * APPENDS those rows and never touches the ones already on screen.
//   * trims from the top past a buffer cap, like a terminal scrollback.
//
// The existing status sub-tabs stay exactly as they were: they are the
// forensic view ("find me job X"), this is the live one.

const JC_BUF_KEY    = 'paprika.jobsConsole.buffer';
const JC_STATUS_KEY = 'paprika.jobsConsole.statuses';
const JC_POLL_MS    = 1000;
// Batch cap. Also the server's hard cap for this endpoint.
const JC_LIMIT      = 500;
// How far back the first poll reaches. Deliberately small: the console is a
// live view, not a backfill -- a fresh open should show "what is happening
// now", and even 8s at 24 jobs/s is ~200 lines to catch up on.
const JC_COLD_START_MS = 8_000;
// Paint cadence for the waterfall. A poll returns a BATCH (a second's worth of
// completions, ~20-25 rows at prod rate); dumping it in one insert makes the
// feed lurch once a second. Instead the batch goes into JC.pending and is
// spread over the interval, one row at a time, so the console reads like a
// terminal scrolling rather than a table refreshing.
const JC_DRAIN_MS = 40;
// Hard ceiling on the waterfall buffer. It exists to SMOOTH about a second of
// arrivals, not to hold a backlog: every row sitting in it is a row the
// operator is seeing late. Past this, the oldest are dropped (they would be
// trimmed off the top of the scrollback shortly anyway) so the feed stays
// pinned to now instead of drifting minutes behind during a catch-up burst.
// Chained catch-up rounds allowed per tick when a batch comes back FULL
// (= more is already waiting). Bounded so a far-behind cursor can't spin.
const JC_MAX_CATCHUP = 3;
const JC_PENDING_CAP = 150;

const JC = {
  cursor: null,        // ISO string; the completed_at watermark
  timer: null,
  inflight: false,
  paused: false,
  follow: true,        // auto-scroll pinned to the bottom
  grep: '',
  seen: new Set(),     // job_ids already rendered (dedupe, see _jcAdvance)
  seenOrder: [],       // insertion order, so the Set can be trimmed too
  statuses: ['completed', 'failed', 'review'],
  max: 1000,
  pending: [],         // fetched but not yet painted (the waterfall buffer)
  drainTimer: null,
  dropped: 0,          // rows trimmed off the top this session
  lastBatch: 0,
  lastMs: 0,
  errors: 0,
};

try {
  const b = parseInt(localStorage.getItem(JC_BUF_KEY) || '', 10);
  if (Number.isFinite(b) && b > 0) JC.max = b;
  const st = JSON.parse(localStorage.getItem(JC_STATUS_KEY) || 'null');
  if (Array.isArray(st) && st.length) JC.statuses = st;
} catch (_) {}

function jcActive() {
  const tab = (location.hash || '').replace(/^#/, '').split('/')[0];
  return (tab === '' || tab === 'jobs') && _jobsStatusFilter === 'live';
}

// ---- rendering -------------------------------------------------------------

const _JC_STATUS_ICON = {
  completed: '✓',
  failed:    '✗',
  review:    '⚑',
  cancelled: '⊘',
};

function _jcDur(row) {
  const st = row.started_at ? parseServerTime(row.started_at) : NaN;
  const en = row.completed_at ? parseServerTime(row.completed_at) : NaN;
  if (!Number.isFinite(st) || !Number.isFinite(en)) return '';
  const s = Math.max(0, (en - st) / 1000);
  if (s < 10)   return s.toFixed(1) + 's';
  if (s < 600)  return Math.round(s) + 's';
  return Math.floor(s / 60) + 'm' + String(Math.round(s % 60)).padStart(2, '0');
}

// Normalise a feed row. `slim=1` gives us exactly the fields below, but an
// older hub (or one mid-rolling-deploy -- nginx round-robins across 20 of
// them, so a single session sees BOTH versions) ignores the flag and returns
// the full JobInfo, where mode lives under options and error is unclipped.
function _jcNorm(row) {
  if (row.mode === undefined && row.options) {
    row.mode = row.options.mode || 'fetch';
  }
  if (row.error && row.error.length > 200) row.error = row.error.slice(0, 200);
  return row;
}

function _jcRowHtml(row) {
  row = _jcNorm(row);
  const jid    = esc(row.job_id || '');
  const status = String(row.status || '');
  const icon   = _JC_STATUS_ICON[status] || '·';
  const lane   = (row.lane_idx !== null && row.lane_idx !== undefined)
    ? '#' + row.lane_idx : '';
  const note   = `<span class="jc-errtext jc-err" title="${esc(row.error || '')}">`
    + `${esc(row.error || '')}</span>`;
  // No per-row menu: that eager dropdown is what made the old table heavy.
  // The id is the affordance -- it opens the same Live panel the table's
  // "watch live" item did.
  return `<tr class="jc-row jc-${esc(status)}">`
    + `<td class="jc-t">${esc(fmtClock(row.completed_at))}</td>`
    + `<td class="jc-s"><span class="badge ${esc(status)}">${icon} `
      + `${esc(_JOB_STATUS_LABEL[status] || status)}</span></td>`
    + `<td class="jc-d">${esc(_jcDur(row))}</td>`
    + `<td class="jc-w"><code>${esc(row.worker_id || '')}</code>`
      + `<small>${esc(lane)}</small></td>`
    + `<td class="jc-m">${esc(row.mode || '')}</td>`
    + `<td class="jc-u" title="${esc(row.url || '')}">`
      + `<a href="${esc(row.url || '')}" target="_blank" rel="noreferrer">`
      + `${esc(row.url || '')}</a></td>`
    + `<td class="jc-n">${note}`
      + `<button class="jc-open" onclick="watchLive('${jid}')" `
      + `title="${esc(jid)}">${esc(jid.substring(0, 8))}</button></td>`
    + `</tr>`;
}

function _jcAtBottom(wrap) {
  return (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight) < 24;
}

function _jcAppend(rows) {
  const tb = document.querySelector('#jobsConsoleTable tbody');
  const wrap = document.getElementById('jobsConsoleWrap');
  if (!tb || !wrap) return;
  // Decide whether to stay pinned by MEASURING, before we mutate anything --
  // not by reading a flag the scroll listener maintains. Scroll events are
  // dispatched at a rendering opportunity, so a batch landing in the same
  // frame as the operator's scroll-up would otherwise see the flag still
  // true and yank them back to the bottom. Measured: that is exactly what
  // happened (scrollTop 200 -> 19191 inside 500ms).
  const pin = _jcAtBottom(wrap);
  const g = JC.grep.toLowerCase();
  const show = g
    ? rows.filter(r => (r.url || '').toLowerCase().includes(g)
                    || (r.worker_id || '').toLowerCase().includes(g)
                    || (r.error || '').toLowerCase().includes(g))
    : rows;
  if (show.length) {
    // ONE insertAdjacentHTML for the whole batch. Appending never reflows or
    // re-parses the rows already on screen -- the property the old
    // rebuild-the-whole-tbody path could not have.
    tb.insertAdjacentHTML('beforeend', show.map(_jcRowHtml).join(''));
  }
  // Trim the scrollback from the top. deleteRow is cheaper than rebuilding,
  // and keeps the DOM bounded no matter how long the tab stays open.
  let over = tb.rows.length - JC.max;
  if (over > 0) {
    JC.dropped += over;
    while (over-- > 0) tb.deleteRow(0);
  }
  if (pin) wrap.scrollTop = wrap.scrollHeight;
  JC.follow = pin;
  _jcRenderFoot();
}

// Waterfall: emit the buffered rows a few at a time so a one-second batch
// lands as a steady stream instead of a lurch. The per-tick count is sized to
// clear whatever is pending before the NEXT poll arrives, so the feed self-
// paces: quiet fleet -> one row every 40ms; catch-up burst -> bigger steps,
// but still visibly scrolling rather than teleporting.
function _jcDrainTick() {
  const now = performance.now();
  // Clamp the step: a BACKGROUNDED tab has its timers throttled to ~1/s, and
  // an un-clamped elapsed-time step would then dump the whole queue in one
  // insert the moment the tab is looked at again -- the exact lurch this
  // buffer exists to remove. 200ms also bounds catch-up after any long frame.
  const dt = Math.min(200, now - (JC._drainAt || now));
  JC._drainAt = now;
  if (JC.paused || !JC.pending.length) return;
  // Size the step by ELAPSED time, not by an assumed tick rate: setInterval
  // under-delivers whenever the append+scroll work overruns 40ms (measured:
  // ~12 effective ticks/s, not 25), and a fixed 1/25th step then leaves the
  // queue permanently behind -- the feed would drift seconds into the past.
  const n = Math.max(1, Math.ceil(JC.pending.length * (dt / JC_POLL_MS)));
  _jcAppend(JC.pending.splice(0, n));
}

// Queue a batch for the waterfall. Anything beyond one buffer's worth is
// dropped from the FRONT: those rows would be trimmed off the top of the
// scrollback the moment they were painted anyway, so holding them only costs
// memory and delays the rows the operator can actually see.
function _jcEnqueue(rows) {
  if (!rows.length) return;
  JC.pending.push(...rows);
  const cap = Math.min(JC_PENDING_CAP, JC.max);
  if (JC.pending.length > cap) {
    JC.dropped += JC.pending.length - cap;
    JC.pending.splice(0, JC.pending.length - cap);
  }
}

// Throttled: the drain tick calls this ~25x/s now, and the footer is a status
// line nobody reads at that rate. `force` is for state flips (pause, clear)
// that must show immediately.
let _jcFootAt = 0;
function _jcRenderFoot(force) {
  const el = document.getElementById('jobsConsoleFoot');
  if (!el) return;
  const now = performance.now();
  if (!force && (now - _jcFootAt) < 200) return;
  _jcFootAt = now;
  const tb = document.querySelector('#jobsConsoleTable tbody');
  const n = tb ? tb.rows.length : 0;
  const t = (k, fb) => (window.i18next && window.i18next.t)
    ? window.i18next.t(k, { defaultValue: fb }) : fb;
  const state = JC.paused
    ? `<span class="jc-paused">■ ${t('jobs.console.stopped', '停止中')}</span>`
    : (JC.follow
        ? `<span class="jc-live">● ${t('jobs.console.following', '追従中')}</span>`
        : `<span class="jc-held">⏸ ${t('jobs.console.held', 'スクロール中')}</span>`);
  el.innerHTML = `${state} · ${n} / ${JC.max} ${t('jobs.console.rows', '行')}`
    + (JC.pending.length ? ` · ${t('jobs.console.buffered', '待機')} ${JC.pending.length}` : '')
    + (JC.dropped ? ` · ${t('jobs.console.trimmed', '切り捨て')} ${JC.dropped}` : '')
    + (JC.lastMs ? ` · ${JC.lastBatch} ${t('jobs.console.newrows', '件')} / ${JC.lastMs}ms` : '')
    + (JC.errors ? ` · <span class="jc-err">err ${JC.errors}</span>` : '');
}

// ---- polling ---------------------------------------------------------------

function _jcIsoBack(ms) {
  return new Date(Date.now() - ms).toISOString();
}

// One fetch round. Returns how many rows the server sent (NOT how many were
// new) -- a full batch means more is already waiting behind the cursor.
async function _jcFetchOnce() {
  const t0 = performance.now();
  try {
    if (!JC.cursor) JC.cursor = _jcIsoBack(JC_COLD_START_MS);
    const q = '/jobs/completes'
      + '?completed_after=' + encodeURIComponent(JC.cursor)
      + '&limit=' + JC_LIMIT
      + '&slim=1'
      + '&status=' + encodeURIComponent(JC.statuses.join(','));
    const ctl = new AbortController();
    const kill = setTimeout(() => ctl.abort(), 20000);
    let data;
    try {
      const r = await fetch(q, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      data = await r.json();
    } finally {
      clearTimeout(kill);
    }
    const rows = (data && data.jobs) || [];
    // Dedupe: the cursor is rewound 1ms below, and completed_at is only
    // DATETIME(3) -- so rows sharing the boundary millisecond come back
    // again on purpose. Without the rewind the endpoint's strict `>` would
    // SKIP a row that shares its millisecond with the last one of a full
    // batch (documented caveat of GET /jobs/completes).
    const fresh = [];
    for (const row of rows) {
      const id = row.job_id;
      if (!id || JC.seen.has(id)) continue;
      JC.seen.add(id);
      JC.seenOrder.push(id);
      fresh.push(row);
    }
    // Keep the dedupe set bounded to a couple of buffers' worth; anything
    // older can never come back through a monotonically advancing cursor.
    const cap = Math.max(2000, JC.max * 2);
    if (JC.seenOrder.length > cap * 1.5) {
      const drop = JC.seenOrder.splice(0, JC.seenOrder.length - cap);
      for (const id of drop) JC.seen.delete(id);
    }
    if (data && data.next_cursor) {
      const ms = parseServerTime(data.next_cursor);
      JC.cursor = Number.isFinite(ms)
        ? new Date(ms - 1).toISOString()
        : data.next_cursor;
    }
    JC.lastBatch = fresh.length;
    JC.lastMs = Math.round(performance.now() - t0);
    JC.errors = 0;
    _jcEnqueue(fresh);
    _jcRenderFoot();
    return rows.length;
  } catch (e) {
    JC.errors++;
    _jcRenderFoot();
    return 0;
  }
}

async function _jcTick() {
  if (JC.paused || JC.inflight || !jcActive()) return;
  // Same in-flight guard as the main refresh loop, for the same reason: the
  // timer does not wait for an async tick, so a slow round would otherwise
  // stack requests on top of each other.
  JC.inflight = true;
  try {
    // A FULL batch means more is already queued behind the cursor -- catch up
    // in this tick instead of trickling one batch per second. Bounded so a
    // far-behind cursor can't spin.
    for (let round = 0; round <= JC_MAX_CATCHUP; round++) {
      const got = await _jcFetchOnce();
      if (got < JC_LIMIT) break;
    }
  } finally {
    JC.inflight = false;
  }
}

function jcStart() {
  if (JC.drainTimer === null) JC.drainTimer = setInterval(_jcDrainTick, JC_DRAIN_MS);
  if (JC.timer !== null) return;
  JC.timer = setInterval(_jcTick, JC_POLL_MS);
  _jcTick();
}

function jcStop() {
  if (JC.drainTimer !== null) { clearInterval(JC.drainTimer); JC.drainTimer = null; }
  if (JC.timer === null) return;
  clearInterval(JC.timer);
  JC.timer = null;
}

function jcClear() {
  const tb = document.querySelector('#jobsConsoleTable tbody');
  if (tb) tb.innerHTML = '';
  JC.seen.clear();
  JC.seenOrder.length = 0;
  JC.pending.length = 0;
  JC.dropped = 0;
  // Re-arm at "now" so a clear behaves like clearing a terminal: the next
  // line you see is the next thing that actually happens.
  JC.cursor = _jcIsoBack(1000);
  JC.follow = true;
  _jcRenderFoot(true);
}

// Called by the sub-tab click handler + setTab: the console must not poll
// while the operator is looking at something else.
function jcSyncRunning() {
  const pane = document.getElementById('jobsConsolePane');
  const on = jcActive();
  if (pane) pane.style.display = on ? '' : 'none';
  // The table sub-pane and its pager are the other half of the swap.
  const tablePane = document.getElementById('jobsTablePane');
  if (tablePane) tablePane.style.display = on ? 'none' : '';
  if (on) jcStart(); else jcStop();
}

// ---- wiring ----------------------------------------------------------------

(function jcWire() {
  const wrap = document.getElementById('jobsConsoleWrap');
  if (!wrap) return;   // console markup not present (older admin.html)
  // Follow is decided by where the operator has scrolled -- exactly like a
  // terminal pager. Scroll up and the feed stops chasing the bottom; come
  // back down and it resumes. No mode to remember, nothing to toggle.
  wrap.addEventListener('scroll', () => {
    const at = _jcAtBottom(wrap);
    if (at !== JC.follow) { JC.follow = at; _jcRenderFoot(true); }
  });

  const pause = document.getElementById('jcPause');
  if (pause) pause.addEventListener('click', () => {
    JC.paused = !JC.paused;
    pause.innerHTML = JC.paused
      ? '<iconify-icon icon="lucide:play"></iconify-icon> <span data-i18n="jobs.console.resume">再開</span>'
      : '<iconify-icon icon="lucide:pause"></iconify-icon> <span data-i18n="jobs.console.pause">停止</span>';
    if (window.applyI18n) window.applyI18n();
    // Resuming from a long pause would otherwise replay the whole gap;
    // jump the watermark to now so "resume" means "resume live".
    if (!JC.paused) { JC.cursor = _jcIsoBack(1000); JC.pending.length = 0; }
    _jcRenderFoot(true);
  });

  const clear = document.getElementById('jcClear');
  if (clear) clear.addEventListener('click', jcClear);

  const grep = document.getElementById('jcGrep');
  if (grep) grep.addEventListener('input', () => { JC.grep = grep.value || ''; });

  const buf = document.getElementById('jcBuffer');
  if (buf) {
    buf.value = String(JC.max);
    buf.addEventListener('change', () => {
      const n = parseInt(buf.value, 10);
      if (!Number.isFinite(n) || n <= 0) return;
      JC.max = n;
      try { localStorage.setItem(JC_BUF_KEY, String(n)); } catch (_) {}
      _jcAppend([]);      // re-trim to the new cap
    });
  }

  document.querySelectorAll('#jobsConsoleStatus [data-jc-status]').forEach(cb => {
    cb.checked = JC.statuses.includes(cb.dataset.jcStatus);
    cb.addEventListener('change', () => {
      JC.statuses = Array.from(
        document.querySelectorAll('#jobsConsoleStatus [data-jc-status]')
      ).filter(x => x.checked).map(x => x.dataset.jcStatus);
      if (!JC.statuses.length) JC.statuses = ['completed'];
      try {
        localStorage.setItem(JC_STATUS_KEY, JSON.stringify(JC.statuses));
      } catch (_) {}
    });
  });

  // Stop polling while the browser tab is hidden (same rationale as the
  // main refresh loop: an admin left open in a background tab shouldn't
  // keep asking the fleet questions nobody is reading).
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) jcStop();
    else if (jcActive()) { JC.cursor = _jcIsoBack(2000); jcStart(); }
  });

  window.addEventListener('hashchange', jcSyncRunning);
  jcSyncRunning();
})();
