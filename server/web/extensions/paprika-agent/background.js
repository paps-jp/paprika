// Paprika Agent -- built-in worker helper (service worker side).
//
// No UI. The worker drives this extension to reach Chrome capabilities
// the DevTools Protocol (CDP) / nodriver can't do directly -- the
// canonical case is genuine per-tab page zoom (chrome.tabs.setZoom),
// which reflows like the browser's Ctrl+/Ctrl- menu zoom and works even
// on full-viewport cross-origin iframe players (CSS zoom can't; CDP
// setPageScaleFactor is only a pinch-zoom).
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

const AGENT_VERSION = "0.2.0";

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

const HANDLERS = {
  async ping() {
    return { pong: true, version: AGENT_VERSION };
  },

  // Genuine per-tab page zoom (reflows == the browser's menu zoom).
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

  // Read the current per-tab zoom factor.
  async getZoom(args, sender) {
    let tabId = targetTabId(args, sender);
    if (tabId == null) tabId = (await activeTab()).id;
    const factor = await chrome.tabs.getZoom(tabId);
    return { factor, tab_id: tabId };
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
