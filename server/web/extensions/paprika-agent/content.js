// Paprika Agent -- content script relay (page <-> service worker).
//
// The worker can't reliably attach to the (dormant) MV3 service worker
// over CDP, but it CAN evaluate JS in the page. So the worker posts a
// command on the page's window; this content script (injected into the
// page) forwards it to the service worker via chrome.runtime.sendMessage
// -- which wakes the worker -- and posts the response back on the
// window, where the worker's evaluated promise is waiting.
//
// Protocol (all on window.postMessage, same-window only):
//   request : { __paprikaAgentReq: "<id>", cmd: "...", args: {...} }
//   response: { __paprikaAgentResp: "<id>", ok, result?, error? }

window.addEventListener("message", (ev) => {
  if (ev.source !== window) return;
  const d = ev.data;
  if (!d || typeof d.__paprikaAgentReq !== "string") return;
  const reqId = d.__paprikaAgentReq;
  try {
    chrome.runtime.sendMessage(
      { __paprikaAgent: true, cmd: d.cmd, args: d.args },
      (resp) => {
        const err = chrome.runtime.lastError;
        window.postMessage(
          {
            __paprikaAgentResp: reqId,
            ok: !err && !!(resp && resp.ok),
            result: resp ? resp.result : undefined,
            error: err ? err.message : (resp ? resp.error : "no response"),
          },
          "*",
        );
      },
    );
  } catch (e) {
    window.postMessage(
      { __paprikaAgentResp: reqId, ok: false, error: String((e && e.message) || e) },
      "*",
    );
  }
});
