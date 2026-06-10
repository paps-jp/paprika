
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
    if (name === 'knowledge') {
      if (typeof loadKnowledge === 'function') loadKnowledge();
    } else if (name === 'grooming') {
      loadGrooming();
    } else if (name === 'oracle') {
      loadOracle();
    } else {
      loadSkillsConventions();
    }
  }

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

  function _esc(s) { return (s == null ? '' : ('' + s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }

  function _aiRow(x, kind) {
    const pct = x.success_rate == null ? null : Math.round(x.success_rate * 100);
    const bar = pct == null
      ? '<span style="color:#aaa;">— untried</span>'
      : '<div class="airate"><div class="airate-fill" style="width:' + pct + '%; background:' +
        (pct >= 50 ? '#4a9d6a' : (pct >= 20 ? '#d6a13a' : '#d65a5a')) + ';"></div><span>' +
        pct + '% (' + (x.success_count || 0) + '/' + (x.use_count || 0) + ')</span></div>';
    const tierBadge = '<span class="aibadge ' + (x.tier === 'curated' ? 'tier-cur' : 'tier-auto') + '">' + _esc(x.tier || 'auto') + '</span>';
    const slug = _esc(x.slug);
    const desc = _esc((x.description || x.advice || '').slice(0, 100));
    const tierBtn = x.tier === 'curated'
      ? '<button class="aibtn" data-act="demote" data-kind="' + kind + '" data-slug="' + slug + '">demote</button>'
      : '<button class="aibtn" data-act="promote" data-kind="' + kind + '" data-slug="' + slug + '">promote</button>';
    const delBtn = '<button class="aibtn del" data-act="delete" data-kind="' + kind + '" data-slug="' + slug + '">delete</button>';
    // Skills ARE code -- give each one a button to view its code_template.
    const codeBtn = kind === 'skills'
      ? '<button class="aibtn" data-act="code" data-kind="skills" data-slug="' + slug + '" title="このスキルのコード(code_template)を表示"><iconify-icon icon="lucide:code-2"></iconify-icon> コード</button>'
      : '';
    return '<tr title="' + desc + '"><td><code>' + slug + '</code></td><td>' + tierBadge + '</td>' +
      '<td class="num">' + (x.use_count || 0) + '</td><td class="num">' + (x.success_count || 0) + '</td>' +
      '<td>' + bar + '</td><td>' + codeBtn + tierBtn + delBtn + '</td></tr>';
  }

  async function aiAction(kind, slug, act) {
    if (act === 'code') { openSkillCode(slug); return; }
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
  function _groomRetireRow(x) {
    const slug = _esc(x.slug);
    const auto = (x.tier === 'auto');
    const tierBadge = '<span class="aibadge ' + (auto ? 'tier-auto' : 'tier-cur') + '">' + _esc(x.tier) + '</span>';
    const act = auto
      ? '<button class="aibtn del" data-act="delete" data-kind="' + _esc(x.kind) + '" data-slug="' + slug + '">delete</button>'
      : '<span style="color:#999; font-size:.82em;">curated — 手動</span>';
    return '<tr><td>' + _esc(x.kind) + '</td><td><code>' + slug + '</code></td><td>' + tierBadge + '</td>' +
      '<td>' + _esc(x.reason) + '</td><td class="num">' + (x.use_count || 0) + '</td>' +
      '<td class="num">' + (x.success_count || 0) + '</td><td>' + act + '</td></tr>';
  }

  function _groomDedupRow(x) {
    const keep = _esc(x.keep);
    const drops = (x.drops || []).map(_esc).join(', ');
    const act = '<button class="aibtn" data-act="merge" data-kind="' + _esc(x.kind) +
      '" data-keep="' + keep + '" data-drops="' + _esc((x.drops || []).join(',')) + '">merge</button>';
    return '<tr><td>' + _esc(x.kind) + '</td><td><code>' + keep + '</code></td>' +
      '<td><code style="color:#933;">' + drops + '</code></td><td>' + act + '</td></tr>';
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
    return '<tr>' +
      '<td><a href="#live/' + jid + '" style="font-family:monospace;font-size:.83em;" title="' + jid + '">' + short + '…</a></td>' +
      '<td style="font-size:.83em;font-family:monospace;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + name + '">' + name + '</td>' +
      '<td style="text-align:center;">' + validMark + '</td>' +
      '<td class="num">' + dur + '</td>' +
      '<td>' + _esc(x.codec || '—') + '</td>' +
      '<td class="num">' + dims + '</td>' +
      '<td class="num">' + _humanBytes(x.bytes) + '</td>' +
      '<td style="font-size:.82em;color:' + (ok ? '#196b2c' : '#933') + ';">' + _esc(x.reason) + '</td>' +
      '</tr>';
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
    const grT = document.getElementById('grTable');
    if (grT) grT.addEventListener('click', (ev) => {
      const b = ev.target.closest('button.aibtn');
      if (b && b.dataset.act === 'delete') aiAction(b.dataset.kind, b.dataset.slug, 'delete').then(loadGrooming);
    });
    const gdT = document.getElementById('gdTable');
    if (gdT) gdT.addEventListener('click', (ev) => {
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
