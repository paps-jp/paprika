// ---- hosts (per-host cookie registry) ------------------------------------
// State: cookies are stored server-side; the UI fetches a list of host
// summaries on demand (refresh / tab activation) and opens a modal to
// edit one host's full cookie array. The modal does the JSON parse
// client-side so we can show a helpful error before POSTing.

const HOST_LIST_URL = '/hosts';
const HOST_ONE_URL = (h) => '/hosts/' + encodeURIComponent(h);
const HOST_VISITED_URL = (h) => '/hosts/' + encodeURIComponent(h) + '/visited';

// Hosts list paging state: persisted only in-memory so a refresh
// returns to page 1. Search box is debounced so each keystroke
// doesn't fire a request.
const HOST_PAGE_SIZE = 50;
let _hostListState = { q: '', offset: 0, total: 0 };
let _hostSearchTimer = null;

async function fetchHostListPaged(q, offset, limit) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (offset) params.set('offset', offset);
  if (limit) params.set('limit', limit);
  try {
    const r = await fetch(HOST_LIST_URL + '?' + params.toString());
    if (!r.ok) return { total: 0, hosts: [] };
    return await r.json();
  } catch (e) {
    return { total: 0, hosts: [] };
  }
}

// Backend timestamps are naive UTC (datetime.utcnow()) and serialize
// WITHOUT a zone designator, e.g. "2026-05-23T10:00:00.123456". JS
// Date.parse() treats such date-time strings as LOCAL time, skewing
// every relative display by the viewer's UTC offset (+9h in JST).
// Append 'Z' when no zone is present so they parse as UTC.
function parseServerTime(iso) {
  if (!iso) return NaN;
  let s = String(iso).trim();
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
  return Date.parse(s);
}

// Absolute local wall-clock for a server (UTC) timestamp.
// "2026-05-23T10:00:00Z" -> "05/23 19:00:23" in JST.
function fmtClock(iso) {
  const t = parseServerTime(iso);
  if (!Number.isFinite(t)) return '';
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtAgoOrNever(iso) {
  if (!iso) return '<span style="color:#999;">never</span>';
  const s = Math.max(0, Math.floor((Date.now() - parseServerTime(iso)) / 1000));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

async function renderHosts() {
  const { q, offset } = _hostListState;
  const data = await fetchHostListPaged(q, offset, HOST_PAGE_SIZE);
  const items = data.hosts || [];
  _hostListState.total = data.total || 0;
  const tbody = document.querySelector('#hostsTable tbody');
  const head = document.getElementById('hostCount');
  if (head) head.textContent = data.total || 0;
  const tabCnt = document.getElementById('cntHosts');
  if (tabCnt) tabCnt.textContent = data.total || 0;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML = '<tr><td colspan=7 class="empty">'
      + (q ? `no hosts matched ${esc(q)}` : 'no hosts registered')
      + '</td></tr>';
    renderHostsPager(0, 0);
    return;
  }
  tbody.innerHTML = items.map(h => {
    const host = esc(h.host || '');
    const notes = h.notes ? esc(h.notes) : '<span style="color:#999;">—</span>';
    const visited = h.visited_count || 0;
    // Show recrawl_patterns count alongside visited count so the
    // operator can see at a glance whether either is set.
    const patCount = (h.recrawl_patterns || []).length;
    const patHint = patCount > 0 ? ` <small style="color:#196b2c;" title="${patCount} recrawl pattern(s)">🎯${patCount}</small>` : '';
    const visitedBtn = visited > 0
      ? `<button class="pill" style="background:#eef8ff; border-color:#9bf; padding:1px 8px; font-size:.78em;" onclick="openVisitedModal('${host}')"><iconify-icon icon="lucide:filter"></iconify-icon> dedup (${visited})${patHint}</button>`
      : `<button class="pill" style="background:#f5f5fa; border-color:#bbc; color:#888; padding:1px 8px; font-size:.78em;" onclick="openVisitedModal('${host}')"><iconify-icon icon="lucide:filter"></iconify-icon> dedup (0)${patHint}</button>`;
    // Recipes column: count badge only. The standalone Recipes browse
    // tab was removed in v2 (recipe data lives in HostKnowledge now,
    // auto-maintained by the R1 Distiller). HostRecord.fetch_recipes
    // still exists on disk so the count is informative, but the click-
    // through to a Recipes tab is gone; open the host's modal to edit.
    const rcpCount = (h.fetch_recipes || []).length;
    const rcpBtn = rcpCount > 0
      ? `<span class="pill" style="background:#fff7e6; border-color:#e8c97a; color:#7a5a14; padding:1px 8px; font-size:.78em;" title="${rcpCount} legacy fetch_recipes (browse removed in v2; managed via Host edit modal)"><iconify-icon icon="lucide:bookmark-plus"></iconify-icon> ${rcpCount}</span>`
      : `<span style="color:#999; font-size:.78em;">—</span>`;
    return `
      <tr>
        <td><code>${host}</code></td>
        <td>${h.cookie_count || 0}</td>
        <td>${visitedBtn}</td>
        <td>${rcpBtn}</td>
        <td>${notes}</td>
        <td><small>${fmtAgoOrNever(h.updated_at)}</small></td>
        <td><small>${fmtAgoOrNever(h.last_used_at)}</small></td>
        <td>
          <button class="pill" style="background:#eef8ff; border-color:#9bf;" onclick="openHostModal('${host}')"><iconify-icon icon="lucide:pencil"></iconify-icon> edit</button>
        </td>
      </tr>`;
  }).join('');
  renderHostsPager(_hostListState.total, _hostListState.offset);
}

function renderHostsPager(total, offset) {
  const el = document.getElementById('hostsPager');
  if (!el) return;
  if (total <= HOST_PAGE_SIZE) { el.innerHTML = ''; return; }
  const pageNo = Math.floor(offset / HOST_PAGE_SIZE) + 1;
  const pageCount = Math.ceil(total / HOST_PAGE_SIZE);
  const prevDisabled = offset <= 0 ? 'disabled' : '';
  const nextDisabled = (offset + HOST_PAGE_SIZE) >= total ? 'disabled' : '';
  el.innerHTML = `
    <button class="pill" ${prevDisabled} onclick="hostsPagerJump(-1)">‹ prev</button>
    <span>page <strong>${pageNo}</strong> / ${pageCount}  (${total} total)</span>
    <button class="pill" ${nextDisabled} onclick="hostsPagerJump(+1)">next ›</button>
  `;
}


function hostsPagerJump(dir) {
  const off = _hostListState.offset + dir * HOST_PAGE_SIZE;
  _hostListState.offset = Math.max(0, off);
  renderHosts();
}

function _hostModalEl() { return document.getElementById('hostModal'); }

function _openHostModal() {
  const m = _hostModalEl();
  if (m) { m.style.display = 'flex'; }
}

function closeHostModal() {
  _entityHashClear('hosts');
  const m = _hostModalEl();
  if (m) { m.style.display = 'none'; }
  const err = document.getElementById('hostModalCookieErr');
  if (err) err.textContent = '';
}

async function openHostModal(host) {
  // host = '' / undefined  -> add new
  // The "host-match / all-cookies" refetch toolbar only makes sense
  // when the modal was reached via "save → host" on a live session,
  // so hide it for this plain add/edit path.
  if (typeof _hideCookieRefetchToggle === 'function') _hideCookieRefetchToggle();
  const titleEl = document.getElementById('hostModalTitle');
  const hostInput = document.getElementById('hostModalHost');
  const cookiesArea = document.getElementById('hostModalCookies');
  const notesInput = document.getElementById('hostModalNotes');
  const popupSel = document.getElementById('hostModalPopupPolicy');
  const noVideoEl = document.getElementById('hostModalNoVideo');
  const delBtn = document.getElementById('hostModalDelete');
  if (host) {
    _entityHashSync('hosts', host);
    titleEl.textContent = 'Edit host: ' + host;
    hostInput.value = host;
    hostInput.disabled = true;
    delBtn.style.display = 'inline-block';
    try {
      const r = await fetch(HOST_ONE_URL(host));
      if (r.ok) {
        const rec = await r.json();
        cookiesArea.value = JSON.stringify(rec.cookies || [], null, 2);
        notesInput.value = rec.notes || '';
        if (popupSel) popupSel.value = rec.popup_policy || 'kill';
        if (noVideoEl) noVideoEl.checked = !!rec.no_video;
      } else {
        cookiesArea.value = '[]';
        notesInput.value = '';
        if (popupSel) popupSel.value = 'kill';
        if (noVideoEl) noVideoEl.checked = false;
      }
    } catch (e) {
      cookiesArea.value = '[]';
      notesInput.value = '';
      if (popupSel) popupSel.value = 'kill';
    }
  } else {
    titleEl.textContent = 'Add host';
    hostInput.value = '';
    hostInput.disabled = false;
    cookiesArea.value = '[]';
    notesInput.value = '';
    if (popupSel) popupSel.value = 'kill';
    if (noVideoEl) noVideoEl.checked = false;
    delBtn.style.display = 'none';
  }
  _openHostModal();
  if (!host) hostInput.focus();
}

async function saveHostModal() {
  const hostInput = document.getElementById('hostModalHost');
  const cookiesArea = document.getElementById('hostModalCookies');
  const notesInput = document.getElementById('hostModalNotes');
  const errEl = document.getElementById('hostModalCookieErr');
  errEl.textContent = '';
  const host = (hostInput.value || '').trim();
  if (!host) {
    errEl.textContent = 'host is required';
    hostInput.focus();
    return;
  }
  let cookies;
  try {
    cookies = JSON.parse(cookiesArea.value || '[]');
  } catch (e) {
    errEl.textContent = 'cookies JSON parse error: ' + e.message;
    return;
  }
  if (!Array.isArray(cookies)) {
    errEl.textContent = 'cookies must be a JSON array';
    return;
  }
  // Host edit modal no longer touches recrawl_patterns -- omitting
  // the field from the PUT preserves the existing patterns (managed
  // separately via the "📋 dedup" modal).
  const popupSel = document.getElementById('hostModalPopupPolicy');
  const noVideoEl = document.getElementById('hostModalNoVideo');
  const body = {
    cookies: cookies,
    notes: (notesInput.value || '').trim() || null,
    popup_policy: popupSel ? popupSel.value : 'kill',
    no_video: noVideoEl ? !!noVideoEl.checked : false,
  };
  try {
    const r = await fetch(HOST_ONE_URL(host), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  closeHostModal();
  renderHosts();
}

async function deleteHostModal() {
  const hostInput = document.getElementById('hostModalHost');
  const host = (hostInput.value || '').trim();
  if (!host) { closeHostModal(); return; }
  if (!confirm("Delete host '" + host + "'? Sessions targeting this host will no longer get cookies auto-injected.")) return;
  try {
    const r = await fetch(HOST_ONE_URL(host), {method: 'DELETE'});
    if (!r.ok) {
      const t = await r.text();
      alert('delete failed (' + r.status + '): ' + t.slice(0, 200));
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  closeHostModal();
  renderHosts();
}

function _pasteCookieTemplate() {
  const cookiesArea = document.getElementById('hostModalCookies');
  if (!cookiesArea) return;
  const tmpl = [
    {
      "name": "session_token",
      "value": "REPLACE_ME",
      "domain": ".example.com",
      "path": "/",
      "secure": true,
      "httpOnly": true,
      "sameSite": "Lax"
    }
  ];
  cookiesArea.value = JSON.stringify(tmpl, null, 2);
  cookiesArea.focus();
}

// Wire up modal buttons + the tab-switch hook.
(function wireHosts() {
  const closeBtn = document.getElementById('hostModalClose');
  const cancelBtn = document.getElementById('hostModalCancel');
  const saveBtn = document.getElementById('hostModalSave');
  const delBtn = document.getElementById('hostModalDelete');
  const addBtn = document.getElementById('addHostBtn');
  const refreshBtn = document.getElementById('refreshHostsBtn');
  const pasteBtn = document.getElementById('hostModalPaste');
  const searchInput = document.getElementById('hostSearch');
  if (closeBtn) closeBtn.addEventListener('click', closeHostModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeHostModal);
  if (saveBtn) saveBtn.addEventListener('click', saveHostModal);
  if (delBtn) delBtn.addEventListener('click', deleteHostModal);
  if (addBtn) addBtn.addEventListener('click', () => openHostModal(''));
  if (refreshBtn) refreshBtn.addEventListener('click', renderHosts);
  if (pasteBtn) pasteBtn.addEventListener('click', _pasteCookieTemplate);
  // Debounce search input -- type-and-pause triggers a refetch.
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(_hostSearchTimer);
      _hostSearchTimer = setTimeout(() => {
        _hostListState.q = (searchInput.value || '').trim();
        _hostListState.offset = 0;
        renderHosts();
      }, 250);
    });
  }
  // Close on Escape, click-outside.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = _hostModalEl();
      if (m && m.style.display === 'flex') closeHostModal();
    }
  });
  const m = _hostModalEl();
  if (m) {
    m.addEventListener('click', (e) => {
      if (e.target === m) closeHostModal();
    });
  }

  // Refresh the table whenever the Hosts tab is activated.
  document.querySelectorAll('#tabs .tab').forEach(btn => {
    if (btn.dataset.tab === 'hosts') {
      btn.addEventListener('click', renderHosts);
    }
  });
})();

// ---- Chrome profile registry (admin UI) ----------------------------------

// ---- Chrome extension registry ------------------------------------------
//
// Mirrors the Profiles tab structure but simpler -- extensions don't
// have a "default" concept (all enabled extensions load on every
// lane), so there's no star-as-default UI here.

async function renderExtensions() {
  let data;
  try {
    const r = await fetch('/extensions');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    console.error('extensions: load failed', e);
    data = { extensions: [] };
  }
  const items = data.extensions || [];
  const tbody = document.querySelector('#extensionsTable tbody');
  const head = document.getElementById('extensionCount');
  const tabCnt = document.getElementById('cntExtensions');
  if (head) head.textContent = items.length;
  if (tabCnt) tabCnt.textContent = items.length;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="7" style="padding:12px; color:#888; text-align:center;">'
      + 'no extensions yet — click <em>upload</em> to add one</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(e => {
    const sizeKb = Math.round((e.size_bytes || 0) / 1024);
    const updated = (e.updated_at || '').slice(0, 16).replace('T', ' ');
    const enabledChecked = e.enabled !== false ? 'checked' : '';
    return `
      <tr style="border-bottom:1px solid #eee;">
        <td style="padding:8px;"><code>${esc(e.slug)}</code></td>
        <td style="padding:8px;">
          <div style="font-weight:600;">${esc(e.name || e.slug)}</div>
          ${e.description ? `<div style="color:#888; font-size:.85em;">${esc(e.description)}</div>` : ''}
        </td>
        <td style="padding:8px; color:#666; font-size:.88em;">${esc(e.version || '—')}</td>
        <td style="padding:8px; color:#888; font-size:.85em;">${sizeKb} KB</td>
        <td style="padding:8px; color:#888; font-size:.85em;">${esc(updated)}</td>
        <td style="padding:8px;">
          <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" class="ext-enabled-toggle" data-slug="${esc(e.slug)}" ${enabledChecked}>
            <span style="font-size:.85em; color:${e.enabled === false ? '#999' : '#196b2c'};">${e.enabled === false ? 'disabled' : 'enabled'}</span>
          </label>
        </td>
        <td style="padding:8px;">
          <a class="pill" href="/extensions/${encodeURIComponent(e.slug)}/download" target="_blank" style="background:#eef0ff; border-color:#6a8ec7; color:#3a5ca8;" title="ダウンロード (tar.gz)"><iconify-icon icon="lucide:download"></iconify-icon></a>
          <button class="pill ext-delete-btn" data-slug="${esc(e.slug)}" style="background:#fee; border-color:#c88; color:#933;" title="削除"><iconify-icon icon="lucide:trash-2"></iconify-icon></button>
        </td>
      </tr>`;
  }).join('');
  // Wire enable/disable toggle.
  tbody.querySelectorAll('.ext-enabled-toggle').forEach(cb => {
    cb.addEventListener('change', async () => {
      const slug = cb.dataset.slug;
      try {
        const r = await fetch('/extensions/' + encodeURIComponent(slug) + '/enabled', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: cb.checked }),
        });
        if (!r.ok) {
          alert('Toggle failed (HTTP ' + r.status + ')');
          cb.checked = !cb.checked;
          return;
        }
      } catch (e) {
        alert('Toggle failed: ' + e);
        cb.checked = !cb.checked;
        return;
      }
      renderExtensions();
    });
  });
  // Wire delete.
  tbody.querySelectorAll('.ext-delete-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const slug = b.dataset.slug;
      if (!confirm('Delete extension "' + slug + '"?')) return;
      try {
        const r = await fetch('/extensions/' + encodeURIComponent(slug), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) {
          alert('Delete failed (HTTP ' + r.status + ')');
          return;
        }
      } catch (e) { alert('Delete failed: ' + e); return; }
      renderExtensions();
    });
  });
}

(function wireExtensionUpload() {
  const btn  = document.getElementById('extUploadBtn');
  const file = document.getElementById('extUploadFile');
  if (!btn || !file) return;
  btn.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    // Default slug from the filename (strip extension); operator
    // can edit before confirming.
    const base = (f.name || '').replace(/\.(zip|crx|tar\.gz|tgz)$/i, '');
    const suggested = base.toLowerCase().replace(/[^a-z0-9._\-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
    const slug = (prompt('Slug for this extension (kebab-case):', suggested) || '').trim();
    if (!slug) { file.value = ''; return; }
    const buf = await f.arrayBuffer();
    try {
      const r = await fetch('/extensions/' + encodeURIComponent(slug), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Filename': f.name,
        },
        body: buf,
      });
      if (!r.ok) {
        const err = await r.text();
        alert('Upload failed (HTTP ' + r.status + '): ' + err);
        return;
      }
    } catch (e) {
      alert('Upload failed: ' + e);
      return;
    } finally {
      file.value = '';
    }
    renderExtensions();
  });
})();

const refreshExtensionsBtn = document.getElementById('refreshExtensionsBtn');
if (refreshExtensionsBtn) refreshExtensionsBtn.addEventListener('click', renderExtensions);

document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'extensions') btn.addEventListener('click', renderExtensions);
});
renderExtensions();

async function renderProfiles() {
  // Pulls /profiles, paints the table, updates the tab count badge.
  // No pagination -- profile counts stay small (typically a handful
  // per operator), so a flat list is fine. fmtAgoOrNever is already
  // defined for the Hosts tab; reuse it for "uploaded N hours ago".
  let data;
  try {
    const r = await fetch('/profiles');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    console.error('profiles: load failed', e);
    data = { profiles: [] };
  }
  const items = data.profiles || [];
  const tbody = document.querySelector('#profilesTable tbody');
  const head = document.getElementById('profileCount');
  const tabCnt = document.getElementById('cntProfiles');
  if (head) head.textContent = items.length;
  if (tabCnt) tabCnt.textContent = items.length;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="empty">no profiles uploaded yet — '
      + 'use the upload area above, the CLI, or the Paprika Bridge '
      + 'extension</td></tr>';
    return;
  }
  const defaultName = data.default || null;
  // Stash the current default name on window so the Workers tab
  // can star-mark the same name without an extra /profiles round
  // trip on every render. Refreshed every time renderProfiles()
  // runs (= every time the Profiles tab is shown or the user
  // explicitly hits refresh).
  window._profilesDefaultName = defaultName;
  // Banner just above the table: which profile (if any) gets auto-
  // applied to jobs that don't set options.use_profile explicitly.
  const banner = document.getElementById('profileDefaultBanner');
  if (banner) {
    if (defaultName) {
      banner.innerHTML =
        `<iconify-icon icon="lucide:star"></iconify-icon> `
        + `Default profile: <code>${esc(defaultName)}</code> `
        + `<small>(auto-applied when <code>options.use_profile</code> is omitted)</small> `
        + `<button class="pill" style="margin-left:8px; padding:1px 8px; font-size:.78em;" `
        +   `onclick="clearDefaultProfile()">clear</button>`;
      banner.style.display = 'block';
    } else {
      banner.innerHTML =
        '<small>No default profile set. '
        + 'Jobs without <code>options.use_profile</code> use the lane\'s stock profile.</small>';
      banner.style.display = 'block';
    }
  }
  tbody.innerHTML = items.map(p => {
    const name = esc(p.name || '');
    const note = p.note ? esc(p.note) : '<span style="color:#999;">—</span>';
    const src = p.source_machine
      ? `<small>${esc(p.source_machine)}</small>`
      : '<span style="color:#999;">—</span>';
    const isDefault = !!p.is_default;
    const rowStyle = isDefault
      ? 'background:#fff8e1;'    // pale yellow tint for the default row
      : '';
    const nameCell = isDefault
      ? `<code>${name}</code> <span style="color:#d4a13d;" title="default profile">★</span>`
      : `<code>${name}</code>`;
    const defaultBtn = isDefault
      ? `<button class="pill" style="padding:1px 8px; font-size:.78em; opacity:.5; cursor:default;" disabled>default</button>`
      : `<button class="pill" style="background:#fef5e7; border-color:#d4a13d; color:#8a5a00; padding:1px 8px; font-size:.78em;" onclick="setDefaultProfile('${name}')" title="auto-apply to jobs without options.use_profile">set default</button>`;
    return `
      <tr style="${rowStyle}">
        <td>${nameCell}</td>
        <td style="text-align:right;"><small>${esc(p.size_human || '')}</small></td>
        <td><small title="${esc(p.uploaded_at || '')}">${fmtAgoOrNever(p.uploaded_at)}</small></td>
        <td>${src}</td>
        <td>${note}</td>
        <td style="text-align:right; white-space:nowrap;">
          ${defaultBtn}
          <button class="pill" style="background:#fef1f1; border-color:#e88; color:#a00; padding:1px 8px; font-size:.78em;"
                  onclick="deleteProfile('${name}')"
                  title="delete this profile">
            <iconify-icon icon="lucide:trash-2"></iconify-icon> delete
          </button>
        </td>
      </tr>`;
  }).join('');
}

async function setDefaultProfile(name) {
  try {
    const r = await fetch(`/profiles/${encodeURIComponent(name)}/default`, { method: 'POST' });
    if (!r.ok) {
      const t = await r.text();
      alert(`set default failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('set default failed: ' + e.message);
    return;
  }
  renderProfiles();
}

async function clearDefaultProfile() {
  if (!confirm('Clear the default profile? Subsequent jobs without options.use_profile will run with the lane\'s stock profile.')) {
    return;
  }
  try {
    const r = await fetch('/profiles/default', { method: 'DELETE' });
    if (!r.ok) {
      const t = await r.text();
      alert(`clear default failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('clear default failed: ' + e.message);
    return;
  }
  renderProfiles();
}

async function deleteProfile(name) {
  if (!confirm(`Delete profile "${name}"?  Jobs already running with this profile won't be affected.`)) {
    return;
  }
  try {
    const r = await fetch(`/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!r.ok) {
      const t = await r.text();
      alert(`delete failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  renderProfiles();
}

(function setupProfileUpload() {
  const drop = document.getElementById('profileUploadDrop');
  const file = document.getElementById('profileUploadFile');
  const nameRow = document.getElementById('profileUploadNameRow');
  const nameInput = document.getElementById('profileUploadName');
  const startBtn = document.getElementById('profileUploadStartBtn');
  const cancelBtn = document.getElementById('profileUploadCancelBtn');
  const progress = document.getElementById('profileUploadProgress');
  const refresh = document.getElementById('profilesRefreshBtn');
  if (!drop || !file) return;     // panel not in DOM yet

  let pendingBlob = null;
  let pendingFileName = '';

  function reset() {
    pendingBlob = null;
    pendingFileName = '';
    file.value = '';
    if (nameRow) nameRow.style.display = 'none';
    if (progress) {
      progress.style.display = 'none';
      progress.textContent = '';
    }
  }

  function deriveName(fileName) {
    // "mydefault.tar.gz" -> "mydefault"; "Default.tgz" -> "Default";
    // "User Data.zip" -> "User_Data" (the hub transcodes ZIP -> tar.gz
    // on upload, so we accept .zip too).
    return (fileName || '')
      .replace(/\.tar\.gz$/i, '')
      .replace(/\.tgz$/i, '')
      .replace(/\.zip$/i, '')
      .replace(/\.gz$/i, '')
      .replace(/[^A-Za-z0-9._\-]/g, '_')
      .slice(0, 64);
  }

  // Read the first 4 bytes of the file synchronously-feeling (we
  // await on a Promise) so we can fail-fast on the common mistake
  // of uploading a Windows-zipped folder (.zip renamed to .tar.gz)
  // instead of a real gzip. Catches it before bytes go out, saves
  // the operator a 400 round-trip.
  async function readMagic(file) {
    const slice = file.slice(0, 4);
    const buf = await slice.arrayBuffer();
    return new Uint8Array(buf);
  }

  // Magic-byte check. Returns null when the format is acceptable
  // (gzip or ZIP -- the hub transcodes ZIP -> tar.gz server-side),
  // otherwise returns a hint string for the alert.
  function magicHint(m) {
    if (m.length < 2) return { err: 'file is too small to be an archive' };
    if (m[0] === 0x1f && m[1] === 0x8b) return { kind: 'gzip' };
    if (m[0] === 0x50 && m[1] === 0x4b) return { kind: 'zip' };
    if (m[0] === 0x7b || m[0] === 0x5b || m[0] === 0x3c) {
      return { err: 'this looks like text (JSON/XML), not an archive.' };
    }
    const hex = Array.from(m).map(b => b.toString(16).padStart(2,'0')).join(' ');
    return {
      err: `first bytes ${hex} -- expected 1f 8b (gzip) or 50 4b (zip).`,
    };
  }

  async function acceptFile(f) {
    if (!f) return;
    if (f.size === 0) {
      alert('selected file is empty');
      return;
    }
    if (f.size > 500 * 1024 * 1024) {
      alert(`file is ${(f.size/1024/1024).toFixed(1)} MB but limit is 500 MB. `
            + `Raise PAPRIKA_PROFILE_MAX_BYTES on the hub or trim the snapshot.`);
      return;
    }
    // Magic-byte check: catch the wrong-archive-format mistake
    // BEFORE the upload kicks off. Hub-side transcodes ZIP to tar.gz
    // automatically so we accept both; only unknown formats are
    // rejected here. Saves the operator a 400 round-trip after a
    // 100 MB upload of garbage.
    let detectedKind = 'gzip';
    try {
      const m = await readMagic(f);
      const r = magicHint(m);
      if (r.err) {
        alert(`Cannot use this file: ${r.err}`);
        return;
      }
      detectedKind = r.kind;     // 'gzip' or 'zip'
    } catch (e) {
      alert('Could not read file: ' + e.message);
      return;
    }
    pendingBlob = f;
    pendingFileName = f.name || '';
    if (nameInput && !nameInput.value) {
      nameInput.value = deriveName(pendingFileName);
    }
    if (nameRow) nameRow.style.display = 'block';
    if (progress) {
      progress.style.display = 'block';
      const note = detectedKind === 'zip'
        ? '✓ ZIP (will transcode to tar.gz on upload)'
        : '✓ gzip';
      progress.textContent =
        `selected: ${pendingFileName} (${(f.size/1024/1024).toFixed(1)} MB) ${note}`;
    }
  }

  // Click anywhere on the drop zone (except buttons / inputs) to pick a file.
  drop.addEventListener('click', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'button' || tag === 'input' || tag === 'a') return;
    file.click();
  });

  file.addEventListener('change', () => {
    if (file.files && file.files[0]) acceptFile(file.files[0]);
  });

  // Drag & drop highlight + accept.
  ['dragenter', 'dragover'].forEach(evt => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.style.background = '#f0f4ff';
      drop.style.borderColor = '#9bf';
    });
  });
  ['dragleave', 'drop'].forEach(evt => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.style.background = '#fafafd';
      drop.style.borderColor = '#c8c8d4';
    });
  });
  drop.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
      acceptFile(e.dataTransfer.files[0]);
    }
  });

  if (cancelBtn) {
    cancelBtn.addEventListener('click', reset);
  }
  if (refresh) {
    refresh.addEventListener('click', renderProfiles);
  }

  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      if (!pendingBlob) {
        alert('select a tarball first');
        return;
      }
      const name = (nameInput && nameInput.value || '').trim();
      if (!/^[A-Za-z0-9._\-]{1,64}$/.test(name)) {
        alert('invalid name: use A-Z a-z 0-9 . _ - only (max 64 chars)');
        return;
      }
      startBtn.disabled = true;
      cancelBtn.disabled = true;
      if (progress) {
        progress.textContent = `uploading ${name} ...`;
        progress.style.display = 'block';
      }
      try {
        // Use XHR rather than fetch so we get upload progress events
        // without having to chunk by hand.
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', `/profiles/${encodeURIComponent(name)}`);
          xhr.setRequestHeader('Content-Type', 'application/gzip');
          xhr.setRequestHeader('X-Paprika-Source-Machine', navigator.userAgent.slice(0, 120));
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && progress) {
              const pct = (e.loaded / e.total * 100).toFixed(0);
              progress.textContent = `uploading ${name} ... ${pct}%`;
            }
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.responseText);
            else reject(new Error('HTTP ' + xhr.status + ': ' + xhr.responseText));
          };
          xhr.onerror = () => reject(new Error('network error'));
          xhr.send(pendingBlob);
        });
        if (progress) progress.textContent = `uploaded ${name} ✓`;
        reset();
        await renderProfiles();
      } catch (e) {
        if (progress) progress.textContent = 'upload failed: ' + e.message;
        alert('upload failed: ' + e.message);
      } finally {
        startBtn.disabled = false;
        cancelBtn.disabled = false;
      }
    });
  }
})();

// Refresh the table whenever the Profiles tab is activated.
document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'profiles') {
    btn.addEventListener('click', renderProfiles);
  }
});

// Kick off a first paint so the count badge is accurate on page load
// even if the operator hasn't clicked the tab yet.
renderProfiles();

// ---- codegen engine dropdown (Submit form) -----------------------------
// Populates the engine selector under the Goal textarea with every
// engine from /engines that can speak chat-completions (kind=chat
// or vision-chat AND protocol=openai). Empty selection -> hub uses
// the env-var default (CODEGEN_LLM_URL + CODEGEN_MODEL_NAME).
// Refreshed every time the operator switches TO the Submit tab so
// engines they just added in the Engines tab show up without a
// page reload.
async function populateCodegenEngineSelect() {
  // Both the LLM mode (codegenEngineSelect) and the Fetch >
  // AI調査 sub-mode (fetchInvestigateEngineSelect) take a
  // codegen-capable engine -- same eligibility rules, same option
  // list, but they hold INDEPENDENT operator selections (LLM mode
  // and AI調査 may want different engines). One fetch of /engines
  // serves both, then we per-select preserve & restore the prior
  // value before rebuilding the option list.
  const sels = [
    document.getElementById('codegenEngineSelect'),
    document.getElementById('fetchInvestigateEngineSelect'),
  ].filter(Boolean);
  if (sels.length === 0) return;
  // Snapshot each select's current selection up-front so the rebuild
  // doesn't wipe the operator's pick.
  const prevBySel = new Map(sels.map(s => [s, s.value || '']));
  let engines = [];
  try {
    const r = await fetch('/engines');
    if (r.ok) {
      const d = await r.json();
      engines = (d && d.engines) || [];
    }
  } catch (_) {
    // Silently fall back -- the placeholder option still works.
  }
  // Operator opt-in: only show engines explicitly flagged as
  // codegen-capable in the Engines admin tab. Old records without
  // the field fall back to the legacy rule via EngineRecord.from_json
  // (kind in chat/vision-chat AND protocol=openai), so existing
  // deployments keep their selector populated.
  const usable = engines.filter(e => !!e.use_for_codegen);
  // Sort: promoted first, then by slug.
  usable.sort((a, b) => {
    if (a.promoted !== b.promoted) return a.promoted ? -1 : 1;
    return (a.slug || '').localeCompare(b.slug || '');
  });
  // Rebuild the option list. Keep the placeholder as the first
  // entry so "(default — env)" stays selectable.
  const opts = ['<option value="">(default — env)</option>'];
  for (const e of usable) {
    const slug = (e.slug || '').replace(/[<>"&]/g, '');
    const name = (e.name || e.slug || '').replace(/[<>"&]/g, '');
    const model = (e.model || '').replace(/[<>"&]/g, '');
    const star = e.promoted ? ' ★' : '';
    const label = `${slug}${star}  (${name}${model && model !== name ? ' / ' + model : ''})`;
    opts.push(`<option value="${slug}">${label}</option>`);
  }
  const html = opts.join('');
  for (const sel of sels) {
    sel.innerHTML = html;
    // Restore the previous selection if still present (= the engine
    // wasn't deleted between renders); otherwise fall through to the
    // default option.
    const prev = prevBySel.get(sel) || '';
    if (prev && [...sel.options].some(o => o.value === prev)) {
      sel.value = prev;
    }
  }
}
// Refresh on Submit-tab activation.
document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'submit') {
    btn.addEventListener('click', populateCodegenEngineSelect);
  }
});
// Initial paint so the dropdown is populated even if the operator
// lands on Submit directly (which is the default tab).
populateCodegenEngineSelect();

// ---- visited URLs modal (per-host) ---------------------------------------

const VISITED_PAGE_SIZE = 100;
let _visitedState = { host: '', q: '', offset: 0, total: 0 };
let _visitedSearchTimer = null;

function _visitedModalEl() { return document.getElementById('visitedModal'); }

// In-memory editor state for the pattern table.
let _patternRows = [];  // array of {value: string, matches: number|null}
let _patternMatchTimer = null;

async function openVisitedModal(host) {
  _visitedState = { host: host, q: '', offset: 0, total: 0 };
  _patternRows = [];
  const hostEl = document.getElementById('visitedModalHost');
  const searchEl = document.getElementById('visitedModalSearch');
  if (hostEl) hostEl.textContent = host;
  if (searchEl) searchEl.value = '';
  const patErr = document.getElementById('recrawlPatternsErr');
  if (patErr) patErr.textContent = '';
  // Load patterns from the host record. If host has none, we render
  // the "(no patterns)" placeholder.
  try {
    const r = await fetch(HOST_ONE_URL(host));
    if (r.ok) {
      const rec = await r.json();
      _patternRows = (rec.recrawl_patterns || []).map(p => ({ value: p, matches: null }));
    }
  } catch (e) { /* best-effort */ }
  renderPatternsTable();
  // Fire match-counts so the UI shows numbers right away.
  scheduleMatchCounts();
  const m = _visitedModalEl();
  if (m) m.style.display = 'flex';
  await refreshVisitedList();
}

function _matchCellHtml(row) {
  if (row.matches === null || row.matches === undefined) {
    return '<small style="color:#aaa;">…</small>';
  }
  if (row.matches === 0 && row.value) {
    return '<small style="color:#a06000;" title="no visited URL matches -- typo?">0 ⚠</small>';
  }
  return '<strong>' + row.matches + '</strong>';
}

// Patch only the per-row "matches" cells without rebuilding the
// <input> elements. Critical for keeping focus + caret position
// while the operator types -- the previous implementation
// re-innerHTML'd the whole tbody on every match-count fetch,
// destroying the focused input.
function updateMatchCellsOnly() {
  const cells = document.querySelectorAll('#patternsTbody td.match-cell');
  cells.forEach((cell, idx) => {
    if (_patternRows[idx]) {
      cell.innerHTML = _matchCellHtml(_patternRows[idx]);
    }
  });
}

function renderPatternsTable() {
  const tb = document.getElementById('patternsTbody');
  if (!tb) return;
  if (_patternRows.length === 0) {
    tb.innerHTML = '<tr><td colspan=3 style="padding:8px; color:#888; text-align:center;">(no patterns — click ➕ add row, or use ➕ pattern on a visited URL)</td></tr>';
    return;
  }
  tb.innerHTML = _patternRows.map((row, idx) => {
    const v = esc(row.value || '');
    return `
      <tr>
        <td style="padding:3px 8px;"><input type="text" value="${v}" data-pat-idx="${idx}"
          style="width:100%; box-sizing:border-box; font-family:ui-monospace, Consolas, monospace; font-size:12.5px; padding:3px 6px;"
          placeholder="https://www.example.com/category/*"></td>
        <td class="match-cell" style="padding:3px 8px; text-align:right;">${_matchCellHtml(row)}</td>
        <td style="padding:3px 8px; text-align:center;">
          <button class="pill" style="padding:0 6px; background:#fee; border-color:#c88; color:#933;" onclick="removePatternRow(${idx})" title="remove row">🗑</button>
        </td>
      </tr>`;
  }).join('');
  // Wire input change for each row -- update model + schedule the
  // match-count fetch. We deliberately DO NOT call
  // renderPatternsTable() from this handler; only the matches cell
  // gets refreshed so the input keeps focus + caret position.
  tb.querySelectorAll('input[data-pat-idx]').forEach(inp => {
    inp.addEventListener('input', (e) => {
      const i = parseInt(e.target.dataset.patIdx, 10);
      if (_patternRows[i]) {
        _patternRows[i].value = e.target.value;
        _patternRows[i].matches = null;   // invalidate cached count
        updateMatchCellsOnly();           // shows "…" immediately
        scheduleMatchCounts();            // debounced refetch
      }
    });
  });
}

function addPatternRow(value) {
  _patternRows.push({ value: value || '', matches: null });
  renderPatternsTable();
  // Focus the new row's input
  setTimeout(() => {
    const inputs = document.querySelectorAll('#patternsTbody input[data-pat-idx]');
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
  }, 0);
  if (value) scheduleMatchCounts();
}

function removePatternRow(idx) {
  if (idx < 0 || idx >= _patternRows.length) return;
  _patternRows.splice(idx, 1);
  renderPatternsTable();
}

function scheduleMatchCounts() {
  clearTimeout(_patternMatchTimer);
  _patternMatchTimer = setTimeout(refreshMatchCounts, 350);
}

async function refreshMatchCounts() {
  const host = _visitedState.host;
  if (!host || _patternRows.length === 0) return;
  const patterns = _patternRows.map(r => r.value || '');
  // Skip the call entirely when every row is blank.
  if (patterns.every(p => !p)) return;
  try {
    const r = await fetch(HOST_VISITED_URL(host) + '/match_counts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ patterns }),
    });
    if (!r.ok) return;
    const d = await r.json();
    const counts = d.counts || [];
    counts.forEach((n, i) => {
      if (_patternRows[i]) _patternRows[i].matches = n;
    });
    // Only touch the matches cells -- the input fields stay intact
    // so the operator's focus + caret position are preserved.
    updateMatchCellsOnly();
  } catch (e) { /* best-effort */ }
}

async function saveRecrawlPatterns() {
  const host = _visitedState.host;
  if (!host) return;
  const patErr = document.getElementById('recrawlPatternsErr');
  const savedHint = document.getElementById('recrawlPatternsSaved');
  if (patErr) patErr.textContent = '';
  // Collect non-empty patterns in display order, dedup.
  const seen = new Set();
  const patterns = [];
  for (const row of _patternRows) {
    const v = (row.value || '').trim();
    if (v && !seen.has(v)) {
      seen.add(v);
      patterns.push(v);
    }
  }
  // Need full body (cookies + notes) because PUT replaces them.
  let existing = {};
  try {
    const r = await fetch(HOST_ONE_URL(host));
    if (r.ok) existing = await r.json();
  } catch (e) {}
  const body = {
    cookies: existing.cookies || [],
    notes: existing.notes || null,
    recrawl_patterns: patterns,
  };
  try {
    const r = await fetch(HOST_ONE_URL(host), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (patErr) patErr.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (patErr) patErr.textContent = 'save failed: ' + e.message;
    return;
  }
  if (savedHint) {
    savedHint.style.opacity = '1';
    setTimeout(() => { savedHint.style.opacity = '0'; }, 1200);
  }
  // Refresh in-memory model from the just-saved server-side state
  // so the dedup'd / cleaned-up patterns are visible immediately.
  _patternRows = patterns.map(p => ({ value: p, matches: null }));
  renderPatternsTable();
  scheduleMatchCounts();
  // The Hosts row pattern count badge should refresh too.
  renderHosts();
}

// Used by the "➕ pattern" button on a visited URL row.
function promoteUrlToPattern(url) {
  if (!url) return;
  // Reject duplicates silently to avoid stuffing.
  const trimmed = url.trim();
  if (_patternRows.some(r => (r.value || '').trim() === trimmed)) {
    return;
  }
  _patternRows.push({ value: trimmed, matches: null });
  renderPatternsTable();
  scheduleMatchCounts();
}

function closeVisitedModal() {
  const m = _visitedModalEl();
  if (m) m.style.display = 'none';
}

async function refreshVisitedList() {
  const { host, q, offset } = _visitedState;
  if (!host) return;
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (offset) params.set('offset', offset);
  params.set('limit', VISITED_PAGE_SIZE);
  const listEl = document.getElementById('visitedModalList');
  const countEl = document.getElementById('visitedModalCount');
  if (listEl) listEl.innerHTML = '<div style="color:#888; padding:14px;">loading…</div>';
  let data;
  try {
    const r = await fetch(HOST_VISITED_URL(host) + '?' + params.toString());
    if (!r.ok) {
      if (listEl) listEl.innerHTML = '<div style="color:#a00; padding:14px;">load failed</div>';
      return;
    }
    data = await r.json();
  } catch (e) {
    if (listEl) listEl.innerHTML = '<div style="color:#a00; padding:14px;">' + esc(e.message) + '</div>';
    return;
  }
  _visitedState.total = data.total || 0;
  if (countEl) {
    if (q) countEl.textContent = `${data.total || 0} match (of full set)`;
    else countEl.textContent = `${data.total || 0} URL(s)`;
  }
  const urls = data.urls || [];
  if (urls.length === 0) {
    if (listEl) listEl.innerHTML = '<div style="color:#888; padding:14px;">'
      + (q ? 'no matches' : 'no visited URLs yet')
      + '</div>';
    renderVisitedPager(0, 0);
    return;
  }
  if (listEl) {
    listEl.innerHTML = urls.map(u => {
      const url = esc(u.url || '');
      const sha = esc(u.hash || '');
      // Pass the URL via a data attribute so we don't need to
      // worry about quote escaping in onclick="" handlers.
      return `
        <div style="display:flex; align-items:center; gap:8px; padding:3px 0; border-bottom:1px solid #f3f3f3;">
          <a href="${url}" target="_blank" style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#06a;">${url}</a>
          <button class="pill visited-promote" data-url="${url}" style="padding:0 6px; background:#eef8ee; border-color:#7ab68a; color:#196b2c; font-size:.78em;" title="この URL を recrawl pattern に追加"><iconify-icon icon="lucide:target"></iconify-icon> pattern</button>
          <button class="pill" style="padding:0 6px; background:#fee; border-color:#c88; color:#933; font-size:.78em;" title="remove from visited set" onclick="removeVisitedUrl('${sha}')">🗑</button>
        </div>`;
    }).join('');
    // Wire the per-row promote buttons.
    listEl.querySelectorAll('.visited-promote').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const url = e.currentTarget.dataset.url || '';
        promoteUrlToPattern(url);
      });
    });
  }
  renderVisitedPager(_visitedState.total, _visitedState.offset);
}

function renderVisitedPager(total, offset) {
  const el = document.getElementById('visitedModalPager');
  if (!el) return;
  if (total <= VISITED_PAGE_SIZE) {
    el.innerHTML = '';
    return;
  }
  const pageNo = Math.floor(offset / VISITED_PAGE_SIZE) + 1;
  const pageCount = Math.ceil(total / VISITED_PAGE_SIZE);
  const prevDisabled = offset <= 0 ? 'disabled' : '';
  const nextDisabled = (offset + VISITED_PAGE_SIZE) >= total ? 'disabled' : '';
  el.innerHTML = `
    <button class="pill" ${prevDisabled} onclick="visitedPagerJump(-1)">‹ prev</button>
    <span>page <strong>${pageNo}</strong> / ${pageCount}  (${total} total)</span>
    <button class="pill" ${nextDisabled} onclick="visitedPagerJump(+1)">next ›</button>
  `;
}

function visitedPagerJump(dir) {
  _visitedState.offset = Math.max(0, _visitedState.offset + dir * VISITED_PAGE_SIZE);
  refreshVisitedList();
}

async function removeVisitedUrl(sha) {
  if (!_visitedState.host || !sha) return;
  try {
    const r = await fetch(HOST_VISITED_URL(_visitedState.host) + '/' + encodeURIComponent(sha), { method: 'DELETE' });
    if (!r.ok) {
      alert('delete failed (' + r.status + ')');
      return;
    }
  } catch (e) { alert('delete failed: ' + e.message); return; }
  // Stay on the same page but refresh; if the current page is now
  // empty, step back one.
  if (_visitedState.total - 1 <= _visitedState.offset && _visitedState.offset > 0) {
    _visitedState.offset = Math.max(0, _visitedState.offset - VISITED_PAGE_SIZE);
  }
  refreshVisitedList();
  // Also refresh the hosts table so the visited count badge updates.
  renderHosts();
}

async function clearVisitedAll() {
  const host = _visitedState.host;
  if (!host) return;
  if (!confirm("Clear ALL visited URLs for host '" + host + "'?\n\nNext pap.walk() on this host will re-crawl from scratch (still respecting recrawl_patterns).")) return;
  try {
    const r = await fetch(HOST_VISITED_URL(host), { method: 'DELETE' });
    if (!r.ok) { alert('clear failed (' + r.status + ')'); return; }
  } catch (e) { alert('clear failed: ' + e.message); return; }
  _visitedState.offset = 0;
  refreshVisitedList();
  renderHosts();
}

(function wireVisitedModal() {
  const closeBtn = document.getElementById('visitedModalClose');
  const clearBtn = document.getElementById('visitedModalClear');
  const searchEl = document.getElementById('visitedModalSearch');
  const patSaveBtn = document.getElementById('recrawlPatternsSave');
  const patAddBtn = document.getElementById('patternsAddRow');
  if (closeBtn) closeBtn.addEventListener('click', closeVisitedModal);
  if (clearBtn) clearBtn.addEventListener('click', clearVisitedAll);
  if (patSaveBtn) patSaveBtn.addEventListener('click', saveRecrawlPatterns);
  if (patAddBtn) patAddBtn.addEventListener('click', () => addPatternRow(''));
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      clearTimeout(_visitedSearchTimer);
      _visitedSearchTimer = setTimeout(() => {
        _visitedState.q = (searchEl.value || '').trim();
        _visitedState.offset = 0;
        refreshVisitedList();
      }, 200);
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = _visitedModalEl();
      if (m && m.style.display === 'flex') closeVisitedModal();
    }
  });
  const m = _visitedModalEl();
  if (m) {
    m.addEventListener('click', (e) => {
      if (e.target === m) closeVisitedModal();
    });
  }
})();

// Initial render so the tab badge has the right count even before
// the user clicks into it.
renderHosts();

