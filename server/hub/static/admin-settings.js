// ---- settings panel (UI defaults + hub toggles) --------------------------

const SETTINGS_URL = '/settings';
const UI_DEFAULTS_KEY = 'paprika.ui.defaults';

// Built-in fallback defaults. These match the values hardcoded in the
// Submit form's HTML, so resetting really means "go back to the
// default-default".
const UI_DEFAULTS_FALLBACK = {
  defaultMode: 'fetch',
  llmMaxAttempts: 3,
  llmAttemptTimeout: 86400,
  llmGoal: '',                // empty = use DEFAULT_CRAWL_GOAL
  llmHostDedup: true,
  codeTimeout: 180,
};

function loadUiDefaults() {
  try {
    const raw = localStorage.getItem(UI_DEFAULTS_KEY);
    if (!raw) return { ...UI_DEFAULTS_FALLBACK };
    const parsed = JSON.parse(raw);
    return { ...UI_DEFAULTS_FALLBACK, ...parsed };
  } catch (e) {
    return { ...UI_DEFAULTS_FALLBACK };
  }
}

function saveUiDefaults(values) {
  try { localStorage.setItem(UI_DEFAULTS_KEY, JSON.stringify(values)); }
  catch (e) {}
}

// Apply current UI defaults to the Submit form. Called at page load
// and right after the operator saves new defaults so the change is
// instantly visible (no reload needed).
function applyUiDefaultsToSubmit() {
  const v = loadUiDefaults();
  // Default mode -- update the radio + visuals. Migrate legacy "llm"
  // (pre-AI-tab-rename) to "ai" so operators with a saved default
  // don't end up with no mode selected.
  if (v.defaultMode === 'llm') v.defaultMode = 'ai';
  const radio = document.querySelector(`input[name="mode"][value="${v.defaultMode}"]`);
  if (radio) {
    radio.checked = true;
    if (typeof syncSubmitMode === 'function') syncSubmitMode();
  }
  const m = document.getElementById('maxAttempts');
  if (m) m.value = v.llmMaxAttempts;
  const t = document.getElementById('attemptTimeout');
  if (t) t.value = v.llmAttemptTimeout;
  const g = document.getElementById('goalInput');
  if (g && !g.value) g.value = v.llmGoal || '';  // don't clobber operator edits mid-session
  const ct = document.getElementById('codeTimeout');
  if (ct) ct.value = v.codeTimeout;
  const dd = document.getElementById('llmHostDedup');
  if (dd) dd.checked = !!v.llmHostDedup;
}

function flashSavedHint() {
  const h = document.getElementById('settingsSavedHint');
  if (!h) return;
  h.style.opacity = '1';
  setTimeout(() => { h.style.opacity = '0'; }, 1200);
}

async function loadSettingsPanel() {
  // (A) UI defaults from localStorage
  const v = loadUiDefaults();
  // Same legacy "llm" -> "ai" migration as in applyUiDefaultsToSubmit
  // so the Settings panel shows the correct value rather than dropping
  // through to the <select>'s first option.
  if (v.defaultMode === 'llm') v.defaultMode = 'ai';
  document.getElementById('setDefaultMode').value      = v.defaultMode;
  document.getElementById('setLlmMaxAttempts').value   = v.llmMaxAttempts;
  document.getElementById('setLlmTimeout').value       = v.llmAttemptTimeout;
  document.getElementById('setLlmHostDedup').checked   = !!v.llmHostDedup;
  document.getElementById('setCodeTimeout').value      = v.codeTimeout;
  document.getElementById('setLlmGoal').value          = v.llmGoal || '';
  // (B + C) Hub settings + system info from server
  try {
    const r = await fetch(SETTINGS_URL);
    if (r.ok) {
      const d = await r.json();
      const hub = d.values || {};
      document.getElementById('setSkillAutoExtract').checked      = !!hub.skill_auto_extract_enabled;
      document.getElementById('setConventionAutoExtract').checked = !!hub.convention_auto_extract_enabled;
      const _aeEl = document.getElementById('setAutoEscalate');
      if (_aeEl) _aeEl.checked = !!hub.auto_escalate_enabled;
      document.getElementById('setSkillTopK').value               = hub.skill_retrieval_top_k ?? 3;
      document.getElementById('setMinAssetSize').value            = hub.min_asset_size_bytes ?? 0;
      // V: URL blacklist textarea
      const _blEl = document.getElementById('setAssetUrlBlacklist');
      if (_blEl) _blEl.value = hub.asset_url_blacklist ?? '';
      // Egress proxy pool textarea
      const _ppEl = document.getElementById('setProxyPool');
      if (_ppEl) _ppEl.value = hub.proxy_pool ?? '';
      // Fetch defaults
      document.getElementById('setFetchWait').value         = hub.fetch_wait_seconds       ?? 20;
      document.getElementById('setFetchSettle').value       = hub.fetch_settle_seconds     ?? 0;
      document.getElementById('setFetchIdle').value         = hub.fetch_idle_seconds       ?? 3;
      document.getElementById('setFetchMaxWait').value      = hub.fetch_max_wait_seconds   ?? 60;
      document.getElementById('setFetchScroll').checked     = !!hub.fetch_scroll;
      document.getElementById('setFetchScrollStep').value   = hub.fetch_scroll_step        ?? 50;
      document.getElementById('setFetchScrollMax').value    = hub.fetch_scroll_max         ?? 3000;
      document.getElementById('setFetchScrollEarly').value  = hub.fetch_scroll_early_after ?? 5;
      document.getElementById('setFetchPostClick').value    = hub.fetch_post_click_seconds ?? 5;
      // Codegen web_search tool. ?? '' so an empty-but-present value
      // renders an empty field (= operator turned the tool off) rather
      // than the placeholder.
      const sxEl = document.getElementById('setSearxngUrl');
      if (sxEl) sxEl.value = hub.searxng_url ?? '';
      const sxTo = document.getElementById('setSearxngTimeout');
      if (sxTo) sxTo.value = hub.searxng_timeout_s ?? 15;
      const sxMc = document.getElementById('setWebSearchMaxCalls');
      if (sxMc) sxMc.value = hub.web_search_max_calls ?? 5;
      // Shared field helpers for the MariaDB / S3 sections below.
      const _setVal = (id, v) => { const e = document.getElementById(id); if (e) e.value = v ?? ''; };
      // Secret fields are redacted server-side (GET never returns them).
      // Leave the field blank and signal whether one is stored via the
      // placeholder; the save path omits a blank field so "save without
      // retyping" keeps the existing secret.
      const _setSecretPw = (id, isSet) => {
        const e = document.getElementById(id);
        if (!e) return;
        e.value = '';
        e.placeholder = isSet ? '(設定済み — 変更時のみ入力)' : '(未設定)';
      };
      const _secretsSet = d.secrets_set || {};

      // ---- Reasoning Judge ----
      const rjMode = hub.reasoning_judge_mode || 'off';
      const rjEngine = hub.reasoning_judge_engine || '';
      const rjModeEl = document.getElementById('setReasoningJudgeMode');
      if (rjModeEl) rjModeEl.value = rjMode;
      // Populate engine dropdown from engines list
      _populateReasoningJudgeEngines(rjEngine);

      // ---- MariaDB ----
      _setVal('setMariadbHost', hub.mariadb_host);
      const _mdbPort = document.getElementById('setMariadbPort');
      if (_mdbPort) _mdbPort.value = hub.mariadb_port || 3306;
      _setVal('setMariadbDatabase', hub.mariadb_database || 'paprika');
      _setVal('setMariadbUsername', hub.mariadb_username);
      _setSecretPw('setMariadbPassword', !!_secretsSet.mariadb_password);
      // MariaDB status banner
      const mdbSt = d.mariadb_status || {};
      _updateMariadbStatusBanner(mdbSt);

      // Show migration section if MariaDB host is configured
      const migSec = document.getElementById('mariadbMigrationSection');
      if (migSec) {
        if ((hub.mariadb_host || '').trim() && (hub.mariadb_username || '').trim()) {
          migSec.style.display = 'block';
          mdbRefreshTableCounts();
        } else {
          migSec.style.display = 'none';
        }
      }

      // ---- S3 / MinIO ----
      const _s3en = document.getElementById('setS3Enabled');
      if (_s3en) _s3en.checked = !!hub.s3_enabled;
      _setVal('setS3Endpoint', hub.s3_endpoint);
      _setVal('setS3Bucket', hub.s3_bucket || 'paprika');
      _setVal('setS3Prefix', hub.s3_prefix || 'jobs');
      _setVal('setS3Region', hub.s3_region || 'us-east-1');
      _setVal('setS3AccessKey', hub.s3_access_key);
      _setSecretPw('setS3SecretKey', !!_secretsSet.s3_secret_key);
      _updateS3StatusBanner(d.s3_status || {});

      // ---- Worker salvage SSH ----
      const _slvEn = document.getElementById('setSalvageEnabled');
      if (_slvEn) _slvEn.checked = !!hub.salvage_enabled;
      _setVal('setWorkerSshUser', hub.worker_ssh_user || 'root');
      const _wsPort = document.getElementById('setWorkerSshPort');
      if (_wsPort) _wsPort.value = hub.worker_ssh_port || 22;
      _setVal('setWorkerSshKeyPath', hub.worker_ssh_key_path);
      const _wsKeyState = document.getElementById('setWorkerSshKeyState');
      if (_wsKeyState) _wsKeyState.textContent = _secretsSet.worker_ssh_key_pem
        ? _slvT('salvage.key.uploaded', '✓ 鍵アップロード済み（変更時のみ再アップロード）')
        : _slvT('salvage.key.none', '（未アップロード）');

      const sys = d.system || {};
      const tbody = document.getElementById('setSystemInfoBody');
      if (tbody) {
        const rows = [
          ['codegen LLM URL',        sys.codegen_llm_url],
          ['codegen model',          sys.codegen_model],
          ['skill distill LLM URL',  sys.skill_distill_llm_url],
          ['skill distill model',    sys.skill_distill_model],
          ['skill retrieval URL',    sys.skill_retrieval_llm_url],
          ['skill retrieval model',  sys.skill_retrieval_model],
          ['convention distill URL', sys.convention_distill_llm_url],
          ['convention distill model', sys.convention_distill_model],
          ['data dir',               sys.data_dir],
          ['storage dir',            sys.storage_dir],
          ['store',                  sys.store],
        ];
        tbody.innerHTML = rows.map(([k, v]) =>
          `<tr><td style="padding:3px 8px; color:#666; white-space:nowrap;">${esc(k)}</td><td style="padding:3px 8px;"><code>${esc(v || '')}</code></td></tr>`
        ).join('');
      }
    }
  } catch (e) {}
}

async function saveSettingsUi() {
  const v = {
    defaultMode:      document.getElementById('setDefaultMode').value,
    llmMaxAttempts:   parseInt(document.getElementById('setLlmMaxAttempts').value, 10) || 3,
    llmAttemptTimeout:parseInt(document.getElementById('setLlmTimeout').value, 10) || 86400,
    llmHostDedup:     document.getElementById('setLlmHostDedup').checked,
    codeTimeout:      parseInt(document.getElementById('setCodeTimeout').value, 10) || 180,
    llmGoal:          document.getElementById('setLlmGoal').value,
  };
  saveUiDefaults(v);
  // Mirror to the Submit form right away.
  applyUiDefaultsToSubmit();
  flashSavedHint();
}

function resetSettingsUi() {
  saveUiDefaults({ ...UI_DEFAULTS_FALLBACK });
  loadSettingsPanel();
  applyUiDefaultsToSubmit();
  flashSavedHint();
}

// i18n helper for dynamic salvage status text — translate via i18next when
// loaded, else fall back to the inline Japanese (CDN down / pre-init).
function _slvT(key, fallback) {
  try {
    return (window.i18next && window.i18next.t)
      ? window.i18next.t(key, { defaultValue: fallback }) : fallback;
  } catch (_) { return fallback; }
}

// Worker salvage SSH (server/hub/_salvage.py). Plain PUT /settings of the
// three worker_ssh_* keys; no secret redaction (the key is a *path*, not
// the key material). The actual private key lives in the hub container at
// that path; operator provisions it out-of-band.
async function saveSettingsWorkerSsh() {
  const stEl = document.getElementById('setWorkerSshStatus');
  if (stEl) { stEl.textContent = ''; stEl.style.color = ''; }
  const body = {
    salvage_enabled: !!document.getElementById('setSalvageEnabled')?.checked,
    worker_ssh_user: (document.getElementById('setWorkerSshUser').value || '').trim() || 'root',
    worker_ssh_port: parseInt(document.getElementById('setWorkerSshPort').value, 10) || 22,
    worker_ssh_key_path: (document.getElementById('setWorkerSshKeyPath').value || '').trim(),
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (stEl) { stEl.textContent = _slvT('salvage.save.failed', '保存失敗: ') + t.slice(0, 120); stEl.style.color = '#c0392b'; }
      return;
    }
    if (stEl) { stEl.textContent = _slvT('salvage.save.ok', '✓ 保存しました (全ハブへ伝播)'); stEl.style.color = '#196b2c'; }
    flashSavedHint();
  } catch (e) {
    if (stEl) { stEl.textContent = _slvT('salvage.save.failed', '保存失敗: ') + (e.message || e); stEl.style.color = '#c0392b'; }
  }
}

// Upload an SSH private key PEM and store it (secret) in settings ->
// worker_ssh_key_pem. Shared cross-hub via settings write-through; each hub
// materialises it to a local 0600 file (server/hub/_salvage._materialize_key)
// so SSH salvage works fleet-wide from a single upload.
async function uploadWorkerSshKey(file) {
  const stEl = document.getElementById('setWorkerSshStatus');
  const keyStateEl = document.getElementById('setWorkerSshKeyState');
  if (!file) return;
  let pem;
  try {
    pem = await file.text();
  } catch (e) {
    if (stEl) { stEl.textContent = _slvT('salvage.key.readfail', '鍵読込失敗: ') + (e.message || e); stEl.style.color = '#c0392b'; }
    return;
  }
  if (!pem || pem.indexOf('PRIVATE KEY') === -1) {
    if (stEl) { stEl.textContent = _slvT('salvage.key.notpem', '鍵が PEM 形式ではないようです（PRIVATE KEY が見つからない）'); stEl.style.color = '#c0392b'; }
    return;
  }
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ worker_ssh_key_pem: pem }),
    });
    if (!r.ok) {
      const t = await r.text();
      if (stEl) { stEl.textContent = _slvT('salvage.key.uploadfail', '鍵アップロード失敗: ') + t.slice(0, 120); stEl.style.color = '#c0392b'; }
      return;
    }
    if (stEl) { stEl.textContent = _slvT('salvage.key.uploadok', '✓ 鍵をアップロード（全ハブへ伝播）'); stEl.style.color = '#196b2c'; }
    if (keyStateEl) keyStateEl.textContent = _slvT('salvage.key.uploaded', '✓ 鍵アップロード済み（変更時のみ再アップロード）');
    flashSavedHint();
  } catch (e) {
    if (stEl) { stEl.textContent = _slvT('salvage.key.uploadfail', '鍵アップロード失敗: ') + (e.message || e); stEl.style.color = '#c0392b'; }
  }
}

async function saveSettingsHub() {
  const errEl = document.getElementById('setHubErr');
  if (errEl) errEl.textContent = '';
  const body = {
    skill_auto_extract_enabled:      document.getElementById('setSkillAutoExtract').checked,
    convention_auto_extract_enabled: document.getElementById('setConventionAutoExtract').checked,
    auto_escalate_enabled:           document.getElementById('setAutoEscalate')?.checked ?? false,
    skill_retrieval_top_k:           parseInt(document.getElementById('setSkillTopK').value, 10) || 3,
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function saveSettingsAssetCapture() {
  const errEl = document.getElementById('setAssetErr');
  if (errEl) errEl.textContent = '';
  const raw = parseHumanBytes(document.getElementById('setMinAssetSize').value);
  const v = (Number.isFinite(raw) && raw >= 0) ? raw : 0;
  // V: URL blacklist (newline-separated string, persisted as-is).
  const blEl = document.getElementById('setAssetUrlBlacklist');
  const blacklist = blEl ? (blEl.value || '').trim() : '';
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        min_asset_size_bytes: v,
        asset_url_blacklist: blacklist,
      }),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function saveSettingsProxyPool() {
  const errEl = document.getElementById('setProxyErr');
  if (errEl) errEl.textContent = '';
  const ppEl = document.getElementById('setProxyPool');
  const proxy_pool = ppEl ? (ppEl.value || '').trim() : '';
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ proxy_pool }),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

function _num(id, fallback) {
  const raw = document.getElementById(id).value;
  const n = parseFloat(raw);
  if (isNaN(n) || n < 0) return fallback;
  return n;
}

async function saveSettingsFetchDefaults() {
  const errEl = document.getElementById('setFetchErr');
  if (errEl) errEl.textContent = '';
  const body = {
    fetch_wait_seconds:        Math.round(_num('setFetchWait', 20)),
    fetch_settle_seconds:      _num('setFetchSettle', 0),
    fetch_idle_seconds:        _num('setFetchIdle', 3),
    fetch_max_wait_seconds:    _num('setFetchMaxWait', 60),
    fetch_scroll:              document.getElementById('setFetchScroll').checked,
    fetch_scroll_step:         Math.round(_num('setFetchScrollStep', 50)),
    fetch_scroll_max:          Math.round(_num('setFetchScrollMax', 3000)),
    fetch_scroll_early_after:  _num('setFetchScrollEarly', 5),
    fetch_post_click_seconds:  _num('setFetchPostClick', 5),
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function saveSettingsWebSearch() {
  // Persist the SearXNG endpoint, timeout, and per-attempt call cap
  // that drive the Coder LLM's web_search tool. Empty URL or 0 calls
  // disables the tool (see server/hub/web_search.is_enabled).
  const errEl = document.getElementById('setWebSearchErr');
  if (errEl) errEl.textContent = '';
  const urlRaw  = (document.getElementById('setSearxngUrl').value || '').trim();
  const timeRaw = parseFloat(document.getElementById('setSearxngTimeout').value);
  const callRaw = parseInt(document.getElementById('setWebSearchMaxCalls').value, 10);
  const body = {
    searxng_url:           urlRaw,
    searxng_timeout_s:     (Number.isFinite(timeRaw) && timeRaw > 0) ? timeRaw : 15,
    web_search_max_calls:  (Number.isFinite(callRaw) && callRaw >= 0) ? callRaw : 5,
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

// ---- Status banner helpers ----

function _updateMariadbStatusBanner(st) {
  const banner = document.getElementById('mariadbStatusBanner');
  if (!banner) return;
  if (!st || (!st.connected && !st.host)) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (st.connected) {
    banner.style.background = '#e6f7e9';
    banner.style.border = '1px solid #7ab68a';
    banner.style.color = '#196b2c';
    const storeLabel = st.store_kind === 'mariadb' ? 'プライマリストア' : 'メタデータのみ';
    banner.innerHTML = '<iconify-icon icon="lucide:check-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>接続中</strong>: '
      + esc(st.host || '') + ':' + (st.port || 3306) + '/' + esc(st.database || '')
      + ' <span style="margin-left:8px; padding:2px 8px; border-radius:4px; background:#d4edda; font-size:.85em;">'
      + esc(st.version || '') + '</span>'
      + ' <span style="margin-left:6px; padding:2px 8px; border-radius:4px; background:#cce5ff; color:#004085; font-size:.85em;">'
      + esc(storeLabel) + '</span>';
  } else {
    banner.style.background = '#fff3e0';
    banner.style.border = '1px solid #e8c97a';
    banner.style.color = '#7a5a14';
    banner.innerHTML = '<iconify-icon icon="lucide:alert-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>未接続</strong>: MariaDB に接続できません。Redis / ファイルで動作中。';
  }
}

function _updateS3StatusBanner(st) {
  const banner = document.getElementById('s3StatusBanner');
  if (!banner) return;
  // Hidden entirely when the S3 mirror is disabled (= local disk only).
  if (!st || !st.enabled) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (st.connected) {
    banner.style.background = '#e6f7e9';
    banner.style.border = '1px solid #7ab68a';
    banner.style.color = '#196b2c';
    banner.innerHTML = '<iconify-icon icon="lucide:check-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>接続中</strong>: '
      + esc(st.endpoint || '(既定エンドポイント)')
      + ' <span style="margin-left:8px; padding:2px 8px; border-radius:4px; background:#d4edda; font-size:.85em;">bucket=' + esc(st.bucket || '') + '</span>'
      + ' <span style="margin-left:6px; padding:2px 8px; border-radius:4px; background:#cce5ff; color:#004085; font-size:.85em;">prefix=' + esc(st.prefix || '') + '</span>';
  } else {
    banner.style.background = '#fdecea';
    banner.style.border = '1px solid #e0a3a0';
    banner.style.color = '#a3261f';
    banner.innerHTML = '<iconify-icon icon="lucide:alert-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>未接続</strong>: S3 / MinIO に接続できません'
      + (st.error ? ' (' + esc(st.error) + ')' : '') + '。設定を確認してください。';
  }
}

async function resetFetchDefaults() {
  // Push the in-code defaults back to the server.
  const body = {
    fetch_wait_seconds: 20,
    fetch_settle_seconds: 0,
    fetch_idle_seconds: 3,
    fetch_max_wait_seconds: 60,
    fetch_scroll: false,
    fetch_scroll_step: 50,
    fetch_scroll_max: 3000,
    fetch_scroll_early_after: 5,
    fetch_post_click_seconds: 5,
  };
  try {
    await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) {}
  loadSettingsPanel();
  flashSavedHint();
}

// ---- Reasoning Judge ----
async function _populateReasoningJudgeEngines(currentSlug) {
  const sel = document.getElementById('setReasoningJudgeEngine');
  if (!sel) return;
  // Keep the first option (fallback)
  sel.innerHTML = '<option value="">(未設定 — env fallback)</option>';
  try {
    const r = await fetch('/engines');
    const data = await r.json();
    const engines = Array.isArray(data) ? data : (data.engines || []);
    engines.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.slug;
      opt.textContent = `${e.slug} (${e.model || e.name})`;
      if (e.slug === currentSlug) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (_) {}
}

async function saveSettingsReasoningJudge() {
  const statusEl = document.getElementById('setReasoningJudgeStatus');
  const mode = (document.getElementById('setReasoningJudgeMode')?.value || 'off').trim();
  const engine = (document.getElementById('setReasoningJudgeEngine')?.value || '').trim();
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        reasoning_judge_mode: mode,
        reasoning_judge_engine: engine,
      }),
    });
    if (r.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = '保存しました'; setTimeout(() => statusEl.textContent = '', 3000); }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `エラー: ${r.status}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = String(e); }
  }
}

// ---- MariaDB ----
async function saveSettingsMariadb() {
  const statusEl = document.getElementById('setMariadbStatus');
  if (statusEl) statusEl.textContent = '';
  const body = {
    mariadb_host: (document.getElementById('setMariadbHost')?.value || '').trim(),
    mariadb_port: parseInt(document.getElementById('setMariadbPort')?.value, 10) || 3306,
    mariadb_database: (document.getElementById('setMariadbDatabase')?.value || 'paprika').trim(),
    mariadb_username: (document.getElementById('setMariadbUsername')?.value || '').trim(),
  };
  // Blank password field => keep the stored one (it's redacted from
  // GET /settings and never re-populated, so omit it from the PUT).
  const _mdbPw = document.getElementById('setMariadbPassword')?.value || '';
  if (_mdbPw) body.mariadb_password = _mdbPw;
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = '保存しました'; setTimeout(() => statusEl.textContent = '', 3000); }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `エラー: ${r.status}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = String(e); }
  }
}

async function testMariadbConnection() {
  const statusEl = document.getElementById('setMariadbStatus');
  const btn = document.getElementById('setMariadbTestBtn');
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> テスト中…';
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  const body = {
    host: (document.getElementById('setMariadbHost')?.value || '').trim(),
    port: parseInt(document.getElementById('setMariadbPort')?.value, 10) || 3306,
    database: (document.getElementById('setMariadbDatabase')?.value || 'paprika').trim(),
    username: (document.getElementById('setMariadbUsername')?.value || '').trim(),
    password: document.getElementById('setMariadbPassword')?.value || '',
  };
  try {
    const r = await fetch('/settings/mariadb/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    let d;
    try { d = await r.json(); } catch { d = { message: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = `✓ ${d.message} (${d.version})`; }
      // Show migration section on successful connection
      const migSec = document.getElementById('mariadbMigrationSection');
      if (migSec) migSec.style.display = 'block';
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.message}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = '接続失敗: ' + e.message; }
  } finally {
    if (btn) btn.innerHTML = origLabel;
  }
}

// ---- S3 / MinIO ----
async function saveSettingsS3() {
  const statusEl = document.getElementById('setS3Status');
  if (statusEl) statusEl.textContent = '';
  const body = {
    s3_enabled: !!document.getElementById('setS3Enabled')?.checked,
    s3_endpoint: (document.getElementById('setS3Endpoint')?.value || '').trim(),
    s3_bucket: (document.getElementById('setS3Bucket')?.value || 'paprika').trim(),
    s3_prefix: (document.getElementById('setS3Prefix')?.value || 'jobs').trim(),
    s3_region: (document.getElementById('setS3Region')?.value || 'us-east-1').trim(),
    s3_access_key: (document.getElementById('setS3AccessKey')?.value || '').trim(),
  };
  // Blank secret field => keep the stored one (redacted from GET, never
  // re-populated, so omit from the PUT).
  const _sk = document.getElementById('setS3SecretKey')?.value || '';
  if (_sk) body.s3_secret_key = _sk;
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = '保存しました'; setTimeout(() => statusEl.textContent = '', 3000); }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `エラー: ${r.status}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = String(e); }
  }
}

async function testS3Connection() {
  const statusEl = document.getElementById('setS3Status');
  const btn = document.getElementById('setS3TestBtn');
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> テスト中…';
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  const body = {
    endpoint: (document.getElementById('setS3Endpoint')?.value || '').trim(),
    bucket: (document.getElementById('setS3Bucket')?.value || 'paprika').trim(),
    prefix: (document.getElementById('setS3Prefix')?.value || 'jobs').trim(),
    region: (document.getElementById('setS3Region')?.value || 'us-east-1').trim(),
    access_key: (document.getElementById('setS3AccessKey')?.value || '').trim(),
    secret_key: document.getElementById('setS3SecretKey')?.value || '',
  };
  try {
    const r = await fetch('/settings/s3/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    let d;
    try { d = await r.json(); } catch { d = { message: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = `✓ ${d.message}`; }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.message}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = '接続失敗: ' + e.message; }
  } finally {
    if (btn) btn.innerHTML = origLabel;
  }
}

// ---- MariaDB Data Migration ----

async function mdbCreateSchema() {
  const statusEl = document.getElementById('mdbSchemaStatus');
  const btn = document.getElementById('mdbSchemaBtn');
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> 作成中…'; }
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  try {
    const r = await fetch('/settings/mariadb/schema', { method: 'POST' });
    let d;
    try { d = await r.json(); } catch { d = { detail: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = `✓ ${d.tables.length} テーブル作成済み`; }
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.detail || d.message || 'エラー'}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = 'エラー: ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origLabel; }
  }
}

async function mdbMigrate(category) {
  // Map category -> status span suffix (capitalised first letter)
  const capMap = { jobs: 'Jobs', hosts: 'Hosts', visited_urls: 'Visited', skills: 'Skills', conventions: 'Conventions', engines: 'Engines', presets: 'Presets' };
  const cap = capMap[category] || category;
  const statusEl = document.getElementById('mdbMigrate' + cap + 'Status');
  const btnMap = { jobs: 'mdbMigrateJobsBtn', hosts: 'mdbMigrateHostsBtn', visited_urls: 'mdbMigrateVisitedBtn', skills: 'mdbMigrateSkillsBtn', conventions: 'mdbMigrateConventionsBtn', engines: 'mdbMigrateEnginesBtn', presets: 'mdbMigratePresetsBtn' };
  const btn = document.getElementById(btnMap[category]);
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> 移行中…'; }
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  try {
    const r = await fetch('/settings/mariadb/migrate/' + category, { method: 'POST' });
    let d;
    try { d = await r.json(); } catch { d = { detail: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) {
        statusEl.style.color = '#196b2c';
        statusEl.textContent = `✓ ${d.migrated} 件移行 / ${d.skipped} 件スキップ (全 ${d.total || d.total_hosts || '?'})`;
        if (d.purged > 0) {
          statusEl.textContent += ` / 元データ ${d.purged} 件削除`;
        }
        if (d.errors && d.errors.length > 0) {
          statusEl.textContent += ` ⚠ ${d.errors.length} 件エラー`;
        }
      }
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.detail || d.message || 'エラー'}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = 'エラー: ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origLabel; }
  }
}

async function mdbRefreshTableCounts() {
  const el = document.getElementById('mdbTableCounts');
  if (!el) return;
  try {
    const r = await fetch('/settings/mariadb/tables');
    let d;
    try { d = await r.json(); } catch { d = {}; }
    if (d.ok && d.tables) {
      const rows = Object.entries(d.tables).map(([name, count]) => {
        const label = count < 0 ? '<span style="color:#a00">未作成</span>' : count.toLocaleString() + ' 行';
        return `<span style="margin-right:14px;"><b>${name}</b>: ${label}</span>`;
      }).join('');
      el.innerHTML = '📊 ' + rows;
      el.style.display = 'block';
    }
  } catch (e) { /* ignore */ }
}

(function wireSettings() {
  const saveUi = document.getElementById('setSaveUiBtn');
  const resetUi = document.getElementById('setResetUiBtn');
  const saveHub = document.getElementById('setSaveHubBtn');
  const saveAsset = document.getElementById('setSaveAssetBtn');
  const saveProxy = document.getElementById('setSaveProxyBtn');
  const saveFetch = document.getElementById('setSaveFetchBtn');
  const resetFetch = document.getElementById('setResetFetchBtn');
  if (saveUi) saveUi.addEventListener('click', saveSettingsUi);
  if (resetUi) resetUi.addEventListener('click', resetSettingsUi);
  if (saveHub) saveHub.addEventListener('click', saveSettingsHub);
  if (saveAsset) saveAsset.addEventListener('click', saveSettingsAssetCapture);
  if (saveProxy) saveProxy.addEventListener('click', saveSettingsProxyPool);
  if (saveFetch) saveFetch.addEventListener('click', saveSettingsFetchDefaults);
  if (resetFetch) resetFetch.addEventListener('click', resetFetchDefaults);
  const saveWs = document.getElementById('setSaveWebSearchBtn');
  if (saveWs) saveWs.addEventListener('click', saveSettingsWebSearch);
  // Reasoning Judge
  const saveRj = document.getElementById('setSaveReasoningJudgeBtn');
  if (saveRj) saveRj.addEventListener('click', saveSettingsReasoningJudge);
  // MariaDB
  const saveMdb = document.getElementById('setSaveMariadbBtn');
  if (saveMdb) saveMdb.addEventListener('click', saveSettingsMariadb);
  const testMdb = document.getElementById('setMariadbTestBtn');
  if (testMdb) testMdb.addEventListener('click', testMariadbConnection);
  // S3 / MinIO
  const saveS3 = document.getElementById('setSaveS3Btn');
  if (saveS3) saveS3.addEventListener('click', saveSettingsS3);
  const testS3 = document.getElementById('setS3TestBtn');
  if (testS3) testS3.addEventListener('click', testS3Connection);
  // Worker salvage SSH (サルベージ用)
  const saveWSsh = document.getElementById('setSaveWorkerSshBtn');
  if (saveWSsh) saveWSsh.addEventListener('click', saveSettingsWorkerSsh);
  const wsKeyFile = document.getElementById('setWorkerSshKeyFile');
  if (wsKeyFile) wsKeyFile.addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) uploadWorkerSshKey(f);
  });
  const s3SecToggle = document.getElementById('setS3SecretToggle');
  if (s3SecToggle) s3SecToggle.addEventListener('click', () => {
    const sk = document.getElementById('setS3SecretKey');
    if (sk) sk.type = sk.type === 'password' ? 'text' : 'password';
  });
  // MariaDB Migration
  const mdbSchema = document.getElementById('mdbSchemaBtn');
  if (mdbSchema) mdbSchema.addEventListener('click', mdbCreateSchema);
  const mdbJobs = document.getElementById('mdbMigrateJobsBtn');
  if (mdbJobs) mdbJobs.addEventListener('click', () => mdbMigrate('jobs'));
  const mdbHosts = document.getElementById('mdbMigrateHostsBtn');
  if (mdbHosts) mdbHosts.addEventListener('click', () => mdbMigrate('hosts'));
  const mdbVisited = document.getElementById('mdbMigrateVisitedBtn');
  if (mdbVisited) mdbVisited.addEventListener('click', () => mdbMigrate('visited_urls'));
  const mdbSkills = document.getElementById('mdbMigrateSkillsBtn');
  if (mdbSkills) mdbSkills.addEventListener('click', () => mdbMigrate('skills'));
  const mdbConventions = document.getElementById('mdbMigrateConventionsBtn');
  if (mdbConventions) mdbConventions.addEventListener('click', () => mdbMigrate('conventions'));
  const mdbEngines = document.getElementById('mdbMigrateEnginesBtn');
  if (mdbEngines) mdbEngines.addEventListener('click', () => mdbMigrate('engines'));
  const mdbPresets = document.getElementById('mdbMigratePresetsBtn');
  if (mdbPresets) mdbPresets.addEventListener('click', () => mdbMigrate('presets'));
  // MariaDB password toggle
  const mdbPwToggle = document.getElementById('setMariadbPasswordToggle');
  if (mdbPwToggle) mdbPwToggle.addEventListener('click', () => {
    const pw = document.getElementById('setMariadbPassword');
    if (pw) pw.type = pw.type === 'password' ? 'text' : 'password';
  });
  // Reload the panel each time the Settings tab is activated so the
  // hub-side info stays fresh.
  document.querySelectorAll('#tabs .tab').forEach(btn => {
    if (btn.dataset.tab === 'settings') {
      btn.addEventListener('click', loadSettingsPanel);
    }
  });
})();

// Apply UI defaults to the Submit form at page load. Defer so the
// form elements and syncSubmitMode are already wired.
setTimeout(() => {
  try { applyUiDefaultsToSubmit(); } catch (e) {}
}, 0);

// ---- Submit URL → host shortcuts ----------------------------------------
// When the operator types a URL into the Submit form, derive the host
// and offer one-click access to that host's Edit (cookies / notes)
// and Dedup (visited URLs / recrawl patterns) modals. Counts are
// pulled live from /hosts/{host} (404 = new host).

let _urlHostInfoTimer = null;
let _urlHostCurrent = '';

function _normaliseHostJs(raw) {
  if (!raw) return '';
  let h = raw.toLowerCase().trim();
  if (h.startsWith('www.')) h = h.substring(4);
  return h;
}

function _extractHostFromUrl(raw) {
  if (!raw) return '';
  const s = raw.trim();
  if (!s) return '';
  // Accept bare hosts like "javdock.com" too.
  try {
    const candidate = /^https?:\/\//i.test(s) ? s : 'https://' + s;
    const u = new URL(candidate);
    return _normaliseHostJs(u.hostname);
  } catch (e) {
    return '';
  }
}

async function refreshUrlHostInfo() {
  const urlInput = document.getElementById('urlInput');
  const row = document.getElementById('urlHostInfo');
  const nameEl = document.getElementById('urlHostName');
  const editCnt = document.getElementById('urlHostEditCount');
  const dedupCnt = document.getElementById('urlHostDedupCount');
  const statusEl = document.getElementById('urlHostStatus');
  if (!urlInput || !row) return;
  const host = _extractHostFromUrl(urlInput.value);
  _urlHostCurrent = host;
  if (!host) {
    row.style.display = 'none';
    return;
  }
  row.style.display = 'flex';
  nameEl.textContent = host;
  // Clear counts immediately so stale info doesn't linger.
  editCnt.textContent = '';
  dedupCnt.textContent = '';
  statusEl.textContent = 'loading…';
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host));
    // Race-safety: the operator may have typed more characters since
    // we issued this fetch -- only paint if we're still showing the
    // same host.
    if (host !== _urlHostCurrent) return;
    if (r.ok) {
      const rec = await r.json();
      const cookieCnt = (rec.cookies || []).length;
      const visitedCnt = rec.visited_count || 0;
      const patternCnt = (rec.recrawl_patterns || []).length;
      editCnt.textContent  = cookieCnt > 0 ? '(' + cookieCnt + ' cookies)' : '';
      let dedupLabel = '';
      if (visitedCnt > 0 || patternCnt > 0) {
        const parts = [];
        if (visitedCnt > 0) parts.push(visitedCnt + ' visited');
        if (patternCnt > 0) parts.push(patternCnt + ' patterns');
        dedupLabel = '(' + parts.join(', ') + ')';
      }
      dedupCnt.textContent = dedupLabel;
      statusEl.textContent = '✓ registered';
      statusEl.style.color = '#196b2c';
    } else if (r.status === 404) {
      statusEl.textContent = '(未登録)';
      statusEl.style.color = '#888';
    } else {
      statusEl.textContent = 'load failed (' + r.status + ')';
      statusEl.style.color = '#a00';
    }
  } catch (e) {
    statusEl.textContent = 'load failed';
    statusEl.style.color = '#a00';
  }
}

function scheduleUrlHostInfo() {
  clearTimeout(_urlHostInfoTimer);
  _urlHostInfoTimer = setTimeout(refreshUrlHostInfo, 250);
}

(function wireUrlHostShortcuts() {
  const urlInput = document.getElementById('urlInput');
  if (urlInput) urlInput.addEventListener('input', scheduleUrlHostInfo);
  const editBtn = document.getElementById('urlHostEditBtn');
  if (editBtn) editBtn.addEventListener('click', () => {
    if (_urlHostCurrent) openHostModal(_urlHostCurrent);
  });
  const dedupBtn = document.getElementById('urlHostDedupBtn');
  if (dedupBtn) dedupBtn.addEventListener('click', () => {
    if (_urlHostCurrent) openVisitedModal(_urlHostCurrent);
  });
  // Refresh once at page load so a pre-filled URL (browser autofill /
  // form restoration) already shows the buttons.
  setTimeout(() => {
    try { refreshUrlHostInfo(); } catch (e) {}
  }, 50);
})();

// Reflect host registry edits back into the URL-host info row. Wrap
// the existing renderHosts() so any code path that bumps the Host
// table also bumps our shortcut row.
const _orig_renderHosts_for_url_info = (typeof renderHosts === 'function') ? renderHosts : null;
if (_orig_renderHosts_for_url_info) {
  renderHosts = async function() {
    const result = await _orig_renderHosts_for_url_info.apply(this, arguments);
    try { refreshUrlHostInfo(); } catch (e) {}
    return result;
  };
}

// =====================================================================
// Workers panel sub-tabs (Workers / Hubs / 機能)
// =====================================================================
// Three panes inside the Workers tab:
//   * workers — existing #workersTable (rendered by refresh())
//   * hubs    — multi-hub presence list from /hubs (auto-populated via
//               Redis-backed hub-heartbeat; refresh on tab activation)
//   * features — admin operations that used to need SSH + deploy.sh
//                (only one button so far: self-restart this hub).
// State (active sub-tab) persisted to localStorage for refresh survival.

const _WORKERS_SUBTABS = ['workers', 'hubs', 'recovery', 'features'];

function setWorkersSubtab(name) {
  if (!_WORKERS_SUBTABS.includes(name)) name = 'workers';
  try { localStorage.setItem('paprika.workersSubtab', name); } catch (_) {}
  document.querySelectorAll('#workersSubtabs .ai-subtab').forEach(t => {
    const on = t.dataset.workersSubtab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.workers-subpane').forEach(p => {
    p.style.display = (p.dataset.workersSubpane === name) ? '' : 'none';
  });
  // Lazy refresh per sub-tab so we don't hammer endpoints when the
  // operator's on a different sub-tab (or different top-level tab).
  if (name === 'hubs') {
    try { refreshHubsTable(); } catch (_) {}
  } else if (name === 'recovery') {
    try { refreshRecoveryTable(); refreshSalvageHistory(); _startRecoveryPoll(); } catch (_) {}
  } else {
    try { _stopRecoveryPoll(); } catch (_) {}
  }
}

(function wireWorkersSubtabs() {
  document.querySelectorAll('#workersSubtabs .ai-subtab').forEach(btn => {
    btn.addEventListener('click', () => setWorkersSubtab(btn.dataset.workersSubtab));
  });
  let initial = 'workers';
  try {
    const saved = localStorage.getItem('paprika.workersSubtab');
    if (saved && _WORKERS_SUBTABS.includes(saved)) initial = saved;
  } catch (_) {}
  setWorkersSubtab(initial);
})();

// ---- Recovery sub-tab -----------------------------------------------
//
// Fleet-wide recovery / lifecycle events (worker reconnect, orphan
// reattach, version self-update, restart, errors) aggregated from each
// worker's in-memory ring buffer via GET /workers/events. Auto-polled
// while the recovery sub-tab is visible.

let _recoveryPollTimer = null;

function _stopRecoveryPoll() {
  if (_recoveryPollTimer) { clearInterval(_recoveryPollTimer); _recoveryPollTimer = null; }
}

function _startRecoveryPoll() {
  _stopRecoveryPoll();
  _recoveryPollTimer = setInterval(() => {
    // Skip if the tab/panel is hidden (don't burn requests).
    if (document.hidden) return;
    const active = document.querySelector('.workers-subpane[data-workers-subpane="recovery"]');
    if (!active || active.style.display === 'none') { _stopRecoveryPoll(); return; }
    try { refreshRecoveryTable(); } catch (_) {}
    try { refreshSalvageHistory(); } catch (_) {}
  }, 10000);
}

function _fmtRecoveryTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const HH = String(d.getHours()).padStart(2, '0');
  const MM = String(d.getMinutes()).padStart(2, '0');
  const SS = String(d.getSeconds()).padStart(2, '0');
  const ago = Math.floor((Date.now() / 1000) - ts);
  const agoStr = ago < 60 ? ago + 's' : ago < 3600 ? Math.floor(ago / 60) + 'm' : Math.floor(ago / 3600) + 'h';
  return `<span title="${d.toLocaleString()}">${HH}:${MM}:${SS}</span> <small style="color:#888;">(${agoStr})</small>`;
}

function _recoveryKindBadge(kind) {
  const colors = {
    lifecycle: { bg: '#e6f4ff', fg: '#16608f', bd: '#9bf' },
    warn:      { bg: '#fff3e6', fg: '#a35a00', bd: '#e0b48a' },
    error:     { bg: '#fee',    fg: '#933',    bd: '#c88' },
    info:      { bg: '#f5f5fa', fg: '#555',    bd: '#bbc' },
    job:       { bg: '#ecf7e9', fg: '#196b2c', bd: '#7ab68a' },
  };
  const c = colors[kind] || colors.info;
  return `<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:${c.bg}; color:${c.fg}; border:1px solid ${c.bd};">${kind}</span>`;
}

function _esc(s) { return (s == null ? '' : ('' + s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

async function refreshRecoveryTable() {
  const tbody = document.querySelector('#recoveryTable tbody');
  if (!tbody) return;
  const kindSel = document.getElementById('recoveryKindFilter');
  const sinceSel = document.getElementById('recoverySinceFilter');
  const workerFilter = (document.getElementById('recoveryWorkerFilter')?.value || '').trim().toLowerCase();
  const statusEl = document.getElementById('recoveryStatus');
  const cntBadge = document.getElementById('workersSubtabCntRecovery');
  const kinds = kindSel?.value || 'lifecycle,warn,error';
  const since_s = parseInt(sinceSel?.value || '3600', 10);
  let payload = null;
  if (statusEl) statusEl.textContent = '取得中…';
  try {
    const r = await fetch(`/workers/events?limit=500&kinds=${encodeURIComponent(kinds)}&since_s=${since_s}`);
    if (r.ok) payload = await r.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty" style="padding:20px; text-align:center; color:#a00;">取得失敗: ${_esc(e.message || e)}</td></tr>`;
    if (statusEl) statusEl.textContent = '';
    return;
  }
  let events = (payload && payload.events) || [];
  if (workerFilter) {
    events = events.filter(e => (e.worker_id || '').toLowerCase().includes(workerFilter));
  }
  if (cntBadge) cntBadge.textContent = events.length;
  if (statusEl) {
    const total = (payload && payload.count) || 0;
    statusEl.textContent = workerFilter
      ? `${events.length} 件表示 / 全 ${total} 件`
      : `${events.length} 件`;
  }
  if (!events.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty" style="padding:20px; text-align:center; color:#888;">該当するイベントはありません</td></tr>';
    return;
  }
  const rows = events.map(e => `
    <tr>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_fmtRecoveryTs(e.ts)}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee;">${_recoveryKindBadge(e.kind || 'info')}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><code style="font-size:.85em;">${_esc(e.worker_id)}</code></td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; font-family:ui-monospace,Consolas,monospace; font-size:.86em; word-break:break-word;">${_esc(e.line)}</td>
    </tr>
  `).join('');
  tbody.innerHTML = rows;
}

// ---- Durable salvage recovery history (段階4 永続化) -----------------
//
// GET /workers/recovery-events -> shared MariaDB recovery_events ledger.
// Unlike the live ring-buffer table above, this survives hub restarts and
// is identical on every hub (no per-hub scoping). Rows: one per salvage
// attempt (success OR failure), recent-first.

function _salvageMethodBadge(m) {
  const map = {
    http:       { bg:'#e6f4ff', fg:'#16608f', bd:'#9bf',     t:'HTTP' },
    ssh:        { bg:'#f0e9ff', fg:'#5a3a8a', bd:'#b9f',     t:'SSH' },
    'http+ssh': { bg:'#fff3e6', fg:'#a35a00', bd:'#e0b48a',  t:'HTTP+SSH' },
  };
  const c = map[m] || { bg:'#f5f5fa', fg:'#555', bd:'#bbc', t:(m || '—') };
  return `<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:${c.bg}; color:${c.fg}; border:1px solid ${c.bd};">${c.t}</span>`;
}

function _salvageResultBadge(r) {
  const ok = (r === 'ok');
  const c = ok ? { bg:'#ecf7e9', fg:'#196b2c', bd:'#7ab68a', t:'✓ 成功' }
               : { bg:'#fee',    fg:'#933',    bd:'#c88',    t:'✗ ' + (r || '失敗') };
  return `<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:${c.bg}; color:${c.fg}; border:1px solid ${c.bd};">${c.t}</span>`;
}

async function refreshSalvageHistory() {
  const tbody = document.querySelector('#salvageHistoryTable tbody');
  if (!tbody) return;
  const cntEl = document.getElementById('salvageHistoryCount');
  const stEl = document.getElementById('salvageHistoryStatus');
  let payload = null;
  try {
    const r = await fetch('/workers/recovery-events?limit=300');
    if (r.ok) payload = await r.json();
  } catch (e) {
    if (stEl) stEl.textContent = '取得失敗';
    return;
  }
  if (payload && payload.durable === false) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty" style="padding:14px; text-align:center; color:#888;">MariaDB 未設定 — 永続履歴は無効（下のライブイベントのみ）</td></tr>';
    if (cntEl) cntEl.textContent = '0';
    if (stEl) stEl.textContent = '';
    return;
  }
  const events = (payload && payload.events) || [];
  if (cntEl) cntEl.textContent = events.length;
  if (stEl) stEl.textContent = '';
  if (!events.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty" style="padding:14px; text-align:center; color:#888;">まだ復旧記録はありません（salvage は既定 OFF）</td></tr>';
    return;
  }
  tbody.innerHTML = events.map(e => `
    <tr>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_fmtRecoveryTs(e.ts)}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><code style="font-size:.85em;">${_esc(e.worker_id)}</code>${e.ip ? ` <small style="color:#999;">${_esc(e.ip)}</small>` : ''}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_salvageMethodBadge(e.method)}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_salvageResultBadge(e.result)}</td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><small style="color:#777;">${_esc(e.hub_id || '—')}</small></td>
      <td style="padding:4px 8px; border-bottom:1px solid #eee; font-size:.86em; color:#555; word-break:break-word;">${_esc(e.detail || '')}</td>
    </tr>
  `).join('');
}

(function wireRecoveryFilters() {
  ['recoveryKindFilter', 'recoverySinceFilter', 'recoveryWorkerFilter'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const ev = el.tagName === 'INPUT' ? 'input' : 'change';
    el.addEventListener(ev, () => { try { refreshRecoveryTable(); } catch (_) {} });
  });
  const btn = document.getElementById('recoveryRefreshBtn');
  if (btn) btn.addEventListener('click', () => { try { refreshRecoveryTable(); refreshSalvageHistory(); } catch (_) {} });
})();

// ---- Hubs sub-tab ----------------------------------------------------

async function refreshHubsTable() {
  const tbody = document.querySelector('#hubsTable tbody');
  if (!tbody) return;
  let payload = null;
  try {
    const r = await fetch('/hubs');
    if (r.ok) payload = await r.json();
  } catch (_) { /* network noise */ }
  const hubs = (payload && payload.hubs) || [];
  const cntEl = document.getElementById('hubsCount');
  if (cntEl) cntEl.textContent = String(hubs.length);
  const subtabCnt = document.getElementById('workersSubtabCntHubs');
  if (subtabCnt) subtabCnt.textContent = String(hubs.length);
  if (hubs.length === 0) {
    tbody.innerHTML = '<tr><td colspan=6 class="empty">no hubs registered</td></tr>';
    return;
  }
  const rows = hubs.map(h => {
    const aliveDot = h.alive
      ? '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#3a8c3a; margin-right:6px;"></span>'
      : '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#bbb; margin-right:6px;"></span>';
    const statusText = h.alive ? 'alive' : 'offline';
    const localBadge = h.local
      ? ' <span style="background:#eef0ff; color:#3a5ca8; padding:1px 6px; border-radius:8px; font-size:.78em; margin-left:4px;">this hub</span>'
      : '';
    const pubBase = h.public_base
      ? `<a href="${esc(h.public_base)}" target="_blank" rel="noopener"><code>${esc(h.public_base)}</code></a>`
      : '<span class="empty">—</span>';
    const ver = h.version ? `<code>${esc(h.version)}</code>` : '<span class="empty">—</span>';
    const tsSec = h.ts || h.last_seen;
    const lastSeen = tsSec
      ? new Date(tsSec * 1000).toLocaleString()
      : '—';
    const forgetBtn = (!h.alive && !h.local)
      ? `<button class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933; padding:2px 8px; font-size:.85em;" onclick="forgetHub('${esc(h.hub_id)}')"><iconify-icon icon="lucide:trash-2"></iconify-icon> forget</button>`
      : '';
    // Same per-hub colour as the Workers tab's hub badge (_wkrTagStyle is a
    // global from admin-dashboard.js, loaded earlier) so a hub reads as the
    // same colour in both places.
    const hubIdCell = h.hub_id
      ? `<span class="badge" style="${_wkrTagStyle(h.hub_id)} font-size:.9em;">${esc(h.hub_id)}</span>`
      : '<span class="empty">—</span>';
    return `<tr>
      <td>${hubIdCell}${localBadge}</td>
      <td>${aliveDot}${esc(statusText)}</td>
      <td>${pubBase}</td>
      <td>${ver}</td>
      <td>${esc(lastSeen)}</td>
      <td>${forgetBtn}</td>
    </tr>`;
  }).join('');
  tbody.innerHTML = rows;
}

window.forgetHub = async function(hubId) {
  if (!confirm(`Forget hub "${hubId}"?\n\nDrops it from the registry index. If the hub is still running anywhere with the same Redis, its next heartbeat (~30s) will re-add it.`)) return;
  try {
    const r = await fetch('/hubs/' + encodeURIComponent(hubId), { method: 'DELETE' });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      alert('forget failed: ' + (d.detail || r.status));
    }
  } catch (e) {
    alert('forget failed: ' + e);
  }
  refreshHubsTable();
};

const hubsRefreshBtn = document.getElementById('hubsRefreshBtn');
if (hubsRefreshBtn) hubsRefreshBtn.addEventListener('click', refreshHubsTable);

// ---- 機能 sub-tab: self-restart button -------------------------------

(function wireFeatureRestartBtn() {
  const btn = document.getElementById('featureRestartHubBtn');
  const status = document.getElementById('featureRestartStatus');
  if (!btn || !status) return;
  btn.addEventListener('click', async () => {
    if (!confirm('このハブを再起動しますか？\n\n進行中のジョブは中断され、約 10-20 秒接続が切れます (docker restart policy で自動復帰)。')) return;
    btn.disabled = true;
    status.style.color = '#666';
    status.textContent = '再起動を要求中...';
    try {
      const r = await fetch('/admin/self-restart', { method: 'POST' });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        status.style.color = '#a00';
        status.textContent = '失敗: ' + (d.detail || r.status);
        btn.disabled = false;
        return;
      }
      status.style.color = '#196b2c';
      status.textContent = '✓ 再起動シグナル送信。接続復帰を待機中...';
      // Poll /health until the hub comes back. Up to ~60s.
      const t0 = Date.now();
      const poll = async () => {
        try {
          const h = await fetch('/health');
          if (h.ok) {
            status.style.color = '#196b2c';
            status.textContent = '✓ 再起動完了 (' + Math.round((Date.now() - t0) / 1000) + 's)';
            btn.disabled = false;
            return;
          }
        } catch (_) {}
        if (Date.now() - t0 > 60000) {
          status.style.color = '#a00';
          status.textContent = '× 60s 経過しても /health が復活しません。ホストで `docker logs hub-a` を確認してください。';
          btn.disabled = false;
          return;
        }
        setTimeout(poll, 1500);
      };
      // Brief delay before the first poll -- the hub needs time to actually exit.
      setTimeout(poll, 1500);
    } catch (e) {
      status.style.color = '#a00';
      status.textContent = '失敗: ' + e;
      btn.disabled = false;
    }
  });
})();

// Bump workers sub-tab count whenever the workers list refreshes
// (refresh() sets #workerCount; mirror it into the sub-tab pill).
(function wireWorkersSubtabCount() {
  const target = document.getElementById('workerCount');
  if (!target) return;
  const cnt = document.getElementById('workersSubtabCntWorkers');
  if (!cnt) return;
  new MutationObserver(() => {
    cnt.textContent = target.textContent || '0';
  }).observe(target, { childList: true, characterData: true, subtree: true });
})();

// Settings sub-tabs: same visual language + structure as #submit.
// Each subtab key maps 1:1 to a `<div class="settings-subpane"
// data-settings-subpane="...">` wrapping its content; switching tabs
// is just a display toggle on the subpanes (form values + cached
// state persist because nothing is removed from the DOM).
(function wireSettingsSubtabs() {
  function setSettingsSubtab(name) {
    if (!name) name = 'defaults';
    document.querySelectorAll('.settings-subtab[data-settings-subtab]').forEach((t) => {
      const on = t.dataset.settingsSubtab === name;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.settings-subpane[data-settings-subpane]').forEach((p) => {
      p.style.display = (p.dataset.settingsSubpane === name) ? '' : 'none';
    });
  }
  window.setSettingsSubtab = setSettingsSubtab;
  document.querySelectorAll('.settings-subtab[data-settings-subtab]').forEach((btn) => {
    btn.addEventListener('click', () => setSettingsSubtab(btn.dataset.settingsSubtab));
  });
  // Activate the default sub-tab on first visit to #settings.
  // setTab() may run before this script if the operator lands
  // directly on #settings, so re-init on tab click too.
  function _initIfActive() {
    const panel = document.querySelector('.panel[data-panel="settings"]');
    if (panel && panel.style.display !== 'none') setSettingsSubtab('defaults');
  }
  _initIfActive();
  document.querySelectorAll('.tab[data-tab="settings"]').forEach((btn) => {
    btn.addEventListener('click', () => setTimeout(_initIfActive, 0));
  });
})();
