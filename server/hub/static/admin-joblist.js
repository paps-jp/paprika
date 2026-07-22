// Operator-event recorder UI (programming-by-demonstration MVP1)
// --------------------------------------------------------------------------
// One-button workflow for trying out the agent extension's operator-event
// logger. The Submit panel hosts the buttons:
//   * 記録開始 -> POST /sessions (with optional worker pin), then ext
//     recordingStart, then surface the noVNC link.
//   * 停止 & 結果表示 -> ext recordingStop, ext getOperatorEvents(drain),
//     dump as pretty JSON into the result block. Session is left alive
//     (idle TTL = 3600 s) so the operator can re-record or inspect manually.
// Errors anywhere in the chain land in #opRecError so the operator sees
// what broke (which worker, what HTTP code, etc.) without needing curl.

const OP_REC = { sid: null, worker_id: null, tStarted: 0, liveTimer: null };

async function _opRecPost(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : null,
  });
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch (_) {}
  if (!r.ok) {
    const detail = (j && (j.detail || j.error)) || t.slice(0, 200);
    throw new Error('HTTP ' + r.status + ': ' + detail);
  }
  return j;
}

function _opRecShowError(msg) {
  const el = document.getElementById('opRecError');
  el.textContent = msg;
  el.style.display = '';
}
function _opRecClearError() {
  const el = document.getElementById('opRecError');
  el.textContent = '';
  el.style.display = 'none';
}

async function _opRecLookupWorkerIdByIp(ip) {
  // Resolve "192.168.50.147" -> worker_id by scanning /workers. Returns
  // null if no alive worker matches.
  if (!ip) return null;
  try {
    const r = await fetch('/workers');
    const d = await r.json();
    for (const w of (d.workers || [])) {
      if (!w.alive) continue;
      const urls = (w.lane_novnc_urls || []).join(' ');
      if (urls.indexOf(ip) >= 0) return w.worker_id;
    }
  } catch (_) {}
  return null;
}

function _opRecTickLive() {
  if (!OP_REC.tStarted) return;
  const secs = Math.floor((Date.now() - OP_REC.tStarted) / 1000);
  const el = document.getElementById('opRecLive');
  if (el) el.textContent = '記録中… ' + secs + 's 経過';
}

document.getElementById('opRecStartBtn').addEventListener('click', async () => {
  _opRecClearError();
  const startUrl = (document.getElementById('opRecStartUrl').value || '').trim();
  const workerIp = (document.getElementById('opRecWorkerIp').value || '').trim();
  if (!startUrl) { _opRecShowError('開始 URL を入力してください'); return; }

  const btn = document.getElementById('opRecStartBtn');
  btn.disabled = true;
  const origLabel = btn.innerHTML;
  btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> セッション作成中…';

  try {
    // 1. Optional worker pin: resolve IP -> worker_id.
    let pinnedWid = null;
    if (workerIp) {
      pinnedWid = await _opRecLookupWorkerIdByIp(workerIp);
      if (!pinnedWid) {
        throw new Error(`worker IP ${workerIp} が現在 alive な inventory に見つかりません`);
      }
    }

    // 2. Create session. Generous TTLs so the operator has plenty of
    // time to interact before idle-reaper grabs it.
    const sessBody = {
      initial_url: startUrl,
      idle_ttl_s: 3600,
      absolute_ttl_s: 7200,
    };
    if (pinnedWid) sessBody.worker_id = pinnedWid;
    const sess = await _opRecPost('/sessions', sessBody);
    OP_REC.sid = sess.session_id;
    OP_REC.worker_id = sess.worker_id;

    // 3. Wait briefly for Chrome to load the page + extension content
    // script. Without this the first ext command races a half-attached
    // worker WS and fails. 5 s covers ~95% of cases on the current
    // fleet; the recordingStart retry below is the belt-and-braces.
    btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> ページ読込待機 (5s)…';
    await new Promise(r => setTimeout(r, 5000));

    // 4. Start recording -- with one retry. The first call sometimes
    // hits an in-flight worker WebSocket reconnect; pause + retry covers
    // that without bothering the operator.
    let started = null;
    let lastErr = null;
    for (let attempt = 1; attempt <= 2; attempt++) {
      btn.innerHTML = `<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> recordingStart (${attempt}/2)…`;
      try {
        started = await _opRecPost(
          '/sessions/' + encodeURIComponent(OP_REC.sid) + '/ext',
          { cmd: 'recordingStart' },
        );
        break;
      } catch (e) {
        lastErr = e;
        if (attempt < 2) await new Promise(r => setTimeout(r, 4000));
      }
    }
    if (!started) {
      throw new Error(
        '記録開始に失敗 (拡張機能 v0.4.0 が未インストールの可能性): ' + (lastErr && lastErr.message)
      );
    }

    // 5. Flip UI to active state.
    document.getElementById('opRecSid').textContent = OP_REC.sid;
    document.getElementById('opRecWorker').textContent = OP_REC.worker_id;
    // Use the hub's canonical noVNC URL (includes the critical
    // ?path=sessions/<sid>/novnc/websockify query that tells the noVNC
    // client which WS path to connect to). My earlier hand-built URL
    // dropped the path= param -- noVNC then tried '/websockify' at the
    // hub root, which 403s, producing 'Something went wrong, connection
    // is closed'.
    const novncHref = sess.novnc_url_autoconnect
      || sess.novnc_url
      || ('/sessions/' + encodeURIComponent(OP_REC.sid)
          + '/novnc/?path=sessions/' + encodeURIComponent(OP_REC.sid)
          + '/novnc/websockify&autoconnect=1&resize=scale&reconnect=1');
    document.getElementById('opRecNovncLink').href = novncHref;
    document.getElementById('opRecIdle').style.display = 'none';
    document.getElementById('opRecActive').style.display = '';
    document.getElementById('opRecResult').style.display = 'none';

    OP_REC.tStarted = Date.now();
    _opRecTickLive();
    if (OP_REC.liveTimer) clearInterval(OP_REC.liveTimer);
    OP_REC.liveTimer = setInterval(_opRecTickLive, 1000);

    // Auto-open noVNC in a new tab so the operator doesn't have to
    // hunt for the link. Popup blockers may swallow this when the
    // click handler ran async after a network round-trip -- the
    // visible link is the fallback.
    try { window.open(novncHref, '_blank'); } catch (_) {}
  } catch (e) {
    _opRecShowError(e.message || String(e));
    OP_REC.sid = null;
    OP_REC.worker_id = null;
  } finally {
    btn.disabled = false;
    btn.innerHTML = origLabel;
  }
});

document.getElementById('opRecStopBtn').addEventListener('click', async () => {
  if (!OP_REC.sid) return;
  const sid = OP_REC.sid;
  const btn = document.getElementById('opRecStopBtn');
  btn.disabled = true;
  const origLabel = btn.innerHTML;
  btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> 停止中…';

  if (OP_REC.liveTimer) {
    clearInterval(OP_REC.liveTimer);
    OP_REC.liveTimer = null;
  }

  try {
    // Stop the recorder. Failure here is non-fatal -- proceed to drain.
    try {
      await _opRecPost(
        '/sessions/' + encodeURIComponent(sid) + '/ext',
        { cmd: 'recordingStop' },
      );
    } catch (_) {}

    // Drain the buffered events.
    const got = await _opRecPost(
      '/sessions/' + encodeURIComponent(sid) + '/ext',
      { cmd: 'getOperatorEvents', args: { drain: true } },
    );
    const ext = (got && got.result) || {};
    let events = ext.events || [];

    document.getElementById('opRecResultMeta').textContent =
      events.length + ' 件キャプチャ · session=' + sid + ' · 言語化中…';

    // Send events to the hub for VLM verbalisation. One-shot per event
    // against the qwen vision-chat engine (Qwen3-VL-32B FP8). Adds a
    // .summary field to each. Best-effort: any per-event error lands in
    // .summary_error and the rendering below shows it.
    try {
      const r = await fetch('/oprec/verbalize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ events }),
      });
      if (r.ok) {
        const vd = await r.json();
        events = vd.events || events;
      }
    } catch (_) {
      // verbalisation is decoration -- if it fails the raw events still render
    }

    // Visual gallery of bbox crops with per-clip natural-language
    // captions from the VLM. The JSON dump below shows the event
    // metadata with the clip dataURL ABBREVIATED so the operator can
    // read the structure without scrolling through 30KB base64.
    const gallery = document.getElementById('opRecClipGrid');
    const clipped = events.filter(e => e && e.clip);
    if (clipped.length > 0) {
      gallery.innerHTML = clipped.map((e) => {
        const idx = events.indexOf(e);
        const lbl = (e.type || '').toString() + ' · ' +
          ((e.target && e.target.text) || (e.target && e.target.tag) || '?').slice(0, 30);
        const summary = e.summary || e.summary_error || '';
        return `<div title="event #${idx + 1}  ${esc(lbl)}"
                     style="display:flex; flex-direction:column; align-items:flex-start; gap:2px; max-width:200px;">
          <img src="${esc(e.clip)}" alt="${esc(lbl)}"
               style="width:200px; max-height:140px; object-fit:contain; border:1px solid #ccc; border-radius:4px; cursor:zoom-in; background:#fafafa;"
               onclick="window.open(this.src, '_blank')">
          <small style="color:#666; font-size:.72em; max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">#${idx + 1} ${esc(lbl)}</small>
          ${summary
            ? `<small style="color:${e.summary_error ? '#a00' : '#196b2c'}; font-size:.78em; max-width:200px; line-height:1.3; padding:2px 0;">${esc(summary).slice(0, 220)}</small>`
            : ''}
        </div>`;
      }).join('');
      document.getElementById('opRecClipCount').textContent = String(clipped.length);
      document.getElementById('opRecClipGallery').style.display = '';
    } else {
      document.getElementById('opRecClipGallery').style.display = 'none';
      gallery.innerHTML = '';
    }

    const withSummary = events.filter(e => e && e.summary).length;
    document.getElementById('opRecResultMeta').textContent =
      events.length + ' 件キャプチャ · クロップ ' + clipped.length
      + ' 件 · 言語化 ' + withSummary + ' 件 · session=' + sid;

    // Abbreviate the data: URL of each clip so the JSON dump stays
    // skim-able. The full image is already shown in the gallery above.
    const eventsForJson = events.map(e => {
      if (!e || !e.clip) return e;
      const c = String(e.clip);
      return { ...e, clip: c.slice(0, 60) + '… (' + c.length + ' chars, see gallery)' };
    });
    document.getElementById('opRecEventsJson').textContent =
      JSON.stringify(eventsForJson, null, 2);
    document.getElementById('opRecResult').style.display = '';
    document.getElementById('opRecActive').style.display = 'none';
    document.getElementById('opRecIdle').style.display = '';

    // Make the result savable: stash events + start_url so the
    // 「デモとして保存」 button can POST without re-draining the
    // (now-closed) session.
    if (events && events.length) {
      _opRecMakeSavable(events, document.getElementById('opRecStartUrl').value || '');
    } else {
      _opRecClearSavable();
    }

    OP_REC.sid = null;
    OP_REC.worker_id = null;
    OP_REC.tStarted = 0;
  } catch (e) {
    // Drain failure (504 from a flapped worker, dead session, ...).
    // Make sure the UI doesn't get stuck in the active state -- reset
    // to idle so the operator can immediately re-record. Surface the
    // error in BOTH the live block and the idle-side error box because
    // we toggle visibility below.
    const msg = (e && e.message) || String(e);
    _opRecShowError(
      '停止 / 結果取得に失敗しました: ' + msg
      + '\n(セッションが reap された / worker が一時切断、等のフリート不安定が原因のことが多いです。'
      + '上の "noVNC を開く" タブを閉じて、もう一度「記録開始」を試してみてください。)'
    );
    // Force the result block to show with whatever happened so the
    // operator sees the failure inline -- not just a hidden alert.
    document.getElementById('opRecResultMeta').textContent =
      '⚠️ 失敗: ' + msg + ' (session=' + sid + ')';
    document.getElementById('opRecEventsJson').textContent =
      '// No events recovered. The session likely terminated before drain.\n'
      + '// Common causes:\n'
      + '//   - worker WebSocket flap (hub log: "worker disconnected: ...")\n'
      + '//   - session idle-reap if recording took longer than idle_ttl_s\n'
      + '//   - heavy video page exhausted the per-session action lock\n';
    document.getElementById('opRecClipGallery').style.display = 'none';
    document.getElementById('opRecResult').style.display = '';
    document.getElementById('opRecActive').style.display = 'none';
    document.getElementById('opRecIdle').style.display = '';
    _opRecClearSavable();
    OP_REC.sid = null;
    OP_REC.worker_id = null;
    OP_REC.tStarted = 0;
  } finally {
    btn.disabled = false;
    btn.innerHTML = origLabel;
  }
});

// --------------------------------------------------------------------------
// Operator-recorder M2: save / list / view / delete demos.
// --------------------------------------------------------------------------
// State for the "current result is savable" flow: when stop succeeds and
// produces events, we stash them here so the 「デモとして保存」 button
// has something to POST without re-draining the (now-closed) session.
const OP_REC_LASTRESULT = { events: null, start_url: '' };

function _opRecMakeSavable(events, startUrl) {
  OP_REC_LASTRESULT.events = events;
  OP_REC_LASTRESULT.start_url = startUrl || '';
  const btn = document.getElementById('opRecSaveDemoBtn');
  if (btn) btn.style.display = (events && events.length) ? '' : 'none';
}
function _opRecClearSavable() {
  OP_REC_LASTRESULT.events = null;
  OP_REC_LASTRESULT.start_url = '';
  const btn = document.getElementById('opRecSaveDemoBtn');
  if (btn) btn.style.display = 'none';
}

// Open the save modal pre-filled with a sensible default title.
document.getElementById('opRecSaveDemoBtn').addEventListener('click', () => {
  if (!OP_REC_LASTRESULT.events) return;
  const modal = document.getElementById('opRecSaveModal');
  const tEl = document.getElementById('opRecSaveTitle');
  const nEl = document.getElementById('opRecSaveNote');
  const eEl = document.getElementById('opRecSaveError');
  let host = '';
  try { host = new URL(OP_REC_LASTRESULT.start_url).hostname || ''; } catch (_) {}
  const stamp = new Date().toISOString().slice(0, 16).replace('T', ' ');
  tEl.value = host ? `${host} demo · ${stamp}` : `demo · ${stamp}`;
  nEl.value = '';
  eEl.textContent = '';
  eEl.style.display = 'none';
  modal.showModal();
  tEl.focus();
});
document.getElementById('opRecSaveCancel').addEventListener('click', () => {
  document.getElementById('opRecSaveModal').close();
});
document.getElementById('opRecSaveSubmit').addEventListener('click', async () => {
  const modal = document.getElementById('opRecSaveModal');
  const eEl = document.getElementById('opRecSaveError');
  const btn = document.getElementById('opRecSaveSubmit');
  eEl.style.display = 'none';
  if (!OP_REC_LASTRESULT.events || !OP_REC_LASTRESULT.events.length) {
    eEl.textContent = '保存できるイベントがありません';
    eEl.style.display = '';
    return;
  }
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = '保存中…';
  try {
    const r = await fetch('/oprec/demos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        events: OP_REC_LASTRESULT.events,
        start_url: OP_REC_LASTRESULT.start_url,
        title: document.getElementById('opRecSaveTitle').value.trim(),
        note: document.getElementById('opRecSaveNote').value.trim(),
      }),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error('HTTP ' + r.status + ': ' + txt.slice(0, 200));
    }
    modal.close();
    _opRecClearSavable();
    // Reload the list so the new entry shows up at the top.
    _opRecRefreshList();
  } catch (e) {
    eEl.textContent = e.message || String(e);
    eEl.style.display = '';
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
});

// ---- list / view / delete -------------------------------------------------
async function _opRecRefreshList() {
  const host = (document.getElementById('opRecListHost').value || '').trim();
  const url = '/oprec/demos?limit=50' + (host ? '&host=' + encodeURIComponent(host) : '');
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const demos = d.demos || [];
    document.getElementById('opRecSavedCount').textContent = String(demos.length);
    const container = document.getElementById('opRecSavedList');
    if (!demos.length) {
      container.innerHTML = '<div style="color:#888; font-size:.85em; padding:6px;">保存済みデモはありません</div>';
      return;
    }
    container.innerHTML = demos.map(d => {
      const created = new Date(d.created_at).toLocaleString();
      return `<div data-demo-id="${esc(d.id)}"
                   style="background:#fff; border:1px solid #e8e0d0; border-radius:6px; padding:8px 10px;">
        <div style="display:flex; align-items:center; gap:10px;">
          <button type="button" class="oprec-expand-btn pill"
                  style="background:#f5f5fa; border-color:#bbc; color:#333; padding:2px 8px; font-size:.8em;"
                  data-demo-id="${esc(d.id)}">
            <iconify-icon icon="lucide:chevron-right"></iconify-icon>
          </button>
          <strong style="font-size:.9em; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
            ${esc(d.title || d.id)}
          </strong>
          <small style="color:#888; white-space:nowrap;">${esc(d.host || '(no host)')}</small>
          <small style="color:#888; white-space:nowrap;">${d.event_count} ev / ${d.clip_count} clip</small>
          <small style="color:#888; white-space:nowrap;">${esc(created)}</small>
          <button type="button" class="oprec-delete-btn pill"
                  style="background:#fee; border-color:#c88; color:#933; padding:2px 8px; font-size:.8em;"
                  data-demo-id="${esc(d.id)}" title="削除">
            <iconify-icon icon="lucide:trash"></iconify-icon>
          </button>
        </div>
        ${d.note ? `<div style="font-size:.78em; color:#7a5a14; margin-top:4px; white-space:pre-wrap;">${esc(d.note)}</div>` : ''}
        <div class="oprec-demo-body" data-demo-id="${esc(d.id)}" style="display:none; margin-top:8px;"></div>
      </div>`;
    }).join('');
    // Wire the expand / delete buttons.
    container.querySelectorAll('.oprec-expand-btn').forEach(btn => {
      btn.addEventListener('click', () => _opRecToggleExpand(btn.dataset.demoId, btn));
    });
    container.querySelectorAll('.oprec-delete-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('このデモを削除しますか? この操作は取り消せません。')) return;
        const r = await fetch('/oprec/demos/' + encodeURIComponent(btn.dataset.demoId), { method: 'DELETE' });
        if (r.ok) _opRecRefreshList();
        else alert('削除失敗: HTTP ' + r.status);
      });
    });
  } catch (e) {
    document.getElementById('opRecSavedList').innerHTML =
      '<div style="color:#c00; font-size:.85em;">読込失敗: ' + esc(e.message || String(e)) + '</div>';
  }
}

async function _opRecToggleExpand(id, btn) {
  const body = document.querySelector('.oprec-demo-body[data-demo-id="' + CSS.escape(id) + '"]');
  if (!body) return;
  if (body.style.display !== 'none' && body.innerHTML) {
    body.style.display = 'none';
    btn.innerHTML = '<iconify-icon icon="lucide:chevron-right"></iconify-icon>';
    return;
  }
  btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon>';
  try {
    const r = await fetch('/oprec/demos/' + encodeURIComponent(id));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const events = d.events || [];
    const clipped = events.filter(e => e && e.clip);
    body.innerHTML = `
      <div style="font-size:.82em; color:#666; margin-bottom:6px;">
        URL: <a href="${esc(d.start_url)}" target="_blank">${esc(d.start_url)}</a>
      </div>
      ${clipped.length > 0 ? `
        <div style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px;">
          ${clipped.map(e => {
            const lbl = ((e.type || '') + ' · ' + ((e.target && e.target.text) || (e.target && e.target.tag) || '?').slice(0, 30));
            const summary = e.summary || '';
            return `<div style="display:flex; flex-direction:column; max-width:180px;">
              <img src="${esc(e.clip)}" alt="${esc(lbl)}"
                   style="width:180px; max-height:120px; object-fit:contain; border:1px solid #ccc; border-radius:4px; cursor:zoom-in; background:#fafafa;"
                   onclick="window.open(this.src, '_blank')">
              <small style="color:#666; font-size:.7em; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(lbl)}</small>
              ${summary ? `<small style="color:#196b2c; font-size:.75em; line-height:1.3; padding:2px 0;">${esc(summary).slice(0, 200)}</small>` : ''}
            </div>`;
          }).join('')}
        </div>` : ''}
      <details>
        <summary style="cursor:pointer; font-size:.82em; color:#666;">全イベント JSON (${events.length} 件)</summary>
        <pre style="background:#1f2330; color:#e6edf3; padding:8px; border-radius:4px; max-height:300px; overflow:auto; font-size:.75em; margin:6px 0 0;">${esc(JSON.stringify(events.map(e => e && e.clip ? { ...e, clip: (e.clip || '').slice(0, 60) + '… (truncated)' } : e), null, 2))}</pre>
      </details>`;
    body.style.display = '';
    btn.innerHTML = '<iconify-icon icon="lucide:chevron-down"></iconify-icon>';
  } catch (e) {
    body.innerHTML = '<div style="color:#c00; font-size:.85em;">' + esc(e.message || String(e)) + '</div>';
    body.style.display = '';
    btn.innerHTML = '<iconify-icon icon="lucide:chevron-right"></iconify-icon>';
  }
}

// host filter input + refresh button + initial load on page ready.
document.getElementById('opRecListRefresh').addEventListener('click', _opRecRefreshList);
document.getElementById('opRecListHost').addEventListener('change', _opRecRefreshList);
// Initial load fires shortly after admin.js parses, so the saved-demos
// section isn't empty when the operator first opens #submit.
setTimeout(_opRecRefreshList, 300);

// NOTE: "save skill" handler removed alongside the Skills tab (v2 cleanup).
// Codegen-loop scripts are now distilled into HostKnowledge directly by the
// R1 Distiller; there's no operator-curated skill registry anymore.

// "↻ refresh" button -- pulls newly-captured assets from the live
// session into the job's gallery / links. Visible only when at
// least one session is bound to the job (keep_session Fetch jobs
// post-completion, or codegen-loop / rerun jobs mid-attempt). The
// endpoint snapshots the current page HTML, overwrites page.html,
// and uploads any worker-tempdir files that weren't shipped yet.
async function ljpRefreshAssetsAndLinks() {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpRefresh');
  if (!btn) return;
  const originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> 取り込み中…';
  try {
    const r = await fetch(
      '/jobs/' + encodeURIComponent(LJP.jobId) + '/refresh',
      { method: 'POST' },
    );
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = data && data.detail
        ? (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('\n') : data.detail)
        : r.statusText;
      alert('refresh failed (' + r.status + '): ' + detail);
      return;
    }
    const result = (data && data.result) || {};
    const added = (result.added || []).length;
    const html = result.html_uploaded ? 'page.html 更新済' : 'page.html 変更なし';
    const url = result.current_url || '';
    // Inline toast on the button itself so the operator sees a
    // confirmation without a blocking alert(). Restores after 4s.
    btn.innerHTML =
      '<iconify-icon icon="lucide:check"></iconify-icon> ' +
      (added > 0 ? `+${added} アセット (${html})` : `差分なし (${html})`);
    btn.style.background = added > 0 ? '#e6f7e9' : '#f5f5fa';
    btn.style.borderColor = added > 0 ? '#7ab68a' : '#bbc';
    btn.style.color = added > 0 ? '#196b2c' : '#555';
    console.log('[ljp refresh] current_url=' + url + ' added=' + added);
    // Kick the gallery + links pollers immediately so the new files
    // show up without waiting for the next 2.5s status tick.
    if (typeof ljpRefreshGallery === 'function') {
      LJP.galleryLastCount = -1;
      LJP.gallerySignature = "";
      ljpRefreshGallery();
    }
    if (typeof ljpLinksRefresh === 'function') ljpLinksRefresh();
    setTimeout(() => {
      btn.innerHTML = originalHtml;
      btn.style.background = '#eef8ff';
      btn.style.borderColor = '#7ab';
      btn.style.color = '#1a5a8a';
    }, 4000);
  } catch (e) {
    alert('refresh failed: ' + e);
    btn.innerHTML = originalHtml;
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('ljpRefresh').addEventListener('click', ljpRefreshAssetsAndLinks);

// "↓ video" button -- runs yt-dlp on a video URL and uploads the
// resulting .mp4 to the job's gallery. Unlike refresh (which only
// flushes already-captured fragments), this kicks off an actual
// download subprocess that may take seconds to minutes.
//
// Click semantics:
//   * normal click: download from the session's current foreground
//     tab URL (= whatever noVNC is showing right now)
//   * shift-click:  prompt for an explicit URL, pre-filled with the
//                   current foreground URL. Lets the operator
//                   override when the foreground isn't a video site
//                   (or batch-download from a different URL while
//                   noVNC stays on a search results page).
async function ljpDownloadVideo(ev) {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpVideoDl');
  if (!btn) return;
  let overrideUrl = null;
  let overridePageId = null;

  // Resolve the session for this job. We need it for both the
  // multi-tab picker AND the shift-click URL prefill.
  let sessionId = null;
  let pagesList = [];          // each: {page_id, url, title, is_default}
  try {
    const sessJson = await fetch(
      '/jobs/' + encodeURIComponent(LJP.jobId) + '/sessions',
    ).then(r => r.json());
    const ses = (sessJson.sessions || [])[0];
    if (ses && ses.session_id) {
      sessionId = ses.session_id;
      // Pull the tab list so we can offer a picker and so the
      // shift-click default URL reflects the right tab.
      const pagesJson = await fetch(
        '/sessions/' + encodeURIComponent(sessionId) + '/pages',
      ).then(r => r.json());
      pagesList = pagesJson.pages || [];
    }
  } catch (_) { /* fall back to "front" behaviour */ }

  if (ev && ev.shiftKey) {
    // Pre-fill with the foreground tab URL if available.
    const front = pagesList.find(p => p.is_default) || pagesList[0] || {};
    overrideUrl = window.prompt(
      'yt-dlp の対象 URL を指定 (空欄で current URL):',
      front.url || '',
    );
    if (overrideUrl === null) return;            // cancel
    overrideUrl = overrideUrl.trim() || null;
  } else if (pagesList.length > 1) {
    // Multi-tab session: ask which tab to operate on. yt-dlp on the
    // worker uses state.default_page_id by default, which can drift
    // from what the operator sees in noVNC (Chrome focus vs worker
    // state). Explicit selection bypasses that confusion.
    const labels = pagesList.map((p, i) => {
      const mark = p.is_default ? ' ★' : '';
      return `${i}: ${(p.url || '(no url)').slice(0, 60)}${mark}`;
    }).join('\n');
    const defaultIdx = String(
      pagesList.findIndex(p => p.is_default) >= 0
        ? pagesList.findIndex(p => p.is_default)
        : 0,
    );
    const picked = window.prompt(
      'どのタブで yt-dlp を実行しますか? ★ = 現在 default\n\n' +
      labels + '\n\n番号を入力 (0..' + (pagesList.length - 1) + '):',
      defaultIdx,
    );
    if (picked === null) return;                  // cancel
    const idx = parseInt(picked.trim(), 10);
    if (!Number.isFinite(idx) || idx < 0 || idx >= pagesList.length) {
      alert('無効な番号: ' + picked);
      return;
    }
    overridePageId = pagesList[idx].page_id;
  }
  const originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> yt-dlp 実行中…';
  try {
    const body = {};
    if (overrideUrl) body.url = overrideUrl;
    if (overridePageId) body.page_id = overridePageId;
    const r = await fetch(
      '/jobs/' + encodeURIComponent(LJP.jobId) + '/download-video',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      },
    );
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const detail = data && data.detail
        ? (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('\n') : data.detail)
        : r.statusText;
      alert('video download failed (' + r.status + '): ' + detail);
      return;
    }
    const result = (data && data.result) || {};
    const files = result.files || [];
    const ok = !!result.ok;
    if (ok && files.length > 0) {
      btn.innerHTML =
        '<iconify-icon icon="lucide:check"></iconify-icon> +' + files.length + ' ファイル';
      btn.style.background = '#e6f7e9';
      btn.style.borderColor = '#7ab68a';
      btn.style.color = '#196b2c';
    } else {
      // yt-dlp failure: surface the FULL message in an alert too,
      // not just a chip-sized snippet on the button. The most common
      // failure (and the one that drove this UX tweak) is
      // "Unsupported URL" when the foreground tab isn't a video host
      // -- operator needs to read the actual message to know whether
      // to navigate elsewhere via noVNC or escalate.
      const fullMsg = (result.message || '').trim() || '取得 0 件';
      const targetUrl = result.url || overrideUrl || '(current foreground URL)';
      const hint = /unsupported url/i.test(fullMsg)
        ? '\n\nヒント: yt-dlp が対応していない URL です。noVNC で動画ページに移動してから再度クリック、または Shift+クリックで URL を直接指定してください。'
        : '';
      alert(
        'yt-dlp failed:\n' +
        '  URL: ' + targetUrl + '\n' +
        '  ' + fullMsg + hint
      );
      btn.innerHTML =
        '<iconify-icon icon="lucide:alert-triangle"></iconify-icon> 失敗';
      btn.style.background = '#fdf5ee';
      btn.style.borderColor = '#d8a06f';
      btn.style.color = '#7a3a0a';
    }
    console.log('[ljp video] result =', result);
    if (typeof ljpRefreshGallery === 'function') {
      LJP.galleryLastCount = -1;
      LJP.gallerySignature = "";
      ljpRefreshGallery();
    }
    setTimeout(() => {
      btn.innerHTML = originalHtml;
      btn.style.background = '#fdf5ee';
      btn.style.borderColor = '#d8a06f';
      btn.style.color = '#7a3a0a';
    }, 6000);
  } catch (e) {
    alert('video download failed: ' + e);
    btn.innerHTML = originalHtml;
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('ljpVideoDl').addEventListener('click', ljpDownloadVideo);

// Asset detail modal -- close on ×, backdrop click, or Escape.
document.getElementById('ljpAssetModalClose').addEventListener('click', ljpCloseAssetModal);
document.getElementById('ljpAssetModal').addEventListener('click', (ev) => {
  // Only close when the user clicks the dark overlay itself, not the
  // white card. The card stops propagation by virtue of being the
  // event.target only on direct clicks (since it's a child).
  if (ev.target === document.getElementById('ljpAssetModal')) ljpCloseAssetModal();
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && document.getElementById('ljpAssetModal').style.display !== 'none') {
    ljpCloseAssetModal();
  }
});

// "▶ rerun this script" -- submit the currently-selected attempt's
// script.py as a fresh rerun-mode job, then re-attach the live panel
// to it. The user can then watch the new run alongside the source.
document.getElementById('ljpCodeRerun').addEventListener('click', async () => {
  if (!LJP.jobId || LJP_CODE.selectedN === null) return;
  const sourceJobId = LJP.jobId;
  const sourceN = LJP_CODE.selectedN;
  const url = (await fetch('/jobs/' + encodeURIComponent(sourceJobId))
                    .then(r => r.ok ? r.json() : null)
                    .catch(() => null) || {}).url || '';
  const body = {
    url,
    options: {
      mode: 'rerun',
      rerun_from: `${sourceJobId}/attempts/${sourceN}`,
      attempt_timeout_s: 180,
    },
  };
  const btn = document.getElementById('ljpCodeRerun');
  btn.disabled = true;
  btn.textContent = '⏳ submitting…';
  try {
    const r = await fetch('/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      const detail = err && (Array.isArray(err.detail) ? err.detail.map(d => d.msg).join('\n') : err.detail);
      alert('rerun failed (' + r.status + '): ' + (detail || r.statusText));
      return;
    }
    const created = await r.json().catch(() => null);
    if (created && created.job_id) ljpAttach(created.job_id);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ rerun this script';
  }
});

// noVNC zoom: now rendered PER SESSION (one .ljp-vnc-zoom select inside
// each iframe wrapper's address bar). All selectors are synced via
// event delegation -- changing one updates the others + localStorage +
// applies zoom across every mounted session. Also runs on mount via
// MutationObserver so freshly-rendered headers initialise to the saved
// value without each call site having to remember.
(function () {
  function _ljpSavedPageZoom() {
    try {
      const saved = localStorage.getItem('paprika.ljp.pageZoom');
      return saved || '1.0';
    } catch (_) { return '1.0'; }
  }
  function _ljpInitZoomSelectsInside(root) {
    const z = _ljpSavedPageZoom();
    (root || document).querySelectorAll('.ljp-vnc-zoom').forEach(sel => {
      if (sel.dataset.ljpZoomInit === '1') return;
      if ([...sel.options].some(o => o.value === z)) sel.value = z;
      sel.dataset.ljpZoomInit = '1';
    });
  }
  // Event delegation: one listener on the grid handles every
  // per-session zoom selector.
  const grid = document.getElementById('ljpVncGrid');
  if (grid) {
    grid.addEventListener('change', (ev) => {
      const sel = ev.target.closest && ev.target.closest('.ljp-vnc-zoom');
      if (!sel) return;
      try { localStorage.setItem('paprika.ljp.pageZoom', sel.value); } catch (_) {}
      // Sync all sibling selectors to the new value so multi-session
      // panels stay coherent.
      document.querySelectorAll('.ljp-vnc-zoom').forEach(other => {
        if (other !== sel && other.value !== sel.value) other.value = sel.value;
      });
      ljpApplyPageZoomAll();
    });
    // Init any selectors already in the DOM at script-load time, and
    // observe future inserts (ljpMountVncFrame appends new wrappers).
    _ljpInitZoomSelectsInside(grid);
    new MutationObserver((muts) => {
      for (const m of muts) {
        m.addedNodes.forEach(n => {
          if (n.nodeType === 1) _ljpInitZoomSelectsInside(n);
        });
      }
    }).observe(grid, { childList: true, subtree: true });
  }
})();

// Wire Live panel tab buttons + restore previously-selected tab from
// localStorage. The HTML defaults to "log" visible; this just makes
// the tab bar reflect that AND honours the user's last pick.
(function () {
  document.querySelectorAll('.ljp-tab').forEach(btn => {
    btn.addEventListener('click', () => ljpSetTab(btn.dataset.ljpTab));
  });
  let initial = 'log';
  try {
    const saved = localStorage.getItem('paprika.ljp.activeTab');
    if (saved && ['log','vnc','code','gallery'].includes(saved)) initial = saved;
  } catch (_) {}
  ljpSetTab(initial);
})();

// Hub-side persisted defaults (from /settings). Currently only
// min_asset_size_bytes drives the Submit form; the cache is structured
// to grow as more Setting-driven defaults are added. Populated once
// at boot via loadHubSettingsDefaults(), re-applied by
// resetFetchOptionsToDefaults() so Clear ends up with the operator's
// persisted preferences instead of the bare HTML "value=" attributes.
let HUB_SETTINGS_DEFAULTS = null;

async function loadHubSettingsDefaults() {
  try {
    const r = await fetch('/settings');
    if (!r.ok) return;
    const data = await r.json();
    HUB_SETTINGS_DEFAULTS = (data && data.values) || {};
    applyHubSettingsDefaultsToForm();
  } catch (_) { /* network noise; the form falls back to HTML defaults */ }
}

function applyHubSettingsDefaultsToForm() {
  if (!HUB_SETTINGS_DEFAULTS) return;
  const minAssetEl = document.getElementById('fetchMinAssetBytes');
  const v = +HUB_SETTINGS_DEFAULTS.min_asset_size_bytes;
  // Don't clobber a value the operator typed by hand. The field is
  // marked userTouched on input; only re-sync the Settings default
  // into fields the operator hasn't edited. This is what makes the
  // "re-sync on Submit-tab activation" safe -- a stale tab picks up
  // the current Settings min-size, but an in-progress manual edit
  // survives a tab round-trip. (Cause of job dee8fb79c625 running
  // at 10KB while Settings said 1KB: the Submit tab was opened
  // before the Settings change and never re-synced.)
  if (minAssetEl && Number.isFinite(v) && v >= 0
      && minAssetEl.dataset.userTouched !== '1') {
    minAssetEl.value = v;
  }
}

// Reset every "Fetch options" field to its declared default (mirrors
// the HTML value= attributes / unchecked-by-default), then re-apply
// the hub-side Settings defaults so the operator's persisted prefs
// win over the bare UI defaults. Called by the Clear button and (via
// applyHubSettingsDefaultsToForm) at boot.
function resetFetchOptionsToDefaults() {
  const setChk = (id, v) => { const e = document.getElementById(id); if (e) e.checked = v; };
  const setVal = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
  setChk('fetchScroll',         true);
  setChk('fetchDownloadVideo',  true);
  setChk('fetchHeadless',       false);
  setChk('fetchCaptureAssets',  true);
  setChk('fetchKeepSession',    false);
  setVal('fetchWaitSec',        20);
  setVal('fetchIdleSec',        3);
  setVal('fetchMaxWaitSec',     60);
  setVal('fetchScrollMax',      3000);
  setVal('fetchPostClickSec',   5);
  setVal('fetchMinAssetBytes',  0);
  setVal('fetchReferer',        '');
  setVal('fetchAttachToJob',    '');
  // Clear = fresh start: forget any manual edit so the Settings
  // default re-applies cleanly below.
  const _mab = document.getElementById('fetchMinAssetBytes');
  if (_mab) delete _mab.dataset.userTouched;
  applyHubSettingsDefaultsToForm();
}

// Mark the Min-file-size field as user-edited the moment the operator
// types in it, so applyHubSettingsDefaultsToForm() stops overwriting
// it on the next Settings re-sync (boot / Submit-tab activation).
(function wireMinAssetTouched() {
  const el = document.getElementById('fetchMinAssetBytes');
  if (el) el.addEventListener('input', () => { el.dataset.userTouched = '1'; });
})();

// Fire the boot fetch so the Min file size field shows the persisted
// Settings value on initial render rather than the bare 0 placeholder.
loadHubSettingsDefaults();

// Clear button next to Submit: explicitly empty URL / Goal / Code
// inputs, reset every Fetch-options field to its declared default
// (re-applying the Settings-derived min-file-size), AND tear down
// any open Live panel from a previous submit. "clear" means "start
// fresh", which for the operator includes both input fields and the
// inline live view below the form. Decoupled from the submit handler
// so operators can hammer Submit repeatedly with the same payload
// without re-typing.
document.getElementById('submitClear').addEventListener('click', () => {
  const u = document.getElementById('urlInput');
  const g = document.getElementById('goalInput');
  const c = document.getElementById('codeInput');
  if (u) u.value = '';
  if (g) g.value = '';
  if (c) c.value = '';
  resetFetchOptionsToDefaults();
  if (typeof ljpReset === 'function') ljpReset();
  if (u) u.focus();
});

// Parse a human-entered byte size into an integer >= 0.
//   "1024" -> 1024 | "1k"/"1kb" -> 1024 | "1.5mb" -> 1572864 | "" -> NaN
// Returns NaN when blank / unparseable so callers can fall back.
function parseHumanBytes(raw) {
  if (raw == null) return NaN;
  const s = String(raw).trim().toLowerCase().replace(/\s+/g, '');
  if (!s) return NaN;
  const m = s.match(/^(\d+(?:\.\d+)?)(b|k|kb|m|mb|g|gb)?$/);
  if (!m) return NaN;
  const n = parseFloat(m[1]);
  if (!Number.isFinite(n)) return NaN;
  const mult = { b: 1, k: 1024, kb: 1024, m: 1048576, mb: 1048576,
                 g: 1073741824, gb: 1073741824 }[m[2] || 'b'];
  return Math.round(n * mult);
}

// Read the "Fetch options" form block and produce a JobOptions-shaped
// dict for POST /jobs. Keys that match the JobOptions default (or are
// blank in text fields) are OMITTED so the server-side defaults still
// apply -- this keeps payloads small and round-trippable. Used by both
// the Submit handler and presetBuildPayload() so the two never drift.
// Phase 2a: which Fetch sub-mode is the operator on?
// Returns "normal" | "recipe" | "ai_investigate". Default = "recipe".
function currentFetchSubMode() {
  const sel = document.querySelector('input[name="fetchSubMode"]:checked');
  return (sel && sel.value) || 'recipe';
}

// X: 解析の目標プリセット。最頻ユースケースを 1 クリックで textarea に
// 投入できる。文言は planner/coder LLM 向けに最適化済み:
//   - SDK 関数名 (page.download_video, pap.assets.add, page.outline,
//     pap.walk) を明示して LLM が候補から外れにくくする
//   - 副次的な落とし穴 (HLS は video.play() 必要、lazy-load は scroll
//     必要、広告除外) を予め指示
//   - 「YouTube 等は url 明示」convention と整合
const GOAL_PRESETS = {
  video:   "このページのメイン動画を再生して動画 URL を検出し、page.download_video(url=...) で yt-dlp 経由でダウンロードして pap.assets.add(path) で保存する。HLS / DASH の .m3u8 / .mpd を network から拾うために必要なら video.play() を発火する。広告動画やプレビューサムネは対象外。",
  gallery: "このページに表示されている主要な画像 (本文中の写真・イラスト等) を全て pap.assets.add(path) で保存する。lazy-load 画像を取りこぼさないよう必要に応じて scroll を発火させる。アイコン・ロゴ・1px トラッカー等の装飾画像は対象外。",
  page:    "ページ全体の HTML を pap.assets.add(name='page.html', content=...) で保存し、メタデータ (title / meta description / og:image) を抽出して pap.assets.add(name='meta.json', content=JSON) で併せて保存する。",
  links:   "このページから同じドメインのリンクをすべて列挙し、pap.walk(seed_urls=[...], same_host=True) で BFS クロールして各ページの URL とタイトルを pap.assets.add(name='links.json', content=JSON) にまとめる。",
};

// Toggle visibility of the inline goal area when AI調査 is selected.
// Wired to radio onchange below and called once on page load.
function syncFetchSubMode() {
  const sub = currentFetchSubMode();
  const area = document.getElementById('fetchInvestigateArea');
  if (area) area.style.display = (sub === 'ai_investigate') ? 'block' : 'none';
  // When AI調査 is picked we ALSO need a non-blank goal -- nudge the
  // operator with a focus + a hint in the badge area.
  const badge = document.getElementById('fetchSubModeBadge');
  if (badge) {
    if (sub === 'ai_investigate') badge.textContent = '(課金 LLM が走ります)';
    else if (sub === 'normal') badge.textContent = '(recipe を無視)';
    else badge.textContent = '';
  }
  // AI 調査 selected => download_video が強制 True (admin.js の
  // buildFetchOptionsFromForm 側で payload を上書きする) なので、
  // UI 側もそれに合わせて 動画DL / アセット保存 のチェックを連動
  // させて見せておく。capture_assets のロックは syncFetchDlGuard で。
  if (sub === 'ai_investigate') {
    const dv = document.getElementById('fetchDownloadVideo');
    if (dv && !dv.checked) {
      dv.checked = true;
    }
  }
  syncFetchDlGuard();
}

// Mutual-constraint guard: 動画ダウンロード ON -> アセットを保存 を
// 強制 ON + disable。download_video=True で capture_assets=False の
// 矛盾組合せ (= 何も保存されない無意味な fetch) を物理的に不能化。
function syncFetchDlGuard() {
  const dv = document.getElementById('fetchDownloadVideo');
  const ca = document.getElementById('fetchCaptureAssets');
  if (!dv || !ca) return;
  if (dv.checked) {
    ca.checked = true;
    ca.disabled = true;
    // 親 <label> も視覚的にグレーアウト + ヒント
    const lbl = ca.closest('label');
    if (lbl) {
      lbl.style.opacity = '0.55';
      lbl.title = '動画をダウンロード ON 時はアセット保存が必須';
    }
  } else {
    ca.disabled = false;
    const lbl = ca.closest('label');
    if (lbl) {
      lbl.style.opacity = '';
      // 元タイトルに戻す
      lbl.title = '拾ったアセットをサーバ側に保存する。';
    }
  }
}

function buildFetchOptionsFromForm() {
  const $ = (id) => document.getElementById(id);
  const opts = { mode: 'fetch' };
  // Toggles. Defaults baked in here MATCH the UI's historical
  // hardcoding (scroll = true), not the JobOptions
  // defaults -- changing them silently would break existing workflows.
  if ($('fetchScroll'))         opts.scroll          = !!$('fetchScroll').checked;
  if ($('fetchDownloadVideo'))  opts.download_video  = !!$('fetchDownloadVideo').checked;
  if ($('fetchHeadless'))       opts.headless        = !!$('fetchHeadless').checked;
  if ($('fetchCaptureAssets'))  opts.capture_assets  = !!$('fetchCaptureAssets').checked;
  if ($('fetchKeepSession'))    opts.keep_session    = !!$('fetchKeepSession').checked;
  // AI 調査 (fetchSubMode='ai_investigate') を選んだときは、UI のチェック
  // ボックスに関係なく codegen 側に download_video=true を必ず通知する。
  // この sub-mode は LLM がコードを生成するため動画 DL ロジックを含めるかの
  // 判断材料が必要、というのが要件 (operator 仕様)。
  try {
    const subEl = document.querySelector('input[name="fetchSubMode"]:checked');
    if (subEl && subEl.value === 'ai_investigate') {
      opts.download_video = true;
    }
  } catch (_) {}
  // Numeric knobs. Only include when the parsed value is a real
  // number AND differs from the server default (so the server can
  // bump its own defaults later without us pinning every payload).
  const numField = (id, parser, dflt, key) => {
    const el = $(id);
    if (!el) return;
    const v = parser(el.value);
    if (Number.isFinite(v) && v !== dflt) opts[key] = v;
  };
  numField('fetchWaitSec',         (s) => parseInt(s, 10), 20,    'wait_seconds');
  numField('fetchIdleSec',         parseFloat,             3.0,   'idle_seconds');
  numField('fetchMaxWaitSec',      parseFloat,             60.0,  'max_wait_seconds');
  numField('fetchScrollMax',       (s) => parseInt(s, 10), 3000,  'scroll_max');
  numField('fetchPostClickSec',    parseFloat,             5.0,   'post_click_seconds');
  // min_asset_size_bytes: parse human sizes ("1k"/"10kb"/1024) and send
  // it whenever the operator entered something parseable -- INCLUDING 0
  // ("no filter"). Sending it explicitly stops the hub from overlaying
  // the Settings default, which previously made a dropped/blank value
  // silently become the 10KB Settings threshold.
  {
    const el = $('fetchMinAssetBytes');
    if (el) {
      const v = parseHumanBytes(el.value);
      if (Number.isFinite(v) && v >= 0) opts.min_asset_size_bytes = v;
    }
  }
  // Text fields: omit when blank so JobOptions's Optional[str]=None wins.
  const txt = (id, key) => {
    const el = $(id);
    if (!el) return;
    const v = (el.value || '').trim();
    if (v) opts[key] = v;
  };
  txt('fetchReferer',      'referer');
  txt('fetchAttachToJob',  'attach_to_job');
  // Phase 2a: include fetch_strategy when the operator picked something
  // other than the default ("recipe"). Omit on default so payloads stay
  // round-trippable with the server default.
  const sub = currentFetchSubMode();
  if (sub === 'normal') opts.fetch_strategy = 'normal';
  return opts;
}

document.getElementById('submit').addEventListener('submit', async e => {
  e.preventDefault();
  // Lock the submit button for the whole "validate -> POST -> attach"
  // window so a double-click / Enter-spam can't fire two jobs in a
  // row. The label flips to a spinning loader so the operator sees
  // that the click registered. Restored in the finally block below.
  const submitBtn = document.getElementById('submitBtn');
  const submitLbl = document.getElementById('submitBtnLabel');
  const originalLabel = submitLbl ? submitLbl.innerHTML : '▶ submit';
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.style.opacity = '0.7';
    submitBtn.style.cursor = 'wait';
  }
  if (submitLbl) {
    submitLbl.innerHTML =
      '<iconify-icon icon="lucide:loader-circle" class="spin"></iconify-icon> 起動中…';
  }
  // Single unlock point: restore the button before any return path.
  const _restoreSubmitBtn = () => {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.style.opacity = '';
      submitBtn.style.cursor = '';
    }
    if (submitLbl) submitLbl.innerHTML = originalLabel;
  };
  try {
  const url = document.getElementById('urlInput').value.trim();
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';

  let body;
  if (mode === 'fetch') {
    if (!url) { alert('URL is required for Fetch mode.'); _restoreSubmitBtn(); return; }
    // Phase 2a: AI調査 sub-mode short-circuits to codegen-loop. The
    // Fetch toggles / cookies / scroll knobs DON'T apply -- the LLM
    // controls its own session via pap.* so we send a minimal payload.
    const _sub = currentFetchSubMode();
    if (_sub === 'ai_investigate') {
      const _goal = (document.getElementById('fetchInvestigateGoal').value || '').trim();
      if (!_goal) {
        alert('「AI で解析する」を選んだ場合は解析の目標 (goal) が必須です。テキストエリアに記入してください。');
        _restoreSubmitBtn();
        return;
      }
      const _max = parseInt(document.getElementById('fetchInvestigateMaxAttempts').value, 10) || 3;
      const _tmo = parseInt(document.getElementById('fetchInvestigateTimeoutSec').value, 10) || 600;
      // Operator-picked engine (same dropdown shape as LLM mode's
      // codegenEngineSelect, but an independent selection). Empty
      // string = "use the hub's env defaults"; in that case we omit
      // the field entirely so the server takes its fallback path.
      const _engineSel = document.getElementById('fetchInvestigateEngineSelect');
      const _engineSlug = (_engineSel && _engineSel.value || '').trim();
      // Start from the full Fetch options (download_video, cookies_from,
      // referer, min_asset_size_bytes, etc.) so the operator's toggles
      // are honoured. Then overlay the codegen-loop-specific fields.
      const _fetchOpts = (typeof buildFetchOptionsFromForm === 'function')
        ? buildFetchOptionsFromForm()
        : {};
      body = {
        url,
        options: {
          ..._fetchOpts,
          mode: 'codegen-loop',
          goal: _goal,
          max_codegen_attempts: _max,
          attempt_timeout_s: _tmo,
        },
      };
      if (_engineSlug) body.options.codegen_engine = _engineSlug;
    } else {
      body = { url, options: buildFetchOptionsFromForm() };
    }
  } else if (mode === 'ai') {
    if (!url) { alert('URL is required for AI mode.'); _restoreSubmitBtn(); return; }
    const engine = currentAiEngine();
    const rawGoal = document.getElementById('goalInput').value.trim();
    const countVal = parseInt(document.getElementById('maxAttempts').value, 10);
    const timeoutVal = parseInt(document.getElementById('attemptTimeout').value, 10);

    if (engine === 'simple') {
      // Simple engine: compile the UI-built macro rows to a paprika-
      // client script and submit as mode=rerun. No LLM in the loop;
      // execution is fully deterministic. CogAgent / agent calls
      // inside the macro still apply (Click visual / Agent rows).
      if (!_simpleRows || _simpleRows.length === 0) {
        alert('Simple モードは少なくとも 1 つの step が必要です。+ add step で追加してください。');
        _restoreSubmitBtn();
        return;
      }
      const code = compileSimpleMacroToCode(url);
      const simpleTimeoutEl = document.getElementById('attemptTimeoutSimple');
      const simpleTimeout = parseInt(simpleTimeoutEl && simpleTimeoutEl.value, 10) || 600;
      body = {
        url: url || 'about:blank',
        options: {
          mode: 'rerun',
          code,
          attempt_timeout_s: simpleTimeout,
        }
      };
    } else {
      // LLM engine: existing codegen-loop pipeline.
      let goal = rawGoal || DEFAULT_CRAWL_GOAL;
      // host_dedup OFF: append an explicit "use host_dedup=False"
      // line so the LLM emits pap.walk(host_dedup=False). When ON we
      // change nothing (the walker's default is True).
      const dedupChk = document.getElementById('llmHostDedup');
      if (dedupChk && !dedupChk.checked) {
        goal += '\n\n追加ガードレール:\n  - **pap.walk(..., host_dedup=False)** を必ず指定する (既訪問URLも再クロール)';
      }
      // Operator-picked engine from the dropdown next to max_attempts.
      // Empty string = "use the hub's env defaults" (= don't send the
      // field at all so the server takes its fallback path).
      const engineSel = document.getElementById('codegenEngineSelect');
      const engineSlug = (engineSel && engineSel.value || '').trim();
      body = {
        url,
        options: {
          mode: 'codegen-loop',
          goal,
          max_codegen_attempts: countVal || 3,
          attempt_timeout_s: timeoutVal || 86400,
        }
      };
      if (engineSlug) body.options.codegen_engine = engineSlug;
    }
  } else if (mode === 'code') {
    const code = document.getElementById('codeInput').value;
    if (!code.trim()) { alert('Paste a Python script into the Code textarea.'); _restoreSubmitBtn(); return; }
    const codeTimeout = parseInt(document.getElementById('codeTimeout').value, 10) || 180;
    body = {
      // url is optional for Code mode; default to about:blank if empty so
      // JobRequest.url validation passes. The script chooses its own
      // initial_url anyway via cli.session(initial_url=...).
      url: url || 'about:blank',
      options: {
        mode: 'rerun',
        code,
        attempt_timeout_s: codeTimeout,
      }
    };
  } else {
    alert('Unknown mode: ' + mode);
    _restoreSubmitBtn();
    return;
  }
  // Tear down any previous Live panel before the POST so the visual
  // transition is instant. Without this the old job's panel keeps
  // showing for the duration of the network round-trip (~200ms) and
  // then gets replaced by ljpAttach -- which produced a "stale panel
  // briefly visible after I hit Submit" flicker. ljpAttach below
  // calls ljpReset again on success, which is idempotent.
  if (typeof ljpReset === 'function') ljpReset();
  const r = await fetch('/jobs', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  if (!r.ok) {
    const err = await r.json().catch(() => null);
    const detail = err && (Array.isArray(err.detail) ? err.detail.map(d => d.msg).join('\n') : err.detail);
    alert('submit failed (' + r.status + '): ' + (detail || r.statusText));
    _restoreSubmitBtn();
    return;
  }
  const created = await r.json().catch(() => null);
  // Intentionally NOT clearing urlInput / goalInput here: operators
  // commonly tweak the same URL/Goal and resubmit ("try again with
  // higher max_steps", "same URL different engine", etc.). The Clear
  // button next to Submit gives an explicit reset when desired.
  // Reveal the inline live panel underneath the form -- log on the left,
  // noVNC iframes on the right. Works for both modes:
  //   fetch -> the job carries a single novnc_url on JobInfo
  //   llm   -> sessions opened by the runner show up via /jobs/{id}/sessions
  if (created && created.job_id) {
    ljpAttach(created.job_id);
  }
  refresh();
  } catch (e) {
    alert('submit error: ' + (e && e.message || e));
  } finally {
    // Always restore the button on the success path too -- by this
    // point ljpAttach has hooked up the Live panel, so the operator
    // sees the job state via the panel and can submit another job.
    _restoreSubmitBtn();
  }
});
// Polling loop with Page Visibility gating. The admin UI is routinely
// left open in a background browser tab; without this guard each open
// tab keeps hammering /health + /workers + /jobs + /sessions every 2s
// forever (≈2 req/s/tab) even when nobody is looking. We pause the
// loop while document.hidden is true and resume — with an immediate
// catch-up refresh — the moment the tab becomes visible again.
const REFRESH_INTERVAL_MS = 2000;
let _refreshTimer = null;
function _startRefreshLoop() {
  if (_refreshTimer !== null) return;   // already running
  _refreshTimer = setInterval(refresh, REFRESH_INTERVAL_MS);
}
function _stopRefreshLoop() {
  if (_refreshTimer === null) return;
  clearInterval(_refreshTimer);
  _refreshTimer = null;
}
// LJP timers also pause on hidden -- once a Live panel is attached
// the job-specific polling (status/sessions/code at 2.5-4s) keeps
// going independently of the main refresh loop. Without this gate the
// LJP keeps polling /jobs/{id} forever in a backgrounded tab.
function _ljpRestartTimersIfNeeded() {
  if (!LJP.jobId || LJP.finished || LJP._terminalStopped) return;
  if (!LJP.pollTimer) LJP.pollTimer = setInterval(ljpRefreshSessions, 3000);
  if (!LJP.statusTimer) LJP.statusTimer = setInterval(ljpRefreshStatus, 2500);
  if (!LJP.codeTimer) LJP.codeTimer = setInterval(ljpRefreshCode, 4000);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _stopRefreshLoop();
    // Also pause LJP per-job polling. ljpStopTimers clears all three
    // (status / sessions / code) — they get re-armed on visibility
    // resume below if LJP is still attached.
    if (typeof ljpStopTimers === 'function') ljpStopTimers();
  } else {
    // Tab came back to the foreground: refresh once immediately so the
    // operator sees current state without waiting a full interval, then
    // resume the periodic loop.
    refresh();
    _startRefreshLoop();
    // Catch-up LJP refresh + restart its timers if a job is attached.
    if (LJP.jobId && !LJP.finished && !LJP._terminalStopped) {
      try { ljpRefreshStatus(); } catch (_) {}
      try { ljpRefreshSessions(); } catch (_) {}
      try { ljpRefreshCode(); } catch (_) {}
      _ljpRestartTimersIfNeeded();
    }
  }
});
// Initial paint + loop. If the page somehow loads already-hidden
// (prerender / background open), start polling only when it first
// becomes visible -- the visibilitychange handler covers that.
refresh();
if (!document.hidden) _startRefreshLoop();

