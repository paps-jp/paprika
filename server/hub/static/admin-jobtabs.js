// --- Submit form auto-populate from JobInfo ------------------------------
// When ljpAttach fires (operator opened #live/<id> or clicked into a
// job from the Jobs list), the "ジョブの実行" sub-tab still shows a
// blank Submit form by default -- that surprised users who expected
// to see "what was this job submitted with?" on the form itself.
// This function pushes JobInfo.options into the same form fields that
// ``presetApplyToForm`` writes to, so the form mirrors the job's
// state and the operator can review (or tweak + resubmit) without
// re-typing every value. Mirrors presetApplyToForm structure for
// consistency.
function ljpPopulateSubmitForm(info) {
  const opts = info.options || {};
  const mode = opts.mode || 'fetch';

  // URL
  const urlIn = document.getElementById('urlInput');
  if (urlIn) urlIn.value = info.url || '';

  // Top-level mode radio:
  //   info.options.mode -> ui_mode for the form
  //   - fetch                  -> "fetch"
  //   - codegen-loop           -> "ai"   (aiEngine = codegen)
  //   - vision-agent           -> "ai"   (aiEngine = simple)
  //   - rerun                  -> "code" (script lives in info.options.code)
  let uiMode = 'fetch';
  if (mode === 'codegen-loop' || mode === 'vision-agent') uiMode = 'ai';
  else if (mode === 'rerun') uiMode = 'code';
  const modeRadio = document.querySelector(`input[name="mode"][value="${uiMode}"]`);
  if (modeRadio) modeRadio.checked = true;

  if (mode === 'vision-agent') {
    const er = document.querySelector('input[name="aiEngine"][value="simple"]');
    if (er) {
      er.checked = true;
      try { localStorage.setItem('paprika.submit.aiEngine', 'simple'); } catch (_) {}
    }
  } else if (mode === 'codegen-loop') {
    const er = document.querySelector('input[name="aiEngine"][value="codegen"]');
    if (er) {
      er.checked = true;
      try { localStorage.setItem('paprika.submit.aiEngine', 'codegen'); } catch (_) {}
    }
  }

  // Fetch sub-mode (normal / recipe / ai_investigate)
  const fs = opts.fetch_strategy;
  if (fs) {
    const fsRadio = document.querySelector(`input[name="fetchSubMode"][value="${fs}"]`);
    if (fsRadio) fsRadio.checked = true;
  }

  // AI: goal / max_attempts / attempt_timeout / engine
  const g = document.getElementById('goalInput');
  if (g && opts.goal !== undefined && opts.goal !== null) g.value = opts.goal;
  const ma = document.getElementById('maxAttempts');
  if (ma && opts.max_codegen_attempts) ma.value = opts.max_codegen_attempts;
  const at = document.getElementById('attemptTimeout');
  if (at && opts.attempt_timeout_s) at.value = opts.attempt_timeout_s;
  const ce = document.getElementById('codegenEngineSelect');
  if (ce && opts.codegen_engine !== undefined && opts.codegen_engine !== null) {
    ce.value = opts.codegen_engine;
  }

  // Code: paste the script body into the editor (rerun mode)
  const codeEl = document.getElementById('codeInput');
  if (codeEl && opts.code) codeEl.value = opts.code;

  // Fetch options (checkboxes / numbers / texts) -- same fields as
  // presetApplyToForm, plus referer / attach_to_job.
  const setChk = (id, v, dflt) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = (v === undefined || v === null) ? dflt : !!v;
  };
  const setNum = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setTxt = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = String(v);
  };
  setChk('fetchScroll',         opts.scroll,          true);
  setChk('fetchDownloadVideo',  opts.download_video,  false);
  setChk('fetchHeadless',       opts.headless,        false);
  setChk('fetchCaptureAssets',  opts.capture_assets,  true);
  setChk('fetchKeepSession',    opts.keep_session,    false);
  setNum('fetchWaitSec',         opts.wait_seconds);
  setNum('fetchIdleSec',         opts.idle_seconds);
  setNum('fetchMaxWaitSec',      opts.max_wait_seconds);
  setNum('fetchScrollMax',       opts.scroll_max);
  setNum('fetchPostClickSec',    opts.post_click_seconds);
  setNum('fetchMinAssetBytes',   opts.min_asset_size_bytes);
  setTxt('fetchReferer',         opts.referer || '');
  setTxt('fetchAttachToJob',     opts.attach_to_job || '');

  // Refresh derived UI state (visible sections / sub-mode badge / guard
  // for "download_video implies capture_assets" etc.).
  if (typeof syncSubmitMode === 'function') syncSubmitMode();
  if (typeof syncFetchSubMode === 'function') {
    try { syncFetchSubMode(); } catch (_) {}
  }
  if (typeof syncFetchDlGuard === 'function') {
    try { syncFetchDlGuard(); } catch (_) {}
  }
}

// --- "実行" (Run config) tab --------------------------------------------
// Read-only mirror of the Submit form values that produced this job.
// Reuses the .fetch-options / .fetch-section / .fetch-toggles /
// .fetch-grid CSS so the layout matches the live Submit form 1:1 --
// the operator instantly recognises what they (or a preset / cron)
// clicked. Called from ljpRefreshStatus the first time it sees the
// JobInfo (gated by LJP._runConfigRendered).
function ljpRenderRunConfig(info) {
  const host = document.getElementById('ljpRunConfig');
  if (!host) return;
  const opts = info.options || {};
  const mode = opts.mode || 'fetch';

  // ---- shared HTML helpers (escape + read-only widget builders) ----
  const _esc = (s) => {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, ch => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
    }[ch]));
  };
  const _check = (label, val, title) => {
    const checked = val ? 'checked' : '';
    const t = title ? ` title="${_esc(title)}"` : '';
    return `<label${t} style="cursor:default;">
      <input type="checkbox" ${checked} disabled> <span>${_esc(label)}</span>
    </label>`;
  };
  const _row = (label, val, unit, title) => {
    const t = title ? ` title="${_esc(title)}"` : '';
    const v = (val === null || val === undefined || val === '') ? '' : val;
    return `<div class="fetch-row"${t}>
      <label>${_esc(label)}</label>
      <input type="text" value="${_esc(v)}" disabled>
      <span class="fg-unit">${_esc(unit || '')}</span>
    </div>`;
  };
  const _wide = (label, val, placeholder, title) => {
    const t = title ? ` title="${_esc(title)}"` : '';
    const v = (val === null || val === undefined) ? '' : val;
    const ph = placeholder ? ` placeholder="${_esc(placeholder)}"` : '';
    return `<label for="" style="color:#555; font-size:.9em;"${t}>${_esc(label)}</label>
      <input type="text" value="${_esc(v)}"${ph} disabled>`;
  };

  // ---- mode-specific top banner ----
  const _modeBadge = ({
    'fetch':         { ico: 'lucide:file-down',  color: '#196b2c', bg: '#eef8ee', label: 'Fetch' },
    'codegen-loop':  { ico: 'lucide:sparkles',   color: '#5a3b8a', bg: '#f5edff', label: 'AI · LLM (codegen-loop)' },
    'vision-agent':  { ico: 'lucide:eye',        color: '#8a5a00', bg: '#fff8e6', label: 'AI · Simple (vision-agent)' },
    'rerun':         { ico: 'lucide:code-2',     color: '#3a5ca8', bg: '#eef0ff', label: 'Code rerun' },
  })[mode] || { ico: 'lucide:help-circle', color: '#666', bg: '#eee', label: mode };

  const subModeLabel = ({
    'normal':         'そのまま開く (レシピ無視)',
    'recipe':         '解析済レシピを使う',
    'ai_investigate': 'AI で解析する',
  })[opts.fetch_strategy] || (opts.fetch_strategy || '');

  // ---- assemble HTML in document order ----
  const blocks = [];

  // Header card: URL + mode + (fetch strategy / goal)
  let headerExtra = '';
  if (mode === 'fetch' && subModeLabel) {
    headerExtra = `<div style="margin-top:6px; font-size:.9em; color:#555;">
      <strong>実行モード:</strong> ${_esc(subModeLabel)}
    </div>`;
  }
  if ((mode === 'codegen-loop' || mode === 'vision-agent') && opts.goal) {
    headerExtra += `<div style="margin-top:6px;">
      <div style="font-weight:600; color:#555; font-size:.85em;">Goal</div>
      <div style="background:#fff; border:1px solid #e6e8ef; border-radius:5px; padding:6px 10px; margin-top:3px; white-space:pre-wrap; font-size:.9em;">${_esc(opts.goal)}</div>
    </div>`;
  }
  if (mode === 'rerun' && opts.rerun_from) {
    headerExtra += `<div style="margin-top:6px; font-size:.9em; color:#555;">
      <strong>rerun from:</strong> <code>${_esc(opts.rerun_from)}</code>
    </div>`;
  }
  blocks.push(`
    <div class="fetch-section" style="background:#fff;">
      <div class="fs-title">
        <iconify-icon icon="${_modeBadge.ico}" style="color:${_modeBadge.color};"></iconify-icon>
        ${_esc(_modeBadge.label)}
      </div>
      <div class="fetch-grid-wide">
        <label>URL</label>
        <input type="text" value="${_esc(info.url || '')}" disabled>
      </div>
      ${headerExtra}
    </div>
  `);

  // codegen-loop / vision-agent: LLM-side knobs (max attempts / timeout / engine)
  if (mode === 'codegen-loop' || mode === 'vision-agent') {
    blocks.push(`
      <div class="fetch-section">
        <div class="fs-title">
          <iconify-icon icon="lucide:bot"></iconify-icon> AI 設定
        </div>
        <div class="fetch-grid">
          ${_row('最大試行回数', opts.max_codegen_attempts, '回')}
          ${_row('1試行タイムアウト', opts.attempt_timeout_s, '秒')}
        </div>
        <div class="fetch-grid-wide" style="margin-top:8px;">
          <label>コード生成 LLM</label>
          <input type="text" value="${_esc(opts.codegen_engine || '(default — env)')}" disabled>
        </div>
      </div>
    `);
  }

  // 動画 (download_video flag)
  blocks.push(`
    <div class="fetch-section video">
      <div class="fs-title">
        <iconify-icon icon="lucide:video"></iconify-icon> 動画
      </div>
      ${_check('動画をダウンロード', opts.download_video, 'iframe / ネスト iframe の通信トレース + yt-dlp 経路を有効化')}
    </div>
  `);

  // 動作 (scroll / headless / capture / keep_session)
  blocks.push(`
    <div class="fetch-section">
      <div class="fs-title">
        <iconify-icon icon="lucide:settings-2"></iconify-icon> 動作
      </div>
      <div class="fetch-toggles">
        ${_check('スクロール', opts.scroll, 'ページを最後までスクロールして遅延読み込みアセットを拾う')}
        ${_check('ヘッドレス', opts.headless, '画面を出さずに実行 (Chrome --headless)')}
        ${_check('アセットを保存', opts.capture_assets, '拾ったアセットをサーバ側に保存する')}
        ${_check('セッションを継続', opts.keep_session, 'クロール後もセッションを閉じずに残す')}
      </div>
    </div>
  `);

  // タイミング / 制限
  blocks.push(`
    <div class="fetch-section">
      <div class="fs-title">
        <iconify-icon icon="lucide:timer"></iconify-icon> タイミング / 制限
      </div>
      <div class="fetch-grid">
        ${_row('ページ読み込み待ち', opts.wait_seconds, '秒')}
        ${_row('ネットワーク無通信', opts.idle_seconds, '秒')}
        ${_row('最大待ち時間', opts.max_wait_seconds, '秒')}
        ${_row('スクロール上限', opts.scroll_max, 'px')}
        ${_row('クリック後の待ち', opts.post_click_seconds, '秒')}
        ${_row('最小ファイルサイズ', opts.min_asset_size_bytes, 'bytes')}
      </div>
    </div>
  `);

  // ヘッダー / セッション再利用
  blocks.push(`
    <div class="fetch-section">
      <div class="fs-title">
        <iconify-icon icon="lucide:globe"></iconify-icon> ヘッダー / セッション再利用
      </div>
      <div class="fetch-grid-wide">
        ${_wide('リファラー', opts.referer, 'https://...', 'Referer ヘッダ')}
        ${_wide('ジョブに接続', opts.attach_to_job, 'job_id', '既存 job にログイン状態を引き継ぐ')}
        ${_wide('Cookies from', opts.cookies_from, '', 'ホスト名 (HostRegistry から cookie 自動注入)')}
        ${_wide('Use profile', opts.use_profile, '', 'paprika-client upload-profile した Chrome プロファイル名')}
      </div>
    </div>
  `);

  // Worker / lane / created info (footer-ish)
  blocks.push(`
    <div class="fetch-section" style="background:#f7f7fa; border-color:#dee0e7;">
      <div class="fs-title">
        <iconify-icon icon="lucide:info"></iconify-icon> 実行情報
      </div>
      <div class="fetch-grid-wide">
        <label>job_id</label>
        <input type="text" value="${_esc(info.job_id || '')}" disabled style="font-family: ui-monospace, Consolas, monospace;">
        <label>worker / lane</label>
        <input type="text" value="${_esc((info.worker_id || '—') + (info.lane_idx !== null && info.lane_idx !== undefined ? '  #' + info.lane_idx : ''))}" disabled>
        <label>session_id</label>
        <input type="text" value="${_esc(info.session_id || '—')}" disabled style="font-family: ui-monospace, Consolas, monospace;">
        <label>created_at</label>
        <input type="text" value="${_esc(info.created_at || '—')}" disabled>
      </div>
    </div>
  `);

  // Wrap everything in .fetch-options for the gradient background.
  host.innerHTML = `<div class="fetch-options" style="margin:0;">${blocks.join('')}</div>`;
}

// --- Preview + Screenshot tab ---------------------------------------------
//
// Three pieces:
//
//   * live preview -- polls the worker's /preview endpoint at the
//     interval the operator picked. Stops when the tab isn't visible
//     so we don't burn ffmpeg cycles for no benefit.
//   * Screenshot button -- POST /jobs/{id}/screenshot which pulls a
//     fresh frame (higher resolution / quality than the live preview
//     polling) and saves it to the job's /assets dir as a
//     "screenshot-<ts>.jpg" file. The thumbnail strip + the rest of
//     the screenshots pipeline pick it up automatically.
//   * thumbnail strip -- filters /jobs/{id}/assets.json for entries
//     whose name starts with "screenshot-" and renders them as
//     clickable mini tiles.
const LJP_SHOT = {
  timer: null,
  refreshThumbsTimer: null,
  // Cached worker_id + lane_idx for the currently-attached job. Both
  // come from JobInfo and may be null early in the job's life (queued,
  // hub still dispatching). The live image stays empty until both
  // are set.
  workerId: null,
  laneIdx: null,
  // === Saved screenshots viewer state ===
  // ``shots`` is the chronologically-sorted list from
  // /jobs/{id}/screenshots.json (oldest first; latest = last index).
  // It includes EVERY image asset (operator captures, SDK
  // page.screenshot(), page.capture() label PNG, AI attempt
  // final_screenshot.jpg, etc.) regardless of subdirectory depth.
  shots: [],
  // -1 = no shots loaded yet. Otherwise the index of the currently
  // shown image in shots[].
  currentIndex: -1,
  // When true, ljpShotRefreshScreenshots() auto-advances currentIndex
  // to the new latest entry as fresh shots arrive. Flipped off when
  // the operator manually navigates backwards via prev/← / thumbnail
  // click; flipped back on by next/→ reaching the end OR the
  // 「⏭ 最新」button.
  followLatest: true,
};

function ljpShotStopTimer() {
  if (LJP_SHOT.timer) {
    clearInterval(LJP_SHOT.timer);
    LJP_SHOT.timer = null;
  }
  if (LJP_SHOT.refreshThumbsTimer) {
    clearInterval(LJP_SHOT.refreshThumbsTimer);
    LJP_SHOT.refreshThumbsTimer = null;
  }
}

function ljpShotOnTabChange(activeTab) {
  if (activeTab !== 'screenshot') {
    ljpShotStopTimer();
    return;
  }
  // Entering the tab: probe lane info, fire one immediate refresh,
  // then start polling at the selected interval. The saved-shots
  // viewer is refreshed at a slower cadence than the live image.
  ljpShotProbeLane().then(() => {
    ljpShotRefreshLive();
    ljpShotRefreshScreenshots();
    ljpShotResetTimer();
  });
}

async function ljpShotProbeLane() {
  if (!LJP.jobId) {
    LJP_SHOT.workerId = null;
    LJP_SHOT.laneIdx = null;
    return;
  }
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId));
    if (!r.ok) return;
    const j = await r.json();
    LJP_SHOT.workerId = j.worker_id || null;
    LJP_SHOT.laneIdx = (j.lane_idx == null) ? null : Number(j.lane_idx);
  } catch (_) {}
}

function ljpShotResetTimer() {
  ljpShotStopTimer();
  const sec = parseInt((document.getElementById('ljpShotInterval') || {}).value, 10);
  if (sec > 0) {
    LJP_SHOT.timer = setInterval(ljpShotRefreshLive, sec * 1000);
  }
  // Saved-shots viewer refreshes every 5s regardless of live interval
  // -- new captures arrive sparingly, polling more often is wasted.
  LJP_SHOT.refreshThumbsTimer = setInterval(ljpShotRefreshScreenshots, 5000);
}

function ljpShotRefreshLive() {
  const img   = document.getElementById('ljpShotLiveImg');
  const empty = document.getElementById('ljpShotLiveEmpty');
  if (!img || !empty) return;
  // Job is terminal (lane torn down) -> there's no live lane to preview.
  // Don't keep hitting /preview: each request just cancels (no lane),
  // which was the ~2s "(canceled)" preview spam in DevTools. The saved-
  // screenshots sub-view (screenshots.json) still works.
  if (LJP._terminalStopped) {
    empty.style.display = '';
    img.style.display = 'none';
    return;
  }
  if (!LJP_SHOT.workerId || LJP_SHOT.laneIdx == null) {
    // Maybe the job's lane just got assigned -- re-probe.
    ljpShotProbeLane().then(() => {
      if (LJP_SHOT.workerId && LJP_SHOT.laneIdx != null) ljpShotRefreshLive();
    });
    return;
  }
  empty.style.display = 'none';
  img.style.display = '';
  // Higher-res than the dashboard's 320px thumbnail so the operator
  // can actually read text in the preview. ``t`` prevents browser
  // caching between polls. Hits the new /preview endpoint (light,
  // ephemeral); the separate ``Capture`` button posts to
  // /jobs/{id}/screenshot for the save-as-asset use case.
  const t = Date.now();
  img.src =
    `/workers/${encodeURIComponent(LJP_SHOT.workerId)}/lanes/${encodeURIComponent(LJP_SHOT.laneIdx)}/preview`
    + `?width=1280&quality=70&t=${t}`;
}

// Refresh the saved-screenshots list. Backed by /jobs/{id}/screenshots.json
// (recursive over assets/, image extensions only) -- includes operator
// 'Screenshot' captures, page.screenshot() / page.capture(label=...)
// from SDK code, and AI-attempt final_screenshot.jpg files.
async function ljpShotRefreshScreenshots() {
  if (!LJP.jobId) return;
  let shots = [];
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/screenshots.json');
    if (!r.ok) return;
    const d = await r.json();
    shots = (d.items || []).slice();
  } catch (_) { return; }
  // Stick-to-latest semantics: keep the operator's current position
  // unchanged on refresh UNLESS they were already viewing the latest
  // (followLatest=true) -- in which case advance to the new latest.
  const prevLen = LJP_SHOT.shots.length;
  const wasAtLatest = LJP_SHOT.followLatest && (
    LJP_SHOT.currentIndex < 0 || LJP_SHOT.currentIndex >= prevLen - 1
  );
  LJP_SHOT.shots = shots;
  if (shots.length === 0) {
    LJP_SHOT.currentIndex = -1;
  } else if (LJP_SHOT.currentIndex < 0 || wasAtLatest) {
    LJP_SHOT.currentIndex = shots.length - 1;
    LJP_SHOT.followLatest = true;
  } else if (LJP_SHOT.currentIndex >= shots.length) {
    LJP_SHOT.currentIndex = shots.length - 1;
  }
  ljpShotRender();
}

function _ljpShotFmtTs(epoch) {
  if (!epoch) return '';
  try {
    const d = new Date(epoch * 1000);
    // ja-JP-ish compact format: 05/30 15:23:45 (year omitted for space)
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${m}/${day} ${hh}:${mm}:${ss}`;
  } catch (_) { return ''; }
}

function ljpShotRender() {
  const n = LJP_SHOT.shots.length;
  const i = LJP_SHOT.currentIndex;
  const cur = (i >= 0 && i < n) ? LJP_SHOT.shots[i] : null;
  // Nav bar
  const prevBtn = document.getElementById('ljpShotPrev');
  const nextBtn = document.getElementById('ljpShotNext');
  const fnameEl = document.getElementById('ljpShotFilename');
  const tsEl = document.getElementById('ljpShotTimestamp');
  const posEl = document.getElementById('ljpShotPosition');
  const fullA = document.getElementById('ljpShotOpenFull');
  if (prevBtn) prevBtn.disabled = (i <= 0);
  if (nextBtn) nextBtn.disabled = (i < 0 || i >= n - 1);
  if (fnameEl) {
    const label = cur && cur.label ? `${cur.label}/` : '';
    fnameEl.textContent = cur ? `${label}${cur.name}` : '(no screenshot)';
    fnameEl.title = cur ? cur.path || cur.name : '';
  }
  if (tsEl) tsEl.textContent = cur ? _ljpShotFmtTs(cur.mtime) : '';
  if (posEl) posEl.textContent = n > 0 ? `${i + 1} / ${n}` : '0 / 0';
  if (fullA) {
    if (cur) {
      fullA.href = cur.href;
      fullA.style.pointerEvents = '';
      fullA.style.opacity = '';
    } else {
      fullA.href = '#';
      fullA.style.pointerEvents = 'none';
      fullA.style.opacity = '0.45';
    }
  }
  // Main viewer
  const vimg = document.getElementById('ljpShotViewerImg');
  const vempty = document.getElementById('ljpShotViewerEmpty');
  if (vimg && vempty) {
    if (cur) {
      // Cache-bust on filename change so an updated file (same name,
      // new bytes) refreshes. Stable URL when index unchanged avoids
      // re-downloading on every poll tick.
      if (vimg.dataset.curPath !== cur.path) {
        vimg.src = cur.href;
        vimg.alt = cur.name;
        vimg.dataset.curPath = cur.path;
      }
      vimg.style.display = '';
      vempty.style.display = 'none';
    } else {
      vimg.src = '';
      vimg.style.display = 'none';
      vempty.style.display = '';
      delete vimg.dataset.curPath;
    }
  }
  // Thumbnail strip
  const strip = document.getElementById('ljpShotThumbs');
  if (strip) {
    if (n === 0) {
      strip.innerHTML = '';
    } else {
      strip.innerHTML = LJP_SHOT.shots.map((a, idx) => {
        const isActive = (idx === i);
        const border = isActive ? '#4a9eff' : '#333';
        return `
          <button data-shot-idx="${idx}" title="${esc(a.path || a.name)}"
            style="flex:0 0 auto; cursor:pointer; padding:0; border:2px solid ${border}; background:#000; border-radius:4px; overflow:hidden; height:70px; aspect-ratio:16/9;">
            <img src="${a.href}" alt="" loading="lazy" style="display:block; width:100%; height:100%; object-fit:cover;">
          </button>`;
      }).join('');
      strip.querySelectorAll('button[data-shot-idx]').forEach(btn => {
        btn.addEventListener('click', () => {
          const newIdx = parseInt(btn.dataset.shotIdx, 10);
          if (!Number.isFinite(newIdx)) return;
          LJP_SHOT.currentIndex = newIdx;
          // Stick-to-latest auto-engages only when the operator clicks
          // the actual latest thumbnail.
          LJP_SHOT.followLatest = (newIdx === LJP_SHOT.shots.length - 1);
          ljpShotRender();
        });
      });
      // Auto-scroll the active thumbnail into view (only when
      // following latest, so the strip doesn't fight manual nav).
      if (LJP_SHOT.followLatest) {
        const active = strip.querySelector(`button[data-shot-idx="${i}"]`);
        if (active && active.scrollIntoView) {
          try { active.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (_) {}
        }
      }
    }
  }
  // Tab counter (top of pane in the tab strip)
  const tCnt = document.getElementById('ljpShotCount');
  if (tCnt) tCnt.textContent = String(n);
}

function ljpShotPrev() {
  if (LJP_SHOT.currentIndex > 0) {
    LJP_SHOT.currentIndex -= 1;
    LJP_SHOT.followLatest = false;
    ljpShotRender();
  }
}
function ljpShotNext() {
  if (LJP_SHOT.currentIndex >= 0 && LJP_SHOT.currentIndex < LJP_SHOT.shots.length - 1) {
    LJP_SHOT.currentIndex += 1;
    if (LJP_SHOT.currentIndex === LJP_SHOT.shots.length - 1) {
      LJP_SHOT.followLatest = true;
    }
    ljpShotRender();
  }
}
function ljpShotJumpLatest() {
  if (LJP_SHOT.shots.length > 0) {
    LJP_SHOT.currentIndex = LJP_SHOT.shots.length - 1;
    LJP_SHOT.followLatest = true;
    ljpShotRender();
  }
}

async function ljpShotCapture() {
  if (!LJP.jobId) {
    alert('No job is currently attached');
    return;
  }
  const btn = document.getElementById('ljpShotCaptureBtn');
  const status = document.getElementById('ljpShotStatus');
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'capturing…';
  try {
    const r = await fetch(
      '/jobs/' + encodeURIComponent(LJP.jobId) + '/screenshot',
      { method: 'POST' },
    );
    if (!r.ok) {
      const err = await r.text();
      if (status) status.textContent = `❌ HTTP ${r.status}: ${err.slice(0, 80)}`;
      return;
    }
    const j = await r.json();
    if (status) {
      const kb = j.size ? `${Math.round(j.size / 1024)} KB` : '';
      status.textContent = `✓ saved ${j.name || '(unnamed)'} ${kb}`;
    }
    // Refresh both surfaces right away so the new thumbnail appears
    // and the viewer auto-advances to it (followLatest=true after a
    // manual capture is the most useful default).
    LJP_SHOT.followLatest = true;
    ljpShotRefreshScreenshots();
    ljpShotRefreshLive();
  } catch (e) {
    if (status) status.textContent = `❌ ${e}`;
  } finally {
    if (btn) btn.disabled = false;
    // Clear the status after a few seconds.
    setTimeout(() => {
      if (status && status.textContent.startsWith('✓')) status.textContent = '';
    }, 4000);
  }
}

(function wireShotControls() {
  const interval = document.getElementById('ljpShotInterval');
  if (interval) interval.addEventListener('change', ljpShotResetTimer);
  const btn = document.getElementById('ljpShotCaptureBtn');
  if (btn) btn.addEventListener('click', ljpShotCapture);
  // Saved-shots viewer nav. The buttons are inert until shots arrive
  // (ljpShotRender toggles disabled state).
  const prevBtn = document.getElementById('ljpShotPrev');
  if (prevBtn) prevBtn.addEventListener('click', ljpShotPrev);
  const nextBtn = document.getElementById('ljpShotNext');
  if (nextBtn) nextBtn.addEventListener('click', ljpShotNext);
  const latestBtn = document.getElementById('ljpShotJumpLatest');
  if (latestBtn) latestBtn.addEventListener('click', ljpShotJumpLatest);
  // Keyboard nav (← / →) while the Screenshot tab is active.
  document.addEventListener('keydown', (ev) => {
    // Skip when the focus is inside an input/textarea so we don't
    // hijack form editing.
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    // Active sub-tab check: only react when Screenshot pane is visible.
    const shotPane = document.querySelector('.ljp-pane[data-ljp-pane="screenshot"]');
    if (!shotPane || shotPane.style.display === 'none') return;
    if (ev.key === 'ArrowLeft')      { ljpShotPrev(); ev.preventDefault(); }
    else if (ev.key === 'ArrowRight'){ ljpShotNext(); ev.preventDefault(); }
  });
})();

// --- Links tab -----------------------------------------------------------
//
// Polls /jobs/{jobId}/sessions to discover every active session bound to
// the live job, then fetches /sessions/{sid}/links for each one in
// parallel and renders the absolute URL list. The poll only runs while
// the Links tab is the active sub-tab -- entering / leaving the tab
// starts / stops the timer.
//
// Filter input is a substring search across href + anchor text. "copy
// URLs" copies the *visible* (filtered) list to the clipboard, one URL
// per line, so the operator can pipe them into an external crawler.
const LJP_LINKS = {
  timer: null,
  cache: [],       // flat array of {href, text, target, rel, _sid, _curUrl} across sessions
  lastSig: null,   // signature of the cache, to skip re-renders
};

function ljpLinksOnTabChange(activeTab) {
  if (activeTab !== 'links') {
    ljpLinksStopTimer();
    return;
  }
  ljpLinksRefresh();
  // No periodic poll -- links are a final snapshot, refreshed on the
  // [[paprika:links]] marker (was ljpLinksResetTimer()).
}

function ljpLinksStopTimer() {
  if (LJP_LINKS.timer) {
    clearInterval(LJP_LINKS.timer);
    LJP_LINKS.timer = null;
  }
}

function ljpLinksResetTimer() {
  ljpLinksStopTimer();
  const sel = document.getElementById('ljpLinksInterval');
  if (!sel) return;
  const sec = parseInt(sel.value, 10);
  if (sec > 0) {
    LJP_LINKS.timer = setInterval(ljpLinksRefresh, sec * 1000);
  }
}

async function ljpLinksRefresh() {
  if (!LJP.jobId) return;
  const list = document.getElementById('ljpLinksList');
  const status = document.getElementById('ljpLinksStatus');
  if (!list) return;
  if (status) status.textContent = 'fetching…';
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/sessions');
    if (!r.ok) throw new Error('GET /sessions -> ' + r.status);
    const d = await r.json();
    const sessions = d.sessions || [];
    if (sessions.length === 0) {
      // No live session (= job already finished, fetch-mode completed,
      // session reaped, etc.). Fall back to the persisted page.html
      // via /jobs/{id}/links so the operator still sees the link list
      // when they open Live on an old job.
      let reply = null;
      try {
        const rr = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/links');
        reply = rr.ok ? await rr.json() : null;
      } catch (_) { reply = null; }
      if (!reply) {
        LJP_LINKS.cache = [];
        LJP_LINKS.lastSig = '';
        ljpLinksRender();
        if (status) status.textContent = 'no active session and no saved page.html';
        return;
      }
      const flat = (reply.links || []).map(l => ({
        href: l.href || '',
        text: l.text || '',
        target: l.target || '',
        rel: l.rel || '',
        _sid: '(stored)',
        _curUrl: reply.current_url || '',
      }));
      LJP_LINKS.cache = flat;
      ljpLinksRender();
      if (status) status.textContent = `${flat.length} link(s) · from saved page.html · ${new Date().toLocaleTimeString()}`;
      return;
    }
    // Fetch links for every session in parallel.
    const replies = await Promise.all(sessions.map(s =>
      fetch('/sessions/' + encodeURIComponent(s.session_id) + '/links')
        .then(rr => rr.ok ? rr.json() : null)
        .catch(() => null)
    ));
    const flat = [];
    for (let i = 0; i < sessions.length; i++) {
      const s = sessions[i];
      const reply = replies[i];
      if (!reply) continue;
      const links = reply.links || [];
      for (const l of links) {
        flat.push({
          href: l.href || '',
          text: l.text || '',
          target: l.target || '',
          rel: l.rel || '',
          _sid: s.session_id,
          _curUrl: reply.current_url || '',
        });
      }
    }
    LJP_LINKS.cache = flat;
    ljpLinksRender();
    if (status) status.textContent = `${flat.length} link(s) · updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (status) status.textContent = `❌ ${e}`;
  }
}

function ljpLinksRender() {
  const list = document.getElementById('ljpLinksList');
  const cnt = document.getElementById('ljpLinksCount');
  if (!list) return;
  const filterEl = document.getElementById('ljpLinksFilter');
  const filter = (filterEl && filterEl.value || '').trim().toLowerCase();
  const all = LJP_LINKS.cache;
  if (cnt) cnt.textContent = String(all.length);

  if (all.length === 0) {
    list.innerHTML = '<div style="color:#888; font-style:italic;">' +
      (LJP.jobId ? 'まだセッションが開いていないか、現在のページにリンクがありません。' :
                   'ジョブが attach されていません。') +
      '</div>';
    return;
  }

  // Group by session so multi-session jobs (codegen-loop with retries)
  // keep an inline header per session. For single-session jobs the
  // header collapses to a 1-line prefix.
  const bySid = new Map();
  for (const l of all) {
    if (!bySid.has(l._sid)) bySid.set(l._sid, { curUrl: l._curUrl, links: [] });
    if (!filter || l.href.toLowerCase().includes(filter) || (l.text || '').toLowerCase().includes(filter)) {
      bySid.get(l._sid).links.push(l);
    } else {
      // keep cur_url visible even if filter wipes the list
    }
  }

  const html = [];
  let total = 0;
  for (const [sid, grp] of bySid) {
    total += grp.links.length;
    const sidPart = (bySid.size > 1)
      ? `<div style="font-size:.8em; color:#666; margin:8px 0 4px;">session <code>${esc(sid)}</code> @ <code>${esc(grp.curUrl || '(no url)')}</code></div>`
      : `<div style="font-size:.85em; color:#666; margin:0 0 8px;">on <code>${esc(grp.curUrl || '(no url)')}</code></div>`;
    html.push(sidPart);
    if (grp.links.length === 0) {
      html.push('<div style="color:#888; padding:4px 0;">(filter にマッチするリンクがありません)</div>');
      continue;
    }
    html.push('<table style="width:100%; border-collapse:collapse;">');
    for (const l of grp.links) {
      html.push('<tr style="border-bottom:1px solid #ececf0;">' +
        '<td style="padding:4px 8px; vertical-align:top; word-break:break-all;">' +
          `<a href="${esc(l.href)}" target="_blank" rel="noopener" style="color:#1565c0; text-decoration:none;">${esc(l.href)}</a>` +
        '</td>' +
        `<td style="padding:4px 8px; vertical-align:top; color:#555; font-size:.9em; max-width:32ch;">${esc(l.text || '')}</td>` +
        '</tr>');
    }
    html.push('</table>');
  }
  if (filter && total === 0) {
    html.push('<div style="color:#888; font-style:italic; padding:8px 0;">filter にマッチするリンクがありません。</div>');
  }
  list.innerHTML = html.join('');
}

async function ljpLinksCopyVisible() {
  const filterEl = document.getElementById('ljpLinksFilter');
  const filter = (filterEl && filterEl.value || '').trim().toLowerCase();
  const urls = LJP_LINKS.cache
    .filter(l => !filter || l.href.toLowerCase().includes(filter) || (l.text || '').toLowerCase().includes(filter))
    .map(l => l.href);
  const text = urls.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    const status = document.getElementById('ljpLinksStatus');
    if (status) status.textContent = `✓ copied ${urls.length} URL(s) to clipboard`;
  } catch (e) {
    alert('copy failed: ' + e);
  }
}

(function wireLinksControls() {
  const sel = document.getElementById('ljpLinksInterval');
  if (sel) sel.addEventListener('change', ljpLinksResetTimer);
  const btn = document.getElementById('ljpLinksRefreshBtn');
  if (btn) btn.addEventListener('click', ljpLinksRefresh);
  const copyBtn = document.getElementById('ljpLinksCopyBtn');
  if (copyBtn) copyBtn.addEventListener('click', ljpLinksCopyVisible);
  const filt = document.getElementById('ljpLinksFilter');
  if (filt) filt.addEventListener('input', ljpLinksRender);
})();

// --- Network tab -----------------------------------------------------------
//
// Shows every media HTTP response the browser loaded during the job's
// session(s), observed via CDP Network listeners. The operator can
// inspect each item and "add to assets" to cherry-pick resources the
// automatic capture missed or filtered out.
const LJP_NET = {
  timer: null,
  cache: [],        // [{url, mime, size, saved, document_url, timestamp, _sid}]
  savedUrls: new Set(),  // URLs already sent to /assets/from_url
};

function ljpNetOnTabChange(activeTab) {
  if (activeTab !== 'network') {
    ljpNetStopTimer();
    return;
  }
  ljpNetRefresh();
  ljpNetResetTimer();
}

function ljpNetStopTimer() {
  if (LJP_NET.timer) {
    clearInterval(LJP_NET.timer);
    LJP_NET.timer = null;
  }
}

function ljpNetResetTimer() {
  ljpNetStopTimer();
  const sel = document.getElementById('ljpNetInterval');
  if (!sel) return;
  const sec = parseInt(sel.value, 10);
  if (sec > 0) {
    LJP_NET.timer = setInterval(ljpNetRefresh, sec * 1000);
  }
}

// Ingest a streamed network-capture delta ([[paprika:netcap]] marker) into
// the SAME LJP_NET.cache the Network tab renders, then re-render. This is
// the push path that replaces the page.network() pull (which 504s on
// streaming pages): the fetch engine emits newly-captured URLs once per
// poll cycle over the one /events pipe, and we merge them in live (dedup
// by URL). ljpNetRender() no-ops when the tab isn't mounted, so the cache
// simply accumulates until the operator opens the Network tab.
function ljpNetIngest(payload) {
  if (!payload || !Array.isArray(payload.net) || payload.net.length === 0) return;
  const seen = new Set(LJP_NET.cache.map(e => e.url));
  let added = 0;
  for (const e of payload.net) {
    if (!e || !e.url || seen.has(e.url)) continue;
    seen.add(e.url);
    LJP_NET.cache.push({
      url: e.url,
      mime: e.mime || '',
      size: (e.size == null ? null : e.size),
      saved: !!e.saved || LJP_NET.savedUrls.has(e.url),
      document_url: '',
      timestamp: Date.now() / 1000,
      _sid: '(live)',
    });
    added++;
  }
  if (added) {
    const cnt = document.getElementById('ljpNetCount');
    if (cnt) cnt.textContent = String(LJP_NET.cache.length);
    if (typeof ljpNetRender === 'function') ljpNetRender();
  }
}

async function ljpNetRefresh() {
  if (!LJP.jobId) return;
  const status = document.getElementById('ljpNetStatus');
  if (status) status.textContent = 'fetching…';
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/sessions');
    if (!r.ok) throw new Error('GET /sessions -> ' + r.status);
    const d = await r.json();
    const sessions = d.sessions || [];
    if (sessions.length === 0) {
      // No live session (= job finished, sessions reaped). Fall back to
      // the worker-dumped /jobs/{id}/network so the operator can still
      // inspect what the page loaded during the completed run. Empty
      // result is treated the same as a session that loaded nothing.
      let stored = null;
      try {
        const rr = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/network');
        stored = rr.ok ? await rr.json() : null;
      } catch (_) { stored = null; }
      const flat = [];
      const seen = new Set();
      for (const e of (stored && stored.entries) || []) {
        if (!e || seen.has(e.url)) continue;
        seen.add(e.url);
        flat.push({
          url: e.url || '',
          mime: e.mime || '',
          size: e.size,
          saved: e.saved || LJP_NET.savedUrls.has(e.url),
          document_url: e.document_url || '',
          timestamp: e.timestamp || 0,
          _sid: e.session_id || '(stored)',
        });
      }
      // Job finished, sessions reaped. The worker-dumped /network is the
      // authoritative source WHEN it has entries -- but fetch jobs don't POST a
      // per-session network dump, so /network is often empty even though the
      // live [[paprika:netcap]] stream already filled LJP_NET.cache (and
      // re-opening a finished job replays that SAME marker from the persisted
      // log.txt back into the cache via ljpNetIngest). So DON'T overwrite a
      // non-empty live/replayed cache with an empty stored dump -- only replace
      // when the dump actually has entries. (LJP_NET.cache is reset per job in
      // ljpReset(), so a kept cache never leaks across jobs.)
      if (flat.length > 0) LJP_NET.cache = flat;
      const shown = LJP_NET.cache.length;
      const cnt0 = document.getElementById('ljpNetCount');
      if (cnt0) cnt0.textContent = String(shown);
      ljpNetRender();
      if (status) {
        status.textContent = shown
          ? (shown + ' item(s) · ' + (flat.length ? 'from saved dump' : 'from live capture')
             + ' · ' + new Date().toLocaleTimeString())
          : 'no active session and no saved network log';
      }
      return;
    }
    const replies = await Promise.all(sessions.map(s =>
      fetch('/sessions/' + encodeURIComponent(s.session_id) + '/network')
        .then(rr => rr.ok ? rr.json() : null)
        .catch(() => null)
    ));
    const flat = [];
    const seen = new Set();
    for (let i = 0; i < sessions.length; i++) {
      const reply = replies[i];
      if (!reply) continue;
      const entries = reply.entries || [];
      for (const e of entries) {
        if (seen.has(e.url)) continue;
        seen.add(e.url);
        flat.push({
          url: e.url || '',
          mime: e.mime || '',
          size: e.size,
          saved: e.saved || LJP_NET.savedUrls.has(e.url),
          document_url: e.document_url || '',
          timestamp: e.timestamp || 0,
          _sid: sessions[i].session_id,
        });
      }
    }
    LJP_NET.cache = flat;
    const cnt = document.getElementById('ljpNetCount');
    if (cnt) cnt.textContent = String(flat.length);
    ljpNetRender();
    if (status) status.textContent = flat.length + ' item(s) · ' + new Date().toLocaleTimeString();
  } catch (e) {
    if (status) status.textContent = '❌ ' + e;
  }
}

function _ljpNetFormatSize(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

function _ljpNetMimeIcon(mime) {
  if (mime.startsWith('image/')) return '🖼️';
  if (mime.startsWith('video/')) return '🎬';
  if (mime.startsWith('audio/')) return '🔊';
  if (mime.startsWith('font/'))  return '🔤';
  return '📦';
}

function ljpNetRender() {
  const body = document.getElementById('ljpNetBody');
  if (!body) return;
  const filterEl = document.getElementById('ljpNetFilter');
  const filter = (filterEl && filterEl.value || '').trim().toLowerCase();
  const hideSaved = document.getElementById('ljpNetHideSaved');
  const hideS = hideSaved && hideSaved.checked;

  let items = LJP_NET.cache;
  // Mark locally-saved URLs.
  items = items.map(e => ({
    ...e,
    saved: e.saved || LJP_NET.savedUrls.has(e.url),
  }));
  if (hideS) items = items.filter(e => !e.saved);
  if (filter) items = items.filter(e =>
    e.url.toLowerCase().includes(filter) ||
    e.mime.toLowerCase().includes(filter)
  );

  if (items.length === 0) {
    body.innerHTML = '<tr><td colspan="5" style="padding:20px; color:#888; text-align:center; font-style:italic;">' +
      (LJP_NET.cache.length === 0 ? 'まだメディアトラフィックがありません…' : 'フィルタに一致する項目がありません') +
      '</td></tr>';
    return;
  }

  const rows = [];
  for (const e of items) {
    // Truncate URL for display; full URL in title.
    const shortUrl = e.url.length > 90 ? e.url.slice(0, 45) + '…' + e.url.slice(-40) : e.url;
    const savedBadge = e.saved
      ? '<span style="color:#196b2c; font-weight:600;">✓ saved</span>'
      : '<span style="color:#888;">—</span>';
    const addBtn = e.saved
      ? ''
      : '<button class="ljp-net-add pill" data-url="' + e.url.replace(/"/g, '&quot;') + '" '
        + 'data-mime="' + (e.mime || '').replace(/"/g, '&quot;') + '" '
        + 'data-page="' + (e.document_url || '').replace(/"/g, '&quot;') + '" '
        + 'style="font-size:11px; padding:2px 8px; --la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c; white-space:nowrap;">'
        + '<iconify-icon icon="lucide:plus"></iconify-icon> asset</button>';
    rows.push(
      '<tr style="border-bottom:1px solid #eee;">'
      + '<td style="padding:4px 8px; white-space:nowrap;">' + _ljpNetMimeIcon(e.mime) + ' <code style="font-size:11px;">' + (e.mime || '?') + '</code></td>'
      + '<td style="padding:4px 8px; text-align:right; white-space:nowrap; font-family:ui-monospace,monospace; font-size:11px;">' + _ljpNetFormatSize(e.size) + '</td>'
      + '<td style="padding:4px 8px; word-break:break-all;"><a href="' + e.url.replace(/"/g, '&quot;') + '" target="_blank" title="' + e.url.replace(/"/g, '&quot;') + '" style="color:#2266aa; text-decoration:none;">' + shortUrl.replace(/</g, '&lt;') + '</a></td>'
      + '<td style="padding:4px 8px; text-align:center;">' + savedBadge + '</td>'
      + '<td style="padding:4px 8px; text-align:center;">' + addBtn + '</td>'
      + '</tr>'
    );
  }
  body.innerHTML = rows.join('');

  // Bind "add to assets" buttons.
  body.querySelectorAll('.ljp-net-add').forEach(btn => {
    btn.addEventListener('click', async function() {
      const url = this.dataset.url;
      const mime = this.dataset.mime;
      const pageUrl = this.dataset.page;
      this.disabled = true;
      this.textContent = '…';
      try {
        const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/assets/from_url', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({url, mime, page_url: pageUrl}),
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        LJP_NET.savedUrls.add(url);
        this.outerHTML = '<span style="color:#196b2c; font-weight:600;">✓ ' + (data.status || 'saved') + '</span>';
        // Refresh gallery count.
        if (typeof ljpRefreshGallery === 'function') ljpRefreshGallery();
      } catch (e) {
        this.disabled = false;
        this.textContent = '❌ retry';
        console.error('add to assets failed:', e);
      }
    });
  });
}

(function wireNetControls() {
  const sel = document.getElementById('ljpNetInterval');
  if (sel) sel.addEventListener('change', ljpNetResetTimer);
  const btn = document.getElementById('ljpNetRefreshBtn');
  if (btn) btn.addEventListener('click', ljpNetRefresh);
  const filt = document.getElementById('ljpNetFilter');
  if (filt) filt.addEventListener('input', ljpNetRender);
  const hide = document.getElementById('ljpNetHideSaved');
  if (hide) hide.addEventListener('change', ljpNetRender);
})();

// --- Code tab state -------------------------------------------------------
const LJP_CODE = {
  attempts: [],       // most recent /attempts response
  selectedN: null,    // attempt N currently displayed in <pre>
  scriptCache: {},    // {n: "full script.py text"} -- cache so switching is instant
  lastSig: null,      // signature of attempts list, to skip re-renders
};

async function ljpRefreshCode() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/attempts');
    if (!r.ok) return;
    const data = await r.json();
    const attempts = data.attempts || [];
    LJP_CODE.attempts = attempts;
    document.getElementById('ljpCodeCount').textContent = String(attempts.length);
    // Cheap signature so we don't re-render the buttons row when nothing
    // about the attempt count or success-flag layout has changed.
    const sig = attempts.map(a => `${a.n}:${a.success ? 'ok' : a.timed_out ? 'to' : 'err'}`).join('|');
    const sigChanged = sig !== LJP_CODE.lastSig;
    LJP_CODE.lastSig = sig;
    if (sigChanged) {
      const row = document.getElementById('ljpCodeAttempts');
      row.innerHTML = '';
      if (attempts.length === 0) {
        row.innerHTML = '<span style="color:#888; font-size:0.85em;">(no attempts yet)</span>';
      } else {
        for (const a of attempts) {
          const btn = document.createElement('button');
          const ok = !!a.success;
          const to = !!a.timed_out;
          btn.textContent = `${ok ? '✓ ' : to ? '⏱ ' : '✗ '}attempt ${a.n}`;
          btn.title = ok ? 'succeeded' : to ? 'timed out' : 'error';
          btn.className = 'ljp-attempt-btn ' + (ok ? 'ok' : to ? 'timeout' : 'err');
          btn.dataset.ljpAttemptN = a.n;
          btn.addEventListener('click', () => ljpShowAttemptCode(a.n));
          row.appendChild(btn);
        }
      }
      // Auto-select the latest attempt the FIRST time we see attempts,
      // then sticky to the user's pick. If new attempts appear later,
      // jump to the newest so the "live" feel works.
      const latest = attempts.length ? attempts[attempts.length - 1].n : null;
      if (latest !== null && LJP_CODE.selectedN !== latest) {
        await ljpShowAttemptCode(latest);
      }
    } else if (LJP_CODE.selectedN !== null) {
      // Same attempts, but maybe stale cache for selected -- re-fetch
      // in case the orchestrator wrote a new script.py mid-attempt
      // (rare, but cheap).
      await ljpShowAttemptCode(LJP_CODE.selectedN, /*forceRefetch*/ false);
    }
  } catch (_) {}
}

async function ljpShowAttemptCode(n, forceRefetch = true) {
  LJP_CODE.selectedN = n;
  // Highlight the selected attempt button via the .selected class.
  document.querySelectorAll('#ljpCodeAttempts .ljp-attempt-btn').forEach(btn => {
    btn.classList.toggle('selected', String(btn.dataset.ljpAttemptN) === String(n));
  });
  // Enable rerun button now that we have a definite attempt selected.
  const rerunBtn = document.getElementById('ljpCodeRerun');
  if (rerunBtn) rerunBtn.disabled = false;
  // Pull meta from the cached attempts row.
  const row = LJP_CODE.attempts.find(a => a.n === n);
  const meta = document.getElementById('ljpCodeMeta');
  if (row && row.llm) {
    const u = row.llm.usage || {};
    meta.textContent =
        `${row.llm.model || '?'} · prompt ${u.prompt_tokens||'?'} tok · ` +
        `completion ${u.completion_tokens||'?'} tok · ${row.llm.elapsed_ms||0}ms · ` +
        `finish=${row.llm.finish_reason||'?'}`;
  } else {
    meta.textContent = row ? `attempt ${n}: ${row.success ? 'success' : row.timed_out ? 'timed out' : 'error'}` : '';
  }
  // Fetch the script text. Cache to avoid re-downloading on tab clicks.
  const body = document.getElementById('ljpCodeBody');
  if (!forceRefetch && LJP_CODE.scriptCache[n]) {
    body.textContent = LJP_CODE.scriptCache[n];
    return;
  }
  try {
    const r = await fetch(`/jobs/${encodeURIComponent(LJP.jobId)}/attempts/${n}/script.py`);
    if (!r.ok) {
      // 404 is the expected race between attempt-dir creation (which
      // makes /attempts list this attempt) and the script.py write
      // (which happens right after the LLM response). Show a friendly
      // placeholder; the next poll will retry and succeed once the
      // file lands. Anything else is unexpected and worth surfacing.
      if (r.status === 404) {
        body.innerHTML = `<span style="color:#888; font-style:italic;">attempt ${n}: waiting for LLM response… (this auto-refreshes)</span>`;
      } else {
        body.innerHTML = `<span style="color:#888; font-style:italic;">attempt ${n} script.py: HTTP ${r.status}</span>`;
      }
      return;
    }
    const text = await r.text();
    LJP_CODE.scriptCache[n] = text;
    body.textContent = text;
  } catch (e) {
    body.innerHTML = `<span style="color:#c33;">fetch failed: ${esc(String(e))}</span>`;
  }
}

function ljpReset() {
  ljpCloseWs();
  ljpStopTimers();
  if (typeof ljpShotStopTimer === 'function') ljpShotStopTimer();
  if (typeof ljpLinksStopTimer === 'function') ljpLinksStopTimer();
  if (typeof ljpNetStopTimer === 'function') ljpNetStopTimer();
  LJP.jobId = null;
  LJP.seenLines = 0;
  LJP._pendingCallEl = null;
  LJP.finished = false;
  LJP.wsBackoff = 1000;
  LJP.vncIframes.clear();
  LJP.galleryLastCount = -1;
  LJP.gallerySignature = "";
  LJP.galleryStopped = false;
  LJP._galleryDirty = true;   // initial gallery load; then driven by [[paprika:asset]]
  LJP._terminalStopped = false;
  // Reset the saved-screenshots viewer so a fresh attach starts at
  // index 0 (no shots) and follow-latest defaults back to true.
  // Clear the live per-download progress bars.
  try {
    LJP.progress.forEach((r) => { if (r.doneTimer) clearTimeout(r.doneTimer); });
  } catch (_) {}
  LJP.progress.clear();
  const _progEl = document.getElementById('ljpProgress');
  if (_progEl) { _progEl.innerHTML = ''; _progEl.style.display = 'none'; }
  LJP_SHOT.shots = [];
  LJP_SHOT.currentIndex = -1;
  LJP_SHOT.followLatest = true;
  LJP.mode = null;
  // Run-config tab: clear the "rendered once" guard so a fresh
  // attach to a different job rebuilds the mirror with that job's
  // options. The pane content itself is overwritten on the next
  // ljpRefreshStatus, but reset the placeholder eagerly so a stale
  // previous-job snapshot doesn't flash for one tick.
  LJP._runConfigRendered = false;
  // Allow a fresh attach to re-populate the Submit form (gated by the
  // URL-empty check). DO NOT wipe the form values here -- if the
  // operator just closed a job view they may want to tweak + resubmit.
  LJP._submitFormPopulated = false;
  try {
    const _rc = document.getElementById('ljpRunConfig');
    if (_rc) _rc.innerHTML = '<div style="color:#888; padding:20px; text-align:center;">読み込み中…</div>';
  } catch (_) {}
  LJP_CODE.attempts = [];
  LJP_CODE.selectedN = null;
  LJP_CODE.scriptCache = {};
  LJP_CODE.lastSig = null;
  LJP_LINKS.cache = [];
  LJP_LINKS.lastSig = null;
  const linksCnt = document.getElementById('ljpLinksCount');
  if (linksCnt) linksCnt.textContent = '0';
  const linksList = document.getElementById('ljpLinksList');
  if (linksList) linksList.innerHTML = '<div style="color:#888; font-style:italic;">セッションがまだ開始されていません…</div>';
  // Reset network tab state.
  LJP_NET.cache = [];
  LJP_NET.savedUrls = new Set();
  const netCnt = document.getElementById('ljpNetCount');
  if (netCnt) netCnt.textContent = '0';
  const netBody = document.getElementById('ljpNetBody');
  if (netBody) netBody.innerHTML = '';
  document.getElementById('ljpLog').innerHTML = '';
  const grid = document.getElementById('ljpVncGrid');
  grid.innerHTML = '<div class="empty" style="padding:20px; text-align:center; color:#888; border:1px dashed #444; border-radius:6px;">noVNC will appear once a session opens…</div>';
  document.getElementById('ljpGalleryGrid').innerHTML = '';
  document.getElementById('ljpGalleryCount').textContent = '0';
  document.getElementById('ljpAssetCount').style.display = 'none';
  document.getElementById('ljpCodeCount').textContent = '0';
  document.getElementById('ljpCodeAttempts').innerHTML = '';
  document.getElementById('ljpCodeMeta').textContent = '';
  document.getElementById('ljpCodeBody').innerHTML = '<span style="color:#888; font-style:italic;">no LLM-generated code yet (codegen-loop mode only)…</span>';
  const rerunBtn = document.getElementById('ljpCodeRerun');
  if (rerunBtn) rerunBtn.disabled = true;
  // Reset screenshot tab state.
  const liveImg = document.getElementById('ljpShotLiveImg');
  const liveEmpty = document.getElementById('ljpShotLiveEmpty');
  if (liveImg) liveImg.src = '';
  if (liveEmpty) liveEmpty.style.display = '';
  const thumbs = document.getElementById('ljpShotThumbs');
  if (thumbs) thumbs.innerHTML = '';
  const cnt = document.getElementById('ljpShotCount');
  if (cnt) cnt.textContent = '0';
  const tCnt = document.getElementById('ljpShotThumbsCount');
  if (tCnt) tCnt.textContent = '0';
  ljpUpdateVncCount();
  document.getElementById('liveJobPanel').style.display = 'none';
  // Refresh the Submit-panel Live sub-tab placeholder + indicator
  // (LJP.jobId is null now -> show placeholder, grey dot).
  if (typeof _updateLivePlaceholder === 'function') _updateLivePlaceholder();
}

// Reflect the attached job in the URL (#live/<id>) so the address bar
// is shareable and survives reload. Suppresses the resulting hashchange
// so it doesn't bounce back through _applyHashTab.
function ljpSyncHash(jobId) {
  const want = '#live/' + encodeURIComponent(jobId);
  if (location.hash === want) return;
  _suppressNextHashChange = true;
  try { history.replaceState(null, '', want); }
  catch (e) { location.hash = want; }
  setTimeout(() => { _suppressNextHashChange = false; }, 0);
}
// Clear a #live/<id> deep-link back to #submit (used on panel close).
// Leaves plain tab hashes untouched.
function ljpClearHash() {
  if (!/^#live\//.test(location.hash || '')) return;
  _suppressNextHashChange = true;
  try { history.replaceState(null, '', '#submit'); }
  catch (e) { location.hash = '#submit'; }
  setTimeout(() => { _suppressNextHashChange = false; }, 0);
}

function ljpAttach(jobId) {
  // Tear down any previous live attachment first.
  ljpReset();
  LJP.jobId = jobId;
  ljpSyncHash(jobId);
  document.getElementById('ljpJobId').textContent = jobId;
  document.getElementById('ljpOpenLog').href = '/ui/log/' + encodeURIComponent(jobId);
  document.getElementById('ljpOpenGallery').href = '/ui/assets/' + encodeURIComponent(jobId);
  document.getElementById('ljpOpenResult').href = '/jobs/' + encodeURIComponent(jobId) + '/result';
  document.getElementById('ljpOpenPageHtml').href = '/jobs/' + encodeURIComponent(jobId) + '/page.html';
  document.getElementById('liveJobPanel').style.display = '';
  // Job is now attached -> hide placeholder, light up Live sub-tab
  // dot, badge with shortened job id, and auto-switch to Live sub-tab
  // so the operator immediately sees the running job (= what they
  // pressed "submit" / "watch live" for).
  if (typeof _updateLivePlaceholder === 'function') _updateLivePlaceholder();
  if (typeof setSubmitSubtab === 'function') setSubmitSubtab('live');
  ljpSetStatus('queued');
  // Stream logs + poll for sessions and status. Tight intervals at the
  // start because the user just hit submit -- they want feedback fast.
  ljpOpenWs();
  ljpRefreshStatus();
  ljpRefreshSessions();
  ljpRefreshCode();
  LJP.pollTimer = setInterval(ljpRefreshSessions, 3000);
  LJP.statusTimer = setInterval(ljpRefreshStatus, 2500);
  // Polling the attempts/code list at a similar cadence -- new
  // attempts only appear every 10s+ in practice so 4s is fine.
  LJP.codeTimer = setInterval(ljpRefreshCode, 4000);
  // Intentionally NOT scrolling the panel into view: that pushed the
  // Submit form off-screen on shorter viewports and felt like a
  // "transition to the log screen". The panel sits inline below the
  // form; users can scroll if they want it bigger.
}

document.getElementById('ljpClose').addEventListener('click', () => { ljpClearHash(); ljpReset(); });

// "その他" overflow menu: toggle on click, close on outside-click or
// Esc, and auto-close after picking an action so the menu doesn't
// linger over the panel. The actual button handlers stay bound to
// their original IDs (now living inside the menu) untouched.
(function wireLjpMoreMenu() {
  const wrap = document.getElementById('ljpMoreWrap');
  const moreBtn = document.getElementById('ljpMore');
  const menu = document.getElementById('ljpMoreMenu');
  if (!wrap || !moreBtn || !menu) return;
  function close() {
    wrap.classList.remove('open');
    moreBtn.setAttribute('aria-expanded', 'false');
  }
  function toggle(e) {
    e.stopPropagation();
    const willOpen = !wrap.classList.contains('open');
    wrap.classList.toggle('open', willOpen);
    moreBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  }
  moreBtn.addEventListener('click', toggle);
  // Close after any menu item is activated (let its own handler run first).
  menu.addEventListener('click', (e) => {
    if (e.target.closest('.pill')) setTimeout(close, 0);
  });
  document.addEventListener('click', (e) => {
    if (wrap.classList.contains('open') && !wrap.contains(e.target)) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') close();
  });
})();
document.getElementById('ljpStop').addEventListener('click', ljpStopJob);
document.getElementById('ljpResume').addEventListener('click', ljpResumeJob);
document.getElementById('ljpSaveRecipe').addEventListener('click', () => {
  if (!LJP.jobId) return;
  if (typeof window.openRecipeSaveModal === 'function') {
    window.openRecipeSaveModal(LJP.jobId);
  }
});

// "📑 save preset" -- open the save-preset modal with rerun_from
// pre-filled to this Live panel's currently-attached job. Saves the
// operator the trip through Submit form + manual job-id paste when
// "this LLM run finally produced what I wanted, capture it" is the
// goal. Modal handles the rest (name / category / description input).
document.getElementById('ljpSavePreset').addEventListener('click', async () => {
  const jid = LJP.jobId;
  if (!jid) { alert('No job attached'); return; }
  // Suggest a default name from the job's URL host so the operator
  // isn't typing into an empty field. e.g. example-com-daily-...
  let suggested = '';
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(jid));
    if (r.ok) {
      const info = await r.json();
      const u = info.url || '';
      const host = (u.match(/^https?:\/\/(?:www\.)?([^\/]+)/) || ['', ''])[1];
      const hostSlug = host.replace(/[^a-z0-9]+/gi, '-').toLowerCase().replace(/^-+|-+$/g, '');
      if (hostSlug) suggested = `${hostSlug}-${jid.slice(0, 6)}`;
    }
  } catch (_) {}
  const res = await openPresetSaveModal({
    mode: 'save-as',
    initialName: suggested,
    titleOverride: `Save job ${jid} as preset`,
    prefillRerunFromJob: jid,
  });
  if (!res) return;
  const { name, category, description, forceMode, rerunFromJob } = res;
  const payload = presetBuildPayload(name, category, description, { forceMode, rerunFromJob });
  try {
    const r = await fetch(PRESET_ONE_URL(name), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      alert(`Save failed (HTTP ${r.status}): ${await r.text()}`);
      return;
    }
    if (typeof renderPresets === 'function') renderPresets();
    // Quick visual ack via the button's own label.
    const btn = document.getElementById('ljpSavePreset');
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<iconify-icon icon="lucide:check"></iconify-icon> saved';
      setTimeout(() => { btn.innerHTML = orig; }, 1800);
    }
  } catch (e) {
    alert(`Save failed: ${e}`);
  }
});

// --------------------------------------------------------------------------
// Forensics 調査モーダル
// POST /sessions/{sessionId}/forensics を呼び出し、LLM 読み取り専用プローブ
// ループを実行してレポートを表示する。
// --------------------------------------------------------------------------
window.openForensicsModal = async function openForensicsModal(sessionId, hintUrl) {
  const modal   = document.getElementById('forensicsModal');
  const goalEl  = document.getElementById('forensicsGoal');
  const stepsEl = document.getElementById('forensicsMaxSteps');
  const errEl   = document.getElementById('forensicsError');
  const runBtn  = document.getElementById('forensicsRun');
  const spinner = document.getElementById('forensicsSpinner');
  const results = document.getElementById('forensicsResults');
  if (!modal) return;

  // Reset state from any previous run.
  errEl.style.display = 'none';
  errEl.textContent = '';
  spinner.style.display = 'none';
  results.style.display = 'none';
  runBtn.disabled = false;
  goalEl.value = '';
  stepsEl.value = '18';
  // Reset interaction permission checkboxes to OFF (read-only default).
  const cbMedia = document.getElementById('forensicsAllowMedia');
  const cbClick = document.getElementById('forensicsAllowClick');
  if (cbMedia) cbMedia.checked = false;
  if (cbClick) cbClick.checked = false;

  // Stash the session ID on the modal so the run handler can read it.
  modal.dataset.sessionId = sessionId || '';
  modal.dataset.hintUrl   = hintUrl   || '';

  modal.showModal();
  goalEl.focus();
};

// Wire the run button (executes the actual API call).
document.getElementById('forensicsRun').addEventListener('click', async () => {
  const modal   = document.getElementById('forensicsModal');
  const goalEl  = document.getElementById('forensicsGoal');
  const stepsEl = document.getElementById('forensicsMaxSteps');
  const errEl   = document.getElementById('forensicsError');
  const runBtn  = document.getElementById('forensicsRun');
  const spinner = document.getElementById('forensicsSpinner');
  const results = document.getElementById('forensicsResults');
  const reportEl    = document.getElementById('forensicsReport');
  const metaEl      = document.getElementById('forensicsResultMeta');
  const traceEl     = document.getElementById('forensicsTrace');
  const traceCount  = document.getElementById('forensicsTraceCount');

  const sessionId = modal.dataset.sessionId || '';
  const goal      = (goalEl.value || '').trim();
  if (!sessionId) { errEl.textContent = 'セッション ID が見つかりません'; errEl.style.display = ''; return; }
  if (!goal)      { errEl.textContent = '調査ゴールを入力してください'; errEl.style.display = ''; return; }
  errEl.style.display = 'none';

  const maxSteps = parseInt(stepsEl.value, 10) || 18;

  // Collect the operator's per-run interaction permissions.
  const allow = [];
  const cbMedia = document.getElementById('forensicsAllowMedia');
  const cbClick = document.getElementById('forensicsAllowClick');
  if (cbMedia && cbMedia.checked) allow.push('media');
  if (cbClick && cbClick.checked) allow.push('click');

  runBtn.disabled      = true;
  spinner.style.display = '';
  results.style.display = 'none';

  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sessionId) + '/forensics', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        goal,
        max_steps: maxSteps,
        page_url:  modal.dataset.hintUrl || undefined,
        allow,
      }),
    });
    if (!r.ok) {
      const txt = await r.text().catch(() => '');
      throw new Error(`HTTP ${r.status}: ${txt}`);
    }
    const data = await r.json();

    // ---- report ----------------------------------------------------------
    reportEl.textContent = data.report || '(レポートなし)';

    // ---- meta line -------------------------------------------------------
    const secs  = ((data.elapsed_ms || 0) / 1000).toFixed(1);
    const compl = data.completed ? '✅ 完了' : `⚠️ 未完了 (${data.steps_taken}/${data.max_steps} ステップ)`;
    metaEl.textContent = `${compl} · ${data.steps_taken} ステップ · ${secs}s · ${data.model || ''}`;

    // ---- trace -----------------------------------------------------------
    const trace = data.trace || [];
    traceCount.textContent = String(trace.length);
    traceEl.innerHTML = trace.map(s => {
      const ok   = !s.error;
      const bg   = ok ? '#f3fff3' : '#fff3f3';
      const bd   = ok ? '#b0d8b0' : '#d8b0b0';
      const res  = s.result !== undefined && s.result !== null
        ? JSON.stringify(s.result).slice(0, 300)
        : '';
      return `<div style="background:${bg}; border:1px solid ${bd}; border-radius:4px; padding:6px 8px;">
        <div style="font-weight:600; margin-bottom:3px; color:#555;">#${s.n} &nbsp;
          <code style="font-size:.95em; color:#333;">${esc(String(s.expression || '').slice(0, 80))}…</code>
        </div>
        ${s.thought ? `<div style="color:#666; margin-bottom:3px; white-space:pre-wrap;">${esc(s.thought.slice(0, 200))}</div>` : ''}
        ${s.error ? `<div style="color:#b00;">⛔ ${esc(s.error)}</div>` : ''}
        ${res ? `<code style="color:#006; display:block; white-space:pre-wrap;">${esc(res)}</code>` : ''}
        <div style="color:#aaa; font-size:.8em; text-align:right;">${s.elapsed_ms || 0} ms</div>
      </div>`;
    }).join('');

    results.style.display = '';
  } catch (e) {
    errEl.textContent = `エラー: ${e.message}`;
    errEl.style.display = '';
  } finally {
    runBtn.disabled       = false;
    spinner.style.display = 'none';
  }
});

// キャンセルボタン
document.getElementById('forensicsCancel').addEventListener('click', () => {
  document.getElementById('forensicsModal').close();
});

// LJP More menu → Forensics ボタン:
// セッション一覧を取得してからモーダルを開く。
document.getElementById('ljpForensics').addEventListener('click', async () => {
  const jid = LJP.jobId;
  if (!jid) { alert('No job attached'); return; }

  let sessionId = null;
  let hintUrl   = '';
  try {
    const d = await fetch('/jobs/' + encodeURIComponent(jid) + '/sessions').then(r => r.json());
    const ses = (d.sessions || [])[0];
    if (ses) {
      sessionId = ses.session_id;
      hintUrl   = ses.initial_url || ses.url || '';
    }
  } catch (_) {}

  if (!sessionId) {
    // Job may have already finished; sessions are torn down.
    alert('実行中のセッションが見つかりません。Forensics は実行中セッションにのみ使えます。');
    return;
  }

  openForensicsModal(sessionId, hintUrl);
});

// --------------------------------------------------------------------------
