"""System / probe routes: /health, /info, /icon.svg, /favicon.ico.

The smallest endpoints the hub exposes -- monitoring probes plus the
SVG logo every HTML surface references. Kept separate from the admin
UI shell route (``/`` -> /static/admin.js) which stays in app.py for
now because of the inline _ADMIN_HTML template (planned for extraction
to a Jinja2/StaticFiles template in a later round).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from server.hub._state import config, state


router = APIRouter(tags=["System"])


# Inline SVG for the paprika logo. Served from /icon.svg so every HTML
# surface (admin dashboard, /screenshots, /jobs/*/log, per-job
# galleries) references one URL instead of duplicating markup. Also
# used as the favicon via ``<link rel="icon" type="image/svg+xml">``.
_PAPRIKA_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1254 1254">
  <defs><style>
    .st0 { fill: #5c9138; }
    .st1 { fill: #f5b800; }
  </style></defs>
  <path class="st1" d="M486.2,486.1c68.1-6.4,95,45.1,159.4,45,56.2-.1,81.5-41.9,134.4-45.5,117.4-7.9,145.8,135.5,143.3,225.8-3.5,123.4-50,342.8-129.3,439.3-80,97.3-221.9,89.2-299.5-6.7-76.3-94.2-125.2-307.3-129.3-427.4-2.7-80.1,20.5-221.3,121-230.7Z"/>
  <path class="st1" d="M843.7,1155.2c-4.7-3.4,5.9-14.6,8.2-18.5,42.9-70.2,69.3-160.6,86.7-240.9,25.8-119.3,55.3-321.8-45.9-413.5-3.8-3.5-24.7-17.4-25.2-18.9-1.2-3.6,8.3-17,10.1-22.8,4-12.6,4.2-27.3.8-40-4.9-18.2-16.7-16.6,1.6-32.1,42.3-36,108-49.4,161.5-36.4,190.6,46.2,117.1,355,76.5,486.2-33.9,109.3-113.6,302.8-236.1,333.9-7.1,1.8-33.1,6.8-38.3,3.1Z"/>
  <path class="st1" d="M278.5,327.6c42.1-3.4,106.4,10.8,136.7,42.2,7.1,7.3-2.3,13.5-5.8,20.8-8.8,18.5-9.4,41.1,0,59.5,2.2,4.3,10.2,11.8,8.9,15.9s-21.2,17.4-25.2,21.5c-97.7,101.7-69.3,293.7-40.8,419.1,17.4,76.7,43.3,157.4,84.3,224.7,4.5,7.3,20.5,22.5,6.2,25.1-33.3,6.1-90.2-29.8-114.2-51.6-112-101.4-181.3-341.5-198.9-488.2-13.7-114.3,3.3-277.2,148.7-289.1Z"/>
  <path class="st0" d="M479.5,372.1c3.2-3.3,0-12.9.7-18.6,4.1-30.7,46.3-42.4,72.1-35.5,6.7,1.8,12.9,7.4,18,8.7s2.1,1.1,3.4-.8c2.5-3.7,3.9-25.9,5.1-32.2,17.6-95.9,76.8-212.3,183.1-231.1,48.4-8.5,88.8,31.3,63.1,76.4-16.6,29.3-44.8,21.9-69.9,38-36.3,23.2-61,93.4-58.2,135,1.6,24.5,6.3,13.8,21.4,8,25.3-9.8,66.2-3.8,75.5,25.6,2.3,7.2.4,19.8,2.9,23.7s12.6,5.9,17,8.3c23.6,12.9,36.2,46.1,13.8,66.2s-16.7,3.6-28.6,3.2c-23-.8-46.6.6-68.2,9.1-29.3,11.6-44.9,33.1-79.6,34.9-46.4,2.5-66.7-27-107-38.3-18.5-5.2-39-6.7-58.2-5.7s-18.3,6.6-30.6-2.7c-23.6-17.7-12.2-51.2,9.6-65,3.3-2.1,13.1-5.5,14.7-7.2Z"/>
  <path class="st1" d="M1053.8,307.2c-68.5-18.8-139.1-4.2-195,38.7-2.8,2.2-8.3,9.8-11.8,9.1s-12.5-9-12.9-9.7c-1.8-2.9-2.6-11.8-4.6-16.7-12.3-30.3-46.8-45.3-77.4-49.1-3.7-.5-15.5,1.2-16.3-2.3-.7-2.9,8.7-28.5,10.7-33,3.3-7.5,15.7-30.3,22-33.9s22.5-6.6,29.5-7.8c81.6-13.7,197.1,20,247.9,88.2,3.6,4.9,8.6,10,8,16.6Z"/>
  <path class="st1" d="M546.3,280.4c-37.6-2.5-83.2,15.3-96.8,53s-1.1,19.2-9,15.1-16.8-13-24.7-17.9c-49.2-30.3-107.4-38.3-163.7-24.8-.9-5.8,3.6-9.7,6.7-13.9,24.1-33.1,68.1-59.9,106.1-73.6,60.9-22,136.2-28.7,196.1-1l-14.8,63.1Z"/>
</svg>
"""


def _hub_version() -> str:
    """Lazy lookup back into app.py for the running version string.

    Lives there because the value is derived from /app/VERSION + a
    cached read; centralising the read on app.py keeps the disk
    access in one place. Imported lazily so this module can be
    imported before app.py has finished loading.
    """
    from server.hub.app import _hub_version as _v
    return _v()


@router.get("/icon.svg")
async def paprika_icon():
    """Serve the paprika logo SVG. Referenced by ``<link rel="icon">``
    + ``<img class="logo">`` in every HTML surface. Cached for a day
    so the browser stops re-fetching."""
    return Response(
        content=_PAPRIKA_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico")
async def favicon_redirect():
    """Browsers default to /favicon.ico. Redirect to the SVG so we
    only maintain one logo file. Modern browsers accept SVG favicons
    via the /icon.svg ``<link rel="icon" type="image/svg+xml">`` too;
    this catches older / inflexible clients."""
    return RedirectResponse(url="/icon.svg", status_code=302)


@router.get("/info", response_class=PlainTextResponse)
async def info_text() -> str:
    """Plain-text equivalent of the old /, kept for terminal users."""
    nstats = state.registry.stats() if state.registry else {"count": 0}
    novnc_lines = ""
    if state.registry:
        for w in state.registry.connections.values():
            nv = w.capabilities.novnc_url
            if nv:
                sep = "&" if "?" in nv else "?"
                full = (f"{nv}{sep}autoconnect=1&resize=scale&reconnect=1"
                        if "autoconnect" not in nv else nv)
                novnc_lines += f"    {w.worker_id:<24} {full}\n"
    return (
        "paprika hub\n"
        f"  data dir   : {config.data_dir.resolve()}\n"
        f"  store      : {state.store_kind}\n"
        f"  workers    : {nstats['count']} connected\n"
        f"  max local  : {config.max_concurrent_jobs}\n"
        "\n"
        "  Client API:\n"
        "    POST /jobs                       submit\n"
        "    GET  /jobs                       list\n"
        "    GET  /jobs/{id}                  status (+worker_id, +novnc_url)\n"
        "    GET  /jobs/{id}/result           final result\n"
        "    GET  /jobs/{id}/page.html / /log.txt / /assets/{f}\n"
        "    WS   /jobs/{id}/events           live log stream\n"
        "    DELETE /jobs/{id}                remove\n"
        "    GET  /https://...                URL pass-through (same as POST /jobs)\n"
        "\n"
        "  Worker API:\n"
        "    WS   /workers/{worker_id}/link\n"
        "    POST /jobs/{id}/assets\n"
        "    POST /jobs/{id}/files/{kind}\n"
        "    GET  /workers\n"
        + (f"\n  noVNC viewers:\n{novnc_lines}" if novnc_lines else "")
    )


@router.get("/health")
async def health() -> dict:
    """The probe every operational sidecar reads. ``version`` surfaces
    the source hash so external monitoring can spot fleet drift --
    compare to each worker's reported version via /workers to see
    which ones haven't auto-updated yet."""
    nstats = state.registry.stats() if state.registry else {"count": 0}
    return {
        "status": "ok",
        "store": state.store_kind,
        "workers": nstats["count"],
        "version": _hub_version(),
    }



# ============================================================================
# Admin UI shell + screenshots page (#2B-G3-partial)
# ============================================================================

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>Paprika</title>
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<!-- Iconify Web Component. Renders icons from the Iconify CDN
     (Lucide set: https://icones.js.org/collection/lucide). Each
     icon is ~1KB, cached after first fetch. Use <iconify-icon
     icon="lucide:name" inline></iconify-icon> in HTML. -->
<script src="https://code.iconify.design/iconify-icon/2.1.0/iconify-icon.min.js"></script>
<!-- i18next: client-side i18n. The library loads from a CDN once and
     caches in the browser. The actual translation resources are
     inlined into the HTML below as a regular JS object, so no extra
     JSON fetches at startup. -->
<script src="https://unpkg.com/i18next@23/i18next.min.js"></script>
<link rel="stylesheet" href="/static/admin.css?v=@@PAPRIKA_VERSION@@">
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:8px;" title="ホーム (Submit form) に戻る"><img src="/icon.svg" alt="paprika" class="logo"> Paprika</a></h1>
  <span class="status" id="status">…</span>
  <a class="manual-link" href="https://paps-jp.github.io/paprika/" target="_blank" rel="noopener"
     title="Open the Paprika manual on GitHub Pages (new tab)" data-i18n="header.manual">
    <iconify-icon icon="lucide:book-open"></iconify-icon>
    <span data-i18n="header.manual">Manual</span>
  </a>
  <select id="localeSwitch" class="locale-switch" aria-label="Language">
    <option value="ja">日本語</option>
    <option value="en">English</option>
  </select>
</header>
<main>

<nav class="tabs" id="tabs">
  <button class="tab" data-tab="submit"><iconify-icon icon="lucide:send-horizontal"></iconify-icon> <span data-i18n="tab.submit">Submit</span></button>
  <button class="tab" data-tab="workers"><iconify-icon icon="lucide:cpu"></iconify-icon> <span data-i18n="tab.workers">Workers</span> <span class="count" id="cntWorkers">0</span></button>
  <button class="tab" data-tab="jobs"><iconify-icon icon="lucide:list-checks"></iconify-icon> <span data-i18n="tab.jobs">Recent jobs</span> <span class="count" id="cntJobs">0</span></button>
  <button class="tab" data-tab="presets"><iconify-icon icon="lucide:bookmark"></iconify-icon> <span data-i18n="tab.presets">Preset job</span> <span class="count" id="cntPresets">0</span></button>
  <button class="tab" data-tab="screens"><iconify-icon icon="lucide:monitor"></iconify-icon> <span data-i18n="tab.screens">Live preview</span></button>
  <div class="tab-dropdown" id="moreTabWrap">
    <button type="button" class="tab tab-dropdown-trigger" id="moreTabBtn"
            aria-haspopup="true" aria-expanded="false">
      <iconify-icon icon="lucide:more-horizontal"></iconify-icon> <span data-i18n="tab.more">More</span> <iconify-icon icon="lucide:chevron-down" class="caret-down"></iconify-icon>
    </button>
    <div class="tab-dropdown-menu" id="moreTabMenu" role="menu">
      <button class="tab" data-tab="sessions" role="menuitem"><iconify-icon icon="lucide:plug-2"></iconify-icon> <span data-i18n="tab.sessions">Sessions</span> <span class="count" id="cntSessions">0</span></button>
      <button class="tab" data-tab="hosts" role="menuitem"><iconify-icon icon="lucide:cookie"></iconify-icon> <span data-i18n="tab.hosts">Hosts</span> <span class="count" id="cntHosts">0</span></button>
      <button class="tab" data-tab="profiles" role="menuitem"><iconify-icon icon="lucide:user-cog"></iconify-icon> <span data-i18n="tab.profiles">Profiles</span> <span class="count" id="cntProfiles">0</span></button>
      <button class="tab" data-tab="extensions" role="menuitem"><iconify-icon icon="lucide:puzzle"></iconify-icon> <span data-i18n="tab.extensions">Extensions</span> <span class="count" id="cntExtensions">0</span></button>
      <button class="tab" data-tab="engines" role="menuitem"><iconify-icon icon="lucide:wand-sparkles"></iconify-icon> <span data-i18n="tab.engines">AI Engines</span> <span class="count" id="cntEngines">0</span></button>
      <button class="tab" data-tab="knowledge" role="menuitem"><iconify-icon icon="lucide:brain"></iconify-icon> <span data-i18n="tab.knowledge">Knowledge</span> <span class="count" id="cntKnowledge">0</span></button>
      <button class="tab" data-tab="plugins" role="menuitem"><iconify-icon icon="lucide:plug"></iconify-icon> <span data-i18n="tab.plugins">Plugins</span> <span class="count" id="cntPlugins">0</span></button>
      <button class="tab" data-tab="settings" role="menuitem"><iconify-icon icon="lucide:settings"></iconify-icon> <span data-i18n="tab.settings">Settings</span></button>
      <!-- v2: Recipes / Skills / Conventions tabs removed.
           Their data lives in HostKnowledge (per_page.content_extraction /
           per_page.barriers / per_page.navigation_hints) which the R1
           Distiller maintains automatically. See internal/v2-architecture.html. -->
    </div>
  </div>
</nav>

<div class="panel" data-panel="submit">
  <section>
    <h2 data-i18n="submit.heading">Submit a job</h2>
    <form id="submit">
      <!-- Named-preset bar: dropdown was removed because operators
           can have 500+ presets which is unwieldy in a <select>.
           Picking + running is done from the "Preset job" tab.
           "💾 save as" stays here as a quick affordance for
           snapshotting the live form. "💾 overwrite" only shows
           after a preset has been loaded into the form (load is
           triggered from the Preset job tab). -->
      <div id="presetBar" style="display:flex; gap:8px; align-items:center; margin-bottom:8px; padding:6px 10px; background:#f5f7ff; border:1px solid #d6dcf0; border-radius:6px; font-size:.9em;">
        <iconify-icon icon="lucide:bookmark" style="color:#3a5ca8;"></iconify-icon>
        <span style="font-weight:600; color:#3a5ca8;" data-i18n="submit.preset">Preset:</span>
        <span id="presetLoadedName" style="color:#888;" data-i18n="submit.preset.none">(none loaded — pick one from the Preset job tab)</span>
        <span style="margin-left:auto; display:flex; gap:6px;">
          <button type="button" id="presetSaveAsBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c; padding:2px 10px;" title="現在のフォーム状態を新しい preset として保存"><iconify-icon icon="lucide:save"></iconify-icon> <span data-i18n="submit.saveas">save as</span></button>
          <button type="button" id="presetOverwriteBtn" class="pill" style="background:#eef0ff; border-color:#6a8ec7; color:#3a5ca8; padding:2px 10px; display:none;" title="読み込み中の preset を現在のフォーム状態で上書き保存"><iconify-icon icon="lucide:save"></iconify-icon> <span data-i18n="submit.overwrite">overwrite</span></button>
        </span>
      </div>
      <input type="text" id="urlInput" placeholder="https://example.com">
      <!-- URL の host を解析して、その host の Cookie / Visited URL を
           Submit する前に直接編集できるショートカット行。
           URL が空 or 不正のときは display:none。 -->
      <div id="urlHostInfo" style="display:none; margin-top:6px; padding:6px 10px; background:#f7f7fc; border-radius:6px; font-size:.88em; color:#555; align-items:center; gap:8px; flex-wrap:wrap;">
        <span><iconify-icon icon="lucide:globe" style="opacity:.7;"></iconify-icon> host: <code id="urlHostName">—</code></span>
        <button type="button" id="urlHostEditBtn" class="pill" style="background:#eef8ff; border-color:#9bf; padding:2px 8px; font-size:.85em;" title="この host の Cookie / Notes を編集">
          <iconify-icon icon="lucide:pencil"></iconify-icon> <span data-i18n="submit.edithost">Edit host</span> <span id="urlHostEditCount" style="opacity:.8;"></span>
        </button>
        <button type="button" id="urlHostDedupBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c; padding:2px 8px; font-size:.85em;" title="この host の Visited URL / Recrawl patterns を編集">
          <iconify-icon icon="lucide:filter"></iconify-icon> <span data-i18n="submit.dedup">Dedup</span> <span id="urlHostDedupCount" style="opacity:.8;"></span>
        </button>
        <span id="urlHostStatus" style="color:#888; font-size:.82em; margin-left:auto;"></span>
      </div>

      <!-- Mode picker: card-style. Each card is a clickable <label> that
           wraps a hidden radio so we get standard form semantics + custom
           visuals. ``syncSubmitMode()`` toggles .selected and reveals the
           matching options panel.
           Note: the AI choice is split into two visually distinct cards
           (LLM コード生成 / Macro) even though they share the underlying
           ui_mode="ai" + aiEngine radio for storage continuity. The
           "LLM" card flips both mode=ai AND aiEngine=codegen on click;
           the "Macro" card flips mode=ai AND aiEngine=simple. -->
      <div style="display:flex; align-items:baseline; gap:8px; margin-top:8px; margin-bottom:4px;">
        <strong style="color:#444;" data-i18n="submit.mode.label">実行モード</strong>
        <span style="color:#888; font-size:.85em;" data-i18n="submit.mode.hint">(クリックで切替。これがそのまま「実行 / 保存」する種類になります)</span>
        <span style="margin-left:auto; color:#c0392b; font-size:.85em; font-weight:600;">
          <span data-i18n="submit.mode.current">選択中:</span> <span id="modeCardsCurrentLabel">Fetch</span>
        </span>
      </div>
      <div class="mode-cards" id="modeCards">
        <label class="mode-card" data-mode="fetch">
          <input type="radio" name="mode" value="fetch" checked hidden>
          <div class="mode-icon"><iconify-icon icon="lucide:file-down" style="font-size:1.7em;"></iconify-icon></div>
          <div class="mode-title" data-i18n="submit.mode.fetch">Fetch</div>
          <div class="mode-desc" data-i18n="submit.mode.fetch.desc">単発ページ取得 (スクロール)。LLM 不使用、最速。</div>
        </label>
        <label class="mode-card" data-mode="script">
          <div class="mode-icon"><iconify-icon icon="lucide:terminal" style="font-size:1.7em;"></iconify-icon></div>
          <div class="mode-title" data-i18n="submit.mode.script">Script</div>
          <div class="mode-desc" data-i18n="submit.mode.script.desc">スクリプトを手で組んで実行 (Code 直書き / Macro UI ビルダー)。LLM 不使用。</div>
        </label>
        <label class="mode-card" data-mode="ai" data-ai-engine="codegen">
          <div class="mode-icon"><iconify-icon icon="lucide:sparkles" style="font-size:1.7em;"></iconify-icon></div>
          <div class="mode-title" data-i18n="submit.mode.ai">AI</div>
          <div class="mode-desc" data-i18n="submit.mode.ai.desc">Goal を LLM に渡してスクリプト自動生成 → sandbox 実行 → 失敗時 retry。</div>
        </label>
      </div>
      <!-- Script sub-tabs (Code / Macro). Visible only when the
           Script card is active. Each sub-tab updates the hidden
           radios so the existing submit / preset code keeps working
           with the same {mode, aiEngine} dispatch values:
             Script>Code  -> mode=code
             Script>Macro -> mode=ai, aiEngine=simple
      -->
      <div id="scriptSubTabs" class="script-subtabs" style="display:none;">
        <button type="button" class="script-tab selected" data-script-kind="code" title="Python を直接貼り付けて 1 回だけ実行"><iconify-icon icon="lucide:code-2"></iconify-icon> <span data-i18n="submit.script.code">Code</span></button>
        <button type="button" class="script-tab" data-script-kind="macro" title="UI で順序立ててアクションを並べる簡易マクロ"><iconify-icon icon="lucide:list-ordered"></iconify-icon> <span data-i18n="submit.script.macro">Macro</span></button>
      </div>
      <!-- The "ai" / "code" radios live outside the cards because the
           Script card maps to TWO backend dispatch values depending on
           the active sub-tab (Code -> mode=code, Macro -> mode=ai +
           aiEngine=simple). Keeping hidden radios means existing form
           code (PresetBuilder, Submit handler) reads {mode, aiEngine}
           unchanged. -->
      <input type="radio" name="mode" value="ai" hidden id="modeRadioAi">
      <input type="radio" name="mode" value="code" hidden id="modeRadioCode">
      <input type="radio" name="aiEngine" value="codegen" checked hidden id="aiEngineRadioCodegen">
      <input type="radio" name="aiEngine" value="simple" hidden id="aiEngineRadioSimple">

      <!-- Fetch options: knobs that map 1:1 to JobOptions (server/protocol.py).
           Collapsed by default so the simple "URL + Submit" path stays clean.
           Visible only when Fetch mode is selected (syncSubmitMode toggles it).
           Defaults here match what the UI used to hard-code (scroll/play_videos
           = true), not the bare JobOptions defaults. -->
      <div id="fetchOptions" class="fetch-options">
        <!-- Fetch sub-mode (Phase 2a). 3-way radio sits ABOVE the
             collapsible options so it's always visible -- this is a
             primary execution choice, not a tuning knob.
              * 通常    : ignore any registered recipe (skip HostRegistry.pick_recipe)
              * 登録    : default, apply matched recipe (Phase 1 behavior)
              * AI調査  : submit as codegen-loop (paid LLM) using the inline goal -->
        <div id="fetchSubMode" class="fetch-submode">
          <span class="fo-submode-title">実行モード:</span>
          <label title="HostRegistry の recipe を無視して素の Fetch を実行">
            <input type="radio" name="fetchSubMode" value="normal" id="fetchSubModeNormal"> 通常
          </label>
          <label title="HostRegistry の recipe があれば適用 (Phase 1 既定)">
            <input type="radio" name="fetchSubMode" value="recipe" id="fetchSubModeRecipe" checked> 登録
          </label>
          <label title="課金 LLM (codegen-loop) で攻略手順を生成">
            <input type="radio" name="fetchSubMode" value="ai_investigate" id="fetchSubModeAi"> AI調査
          </label>
          <span id="fetchSubModeBadge" style="color:#888; font-size:.85em; margin-left:auto;"></span>
        </div>
        <!-- AI調査 only: inline goal textarea + sensible defaults. -->
        <div id="fetchInvestigateArea" class="fetch-investigate-area" style="display:none;">
          <label style="display:block; font-weight:600; margin-bottom:4px;">
            <iconify-icon icon="lucide:bot"></iconify-icon> AI調査の目標
          </label>
          <textarea id="fetchInvestigateGoal"
            style="width:100%; min-height:64px; box-sizing:border-box;"
            placeholder="例: このページのメイン動画 URL を pap.assets.add() で保存する"></textarea>
          <div style="display:flex; gap:14px; align-items:center; margin-top:6px; font-size:.9em; flex-wrap:wrap;">
            <label>最大試行回数 <input type="number" id="fetchInvestigateMaxAttempts" value="3" min="1" max="10" style="width:50px"></label>
            <label>1試行タイムアウト <input type="number" id="fetchInvestigateTimeoutSec" value="600" min="60" max="86400" style="width:80px"> 秒</label>
            <span style="color:#888;">※ 成功時は次タブで「recipe として保存」できます (Phase 2c)</span>
          </div>
        </div>
        <div id="fetchOptionsDetails">
          <div class="fo-header">
            <iconify-icon icon="lucide:sliders-horizontal"></iconify-icon>
            <span data-i18n="fetch.heading">Fetch オプション</span>
          </div>

          <!-- 動画ダウンロード: 通信トレース + yt-dlp の有効化フラグ。
               AI 調査モード(codegen-loop)時は admin.js 側で強制 True。 -->
          <div class="fetch-section video">
            <div class="fs-title">
              <iconify-icon icon="lucide:video"></iconify-icon> 動画
            </div>
            <label class="fetch-toggle-big" data-i18n-title="fetch.downloadvideo.title" title="iframe / ネスト iframe の通信トレースを ON にし、yt-dlp で動画をダウンロードする。OFF の場合は動画 DL ロジックを全休眠。">
              <input type="checkbox" id="fetchDownloadVideo">
              <span data-i18n="fetch.downloadvideo"><iconify-icon icon="lucide:download"></iconify-icon> 動画をダウンロード</span>
            </label>
          </div>

          <!-- 動作: スクロール / ヘッドレス / アセット保存 / セッション継続 -->
          <div class="fetch-section">
            <div class="fs-title">
              <iconify-icon icon="lucide:settings-2"></iconify-icon> 動作
            </div>
            <div class="fetch-toggles">
              <label data-i18n-title="fetch.scroll.title" title="ページを最後までスクロールして遅延読み込み (lazy) のアセットを拾う。">
                <input type="checkbox" id="fetchScroll" checked> <span data-i18n="fetch.scroll">スクロール</span>
              </label>
              <label data-i18n-title="fetch.headless.title" title="画面を出さずに実行 (Chrome --headless)。">
                <input type="checkbox" id="fetchHeadless"> <span data-i18n="fetch.headless">ヘッドレス</span>
              </label>
              <label data-i18n-title="fetch.capture.title" title="拾ったアセットをサーバ側に保存する。">
                <input type="checkbox" id="fetchCaptureAssets" checked> <span data-i18n="fetch.capture">アセットを保存</span>
              </label>
              <label data-i18n-title="fetch.keepsession.title" title="クロール後もセッションを閉じずに残す。">
                <input type="checkbox" id="fetchKeepSession"> <span data-i18n="fetch.keepsession">セッションを継続</span>
              </label>
            </div>
          </div>

          <!-- タイミング / 制限: 6 つの数値 knob を grid で整列 -->
          <div class="fetch-section">
            <div class="fs-title">
              <iconify-icon icon="lucide:timer"></iconify-icon> タイミング / 制限
            </div>
            <div class="fetch-grid">
              <div class="fetch-row">
                <label for="fetchWaitSec" data-i18n-title="fetch.wait.title" title="ページ読み込みを待つ秒数。"><span data-i18n="fetch.wait">ページ読み込み待ち</span></label>
                <input type="number" id="fetchWaitSec" value="20" min="0" max="3600">
                <span class="fg-unit" data-i18n="fetch.unit.sec">秒</span>
              </div>
              <div class="fetch-row">
                <label for="fetchIdleSec" data-i18n-title="fetch.idle.title" title="ネットワーク無通信の判定秒数。"><span data-i18n="fetch.idle">ネットワーク無通信</span></label>
                <input type="number" id="fetchIdleSec" value="3" min="0" max="60" step="0.5">
                <span class="fg-unit" data-i18n="fetch.unit.sec">秒</span>
              </div>
              <div class="fetch-row">
                <label for="fetchMaxWaitSec" data-i18n-title="fetch.maxwait.title" title="ページに費やす最大秒数。"><span data-i18n="fetch.maxwait">最大待ち時間</span></label>
                <input type="number" id="fetchMaxWaitSec" value="60" min="1" max="3600">
                <span class="fg-unit" data-i18n="fetch.unit.sec">秒</span>
              </div>
              <div class="fetch-row">
                <label for="fetchScrollMax" data-i18n-title="fetch.scrollmax.title" title="スクロール上限ピクセル数。"><span data-i18n="fetch.scrollmax">スクロール上限</span></label>
                <input type="number" id="fetchScrollMax" value="3000" min="0" max="100000">
                <span class="fg-unit">px</span>
              </div>
              <div class="fetch-row">
                <label for="fetchPostClickSec" data-i18n-title="fetch.postclick.title" title="クリック後に追加で待つ秒数。"><span data-i18n="fetch.postclick">クリック後の待ち</span></label>
                <input type="number" id="fetchPostClickSec" value="5" min="0" max="60" step="0.5">
                <span class="fg-unit" data-i18n="fetch.unit.sec">秒</span>
              </div>
              <div class="fetch-row">
                <label for="fetchMinAssetBytes" data-i18n-title="fetch.minsize.title" title="最小アセットサイズ (byte)。"><span data-i18n="fetch.minsize">最小ファイルサイズ</span></label>
                <input type="text" id="fetchMinAssetBytes" value="0" inputmode="numeric" placeholder="0 / 1k / 10kb">
                <span class="fg-unit">bytes</span>
              </div>
            </div>
          </div>

          <!-- ヘッダー / セッション再利用: リファラー + ジョブ接続 -->
          <div class="fetch-section">
            <div class="fs-title">
              <iconify-icon icon="lucide:globe"></iconify-icon> ヘッダー / セッション再利用
            </div>
            <div class="fetch-grid-wide">
              <label for="fetchReferer" data-i18n-title="fetch.referer.title" title="Referer ヘッダ。"><span data-i18n="fetch.referer">リファラー</span></label>
              <input type="text" id="fetchReferer" placeholder="https://...">
              <label for="fetchAttachToJob" data-i18n-title="fetch.attach.title" title="既存 job を再利用してログイン状態を引き継ぐ。"><span data-i18n="fetch.attach">ジョブに接続</span></label>
              <input type="text" id="fetchAttachToJob" data-i18n-placeholder="fetch.attach.placeholder" placeholder="job_id (任意, ログイン継続)">
            </div>
          </div>
        </div>
      </div>

      <div id="aiOptions" style="display:none; padding:8px 12px; background:#f7f7fc; border-radius:6px; margin-bottom:10px;">
        <!-- AI engine sub-selector (codegen vs simple) used to live
             here as an inner fieldset. It now lives at the top level
             as the LLM / Macro mode cards, so we hide the inner UI to
             avoid showing the same choice twice -- but keep the
             actual radios in the DOM (id'd up top) for back-compat
             with code that reads `input[name="aiEngine"]:checked`. -->

        <!-- codegen-engine area: natural-language Goal + retry knobs -->
        <div id="aiGoalArea">
          <label for="goalInput" style="font-weight:600;">Goal</label>
          <textarea id="goalInput" rows="3" data-i18n-placeholder="ai.goal.placeholder" placeholder="(空 → デフォの「サイト全体をクロール…」が使われる)"
                    style="width:100%; box-sizing:border-box; margin-top:4px;"></textarea>
          <div style="margin-top:8px; display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
            <label id="aiCountLabel" title="script-generation retry の上限。失敗するごとに LLM にエラー文を渡して書き直してもらう。">
              <span id="aiCountLabelText" data-i18n="ai.maxattempts">最大試行回数</span>:
              <input type="number" id="maxAttempts" value="3" min="1" max="200" style="width:60px">
              <span style="color:#888; font-size:.82em;" data-i18n="ai.unit.times">回</span>
            </label>
            <label id="aiTimeoutLabel" data-i18n-title="ai.timeout.title" title="1 試行あたりの実行制限時間。">
              <span data-i18n="ai.timeout">1 試行のタイムアウト</span>:
              <input type="number" id="attemptTimeout" value="86400" min="30" max="864000" style="width:110px">
              <span style="color:#888; font-size:.82em;">秒</span>
            </label>
            <!-- Engine picker: lists every chat / vision-chat engine
                 from /engines that speaks the openai protocol (= can
                 act as a chat-completions backend for codegen, planner,
                 and judge). Empty selection means "use the hub's env
                 defaults" (CODEGEN_LLM_URL + CODEGEN_MODEL_NAME). The
                 label explicitly tags this as the *codegen-time* LLM
                 to distinguish it from the *runtime* vision agent
                 (CogAgent / Qwen-VL) used inside page.agent() --
                 operators repeatedly conflated the two until the
                 wording was made unambiguous. -->
            <label id="aiEngineLabel" title="ここで選ぶのは「スクリプトを書く LLM」(planner + coder + judge 用)。スクリプト実行中に page.agent() が呼ぶ Vision agent (CogAgent / Qwen-VL) は別管理 (worker の AGENT_URL / COGAGENT_URL 環境変数で固定)。">
              <span style="font-weight:600;" data-i18n="ai.engine">コード生成 LLM:</span>
              <select id="codegenEngineSelect" style="padding:3px 6px; font-size:.9em;">
                <option value="">(default — env)</option>
              </select>
              <span style="color:#888; font-size:.82em;" data-i18n="ai.engine.note">※ planner / coder / judge 用</span>
            </label>
            <label id="aiHostDedupLabel" data-i18n-title="ai.hostdedup.title" title="既訪問URLをスキップ (cron 等で日次再クロール時に有効)。">
              <input type="checkbox" id="llmHostDedup"> <span data-i18n="ai.hostdedup">既訪問URLをスキップ (host_dedup)</span>
            </label>
          </div>
          <!-- One-liner clarification so the operator doesn't worry
               that picking a chat-only engine here will also be used
               for clicking / scrolling on the actual page. -->
          <div style="margin-top:6px; padding:6px 10px; background:#fff8e6; border-left:3px solid #d4a13d; border-radius:4px; color:#7a5a00; font-size:.82em;" data-i18n="ai.engine.info">
            上の「コード生成 LLM」はスクリプトを書くためのモデル選択です。スクリプト実行中に page.agent() が使う Vision agent は worker 側で固定です。ここで変更しても挙動は変わりません。
          </div>
        </div>

        <!-- simple-engine area: macro builder. Each row = one action
             (dropdown) + one text input (parameter / description).
             Rows compile to a Python script that gets submitted as
             mode=rerun. -->
        <div id="aiMacroArea" style="display:none;">
          <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
            <label style="font-weight:600;">Macro</label>
            <span style="color:#888; font-size:.85em;" data-i18n="macro.hint">行を増やして順に並べる → 自動で paprika-client スクリプトに変換 → 実行</span>
            <span style="margin-left:auto; display:flex; gap:6px;">
              <button type="button" id="simplePreviewBtn" class="pill" style="background:#f5f5fa; border-color:#ccd; color:#555;" data-i18n-title="macro.preview.title" title="生成される Python スクリプトをプレビュー"><iconify-icon icon="lucide:eye"></iconify-icon> <span data-i18n="macro.preview">preview</span></button>
              <button type="button" id="simpleClearBtn" class="pill" style="background:#fee; border-color:#c88; color:#933;" data-i18n-title="macro.clear.title" title="macro をすべて削除"><iconify-icon icon="lucide:trash-2"></iconify-icon> <span data-i18n="macro.clear">clear</span></button>
            </span>
          </div>
          <div id="simpleRows" style="display:flex; flex-direction:column; gap:6px;"></div>
          <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
            <button type="button" id="simpleAddRowBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;" data-i18n-title="macro.addstep.title" title="アクション 1 行を末尾に追加"><iconify-icon icon="lucide:plus"></iconify-icon> <span data-i18n="macro.addstep">add step</span></button>
            <button type="button" id="simpleAddLoopBtn" class="pill" style="background:#eef0ff; border-color:#6a8ec7; color:#3a5ca8;" data-i18n-title="macro.addloop.title" title="Loop と End loop のペアを末尾に追加"><iconify-icon icon="lucide:repeat"></iconify-icon> <span data-i18n="macro.addloop">add loop</span></button>
            <button type="button" id="simpleAddIfCssBtn" class="pill" style="background:#fef5e7; border-color:#d4a13d; color:#8a5a00;" data-i18n-title="macro.addifcss.title" title="If (CSS) / End if を追加"><iconify-icon icon="lucide:braces"></iconify-icon> add if (CSS)</button>
            <button type="button" id="simpleAddIfAgentBtn" class="pill" style="background:#f5edff; border-color:#9b78c7; color:#5a3b8a;" data-i18n-title="macro.addifagent.title" title="If (Agent) / End if を追加"><iconify-icon icon="lucide:help-circle"></iconify-icon> add if (Agent)</button>
          </div>
          <pre id="simplePreviewPre" style="display:none; margin-top:10px; padding:10px; background:#fafafb; border:1px solid #e0e0e8; border-radius:5px; font-size:12px; line-height:1.45; white-space:pre; overflow:auto; max-height:300px;"></pre>
          <div style="margin-top:8px; display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
            <label id="aiSimpleTimeoutLabel" data-i18n-title="macro.timeout.title" title="macro 全体の実行時間上限。">
              <span data-i18n="macro.timeout">実行タイムアウト</span>:
              <input type="number" id="attemptTimeoutSimple" value="600" min="30" max="864000" style="width:110px">
              <span style="color:#888; font-size:.82em;">秒</span>
            </label>
          </div>
        </div>
      </div>

      <div id="codeOptions" style="display:none; padding:8px 12px; background:#f7f7fc; border-radius:6px; margin-bottom:10px;">
        <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
          <label for="codeInput" style="font-weight:600;">Python script</label>
          <span style="color:#888; font-size:.85em;" data-i18n="code.hint">Tab で 4 スペース / mode=rerun で実行</span>
          <span style="margin-left:auto;">
            <button type="button" id="codeLoadTemplate" class="pill" style="background:#eef8ff; border-color:#9bf;" data-i18n-title="code.template.title" title="テンプレ挿入"><span data-i18n="code.template">📄 template</span></button>
          </span>
        </div>
        <textarea id="codeInput" rows="15"
                  spellcheck="false"
                  placeholder="import asyncio
import paprika_client as pap
from paprika_client import async_paprika

# connect() の引数は省略 OK → paprika-runner 内では PAPRIKA_HUB env が
# 自動で読まれる (= http://hub:8000)。ローカル実行時のみ
# os.environ['PAPRIKA_HUB']=http://localhost:8000 をセットしてから走らせる。

async def main():
    async with async_paprika.connect() as cli:
        async with cli.session(initial_url='https://example.com/') as page:
            async for visit in pap.walk(page, target_pages=10):
                print(f'[{visit.n}/{visit.target}] {visit.url}')

asyncio.run(main())"
                  style="width:100%; box-sizing:border-box; margin-top:4px; font-family:ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.4; tab-size:4;"></textarea>
        <div style="margin-top:8px; display:flex; gap:14px; align-items:center; flex-wrap:wrap;">
          <label data-i18n-title="code.urlopt.title" title="Code mode は URL を表示用にのみ使用。空 OK。"><span data-i18n="code.urlopt">URL は省略可</span></label>
          <label title="Up to 864000s (10 days)."><span data-i18n="code.timeout">attempt timeout</span>: <input type="number" id="codeTimeout" value="180" min="30" max="864000" style="width:90px"> s</label>
          <label data-i18n-title="code.hostdedup.title" title="参考表示。Code mode では script 内で直接指定。" style="opacity:.7;">
            <input type="checkbox" id="codeHostDedup" checked disabled> <span data-i18n="code.hostdedup">既訪問URLをスキップ (host_dedup) — 参考表示</span>
          </label>
        </div>
      </div>

      <div style="display:flex; gap:8px; align-items:center;">
        <button type="submit" id="submitBtn"><span id="submitBtnLabel" data-i18n="submit.btn">▶ submit</span></button>
        <!-- Clear resets the per-mode input fields (URL, Goal, Code).
             Submit-and-resubmit is the common path so we keep values
             after submit; Clear is the explicit "start fresh" knob. -->
        <button type="button" id="submitClear"
                style="background:#f5f5fa; border:1px solid #bbc; color:#555;
                       padding:6px 14px; border-radius:5px; cursor:pointer;"
                data-i18n-title="submit.clear.title" title="URL / Goal / Code 入力欄をクリアする">
          <iconify-icon icon="lucide:eraser"></iconify-icon> <span data-i18n="submit.clear">clear</span>
        </button>
      </div>
      <div class="help" style="margin-top:8px;">
        Shortcut: <code>GET /https://example.com</code> でアドレスバーから直接 submit (Fetch mode)。
      </div>
    </form>
  </section>

  <!-- Phase 2c: Save-as-HostRecipe approval modal. Opened from the job
       detail panel when the operator clicks "🍱 recipe として保存" on a
       completed AI調査 (or any codegen-loop / rerun) job that captured
       at least one action. Pre-filled from /jobs/{id}/recipe_suggestion;
       submit POSTs to /hosts/{host}/recipes.
       MUST live OUTSIDE the #submit form -- a nested <form> would
       auto-close the outer form and detach the submit button. -->
  <dialog id="recipeSaveModal" style="border:1px solid #ccc; border-radius:8px; padding:0; width:min(720px, 92vw); max-height:90vh; overflow:auto;">
    <form method="dialog" id="recipeSaveForm" style="padding:16px;">
      <h3 style="margin:0 0 12px;"><iconify-icon icon="lucide:bento"></iconify-icon> recipe として保存</h3>
      <div id="recipeSaveBody" style="display:grid; gap:8px;">
        <label style="display:grid; gap:2px;">
          <span style="font-size:.85em; color:#666;">host (例: example.com)</span>
          <input type="text" id="recipeSaveHost" required>
        </label>
        <label style="display:grid; gap:2px;">
          <span style="font-size:.85em; color:#666;">pattern (URL path のグロブ。例: /frame*)</span>
          <input type="text" id="recipeSavePattern" required>
        </label>
        <label style="display:grid; gap:2px;">
          <span style="font-size:.85em; color:#666;">説明</span>
          <input type="text" id="recipeSaveDescription">
        </label>
        <details>
          <summary style="cursor:pointer; font-weight:600;">action trace のプレビュー (<span id="recipeSaveActionCount">0</span>)</summary>
          <pre id="recipeSaveActionsPreview" style="background:#f7f7fc; padding:8px; border-radius:4px; max-height:220px; overflow:auto; font-size:.8em;"></pre>
        </details>
        <details>
          <summary style="cursor:pointer; font-weight:600;">生成スクリプト (監査用)</summary>
          <pre id="recipeSaveCodePreview" style="background:#f7f7fc; padding:8px; border-radius:4px; max-height:240px; overflow:auto; font-size:.8em;"></pre>
        </details>
        <details>
          <summary style="cursor:pointer; font-weight:600;">goal</summary>
          <pre id="recipeSaveGoalPreview" style="background:#f7f7fc; padding:8px; border-radius:4px; max-height:120px; overflow:auto; white-space:pre-wrap; font-size:.85em;"></pre>
        </details>
        <div id="recipeSaveError" style="color:#c00; display:none;"></div>
      </div>
      <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:12px;">
        <button type="button" id="recipeSaveCancel">キャンセル</button>
        <button type="submit" id="recipeSaveSubmit" style="font-weight:600;">保存</button>
      </div>
    </form>
  </dialog>

  <!-- Inline live panel: revealed after submit. Layout/styling is in
       the page <style> block as `#liveJobPanel ...`; inline styles here
       are kept to a minimum (only those that act as runtime state, like
       "hidden by default" or specific pane heights). -->
  <section id="liveJobPanel" style="display:none;">
    <h2 id="ljpHeader">
      <span class="ljp-status-group">
        <span class="ljp-live-badge" title="このパネルは進行中ジョブの live ビューです">
          <span class="ljp-livedot" aria-hidden="true"></span> LIVE
        </span>
        <code id="ljpJobId"></code>
        <span id="ljpStatus" class="ljp-status-pill status-queued">…</span>
        <span id="ljpAssetCount" class="ljp-asset-pill" style="display:none;">0 assets</span>
      </span>
      <span class="ljp-actions-group">
        <button id="ljpStop" class="pill" disabled data-i18n-title="ljp.pause.title" title="ジョブを一時停止する" style="--la-bg:#fde6e6; --la-bd:#d68080; --la-fg:#8a1d1d;"><iconify-icon icon="lucide:pause"></iconify-icon> <span data-i18n="ljp.pause">pause</span></button>
        <button id="ljpResume" class="pill" disabled data-i18n-title="ljp.resume.title" title="直前のスクリプトで再実行する" style="--la-bg:#e6f7e9; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:play"></iconify-icon> <span data-i18n="ljp.resume">resume</span></button>
        <button id="ljpSaveRecipe" class="pill" style="display:none; --la-bg:#fff7e6; --la-bd:#e8c97a; --la-fg:#7a5a14;" title="この job を HostRegistry のレシピとして登録"><iconify-icon icon="lucide:bento"></iconify-icon> <span data-i18n="ljp.saverecipe">レシピとして保存</span></button>
        <span class="ljp-more-wrap" id="ljpMoreWrap">
          <button id="ljpMore" class="pill" data-i18n-title="ljp.more.title" title="その他の操作" aria-haspopup="true" aria-expanded="false" style="--la-bg:#eceaf6; --la-bd:#cfcbe6; --la-fg:#38306a;"><iconify-icon icon="lucide:ellipsis"></iconify-icon> <span data-i18n="ljp.more">その他</span> <iconify-icon icon="lucide:chevron-down" class="ljp-more-caret"></iconify-icon></button>
          <div id="ljpMoreMenu" class="ljp-more-menu" role="menu">
            <a id="ljpOpenGallery" class="pill" href="#" target="_blank" style="--la-bg:#eef8ff; --la-bd:#9bf; --la-fg:#0a4a7e;"><iconify-icon icon="lucide:image"></iconify-icon> <span data-i18n="ljp.screenshots">スクリーンショット一覧</span></a>
            <button id="ljpRefresh" class="pill" style="display:none; --la-bg:#eef8ff; --la-bd:#7ab; --la-fg:#1a5a8a;" data-i18n-title="ljp.refresh.title" title="ブラウザ状態からアセット/リンクを再取り込み"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="ljp.refresh">refresh</span></button>
            <button id="ljpVideoDl" class="pill" style="display:none; --la-bg:#fdf5ee; --la-bd:#d8a06f; --la-fg:#7a3a0a;" data-i18n-title="ljp.video.title" title="yt-dlp で動画をダウンロード"><iconify-icon icon="lucide:download"></iconify-icon> <span data-i18n="ljp.video">video</span></button>
            <a id="ljpOpenResult" class="pill" href="#" target="_blank" style="--la-bg:#f0f0f6; --la-bd:#bbc; --la-fg:#333;"><iconify-icon icon="lucide:file-text"></iconify-icon> <span data-i18n="ljp.result">result</span></a>
            <a id="ljpOpenPageHtml" class="pill" href="#" target="_blank" data-i18n-title="ljp.pagehtml.title" title="クロール時点の DOM スナップショット (page.html) を別タブで開く" style="--la-bg:#f0f6f0; --la-bd:#9bd09b; --la-fg:#1f5a1f;"><iconify-icon icon="lucide:code-xml"></iconify-icon> <span data-i18n="ljp.pagehtml">取得 HTML</span></a>
            <a id="ljpOpenLog" class="pill" href="#" target="_blank" style="--la-bg:#f0f0f6; --la-bd:#bbc; --la-fg:#333;"><iconify-icon icon="lucide:external-link"></iconify-icon> <span data-i18n="ljp.logtab">log tab</span></a>
            <hr>
            <button id="ljpSavePreset" class="pill" data-i18n-title="ljp.savepreset.title" title="このジョブを preset として保存" style="--la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:bookmark"></iconify-icon> <span data-i18n="ljp.savepreset">save preset</span></button>
          </div>
        </span>
        <button id="ljpClose" class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933;"><iconify-icon icon="lucide:x"></iconify-icon> <span data-i18n="ljp.close">close</span></button>
      </span>
    </h2>
    <!-- Tabbed inner layout: Log / noVNC / Code / Gallery. Each pane
         takes the full panel width so long lines (script code, log
         tracebacks) aren't crammed into a 50% column.
         The active tab is persisted via paprika.ljp.activeTab. -->
    <div class="ljp-tabbar">
      <button class="ljp-tab active" data-ljp-tab="log"><iconify-icon icon="lucide:scroll-text"></iconify-icon> <span data-i18n="ljp.tab.log">Log</span></button>
      <button class="ljp-tab" data-ljp-tab="vnc"><iconify-icon icon="lucide:monitor"></iconify-icon> noVNC <span class="count" id="ljpVncCount">0</span></button>
      <button class="ljp-tab" data-ljp-tab="screenshot"><iconify-icon icon="lucide:camera"></iconify-icon> <span data-i18n="ljp.tab.screenshot">Screenshot</span> <span class="count" id="ljpShotCount">0</span></button>
      <button class="ljp-tab" data-ljp-tab="links"><iconify-icon icon="lucide:link"></iconify-icon> <span data-i18n="ljp.tab.links">Links</span> <span class="count" id="ljpLinksCount">0</span></button>
      <button class="ljp-tab" data-ljp-tab="network"><iconify-icon icon="lucide:activity"></iconify-icon> <span data-i18n="ljp.network">Network</span> <span class="count" id="ljpNetCount">0</span></button>
      <button class="ljp-tab" data-ljp-tab="code"><iconify-icon icon="lucide:code-2"></iconify-icon> <span data-i18n="ljp.tab.code">Code</span> <span class="count" id="ljpCodeCount">0</span></button>
      <button class="ljp-tab" data-ljp-tab="gallery"><iconify-icon icon="lucide:image"></iconify-icon> <span data-i18n="ljp.tab.gallery">Gallery</span> <span class="count" id="ljpGalleryCount">0</span></button>
    </div>

    <!-- Each pane has its body at height:720px so tab switches don't resize -->
    <div class="ljp-pane" data-ljp-pane="log">
      <pre id="ljpLog" class="ljp-pane-body" style="padding:10px; height:720px; overflow:auto; font-size:12px; line-height:1.45; white-space:pre-wrap; margin:0;"></pre>
    </div>

    <div class="ljp-pane" data-ljp-pane="vnc" style="display:none;">
      <div class="ljp-pane-toolbar">
        <span>live noVNC</span>
        <label style="margin-left:auto; cursor:pointer;" title="ページズーム。ブラウザ内のウェブページを拡大縮小します (Ctrl+/Ctrl- 相当)。ウィンドウサイズは変えず、ページ内容だけ拡大縮小 (CSS zoom)。">
          zoom:
          <select id="ljpVncZoom" style="font-size:0.95em; margin-left:4px;">
            <option value="0.5">50%</option>
            <option value="0.75">75%</option>
            <option value="1.0" selected>100%</option>
            <option value="1.25">125%</option>
            <option value="1.5">150%</option>
            <option value="2.0">200%</option>
          </select>
        </label>
      </div>
      <div id="ljpVncGrid" class="ljp-pane-body" style="display:grid; grid-template-columns: 1fr; gap:8px; height:720px; overflow:auto; background:#0a0a14;">
        <div class="empty" style="padding:24px; text-align:center; color:#888; border:1px dashed #444; border-radius:6px; align-self:start;">noVNC will appear once a session opens…</div>
      </div>
    </div>

    <!-- Code pane: per-attempt script with model/usage meta. Attempt
         selector buttons at the top let the user inspect every retry. -->
    <div class="ljp-pane" data-ljp-pane="code" style="display:none;">
      <div class="ljp-pane-toolbar" style="flex-wrap:wrap;">
        <div id="ljpCodeAttempts" style="display:flex; gap:6px; flex-wrap:wrap;"></div>
        <button id="ljpCodeRerun" disabled title="Submit the selected attempt's script as a fresh rerun job (no LLM, just sandbox)" style="background:#eef8ee; border:1px solid #6a6; color:#252; padding:4px 12px; cursor:pointer; border-radius:5px; font-size:0.85em;">▶ rerun this script</button>
        <span id="ljpCodeMeta" style="margin-left:auto; color:#555; font-size:0.8em; font-family:ui-monospace, Consolas, monospace;"></span>
      </div>
      <pre id="ljpCodeBody" class="ljp-pane-body" style="padding:12px; height:720px; overflow:auto; font-size:12.5px; line-height:1.55; white-space:pre; margin:0;"><span style="color:#888; font-style:italic;">no LLM-generated code yet (codegen-loop mode only)…</span></pre>
    </div>

    <!-- Preview pane: live preview of the bound lane (= ephemeral,
         polled) + "Capture" button that POSTs to
         /jobs/{id}/screenshot which saves the current frame
         as a "screenshot-<ts>.jpg" asset on the job. Two distinct
         concepts intentionally co-located so the operator sees
         "this is what's on screen now" and "save it" side by side.
         The thumbnail strip below the live preview shows captures
         saved so far for this job (filtered by name prefix). -->
    <div class="ljp-pane" data-ljp-pane="screenshot" style="display:none;">
      <div class="ljp-pane-toolbar" style="flex-wrap:wrap; gap:10px;">
        <span>live preview</span>
        <label style="margin-left:8px; cursor:pointer;">
          auto-refresh:
          <select id="ljpShotInterval" style="font-size:0.95em; margin-left:4px;">
            <option value="0">off</option>
            <option value="1">1s</option>
            <option value="2" selected>2s</option>
            <option value="5">5s</option>
            <option value="10">10s</option>
          </select>
        </label>
        <button id="ljpShotCaptureBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c; font-weight:600;" title="今のフレームを保存"><iconify-icon icon="lucide:camera"></iconify-icon> Screenshot</button>
        <span id="ljpShotStatus" style="margin-left:auto; color:#666; font-size:.85em;"></span>
      </div>
      <div class="ljp-pane-body" style="height:720px; overflow:auto; padding:12px; background:#0a0a14; display:flex; flex-direction:column; gap:10px;">
        <div id="ljpShotLiveWrap" style="background:#000; border-radius:6px; overflow:hidden; flex:0 0 auto; max-height:55%;">
          <img id="ljpShotLiveImg" alt="live" style="display:block; width:100%; max-height:55vh; object-fit:contain;">
          <div id="ljpShotLiveEmpty" style="padding:32px; text-align:center; color:#888; font-style:italic;">waiting for a worker + lane…</div>
        </div>
        <div id="ljpShotThumbsHeader" style="color:#aaa; font-size:.85em; padding:4px 2px;">
          screenshots (<span id="ljpShotThumbsCount">0</span>)
          <span id="ljpShotThumbsHint" style="color:#666; margin-left:8px;">— click a thumbnail to open the full-size image</span>
        </div>
        <div id="ljpShotThumbs" style="display:grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap:8px; align-content:start;"></div>
      </div>
    </div>

    <!-- Links pane: every <a href> on the current page of each active
         session, resolved to absolute URLs. Polled while the tab is
         visible so the operator can watch outbound URLs change as a
         macro navigates / scrolls. -->
    <div class="ljp-pane" data-ljp-pane="links" style="display:none;">
      <div class="ljp-pane-toolbar" style="flex-wrap:wrap; gap:10px;">
        <span>page links</span>
        <label style="margin-left:8px; cursor:pointer;">
          auto-refresh:
          <select id="ljpLinksInterval" style="font-size:0.95em; margin-left:4px;">
            <option value="0">off</option>
            <option value="2">2s</option>
            <option value="5" selected>5s</option>
            <option value="10">10s</option>
            <option value="30">30s</option>
          </select>
        </label>
        <button id="ljpLinksRefreshBtn" class="pill" title="今すぐ再取得"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> refresh</button>
        <button id="ljpLinksCopyBtn" class="pill" title="現在表示中の URL をクリップボードにコピー (1 行 1 URL)"><iconify-icon icon="lucide:clipboard-copy"></iconify-icon> copy URLs</button>
        <input id="ljpLinksFilter" type="search" placeholder="filter (substring)" style="flex:1; min-width:160px; padding:4px 8px; font-size:.9em; border:1px solid #ccd; border-radius:4px;">
        <span id="ljpLinksStatus" style="color:#666; font-size:.85em;"></span>
      </div>
      <div id="ljpLinksList" class="ljp-pane-body" style="padding:12px; height:720px; overflow:auto; background:#fafafb; font-size:13px;">
        <div style="color:#888; font-style:italic;">セッションがまだ開始されていません…</div>
      </div>
    </div>

    <!-- Network pane: media traffic observed by CDP Network listeners.
         Shows every image/audio/video/font response the browser loaded,
         deduped by URL. The operator can filter and "add to assets" to
         cherry-pick resources the automatic capture missed or filtered
         out (e.g. below min_asset_size_bytes). -->
    <div class="ljp-pane" data-ljp-pane="network" style="display:none;">
      <div class="ljp-pane-toolbar" style="flex-wrap:wrap; gap:10px;">
        <span data-i18n="ljp.network.heading">ネットワーク</span>
        <label style="margin-left:8px; cursor:pointer;">
          auto-refresh:
          <select id="ljpNetInterval" style="font-size:0.95em; margin-left:4px;">
            <option value="0">off</option>
            <option value="3">3s</option>
            <option value="5" selected>5s</option>
            <option value="10">10s</option>
            <option value="30">30s</option>
          </select>
        </label>
        <button id="ljpNetRefreshBtn" class="pill" title="今すぐ再取得"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="ljp.network.refresh">refresh</span></button>
        <label style="cursor:pointer; font-size:.9em;">
          <input type="checkbox" id="ljpNetHideSaved" style="margin-right:3px;">
          <span data-i18n="ljp.network.hideSaved">保存済みを隠す</span>
        </label>
        <input id="ljpNetFilter" type="search" placeholder="filter (URL / MIME)" style="flex:1; min-width:160px; padding:4px 8px; font-size:.9em; border:1px solid #ccd; border-radius:4px;">
        <span id="ljpNetStatus" style="color:#666; font-size:.85em;"></span>
      </div>
      <div id="ljpNetList" class="ljp-pane-body" style="padding:0; height:720px; overflow:auto; background:#fafafb; font-size:12px;">
        <table style="width:100%; border-collapse:collapse; font-size:12px;">
          <thead style="position:sticky; top:0; background:#f0f0f5; z-index:1;">
            <tr>
              <th style="text-align:left; padding:6px 8px; border-bottom:2px solid #ddd; white-space:nowrap;" data-i18n="ljp.network.th.mime">MIME</th>
              <th style="text-align:right; padding:6px 8px; border-bottom:2px solid #ddd; white-space:nowrap;" data-i18n="ljp.network.th.size">サイズ</th>
              <th style="text-align:left; padding:6px 8px; border-bottom:2px solid #ddd;" data-i18n="ljp.network.th.url">URL</th>
              <th style="text-align:center; padding:6px 8px; border-bottom:2px solid #ddd; white-space:nowrap;" data-i18n="ljp.network.th.status">状態</th>
              <th style="text-align:center; padding:6px 8px; border-bottom:2px solid #ddd; white-space:nowrap;" data-i18n="ljp.network.th.action">操作</th>
            </tr>
          </thead>
          <tbody id="ljpNetBody"></tbody>
        </table>
      </div>
    </div>

    <!-- Gallery pane: full-width thumbnails. -->
    <div class="ljp-pane" data-ljp-pane="gallery" style="display:none;">
      <div class="ljp-pane-toolbar">
        <span>captured assets</span>
        <span id="ljpGalleryEmpty" style="color:#888; display:none;">— none captured; the page may have no extra resources (try a richer URL).</span>
      </div>
      <div id="ljpGalleryGrid" class="ljp-pane-body" style="display:grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); grid-auto-rows: 180px; gap:10px; height:720px; overflow:auto; padding:12px; background:#fafafb; align-content:start;"></div>
    </div>
    <!-- Backwards-compat shim: keep #ljpGalleryWrap so legacy show/hide
         JS doesn't NPE while we transition. It's now just a no-op
         wrapper around the real gallery pane. -->
    <div id="ljpGalleryWrap" style="display:none;"></div>
  </section>

  <!-- Asset detail modal (overlay). One per page; populated on click. -->
  <div id="ljpAssetModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center; padding:20px;">
    <div style="background:#fff; max-width:720px; width:100%; max-height:90vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
      <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa;">
        <code id="ljpAssetModalName" style="flex:1; font-size:0.95em; background:transparent; padding:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></code>
        <button id="ljpAssetModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
      </div>
      <div id="ljpAssetModalPreview" style="background:#1a1a1a; min-height:200px; display:flex; align-items:center; justify-content:center; overflow:hidden;"></div>
      <div style="padding:12px 14px; font-size:0.85em; overflow:auto;">
        <table style="width:100%; border-collapse:collapse;">
          <tbody>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap; width:1%;">掲載ページ</td>
                <td style="padding:4px 8px; word-break:break-all;" id="ljpAssetModalPage"><span style="color:#888;">(unknown)</span></td></tr>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap;">取得元 URL</td>
                <td style="padding:4px 8px; word-break:break-all;" id="ljpAssetModalSrc"><span style="color:#888;">(unknown)</span></td></tr>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap;">ハブ URL</td>
                <td style="padding:4px 8px; word-break:break-all;"><a id="ljpAssetModalHubLink" href="#" target="_blank" style="color:#06a;"></a></td></tr>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap;">サイズ</td>
                <td style="padding:4px 8px;" id="ljpAssetModalSize"></td></tr>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap;">MIME / 拡張子</td>
                <td style="padding:4px 8px;" id="ljpAssetModalMime"></td></tr>
            <tr><td style="padding:4px 8px; color:#777; vertical-align:top; white-space:nowrap;">寸法</td>
                <td style="padding:4px 8px;" id="ljpAssetModalDims"><span style="color:#888;">(loading…)</span></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="panel" data-panel="workers">
  <section>
    <h2><span data-i18n="tab.workers">Workers</span> (<span id="workerCount">0</span>)</h2>
    <table id="workersTable"><thead>
      <tr>
        <th data-i18n="workers.th.id">worker_id</th>
        <th data-i18n="workers.th.address">address</th>
        <th data-i18n="workers.th.status">status</th>
        <th data-i18n="workers.th.load">load</th>
        <th data-i18n="workers.th.profiles" title="Chrome profiles prefetched on this worker (from /profiles registry)">profiles</th>
        <th data-i18n="workers.th.version">version</th>
        <th data-i18n="workers.th.labels">labels</th>
        <th data-i18n="workers.th.actions">actions</th>
      </tr>
    </thead><tbody><tr><td colspan=8 class="empty" data-i18n="workers.empty">no workers connected</td></tr></tbody></table>
  </section>
</div>

<div class="panel" data-panel="jobs">
  <section>
    <h2><span data-i18n="tab.jobs">Recent jobs</span>
      <span class="hctrl flat-actions">
        <span class="menu-wrap" style="display:inline-block; position:relative;">
          <button id="jobsColsBtn" class="pill" style="--la-bg:#eef0ff; --la-bd:#6a8ec7; --la-fg:#3a5ca8;" title="表示する列を選ぶ"><iconify-icon icon="lucide:columns-3"></iconify-icon> <span data-i18n="jobs.cols">columns</span> <span class="caret">▾</span></button>
          <div id="jobsColsMenu" class="menu" style="min-width:180px;"></div>
        </span>
        <button id="bulkCleanup" class="pill" style="--la-bg:#fff5e6; --la-bd:#e0a060; --la-fg:#7a4500;" title="Delete old completed jobs (kept last 10 always)"><iconify-icon icon="lucide:broom"></iconify-icon> <span data-i18n="jobs.cleanup">cleanup old…</span></button>
        <button id="bulkDelete" class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933;"><iconify-icon icon="lucide:trash-2"></iconify-icon> <span data-i18n="jobs.deleteall">delete all</span></button>
      </span>
    </h2>
    <table id="jobsTable"><thead>
      <tr><th data-col="id" data-i18n="jobs.th.id">id</th><th data-col="mode" data-i18n="jobs.th.mode">mode</th><th data-col="status" data-i18n="jobs.th.status">status</th><th data-col="url">URL</th><th data-col="worker" data-i18n="jobs.th.worker">worker/lane</th><th data-col="started" data-i18n="jobs.th.started">started</th><th data-col="ended" data-i18n="jobs.th.ended">ended</th><th data-col="duration" data-i18n="jobs.th.duration">duration</th><th data-col="actions" data-i18n="jobs.th.actions">actions</th></tr>
    </thead><tbody><tr><td colspan=9 class="empty" data-i18n="jobs.empty">no jobs yet</td></tr></tbody></table>
    <!-- Recent-jobs client-side pager. Rebuilt by admin.js after each
         refresh() tick so total / range / button-enabled state track
         the live data. Page size is operator-set via the select on
         the right, persisted in localStorage. -->
    <div id="jobsPager" style="margin-top:10px; display:flex; gap:8px; align-items:center; font-size:.88em;"></div>
  </section>
</div>

<div class="panel" data-panel="sessions">
  <section>
    <h2><span data-i18n="tab.sessions">Sessions</span> (<span id="sessionCount">0</span>)
      <span class="hctrl">
        <button id="openSessionBtn" class="pill" style="background:#eef; border-color:#88c;"><iconify-icon icon="lucide:plus"></iconify-icon> <span data-i18n="sessions.open">open session</span></button>
        <button id="closeAllSessions" class="pill" style="background:#fee; border-color:#c88;"><iconify-icon icon="lucide:trash-2"></iconify-icon> <span data-i18n="sessions.closeall">close all</span></button>
      </span>
    </h2>
    <p class="help" style="margin:4px 0 12px;">
      Long-lived <code>/sessions/{id}</code> reservations. Each session
      pins one Lane until you (or its TTL) closes it. Drive over HTTP
      with <code>paprika-client</code> (see <code>client/python/README.md</code>).
    </p>
    <table id="sessionsTable"><thead>
      <tr>
        <th data-i18n="sessions.th.id">session_id</th>
        <th data-i18n="sessions.th.state">state</th>
        <th data-i18n="sessions.th.worker">worker / lane</th>
        <th data-i18n="sessions.th.url">initial url</th>
        <th data-i18n="sessions.th.active">last active</th>
        <th data-i18n="sessions.th.visits">visits</th>
        <th>noVNC</th>
        <th data-i18n="sessions.th.actions">actions</th>
      </tr>
    </thead><tbody><tr><td colspan=8 class="empty" data-i18n="sessions.empty">no active sessions</td></tr></tbody></table>
  </section>
</div>

<div class="panel" data-panel="hosts">
  <section>
    <h2><span data-i18n="tab.hosts">Hosts</span> (<span id="hostCount">0</span>)
      <span class="hctrl">
        <input type="search" id="hostSearch" data-i18n-placeholder="hosts.search.placeholder" placeholder="host / notes で絞り込み" style="padding:4px 8px; min-width:220px;">
        <button id="addHostBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:plus"></iconify-icon> <span data-i18n="hosts.add">add host</span></button>
        <button id="refreshHostsBtn" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="hosts.refresh">refresh</span></button>
      </span>
    </h2>
    <p class="help" style="margin:4px 0 12px;">
      ホスト毎に Cookie / 既訪問URL / 再クロール対象パターンを管理。
      Cookie はセッション開始時に自動注入、Visited URL は <code>pap.walk()</code> の
      重複防止に共有、Recrawl Patterns は visited に登録済みでも常に再訪問する URL の
      glob (<code>*</code> ワイルドカード)。
    </p>
    <table id="hostsTable"><thead>
      <tr>
        <th data-i18n="hosts.th.host">host</th>
        <th data-i18n="hosts.th.cookies">cookies</th>
        <th data-i18n="hosts.th.dedup">dedup</th>
        <th data-i18n="hosts.th.recipes">recipes</th>
        <th data-i18n="hosts.th.notes">notes</th>
        <th data-i18n="hosts.th.updated">updated</th>
        <th data-i18n="hosts.th.lastused">last used</th>
        <th data-i18n="hosts.th.actions">actions</th>
      </tr>
    </thead><tbody><tr><td colspan=8 class="empty" data-i18n="hosts.empty">no hosts registered</td></tr></tbody></table>
    <div id="hostsPager" style="margin-top:10px; display:flex; gap:8px; align-items:center; justify-content:center; color:#666; font-size:.9em;"></div>
  </section>
</div>

<!-- Host edit modal: shared for "add new" and "edit existing". The
     cookie textarea takes a JSON array; the form normalises on save. -->
<div id="hostModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:760px; width:100%; max-height:92vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa;">
      <strong id="hostModalTitle" style="flex:1;">Add host</strong>
      <button id="hostModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <div style="padding:14px; overflow:auto; display:flex; flex-direction:column; gap:10px;">
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Host <span style="color:#a00;">*</span></span>
        <input type="text" id="hostModalHost" placeholder="example.com (www. は自動で除去)"
               style="font-family:ui-monospace, Consolas, monospace; padding:6px 8px; font-size:0.95em;">
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600; display:flex; align-items:center; gap:8px;">
          Cookies (JSON array)
          <button type="button" id="hostModalPaste" class="pill" style="background:#eef8ff; border-color:#9bf; font-size:0.78em;" title="DevTools → Application → Cookies の export を貼り付けやすいテンプレを挿入">📋 paste template</button>
          <span id="hostModalCookieErr" style="color:#a00; font-size:0.85em;"></span>
        </span>
        <textarea id="hostModalCookies" rows="14" spellcheck="false"
                  style="font-family:ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.45; padding:8px; tab-size:2;"
                  placeholder='[\n  {"name": "session_token", "value": "abc...", "domain": ".example.com", "path": "/", "secure": true, "httpOnly": true, "sameSite": "Lax"},\n  {"name": "lang", "value": "en", "domain": ".example.com", "path": "/"}\n]'></textarea>
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Notes (任意)</span>
        <input type="text" id="hostModalNotes" placeholder="ex: 2026-05-17 paps acct"
               style="padding:6px 8px;">
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Popup policy <small style="font-weight:normal; color:#888;">— 別タブが開いたときの処理</small></span>
        <select id="hostModalPopupPolicy" style="padding:6px 8px; max-width:380px;">
          <option value="kill">kill — popup を閉じる + 同ドメインのときだけ main tab を redirect (デフォ)</option>
          <option value="follow">follow — popup を閉じる + ドメイン無関係に main tab を redirect</option>
        </select>
        <small style="color:#888;">
          window.open / target="_blank" でクリック先が別タブに飛ぶ動画サイトでは <strong>follow</strong>。
          main tab がそのまま動画ページに遷移するので、後続の <code>page.download_video()</code> が効きます。
        </small>
      </label>
      <div style="display:flex; gap:6px; flex-wrap:wrap; color:#666; font-size:0.85em;">
        <span>使える CDP フィールド:</span>
        <code>name</code> <code>value</code> <code>domain</code> <code>path</code>
        <code>expires</code> <code>secure</code> <code>httpOnly</code>
        <code>sameSite</code> <code>priority</code> <code>url</code>
      </div>
      <div style="background:#f7f7fc; padding:8px 12px; border-radius:6px; font-size:.88em; color:#555;">
        🗂 <strong>Dedup data</strong> (既訪問URL一覧 / Recrawl patterns) は
        Hosts タブの <strong>📋 dedup</strong> ボタンから別画面で管理。
      </div>
    </div>
    <div class="flat-actions" style="display:flex; gap:8px; padding:10px 14px; border-top:1px solid #eee; background:#fafafa;">
      <button id="hostModalDelete" class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933; margin-right:auto; display:none;"><iconify-icon icon="lucide:trash-2"></iconify-icon> delete</button>
      <button id="hostModalCancel" class="pill" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;">cancel</button>
      <button id="hostModalSave" class="pill" style="--la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> save</button>
    </div>
  </div>
</div>

<!-- Dedup-data modal: Recrawl patterns (top) + Visited URLs (bottom) for one host -->
<div id="visitedModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1001; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:880px; width:100%; max-height:92vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa; gap:8px;">
      <strong><iconify-icon icon="lucide:filter"></iconify-icon> Dedup data &middot; <code id="visitedModalHost">host</code></strong>
      <button id="visitedModalClose" style="margin-left:auto; background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <!-- Recrawl patterns section: spreadsheet-style per-row editor -->
    <div style="padding:10px 14px; border-bottom:1px solid #eee; background:#fcfcfc;">
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
        <strong style="font-size:.95em;"><iconify-icon icon="lucide:target"></iconify-icon> Recrawl patterns</strong>
        <span style="color:#888; font-size:.85em;">
          (<code>*</code> = 任意文字列、<code>?</code> = 1 文字。一致 URL は visited 登録済みでも常に再クロール)
        </span>
        <span id="recrawlPatternsSaved" style="margin-left:auto; color:#196b2c; font-size:.85em; opacity:0; transition:opacity .3s;">✓ saved</span>
      </div>
      <table id="patternsTable" style="width:100%; border-collapse:collapse; font-size:.9em; margin-bottom:6px;">
        <thead>
          <tr style="background:#f3f3f7; color:#555;">
            <th style="padding:4px 8px; text-align:left;">Pattern</th>
            <th style="padding:4px 8px; text-align:right; width:90px;" title="visited URL 内で一致した件数">matches</th>
            <th style="padding:4px 8px; width:40px;"></th>
          </tr>
        </thead>
        <tbody id="patternsTbody">
          <tr><td colspan=3 style="padding:8px; color:#888; text-align:center;">(no patterns)</td></tr>
        </tbody>
      </table>
      <div style="display:flex; gap:6px; align-items:center;">
        <button id="patternsAddRow" class="pill" style="background:#eef8ff; border-color:#9bf;"><iconify-icon icon="lucide:plus"></iconify-icon> add row</button>
        <button id="recrawlPatternsSave" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> save patterns</button>
        <span id="recrawlPatternsErr" style="color:#a00; font-size:.85em;"></span>
      </div>
    </div>
    <!-- Visited URLs section -->
    <div style="padding:8px 14px; border-bottom:1px solid #eee; background:#fafafa; display:flex; gap:8px; align-items:center;">
      <strong style="font-size:.95em;"><iconify-icon icon="lucide:list"></iconify-icon> Visited URLs</strong>
      <span id="visitedModalCount" style="color:#666; font-size:.9em;"></span>
      <input type="search" id="visitedModalSearch" placeholder="🔎 substring filter" style="margin-left:auto; padding:4px 8px; min-width:220px;">
      <button id="visitedModalClear" class="pill" style="background:#fee; border-color:#c88; color:#933;"><iconify-icon icon="lucide:trash-2"></iconify-icon> clear all</button>
    </div>
    <div id="visitedModalList" style="padding:6px 14px; overflow:auto; flex:1; min-height:200px; font-family:ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.5;">
      <div style="color:#888; padding:14px;">loading…</div>
    </div>
    <div id="visitedModalPager" style="padding:8px 14px; border-top:1px solid #eee; background:#fafafa; display:flex; gap:8px; align-items:center; justify-content:center; font-size:.9em; color:#666;"></div>
  </div>
</div>

<div class="panel" data-panel="presets">
  <section>
    <h2><span data-i18n="tab.presets">Preset job</span> (<span id="presetCount">0</span>)
      <span class="hctrl">
        <input type="text" id="presetSearch" data-i18n-placeholder="presets.search.placeholder" placeholder="🔍 search name / category / URL …"
               style="padding:4px 10px; border:1px solid #ccd; border-radius:5px; min-width:240px;">
        <select id="presetCategoryFilter" style="padding:4px 8px; border:1px solid #ccd; border-radius:5px;" title="category で絞り込み">
          <option value="" data-i18n="presets.category.all">(all categories)</option>
        </select>
        <button id="refreshPresetsBtn" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="presets.refresh">refresh</span></button>
      </span>
    </h2>
    <p style="color:#666; font-size:.9em; margin-top:4px;">
      Submit form の設定スナップショット。UI から <strong>load</strong> でフォームに復元、
      または <strong>run</strong> で直接実行。外部からは <code>POST /presets/{name}/run</code> で
      cron / 外部スケジューラから発火可能。
    </p>
    <table id="presetsTable" style="width:100%; border-collapse:collapse; margin-top:8px; font-size:.9em;">
      <thead>
        <tr style="background:#f5f5fa; border-bottom:1px solid #ccd;">
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.name">Name</th>
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.category">Category</th>
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.mode">Mode</th>
          <th style="padding:8px; text-align:left;">URL</th>
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.updated">Updated</th>
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.lastused">Last used</th>
          <th style="padding:8px; text-align:left;" data-i18n="presets.th.actions">Actions</th>
        </tr>
      </thead>
      <tbody>
        <tr><td colspan="7" style="padding:12px; color:#888; text-align:center;" data-i18n="presets.empty">no presets yet — save one from the Submit form</td></tr>
      </tbody>
    </table>
    <div id="presetsPager" style="margin-top:10px; display:flex; gap:8px; align-items:center; font-size:.88em;"></div>
    <details style="margin-top:14px; font-size:.88em;">
      <summary style="cursor:pointer; color:#3a5ca8;">📖 API examples (curl / cron)</summary>
      <pre style="background:#fafafb; border:1px solid #e0e0e8; padding:10px; border-radius:5px; margin-top:6px; overflow:auto;">## List presets (paginated)
curl 'http://paprika.lan/presets?q=daily&category=daily&offset=0&limit=50'

## Get one preset's full record
curl http://paprika.lan/presets/my-preset-name

## Run a preset (fires a job, returns JobInfo)
curl -X POST http://paprika.lan/presets/my-preset-name/run

## Run with overrides (URL / timeout)
curl -X POST http://paprika.lan/presets/my-preset-name/run \
  -H "Content-Type: application/json" \
  -d '{"url": "https://different.example.com/", "attempt_timeout_s": 300}'

## Delete a preset
curl -X DELETE http://paprika.lan/presets/my-preset-name

## Cron example (every day at 03:00)
0 3 * * * curl -fsS -X POST http://paprika.lan/presets/my-daily-fetch/run</pre>
    </details>
  </section>
</div>

<div class="panel" data-panel="profiles">
  <section>
    <h2><span data-i18n="profiles.heading">Chrome Profiles</span> (<span id="profileCount">0</span>)
      <span class="hctrl">
        <button type="button" id="profilesRefreshBtn" class="pill"
                data-i18n-title="profiles.refresh.title">
          <iconify-icon icon="lucide:refresh-cw"></iconify-icon>
          <span data-i18n="profiles.refresh">refresh</span>
        </button>
      </span>
    </h2>
    <p class="muted" style="margin: 4px 0 12px; font-size: 13px;">
      操作者の Chrome のクッキー・ログイン状態を <code>.tar.gz</code> にして上げておくと、
      ジョブ投入時に <code>options.use_profile = "&lt;name&gt;"</code>
      でその状態でクロールが回せます。
      <strong>★ ボタンで「デフォルト」</strong>に設定すると、
      <code>options.use_profile</code> を指定しないジョブも自動的にそのプロファイルで動きます。
      タールボールはローカル側で
      <code>paprika-client upload-profile</code> CLI が作るか、Chrome 拡張機能
      <a href="/profiles/extension/install" target="_blank" style="color:#4275a8;">Paprika Bridge</a>
      を使うとブラウザから直接送れます (cookie のみ、即時)。
    </p>

    <div id="profileUploadDrop" style="
      border: 2px dashed #c8c8d4;
      border-radius: 8px;
      padding: 18px;
      text-align: center;
      background: #fafafd;
      cursor: pointer;
      margin-bottom: 16px;
      transition: background 120ms, border-color 120ms;
    ">
      <iconify-icon icon="lucide:upload-cloud" style="font-size: 32px; color: #8888a0;"></iconify-icon>
      <div style="margin-top: 8px; font-weight: 600;">
        <span data-i18n="profiles.drop.title">アーカイブをここにドラッグ&ドロップ</span>
      </div>
      <div class="muted" style="font-size: 12px; margin-top: 4px;">
        <span data-i18n="profiles.drop.hint">
          または クリックでファイル選択 ・ サイズ上限 500 MB ・
          <code>.tar.gz</code> / <code>.zip</code> どちらも OK
          (ZIP は自動で tar.gz に変換)
        </span>
      </div>
      <input type="file" id="profileUploadFile"
             accept=".gz,.tgz,.zip,application/gzip,application/zip,application/x-zip-compressed"
             style="display:none;">
      <div id="profileUploadNameRow" style="display:none; margin-top: 10px;">
        <label style="font-size:12px; margin-right:6px;" data-i18n="profiles.name.label">
          名前:
        </label>
        <input type="text" id="profileUploadName"
               style="padding:4px 8px; border:1px solid #ccc; border-radius:4px; width:200px;"
               placeholder="mydefault" maxlength="64" />
        <button type="button" id="profileUploadStartBtn" class="pill"
                style="background:#fef5e7; border-color:#d4a13d; color:#8a5a00; margin-left:8px;">
          <iconify-icon icon="lucide:upload"></iconify-icon>
          <span data-i18n="profiles.upload">upload</span>
        </button>
        <button type="button" id="profileUploadCancelBtn" class="pill" style="margin-left:4px;">
          <span data-i18n="profiles.cancel">cancel</span>
        </button>
      </div>
      <div id="profileUploadProgress" style="display:none; margin-top:10px; font-size:12px;"></div>
    </div>

    <div id="profileDefaultBanner" style="
      display:none;
      background:#fff8e1;
      border-left:4px solid #d4a13d;
      padding:8px 12px;
      margin-bottom:12px;
      border-radius:0 6px 6px 0;
      font-size:13px;
    "></div>

    <table id="profilesTable" style="width:100%;">
      <thead>
        <tr>
          <th style="text-align:left;">name</th>
          <th style="text-align:right; width:90px;">size</th>
          <th style="text-align:left;">uploaded</th>
          <th style="text-align:left;">source</th>
          <th style="text-align:left;">note</th>
          <th style="text-align:right; width:120px;"></th>
        </tr>
      </thead>
      <tbody>
        <tr><td colspan="6" class="empty" data-i18n="profiles.empty">no profiles uploaded</td></tr>
      </tbody>
    </table>

    <details style="margin-top:18px;">
      <summary style="cursor:pointer; color:#666;">
        <span data-i18n="profiles.howto.title">使い方</span>
      </summary>
      <div style="margin-top:8px; line-height:1.6; font-size:13px;">
        <strong>方法 1: CLI でアップロード</strong>
        <pre style="background:#f7f7fa; padding:10px; border-radius:6px; font-size:12px;"># Windows
taskkill /F /IM chrome.exe /T

# macOS / Linux
pkill -f chrome

# どの OS でも
paprika-client upload-profile --name mydefault --hub http://paprika.lan</pre>

        <strong>方法 2: Web GUI でアップロード</strong>
        <ol style="margin: 6px 0 12px 18px;">
          <li>Chrome を完全終了 (上記コマンドで)</li>
          <li>ローカルで profile を <code>.zip</code> か <code>.tar.gz</code> に固める。<strong>Windows は ZIP が一番楽</strong>:
            <pre style="background:#f7f7fa; padding:10px; border-radius:6px; font-size:12px;"># Windows -- 右クリックで ZIP 化
# 1. エクスプローラーで %LOCALAPPDATA%\Google\Chrome\User Data を開く
# 2. "Default" フォルダと "Local State" ファイルを選択
# 3. 右クリック -> 「送る」 -> 「圧縮 (ZIP) 形式のフォルダー」
# 4. できた .zip を Profiles タブにドラッグ&ドロップ

# Mac / Linux -- tar.gz でも ZIP でも OK
cd ~/.config/google-chrome           # Mac: ~/Library/Application\ Support/Google/Chrome
tar czf ~/mydefault.tar.gz Default "Local State"
# または:
zip -r ~/mydefault.zip Default "Local State"</pre>
          </li>
          <li>できた <code>.zip</code> または <code>.tar.gz</code> を上の領域にドラッグ&ドロップ、名前を入れて upload</li>
        </ol>
        <div style="background:#eef8ee; border-left:4px solid #5c9138; padding:8px 12px; margin:8px 0; font-size:12px;">
          <strong>💡 ZIP も受け付けます:</strong> 0.3 から hub 側で
          <code>.zip</code> を <code>.tar.gz</code> に自動変換するので、
          Windows Explorer の「送る → 圧縮 (ZIP) 形式」で十分です。
          無理に <code>tar</code> コマンドを使う必要はありません。
        </div>

        <strong>方法 3: Chrome 拡張 (Paprika Bridge) で cookie 即送信</strong>
        <p style="margin:6px 0;">
          <a href="/profiles/extension/install" target="_blank" style="color:#4275a8;">Paprika Bridge</a>
          をインストールすると、ツールバーから「現在の Chrome のクッキーを Paprika Hub に push」できます。
          tarball 不要・Chrome を閉じる必要も無し。ただし保存対象は <strong>cookie だけ</strong>
          (Login Data / IndexedDB は含まれない)。実用上 90% のサイトはこれで OK。
          今後のバージョンで URL 転送、クリップボード共有、ジョブ状態取得などを追加予定。
        </p>

        <strong>ジョブ投入時の指定</strong>
        <pre style="background:#f7f7fa; padding:10px; border-radius:6px; font-size:12px;">POST /jobs
{
  "url": "https://example.com/logged-in-page",
  "options": {"use_profile": "mydefault"}
}</pre>
      </div>
    </details>
  </section>
</div>

<!--
  Chrome extension registry. Operators upload .zip / .crx / .tar.gz
  of an unpacked Chrome extension; the worker fleet auto-loads each
  enabled extension on every lane via --load-extension.

  Why this is separate from Profiles:
    - Profiles carry operator-identity data (cookies, login state).
      They're picked per job via options.use_profile.
    - Extensions are app-shaped (an ad blocker should run on every
      lane regardless of which operator profile is in use).
    - Chrome's own sync mechanism for extensions breaks within
      ~hours of profile upload (Google revokes the session, "Sync is
      paused"). Loading via --load-extension from a hub-managed
      cache sidesteps Chrome sync entirely.
-->
<div class="panel" data-panel="extensions">
  <section>
    <h2><span data-i18n="tab.extensions">Extensions</span> (<span id="extensionCount">0</span>)
      <span class="hctrl">
        <button id="extUploadBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;" title="Upload a Chrome extension (.zip / .crx / .tar.gz of the unpacked dir)"><iconify-icon icon="lucide:upload"></iconify-icon> <span data-i18n="extensions.upload">upload</span></button>
        <button id="refreshExtensionsBtn" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="extensions.refresh">refresh</span></button>
      </span>
    </h2>
    <p style="color:#666; font-size:.9em; margin-top:4px;">
      ハブで管理する Chrome 拡張機能。アップロードした拡張は <strong>全 worker のすべての lane</strong> で
      <code>--load-extension</code> 経由で自動的に読み込まれます (次回 Chrome 起動時から)。
      対応フォーマット: <strong>.zip</strong> / <strong>.crx</strong> / <strong>.tar.gz</strong>
      (中身は manifest.json を含む unpacked extension のディレクトリ)。
    </p>
    <p style="color:#888; font-size:.85em;">
      <strong>※ Chrome sync との関係</strong>: Google プロファイルからアップロードした profile に
      含まれる拡張も別経路でロードされますが、Google 側が数時間後にセッションを失効させると
      「Sync is paused」状態になり sync 由来の拡張が動かなくなることがあります。ここから
      アップロードすると Chrome sync に依存しないので、Google ログインに関係なく常時動作します。
    </p>
    <input type="file" id="extUploadFile" accept=".zip,.crx,.tar.gz,.tgz,application/zip,application/octet-stream,application/gzip" style="display:none;">
    <table id="extensionsTable" style="width:100%; border-collapse:collapse; margin-top:8px; font-size:.9em;">
      <thead>
        <tr style="background:#f5f5fa; border-bottom:1px solid #ccd;">
          <th style="padding:8px; text-align:left;">Slug</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.name">Name</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.version">Version</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.size">Size</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.updated">Updated</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.enabled">Enabled</th>
          <th style="padding:8px; text-align:left;" data-i18n="extensions.th.actions">Actions</th>
        </tr>
      </thead>
      <tbody>
        <tr><td colspan="7" style="padding:12px; color:#888; text-align:center;" data-i18n="extensions.empty">no extensions yet — click upload to add one</td></tr>
      </tbody>
    </table>
    <details style="margin-top:14px; font-size:.88em;">
      <summary style="cursor:pointer; color:#3a5ca8;">📖 API examples (curl)</summary>
      <pre style="background:#fafafb; border:1px solid #e0e0e8; padding:10px; border-radius:5px; margin-top:6px; overflow:auto;">## List extensions
curl 'http://paprika.lan/extensions'

## Upload a .crx (or .zip / .tar.gz)
curl -X POST 'http://paprika.lan/extensions/ublock-lite' \
  -H "X-Filename: ublock-lite.crx" \
  --data-binary @ublock-lite.crx

## Toggle enabled
curl -X POST 'http://paprika.lan/extensions/ublock-lite/enabled' \
  -H "Content-Type: application/json" -d '{"enabled": false}'

## Delete
curl -X DELETE 'http://paprika.lan/extensions/ublock-lite'

## Workers fetch this on connect:
curl 'http://paprika.lan/extensions/ublock-lite/download' --output ublock-lite.tar.gz</pre>
    </details>
  </section>
</div>

<div class="panel" data-panel="engines">
  <section>
    <h2><span data-i18n="engines.heading">AI Engines</span> (<span id="engineCount">0</span>)
      <span class="hctrl">
        <button type="button" id="enginesNewBtn" class="pill" style="background:#fef5e7; border-color:#d4a13d; color:#8a5a00;" data-i18n-title="engines.add.title"><iconify-icon icon="lucide:plus"></iconify-icon> <span data-i18n="engines.add">add engine</span></button>
        <button type="button" id="enginesRefreshBtn" class="pill" data-i18n-title="engines.refresh.title"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="engines.refresh">refresh</span></button>
      </span>
    </h2>
    <div style="color:#555; font-size:.92em; margin-bottom:10px;">
      <code>page.agent(engine="&lt;slug&gt;")</code> や <code>page.ask()</code> から呼べる AI バックエンドの一覧。
      組み込みの <code>qwen</code> / <code>qwen-chat</code> / <code>cogagent</code> はそのまま使えます。
      新規追加は OpenAI / Claude (LiteLLM 経由) / Gemini など <strong>OpenAI 互換</strong> エンドポイントに対応。
      API キーは <code>.env</code> の環境変数を参照する方式 (本ファイルには値を保存しません)。
    </div>
    <!-- Master-detail layout: 左に slug 一覧、右に編集フォーム -->
    <div id="enginesLayout" style="display:grid; grid-template-columns: 280px 1fr; gap:14px; align-items:start;">
      <!-- LEFT: list -->
      <div id="enginesList" style="display:flex; flex-direction:column; gap:4px; border:1px solid #e0e0e8; border-radius:6px; padding:6px; background:#fafafb; max-height:560px; overflow-y:auto;">
        <div style="color:#888; padding:12px; text-align:center;">loading…</div>
      </div>
      <!-- RIGHT: form -->
      <div id="enginesDetail" style="border:1px solid #e0e0e8; border-radius:6px; padding:14px; background:#fff; min-height:560px;">
        <div id="enginesDetailEmpty" style="color:#888; text-align:center; padding:40px 0;" data-i18n="engines.detail.empty">
          左のリストから 1 つ選ぶか、上部の add engine で新規追加してください。
        </div>
        <div id="enginesDetailForm" style="display:none;">
          <div style="display:grid; grid-template-columns:max-content 1fr; gap:8px 10px; align-items:center;">
            <label for="engineSlug" style="font-weight:600;">Slug</label>
            <input type="text" id="engineSlug" placeholder="claude-sonnet-3.5 (lowercase, kebab-case)" style="padding:5px 8px;">

            <label for="engineName" style="font-weight:600;">Name</label>
            <input type="text" id="engineName" placeholder="Claude 3.5 Sonnet" style="padding:5px 8px;">

            <label for="engineKind" style="font-weight:600;">Kind</label>
            <select id="engineKind" style="padding:5px 8px;">
              <option value="chat">chat (text in -&gt; text out: page.ask, codegen)</option>
              <option value="vision-chat">vision-chat (text + image: page.agent qwen/Claude-V)</option>
              <option value="gui-agent">gui-agent (image + task -&gt; CLICK x,y: CogAgent)</option>
            </select>

            <label for="engineProtocol" style="font-weight:600;">Protocol</label>
            <select id="engineProtocol" style="padding:5px 8px;">
              <option value="openai">openai (OpenAI / vLLM / Ollama / LiteLLM / OpenRouter)</option>
              <option value="anthropic">anthropic (reserved for v2 -- use openai for now)</option>
              <option value="agent-service">agent-service (paprika bundled)</option>
              <option value="cogagent">cogagent (paprika bundled)</option>
            </select>

            <label for="engineEndpoint" style="font-weight:600;">Endpoint</label>
            <input type="text" id="engineEndpoint" placeholder="https://api.openai.com  (no trailing /v1)" style="padding:5px 8px;">

            <label for="engineModel" style="font-weight:600;">Model</label>
            <input type="text" id="engineModel" placeholder="gpt-4o-mini" style="padding:5px 8px;">

            <label for="engineApiKey" style="font-weight:600;">API key (direct)</label>
            <div style="display:flex; gap:8px; align-items:center;">
              <input type="password" id="engineApiKey" placeholder="sk-...  (literal key; stored on disk, never returned via API)" style="padding:5px 8px; flex:1;" autocomplete="new-password">
              <span id="engineApiKeyDirectStatus" style="font-size:.85em; color:#888;"></span>
            </div>
            <div style="grid-column:2; font-size:.75em; color:#888; margin-top:-4px;">
              リテラルキーを直接保存。空のまま編集すると現状の値を維持。
              明示的にクリアしたければ <code>clear</code> ボタン。
              <button type="button" id="engineApiKeyClearBtn" style="margin-left:4px; font-size:.85em; padding:1px 6px;">clear</button>
            </div>

            <label for="engineApiKeyEnv" style="font-weight:600;">API key (env var)</label>
            <div style="display:flex; gap:8px; align-items:center;">
              <input type="text" id="engineApiKeyEnv" placeholder="OPENAI_API_KEY  (env var NAME on the hub container)" style="padding:5px 8px; flex:1;">
              <span id="engineApiKeyStatus" style="font-size:.85em; color:#888;"></span>
            </div>
            <div style="grid-column:2; font-size:.75em; color:#888; margin-top:-4px;">
              env var の <strong>名前</strong>。hub の .env に
              <code>OPENAI_API_KEY=sk-...</code> を入れて hub を再起動した場合に使う。
              直接キーと両方設定されたら direct を優先。
            </div>

            <label for="engineTimeout" style="font-weight:600;">Timeout (s)</label>
            <input type="number" id="engineTimeout" min="5" max="600" value="60" style="padding:5px 8px; width:120px;">

            <label for="engineHeaders" style="font-weight:600; align-self:start; padding-top:5px;">Headers (JSON)</label>
            <textarea id="engineHeaders" rows="3" placeholder='{"anthropic-version": "2023-06-01"}' style="padding:5px 8px; font-family:ui-monospace,Consolas,monospace; font-size:12px;"></textarea>

            <label style="font-weight:600;">Promoted</label>
            <label style="cursor:pointer;"><input type="checkbox" id="enginePromoted"> engine="auto" 時にこの kind の中で優先する</label>

            <label style="font-weight:600;">コード生成LLM</label>
            <label style="cursor:pointer;" title="Submit フォームの「コード生成 LLM」セレクタに表示。チェックを入れた engine だけが選択肢として並ぶ。"><input type="checkbox" id="engineUseForCodegen"> Submit の「コード生成 LLM」セレクタに表示する</label>

            <label style="font-weight:600; align-self:start; padding-top:5px;">Daily quota</label>
            <div style="display:flex; flex-direction:column; gap:6px;">
              <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                <label style="display:flex; align-items:center; gap:6px;" title="このエンジン経由の 1 日合計トークン上限 (prompt + completion)。0 = 制限なし。UTC 0:00 にリセット。">
                  <span>token / day</span>
                  <input type="number" id="engineDailyTokenBudget" min="0" step="1000" placeholder="0 (=無制限)" style="padding:4px 6px; width:140px;">
                </label>
                <label style="display:flex; align-items:center; gap:6px;" title="このエンジン経由の 1 日 API リクエスト数の上限。0 = 制限なし。">
                  <span>requests / day</span>
                  <input type="number" id="engineDailyRequestBudget" min="0" step="10" placeholder="0 (=無制限)" style="padding:4px 6px; width:120px;">
                </label>
              </div>
              <div id="engineUsageToday" style="font-size:.82em; color:#666;">今日の利用量: 読み込み中…</div>
              <div style="font-size:.75em; color:#888;">
                上限を超えると次の codegen / planner / judge 呼び出しが <strong>このエンジンだけ</strong> 拒否されます (他エンジンや非 LLM ジョブはそのまま動作)。リセットは UTC 0:00 (= JST 9:00)。
              </div>
            </div>

            <label for="engineNotes" style="font-weight:600; align-self:start; padding-top:5px;">Notes</label>
            <textarea id="engineNotes" rows="2" placeholder="自由記述メモ" style="padding:5px 8px;"></textarea>
          </div>
          <div style="margin-top:14px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <button type="button" id="engineSaveBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c; font-weight:600;"><iconify-icon icon="lucide:save"></iconify-icon> <span data-i18n="engines.save">Save</span></button>
            <button type="button" id="engineTestBtn" class="pill"><iconify-icon icon="lucide:plug-zap"></iconify-icon> <span data-i18n="engines.test">Test connection</span></button>
            <button type="button" id="engineDeleteBtn" class="pill" style="background:#fde6e6; border-color:#d68080; color:#8a1d1d;"><iconify-icon icon="lucide:trash-2"></iconify-icon> <span data-i18n="engines.delete">Delete</span></button>
            <span id="engineStatus" style="margin-left:auto; font-size:.85em; color:#666;"></span>
          </div>
          <pre id="engineMeta" style="margin-top:10px; padding:8px 10px; background:#fafafb; border:1px solid #e0e0e8; border-radius:4px; font-size:11px; color:#666;"></pre>
        </div>
      </div>
    </div>
  </section>
</div>

<!--
  Preset save / overwrite modal. Replaces the older 3x window.prompt()
  chain (name / category / description) and lets the operator pick
  WHICH execution mode the saved preset should run in, instead of
  silently inheriting whatever radio happens to be checked on the
  Submit form (which produced surprises like "I clicked save and got
  a fetch-mode preset because the form had drifted back to fetch").
-->
<div id="presetSaveModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:640px; width:100%; max-height:94vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa;">
      <strong id="presetSaveModalTitle" style="flex:1;">Save preset</strong>
      <button id="presetSaveModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <div style="padding:14px; overflow:auto; display:flex; flex-direction:column; gap:12px;">
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Name <span style="color:#a00;">*</span></span>
        <input type="text" id="presetSaveModalName"
               placeholder="e.g. youtube-channel-daily"
               style="font-family:ui-monospace, Consolas, monospace; padding:6px 8px;">
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Category <span style="color:#888; font-weight:normal;">(任意; 既存の category から候補が出ます)</span></span>
        <input type="text" id="presetSaveModalCategory" list="presetSaveModalCategoryList"
               placeholder="e.g. video / daily / login" style="padding:6px 8px;">
        <datalist id="presetSaveModalCategoryList"></datalist>
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Description <span style="color:#888; font-weight:normal;">(任意)</span></span>
        <textarea id="presetSaveModalDescription" rows="2"
                  placeholder="このプリセットは何をする?"
                  style="padding:6px 8px;"></textarea>
      </label>
      <!--
        実行方法 (mode) は entry point 側 (Submit form の save-as
        dropdown / Live panel の save preset / Preset job tab の
        edit) で既に決まっているので、モーダルでは picker を出さない。
        radio はコード互換のために hidden で DOM に残しているだけ。
        Job ID input も同様 -- prefill 値を programmatic に受け渡す
        ためだけに残し、ユーザーには非表示。
      -->
      <div style="display:none;">
        <input type="radio" name="presetSaveModalMode" value="inherit" checked>
        <input type="radio" name="presetSaveModalMode" value="fetch">
        <input type="radio" name="presetSaveModalMode" value="codegen-loop">
        <input type="radio" name="presetSaveModalMode" value="code">
        <input type="radio" name="presetSaveModalMode" value="rerun_from">
        <input type="text" id="presetSaveModalRerunFromJob">
      </div>
      <div id="presetSaveModalRerunFromBlock" style="display:none;"></div>
      <div id="presetSaveModalCodeNote" style="display:none;"></div>
      <div id="presetSaveModalCodegenNote" style="display:none;"></div>
      <span id="presetSaveModalErr" style="color:#a00; font-size:0.9em;"></span>
    </div>
    <div class="flat-actions" style="display:flex; gap:8px; padding:10px 14px; border-top:1px solid #eee; background:#fafafa;">
      <span id="presetSaveModalHint" style="margin-right:auto; color:#888; font-size:.85em; align-self:center;"></span>
      <button id="presetSaveModalCancel" class="pill" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;">cancel</button>
      <button id="presetSaveModalSave" class="pill" style="--la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> save</button>
    </div>
  </div>
</div>

<!--
  Preset edit modal (Preset job tab). The pre-existing path required
  the operator to "load" a preset into the Submit form, edit it
  there, then click "overwrite" — three tab switches for what should
  be a single inline edit. This modal lets them change every field
  the Preset job tab cares about in one place, including renaming
  (which the previous UI only exposed via window.prompt).
-->
<div id="presetEditModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:780px; width:100%; max-height:94vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa;">
      <strong id="presetEditModalTitle" style="flex:1;">Edit preset</strong>
      <button id="presetEditModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <div style="padding:14px; overflow:auto; display:flex; flex-direction:column; gap:12px;">
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <label style="display:flex; flex-direction:column; gap:4px; flex:1; min-width:240px;">
          <span style="font-weight:600;">Name <span style="color:#a00;">*</span></span>
          <input type="text" id="presetEditModalName"
                 placeholder="kebab-case name"
                 style="font-family:ui-monospace, Consolas, monospace; padding:6px 8px;">
          <span id="presetEditModalRenameHint" style="display:none; color:#a06000; font-size:.82em;">⚠ name 変更時は内部で新規 PUT + 旧 name DELETE が走ります</span>
        </label>
        <label style="display:flex; flex-direction:column; gap:4px; min-width:200px;">
          <span style="font-weight:600;">Category</span>
          <input type="text" id="presetEditModalCategory" list="presetEditModalCategoryList"
                 placeholder="e.g. video / daily" style="padding:6px 8px;">
          <datalist id="presetEditModalCategoryList"></datalist>
        </label>
      </div>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Description</span>
        <textarea id="presetEditModalDescription" rows="2"
                  style="padding:6px 8px;"></textarea>
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        <span style="font-weight:600;">Start URL</span>
        <input type="text" id="presetEditModalUrl"
               placeholder="https://example.com/"
               style="padding:6px 8px;">
      </label>
      <div style="display:flex; flex-direction:column; gap:6px; padding:10px; background:#f7f7fc; border-radius:6px; border:1px solid #e3e3ed;">
        <span style="font-weight:600;">実行モード</span>
        <label style="display:flex; gap:8px; align-items:flex-start; cursor:pointer;">
          <input type="radio" name="presetEditModalMode" value="fetch" style="margin-top:3px;">
          <span><strong>fetch</strong> <span style="color:#666; font-size:.85em;">— HTML + アセットを 1 回キャプチャ</span></span>
        </label>
        <label style="display:flex; gap:8px; align-items:flex-start; cursor:pointer;">
          <input type="radio" name="presetEditModalMode" value="codegen-loop" style="margin-top:3px;">
          <span><strong>codegen-loop</strong> <span style="color:#666; font-size:.85em;">— AI が毎回スクリプトを生成</span></span>
        </label>
        <label style="display:flex; gap:8px; align-items:flex-start; cursor:pointer;">
          <input type="radio" name="presetEditModalMode" value="code" style="margin-top:3px;">
          <span><strong>rerun (inline code)</strong> <span style="color:#666; font-size:.85em;">— 保存済み固定スクリプトを再実行</span></span>
        </label>
        <label style="display:flex; gap:8px; align-items:flex-start; cursor:pointer;">
          <input type="radio" name="presetEditModalMode" value="rerun_from" style="margin-top:3px;">
          <span><strong>rerun_from</strong> <span style="color:#666; font-size:.85em;">— 既存ジョブの script を再実行 (軽量)</span></span>
        </label>
      </div>
      <!-- codegen-loop block -->
      <div id="presetEditModalCodegenBlock" style="display:none; padding:10px; background:#eef0ff; border-radius:6px; border:1px solid #c0c8e8; flex-direction:column; gap:8px;">
        <label style="display:flex; flex-direction:column; gap:4px;">
          <span style="font-weight:600;">Goal</span>
          <textarea id="presetEditModalGoal" rows="4"
                    placeholder="Natural-language task for the codegen LLM"
                    style="font-family:ui-monospace, Consolas, monospace; font-size:12.5px; padding:6px 8px;"></textarea>
        </label>
        <div style="display:flex; gap:10px; flex-wrap:wrap;">
          <label style="display:flex; flex-direction:column; gap:4px; flex:1; min-width:200px;">
            <span style="font-weight:600;">Codegen engine</span>
            <select id="presetEditModalEngine" style="padding:6px 8px;">
              <option value="">(default — env)</option>
            </select>
          </label>
          <label style="display:flex; flex-direction:column; gap:4px;">
            <span style="font-weight:600;">max_codegen_attempts</span>
            <input type="number" id="presetEditModalMaxAttempts" min="1" max="10" value="3"
                   style="padding:6px 8px; width:90px;">
          </label>
          <label style="display:flex; flex-direction:column; gap:4px;">
            <span style="font-weight:600;">attempt_timeout_s</span>
            <input type="number" id="presetEditModalTimeoutCodegen" min="30" value="200"
                   style="padding:6px 8px; width:120px;">
          </label>
        </div>
        <label style="display:flex; gap:8px; align-items:center; cursor:pointer;">
          <input type="checkbox" id="presetEditModalHostDedup" checked>
          <span>host-level URL dedup を有効にする (<code>pap.walk(host_dedup=True)</code> 相当)</span>
        </label>
      </div>
      <!-- code (inline rerun) block -->
      <div id="presetEditModalCodeBlock" style="display:none; padding:10px; background:#fff8e6; border-radius:6px; border:1px solid #e0c060; flex-direction:column; gap:8px;">
        <label style="display:flex; flex-direction:column; gap:4px;">
          <span style="font-weight:600;">Inline script (Python)</span>
          <textarea id="presetEditModalCode" rows="10" spellcheck="false"
                    placeholder="import asyncio&#10;import paprika_client as pap&#10;..."
                    style="font-family:ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.45; padding:8px;"></textarea>
        </label>
        <label style="display:flex; flex-direction:column; gap:4px;">
          <span style="font-weight:600;">attempt_timeout_s</span>
          <input type="number" id="presetEditModalTimeoutCode" min="30" value="86400"
                 style="padding:6px 8px; width:120px;">
        </label>
      </div>
      <!-- rerun_from block -->
      <div id="presetEditModalRerunFromBlock" style="display:none; padding:10px; background:#fff8e6; border-radius:6px; border:1px solid #e0c060; flex-direction:column; gap:8px;">
        <label style="display:flex; flex-direction:column; gap:4px;">
          <span style="font-weight:600;">参照する Job ID <span style="color:#a00;">*</span></span>
          <input type="text" id="presetEditModalRerunFromJob"
                 placeholder="e.g. 6e3e7ffe2ce5  (or 6e3e7ffe2ce5/attempts/2)"
                 style="font-family:ui-monospace, Consolas, monospace; padding:6px 8px;">
        </label>
        <label style="display:flex; flex-direction:column; gap:4px;">
          <span style="font-weight:600;">attempt_timeout_s</span>
          <input type="number" id="presetEditModalTimeoutRerun" min="30" value="200"
                 style="padding:6px 8px; width:120px;">
        </label>
      </div>
      <!-- fetch mode info -->
      <div id="presetEditModalFetchNote" style="display:none; padding:10px; background:#eef8ee; border-radius:6px; border:1px solid #7ab68a; color:#196b2c; font-size:.88em;">
        <iconify-icon icon="lucide:info" style="vertical-align:middle;"></iconify-icon>
        fetch モードのオプション (scroll / timing 等) はこのモーダルでは編集できません。<br>
        詳細編集は <strong>load → Submit form → overwrite</strong> でお願いします。
        この画面では URL / category / description / モード切替のみ反映されます。
      </div>
      <span id="presetEditModalErr" style="color:#a00; font-size:0.9em;"></span>
    </div>
    <div class="flat-actions" style="display:flex; gap:8px; padding:10px 14px; border-top:1px solid #eee; background:#fafafa;">
      <button id="presetEditModalDelete" class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933;"><iconify-icon icon="lucide:trash-2"></iconify-icon> delete</button>
      <span style="flex:1;"></span>
      <button id="presetEditModalCancel" class="pill" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;">cancel</button>
      <button id="presetEditModalSave" class="pill" style="--la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> save</button>
    </div>
  </div>
</div>

<!-- ============================================================
     v2 Knowledge tab — visualises HostKnowledge (Phase 2/5/6).
     Read-only at this milestone; future Phase 7 will let operators
     edit / pin / delete individual barrier strategies.
     ============================================================ -->
<div class="panel" data-panel="knowledge">
  <section>
    <h2><iconify-icon icon="lucide:brain"></iconify-icon> <span data-i18n="tab.knowledge">Knowledge</span>
      <span style="font-size:0.7em; color:#888; font-weight:normal; margin-left:8px;" data-i18n="knowledge.subtitle">
        — per-host knowledge v2 has learned (barriers / content / stats)
      </span>
      <span class="hctrl">
        <input type="search" id="hkSearch" data-i18n-placeholder="knowledge.search.placeholder" placeholder="filter by host" style="padding:4px 8px; min-width:220px;">
        <select id="hkTierFilter" style="padding:4px 8px;">
          <option value="" data-i18n="knowledge.tier.all">all tiers</option>
          <option value="high">high</option>
          <option value="medium">medium</option>
          <option value="low">low</option>
          <option value="stale">stale</option>
        </select>
        <button id="hkRefreshBtn" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="knowledge.refresh">refresh</span></button>
      </span>
    </h2>
    <div id="hkSummary" style="display:flex; gap:12px; margin:12px 0; flex-wrap:wrap;">
      <div class="hk-stat" data-stat="total"><strong id="hkTotal">0</strong><span data-i18n="knowledge.tile.hosts"> hosts</span></div>
      <div class="hk-stat hk-tier-high" data-stat="high"><strong id="hkHigh">0</strong><span> high</span></div>
      <div class="hk-stat hk-tier-medium" data-stat="medium"><strong id="hkMedium">0</strong><span> medium</span></div>
      <div class="hk-stat hk-tier-low" data-stat="low"><strong id="hkLow">0</strong><span> low</span></div>
      <div class="hk-stat hk-tier-stale" data-stat="stale"><strong id="hkStale">0</strong><span> stale</span></div>
      <div class="hk-stat" data-stat="barriers"><strong id="hkBarriersTotal">0</strong><span data-i18n="knowledge.tile.barriers"> barriers learned</span></div>
      <div class="hk-stat" data-stat="ce"><strong id="hkExtractionsTotal">0</strong><span data-i18n="knowledge.tile.extractions"> extractions learned</span></div>
    </div>

    <!-- AI Insights tile: judge comparison + R1 distiller activity -->
    <div style="background:#f6f8fa; border:1px solid #d1d9e0; border-radius:10px; padding:14px 18px; margin:10px 0 16px;">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
        <iconify-icon icon="lucide:cpu" style="font-size:1.2em;"></iconify-icon>
        <strong data-i18n="knowledge.ai.heading">AI Insights</strong>
        <span style="color:#888; font-size:0.85em;" data-i18n="knowledge.ai.sub">Phase 3-6 ─ shadow judge & R1 distiller</span>
        <span style="flex:1;"></span>
        <a href="/admin/judge_comparisons" target="_blank" rel="noopener" class="pill" style="background:#fff; border-color:#bbc; color:#444; font-size:0.85em;"><span data-i18n="knowledge.ai.rawapi">raw API</span> ↗</a>
      </div>
      <div id="aiInsights" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:10px;">
        <div class="ai-tile">
          <div class="ai-tile-label" data-i18n="knowledge.ai.paired">Judge verdicts paired</div>
          <div class="ai-tile-value" id="aiPaired">—</div>
          <div class="ai-tile-sub" id="aiPairedSub" data-i18n="knowledge.ai.paired.sub">legacy vs R1 shadow</div>
        </div>
        <div class="ai-tile">
          <div class="ai-tile-label" data-i18n="knowledge.ai.agree">Agreement rate</div>
          <div class="ai-tile-value" id="aiAgree">—</div>
          <div class="ai-tile-sub" id="aiAgreeSub" data-i18n="knowledge.ai.agree.sub">higher = R1 ready to promote</div>
        </div>
        <div class="ai-tile">
          <div class="ai-tile-label" data-i18n="knowledge.ai.distilled">Recent R1 distiller updates</div>
          <div class="ai-tile-value" id="aiDistilled">—</div>
          <div class="ai-tile-sub" id="aiDistilledSub" data-i18n="knowledge.ai.distilled.sub">across all hosts</div>
        </div>
        <div class="ai-tile">
          <div class="ai-tile-label" data-i18n="knowledge.ai.r1hosts">Hosts with R1-learned data</div>
          <div class="ai-tile-value" id="aiR1Hosts">—</div>
          <div class="ai-tile-sub" data-i18n="knowledge.ai.r1hosts.sub">provenance: distiller-r1</div>
        </div>
      </div>
    </div>
    <table id="hkTable" class="hosttbl" style="width:100%;">
      <thead><tr>
        <th data-i18n="knowledge.th.host">host</th>
        <th data-i18n="knowledge.th.tier">tier</th>
        <th data-i18n="knowledge.th.jobs">jobs</th>
        <th data-i18n="knowledge.th.success">success</th>
        <th data-i18n="knowledge.th.barriers">barriers</th>
        <th data-i18n="knowledge.th.extractions">extractions</th>
        <th data-i18n="knowledge.th.updated">last updated</th>
        <th data-i18n="knowledge.th.by">by</th>
      </tr></thead>
      <tbody><tr><td colspan=8 class="empty" data-i18n="knowledge.empty">no HostKnowledge yet — submit a job to start learning</td></tr></tbody>
    </table>
  </section>
</div>

<!-- HostKnowledge detail modal -->
<div id="hkModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1100; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:1000px; width:100%; max-height:92vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa; gap:8px;">
      <iconify-icon icon="lucide:brain"></iconify-icon>
      <strong id="hkModalTitle" style="flex:1;"></strong>
      <span id="hkModalTier" class="hk-badge"></span>
      <a id="hkModalRaw" target="_blank" rel="noopener" class="pill" style="background:#eef8ff; border-color:#9bf; color:#1a5a8a;"><iconify-icon icon="lucide:external-link"></iconify-icon> raw JSON</a>
      <button id="hkModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <div id="hkModalBody" style="padding:14px 18px; overflow:auto; flex:1; font-size:13px; line-height:1.55;"></div>
  </div>
</div>

<style>
.hk-stat { background:#f6f8fa; border:1px solid #d1d9e0; border-radius:8px; padding:8px 14px; font-size:0.9em; display:flex; gap:6px; align-items:baseline; }
.hk-stat strong { font-size:1.4em; font-weight:700; color:#1f2328; }
.hk-stat span { color:#59636e; }
.hk-tier-high  { border-color:#7ab68a; background:#eef8ee; }
.hk-tier-high  strong { color:#196b2c; }
.hk-tier-medium { border-color:#e0a060; background:#fff5e6; }
.hk-tier-medium strong { color:#7a4500; }
.hk-tier-low   { border-color:#bbc; background:#f5f5fa; }
.hk-tier-stale { border-color:#c88; background:#fee; }
.hk-tier-stale strong { color:#933; }
.hk-badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.85em; font-weight:600; border:1px solid; }
.hk-badge.tier-high   { background:#eef8ee; color:#196b2c; border-color:#7ab68a; }
.hk-badge.tier-medium { background:#fff5e6; color:#7a4500; border-color:#e0a060; }
.hk-badge.tier-low    { background:#f5f5fa; color:#444; border-color:#bbc; }
.hk-badge.tier-stale  { background:#fee;   color:#933; border-color:#c88; }
.hk-chip { display:inline-block; padding:2px 8px; border-radius:6px; background:#eef4fa; color:#1a5a8a; border:1px solid #b6d3eb; font-size:0.85em; margin:2px 4px 2px 0; }
.hk-chip.barrier { background:#fdecea; color:#933; border-color:#e8a4a0; }
#hkTable td { padding:6px 10px; border-bottom:1px solid #eee; vertical-align:top; }
#hkTable tr:hover { background:#fafafa; cursor:pointer; }
#hkTable .num { text-align:right; font-variant-numeric:tabular-nums; }
.ai-tile { background:#fff; border:1px solid #e8ecf0; border-radius:8px; padding:10px 14px; }
.ai-tile-label { font-size:0.78em; color:#666; text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px; }
.ai-tile-value { font-size:1.6em; font-weight:700; color:#1f2328; font-variant-numeric:tabular-nums; line-height:1.1; }
.ai-tile-sub { font-size:0.78em; color:#888; margin-top:4px; }
</style>

<script>
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
    let list;
    try {
      const r = await fetch('/host_knowledge');
      const j = await r.json();
      list = j.hosts || [];
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">error: ' + e + '</td></tr>';
      return;
    }
    const details = await Promise.all(list.map(async h => {
      try {
        const rr = await fetch('/hosts/' + encodeURIComponent(h) + '/knowledge');
        if (!rr.ok) return null;
        const k = await rr.json();
        return { host: h, k };
      } catch (e) { return null; }
    }));
    _hkData = details.filter(x => x);
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
      document.getElementById('aiPaired').textContent = paired;
      document.getElementById('aiPairedSub').textContent =
        paired > 0 ? `agree=${counts.agree} disagree=${counts.disagree}` : 'enable PAPRIKA_R1_JUDGE_MODE=shadow';
      const agreeRate = paired > 0 ? Math.round((counts.agree / paired) * 100) + '%' : '—';
      document.getElementById('aiAgree').textContent = agreeRate;
    } catch (e) {
      document.getElementById('aiPaired').textContent = '?';
    }
    // R1 distiller stats — count hosts whose provenance.last_updated_by == 'distiller-r1'
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
    document.getElementById('aiDistilled').textContent = recent.length;
    document.getElementById('aiDistilledSub').textContent =
      recent.length > 0 ? 'in last 24h' : 'no R1 updates in last 24h';
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
      return;
    }
    tbody.innerHTML = rows.map(e => {
      const k = e.k;
      const stats = k.stats || {};
      const tier = stats.overall_confidence || 'low';
      const barriers = ((k.per_page || {}).barriers || {});
      const presentBarriers = Object.entries(barriers).filter(([,v]) => v && v.present).map(([kk]) => kk);
      const extractions = ((k.per_page || {}).content_extraction || []);
      const prov = (k.provenance || {});
      return `<tr data-host="${e.host}">
        <td><strong>${e.host}</strong></td>
        <td>${tierBadge(tier)}</td>
        <td class="num">${stats.total_jobs || 0}</td>
        <td class="num">${pct(stats.success_rate)}</td>
        <td>${presentBarriers.map(b => '<span class="hk-chip barrier">' + b + '</span>').join('') || '<span style="color:#aaa">—</span>'}</td>
        <td>${extractions.length > 0 ? extractions.map(c => '<span class="hk-chip">' + (c.url_pattern || '*') + '</span>').join('') : '<span style="color:#aaa">—</span>'}</td>
        <td>${ago(k.updated_at)}</td>
        <td style="font-size:0.85em; color:#666;">${prov.last_updated_by || '—'}</td>
      </tr>`;
    }).join('');
    tbody.querySelectorAll('tr[data-host]').forEach(tr => {
      tr.addEventListener('click', () => openHkModal(tr.dataset.host));
    });
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
    document.getElementById('hkSearch').addEventListener('input', renderTable);
    document.getElementById('hkTierFilter').addEventListener('change', renderTable);
    document.getElementById('hkModalClose').addEventListener('click', () => {
      document.getElementById('hkModal').style.display = 'none';
    });
    document.getElementById('hkModal').addEventListener('click', (ev) => {
      if (ev.target.id === 'hkModal') document.getElementById('hkModal').style.display = 'none';
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
    // Re-render whenever the locale changes / i18next finishes init,
    // because table contents are built dynamically with tt() lookups.
    if (window.i18next) {
      window.i18next.on('languageChanged', () => { renderTable(); renderSummary(); });
      window.i18next.on('initialized',     () => { renderTable(); renderSummary(); });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
</script>

<!-- ===========================================================
     v2 Phase 7: Plugins tab — Tool Registry visualization.
     Backs the /admin/plugins + /admin/plugins/invocations API.
     ============================================================ -->
<div class="panel" data-panel="plugins">
  <section>
    <h2><iconify-icon icon="lucide:plug"></iconify-icon> <span data-i18n="tab.plugins">Plugins</span>
      <span style="font-size:0.7em; color:#888; font-weight:normal; margin-left:8px;" data-i18n="plugins.subtitle">
        — Tool Registry under <code>data/tools/installed</code> (capability-based auto-invocation)
      </span>
      <span class="hctrl flat-actions">
        <button id="plRefreshBtn" class="pill" style="--la-bg:#eef0ff; --la-bd:#6a8ec7; --la-fg:#3a5ca8;"><iconify-icon icon="lucide:refresh-cw"></iconify-icon> <span data-i18n="plugins.refresh">refresh</span></button>
      </span>
    </h2>
    <div id="plSummary" style="display:flex; gap:12px; margin:12px 0; flex-wrap:wrap;">
      <div class="pl-stat"><strong id="plTotal">0</strong><span data-i18n="plugins.tile.installed"> installed</span></div>
      <div class="pl-stat pl-kind-python"><strong id="plAvailable">0</strong><span data-i18n="plugins.tile.available"> available (catalog)</span></div>
      <div class="pl-stat"><strong id="plInvocations">0</strong><span data-i18n="plugins.tile.invocations"> invocations logged</span></div>
      <div class="pl-stat"><strong id="plOkRate">—</strong><span data-i18n="plugins.tile.successrate"> success rate</span></div>
    </div>

    <p class="help" style="margin:8px 0 14px; padding:8px 12px; background:#f6f8fa; border-left:3px solid #58a6ff; border-radius:4px; font-size:0.9em;">
      <iconify-icon icon="lucide:info"></iconify-icon>
      <span data-i18n="plugins.howto">Auto-invocation: when a job is dispatched, _consult_host_knowledge reads the host's HostKnowledge. If per_page.barriers.&lt;kind&gt;.suggested_tool is set, the plugin's get_cookies action is pre-flighted here and the returned cookies are merged into the HostRecord before the Worker dispatch. Failures silently fall through (the job itself is never blocked).</span>
    </p>

    <table id="plTable" class="hosttbl" style="width:100%;">
      <thead><tr>
        <th data-i18n="plugins.th.status">status</th>
        <th data-i18n="plugins.th.name">name</th>
        <th data-i18n="plugins.th.category">category</th>
        <th data-i18n="plugins.th.summary">summary</th>
        <th data-i18n="plugins.th.capabilities">capabilities</th>
        <th data-i18n="plugins.th.lastinvoked">last invoked</th>
        <th data-i18n="plugins.th.recent">recent</th>
        <th></th>
      </tr></thead>
      <tbody><tr><td colspan=8 class="empty" data-i18n="plugins.loading">loading…</td></tr></tbody>
    </table>
    <p style="margin:8px 0 0; font-size:0.82em; color:#888;">
      <iconify-icon icon="lucide:file-json"></iconify-icon>
      <span data-i18n="plugins.catalog.howto">
        Edit <code>data/tools/catalog.json</code> to advertise additional plugins.
        Installed plugins live under <code>data/tools/installed/&lt;name&gt;/</code>.
      </span>
    </p>

    <h3 style="margin-top:24px; display:flex; align-items:center; gap:8px;">
      <iconify-icon icon="lucide:history"></iconify-icon>
      <span data-i18n="plugins.invocations.heading">History</span>
      <span style="font-size:0.8em; color:#888; font-weight:normal;" data-i18n="plugins.invocations.sub">
        pre-flight / manual / job-triggered — all recorded
      </span>
      <span class="hctrl flat-actions" style="margin-left:auto;">
        <button id="plInvDeleteAll" class="pill" style="--la-bg:#fee; --la-bd:#c88; --la-fg:#933;" title="Clear the invocations audit log"><iconify-icon icon="lucide:trash-2"></iconify-icon> <span data-i18n="plugins.inv.deleteall">delete all</span></button>
      </span>
    </h3>
    <table id="plInvTable" class="hosttbl" style="width:100%;">
      <thead><tr>
        <th data-i18n="plugins.inv.th.at">at</th>
        <th data-i18n="plugins.inv.th.plugin">plugin</th>
        <th data-i18n="plugins.inv.th.action">action</th>
        <th data-i18n="plugins.inv.th.status">status</th>
        <th data-i18n="plugins.inv.th.elapsed">elapsed</th>
        <th data-i18n="plugins.inv.th.hostjob">host / job</th>
        <th data-i18n="plugins.inv.th.trigger">trigger</th>
      </tr></thead>
      <tbody><tr><td colspan=7 class="empty" data-i18n="plugins.inv.empty">no invocations yet</td></tr></tbody>
    </table>
    <!-- Invocations pager. Rebuilt by renderInvocations() so total / range /
         enabled state stay in sync with the live data. Same shape as jobsPager. -->
    <div id="plInvPager" style="margin-top:10px; display:flex; gap:8px; align-items:center; font-size:.88em;"></div>
  </section>
</div>

<!-- Plugin detail / manual-invoke modal -->
<div id="plModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1100; align-items:center; justify-content:center; padding:20px;">
  <div style="background:#fff; max-width:900px; width:100%; max-height:92vh; border-radius:8px; overflow:hidden; display:flex; flex-direction:column;">
    <div style="display:flex; align-items:center; padding:10px 14px; border-bottom:1px solid #eee; background:#fafafa; gap:8px;">
      <iconify-icon icon="lucide:plug"></iconify-icon>
      <strong id="plModalTitle" style="flex:1;"></strong>
      <button id="plModalClose" style="background:transparent; border:none; cursor:pointer; font-size:1.4em; color:#666; padding:0 6px;" title="close">✕</button>
    </div>
    <div id="plModalBody" style="padding:14px 18px; overflow:auto; flex:1; font-size:13px; line-height:1.55;"></div>
  </div>
</div>

<style>
.pl-stat { background:#f6f8fa; border:1px solid #d1d9e0; border-radius:8px; padding:8px 14px; font-size:0.9em; display:flex; gap:6px; align-items:baseline; }
.pl-stat strong { font-size:1.4em; font-weight:700; color:#1f2328; }
.pl-stat span { color:#59636e; }
.pl-kind-python     { border-color:#7ab68a; background:#eef8ee; }
.pl-kind-python     strong { color:#196b2c; }
.pl-kind-subprocess { border-color:#e0a060; background:#fff5e6; }
.pl-kind-subprocess strong { color:#7a4500; }
.pl-kind-http       { border-color:#9bf;    background:#eef8ff; }
.pl-kind-http       strong { color:#1a5a8a; }
#plTable td, #plInvTable td { padding:6px 10px; border-bottom:1px solid #eee; vertical-align:top; }
#plTable tr:hover { background:#fafafa; }
#plTable .num, #plInvTable .num { text-align:right; font-variant-numeric:tabular-nums; }
.pl-chip { display:inline-block; padding:2px 8px; border-radius:6px; background:#eef4fa; color:#1a5a8a; border:1px solid #b6d3eb; font-size:0.82em; margin:2px 4px 2px 0; }
.pl-chip.action { background:#eef8ee; color:#196b2c; border-color:#7ab68a; }
.pl-kind-badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:0.82em; font-weight:600; border:1px solid; }
.pl-kind-badge.kind-python_lib  { background:#eef8ee; color:#196b2c; border-color:#7ab68a; }
.pl-kind-badge.kind-subprocess  { background:#fff5e6; color:#7a4500; border-color:#e0a060; }
.pl-kind-badge.kind-http_service{ background:#eef8ff; color:#1a5a8a; border-color:#9bf; }
.pl-status-ok   { color:#196b2c; font-weight:600; }
.pl-status-fail { color:#933;    font-weight:600; }
.pl-trigger { display:inline-block; padding:1px 6px; border-radius:4px; background:#f0f0f5; color:#555; font-size:0.78em; border:1px solid #ddd; }
.pl-trigger.preflight { background:#eef4fa; color:#1a5a8a; border-color:#b6d3eb; }
.pl-trigger.admin_ui  { background:#fff5e6; color:#7a4500; border-color:#e0a060; }
/* Use the global .pill button style (matches Recent Jobs / Hosts). */
#plTable .pl-row-details { /* "details" button in each row */
  --la-bg:#eef8ff; --la-bd:#9bf; --la-fg:#1a5a8a;
}
.pl-modal-invoke { /* "実行 / invoke" button inside the modal */
  --la-bg:#eef8ee; --la-bd:#7ab68a; --la-fg:#196b2c;
}
.pl-status-badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.78em; font-weight:600; border:1px solid; white-space:nowrap; }
.pl-status-installed { background:#eef8ee; color:#196b2c; border-color:#7ab68a; }
.pl-status-available { background:#f5f5fa; color:#555;    border-color:#bbc; }
.pl-status-localonly { background:#fff5e6; color:#7a4500; border-color:#e0a060; }
</style>

<script>
// ===== v2 Phase 7 Plugins tab — self-contained =====
(function () {
  let _plData = [];
  let _plInvocations = [];
  // Invocations pager state.
  const PL_INV_PAGE_SIZES = [10, 25, 50, 100];
  let _plInvPage = 0;
  function _plInvPageSize() {
    try {
      const stored = parseInt(localStorage.getItem('paprika.plInvPageSize') || '', 10);
      if (PL_INV_PAGE_SIZES.includes(stored)) return stored;
    } catch (_) {}
    return 25;
  }
  function _plInvPageSizeSet(n) {
    try { localStorage.setItem('paprika.plInvPageSize', String(n)); } catch (_) {}
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function ago(iso) {
    if (!iso) return '—';
    // Borrow the global tt() helper from admin.js so units localise too.
    const T = window.tt || ((k, fb) => fb);
    try {
      const d = new Date(iso);
      const s = (Date.now() - d.getTime()) / 1000;
      if (s < 60)     return Math.round(s)       + T('plugins.ago.s', 's ago');
      if (s < 3600)   return Math.round(s/60)    + T('plugins.ago.m', 'm ago');
      if (s < 86400)  return Math.round(s/3600)  + T('plugins.ago.h', 'h ago');
      return Math.round(s/86400) + T('plugins.ago.d', 'd ago');
    } catch (e) { return '—'; }
  }

  function renderSummary() {
    const installedN = _plData.filter(p => p.installed).length;
    document.getElementById('plTotal').textContent     = installedN;
    document.getElementById('plAvailable').textContent = _plData.length;
    document.getElementById('plInvocations').textContent = _plInvocations.length;
    if (_plInvocations.length === 0) {
      document.getElementById('plOkRate').textContent = '—';
    } else {
      const ok = _plInvocations.filter(i => i.ok).length;
      document.getElementById('plOkRate').textContent =
        Math.round((ok / _plInvocations.length) * 100) + '%';
    }
    // Tab badge: show the catalog total so users see how many plugins
    // exist in paprika's universe (installed + advertised).
    const cnt = document.getElementById('cntPlugins');
    if (cnt) cnt.textContent = _plData.length;
  }

  function renderTable() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plTable tbody');
    if (!_plData.length) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.empty', 'catalog is empty — edit data/tools/catalog.json to advertise a plugin') + '</td></tr>';
      return;
    }
    const neverLabel        = T('plugins.never',          'never');
    const detailsLabel      = T('plugins.details',        'details');
    const statusInstalledTxt = T('plugins.status.installed', '✓ installed');
    const statusAvailableTxt = T('plugins.status.available', 'available');
    const localOnlyTxt       = T('plugins.status.localonly', 'local-only');

    const rows = _plData.map(p => {
      const lastInv = _plInvocations.find(i => i.plugin === p.name);
      const recent  = _plInvocations.filter(i => i.plugin === p.name);
      const okN     = recent.filter(i => i.ok).length;
      const recentSummary = recent.length === 0
        ? '<span style="color:#888;">—</span>'
        : `<span class="pl-status-ok">${okN}</span> / ${recent.length}`;
      const caps = (p.capabilities || []).map(c => `<span class="pl-chip">${escHtml(c)}</span>`).join('');

      // Status badge — installed vs catalog-only vs local-only.
      let statusBadge = '';
      if (p.installed && p.in_catalog) {
        statusBadge = `<span class="pl-status-badge pl-status-installed">${statusInstalledTxt}</span>`;
      } else if (p.installed && !p.in_catalog) {
        statusBadge = `<span class="pl-status-badge pl-status-localonly" title="installed but not in catalog.json">${localOnlyTxt}</span>`;
      } else {
        statusBadge = `<span class="pl-status-badge pl-status-available">${statusAvailableTxt}</span>`;
      }

      const versionStr = p.installed ? `${escHtml(p.installed_version || p.version || '')}` : `${escHtml(p.version || '')}`;
      const lastInvCell = p.installed
        ? (lastInv ? ago(lastInv.at) : `<span style="color:#888;">${neverLabel}</span>`)
        : '<span style="color:#bbb;">—</span>';
      const recentCell = p.installed
        ? recentSummary
        : '<span style="color:#bbb;">—</span>';

      // Action button: details (if installed) or disabled "not installed" hint.
      const actionCell = p.installed
        ? `<button class="pill pl-row-details" data-plugin="${escHtml(p.name)}"><iconify-icon icon="lucide:settings-2"></iconify-icon> ${detailsLabel}</button>`
        : '';

      const rowCursor = p.installed ? 'cursor:pointer;' : '';
      return `<tr data-plugin="${escHtml(p.name)}" data-installed="${p.installed ? '1' : '0'}" style="${rowCursor}">
        <td>${statusBadge}</td>
        <td>
          <strong>${escHtml(p.name)}</strong>
          <span style="color:#888; font-size:0.85em; margin-left:6px;">v${versionStr}</span>
        </td>
        <td><span class="pl-chip">${escHtml(p.category || 'uncategorized')}</span></td>
        <td style="max-width:340px;">
          <div style="color:#444; font-size:0.92em;">${escHtml(p.summary || '')}</div>
          ${p.homepage ? `<a href="${escHtml(p.homepage)}" target="_blank" rel="noopener" style="font-size:0.78em; color:#1a5a8a;">${escHtml(p.homepage)}</a>` : ''}
        </td>
        <td>${caps || '<span style="color:#888;">—</span>'}</td>
        <td>${lastInvCell}</td>
        <td>${recentCell}</td>
        <td>${actionCell}</td>
      </tr>`;
    });
    tbody.innerHTML = rows.join('');
    tbody.querySelectorAll('tr').forEach(tr => {
      if (tr.getAttribute('data-installed') !== '1') return;
      tr.addEventListener('click', (e) => {
        const name = tr.getAttribute('data-plugin');
        if (name) openPluginModal(name);
      });
    });
  }

  function renderInvocations() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plInvTable tbody');
    if (!_plInvocations.length) {
      tbody.innerHTML = '<tr><td colspan=7 class="empty">' + T('plugins.inv.empty', 'no invocations yet') + '</td></tr>';
      return;
    }
    const okLabel   = T('plugins.status.ok',   '✓ ok');
    const failLabel = T('plugins.status.fail', '✗ fail');
    const jobLabel  = T('plugins.job', 'job');
    // Slice the invocations array to the current page.
    const pageSize = _plInvPageSize();
    const total    = _plInvocations.length;
    const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (_plInvPage > maxPage) _plInvPage = maxPage;
    if (_plInvPage < 0) _plInvPage = 0;
    const startIdx = _plInvPage * pageSize;
    const endIdx   = Math.min(total, startIdx + pageSize);
    const slice = _plInvocations.slice(startIdx, endIdx);
    const rows = slice.map(i => {
      const trig = i.trigger || i.source || '—';
      const trigCls = trig === 'preflight' ? 'preflight' : (trig === 'admin_ui' ? 'admin_ui' : '');
      const statusHtml = i.ok
        ? '<span class="pl-status-ok">' + okLabel + '</span>'
        : '<span class="pl-status-fail">' + failLabel + '</span>';
      const hostJob = [
        i.host  ? `<span class="pl-chip">${escHtml(i.host)}</span>`   : '',
        i.job_id ? `<span class="pl-chip">${jobLabel} ${escHtml(i.job_id.slice(0,8))}</span>` : '',
      ].filter(x => x).join(' ');
      return `<tr>
        <td style="font-size:0.85em; color:#666; white-space:nowrap;">${ago(i.at)}</td>
        <td><strong>${escHtml(i.plugin)}</strong></td>
        <td><span class="pl-chip action">${escHtml(i.action)}</span></td>
        <td>${statusHtml}${i.error ? ' <span style="color:#933; font-size:0.85em;" title="' + escHtml(i.error) + '">⚠</span>' : ''}</td>
        <td class="num">${i.elapsed_ms != null ? i.elapsed_ms + ' ms' : '—'}</td>
        <td>${hostJob || '<span style="color:#888;">—</span>'}</td>
        <td><span class="pl-trigger ${trigCls}">${escHtml(trig)}</span></td>
      </tr>`;
    });
    tbody.innerHTML = rows.join('');
    renderInvPager(total, startIdx, endIdx);
  }

  function renderInvPager(total, startIdx, endIdx) {
    const T = window.tt || ((k, fb) => fb);
    const host = document.getElementById('plInvPager');
    if (!host) return;
    if (total === 0) { host.innerHTML = ''; return; }
    const pageSize = _plInvPageSize();
    const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
    const prevDisabled = _plInvPage <= 0;
    const nextDisabled = _plInvPage >= maxPage;
    const opts = PL_INV_PAGE_SIZES
      .map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n}</option>`)
      .join('');
    const prevLabel = T('plugins.pager.prev', 'prev');
    const nextLabel = T('plugins.pager.next', 'next');
    const pageLabel = T('plugins.pager.page', 'page');
    const perPage   = T('plugins.pager.perpage', 'per page');
    host.innerHTML = `
      <span style="color:#666;">${startIdx + 1}-${endIdx} / ${total}</span>
      <button class="pill" id="plInvPagerPrev" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;" ${prevDisabled ? 'disabled' : ''}>
        <iconify-icon icon="lucide:chevron-left"></iconify-icon> ${prevLabel}
      </button>
      <span style="color:#666;">${pageLabel} ${_plInvPage + 1} / ${maxPage + 1}</span>
      <button class="pill" id="plInvPagerNext" style="--la-bg:#f5f5fa; --la-bd:#bbc; --la-fg:#444;" ${nextDisabled ? 'disabled' : ''}>
        ${nextLabel} <iconify-icon icon="lucide:chevron-right"></iconify-icon>
      </button>
      <span style="margin-left:auto; color:#888; font-size:.85em;">
        ${perPage} <select id="plInvPagerSize" style="padding:2px 4px;">${opts}</select>
      </span>
    `;
    const prevBtn = document.getElementById('plInvPagerPrev');
    const nextBtn = document.getElementById('plInvPagerNext');
    const sizeSel = document.getElementById('plInvPagerSize');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      if (_plInvPage > 0) { _plInvPage--; renderInvocations(); }
    });
    if (nextBtn) nextBtn.addEventListener('click', () => {
      _plInvPage++; renderInvocations();
    });
    if (sizeSel) sizeSel.addEventListener('change', () => {
      const n = parseInt(sizeSel.value, 10);
      if (PL_INV_PAGE_SIZES.includes(n)) {
        _plInvPageSizeSet(n);
        _plInvPage = 0;
        renderInvocations();
      }
    });
  }

  function openPluginModal(name) {
    const T = window.tt || ((k, fb) => fb);
    const p = _plData.find(x => x.name === name);
    if (!p) return;
    if (!p.installed) return;  // catalog-only entries have no action surface yet
    document.getElementById('plModalTitle').textContent = name + ' · ' + (p.installed_version || p.version || '');
    const body = document.getElementById('plModalBody');

    const myInvocations = _plInvocations.filter(i => i.plugin === name).slice(0, 20);
    const lastErrorEntry = myInvocations.find(i => !i.ok);

    const invokeLabel = T('plugins.modal.invoke', 'invoke');
    // Build per-action invocation form -- one tiny textarea + run button per action.
    const actionsHtml = (p.actions || []).map(act => `
      <div style="border:1px solid #e8ecf0; border-radius:6px; padding:10px 12px; margin:8px 0;">
        <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
          <strong>${escHtml(act)}</strong>
          <span style="flex:1;"></span>
          <button class="pill pl-modal-invoke" data-act="${escHtml(act)}" data-plugin="${escHtml(name)}">
            <iconify-icon icon="lucide:play"></iconify-icon> ${invokeLabel}
          </button>
        </div>
        <textarea data-params-for="${escHtml(act)}"
          style="width:100%; box-sizing:border-box; min-height:80px; font-family:ui-monospace,Consolas,monospace; font-size:12px; padding:6px 8px; border:1px solid #ccc; border-radius:4px;"
          placeholder='{"url": "https://example.com/"}'>{}</textarea>
        <div data-result-for="${escHtml(act)}"
          style="margin-top:6px; font-family:ui-monospace,Consolas,monospace; font-size:11px; color:#666; max-height:240px; overflow:auto;"></div>
      </div>
    `).join('');

    const capsHtml = (p.capabilities || []).map(c => `<span class="pl-chip">${escHtml(c)}</span>`).join('');
    body.innerHTML = `
      <div style="margin-bottom:14px;">
        <span class="pl-kind-badge kind-${escHtml(p.kind)}">${escHtml(p.kind)}</span>
        ${p.disabled ? '<span style="color:#933; font-weight:600; margin-left:8px;">' + T('plugins.disabled', 'DISABLED') + '</span>' : ''}
      </div>

      ${p.notes ? `<div style="background:#f6f8fa; border-left:3px solid #58a6ff; padding:8px 12px; margin:10px 0; border-radius:4px;">${escHtml(p.notes)}</div>` : ''}

      <h4 style="margin:14px 0 4px;">${T('plugins.modal.capabilities', 'Capabilities')}</h4>
      <div>${capsHtml || '<span style="color:#888;">—</span>'}</div>

      <h4 style="margin:18px 0 4px;">${T('plugins.modal.actions', 'Actions')}</h4>
      ${actionsHtml || '<span style="color:#888;">' + T('plugins.modal.noactions', 'no actions') + '</span>'}

      ${lastErrorEntry ? `
        <h4 style="margin:18px 0 4px; color:#933;">${T('plugins.modal.lastfail', 'Last failure')}</h4>
        <div style="background:#fee; border:1px solid #e8a4a0; padding:8px 12px; border-radius:4px; font-family:ui-monospace,Consolas,monospace; font-size:11px; white-space:pre-wrap; max-height:200px; overflow:auto;">${escHtml(lastErrorEntry.error || '')}</div>
      ` : ''}

      ${myInvocations.length ? `
        <h4 style="margin:18px 0 4px;">${T('plugins.modal.recent', 'Recent invocations')} (${myInvocations.length})</h4>
        <table style="width:100%; font-size:0.9em; border-collapse:collapse;">
          <thead><tr style="border-bottom:1px solid #eee; text-align:left; color:#666;">
            <th style="padding:4px 6px;">${T('plugins.modal.th.when', 'when')}</th><th style="padding:4px 6px;">${T('plugins.modal.th.action', 'action')}</th>
            <th style="padding:4px 6px;">${T('plugins.modal.th.status', 'status')}</th><th style="padding:4px 6px;">${T('plugins.modal.th.elapsed', 'elapsed')}</th>
            <th style="padding:4px 6px;">${T('plugins.modal.th.trigger', 'trigger')}</th>
          </tr></thead>
          <tbody>
            ${myInvocations.map(i => `
              <tr style="border-bottom:1px solid #f5f5f5;">
                <td style="padding:4px 6px; color:#666;">${ago(i.at)}</td>
                <td style="padding:4px 6px;">${escHtml(i.action)}</td>
                <td style="padding:4px 6px;">${i.ok ? '<span class="pl-status-ok">✓</span>' : '<span class="pl-status-fail">✗</span>'}</td>
                <td style="padding:4px 6px;" class="num">${i.elapsed_ms != null ? i.elapsed_ms + ' ms' : '—'}</td>
                <td style="padding:4px 6px;"><span class="pl-trigger">${escHtml(i.trigger || i.source || '—')}</span></td>
              </tr>`).join('')}
          </tbody>
        </table>
      ` : ''}
    `;
    document.getElementById('plModal').style.display = 'flex';
    // Re-apply i18n in case the modal body picks up new data-i18n attrs later.
    try { window.applyI18n && window.applyI18n(document.getElementById('plModalBody')); } catch (_) {}

    // Wire per-action invoke buttons inside the modal.
    body.querySelectorAll('button.pl-modal-invoke').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-act');
        const plName = btn.getAttribute('data-plugin');
        const ta  = body.querySelector(`textarea[data-params-for="${act}"]`);
        const out = body.querySelector(`div[data-result-for="${act}"]`);
        const T2 = window.tt || ((k, fb) => fb);
        let params = {};
        try { params = JSON.parse(ta.value || '{}'); }
        catch (e) { out.innerHTML = '<span style="color:#933;">' + T2('plugins.modal.parseerror', 'JSON parse error') + ': ' + escHtml(e.message) + '</span>'; return; }
        out.innerHTML = '<span style="color:#888;">' + T2('plugins.modal.running', 'running…') + '</span>';
        btn.disabled = true;
        try {
          const r = await fetch('/admin/plugins/' + encodeURIComponent(plName) + '/invoke', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: act, params: params })
          });
          const txt = await r.text();
          let pretty;
          try { pretty = JSON.stringify(JSON.parse(txt), null, 2); }
          catch (e) { pretty = txt; }
          out.innerHTML = `<pre style="margin:0; white-space:pre-wrap; color:${r.ok ? '#196b2c' : '#933'};">${escHtml(pretty)}</pre>`;
        } catch (e) {
          out.innerHTML = '<span style="color:#933;">' + T2('plugins.modal.error', 'error') + ': ' + escHtml(String(e)) + '</span>';
        } finally {
          btn.disabled = false;
          // Refresh invocation list so the new call is visible.
          setTimeout(loadInvocations, 600);
        }
      });
    });
  }

  function closeModal() {
    document.getElementById('plModal').style.display = 'none';
  }

  async function loadPlugins() {
    const T = window.tt || ((k, fb) => fb);
    const tbody = document.querySelector('#plTable tbody');
    tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.loading', 'loading…') + '</td></tr>';
    try {
      // Catalog endpoint returns the union of catalog.json + currently-installed
      // plugins with `installed: bool` per entry.
      const r = await fetch('/admin/plugin_catalog');
      const j = await r.json();
      _plData = j.plugins || [];
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan=8 class="empty">' + T('plugins.modal.error', 'error') + ': ' + e + '</td></tr>';
      return;
    }
    await loadInvocations();
  }

  async function loadInvocations() {
    try {
      const r = await fetch('/admin/plugins/invocations?limit=1000');
      const j = await r.json();
      _plInvocations = j.invocations || [];
    } catch (e) {
      _plInvocations = [];
    }
    renderTable();
    renderInvocations();
    renderSummary();
  }

  async function deleteAllInvocations() {
    const T = window.tt || ((k, fb) => fb);
    const confirmMsg = T(
      'plugins.inv.deleteall.confirm',
      'Delete the entire invocations audit log? This cannot be undone.',
    );
    if (!confirm(confirmMsg)) return;
    const btn = document.getElementById('plInvDeleteAll');
    if (btn) btn.disabled = true;
    try {
      const r = await fetch('/admin/plugins/invocations', { method: 'DELETE' });
      if (!r.ok) {
        const t = await r.text();
        alert((T('plugins.modal.error', 'error')) + ': ' + t.slice(0, 200));
        return;
      }
      _plInvPage = 0;
      await loadInvocations();
    } catch (e) {
      alert((T('plugins.modal.error', 'error')) + ': ' + e);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function wire() {
    document.getElementById('plRefreshBtn').addEventListener('click', loadPlugins);
    document.getElementById('plInvDeleteAll').addEventListener('click', deleteAllInvocations);
    document.getElementById('plModalClose').addEventListener('click', closeModal);
    document.getElementById('plModal').addEventListener('click', (e) => {
      if (e.target.id === 'plModal') closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && document.getElementById('plModal').style.display === 'flex') {
        closeModal();
      }
    });
    // Refresh whenever the tab is opened
    document.querySelectorAll('[data-tab="plugins"]').forEach(btn => {
      btn.addEventListener('click', loadPlugins);
    });
    // Silent initial load so the badge count is accurate before the
    // operator opens the tab.
    loadPlugins();
    // Re-render dynamic table contents whenever i18next finishes init
    // (the very first render may happen before init resolves) or the
    // operator switches locale via the header dropdown.
    if (window.i18next) {
      window.i18next.on('languageChanged', () => { renderTable(); renderInvocations(); renderSummary(); });
      window.i18next.on('initialized',     () => { renderTable(); renderInvocations(); renderSummary(); });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
})();
</script>

<div class="panel" data-panel="settings">
  <section>
    <h2><iconify-icon icon="lucide:settings"></iconify-icon> <span data-i18n="tab.settings">Settings</span>
      <span class="hctrl">
        <span id="settingsSavedHint" style="color:#196b2c; font-size:.85em; opacity:0; transition:opacity .3s;">✓ saved</span>
      </span>
    </h2>
    <p class="help" style="margin:4px 0 12px;">
      Submit デフォルト値 (ブラウザ毎に保存) + Hub 全体の学習動作トグル (永続)。
      LLM URL / モデル名 / Worker / Data dir 等は <strong>環境変数で固定</strong> (デプロイ時に決まるので表示のみ)。
    </p>

    <!-- A. Submit defaults (localStorage) -->
    <h3 style="margin-top:.6rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;">📥 Submit defaults
      <small style="font-weight:normal; color:#888;">— このブラウザにだけ保存</small>
    </h3>
    <div style="display:grid; grid-template-columns: 220px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label for="setDefaultMode">Default mode:</label>
      <select id="setDefaultMode" style="padding:4px 8px; width:200px;">
        <option value="fetch">📄 Fetch</option>
        <option value="ai">🤖 AI</option>
        <option value="code">📝 Code</option>
      </select>

      <label for="setLlmMaxAttempts">AI · LLM: max attempts:</label>
      <input type="number" id="setLlmMaxAttempts" min="1" max="10" style="width:80px; padding:4px 8px;">

      <label for="setLlmTimeout">AI · LLM: attempt timeout (s):</label>
      <input type="number" id="setLlmTimeout" min="30" max="864000" style="width:120px; padding:4px 8px;">

      <label for="setLlmHostDedup">AI · LLM: host_dedup default:</label>
      <label style="font-weight:normal;"><input type="checkbox" id="setLlmHostDedup"> 既訪問URLをスキップ</label>

      <label for="setCodeTimeout">Code: attempt timeout (s):</label>
      <input type="number" id="setCodeTimeout" min="30" max="864000" style="width:120px; padding:4px 8px;">

      <label for="setLlmGoal" style="align-self:start; padding-top:6px;">AI · LLM: default Goal:</label>
      <textarea id="setLlmGoal" rows="8" style="font-family:ui-monospace, Consolas, monospace; font-size:12.5px; padding:6px 8px; resize:vertical;"
                placeholder="(空 = ハードコードされたデフォを使用)"></textarea>

      <div></div>
      <div style="display:flex; gap:8px;">
        <button id="setSaveUiBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> Submit defaults を保存</button>
        <button id="setResetUiBtn" class="pill" style="background:#fee; border-color:#c88; color:#933;"><iconify-icon icon="lucide:rotate-ccw"></iconify-icon> デフォルトに戻す</button>
      </div>
    </div>

    <!-- B. Hub-side learning behaviour -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;">🧠 学習動作
      <small style="font-weight:normal; color:#888;">— Hub 永続 (全 operator 共通)</small>
    </h3>
    <div style="display:grid; grid-template-columns: 280px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label>Skill auto-extract:</label>
      <label style="font-weight:normal;">
        <input type="checkbox" id="setSkillAutoExtract">
        codegen-loop の成功後に skill を自動抽出
      </label>

      <label>Convention auto-extract:</label>
      <label style="font-weight:normal;">
        <input type="checkbox" id="setConventionAutoExtract">
        failure→success diff から convention を自動抽出 (attempts ≥ 2 のとき)
      </label>

      <label for="setSkillTopK">Skill retrieval top-K:</label>
      <input type="number" id="setSkillTopK" min="0" max="10" style="width:80px; padding:4px 8px;">

      <div></div>
      <div>
        <button id="setSaveHubBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> 学習動作を保存</button>
        <span id="setHubErr" style="color:#a00; font-size:.9em; margin-left:8px;"></span>
      </div>
    </div>

    <!-- B2. Asset capture (hub-side, persisted) -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;"><iconify-icon icon="lucide:package"></iconify-icon> Asset capture
      <small style="font-weight:normal; color:#888;">— Hub 永続。Fetch / Code / LLM すべてに適用</small>
    </h3>
    <div style="display:grid; grid-template-columns: 280px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label for="setMinAssetSize">最小ファイルサイズ (bytes):</label>
      <div>
        <input type="text" id="setMinAssetSize" inputmode="numeric" placeholder="0 / 1k / 10kb" title="バイト数。1k=1024 / 10kb=10240 のような単位付きでも可。0 = フィルタ無効" style="width:120px; padding:4px 8px;">
        <span style="color:#888; font-size:.85em; margin-left:8px;">
          このサイズ未満の asset は保存しない。<strong>0</strong> = フィルタ無効。
          目安: <code>1024</code>=1KB / <code>4096</code>=4KB / <code>10240</code>=10KB
        </span>
      </div>

      <div></div>
      <div>
        <button id="setSaveAssetBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> Asset capture を保存</button>
        <span id="setAssetErr" style="color:#a00; font-size:.9em; margin-left:8px;"></span>
      </div>
    </div>

    <!-- B3. Fetch defaults (hub-side, persisted) -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;"><iconify-icon icon="lucide:file-down"></iconify-icon> Fetch defaults
      <small style="font-weight:normal; color:#888;">— Hub 永続。Fetch mode のジョブで client が明示してない項目に自動適用</small>
    </h3>
    <div style="display:grid; grid-template-columns: 240px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <div style="grid-column: 1 / -1; color:#555; font-size:.85em; font-weight:600; margin-top:4px;">▸ Wait timing</div>

      <label for="setFetchWait" title="document.readyState=complete までの最大待ち秒数。タイムアウトしても処理は続く。">wait_seconds:</label>
      <div><input type="number" id="setFetchWait" min="0" max="600" style="width:90px; padding:4px 8px;"> 秒 <small style="color:#888;">(readyState=complete までの上限)</small></div>

      <label for="setFetchSettle" title="readyState=complete 後、idle 判定の前に追加で待つ秒数。重い遅延ロードに有効。">settle_seconds:</label>
      <div><input type="number" id="setFetchSettle" min="0" max="600" step="0.5" style="width:90px; padding:4px 8px;"> 秒 <small style="color:#888;">(ready 後の追加待機)</small></div>

      <label for="setFetchIdle" title="ネットワークがこの秒数以上 idle なら capture 完了とみなす。">idle_seconds:</label>
      <div><input type="number" id="setFetchIdle" min="0" max="600" step="0.5" style="width:90px; padding:4px 8px;"> 秒 <small style="color:#888;">(idle 判定の閾値)</small></div>

      <label for="setFetchMaxWait" title="capture フェーズ全体の上限。idle 待ちが長引いてもここで打ち切る。">max_wait_seconds:</label>
      <div><input type="number" id="setFetchMaxWait" min="0" max="3600" step="1" style="width:90px; padding:4px 8px;"> 秒 <small style="color:#888;">(capture 全体の上限)</small></div>

      <div style="grid-column: 1 / -1; color:#555; font-size:.85em; font-weight:600; margin-top:8px;">▸ Scroll &amp; video</div>

      <label for="setFetchScroll" title="ページ読み込み後に自動スクロールするか (lazy-load 画像を踏ませる)。">scroll:</label>
      <label style="font-weight:normal;"><input type="checkbox" id="setFetchScroll"> 自動スクロール ON</label>

      <label for="setFetchScrollStep" title="1 ステップで進むピクセル数。">scroll_step:</label>
      <div><input type="number" id="setFetchScrollStep" min="1" max="2000" style="width:90px; padding:4px 8px;"> px</div>

      <label for="setFetchScrollMax" title="スクロールの最大累積ピクセル。長すぎるページの暴走を防ぐ。">scroll_max:</label>
      <div><input type="number" id="setFetchScrollMax" min="0" max="100000" style="width:120px; padding:4px 8px;"> px</div>

      <label for="setFetchScrollEarly" title="readyState 待ちがこの秒数を超え、ページが scrollable なら早期にスクロール開始。">scroll_early_after:</label>
      <div><input type="number" id="setFetchScrollEarly" min="0" max="600" step="0.5" style="width:90px; padding:4px 8px;"> 秒</div>

      <label for="setFetchPostClick" title="動画クリック後に待つ秒数 (プレイヤー反応を待つ)。">post_click_seconds:</label>
      <div><input type="number" id="setFetchPostClick" min="0" max="60" step="0.5" style="width:90px; padding:4px 8px;"> 秒</div>

      <div></div>
      <div>
        <button id="setSaveFetchBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> Fetch defaults を保存</button>
        <button id="setResetFetchBtn" class="pill" style="background:#fee; border-color:#c88; color:#933;"><iconify-icon icon="lucide:rotate-ccw"></iconify-icon> built-in 既定値に戻す</button>
        <span id="setFetchErr" style="color:#a00; font-size:.9em; margin-left:8px;"></span>
      </div>
    </div>

    <!-- B4. Web search (Codegen) — SearXNG-backed LLM tool -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;"><iconify-icon icon="lucide:search"></iconify-icon> Web search (Codegen)
      <small style="font-weight:normal; color:#888;">— Hub 永続。Coder LLM が外部知識を引くツール</small>
    </h3>
    <div style="display:grid; grid-template-columns: 240px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label for="setSearxngUrl" title="SearXNG の HTTP エンドポイント (例: http://searxng.local:8080)。空にするとツール無効。">SearXNG URL:</label>
      <div>
        <input type="text" id="setSearxngUrl" placeholder="http://host:8080 (空 = ツール無効)" style="width:360px; padding:4px 8px;">
        <small style="color:#888; display:block; margin-top:4px;">
          設定すると Coder の LLM (engine.supports_tools=true) に <code>web_search</code> ツールが渡される。
          空にするとツール無効 (この設定で <em>機能 OFF</em> 可能)。
        </small>
      </div>

      <label for="setSearxngTimeout" title="1 回の検索リクエストの timeout 秒数。">Timeout (s):</label>
      <div><input type="number" id="setSearxngTimeout" min="1" max="120" step="1" style="width:90px; padding:4px 8px;"> 秒</div>

      <label for="setWebSearchMaxCalls" title="1 attempt あたり LLM が web_search を呼び出してよい回数の上限。0 = ツール無効。">Max calls / attempt:</label>
      <div>
        <input type="number" id="setWebSearchMaxCalls" min="0" max="20" step="1" style="width:90px; padding:4px 8px;">
        <small style="color:#888; margin-left:8px;">回 (0 = ツール無効)</small>
      </div>

      <div></div>
      <div>
        <button id="setSaveWebSearchBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> Web search を保存</button>
        <span id="setWebSearchErr" style="color:#a00; font-size:.9em; margin-left:8px;"></span>
      </div>
    </div>

    <!-- B5. Storage: SMB / NAS connection settings -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;"><iconify-icon icon="lucide:hard-drive"></iconify-icon> Storage (SMB)
      <small style="font-weight:normal; color:#888;">— Hub 永続。ジョブデータの外部ストレージ</small>
    </h3>
    <p style="margin:4px 0 8px; color:#666; font-size:.88em;">
      SMB/CIFS 共有をマウントしてジョブのアーティファクト (assets, page.html, log.txt) を NAS に保存。
      Hub メタデータ (skills, hosts, engines 等) は常にローカル data_dir に残る。
      未設定ならデフォルトの data_dir をそのまま使用。
    </p>

    <!-- SMB connection status indicator -->
    <div id="smbStatusBanner" style="display:none; padding:8px 14px; border-radius:6px; margin-bottom:10px; font-size:.9em; display:flex; align-items:center; gap:8px;">
    </div>

    <div style="display:grid; grid-template-columns: 200px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label for="setSmbServer" title="SMB サーバーの IP またはホスト名">SMB サーバー:</label>
      <div>
        <input type="text" id="setSmbServer" placeholder="例: 192.168.1.100" style="width:240px; padding:4px 8px;">
      </div>

      <label for="setSmbShare" title="共有名">共有名:</label>
      <div>
        <input type="text" id="setSmbShare" placeholder="例: paprika" style="width:240px; padding:4px 8px;">
      </div>

      <label for="setSmbUsername" title="SMB ユーザー名 (空 = guest)">ユーザー名:</label>
      <div>
        <input type="text" id="setSmbUsername" placeholder="(空 = guest)" style="width:240px; padding:4px 8px;" autocomplete="off">
      </div>

      <label for="setSmbPassword" title="SMB パスワード">パスワード:</label>
      <div>
        <input type="password" id="setSmbPassword" placeholder="" style="width:240px; padding:4px 8px;" autocomplete="off">
        <button type="button" id="setSmbPasswordToggle" class="pill" style="padding:2px 8px; font-size:.8em; margin-left:4px; background:#f5f5fa; border-color:#ccd; color:#555;" title="パスワード表示/非表示">👁</button>
      </div>

      <label for="setSmbMountPoint" title="ローカルのマウントポイントパス">マウントポイント:</label>
      <div>
        <input type="text" id="setSmbMountPoint" value="/mnt/paprika" style="width:300px; padding:4px 8px;">
        <small style="color:#888; display:block; margin-top:2px;">コンテナ内のマウント先パス</small>
      </div>

      <label for="setSmbMountOptions" title="追加の mount -o オプション (例: vers=3.0)">追加オプション:</label>
      <div>
        <input type="text" id="setSmbMountOptions" placeholder="例: vers=3.0" style="width:300px; padding:4px 8px;">
      </div>

      <div></div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:4px;">
        <button id="setSaveSmbBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> 接続設定を保存</button>
        <button id="setSmbMountBtn" class="pill" style="background:#e8f0ff; border-color:#6a8ec7; color:#2a4a8a;"><iconify-icon icon="lucide:plug"></iconify-icon> マウント</button>
        <button id="setSmbUnmountBtn" class="pill" style="background:#fee; border-color:#c88; color:#933;"><iconify-icon icon="lucide:plug-zap"></iconify-icon> アンマウント</button>
        <span id="setSmbErr" style="color:#a00; font-size:.9em; margin-left:4px;"></span>
      </div>

      <!-- Disk usage (populated by JS when mounted) -->
      <div></div>
      <div id="smbDiskUsage" style="font-size:.85em; color:#888; margin-top:4px;"></div>
    </div>

    <!-- B6. Reasoning Judge -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;"><iconify-icon icon="lucide:brain-circuit"></iconify-icon> 推論ジャッジ
      <small style="font-weight:normal; color:#888;">— 高品質な LLM 判定 (DeepSeek R1 / Claude / GPT 等)</small>
    </h3>
    <p style="margin:4px 0 8px; color:#666; font-size:.88em;">
      デフォルトジャッジに加え、推論特化モデルで判定を行う。
      shadow = 両方実行して比較ログ、primary = 推論ジャッジの判定を採用。
    </p>

    <div style="display:grid; grid-template-columns: 200px 1fr; gap:8px 12px; align-items:center; padding:8px 4px;">
      <label for="setReasoningJudgeMode">モード:</label>
      <div>
        <select id="setReasoningJudgeMode" style="padding:4px 8px; min-width:200px;">
          <option value="off">off — 無効</option>
          <option value="shadow">shadow — 比較ログのみ</option>
          <option value="primary">primary — 推論ジャッジ優先</option>
        </select>
      </div>

      <label for="setReasoningJudgeEngine">エンジン:</label>
      <div>
        <select id="setReasoningJudgeEngine" style="padding:4px 8px; min-width:200px;">
          <option value="">(未設定 — env fallback)</option>
        </select>
        <small style="color:#888; display:block; margin-top:2px;">AI エンジンタブで登録済みのエンジンから選択</small>
      </div>

      <div></div>
      <div style="display:flex; gap:8px; align-items:center; margin-top:4px;">
        <button id="setSaveReasoningJudgeBtn" class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"><iconify-icon icon="lucide:save"></iconify-icon> 推論ジャッジを保存</button>
        <span id="setReasoningJudgeStatus" style="font-size:.9em; margin-left:4px;"></span>
      </div>
    </div>

    <!-- C. System info (read-only) -->
    <h3 style="margin-top:1.2rem; font-size:.95rem; color:#444; border-bottom:1px solid #eee; padding-bottom:.3rem;">ℹ System info
      <small style="font-weight:normal; color:#888;">— 環境変数依存、再デプロイで変更</small>
    </h3>
    <table style="font-size:.9em; margin-top:.4rem;">
      <tbody id="setSystemInfoBody">
        <tr><td colspan=2 style="color:#888;">loading…</td></tr>
      </tbody>
    </table>
  </section>
</div>

<div class="panel" data-panel="screens">
  <section>
    <h2><span data-i18n="screens.heading">Live preview</span>
      <span class="hctrl">
        <a href="/screenshots" target="_blank" title="Open standalone page">↗ open in tab</a>
        <span data-i18n="screens.sort">sort</span>
        <select id="ssSort" title="Tile sort order">
          <option value="default" data-i18n="screens.sort.default">default</option>
          <option value="status" selected data-i18n="screens.sort.status">status (Running first)</option>
          <option value="worker" data-i18n="screens.sort.worker">worker ID</option>
          <option value="worker-desc" data-i18n="screens.sort.worker.desc">worker ID (desc)</option>
        </select>
        <span data-i18n="screens.cols">cols</span>
        <select id="ssCols">
          <option value="auto" selected>auto</option>
          <option value="2">2</option>
          <option value="3">3</option>
          <option value="4">4</option>
          <option value="5">5</option>
          <option value="6">6</option>
          <option value="8">8</option>
          <option value="10">10</option>
          <option value="12">12</option>
        </select>
        size
        <select id="ssSize" title="thumbnail width × quality. lower = lighter bandwidth.">
          <option value="240:25">XS (240·q25)</option>
          <option value="320:30" selected>S (320·q30)</option>
          <option value="480:40">M (480·q40)</option>
          <option value="640:50">L (640·q50)</option>
          <option value="960:60">XL (960·q60)</option>
        </select>
        every <input type="number" id="ssInterval" value="5" min="1" max="60"> s
        <label><input type="checkbox" id="ssEnabled" checked> on</label>
      </span>
    </h2>
    <div id="ssGrid" class="ssgrid">
      <div class="empty">no workers connected</div>
    </div>
  </section>
</div>

<!-- Worker detail modal. Lives OUTSIDE all .panel divs so showModal()
     works regardless of which tab is active (panels not active have
     display:none which would silently swallow showModal() calls). -->
<dialog id="workerDetailModal" style="border:1px solid #ccc; border-radius:8px; padding:0; width:min(680px, 92vw); max-height:90vh; overflow:auto;">
  <div style="padding:16px;">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
      <h3 style="margin:0;">
        <iconify-icon icon="lucide:server"></iconify-icon> Worker 詳細
      </h3>
      <div style="display:flex; gap:4px; align-items:center;">
        <button type="button" id="workerDetailShareLink"
                title="このワーカーへの直接リンクをコピー"
                style="border:1px solid #ccd; background:#eef0ff; cursor:pointer; color:#3a5ca8; padding:3px 10px; border-radius:5px; font-size:.82em; white-space:nowrap;">
          <iconify-icon icon="lucide:link"></iconify-icon> リンクをコピー
        </button>
        <button type="button" id="workerDetailRefresh"
                title="再取得"
                style="border:0; background:transparent; cursor:pointer; color:#888; padding:4px 8px;">
          <iconify-icon icon="lucide:refresh-cw"></iconify-icon>
        </button>
      </div>
    </div>
    <div id="workerDetailBody">
      <div style="color:#888; padding:8px 0;">
        <iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> loading…
      </div>
    </div>
    <div style="display:flex; justify-content:flex-end; margin-top:14px;">
      <button type="button" id="workerDetailClose">閉じる</button>
    </div>
  </div>
</dialog>

</main>
<script src="/static/admin.js?v=@@PAPRIKA_VERSION@@" defer></script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def admin_ui() -> HTMLResponse:
    # JS / CSS are served from /static (mounted above) and tagged with
    # ``?v={hub_version}`` so a fresh deploy invalidates browser caches
    # without us having to fight ETags. The shell HTML itself is small
    # enough that no-cache on it is cheap and avoids stale-version-tag
    # foot-guns.
    html = _ADMIN_HTML.replace("@@PAPRIKA_VERSION@@", _hub_version())
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# ----------------------------------------------------------------------------
# /screenshots — standalone fullscreen-friendly live preview grid
# (URL retained as ``/screenshots`` for the bookmark-compat; the page
# content + endpoints inside it all use the new "preview" naming)
# ----------------------------------------------------------------------------



_SCREENSHOTS_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Paprika · live preview</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: #1b1b1b; color: #ddd; font: 14px/1.5 -apple-system,"Segoe UI",sans-serif; }
  header {
    display: flex; align-items: center; gap: 1.2rem;
    padding: .7rem 1.2rem;
    background: #c0392b; color: #fff;
    position: sticky; top: 0; z-index: 10;
    box-shadow: 0 2px 6px rgba(0,0,0,.4);
  }
  header h1 { margin: 0; font-size: 1.05rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.5rem; }
  header h1 .logo { width: 1.5em; height: 1.5em; vertical-align: middle; flex-shrink: 0; }
  header h1 small { font-weight: 400; opacity: .8; margin-left: .4rem; }
  .ctrl { display: flex; align-items: center; gap: .8rem; margin-left: auto; font-size: .85rem; }
  .ctrl label { display: flex; align-items: center; gap: .35rem; }
  .ctrl input[type=number] {
    width: 56px; padding: 2px 6px;
    background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.35);
    border-radius: 4px; color: #fff; font: inherit;
  }
  .ctrl select {
    padding: 2px 6px;
    background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.35);
    border-radius: 4px; color: #fff; font: inherit;
  }
  .ctrl select option { color: #222; }
  .ctrl a { color: #ffe; text-decoration: none; opacity: .85; }
  .ctrl a:hover { opacity: 1; text-decoration: underline; }
  main { padding: 1rem; }
  #status { font-size: .82rem; opacity: .8; margin-bottom: .8rem; }
  .grid {
    display: grid;
    grid-template-columns: var(--ss-cols, repeat(auto-fill, minmax(var(--tile-min, 380px), 1fr)));
    gap: .8rem;
  }
  .tile {
    background: #000; border-radius: 8px; overflow: hidden;
    position: relative; aspect-ratio: 16/9;
    box-shadow: 0 4px 12px rgba(0,0,0,.5);
    display: block; text-decoration: none; color: inherit;
    transition: transform .15s, box-shadow .15s, outline-color .15s;
    outline: 2px solid transparent;
  }
  a.tile { cursor: pointer; }
  a.tile:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,.6); outline-color: #c0392b; }
  a.tile:hover .open { opacity: 1; }
  .tile img { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
  .tile .lbl {
    position: absolute; top: 6px; left: 8px;
    font-size: .78rem; padding: 2px 8px;
    background: rgba(0,0,0,.6); color: #fff; border-radius: 4px;
    backdrop-filter: blur(2px); pointer-events: none;
  }
  .tile .open {
    position: absolute; top: 6px; right: 8px;
    font-size: .72rem; padding: 2px 8px;
    background: rgba(192,57,43,.85); color: #fff; border-radius: 4px;
    opacity: 0; transition: opacity .15s; pointer-events: none;
  }
  .tile .err {
    position: absolute; bottom: 8px; left: 8px; right: 8px;
    font-size: .76rem; color: #ffb4b4;
    padding: 4px 8px; background: rgba(120,0,0,.75); border-radius: 4px;
    pointer-events: none;
  }
  .tile.busy { outline-color: #c0392b; box-shadow: 0 0 0 1px rgba(192,57,43,.45), 0 0 14px rgba(192,57,43,.4); }
  .tile.idle { opacity: .78; }
  .tile .badge {
    position: absolute; bottom: 8px; right: 8px;
    font-size: .72rem; font-weight: 600; color: #fff;
    padding: 2px 9px; border-radius: 10px;
    display: flex; align-items: center; gap: 5px;
    pointer-events: none;
  }
  .tile .badge.running   { background: rgba(192,57,43,.85); }
  .tile .badge.keepalive { background: rgba(217,127,38,.9); }
  .tile .badge.idle      { background: rgba(80,80,90,.7); }
  .tile .badge .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: currentColor; display: inline-block;
  }
  .tile .badge.running .dot   { animation: paprikaSsPulse2 1.2s infinite; }
  .tile .badge.keepalive .dot { animation: paprikaSsPulse2 2.4s infinite; }
  @keyframes paprikaSsPulse2 { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
  /* Loading overlay: striped backdrop + spinner. Shown on first
     image load only (class is removed by JS in the 'load' / 'error'
     handlers below). Polling refreshes do not re-add the class so
     subsequent updates silently swap pixels without flicker. */
  .tile.loading::before {
    content: '';
    position: absolute; inset: 0;
    background: linear-gradient(135deg, #1a1a22 25%, #222 25%, #222 50%, #1a1a22 50%, #1a1a22 75%, #222 75%);
    background-size: 18px 18px;
    z-index: 1;
  }
  .tile.loading::after {
    content: '';
    position: absolute;
    top: 50%; left: 50%;
    width: 34px; height: 34px;
    margin: -17px 0 0 -17px;
    border: 3px solid rgba(255,255,255,.15);
    border-top-color: #c0392b;
    border-radius: 50%;
    animation: paprikaSsLoad2 0.8s linear infinite;
    z-index: 2;
    pointer-events: none;
  }
  @keyframes paprikaSsLoad2 { to { transform: rotate(360deg); } }
  .tile.loading .open { display: none; }
  .tile .sub {
    position: absolute; top: 28px; left: 8px; right: 8px;
    font-size: .68rem; color: #fff;
    padding: 1px 6px; background: rgba(0,0,0,.55);
    border-radius: 3px; pointer-events: none;
    text-overflow: ellipsis; overflow: hidden; white-space: nowrap;
  }
  .empty { padding: 2rem; text-align: center; opacity: .6; }
</style>
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:8px;" title="ホーム (Submit form) に戻る"><img src="/icon.svg" alt="paprika" class="logo"> Paprika</a> <small>live preview</small></h1>
  <span class="ctrl">
    <label>every <input type="number" id="ssInterval" value="5" min="1" max="60"> s</label>
    <label><input type="checkbox" id="ssEnabled" checked> on</label>
    <label>size
      <select id="ssSize">
        <option value="180">XS</option>
        <option value="280">S</option>
        <option value="380" selected>M</option>
        <option value="520">L</option>
        <option value="720">XL</option>
      </select>
    </label>
    <label>cols
      <select id="ssCols">
        <option value="auto" selected>auto</option>
        <option value="2">2</option>
        <option value="3">3</option>
        <option value="4">4</option>
        <option value="5">5</option>
        <option value="6">6</option>
        <option value="8">8</option>
        <option value="10">10</option>
        <option value="12">12</option>
      </select>
    </label>
    <a href="/" title="back to admin UI">← admin</a>
  </span>
</header>
<main>
  <div id="status">connecting…</div>
  <div id="grid" class="grid"></div>
</main>
<script>
const ssTiles = new Map();
let ssTimer = null;

function ssKey(w, s) { return w + '/' + s; }

async function syncGrid() {
  let data;
  let jobsData = [];
  try {
    const [r1, r2] = await Promise.all([
      fetch('/workers'),
      fetch('/jobs?limit=200'),
    ]);
    data = await r1.json();
    const jResp = await r2.json().catch(() => ({}));
    jobsData = (jResp && jResp.jobs) || [];
  } catch (e) {
    document.getElementById('status').textContent = 'fetch failed: ' + e.message;
    return;
  }
  const workers = data.workers || [];
  // Build "busy" lookup: ``worker_id|lane`` -> running job. The
  // standalone page mirrors the in-app Live preview panel; both
  // surfaces drive the RUNNING / IDLE badge from the same data.
  const busy = new Map();
  for (const j of jobsData) {
    if (j.status !== 'running') continue;
    if (j.worker_id == null || j.lane_idx == null) continue;
    busy.set(ssKey(j.worker_id, j.lane_idx), j);
  }
  const busyCount = busy.size;
  document.getElementById('status').textContent =
    `${workers.length} worker(s) · ${[...ssTiles.keys()].length} tile(s) · ${busyCount} running`;
  const want = new Set();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    for (let i = 0; i < cap; i++) want.add(ssKey(w.worker_id, i));
  }
  for (const [k, t] of [...ssTiles.entries()]) {
    if (!want.has(k)) { t.wrap.remove(); ssTiles.delete(k); }
  }
  const grid = document.getElementById('grid');
  if (want.size === 0 && ssTiles.size === 0) {
    grid.innerHTML = '<div class="empty">no workers connected</div>';
    return;
  }
  const ph = grid.querySelector('.empty');
  if (ph) ph.remove();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    const urls = w.lane_novnc_urls || w.slot_novnc_urls || [];
    for (let i = 0; i < cap; i++) {
      const key = ssKey(w.worker_id, i);
      if (ssTiles.has(key)) continue;
      const novncUrl = urls[i];
      const wrap = document.createElement(novncUrl ? 'a' : 'div');
      // Same as the in-app Live preview tile: start in 'loading'
      // state, drop the class on first 'load' / 'error'. CSS overlay
      // shows a diagonal stripe + spinner so the tile doesn't look
      // broken during the 1-2 s cold-start fetch.
      wrap.className = 'tile loading';
      if (novncUrl) {
        let u = novncUrl;
        if (!u.includes('autoconnect')) {
          u += (u.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
        }
        wrap.href = u; wrap.target = '_blank'; wrap.rel = 'noopener';
        wrap.title = 'Open noVNC viewer in a new tab';
      }
      const img = document.createElement('img');
      img.alt = key; img.loading = 'lazy';
      const lbl = document.createElement('span');
      lbl.className = 'lbl';
      lbl.textContent = w.worker_id + ' #' + i;
      const sub = document.createElement('span');
      sub.className = 'sub';
      sub.style.display = 'none';
      const badge = document.createElement('span');
      badge.className = 'badge idle';
      badge.innerHTML = '<span class="dot"></span><span class="badge-text">IDLE</span>';
      const open = document.createElement('span');
      open.className = 'open'; open.textContent = '↗ noVNC';
      const err = document.createElement('span');
      err.className = 'err'; err.style.display = 'none';
      wrap.appendChild(img); wrap.appendChild(lbl); wrap.appendChild(sub);
      if (novncUrl) wrap.appendChild(open);
      wrap.appendChild(badge);
      wrap.appendChild(err);
      wrap.classList.add('idle');
      grid.appendChild(wrap);
      img.addEventListener('error', () => {
        err.textContent = 'capture failed (worker offline or lane not ready)';
        err.style.display = 'block';
        wrap.classList.remove('loading');
      });
      img.addEventListener('load', () => {
        err.style.display = 'none';
        wrap.classList.remove('loading');
      });
      ssTiles.set(key, { wrap, img, err, sub, badge });
    }
  }
  // Flip RUNNING / KEEPALIVE / IDLE for every tile based on the
  // jobs snapshot. KEEPALIVE = crawl finished but session is alive
  // for the operator (= keep_session Fetch jobs + post-detach
  // codegen-loop sessions).
  for (const [key, tile] of ssTiles) {
    const job = busy.get(key);
    if (job) {
      const isKeepalive = !!(
        job.progress && job.progress.phase === 'keepalive'
      );
      tile.wrap.classList.add('busy');
      tile.wrap.classList.remove('idle');
      tile.badge.className = isKeepalive ? 'badge keepalive' : 'badge running';
      const txt = tile.badge.querySelector('.badge-text');
      if (txt) txt.textContent = isKeepalive ? 'KEEPALIVE' : 'RUNNING';
      if (tile.sub) {
        tile.sub.textContent = job.url || `(job ${job.job_id})`;
        tile.sub.style.display = '';
      }
    } else {
      tile.wrap.classList.add('idle');
      tile.wrap.classList.remove('busy');
      tile.badge.className = 'badge idle';
      const txt = tile.badge.querySelector('.badge-text');
      if (txt) txt.textContent = 'IDLE';
      if (tile.sub) { tile.sub.textContent = ''; tile.sub.style.display = 'none'; }
    }
  }
}

function refreshImages() {
  if (!document.getElementById('ssEnabled').checked) return;
  const t = Date.now();
  // Match the size shown in the grid so we don't ship more pixels than
  // we'll display. The browser still scales to the tile, but smaller
  // requests = less ffmpeg work + smaller JPEG over the wire.
  const w = parseInt(document.getElementById('ssSize').value, 10) || 380;
  const px = Math.min(1920, Math.max(160, w * 2));  // 2x for crisp on hi-dpi
  // Pair the larger pixel size with mid-range JPEG quality. Standalone
  // monitor view is "operator wants to read the screen" so we trade
  // a bit more bandwidth for less compression artefacts vs the inline
  // tile grid (quality=30).
  const q = 45;
  for (const [key, tile] of ssTiles) {
    if (tile._loading) continue;
    const [wid, lane] = key.split('/');
    const url =
      `/workers/${encodeURIComponent(wid)}/lanes/${encodeURIComponent(lane)}/preview`
      + `?width=${px}&quality=${q}&t=${t}`;
    // Double-buffer: preload off-screen, swap only after decode.
    const probe = new Image();
    tile._loading = true;
    probe.onload = () => {
      tile.img.src = probe.src;
      tile._loading = false;
    };
    probe.onerror = () => {
      tile.img.src = url;
      tile._loading = false;
    };
    probe.src = url;
  }
}

function resetTimer() {
  if (ssTimer) clearInterval(ssTimer);
  const sec = Math.max(1, parseInt(document.getElementById('ssInterval').value, 10) || 5);
  ssTimer = setInterval(refreshImages, sec * 1000);
  refreshImages();
}

function applySize() {
  const w = parseInt(document.getElementById('ssSize').value, 10) || 380;
  document.documentElement.style.setProperty('--tile-min', w + 'px');
}
function applyCols() {
  const v = document.getElementById('ssCols').value;
  const grid = document.getElementById('grid');
  if (v === 'auto') {
    grid.style.removeProperty('--ss-cols');
  } else {
    grid.style.setProperty('--ss-cols', `repeat(${parseInt(v,10)}, 1fr)`);
  }
}

document.getElementById('ssInterval').addEventListener('change', resetTimer);
document.getElementById('ssEnabled').addEventListener('change', () => {
  if (document.getElementById('ssEnabled').checked) resetTimer();
  else if (ssTimer) { clearInterval(ssTimer); ssTimer = null; }
});
document.getElementById('ssSize').addEventListener('change', () => {
  applySize(); refreshImages();
});
document.getElementById('ssCols').addEventListener('change', () => {
  applyCols(); refreshImages();
});

applySize();
applyCols();
syncGrid().then(resetTimer);
setInterval(syncGrid, 5000);
</script>
</body>
</html>
"""


@router.get("/screenshots", response_class=HTMLResponse)
async def screenshots_page() -> str:
    return _SCREENSHOTS_HTML

