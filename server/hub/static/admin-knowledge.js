
// ===== v2 Knowledge tab — self-contained, no admin.js dependency =====
(function () {
  const TIER_ORDER = { high:3, medium:2, low:1, stale:0 };
  let _hkData = [];

  function tierBadge(tier) {
    const t = tier || 'low';
    return `<span class="hk-badge tier-${t}">${t}</span>`;
  }

  function pct(n) {
    if (n == null || isNaN(n)) return '—';
    return Math.round(n * 100) + '%';
  }

  function ago(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      const s = (Date.now() - d.getTime()) / 1000;
      if (s < 60) return Math.round(s) + 's ago';
      if (s < 3600) return Math.round(s/60) + 'm ago';
      if (s < 86400) return Math.round(s/3600) + 'h ago';
      return Math.round(s/86400) + 'd ago';
    } catch (e) { return '—'; }
  }

  async function loadKnowledge() {
    const tbody = document.querySelector('#hkTable tbody');
    tbody.innerHTML = '<tr><td colspan=8 class="empty">loading…</td></tr>';
    // Single aggregate fetch -- /host_knowledge_all returns ALL hosts'
    // knowledge in one response. Replaces the previous N+1 pattern
    // (1 list call + 1 per-host detail call) that hit DevTools as
    // a flood of `knowledge` rows on every Knowledge-tab open AND
    // every page load (for the badge-count seed). For a 25-host fleet
    // that's 25 → 1 requests per load.
    let entries;
    try {
      const r = await fetch('/host_knowledge_all');
      if (!r.ok) {
        tbody.innerHTML = '<tr><td colspan=8 class="empty">error: HTTP ' + r.status + '</td></tr>';
        return;
      }
      const j = await r.json();
      entries = j.entries || [];
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">error: ' + e + '</td></tr>';
      return;
    }
    _hkData = entries
      .filter(x => x && x.host && x.knowledge)
      .map(x => ({ host: x.host, k: x.knowledge }));
    renderTable();
    renderSummary();
    loadAiInsights();
    // tab counter
    const cnt = document.getElementById('cntKnowledge');
    if (cnt) cnt.textContent = _hkData.length;
  }

  async function loadAiInsights() {
    // Judge comparisons
    try {
      const r = await fetch('/admin/judge_comparisons?limit=1');
      const j = await r.json();
      const counts = j.counts || {};
      const paired = counts.total_paired || 0;
      const _tt2 = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
      document.getElementById('aiPaired').textContent = paired;
      document.getElementById('aiPairedSub').textContent =
        paired > 0
          ? `${_tt2('knowledge.ai.paired.agree', '一致')}=${counts.agree} ${_tt2('knowledge.ai.paired.disagree', '不一致')}=${counts.disagree}`
          : _tt2('knowledge.ai.paired.disabled', 'PAPRIKA_R1_JUDGE_MODE=shadow を有効化してください');
      const agreeRate = paired > 0 ? Math.round((counts.agree / paired) * 100) + '%' : '—';
      document.getElementById('aiAgree').textContent = agreeRate;
    } catch (e) {
      document.getElementById('aiPaired').textContent = '?';
    }
    // 推論 AI distiller stats — count hosts whose provenance.last_updated_by == 'distiller-r1'.
    // 内部識別子 'distiller-r1' は DB/履歴互換のため固定 (元は DeepSeek-R1 由来)。
    // UI 表記は engine 非依存の「推論 AI」に統一。
    const r1Hosts = _hkData.filter(e => ((e.k.provenance || {}).last_updated_by || '') === 'distiller-r1');
    document.getElementById('aiR1Hosts').textContent = r1Hosts.length;
    // Recent updates in the last 24h
    const cutoff = Date.now() - 24 * 3600 * 1000;
    const recent = r1Hosts.filter(e => {
      const t = (e.k.provenance || {}).last_updated_at;
      if (!t) return false;
      const ts = Date.parse(t);
      return !isNaN(ts) && ts >= cutoff;
    });
    const _tt3 = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
    document.getElementById('aiDistilled').textContent = recent.length;
    document.getElementById('aiDistilledSub').textContent =
      recent.length > 0
        ? _tt3('knowledge.ai.distilled.recent', '直近 24 時間以内')
        : _tt3('knowledge.ai.distilled.none', '直近 24 時間以内の更新なし');
  }

  // ---- AI self-improvement loop: skills & conventions (paged) ----
  const _AI_PAGE = 15;
  const _aiState = {
    skills:         { items: [], page: 0, tableId: 'skTable',      cntId: 'cntSkills',      cols: 6 },
    conventions:    { items: [], page: 0, tableId: 'cvTable',      cntId: 'cntConventions', cols: 6 },
    'groom-retire': { items: [], page: 0, tableId: 'grTable',      cntId: 'cntGroomRetire', cols: 7, rowFn: (x) => _groomRetireRow(x) },
    'groom-dedup':  { items: [], page: 0, tableId: 'gdTable',      cntId: 'cntGroomDedup',  cols: 4, rowFn: (x) => _groomDedupRow(x) },
    'oracle':       { items: [], page: 0, tableId: 'oracleTable',  cntId: 'cntOracle',      cols: 8, rowFn: (x) => _oracleRow(x) },
  };

  async function loadSkillsConventions() {
    // _rolesSettings.skill_audit_warn_threshold drives the 監査要 badge
    // in _aiRow. loadRoles() also populates it, but the operator may
    // jump straight to the skills/conventions sub-tab without visiting
    // 役割 first -- prime the settings cache here so the warning shows.
    if (!_rolesSettings || Object.keys(_rolesSettings).length === 0) {
      try {
        const sr = await fetch('/settings');
        if (sr.ok) _rolesSettings = ((await sr.json()).values) || {};
      } catch (_e) { /* transient */ }
    }
    await _loadAiTable('/skills', 'skills');
    await _loadAiTable('/conventions', 'conventions');
  }

  async function _loadAiTable(url, kind) {
    const st = _aiState[kind];
    const tbody = document.querySelector('#' + st.tableId + ' tbody');
    let items;
    try {
      const r = await fetch(url);
      if (!r.ok) { tbody.innerHTML = '<tr><td colspan=6 class="empty">error: HTTP ' + r.status + '</td></tr>'; return; }
      const j = await r.json();
      items = j.skills || j.conventions || j.items || (Array.isArray(j) ? j : []);
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan=6 class="empty">error: ' + e + '</td></tr>'; return;
    }
    // Proven first: success_rate desc (untried/null last), then use_count desc.
    items.sort((a, b) => {
      const ra = a.success_rate, rb = b.success_rate;
      if (ra == null && rb == null) return (b.use_count || 0) - (a.use_count || 0);
      if (ra == null) return 1;
      if (rb == null) return -1;
      return (rb - ra) || ((b.use_count || 0) - (a.use_count || 0));
    });
    st.items = items;
    st.page = 0;
    _renderAiPage(kind);
  }

  function _renderAiPage(kind) {
    const st = _aiState[kind];
    const cols = st.cols || 6;
    const tbody = document.querySelector('#' + st.tableId + ' tbody');
    const cnt = document.getElementById(st.cntId); if (cnt) cnt.textContent = st.items.length;
    if (!st.items.length) { tbody.innerHTML = '<tr><td colspan=' + cols + ' class="empty">none</td></tr>'; _renderAiPager(kind); return; }
    const pages = Math.max(1, Math.ceil(st.items.length / _AI_PAGE));
    if (st.page >= pages) st.page = pages - 1;
    if (st.page < 0) st.page = 0;
    const start = st.page * _AI_PAGE;
    const rf = st.rowFn || ((x) => _aiRow(x, kind));
    tbody.innerHTML = st.items.slice(start, start + _AI_PAGE).map(rf).join('');
    _renderAiPager(kind);
  }

  function _renderAiPager(kind) {
    const st = _aiState[kind];
    const el = document.getElementById('pager-' + kind);
    if (!el) return;
    const n = st.items.length;
    if (n <= _AI_PAGE) { el.innerHTML = ''; return; }
    const pages = Math.ceil(n / _AI_PAGE);
    const from = st.page * _AI_PAGE + 1;
    const to = Math.min(n, (st.page + 1) * _AI_PAGE);
    el.innerHTML =
      '<button class="aipg" data-aipg="prev" data-kind="' + kind + '"' + (st.page <= 0 ? ' disabled' : '') + '>‹ prev</button>' +
      '<span class="aipg-info">' + from + '–' + to + ' / ' + n + '（' + (st.page + 1) + ' / ' + pages + '）</span>' +
      '<button class="aipg" data-aipg="next" data-kind="' + kind + '"' + (st.page >= pages - 1 ? ' disabled' : '') + '>next ›</button>';
  }

  function _aiPagerClick(kind, dir) {
    const st = _aiState[kind];
    if (!st) return;
    st.page += (dir === 'next' ? 1 : -1);
    _renderAiPage(kind);
  }

  function setAiSubtab(name) {
    // Scope to elements that actually carry data-ai-subtab. The same
    // .ai-subtab class is shared with the Jobs panel's status-filter
    // tabs (#jobsStatusTabs), which have NO data-ai-subtab attribute.
    // Without this scope, `dataset.aiSubtab === name` evaluates to
    // `undefined === undefined` for those siblings when name itself is
    // undefined (or the same kind of stray comparison when name is a
    // real string), flipping their .active state in lockstep — hence
    // the "click エラー and all four jobs tabs light up" bug.
    document.querySelectorAll('.ai-subtab[data-ai-subtab]').forEach(t => {
      const on = t.dataset.aiSubtab === name;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.ai-subpane').forEach(p => {
      p.style.display = (p.dataset.aiSubpane === name) ? '' : 'none';
    });
    // Lazy-load the activated sub-tab's data. The 稼働中 tab starts a light
    // poller; every OTHER sub-tab stops it, so it only polls while visible.
    if (name === 'live') {
      _aiLiveStart();
      return;
    }
    _aiLiveStop();
    if (name === 'roles') {
      loadRoles();
      return;
    }
    if (name === 'engines') {
      _aiMountEnginesPanel();
      if (typeof loadEngines === 'function') loadEngines().then(() => { if (typeof renderEnginesList === 'function') renderEnginesList(); });
      return;
    }
    if (name === 'knowledge') {
      if (typeof loadKnowledge === 'function') loadKnowledge();
    } else if (name === 'grooming') {
      loadGrooming();
    } else if (name === 'oracle') {
      loadOracle();
    } else if (name === 'aiio') {
      _aiIoStart();
    } else if (name === 'audit') {
      _auditStart();
    } else {
      loadSkillsConventions();
    }
    if (name !== 'aiio') _aiIoStop();
    if (name !== 'audit') _auditStop();
  }

  // ===== Success Audit sub-tab ============================================
  // VisionAI-sampled audit of completed video-download jobs. Backed by
  // GET /ai/audit-stats (KPIs) + GET /ai/audit-recent (table) +
  // POST /ai/audit-now (operator-triggered immediate run).
  let _auditPollTimer = null;
  function _auditStop() { if (_auditPollTimer) { clearInterval(_auditPollTimer); _auditPollTimer = null; } }
  function _auditStart() {
    _auditStop();
    try { refreshAuditPanel(); } catch (_) {}
    _auditPollTimer = setInterval(() => {
      if (document.hidden) return;
      const a = document.querySelector('.ai-subpane[data-ai-subpane="audit"]');
      if (!a || a.style.display === 'none') { _auditStop(); return; }
      try { refreshAuditPanel(); } catch (_) {}
    }, 60000);
  }
  function _fmtAuditTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.getHours().toString().padStart(2,'0') + ':' +
           d.getMinutes().toString().padStart(2,'0') + ':' +
           d.getSeconds().toString().padStart(2,'0');
  }
  function _auditVerdictBadge(ts) {
    // ts here is the actually_succeeded value (1 / 0 / null).
    if (ts === 1 || ts === true)  return '<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:#ecf7e9; color:#196b2c; border:1px solid #7ab68a;">✓ OK</span>';
    if (ts === 0 || ts === false) return '<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:#fdecec; color:#a23c2a; border:1px solid #e0a99a;">✗ NG</span>';
    return '<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:#f5f5fa; color:#555; border:1px solid #bbc;">?? 不明</span>';
  }
  function _reportedStatusBadge(rs) {
    const colors = {
      completed: { bg: '#ecf7e9', fg: '#196b2c', bd: '#7ab68a' },
      failed:    { bg: '#fdecec', fg: '#a23c2a', bd: '#e0a99a' },
      review:    { bg: '#fff3e6', fg: '#b8860b', bd: '#e0b48a' },
    };
    const c = colors[rs] || { bg: '#f5f5fa', fg: '#555', bd: '#bbc' };
    return `<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:${c.bg}; color:${c.fg}; border:1px solid ${c.bd};">${_esc(rs || '?')}</span>`;
  }
  function _verdictRowFlag(vk) {
    // Highlight disagreement rows so the eye catches them.
    if (vk === 'false_positive') return 'background:#fffaf0;';
    if (vk === 'false_negative') return 'background:#faf5ff;';
    return '';
  }
  async function refreshAuditPanel() {
    const since = parseInt(document.getElementById('auditSince')?.value || '86400', 10);
    const failsOnly = !!document.getElementById('auditFailsOnly')?.checked;
    const vk = (document.getElementById('auditVerdictKind')?.value || '').trim();
    const statusEl = document.getElementById('auditStatus');
    if (statusEl) statusEl.textContent = '取得中…';
    // 1) KPI tiles (4-quadrant)
    try {
      const r = await fetch('/ai/audit-stats?since_s=' + since);
      if (r.ok) {
        const d = await r.json();
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        set('auditCntAudited', d.audited ?? 0);
        set('auditCntTrueOk', d.true_ok ?? 0);
        set('auditCntFalsePos', d.false_positive ?? 0);
        set('auditCntFalseNeg', d.false_negative ?? 0);
        set('auditCntTrueFail', d.true_failure ?? 0);
        const ar = d.report_agreement_rate;
        set('auditAgreeRate', (ar == null) ? '—' : Math.round(ar * 100) + '%');
        const tr = d.true_success_rate;
        set('auditTrueRate', (tr == null) ? '—' : Math.round(tr * 100) + '%');
        const renderHosts = (id, list) => {
          const el = document.getElementById(id);
          if (!el) return;
          el.innerHTML = (list && list.length)
            ? list.map(h => `<div><code>${_esc(h[0])}</code> ×${h[1]}</div>`).join('')
            : '<span style="color:#999;">(なし)</span>';
        };
        renderHosts('auditTopFpHosts', d.top_false_positive_hosts || []);
        renderHosts('auditTopFnHosts', d.top_false_negative_hosts || []);
        const badge = document.getElementById('cntAudit');
        if (badge) badge.textContent = (d.false_positive ?? 0) + (d.false_negative ?? 0);
      }
    } catch (_) {}
    // 2) Table
    const tbody = document.querySelector('#auditTable tbody');
    if (!tbody) return;
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (failsOnly) params.set('only_failures', '1');
      if (vk) params.set('verdict_kind', vk);
      const r = await fetch('/ai/audit-recent?' + params.toString());
      if (!r.ok) { tbody.innerHTML = `<tr><td colspan="8" class="empty" style="padding:20px; text-align:center; color:#a00;">取得失敗 (HTTP ${r.status})</td></tr>`; return; }
      const d = await r.json();
      const rows = d.rows || [];
      if (statusEl) statusEl.textContent = `${rows.length} 件表示`;
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty" style="padding:20px; text-align:center; color:#888;">監査結果なし — 「即時実行」で開始するか、Settings で success_audit_enabled を true に</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map(e => `
        <tr style="${_verdictRowFlag(e.verdict_kind)}">
          <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_fmtAuditTs(e.ts)}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee;">${_reportedStatusBadge(e.reported_status)}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee;">${_auditVerdictBadge(e.truly_succeeded)}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; text-align:right; font-variant-numeric:tabular-nums;">${e.confidence == null ? '—' : (Math.round(e.confidence*100)+'%')}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><code style="font-size:.83em;">${_esc((e.job_id || '').slice(0,12))}</code></td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><a href="${_esc(e.url || '#')}" target="_blank" rel="noopener" style="font-size:.85em;">${_esc((e.url || '').slice(0, 60))}</a></td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; font-family:ui-monospace,Consolas,monospace; font-size:.82em; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${_esc(e.video_file || '—')}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; font-size:.85em; max-width:380px; word-break:break-word;">${_esc(e.reason || '')}</td>
        </tr>`).join('');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty" style="padding:20px; text-align:center; color:#a00;">取得失敗: ${_esc(e.message || e)}</td></tr>`;
    }
    if (statusEl && (statusEl.textContent || '').endsWith('取得中…')) statusEl.textContent = '';
  }
  (function wireAudit() {
    document.addEventListener('DOMContentLoaded', () => {
      ['auditSince','auditFailsOnly','auditVerdictKind'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const ev = el.type === 'checkbox' ? 'change' : 'change';
        el.addEventListener(ev, () => { try { refreshAuditPanel(); } catch (_) {} });
      });
      const r = document.getElementById('auditRefresh');
      if (r) r.addEventListener('click', () => { try { refreshAuditPanel(); } catch (_) {} });
      const run = document.getElementById('auditRunNow');
      if (run) run.addEventListener('click', async () => {
        run.disabled = true;
        const status = document.getElementById('auditStatus');
        if (status) status.textContent = '実行中…';
        try {
          const resp = await fetch('/ai/audit-now', { method: 'POST' });
          if (resp.ok) {
            const d = await resp.json();
            if (status) status.textContent = `audited=${d.audited||0} ok=${d.ok||0} ng=${d.ng||0}`;
          } else {
            if (status) status.textContent = `失敗 (HTTP ${resp.status})`;
          }
        } catch (e) {
          if (status) status.textContent = '失敗: ' + (e.message || e);
        } finally {
          run.disabled = false;
          try { refreshAuditPanel(); } catch (_) {}
        }
      });
    });
  })();

  // ===== AI I/O log sub-tab =================================================
  // Lists every LLM call (planner / skill_retrieval / codegen / judge /
  // skill_distill / convention_distill / reasoning_distill / perception)
  // with its prompt/response/latency, so the operator can see the whole
  // loop end-to-end. Backed by GET /ai/io (server/hub/routes/skills.py)
  // reading from MariaDB ai_io_log. Auto-polls while the tab is visible.
  let _aiIoPollTimer = null;
  function _aiIoStop() { if (_aiIoPollTimer) { clearInterval(_aiIoPollTimer); _aiIoPollTimer = null; } }
  function _aiIoStart() {
    _aiIoStop();
    try { refreshAiIoTable(); } catch (_) {}
    _aiIoPollTimer = setInterval(() => {
      if (document.hidden) return;
      const active = document.querySelector('.ai-subpane[data-ai-subpane="aiio"]');
      if (!active || active.style.display === 'none') { _aiIoStop(); return; }
      try { refreshAiIoTable(); } catch (_) {}
    }, 10000);
  }
  function _fmtAiIoTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const HH = String(d.getHours()).padStart(2, '0');
    const MM = String(d.getMinutes()).padStart(2, '0');
    const SS = String(d.getSeconds()).padStart(2, '0');
    const ago = Math.floor((Date.now() / 1000) - ts);
    const agoStr = ago < 60 ? ago + 's' : ago < 3600 ? Math.floor(ago / 60) + 'm' : Math.floor(ago / 3600) + 'h';
    return `<span title="${d.toLocaleString()}">${HH}:${MM}:${SS}</span> <small style="color:#888;">(${agoStr})</small>`;
  }
  function _purposeBadge(p) {
    const colors = {
      planner:            { bg:'#e6f4ff', fg:'#16608f', bd:'#9bf' },
      skill_retrieval:    { bg:'#ecf7e9', fg:'#196b2c', bd:'#7ab68a' },
      codegen:            { bg:'#f3ecfb', fg:'#3a2a7a', bd:'#a09bd0' },
      judge:              { bg:'#fff3e6', fg:'#7a4500', bd:'#e0a060' },
      skill_distill:      { bg:'#ecf7e9', fg:'#196b2c', bd:'#7ab68a' },
      convention_distill: { bg:'#f3ecfb', fg:'#3a2a7a', bd:'#a09bd0' },
      reasoning_distill:  { bg:'#fff3e6', fg:'#7a4500', bd:'#e0a060' },
      perception:         { bg:'#e6f4ff', fg:'#16608f', bd:'#9bf' },
    };
    const c = colors[p] || { bg:'#f5f5fa', fg:'#555', bd:'#bbc' };
    return `<span style="display:inline-block; padding:1px 8px; border-radius:10px; font-size:.78em; font-weight:600; background:${c.bg}; color:${c.fg}; border:1px solid ${c.bd};">${_esc(p || 'other')}</span>`;
  }
  let _aiIoRows = [];
  let _aiIoCopyText = '';
  async function refreshAiIoTable() {
    const tbody = document.querySelector('#aiIoTable tbody');
    if (!tbody) return;
    const purpose = (document.getElementById('aiIoPurpose')?.value || '').trim();
    const engine  = (document.getElementById('aiIoEngine')?.value || '').trim();
    const job_id  = (document.getElementById('aiIoJob')?.value || '').trim();
    const since   = parseInt(document.getElementById('aiIoSince')?.value || '3600', 10);
    const errs    = !!document.getElementById('aiIoErrorsOnly')?.checked;
    const statusEl = document.getElementById('aiIoStatus');
    const cntBadge = document.getElementById('cntAiIo');
    if (statusEl) statusEl.textContent = '取得中…';
    const params = new URLSearchParams({ limit:'200', since_s:String(since), errors_only:errs?'1':'0' });
    if (purpose) params.set('purpose', purpose);
    if (engine)  params.set('engine_slug', engine);
    if (job_id)  params.set('job_id', job_id);
    let payload = null;
    try {
      const r = await fetch('/ai/io?' + params.toString());
      if (r.ok) payload = await r.json();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty" style="padding:20px; text-align:center; color:#a00;">取得失敗: ${_esc(e.message || e)}</td></tr>`;
      if (statusEl) statusEl.textContent = '';
      return;
    }
    _aiIoRows = (payload && payload.events) || [];
    if (cntBadge) cntBadge.textContent = _aiIoRows.length;
    if (statusEl) statusEl.textContent = `${_aiIoRows.length} 件`;
    if (!_aiIoRows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty" style="padding:20px; text-align:center; color:#888;">該当する LLM 呼び出しはありません</td></tr>';
      return;
    }
    const rows = _aiIoRows.map((e, i) => {
      const preview = (e.prompt_text || '').slice(0, 80).replace(/\n/g, ' ');
      const tio = (e.tokens_in != null || e.tokens_out != null)
        ? `${e.tokens_in||'—'}/${e.tokens_out||'—'}` : '—';
      const errMark = e.error ? ' <span style="color:#a00; font-weight:700;" title="' + _esc(e.error) + '">⚠</span>' : '';
      return `
        <tr data-aiio-row="${i}" style="cursor:pointer;" onmouseover="this.style.background='#fafafa'" onmouseout="this.style.background=''">
          <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;">${_fmtAiIoTs(e.ts)}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee;">${_purposeBadge(e.purpose)}${errMark}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><code style="font-size:.85em;">${_esc(e.engine_slug || '—')}</code></td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; white-space:nowrap;"><code style="font-size:.83em;">${_esc((e.job_id || '—').slice(0, 12))}</code></td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums;">${e.latency_ms || 0}ms</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums;">${tio}</td>
          <td style="padding:4px 8px; border-bottom:1px solid #eee; font-family:ui-monospace,Consolas,monospace; font-size:.83em; max-width:380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${_esc(preview)}</td>
        </tr>`;
    }).join('');
    tbody.innerHTML = rows;
  }
  function _openAiIoDetail(idx) {
    const e = _aiIoRows[idx];
    if (!e) return;
    const modal = document.getElementById('aiIoModal');
    if (!modal) return;
    document.getElementById('aiIoModalTitle').textContent = (e.purpose || 'other') + ' · ' + (e.engine_slug || '');
    document.getElementById('aiIoModalMeta').textContent =
      `job: ${e.job_id || '—'} · ${e.latency_ms || 0}ms · in/out ${e.tokens_in||'—'}/${e.tokens_out||'—'}`;
    document.getElementById('aiIoModalPLen').textContent = `(${e.prompt_len || 0} bytes${e.prompt_ref?' · MinIO ai_io/'+e.prompt_ref+'.bin で全文':''})`;
    document.getElementById('aiIoModalRLen').textContent = `(${e.response_len || 0} bytes${e.response_ref?' · MinIO ai_io/'+e.response_ref+'.bin で全文':''})`;
    document.getElementById('aiIoModalPrompt').textContent = e.prompt_text || '(プロンプト無し)';
    document.getElementById('aiIoModalResponse').textContent = e.response_text || '(レスポンス無し)';
    const errEl = document.getElementById('aiIoModalError');
    if (e.error) { errEl.style.display = ''; errEl.textContent = 'エラー: ' + e.error; }
    else { errEl.style.display = 'none'; errEl.textContent = ''; }
    _aiIoCopyText = (e.response_text || '');
    modal.style.display = 'flex';
  }
  (function wireAiIo() {
    document.addEventListener('DOMContentLoaded', () => {
      const filterIds = ['aiIoPurpose', 'aiIoEngine', 'aiIoJob', 'aiIoSince', 'aiIoErrorsOnly'];
      filterIds.forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const ev = (el.tagName === 'INPUT' && el.type !== 'checkbox') ? 'input' : 'change';
        el.addEventListener(ev, () => { try { refreshAiIoTable(); } catch (_) {} });
      });
      const r = document.getElementById('aiIoRefresh');
      if (r) r.addEventListener('click', () => { try { refreshAiIoTable(); } catch (_) {} });
      const t = document.getElementById('aiIoTable');
      if (t) t.addEventListener('click', (ev) => {
        const tr = ev.target.closest('tr[data-aiio-row]');
        if (!tr) return;
        _openAiIoDetail(parseInt(tr.dataset.aiioRow, 10));
      });
      const m = document.getElementById('aiIoModal');
      const close = document.getElementById('aiIoModalClose');
      const copy = document.getElementById('aiIoModalCopy');
      if (close && m) close.addEventListener('click', () => { m.style.display = 'none'; });
      if (m) m.addEventListener('click', (e) => { if (e.target === m) m.style.display = 'none'; });
      if (copy) copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(_aiIoCopyText || ''); const old = copy.innerHTML; copy.textContent = '✓ copied'; setTimeout(() => { copy.innerHTML = old; }, 1200); } catch (_) {}
      });
    });
  })();

  // ===== 稼働中 (live activity): what the AI engines are doing right now =====
  // Polls the cheap in-memory /ai/activity (5s) + /engines (15s) ONLY while
  // the pane is visible (offsetParent !== null) and the browser tab is
  // foregrounded -- so it adds no load when you're elsewhere.
  let _aiLiveTimer = null, _aiEngTimer = null, _aiServerSkew = 0;
  let _aiEnginesData = [], _aiActiveSet = new Set(), _aiDisabledSet = new Set(), _aiEngWired = false;

  function _aiLiveStop() {
    if (_aiLiveTimer) { clearInterval(_aiLiveTimer); _aiLiveTimer = null; }
    if (_aiEngTimer) { clearInterval(_aiEngTimer); _aiEngTimer = null; }
  }
  function _aiLiveVisible() {
    if (document.hidden) return false;
    const pane = document.querySelector('.ai-subpane[data-ai-subpane="live"]');
    return !!(pane && pane.offsetParent !== null);
  }
  function _aiLiveStart() {
    _aiLiveStop();
    _aiEngWire();
    loadAiActivity();
    loadAiEngines();
    _aiLiveTimer = setInterval(() => { if (_aiLiveVisible()) loadAiActivity(); }, 5000);
    _aiEngTimer = setInterval(() => { if (_aiLiveVisible()) loadAiEngines(); }, 15000);
  }

  function _aiAgo(ts) {
    if (!ts) return '—';
    const sec = Math.max(0, (Date.now() / 1000 + _aiServerSkew) - ts);
    if (sec < 60) return Math.round(sec) + 's';
    if (sec < 3600) return Math.round(sec / 60) + 'm';
    if (sec < 86400) return Math.round(sec / 3600) + 'h';
    return Math.round(sec / 86400) + 'd';
  }
  // Format an ISO8601 UTC timestamp string as "Ns/Nm/Nh/Nd" — past by default,
  // future (e.g. next-run-at) when ``future`` is true (returns "Ns 後").
  function _aiAgoUtc(iso, future) {
    if (!iso) return '—';
    try {
      const ms = Date.parse(iso);
      if (isNaN(ms)) return '—';
      const now = Date.now();
      const delta = future ? (ms - now) : (now - ms);
      const sec = Math.max(0, Math.floor(delta / 1000));
      const out = sec < 60 ? sec + 's'
        : sec < 3600 ? Math.floor(sec / 60) + 'm'
        : sec < 86400 ? Math.floor(sec / 3600) + 'h'
        : Math.floor(sec / 86400) + 'd';
      return future ? (out + ' 後') : out;
    } catch (_e) { return '—'; }
  }

  function _aiCard(icon, label, active, sub, color) {
    const hot = (active || 0) > 0;
    return '<div style="flex:1 1 150px;border:1px solid #e2e6ea;border-radius:10px;padding:9px 12px;background:' +
      (hot ? '#eef7f0' : '#fafbfc') + ';">' +
      '<div style="font-size:.78em;color:#666;white-space:nowrap;"><iconify-icon icon="' + icon + '"></iconify-icon> ' + label + '</div>' +
      '<div style="font-size:1.7em;font-weight:700;line-height:1.15;color:' + (hot ? color : '#aaa') + ';">' + (active || 0) + '</div>' +
      '<div style="font-size:.7em;color:#999;">' + (sub || '') + '</div></div>';
  }

  async function loadAiActivity() {
    try {
      const r = await fetch('/ai/activity');
      if (!r.ok) return;
      const d = await r.json();
      if (d.server_now) _aiServerSkew = d.server_now - Date.now() / 1000;
      const f = d.inflight || {}, rr = d.reasoning || {};
      const v = f.vision || {}, j = f.judge || {}, di = f.distiller || {}, c = f.codegen || {};
      const dOn = rr.distiller_mode && rr.distiller_mode !== 'off';
      const jOn = rr.judge_mode && rr.judge_mode !== 'off';
      const inf = document.getElementById('aiLiveInflight');
      if (inf) inf.innerHTML =
        _aiCard('lucide:eye', '視覚 perception', v.active, 'peak ' + (v.peak || 0) + ' · total ' + (v.total || 0), '#2e7d32') +
        _aiCard('lucide:code-2', 'コード生成 codegen', c.active, 'total ' + (c.total || 0), '#1a6a8b') +
        _aiCard('lucide:brain', '推論 蒸留 distiller', di.active, dOn ? _esc(rr.distiller_engine || '') : 'OFF', '#66558c') +
        _aiCard('lucide:scale', '審査 judge', j.active, jOn ? _esc(rr.judge_mode) : 'OFF', '#7a6a2a');
      _renderAiCodegen(d.codegen_loop);
      _renderAiRecent(d.recent || []);
      // Fast path for per-engine 稼働中/停止中 badges (the slower /engines
      // poll carries model/temp/tokens; re-render the table with fresh sets).
      _aiActiveSet = new Set(d.active_engines || []);
      _aiDisabledSet = new Set(d.disabled_engines || []);
      _renderAiEngines();
    } catch (e) { /* transient */ }
  }

  function _renderAiCodegen(cg) {
    const el = document.getElementById('aiLiveCodegen'); if (!el) return;
    const jobs = (cg && cg.codegen_loop_jobs) || [];
    const limit = (cg && cg.codegen_loop_limit) || 0;
    const running = (cg && cg.codegen_loop_running) || 0;
    const head = '<div style="font-size:.8em;color:#666;margin-bottom:5px;">稼働 ' + running +
      (limit ? ' / 上限 ' + limit : ' / 上限なし') + '</div>';
    if (!jobs.length) { el.innerHTML = head + '<div class="empty" style="color:#aaa;">いま生成中のジョブはありません</div>'; return; }
    el.innerHTML = head + jobs.map(j =>
      '<div style="border-left:3px solid #1a6a8b;padding:5px 10px;margin-bottom:5px;background:#f7fafb;border-radius:0 6px 6px 0;">' +
      '<a href="#live/' + encodeURIComponent(j.job_id) + '" style="font-family:monospace;font-size:.82em;">' + _esc(j.job_id) + '</a>' +
      (j.host ? ' <span class="aibadge tier-auto">' + _esc(j.host) + '</span>' : '') +
      (j.phase ? ' <span style="font-size:.75em;color:#888;">' + _esc(j.phase) + '</span>' : '') +
      (j.goal ? '<div style="font-size:.82em;color:#444;margin-top:2px;">' + _esc(j.goal) + '</div>' : '') +
      '</div>'
    ).join('');
  }

  function _renderAiRecent(events) {
    const el = document.getElementById('aiLiveRecent'); if (!el) return;
    if (!events.length) { el.innerHTML = '<div class="empty" style="color:#aaa;">まだイベントがありません（再起動後に蓄積）</div>'; return; }
    const kindColor = { distill: '#66558c', perceive: '#2e7d32', escalate: '#bf722a', recipe: '#1f7a63' };
    const kindLabel = { distill: '蒸留', perceive: '視覚', escalate: 'エスカレ', recipe: 'recipe' };
    el.innerHTML = '<div style="max-height:300px;overflow:auto;border:1px solid #eef0f2;border-radius:6px;">' + events.map(e =>
      '<div style="display:flex;gap:8px;align-items:baseline;padding:4px 8px;border-bottom:1px solid #f3f5f7;font-size:.84em;">' +
      '<span style="color:#999;width:32px;flex:none;text-align:right;">' + _aiAgo(e.at) + '</span>' +
      '<span class="aibadge" style="background:' + (kindColor[e.kind] || '#777') + ';color:#fff;flex:none;">' + (kindLabel[e.kind] || _esc(e.kind)) + '</span>' +
      (e.host ? '<span style="color:#555;flex:none;font-family:monospace;font-size:.92em;">' + _esc(e.host) + '</span>' : '') +
      '<span style="color:#444;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + _esc(e.summary || '') + '</span>' +
      '</div>'
    ).join('') + '</div>';
  }

  async function loadAiEngines() {
    try {
      const r = await fetch('/engines');
      if (!r.ok) return;
      const d = await r.json();
      _aiEnginesData = d.engines || d.items || (Array.isArray(d) ? d : []);
      _renderAiEngines();
    } catch (e) { /* transient */ }
  }

  // 状態: 停止中(operator) / サーマル停止(GPU過熱) / 稼働中(in-flight) / 接続中(ready).
  function _engineStatus(e) {
    const th = e.thermal || {};
    if (_aiDisabledSet.has(e.slug) || e.enabled === false) return { label: '停止中', color: '#9aa3ab', stopped: true };
    if (th.temp_c != null && th.accepting === false) return { label: 'サーマル停止', color: '#d6791b', stopped: false };
    if (_aiActiveSet.has(e.slug) || e.active === true) return { label: '稼働中', color: '#2e7d32', stopped: false };
    return { label: '接続中', color: '#2c6e8e', stopped: false };
  }

  function _renderAiEngines() {
    const tb = document.getElementById('aiLiveEngines'); if (!tb) return;
    const items = _aiEnginesData || [];
    if (!items.length) { tb.innerHTML = '<tr><td colspan="7" class="empty">エンジンなし</td></tr>'; return; }
    tb.innerHTML = items.map(e => {
      const u = e.usage_today || {}, th = e.thermal || {};
      const tc = th.temp_c;
      const temp = (tc == null) ? '—' : (Math.round(tc) + '°C');
      const tcol = (tc == null) ? '#bbb' : (tc >= (th.stop_c || 999) ? '#d65a5a' : (tc >= (th.resume_c || 0) ? '#d6a13a' : '#4a9d6a'));
      const st = _engineStatus(e);
      const dot = (st.label === '稼働中')
        ? '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:' + st.color + ';margin-right:5px;"></span>' : '';
      const badge = '<span style="color:' + st.color + ';font-weight:600;font-size:.92em;white-space:nowrap;">' + dot + st.label + '</span>';
      const tok = (u.prompt || 0) + (u.completion || 0);
      const btn = st.stopped
        ? '<button class="aibtn ai-eng-toggle" data-slug="' + _esc(e.slug) + '" data-stop="0" style="color:#2e7d32;">▶ 再開</button>'
        : '<button class="aibtn ai-eng-toggle" data-slug="' + _esc(e.slug) + '" data-stop="1" style="color:#c0392b;">■ 停止</button>';
      return '<tr style="border-bottom:1px solid #f0f2f4;">' +
        '<td style="padding:4px 6px;"><code>' + _esc(e.slug) + '</code></td>' +
        '<td style="padding:4px 6px;">' + badge + '</td>' +
        '<td style="padding:4px 6px;font-size:.85em;">' + _esc(e.model || '') + '</td>' +
        '<td style="padding:4px 6px;font-size:.82em;color:#777;">' + _esc(e.kind || '') + '</td>' +
        '<td style="padding:4px 6px;color:' + tcol + ';font-weight:600;">' + temp + '</td>' +
        '<td style="padding:4px 6px;text-align:right;">' + tok.toLocaleString() + '</td>' +
        '<td style="padding:4px 6px;text-align:center;">' + btn + '</td></tr>';
    }).join('');
  }

  async function _aiEngToggle(slug, stop) {
    try {
      await fetch('/engines/' + encodeURIComponent(slug) + '/' + (stop ? 'stop' : 'resume'), { method: 'POST' });
    } catch (e) { /* transient */ }
    loadAiActivity();
    loadAiEngines();  // immediate refresh; don't wait for the poll
  }

  function _aiEngWire() {
    if (_aiEngWired) return;
    const tb = document.getElementById('aiLiveEngines'); if (!tb) return;
    tb.addEventListener('click', (ev) => {
      const b = ev.target.closest('button.ai-eng-toggle');
      if (!b) return;
      _aiEngToggle(b.dataset.slug, b.dataset.stop === '1');
    });
    _aiEngWired = true;
  }

  // Move the existing #engines panel DOM (the full CRUD form) under the AI
  // tab's エンジン sub-pane on first activation. Idempotent: re-runs are
  // no-ops once mounted. Preserves all wiring + IDs so admin-presets-engines.js
  // listeners (newBtn / refresh / save / delete / stop-resume) keep working.
  function _aiMountEnginesPanel() {
    const mount = document.getElementById('aiEnginesMount');
    if (!mount || mount._mounted) return;
    const panel = document.querySelector('.panel[data-panel="engines"]');
    if (!panel) return;
    while (panel.firstChild) mount.appendChild(panel.firstChild);
    panel.style.display = 'none';
    mount._mounted = true;
  }

  // ===== 役割 (Roles): each AI job = an ORDERED priority list of engines =====
  // UI over Settings: each role writes {role}_engine_order (csv). Resolvers in
  // server/hub/_roles.py try the list top-down with thermal/stop failover.
  // Empty -> the role's legacy default (promoted / env / single setting).
  let _rolesEngines = [], _rolesSettings = {}, _rolesEditing = {};
  const _VIS_PAT = ['vl', 'vision', 'gpt-4o', 'claude-3', 'gemini', 'qwen3.5'];
  const _ROLE_DEFS = [
    { key: 'chat', setting: 'chat_engine_order', icon: 'lucide:message-square', name: 'チャット', code: 'page.ask / extract / observe', desc: '指定なし(auto)でページを読む・質問' },
    { key: 'codegen', setting: 'codegen_engine_order', icon: 'lucide:code-2', name: 'コード生成', code: 'codegen-loop', desc: 'スクリプト自動生成（Submit で上書き可）' },
    { key: 'page_agent', setting: 'page_agent_engine_order', icon: 'lucide:bot', name: '自律エージェント', code: 'page.agent()', desc: '空なら page.agent は無効' },
    { key: 'vision', setting: 'vision_engine_order', icon: 'lucide:eye', name: '視覚', code: 'perception', desc: 'ページを画像で見る（画像対応エンジンのみ）', vis: true },
    { key: 'judge', setting: 'judge_engine_order', icon: 'lucide:scale', name: '判定', code: 'judge', desc: 'codegen がゴール達成か採点', mode: 'reasoning_judge_mode', modes: ['off', 'on', 'shadow'], flags: [
      { key: 'judge_objective_gates_first', label: '客観gate優先', tip: '動画intent×assets数 など客観的に決まる場合は LLM 判定を呼ばずに確定（短絡）' },
      { key: 'judge_blind_mode', label: 'blind', tip: 'judge にはスクリプト本体・stdout/stderr を見せない（asset 数・終了コード・スクショ・perception だけで判定 = "evaluator-optimizer" 原則）' },
    ]},
    { key: 'distiller', setting: 'distiller_engine_order', icon: 'lucide:brain', name: '推論蒸留', code: 'distiller', desc: '失敗から壁・ホスト知識を学習', mode: 'reasoning_distiller_mode', modes: ['off', 'on', 'new'] },
    { key: 'translate', setting: 'translate_engine_order', icon: 'lucide:languages', name: '翻訳', code: 'POST /translate', desc: '#ai 作法モーダルの「翻訳」用。空ならチャット既定にフォールバック' },
  ];
  const _ROLE_BY_KEY = {}; _ROLE_DEFS.forEach(d => { _ROLE_BY_KEY[d.key] = d; });

  async function loadRoles() {
    try {
      const [er, sr] = await Promise.all([fetch('/engines'), fetch('/settings')]);
      _rolesEngines = ((await er.json()).engines) || [];
      _rolesSettings = ((await sr.json()).values) || {};
      _renderRoles();
    } catch (e) { /* transient */ }
  }
  function _engBySlug(sl) { return _rolesEngines.find(x => x.slug === sl); }
  function _enabledOf(sl) { const e = _engBySlug(sl); return e ? (e.enabled !== false) : true; }
  function _isCloud(e) { return !((String(e.gpu_temp_url || '').trim()) || ((e.gpu_temp_stop_c || 0) > 0)); }
  function _visSlugs() {
    return _rolesEngines.filter(e => _VIS_PAT.some(p => (e.model || '').toLowerCase().includes(p)))
      .sort((a, b) => (_isCloud(a) - _isCloud(b)) || a.slug.localeCompare(b.slug)).map(e => e.slug);
  }
  function _legacyDefault(key) {
    const s = _rolesSettings;
    if (key === 'chat') { const e = _rolesEngines.find(x => x.promoted && x.kind === 'chat'); return e ? [e.slug] : []; }
    if (key === 'page_agent') return s.worker_agent_engine_slug ? [s.worker_agent_engine_slug] : [];
    if (key === 'judge') return s.reasoning_judge_engine ? [s.reasoning_judge_engine] : [];
    if (key === 'distiller') return s.reasoning_distiller_engine ? [s.reasoning_distiller_engine] : [];
    if (key === 'vision') return _visSlugs();
    return [];
  }
  // Tier-aware CSV parse. `|` joins same-tier engines (load-balanced via
  // round-robin in server/hub/_roles.py); `,` separates priority tiers
  // (ranked fallback when the higher tier is throttled/disabled).
  // Returns [[slug,...], ...]; outer = tiers, inner = same-tier engines.
  function _roleOrderTiers(def) {
    const all = new Set(_rolesEngines.map(e => e.slug));
    const raw = _rolesSettings[def.setting] || '';
    let tiers = raw.split(',').map(t =>
      t.split('|').map(s => s.trim()).filter(s => s && all.has(s))
    ).filter(t => t.length);
    if (!tiers.length) tiers = _legacyDefault(def.key).filter(sl => all.has(sl)).map(sl => [sl]);
    return tiers;
  }
  function _flattenTiers(tiers) {
    return (tiers || []).filter(t => t && t.length).map(t => t.join('|')).join(',');
  }
  function _tiersFlat(tiers) {
    const out = []; tiers.forEach(t => t.forEach(s => out.push(s))); return out;
  }
  // Back-compat: callers that just want the flat slug list (no tier info).
  function _roleOrder(def) { return _tiersFlat(_roleOrderTiers(def)); }
  function _roleCard(def) {
    const tiers = _roleOrderTiers(def);
    const editing = !!_rolesEditing[def.key];
    const used = new Set(_tiersFlat(tiers));
    const pool = (def.vis ? _visSlugs() : _rolesEngines.map(e => e.slug)).filter(sl => !used.has(sl));
    const badge = (sl, isFirstTier) => '<span class="aibadge ' + (isFirstTier ? 'tier-cur' : 'tier-auto') + '"' + (_enabledOf(sl) ? '' : ' style="opacity:.5;text-decoration:line-through;"') + '>' + _esc(sl) + '</span>';
    const TIE_SEP = '<span style="color:#5b6770;margin:0 3px;font-size:11px;" title="同列（同優先度・ラウンドロビンで負荷分散）">⇄</span>';
    const RANK_SEP = '<span style="color:#9aa3ab;margin:0 3px;" title="フォールバック（上が全部過熱/停止のときだけ次へ）">›</span>';
    let control;
    if (editing) {
      const tierRows = tiers.map((tier, ti) => {
        const engs = tier.map((sl, ei) =>
          '<span class="aibadge ' + (ti === 0 ? 'tier-cur' : 'tier-auto') + '" style="min-width:120px;text-align:center;display:inline-flex;align-items:center;gap:4px;' + (_enabledOf(sl) ? '' : 'opacity:.5;text-decoration:line-through;') + '">' +
          '<span>' + _esc(sl) + '</span>' +
          '<button class="aibtn del ro-del" data-role="' + def.key + '" data-ti="' + ti + '" data-ei="' + ei + '" style="padding:0 5px;font-size:11px;line-height:1.4;" title="このエンジンをティアから外す">×</button>' +
          '</span>'
        ).join(TIE_SEP);
        const tierAddPool = pool.length
          ? '<select class="ro-add-to-tier ro-sel" data-role="' + def.key + '" data-ti="' + ti + '" title="このティア(同優先度)に追加して負荷分散"><option value="">＋同列…</option>' + pool.map(sl => '<option value="' + _esc(sl) + '">' + _esc(sl) + '</option>').join('') + '</select>'
          : '';
        const mergeBtn = ti > 0
          ? '<button class="aibtn ro-merge-up" data-role="' + def.key + '" data-ti="' + ti + '" title="上のティアと統合(同列・負荷分散)">⇧合体</button>'
          : '';
        return '<div style="display:flex;align-items:center;gap:5px;margin:2px 0;flex-wrap:wrap;">' +
          '<span style="font-size:11px;color:#9aa3ab;width:14px;text-align:right;">' + (ti + 1) + '.</span>' +
          '<div style="display:inline-flex;align-items:center;flex-wrap:wrap;">' + engs + '</div>' +
          '<button class="aibtn ro-tier-up" data-role="' + def.key + '" data-ti="' + ti + '"' + (ti === 0 ? ' disabled' : '') + ' title="このティアの優先度を上げる">▲</button>' +
          '<button class="aibtn ro-tier-down" data-role="' + def.key + '" data-ti="' + ti + '"' + (ti === tiers.length - 1 ? ' disabled' : '') + ' title="このティアの優先度を下げる">▼</button>' +
          mergeBtn + tierAddPool +
          '</div>';
      }).join('');
      const addNewTier = pool.length
        ? '<select class="ro-add ro-sel" data-role="' + def.key + '" title="末尾に新しい優先度ティアとして追加"><option value="">＋ 新ティア…</option>' + pool.map(sl => '<option value="' + _esc(sl) + '">' + _esc(sl) + '</option>').join('') + '</select>'
        : '';
      control = '<div style="display:flex;flex-direction:column;gap:2px;align-items:flex-end;">' + (tierRows || '<span style="font-size:12px;color:#9aa3ab;">なし</span>') + '<div style="margin-top:5px;display:flex;gap:6px;align-items:center;">' + addNewTier + '<button class="aibtn ro-done" data-role="' + def.key + '">完了</button></div></div>';
    } else {
      if (tiers.length) {
        const tierStrs = tiers.map((tier, ti) =>
          tier.map(sl => badge(sl, ti === 0)).join(TIE_SEP)
        );
        const chain = tierStrs.join(RANK_SEP);
        control = chain + ' <button class="aibtn ro-edit" data-role="' + def.key + '">編集</button>';
      } else {
        control = '<span style="font-size:12px;color:#9aa3ab;">（env 既定）</span> <button class="aibtn ro-edit" data-role="' + def.key + '">編集</button>';
      }
    }
    let modeSel = '';
    if (def.mode) {
      const cur = _rolesSettings[def.mode] || 'off';
      modeSel = ' <span class="ro-seg">' + def.modes.map(m => '<button type="button" class="ro-mode' + (m === cur ? ' on' : '') + '" data-mode="' + def.mode + '" data-val="' + m + '">' + m + '</button>').join('') + '</span>';
    }
    let flagsSel = '';
    if (def.flags && def.flags.length) {
      flagsSel = ' ' + def.flags.map(fl => {
        const on = !!_rolesSettings[fl.key];
        const style = on
          ? 'background:#1e6b3a;color:#fff;border:1px solid #155126;'
          : 'background:#f0f1f5;color:#6b7380;border:1px solid #d6dae0;';
        return '<button type="button" class="ro-flag' + (on ? ' on' : '') + '" data-flag="' + fl.key + '" data-val="' + (on ? 'off' : 'on') + '" title="' + _esc(fl.tip) + '" style="' + style + 'padding:2px 8px;font-size:0.76em;border-radius:6px;cursor:pointer;margin-left:4px;">' + (on ? '✓ ' : '☐ ') + _esc(fl.label) + '</button>';
      }).join('');
    }
    return '<div style="display:flex;align-items:' + (editing ? 'flex-start' : 'center') + ';gap:14px;border:1px solid ' + (def.vis ? '#b5d4f4' : '#e2e6ea') + ';border-radius:10px;padding:10px 14px;margin-bottom:9px;background:#fff;">' +
      '<iconify-icon icon="' + def.icon + '" style="font-size:20px;color:#5b6770;margin-top:' + (editing ? '2px' : '0') + ';"></iconify-icon>' +
      '<div style="flex:1;min-width:0;"><div style="font-size:14.5px;font-weight:600;">' + def.name + ' <span style="font-size:11.5px;color:#9aa3ab;font-weight:400;font-family:monospace;">' + def.code + '</span>' + modeSel + flagsSel + '</div>' +
      '<div style="font-size:12px;color:#6b7680;">' + def.desc + '</div></div>' +
      '<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;justify-content:flex-end;">' + control + '</div></div>';
  }
  function _renderRoles() {
    const el = document.getElementById('aiRolesPanel'); if (!el) return;
    el.innerHTML = _ROLE_DEFS.map(_roleCard).join('') +
      '<div style="font-size:11.5px;color:#9aa3ab;margin-top:8px;line-height:1.5;">▲▼ で優先度ティアを並べ替え、× でエンジン除外。＋同列で同優先度に追加（同ティア内はラウンドロビンで負荷分散）、＋新ティアで次の優先度を追加、⇧合体で上のティアに統合。上のティアから試し、全エンジンが過熱/停止のときだけ次のティアへ。空欄は従来の既定（promoted / env / 単一設定）にフォールバック。</div>';
    _wireRoles();
  }
  async function _putSetting(obj) { try { await fetch('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) }); } catch (e) { /* transient */ } }
  async function _saveOrder(def, tiers) { const o = {}; o[def.setting] = _flattenTiers(tiers); await _putSetting(o); }
  function _wireRoles() {
    const el = document.getElementById('aiRolesPanel'); if (!el || el._wired) return; el._wired = true;
    el.addEventListener('click', async (ev) => {
      const b = ev.target.closest('button'); if (!b) return;
      const role = b.dataset.role, def = _ROLE_BY_KEY[role];
      if (b.classList.contains('ro-edit')) { _rolesEditing[role] = true; _renderRoles(); return; }
      if (b.classList.contains('ro-done')) { _rolesEditing[role] = false; _renderRoles(); return; }
      if (b.classList.contains('ro-mode')) { const o = {}; o[b.dataset.mode] = b.dataset.val; await _putSetting(o); loadRoles(); return; }
      if (b.classList.contains('ro-flag')) { const o = {}; o[b.dataset.flag] = (b.dataset.val === 'on'); await _putSetting(o); loadRoles(); return; }
      if (!def) return;
      const tiers = _roleOrderTiers(def).map(t => t.slice());
      const ti = parseInt(b.dataset.ti, 10);
      if (isNaN(ti)) return;
      if (b.classList.contains('ro-tier-up') && ti > 0) {
        const t = tiers[ti]; tiers[ti] = tiers[ti - 1]; tiers[ti - 1] = t;
      } else if (b.classList.contains('ro-tier-down') && ti < tiers.length - 1) {
        const t = tiers[ti]; tiers[ti] = tiers[ti + 1]; tiers[ti + 1] = t;
      } else if (b.classList.contains('ro-merge-up') && ti > 0) {
        tiers[ti - 1] = tiers[ti - 1].concat(tiers[ti]);
        tiers.splice(ti, 1);
      } else if (b.classList.contains('ro-del')) {
        const ei = parseInt(b.dataset.ei, 10);
        if (isNaN(ei)) return;
        tiers[ti].splice(ei, 1);
        if (!tiers[ti].length) tiers.splice(ti, 1);
      } else return;
      await _saveOrder(def, tiers); loadRoles();
    });
    el.addEventListener('change', async (ev) => {
      const sel = ev.target;
      if (!sel.classList) return;
      const def = _ROLE_BY_KEY[sel.dataset.role]; if (!def) return;
      if (sel.classList.contains('ro-add') && sel.value) {
        // Append as a NEW lowest-priority tier (fallback).
        const tiers = _roleOrderTiers(def).map(t => t.slice());
        tiers.push([sel.value]);
        await _saveOrder(def, tiers); loadRoles();
      } else if (sel.classList.contains('ro-add-to-tier') && sel.value) {
        // Add to an existing tier (same priority = load-balanced).
        const ti = parseInt(sel.dataset.ti, 10);
        if (isNaN(ti)) return;
        const tiers = _roleOrderTiers(def).map(t => t.slice());
        if (!tiers[ti]) return;
        tiers[ti].push(sel.value);
        await _saveOrder(def, tiers); loadRoles();
      }
    });
  }

  function _esc(s) { return (s == null ? '' : ('' + s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

  function _aiRow(x, kind) {
    const pct = x.success_rate == null ? null : Math.round(x.success_rate * 100);
    const bar = pct == null
      ? '<span style="color:#aaa;">— untried</span>'
      : '<div class="airate"><div class="airate-fill" style="width:' + pct + '%; background:' +
        (pct >= 50 ? '#4a9d6a' : (pct >= 20 ? '#d6a13a' : '#d65a5a')) + ';"></div><span>' +
        pct + '% (' + (x.success_count || 0) + '/' + (x.use_count || 0) + ')</span></div>';
    const tierBadge = '<span class="aibadge ' + (x.tier === 'curated' ? 'tier-cur' : 'tier-auto') + '">' + _esc(x.tier || 'auto') + '</span>';
    // Audit warning: an `auto`-tier skill/convention that has been used
    // a lot WITHOUT operator promotion is a prompt-injection-style
    // concern (0xCodez 14-step Tier 3 — "audit skill sources, skills
    // are prompt-injection vectors"). Surface a warning badge so the
    // operator notices and either promotes (curated = reviewed) or
    // deletes. Threshold = Settings.skill_audit_warn_threshold.
    const _auditThr = Number(_rolesSettings && _rolesSettings.skill_audit_warn_threshold);
    const auditBadge = (x.tier !== 'curated' && _auditThr > 0 && Number(x.use_count || 0) >= _auditThr)
      ? '<span class="aibadge" style="background:#fff4d6; color:#7a4a14; border:1px solid #d6a13a; margin-left:4px;" title="auto-tier のまま ' + _auditThr + ' 回以上使われています。注入経路として安全か内容を確認(promote)してください">⚠ 監査要</span>'
      : '';
    const slug = _esc(x.slug);
    const desc = _esc((x.description || x.advice || '').slice(0, 100));
    const tierBtn = x.tier === 'curated'
      ? '<button class="aibtn" data-act="demote" data-kind="' + kind + '" data-slug="' + slug + '">demote</button>'
      : '<button class="aibtn" data-act="promote" data-kind="' + kind + '" data-slug="' + slug + '">promote</button>';
    const delBtn = '<button class="aibtn del" data-act="delete" data-kind="' + kind + '" data-slug="' + slug + '">delete</button>';
    // Skills ARE code -- give each one a button to view its code_template.
    // Conventions ARE prose advice + good/bad examples -- give each one a
    // matching button so the operator can read the distilled content and
    // judge whether it's worth promoting / keeping.
    const codeBtn = kind === 'skills'
      ? '<button class="aibtn" data-act="code" data-kind="skills" data-slug="' + slug + '" title="このスキルのコード(code_template)を表示"><iconify-icon icon="lucide:code-2"></iconify-icon> コード</button>'
      : kind === 'conventions'
      ? '<button class="aibtn" data-act="detail" data-kind="conventions" data-slug="' + slug + '" title="この作法の蒸留内容 (advice / good/bad example / 由来) を表示"><iconify-icon icon="lucide:scroll-text"></iconify-icon> 内容</button>'
      : '';
    return '<tr title="' + desc + '"><td><code>' + slug + '</code></td><td>' + tierBadge + auditBadge + '</td>' +
      '<td class="num">' + (x.use_count || 0) + '</td><td class="num">' + (x.success_count || 0) + '</td>' +
      '<td>' + bar + '</td><td>' + codeBtn + tierBtn + delBtn + '</td></tr>';
  }

  async function aiAction(kind, slug, act) {
    if (act === 'code') { openSkillCode(slug); return; }
    if (act === 'detail') { openConventionDetail(slug); return; }
    if (act === 'delete' && !confirm('Delete ' + kind + ' "' + slug + '"?')) return;
    const enc = encodeURIComponent(slug);
    const method = act === 'delete' ? 'DELETE' : 'POST';
    const path = act === 'delete' ? '/' + kind + '/' + enc : '/' + kind + '/' + enc + '/' + act;
    try {
      const r = await fetch(path, { method });
      if (!r.ok) { alert(act + ' failed: HTTP ' + r.status); return; }
    } catch (e) { alert(act + ' failed: ' + e); return; }
    loadSkillsConventions();
  }

  // ---- Skill code viewer (a skill = reusable code_template) -----------
  let _skillCodeCurrent = '';
  async function openSkillCode(slug) {
    const modal = document.getElementById('skillCodeModal');
    if (!modal) return;
    const $ = (id) => document.getElementById(id);
    $('skillCodeTitle').textContent = slug;
    $('skillCodeMeta').textContent = '';
    $('skillCodeDesc').innerHTML = '';
    $('skillCodeBody').textContent = '読み込み中…';
    $('skillCodeInstr').textContent = '';
    $('skillCodeProv').textContent = '';
    _skillCodeCurrent = '';
    modal.style.display = 'flex';
    // Reflect the open skill in the address bar so #ai/skill/<slug> is
    // shareable / survives reload (no-op when opened via that deep-link).
    try { if (typeof _entityHashSync === 'function') _entityHashSync('ai', 'skill/' + slug); } catch (_e) {}
    try {
      const r = await fetch('/skills/' + encodeURIComponent(slug));
      if (!r.ok) { $('skillCodeBody').textContent = '取得失敗 (HTTP ' + r.status + ')'; return; }
      const s = await r.json();
      $('skillCodeTitle').textContent = (s.name || s.slug || slug);
      const p = s.success_rate == null ? '—' : Math.round(s.success_rate * 100) + '%';
      $('skillCodeMeta').textContent = (s.tier || 'auto') + ' · fitness ' + p +
        ' (' + (s.success_count || 0) + '/' + (s.use_count || 0) + ')';
      let dh = '';
      if (s.description) dh += '<div style="margin-bottom:6px; color:#333;">' + _esc(s.description) + '</div>';
      if ((s.applicable_when || []).length) dh += '<div style="font-size:.85em; color:#555;"><b>使う条件:</b> ' + s.applicable_when.map(_esc).join(' ／ ') + '</div>';
      if ((s.tags || []).length) dh += '<div style="margin-top:5px;">' + s.tags.map(function (t) { return '<span class="aibadge tier-auto" style="margin-right:4px;">' + _esc(t) + '</span>'; }).join('') + '</div>';
      $('skillCodeDesc').innerHTML = dh;
      _skillCodeCurrent = s.code_template || '';
      $('skillCodeBody').textContent = _skillCodeCurrent || '(コードなし)';
      $('skillCodeInstr').textContent = s.llm_instructions || '(指示文なし)';
      const prov = (s.extracted_from || []);
      if (prov.length) $('skillCodeProv').textContent = '由来ジョブ (' + prov.length + '): ' + prov.slice(0, 8).map(_esc).join(', ') + (prov.length > 8 ? ' …' : '');
    } catch (e) {
      $('skillCodeBody').textContent = '取得失敗: ' + (e && e.message ? e.message : e);
    }
  }

  // ---- Convention detail viewer (operator-readable distillation) ----------
  // Conventions are prose advice + bad/good code snippets distilled by the
  // codegen-loop. Surfacing them in a modal lets the operator actually read
  // and rate them (same goal as openSkillCode for skills).
  let _convTranslated = false;  // toggle state for the current modal session
  async function openConventionDetail(slug) {
    const modal = document.getElementById('convDetailModal');
    if (!modal) return;
    const $ = (id) => document.getElementById(id);
    // Reset translation toggle state when re-opening on a different row.
    _convTranslated = false;
    _convResetTranslateBtn();
    $('convDetailTitle').textContent = slug;
    $('convDetailMeta').textContent = '';
    $('convDetailAdvice').textContent = '読み込み中…';
    $('convDetailRationale').textContent = '';
    $('convDetailWhen').innerHTML = '';
    $('convDetailBad').textContent = '';
    $('convDetailGood').textContent = '';
    $('convDetailTags').innerHTML = '';
    $('convDetailProv').textContent = '';
    modal.style.display = 'flex';
    try {
      const r = await fetch('/conventions/' + encodeURIComponent(slug));
      if (!r.ok) { $('convDetailAdvice').textContent = '取得失敗 (HTTP ' + r.status + ')'; return; }
      const c = await r.json();
      $('convDetailTitle').textContent = c.name || c.slug || slug;
      const p = c.success_rate == null ? '—' : Math.round(c.success_rate * 100) + '%';
      $('convDetailMeta').textContent = (c.tier || 'auto') + ' · fitness ' + p +
        ' (' + (c.success_count || 0) + '/' + (c.use_count || 0) + ')';
      $('convDetailAdvice').textContent = c.advice || '(advice なし)';
      $('convDetailRationale').textContent = c.rationale || '(rationale なし)';
      const when = c.applicable_when || [];
      $('convDetailWhen').innerHTML = when.length
        ? when.map(function (w) { return '<span class="aibadge tier-auto" style="margin:2px 4px 2px 0;">' + _esc(w) + '</span>'; }).join('')
        : '<span style="color:#999;">—</span>';
      $('convDetailBad').textContent = c.bad_example || '(bad_example なし)';
      $('convDetailGood').textContent = c.good_example || '(good_example なし)';
      const tags = c.tags || [];
      if (tags.length) $('convDetailTags').innerHTML = '<b style="color:#666;">tags:</b> ' +
        tags.map(function (t) { return '<span class="aibadge tier-auto" style="margin:0 4px;">' + _esc(t) + '</span>'; }).join('');
      const prov = c.extracted_from || [];
      if (prov.length) $('convDetailProv').textContent = '由来ジョブ (' + prov.length + '): ' + prov.slice(0, 8).map(_esc).join(', ') + (prov.length > 8 ? ' …' : '');
    } catch (e) {
      $('convDetailAdvice').textContent = '取得失敗: ' + (e && e.message ? e.message : e);
    }
  }

  // ---- Convention 翻訳 (hub-side LLM + cross-hub MariaDB cache) -----------
  // Translates advice / rationale / applicable_when via POST /translate
  // (chat Promoted engine, thermal/stop failover, sha256(text)+lang cache).
  // Bad/good_example stay verbatim — they're Python code; identifier
  // translation would break the example. Re-opens hit the cache instantly.
  function _convResetTranslateBtn() {
    const lbl = document.getElementById('convDetailTranslateLabel');
    if (lbl) lbl.textContent = '翻訳';
  }
  function _convTargetLang() {
    try {
      const il = window.i18next && window.i18next.language;
      if (il) return String(il).split('-')[0];
    } catch (_) {}
    return (navigator.language || 'en').split('-')[0];
  }
  async function _convTranslateModal() {
    const $ = (id) => document.getElementById(id);
    const lblEl = $('convDetailTranslateLabel');
    const adv = $('convDetailAdvice');
    const rat = $('convDetailRationale');
    const whn = $('convDetailWhen');
    if (!adv || !rat || !whn) return;
    // Toggle back to the originals -- they were stashed on first translate.
    if (_convTranslated) {
      if (adv.dataset.orig != null) adv.textContent = adv.dataset.orig;
      if (rat.dataset.orig != null) rat.textContent = rat.dataset.orig;
      if (whn.dataset.orig != null) whn.innerHTML = whn.dataset.orig;
      _convTranslated = false;
      if (lblEl) lblEl.textContent = '翻訳';
      return;
    }
    const tgt = _convTargetLang();
    if (tgt === 'en') { alert('表示言語が英語のため、翻訳は不要です。'); return; }
    if (lblEl) lblEl.textContent = '翻訳中…';
    // Stash originals so the toggle can restore them.
    adv.dataset.orig = adv.textContent;
    rat.dataset.orig = rat.textContent;
    whn.dataset.orig = whn.innerHTML;
    const badges = Array.from(whn.querySelectorAll('.aibadge'));
    const badgeTexts = badges.map(b => b.textContent || '');
    const texts = [adv.textContent, rat.textContent, ...badgeTexts];
    try {
      const r = await fetch('/translate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts, target_lang: tgt }),
      });
      if (!r.ok) {
        const msg = r.status === 503 ? '翻訳機能が無効か、利用可能な chat エンジンがありません (HTTP 503)' : '翻訳に失敗 (HTTP ' + r.status + ')';
        throw new Error(msg);
      }
      const d = await r.json();
      const out = d.translated || [];
      adv.textContent = out[0] || adv.textContent;
      rat.textContent = out[1] || rat.textContent;
      badges.forEach((b, i) => { b.textContent = out[2 + i] || badgeTexts[i]; });
      _convTranslated = true;
      if (lblEl) lblEl.textContent = '原文に戻す';
      // Soft hint when most cells came from cache (re-open).
      const hits = (d.cache_hits || []).filter(Boolean).length;
      if (hits === texts.length && lblEl) lblEl.title = 'cache hit · 0 LLM call';
    } catch (e) {
      // Roll back the originals so the operator isn't left with empty cells.
      if (adv.dataset.orig != null) adv.textContent = adv.dataset.orig;
      if (rat.dataset.orig != null) rat.textContent = rat.dataset.orig;
      if (whn.dataset.orig != null) whn.innerHTML = whn.dataset.orig;
      if (lblEl) lblEl.textContent = '翻訳';
      alert('翻訳に失敗しました: ' + (e && e.message ? e.message : e));
    }
  }

  // ---- Deep-link entry point: #ai/<kind>/<slug> -> open that entity ----
  // Registered with the core router via window.aiOpenEntity (admin-core.js
  // _entityDeepLinkOpeners), so a pasted / shared / reloaded
  //   #ai/skill/<slug>        -> Skills sub-tab + code modal
  //   #ai/host/<host>         -> Knowledge sub-tab + host-knowledge modal
  //   #ai/convention/<slug>   -> Conventions sub-tab
  // lands directly on the entity. ``entityId`` is everything after "#ai/".
  function aiOpenEntity(entityId) {
    const s = String(entityId == null ? '' : entityId);
    const i = s.indexOf('/');
    const kind = (i >= 0 ? s.slice(0, i) : 'skill').toLowerCase();
    const id = i >= 0 ? s.slice(i + 1) : s;
    if (!id) return;
    if (kind === 'engines' || s === 'engines') {
      setAiSubtab('engines');
      return;
    }
    if (kind === 'engine') {
      setAiSubtab('engines');
      // selectEngine is defined in admin-presets-engines.js (global) after
      // loadEngines populates the records; wait briefly for both.
      let tries = 12;
      const open = () => {
        if (typeof selectEngine === 'function' && Array.isArray(window.ENGINES_STATE && ENGINES_STATE.records) && ENGINES_STATE.records.length) {
          selectEngine(id); return;
        }
        if (--tries > 0) setTimeout(open, 400);
      };
      open();
      return;
    }
    if (kind === 'host' || kind === 'hosts' || kind === 'knowledge') {
      setAiSubtab('knowledge');
      // _hkData is empty on a cold deep-link; setAiSubtab() kicked off
      // loadKnowledge() which fills it async, so retry briefly.
      let tries = 12;
      const open = () => {
        if (_hkData.find(e => e.host === id)) { openHkModal(id); return; }
        if (--tries > 0) setTimeout(open, 400);
      };
      open();
    } else if (kind === 'convention' || kind === 'conventions') {
      setAiSubtab('conventions');
    } else {
      // skill (default): openSkillCode fetches /skills/{slug} directly, so
      // this works cold without the list having rendered first.
      setAiSubtab('skills');
      openSkillCode(id);
    }
  }
  try { window.aiOpenEntity = aiOpenEntity; } catch (_e) {}

  // ---- Grooming sub-tab: retire + dedup candidates + auto toggles ----
  // Open the kind-appropriate detail modal so the operator can see the
  // distilled content (skill code or convention prose) before deciding
  // whether to keep/delete/merge a grooming candidate.
  function _aiOpenItemDetail(kind, slug) {
    if (!slug) return;
    if (kind === 'skills' || kind === 'skill') { openSkillCode(slug); return; }
    if (kind === 'conventions' || kind === 'convention') { openConventionDetail(slug); return; }
  }
  function _groomDetailBtn(kind, slug) {
    const label = (kind === 'skills' || kind === 'skill') ? 'コード' : '内容';
    const icon = (kind === 'skills' || kind === 'skill') ? 'lucide:code-2' : 'lucide:scroll-text';
    return '<button class="aibtn gr-view" data-act="view" data-kind="' + _esc(kind) + '" data-slug="' + _esc(slug) +
      '" title="この ' + label + ' の蒸留結果を表示"><iconify-icon icon="' + icon + '"></iconify-icon> ' + label + '</button>';
  }
  function _groomSlugLink(kind, slug) {
    return '<code class="gr-slug" data-kind="' + _esc(kind) + '" data-slug="' + _esc(slug) +
      '" style="cursor:pointer; text-decoration:underline dotted;" title="蒸留結果を表示">' + _esc(slug) + '</code>';
  }

  function _groomRetireRow(x) {
    const slug = _esc(x.slug);
    const auto = (x.tier === 'auto');
    const tierBadge = '<span class="aibadge ' + (auto ? 'tier-auto' : 'tier-cur') + '">' + _esc(x.tier) + '</span>';
    const viewBtn = _groomDetailBtn(x.kind, x.slug);
    const delOrLock = auto
      ? '<button class="aibtn del" data-act="delete" data-kind="' + _esc(x.kind) + '" data-slug="' + slug + '">delete</button>'
      : '<span style="color:#999; font-size:.82em;">curated — 手動</span>';
    return '<tr><td>' + _esc(x.kind) + '</td><td>' + _groomSlugLink(x.kind, x.slug) + '</td><td>' + tierBadge + '</td>' +
      '<td class="gr-reason-cell">' + _esc(x.reason) + '</td><td class="num">' + (x.use_count || 0) + '</td>' +
      '<td class="num">' + (x.success_count || 0) + '</td><td style="white-space:nowrap;">' + viewBtn + ' ' + delOrLock + '</td></tr>';
  }

  function _groomDedupRow(x) {
    const dropLinks = (x.drops || []).map(d => _groomSlugLink(x.kind, d)).join(', ');
    const act = '<button class="aibtn" data-act="merge" data-kind="' + _esc(x.kind) +
      '" data-keep="' + _esc(x.keep) + '" data-drops="' + _esc((x.drops || []).join(',')) + '">merge</button>';
    return '<tr><td>' + _esc(x.kind) + '</td><td>' + _groomSlugLink(x.kind, x.keep) + '</td>' +
      '<td style="color:#933;">' + dropLinks + '</td><td>' + act + '</td></tr>';
  }

  async function loadGrooming() {
    const retire = [], dedup = [];
    try {
      const r = await fetch('/ai/groom-candidates');
      const j = await r.json();
      for (const [sing, plural] of [['skill', 'skills'], ['convention', 'conventions']]) {
        const sec = j[sing] || { retire: [], dedup: [] };
        (sec.retire || []).forEach(x => retire.push(Object.assign({ kind: plural }, x)));
        (sec.dedup || []).forEach(x => dedup.push(Object.assign({ kind: plural }, x)));
      }
    } catch (e) { /* leave empty */ }
    _aiState['groom-retire'].items = retire; _aiState['groom-retire'].page = 0; _renderAiPage('groom-retire');
    _aiState['groom-dedup'].items = dedup;   _aiState['groom-dedup'].page = 0;   _renderAiPage('groom-dedup');
    const gc = document.getElementById('cntGroom'); if (gc) gc.textContent = retire.length + dedup.length;
    // Surface a 1-line state so the operator can tell at-a-glance the panel
    // IS live + the reaper IS scanning.
    // Build a rich state line so the operator can SEE the reaper is alive,
    // when it last ran, when it'll run next, and (importantly) why the
    // candidate list might be 0 even though everything is healthy.
    try {
      const gs = document.getElementById('groomState');
      if (gs) {
        let st = null;
        try { st = await (await fetch('/ai/grooming-status')).json(); } catch (_e) { st = null; }
        const lines = [];
        // Line 1: liveness.
        const livePart = (st && st.last_run_at)
          ? '<span title="' + _esc(st.last_run_at) + '"><iconify-icon icon="lucide:radio-tower" style="color:#196b2c; vertical-align:-2px;"></iconify-icon> reaper 稼働中 · 最終 ' + _aiAgoUtc(st.last_run_at) + '前</span>'
          : '<span style="color:#a06000;"><iconify-icon icon="lucide:loader-circle" style="vertical-align:-2px;"></iconify-icon> 起動直後 — 最初のスキャン待ち</span>';
        let nextPart = '';
        if (st && st.next_run_at) nextPart = ' · 次回 ' + _aiAgoUtc(st.next_run_at, true);
        const autoR = st && st.auto_retire_enabled ? '<b style="color:#196b2c;">ON</b>' : '<b style="color:#933;">OFF</b>';
        const autoD = st && st.auto_dedup_enabled  ? '<b style="color:#196b2c;">ON</b>' : '<b style="color:#933;">OFF</b>';
        lines.push(livePart + nextPart + ' · auto-retire ' + autoR + ' · auto-dedup ' + autoD);
        // Line 2: candidate counts.
        lines.push('retire 候補 <b>' + retire.length + '</b> · dedup 候補 <b>' + dedup.length + '</b>');
        // Line 3: explain WHY the count might be 0.
        if (st && st.last_pass && st.last_pass.by_kind) {
          const sk = st.last_pass.by_kind.skill || {};
          const cv = st.last_pass.by_kind.convention || {};
          const guards = [];
          if (sk.cold_start_skip_reason) guards.push('skill: ' + sk.cold_start_skip_reason);
          if (cv.cold_start_skip_reason) guards.push('convention: ' + cv.cold_start_skip_reason);
          if (retire.length === 0 && dedup.length === 0 && guards.length) {
            lines.push('<span style="color:#a06000;"><iconify-icon icon="lucide:shield-alert" style="vertical-align:-2px;"></iconify-icon> 候補ゼロの理由: ' + guards.map(_esc).join(' / ') + '</span>');
          }
          // Always show what the last pass scanned (records / dud-allow).
          const scan = (sk.records != null) ? 'skill ' + sk.records + ' 件 (累計成功 ' + (sk.total_success || 0) + ', dud判定=' + (sk.allow_dud ? 'ON' : 'OFF') + ')' : '';
          const scan2 = (cv.records != null) ? ' / convention ' + cv.records + ' 件 (累計成功 ' + (cv.total_success || 0) + ', dud判定=' + (cv.allow_dud ? 'ON' : 'OFF') + ')' : '';
          if (scan || scan2) lines.push('<span style="color:#666;">前回スキャン対象: ' + scan + scan2 + '</span>');
        }
        gs.innerHTML = lines.join('<br>');
        gs.style.display = 'block';
        gs.style.width = '100%';
        gs.style.marginTop = '6px';
        gs.style.lineHeight = '1.6';
      }
    } catch (_e) {}
    // Reflect the auto-* toggles from hub settings.
    try {
      const s = await (await fetch('/settings')).json();
      const v = s.values || {};
      const tr = document.getElementById('tglAutoRetire'); if (tr) tr.checked = !!v.auto_retire_enabled;
      const td = document.getElementById('tglAutoDedup');  if (td) td.checked = !!v.auto_dedup_enabled;
    } catch (e) { /* ignore */ }
  }

  async function _groomMerge(kind, keep, dropsCsv) {
    const drops = (dropsCsv || '').split(',').filter(Boolean);
    if (!drops.length) return;
    if (!confirm('Merge ' + drops.length + ' ' + kind + ' into "' + keep + '"?')) return;
    try {
      const r = await fetch('/' + kind + '/merge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keep: keep, drops: drops }),
      });
      if (!r.ok) { alert('merge failed: HTTP ' + r.status); return; }
    } catch (e) { alert('merge failed: ' + e); return; }
    loadGrooming();
  }

  async function _setAutoToggle(key, on) {
    try {
      const body = {}; body[key] = on;
      await fetch('/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (e) { alert('toggle failed: ' + e); }
  }

  // ---- Oracle sub-tab: L1 ffprobe re-probe of stored video assets ----
  function _humanBytes(b) {
    if (b == null) return '—';
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }

  function _oracleRow(x) {
    const ok = x.valid;
    const validMark = ok
      ? '<span style="color:#196b2c; font-weight:700; font-size:1.05em;">✓</span>'
      : '<span style="color:#c0392b; font-weight:700; font-size:1.05em;">✗</span>';
    const dur = x.duration_s != null ? x.duration_s + 's' : '—';
    const dims = (x.width && x.height) ? x.width + '×' + x.height : '—';
    const jid = _esc(x.job_id || '');
    const short = jid.slice(0, 8);
    const name = _esc(x.name || '');
    const reason = _esc(x.reason || '');
    return '<tr>' +
      '<td><a href="#live/' + jid + '" style="font-family:monospace;font-size:.83em;" title="このジョブの Live パネル (Log+noVNC+Code+Gallery) を開く  job_id=' + jid + '">' + short + '…</a></td>' +
      '<td style="font-size:.83em;font-family:monospace;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + name + ' — クリックで /jobs/' + jid + '/result.json (アセット+raw 蒸留結果) を新タブで開く">' +
        '<a href="/jobs/' + jid + '/result" target="_blank" style="color:inherit;text-decoration:none;">' + name + '</a></td>' +
      '<td style="text-align:center;">' + validMark + '</td>' +
      '<td class="num">' + dur + '</td>' +
      '<td>' + _esc(x.codec || '—') + '</td>' +
      '<td class="num">' + dims + '</td>' +
      '<td class="num">' + _humanBytes(x.bytes) + '</td>' +
      '<td class="orc-reason-cell" style="font-size:.82em;color:' + (ok ? '#196b2c' : '#933') + ';" title="' + reason + '">' + reason + '</td>' +
      '</tr>';
  }

  // Common: translate every cell matching ``selector`` via /translate, stash
  // the original on ``dataset.orig`` so a second click can restore. Used by
  // both grooming (reason col) and oracle (reason col).
  async function _translateTableCells(rootSelector, btnId, targetLang) {
    const cells = Array.from(document.querySelectorAll(rootSelector));
    if (!cells.length) return;
    const btn = document.getElementById(btnId);
    const lblEl = btn ? btn.querySelector('span') : null;
    // Toggle back to originals.
    const allHaveOrig = cells.every(c => c.dataset.orig != null);
    const anyTranslated = cells.some(c => c.dataset.translated === '1');
    if (anyTranslated && allHaveOrig) {
      cells.forEach(c => {
        c.textContent = c.dataset.orig;
        c.removeAttribute('data-translated');
      });
      if (btn) btn.title = btn.dataset.titleOrig || btn.title;
      return;
    }
    const texts = cells.map(c => c.textContent || '');
    if (btn) { btn.disabled = true; if (lblEl) lblEl.textContent = '翻訳中…'; }
    try {
      const r = await fetch('/translate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts, target_lang: targetLang || _convTargetLang() }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const out = d.translated || [];
      cells.forEach((c, i) => {
        if (c.dataset.orig == null) c.dataset.orig = c.textContent;
        c.textContent = out[i] || c.textContent;
        c.dataset.translated = '1';
      });
      if (btn) {
        btn.dataset.titleOrig = btn.title || '';
        const hits = (d.cache_hits || []).filter(Boolean).length;
        btn.title = '翻訳済み (' + hits + '/' + texts.length + ' cache hit) — engine: ' + (d.engine_slug || '?');
      }
    } catch (e) {
      alert('翻訳に失敗: ' + (e && e.message ? e.message : e));
    } finally {
      if (btn) { btn.disabled = false; if (lblEl) lblEl.textContent = '翻訳'; }
    }
  }

  let _oracleLoading = false;
  async function loadOracle() {
    if (_oracleLoading) return;
    _oracleLoading = true;
    const tbody = document.querySelector('#oracleTable tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan=8 class="empty">scanning… (ffprobe 実行中)</td></tr>';
    try {
      const r = await fetch('/ai/oracle-stats?limit=200');
      if (!r.ok) {
        if (tbody) tbody.innerHTML = '<tr><td colspan=8 class="empty">error: HTTP ' + r.status + '</td></tr>';
        return;
      }
      const j = await r.json();
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v != null ? v : '—'); };
      set('oracleTotal',    j.total);
      set('oracleValid',    j.valid);
      set('oracleInvalid',  j.invalid);
      set('oracleValidPct', j.valid_pct != null ? Math.round(j.valid_pct * 100) + '%' : '—');
      const cnt = document.getElementById('cntOracle'); if (cnt) cnt.textContent = j.total;
      // By-reason summary chip row
      const br = document.getElementById('oracleByReason');
      if (br && j.by_reason) {
        br.innerHTML = Object.entries(j.by_reason)
          .sort((a, b) => b[1] - a[1])
          .map(([k, v]) => '<span style="display:inline-block;margin:0 3px;padding:1px 7px;border-radius:10px;font-size:.82em;background:' +
            (k === 'ok' ? '#eef8ee' : '#fdecea') + ';color:' + (k === 'ok' ? '#196b2c' : '#933') + ';border:1px solid ' +
            (k === 'ok' ? '#7ab68a' : '#e8b4b0') + ';">' + _esc(k) + ':' + v + '</span>')
          .join('');
      }
      _aiState['oracle'].items = j.files || [];
      _aiState['oracle'].page  = 0;
      _renderAiPage('oracle');
      try {
        const os = document.getElementById('oracleState');
        if (os) {
          const total = j.total || 0, valid = j.valid || 0, invalid = j.invalid || 0;
          const pct = j.valid_pct != null ? Math.round(j.valid_pct * 100) : null;
          const ico = invalid === 0
            ? '<iconify-icon icon="lucide:shield-check" style="color:#196b2c; vertical-align:-2px;"></iconify-icon>'
            : '<iconify-icon icon="lucide:alert-triangle" style="color:#b35900; vertical-align:-2px;"></iconify-icon>';
          const healthMsg = invalid === 0 && total > 0
            ? '<span style="color:#196b2c;">健全 (invalid 0/' + total + ')</span>'
            : (total === 0
                ? '<span style="color:#a06000;">直近に動画アセット無し</span>'
                : '<span style="color:#b35900;">invalid ' + invalid + '/' + total + ' を検出 (要調査)</span>');
          os.innerHTML = ico + ' ffprobe 稼働中 · 最終スキャン ' + new Date().toLocaleTimeString()
            + ' · ' + healthMsg + (pct != null ? ' · valid ' + pct + '%' : '');
        }
      } catch (_e) {}
    } catch (e) {
      if (tbody) tbody.innerHTML = '<tr><td colspan=8 class="empty">error: ' + _esc('' + e) + '</td></tr>';
    } finally {
      _oracleLoading = false;
    }
  }

  function renderSummary() {
    const tiers = { high:0, medium:0, low:0, stale:0 };
    let barriersTotal = 0;
    let extractionsTotal = 0;
    for (const e of _hkData) {
      const t = (e.k.stats || {}).overall_confidence || 'low';
      tiers[t] = (tiers[t] || 0) + 1;
      const barriers = ((e.k.per_page || {}).barriers || {});
      barriersTotal += Object.values(barriers).filter(b => b && b.present).length;
      extractionsTotal += ((e.k.per_page || {}).content_extraction || []).length;
    }
    document.getElementById('hkTotal').textContent = _hkData.length;
    document.getElementById('hkHigh').textContent = tiers.high;
    document.getElementById('hkMedium').textContent = tiers.medium;
    document.getElementById('hkLow').textContent = tiers.low;
    document.getElementById('hkStale').textContent = tiers.stale;
    document.getElementById('hkBarriersTotal').textContent = barriersTotal;
    document.getElementById('hkExtractionsTotal').textContent = extractionsTotal;
  }

  // --- pagination state --------------------------------------------------
  // 100+ ホストでスクロール地獄になるのを防ぐ。filter / search が変わったら
  // 1 ページ目に戻す (= _hkPage = 0)。page size は select から取得。
  let _hkPage = 0;

  function _hkPageSize() {
    const sel = document.getElementById('hkPageSize');
    return sel ? (parseInt(sel.value, 10) || 50) : 50;
  }

  function renderTable() {
    const tbody = document.querySelector('#hkTable tbody');
    const q = (document.getElementById('hkSearch').value || '').toLowerCase();
    const tierFilter = document.getElementById('hkTierFilter').value || '';
    let rows = _hkData.slice();
    if (q) rows = rows.filter(e => e.host.toLowerCase().includes(q));
    if (tierFilter) rows = rows.filter(e => ((e.k.stats || {}).overall_confidence || 'low') === tierFilter);
    rows.sort((a, b) => {
      const ta = TIER_ORDER[(a.k.stats || {}).overall_confidence || 'low'];
      const tb = TIER_ORDER[(b.k.stats || {}).overall_confidence || 'low'];
      if (tb !== ta) return tb - ta;
      return a.host.localeCompare(b.host);
    });
    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">no matches</td></tr>';
      _updateHkPagerControls(0, 0, 0);
      return;
    }
    // --- apply pagination ---
    const pageSize = _hkPageSize();
    const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
    // Clamp the page index if filter has narrowed results below current page.
    if (_hkPage >= totalPages) _hkPage = totalPages - 1;
    if (_hkPage < 0) _hkPage = 0;
    const start = _hkPage * pageSize;
    const end   = Math.min(start + pageSize, rows.length);
    const visible = rows.slice(start, end);
    _updateHkPagerControls(start + 1, end, rows.length);
    tbody.innerHTML = visible.map(e => {  // eslint-disable-line no-shadow
      const k = e.k;
      const stats = k.stats || {};
      const tier = stats.overall_confidence || 'low';
      const barriers = ((k.per_page || {}).barriers || {});
      const presentBarriers = Object.entries(barriers).filter(([,v]) => v && v.present);
      const extractions = ((k.per_page || {}).content_extraction || []);
      const prov = (k.provenance || {});
      // Distiller-r1 badge: highlight hosts whose KNOWLEDGE was last
      // written by the R1 brain (vs the light distiller / migration
      // script / operator UI). Plus "suggested_tool" chip when any
      // barrier carries a pre-flight plugin recommendation -- that's
      // the actionable bit R1 produced.
      // provenance.last_updated_by は内部識別子のまま 'distiller-r1' を維持
      // (DB と過去ジョブの履歴を壊さないため)。UI 表記だけ「推論 AI」に統一。
      const writtenByR1 = (prov.last_updated_by || '') === 'distiller-r1';
      const _tt = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
      const r1Badge = writtenByR1
        ? `<span class="hk-r1-badge" title="${_tt('knowledge.badge.r1.title', '推論 AI が直近更新')}">${_tt('knowledge.badge.r1', '推論AI')}</span>`
        : '';
      const suggestedTools = presentBarriers
        .map(([kk, v]) => v && v.suggested_tool)
        .filter(t => t);
      const toolChips = suggestedTools.length
        ? suggestedTools.map(t => `<span class="hk-chip hk-tool">⚙ ${t}</span>`).join('')
        : '';
      return `<tr data-host="${e.host}">
        <td><strong>${e.host}</strong> ${r1Badge}</td>
        <td>${tierBadge(tier)}</td>
        <td class="num">${stats.total_jobs || 0}</td>
        <td class="num">${pct(stats.success_rate)}</td>
        <td>${presentBarriers.map(([kk]) => '<span class="hk-chip barrier">' + kk + '</span>').join('') || '<span style="color:#aaa">—</span>'}${toolChips}</td>
        <td>${extractions.length > 0 ? extractions.map(c => '<span class="hk-chip">' + (c.url_pattern || '*') + '</span>').join('') : '<span style="color:#aaa">—</span>'}</td>
        <td>${ago(k.updated_at)}</td>
        <td style="font-size:0.85em; color:#666;">${prov.last_updated_by || '—'}</td>
      </tr>`;
    }).join('');
    tbody.querySelectorAll('tr[data-host]').forEach(tr => {
      tr.addEventListener('click', () => openHkModal(tr.dataset.host));
    });
  }

  // Update the pager controls (prev/next buttons, page indicator, info text).
  function _updateHkPagerControls(fromIdx, toIdx, total) {
    const pageSize = _hkPageSize();
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const info = document.getElementById('hkPagerInfo');
    const cur  = document.getElementById('hkPagerCurrent');
    const prev = document.getElementById('hkPagerPrev');
    const next = document.getElementById('hkPagerNext');
    const first = document.getElementById('hkPagerFirst');
    const last  = document.getElementById('hkPagerLast');
    if (info) {
      info.textContent = total === 0
        ? '0 件'
        : `${fromIdx}-${toIdx} / ${total} 件`;
    }
    if (cur) cur.textContent = `${_hkPage + 1} / ${totalPages}`;
    const atFirst = _hkPage <= 0;
    const atLast  = _hkPage >= totalPages - 1;
    if (prev) prev.disabled = atFirst;
    if (first) first.disabled = atFirst;
    if (next) next.disabled = atLast;
    if (last) last.disabled = atLast;
  }

  function openHkModal(host) {
    const entry = _hkData.find(e => e.host === host);
    if (!entry) return;
    const k = entry.k;
    document.getElementById('hkModalTitle').textContent = host;
    const tier = (k.stats || {}).overall_confidence || 'low';
    document.getElementById('hkModalTier').innerHTML = '';
    document.getElementById('hkModalTier').className = 'hk-badge tier-' + tier;
    document.getElementById('hkModalTier').textContent = tier;
    document.getElementById('hkModalRaw').href = '/hosts/' + encodeURIComponent(host) + '/knowledge';
    document.getElementById('hkModalBody').innerHTML = renderHkBody(k);
    document.getElementById('hkModal').style.display = 'flex';
    try { if (typeof _entityHashSync === 'function') _entityHashSync('ai', 'host/' + host); } catch (_e) {}
  }

  function renderHkBody(k) {
    const per = k.per_page || {};
    const barriers = per.barriers || {};
    const extractions = per.content_extraction || [];
    const navHints = per.navigation_hints || {};
    const stats = k.stats || {};
    const prov = k.provenance || {};

    const fmt = (v) => v == null ? '—' : (typeof v === 'object' ? '<pre style="margin:4px 0; padding:6px 10px; background:#f6f8fa; border-radius:4px; font-size:11.5px; white-space:pre-wrap;">' + JSON.stringify(v, null, 2) + '</pre>' : String(v));

    let out = '';

    // Stats
    out += `<div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-bottom:14px;">
      <div class="hk-stat"><strong>${stats.total_jobs || 0}</strong><span>jobs</span></div>
      <div class="hk-stat"><strong>${stats.successful_jobs || 0}</strong><span>success</span></div>
      <div class="hk-stat"><strong>${pct(stats.success_rate)}</strong><span>rate</span></div>
    </div>`;

    // Barriers
    out += '<h3 style="margin:14px 0 6px;">Barriers</h3>';
    const presentBs = Object.entries(barriers).filter(([,v]) => v && v.present);
    if (presentBs.length === 0) {
      out += '<div style="color:#888;">none detected</div>';
    } else {
      out += '<table style="width:100%;"><thead><tr><th align="left">kind</th><th align="left">strategy</th><th align="left">confidence</th></tr></thead><tbody>';
      for (const [kind, val] of presentBs) {
        out += `<tr>
          <td><span class="hk-chip barrier">${kind}</span></td>
          <td>${fmt(val.strategy)}</td>
          <td>${val.confidence != null ? Math.round(val.confidence * 100) + '%' : '—'}</td>
        </tr>`;
      }
      out += '</tbody></table>';
    }

    // Content extraction
    out += '<h3 style="margin:14px 0 6px;">Content extraction</h3>';
    if (extractions.length === 0) {
      out += '<div style="color:#888;">none learned</div>';
    } else {
      for (const ce of extractions) {
        out += `<div style="border:1px solid #eef; border-radius:6px; padding:8px 12px; margin:6px 0;">
          <div><strong>${ce.url_pattern || '*'}</strong> <span style="color:#888; font-size:0.85em;">→ ${ce.page_kind || 'unknown'}</span></div>
          ${ce.strategy ? '<div style="margin-top:4px;">strategy: ' + fmt(ce.strategy) + '</div>' : ''}
          ${ce.notes ? '<div style="margin-top:4px; color:#666; font-size:0.9em;">notes: ' + ce.notes + '</div>' : ''}
        </div>`;
      }
    }

    // Navigation hints
    if (Object.keys(navHints).some(k => navHints[k] != null && (Array.isArray(navHints[k]) ? navHints[k].length > 0 : true))) {
      out += '<h3 style="margin:14px 0 6px;">Navigation hints</h3>';
      out += '<dl style="display:grid; grid-template-columns:auto 1fr; gap:4px 12px;">';
      for (const [k, v] of Object.entries(navHints)) {
        if (v == null) continue;
        if (Array.isArray(v) && v.length === 0) continue;
        out += `<dt style="font-weight:600; color:#444;">${k}</dt><dd style="margin:0;">${fmt(v)}</dd>`;
      }
      out += '</dl>';
    }

    // Provenance
    out += '<h3 style="margin:14px 0 6px;">Last updated</h3>';
    out += `<div style="color:#666; font-size:0.9em;">
      ${prov.last_updated_by || '—'} at ${prov.last_updated_at || k.updated_at || '—'}
    </div>`;

    return out;
  }

  // Wire up controls. Use a small interval to wait until the tab elements exist
  // (the DOM might not be ready when this script runs depending on tab init order).
  function wire() {
    const ref = document.getElementById('hkRefreshBtn');
    if (!ref) { setTimeout(wire, 200); return; }
    ref.addEventListener('click', loadKnowledge);
    // Filter / search changes always go back to page 1 so the operator
    // sees results from the top rather than a stale offset.
    document.getElementById('hkSearch').addEventListener('input', () => { _hkPage = 0; renderTable(); });
    document.getElementById('hkTierFilter').addEventListener('change', () => { _hkPage = 0; renderTable(); });
    // Pager controls.
    const _pagerSize = document.getElementById('hkPageSize');
    if (_pagerSize) _pagerSize.addEventListener('change', () => { _hkPage = 0; renderTable(); });
    const _pgFirst = document.getElementById('hkPagerFirst');
    if (_pgFirst) _pgFirst.addEventListener('click', () => { _hkPage = 0; renderTable(); });
    const _pgPrev  = document.getElementById('hkPagerPrev');
    if (_pgPrev)  _pgPrev.addEventListener('click',  () => { if (_hkPage > 0) { _hkPage--; renderTable(); } });
    const _pgNext  = document.getElementById('hkPagerNext');
    if (_pgNext)  _pgNext.addEventListener('click',  () => { _hkPage++; renderTable(); });
    const _pgLast  = document.getElementById('hkPagerLast');
    if (_pgLast)  _pgLast.addEventListener('click',  () => {
      const total = _hkData.length;
      const ps = _hkPageSize();
      _hkPage = Math.max(0, Math.ceil(total / ps) - 1);
      renderTable();
    });
    // Delegated handler for the skills/conventions action buttons (works
    // regardless of script scope -- no global onclick needed).
    ['skTable', 'cvTable'].forEach(id => {
      const t = document.getElementById(id);
      if (t) t.addEventListener('click', (ev) => {
        const b = ev.target.closest('button.aibtn');
        if (!b) return;
        aiAction(b.dataset.kind, b.dataset.slug, b.dataset.act);
      });
    });
    // Skill code viewer modal: close / copy / click-outside.
    const _scm = document.getElementById('skillCodeModal');
    const _scClose = document.getElementById('skillCodeClose');
    const _scCopy = document.getElementById('skillCodeCopy');
    const _scHide = () => {
      _scm.style.display = 'none';
      try { if (typeof _entityHashClear === 'function') _entityHashClear('ai'); } catch (_e) {}
    };
    if (_scClose && _scm) _scClose.addEventListener('click', _scHide);
    if (_scm) _scm.addEventListener('click', (e) => { if (e.target === _scm) _scHide(); });
    // Convention detail modal: same close behaviour as the skill modal.
    const _cdm = document.getElementById('convDetailModal');
    const _cdClose = document.getElementById('convDetailClose');
    const _cdHide = () => { if (_cdm) _cdm.style.display = 'none'; };
    if (_cdClose && _cdm) _cdClose.addEventListener('click', _cdHide);
    if (_cdm) _cdm.addEventListener('click', (e) => { if (e.target === _cdm) _cdHide(); });
    // Built-in translator button.
    const _cdTr = document.getElementById('convDetailTranslate');
    if (_cdTr) _cdTr.addEventListener('click', _convTranslateModal);
    if (_scCopy) _scCopy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(_skillCodeCurrent || '');
        const old = _scCopy.innerHTML; _scCopy.textContent = '✓ copied';
        setTimeout(() => { _scCopy.innerHTML = old; }, 1200);
      } catch (e) {}
    });
    // AI tab: load the currently-active sub-tab's data on activation.
    document.querySelectorAll('[data-tab="ai"]').forEach(btn => {
      btn.addEventListener('click', () => {
        // Scope to AI subtabs only — the .ai-subtab class is also used
        // by the Jobs panel's status filter, which can be .active when
        // the AI tab is opened.
        const active = document.querySelector('.ai-subtab[data-ai-subtab].active');
        setAiSubtab(active ? active.dataset.aiSubtab : 'knowledge');
      });
    });
    // AI sub-tabs (Skills / Conventions) -- same design as the Submit tab.
    // Scope to [data-ai-subtab] so this listener is NOT installed on the
    // Jobs panel's status-filter tabs (which share class .ai-subtab but
    // use data-jobs-status instead). Without the scope, clicking a Jobs
    // status tab would fire setAiSubtab(undefined) and visually mark
    // every status tab as .active simultaneously.
    document.querySelectorAll('.ai-subtab[data-ai-subtab]').forEach(btn => {
      btn.addEventListener('click', () => setAiSubtab(btn.dataset.aiSubtab));
    });
    // Pager prev/next (delegated, per feature -- incl. grooming + oracle).
    ['pager-skills', 'pager-conventions', 'pager-groom-retire', 'pager-groom-dedup', 'pager-oracle'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', (ev) => {
        const b = ev.target.closest('button.aipg');
        if (!b || b.disabled) return;
        _aiPagerClick(b.dataset.kind, b.dataset.aipg);
      });
    });
    // Grooming: retire-table delete, dedup-table merge, refresh, toggles.
    // Translate buttons on grooming / oracle: shared helper.
    const _gT = document.getElementById('groomTranslateBtn');
    if (_gT) _gT.addEventListener('click', () => _translateTableCells('#grTable .gr-reason-cell', 'groomTranslateBtn'));
    const _oT = document.getElementById('oracleTranslateBtn');
    if (_oT) _oT.addEventListener('click', () => _translateTableCells('#oracleTable .orc-reason-cell', 'oracleTranslateBtn'));
    const grT = document.getElementById('grTable');
    if (grT) grT.addEventListener('click', (ev) => {
      const sl = ev.target.closest('code.gr-slug');
      if (sl) { _aiOpenItemDetail(sl.dataset.kind, sl.dataset.slug); return; }
      const v = ev.target.closest('button.gr-view');
      if (v) { _aiOpenItemDetail(v.dataset.kind, v.dataset.slug); return; }
      const b = ev.target.closest('button.aibtn');
      if (b && b.dataset.act === 'delete') aiAction(b.dataset.kind, b.dataset.slug, 'delete').then(loadGrooming);
    });
    const gdT = document.getElementById('gdTable');
    if (gdT) gdT.addEventListener('click', (ev) => {
      const sl = ev.target.closest('code.gr-slug');
      if (sl) { _aiOpenItemDetail(sl.dataset.kind, sl.dataset.slug); return; }
      const b = ev.target.closest('button.aibtn');
      if (b && b.dataset.act === 'merge') _groomMerge(b.dataset.kind, b.dataset.keep, b.dataset.drops);
    });
    const grefresh = document.getElementById('groomRefreshBtn');
    if (grefresh) grefresh.addEventListener('click', loadGrooming);
    const tr = document.getElementById('tglAutoRetire');
    if (tr) tr.addEventListener('change', () => _setAutoToggle('auto_retire_enabled', tr.checked));
    const td = document.getElementById('tglAutoDedup');
    if (td) td.addEventListener('change', () => _setAutoToggle('auto_dedup_enabled', td.checked));
    const oref = document.getElementById('oracleRefreshBtn');
    if (oref) oref.addEventListener('click', loadOracle);
    const _hkHide = () => {
      document.getElementById('hkModal').style.display = 'none';
      try { if (typeof _entityHashClear === 'function') _entityHashClear('ai'); } catch (_e) {}
    };
    document.getElementById('hkModalClose').addEventListener('click', _hkHide);
    document.getElementById('hkModal').addEventListener('click', (ev) => {
      if (ev.target.id === 'hkModal') _hkHide();
    });
    // Lazy load when the tab is first shown (the existing tab switcher
    // toggles .active on the panel; observe via clicks).
    document.querySelectorAll('[data-tab="knowledge"]').forEach(btn => {
      btn.addEventListener('click', () => {
        // Load on every click so the data stays fresh; cheap (1 listing + N small JSON reads).
        loadKnowledge();
      });
    });
    // Also do an initial silent load so the tab badge count is correct
    // even before the operator opens the tab.
    loadKnowledge();
    // Live R1 activity ribbon -- redraw whenever _hkData changes (after
    // loadKnowledge) plus a 30s heartbeat poller so the ribbon stays
    // current even when the operator never touches the AI tab.
    renderAiTicker();
    setInterval(() => { loadKnowledge().then(renderAiTicker).catch(() => {}); }, 30_000);
    // GPU gauge poller (lightweight: /health hits no LLM, just gauges).
    // 5 秒間隔 ─ active=1 で約 5-15s かかる 1 推論を確実に取りこぼさない。
    async function updateGpuGauge() {
      try {
        const r = await fetch('/health');
        if (!r.ok) return;
        const j = await r.json();
        const vi = j.vision_inference;
        const el = document.getElementById('aiGpuGaugeText');
        const wrap = document.getElementById('aiGpuGauge');
        const _ttG = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
        if (el && wrap) {
          if (!vi) {
            el.textContent = 'GPU: ' + _ttG('ai.gpu.na', '不明');
          } else {
            const active = vi.active || 0;
            const peak   = vi.peak   || 0;
            const total  = vi.total  || 0;
            const stateTxt = active > 0
              ? `🟢 ${_ttG('ai.gpu.busy', '稼働中')}`
              : `⚪ ${_ttG('ai.gpu.idle', 'アイドル')}`;
            el.textContent = `GPU: ${stateTxt} (${_ttG('ai.gpu.active', '実行中')}=${active}, ${_ttG('ai.gpu.peak', 'ピーク')}=${peak}, ${_ttG('ai.gpu.total', '累計')}=${total})`;
            // Color the chip red if peak >= 3 (queue building up); orange if 2.
            wrap.style.color = peak >= 3 ? '#d24' : (peak >= 2 ? '#d80' : '#888');
          }
        }
        // Reasoning AI chip — show actual backend engine (T)
        const re = j.reasoning_engine;
        const rEl = document.getElementById('aiReasoningChipText');
        if (rEl) {
          if (re && re.distiller_engine) {
            const eng = re.distiller_engine;
            const name = eng.name || eng.slug || '?';
            const model = eng.model ? ` (${eng.model})` : '';
            const dist = re.distiller_mode || 'off';
            const judg = re.judge_mode || 'off';
            rEl.textContent = `${_ttG('ai.reason.label', '推論 AI')}: ${name}${model} · ${_ttG('ai.reason.distill', '蒸留')}=${dist} · ${_ttG('ai.reason.judge', '判定')}=${judg}`;
          } else {
            rEl.textContent = _ttG('ai.reason.na', '推論 AI: 未設定');
          }
        }
      } catch (e) { /* swallow */ }
    }
    updateGpuGauge();
    setInterval(updateGpuGauge, 5_000);
    // Re-render whenever the locale changes / i18next finishes init,
    // because table contents are built dynamically with tt() lookups.
    if (window.i18next) {
      window.i18next.on('languageChanged', () => { renderTable(); renderSummary(); renderAiTicker(); });
      window.i18next.on('initialized',     () => { renderTable(); renderSummary(); renderAiTicker(); });
    }
  }

  // --- live R1 ticker --------------------------------------------------
  // Pulls the 5 most recent provenance.last_updated_at entries written
  // by distiller-r1 across the host fleet, renders them as a horizontal
  // strip of chips. "Fresh" (< 5 min ago) entries get a glow ring so the
  // operator notices what just happened without reading timestamps.
  function renderAiTicker() {
    const rail = document.getElementById('aiTickerRail');
    if (!rail) return;
    const all = (_hkData || []).map(e => ({
      host: e.host,
      by: ((e.k.provenance || {}).last_updated_by || ''),
      at: ((e.k.provenance || {}).last_updated_at || ''),
      barriers: ((e.k.per_page || {}).barriers || {}),
    }));
    const r1 = all
      .filter(x => x.by === 'distiller-r1' && x.at)
      .sort((a, b) => (b.at > a.at ? 1 : -1))
      .slice(0, 5);
    if (!r1.length) {
      const _ttE = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
      rail.innerHTML = `<span class="ai-ticker-empty">${_ttE('ai.ticker.empty', '推論 AI の更新はまだありません')}</span>`;
      return;
    }
    const nowMs = Date.now();
    const _ttU = (k, fb) => (window.i18next && window.i18next.t) ? window.i18next.t(k, { defaultValue: fb }) : fb;
    rail.innerHTML = r1.map(x => {
      let what = _ttU('ai.ticker.what.update', '更新');
      const sug = Object.values(x.barriers).map(v => v && v.suggested_tool).filter(t => t);
      if (sug.length) what = '→ ' + sug[0];
      const ageMs = Math.max(0, nowMs - Date.parse(x.at));
      const fresh = ageMs < 5 * 60 * 1000 ? ' fresh' : '';
      return `<span class="ai-ticker-item${fresh}" title="${x.at}">
        <span class="when">${ago(x.at)}</span>
        <span class="host">${x.host}</span>
        <span class="what">${what}</span>
      </span>`;
    }).join('');
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();

// ---- i18n: merge this session's new Japanese strings into the existing
// JP2EN dictionary (admin-core.js applies it at language=en). Kept OUTSIDE
// the IIFE + at file end so the assignment runs AFTER admin-core.js declares
// JP2EN (script tags are loaded in order with `defer`). Guard with typeof
// so the merge silently no-ops if admin-core hasn't loaded.
try {
  if (typeof JP2EN !== 'undefined' && JP2EN) {
    Object.assign(JP2EN, {
      // --- 役割 (roles) panel labels ---
      "チャット": "Chat",
      "コード生成": "Code generation",
      "自律エージェント": "Autonomous agent",
      "視覚": "Vision",
      "判定": "Judge",
      "推論蒸留": "Reasoning distiller",
      "翻訳": "Translate",
      "第一": "primary",
      "編集": "Edit",
      "完了": "Done",
      "＋ 追加…": "+ Add…",
      "なし（無効）": "none (disabled)",
      "(env 既定)": "(env default)",
      "(既定)": "(default)",
      "▲▼ で優先順、× で除外、＋ で追加。上から試し、過熱/停止のエンジンは飛ばして次へ。空欄は従来の既定（promoted / env / 単一設定）にフォールバック。": "▲▼ to reorder, × to exclude, + to add. Tried top-down; thermal-throttled or stopped engines are skipped. Empty falls back to the legacy default (promoted / env / single setting).",
      "指定なし(auto)でページを読む・質問": "Read / ask a page with no engine specified (auto)",
      "スクリプト自動生成（Submit で上書き可）": "Auto-generate scripts (overridable in Submit)",
      "空なら page.agent は無効": "page.agent is disabled when empty",
      "ページを画像で見る（画像対応エンジンのみ）": "View pages as images (vision-capable engines only)",
      "codegen がゴール達成か採点": "Score whether codegen reached the goal",
      "失敗から壁・ホスト知識を学習": "Learn barriers and host knowledge from failures",
      "#ai 作法モーダルの「翻訳」用。空ならチャット既定にフォールバック": "For the #ai conventions modal Translate button. Falls back to chat default when empty.",
      // --- engines table on #ai / #engines ---
      "状態": "Status",
      "操作": "Actions",
      "本日トークン": "Today's tokens",
      "停止": "Stop",
      "再開": "Resume",
      "停止中": "Stopped",
      "有効": "Enabled",
      "稼働中": "Running",
      "接続中": "Connected",
      "サーマル停止": "Thermal stop",
      "▶ 再開": "▶ Resume",
      "■ 停止": "■ Stop",
      "● 停止中": "● Stopped",
      "● 有効": "● Enabled",
      // --- page-role badges on #jobs ---
      "詳細": "Detail",
      "一覧": "Listing",
      "カテゴリ": "Category",
      "タグ": "Tag",
      "トップ": "Top",
      "エラー": "Error",
      "不明": "Unknown",
      "未分類": "unclassified",
      "種類": "Type",
      // --- settings sub-tabs ---
      "ジョブ既定": "Job defaults",
      "AI 学習動作": "AI learning",
      "Asset / Proxy": "Asset / Proxy",
      "ストレージ / DB": "Storage / DB",
      "システム情報": "System info",
      // --- host edit modal sub-tabs ---
      "クッキー": "Cookies",
      "ページ種別判定": "Page-role rules",
      "その他": "Other",
      "判定キーワード一覧": "Keyword rules",
      "テンプレ化規則（URL の数字 / UUID / slug が変数に置換される順序）": "Templatization rules (priority order for converting digits / UUIDs / slugs into variables)",
      "この変換に含まれる実 URL を見る": "Show real URLs collapsed into this template",
      "新規ホストです。保存後に「ページ種別判定」が利用可能になります。": "New host. Page-role rules will be available after saving.",
      // --- convention detail modal ---
      "なぜ": "Why",
      "適用条件": "Applicable when",
      "悪い例": "Bad example",
      "良い例": "Good example",
      "(advice なし)": "(no advice)",
      "(rationale なし)": "(no rationale)",
      "(bad_example なし)": "(no bad_example)",
      "(good_example なし)": "(no good_example)",
      "由来ジョブ": "Source jobs",
      "原文に戻す": "Show original",
      "翻訳中…": "Translating…",
      // --- grooming / oracle status lines ---
      "reaper 稼働中": "reaper running",
      "ffprobe 稼働中": "ffprobe running",
      "最終": "last",
      "次回": "next",
      "前": "ago",
      "後": "from now",
      "retire 候補": "retire candidates",
      "dedup 候補": "dedup candidates",
      "起動直後 — 最初のスキャン待ち": "just started — waiting for first scan",
      "候補ゼロの理由": "Why zero candidates",
      "前回スキャン対象": "Last scan scope",
      "最終取得": "last fetched",
      "最終スキャン": "last scan",
      "取得": "fetched",
      "件": "items",
      "健全": "healthy",
      "直近に動画アセット無し": "no recent video assets",
      "要調査": "needs investigation",
      "を検出": "detected",
      "dud 判定は cold-start で skip 中 (skill 全体の success=0)": "dud check skipped (cold-start guard: total skill success=0)",
      "skill 全体の success=0 のため dud 判定を skip 中 (zombie 判定は常時有効)": "skill total success=0 → dud check skipped (zombie check is always on)",
      "convention は use rate が per-rule signal でないため dud 判定 無効 (zombie のみ)": "for conventions the use rate is not a per-rule signal → dud check disabled (zombie only)",
      // --- grooming / oracle action buttons ---
      "内容": "Details",
      "コード": "Code",
      "再読込": "Refresh",
      "蒸留結果を表示": "Show distilled result",
      // --- misc ---
      "翻訳に失敗": "translation failed",
      "ハブ側 LLM (chat Promoted エンジン) で advice / rationale / 適用条件 を現在の表示言語へ翻訳。MariaDB に sha256(text)+lang でキャッシュされるので 2 回目以降は瞬時。コードブロック (bad/good example) はそのまま。": "Translate advice / rationale / applicable_when into the current display language via the hub's chat Promoted engine. Cached in MariaDB by sha256(text)+lang so subsequent opens are instant. Code blocks (bad/good example) are left as-is.",
      "reason 列を表示言語に翻訳 (ハブ側 LLM)": "Translate the reason column into the display language (hub-side LLM)",
      "URL 構造とホスト統計から推定したページ種別 (詳細 / 一覧 / トップ / エラー / 不明)": "Page kind estimated from URL structure + per-host template stats (Detail / Listing / Top / Error / Unknown)",
    });
  }
} catch (_e) {}
