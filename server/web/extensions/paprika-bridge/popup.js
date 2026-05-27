// Paprika Bridge -- popup logic.
//
// Browser-side companion to the Paprika Hub. The 0.x line ships
// with one capability ("push cookies to /hosts/{host}") but the
// bridge is designed to grow: send URLs to running jobs, share
// clipboard, pull job state, etc. New capabilities should add
// their own button / tab in this popup rather than forking.
//
// Current capability: read cookies via the chrome.cookies API and
// PUT them to the configured Paprika Hub's /hosts/{host} registry.
// No server-side component beyond Paprika itself. Designed so the
// operator can re-push at any time -- last write wins, so "I logged
// into a new site, push again" Just Works.
//
// Permissions used:
//   cookies        chrome.cookies.getAll() / chrome.cookies.getAllCookieStores()
//   storage        remember hub URL across popup invocations
//   tabs           read the active tab's URL to derive the default host
//   <all_urls>     required by chrome.cookies API
//
// Notes:
//   * Chrome 'host' cookies are returned with hostOnly=true; for those
//     we strip the leading dot the API never adds. Domain cookies
//     keep their leading-dot domain.
//   * SameSite mapping: chrome.cookies uses "no_restriction" / "lax" /
//     "strict" / "unspecified"; paprika /hosts/{host} accepts CDP-shape
//     "None" / "Lax" / "Strict". We translate before sending.

const STORAGE_KEY_HUB = 'paprika.hub_url';

// ----- helpers --------------------------------------------------------

function setStatus(msg, kind = 'info') {
  const el = document.getElementById('status');
  el.className = kind;
  el.textContent = msg;
}

function clearStatus() {
  const el = document.getElementById('status');
  el.className = '';
  el.textContent = '';
  el.style.display = 'none';
}

function normaliseHubUrl(url) {
  let u = (url || '').trim();
  if (!u) return '';
  // Add scheme if missing -- operators frequently type "paprika.lan".
  if (!/^https?:\/\//i.test(u)) u = 'http://' + u;
  return u.replace(/\/+$/, '');
}

function normaliseHost(host) {
  let h = (host || '').toLowerCase().trim();
  if (h.startsWith('www.')) h = h.slice(4);
  return h;
}

function activeTabHost() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      try {
        const u = new URL(tabs[0].url);
        resolve(normaliseHost(u.hostname));
      } catch (_) {
        resolve('');
      }
    });
  });
}

// Map chrome.cookies sameSite -> CDP-shape (capitalised; "None" / "Lax" / "Strict").
// "unspecified" is dropped (lets the worker fall back to Chrome's default).
function mapSameSite(s) {
  if (!s || s === 'unspecified' || s === 'no_restriction') {
    return s === 'no_restriction' ? 'None' : undefined;
  }
  if (s === 'lax') return 'Lax';
  if (s === 'strict') return 'Strict';
  return undefined;
}

// chrome.cookies.Cookie -> paprika /hosts cookie shape.
function toPaprikaCookie(c) {
  const out = {
    name: c.name,
    value: c.value,
    domain: c.domain,            // may start with '.' for domain-cookies
    path: c.path || '/',
    secure: !!c.secure,
    httpOnly: !!c.httpOnly,
  };
  if (c.expirationDate) {
    // Chrome returns expirationDate as seconds since epoch (float).
    // paprika /hosts/{host} accepts either ``expires`` (epoch float)
    // or ``expirationDate``; use the more obvious name.
    out.expires = c.expirationDate;
  }
  const ss = mapSameSite(c.sameSite);
  if (ss) out.sameSite = ss;
  return out;
}

function groupByHost(cookies, includeSession) {
  const byHost = new Map();
  for (const c of cookies) {
    if (!includeSession && !c.expirationDate) continue;
    const host = normaliseHost(c.domain.replace(/^\./, ''));
    if (!host) continue;
    if (!byHost.has(host)) byHost.set(host, []);
    byHost.get(host).push(toPaprikaCookie(c));
  }
  return byHost;
}

// ----- network --------------------------------------------------------

async function fetchHost(hubUrl, host) {
  // GET the existing record so we can preserve note / popup_policy.
  // If 404, that's fine -- we send a fresh PUT.
  try {
    const r = await fetch(`${hubUrl}/hosts/${encodeURIComponent(host)}`);
    if (r.ok) return await r.json();
  } catch (_) { /* ignore */ }
  return null;
}

async function putHost(hubUrl, host, cookies, existing) {
  // PUT /hosts/{host}: paprika replaces the cookie list with ours,
  // but we preserve note / popup_policy / recrawl_patterns when
  // present so re-pushing doesn't clobber operator-set metadata.
  const body = {
    cookies: cookies,
  };
  if (existing) {
    if (existing.notes) body.notes = existing.notes;
    if (existing.popup_policy) body.popup_policy = existing.popup_policy;
    if (existing.recrawl_patterns && existing.recrawl_patterns.length) {
      body.recrawl_patterns = existing.recrawl_patterns;
    }
  }
  const r = await fetch(`${hubUrl}/hosts/${encodeURIComponent(host)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`PUT /hosts/${host}: HTTP ${r.status}: ${t.slice(0, 120)}`);
  }
  return await r.json();
}

// ----- main button ----------------------------------------------------

async function pushCookies() {
  clearStatus();
  const btn = document.getElementById('pushBtn');
  btn.disabled = true;
  try {
    const hubInput = document.getElementById('hub').value;
    const hubUrl = normaliseHubUrl(hubInput);
    if (!hubUrl) {
      setStatus('Hub URL を入力してください', 'err');
      return;
    }
    // Persist the URL for next time.
    chrome.storage.local.set({ [STORAGE_KEY_HUB]: hubUrl });

    const scope = document.getElementById('scope').value;
    const includeSession = document.getElementById('includeSessionCookies').checked;

    setStatus('cookies 読み込み中 ...', 'info');
    // Pull every cookie from every cookie store. The user might have
    // an "Incognito" or "Person 2" store with different cookies; we
    // merge them all and let the host-grouping step dedup.
    const allCookies = await new Promise((resolve, reject) => {
      chrome.cookies.getAll({}, (cs) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else resolve(cs || []);
      });
    });

    let byHost = groupByHost(allCookies, includeSession);

    if (scope === 'active-host') {
      const active = await activeTabHost();
      if (!active) {
        setStatus('active tab のホスト名が取れませんでした (chrome://* タブ?)', 'err');
        return;
      }
      // Keep ONLY the active host's cookies. Domain-cookies whose
      // domain is a parent of active still apply, but we leave them
      // off the simple model -- operator wanted "this site only".
      const onlyActive = new Map();
      if (byHost.has(active)) onlyActive.set(active, byHost.get(active));
      byHost = onlyActive;
    }

    const hosts = [...byHost.keys()];
    if (hosts.length === 0) {
      setStatus('対象 cookie が無いか、フィルタで全部除外されました', 'err');
      return;
    }

    let okCount = 0;
    let failCount = 0;
    const errs = [];
    for (const host of hosts) {
      setStatus(`pushing ${okCount + failCount + 1}/${hosts.length}: ${host}`, 'info');
      try {
        const existing = await fetchHost(hubUrl, host);
        await putHost(hubUrl, host, byHost.get(host), existing);
        okCount++;
      } catch (e) {
        failCount++;
        errs.push(`${host}: ${e.message}`);
      }
    }

    if (failCount === 0) {
      setStatus(
        `✓ ${okCount} host(s) pushed to ${hubUrl}\n`
        + hosts.map(h => `  · ${h} (${byHost.get(h).length} cookies)`).join('\n'),
        'ok',
      );
    } else {
      setStatus(
        `partial: ${okCount} ok, ${failCount} failed\n`
        + errs.slice(0, 5).join('\n'),
        'err',
      );
    }
  } catch (e) {
    setStatus('error: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ----- init -----------------------------------------------------------

(async function init() {
  // Load saved hub URL.
  chrome.storage.local.get([STORAGE_KEY_HUB], (st) => {
    const v = st[STORAGE_KEY_HUB];
    if (v) document.getElementById('hub').value = v;
  });
  // Show the active tab's host so the user knows what "active-host"
  // scope will target.
  const h = await activeTabHost();
  document.getElementById('activeHost').textContent = h || '(none)';
  // Wire up the button.
  document.getElementById('pushBtn').addEventListener('click', pushCookies);
})();
