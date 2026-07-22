// Paprika Agent -- built-in worker helper (service worker side).
//
// No UI. The worker drives this extension to reach Chrome capabilities
// the DevTools Protocol (CDP) / nodriver can't do directly -- genuine
// per-tab page zoom (chrome.tabs.setZoom), request blocking / header
// rewrite (declarativeNetRequest), per-site content settings, privacy
// knobs, controlled downloads, per-profile proxy, ...
//
// REACHABILITY: an MV3 service worker is dormant until an event wakes
// it, so the worker can't reliably attach to its CDP target. Instead
// the worker evaluates a tiny snippet in the PAGE that postMessages a
// command; content.js (injected in the page) relays it here via
// chrome.runtime.sendMessage -- which WAKES this worker -- and relays
// the response back. So commands work on demand regardless of dormancy.
//
// EXTENDING: add an async function to HANDLERS keyed by command name.
// It receives (args, sender) and returns a JSON-able value (or throws).
// The worker reaches every handler through the single generic "ext"
// session action -> POST /sessions/{id}/ext {cmd,args}; thin typed
// wrappers live in the Python client (_page.py).

const AGENT_VERSION = "0.4.4";

async function activeTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true });
  if (!tabs.length) tabs = await chrome.tabs.query({});
  if (!tabs.length) throw new Error("no tab available");
  return tabs[0];
}

// Resolve the target tab: prefer the tab that relayed the command (the
// page the operator is actually on), else the active tab, else args.
function targetTabId(args, sender) {
  if (args && args.tab_id != null) return Number(args.tab_id);
  if (sender && sender.tab && sender.tab.id != null) return sender.tab.id;
  return null;
}

// ---- webNavigation event buffer -------------------------------------
// The SW is dormant between events, so an in-memory array would be lost
// on unload. Persist a ring buffer in storage.session (survives SW
// restarts within the browser session; cleared on browser restart).
const NAV_KEY = "navEvents";
// Operator-event recorder (programming-by-demonstration MVP1).
// Two storage.session keys:
//   opRecording: bool      -- recording active flag
//   opEvents:    array     -- ring buffer (max 1000), kept under 1 MB of session storage
const OP_REC_KEY = "opRecording";
const OP_EVT_KEY = "opEvents";
const OP_EVT_MAX = 1000;

// Pre-click screenshot pre-fetch buffer. content.js fires a
// prefetchClipForCursor command on every mousedown that happens
// while recording is active; this handler kicks off captureVisibleTab
// immediately (capturing the page in its pre-click visual state) and
// stores the still-resolving Promise under a content-script-supplied id.
// When the corresponding click event arrives a few ms later carrying
// the same id in __prefetchId, pushOperatorEvent awaits the buffered
// promise and attaches the resulting clip to the event -- so the
// recorded image is "what the operator saw when they decided to click"
// rather than "what the page looks like after the click started
// processing". Entries TTL out after 2s in case mousedown happened
// without a follow-up click (drag, cancelled gesture).
const PREFETCH_TTL_MS = 2000;
const _prefetchBuffer = new Map(); // id -> { promise: Promise<dataURL|null>, t: ms }
let _prefetchInFlight = false;     // single-flight gate (see prefetchClipForCursor)

function _prefetchSweep() {
  const now = Date.now();
  for (const [k, v] of _prefetchBuffer) {
    if (now - v.t > PREFETCH_TTL_MS) _prefetchBuffer.delete(k);
  }
}

// MVP1 phase 2: capture a small JPEG crop around the click/change
// target so the recorded demonstration carries the same visual context
// (button colour / shape, surrounding labels) that the operator's eye
// used to find it. Pure pixel record; the DOM selector handles the
// "where" — this is the "what does it look like".
//
// Pipeline:
//   chrome.tabs.captureVisibleTab -> JPEG dataURL of full tab
//   createImageBitmap (works in MV3 SW) -> ImageBitmap
//   OffscreenCanvas + drawImage -> crop region with padding around bbox
//   convertToBlob(jpeg quality 0.75) -> Blob
//   FileReader.readAsDataURL -> base64 dataURL string we can stash on
//   the event in chrome.storage.session
//
// Sizing:
//   * 40px padding on each side of the bbox so the visual neighbourhood
//     is recognisable, not just the pixel of the click target itself.
//   * Down-scaled to fit in a 400x400 box so storage stays small (~25KB
//     per clip after JPEG encode). 50 events x 25KB = ~1.2 MB << the
//     10 MB storage.session quota.
//
// Failure mode: best-effort. Any error (no active tab, captureVisibleTab
// not allowed, OffscreenCanvas unsupported, ...) returns null and the
// caller simply omits the clip from the event. Recording continues.
async function _captureBboxClip(tabId, bbox, viewport) {
  const PAD = 40;
  const cx = Math.max(0, Math.floor(bbox.x - PAD));
  const cy = Math.max(0, Math.floor(bbox.y - PAD));
  const cw = Math.ceil(bbox.w + 2 * PAD);
  const ch = Math.ceil(bbox.h + 2 * PAD);

  // captureVisibleTab needs a windowId (defaults to lastFocusedWindow).
  // Use the sender tab's window when available so we capture exactly
  // the tab the operator clicked on, not whatever Chrome considers
  // "focused" at this instant.
  let windowId = null;
  if (tabId != null) {
    try {
      const t = await chrome.tabs.get(tabId);
      windowId = t.windowId;
    } catch (_e) {}
  }

  // Capture the visible viewport of the active tab as JPEG.
  const dataUrl = await new Promise((resolve) => {
    chrome.tabs.captureVisibleTab(
      windowId,
      { format: "jpeg", quality: 80 },
      (url) => {
        if (chrome.runtime.lastError) resolve(null);
        else resolve(url);
      },
    );
  });
  if (!dataUrl) return null;

  // Decode the JPEG into an ImageBitmap we can draw into a Canvas.
  let bitmap;
  try {
    const blob = await (await fetch(dataUrl)).blob();
    bitmap = await createImageBitmap(blob);
  } catch (_e) {
    return null;
  }

  // Clamp the crop rect to the image bounds (bbox can go negative
  // when scrolled, or off-screen at the right edge).
  const sx = Math.max(0, Math.min(bitmap.width - 1, cx));
  const sy = Math.max(0, Math.min(bitmap.height - 1, cy));
  const sw = Math.max(1, Math.min(bitmap.width - sx, cw));
  const sh = Math.max(1, Math.min(bitmap.height - sy, ch));

  // Scale down to fit MAX x MAX. Preserves aspect ratio.
  const MAX = 400;
  const scale = Math.min(1, MAX / Math.max(sw, sh));
  const dw = Math.max(1, Math.round(sw * scale));
  const dh = Math.max(1, Math.round(sh * scale));

  let outBlob;
  try {
    const canvas = new OffscreenCanvas(dw, dh);
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, sx, sy, sw, sh, 0, 0, dw, dh);
    outBlob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.75 });
  } catch (_e) {
    return null;
  } finally {
    try { bitmap.close(); } catch (_) {}
  }

  // Convert Blob -> base64 dataURL. FileReader works in MV3 SWs.
  return await new Promise((resolve) => {
    const r = new FileReader();
    r.onloadend = () => resolve(typeof r.result === "string" ? r.result : null);
    r.onerror = () => resolve(null);
    r.readAsDataURL(outBlob);
  });
}
function pickNav(d) {
  return {
    tabId: d.tabId, frameId: d.frameId, url: d.url,
    sourceTabId: d.sourceTabId, sourceFrameId: d.sourceFrameId,
    transitionType: d.transitionType, error: d.error,
  };
}
async function pushNav(type, d) {
  try {
    const cur = (await chrome.storage.session.get(NAV_KEY))[NAV_KEY] || [];
    cur.push({ type, t: Date.now(), ...pickNav(d) });
    while (cur.length > 500) cur.shift();
    await chrome.storage.session.set({ [NAV_KEY]: cur });
  } catch (_e) { /* best-effort */ }
}
chrome.webNavigation.onCommitted.addListener((d) => pushNav("committed", d));
chrome.webNavigation.onCompleted.addListener((d) => pushNav("completed", d));
chrome.webNavigation.onCreatedNavigationTarget.addListener((d) => pushNav("newtarget", d));
chrome.webNavigation.onErrorOccurred.addListener((d) => pushNav("error", d));

const HANDLERS = {
  async ping() {
    return { pong: true, version: AGENT_VERSION };
  },

  // ----- page zoom (genuine, reflows like the menu zoom) -------------
  // args: { factor: number (1.0 = 100%), tab_id?: number }
  async setZoom(args, sender) {
    const factor = Number(args.factor);
    if (!isFinite(factor) || factor <= 0) {
      throw new Error("setZoom: 'factor' must be a positive number");
    }
    let tabId = targetTabId(args, sender);
    if (tabId == null) tabId = (await activeTab()).id;
    await chrome.tabs.setZoom(tabId, factor);
    return { factor, tab_id: tabId };
  },
  async getZoom(args, sender) {
    let tabId = targetTabId(args, sender);
    if (tabId == null) tabId = (await activeTab()).id;
    const factor = await chrome.tabs.getZoom(tabId);
    return { factor, tab_id: tabId };
  },

  // ----- declarativeNetRequest: block / rewrite headers --------------
  // args: { hosts: ["ads.example.com", ...], id_base?: number }
  async netBlock(args) {
    const base = args.id_base || 1000;
    const rt = ["main_frame", "sub_frame", "script", "image", "xmlhttprequest",
      "media", "stylesheet", "font", "object", "ping", "websocket", "other"];
    const rules = (args.hosts || []).map((h, i) => ({
      id: base + i, priority: 1,
      action: { type: "block" },
      condition: { urlFilter: "||" + h, resourceTypes: rt },
    }));
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: rules.map((r) => r.id), addRules: rules,
    });
    return { added: rules.length };
  },
  // args: { id?, header, value, urlFilter?, where?: "request"|"response" }
  async netSetHeader(args) {
    const id = args.id || 2000;
    const op = { header: args.header, operation: "set", value: String(args.value) };
    const action = { type: "modifyHeaders" };
    if ((args.where || "request") === "response") action.responseHeaders = [op];
    else action.requestHeaders = [op];
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [id],
      addRules: [{
        id, priority: 1, action,
        condition: {
          urlFilter: args.urlFilter || "*",
          resourceTypes: ["main_frame", "sub_frame", "xmlhttprequest",
            "media", "script", "image", "other"],
        },
      }],
    });
    return { id };
  },
  async netClear() {
    const ex = await chrome.declarativeNetRequest.getDynamicRules();
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: ex.map((r) => r.id),
    });
    return { removed: ex.length };
  },
  async netList() {
    return { rules: await chrome.declarativeNetRequest.getDynamicRules() };
  },

  // ----- contentSettings: per-site popups / images / js / auto-DL ----
  // args: { type, setting: "allow"|"block"|"session_only"|"ask", pattern? }
  async setContentSetting(args) {
    const cs = chrome.contentSettings[args.type];
    if (!cs) throw new Error("unknown content setting: " + args.type);
    await cs.set({ primaryPattern: args.pattern || "*://*/*", setting: args.setting });
    return { type: args.type, pattern: args.pattern || "*://*/*", setting: args.setting };
  },

  // ----- privacy: e.g. webRTC IP leak prevention ---------------------
  // args: { path: "network.webRTCIPHandlingPolicy", value: ... }
  async setPrivacy(args) {
    let node = chrome.privacy;
    for (const p of (args.path || "").split(".")) node = node && node[p];
    if (!node || !node.set) throw new Error("unknown privacy setting: " + args.path);
    await node.set({ value: args.value });
    return { path: args.path, value: args.value };
  },

  // ----- webNavigation: drain buffered nav events --------------------
  // args: { since?: epoch_ms }
  async getNavEvents(args) {
    const cur = (await chrome.storage.session.get(NAV_KEY))[NAV_KEY] || [];
    const since = args.since || 0;
    return { events: cur.filter((e) => e.t > since) };
  },

  // ----- operator-event recorder (programming-by-demonstration MVP1) -
  // The content script listens for clicks / changes / Enter / Esc /
  // submit / unload and forwards each event here for buffering. The
  // operator (or the admin UI on their behalf) toggles recording via
  // recordingStart / recordingStop; recordingState is the cheap poll
  // the content script makes to gate its forwarding. Buffer survives
  // SW unload via chrome.storage.session (same pattern as navEvents)
  // but is cleared on browser restart.
  //
  // Sensitive value handling lives in content.js (password fields are
  // never sent here; other input values are pre-truncated to 40 chars).
  // The background handler is dumb storage — by design — so a future
  // privacy review only has to audit one place.
  async recordingState() {
    const v = (await chrome.storage.session.get(OP_REC_KEY))[OP_REC_KEY];
    return { active: !!v };
  },
  async recordingStart() {
    await chrome.storage.session.set({ [OP_REC_KEY]: true });
    return { active: true };
  },
  async recordingStop() {
    await chrome.storage.session.set({ [OP_REC_KEY]: false });
    _prefetchBuffer.clear();
    return { active: false };
  },

  // Fired from content.js mousedown (capture phase). Kicks off the
  // captureVisibleTab + bbox crop synchronously so the page bitmap we
  // grab is the pre-click state. Returns immediately; the actual clip
  // is awaited later by pushOperatorEvent when the matching click
  // arrives carrying __prefetchId = this args.id.
  //
  // Single-flight: chrome.tabs.captureVisibleTab is rate-limited by
  // Chrome (~2/s); rapid clicking would queue multiple captures and
  // back-pressure the service worker, which makes EVERY other ext
  // command (including recordingStop / getOperatorEvents from the
  // admin UI Stop button) time out at the hub 30s gateway. So we
  // gate on _prefetchInFlight -- when one is running, subsequent
  // mousedowns just record metadata without a clip, and pushOperator
  // Event falls back to its own cursor-fallback capture path.
  async prefetchClipForCursor(args, sender) {
    const active = (await chrome.storage.session.get(OP_REC_KEY))[OP_REC_KEY];
    if (!active) return { skipped: "not recording" };
    const id = String(args && args.id || "");
    if (!id) return { skipped: "no id" };
    const x = Number(args.x), y = Number(args.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return { skipped: "bad coords" };
    if (_prefetchInFlight) {
      // Another prefetch is still running. Don't pile on; the click
      // path will fall back to bbox / cursor crop after the fact.
      return { skipped: "in flight" };
    }
    const tabId = sender && sender.tab && sender.tab.id;
    const r = 80;
    const rect = {
      x: Math.max(0, x - r),
      y: Math.max(0, y - r),
      w: r * 2,
      h: r * 2,
    };
    _prefetchInFlight = true;
    const promise = (async () => {
      try {
        return await _captureBboxClip(tabId, rect);
      } catch (_e) {
        return null;
      } finally {
        _prefetchInFlight = false;
      }
    })();
    _prefetchBuffer.set(id, { promise, t: Date.now() });
    _prefetchSweep();
    return { queued: true, buffered: _prefetchBuffer.size };
  },
  async pushOperatorEvent(args, sender) {
    // Gate again here too: content.js may race a stop, and we'd
    // rather lose a couple of events than capture past the stop.
    const active = (await chrome.storage.session.get(OP_REC_KEY))[OP_REC_KEY];
    if (!active) return { dropped: true };
    // Optional screenshot crop around the target bbox. The content
    // script sets __captureClip on click/change events; we strip the
    // flag here so it doesn't leak into the persisted payload. Best-
    // effort: any failure (tab busy, OffscreenCanvas not ready, ...)
    // just stores the event without the clip.
    const wantClip = !!args.__captureClip;
    delete args.__captureClip;
    // Pre-click pre-fetched clip (mousedown side path). When the
    // click event carries a __prefetchId matching a buffered Promise,
    // use that clip (= "pre-click visual state") instead of capturing
    // fresh here (= "post-click visual state, after the page may have
    // started rendering the click's side effect"). The bounded await
    // means we wait at most 800 ms for the pre-fetch -- if it's
    // somehow stuck we fall back to the live capture path below.
    const prefetchId = args.__prefetchId;
    delete args.__prefetchId;
    if (wantClip && prefetchId) {
      const entry = _prefetchBuffer.get(prefetchId);
      if (entry) {
        _prefetchBuffer.delete(prefetchId);
        try {
          const clip = await Promise.race([
            entry.promise,
            new Promise((res) => setTimeout(() => res(null), 800)),
          ]);
          if (clip) args.clip = clip;
        } catch (_) {}
      }
    }
    // Live-capture fallback path. Only taken when (a) no prefetched
    // clip was attached above AND (b) no other capture is currently
    // in flight -- otherwise we'd pile on captureVisibleTab calls and
    // back-pressure the service worker, blocking recordingStop /
    // getOperatorEvents at the hub 30s gateway. Skipping the fallback
    // gracefully leaves the event without a clip; the event metadata
    // alone is still useful for verbalisation.
    if (wantClip && !args.clip && !_prefetchInFlight) {
      const tgt = (args && args.target) || {};
      const bbox = tgt.bbox;
      // Prefer the element's bbox. Some sites use absolutely-positioned
      // elements (CSS transforms, transparent overlays for video player
      // hit-boxes -- e.g. an overlay play button) whose
      // getBoundingClientRect returns 0x0. Fall back to a small square
      // around the cursor so the visual neighbourhood is still
      // captured -- this is "what the operator was looking at" rather
      // than "what the engine thought the element was".
      let clipRect = null;
      if (bbox && bbox.w > 0 && bbox.h > 0) {
        clipRect = bbox;
      } else if (args.cursor && Number.isFinite(args.cursor.x) && Number.isFinite(args.cursor.y)) {
        const r = 80;
        clipRect = {
          x: Math.max(0, args.cursor.x - r),
          y: Math.max(0, args.cursor.y - r),
          w: r * 2,
          h: r * 2,
        };
      }
      if (clipRect) {
        _prefetchInFlight = true;
        try {
          const tabId = sender && sender.tab && sender.tab.id;
          const clip = await _captureBboxClip(tabId, clipRect, args.viewport);
          if (clip) args.clip = clip;
        } catch (_e) {
          // swallow — clip is purely a nice-to-have
        } finally {
          _prefetchInFlight = false;
        }
      }
    }
    const cur = (await chrome.storage.session.get(OP_EVT_KEY))[OP_EVT_KEY] || [];
    cur.push(args);
    while (cur.length > OP_EVT_MAX) cur.shift();
    await chrome.storage.session.set({ [OP_EVT_KEY]: cur });
    return { stored: true, buffered: cur.length, clipped: !!args.clip };
  },
  // args: { since?: epoch_ms, drain?: bool }
  async getOperatorEvents(args) {
    const cur = (await chrome.storage.session.get(OP_EVT_KEY))[OP_EVT_KEY] || [];
    const since = args.since || 0;
    const out = cur.filter((e) => (e.t || 0) > since);
    if (args.drain) {
      // Keep only events older than the cutoff (none of them, since
      // we just emitted everything > since). Simpler: wipe.
      await chrome.storage.session.set({ [OP_EVT_KEY]: [] });
    }
    return { events: out, total_buffered: cur.length };
  },

  // ----- userScripts: persistent MAIN-world page hooks ---------------
  // args: { id, code, matches?, world?, run_at? }
  async registerUserScript(args) {
    if (!chrome.userScripts) throw new Error("userScripts API unavailable");
    const def = {
      id: args.id, matches: args.matches || ["<all_urls>"],
      js: [{ code: args.code || "" }],
      world: args.world || "MAIN", runAt: args.run_at || "document_idle",
    };
    const ex = await chrome.userScripts.getScripts({ ids: [args.id] }).catch(() => []);
    if (ex.length) await chrome.userScripts.update([def]);
    else await chrome.userScripts.register([def]);
    return { id: args.id };
  },
  async unregisterUserScript(args) {
    await chrome.userScripts.unregister({ ids: [args.id] });
    return { id: args.id };
  },

  // ----- management: verify / toggle other extensions ----------------
  async listExtensions() {
    const all = await chrome.management.getAll();
    return {
      extensions: all.map((e) => ({
        id: e.id, name: e.name, enabled: e.enabled,
        type: e.type, version: e.version,
      })),
    };
  },
  async setExtensionEnabled(args) {
    await chrome.management.setEnabled(args.id, !!args.enabled);
    return { id: args.id, enabled: !!args.enabled };
  },

  // ----- downloads: controlled file download -------------------------
  // args: { url, filename?, conflictAction?, headers?: [{name,value}] }
  async download(args) {
    const opts = { url: args.url, saveAs: false };
    if (args.filename) opts.filename = args.filename;
    if (args.conflictAction) opts.conflictAction = args.conflictAction;
    if (Array.isArray(args.headers)) opts.headers = args.headers;
    const id = await chrome.downloads.download(opts);
    return { download_id: id };
  },
  async downloadSearch(args) {
    const items = await chrome.downloads.search(args.query || {});
    return {
      items: items.map((i) => ({
        id: i.id, url: i.url, filename: i.filename, state: i.state,
        paused: i.paused, error: i.error, mime: i.mime,
        bytesReceived: i.bytesReceived, totalBytes: i.totalBytes,
        startTime: i.startTime, endTime: i.endTime,
      })),
    };
  },
  async downloadCancel(args) { await chrome.downloads.cancel(args.id); return { id: args.id }; },
  async downloadPause(args) { await chrome.downloads.pause(args.id); return { id: args.id }; },
  async downloadResume(args) { await chrome.downloads.resume(args.id); return { id: args.id }; },

  // ----- proxy: per-profile (= per-lane) proxy config ----------------
  // args: { mode, host?, port?, scheme?, bypass?, pac_url? }
  async setProxy(args) {
    let config;
    const m = args.mode || "fixed_servers";
    if (m === "direct" || m === "system" || m === "auto_detect") config = { mode: m };
    else if (m === "pac_script") {
      config = { mode: "pac_script", pacScript: { url: args.pac_url, mandatory: true } };
    } else {
      config = {
        mode: "fixed_servers",
        rules: {
          singleProxy: {
            scheme: args.scheme || "http",
            host: args.host, port: Number(args.port),
          },
          bypassList: args.bypass || ["<local>"],
        },
      };
    }
    await chrome.proxy.settings.set({ value: config, scope: "regular" });
    return { mode: config.mode };
  },
  async getProxy() {
    const s = await chrome.proxy.settings.get({});
    return { levelOfControl: s.levelOfControl, value: s.value };
  },
  async clearProxy() {
    await chrome.proxy.settings.clear({ scope: "regular" });
    return { cleared: true };
  },
};

async function dispatch(cmd, args, sender) {
  const handler = HANDLERS[cmd];
  if (!handler) return { ok: false, error: "unknown command: " + cmd };
  try {
    return { ok: true, result: await handler(args || {}, sender) };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

// Messages relayed from content.js. Returning true keeps sendResponse
// alive for the async handler.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.__paprikaAgent !== true) return;
  dispatch(msg.cmd, msg.args, sender).then(sendResponse);
  return true;
});
