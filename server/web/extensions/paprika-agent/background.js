// Paprika Agent -- built-in worker helper.
//
// This extension has NO popup / UI. It exists so the Paprika worker can
// reach Chrome capabilities that the DevTools Protocol (CDP) and
// nodriver can't drive directly -- the canonical example is the genuine
// per-tab page zoom (chrome.tabs.setZoom), which reflows the page like
// the browser's Ctrl+/Ctrl- menu zoom. CSS `zoom` can't scale a
// full-viewport cross-origin iframe player, and CDP setPageScaleFactor
// is only a pinch-zoom; chrome.tabs.setZoom is the real thing.
//
// HOW THE WORKER CALLS IT
//   The worker enumerates CDP targets, finds THIS extension's service
//   worker target (by manifest name), attaches, and evaluates:
//       globalThis.__paprikaAgent.run("<cmd>", {<args>})
//   which returns {ok, result?|error}. Everything is funnelled through
//   one command bus so adding a capability = adding one handler below;
//   no new extension, no new messaging plumbing.
//
// EXTENDING: add an async function to HANDLERS keyed by command name.
// Each handler gets the parsed args object and returns a JSON-able
// value (or throws -> {ok:false,error}).

const AGENT_VERSION = "0.1.0";

async function activeTab() {
  // Prefer the focused window's active tab (the one the operator /
  // session is actually looking at), falling back to any active tab.
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs.length) tabs = await chrome.tabs.query({ active: true });
  if (!tabs.length) tabs = await chrome.tabs.query({});
  if (!tabs.length) throw new Error("no tab available");
  return tabs[0];
}

const HANDLERS = {
  // Liveness / discovery probe. The worker uses this to confirm it
  // attached to the right service worker.
  async ping() {
    return { pong: true, version: AGENT_VERSION };
  },

  // Genuine per-tab page zoom (reflows; == the browser's menu zoom).
  // args: { factor: number (1.0 = 100%), tab_id?: number }
  async setZoom(args) {
    const factor = Number(args.factor);
    if (!isFinite(factor) || factor <= 0) {
      throw new Error("setZoom: 'factor' must be a positive number");
    }
    const tabId = args.tab_id != null ? Number(args.tab_id) : (await activeTab()).id;
    await chrome.tabs.setZoom(tabId, factor);
    return { factor, tab_id: tabId };
  },

  // Read the current per-tab zoom factor.
  // args: { tab_id?: number }
  async getZoom(args) {
    const tabId = args.tab_id != null ? Number(args.tab_id) : (await activeTab()).id;
    const factor = await chrome.tabs.getZoom(tabId);
    return { factor, tab_id: tabId };
  },
};

// Single entry point the worker evaluates over CDP.
globalThis.__paprikaAgent = {
  version: AGENT_VERSION,
  commands: Object.keys(HANDLERS),
  async run(cmd, args) {
    const handler = HANDLERS[cmd];
    if (!handler) {
      return { ok: false, error: "unknown command: " + cmd };
    }
    try {
      const result = await handler(args || {});
      return { ok: true, result };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },
};
