// ---- presets (saved Submit-form snapshots) -------------------------------
//
// Paginated list. Designed to scale to 500+ presets without dragging
// the whole catalog across the wire on every render.

const PRESET_PAGE_SIZE = 50;
let _presetListState = { q: '', category: '', offset: 0, total: 0 };
let _presetSearchTimer = null;

// ---- Preset edit modal --------------------------------------------------
//
// Lets the operator change every field the Preset job tab cares about
// without round-tripping through "load -> Submit form -> overwrite".
// Sharable for both new-blank and load-existing flows; the openness
// of which fields are visible depends on the selected mode.
const _PRESET_EDIT_MODAL = { open: false, originalName: null, originalRecord: null };

function _presetEditModalSyncBlocks() {
  const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value || 'fetch';
  document.getElementById('presetEditModalCodegenBlock').style.display    = (mode === 'codegen-loop') ? 'flex' : 'none';
  document.getElementById('presetEditModalCodeBlock').style.display       = (mode === 'code') ? 'flex' : 'none';
  document.getElementById('presetEditModalRerunFromBlock').style.display  = (mode === 'rerun_from') ? 'flex' : 'none';
  document.getElementById('presetEditModalFetchNote').style.display       = (mode === 'fetch') ? 'block' : 'none';
}

async function _presetEditModalPopulateEngines() {
  const sel = document.getElementById('presetEditModalEngine');
  if (!sel) return;
  const prev = sel.value || '';
  let engines = [];
  try {
    const r = await fetch('/engines');
    if (r.ok) {
      const d = await r.json();
      engines = (d && d.engines) || [];
    }
  } catch (_) {}
  const usable = engines.filter(e =>
    (e.kind === 'chat' || e.kind === 'vision-chat') && e.protocol === 'openai'
  );
  usable.sort((a, b) => {
    if (a.promoted !== b.promoted) return a.promoted ? -1 : 1;
    return (a.slug || '').localeCompare(b.slug || '');
  });
  const opts = ['<option value="">(default — env)</option>'];
  for (const e of usable) {
    const slug = (e.slug || '').replace(/[<>"&]/g, '');
    const name = (e.name || e.slug || '').replace(/[<>"&]/g, '');
    const star = e.promoted ? ' ★' : '';
    opts.push(`<option value="${slug}">${slug}${star}  (${name})</option>`);
  }
  sel.innerHTML = opts.join('');
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
}

function _presetEditModalFetchCategories() {
  const dl = document.getElementById('presetEditModalCategoryList');
  if (!dl) return;
  fetch('/presets?limit=500').then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !Array.isArray(d.categories)) return;
    dl.innerHTML = d.categories
      .map(c => `<option value="${(c || '').replace(/"/g, '&quot;')}"></option>`)
      .join('');
  }).catch(() => {});
}

// Pick the mode radio value the modal should default to, given a
// loaded preset record. The record stores ui_mode (form-mode) and
// options.mode (the actual mode that runs); together they map to one
// of the modal's four radio choices.
function _presetEditModalInferMode(rec) {
  const ui = rec.ui_mode || 'fetch';
  const opts = rec.options || {};
  const oMode = opts.mode || '';
  if (oMode === 'codegen-loop') return 'codegen-loop';
  if (oMode === 'rerun') {
    if (opts.rerun_from) return 'rerun_from';
    return 'code';
  }
  if (oMode === 'fetch') return 'fetch';
  // Fallback by ui_mode.
  if (ui === 'ai') return 'codegen-loop';
  if (ui === 'code') return 'code';
  return 'fetch';
}

async function openPresetEditModal(presetName) {
  const modal = document.getElementById('presetEditModal');
  if (!modal) return;
  // Pull the full record. The /presets/{name} endpoint returns the
  // operator-set fields plus the captured options snapshot.
  let rec = {};
  try {
    const r = await fetch(PRESET_ONE_URL(presetName));
    if (!r.ok) { alert(`Load failed (HTTP ${r.status})`); return; }
    rec = await r.json();
  } catch (e) { alert(`Load failed: ${e}`); return; }

  _entityHashSync('presets', presetName);
  document.getElementById('presetEditModalTitle').textContent = `Edit preset: ${presetName}`;
  document.getElementById('presetEditModalName').value        = rec.name || presetName;
  document.getElementById('presetEditModalCategory').value    = rec.category || '';
  document.getElementById('presetEditModalDescription').value = rec.description || '';
  document.getElementById('presetEditModalUrl').value         = rec.url || '';
  document.getElementById('presetEditModalGoal').value        = rec.goal || (rec.options && rec.options.goal) || '';
  document.getElementById('presetEditModalCode').value        = rec.code_script || (rec.options && rec.options.code) || '';
  document.getElementById('presetEditModalMaxAttempts').value = rec.max_attempts || (rec.options && rec.options.max_codegen_attempts) || 3;
  const fopt = rec.options || {};
  document.getElementById('presetEditModalTimeoutCodegen').value = fopt.attempt_timeout_s || rec.attempt_timeout_s || 200;
  document.getElementById('presetEditModalTimeoutCode').value    = fopt.attempt_timeout_s || rec.attempt_timeout_s || 86400;
  document.getElementById('presetEditModalTimeoutRerun').value   = fopt.attempt_timeout_s || rec.attempt_timeout_s || 200;
  document.getElementById('presetEditModalRerunFromJob').value   = (fopt && fopt.rerun_from) || '';
  document.getElementById('presetEditModalHostDedup').checked    = (rec.host_dedup === undefined ? true : !!rec.host_dedup);
  document.getElementById('presetEditModalErr').textContent      = '';
  document.getElementById('presetEditModalRenameHint').style.display = 'none';

  // Mode radio
  const mode = _presetEditModalInferMode(rec);
  const modeRadio = document.querySelector(`input[name="presetEditModalMode"][value="${mode}"]`);
  if (modeRadio) modeRadio.checked = true;
  _presetEditModalSyncBlocks();

  // Populate engine select then set the value.
  await _presetEditModalPopulateEngines();
  const engineSel = document.getElementById('presetEditModalEngine');
  const wantEngine = (fopt && fopt.codegen_engine) || rec.codegen_engine || '';
  if (engineSel && [...engineSel.options].some(o => o.value === wantEngine)) {
    engineSel.value = wantEngine;
  } else if (engineSel) {
    engineSel.value = '';
  }

  _presetEditModalFetchCategories();
  _PRESET_EDIT_MODAL.open = true;
  _PRESET_EDIT_MODAL.originalName = presetName;
  // Stash the loaded record so we can preserve the fetch-options
  // sub-keys when the operator keeps the preset in fetch mode (the
  // modal intentionally doesn't surface those for editing here).
  _PRESET_EDIT_MODAL.originalRecord = rec;
  modal.style.display = 'flex';
}

function closePresetEditModal() {
  _entityHashClear('presets');
  const modal = document.getElementById('presetEditModal');
  if (modal) modal.style.display = 'none';
  _PRESET_EDIT_MODAL.open = false;
  _PRESET_EDIT_MODAL.originalName = null;
  _PRESET_EDIT_MODAL.originalRecord = null;
}

function _presetEditModalBuildPayload() {
  const name = (document.getElementById('presetEditModalName').value || '').trim();
  const category = (document.getElementById('presetEditModalCategory').value || '').trim();
  const description = (document.getElementById('presetEditModalDescription').value || '').trim();
  const url = (document.getElementById('presetEditModalUrl').value || '').trim();
  const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value || 'fetch';
  const goal = (document.getElementById('presetEditModalGoal').value || '').trim();
  const code = (document.getElementById('presetEditModalCode').value || '');
  const engine = document.getElementById('presetEditModalEngine').value || '';
  const maxAttempts = parseInt(document.getElementById('presetEditModalMaxAttempts').value, 10) || 3;
  const hostDedup = !!document.getElementById('presetEditModalHostDedup').checked;
  const rerunFromJob = (document.getElementById('presetEditModalRerunFromJob').value || '').trim();
  // Pick the right timeout field for the active mode.
  let timeout = 200;
  if (mode === 'codegen-loop') timeout = parseInt(document.getElementById('presetEditModalTimeoutCodegen').value, 10) || 200;
  else if (mode === 'code')        timeout = parseInt(document.getElementById('presetEditModalTimeoutCode').value, 10) || 86400;
  else if (mode === 'rerun_from')  timeout = parseInt(document.getElementById('presetEditModalTimeoutRerun').value, 10) || 200;

  let uiMode = mode;
  let aiEngine = 'codegen';
  let options = {};
  if (mode === 'fetch') {
    uiMode = 'fetch';
    // Carry over the existing fetch-options sub-keys (scroll /
    // play_videos / timing / referer / cookies_from / attach_to_job
    // …) from the loaded record so renaming or tweaking
    // category/description doesn't accidentally wipe them. Only
    // when there's no original (= fresh-blank edit) do we fall
    // back to bare {mode:'fetch'}.
    const prevOpts = (_PRESET_EDIT_MODAL.originalRecord && _PRESET_EDIT_MODAL.originalRecord.options) || null;
    if (prevOpts && (prevOpts.mode === 'fetch' || prevOpts.mode === undefined)) {
      options = Object.assign({}, prevOpts, { mode: 'fetch' });
    } else {
      options = { mode: 'fetch' };
    }
  } else if (mode === 'codegen-loop') {
    uiMode = 'ai';
    aiEngine = 'codegen';
    let g = goal || '';
    if (!hostDedup) {
      g += '\n\n追加ガードレール:\n  - **pap.walk(..., host_dedup=False)** を必ず指定する (既訪問URLも再クロール)';
    }
    options = {
      mode: 'codegen-loop',
      goal: g,
      max_codegen_attempts: maxAttempts,
      attempt_timeout_s: timeout,
    };
    if (engine) options.codegen_engine = engine;
  } else if (mode === 'code') {
    uiMode = 'code';
    aiEngine = 'code';
    options = {
      mode: 'rerun',
      code,
      attempt_timeout_s: timeout,
    };
  } else if (mode === 'rerun_from') {
    uiMode = 'code';
    aiEngine = 'code';
    options = {
      mode: 'rerun',
      rerun_from: rerunFromJob,
      attempt_timeout_s: timeout,
    };
  }
  return {
    name, category, description, url, goal,
    ui_mode: uiMode, ai_engine: aiEngine,
    code_script: code,
    max_attempts: maxAttempts,
    attempt_timeout_s: timeout,
    attempt_timeout_simple_s: 600,
    host_dedup: hostDedup,
    options,
  };
}

(function wirePresetEditModal() {
  const modal = document.getElementById('presetEditModal');
  if (!modal) return;
  // Mode radio change → toggle conditional blocks.
  document.querySelectorAll('input[name="presetEditModalMode"]').forEach(r => {
    r.addEventListener('change', _presetEditModalSyncBlocks);
  });
  // Rename hint shows when name diverges from the loaded record.
  const nameEl = document.getElementById('presetEditModalName');
  if (nameEl) {
    nameEl.addEventListener('input', () => {
      const hint = document.getElementById('presetEditModalRenameHint');
      if (!hint) return;
      const diverged = _PRESET_EDIT_MODAL.originalName
        && nameEl.value.trim() !== _PRESET_EDIT_MODAL.originalName;
      hint.style.display = diverged ? 'block' : 'none';
    });
  }
  const closeBtn  = document.getElementById('presetEditModalClose');
  const cancelBtn = document.getElementById('presetEditModalCancel');
  const saveBtn   = document.getElementById('presetEditModalSave');
  const delBtn    = document.getElementById('presetEditModalDelete');
  if (closeBtn)  closeBtn.addEventListener('click', closePresetEditModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closePresetEditModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closePresetEditModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _PRESET_EDIT_MODAL.open) closePresetEditModal();
  });
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      const errEl = document.getElementById('presetEditModalErr');
      const setErr = (m) => { if (errEl) errEl.textContent = m || ''; };
      setErr('');
      const payload = _presetEditModalBuildPayload();
      if (!payload.name) { setErr('Name は必須です'); return; }
      const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value;
      if (mode === 'rerun_from' && !(payload.options && payload.options.rerun_from)) {
        setErr('rerun_from モードでは Job ID が必須です'); return;
      }
      const oldName = _PRESET_EDIT_MODAL.originalName;
      const renaming = oldName && payload.name !== oldName;
      try {
        const r = await fetch(PRESET_ONE_URL(payload.name), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!r.ok) { setErr(`Save failed (HTTP ${r.status}): ${await r.text()}`); return; }
        // On rename, drop the old record after the new one was saved
        // successfully -- abort-and-leave-old beats abort-and-orphan.
        if (renaming) {
          try { await fetch(PRESET_ONE_URL(oldName), { method: 'DELETE' }); } catch (_) {}
        }
        closePresetEditModal();
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) {
        setErr(`Save failed: ${e}`);
      }
    });
  }
  if (delBtn) {
    delBtn.addEventListener('click', async () => {
      const oldName = _PRESET_EDIT_MODAL.originalName;
      if (!oldName) return;
      if (!confirm(`Delete preset "${oldName}"?`)) return;
      try {
        const r = await fetch(PRESET_ONE_URL(oldName), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) { alert(`Delete failed (HTTP ${r.status})`); return; }
        closePresetEditModal();
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) { alert(`Delete failed: ${e}`); }
    });
  }
})();

async function renderPresets() {
  const tbody    = document.querySelector('#presetsTable tbody');
  const cntBadge = document.getElementById('presetCount');
  const cntTab   = document.getElementById('cntPresets');
  const pagerHost = document.getElementById('presetsPager');
  const catSel    = document.getElementById('presetCategoryFilter');

  const params = new URLSearchParams();
  if (_presetListState.q)        params.set('q', _presetListState.q);
  if (_presetListState.category !== '') params.set('category', _presetListState.category);
  params.set('offset', _presetListState.offset);
  params.set('limit', PRESET_PAGE_SIZE);

  let payload = {};
  try {
    const r = await fetch(PRESET_LIST_URL + '?' + params.toString());
    if (r.ok) payload = await r.json();
  } catch (_) {}

  const presets    = payload.presets || [];
  const total      = payload.total   || 0;
  const categories = payload.categories || [];

  _presetListState.total = total;

  if (cntBadge) cntBadge.textContent = total;
  if (cntTab)   cntTab.textContent   = total;

  // Refresh the category filter dropdown without nuking the
  // operator's current selection.
  if (catSel) {
    const prev = catSel.value;
    let html = '<option value="">(all categories)</option>';
    html += `<option value="" disabled>──────────</option>`;
    for (const c of categories) {
      html += `<option value="${esc(c)}">${esc(c)}</option>`;
    }
    catSel.innerHTML = html;
    if (categories.includes(prev) || prev === '') catSel.value = prev;
  }

  if (!tbody) return;
  if (presets.length === 0) {
    const msg = total === 0
      ? 'no presets yet — save one from the Submit form'
      : 'no preset matches the current filter';
    tbody.innerHTML = `<tr><td colspan="7" style="padding:12px; color:#888; text-align:center;">${esc(msg)}</td></tr>`;
    if (pagerHost) pagerHost.innerHTML = '';
    return;
  }
  tbody.innerHTML = presets.map(p => {
    const modeBadge = p.ui_mode === 'ai'
      ? (p.ai_engine === 'simple' ? 'AI · simple' : 'AI · LLM')
      : p.ui_mode;
    return `
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:8px;"><strong>${esc(p.name)}</strong><div style="color:#888; font-size:.85em;">${esc(p.description || '')}</div></td>
      <td style="padding:8px;">${esc(p.category || '—')}</td>
      <td style="padding:8px;"><code>${esc(modeBadge)}</code></td>
      <td style="padding:8px; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(p.url)}"><a href="${esc(p.url)}" target="_blank">${esc(p.url)}</a></td>
      <td style="padding:8px; color:#888; font-size:.85em;">${esc((p.updated_at || '').slice(0, 16))}</td>
      <td style="padding:8px; color:#888; font-size:.85em;">${esc((p.last_used_at || '').slice(0, 16) || '—')}</td>
      <td>
        <div class="menu-wrap">
          <button class="action-btn" onclick="toggleMenu(this)" title="${tt('presets.th.actions','actions')}">${ICONS.moreV}</button>
          <div class="menu">
            <button class="preset-run-btn" data-name="${esc(p.name)}" title="このプリセットを実行 (POST /run)"><iconify-icon icon="lucide:play"></iconify-icon> run</button>
            <button class="preset-load-btn" data-name="${esc(p.name)}" title="Submit form に読み込む"><iconify-icon icon="lucide:download"></iconify-icon> load</button>
            <button class="preset-edit-btn" data-name="${esc(p.name)}" title="モーダルで編集 (rename / mode / goal / code …)"><iconify-icon icon="lucide:pencil"></iconify-icon> edit</button>
            <div class="divider"></div>
            <button class="danger preset-delete-btn" data-name="${esc(p.name)}" title="削除"><iconify-icon icon="lucide:trash-2"></iconify-icon> delete</button>
          </div>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Wire up per-row buttons.
  tbody.querySelectorAll('.preset-run-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      try {
        const r = await fetch(PRESET_ONE_URL(name) + '/run', { method: 'POST' });
        if (!r.ok) {
          const err = await r.text();
          alert(`Run failed (HTTP ${r.status}): ${err}`);
          return;
        }
        const job = await r.json();
        if (job && job.job_id) {
          // Attach the live panel so the operator can watch.
          if (typeof ljpAttach === 'function') ljpAttach(job.job_id);
          // Close the More menu so the user sees the live panel.
          const more = document.querySelector('#tabs .more.open');
          if (more) more.classList.remove('open');
        }
        renderPresets();
      } catch (e) { alert(`Run failed: ${e}`); }
    });
  });
  tbody.querySelectorAll('.preset-load-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      try {
        const r = await fetch(PRESET_ONE_URL(name));
        if (!r.ok) { alert(`Load failed (HTTP ${r.status})`); return; }
        const rec = await r.json();
        presetApplyToForm(rec);
        presetSetLoaded(name);
        // Switch to the Submit tab so the operator sees the loaded
        // form right away.
        const submitTab = document.querySelector('#tabs .tab[data-tab="submit"]');
        if (submitTab) submitTab.click();
        const urlInput = document.getElementById('urlInput');
        if (urlInput) urlInput.scrollIntoView({behavior: 'smooth', block: 'center'});
      } catch (e) { alert(`Load failed: ${e}`); }
    });
  });
  tbody.querySelectorAll('.preset-edit-btn').forEach(b => {
    b.addEventListener('click', () => {
      // Open the edit modal -- it handles its own fetch + populate
      // + save (including rename via PUT-new + DELETE-old) and
      // calls renderPresets() on close.
      if (typeof openPresetEditModal === 'function') {
        openPresetEditModal(b.dataset.name);
      }
    });
  });
  tbody.querySelectorAll('.preset-delete-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      if (!confirm(`Delete preset "${name}"?`)) return;
      try {
        const r = await fetch(PRESET_ONE_URL(name), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) {
          alert(`Delete failed (HTTP ${r.status})`);
          return;
        }
        renderPresets();
      } catch (e) { alert(`Delete failed: ${e}`); }
    });
  });

  // ---- pager --------------------------------------------------
  if (pagerHost) {
    const total = _presetListState.total;
    const offset = _presetListState.offset;
    const start = total ? offset + 1 : 0;
    const end   = Math.min(offset + PRESET_PAGE_SIZE, total);
    const prevDisabled = offset <= 0;
    const nextDisabled = offset + PRESET_PAGE_SIZE >= total;
    pagerHost.innerHTML = `
      <span style="color:#666;">${start}-${end} / ${total}</span>
      <button class="pill" id="presetPagerPrev" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${prevDisabled ? 'disabled' : ''}><iconify-icon icon="lucide:chevron-left"></iconify-icon> prev</button>
      <button class="pill" id="presetPagerNext" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${nextDisabled ? 'disabled' : ''}>next <iconify-icon icon="lucide:chevron-right"></iconify-icon></button>
    `;
    const prevBtn = document.getElementById('presetPagerPrev');
    const nextBtn = document.getElementById('presetPagerNext');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      _presetListState.offset = Math.max(0, offset - PRESET_PAGE_SIZE);
      renderPresets();
    });
    if (nextBtn) nextBtn.addEventListener('click', () => {
      _presetListState.offset = offset + PRESET_PAGE_SIZE;
      renderPresets();
    });
  }
}

// Wire search + category-filter inputs (debounced) so typing
// a query doesn't fire a request on every keystroke.
(function wirePresetFilters() {
  const search = document.getElementById('presetSearch');
  const cat    = document.getElementById('presetCategoryFilter');
  if (search) {
    search.addEventListener('input', () => {
      _presetListState.q = search.value;
      _presetListState.offset = 0;
      clearTimeout(_presetSearchTimer);
      _presetSearchTimer = setTimeout(renderPresets, 200);
    });
  }
  if (cat) {
    cat.addEventListener('change', () => {
      _presetListState.category = cat.value;
      _presetListState.offset = 0;
      renderPresets();
    });
  }
})();

const refreshPresetsBtn = document.getElementById('refreshPresetsBtn');
if (refreshPresetsBtn) refreshPresetsBtn.addEventListener('click', renderPresets);

document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'presets') btn.addEventListener('click', renderPresets);
});
renderPresets();


// ---- AI Engines panel ---------------------------------------------------
//
// Master-detail UI:
//   * left:  list of all engines (built-in first, then user-added)
//   * right: form for the currently-selected engine (or empty add form)
//
// State: ENGINES_STATE.records holds the latest list, .selectedSlug
// the current selection. Saves / deletes round-trip through the
// /engines REST endpoints then re-fetch the list.

const ENGINES_STATE = {
  records: [],
  selectedSlug: null,
  isNew: false,
};

async function loadEngines() {
  try {
    const r = await fetch('/engines');
    if (!r.ok) return;
    const j = await r.json();
    ENGINES_STATE.records = j.engines || [];
    const cnt = document.getElementById('engineCount');
    if (cnt) cnt.textContent = String(ENGINES_STATE.records.length);
    const tabCnt = document.getElementById('cntEngines');
    if (tabCnt) tabCnt.textContent = String(ENGINES_STATE.records.length);
  } catch (e) {
    console.error('loadEngines:', e);
  }
}

// --- 14-day token + ¥ cost chart per engine (U) ---------------------------
// Chart.js is loaded in <head>. We keep a single Chart instance and
// .destroy() before each refresh so switching engines doesn't leak
// canvases. Empty history → swap to a placeholder text.
let _engineCostChartInstance = null;

function _renderEngineCostChart(history) {
  const canvas = document.getElementById('engineCostChart');
  const empty  = document.getElementById('engineCostChartEmpty');
  if (!canvas) return;
  // Destroy any prior instance to avoid stacking.
  if (_engineCostChartInstance) {
    try { _engineCostChartInstance.destroy(); } catch (_) {}
    _engineCostChartInstance = null;
  }
  if (!history || history.length === 0) {
    canvas.style.display = 'none';
    if (empty) empty.style.display = '';
    return;
  }
  canvas.style.display = '';
  if (empty) empty.style.display = 'none';
  if (typeof Chart === 'undefined') {
    // Chart.js failed to load (offline, blocked CDN, …). Fall back to a
    // simple text summary so the operator at least sees the numbers.
    if (empty) {
      empty.style.display = '';
      const last = history[history.length - 1] || {};
      const days = history.length;
      const totJpy = history.reduce((s, r) => s + (r.cost_jpy || 0), 0);
      const totTok = history.reduce((s, r) => s + (r.prompt || 0) + (r.completion || 0), 0);
      empty.textContent = `Chart.js が読み込めません。直近 ${days} 日合計: ${totTok.toLocaleString()} tokens / ¥${totJpy.toLocaleString(undefined, {minimumFractionDigits:2})}`;
    }
    return;
  }
  const labels = history.map(r => (r.date || '').slice(5));  // MM-DD
  const promptD     = history.map(r => r.prompt || 0);
  const completionD = history.map(r => r.completion || 0);
  const costD       = history.map(r => r.cost_jpy || 0);
  _engineCostChartInstance = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: '入力 (prompt)',
          data: promptD,
          backgroundColor: 'rgba(58,92,168,0.55)',
          borderColor: 'rgba(58,92,168,1)',
          borderWidth: 1,
          stack: 'tokens',
          yAxisID: 'yTok',
        },
        {
          label: '出力 (completion)',
          data: completionD,
          backgroundColor: 'rgba(212,161,61,0.55)',
          borderColor: 'rgba(212,161,61,1)',
          borderWidth: 1,
          stack: 'tokens',
          yAxisID: 'yTok',
        },
        {
          type: 'line',
          label: 'コスト ¥',
          data: costD,
          borderColor: 'rgba(192,32,32,1)',
          backgroundColor: 'rgba(192,32,32,0.15)',
          fill: false,
          tension: 0.25,
          pointRadius: 3,
          yAxisID: 'yYen',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const v = ctx.parsed.y;
              if (ctx.dataset.yAxisID === 'yYen') {
                return `${ctx.dataset.label}: ¥${v.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`;
              }
              return `${ctx.dataset.label}: ${v.toLocaleString()} tokens`;
            },
          },
        },
      },
      scales: {
        x: { ticks: { font: { size: 10 } } },
        yTok: {
          type: 'linear', position: 'left', beginAtZero: true,
          title: { display: true, text: 'tokens', font: { size: 11 } },
          ticks: {
            font: { size: 10 },
            callback: (v) => v >= 1000 ? (v/1000).toFixed(0)+'k' : v,
          },
        },
        yYen: {
          type: 'linear', position: 'right', beginAtZero: true,
          grid: { drawOnChartArea: false },
          title: { display: true, text: '¥', font: { size: 11 } },
          ticks: { font: { size: 10 } },
        },
      },
    },
  });
}


function renderEnginesList() {
  const host = document.getElementById('enginesList');
  if (!host) return;
  if (ENGINES_STATE.records.length === 0) {
    host.innerHTML = '<div style="color:#888; padding:12px; text-align:center;">(none)</div>';
    return;
  }
  const kindBadge = (k) => {
    const colors = {
      'chat': ['#eef0ff','#3a5ca8'],
      'vision-chat': ['#fef5e7','#8a5a00'],
      'gui-agent': ['#f5edff','#5a3b8a'],
    };
    const [bg, fg] = colors[k] || ['#eee','#666'];
    return `<span style="display:inline-block; padding:0 5px; border-radius:3px; font-size:.78em; background:${bg}; color:${fg};">${esc(k)}</span>`;
  };
  // Build per-row entries plus a fleet-wide cost summary in the header.
  // ぱっぷす運用では「今日の AI 課金合計」が一番気になる数字。
  let totalToday = 0;
  let totalReqsToday = 0;
  ENGINES_STATE.records.forEach(r => {
    totalToday += (r.cost_today_jpy || 0);
    totalReqsToday += ((r.usage_today || {}).requests || 0);
  });
  host.innerHTML = ENGINES_STATE.records.map(rec => {
    const isSel = rec.slug === ENGINES_STATE.selectedSlug;
    const bg = isSel ? '#fff4d4' : '';
    const promoted = rec.promoted ? ' <span title="promoted" style="color:#d4a13d;">●</span>' : '';
    // Cost chip (U): show today's ¥ next to the row so the operator
    // can spot the expensive engine at a glance. Green at ¥0, amber
    // < ¥100, red ≥ ¥100.
    const cost = rec.cost_today_jpy || 0;
    let costColor = '#888';
    let costBg = '#f0f0f0';
    if (cost > 0) {
      costColor = cost >= 100 ? '#fff' : '#7a4a00';
      costBg    = cost >= 100 ? '#c02020' : '#fff0c0';
    } else if ((rec.cost_input_per_1m_jpy || 0) || (rec.cost_output_per_1m_jpy || 0)) {
      // Priced but no usage today — green.
      costColor = '#196b2c';
      costBg    = '#eef8ee';
    }
    const costChip = `<span class="engine-cost-chip" title="本日累計コスト" style="float:right; padding:0 6px; border-radius:8px; font-size:.72em; background:${costBg}; color:${costColor}; font-weight:600;">¥${cost.toLocaleString(undefined, {minimumFractionDigits: cost < 10 ? 2 : 0, maximumFractionDigits: 2})}</span>`;
    return `<div class="engine-row" data-slug="${esc(rec.slug)}" style="padding:6px 8px; border-radius:4px; cursor:pointer; background:${bg};">
      <div style="font-weight:600; font-size:.92em;">${esc(rec.slug)}${promoted}${costChip}</div>
      <div style="font-size:.78em; color:#666; margin-top:1px;">${kindBadge(rec.kind)} ${esc(rec.model || '')}</div>
    </div>`;
  }).join('');
  // Fleet-wide cost summary at the top of the list, if the host has a
  // sibling element we can put it in.
  const summaryEl = document.getElementById('enginesCostSummary');
  if (summaryEl) {
    summaryEl.innerHTML = `本日累計: <strong>¥${totalToday.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}</strong> (${totalReqsToday.toLocaleString()} req)`;
    summaryEl.style.color = totalToday >= 1000 ? '#c00' : (totalToday >= 100 ? '#a06000' : '#196b2c');
  }
  host.querySelectorAll('.engine-row').forEach(el => {
    el.addEventListener('click', () => {
      selectEngine(el.dataset.slug);
    });
  });
}

function selectEngine(slug) {
  ENGINES_STATE.selectedSlug = slug;
  ENGINES_STATE.isNew = false;
  const rec = ENGINES_STATE.records.find(r => r.slug === slug);
  if (!rec) return;
  fillEngineForm(rec);
  renderEnginesList();
}

function newEngineForm() {
  ENGINES_STATE.selectedSlug = null;
  ENGINES_STATE.isNew = true;
  fillEngineForm({
    slug: '', name: '', kind: 'chat', protocol: 'openai',
    endpoint: '', model: '', api_key_env: '', api_key_set: false,
    api_key_direct_set: false,
    headers: {}, timeout_s: 60, promoted: false, notes: '',
    builtin: false, created_at: '', updated_at: '',
  });
  renderEnginesList();
}

function fillEngineForm(rec) {
  const empty = document.getElementById('enginesDetailEmpty');
  const form = document.getElementById('enginesDetailForm');
  if (empty) empty.style.display = 'none';
  if (form) form.style.display = '';
  document.getElementById('engineSlug').value = rec.slug || '';
  document.getElementById('engineSlug').disabled = !ENGINES_STATE.isNew;
  document.getElementById('engineName').value = rec.name || '';
  document.getElementById('engineKind').value = rec.kind || 'chat';
  document.getElementById('engineKind').disabled = false;
  document.getElementById('engineProtocol').value = rec.protocol || 'openai';
  document.getElementById('engineProtocol').disabled = false;
  document.getElementById('engineEndpoint').value = rec.endpoint || '';
  document.getElementById('engineModel').value = rec.model || '';
  document.getElementById('engineApiKeyEnv').value = rec.api_key_env || '';
  // The direct key is intentionally never echoed back -- we just
  // surface whether one is stored. Leaving the password field empty
  // during save preserves the existing value (see api_key body
  // convention in upsert_engine).
  document.getElementById('engineApiKey').value = '';
  const directStatus = document.getElementById('engineApiKeyDirectStatus');
  if (directStatus) {
    if (rec.api_key_direct_set) {
      directStatus.textContent = '✓ direct key stored';
      directStatus.style.color = '#196b2c';
    } else {
      directStatus.textContent = '(none)';
      directStatus.style.color = '#888';
    }
  }
  const status = document.getElementById('engineApiKeyStatus');
  if (status) {
    if (!rec.api_key_env) {
      status.textContent = '(none)';
      status.style.color = '#888';
    } else if (rec.api_key_set) {
      status.textContent = '✓ env set on hub';
      status.style.color = '#196b2c';
    } else {
      status.textContent = '⚠ env not set on hub';
      status.style.color = '#c00';
    }
  }
  document.getElementById('engineTimeout').value = rec.timeout_s || 60;
  document.getElementById('engineHeaders').value = JSON.stringify(rec.headers || {}, null, 2);
  document.getElementById('enginePromoted').checked = !!rec.promoted;
  document.getElementById('engineUseForCodegen').checked = !!rec.use_for_codegen;
  { const _wa = document.getElementById('engineUseForWorkerAgent'); if (_wa) _wa.checked = !!rec.use_for_worker_agent; }
  // Daily quota (0 = unlimited). Empty input = treat as 0 for save.
  document.getElementById('engineDailyTokenBudget').value =
    (rec.daily_token_budget || 0) || '';
  document.getElementById('engineDailyRequestBudget').value =
    (rec.daily_request_budget || 0) || '';
  // Today's usage display. usage_today = {prompt, completion, requests}.
  const usage = rec.usage_today || { prompt: 0, completion: 0, requests: 0 };
  const usageEl = document.getElementById('engineUsageToday');
  if (usageEl) {
    const tt = (usage.prompt || 0) + (usage.completion || 0);
    const cap = rec.daily_token_budget || 0;
    const reqCap = rec.daily_request_budget || 0;
    const tokenLine = cap > 0
      ? `${tt.toLocaleString()} / ${cap.toLocaleString()} tokens (${Math.round(tt * 100 / cap)}%)`
      : `${tt.toLocaleString()} tokens (制限なし)`;
    const reqLine = reqCap > 0
      ? `${usage.requests} / ${reqCap} requests (${Math.round(usage.requests * 100 / reqCap)}%)`
      : `${usage.requests} requests`;
    usageEl.textContent = `今日の利用量: ${tokenLine} · ${reqLine}`;
    // Warn colour at >=90%, error colour when exceeded.
    if (cap > 0 && tt >= cap) usageEl.style.color = '#c00';
    else if (cap > 0 && tt >= cap * 0.9) usageEl.style.color = '#a06000';
    else usageEl.style.color = '#666';
  }
  // ¥ pricing inputs (U) + today's cost display.
  const costInEl  = document.getElementById('engineCostInputJpy');
  const costOutEl = document.getElementById('engineCostOutputJpy');
  if (costInEl)  costInEl.value  = (rec.cost_input_per_1m_jpy  || 0) || '';
  if (costOutEl) costOutEl.value = (rec.cost_output_per_1m_jpy || 0) || '';
  const costTodayEl = document.getElementById('engineCostToday');
  if (costTodayEl) {
    const c = rec.cost_today_jpy || 0;
    const inRate  = rec.cost_input_per_1m_jpy  || 0;
    const outRate = rec.cost_output_per_1m_jpy || 0;
    if (!inRate && !outRate) {
      costTodayEl.textContent = '本日のコスト: ¥0 (単価未設定 ─ 自前 GPU または非課金)';
      costTodayEl.style.color = '#888';
    } else {
      costTodayEl.textContent = `本日のコスト: ¥${c.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2})}`;
      costTodayEl.style.color = c > 1000 ? '#c00' : (c > 100 ? '#a06000' : '#196b2c');
    }
  }
  // 14-day chart (token bars + cost line). Lazy-load Chart.js once.
  _renderEngineCostChart(rec.cost_history || []);
  document.getElementById('engineNotes').value = rec.notes || '';
  document.getElementById('engineDeleteBtn').disabled = false;
  document.getElementById('engineDeleteBtn').title = '削除';
  const meta = document.getElementById('engineMeta');
  if (meta) {
    if (ENGINES_STATE.isNew) {
      meta.textContent = '(new engine)';
    } else {
      meta.textContent =
        `created: ${rec.created_at || '(unknown)'}\n` +
        `updated: ${rec.updated_at || '(unknown)'}`;
    }
  }
  const stat = document.getElementById('engineStatus');
  if (stat) stat.textContent = '';
}

async function saveEngine() {
  const stat = document.getElementById('engineStatus');
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) { if (stat) stat.textContent = '❌ slug required'; return; }
  let headers = {};
  try {
    const raw = document.getElementById('engineHeaders').value.trim();
    if (raw) headers = JSON.parse(raw);
  } catch (e) {
    if (stat) stat.textContent = '❌ headers must be valid JSON';
    return;
  }
  const body = {
    name: document.getElementById('engineName').value.trim(),
    kind: document.getElementById('engineKind').value,
    protocol: document.getElementById('engineProtocol').value,
    endpoint: document.getElementById('engineEndpoint').value.trim(),
    model: document.getElementById('engineModel').value.trim(),
    api_key_env: document.getElementById('engineApiKeyEnv').value.trim(),
    headers,
    timeout_s: parseInt(document.getElementById('engineTimeout').value, 10) || 60,
    promoted: document.getElementById('enginePromoted').checked,
    use_for_codegen: document.getElementById('engineUseForCodegen').checked,
    use_for_worker_agent: (document.getElementById('engineUseForWorkerAgent') || {}).checked || false,
    daily_token_budget:
      parseInt(document.getElementById('engineDailyTokenBudget').value, 10) || 0,
    daily_request_budget:
      parseInt(document.getElementById('engineDailyRequestBudget').value, 10) || 0,
    // U: ¥/1M pricing. Empty = 0 = no cost calculation.
    cost_input_per_1m_jpy:
      parseFloat(document.getElementById('engineCostInputJpy').value) || 0,
    cost_output_per_1m_jpy:
      parseFloat(document.getElementById('engineCostOutputJpy').value) || 0,
    notes: document.getElementById('engineNotes').value,
  };
  // Direct API key: only include in body if the user typed something
  // OR explicitly clicked Clear (which leaves a sentinel). Skipping
  // the key altogether means "keep current value" on the hub side.
  const directInput = document.getElementById('engineApiKey');
  const directVal = directInput.value;
  if (directInput.dataset.cleared === '1') {
    body.api_key = '';   // explicit wipe
  } else if (directVal) {
    body.api_key = directVal;
  }
  // Reset the cleared flag so next save doesn't re-wipe unintentionally.
  directInput.dataset.cleared = '';
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (stat) stat.textContent = `❌ ${r.status}: ${t.slice(0, 200)}`;
      return;
    }
    if (stat) stat.textContent = '✓ saved';
    ENGINES_STATE.isNew = false;
    ENGINES_STATE.selectedSlug = slug;
    await loadEngines();
    renderEnginesList();
    const rec = ENGINES_STATE.records.find(r => r.slug === slug);
    if (rec) fillEngineForm(rec);
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

async function deleteEngine() {
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) return;
  if (!confirm(`engine "${slug}" を削除しますか?`)) return;
  const stat = document.getElementById('engineStatus');
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug), {method: 'DELETE'});
    if (!r.ok) {
      const t = await r.text();
      if (stat) stat.textContent = `❌ ${r.status}: ${t.slice(0, 200)}`;
      return;
    }
    ENGINES_STATE.selectedSlug = null;
    ENGINES_STATE.isNew = false;
    await loadEngines();
    renderEnginesList();
    document.getElementById('enginesDetailEmpty').style.display = '';
    document.getElementById('enginesDetailForm').style.display = 'none';
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

async function testEngine() {
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) return;
  const stat = document.getElementById('engineStatus');
  if (stat) stat.textContent = '⏳ testing...';
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug) + '/test', {method: 'POST'});
    const j = await r.json();
    if (j.ok) {
      if (stat) stat.textContent = `✓ reachable (${j.elapsed_ms}ms, HTTP ${j.status_code || 200})`;
    } else {
      if (stat) stat.textContent = `❌ ${j.error || 'failed'} (${j.elapsed_ms}ms)`;
    }
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

(function wireEngines() {
  const newBtn = document.getElementById('enginesNewBtn');
  const refreshBtn = document.getElementById('enginesRefreshBtn');
  const saveBtn = document.getElementById('engineSaveBtn');
  const delBtn = document.getElementById('engineDeleteBtn');
  const testBtn = document.getElementById('engineTestBtn');
  const clearKeyBtn = document.getElementById('engineApiKeyClearBtn');
  if (newBtn) newBtn.addEventListener('click', newEngineForm);
  if (refreshBtn) refreshBtn.addEventListener('click', async () => {
    await loadEngines();
    renderEnginesList();
  });
  if (saveBtn) saveBtn.addEventListener('click', saveEngine);
  if (delBtn) delBtn.addEventListener('click', deleteEngine);
  if (testBtn) testBtn.addEventListener('click', testEngine);
  if (clearKeyBtn) clearKeyBtn.addEventListener('click', () => {
    // Marks the direct-key field for explicit wipe on next save.
    // We don't fire the PUT immediately so the operator can pair it
    // with other edits and review before committing.
    const inp = document.getElementById('engineApiKey');
    inp.value = '';
    inp.dataset.cleared = '1';
    const ds = document.getElementById('engineApiKeyDirectStatus');
    if (ds) {
      ds.textContent = '(will be cleared on save)';
      ds.style.color = '#c00';
    }
  });
})();

// Initial load -- the count badge needs to be populated even before
// the operator opens the AI Engines tab.
loadEngines().then(() => renderEnginesList());

