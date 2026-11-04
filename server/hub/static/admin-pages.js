// 取得ページ (#pages) -- the fetched-page wall, embedded in the admin UI.
//
// Same feed as the standalone /wall kiosk page (server/hub/wallfeed.py):
// every completed fetch uploads a full-page screenshot, the hub thumbnails a
// rate-capped sample of them into a Redis ring, and this polls the ring with a
// cursor. The differences from /wall are all about living inside a tab:
//
//   * it polls ONLY while its panel is active AND the document is visible --
//     an admin left open on #jobs costs this feature nothing;
//   * the card count is computed from the board's own box, not the viewport;
//   * labels go through the admin's i18n (t()), so the language switch works.
//
// Playout is a jitter buffer with decoded frames -- see releaseTick() for why.

(() => {
  "use strict";

  const PANEL = () => document.querySelector('.panel[data-panel="pages"]');
  const grid = () => document.getElementById('pgGrid');
  const board = () => document.getElementById('pgBoard');

  const POLL_MS = 1000;
  const RELEASE_MIN_MS = 110;
  const RELEASE_MAX_MS = 1400;
  const TARGET_BUFFER = 6;        // decoded cards to keep waiting
  const LOAD_TIMEOUT_MS = 8000;

  let cursor = 0;                 // highest seq rendered
  let total = 0;                  // cards shown since the tab was first opened
  let paused = false;
  let inFlight = false;
  let supplyRate = 3;             // cards/sec the server says it can feed us
  let queue = [];                 // entries awaiting playout (image preloading)
  const liveCards = [];           // on-board cards, newest first
  let maxCards = 12;
  let pollTimer = null;
  let releaseTimer = null;

  // i18n: the admin translates data-i18n attributes through i18next, but the
  // header units are written from JS, so they go through i18next directly.
  // The literal is the Japanese fallback, so a failed CDN load (LAN with no
  // internet) still renders words rather than raw keys.
  function tr(key, fallback) {
    try {
      if (window.i18next && window.i18next.t) {
        const v = window.i18next.t(key);
        if (v && v !== key) return v;
      }
    } catch (_) { /* fall through */ }
    return fallback;
  }

  function active() {
    const p = PANEL();
    return !!p && p.classList.contains('active') && !document.hidden;
  }

  // ---- layout ---------------------------------------------------------
  // Show exactly as many cards as the board can hold: anything more is
  // fetched, decoded and then clipped away by overflow:hidden, and it leaves
  // a half-cut row along the bottom edge.
  // fitCards() can only measure once the panel is actually laid out (a hidden
  // panel has clientHeight 0). The board is often built while the tab is in
  // the background, so measurement is deferred until the first moment it can
  // succeed, and invalidated whenever the box could have changed.
  let fitted = false;
  function ensureFit() {
    const g = grid();
    if (!fitted && g && g.clientHeight > 0 && g.clientWidth > 0) {
      fitCards();
      fitted = true;
    }
  }

  function fitCards() {
    const g = grid();
    if (!g) return;
    // Measure against the CSS height budget, not against whatever we set last
    // time, or the board would ratchet smaller on every resize.
    g.style.height = '';
    const cs = getComputedStyle(g);
    const cols = cs.gridTemplateColumns.split(' ').filter(Boolean).length || 1;
    const gap = parseFloat(cs.gap) || 12;
    const colW = (g.clientWidth - (cols + 1) * gap) / cols;
    const ratio = parseFloat(cs.getPropertyValue('--pg-thumb-ratio')) / 100 || 0.781;
    const cardH = colW * ratio + 27;          // + caption
    const avail = g.clientHeight - 2 * gap;
    const rows = Math.max(1, Math.floor((avail + gap) / (cardH + gap)));
    maxCards = Math.max(4, cols * rows);
    // Shrink the board to exactly the rows that fit: the leftover strip of a
    // partial row is dead space inside a dark panel, which reads as a bug.
    g.style.height = Math.ceil(rows * cardH + (rows + 1) * gap) + 'px';
  }

  // ---- jitter buffer --------------------------------------------------
  // Entries are queued with their thumbnail already downloading, and only
  // entries whose image finished decoding are ever inserted -- otherwise a
  // card sits on the board as a black rectangle until its pixels turn up.
  // Playout is paced from the throughput the server reports, so the display
  // rate tracks the real supply instead of the (jittery) poll timing.
  function enqueue(item) {
    const entry = { item: item, img: null, state: 'loading' };
    queue.push(entry);
    const img = new Image();
    const fail = () => { if (entry.state === 'loading') entry.state = 'failed'; };
    const done = () => {
      if (entry.state !== 'loading') return;
      entry.img = img;
      entry.state = 'ready';
    };
    img.onload = () => {
      // decode() moves the bitmap work off the insertion frame.
      if (img.decode) img.decode().then(done, done); else done();
    };
    img.onerror = fail;           // thumbnail aged out of the ring, or a blip
    setTimeout(fail, LOAD_TIMEOUT_MS);
    img.src = '/wall/thumb/' + item.seq + '.jpg';
  }

  function fmtMs(ms) {
    if (ms == null) return '';
    return ms < 10000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms / 1000) + 's';
  }

  function addCard(entry) {
    const g = grid();
    if (!g) return;
    const item = entry.item;
    const el = document.createElement('div');
    el.className = 'pg-card fresh';
    const thumb = document.createElement('div');
    thumb.className = 'pg-thumb';
    entry.img.alt = '';
    thumb.appendChild(entry.img);
    const meta = document.createElement('div');
    meta.className = 'pg-meta';
    const host = document.createElement('span');
    host.className = 'pg-host';
    host.textContent = item.host || item.url || item.job_id;
    host.title = item.url || '';
    const ms = document.createElement('span');
    ms.className = 'pg-ms';
    ms.textContent = fmtMs(item.elapsed_ms);
    const lane = document.createElement('span');
    lane.className = 'pg-lane';
    lane.textContent = item.worker_id ? (item.worker_id + '#' + item.lane_idx) : '';
    meta.append(host, ms, lane);
    el.append(thumb, meta);

    const empty = document.getElementById('pgEmpty');
    if (empty) empty.remove();
    g.prepend(el);
    liveCards.unshift(el);
    setTimeout(() => el.classList.remove('fresh'), 1700);

    total += 1;
    const tot = document.getElementById('pgTotal');
    if (tot) tot.textContent = total.toLocaleString();

    // Evict synchronously: a card animating away is still laid out, so it
    // spills into a partial extra row for the length of the animation.
    while (liveCards.length > maxCards) liveCards.pop().remove();
  }

  function releaseTick() {
    ensureFit();
    while (queue.length && queue[0].state === 'failed') queue.shift();
    if (!paused && active()) {
      const i = queue.findIndex((e) => e.state === 'ready');
      if (i >= 0) addCard(queue.splice(i, 1)[0]);
    }
    const ready = queue.reduce((n, e) => n + (e.state === 'ready' ? 1 : 0), 0);
    // At target buffer depth we play at exactly the supply rate; ±50% at the
    // extremes drains a backlog without a visible rush, and stretches the last
    // few cards across a stalled poll instead of running dry.
    const correction = 1 + 0.5 * Math.max(-1, Math.min(1,
      (ready - TARGET_BUFFER) / TARGET_BUFFER));
    const rate = Math.max(0.7, supplyRate * correction);
    const ms = ready
      ? Math.max(RELEASE_MIN_MS, Math.min(RELEASE_MAX_MS, 1000 / rate))
      : RELEASE_MAX_MS;
    releaseTimer = setTimeout(releaseTick, ms);
  }

  // ---- header ---------------------------------------------------------
  function drawSpark(series) {
    const c = document.getElementById('pgSpark');
    if (!c) return;
    const ctx = c.getContext('2d');
    const W = c.width, H = c.height;
    ctx.clearRect(0, 0, W, H);
    if (!series || !series.length) return;
    const max = Math.max(1, ...series);
    const bw = W / series.length;
    for (let i = 0; i < series.length; i++) {
      const h = Math.max(1, (series[i] / max) * (H - 6));
      ctx.fillStyle = 'rgba(245,184,0,' + (0.25 + 0.75 * (i / series.length)) + ')';
      ctx.fillRect(i * bw, H - h, Math.max(1, bw - 1), h);
    }
  }

  function applyStats(s) {
    if (!s) return;
    if (typeof s.card_rate === 'number' && s.card_rate > 0) supplyRate = s.card_rate;
    const sec = document.getElementById('pgSec');
    const min = document.getElementById('pgMin');
    const fleet = document.getElementById('pgFleet');
    if (sec) sec.innerHTML = (s.per_sec || 0).toFixed(1)
      + '<span class="u">' + tr('pages.unit.persec', 'ページ/秒') + '</span>';
    if (min) min.innerHTML = (s.per_min || 0).toLocaleString()
      + '<span class="u">' + tr('pages.unit.permin', '/分') + '</span>';
    if (fleet) {
      fleet.textContent = s.workers
        ? (s.workers.toLocaleString() + ' ' + tr('pages.unit.workers', 'ワーカー稼働中'))
        : tr('pages.metric.fleet', 'フリート');
    }
    drawSpark(s.series);
  }

  // ---- polling --------------------------------------------------------
  async function tick() {
    if (inFlight || !active()) return;
    ensureFit();
    inFlight = true;
    // Abandon a stalled poll rather than holding the only in-flight slot:
    // nginx round-robins, so the retry usually lands on a hub that answers at
    // once. Without this one stuck hub freezes the board.
    const ac = new AbortController();
    const killer = setTimeout(() => ac.abort(), POLL_MS * 4);
    try {
      const r = await fetch('/wall/feed?after=' + cursor + '&limit=' + maxCards,
                            { cache: 'no-store', signal: ac.signal });
      if (!r.ok) return;
      const d = await r.json();
      applyStats(d.stats);
      if (d.seq > cursor) cursor = d.seq;
      if (paused) return;              // cursor still advances, so resuming
      const items = d.items || [];     // shows NOW, not a stale backlog
      if (!items.length) return;
      for (const it of items.slice(-maxCards)) enqueue(it);
      if (queue.length > maxCards) queue.splice(0, queue.length - maxCards);
    } catch (_) {
      /* abort or transient blip: the next tick retries */
    } finally {
      clearTimeout(killer);
      inFlight = false;
    }
  }

  // ---- wiring ---------------------------------------------------------
  function start() {
    fitted = false;
    ensureFit();
    if (!pollTimer) pollTimer = setInterval(tick, POLL_MS);
    if (!releaseTimer) releaseTick();
    tick();
  }

  function init() {
    const p = PANEL();
    if (!p) return;                    // older admin.html without the panel

    const safeBtn = document.getElementById('pgSafe');
    if (safeBtn) {
      // Blur for showing the board to visitors. Remembered per browser, and
      // shared with the standalone /wall page's own toggle by key name.
      const setSafe = (on) => {
        const b = board();
        if (b) b.classList.toggle('safe', on);
        safeBtn.classList.toggle('active', on);
        try { localStorage.setItem('wallSafe', on ? '1' : '0'); } catch (_) {}
      };
      let on = false;
      try { on = localStorage.getItem('wallSafe') === '1'; } catch (_) {}
      setSafe(on);
      safeBtn.addEventListener('click', () => {
        const b = board();
        setSafe(!(b && b.classList.contains('safe')));
      });
    }

    const pauseBtn = document.getElementById('pgPause');
    if (pauseBtn) {
      pauseBtn.addEventListener('click', () => {
        paused = !paused;
        pauseBtn.classList.toggle('active', paused);
        const label = pauseBtn.querySelector('span');
        if (label) {
          label.textContent = paused
            ? tr('pages.resume', '再開') : tr('pages.pause', '一時停止');
        }
      });
    }

    // Only run while the tab is the one on screen.
    document.querySelectorAll('.tab[data-tab="pages"]').forEach((b) => {
      b.addEventListener('click', () => setTimeout(start, 0));
    });
    document.addEventListener('visibilitychange', () => {
      if (!active()) return;
      fitted = false;
      ensureFit();
      tick();
    });
    addEventListener('resize', () => {
      fitted = false;
      ensureFit();
      while (liveCards.length > maxCards) liveCards.pop().remove();
    });
    // Deep link / reload straight onto #pages.
    if (active()) start();
    else if (!releaseTimer) releaseTick();
    if (!pollTimer) pollTimer = setInterval(tick, POLL_MS);
  }

  // Small ops hatch: `__pagesDebug()` in the console reports why the board is
  // or isn't moving (buffer depth, paced rate, whether the panel counts as
  // active) without needing a source map.
  window.__pagesDebug = () => ({
    queued: queue.length,
    ready: queue.filter((e) => e.state === 'ready').length,
    loading: queue.filter((e) => e.state === 'loading').length,
    failed: queue.filter((e) => e.state === 'failed').length,
    onBoard: liveCards.length,
    maxCards: maxCards, supplyRate: supplyRate, paused: paused,
    active: active(), fitted: fitted, cursor: cursor, total: total,
    timers: { poll: !!pollTimer, release: !!releaseTimer },
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
