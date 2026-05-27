# Paprika Bridge

Chrome extension that connects the operator's browser to a Paprika
Hub. Started as a single-purpose "cookie pusher" in 0.1; from 0.2
onward it grows into a general bridge -- cookies today, URL handoff
/ clipboard / job-state pulls in upcoming versions.

## What it does today (0.2)

Reads cookies via `chrome.cookies.getAll({})` and PUTs them per host
to the configured Paprika Hub's `/hosts/{host}` registry. Jobs
reference the resulting login state via
`options.cookies_from="example.com"`.

## What it does, exactly

* Reads cookies via `chrome.cookies.getAll({})` from every cookie
  store the browser exposes (default + Incognito + per-profile if you
  use multiple).
* Groups them by host (www-stripped, lowercased — matches the paprika
  HostRegistry's canonical form).
* For the selected scope:
  * **active-host** (default): keep only the active tab's host. Push
    one record.
  * **all**: push every host the browser has cookies for.
* PUTs each host's cookies to `<hub>/hosts/{host}`. Existing notes /
  popup_policy / recrawl_patterns on the hub side are preserved
  (only the cookie list is replaced).

## What it does NOT cover

* `Login Data` (saved passwords) — Chrome's password manager API is
  not exposed to extensions.
* `Local Storage` / `Session Storage` / `IndexedDB` — accessible only
  via content scripts in pages the user actively visits, and only
  for one origin at a time. Not worth the complexity for V1.
* `Preferences` / `Local State` (extension config, autofill, etc.) —
  ditto.

Cookies cover login state for >90% of sites. If you genuinely need a
full Chrome profile (autofill, saved logins, per-origin storage), use
`paprika-client upload-profile` from the CLI instead — it tarballs
the on-disk profile directly.

## Install

Two ways:

1. **From the hub's install page**:
   - Open `http://<your-hub>/profiles/extension/install` in Chrome
   - Download the .zip, extract
   - chrome://extensions → enable "Developer mode" → "Load unpacked"
     → pick the extracted folder

2. **Direct from the git source tree**:
   - chrome://extensions → "Load unpacked"
     → pick `server/web/extensions/paprika-bridge/`

The extension lives entirely client-side. Re-loading (after a `git
pull` etc.) is "click the refresh icon next to the extension in
chrome://extensions".

## Use

1. Click the toolbar icon (or pin it from the puzzle-piece menu first).
2. First time only: enter the Hub URL (e.g. `http://paprika.lan`).
   Saved across popup invocations.
3. Pick scope (active host vs all).
4. Click **Push cookies to hub**.
5. Status line shows ✓ / per-host failures.

Re-run any time you log into a new site. Last write wins on the hub
side, so repeat pushes are safe.

## Permissions explained

| Permission | Why |
|------------|-----|
| `cookies` | Read cookies via `chrome.cookies.getAll()` |
| `storage` | Remember the Hub URL across popup invocations |
| `activeTab` + `tabs` | Read the current tab's URL to derive the default host |
| `<all_urls>` | Required by `chrome.cookies` API; the extension never opens pages or injects scripts |

The extension does not run in the background — it only does anything
while the popup is open and you click Push.

## Build / package for distribution

The hub serves a fresh .zip from `GET /profiles/extension/paprika-bridge.zip`,
built on demand from this directory. Nothing to "build" — the source
files are loaded directly.
