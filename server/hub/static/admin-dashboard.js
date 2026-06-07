// --- Worker tag colouring + hub-badge stability (Workers tab) ---------------
// Stable per-string colour: hash -> hue, spread by *137 (golden-angle-ish) so
// near-identical strings (hub-35 / hub-36 / hub-37 differ by ONE char) still get
// well-separated hues. Tints the hub badge (a colour per hub) and the version
// cell (a colour per worker version) so the operator reads the fleet's hub
// spread + version rollout at a glance.
function _wkrHashHue(s) {
  s = String(s == null ? '' : s);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return (Math.abs(h) * 137) % 360;
}
function _wkrTagStyle(s) {
  const hue = _wkrHashHue(s);
  return `background:hsl(${hue},64%,91%); color:hsl(${hue},58%,30%); border-color:hsl(${hue},48%,78%);`;
}
// Last hub_id seen per worker, so the "which hub" badge survives a TRANSIENT
// null while a worker self-updates (disconnect -> download -> reconnect): in
// that window no hub holds its control-WS so the cross-hub aggregate reports
// hub_id="" for a few seconds. We then show the cached hub (dimmed) rather than
// dropping the badge. (Confirmed: a settled fleet has hub_id on every row; only
// mid-update rows go briefly blank.)
const _wkrLastHub = {};

// --- Submit panel sub-tabs (ジョブの実行 / Live) ----------------------
// Two sub-panes share the Submit panel. The form sub-pane holds the job
// submission UI; the live sub-pane holds the inline #liveJobPanel that
// shows status / log / noVNC / etc. for an attached job. Switching
// between them just toggles ``display`` -- the form's input values
// persist because nothing is removed from the DOM.
function setSubmitSubtab(name) {
  if (name !== 'form' && name !== 'live') name = 'form';
  document.querySelectorAll('.submit-subtab').forEach(t => {
    const on = t.dataset.submitSubtab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.submit-subpane').forEach(p => {
    p.style.display = (p.dataset.submitSubpane === name) ? '' : 'none';
  });
  // When the live sub-tab is shown but no job is attached, surface a
  // placeholder so the pane doesn't look empty. ljpAttach / ljpReset
  // also call into this code (via _updateLivePlaceholder) to keep the
  // placeholder in sync as jobs come and go.
  if (typeof _updateLivePlaceholder === 'function') _updateLivePlaceholder();
}

function _updateLivePlaceholder() {
  const ph = document.getElementById('ljpNoJobPlaceholder');
  const ljp = document.getElementById('liveJobPanel');
  if (!ph || !ljp) return;
  const attached = (typeof LJP !== 'undefined') && !!LJP.jobId;
  ph.style.display = attached ? 'none' : '';
  // The section's own inline display still controls whether the LJP
  // chrome shows once a job is attached. ljpAttach sets it to '' and
  // ljpReset to 'none'; we mirror that here so a tab switch with an
  // attached job doesn't briefly flash the placeholder.
  ljp.style.display = attached ? '' : 'none';
  // Live sub-tab indicator (dot + jobId badge) reflects the attach state.
  const dot = document.getElementById('submitSubtabLiveDot');
  const badge = document.getElementById('submitSubtabLiveJobBadge');
  if (dot) dot.style.background = attached ? '#c0392b' : '#bbb';
  if (badge) badge.textContent = attached ? (LJP.jobId.slice(0, 12)) : '';
}

// Wire sub-tab clicks. Runs once at script load; the elements exist in
// the static HTML.
(function _wireSubmitSubtabs() {
  document.querySelectorAll('.submit-subtab').forEach(btn => {
    btn.addEventListener('click', () => {
      setSubmitSubtab(btn.dataset.submitSubtab);
    });
  });
})();
window.addEventListener('hashchange', _applyHashTab);

// restore previously selected tab. Precedence (first wins):
//   1. URL hash               -- supports /#profiles deep-links
//   2. localStorage memory    -- "what was open last time"
//   3. 'submit' (default)
let _initialTab = 'submit';
let _initialLiveJob = null;
let _initialEntity = null;   // { tab, id }
try {
  const _parsed = _parseHash();
  _initialLiveJob = _parsed.jobId;
  if (_parsed.entityId) _initialEntity = { tab: _parsed.tab, id: _parsed.entityId };
  _initialTab = _parsed.tab
    || localStorage.getItem('paprika.tab')
    || 'submit';
} catch (e) {}
if (!_validTabName(_initialTab)) _initialTab = 'submit';
// When deep-linking to #live/<id> or #<tab>/<id> keep the hash intact
// (don't let setTab rewrite it); otherwise sync the tab hash as before.
setTab(_initialTab, { updateHash: !(_initialLiveJob || _initialEntity) });
if (_initialLiveJob) {
  // ljpAttach + the LJP const are declared further down this script,
  // so defer to a macrotask to dodge the temporal-dead-zone.
  setTimeout(() => {
    if (typeof ljpAttach === 'function') ljpAttach(_initialLiveJob);
  }, 0);
} else if (_initialEntity) {
  setTimeout(() => {
    const fn = _entityDeepLinkOpeners[_initialEntity.tab];
    if (fn) fn(_initialEntity.id);
  }, 0);
}

// --- actions dropdown menu --------------------------------------------------
// Opens on click. Closes on:
//   - clicking another row's actions button (only one open at a time)
//   - clicking a menu item (run the action, then dismiss)
//   - clicking anywhere outside the wrap
//   - mouse leaving the wrap (350ms grace -- so brushing past the edge
//     between button and menu doesn't accidentally close it)
//   - focus moving out of the wrap (keyboard users)
let _hoverCloseTimer = null;
function closeAllMenus(except) {
  document.querySelectorAll('.menu.open').forEach(m => {
    if (m !== except) {
      m.classList.remove('open');
      const trigger = m.previousElementSibling;
      if (trigger) trigger.classList.remove('open');
    }
  });
}
function toggleMenu(btn) {
  const menu = btn.nextElementSibling;
  const wasOpen = menu.classList.contains('open');
  closeAllMenus();
  if (!wasOpen) {
    menu.classList.add('open');
    btn.classList.add('open');
    bindAutoClose(btn.closest('.menu-wrap'));
  }
}
function bindAutoClose(wrap) {
  const menu = wrap.querySelector('.menu');
  function close() {
    menu.classList.remove('open');
    const trigger = menu.previousElementSibling;
    if (trigger) trigger.classList.remove('open');
    unbind();
  }
  function delayedClose() {
    if (_hoverCloseTimer) clearTimeout(_hoverCloseTimer);
    _hoverCloseTimer = setTimeout(() => { close(); _hoverCloseTimer = null; }, 350);
  }
  function cancelClose() {
    if (_hoverCloseTimer) { clearTimeout(_hoverCloseTimer); _hoverCloseTimer = null; }
  }
  function onFocusOut() {
    // Wait one tick for document.activeElement to settle on the new target.
    setTimeout(() => { if (!wrap.contains(document.activeElement)) close(); }, 0);
  }
  function unbind() {
    wrap.removeEventListener('mouseleave', delayedClose);
    wrap.removeEventListener('mouseenter', cancelClose);
    wrap.removeEventListener('focusout', onFocusOut);
  }
  wrap.addEventListener('mouseleave', delayedClose);
  wrap.addEventListener('mouseenter', cancelClose);
  wrap.addEventListener('focusout', onFocusOut);
}
document.addEventListener('click', e => {
  // Clicking a menu item: let the link/button handle the action, then close.
  if (e.target.closest('.menu a, .menu button')) {
    closeAllMenus();
    return;
  }
  // Clicking anywhere else inside the wrap (the actions button itself):
  // toggleMenu has already handled it.
  if (e.target.closest('.menu-wrap')) return;
  // Truly outside: close everything.
  closeAllMenus();
});

// --- main refresh -----------------------------------------------------------
// Resilient JSON fetch: returns ``fallback`` on a network error OR a
// non-2xx status (an HTML error page would otherwise blow up r.json()).
// Used by refresh() so a single flaky endpoint can't reject the whole
// Promise.all and freeze every table for the rest of the session.
async function _refreshJson(url, fallback) {
  try {
    const r = await fetch(url);
    if (!r.ok) return fallback;
    return await r.json();
  } catch (_) {
    return fallback;
  }
}

async function refresh() {
  try {
    const _tab = (location.hash || '').replace(/^#/, '').split('/')[0];
    const jobsTabActive = (_tab === '' || _tab === 'jobs');
    // ONE consolidated poll (/overview) instead of 4 separate requests
    // (/health + /workers + /jobs + /sessions) every ~2s. /overview returns
    // the job COUNT only (no per-job hydration); the Jobs tab fetches the
    // full paginated /jobs itself when it's open.
    const ov = await _refreshJson('/overview', {
      health: { store: '?', workers: '?' },
      workers: { count: 0, workers: [] },
      jobs: { total: 0 },
      sessions: { count: 0, sessions: [] },
    });
    const h = ov.health || { store: '?', workers: '?' };
    const workers = ov.workers || { count: 0, workers: [] };
    const sessions = ov.sessions || { count: 0, sessions: [] };
    let jobs = ov.jobs || { total: 0 };
    if (jobsTabActive) {
      // "最近のジョブ" = Recent Jobs. Server-side filter + pagination so
      // each status sub-tab can show ALL matching jobs (not just the
      // 300-newest window the table used to cap at). Counts come from
      // /jobs/summary (single round-trip, all 4 sub-tab badges accurate
      // every poll). The previous "fetch top 300, filter client-side"
      // path silently capped every sub-tab at 300 total entries even
      // when the store had thousands of completed / failed jobs.
      const _f = _jobsStatusFilter || 'all';
      const _ps = _jobsPageSize();
      const _off = (_jobsPage || 0) * _ps;
      let _q = `?limit=${_ps}&offset=${_off}`;
      if (_f === 'running') {
        // "実行中" tab folds queued + running so the operator sees both
        // pre-dispatch and in-flight jobs together. Server accepts a
        // comma-separated status list.
        _q += '&status=running,queued';
      } else if (_f !== 'all') {
        _q += '&status=' + encodeURIComponent(_f);
      }
      // Issue the page + the summary call in parallel so the only
      // wall-clock cost is whichever is slower. /jobs/summary is
      // memoised server-side (2s TTL) so a 2s admin poll touches the
      // slow path at most once per tick. Cache fallback `null` keeps
      // the rest of refresh() going if the summary call hiccups.
      const [pageRes, summaryRes] = await Promise.all([
        _refreshJson('/jobs' + _q, { total: 0, jobs: [], __fetchFailed: true }),
        _refreshJson('/jobs/summary', null),
      ]);
      jobs = pageRes;
      // Mark the list as having loaded at least once so the table can tell
      // "still loading" (→ spinner) apart from "loaded, 0 rows" (→ empty
      // state). A failed/timed-out page carries __fetchFailed and is NOT
      // counted as a load, so a slow/wedged hub keeps showing "Loading…"
      // instead of flashing "no jobs yet".
      if (jobs && !jobs.__fetchFailed) _jobsEverLoaded = true;
      if (summaryRes && summaryRes.by_status) {
        const _bs = summaryRes.by_status;
        const _setCnt = (id, n) => {
          const el = document.getElementById(id);
          if (el) el.textContent = n;
        };
        _setCnt('jobsCntAll',     summaryRes.total ?? 0);
        _setCnt('jobsCntSuccess', _bs.completed ?? 0);
        _setCnt('jobsCntError',   _bs.failed ?? 0);
        // "実行中" folds queued + running to match the sub-tab filter.
        _setCnt('jobsCntRunning', (_bs.running ?? 0) + (_bs.queued ?? 0));
      }
    }
    const wcount = workers.count || 0;
    const jcount = jobs.total ?? (jobs.jobs || jobs).length;
    const scount = sessions.count || 0;
    // workers=${wcount} (shared /workers count, consistent across hubs) not
    // h.workers (the serving hub's LOCAL connected count, which flickered to
    // 0 whenever nginx routed /overview to a hub owning no workers). scount is
    // now the fleet-wide session count too (see /overview).
    document.getElementById('status').textContent =
      `store=${h.store}  workers=${wcount}  jobs=${jcount}  sessions=${scount}`;
    document.getElementById('cntWorkers').textContent = wcount;
    document.getElementById('cntJobs').textContent = jcount;
    document.getElementById('cntSessions').textContent = scount;
    document.getElementById('workerCount').textContent = wcount;
    document.getElementById('sessionCount').textContent = scount;

    // Cache the latest workers list so the per-row "..." menu can
    // render its info block without a second round-trip when it opens.
    try { window._lastWorkersPayload = workers.workers || []; } catch (_) {}

    // workers table -- skip rebuild while any row's "..." menu is open,
    // matches the Jobs-tab behaviour: a 2-second refresh would otherwise
    // close the menu under the operator the moment a heartbeat fires.
    const ntbody = document.querySelector('#workersTable tbody');
    const wmenuOpen = !!document.querySelector('#workersTable .menu.open');
    if (wmenuOpen) {
      // leave rendered rows alone this tick (counts in the tab header
      // still update via cntWorkers / workerCount above)
    } else if (!workers.workers || workers.workers.length === 0) {
      ntbody.innerHTML = '<tr><td colspan=8 class="empty">no workers connected</td></tr>';
    } else {
      // Sort: alive first (live fleet stays on top, historical workers fall
      // to the bottom), then by IP ADDRESS in numeric-octet order so
      // .9 < .11 < .140 (a plain string sort would put .140 before .9).
      // worker_id breaks ties when two workers share a host IP (2 lanes).
      const _ipKey = (s) => {
        const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)/.exec(s || '');
        return m ? ((((+m[1]) * 256 + (+m[2])) * 256 + (+m[3])) * 256 + (+m[4])) : -1;
      };
      const sortedWorkers = [...workers.workers].sort((a, b) => {
        if (!!b.alive - !!a.alive) return !!b.alive - !!a.alive;
        const ka = _ipKey(a.address), kb = _ipKey(b.address);
        if (ka !== kb) return ka - kb;
        return (a.worker_id || '').localeCompare(b.worker_id || '');
      });
      ntbody.innerHTML = sortedWorkers.map(w => {
        const status = w.status || 'active';
        const wid = esc(w.worker_id);
        const alive = !!w.alive;
        // Which hub this worker's control-WS is connected to (multi-hub deploy),
        // colour-coded per hub. Prefer the live value; fall back to the
        // last-known one so the badge doesn't vanish mid-self-update (the
        // aggregate reports hub_id="" while the worker is briefly disconnected).
        let _hub = w.hub_id || '';
        if (_hub) { _wkrLastHub[w.worker_id] = _hub; }
        else { _hub = _wkrLastHub[w.worker_id] || ''; }
        const _hubCached = !w.hub_id && !!_hub;
        const hubBadge = _hub
          ? ` <span class="badge" style="${_wkrTagStyle(_hub)}${_hubCached ? ' opacity:.55;' : ''} font-size:.8em;" title="${_hubCached ? 'last-known hub (updating / reconnecting): ' : 'connected to hub '}${esc(_hub)}">${esc(_hub)}</span>`
          : '';
        // Historical workers render in greyed-out rows with no
        // status-toggle (the worker isn't here to honour it). Selecting
        // a new status posts to a 404'd /workers/{id}/status -- the UI
        // already disabled the select, but defence in depth.
        const opts = ['active', 'drain', 'standby'].map(s =>
          `<option value="${s}"${s === status ? ' selected' : ''}>${s}</option>`
        ).join('');
        const selectDisabled = alive ? '' : 'disabled';
        // Colour-coded per version so the operator sees the rollout spread
        // (old vs new hash) at a glance.
        const version = w.version
          ? `<code title="${esc(w.version)}" style="${_wkrTagStyle(w.version.split(' ')[0])} padding:1px 5px; border-radius:3px;">${esc(w.version.split(' ')[0])}</code>`
          : '<span class="empty">—</span>';
        // Profile-cache status: number of prefetched profiles + a
        // hover tooltip listing names + sizes. Click pivots to the
        // Profiles tab. "—" when nothing is cached (= no profile
        // uploads yet, or this worker just connected and prefetches
        // haven't completed). Default profile shows a yellow ★.
        const pcache = w.profiles_cached || [];
        let profilesCell;
        if (pcache.length === 0) {
          profilesCell = '<span class="empty">—</span>';
        } else {
          const _bytes = (n) => {
            if (n < 1024) return n + 'B';
            if (n < 1048576) return (n/1024).toFixed(0) + 'K';
            if (n < 1073741824) return (n/1048576).toFixed(1) + 'M';
            return (n/1073741824).toFixed(1) + 'G';
          };
          const tooltip = pcache.map(p =>
            `${p.name}  (${_bytes(p.size_bytes || 0)})`
          ).join('\n');
          // Show first 2 names inline, "+N more" if more. Default
          // profile (= name matches _profilesDefaultName cached
          // from the last /profiles round-trip) gets a ★ prefix.
          const def = (window._profilesDefaultName || '');
          const chips = pcache.slice(0, 2).map(p => {
            const star = (p.name === def) ? '<span style="color:#d4a13d;" title="default">★</span> ' : '';
            return `${star}<code style="font-size:.85em;">${esc(p.name)}</code>`;
          }).join(', ');
          const more = pcache.length > 2 ? ` <small>+${pcache.length - 2}</small>` : '';
          profilesCell = `<a href="#profiles" title="${esc(tooltip)}" `
            + `style="text-decoration:none; color:inherit;">${chips}${more}</a>`;
        }
        // The transport-level w.address is the WS source IP. For workers
        // running inside docker on the same host as the hub that becomes
        // an internal bridge IP (172.18.x.x or 10.x.x.x) -- useless to
        // an operator. The worker's noVNC URLs carry the externally-
        // reachable hostname they advertise, so prefer that.
        const novncs = w.lane_novnc_urls || w.slot_novnc_urls || [];
        let externalHost = '';
        try {
          for (const u of novncs) {
            const h = new URL(u).hostname;
            if (h && h !== 'localhost' && h !== '127.0.0.1') { externalHost = h; break; }
          }
        } catch (_) {}
        // Decide which to show as the primary value:
        //   - prefer external if w.address looks docker-internal
        //   - otherwise w.address (matches the WS dial-in IP, useful)
        const dockerInternal = w.address && /^(?:172\.(?:1[6-9]|2\d|3[01])\.|10\.\d+\.\d+\.\d+$|127\.)/.test(w.address);
        const primary = (dockerInternal && externalHost) ? externalHost : w.address;
        let address;
        if (primary) {
          let extra = '';
          if (dockerInternal && externalHost && externalHost !== w.address) {
            extra = ` <small style="color:#888;" title="docker-internal WS dial-in: ${esc(w.address)}">(via ${esc(w.address)})</small>`;
          }
          address = `<code>${esc(primary)}</code>${extra}`;
        } else {
          address = '<span class="empty">—</span>';
        }
        // Status badge shows "offline" for historical (disconnected)
        // workers so the operator can tell at a glance which entries
        // are alive vs remembered.
        let statusBadge;
        // When `updating`, the operator-controlled active/drain select
        // is meaningless (the worker is mid self-update and will exit
        // shortly anyway). Hide it so the row is less noisy and the
        // operator doesn't accidentally try to override the update.
        let hideStatusSelect = false;
        if (!alive) {
          statusBadge = `<span class="badge" style="background:#eee; color:#888; border-color:#ccc;">offline</span>`;
        } else if (w.pending_update_to) {
          // Rolling self-update in progress: worker has signalled
          // WorkerDraining and is waiting for in-flight work + the
          // hub's update slot before fetching the new source and
          // restarting. Distinct from operator-controlled drain
          // (status === 'drain' WITHOUT pending_update_to) so the
          // operator can tell at a glance "this is a normal upgrade,
          // not a problem".
          const target = w.pending_update_to.slice(0, 8);
          statusBadge = `<span class="badge" `
            + `style="background:#fff0c4; color:#8a5a00; border-color:#f5d77a;" `
            + `title="self-updating to ${esc(w.pending_update_to)}">`
            + `updating → ${esc(target)}</span>`;
          hideStatusSelect = true;
        } else {
          statusBadge = `<span class="badge ${esc(status)}">${esc(status)}</span>`;
        }
        // Resource cell formatter. Empty (0 from a pre-2026-06-06 worker)
        // renders "—"; live values get a colour band keyed off the same
        // thresholds the disk-pressure dispatcher uses (>=90% triggers
        // pick_worker skip + worker-side job-fail + auto cleanup).
        const _fmtPct = (v, opts) => {
          opts = opts || {};
          const warn = opts.warn != null ? opts.warn : 70;
          const crit = opts.crit != null ? opts.crit : 90;
          if (v == null || v <= 0) return '<span class="empty">—</span>';
          const c = v >= crit ? '#c00' : (v >= warn ? '#a04a00' : '#444');
          const bg = v >= crit ? '#fde7e7' : (v >= warn ? '#fff4d9' : 'transparent');
          const title = opts.title ? ` title="${esc(opts.title)}"` : '';
          return `<span${title} style="color:${c};background:${bg};padding:1px 5px;border-radius:3px;font-variant-numeric:tabular-nums;">${Math.round(v)}%</span>`;
        };
        const cpuCell = _fmtPct(w.cpu_pct);
        const memCell = _fmtPct(w.mem_pct);
        const diskTitle = (w.disk_free_gb != null && w.disk_free_gb > 0)
          ? `${w.disk_free_gb.toFixed(1)} GB free`
          : '';
        const diskCell = _fmtPct(w.disk_pct, { title: diskTitle });
        const ageHint = (!alive && w.last_heartbeat)
          ? ` <small style="color:#aaa;" title="last seen ${esc(new Date(w.last_heartbeat*1000).toISOString())}">${esc(fmtAgoOrNever(new Date(w.last_heartbeat*1000).toISOString()))}</small>`
          : '';
        const rowStyle = alive ? '' : ' style="opacity:0.55;"';
        // Delete is offered only for historical rows -- you can't
        // forget a worker that's still WS-connected (the DELETE endpoint
        // 409s anyway, but the UI shouldn't tempt the operator).
        const deleteItem = alive
          ? `<button onclick="window.alert('Drain and disconnect this worker first.')" disabled title="worker still connected — drain it first">${ico('trash')} delete</button>`
          : `<button class="danger" onclick="window.deleteWorker('${wid}')">${ico('trash')} forget worker</button>`;
        // worker_id is now an IP-derived w50<3-digit> (≤ 8 chars) per
        // [[stable-worker-id]]; the old column width was set for the
        // legacy hash-based ids and looked wide-empty. Tabular-nums +
        // a compact size keeps it readable but narrow.
        const widCell = `<code style="font-size:.88em;font-variant-numeric:tabular-nums;letter-spacing:-.02em;white-space:nowrap;">${wid}</code>`;
        // When mid self-update, hide the active/drain selector entirely
        // (just the badge stays) so the operator can't fight the rollout.
        const statusInner = hideStatusSelect
          ? statusBadge
          : `${statusBadge}<select onchange="setWorkerStatus('${wid}', this.value)" ${selectDisabled}>${opts}</select>`;
        return `
        <tr${rowStyle}>
          <td>${widCell}${hubBadge}${ageHint}</td>
          <td>${address}</td>
          <td>
            <span class="wstat">
              ${statusInner}
            </span>
          </td>
          <td>${w.in_flight} / ${w.capacity}</td>
          <td>${cpuCell}</td>
          <td>${memCell}</td>
          <td>${diskCell}</td>
          <td>${profilesCell}</td>
          <td>${version}</td>
          <td>
            <div class="menu-wrap">
              <button class="action-btn" onclick="window.toggleWorkerMenu(this, '${wid}')" title="worker actions">${ICONS.moreV}</button>
              <div class="menu">
                <button onclick="window.openWorkerDetailModal('${wid}')" title="この worker の状態とログを表示">
                  <span class="ico"><iconify-icon icon="lucide:info"></iconify-icon></span> 詳細
                </button>
                <div class="divider"></div>
                ${deleteItem}
              </div>
            </div>
          </td>
        </tr>`;
      }).join('');
    }
    // sessions table
    renderSessions(sessions.sessions || []);

    // screenshot grid follows the worker set -- but only ALIVE workers.
    // The /workers payload now includes historical (alive=false) workers
    // so the Workers tab can show them dimmed; without this filter the
    // Live Preview grid would render dead tiles for each disconnected
    // worker (they 404 on screenshot polls and look broken).
    syncScreenshotGrid((workers.workers || []).filter(w => w.alive));
    // Flip the RUNNING / IDLE indicator on each tile based on the
    // current jobs list AND active sessions. Done every refresh()
    // tick so a freshly started job lights up its lane within 2
    // seconds. Sessions are needed for codegen-loop / vision-agent
    // jobs whose JobInfo doesn't carry worker_id -- only the
    // SessionInfo records which (worker, lane) is in use.
    // Normalise jobs response: API now returns {jobs:[...], total, ...}
    // but keep compat with the legacy bare-array response.
    // On the Jobs tab we already fetched the full page. On #screens the
    // jobs TABLE is hidden, but the GRID still needs the running jobs to
    // flip each lane's RUNNING / KEEPALIVE / IDLE badge (it maps by
    // worker_id + lane_idx). /overview only carries a job COUNT, so fetch
    // the small running set here -- otherwise every tile reads IDLE even
    // while lanes are busy. Other tabs need neither, so stay empty.
    let jobList = [];
    if (jobsTabActive) {
      jobList = Array.isArray(jobs) ? jobs : (jobs.jobs || []);
    } else if (_tab === 'screens') {
      try {
        const rj = await _refreshJson('/jobs?status=running&limit=1000', { jobs: [] });
        jobList = Array.isArray(rj) ? rj : (rj.jobs || []);
      } catch (_) { jobList = []; }
    }
    syncScreenshotBusyState(jobList, sessions.sessions || []);
    sortScreenshotGrid();

    // jobs table -- skip rebuild while a row's actions menu is open,
    // otherwise the 2-second refresh would tear it down underneath the
    // user. Counts in the tab header still tick.
    // ``jobList`` is now a single server-paginated page (e.g. 20 rows)
    // for the current filter, not the unfiltered ~300 the old path
    // fetched. Sort defensively in case the store returns out of order.
    const sortedAll = [...jobList].sort((a,b) => (b.created_at || '').localeCompare(a.created_at || ''));
    // Sub-tab counters are now populated by the /jobs/counts call
    // above (all 4 in one round-trip). Just resolve _filter so the
    // visSig downstream can include it; no per-tab update needed here.
    const _filter = _jobsStatusFilter || 'all';
    // Server already filtered + paginated; no client-side filter step
    // remains. ``sorted`` is the page to render as-is.
    const sorted = sortedAll;
    const jtbody = document.querySelector('#jobsTable tbody');
    const menuOpen = !!document.querySelector('#jobsTable .menu.open');
    // Pager state: page index + page size persist across refresh()
    // ticks. _jobsPagerTotal is restamped so the pager UI rebuild
    // below can read it without re-sorting.
    const pageSize = _jobsPageSize();
    // Total = filtered-store total returned by the server (e.g. 2900
    // completed when on the 完成 sub-tab). Use this for max-page
    // calculation so the pager spans the full filtered set, not just
    // the page that's currently in memory.
    const total = jobs.total ?? sorted.length;
    const maxPage = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (_jobsPage > maxPage) _jobsPage = maxPage;     // clamp when total shrinks
    if (_jobsPage < 0)       _jobsPage = 0;
    // The server already sliced to the requested page, so ``sorted``
    // IS the visible window. The startIdx / endIdx values below are
    // for display only (= "21-40 / 2900" in the pager footer).
    const startIdx = _jobsPage * pageSize;
    const endIdx   = startIdx + sorted.length;
    const visible  = sorted;

    // Signature of the rendered set. Excludes duration (time-based) so
    // a tick that only advances the running-job clocks doesn't bust
    // the cache and force a full rebuild. Includes session_id +
    // status pair because the actions menu shows a "save cookies"
    // entry that depends on (j.session_id && j.status === 'running').
    const visSig = visible.map(j =>
      j.job_id + '|' + j.status + '|' + (j.started_at || '')
      + '|' + (j.completed_at || '') + '|' + (j.worker_id || '')
      + '|' + (j.lane_idx ?? j.slot_idx ?? '')
      + '|' + (j.session_id || '')
    ).join('!') + '#' + _filter + '#' + total + '#' + _jobsPage + '#' + pageSize;

    // Did this tick produce a structural change that warrants rebuilding
    // the table + pager? Set to true only in the full-rebuild branch
    // below; the duration-only fast path leaves it false so we skip
    // applyJobCols + renderJobsPager's innerHTML churn too.
    let didStructuralUpdate = false;
    if (menuOpen) {
      // leave the rendered rows alone this tick. Don't update _jobsLastSig
      // either, so the next clean tick re-renders even if the user closed
      // the menu while the data was changing.
    } else if (sorted.length === 0) {
      // Three distinct zero-row situations -- don't lump them into one
      // misleading "no jobs yet":
      //   • first page hasn't arrived yet, or it failed/timed-out and we
      //     never had data        → "Loading…" spinner
      //   • fetch failed this tick but we HAD rows before (transient hub
      //     hiccup / wedged hub)  → keep the last rows, don't flash empty
      //   • fetch OK, genuinely 0 → "no jobs yet"
      const _tr = (k, fb) => (window.i18next && window.i18next.t)
        ? window.i18next.t(k, { defaultValue: fb }) : fb;
      const fetchFailed = !!(jobs && jobs.__fetchFailed);
      if (fetchFailed && _jobsEverLoaded) {
        // transient miss after a good load: leave existing rows untouched
        // and DON'T restamp _jobsLastSig, so the next good tick redraws.
      } else if (!_jobsEverLoaded) {
        if (_jobsLastSig !== '__loading__') {
          jtbody.innerHTML = '<tr><td colspan=10 class="empty loading-row" data-i18n="jobs.loading">'
            + _tr('jobs.loading', 'Loading…') + '</td></tr>';
          _jobsLastSig = '__loading__';
          didStructuralUpdate = true;
        }
      } else if (_jobsLastSig !== '__empty__') {
        jtbody.innerHTML = '<tr><td colspan=10 class="empty" data-i18n="jobs.empty">'
          + _tr('jobs.empty', 'no jobs yet') + '</td></tr>';
        _jobsLastSig = '__empty__';
        didStructuralUpdate = true;
      }
    } else if (visSig === _jobsLastSig) {
      // Fast path: nothing structurally changed since last render. Only
      // update the duration cell of in-flight rows so the elapsed-time
      // text keeps ticking. This skips the expensive per-row HTML
      // template + innerHTML replacement that used to fire every 2 s.
      for (const j of visible) {
        if (j.status !== 'running' && j.status !== 'queued') continue;
        const cell = jtbody.querySelector(
          'tr[data-job-id="' + j.job_id + '"] td[data-col="duration"]'
        );
        if (cell) cell.innerHTML = fmtJobDuration(j);
      }
    } else {
      didStructuralUpdate = true;
      jtbody.innerHTML = visible.map(j => {
        const jid = esc(j.job_id);
        const mode = (j.options && j.options.mode) || 'fetch';
        // Mode badge in the jobs list. We collapse codegen-loop +
        // vision-agent under the visual "AI" umbrella (matches the
        // Submit form tab name) but keep the engine sub-label so
        // operators can tell the two apart at a glance.
        let modeLabel;
        if (mode === 'codegen-loop') {
          modeLabel = '<iconify-icon icon="lucide:sparkles"></iconify-icon> AI · LLM';
        } else if (mode === 'vision-agent') {
          modeLabel = '<iconify-icon icon="lucide:eye"></iconify-icon> AI · Simple';
        } else if (mode === 'rerun') {
          modeLabel = '<iconify-icon icon="lucide:code-2"></iconify-icon> code';
        } else {
          modeLabel = '<iconify-icon icon="lucide:file-down"></iconify-icon> fetch';
        }
        const laneIdx = (j.lane_idx !== null && j.lane_idx !== undefined)
          ? j.lane_idx
          : ((j.slot_idx !== null && j.slot_idx !== undefined) ? j.slot_idx : null);
        const canAttach = laneIdx !== null;
        const novncItem = j.novnc_url
          ? `<a href="${esc(j.novnc_url)}" target="_blank">${ico('play')} live noVNC</a>`
          : '';
        const attachItem = canAttach
          ? `<button onclick="attachTo('${jid}')">${ico('link')} attach next job here</button>`
          : '';
        // codegen-loop-specific menu items (script.py / attempts only exist for codegen-loop)
        const modeSpecificItems = (mode === 'codegen-loop')
          ? `<a href="/jobs/${jid}/script.py" target="_blank">${ico('code')} download script.py</a>
             <a href="/jobs/${jid}/attempts" target="_blank">${ico('list')} all attempts</a>`
          : `<a href="/jobs/${jid}/page.html" target="_blank">${ico('fileText')} captured HTML</a>`;
        // recipe save: available for all modes — backend recipe_suggestion endpoint
        // handles missing actions.json / script.py gracefully (AI investigation, rerun, vision-agent, etc.).
        const recipeSaveItem = `&nbsp;|&nbsp;
              <a href="javascript:void(0)" onclick="window.openRecipeSaveModal('${jid}')" title="この job を HostRegistry のレシピとして登録">${ico('bento')} recipe として保存</a>`;
        const codegenItems = modeSpecificItems + recipeSaveItem;
        const startedCell = j.started_at
          ? `<small title="開始 ${esc(j.started_at)} (${fmtAgoOrNever(j.started_at)})">${fmtClock(j.started_at)}</small>`
          : '<span class="empty">—</span>';
        const endedCell = j.completed_at
          ? `<small title="終了 ${esc(j.completed_at)} (${fmtAgoOrNever(j.completed_at)})">${fmtClock(j.completed_at)}</small>`
          : '<span class="empty">—</span>';
        // duration: started→completed for finished jobs, started→now while running.
        const durCell = fmtJobDuration(j);
        return `
        <tr data-job-id="${jid}">
          <td data-col="id"><code>${esc(j.job_id.substring(0,10))}</code></td>
          <td data-col="mode"><span class="badge">${modeLabel}</span></td>
          <td data-col="status"><span class="badge ${esc(j.status)}">${esc(j.status)}</span></td>
          <td data-col="url" class="url" title="${esc(j.url)}"><a href="${esc(j.url)}" target="_blank">${esc(j.url)}</a></td>
          <td data-col="worker">${j.worker_id ? `<code>${esc(j.worker_id)}</code>${canAttach ? ` <small>#${laneIdx}</small>` : ''}` : '<span class="empty">—</span>'}</td>
          <td data-col="started">${startedCell}</td>
          <td data-col="ended">${endedCell}</td>
          <td data-col="duration">${durCell}</td>
          <td data-col="actions">
            <div class="menu-wrap">
              <button class="action-btn" onclick="toggleMenu(this)" title="${tt('jobs.th.actions','actions')}">${ICONS.moreV}</button>
              <div class="menu">
                <button onclick="watchLive('${jid}')" title="Attach the Submit-tab Live panel to this job (Log / noVNC / Code / Gallery)"><span class="ico" style="color:#c0392b;">●</span> watch live (Log+noVNC+Code+Gallery)</button>
                ${novncItem}
                <a href="/ui/log/${jid}" target="_blank">${ico('signal')} live log (tail -f)</a>
                <a href="/ui/assets/${jid}" target="_blank">${ico('image')} screenshots</a>
                ${codegenItems}
                <a href="/jobs/${jid}/log.txt" target="_blank">${ico('list')} raw log file</a>
                <a href="/jobs/${jid}/result" target="_blank">${ico('code')} result JSON</a>
                ${
                  // Fetch-as-session: while a fetch is alive, the hub
                  // has registered a read-only session under
                  // j.session_id so the operator can grab cookies
                  // without waiting for the job to finish. Hidden once
                  // the job ends (the session is torn down then).
                  (j.session_id && j.status === 'running')
                    ? `<div class="divider"></div>
                       <button onclick="saveSessionCookiesToHost('${esc(j.session_id)}')" title="今のブラウザの Cookie を Host レジストリに保存 (fetch 実行中限定)"><iconify-icon icon="lucide:cookie"></iconify-icon> save cookies → host</button>`
                    : ''
                }
                <div class="divider"></div>
                <button onclick="rerun('${jid}')">${ico('refresh')} rerun</button>
                ${attachItem}
                <div class="divider"></div>
                <button class="danger" onclick="del('${jid}')">${ico('trash')} delete</button>
              </div>
            </div>
          </td>
        </tr>`;
      }).join('');
      _jobsLastSig = visSig;
    }
    if (didStructuralUpdate) {
      applyJobCols();
      renderJobsPager(total, startIdx, endIdx);
      // Drop the click-acknowledgement dim once real rows are painted.
      const _jt = document.getElementById('jobsTable');
      if (_jt) _jt.classList.remove('jobs-refreshing');
    }
  } catch (e) {
    document.getElementById('status').textContent = 'error: ' + e.message;
  }
}

// ---- recent-jobs pager --------------------------------------------------
// Client-side pagination over the already-fetched jobs list (refresh()
// hits /jobs every poll tick, so all rows are local). Page index lives
// in module state so the operator's cursor survives the 2-second
// refresh -- without that, every tick would reset to page 0 and
// scrolling-to-page-3 would be impossible.
let _jobsPage = 0;
// Signature of the LAST rendered page of the jobs table -- captures
// every visible row's identity + the fields that affect its rendered
// HTML (status badge, started/ended timestamps, worker assignment,
// session-cookie button visibility). Excludes the duration column,
// which is recomputed against `now` on every tick and would otherwise
// invalidate the cache constantly. When this matches the freshly-
// computed sig we SKIP the innerHTML rebuild entirely and just update
// the duration cells of running rows in place -- the win for an
// 800-job operator on a 2 s poll: ~50× fewer DOM teardown/rebuild
// cycles on a steady-state page where nothing structurally changed.
let _jobsLastSig = '';
// True once the Recent Jobs page has come back successfully at least once.
// Until then (or while a fetch is failing on first load) the empty table
// shows a "Loading…" spinner instead of the misleading "no jobs yet".
let _jobsEverLoaded = false;
// W: status filter for the Recent Jobs table. One of:
//   'all' | 'completed' | 'failed' | 'running'
// Always defaults to "全部" (all) on page load -- operators expect
// the Jobs tab to show everything when they navigate to it. A
// previously-selected sub-tab filter is intentionally NOT restored
// from localStorage (older behavior was confusing: the table looked
// empty when the restored filter had no matches, making it seem
// like the tab wasn't loading).
const JOBS_STATUS_FILTER_KEY = 'paprika.jobs.statusFilter';
let _jobsStatusFilter = 'all';
const JOBS_PAGE_SIZE_KEY = 'paprika.jobs.pageSize';
const JOBS_PAGE_SIZE_OPTIONS = [10, 20, 50, 100, 200];

function _jobsPageSize() {
  try {
    const v = parseInt(localStorage.getItem(JOBS_PAGE_SIZE_KEY) || '20', 10);
    return JOBS_PAGE_SIZE_OPTIONS.includes(v) ? v : 20;
  } catch (_) { return 20; }
}
function _jobsPageSizeSet(n) {
  try { localStorage.setItem(JOBS_PAGE_SIZE_KEY, String(n)); } catch (_) {}
}

function renderJobsPager(total, startIdx, endIdx) {
  const host = document.getElementById('jobsPager');
  if (!host) return;
  const pageSize = _jobsPageSize();
  const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
  if (total === 0) {
    host.innerHTML = '';
    return;
  }
  // Don't trigger refresh() while the actions menu is open -- the
  // host's innerHTML rebuild would close any menu the operator just
  // opened. Same protection as the table rebuild above.
  if (document.querySelector('#jobsTable .menu.open')) return;
  const display1 = startIdx + 1;            // 1-based for humans
  const display2 = endIdx;                  // already exclusive end → upper bound
  const prevDisabled = _jobsPage <= 0;
  const nextDisabled = _jobsPage >= maxPage;
  const opts = JOBS_PAGE_SIZE_OPTIONS
    .map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n}</option>`)
    .join('');
  host.innerHTML = `
    <span style="color:#666;">${display1}-${display2} / ${total}</span>
    <button class="pill" id="jobsPagerPrev" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${prevDisabled ? 'disabled' : ''}>
      <iconify-icon icon="lucide:chevron-left"></iconify-icon> prev
    </button>
    <span style="color:#666;">page ${_jobsPage + 1} / ${maxPage + 1}</span>
    <button class="pill" id="jobsPagerNext" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${nextDisabled ? 'disabled' : ''}>
      next <iconify-icon icon="lucide:chevron-right"></iconify-icon>
    </button>
    <span style="margin-left:auto; color:#888; font-size:.85em;">
      per page <select id="jobsPagerSize" style="padding:2px 4px;">${opts}</select>
    </span>
  `;
  const prevBtn = document.getElementById('jobsPagerPrev');
  const nextBtn = document.getElementById('jobsPagerNext');
  const sizeSel = document.getElementById('jobsPagerSize');
  if (prevBtn) prevBtn.addEventListener('click', () => {
    if (_jobsPage > 0) { _jobsPage--; refresh(); }
  });
  if (nextBtn) nextBtn.addEventListener('click', () => {
    _jobsPage++; refresh();  // refresh() re-clamps to maxPage
  });
  if (sizeSel) sizeSel.addEventListener('change', () => {
    const n = parseInt(sizeSel.value, 10);
    if (JOBS_PAGE_SIZE_OPTIONS.includes(n)) {
      _jobsPageSizeSet(n);
      _jobsPage = 0;   // start of the new pagination
      refresh();
    }
  });
}

// ---- jobs column picker -------------------------------------------------
// The jobs table is column-heavy. Rather than cram start/end times in or
// drop existing data, we let the operator choose which columns to show.
// Selection is persisted in localStorage and re-applied after every poll
// re-render (rows are rebuilt ~every 2s, so visibility must be reasserted).
const JOB_COLS = [
  { key: 'id',       i18n: 'jobs.th.id',       fallback: 'id' },
  { key: 'mode',     i18n: 'jobs.th.mode',     fallback: 'mode' },
  { key: 'status',   i18n: 'jobs.th.status',   fallback: 'status' },
  { key: 'url',      i18n: null,               fallback: 'URL' },
  { key: 'worker',   i18n: 'jobs.th.worker',   fallback: 'worker/lane' },
  { key: 'started',  i18n: 'jobs.th.started',  fallback: 'started' },
  { key: 'ended',    i18n: 'jobs.th.ended',    fallback: 'ended' },
  { key: 'duration', i18n: 'jobs.th.duration', fallback: 'duration' },
  { key: 'actions',  i18n: 'jobs.th.actions',  fallback: 'actions', fixed: true },
];
// Columns hidden by default (operators can opt them back in). Kept lean so
// the default view stays readable while still surfacing start/end times.
const JOB_COLS_DEFAULT_HIDDEN = ['duration'];

function _jobColsHidden() {
  try {
    const raw = localStorage.getItem('paprika.jobs.cols');
    if (raw === null) return new Set(JOB_COLS_DEFAULT_HIDDEN);
    return new Set(JSON.parse(raw));
  } catch (_) { return new Set(JOB_COLS_DEFAULT_HIDDEN); }
}
function _jobColsSave(hidden) {
  try { localStorage.setItem('paprika.jobs.cols', JSON.stringify([...hidden])); } catch (_) {}
}

// Apply current visibility to every header + body cell carrying data-col.
function applyJobCols() {
  const hidden = _jobColsHidden();
  document.querySelectorAll('#jobsTable [data-col]').forEach(el => {
    el.style.display = hidden.has(el.getAttribute('data-col')) ? 'none' : '';
  });
}

// Build the checkbox list inside the picker dropdown.
function renderJobColsMenu() {
  const menu = document.getElementById('jobsColsMenu');
  if (!menu) return;
  const hidden = _jobColsHidden();
  const tr = (window.i18next && window.i18next.t) ? window.i18next.t.bind(window.i18next) : null;
  menu.innerHTML = JOB_COLS.map(c => {
    const label = (tr && c.i18n) ? tr(c.i18n) : c.fallback;
    const checked = c.fixed || !hidden.has(c.key) ? 'checked' : '';
    const dis = c.fixed ? 'disabled' : '';
    return `<label style="display:flex; align-items:center; gap:8px; padding:4px 10px; white-space:nowrap; cursor:${c.fixed ? 'default' : 'pointer'}; opacity:${c.fixed ? 0.5 : 1};">
      <input type="checkbox" data-col-toggle="${c.key}" ${checked} ${dis}> ${esc(label)}
    </label>`;
  }).join('');
  menu.querySelectorAll('input[data-col-toggle]').forEach(inp => {
    inp.addEventListener('change', () => {
      const key = inp.getAttribute('data-col-toggle');
      const h = _jobColsHidden();
      if (inp.checked) h.delete(key); else h.add(key);
      _jobColsSave(h);
      applyJobCols();
    });
  });
}

// Wire up the columns button (toggle dropdown, close on outside click).
(function initJobColsPicker() {
  function bind() {
    const btn = document.getElementById('jobsColsBtn');
    const menu = document.getElementById('jobsColsMenu');
    if (!btn || !menu) { setTimeout(bind, 200); return; }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle('open');
      if (open) renderJobColsMenu();
    });
    document.addEventListener('click', (e) => {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        menu.classList.remove('open');
      }
    });
    applyJobCols();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else { bind(); }
})();

// Format a job's elapsed/total duration. running -> started→now (live),
// finished -> started→completed. Returns a dash when no start time yet.
function fmtJobDuration(j) {
  const start = j.started_at ? parseServerTime(j.started_at) : NaN;
  if (isNaN(start)) return '<span class="empty">—</span>';
  const end = j.completed_at ? parseServerTime(j.completed_at) : Date.now();
  let s = Math.max(0, Math.floor((end - start) / 1000));
  const live = !j.completed_at;
  let out;
  if (s < 60) out = s + 's';
  else if (s < 3600) out = Math.floor(s/60) + 'm' + (s % 60 ? (s%60)+'s' : '');
  else out = Math.floor(s/3600) + 'h' + (Math.floor((s%3600)/60) ? Math.floor((s%3600)/60)+'m' : '');
  return live ? `<small style="color:#a07000;">▶ ${out}</small>` : `<small>${out}</small>`;
}

// ---- sessions ----------------------------------------------------------

function renderSessions(items) {
  const tbody = document.querySelector('#sessionsTable tbody');
  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan=8 class="empty">no active sessions</td></tr>';
    return;
  }
  const fmtAgo = (iso) => {
    if (!iso) return '—';
    const s = Math.max(0, Math.floor((Date.now() - parseServerTime(iso)) / 1000));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s/60) + 'm ago';
    return Math.floor(s/3600) + 'h ago';
  };
  // state -> ( emoji, label, css-color )
  const stateBadge = (s) => {
    const st = s.state || 'idle';
    if (st === 'running') {
      const a = s.current_action ? `: ${esc(s.current_action)}` : '';
      return `<span title="action in flight" style="color:#a07000;">🟡 running${a}</span>`;
    }
    if (st === 'closing') {
      return `<span title="DELETE in progress" style="color:#aa3030;">🔴 closing</span>`;
    }
    return `<span title="open, waiting for next command" style="color:#208030;">🟢 idle</span>`;
  };
  tbody.innerHTML = items.map(s => {
    const sid = esc(s.session_id);
    const shortSid = sid.length > 14 ? sid.substring(0,12) + '…' : sid;
    const wid = esc(s.worker_id || '');
    const lane = (s.lane_idx ?? '') !== '' ? `#${s.lane_idx}` : '';
    const url = esc(s.initial_url || '');
    const novnc = s.novnc_url
      ? `<a href="${esc(s.novnc_url)}${s.novnc_url.includes('?') ? '&' : '?'}autoconnect=1&resize=scale&reconnect=1" target="_blank">↗ noVNC</a>`
      : '<span class="empty">—</span>';
    return `
      <tr>
        <td><code title="${sid}">${esc(shortSid)}</code></td>
        <td>${stateBadge(s)}</td>
        <td><code>${wid}</code> <small>${lane}</small></td>
        <td class="url" title="${url}">${url ? `<a href="${url}" target="_blank">${url}</a>` : '<span class="empty">—</span>'}</td>
        <td><span title="created ${esc(s.created_at || '')}">${fmtAgo(s.last_active_at || s.created_at)}</span></td>
        <td>${s.visited_count ?? 0}</td>
        <td>${novnc}</td>
        <td style="white-space:nowrap;">
          <button class="pill" style="background:#f3eeff; border-color:#b89fe0; color:#4a1f8a;"
                  onclick="openForensicsModal('${sid}')"
                  title="このセッションで Forensics 調査を実行 (LLM 読み取り/操作プローブ)">
            <iconify-icon icon="lucide:microscope"></iconify-icon> forensics
          </button>
          <button class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"
                  onclick="saveSessionCookiesToHost('${sid}')"
                  title="今のブラウザの Cookie を Host レジストリに保存 (再ログイン不要に)">
            <iconify-icon icon="lucide:cookie"></iconify-icon> save → host
          </button>
          <button class="pill" onclick="closeSession('${sid}')" title="DELETE /sessions/${sid}">
            ${ico('trash')} close
          </button>
        </td>
      </tr>`;
  }).join('');
}

// "save cookies → host": fetch the session's current cookies from the
// worker, then open the existing Hosts modal pre-filled so the operator
// can review / edit / save. Host is inferred from the current URL but
// remains editable in the modal. By default the cookies are filtered
// to those that apply to the current host; the operator can re-fetch
// with the "all cookies" toggle below the textarea to see everything.
async function _fetchSessionCookies(sid, opts) {
  const showAll = !!(opts && opts.all);
  const explicitHost = (opts && opts.host) || '';
  const params = new URLSearchParams();
  if (showAll) params.set('all_cookies', 'true');
  if (explicitHost) params.set('host', explicitHost);
  const qs = params.toString();
  const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/cookies' + (qs ? ('?' + qs) : ''));
  if (!r.ok) {
    const err = await r.json().catch(() => null);
    throw new Error((err && err.detail) || ('HTTP ' + r.status));
  }
  return await r.json();
}

async function saveSessionCookiesToHost(sid) {
  try {
    const j = await _fetchSessionCookies(sid, {});
    const cookies = j.cookies || [];
    const currentUrl = j.current_url || '';
    const total = j.total_in_browser || cookies.length;
    let host = j.host_filter || '';
    if (!host) {
      try { host = new URL(currentUrl).hostname || ''; } catch (e) { host = ''; }
    }
    // Normalise client-side too so the input box shows the same host
    // the server would store (example.com vs www.example.com).
    if (host && host.startsWith('www.')) host = host.substring(4);
    // Pre-load the existing record (if any) so we can merge notes.
    let existingNotes = '';
    if (host) {
      try {
        const er = await fetch('/hosts/' + encodeURIComponent(host));
        if (er.ok) {
          const ex = await er.json();
          existingNotes = ex.notes || '';
        }
      } catch (e) {}
    }
    const titleEl = document.getElementById('hostModalTitle');
    const hostInput = document.getElementById('hostModalHost');
    const cookiesArea = document.getElementById('hostModalCookies');
    const notesInput = document.getElementById('hostModalNotes');
    const delBtn = document.getElementById('hostModalDelete');
    const filterInfo = cookies.length + ' / ' + total + ' cookies (matching ' + (host || '?') + ')';
    titleEl.textContent = 'Save browser cookies → ' + (host || 'host') + ' — ' + filterInfo;
    hostInput.value = host;
    hostInput.disabled = false;
    cookiesArea.value = JSON.stringify(cookies, null, 2);
    notesInput.value = existingNotes || ('imported from session ' + sid.substring(0, 12) + ' at ' + new Date().toISOString().substring(0, 19) + 'Z');
    delBtn.style.display = 'none';
    // Stash the session id on the modal so a "show all" toggle button
    // can re-fetch without arguments. We add the toggle below the
    // cookies textarea each time the import flow runs; idempotent.
    _ensureCookieRefetchToggle(sid, host);
    _openHostModal();
    setTimeout(() => {
      const hostsTab = document.querySelector('#tabs .tab[data-tab="hosts"]');
      if (hostsTab) hostsTab.click();
    }, 0);
  } catch (e) {
    alert('cookie fetch failed: ' + e.message);
  }
}

// Inject (once) a small toolbar inside the Hosts modal that lets the
// operator switch between "host-filtered" and "all cookies in browser"
// views when they got there via the "save → host" path. Hidden when
// they opened the modal manually via Add / Edit.
function _ensureCookieRefetchToggle(sid, host) {
  let bar = document.getElementById('hostModalCookieToolbar');
  if (!bar) {
    const cookiesArea = document.getElementById('hostModalCookies');
    if (!cookiesArea || !cookiesArea.parentNode) return;
    bar = document.createElement('div');
    bar.id = 'hostModalCookieToolbar';
    bar.style.cssText = 'display:flex; gap:8px; align-items:center; margin-top:-4px; padding:6px 0; font-size:0.85em; color:#666;';
    bar.innerHTML = `
      <button type="button" id="hostModalCookieFilterMatch" class="pill" style="background:#eef8ff; border-color:#9bf;" title="現在の host に一致する Cookie のみを表示">🎯 host-match only</button>
      <button type="button" id="hostModalCookieFilterAll" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;" title="ブラウザのすべての Cookie (cross-site トラッカー含む)">🌐 all cookies in browser</button>
      <span id="hostModalCookieFilterHint" style="margin-left:auto; color:#888;"></span>`;
    cookiesArea.parentNode.insertBefore(bar, cookiesArea);
  }
  bar.style.display = 'flex';
  bar.dataset.sid = sid;
  bar.dataset.host = host || '';
  const matchBtn = document.getElementById('hostModalCookieFilterMatch');
  const allBtn = document.getElementById('hostModalCookieFilterAll');
  matchBtn.onclick = async () => {
    try {
      const j = await _fetchSessionCookies(sid, { host });
      document.getElementById('hostModalCookies').value = JSON.stringify(j.cookies || [], null, 2);
      document.getElementById('hostModalCookieFilterHint').textContent =
        (j.cookies || []).length + ' / ' + (j.total_in_browser || 0) + ' shown';
    } catch (e) { alert('refetch failed: ' + e.message); }
  };
  allBtn.onclick = async () => {
    try {
      const j = await _fetchSessionCookies(sid, { all: true });
      document.getElementById('hostModalCookies').value = JSON.stringify(j.cookies || [], null, 2);
      document.getElementById('hostModalCookieFilterHint').textContent =
        (j.cookies || []).length + ' shown (no filter)';
    } catch (e) { alert('refetch failed: ' + e.message); }
  };
}

// Hide the cookie-refetch toolbar when the modal is opened via add/edit
// (we only want it for the session-import flow).
function _hideCookieRefetchToggle() {
  const bar = document.getElementById('hostModalCookieToolbar');
  if (bar) bar.style.display = 'none';
}

async function closeSession(sid) {
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid), { method: 'DELETE' });
    if (!r.ok && r.status !== 404) {
      alert('close failed: ' + r.status);
    }
  } catch (e) {
    alert('close failed: ' + e.message);
  }
  refresh();
}

async function closeAllSessions() {
  if (!confirm('Close ALL active sessions?')) return;
  try {
    const r = await fetch('/sessions').then(r => r.json());
    await Promise.all((r.sessions || []).map(s =>
      fetch('/sessions/' + encodeURIComponent(s.session_id), { method: 'DELETE' })
        .catch(() => null)
    ));
  } catch (e) {
    alert('close-all failed: ' + e.message);
  }
  refresh();
}

async function openSessionInteractive() {
  const url = prompt('Initial URL? (leave empty for about:blank)', 'https://example.com');
  if (url === null) return;
  const body = {};
  if (url) body.initial_url = url;
  try {
    const r = await fetch('/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('open failed: ' + ((err && err.detail) || r.status));
      return;
    }
    const info = await r.json();
    // Open the live noVNC for the new session in a new tab so the
    // operator can see what they just spun up.
    if (info.novnc_url_autoconnect) {
      window.open(info.novnc_url_autoconnect, '_blank');
    }
  } catch (e) {
    alert('open failed: ' + e.message);
  }
  refresh();
}

async function setWorkerStatus(workerId, status) {
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId) + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('status update failed: ' + ((err && err.detail) || r.status));
    }
  } catch (e) {
    alert('status update failed: ' + e.message);
  }
  refresh();
}

// ---- worker actions menu (Workers tab "..." dropdown) -------------------
// Mirrors the Jobs-tab actions menu visually: a "..." button per row opens
// a popover with the worker's recent activity log + a delete button.
// Designed to defer the /workers/{id}/logs round-trip until the menu
// actually opens so the 2-second tab refresh doesn't fire one per worker.

// Simplified: the menu is now just a list of action items (詳細 / delete /
// future...). All the heavy info rendering moved to openWorkerDetailModal
// below so the dropdown stays narrow and has room for more items.
window.toggleWorkerMenu = function(btn, workerId) {
  try { window.toggleMenu(btn); }
  catch (_) {
    const menu = btn.nextElementSibling;
    if (menu) menu.classList.toggle('open');
  }
};

// "詳細" menu item handler. Pulls the worker's live snapshot + recent
// activity log into the workerDetailModal <dialog>. Reuses
// renderWorkerInfoBlock so the rendered info matches what the old
// inline dropdown used to show, plus the same logs panel.
window.openWorkerDetailModal = async function(workerId) {
  const dlg = document.getElementById('workerDetailModal');
  const body = document.getElementById('workerDetailBody');
  if (!dlg || !body) return;
  // Close any open kebab menu first so the dropdown doesn't shadow
  // the modal's backdrop on slow renders.
  try {
    document.querySelectorAll('#workersTable .menu.open')
      .forEach(m => m.classList.remove('open'));
  } catch (_) {}
  // Stash the active workerId so the refresh button can re-run.
  dlg.__workerId = workerId;
  // Resolve from the last /workers payload so we don't wait on a
  // separate round-trip just for the static info block; logs are
  // fetched in parallel below.
  let snap = null;
  try {
    snap = (window._lastWorkersPayload || []).find(w => w.worker_id === workerId);
  } catch (_) {}
  body.innerHTML = renderWorkerInfoBlock(snap, workerId) + `
    <div style="margin-top:14px; padding-top:10px; border-top:1px solid #eee;">
      <div style="font-weight:600; color:#555; margin-bottom:6px;">recent activity</div>
      <div data-worker-logs-modal style="font-family: ui-monospace, Menlo, Consolas, monospace; font-size:.82em; background:#fafbfc; border:1px solid #eee; border-radius:4px; padding:8px 10px; max-height:320px; overflow-y:auto; color:#444;">
        <div style="color:#888;">loading…</div>
      </div>
    </div>`;
  if (typeof dlg.showModal === 'function') {
    try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); }
  } else {
    dlg.setAttribute('open', '');
  }
  // Keep the URL bar in sync so the page is bookmarkable / shareable.
  if (typeof _entityHashSync === 'function') _entityHashSync('workers', workerId);
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId) + '/logs?limit=200');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const logHost = body.querySelector('[data-worker-logs-modal]');
    if (!logHost) return;
    const rows = data.logs || [];
    if (rows.length === 0) {
      logHost.innerHTML = '<div style="color:#aaa;">no activity recorded yet (hub may have restarted)</div>';
      return;
    }
    logHost.innerHTML = rows.map(row => {
      const t = (row.ts ? new Date(row.ts * 1000).toLocaleTimeString() : '');
      const kind = row.kind || 'info';
      const colour = ({
        error:     '#c0392b',
        warn:      '#d4a13d',
        lifecycle: '#3a5ca8',
        job:       '#444',
        info:      '#666',
      })[kind] || '#444';
      return `<div style="white-space:pre-wrap; word-break:break-word; line-height:1.35;">
        <span style="color:#999;">${esc(t)}</span>
        <span style="color:${colour}; font-weight:600; margin-left:4px;">${esc(kind)}</span>
        <span style="margin-left:4px;">${esc(row.line || '')}</span>
      </div>`;
    }).join('');
    logHost.scrollTop = logHost.scrollHeight;
  } catch (e) {
    const logHost = body.querySelector('[data-worker-logs-modal]');
    if (logHost) {
      logHost.innerHTML = `<div style="color:#c0392b;">failed to load logs: ${esc(e.message)}</div>`;
    }
  }
};

// Wire the modal's close + refresh buttons at DOM ready. Safe to
// run multiple times -- replaces any previously-attached handlers
// because we use direct .onclick assignment (not addEventListener).
(function _wireWorkerDetailModal() {
  const dlg = document.getElementById('workerDetailModal');
  if (!dlg) return; // not on this page
  const closeBtn = document.getElementById('workerDetailClose');
  if (closeBtn) {
    closeBtn.onclick = () => {
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
    };
  }
  // Clear the hash when the dialog closes (via button, ESC, or backdrop).
  dlg.addEventListener('close', () => {
    if (typeof _entityHashClear === 'function') _entityHashClear('workers');
  });
  const refreshBtn = document.getElementById('workerDetailRefresh');
  if (refreshBtn) {
    refreshBtn.onclick = () => {
      const wid = dlg.__workerId;
      if (wid) window.openWorkerDetailModal(wid);
    };
  }
  // Share-link button: sync hash then copy the URL to clipboard.
  const shareBtn = document.getElementById('workerDetailShareLink');
  if (shareBtn) {
    shareBtn.onclick = () => {
      const wid = dlg.__workerId;
      if (!wid) return;
      // Make sure the hash is current before we copy.
      if (typeof _entityHashSync === 'function') _entityHashSync('workers', wid);
      const url = location.href;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).catch(() => {});
      } else {
        // Fallback for older browsers / HTTP contexts.
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
      }
      // Brief "done" feedback on the button itself.
      const orig = shareBtn.innerHTML;
      shareBtn.innerHTML = '<iconify-icon icon="lucide:check"></iconify-icon> コピー完了!';
      shareBtn.style.color = '#196b2c';
      shareBtn.style.borderColor = '#7ab68a';
      shareBtn.style.background = '#eef8ee';
      setTimeout(() => {
        shareBtn.innerHTML = orig;
        shareBtn.style.color = '';
        shareBtn.style.borderColor = '';
        shareBtn.style.background = '';
      }, 1500);
    };
  }
})();

function renderWorkerInfoBlock(w, workerId) {
  if (!w) {
    return `<div style="color:#888;">worker <code>${esc(workerId)}</code> (no live snapshot)</div>`;
  }
  const aliveDot = w.alive
    ? '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#3a8c3a; margin-right:6px;"></span>'
    : '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#bbb; margin-right:6px;"></span>';
  const labels = Object.entries(w.labels || {}).map(([k,v]) => `${k}=${v}`).join(', ') || '—';
  const profileNames = (w.profiles_cached || []).map(p => p.name).join(', ') || '—';
  const last = w.last_heartbeat
    ? new Date(w.last_heartbeat * 1000).toLocaleString()
    : '—';
  return `
    <div style="display:grid; grid-template-columns: max-content 1fr; gap:4px 12px; color:#333;">
      <div style="color:#888;">worker_id</div><div><code>${esc(w.worker_id)}</code></div>
      <div style="color:#888;">state</div><div>${aliveDot}${esc(w.alive ? 'alive' : 'offline')} <small style="color:#999;">(${esc(w.status || '?')})</small></div>
      <div style="color:#888;">address</div><div>${w.address ? `<code>${esc(w.address)}</code>` : '<span class="empty">—</span>'}</div>
      <div style="color:#888;">load</div><div>${esc(String(w.in_flight))} / ${esc(String(w.capacity))}</div>
      <div style="color:#888;">version</div><div>${w.version ? `<code>${esc(w.version)}</code>` : '<span class="empty">—</span>'}</div>
      <div style="color:#888;">labels</div><div>${esc(labels)}</div>
      <div style="color:#888;">profiles</div><div>${esc(profileNames)}</div>
      <div style="color:#888;">last heartbeat</div><div>${esc(last)}</div>
    </div>`;
}

// Tiny CSS.escape polyfill for older browsers (worker_id is normally
// safe alnum + dashes, but the menu uses it inside an attribute
// selector so we still want to be defensive).
function cssEscape(s) {
  if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
}

window.deleteWorker = async function(workerId) {
  if (!confirm(`Forget worker "${workerId}"?\n\nThis removes its history from the hub (Redis row + in-process logs). The worker is NOT contacted. It can still re-register at any time.`)) {
    return;
  }
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId), { method: 'DELETE' });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('delete failed: ' + ((err && err.detail) || r.status));
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  refresh();
};

async function del(id) {
  if (!confirm(`delete job ${id}?`)) return;
  await fetch('/jobs/' + id, { method: 'DELETE' });
  refresh();
}

// "watch live" -- attach the Submit-tab Live panel (tabbed Log / noVNC
// / Code / Gallery) to an existing job. Useful for jobs you didn't
// just submit yourself (e.g. one another user kicked off, or a
// long-running job whose Submit panel you accidentally closed).
//
// Switches to the Submit tab so the panel is actually visible -- the
// panel is nested inside the Submit pane and is hidden on every other
// tab.
function watchLive(id) {
  // updateHash:false so we don't flash #submit into the URL -- ljpAttach
  // sets the shareable #live/<id> hash itself.
  setTab('submit', { updateHash: false });
  // Defer one tick so the Submit pane actually paints before we ask
  // ljpAttach to start mounting iframes / WS connections inside it.
  setTimeout(() => ljpAttach(id), 0);
}

async function rerun(id) {
  const info = await fetch('/jobs/' + id).then(r => r.json());
  if (!info || !info.url) return;
  const r = await fetch('/jobs', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ url: info.url, options: info.options || {} }),
  });
  if (!r.ok) { alert('rerun failed: ' + r.status); return; }
  refresh();
}
async function attachTo(id) {
  // Re-fetch the previous job's URL and trigger a fresh fetch-mode
  // submit pinned to the same lane. Lives on Recent Jobs row's
  // actions menu now that the Submit form no longer has an
  // "attach to job" input (Submit was simplified in PR-14).
  let prevUrl = '';
  try {
    const j = await fetch('/jobs/' + encodeURIComponent(id)).then(r => r.json());
    prevUrl = j.url || '';
  } catch (_) {}
  if (!prevUrl) {
    alert('could not look up the previous job\'s URL');
    return;
  }
  const r = await fetch('/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: prevUrl,
      options: {
        mode: 'fetch',
        scroll: true,
        attach_to_job: id,
      },
    }),
  });
  if (!r.ok) { alert('attach-rerun failed: ' + r.status); return; }
  setTab('jobs');
  refresh();
}
