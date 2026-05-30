function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// --- i18n (i18next) --------------------------------------------------------
//
// Client-side translation using i18next (loaded from CDN in <head>).
// Resources are embedded inline below — no extra fetches at startup,
// no build step. JP is the default + fallback; missing EN keys fall
// back to JP automatically (so we can add EN incrementally).
//
// HTML elements opt in via data attributes:
//   <span data-i18n="tab.submit">Submit</span>
//   <input data-i18n-placeholder="form.url.placeholder" ...>
//   <button data-i18n-title="engines.add.title" ...>...</button>
//
// The initial HTML carries the JP text as fallback so the page is
// usable even if i18next fails to load (CDN down, etc.).
const I18N_RESOURCES = {
  ja: { translation: {
    // top-bar
    "header.manual":   "マニュアル",
    // tabs
    "tab.submit":      "実行",
    "tab.workers":     "ワーカー",
    "tab.jobs":        "最近のジョブ",
    "tab.presets":     "定義済みジョブ",
    "tab.screens":     "ライブプレビュー",
    "screens.heading": "ライブプレビュー",
    "screens.sort":         "並べ替え",
    "screens.sort.default": "デフォルト",
    "screens.sort.status":  "ステータス (実行中が先)",
    "screens.sort.worker":  "ワーカーID",
    "screens.sort.worker.desc": "ワーカーID (降順)",
    "screens.cols":         "列数",
    "tab.more":        "その他",
    "tab.sessions":    "セッション",
    "tab.hosts":       "ホスト",
    "tab.profiles":    "Chrome設定",
    "tab.extensions":  "Chrome 拡張機能",
    "tab.engines":     "AI エンジン",
    "tab.knowledge":   "AI ナレッジ",
    "tab.plugins":     "プラグイン",
    "tab.settings":    "設定",
    // Submit bar
    "submit.preset":       "プリセット:",
    "submit.preset.none":  "(未選択 — 定義済みジョブ タブから選択)",
    "submit.saveas":       "保存",
    "submit.overwrite":    "上書き保存",
    "submit.edithost":     "ホスト編集",
    "submit.dedup":        "重複除外",
    // Workers panel
    "workers.th.id":       "ワーカーID",
    "workers.th.address":  "IPアドレス",
    "workers.th.status":   "ステータス",
    "workers.th.load":     "負荷",
    "workers.th.profiles": "プロファイル",
    "workers.th.version":  "バージョン",
    "workers.th.labels":   "ラベル",
    "workers.th.actions":  "操作",
    "workers.empty":       "接続中のワーカーなし",
    // Jobs panel
    "jobs.cleanup":      "古いジョブを削除…",
    "jobs.deleteall":    "すべて削除",
    "jobs.cols":         "列",
    "jobs.th.id":        "ID",
    "jobs.th.mode":      "モード",
    "jobs.th.status":    "ステータス",
    "jobs.th.worker":    "ワーカー/レーン",
    "jobs.th.started":   "開始",
    "jobs.th.ended":     "終了",
    "jobs.th.duration":  "所要",
    "jobs.th.actions":   "操作",
    "jobs.empty":        "ジョブなし",
    // Sessions panel
    "sessions.open":       "セッション開始",
    "sessions.closeall":   "すべて閉じる",
    "sessions.th.id":      "セッションID",
    "sessions.th.state":   "状態",
    "sessions.th.worker":  "ワーカー / レーン",
    "sessions.th.url":     "初期URL",
    "sessions.th.active":  "最終アクティブ",
    "sessions.th.visits":  "訪問数",
    "sessions.th.actions": "操作",
    "sessions.empty":      "アクティブなセッションなし",
    // Hosts panel
    "hosts.search.placeholder": "host / notes で絞り込み",
    "hosts.add":           "ホスト追加",
    "hosts.refresh":       "更新",
    "hosts.th.host":       "ホスト",
    "hosts.th.cookies":    "Cookie",
    "hosts.th.dedup":      "重複除外",
    "hosts.th.notes":      "メモ",
    "hosts.th.updated":    "更新日",
    "hosts.th.lastused":   "最終使用",
    "hosts.th.actions":    "操作",
    "hosts.empty":         "ホスト未登録",
    // Presets panel
    "presets.search.placeholder": "🔍 名前 / カテゴリ / URL で検索 …",
    "presets.category.all": "(すべてのカテゴリ)",
    "presets.refresh":      "更新",
    "presets.th.name":      "名前",
    "presets.th.category":  "カテゴリ",
    "presets.th.mode":      "モード",
    "presets.th.updated":   "更新日",
    "presets.th.lastused":  "最終使用",
    "presets.th.actions":   "操作",
    "presets.empty":        "プリセットなし — Submit フォームから保存",
    // AI Engines panel
    "engines.heading":       "AI エンジン",
    "engines.add":           "エンジン追加",
    "engines.add.title":     "新規エンジン登録",
    "engines.refresh":       "更新",
    "engines.refresh.title": "一覧を再取得",
    "engines.save":          "保存",
    "engines.test":          "接続テスト",
    "engines.delete":        "削除",
    "engines.detail.empty":  "左のリストから 1 つ選ぶか、上部の エンジン追加 で新規追加してください。",
    // Chrome Profiles panel
    "profiles.heading":       "Chrome プロファイル",
    "profiles.refresh":       "更新",
    "profiles.refresh.title": "一覧を再取得",
    "profiles.drop.title":    "アーカイブをここにドラッグ&ドロップ",
    "profiles.drop.hint":     "または クリックでファイル選択 ・ サイズ上限 500 MB ・ .tar.gz / .zip どちらも OK (ZIP は自動で tar.gz に変換)",
    "profiles.name.label":    "名前:",
    "profiles.upload":        "アップロード",
    "profiles.cancel":        "キャンセル",
    "profiles.empty":         "プロファイル未登録",
    "profiles.howto.title":   "使い方",
    // Extensions panel
    "extensions.upload":      "アップロード",
    "extensions.refresh":     "更新",
    "extensions.th.name":     "名前",
    "extensions.th.version":  "バージョン",
    "extensions.th.size":     "サイズ",
    "extensions.th.updated":  "更新日",
    "extensions.th.enabled":  "有効",
    "extensions.th.actions":  "操作",
    "extensions.empty":       "拡張機能なし — upload から追加",
    // Submit form
    "submit.heading":         "ジョブの実行",
    "submit.subtab.form":     "ジョブの実行",
    "submit.subtab.live":     "Live",
    "submit.btn":             "▶ 実行",
    "submit.clear":           "クリア",
    "submit.clear.title":     "URL / Goal / Code 入力欄をクリアする",
    "submit.mode.label":      "実行モード",
    "submit.mode.hint":       "(クリックで切替。これがそのまま「実行 / 保存」する種類になります)",
    "submit.mode.current":    "選択中:",
    "submit.mode.fetch":      "Fetch",
    "submit.mode.fetch.desc": "単発ページ取得 (scroll + 動画自動)。LLM 不使用、最速。",
    "submit.mode.llm":        "LLM (コード生成)",
    "submit.mode.llm.desc":   "Goal を LLM に渡してスクリプト自動生成 → sandbox 実行 → 失敗時 retry。",
    "submit.mode.macro":      "Macro",
    "submit.mode.macro.desc": "UI でアクションを順に並べる簡易マクロ。LLM 不要で即実行。",
    "submit.mode.code":       "Code",
    "submit.mode.code.desc":  "Python スクリプトを直接貼り付けて 1 回だけ実行。",
    // Fetch options
    "fetch.heading":          "Fetch オプション",
    "fetch.heading.hint":     "(展開して個別チューニング)",
    "fetch.scroll":           "スクロール",
    "fetch.scroll.title":     "ページを最後までスクロールして遅延読み込みのアセットを拾う。",
    "fetch.downloadvideo":    "動画をダウンロード",
    "fetch.downloadvideo.title":"iframe + ネスト iframe の通信トレースを ON にし、yt-dlp で動画をダウンロード。OFF なら動画 DL ロジックは完全休眠。",
    "fetch.headless":         "ヘッドレス",
    "fetch.headless.title":   "画面を出さずに実行 (Chrome --headless)。",
    "fetch.capture":          "アセットを保存",
    "fetch.capture.title":    "拾ったアセットをサーバ側に保存する。OFF なら HTML/Links のみ。",
    "fetch.keepsession":      "セッションを継続",
    "fetch.keepsession.title":"クロール後もセッションを閉じずに残す。",
    "fetch.wait":             "ページ読み込み待ち",
    "fetch.wait.title":       "body 要素の出現を待つ秒数。",
    "fetch.idle":             "ネットワーク無通信",
    "fetch.idle.title":       "ネットワークが N 秒アイドルで落ち着いたと見なす。",
    "fetch.maxwait":          "最大待ち時間",
    "fetch.maxwait.title":    "ページに費やす最大秒数。",
    "fetch.scrollmax":        "スクロール上限",
    "fetch.scrollmax.title":  "スクロールのピクセル数上限。",
    "fetch.postclick":        "クリック後の待ち",
    "fetch.postclick.title":  "クリック/再生後に追加で待つ秒数。",
    "fetch.minsize":          "最小ファイルサイズ",
    "fetch.minsize.title":    "このバイト数より小さいアセットは除外。",
    "fetch.unit.sec":         "秒",
    "fetch.referer":          "リファラー",
    "fetch.referer.title":    "Referer ヘッダ。",
    "fetch.attach":           "ジョブに接続",
    "fetch.attach.title":     "既存 job を再利用してログイン状態を引き継ぐ。",
    "fetch.attach.placeholder":"job_id (任意, ログイン継続)",
    // AI / LLM options
    "ai.goal.placeholder":    "(空 → デフォの「サイト全体をクロール…」が使われる)",
    "ai.maxattempts":         "最大試行回数",
    "ai.unit.times":          "回",
    "ai.timeout":             "1 試行のタイムアウト",
    "ai.timeout.title":       "1 試行あたりの実行制限時間。",
    "ai.engine":              "コード生成 LLM:",
    "ai.engine.note":         "※ planner / coder / judge 用",
    "ai.hostdedup":           "既訪問URLをスキップ (host_dedup)",
    "ai.hostdedup.title":     "既訪問URLをスキップ (cron 等で日次再クロール時に有効)。",
    "ai.engine.info":         "上の「コード生成 LLM」はスクリプトを書くためのモデル選択です。スクリプト実行中に page.agent() が使う Vision agent は worker 側で固定です。ここで変更しても挙動は変わりません。",
    // Macro options
    "macro.hint":             "行を増やして順に並べる → 自動で paprika-client スクリプトに変換 → 実行",
    "macro.preview":          "プレビュー",
    "macro.preview.title":    "生成される Python スクリプトをプレビュー",
    "macro.clear":            "クリア",
    "macro.clear.title":      "macro をすべて削除",
    "macro.addstep":          "ステップ追加",
    "macro.addstep.title":    "アクション 1 行を末尾に追加",
    "macro.addloop":          "ループ追加",
    "macro.addloop.title":    "Loop と End loop のペアを末尾に追加",
    "macro.addifcss.title":   "If (CSS) / End if を追加",
    "macro.addifagent.title": "If (Agent) / End if を追加",
    "macro.timeout":          "実行タイムアウト",
    "macro.timeout.title":    "macro 全体の実行時間上限。",
    // Code options
    "code.hint":              "Tab で 4 スペース / mode=rerun で実行",
    "code.template":          "📄 テンプレ",
    "code.template.title":    "テンプレ挿入",
    "code.urlopt":            "URL は省略可",
    "code.urlopt.title":      "Code mode は URL を表示用にのみ使用。空 OK。",
    "code.timeout":           "タイムアウト",
    "code.hostdedup":         "既訪問URLをスキップ (host_dedup) — 参考表示",
    "code.hostdedup.title":   "参考表示。Code mode では script 内で直接指定。",
    // Live job panel
    "ljp.pause":              "一時停止",
    "ljp.pause.title":        "ジョブを一時停止する",
    "ljp.resume":             "再開",
    "ljp.resume.title":       "直前のスクリプトで再実行する",
    "ljp.refresh":            "情報の更新",
    "ljp.refresh.title":      "ブラウザの現在状態からアセット/リンクを取り込み直して最新化",
    "ljp.video.title":        "yt-dlp で動画をダウンロード",
    "ljp.video":              "動画",
    "ljp.screenshots":        "スクリーンショット一覧",
    // noVNC per-session header (戻る / 進む / reload / popup / fit / open / screenshot)
    "ljp.vnc.back":           "戻る",
    "ljp.vnc.back.title":     "戻る (記録)",
    "ljp.vnc.fwd":            "進む",
    "ljp.vnc.fwd.title":      "進む (記録)",
    "ljp.vnc.reload.title":   "このフレームを再読み込み",
    "ljp.vnc.go.title":       "URL へ移動",
    "ljp.vnc.popups":         "popup",
    "ljp.vnc.popups.title":   "広告などのポップアップ・別タブを閉じる (記録)",
    "ljp.vnc.screenshot.title":"現在のフレームを保存",
    "ljp.vnc.fit":            "fit",
    "ljp.vnc.fit.title":      "Chrome のウィンドウサイズを現在の zoom 設定に再同期する",
    "ljp.vnc.open":           "open",
    "ljp.vnc.open.title":     "新しいタブで開く",
    "ljp.tab.log":            "ログ",
    "ljp.tab.screenshot":     "スクリーンショット",
    "ljp.tab.links":          "リンク",
    "ljp.tab.code":           "コード",
    "ljp.tab.gallery":        "ギャラリー",
    "ljp.result":             "結果",
    "ljp.pagehtml":           "取得 HTML",
    "ljp.pagehtml.title":     "クロール時点の DOM スナップショット (page.html) を別タブで開く",
    "ljp.logtab":             "ログ (別タブ)",
    "ljp.savepreset":         "プリセット保存",
    "ljp.savepreset.title":   "このジョブを preset として保存",
    "ljp.forensics":          "Forensics 調査",
    "ljp.forensics.title":    "LLM 読み取り専用プローブでページを解析する",
    "ljp.more":               "その他",
    "ljp.more.title":         "その他の操作",
    "ljp.close":              "閉じる",
    "ljp.network":            "ネットワーク",
    "ljp.network.heading":    "ネットワーク",
    "ljp.network.refresh":    "更新",
    "ljp.network.hideSaved":  "保存済みを隠す",
    "ljp.network.th.mime":    "MIME",
    "ljp.network.th.size":    "サイズ",
    "ljp.network.th.url":     "URL",
    "ljp.network.th.status":  "状態",
    "ljp.network.th.action":  "操作",
    // Knowledge panel (v2 Phase 5/6 — HostKnowledge visualization)
    "knowledge.subtitle":           "— v2 がホストごとに学習した結果（barriers / content / stats）",
    "knowledge.search.placeholder": "ホストで絞り込み",
    "knowledge.tier.all":           "全 tier",
    "knowledge.refresh":            "再読み込み",
    "knowledge.tile.hosts":         " ホスト",
    "knowledge.tile.barriers":      " barriers 学習済",
    "knowledge.tile.extractions":   " extractions 学習済",
    "knowledge.ai.heading":         "AI Insights",
    "knowledge.ai.sub":             "Phase 3-6 ─ shadow judge と R1 distiller",
    "knowledge.ai.rawapi":          "raw API",
    "knowledge.ai.paired":          "Judge 判定ペア数",
    "knowledge.ai.paired.sub":      "従来 vs R1 シャドウ",
    "knowledge.ai.agree":           "一致率",
    "knowledge.ai.agree.sub":       "高いほど R1 への切替が安全",
    "knowledge.ai.distilled":       "R1 Distiller の最近の更新",
    "knowledge.ai.distilled.sub":   "全ホスト合計",
    "knowledge.ai.r1hosts":         "R1 が学習に貢献したホスト",
    "knowledge.ai.r1hosts.sub":     "provenance: distiller-r1",
    "knowledge.th.host":            "ホスト",
    "knowledge.th.tier":            "信頼度",
    "knowledge.th.jobs":            "ジョブ数",
    "knowledge.th.success":         "成功率",
    "knowledge.th.barriers":        "barriers",
    "knowledge.th.extractions":     "extractions",
    "knowledge.th.updated":         "最終更新",
    "knowledge.th.by":              "更新元",
    "knowledge.empty":              "HostKnowledge はまだありません — ジョブを実行すると学習が始まります",
    // Plugins panel (v2 Phase 7 — Tool Registry visualization)
    "plugins.subtitle":            "— data/tools/installed の Tool Registry（capability ベースで自動起動）",
    "plugins.refresh":             "再読み込み",
    "plugins.tile.installed":      " インストール済",
    "plugins.tile.available":      " カタログ登録",
    "plugins.tile.invocations":    " 呼び出し記録",
    "plugins.tile.successrate":    " 成功率",
    "plugins.th.status":           "状態",
    "plugins.th.category":         "カテゴリ",
    "plugins.th.summary":          "説明",
    "plugins.status.installed":    "✓ インストール済",
    "plugins.status.available":    "未インストール",
    "plugins.status.localonly":    "ローカルのみ",
    "plugins.empty":               "カタログが空です — data/tools/catalog.json で広告するプラグインを追加してください",
    "plugins.catalog.howto":       "data/tools/catalog.json を編集して新しいプラグインを追加できます。インストール済プラグインは data/tools/installed/<name>/ に配置されます。",
    "plugins.howto":               "自動起動の仕組み: ジョブ投入時、_consult_host_knowledge がそのホストの HostKnowledge を読み、per_page.barriers.<kind>.suggested_tool が設定されていれば、ここに表示されているプラグインの get_cookies アクションを pre-flight で呼び出し、Worker 配信前に cookies を HostRecord にマージします。失敗時は黙ってフォールバック（ジョブ自体は止めません）。",
    "plugins.th.name":             "名前",
    "plugins.th.version":          "バージョン",
    "plugins.th.kind":             "種別",
    "plugins.th.capabilities":     "機能 (capabilities)",
    "plugins.th.actions":          "アクション",
    "plugins.th.lastinvoked":      "最終呼び出し",
    "plugins.th.recent":           "直近成功",
    "plugins.loading":             "読み込み中…",
    "plugins.never":               "未呼び出し",
    "plugins.details":             "詳細",
    "plugins.empty":               "インストール済みのプラグインはありません — data/tools/installed/ に配置してください",
    "plugins.invocations.heading": "履歴",
    "plugins.invocations.sub":     "pre-flight / 手動 / ジョブ起動 すべて記録",
    "plugins.inv.deleteall":       "全削除",
    "plugins.inv.deleteall.confirm": "呼び出し履歴をすべて削除します。元に戻せません。よろしいですか？",
    "plugins.pager.prev":          "前へ",
    "plugins.pager.next":          "次へ",
    "plugins.pager.page":          "ページ",
    "plugins.pager.perpage":       "ページあたり",
    "plugins.inv.th.at":           "時刻",
    "plugins.inv.th.plugin":       "プラグイン",
    "plugins.inv.th.action":       "アクション",
    "plugins.inv.th.status":       "結果",
    "plugins.inv.th.elapsed":      "所要時間",
    "plugins.inv.th.hostjob":      "ホスト / ジョブ",
    "plugins.inv.th.trigger":      "呼び出し元",
    "plugins.inv.empty":           "呼び出し履歴はまだありません",
    "plugins.status.ok":           "✓ 成功",
    "plugins.status.fail":         "✗ 失敗",
    "plugins.job":                 "ジョブ",
    "plugins.disabled":            "無効化中",
    "plugins.modal.invoke":        "実行",
    "plugins.modal.running":       "実行中…",
    "plugins.modal.parseerror":    "JSON パースエラー",
    "plugins.modal.error":         "エラー",
    "plugins.modal.capabilities":  "機能 (capabilities)",
    "plugins.modal.actions":       "アクション",
    "plugins.modal.noactions":     "アクションなし",
    "plugins.modal.lastfail":      "直近の失敗",
    "plugins.modal.recent":        "直近の呼び出し履歴",
    "plugins.modal.th.when":       "時刻",
    "plugins.modal.th.action":     "アクション",
    "plugins.modal.th.status":     "結果",
    "plugins.modal.th.elapsed":    "所要時間",
    "plugins.modal.th.trigger":    "呼び出し元",
    "plugins.ago.s":               " 秒前",
    "plugins.ago.m":               " 分前",
    "plugins.ago.h":               " 時間前",
    "plugins.ago.d":               " 日前",
  }},
  en: { translation: {
    // top-bar
    "header.manual":   "Manual",
    // tabs
    "tab.submit":      "Submit",
    "tab.workers":     "Workers",
    "tab.jobs":        "Recent jobs",
    "tab.presets":     "Presets",
    "tab.screens":     "Live preview",
    "screens.heading": "Live preview",
    "screens.sort":         "sort",
    "screens.sort.default": "default",
    "screens.sort.status":  "status (Running first)",
    "screens.sort.worker":  "worker ID",
    "screens.sort.worker.desc": "worker ID (desc)",
    "screens.cols":         "cols",
    "tab.more":        "More",
    "tab.sessions":    "Sessions",
    "tab.hosts":       "Hosts",
    "tab.profiles":    "Chrome Settings",
    "tab.extensions":  "Chrome Extensions",
    "tab.engines":     "AI Engines",
    "tab.knowledge":   "AI Knowledge",
    "tab.plugins":     "Plugins",
    "tab.settings":    "Settings",
    // Submit bar
    "submit.preset":       "Preset:",
    "submit.preset.none":  "(none loaded — pick one from the Presets tab)",
    "submit.saveas":       "save as",
    "submit.overwrite":    "overwrite",
    "submit.edithost":     "Edit host",
    "submit.dedup":        "Dedup",
    // Workers panel
    "workers.th.id":       "worker_id",
    "workers.th.address":  "address",
    "workers.th.status":   "status",
    "workers.th.load":     "load",
    "workers.th.profiles": "profiles",
    "workers.th.version":  "version",
    "workers.th.labels":   "labels",
    "workers.th.actions":  "actions",
    "workers.empty":       "no workers connected",
    // Jobs panel
    "jobs.cleanup":      "cleanup old…",
    "jobs.deleteall":    "delete all",
    "jobs.cols":         "columns",
    "jobs.th.id":        "id",
    "jobs.th.mode":      "mode",
    "jobs.th.status":    "status",
    "jobs.th.worker":    "worker/lane",
    "jobs.th.started":   "started",
    "jobs.th.ended":     "ended",
    "jobs.th.duration":  "duration",
    "jobs.th.actions":   "actions",
    "jobs.empty":        "no jobs yet",
    // Sessions panel
    "sessions.open":       "open session",
    "sessions.closeall":   "close all",
    "sessions.th.id":      "session_id",
    "sessions.th.state":   "state",
    "sessions.th.worker":  "worker / lane",
    "sessions.th.url":     "initial url",
    "sessions.th.active":  "last active",
    "sessions.th.visits":  "visits",
    "sessions.th.actions": "actions",
    "sessions.empty":      "no active sessions",
    // Hosts panel
    "hosts.search.placeholder": "filter by host / notes",
    "hosts.add":           "add host",
    "hosts.refresh":       "refresh",
    "hosts.th.host":       "host",
    "hosts.th.cookies":    "cookies",
    "hosts.th.dedup":      "dedup",
    "hosts.th.notes":      "notes",
    "hosts.th.updated":    "updated",
    "hosts.th.lastused":   "last used",
    "hosts.th.actions":    "actions",
    "hosts.empty":         "no hosts registered",
    // Presets panel
    "presets.search.placeholder": "🔍 search name / category / URL …",
    "presets.category.all": "(all categories)",
    "presets.refresh":      "refresh",
    "presets.th.name":      "Name",
    "presets.th.category":  "Category",
    "presets.th.mode":      "Mode",
    "presets.th.updated":   "Updated",
    "presets.th.lastused":  "Last used",
    "presets.th.actions":   "Actions",
    "presets.empty":        "no presets yet — save one from the Submit form",
    // AI Engines panel
    "engines.heading":       "AI Engines",
    "engines.add":           "add engine",
    "engines.add.title":     "register a new engine",
    "engines.refresh":       "refresh",
    "engines.refresh.title": "reload the list",
    "engines.save":          "Save",
    "engines.test":          "Test connection",
    "engines.delete":        "Delete",
    "engines.detail.empty":  "Pick one from the list or click add engine above.",
    // Chrome Profiles panel
    "profiles.heading":       "Chrome Profiles",
    "profiles.refresh":       "refresh",
    "profiles.refresh.title": "reload the list",
    "profiles.drop.title":    "Drag & drop an archive here",
    "profiles.drop.hint":     "or click to choose a file · max 500 MB · .tar.gz / .zip both OK (ZIP auto-converted to tar.gz)",
    "profiles.name.label":    "Name:",
    "profiles.upload":        "upload",
    "profiles.cancel":        "cancel",
    "profiles.empty":         "no profiles uploaded",
    "profiles.howto.title":   "How to use",
    // Extensions panel
    "extensions.upload":      "upload",
    "extensions.refresh":     "refresh",
    "extensions.th.name":     "Name",
    "extensions.th.version":  "Version",
    "extensions.th.size":     "Size",
    "extensions.th.updated":  "Updated",
    "extensions.th.enabled":  "Enabled",
    "extensions.th.actions":  "Actions",
    "extensions.empty":       "no extensions yet — click upload to add one",
    // Submit form
    "submit.heading":         "Submit a job",
    "submit.subtab.form":     "Submit",
    "submit.subtab.live":     "Live",
    "submit.btn":             "▶ submit",
    "submit.clear":           "clear",
    "submit.clear.title":     "Clear URL / Goal / Code fields",
    "submit.mode.label":      "Mode",
    "submit.mode.hint":       "(click to switch execution mode)",
    "submit.mode.current":    "Selected:",
    "submit.mode.fetch":      "Fetch",
    "submit.mode.fetch.desc": "Single-page fetch (scroll + auto video). No LLM, fastest.",
    "submit.mode.llm":        "LLM (codegen)",
    "submit.mode.llm.desc":   "Give a Goal to the LLM → auto-generate script → sandbox run → retry on failure.",
    "submit.mode.macro":      "Macro",
    "submit.mode.macro.desc": "Line up actions in the UI as a simple macro. No LLM needed.",
    "submit.mode.code":       "Code",
    "submit.mode.code.desc":  "Paste a Python script and run it once.",
    // Fetch options
    "fetch.heading":          "Fetch options",
    "fetch.heading.hint":     "(expand to fine-tune)",
    "fetch.scroll":           "Scroll",
    "fetch.scroll.title":     "Scroll to the bottom to pick up lazy-loaded assets.",
    "fetch.downloadvideo":    "Download video",
    "fetch.downloadvideo.title":"Enable iframe + nested-iframe network tracing and run yt-dlp. OFF keeps the video-DL machinery fully dormant.",
    "fetch.headless":         "Headless",
    "fetch.headless.title":   "Run without a visible window (Chrome --headless).",
    "fetch.capture":          "Save assets",
    "fetch.capture.title":    "Save captured assets server-side. OFF keeps only HTML/Links.",
    "fetch.keepsession":      "Keep session",
    "fetch.keepsession.title":"Keep the browser/session open after crawling.",
    "fetch.wait":             "Page load wait",
    "fetch.wait.title":       "Seconds to wait for body element to appear.",
    "fetch.idle":             "Network idle",
    "fetch.idle.title":       "Seconds of network silence before considering the page settled.",
    "fetch.maxwait":          "Max wait",
    "fetch.maxwait.title":    "Maximum seconds spent on this page.",
    "fetch.scrollmax":        "Scroll limit",
    "fetch.scrollmax.title":  "Maximum scroll pixels.",
    "fetch.postclick":        "Post-click wait",
    "fetch.postclick.title":  "Extra seconds to wait after a click/play.",
    "fetch.minsize":          "Min file size",
    "fetch.minsize.title":    "Assets smaller than this are discarded.",
    "fetch.unit.sec":         "sec",
    "fetch.referer":          "Referer",
    "fetch.referer.title":    "Referer header.",
    "fetch.attach":           "Attach to job",
    "fetch.attach.title":     "Reuse an existing job's lane to carry over login state.",
    "fetch.attach.placeholder":"job_id (optional, carry over login)",
    // AI / LLM options
    "ai.goal.placeholder":    "(empty → default 'crawl entire site…' goal is used)",
    "ai.maxattempts":         "Max attempts",
    "ai.unit.times":          "times",
    "ai.timeout":             "Attempt timeout",
    "ai.timeout.title":       "Execution time limit per attempt.",
    "ai.engine":              "Codegen LLM:",
    "ai.engine.note":         "for planner / coder / judge",
    "ai.hostdedup":           "Skip visited URLs (host_dedup)",
    "ai.hostdedup.title":     "Skip visited URLs (useful for daily re-crawls via cron).",
    "ai.engine.info":         "The Codegen LLM above selects the model that writes scripts. The Vision agent used by page.agent() at runtime is fixed on the worker side.",
    // Macro options
    "macro.hint":             "Add rows in order → auto-converted to a paprika-client script → run",
    "macro.preview":          "preview",
    "macro.preview.title":    "Preview the generated Python script",
    "macro.clear":            "clear",
    "macro.clear.title":      "Delete all macro rows",
    "macro.addstep":          "add step",
    "macro.addstep.title":    "Add one action row at the end",
    "macro.addloop":          "add loop",
    "macro.addloop.title":    "Add a Loop / End loop pair at the end",
    "macro.addifcss.title":   "Add If (CSS) / End if pair",
    "macro.addifagent.title": "Add If (Agent) / End if pair",
    "macro.timeout":          "Execution timeout",
    "macro.timeout.title":    "Time limit for the entire macro run.",
    // Code options
    "code.hint":              "Tab inserts 4 spaces / runs as mode=rerun",
    "code.template":          "📄 template",
    "code.template.title":    "Insert template",
    "code.urlopt":            "URL is optional",
    "code.urlopt.title":      "Code mode uses URL for display only. May be empty.",
    "code.timeout":           "attempt timeout",
    "code.hostdedup":         "Skip visited URLs (host_dedup) — info only",
    "code.hostdedup.title":   "Info only. In Code mode, set it directly in your script.",
    // Live job panel
    "ljp.pause":              "pause",
    "ljp.pause.title":        "Pause this job",
    "ljp.resume":             "resume",
    "ljp.resume.title":       "Re-run from the last attempt's script",
    "ljp.refresh":            "refresh",
    "ljp.refresh.title":      "Re-capture assets/links from current browser state",
    "ljp.video.title":        "Download video via yt-dlp",
    "ljp.video":              "video",
    "ljp.screenshots":        "screenshots",
    // noVNC per-session header (戻る / 進む / reload / popup / fit / open / screenshot)
    "ljp.vnc.back":           "back",
    "ljp.vnc.back.title":     "Back (recorded)",
    "ljp.vnc.fwd":            "fwd",
    "ljp.vnc.fwd.title":      "Forward (recorded)",
    "ljp.vnc.reload.title":   "Reload this frame",
    "ljp.vnc.go.title":       "Go to URL",
    "ljp.vnc.popups":         "popup",
    "ljp.vnc.popups.title":   "Close ad popups / extra tabs (recorded)",
    "ljp.vnc.screenshot.title":"Save the current frame",
    "ljp.vnc.fit":            "fit",
    "ljp.vnc.fit.title":      "Re-sync Chrome window size to the current zoom",
    "ljp.vnc.open":           "open",
    "ljp.vnc.open.title":     "Open in a new tab",
    "ljp.tab.log":            "Log",
    "ljp.tab.screenshot":     "Screenshot",
    "ljp.tab.links":          "Links",
    "ljp.tab.code":           "Code",
    "ljp.tab.gallery":        "Gallery",
    "ljp.result":             "result",
    "ljp.pagehtml":           "page.html",
    "ljp.pagehtml.title":     "Open the captured DOM snapshot (page.html) in a new tab",
    "ljp.logtab":             "log tab",
    "ljp.savepreset":         "save preset",
    "ljp.savepreset.title":   "Save this job as a preset",
    "ljp.forensics":          "Forensics",
    "ljp.forensics.title":    "Investigate this page with a read-only LLM probe loop",
    "ljp.more":               "more",
    "ljp.more.title":         "More actions",
    "ljp.close":              "close",
    "ljp.network":            "Network",
    "ljp.network.heading":    "Network",
    "ljp.network.refresh":    "refresh",
    "ljp.network.hideSaved":  "hide saved",
    "ljp.network.th.mime":    "MIME",
    "ljp.network.th.size":    "Size",
    "ljp.network.th.url":     "URL",
    "ljp.network.th.status":  "Status",
    "ljp.network.th.action":  "Action",
    // Knowledge panel (v2 Phase 5/6)
    "knowledge.subtitle":           "— per-host knowledge v2 has learned (barriers / content / stats)",
    "knowledge.search.placeholder": "filter by host",
    "knowledge.tier.all":           "all tiers",
    "knowledge.refresh":            "refresh",
    "knowledge.tile.hosts":         " hosts",
    "knowledge.tile.barriers":      " barriers learned",
    "knowledge.tile.extractions":   " extractions learned",
    "knowledge.ai.heading":         "AI Insights",
    "knowledge.ai.sub":             "Phase 3-6 ─ shadow judge & R1 distiller",
    "knowledge.ai.rawapi":          "raw API",
    "knowledge.ai.paired":          "Judge verdicts paired",
    "knowledge.ai.paired.sub":      "legacy vs R1 shadow",
    "knowledge.ai.agree":           "Agreement rate",
    "knowledge.ai.agree.sub":       "higher = R1 ready to promote",
    "knowledge.ai.distilled":       "Recent R1 distiller updates",
    "knowledge.ai.distilled.sub":   "across all hosts",
    "knowledge.ai.r1hosts":         "Hosts with R1-learned data",
    "knowledge.ai.r1hosts.sub":     "provenance: distiller-r1",
    "knowledge.th.host":            "host",
    "knowledge.th.tier":            "tier",
    "knowledge.th.jobs":            "jobs",
    "knowledge.th.success":         "success",
    "knowledge.th.barriers":        "barriers",
    "knowledge.th.extractions":     "extractions",
    "knowledge.th.updated":         "last updated",
    "knowledge.th.by":              "by",
    "knowledge.empty":              "no HostKnowledge yet — submit a job to start learning",
    // Plugins panel (v2 Phase 7)
    "plugins.subtitle":            "— Tool Registry under data/tools/installed (capability-based auto-invocation)",
    "plugins.refresh":             "refresh",
    "plugins.tile.installed":      " installed",
    "plugins.tile.available":      " in catalog",
    "plugins.tile.invocations":    " invocations logged",
    "plugins.tile.successrate":    " success rate",
    "plugins.th.status":           "status",
    "plugins.th.category":         "category",
    "plugins.th.summary":          "summary",
    "plugins.status.installed":    "✓ installed",
    "plugins.status.available":    "available",
    "plugins.status.localonly":    "local-only",
    "plugins.empty":               "catalog is empty — edit data/tools/catalog.json to advertise a plugin",
    "plugins.catalog.howto":       "Edit data/tools/catalog.json to advertise additional plugins. Installed plugins live under data/tools/installed/<name>/.",
    "plugins.howto":               "Auto-invocation: when a job is dispatched, _consult_host_knowledge reads the host's HostKnowledge. If per_page.barriers.<kind>.suggested_tool is set, the plugin's get_cookies action is pre-flighted here and the returned cookies are merged into the HostRecord before the Worker dispatch. Failures silently fall through (the job itself is never blocked).",
    "plugins.th.name":             "name",
    "plugins.th.version":          "version",
    "plugins.th.kind":             "kind",
    "plugins.th.capabilities":     "capabilities",
    "plugins.th.actions":          "actions",
    "plugins.th.lastinvoked":      "last invoked",
    "plugins.th.recent":           "recent",
    "plugins.loading":             "loading…",
    "plugins.never":               "never",
    "plugins.details":             "details",
    "plugins.empty":               "no plugins installed — drop one into data/tools/installed/",
    "plugins.invocations.heading": "History",
    "plugins.invocations.sub":     "pre-flight / manual / job-triggered — all recorded",
    "plugins.inv.deleteall":       "delete all",
    "plugins.inv.deleteall.confirm": "Delete the entire invocations audit log? This cannot be undone.",
    "plugins.pager.prev":          "prev",
    "plugins.pager.next":          "next",
    "plugins.pager.page":          "page",
    "plugins.pager.perpage":       "per page",
    "plugins.inv.th.at":           "at",
    "plugins.inv.th.plugin":       "plugin",
    "plugins.inv.th.action":       "action",
    "plugins.inv.th.status":       "status",
    "plugins.inv.th.elapsed":      "elapsed",
    "plugins.inv.th.hostjob":      "host / job",
    "plugins.inv.th.trigger":      "trigger",
    "plugins.inv.empty":           "no invocations yet",
    "plugins.status.ok":           "✓ ok",
    "plugins.status.fail":         "✗ fail",
    "plugins.job":                 "job",
    "plugins.disabled":            "DISABLED",
    "plugins.modal.invoke":        "invoke",
    "plugins.modal.running":       "running…",
    "plugins.modal.parseerror":    "JSON parse error",
    "plugins.modal.error":         "error",
    "plugins.modal.capabilities":  "Capabilities",
    "plugins.modal.actions":       "Actions",
    "plugins.modal.noactions":     "no actions",
    "plugins.modal.lastfail":      "Last failure",
    "plugins.modal.recent":        "Recent invocations",
    "plugins.modal.th.when":       "when",
    "plugins.modal.th.action":     "action",
    "plugins.modal.th.status":     "status",
    "plugins.modal.th.elapsed":    "elapsed",
    "plugins.modal.th.trigger":    "trigger",
    "plugins.ago.s":               "s ago",
    "plugins.ago.m":               "m ago",
    "plugins.ago.h":               "h ago",
    "plugins.ago.d":               "d ago",
  }},
};

// Initial locale: localStorage > browser language > 'en' (default).
// Policy: Japanese browsers see Japanese, everyone else sees English.
// We check the FULL navigator.languages list (not just the primary)
// for any Japanese entry, so a "en-US, ja" multi-language profile
// still gets Japanese. Any explicit operator choice in the header
// switch wins via localStorage.
function _pickInitialLocale() {
  try {
    const stored = localStorage.getItem('paprika.locale');
    if (stored === 'ja' || stored === 'en') return stored;
  } catch (_) {}
  const navs = (navigator.languages && navigator.languages.length)
    ? navigator.languages
    : [navigator.language || ''];
  for (const l of navs) {
    if (((l || '') + '').toLowerCase().startsWith('ja')) return 'ja';
  }
  return 'en';  // default for non-Japanese / unset browsers
}

// ---- Runtime JP->EN fallback dictionary --------------------------------
// The admin markup ships Japanese inline text. Strings that carry a
// data-i18n key are handled by i18next; everything else (older panels,
// help paragraphs, tooltips, placeholders) is swapped at runtime by a
// DOM walk against this exact-match JP->EN map when locale is English.
// Built from the unique user-facing Japanese strings in the main admin
// template; particles / bare units were excluded to avoid mis-matches.
const JP2EN = JSON.parse("{\"# Windows taskkill /F /IM chrome.exe /T # macOS / Linux pkill -f chrome # どの OS でも paprika-client upload-profile --name mydefault --hub http://paprika.lan\": \"# Windows taskkill /F /IM chrome.exe /T # macOS / Linux pkill -f chrome # On any OS paprika-client upload-profile --name mydefault --hub http://paprika.lan\", \"(Login Data / IndexedDB は含まれない)。実用上 90% のサイトはこれで OK。 今後のバージョンで URL 転送、クリップボード共有、ジョブ状態取得などを追加予定。\": \"(Login Data / IndexedDB are not included). In practice this works fine for 90% of sites. Future versions will add URL forwarding, clipboard sharing, job status retrieval, and more.\", \"(capture 全体の上限)\": \"(overall capture cap)\", \"(idle 判定の閾値)\": \"(idle detection threshold)\", \"(ready 後の追加待機)\": \"(extra wait after ready)\", \"(readyState=complete までの上限)\": \"(cap until readyState=complete)\", \"(クリックで切替。これがそのまま「実行 / 保存」する種類になります)\": \"(click to toggle. This becomes the type that is \\\"run / saved\\\")\", \"(デプロイ時に決まるので表示のみ)。\": \"(set at deploy time, display only).\", \"(中身は manifest.json を含む unpacked extension のディレクトリ)。\": \"(the contents are an unpacked extension directory containing manifest.json).\", \"(任意)\": \"(optional)\", \"(任意; 既存の category から候補が出ます)\": \"(optional; suggestions come from existing categories)\", \"(展開して個別チューニング)\": \"(expand to tune individually)\", \"(既訪問URL一覧 / Recrawl patterns) は Hosts タブの\": \"(visited URL list / Recrawl patterns) is in the Hosts tab's\", \"(空 = ハードコードされたデフォを使用)\": \"(empty = use hardcoded default)\", \"(空 → デフォの「サイト全体をクロール…」が使われる)\": \"(empty → the default \\\"crawl the whole site…\\\" is used)\", \"0.3 から hub 側で\": \"since 0.3, on the hub side\", \"1 ステップで進むピクセル数。\": \"Pixels to advance per step.\", \"1 試行あたりの実行制限時間。\": \"Execution time limit per attempt.\", \"1 試行のタイムアウト\": \"Per-attempt timeout\", \": Google プロファイルからアップロードした profile に 含まれる拡張も別経路でロードされますが、Google 側が数時間後にセッションを失効させると 「Sync is paused」状態になり sync 由来の拡張が動かなくなることがあります。ここから アップロードすると Chrome sync に依存しないので、Google ログインに関係なく常時動作します。\": \": Extensions included in a profile uploaded from a Google profile are also loaded via a separate path, but if Google expires the session after a few hours it enters a \\\"Sync is paused\\\" state and sync-sourced extensions may stop working. Uploading from here doesn't depend on Chrome sync, so they work all the time regardless of Google login.\", \"<video> / <iframe> プレイヤーを自動再生して動画 URL を踏ませる。\": \"Auto-play <video> / <iframe> players to trigger video URLs.\", \"= 1 文字。一致 URL は visited 登録済みでも常に再クロール)\": \"= 1 character. Matching URLs are always re-crawled even if already registered as visited)\", \"= operator が promote 済み (高信頼)。次のジョブでは目的に合うものを上位 K 件だけ LLM プロンプトに自動注入されます。 不要なら delete、良ければ\": \"= promoted by operator (high confidence). On the next job, only the top K that fit the goal are auto-injected into the LLM prompt. Delete if unneeded; if good,\", \"= フィルタ無効。 目安:\": \"= filter disabled. Rule of thumb:\", \"= 任意文字列、\": \"= any string,\", \"= 自動抽出 (低信頼)、\": \"= auto-extracted (low confidence),\", \"Asset capture を保存\": \"Save Asset capture\", \"CLI が作るか、Chrome 拡張機能\": \"Created by the CLI, or the Chrome extension\", \"Chrome のウィンドウサイズを現在の zoom 設定に再同期する\": \"Re-sync the Chrome window size to the current zoom setting\", \"Chrome を完全終了 (上記コマンドで)\": \"Fully quit Chrome (with the command above)\", \"Chrome ウィンドウサイズ。100% = 1280×720 (基準)。変更すると Chrome の OS ウィンドウが実際にリサイズされ、iframe もそのまま 1:1 で表示する (CSS 拡大縮小ではない)。\": \"Chrome window size. 100% = 1280×720 (baseline). Changing it actually resizes the Chrome OS window, and iframes are shown 1:1 as-is (not CSS scaling).\", \"Code mode は URL を表示用にのみ使用。空 OK。\": \"Code mode uses the URL for display only. Empty is OK.\", \"Code template (paste into Code mode; URL/selector を PLACEHOLDER に)\": \"Code template (paste into Code mode; put URL/selector in PLACEHOLDER)\", \"Codegen-loop の\": \"Codegen-loop's\", \"DevTools → Application → Cookies の export を貼り付けやすいテンプレを挿入\": \"Insert a template that makes it easy to paste a DevTools → Application → Cookies export\", \"Fetch defaults を保存\": \"Save Fetch defaults\", \"Fetch オプション\": \"Fetch options\", \"Goal を LLM に渡してスクリプト自動生成 → sandbox 実行 → 失敗時 retry。\": \"Pass the Goal to the LLM to auto-generate a script → run in sandbox → retry on failure.\", \"If (Agent) / End if を追加\": \"Add If (Agent) / End if\", \"If (CSS) / End if を追加\": \"Add If (CSS) / End if\", \"LLM (コード生成)\": \"LLM (code generation)\", \"LLM が成功した codegen-loop ジョブから抽出した\": \"Extracted by the LLM from a successful codegen-loop job\", \"Loop と End loop のペアを末尾に追加\": \"Append a Loop / End loop pair\", \"MIME / 拡張子\": \"MIME / extension\", \"Notes (任意)\": \"Notes (optional)\", \"OpenAI 互換\": \"OpenAI-compatible\", \"Python スクリプトを直接貼り付けて 1 回だけ実行。\": \"Paste a Python script directly and run it once.\", \"Referer ヘッダ。\": \"Referer header.\", \"Submit defaults を保存\": \"Save Submit defaults\", \"Submit form に読み込む\": \"Load into Submit form\", \"Submit form の設定スナップショット。UI から\": \"A settings snapshot of the Submit form. From the UI,\", \"Submit デフォルト値 (ブラウザ毎に保存) + Hub 全体の学習動作トグル (永続)。 LLM URL / モデル名 / Worker / Data dir 等は\": \"Submit default values (saved per browser) + hub-wide learning behavior toggles (persistent). LLM URL / model name / Worker / Data dir, etc. are\", \"Tab で 4 スペース / mode=rerun で実行\": \"Tab for 4 spaces / run with mode=rerun\", \"UI でアクションを順に並べる簡易マクロ。LLM 不要で即実行。\": \"A simple macro that lines up actions in order in the UI. No LLM needed, runs instantly.\", \"URL / Goal / Code 入力欄をクリアする\": \"Clear the URL / Goal / Code input fields\", \"URL は省略可\": \"URL is optional\", \"Windows は ZIP が一番楽\": \"ZIP is easiest on Windows\", \"auto (低信頼)\": \"auto (low confidence)\", \"auto (注入されない、レビュー用)\": \"auto (not injected, for review)\", \"built-in 既定値に戻す\": \"Reset to built-in defaults\", \"capture フェーズ全体の上限。idle 待ちが長引いてもここで打ち切る。\": \"Overall cap for the capture phase. Cuts off here even if the idle wait drags on.\", \"category で絞り込み\": \"Filter by category\", \"codegen-loop の成功後に skill を自動抽出\": \"Auto-extract a skill after a successful codegen-loop\", \"cookie だけ\": \"Cookies only\", \"curated (常時注入)\": \"curated (always injected)\", \"curated (高信頼)\": \"curated (high confidence)\", \"document.readyState=complete までの最大待ち秒数。タイムアウトしても処理は続く。\": \"Max seconds to wait until document.readyState=complete. Processing continues even on timeout.\", \"engine=\\\"auto\\\" 時にこの kind の中で優先する\": \"Prefer this within the kind when engine=\\\"auto\\\"\", \"env var の\": \"the env var's\", \"example.com (www. は自動で除去)\": \"example.com (www. auto-stripped)\", \"failure→success diff から convention を自動抽出 (attempts ≥ 2 のとき)\": \"Auto-extract a convention from the failure→success diff (when attempts ≥ 2)\", \"fetch モードのオプション (scroll / play_videos / timing 等) はこのモーダルでは編集できません。\": \"Fetch mode options (scroll / play_videos / timing, etc.) cannot be edited in this modal.\", \"follow — popup を閉じる + ドメイン無関係に main tab を redirect\": \"follow — close the popup + redirect the main tab regardless of domain\", \"host / notes で絞り込み\": \"Filter by host / notes\", \"host-level URL dedup を有効にする (\": \"Enable host-level URL dedup (\", \"import asyncio\\nimport paprika_client as pap\\nfrom paprika_client import async_paprika\\n\\n# connect() の引数は省略 OK → paprika-runner 内では PAPRIKA_HUB env が\\n# 自動で読まれる (= http://hub:8000)。ローカル実行時のみ\\n# os.environ['PAPRIKA_HUB']=http://localhost:8000 をセットしてから走らせる。\\n\\nasync def main():\\n    async with async_paprika.connect() as cli:\\n        async with cli.session(initial_url='https://example.com/') as page:\\n            async for visit in pap.walk(page, target_pages=10):\\n                print(f'[{visit.n}/{visit.target}] {visit.url}')\\n\\nasyncio.run(main())\": \"import asyncio\\nimport paprika_client as pap\\nfrom paprika_client import async_paprika\\n\\n# connect() args can be omitted → inside paprika-runner the PAPRIKA_HUB env\\n# is read automatically (= http://hub:8000). Only for local runs,\\n# set os.environ['PAPRIKA_HUB']=http://localhost:8000 before running.\\n\\nasync def main():\\n    async with async_paprika.connect() as cli:\\n        async with cli.session(initial_url='https://example.com/') as page:\\n            async for visit in pap.walk(page, target_pages=10):\\n                print(f'[{visit.n}/{visit.target}] {visit.url}')\\n\\nasyncio.run(main())\", \"job_id (任意, ログイン継続)\": \"job_id (optional, keeps login)\", \"kill — popup を閉じる + 同ドメインのときだけ main tab を redirect (デフォ)\": \"kill — close the popup + redirect the main tab only on the same domain (default)\", \"macro をすべて削除\": \"Delete all macros\", \"macro 全体の実行時間上限。\": \"Execution time limit for the whole macro.\", \"readyState 待ちがこの秒数を超え、ページが scrollable なら早期にスクロール開始。\": \"If the readyState wait exceeds this many seconds and the page is scrollable, start scrolling early.\", \"readyState=complete 後、idle 判定の前に追加で待つ秒数。重い遅延ロードに有効。\": \"Extra seconds to wait after readyState=complete, before the idle check. Useful for heavy lazy loading.\", \"script-generation retry の上限。失敗するごとに LLM にエラー文を渡して書き直してもらう。\": \"Cap on script-generation retries. On each failure, the error message is passed to the LLM to rewrite.\", \"visited URL 内で一致した件数\": \"Number of matches within visited URLs\", \"window.open / target=\\\"_blank\\\" でクリック先が別タブに飛ぶ動画サイトでは\": \"On video sites where clicks open in a separate tab via window.open / target=\\\"_blank\\\",\", \"yt-dlp で動画をダウンロード\": \"Download the video with yt-dlp\", \"— AI が毎回スクリプトを生成\": \"— AI generates a script each time\", \"— HTML + アセットを 1 回キャプチャ\": \"— Capture HTML + assets once\", \"— Hub 永続 (全 operator 共通)\": \"— Hub-persistent (shared by all operators)\", \"— Hub 永続。Fetch / Code / LLM すべてに適用\": \"— Hub-persistent. Applies to Fetch / Code / LLM alike\", \"— Hub 永続。Fetch mode のジョブで client が明示してない項目に自動適用\": \"— Hub-persistent. Auto-applied to fields the client doesn't specify in Fetch mode jobs\", \"— このブラウザにだけ保存\": \"— Saved only on this browser\", \"— 保存済み固定スクリプトを再実行\": \"— Re-run a saved fixed script\", \"— 別タブが開いたときの処理\": \"— Handling when a separate tab opens\", \"— 既存ジョブの script を再実行 (軽量)\": \"— Re-run an existing job's script (lightweight)\", \"— 環境変数依存、再デプロイで変更\": \"— Depends on env vars, changed by redeploy\", \"※ Chrome sync との関係\": \"* Relationship with Chrome sync\", \"※ planner / coder / judge 用\": \"* For planner / coder / judge\", \"★ ボタンで「デフォルト」\": \"Set as \\\"default\\\" with the ★ button\", \"⚠ name 変更時は内部で新規 PUT + 旧 name DELETE が走ります\": \"⚠ Renaming runs an internal new PUT + old-name DELETE\", \"。 Skill が「再利用パターン」なのに対し、Convention は「foot-gun 防止のたしなみ」(常時注入される短い注意書き)。\": \". Whereas a Skill is a \\\"reusable pattern,\\\" a Convention is a \\\"foot-gun-prevention etiquette\\\" (a short, always-injected note).\", \"。 main tab がそのまま動画ページに遷移するので、後続の\": \". The main tab navigates straight to the video page, so the subsequent\", \"。hub の .env に\": \". In the hub's .env,\", \"から呼べる AI バックエンドの一覧。 組み込みの\": \"List of AI backends that can be called from here. The built-in\", \"が効きます。\": \"takes effect.\", \"ここで選ぶのは「スクリプトを書く LLM」(planner + coder + judge 用)。スクリプト実行中に page.agent() が呼ぶ Vision agent (CogAgent / Qwen-VL) は別管理 (worker の AGENT_URL / COGAGENT_URL 環境変数で固定)。\": \"What you select here is the \\\"LLM that writes scripts\\\" (for planner + coder + judge). The Vision agent (CogAgent / Qwen-VL) that page.agent() calls during script execution is managed separately (fixed via the worker's AGENT_URL / COGAGENT_URL env vars).\", \"この URL を recrawl pattern に追加\": \"Add this URL to recrawl patterns\", \"この host の Cookie / Notes を編集\": \"Edit this host's Cookie / Notes\", \"この host の Visited URL / Recrawl patterns を編集\": \"Edit this host's Visited URL / Recrawl patterns\", \"この step を削除\": \"Delete this step\", \"このサイズ未満の asset は保存しない。\": \"Don't save assets smaller than this size.\", \"このジョブを preset として保存\": \"Save this job as a preset\", \"このパネルは進行中ジョブの live ビューです\": \"This panel is a live view of the in-progress job\", \"このプリセットは何をする?\": \"What does this preset do?\", \"このプリセットを実行 (POST /run)\": \"Run this preset (POST /run)\", \"この直後に空の navigate 行を挿入\": \"Insert an empty navigate row right after this\", \"されます。 不要なら delete、効くなら\": \"is done. Delete if unneeded; if it helps,\", \"すべての codegen-loop ジョブの system prompt に常時 prepend\": \"Always prepend to the system prompt of every codegen-loop job\", \"で cron / 外部スケジューラから発火可能。\": \"can be triggered from cron / an external scheduler.\", \"でお願いします。 この画面では URL / category / description / モード切替のみ反映されます。\": \"please. On this screen only URL / category / description / mode toggle are applied.\", \"できた\": \"Done\", \"でその状態でクロールが回せます。\": \"you can run a crawl in that state.\", \"でアドレスバーから直接 submit (Fetch mode)。\": \"submit directly from the address bar (Fetch mode).\", \"でフォームに復元、 または\": \"restore to the form with, or\", \"で直接実行。外部からは\": \"run directly with. From outside,\", \"どちらも OK (ZIP は自動で tar.gz に変換)\": \"Either is OK (ZIP is auto-converted to tar.gz)\", \"にして上げておくと、 ジョブ投入時に\": \"and uploading it ahead of time, on job submission\", \"に固める。\": \"package into.\", \"に昇格。\": \"promote to.\", \"に自動変換するので、 Windows Explorer の「送る → 圧縮 (ZIP) 形式」で十分です。 無理に\": \"is auto-converted, so Windows Explorer's \\\"Send to → Compressed (ZIP) folder\\\" is enough. There's no need to force\", \"に設定すると、\": \"when set to,\", \"の 重複防止に共有、Recrawl Patterns は visited に登録済みでも常に再訪問する URL の glob (\": \"shared for dedup of, and Recrawl Patterns are globs of URLs always re-visited even if already registered as visited (\", \"の環境変数を参照する方式 (本ファイルには値を保存しません)。\": \"method that references env vars (no value is saved in this file).\", \"はそのまま使えます。 新規追加は OpenAI / Claude (LiteLLM 経由) / Gemini など\": \"can be used as-is. New additions like OpenAI / Claude (via LiteLLM) / Gemini, etc.\", \"はレビュー待ち、\": \"is awaiting review,\", \"または クリックでファイル選択 ・ サイズ上限 500 MB ・\": \"or click to choose a file ・ size limit 500 MB ・\", \"をインストールすると、ツールバーから「現在の Chrome のクッキーを Paprika Hub に push」できます。 tarball 不要・Chrome を閉じる必要も無し。ただし保存対象は\": \"Once installed, you can \\\"push the current Chrome cookies to the Paprika Hub\\\" from the toolbar. No tarball, no need to close Chrome. However, what's saved is\", \"を上の領域にドラッグ&ドロップ、名前を入れて upload\": \"Drag & drop into the area above, enter a name, and upload\", \"を使うとブラウザから直接送れます (cookie のみ、即時)。\": \"lets you send directly from the browser (cookies only, instant).\", \"を入れて hub を再起動した場合に使う。 直接キーと両方設定されたら direct を優先。\": \"and restarting the hub. If both this and a direct key are set, direct takes priority.\", \"を指定しないジョブも自動的にそのプロファイルで動きます。 タールボールはローカル側で\": \"Jobs that don't specify one also run on that profile automatically. The tarball, on the local side,\", \"アクション 1 行を末尾に追加\": \"Append one action row\", \"アセットを保存\": \"Save assets\", \"アーカイブをここにドラッグ&ドロップ\": \"Drag & drop an archive here\", \"エンドポイントに対応。 API キーは\": \"Supports the endpoint. The API key is\", \"クリック後に追加で待つ秒数。\": \"Extra seconds to wait after clicking.\", \"クリック後の待ち\": \"Wait after click\", \"クロール後もセッションを閉じずに残す。\": \"Keep the session open instead of closing it after crawling.\", \"コマンドを使う必要はありません。\": \"There's no need to use the command.\", \"コード生成 LLM:\": \"Code-generation LLM:\", \"サイズ\": \"Size\", \"ジョブに接続\": \"Connect to job\", \"ジョブを一時停止する\": \"Pause the job\", \"ジョブ投入時の指定\": \"Specification at job submission\", \"スクロール\": \"Scroll\", \"スクロールの最大累積ピクセル。長すぎるページの暴走を防ぐ。\": \"Max cumulative scroll pixels. Prevents runaway on overly long pages.\", \"スクロール上限\": \"Scroll cap\", \"スクロール上限ピクセル数。\": \"Scroll cap in pixels.\", \"セッションがまだ開始されていません…\": \"Session hasn't started yet…\", \"セッションを継続\": \"Keep session alive\", \"ダウンロード (tar.gz)\": \"Download (tar.gz)\", \"テンプレ挿入\": \"Insert template\", \"デフォルトに戻す\": \"Reset to default\", \"ネットワークがこの秒数以上 idle なら capture 完了とみなす。\": \"If the network is idle for at least this many seconds, capture is considered complete.\", \"ネットワーク無通信\": \"Network idle\", \"ネットワーク無通信の判定秒数。\": \"Seconds to determine network idle.\", \"ハブ URL\": \"Hub URL\", \"ハブで管理する Chrome 拡張機能。アップロードした拡張は\": \"Chrome extensions managed by the hub. Uploaded extensions are\", \"ブラウザのすべての Cookie (cross-site トラッカー含む)\": \"All browser cookies (including cross-site trackers)\", \"ブラウザ状態からアセット/リンクを再取り込み\": \"Re-import assets/links from browser state\", \"プレイヤーを自動再生して動画 URL を踏ませる。\\\">play_videos:\": \"Auto-play the player to trigger video URLs.\\\">play_videos:\", \"ヘッドレス\": \"Headless\", \"ページに費やす最大秒数。\": \"Max seconds to spend per page.\", \"ページを最後までスクロールして遅延読み込み (lazy) のアセットを拾う。\": \"Scroll the page to the bottom to pick up lazy-loaded assets.\", \"ページ読み込みを待つ秒数。\": \"Seconds to wait for the page to load.\", \"ページ読み込み待ち\": \"Page load wait\", \"ページ読み込み後に自動スクロールするか (lazy-load 画像を踏ませる)。\": \"Whether to auto-scroll after page load (to trigger lazy-load images).\", \"ホスト毎に Cookie / 既訪問URL / 再クロール対象パターンを管理。 Cookie はセッション開始時に自動注入、Visited URL は\": \"Manage Cookies / visited URLs / recrawl patterns per host. Cookies are auto-injected at session start; Visited URLs are\", \"ボタン。\": \"button.\", \"ボタンから別画面で管理。\": \"Managed on a separate screen via the button.\", \"モーダルで編集 (rename / mode / goal / code …)\": \"Edit in a modal (rename / mode / goal / code …)\", \"リテラルキーを直接保存。空のまま編集すると現状の値を維持。 明示的にクリアしたければ\": \"Save the literal key directly. Editing while left empty keeps the current value. To clear it explicitly,\", \"リファラー\": \"Referer\", \"ローカルで profile を\": \"Locally, the profile\", \"ワイルドカード)。\": \"wildcard).\", \"上に移動\": \"Move up\", \"上の「コード生成 LLM」はスクリプトを書くためのモデル選択です。スクリプト実行中に page.agent() が使う Vision agent は worker 側で固定です。ここで変更しても挙動は変わりません。\": \"The \\\"Code-generation LLM\\\" above is the model choice for writing scripts. The Vision agent that page.agent() uses during script execution is fixed on the worker side. Changing it here won't change behavior.\", \"下に移動\": \"Move down\", \"今すぐ再取得\": \"Refetch now\", \"今のフレームを保存\": \"Save current frame\", \"今のブラウザの Cookie を Host レジストリに保存 (fetch 実行中限定)\": \"Save the current browser's cookies to the Host registry (only during a fetch run)\", \"今のブラウザの Cookie を Host レジストリに保存 (再ログイン不要に)\": \"Save the current browser's cookies to the Host registry (so no re-login is needed)\", \"使い方\": \"How to use\", \"使える CDP フィールド:\": \"Available CDP fields:\", \"全 worker のすべての lane\": \"All lanes across all workers\", \"再利用可能パターン\": \"Reusable patterns\", \"削除\": \"Delete\", \"動画を自動再生\": \"Auto-play videos\", \"動画を自動再生してアセットとして拾う。\": \"Auto-play videos and pick them up as assets.\", \"動画クリック後に待つ秒数 (プレイヤー反応を待つ)。\": \"Seconds to wait after clicking a video (to wait for the player to respond).\", \"動画自動再生 ON\": \"Video auto-play ON\", \"単発ページ取得 (scroll + 動画自動)。LLM 不使用、最速。\": \"Single-page fetch (scroll + video auto). No LLM, fastest.\", \"参照する Job ID\": \"Job ID to reference\", \"参考表示。Code mode では script 内で直接指定。\": \"For reference. In Code mode, specify it directly within the script.\", \"取得元 URL\": \"Source URL\", \"名前\": \"Name\", \"名前:\": \"Name:\", \"失敗→成功 diff から自動抽出される細かいルール\": \"Fine-grained rules auto-extracted from the failure→success diff\", \"学習動作を保存\": \"Save learning behavior\", \"実行タイムアウト\": \"Execution timeout\", \"実行モード\": \"Run mode\", \"寸法\": \"Dimensions\", \"左のリストから 1 つ選ぶか、上部の add engine で新規追加してください。\": \"Pick one from the list on the left, or add a new one with add engine at the top.\", \"拾ったアセットをサーバ側に保存する。\": \"Save the picked-up assets on the server side.\", \"掲載ページ\": \"Source page\", \"操作者の Chrome のクッキー・ログイン状態を\": \"The operator's Chrome cookies / login state\", \"方法 1: CLI でアップロード\": \"Method 1: Upload via the CLI\", \"方法 2: Web GUI でアップロード\": \"Method 2: Upload via the Web GUI\", \"方法 3: Chrome 拡張 (Paprika Bridge) で cookie 即送信\": \"Method 3: Send cookies instantly via the Chrome extension (Paprika Bridge)\", \"既存 job を再利用してログイン状態を引き継ぐ。\": \"Reuse an existing job to carry over the login state.\", \"既訪問URLをスキップ\": \"Skip visited URLs\", \"既訪問URLをスキップ (cron 等で日次再クロール時に有効)。\": \"Skip visited URLs (useful for daily re-crawls via cron, etc.).\", \"既訪問URLをスキップ (host_dedup)\": \"Skip visited URLs (host_dedup)\", \"既訪問URLをスキップ (host_dedup) — 参考表示\": \"Skip visited URLs (host_dedup) — for reference\", \"日本語\": \"Japanese\", \"最大待ち時間\": \"Max wait time\", \"最大試行回数\": \"Max attempts\", \"最小アセットサイズ (byte)。\": \"Minimum asset size (bytes).\", \"最小ファイルサイズ\": \"Minimum file size\", \"最小ファイルサイズ (bytes):\": \"Minimum file size (bytes):\", \"現在の host に一致する Cookie のみを表示\": \"Show only cookies matching the current host\", \"現在のフォーム状態を新しい preset として保存\": \"Save the current form state as a new preset\", \"現在表示中の URL をクリップボードにコピー (1 行 1 URL)\": \"Copy the currently shown URLs to the clipboard (one URL per line)\", \"環境変数で固定\": \"Fixed via env var\", \"生成される Python スクリプトをプレビュー\": \"Preview the generated Python script\", \"生成スクリプトを skill として登録\": \"Register the generated script as a skill\", \"画面を出さずに実行 (Chrome --headless)。\": \"Run without showing a window (Chrome --headless).\", \"直前のスクリプトで再実行する\": \"Re-run with the previous script\", \"相当)\": \"equivalent)\", \"経由で自動的に読み込まれます (次回 Chrome 起動時から)。 対応フォーマット:\": \"is loaded automatically via it (from the next Chrome launch). Supported formats:\", \"自動スクロール ON\": \"Auto-scroll ON\", \"自由記述メモ\": \"Free-form notes\", \"行を増やして順に並べる → 自動で paprika-client スクリプトに変換 → 実行\": \"Add rows and line them up in order → auto-convert to a paprika-client script → run\", \"詳細編集は\": \"For detailed editing,\", \"読み込み中の preset を現在のフォーム状態で上書き保存\": \"Overwrite the loaded preset with the current form state\", \"選択中:\": \"Selected:\", \"💡 ZIP も受け付けます:\": \"💡 ZIP is also accepted:\", \"🧠 学習動作\": \"🧠 Learning behavior\", \"空ならデフォルトの data_dir にジョブデータを保存。\": \"If empty, job data is stored in the default data_dir.\", \"— Hub 永続。ジョブデータの外部ストレージ\": \"— Hub-persistent. External storage for job data\", \"接続設定を保存\": \"Save connection settings\", \"マウント\": \"Mount\", \"アンマウント\": \"Unmount\", \"マウント中…\": \"Mounting...\", \"接続中\": \"Connected\", \"未接続\": \"Disconnected\", \"(マウントボタンで接続)\": \"(click Mount to connect)\", \"SMB サーバー:\": \"SMB Server:\", \"共有名:\": \"Share name:\", \"ユーザー名:\": \"Username:\", \"パスワード:\": \"Password:\", \"マウントポイント:\": \"Mount point:\", \"追加オプション:\": \"Extra options:\", \"コンテナ内のマウント先パス\": \"Mount path inside the container\", \"パスワード表示/非表示\": \"Show/hide password\", \"推論ジャッジ\": \"Reasoning Judge\", \"— 高品質な LLM 判定 (DeepSeek R1 / Claude / GPT 等)\": \"— High-quality LLM judgment (DeepSeek R1 / Claude / GPT, etc.)\", \"デフォルトジャッジに加え、推論特化モデルで判定を行う。shadow = 両方実行して比較ログ、primary = 推論ジャッジの判定を採用。\": \"Run a reasoning-specialized model alongside the default judge. shadow = run both and log comparison, primary = use reasoning judge verdict.\", \"モード:\": \"Mode:\", \"off — 無効\": \"off — Disabled\", \"shadow — 比較ログのみ\": \"shadow — Compare log only\", \"primary — 推論ジャッジ優先\": \"primary — Reasoning judge priority\", \"エンジン:\": \"Engine:\", \"(未設定 — env fallback)\": \"(Not set — env fallback)\", \"AI エンジンタブで登録済みのエンジンから選択\": \"Select from engines registered in the AI Engines tab\", \"推論ジャッジを保存\": \"Save Reasoning Judge\", \"保存しました\": \"Saved\", \"— 外部 MariaDB / MySQL への接続\": \"— External MariaDB / MySQL connection\", \"ジョブ・ワーカー・ホスト等の永続データを MariaDB に保管。未設定なら従来通り Redis + ファイルを使用。\": \"Store persistent data (jobs, workers, hosts, etc.) in MariaDB. If not configured, Redis + files are used as before.\", \"ホスト:\": \"Host:\", \"ポート:\": \"Port:\", \"データベース:\": \"Database:\", \"接続テスト\": \"Test Connection\", \"テスト中…\": \"Testing...\", \"接続成功\": \"Connection successful\", \"接続失敗\": \"Connection failed\", \"ホストが未設定です\": \"Host is not configured\", \"ユーザー名が未設定です\": \"Username is not configured\", \"データ移行\": \"Data Migration\", \"Redis / ファイルに保存されているデータを MariaDB に移行します（成功後、元データは削除されます）。\": \"Migrate data stored in Redis / files to MariaDB (original data is deleted after success).\", \"テーブル作成\": \"Create Tables\", \"Jobs を移行\": \"Migrate Jobs\", \"Hosts を移行\": \"Migrate Hosts\", \"Visited URLs を移行\": \"Migrate Visited URLs\", \"作成中…\": \"Creating...\", \"移行中…\": \"Migrating...\", \"テーブル作成済み\": \"tables created\", \"件移行\": \"migrated\", \"件スキップ\": \"skipped\", \"件エラー\": \"errors\"}");

function _jpToEn(root) {
  if (!window.i18next || window.i18next.language !== 'en') return;
  root = root || document.body;
  if (!root) return;
  try {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const fixes = [];
    let n;
    while ((n = walker.nextNode())) {
      const raw = n.nodeValue;
      if (!raw) continue;
      const key = raw.trim();
      if (key && Object.prototype.hasOwnProperty.call(JP2EN, key)) {
        fixes.push([n, raw.replace(key, JP2EN[key])]);
      }
    }
    for (const fx of fixes) fx[0].nodeValue = fx[1];
    root.querySelectorAll('[title]').forEach(el => {
      const k = (el.getAttribute('title') || '').trim();
      if (k && Object.prototype.hasOwnProperty.call(JP2EN, k)) el.setAttribute('title', JP2EN[k]);
    });
    root.querySelectorAll('[placeholder]').forEach(el => {
      const k = (el.getAttribute('placeholder') || '').trim();
      if (k && Object.prototype.hasOwnProperty.call(JP2EN, k)) el.setAttribute('placeholder', JP2EN[k]);
    });
  } catch (_) {}
}

// applyI18n walks every [data-i18n*] element under ``root`` (defaults
// to document) and rewrites its text / placeholder / title / aria
// according to the current language. Safe to call repeatedly and
// after dynamic content insertion (e.g. after re-rendering a list).
function applyI18n(root) {
  if (!window.i18next || !window.i18next.t) return;
  root = root || document;
  const t = window.i18next.t.bind(window.i18next);
  // Keep the document's lang attribute in sync with the active
  // locale so the browser's spellcheck / screen readers / CSS
  // :lang() selectors agree with what's actually on screen. The
  // inline markup ships lang="ja"; flip it when English is active.
  try {
    const lng = (window.i18next.language || 'en').startsWith('ja') ? 'ja' : 'en';
    if (document.documentElement) document.documentElement.lang = lng;
  } catch (_) {}
  root.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (key) el.textContent = t(key);
  });
  root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    if (key) el.setAttribute('placeholder', t(key));
  });
  root.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    if (key) el.setAttribute('title', t(key));
  });
  root.querySelectorAll('[data-i18n-aria]').forEach(el => {
    const key = el.getAttribute('data-i18n-aria');
    if (key) el.setAttribute('aria-label', t(key));
  });
  // Catch-all: translate any remaining inline Japanese to English.
  _jpToEn(root);
}

// Initialize i18next as soon as possible. The script is loaded
// synchronously in <head> so window.i18next is ready here.
if (window.i18next) {
  window.i18next.init({
    lng: _pickInitialLocale(),
    fallbackLng: 'en',
    resources: I18N_RESOURCES,
    interpolation: { escapeValue: false },  // we never inject HTML via t()
  }).then(() => applyI18n());
} else {
  console.warn('i18next not loaded; UI stays in inline (Japanese) text');
}

// Locale switcher in the header. Persists across reloads via
// localStorage; reflected on initial render via _pickInitialLocale.
document.addEventListener('DOMContentLoaded', () => {
  const sw = document.getElementById('localeSwitch');
  if (!sw) return;
  // Reflect current state.
  if (window.i18next) sw.value = window.i18next.language || 'en';
  sw.addEventListener('change', e => {
    const next = e.target.value === 'ja' ? 'ja' : 'en';
    try { localStorage.setItem('paprika.locale', next); } catch (_) {}
    if (window.i18next) {
      window.i18next.changeLanguage(next).then(() => applyI18n());
    }
  });
});

// --- inline SVG icons (Lucide style) ---------------------------------------
// Static strings, embedded once per row -- no external CSS framework needed.
const _SVG_BASE = 'xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  play:     `<svg ${_SVG_BASE}><polygon points="6 4 20 12 6 20 6 4"/></svg>`,
  signal:   `<svg ${_SVG_BASE}><path d="M2 20a16 16 0 0 1 20 0"/><path d="M5 16.5a12 12 0 0 1 14 0"/><path d="M8 13a8 8 0 0 1 8 0"/><circle cx="12" cy="20" r="1"/></svg>`,
  image:    `<svg ${_SVG_BASE}><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>`,
  fileText: `<svg ${_SVG_BASE}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>`,
  list:     `<svg ${_SVG_BASE}><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/></svg>`,
  code:     `<svg ${_SVG_BASE}><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>`,
  refresh:  `<svg ${_SVG_BASE}><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`,
  link:     `<svg ${_SVG_BASE}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`,
  trash:    `<svg ${_SVG_BASE}><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1.5 14a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>`,
  more:     `<svg ${_SVG_BASE}><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>`,
  moreV:    `<svg ${_SVG_BASE}><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>`,
  chevron:  `<svg ${_SVG_BASE}><polyline points="6 9 12 15 18 9"/></svg>`,
};
function ico(name) { return `<span class="ico">${ICONS[name] || ''}</span>`; }
// Localised label for strings built inside JS templates (table rows
// rendered dynamically don't get data-i18n attributes, so we resolve
// the i18next key inline). Falls back to ``fb`` when i18next isn't
// ready yet.
function tt(key, fb) {
  try {
    if (window.i18next && window.i18next.t) {
      const v = window.i18next.t(key);
      if (v && v !== key) return v;
    }
  } catch (_) {}
  return fb;
}

// --- tabs -------------------------------------------------------------------
// True while we're updating the hash from setTab(), so the
// hashchange handler doesn't bounce back and re-trigger setTab.
let _suppressNextHashChange = false;

function setTab(name, { updateHash = true } = {}) {
  document.querySelectorAll('.tab').forEach(t => {
    // The "More" trigger has no data-tab and must never be marked
    // .active itself; the wrap gets .has-active instead so the
    // trigger gets its red highlight via CSS.
    if (!t.dataset.tab) return;
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.panel').forEach(p => {
    p.classList.toggle('active', p.dataset.panel === name);
  });
  // Re-sync hub Settings-derived defaults whenever the Submit tab is
  // (re)activated, so a tab left open across a Settings change picks
  // up the new min-file-size etc. instead of submitting a stale
  // value. applyHubSettingsDefaultsToForm() (called inside) skips
  // any field the operator manually edited, so an in-progress edit
  // is preserved. Guarded on the function existing because setTab
  // can fire before the Submit-form script block has defined it.
  if (name === 'submit' && typeof loadHubSettingsDefaults === 'function') {
    loadHubSettingsDefaults();
  }
  // Recipes tab: refresh on activation so a recipe added from
  // another browser tab / via the API shows up without a manual
  // refresh button press. Cheap: full host list + flatten is
  // fast for typical fleet sizes (< 100 hosts).
  if (name === 'recipes' && typeof renderRecipes === 'function') {
    renderRecipes();
  }
  // Settings tab: load hub settings + MariaDB/SMB status on activation
  // so the panel is populated when deep-linking via #settings or on
  // browser refresh. Deferred via setTimeout because setTab() runs at
  // script-parse line ~1065 while SETTINGS_URL / UI_DEFAULTS_FALLBACK
  // are const-declared much later (~9473); calling loadSettingsPanel()
  // synchronously would hit the temporal dead zone. The macrotask fires
  // after the full script has executed, so all constants are initialised.
  if (name === 'settings' && typeof loadSettingsPanel === 'function') {
    setTimeout(loadSettingsPanel, 0);
  }
  // Highlight the "More" dropdown when one of its children is active.
  const moreWrap = document.getElementById('moreTabWrap');
  if (moreWrap) {
    const childActive = !!moreWrap.querySelector('.tab.active');
    moreWrap.classList.toggle('has-active', childActive);
  }
  // Live Preview (= tab name 'screens') polls every N seconds across
  // every connected worker × lane to refresh the thumbnail tiles.
  // That's bandwidth + JPEG decode cost we don't want to pay while
  // the operator is on a different tab (Submit / Jobs / Workers /
  // ...). Start the poll loop only when entering the screens tab,
  // stop it on every other tab. ``ssEnabled`` checkbox stays
  // authoritative inside refreshScreenshots(), so a user who
  // explicitly unchecked it doesn't get re-armed.
  try {
    if (name === 'screens') {
      if (typeof resetScreenshotTimer === 'function'
          && document.getElementById('ssEnabled')
          && document.getElementById('ssEnabled').checked) {
        resetScreenshotTimer();
      }
    } else if (typeof ssTimer !== 'undefined' && ssTimer) {
      clearInterval(ssTimer);
      ssTimer = null;
    }
  } catch (_) { /* setTab fires before screenshot wiring is ready */ }
  try { localStorage.setItem('paprika.tab', name); } catch (e) {}
  // Sync the URL hash so the address bar reflects the active tab.
  // - Copy/paste / share the URL -> recipient lands on same tab.
  // - Browser back/forward navigates between tabs.
  // We use history.replaceState rather than location.hash so each
  // tab click doesn't pile up history entries (back button would
  // become useless if every click added a state). The hashchange
  // handler is suppressed for one tick because some browsers fire
  // it even on replaceState-induced hash changes.
  if (updateHash) {
    const want = '#' + name;
    if (location.hash !== want) {
      _suppressNextHashChange = true;
      try {
        history.replaceState(null, '', want);
      } catch (e) {
        // Fallback for environments where replaceState isn't
        // available (very old browsers / file://) -- accept the
        // history entry rather than silently dropping the sync.
        location.hash = want;
      }
      // Clear the suppression flag on the next tick; if hashchange
      // didn't fire by then we don't want to swallow real ones.
      setTimeout(() => { _suppressNextHashChange = false; }, 0);
    }
  }
}
document.querySelectorAll('.tab').forEach(t => {
  // Skip the "More" trigger -- it has no data-tab and toggles the
  // menu instead of switching panels.
  if (!t.dataset.tab) return;
  t.addEventListener('click', () => {
    setTab(t.dataset.tab);
    // If we clicked a menu item, close the dropdown.
    closeMoreMenu();
  });
});

// "More" dropdown wiring
const _moreWrap = document.getElementById('moreTabWrap');
const _moreBtn  = document.getElementById('moreTabBtn');
function openMoreMenu()  { if (_moreWrap) { _moreWrap.classList.add('open');    _moreBtn.setAttribute('aria-expanded','true');  } }
function closeMoreMenu() { if (_moreWrap) { _moreWrap.classList.remove('open'); _moreBtn.setAttribute('aria-expanded','false'); } }
function toggleMoreMenu() {
  if (!_moreWrap) return;
  if (_moreWrap.classList.contains('open')) closeMoreMenu();
  else openMoreMenu();
}
if (_moreBtn) {
  _moreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMoreMenu();
  });
}
// Outside click + Escape closes the menu.
document.addEventListener('click', (e) => {
  if (_moreWrap && !_moreWrap.contains(e.target)) closeMoreMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeMoreMenu();
});

// Tab name lookup -- which tabs are recognised. Used to validate
// URL hashes / localStorage values before applying.
function _validTabName(name) {
  return !!document.querySelector(`.tab[data-tab="${name}"]`);
}

// URL hash -> tab. When the operator opens (or shares) a link like
// http://hub/#profiles we want to land on that tab directly. The
// hash takes precedence over the localStorage memory; without this
// the persisted tab would always win and the deep-link would look
// broken.
function _hashTab() {
  const h = (location.hash || '').replace(/^#/, '').trim();
  return _validTabName(h) ? h : null;
}

// Parse the URL hash into { tab, jobId }. Two shapes are understood:
//   #profiles            -> { tab: 'profiles', jobId: null }
//   #live/<jobid>        -> { tab: 'submit',   jobId: '<jobid>' }
// The #live/ form is what "watch live" / submit-and-attach write so the
// address bar reflects the attached job and the link is shareable.
function _parseHash() {
  const h = (location.hash || '').replace(/^#/, '').trim();
  // #live/<jobid> -> Submit tab + Live panel attached to that job.
  let m = h.match(/^live\/(.+)$/);
  if (m) return { tab: 'submit', jobId: decodeURIComponent(m[1]), entityId: null };
  // #<tab>/<entityId> -> open that entity's editor on the given tab.
  m = h.match(/^([^\/]+)\/(.+)$/);
  if (m && _validTabName(m[1])) {
    return { tab: m[1], jobId: null, entityId: decodeURIComponent(m[2]) };
  }
  return { tab: _validTabName(h) ? h : null, jobId: null, entityId: null };
}

// Per-tab opener for entity deep-links. Each opener fetches its own
// record by id, so a cold #<tab>/<id> link works without the list
// having rendered first. Guarded by typeof so load order is moot.
const _entityDeepLinkOpeners = {
  hosts:       (id) => { if (typeof openHostModal === 'function') openHostModal(id); },
  presets:     (id) => { if (typeof openPresetEditModal === 'function') openPresetEditModal(id); },
  workers:     (id) => { if (typeof openWorkerDetailModal === 'function') openWorkerDetailModal(id); },
};

// Write #<tab>/<id> into the address bar (shareable / survives reload).
// Suppresses the resulting hashchange so it doesn't bounce back through
// _applyHashTab and reopen what's already open.
function _entityHashSync(tab, id) {
  const want = '#' + tab + '/' + encodeURIComponent(id);
  if (location.hash === want) return;
  _suppressNextHashChange = true;
  try { history.replaceState(null, '', want); }
  catch (e) { location.hash = want; }
  setTimeout(() => { _suppressNextHashChange = false; }, 0);
}
// Clear a #<tab>/<id> deep-link back to the bare #<tab> (used on modal
// close). Leaves the hash alone if it isn't an <tab>/<id> deep-link.
function _entityHashClear(tab) {
  const re = new RegExp('^#' + tab + '\\/');
  if (!re.test(location.hash || '')) return;
  _suppressNextHashChange = true;
  try { history.replaceState(null, '', '#' + tab); }
  catch (e) { location.hash = '#' + tab; }
  setTimeout(() => { _suppressNextHashChange = false; }, 0);
}

// Apply the current hash to the tab selector. Wrap setTab so we can
// call it from the initial paint AND on every hashchange (the
// browser's back/forward buttons fire that event). Pass
// updateHash: false so we don't recurse -- the hash is already
// what it is, no need to sync it back.
function _applyHashTab() {
  if (_suppressNextHashChange) {
    _suppressNextHashChange = false;
    return;
  }
  const parsed = _parseHash();
  if (parsed.jobId) {
    // Deep-link to a watched job: land on Submit, switch to the Live
    // sub-tab, and (re)attach the Live panel if it isn't already
    // showing that job.
    setTab('submit', { updateHash: false });
    if (typeof setSubmitSubtab === 'function') setSubmitSubtab('live');
    if (typeof ljpAttach === 'function'
        && (typeof LJP === 'undefined' || LJP.jobId !== parsed.jobId)) {
      ljpAttach(parsed.jobId);
    }
  } else if (parsed.entityId && _entityDeepLinkOpeners[parsed.tab]) {
    // Deep-link to an entity editor: switch to its tab and open it.
    setTab(parsed.tab, { updateHash: false });
    _entityDeepLinkOpeners[parsed.tab](parsed.entityId);
  } else if (parsed.tab) {
    setTab(parsed.tab, { updateHash: false });
    // Bare #submit defaults to the form sub-tab so reload of #submit
    // doesn't strand the operator on Live (an empty Live pane when
    // no job attached is confusing).
    if (parsed.tab === 'submit' && typeof setSubmitSubtab === 'function') {
      setSubmitSubtab('form');
    }
  }
}

// --- Submit panel sub-tabs (ジョブの実行 / Live) ----------------------
// Two sub-panes share the Submit panel. The form sub-pane holds the job
// submission UI; the live sub-pane holds the inline #liveJobPanel that
// shows status / log / noVNC / etc. for an attached job. Switching
// between them just toggles ``display`` -- the form's input values
// persist because nothing is removed from the DOM.
function setSubmitSubtab(name) {
  if (name !== 'form' && name !== 'live') name = 'form';
  document.querySelectorAll('.submit-subtab').forEach(t => {
    const on = t.dataset.submitSubtab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('.submit-subpane').forEach(p => {
    p.style.display = (p.dataset.submitSubpane === name) ? '' : 'none';
  });
  // When the live sub-tab is shown but no job is attached, surface a
  // placeholder so the pane doesn't look empty. ljpAttach / ljpReset
  // also call into this code (via _updateLivePlaceholder) to keep the
  // placeholder in sync as jobs come and go.
  if (typeof _updateLivePlaceholder === 'function') _updateLivePlaceholder();
}

function _updateLivePlaceholder() {
  const ph = document.getElementById('ljpNoJobPlaceholder');
  const ljp = document.getElementById('liveJobPanel');
  if (!ph || !ljp) return;
  const attached = (typeof LJP !== 'undefined') && !!LJP.jobId;
  ph.style.display = attached ? 'none' : '';
  // The section's own inline display still controls whether the LJP
  // chrome shows once a job is attached. ljpAttach sets it to '' and
  // ljpReset to 'none'; we mirror that here so a tab switch with an
  // attached job doesn't briefly flash the placeholder.
  ljp.style.display = attached ? '' : 'none';
  // Live sub-tab indicator (dot + jobId badge) reflects the attach state.
  const dot = document.getElementById('submitSubtabLiveDot');
  const badge = document.getElementById('submitSubtabLiveJobBadge');
  if (dot) dot.style.background = attached ? '#c0392b' : '#bbb';
  if (badge) badge.textContent = attached ? (LJP.jobId.slice(0, 12)) : '';
}

// Wire sub-tab clicks. Runs once at script load; the elements exist in
// the static HTML.
(function _wireSubmitSubtabs() {
  document.querySelectorAll('.submit-subtab').forEach(btn => {
    btn.addEventListener('click', () => {
      setSubmitSubtab(btn.dataset.submitSubtab);
    });
  });
})();
window.addEventListener('hashchange', _applyHashTab);

// restore previously selected tab. Precedence (first wins):
//   1. URL hash               -- supports /#profiles deep-links
//   2. localStorage memory    -- "what was open last time"
//   3. 'submit' (default)
let _initialTab = 'submit';
let _initialLiveJob = null;
let _initialEntity = null;   // { tab, id }
try {
  const _parsed = _parseHash();
  _initialLiveJob = _parsed.jobId;
  if (_parsed.entityId) _initialEntity = { tab: _parsed.tab, id: _parsed.entityId };
  _initialTab = _parsed.tab
    || localStorage.getItem('paprika.tab')
    || 'submit';
} catch (e) {}
if (!_validTabName(_initialTab)) _initialTab = 'submit';
// When deep-linking to #live/<id> or #<tab>/<id> keep the hash intact
// (don't let setTab rewrite it); otherwise sync the tab hash as before.
setTab(_initialTab, { updateHash: !(_initialLiveJob || _initialEntity) });
if (_initialLiveJob) {
  // ljpAttach + the LJP const are declared further down this script,
  // so defer to a macrotask to dodge the temporal-dead-zone.
  setTimeout(() => {
    if (typeof ljpAttach === 'function') ljpAttach(_initialLiveJob);
  }, 0);
} else if (_initialEntity) {
  setTimeout(() => {
    const fn = _entityDeepLinkOpeners[_initialEntity.tab];
    if (fn) fn(_initialEntity.id);
  }, 0);
}

// --- actions dropdown menu --------------------------------------------------
// Opens on click. Closes on:
//   - clicking another row's actions button (only one open at a time)
//   - clicking a menu item (run the action, then dismiss)
//   - clicking anywhere outside the wrap
//   - mouse leaving the wrap (350ms grace -- so brushing past the edge
//     between button and menu doesn't accidentally close it)
//   - focus moving out of the wrap (keyboard users)
let _hoverCloseTimer = null;
function closeAllMenus(except) {
  document.querySelectorAll('.menu.open').forEach(m => {
    if (m !== except) {
      m.classList.remove('open');
      const trigger = m.previousElementSibling;
      if (trigger) trigger.classList.remove('open');
    }
  });
}
function toggleMenu(btn) {
  const menu = btn.nextElementSibling;
  const wasOpen = menu.classList.contains('open');
  closeAllMenus();
  if (!wasOpen) {
    menu.classList.add('open');
    btn.classList.add('open');
    bindAutoClose(btn.closest('.menu-wrap'));
  }
}
function bindAutoClose(wrap) {
  const menu = wrap.querySelector('.menu');
  function close() {
    menu.classList.remove('open');
    const trigger = menu.previousElementSibling;
    if (trigger) trigger.classList.remove('open');
    unbind();
  }
  function delayedClose() {
    if (_hoverCloseTimer) clearTimeout(_hoverCloseTimer);
    _hoverCloseTimer = setTimeout(() => { close(); _hoverCloseTimer = null; }, 350);
  }
  function cancelClose() {
    if (_hoverCloseTimer) { clearTimeout(_hoverCloseTimer); _hoverCloseTimer = null; }
  }
  function onFocusOut() {
    // Wait one tick for document.activeElement to settle on the new target.
    setTimeout(() => { if (!wrap.contains(document.activeElement)) close(); }, 0);
  }
  function unbind() {
    wrap.removeEventListener('mouseleave', delayedClose);
    wrap.removeEventListener('mouseenter', cancelClose);
    wrap.removeEventListener('focusout', onFocusOut);
  }
  wrap.addEventListener('mouseleave', delayedClose);
  wrap.addEventListener('mouseenter', cancelClose);
  wrap.addEventListener('focusout', onFocusOut);
}
document.addEventListener('click', e => {
  // Clicking a menu item: let the link/button handle the action, then close.
  if (e.target.closest('.menu a, .menu button')) {
    closeAllMenus();
    return;
  }
  // Clicking anywhere else inside the wrap (the actions button itself):
  // toggleMenu has already handled it.
  if (e.target.closest('.menu-wrap')) return;
  // Truly outside: close everything.
  closeAllMenus();
});

// --- main refresh -----------------------------------------------------------
async function refresh() {
  try {
    const [h, workers, jobs, sessions] = await Promise.all([
      fetch('/health').then(r => r.json()),
      fetch('/workers').then(r => r.json()),
      fetch('/jobs').then(r => r.json()),
      fetch('/sessions').then(r => r.json()).catch(() => ({count:0, sessions:[]})),
    ]);
    const wcount = workers.count || 0;
    const jcount = jobs.total ?? (jobs.jobs || jobs).length;
    const scount = sessions.count || 0;
    document.getElementById('status').textContent =
      `store=${h.store}  workers=${h.workers}  jobs=${jcount}  sessions=${scount}`;
    document.getElementById('cntWorkers').textContent = wcount;
    document.getElementById('cntJobs').textContent = jcount;
    document.getElementById('cntSessions').textContent = scount;
    document.getElementById('workerCount').textContent = wcount;
    document.getElementById('sessionCount').textContent = scount;

    // Cache the latest workers list so the per-row "..." menu can
    // render its info block without a second round-trip when it opens.
    try { window._lastWorkersPayload = workers.workers || []; } catch (_) {}

    // workers table -- skip rebuild while any row's "..." menu is open,
    // matches the Jobs-tab behaviour: a 2-second refresh would otherwise
    // close the menu under the operator the moment a heartbeat fires.
    const ntbody = document.querySelector('#workersTable tbody');
    const wmenuOpen = !!document.querySelector('#workersTable .menu.open');
    if (wmenuOpen) {
      // leave rendered rows alone this tick (counts in the tab header
      // still update via cntWorkers / workerCount above)
    } else if (!workers.workers || workers.workers.length === 0) {
      ntbody.innerHTML = '<tr><td colspan=8 class="empty">no workers connected</td></tr>';
    } else {
      // Sort: alive first, then by recency. Historical (disconnected)
      // workers fall to the bottom so the operator's live fleet stays
      // up top.
      const sortedWorkers = [...workers.workers].sort((a, b) => {
        if (!!b.alive - !!a.alive) return !!b.alive - !!a.alive;
        return (b.last_heartbeat || 0) - (a.last_heartbeat || 0);
      });
      ntbody.innerHTML = sortedWorkers.map(w => {
        const status = w.status || 'active';
        const wid = esc(w.worker_id);
        const alive = !!w.alive;
        // Historical workers render in greyed-out rows with no
        // status-toggle (the worker isn't here to honour it). Selecting
        // a new status posts to a 404'd /workers/{id}/status -- the UI
        // already disabled the select, but defence in depth.
        const opts = ['active', 'drain', 'standby'].map(s =>
          `<option value="${s}"${s === status ? ' selected' : ''}>${s}</option>`
        ).join('');
        const selectDisabled = alive ? '' : 'disabled';
        const version = w.version
          ? `<code title="${esc(w.version)}">${esc(w.version.split(' ')[0])}</code>`
          : '<span class="empty">—</span>';
        // Profile-cache status: number of prefetched profiles + a
        // hover tooltip listing names + sizes. Click pivots to the
        // Profiles tab. "—" when nothing is cached (= no profile
        // uploads yet, or this worker just connected and prefetches
        // haven't completed). Default profile shows a yellow ★.
        const pcache = w.profiles_cached || [];
        let profilesCell;
        if (pcache.length === 0) {
          profilesCell = '<span class="empty">—</span>';
        } else {
          const _bytes = (n) => {
            if (n < 1024) return n + 'B';
            if (n < 1048576) return (n/1024).toFixed(0) + 'K';
            if (n < 1073741824) return (n/1048576).toFixed(1) + 'M';
            return (n/1073741824).toFixed(1) + 'G';
          };
          const tooltip = pcache.map(p =>
            `${p.name}  (${_bytes(p.size_bytes || 0)})`
          ).join('\n');
          // Show first 2 names inline, "+N more" if more. Default
          // profile (= name matches _profilesDefaultName cached
          // from the last /profiles round-trip) gets a ★ prefix.
          const def = (window._profilesDefaultName || '');
          const chips = pcache.slice(0, 2).map(p => {
            const star = (p.name === def) ? '<span style="color:#d4a13d;" title="default">★</span> ' : '';
            return `${star}<code style="font-size:.85em;">${esc(p.name)}</code>`;
          }).join(', ');
          const more = pcache.length > 2 ? ` <small>+${pcache.length - 2}</small>` : '';
          profilesCell = `<a href="#profiles" title="${esc(tooltip)}" `
            + `style="text-decoration:none; color:inherit;">${chips}${more}</a>`;
        }
        // The transport-level w.address is the WS source IP. For workers
        // running inside docker on the same host as the hub that becomes
        // an internal bridge IP (172.18.x.x or 10.x.x.x) -- useless to
        // an operator. The worker's noVNC URLs carry the externally-
        // reachable hostname they advertise, so prefer that.
        const novncs = w.lane_novnc_urls || w.slot_novnc_urls || [];
        let externalHost = '';
        try {
          for (const u of novncs) {
            const h = new URL(u).hostname;
            if (h && h !== 'localhost' && h !== '127.0.0.1') { externalHost = h; break; }
          }
        } catch (_) {}
        // Decide which to show as the primary value:
        //   - prefer external if w.address looks docker-internal
        //   - otherwise w.address (matches the WS dial-in IP, useful)
        const dockerInternal = w.address && /^(?:172\.(?:1[6-9]|2\d|3[01])\.|10\.\d+\.\d+\.\d+$|127\.)/.test(w.address);
        const primary = (dockerInternal && externalHost) ? externalHost : w.address;
        let address;
        if (primary) {
          let extra = '';
          if (dockerInternal && externalHost && externalHost !== w.address) {
            extra = ` <small style="color:#888;" title="docker-internal WS dial-in: ${esc(w.address)}">(via ${esc(w.address)})</small>`;
          }
          address = `<code>${esc(primary)}</code>${extra}`;
        } else {
          address = '<span class="empty">—</span>';
        }
        // Status badge shows "offline" for historical (disconnected)
        // workers so the operator can tell at a glance which entries
        // are alive vs remembered.
        const statusBadge = alive
          ? `<span class="badge ${esc(status)}">${esc(status)}</span>`
          : `<span class="badge" style="background:#eee; color:#888; border-color:#ccc;">offline</span>`;
        const ageHint = (!alive && w.last_heartbeat)
          ? ` <small style="color:#aaa;" title="last seen ${esc(new Date(w.last_heartbeat*1000).toISOString())}">${esc(fmtAgoOrNever(new Date(w.last_heartbeat*1000).toISOString()))}</small>`
          : '';
        const rowStyle = alive ? '' : ' style="opacity:0.55;"';
        // Delete is offered only for historical rows -- you can't
        // forget a worker that's still WS-connected (the DELETE endpoint
        // 409s anyway, but the UI shouldn't tempt the operator).
        const deleteItem = alive
          ? `<button onclick="window.alert('Drain and disconnect this worker first.')" disabled title="worker still connected — drain it first">${ico('trash')} delete</button>`
          : `<button class="danger" onclick="window.deleteWorker('${wid}')">${ico('trash')} forget worker</button>`;
        return `
        <tr${rowStyle}>
          <td><code>${wid}</code>${ageHint}</td>
          <td>${address}</td>
          <td>
            <span class="wstat">
              ${statusBadge}
              <select onchange="setWorkerStatus('${wid}', this.value)" ${selectDisabled}>${opts}</select>
            </span>
          </td>
          <td>${w.in_flight} / ${w.capacity}</td>
          <td>${profilesCell}</td>
          <td>${version}</td>
          <td>${esc(Object.entries(w.labels || {}).map(([k,v]) => `${k}=${v}`).join(', '))}</td>
          <td>
            <div class="menu-wrap">
              <button class="action-btn" onclick="window.toggleWorkerMenu(this, '${wid}')" title="worker actions">${ICONS.moreV}</button>
              <div class="menu">
                <button onclick="window.openWorkerDetailModal('${wid}')" title="この worker の状態とログを表示">
                  <span class="ico"><iconify-icon icon="lucide:info"></iconify-icon></span> 詳細
                </button>
                <div class="divider"></div>
                ${deleteItem}
              </div>
            </div>
          </td>
        </tr>`;
      }).join('');
    }
    // sessions table
    renderSessions(sessions.sessions || []);

    // screenshot grid follows the worker set -- but only ALIVE workers.
    // The /workers payload now includes historical (alive=false) workers
    // so the Workers tab can show them dimmed; without this filter the
    // Live Preview grid would render dead tiles for each disconnected
    // worker (they 404 on screenshot polls and look broken).
    syncScreenshotGrid((workers.workers || []).filter(w => w.alive));
    // Flip the RUNNING / IDLE indicator on each tile based on the
    // current jobs list AND active sessions. Done every refresh()
    // tick so a freshly started job lights up its lane within 2
    // seconds. Sessions are needed for codegen-loop / vision-agent
    // jobs whose JobInfo doesn't carry worker_id -- only the
    // SessionInfo records which (worker, lane) is in use.
    // Normalise jobs response: API now returns {jobs:[...], total, ...}
    // but keep compat with the legacy bare-array response.
    const jobList = Array.isArray(jobs) ? jobs : (jobs.jobs || []);
    syncScreenshotBusyState(jobList, sessions.sessions || []);
    sortScreenshotGrid();

    // jobs table -- skip rebuild while a row's actions menu is open,
    // otherwise the 2-second refresh would tear it down underneath the
    // user. Counts in the tab header still tick.
    const sorted = [...jobList].sort((a,b) => (b.created_at || '').localeCompare(a.created_at || ''));
    const jtbody = document.querySelector('#jobsTable tbody');
    const menuOpen = !!document.querySelector('#jobsTable .menu.open');
    // Pager state: page index + page size persist across refresh()
    // ticks. _jobsPagerTotal is restamped so the pager UI rebuild
    // below can read it without re-sorting.
    const pageSize = _jobsPageSize();
    const total = sorted.length;
    const maxPage = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (_jobsPage > maxPage) _jobsPage = maxPage;     // clamp when total shrinks
    if (_jobsPage < 0)       _jobsPage = 0;
    const startIdx = _jobsPage * pageSize;
    const endIdx   = Math.min(startIdx + pageSize, total);
    const visible  = sorted.slice(startIdx, endIdx);
    if (menuOpen) {
      // leave the rendered rows alone this tick
    } else if (sorted.length === 0) {
      jtbody.innerHTML = '<tr><td colspan=10 class="empty">no jobs yet</td></tr>';
    } else {
      jtbody.innerHTML = visible.map(j => {
        const jid = esc(j.job_id);
        const mode = (j.options && j.options.mode) || 'fetch';
        // Mode badge in the jobs list. We collapse codegen-loop +
        // vision-agent under the visual "AI" umbrella (matches the
        // Submit form tab name) but keep the engine sub-label so
        // operators can tell the two apart at a glance.
        let modeLabel;
        if (mode === 'codegen-loop') {
          modeLabel = '<iconify-icon icon="lucide:sparkles"></iconify-icon> AI · LLM';
        } else if (mode === 'vision-agent') {
          modeLabel = '<iconify-icon icon="lucide:eye"></iconify-icon> AI · Simple';
        } else if (mode === 'rerun') {
          modeLabel = '<iconify-icon icon="lucide:code-2"></iconify-icon> code';
        } else {
          modeLabel = '<iconify-icon icon="lucide:file-down"></iconify-icon> fetch';
        }
        const laneIdx = (j.lane_idx !== null && j.lane_idx !== undefined)
          ? j.lane_idx
          : ((j.slot_idx !== null && j.slot_idx !== undefined) ? j.slot_idx : null);
        const canAttach = laneIdx !== null;
        const novncItem = j.novnc_url
          ? `<a href="${esc(j.novnc_url)}" target="_blank">${ico('play')} live noVNC</a>`
          : '';
        const attachItem = canAttach
          ? `<button onclick="attachTo('${jid}')">${ico('link')} attach next job here</button>`
          : '';
        // codegen-loop-specific menu items (script.py / attempts only exist for codegen-loop)
        const modeSpecificItems = (mode === 'codegen-loop')
          ? `<a href="/jobs/${jid}/script.py" target="_blank">${ico('code')} download script.py</a>
             <a href="/jobs/${jid}/attempts" target="_blank">${ico('list')} all attempts</a>`
          : `<a href="/jobs/${jid}/page.html" target="_blank">${ico('fileText')} captured HTML</a>`;
        // recipe save: available for all modes — backend recipe_suggestion endpoint
        // handles missing actions.json / script.py gracefully (AI investigation, rerun, vision-agent, etc.).
        const recipeSaveItem = `&nbsp;|&nbsp;
              <a href="javascript:void(0)" onclick="window.openRecipeSaveModal('${jid}')" title="この job を HostRegistry のレシピとして登録">${ico('bento')} recipe として保存</a>`;
        const codegenItems = modeSpecificItems + recipeSaveItem;
        const startedCell = j.started_at
          ? `<small title="開始 ${esc(j.started_at)} (${fmtAgoOrNever(j.started_at)})">${fmtClock(j.started_at)}</small>`
          : '<span class="empty">—</span>';
        const endedCell = j.completed_at
          ? `<small title="終了 ${esc(j.completed_at)} (${fmtAgoOrNever(j.completed_at)})">${fmtClock(j.completed_at)}</small>`
          : '<span class="empty">—</span>';
        // duration: started→completed for finished jobs, started→now while running.
        const durCell = fmtJobDuration(j);
        return `
        <tr>
          <td data-col="id"><code>${esc(j.job_id.substring(0,10))}</code></td>
          <td data-col="mode"><span class="badge">${modeLabel}</span></td>
          <td data-col="status"><span class="badge ${esc(j.status)}">${esc(j.status)}</span></td>
          <td data-col="url" class="url" title="${esc(j.url)}"><a href="${esc(j.url)}" target="_blank">${esc(j.url)}</a></td>
          <td data-col="worker">${j.worker_id ? `<code>${esc(j.worker_id)}</code>${canAttach ? ` <small>#${laneIdx}</small>` : ''}` : '<span class="empty">—</span>'}</td>
          <td data-col="started">${startedCell}</td>
          <td data-col="ended">${endedCell}</td>
          <td data-col="duration">${durCell}</td>
          <td data-col="actions">
            <div class="menu-wrap">
              <button class="action-btn" onclick="toggleMenu(this)" title="${tt('jobs.th.actions','actions')}">${ICONS.moreV}</button>
              <div class="menu">
                <button onclick="watchLive('${jid}')" title="Attach the Submit-tab Live panel to this job (Log / noVNC / Code / Gallery)"><span class="ico" style="color:#c0392b;">●</span> watch live (Log+noVNC+Code+Gallery)</button>
                ${novncItem}
                <a href="/ui/log/${jid}" target="_blank">${ico('signal')} live log (tail -f)</a>
                <a href="/ui/assets/${jid}" target="_blank">${ico('image')} screenshots</a>
                ${codegenItems}
                <a href="/jobs/${jid}/log.txt" target="_blank">${ico('list')} raw log file</a>
                <a href="/jobs/${jid}/result" target="_blank">${ico('code')} result JSON</a>
                ${
                  // Fetch-as-session: while a fetch is alive, the hub
                  // has registered a read-only session under
                  // j.session_id so the operator can grab cookies
                  // without waiting for the job to finish. Hidden once
                  // the job ends (the session is torn down then).
                  (j.session_id && j.status === 'running')
                    ? `<div class="divider"></div>
                       <button onclick="saveSessionCookiesToHost('${esc(j.session_id)}')" title="今のブラウザの Cookie を Host レジストリに保存 (fetch 実行中限定)"><iconify-icon icon="lucide:cookie"></iconify-icon> save cookies → host</button>`
                    : ''
                }
                <div class="divider"></div>
                <button onclick="rerun('${jid}')">${ico('refresh')} rerun</button>
                ${attachItem}
                <div class="divider"></div>
                <button class="danger" onclick="del('${jid}')">${ico('trash')} delete</button>
              </div>
            </div>
          </td>
        </tr>`;
      }).join('');
    }
    applyJobCols();
    renderJobsPager(total, startIdx, endIdx);
  } catch (e) {
    document.getElementById('status').textContent = 'error: ' + e.message;
  }
}

// ---- recent-jobs pager --------------------------------------------------
// Client-side pagination over the already-fetched jobs list (refresh()
// hits /jobs every poll tick, so all rows are local). Page index lives
// in module state so the operator's cursor survives the 2-second
// refresh -- without that, every tick would reset to page 0 and
// scrolling-to-page-3 would be impossible.
let _jobsPage = 0;
const JOBS_PAGE_SIZE_KEY = 'paprika.jobs.pageSize';
const JOBS_PAGE_SIZE_OPTIONS = [10, 20, 50, 100, 200];

function _jobsPageSize() {
  try {
    const v = parseInt(localStorage.getItem(JOBS_PAGE_SIZE_KEY) || '20', 10);
    return JOBS_PAGE_SIZE_OPTIONS.includes(v) ? v : 20;
  } catch (_) { return 20; }
}
function _jobsPageSizeSet(n) {
  try { localStorage.setItem(JOBS_PAGE_SIZE_KEY, String(n)); } catch (_) {}
}

function renderJobsPager(total, startIdx, endIdx) {
  const host = document.getElementById('jobsPager');
  if (!host) return;
  const pageSize = _jobsPageSize();
  const maxPage  = Math.max(0, Math.ceil(total / pageSize) - 1);
  if (total === 0) {
    host.innerHTML = '';
    return;
  }
  // Don't trigger refresh() while the actions menu is open -- the
  // host's innerHTML rebuild would close any menu the operator just
  // opened. Same protection as the table rebuild above.
  if (document.querySelector('#jobsTable .menu.open')) return;
  const display1 = startIdx + 1;            // 1-based for humans
  const display2 = endIdx;                  // already exclusive end → upper bound
  const prevDisabled = _jobsPage <= 0;
  const nextDisabled = _jobsPage >= maxPage;
  const opts = JOBS_PAGE_SIZE_OPTIONS
    .map(n => `<option value="${n}"${n === pageSize ? ' selected' : ''}>${n}</option>`)
    .join('');
  host.innerHTML = `
    <span style="color:#666;">${display1}-${display2} / ${total}</span>
    <button class="pill" id="jobsPagerPrev" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${prevDisabled ? 'disabled' : ''}>
      <iconify-icon icon="lucide:chevron-left"></iconify-icon> prev
    </button>
    <span style="color:#666;">page ${_jobsPage + 1} / ${maxPage + 1}</span>
    <button class="pill" id="jobsPagerNext" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${nextDisabled ? 'disabled' : ''}>
      next <iconify-icon icon="lucide:chevron-right"></iconify-icon>
    </button>
    <span style="margin-left:auto; color:#888; font-size:.85em;">
      per page <select id="jobsPagerSize" style="padding:2px 4px;">${opts}</select>
    </span>
  `;
  const prevBtn = document.getElementById('jobsPagerPrev');
  const nextBtn = document.getElementById('jobsPagerNext');
  const sizeSel = document.getElementById('jobsPagerSize');
  if (prevBtn) prevBtn.addEventListener('click', () => {
    if (_jobsPage > 0) { _jobsPage--; refresh(); }
  });
  if (nextBtn) nextBtn.addEventListener('click', () => {
    _jobsPage++; refresh();  // refresh() re-clamps to maxPage
  });
  if (sizeSel) sizeSel.addEventListener('change', () => {
    const n = parseInt(sizeSel.value, 10);
    if (JOBS_PAGE_SIZE_OPTIONS.includes(n)) {
      _jobsPageSizeSet(n);
      _jobsPage = 0;   // start of the new pagination
      refresh();
    }
  });
}

// ---- jobs column picker -------------------------------------------------
// The jobs table is column-heavy. Rather than cram start/end times in or
// drop existing data, we let the operator choose which columns to show.
// Selection is persisted in localStorage and re-applied after every poll
// re-render (rows are rebuilt ~every 2s, so visibility must be reasserted).
const JOB_COLS = [
  { key: 'id',       i18n: 'jobs.th.id',       fallback: 'id' },
  { key: 'mode',     i18n: 'jobs.th.mode',     fallback: 'mode' },
  { key: 'status',   i18n: 'jobs.th.status',   fallback: 'status' },
  { key: 'url',      i18n: null,               fallback: 'URL' },
  { key: 'worker',   i18n: 'jobs.th.worker',   fallback: 'worker/lane' },
  { key: 'started',  i18n: 'jobs.th.started',  fallback: 'started' },
  { key: 'ended',    i18n: 'jobs.th.ended',    fallback: 'ended' },
  { key: 'duration', i18n: 'jobs.th.duration', fallback: 'duration' },
  { key: 'actions',  i18n: 'jobs.th.actions',  fallback: 'actions', fixed: true },
];
// Columns hidden by default (operators can opt them back in). Kept lean so
// the default view stays readable while still surfacing start/end times.
const JOB_COLS_DEFAULT_HIDDEN = ['duration'];

function _jobColsHidden() {
  try {
    const raw = localStorage.getItem('paprika.jobs.cols');
    if (raw === null) return new Set(JOB_COLS_DEFAULT_HIDDEN);
    return new Set(JSON.parse(raw));
  } catch (_) { return new Set(JOB_COLS_DEFAULT_HIDDEN); }
}
function _jobColsSave(hidden) {
  try { localStorage.setItem('paprika.jobs.cols', JSON.stringify([...hidden])); } catch (_) {}
}

// Apply current visibility to every header + body cell carrying data-col.
function applyJobCols() {
  const hidden = _jobColsHidden();
  document.querySelectorAll('#jobsTable [data-col]').forEach(el => {
    el.style.display = hidden.has(el.getAttribute('data-col')) ? 'none' : '';
  });
}

// Build the checkbox list inside the picker dropdown.
function renderJobColsMenu() {
  const menu = document.getElementById('jobsColsMenu');
  if (!menu) return;
  const hidden = _jobColsHidden();
  const tr = (window.i18next && window.i18next.t) ? window.i18next.t.bind(window.i18next) : null;
  menu.innerHTML = JOB_COLS.map(c => {
    const label = (tr && c.i18n) ? tr(c.i18n) : c.fallback;
    const checked = c.fixed || !hidden.has(c.key) ? 'checked' : '';
    const dis = c.fixed ? 'disabled' : '';
    return `<label style="display:flex; align-items:center; gap:8px; padding:4px 10px; white-space:nowrap; cursor:${c.fixed ? 'default' : 'pointer'}; opacity:${c.fixed ? 0.5 : 1};">
      <input type="checkbox" data-col-toggle="${c.key}" ${checked} ${dis}> ${esc(label)}
    </label>`;
  }).join('');
  menu.querySelectorAll('input[data-col-toggle]').forEach(inp => {
    inp.addEventListener('change', () => {
      const key = inp.getAttribute('data-col-toggle');
      const h = _jobColsHidden();
      if (inp.checked) h.delete(key); else h.add(key);
      _jobColsSave(h);
      applyJobCols();
    });
  });
}

// Wire up the columns button (toggle dropdown, close on outside click).
(function initJobColsPicker() {
  function bind() {
    const btn = document.getElementById('jobsColsBtn');
    const menu = document.getElementById('jobsColsMenu');
    if (!btn || !menu) { setTimeout(bind, 200); return; }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = menu.classList.toggle('open');
      if (open) renderJobColsMenu();
    });
    document.addEventListener('click', (e) => {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        menu.classList.remove('open');
      }
    });
    applyJobCols();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else { bind(); }
})();

// Format a job's elapsed/total duration. running -> started→now (live),
// finished -> started→completed. Returns a dash when no start time yet.
function fmtJobDuration(j) {
  const start = j.started_at ? parseServerTime(j.started_at) : NaN;
  if (isNaN(start)) return '<span class="empty">—</span>';
  const end = j.completed_at ? parseServerTime(j.completed_at) : Date.now();
  let s = Math.max(0, Math.floor((end - start) / 1000));
  const live = !j.completed_at;
  let out;
  if (s < 60) out = s + 's';
  else if (s < 3600) out = Math.floor(s/60) + 'm' + (s % 60 ? (s%60)+'s' : '');
  else out = Math.floor(s/3600) + 'h' + (Math.floor((s%3600)/60) ? Math.floor((s%3600)/60)+'m' : '');
  return live ? `<small style="color:#a07000;">▶ ${out}</small>` : `<small>${out}</small>`;
}

// ---- sessions ----------------------------------------------------------

function renderSessions(items) {
  const tbody = document.querySelector('#sessionsTable tbody');
  if (!items || items.length === 0) {
    tbody.innerHTML = '<tr><td colspan=8 class="empty">no active sessions</td></tr>';
    return;
  }
  const fmtAgo = (iso) => {
    if (!iso) return '—';
    const s = Math.max(0, Math.floor((Date.now() - parseServerTime(iso)) / 1000));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s/60) + 'm ago';
    return Math.floor(s/3600) + 'h ago';
  };
  // state -> ( emoji, label, css-color )
  const stateBadge = (s) => {
    const st = s.state || 'idle';
    if (st === 'running') {
      const a = s.current_action ? `: ${esc(s.current_action)}` : '';
      return `<span title="action in flight" style="color:#a07000;">🟡 running${a}</span>`;
    }
    if (st === 'closing') {
      return `<span title="DELETE in progress" style="color:#aa3030;">🔴 closing</span>`;
    }
    return `<span title="open, waiting for next command" style="color:#208030;">🟢 idle</span>`;
  };
  tbody.innerHTML = items.map(s => {
    const sid = esc(s.session_id);
    const shortSid = sid.length > 14 ? sid.substring(0,12) + '…' : sid;
    const wid = esc(s.worker_id || '');
    const lane = (s.lane_idx ?? '') !== '' ? `#${s.lane_idx}` : '';
    const url = esc(s.initial_url || '');
    const novnc = s.novnc_url
      ? `<a href="${esc(s.novnc_url)}${s.novnc_url.includes('?') ? '&' : '?'}autoconnect=1&resize=scale&reconnect=1" target="_blank">↗ noVNC</a>`
      : '<span class="empty">—</span>';
    return `
      <tr>
        <td><code title="${sid}">${esc(shortSid)}</code></td>
        <td>${stateBadge(s)}</td>
        <td><code>${wid}</code> <small>${lane}</small></td>
        <td class="url" title="${url}">${url ? `<a href="${url}" target="_blank">${url}</a>` : '<span class="empty">—</span>'}</td>
        <td><span title="created ${esc(s.created_at || '')}">${fmtAgo(s.last_active_at || s.created_at)}</span></td>
        <td>${s.visited_count ?? 0}</td>
        <td>${novnc}</td>
        <td style="white-space:nowrap;">
          <button class="pill" style="background:#f3eeff; border-color:#b89fe0; color:#4a1f8a;"
                  onclick="openForensicsModal('${sid}')"
                  title="このセッションで Forensics 調査を実行 (LLM 読み取り/操作プローブ)">
            <iconify-icon icon="lucide:microscope"></iconify-icon> forensics
          </button>
          <button class="pill" style="background:#eef8ee; border-color:#7ab68a; color:#196b2c;"
                  onclick="saveSessionCookiesToHost('${sid}')"
                  title="今のブラウザの Cookie を Host レジストリに保存 (再ログイン不要に)">
            <iconify-icon icon="lucide:cookie"></iconify-icon> save → host
          </button>
          <button class="pill" onclick="closeSession('${sid}')" title="DELETE /sessions/${sid}">
            ${ico('trash')} close
          </button>
        </td>
      </tr>`;
  }).join('');
}

// "save cookies → host": fetch the session's current cookies from the
// worker, then open the existing Hosts modal pre-filled so the operator
// can review / edit / save. Host is inferred from the current URL but
// remains editable in the modal. By default the cookies are filtered
// to those that apply to the current host; the operator can re-fetch
// with the "all cookies" toggle below the textarea to see everything.
async function _fetchSessionCookies(sid, opts) {
  const showAll = !!(opts && opts.all);
  const explicitHost = (opts && opts.host) || '';
  const params = new URLSearchParams();
  if (showAll) params.set('all_cookies', 'true');
  if (explicitHost) params.set('host', explicitHost);
  const qs = params.toString();
  const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/cookies' + (qs ? ('?' + qs) : ''));
  if (!r.ok) {
    const err = await r.json().catch(() => null);
    throw new Error((err && err.detail) || ('HTTP ' + r.status));
  }
  return await r.json();
}

async function saveSessionCookiesToHost(sid) {
  try {
    const j = await _fetchSessionCookies(sid, {});
    const cookies = j.cookies || [];
    const currentUrl = j.current_url || '';
    const total = j.total_in_browser || cookies.length;
    let host = j.host_filter || '';
    if (!host) {
      try { host = new URL(currentUrl).hostname || ''; } catch (e) { host = ''; }
    }
    // Normalise client-side too so the input box shows the same host
    // the server would store (example.com vs www.example.com).
    if (host && host.startsWith('www.')) host = host.substring(4);
    // Pre-load the existing record (if any) so we can merge notes.
    let existingNotes = '';
    if (host) {
      try {
        const er = await fetch('/hosts/' + encodeURIComponent(host));
        if (er.ok) {
          const ex = await er.json();
          existingNotes = ex.notes || '';
        }
      } catch (e) {}
    }
    const titleEl = document.getElementById('hostModalTitle');
    const hostInput = document.getElementById('hostModalHost');
    const cookiesArea = document.getElementById('hostModalCookies');
    const notesInput = document.getElementById('hostModalNotes');
    const delBtn = document.getElementById('hostModalDelete');
    const filterInfo = cookies.length + ' / ' + total + ' cookies (matching ' + (host || '?') + ')';
    titleEl.textContent = 'Save browser cookies → ' + (host || 'host') + ' — ' + filterInfo;
    hostInput.value = host;
    hostInput.disabled = false;
    cookiesArea.value = JSON.stringify(cookies, null, 2);
    notesInput.value = existingNotes || ('imported from session ' + sid.substring(0, 12) + ' at ' + new Date().toISOString().substring(0, 19) + 'Z');
    delBtn.style.display = 'none';
    // Stash the session id on the modal so a "show all" toggle button
    // can re-fetch without arguments. We add the toggle below the
    // cookies textarea each time the import flow runs; idempotent.
    _ensureCookieRefetchToggle(sid, host);
    _openHostModal();
    setTimeout(() => {
      const hostsTab = document.querySelector('#tabs .tab[data-tab="hosts"]');
      if (hostsTab) hostsTab.click();
    }, 0);
  } catch (e) {
    alert('cookie fetch failed: ' + e.message);
  }
}

// Inject (once) a small toolbar inside the Hosts modal that lets the
// operator switch between "host-filtered" and "all cookies in browser"
// views when they got there via the "save → host" path. Hidden when
// they opened the modal manually via Add / Edit.
function _ensureCookieRefetchToggle(sid, host) {
  let bar = document.getElementById('hostModalCookieToolbar');
  if (!bar) {
    const cookiesArea = document.getElementById('hostModalCookies');
    if (!cookiesArea || !cookiesArea.parentNode) return;
    bar = document.createElement('div');
    bar.id = 'hostModalCookieToolbar';
    bar.style.cssText = 'display:flex; gap:8px; align-items:center; margin-top:-4px; padding:6px 0; font-size:0.85em; color:#666;';
    bar.innerHTML = `
      <button type="button" id="hostModalCookieFilterMatch" class="pill" style="background:#eef8ff; border-color:#9bf;" title="現在の host に一致する Cookie のみを表示">🎯 host-match only</button>
      <button type="button" id="hostModalCookieFilterAll" class="pill" style="background:#f5f5fa; border-color:#bbc; color:#444;" title="ブラウザのすべての Cookie (cross-site トラッカー含む)">🌐 all cookies in browser</button>
      <span id="hostModalCookieFilterHint" style="margin-left:auto; color:#888;"></span>`;
    cookiesArea.parentNode.insertBefore(bar, cookiesArea);
  }
  bar.style.display = 'flex';
  bar.dataset.sid = sid;
  bar.dataset.host = host || '';
  const matchBtn = document.getElementById('hostModalCookieFilterMatch');
  const allBtn = document.getElementById('hostModalCookieFilterAll');
  matchBtn.onclick = async () => {
    try {
      const j = await _fetchSessionCookies(sid, { host });
      document.getElementById('hostModalCookies').value = JSON.stringify(j.cookies || [], null, 2);
      document.getElementById('hostModalCookieFilterHint').textContent =
        (j.cookies || []).length + ' / ' + (j.total_in_browser || 0) + ' shown';
    } catch (e) { alert('refetch failed: ' + e.message); }
  };
  allBtn.onclick = async () => {
    try {
      const j = await _fetchSessionCookies(sid, { all: true });
      document.getElementById('hostModalCookies').value = JSON.stringify(j.cookies || [], null, 2);
      document.getElementById('hostModalCookieFilterHint').textContent =
        (j.cookies || []).length + ' shown (no filter)';
    } catch (e) { alert('refetch failed: ' + e.message); }
  };
}

// Hide the cookie-refetch toolbar when the modal is opened via add/edit
// (we only want it for the session-import flow).
function _hideCookieRefetchToggle() {
  const bar = document.getElementById('hostModalCookieToolbar');
  if (bar) bar.style.display = 'none';
}

async function closeSession(sid) {
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid), { method: 'DELETE' });
    if (!r.ok && r.status !== 404) {
      alert('close failed: ' + r.status);
    }
  } catch (e) {
    alert('close failed: ' + e.message);
  }
  refresh();
}

async function closeAllSessions() {
  if (!confirm('Close ALL active sessions?')) return;
  try {
    const r = await fetch('/sessions').then(r => r.json());
    await Promise.all((r.sessions || []).map(s =>
      fetch('/sessions/' + encodeURIComponent(s.session_id), { method: 'DELETE' })
        .catch(() => null)
    ));
  } catch (e) {
    alert('close-all failed: ' + e.message);
  }
  refresh();
}

async function openSessionInteractive() {
  const url = prompt('Initial URL? (leave empty for about:blank)', 'https://example.com');
  if (url === null) return;
  const body = {};
  if (url) body.initial_url = url;
  try {
    const r = await fetch('/sessions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('open failed: ' + ((err && err.detail) || r.status));
      return;
    }
    const info = await r.json();
    // Open the live noVNC for the new session in a new tab so the
    // operator can see what they just spun up.
    if (info.novnc_url_autoconnect) {
      window.open(info.novnc_url_autoconnect, '_blank');
    }
  } catch (e) {
    alert('open failed: ' + e.message);
  }
  refresh();
}

async function setWorkerStatus(workerId, status) {
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId) + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('status update failed: ' + ((err && err.detail) || r.status));
    }
  } catch (e) {
    alert('status update failed: ' + e.message);
  }
  refresh();
}

// ---- worker actions menu (Workers tab "..." dropdown) -------------------
// Mirrors the Jobs-tab actions menu visually: a "..." button per row opens
// a popover with the worker's recent activity log + a delete button.
// Designed to defer the /workers/{id}/logs round-trip until the menu
// actually opens so the 2-second tab refresh doesn't fire one per worker.

// Simplified: the menu is now just a list of action items (詳細 / delete /
// future...). All the heavy info rendering moved to openWorkerDetailModal
// below so the dropdown stays narrow and has room for more items.
window.toggleWorkerMenu = function(btn, workerId) {
  try { window.toggleMenu(btn); }
  catch (_) {
    const menu = btn.nextElementSibling;
    if (menu) menu.classList.toggle('open');
  }
};

// "詳細" menu item handler. Pulls the worker's live snapshot + recent
// activity log into the workerDetailModal <dialog>. Reuses
// renderWorkerInfoBlock so the rendered info matches what the old
// inline dropdown used to show, plus the same logs panel.
window.openWorkerDetailModal = async function(workerId) {
  const dlg = document.getElementById('workerDetailModal');
  const body = document.getElementById('workerDetailBody');
  if (!dlg || !body) return;
  // Close any open kebab menu first so the dropdown doesn't shadow
  // the modal's backdrop on slow renders.
  try {
    document.querySelectorAll('#workersTable .menu.open')
      .forEach(m => m.classList.remove('open'));
  } catch (_) {}
  // Stash the active workerId so the refresh button can re-run.
  dlg.__workerId = workerId;
  // Resolve from the last /workers payload so we don't wait on a
  // separate round-trip just for the static info block; logs are
  // fetched in parallel below.
  let snap = null;
  try {
    snap = (window._lastWorkersPayload || []).find(w => w.worker_id === workerId);
  } catch (_) {}
  body.innerHTML = renderWorkerInfoBlock(snap, workerId) + `
    <div style="margin-top:14px; padding-top:10px; border-top:1px solid #eee;">
      <div style="font-weight:600; color:#555; margin-bottom:6px;">recent activity</div>
      <div data-worker-logs-modal style="font-family: ui-monospace, Menlo, Consolas, monospace; font-size:.82em; background:#fafbfc; border:1px solid #eee; border-radius:4px; padding:8px 10px; max-height:320px; overflow-y:auto; color:#444;">
        <div style="color:#888;">loading…</div>
      </div>
    </div>`;
  if (typeof dlg.showModal === 'function') {
    try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); }
  } else {
    dlg.setAttribute('open', '');
  }
  // Keep the URL bar in sync so the page is bookmarkable / shareable.
  if (typeof _entityHashSync === 'function') _entityHashSync('workers', workerId);
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId) + '/logs?limit=200');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const logHost = body.querySelector('[data-worker-logs-modal]');
    if (!logHost) return;
    const rows = data.logs || [];
    if (rows.length === 0) {
      logHost.innerHTML = '<div style="color:#aaa;">no activity recorded yet (hub may have restarted)</div>';
      return;
    }
    logHost.innerHTML = rows.map(row => {
      const t = (row.ts ? new Date(row.ts * 1000).toLocaleTimeString() : '');
      const kind = row.kind || 'info';
      const colour = ({
        error:     '#c0392b',
        warn:      '#d4a13d',
        lifecycle: '#3a5ca8',
        job:       '#444',
        info:      '#666',
      })[kind] || '#444';
      return `<div style="white-space:pre-wrap; word-break:break-word; line-height:1.35;">
        <span style="color:#999;">${esc(t)}</span>
        <span style="color:${colour}; font-weight:600; margin-left:4px;">${esc(kind)}</span>
        <span style="margin-left:4px;">${esc(row.line || '')}</span>
      </div>`;
    }).join('');
    logHost.scrollTop = logHost.scrollHeight;
  } catch (e) {
    const logHost = body.querySelector('[data-worker-logs-modal]');
    if (logHost) {
      logHost.innerHTML = `<div style="color:#c0392b;">failed to load logs: ${esc(e.message)}</div>`;
    }
  }
};

// Wire the modal's close + refresh buttons at DOM ready. Safe to
// run multiple times -- replaces any previously-attached handlers
// because we use direct .onclick assignment (not addEventListener).
(function _wireWorkerDetailModal() {
  const dlg = document.getElementById('workerDetailModal');
  if (!dlg) return; // not on this page
  const closeBtn = document.getElementById('workerDetailClose');
  if (closeBtn) {
    closeBtn.onclick = () => {
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
    };
  }
  // Clear the hash when the dialog closes (via button, ESC, or backdrop).
  dlg.addEventListener('close', () => {
    if (typeof _entityHashClear === 'function') _entityHashClear('workers');
  });
  const refreshBtn = document.getElementById('workerDetailRefresh');
  if (refreshBtn) {
    refreshBtn.onclick = () => {
      const wid = dlg.__workerId;
      if (wid) window.openWorkerDetailModal(wid);
    };
  }
  // Share-link button: sync hash then copy the URL to clipboard.
  const shareBtn = document.getElementById('workerDetailShareLink');
  if (shareBtn) {
    shareBtn.onclick = () => {
      const wid = dlg.__workerId;
      if (!wid) return;
      // Make sure the hash is current before we copy.
      if (typeof _entityHashSync === 'function') _entityHashSync('workers', wid);
      const url = location.href;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).catch(() => {});
      } else {
        // Fallback for older browsers / HTTP contexts.
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch (_) {}
        document.body.removeChild(ta);
      }
      // Brief "done" feedback on the button itself.
      const orig = shareBtn.innerHTML;
      shareBtn.innerHTML = '<iconify-icon icon="lucide:check"></iconify-icon> コピー完了!';
      shareBtn.style.color = '#196b2c';
      shareBtn.style.borderColor = '#7ab68a';
      shareBtn.style.background = '#eef8ee';
      setTimeout(() => {
        shareBtn.innerHTML = orig;
        shareBtn.style.color = '';
        shareBtn.style.borderColor = '';
        shareBtn.style.background = '';
      }, 1500);
    };
  }
})();

function renderWorkerInfoBlock(w, workerId) {
  if (!w) {
    return `<div style="color:#888;">worker <code>${esc(workerId)}</code> (no live snapshot)</div>`;
  }
  const aliveDot = w.alive
    ? '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#3a8c3a; margin-right:6px;"></span>'
    : '<span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#bbb; margin-right:6px;"></span>';
  const labels = Object.entries(w.labels || {}).map(([k,v]) => `${k}=${v}`).join(', ') || '—';
  const profileNames = (w.profiles_cached || []).map(p => p.name).join(', ') || '—';
  const last = w.last_heartbeat
    ? new Date(w.last_heartbeat * 1000).toLocaleString()
    : '—';
  return `
    <div style="display:grid; grid-template-columns: max-content 1fr; gap:4px 12px; color:#333;">
      <div style="color:#888;">worker_id</div><div><code>${esc(w.worker_id)}</code></div>
      <div style="color:#888;">state</div><div>${aliveDot}${esc(w.alive ? 'alive' : 'offline')} <small style="color:#999;">(${esc(w.status || '?')})</small></div>
      <div style="color:#888;">address</div><div>${w.address ? `<code>${esc(w.address)}</code>` : '<span class="empty">—</span>'}</div>
      <div style="color:#888;">load</div><div>${esc(String(w.in_flight))} / ${esc(String(w.capacity))}</div>
      <div style="color:#888;">version</div><div>${w.version ? `<code>${esc(w.version)}</code>` : '<span class="empty">—</span>'}</div>
      <div style="color:#888;">labels</div><div>${esc(labels)}</div>
      <div style="color:#888;">profiles</div><div>${esc(profileNames)}</div>
      <div style="color:#888;">last heartbeat</div><div>${esc(last)}</div>
    </div>`;
}

// Tiny CSS.escape polyfill for older browsers (worker_id is normally
// safe alnum + dashes, but the menu uses it inside an attribute
// selector so we still want to be defensive).
function cssEscape(s) {
  if (window.CSS && window.CSS.escape) return window.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, ch => '\\' + ch);
}

window.deleteWorker = async function(workerId) {
  if (!confirm(`Forget worker "${workerId}"?\n\nThis removes its history from the hub (Redis row + in-process logs). The worker is NOT contacted. It can still re-register at any time.`)) {
    return;
  }
  try {
    const r = await fetch('/workers/' + encodeURIComponent(workerId), { method: 'DELETE' });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('delete failed: ' + ((err && err.detail) || r.status));
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  refresh();
};

async function del(id) {
  if (!confirm(`delete job ${id}?`)) return;
  await fetch('/jobs/' + id, { method: 'DELETE' });
  refresh();
}

// "watch live" -- attach the Submit-tab Live panel (tabbed Log / noVNC
// / Code / Gallery) to an existing job. Useful for jobs you didn't
// just submit yourself (e.g. one another user kicked off, or a
// long-running job whose Submit panel you accidentally closed).
//
// Switches to the Submit tab so the panel is actually visible -- the
// panel is nested inside the Submit pane and is hidden on every other
// tab.
function watchLive(id) {
  // updateHash:false so we don't flash #submit into the URL -- ljpAttach
  // sets the shareable #live/<id> hash itself.
  setTab('submit', { updateHash: false });
  // Defer one tick so the Submit pane actually paints before we ask
  // ljpAttach to start mounting iframes / WS connections inside it.
  setTimeout(() => ljpAttach(id), 0);
}

async function rerun(id) {
  const info = await fetch('/jobs/' + id).then(r => r.json());
  if (!info || !info.url) return;
  const r = await fetch('/jobs', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ url: info.url, options: info.options || {} }),
  });
  if (!r.ok) { alert('rerun failed: ' + r.status); return; }
  refresh();
}
async function attachTo(id) {
  // Re-fetch the previous job's URL and trigger a fresh fetch-mode
  // submit pinned to the same lane. Lives on Recent Jobs row's
  // actions menu now that the Submit form no longer has an
  // "attach to job" input (Submit was simplified in PR-14).
  let prevUrl = '';
  try {
    const j = await fetch('/jobs/' + encodeURIComponent(id)).then(r => r.json());
    prevUrl = j.url || '';
  } catch (_) {}
  if (!prevUrl) {
    alert('could not look up the previous job\'s URL');
    return;
  }
  const r = await fetch('/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url: prevUrl,
      options: {
        mode: 'fetch',
        scroll: true,
        attach_to_job: id,
      },
    }),
  });
  if (!r.ok) { alert('attach-rerun failed: ' + r.status); return; }
  setTab('jobs');
  refresh();
}
// --- bulk cleanup (kept-last-N + age filter) ----------------------------
//   1) ask the user for "older than N days" + keep_last
//   2) POST dry_run=true to /jobs/cleanup, show preview
//   3) if confirmed, POST dry_run=false
async function bulkCleanup() {
  // Step 1: gather inputs.
  const olderRaw = prompt(
    "Delete completed jobs older than how many days? (blank = any age)\n"
    + "  - In-flight jobs are NEVER deleted.\n"
    + "  - The last 10 most-recent jobs are always kept (protected_count).",
    "7"
  );
  if (olderRaw === null) return;
  const older = olderRaw.trim() === '' ? null : parseInt(olderRaw, 10);
  if (olderRaw.trim() !== '' && !(older >= 0)) {
    alert('age must be a non-negative integer or blank');
    return;
  }
  const btn = document.getElementById('bulkCleanup');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> scanning…';
  // Step 2: dry-run preview.
  let preview;
  try {
    const body = {dry_run: true, keep_last: 10};
    if (older !== null) body.older_than_days = older;
    const r = await fetch('/jobs/cleanup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      alert('cleanup preview failed (HTTP ' + r.status + ')');
      return;
    }
    preview = await r.json();
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
  const n = preview.candidate_count;
  const totalMB = (preview.candidate_total_bytes / (1024*1024)).toFixed(1);
  if (n === 0) {
    alert(`No matching jobs found (skipped ${preview.skipped.length}). Nothing to clean.`);
    return;
  }
  if (!confirm(
    `${n} job(s) match (${totalMB} MiB total). Delete now?\n\n`
    + `Sample:\n` + preview.candidates.slice(0, 5).map(c =>
        `  ${c.job_id} · ${c.status} · ${(c.size_bytes/(1024*1024)).toFixed(1)} MiB`
        + (c.age_days ? ` · ${c.age_days.toFixed(1)}d old` : '')
      ).join('\n')
    + (n > 5 ? `\n  ...and ${n - 5} more` : '')
  )) return;
  // Step 3: actually delete.
  btn.disabled = true;
  btn.innerHTML = `<span class="ljp-spinner"></span> deleting ${n}…`;
  try {
    const body = {dry_run: false, keep_last: 10};
    if (older !== null) body.older_than_days = older;
    const r = await fetch('/jobs/cleanup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      alert('cleanup failed (HTTP ' + r.status + ')');
      return;
    }
    const result = await r.json();
    const freedMB = (result.total_freed_bytes / (1024*1024)).toFixed(1);
    alert(`Deleted ${result.deleted.length} job(s), freed ${freedMB} MiB.`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
    refresh();
  }
}

async function bulkDelete() {
  const raw = await fetch('/jobs').then(r => r.json());
  const jobs = Array.isArray(raw) ? raw : (raw.jobs || []);
  if (!jobs.length) return;
  if (!confirm(`Delete ALL ${jobs.length} jobs (with their files)?`)) return;
  const btn = document.getElementById('bulkDelete');
  const origLabel = btn.innerHTML;
  let done = 0;
  const total = jobs.length;
  btn.disabled = true;
  btn.style.background = '#f8e6c8';
  btn.style.borderColor = '#d99';
  btn.innerHTML = `<span class="ljp-spinner"></span> deleting 0 / ${total}…`;
  const updateLabel = () => {
    btn.innerHTML = `<span class="ljp-spinner"></span> deleting ${done} / ${total}…`;
  };
  // Limit concurrency so we don't fire 100+ DELETEs simultaneously --
  // each one does a large `rm -rf` server-side and can starve the disk.
  const concurrency = 6;
  let cursor = 0;
  async function worker() {
    while (cursor < jobs.length) {
      const j = jobs[cursor++];
      try {
        await fetch('/jobs/' + j.job_id, {method:'DELETE'});
      } catch (_) {}
      done += 1;
      updateLabel();
    }
  }
  await Promise.all(Array.from({length: concurrency}, () => worker()));
  btn.disabled = false;
  btn.style.background = '#eee';
  btn.style.borderColor = '#ccc';
  btn.innerHTML = origLabel;
  refresh();
}

// --- live preview -----------------------------------------------------------
const ssTiles = new Map();
let ssTimer = null;

function ssKey(workerId, lane) { return workerId + '/' + lane; }

function buildTile(workerId, laneIdx, novncUrl) {
  // Use an <a> when we have a click-through URL, plain <div> otherwise.
  // The "↗ open" badge on the top-right hints that the tile is clickable.
  const wrap = document.createElement(novncUrl ? 'a' : 'div');
  // Start in 'loading' state -- the first /preview round-trip can be
  // 1-2 seconds when the worker's lane just woke up and ffmpeg hasn't
  // primed Xvfb's frame buffer yet. The CSS overlay (.ssitem.loading)
  // paints a diagonal stripe + spinner so the tile looks intentional
  // instead of "broken / black". Cleared on the first img 'load' or
  // 'error' event below.
  wrap.className = 'ssitem idle loading';
  if (novncUrl) {
    let url = novncUrl;
    if (!url.includes('autoconnect')) {
      url += (url.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
    }
    // Cache the LAN-direct URL on the element so syncScreenshotBusyState
    // can fall back to it when a lane is idle (no active session ->
    // nothing to hub-proxy). Operators occasionally want to peek at an
    // idle lane's fluxbox desktop directly for debugging.
    wrap.dataset.directUrl = url;
    wrap.href = url;
    wrap.target = '_blank';
    wrap.rel = 'noopener';
    wrap.title = 'Open noVNC viewer in a new tab';
  }
  const img = document.createElement('img');
  img.alt = workerId + ' #' + laneIdx; img.loading = 'lazy';
  const lbl = document.createElement('span');
  lbl.className = 'sslabel'; lbl.textContent = workerId + ' #' + laneIdx;
  // Sub-label for the running job's URL (set/cleared in
  // syncScreenshotBusyState).
  const sub = document.createElement('span');
  sub.className = 'sssub';
  sub.style.display = 'none';
  // RUNNING / IDLE badge, updated by syncScreenshotBusyState.
  const badge = document.createElement('span');
  badge.className = 'ssbadge idle';
  badge.innerHTML = '<span class="dot"></span><span class="ssbadge-text">IDLE</span>';
  const open = document.createElement('span');
  open.className = 'ssopen'; open.textContent = '↗ noVNC';
  const err = document.createElement('span');
  err.className = 'sserr'; err.style.display = 'none';
  wrap.appendChild(img); wrap.appendChild(lbl); wrap.appendChild(sub);
  if (novncUrl) wrap.appendChild(open);
  wrap.appendChild(badge);
  wrap.appendChild(err);
  img.addEventListener('error', () => {
    err.textContent = 'capture failed (worker offline or lane not ready)';
    err.style.display = 'block';
    // Even an error clears the loading state -- the err stripe at the
    // bottom is the operator's signal that something went wrong, the
    // spinning overlay just gets in the way.
    wrap.classList.remove('loading');
  });
  img.addEventListener('load', () => {
    err.style.display = 'none';
    // First successful frame -- drop the loading overlay so polling
    // refreshes silently swap pixels from here on out. Idempotent
    // (no-op if already removed) so re-firing on every poll is fine.
    wrap.classList.remove('loading');
  });
  return { wrap, img, err, sub, badge };
}

function syncScreenshotGrid(workers) {
  const grid = document.getElementById('ssGrid');
  const want = new Set();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    for (let i = 0; i < cap; i++) want.add(ssKey(w.worker_id, i));
  }
  for (const [k, t] of [...ssTiles.entries()]) {
    if (!want.has(k)) { t.wrap.remove(); ssTiles.delete(k); }
  }
  if (want.size === 0) {
    if (ssTiles.size === 0) {
      grid.innerHTML = '<div class="empty">no workers connected</div>';
    }
    return;
  }
  const placeholder = grid.querySelector('.empty');
  if (placeholder) placeholder.remove();
  for (const w of workers) {
    const cap = Math.max(1, w.capacity || 1);
    // Prefer new field name; fall back to legacy alias.
    const urls = w.lane_novnc_urls || w.slot_novnc_urls || [];
    for (let i = 0; i < cap; i++) {
      const key = ssKey(w.worker_id, i);
      if (ssTiles.has(key)) continue;
      const tile = buildTile(w.worker_id, i, urls[i]);
      grid.appendChild(tile.wrap);
      ssTiles.set(key, tile);
    }
  }
}

// Update each tile's RUNNING / IDLE badge from the live jobs list.
// A lane is "busy" when there's at least one job with status=running
// whose worker_id + lane_idx point at it, OR when an active session
// is currently holding that lane. The session check matters for
// codegen-loop / vision-agent jobs where the JobInfo itself doesn't
// carry worker_id (the runner container drives via /sessions/*, and
// only the SessionInfo records the worker+lane assignment).
// Idle lanes get a quieter look so the eye lands on the active ones.
// Called from refresh() every 2s after the workers+jobs round-trip.
function syncScreenshotBusyState(jobs, sessions) {
  // worker_id|lane -> { job, session, label } (we keep the freshest
  // running mapping for the sub-label).
  const busy = new Map();
  for (const j of (jobs || [])) {
    if (j.status !== 'running') continue;
    if (j.worker_id == null) continue;
    // lane_idx absent or null -> can't map to a lane tile.
    if (j.lane_idx == null) continue;
    busy.set(ssKey(j.worker_id, j.lane_idx), { job: j, session: null });
  }
  // Sessions overlay -- a codegen-loop job's lane only shows up here.
  // If a job-driven entry already exists for this (worker, lane), keep
  // it (more informative label); otherwise synthesize a "session-only"
  // entry so the tile still flips to busy.
  for (const s of (sessions || [])) {
    if (!s) continue;
    if (s.worker_id == null || s.lane_idx == null) continue;
    const key = ssKey(s.worker_id, s.lane_idx);
    if (!busy.has(key)) {
      busy.set(key, { job: null, session: s });
    }
  }
  for (const [key, tile] of ssTiles) {
    const entry = busy.get(key);
    const job = entry && entry.job;
    const sess = entry && entry.session;
    if (entry) {
      // KEEPALIVE = crawl is done but the session is alive for the
      // operator to drive via noVNC. Detected via the job's
      // progress.phase set by WorkerJobComplete when keep_session=True.
      // Falls back to RUNNING for in-progress crawls + codegen-loop
      // sessions (which don't have the keepalive phase).
      const _phase = job && job.progress && job.progress.phase;
      const isKeepalive = _phase === 'keepalive';
      // "downloading": fetch finished, a detached yt-dlp download is
      // still uploading the video. Distinct orange-ish badge.
      const isDownloading = _phase === 'downloading';
      tile.wrap.classList.add('busy');
      tile.wrap.classList.remove('idle');
      tile.badge.className =
        (isKeepalive || isDownloading) ? 'ssbadge keepalive' : 'ssbadge running';
      const txt = tile.badge.querySelector('.ssbadge-text');
      if (txt) txt.textContent =
        isDownloading ? 'DOWNLOADING' : (isKeepalive ? 'KEEPALIVE' : 'RUNNING');
      // Title + sub-label: prefer the job URL when we have it, otherwise
      // fall back to the session's current_url / initial_url so codegen-
      // loop / vision-agent jobs still give the operator something
      // legible.
      const labelUrl = (job && job.url)
        || (sess && (sess.current_url || sess.initial_url))
        || '';
      const labelJobId = (job && job.job_id) || (sess && sess.job_id) || '';
      tile.wrap.title =
        `Running job ${(labelJobId || '').slice(0, 12)} — ${labelUrl}`;
      if (tile.sub) {
        tile.sub.textContent = labelUrl || `(job ${labelJobId})`;
        tile.sub.style.display = '';
      }
      // Click-through URL: if a session is bound to this lane, prefer
      // the session-rooted hub-proxy URL so the operator's click opens
      // a URL that doesn't expose worker LAN IPs (matches /jobs/{id}
      // novnc_url rewriting). Falls back to job-side novnc_url
      // (already proxied), then the LAN-direct cached on the tile.
      if (tile.wrap.tagName === 'A') {
        const sid = (sess && sess.session_id)
                 || (job && job.session_id);
        let nextHref = tile.wrap.dataset.directUrl || '';
        if (sid) {
          nextHref =
            `/sessions/${encodeURIComponent(sid)}/novnc/` +
            `?path=sessions/${encodeURIComponent(sid)}/novnc/websockify` +
            `&autoconnect=1&resize=scale&reconnect=1`;
        } else if (job && job.novnc_url) {
          // /jobs/{id} responses are already proxy-URL rewritten
          // server-side by _proxy_info; trust it.
          nextHref = job.novnc_url;
        }
        if (nextHref) tile.wrap.href = nextHref;
      }
    } else {
      tile.wrap.classList.add('idle');
      tile.wrap.classList.remove('busy');
      tile.badge.className = 'ssbadge idle';
      const txt = tile.badge.querySelector('.ssbadge-text');
      if (txt) txt.textContent = 'IDLE';
      if (tile.sub) {
        tile.sub.textContent = '';
        tile.sub.style.display = 'none';
      }
      // Reset title to its "open noVNC" hint when no job is running.
      if (tile.wrap.tagName === 'A') {
        tile.wrap.title = 'Open noVNC viewer in a new tab';
        // Lane is idle -> no session -> revert to the LAN-direct URL
        // cached at tile-build time. Idle lanes have no hub-proxy URL
        // (no session_id to use as the route key); operators clicking
        // an idle tile see the worker's fluxbox desktop directly.
        if (tile.wrap.dataset.directUrl) {
          tile.wrap.href = tile.wrap.dataset.directUrl;
        }
      } else {
        tile.wrap.title = '';
      }
    }
  }
}

// Sort tiles in the grid based on the operator's chosen order.
// Called after syncScreenshotBusyState so status classes are current.
// Mutates only DOM order; ssTiles Map stays intact.
function sortScreenshotGrid() {
  const grid = document.getElementById('ssGrid');
  if (!grid || ssTiles.size === 0) return;
  const mode = (document.getElementById('ssSort') || {}).value || 'default';
  if (mode === 'default') return;           // insertion order, nothing to do

  // Build a sortable array of [key, tile, sortVal].
  const statusRank = (tile) => {
    if (tile.wrap.classList.contains('busy')) {
      // running (red) = 0, keepalive (orange) = 1
      return tile.badge.classList.contains('keepalive') ? 1 : 0;
    }
    return 2; // idle
  };

  const entries = [...ssTiles.entries()].map(([key, tile]) => {
    const [wid, lane] = key.split('/');
    return { key, tile, wid, lane: parseInt(lane, 10) || 0, status: statusRank(tile) };
  });

  if (mode === 'status') {
    // Running → Keepalive → Idle ; within same status: worker → lane
    entries.sort((a, b) =>
      (a.status - b.status)
      || a.wid.localeCompare(b.wid)
      || (a.lane - b.lane)
    );
  } else if (mode === 'worker') {
    entries.sort((a, b) =>
      a.wid.localeCompare(b.wid) || (a.lane - b.lane)
    );
  } else if (mode === 'worker-desc') {
    entries.sort((a, b) =>
      b.wid.localeCompare(a.wid) || (b.lane - a.lane)
    );
  }

  // Re-append in sorted order (moves existing DOM nodes, doesn't clone).
  for (const e of entries) grid.appendChild(e.tile.wrap);
}

// Resolve the operator's chosen tile size (width × quality) from the
// ssSize <select>. Falls back to the small preset if the control is
// missing or malformed. Persisted to localStorage so a refresh picks
// the same setting (cuts bandwidth when reloading the dashboard).
function ssCurrentSize() {
  const el = document.getElementById('ssSize');
  const raw = (el && el.value) || '320:30';
  const [w, q] = raw.split(':');
  return {
    width: Math.max(80, Math.min(1920, parseInt(w, 10) || 320)),
    quality: Math.max(0, Math.min(100, parseInt(q, 10) || 30)),
  };
}

function refreshScreenshots() {
  if (!document.getElementById('ssEnabled').checked) return;
  const t = Date.now();
  const { width, quality } = ssCurrentSize();
  for (const [key, tile] of ssTiles) {
    // Skip tiles that are still loading from the previous cycle.
    if (tile._loading) continue;
    const [wid, lane] = key.split('/');
    const url =
      `/workers/${encodeURIComponent(wid)}/lanes/${encodeURIComponent(lane)}/preview` +
      `?width=${width}&quality=${quality}&t=${t}`;
    // Double-buffer: preload the new frame off-screen, then swap only
    // after it's fully decoded. This avoids the flash-to-blank that
    // happens when img.src is assigned directly (browser clears the
    // old pixels before the new response arrives).
    const probe = new Image();
    tile._loading = true;
    probe.onload = () => {
      tile.img.src = probe.src;   // instant swap — already cached
      tile._loading = false;
    };
    probe.onerror = () => {
      // Surface the error on the visible img so the existing
      // error-overlay logic (sserr) fires.
      tile.img.src = url;
      tile._loading = false;
    };
    probe.src = url;
  }
}
function resetScreenshotTimer() {
  if (ssTimer) clearInterval(ssTimer);
  const sec = Math.max(1, parseInt(document.getElementById('ssInterval').value, 10) || 5);
  ssTimer = setInterval(refreshScreenshots, sec * 1000);
  refreshScreenshots();
}
function applyCols() {
  const v = document.getElementById('ssCols').value;
  const grid = document.getElementById('ssGrid');
  if (v === 'auto') {
    grid.style.setProperty('--ss-cols', 'repeat(auto-fill, minmax(260px, 1fr))');
  } else {
    grid.style.setProperty('--ss-cols', `repeat(${parseInt(v,10)}, 1fr)`);
  }
}

document.getElementById('ssInterval').addEventListener('change', resetScreenshotTimer);
document.getElementById('ssEnabled').addEventListener('change', () => {
  if (document.getElementById('ssEnabled').checked) resetScreenshotTimer();
  else if (ssTimer) { clearInterval(ssTimer); ssTimer = null; }
});
document.getElementById('ssCols').addEventListener('change', applyCols);
// Sort change: re-sort immediately, persist to localStorage.
(function wireSsSort() {
  const el = document.getElementById('ssSort');
  if (!el) return;
  try {
    const stored = localStorage.getItem('paprika.ssSort');
    if (stored) el.value = stored;
  } catch (_) {}
  el.addEventListener('change', () => {
    try { localStorage.setItem('paprika.ssSort', el.value); } catch (_) {}
    sortScreenshotGrid();
  });
})();
// Size change: trigger an immediate re-render so the operator sees
// the new quality/width without waiting for the next polling tick.
// Persist to localStorage so the dashboard remembers across reloads.
(function wireSsSize() {
  const el = document.getElementById('ssSize');
  if (!el) return;
  try {
    const stored = localStorage.getItem('paprika.ssSize');
    if (stored) el.value = stored;
  } catch (_) {}
  el.addEventListener('change', () => {
    try { localStorage.setItem('paprika.ssSize', el.value); } catch (_) {}
    refreshScreenshots();
  });
})();
applyCols();
// Don't start the polling loop on page load -- setTab() will arm it
// when (and only when) the user opens the Live Preview tab. Without
// this guard, every page load (including direct-link to e.g.
// #submit) starts a screenshot poll that runs invisibly and
// degrades typing/clicking responsiveness in the active tab.
if ((location.hash || '').replace(/^#/, '').split('/')[0] === 'screens') {
  resetScreenshotTimer();
}

document.getElementById('bulkDelete').addEventListener('click', bulkDelete);
document.getElementById('bulkCleanup').addEventListener('click', bulkCleanup);
document.getElementById('openSessionBtn').addEventListener('click', openSessionInteractive);
document.getElementById('closeAllSessions').addEventListener('click', closeAllSessions);

// Default goal stuffed when LLM mode is picked with an empty Goal field.
// Tuned for a multi-hour to single-day crawl (target 10,000 pages). The
// LLM is told to use pap.walk() explicitly so it doesn't reach for a
// hand-rolled BFS loop -- the latter consistently miscounts (i++ on
// dedup skips) and trips UNDER-TARGET on long runs, while pap.walk
// handles dedup, dead-end filtering, and depth bound internally.
const DEFAULT_CRAWL_GOAL = (
  "このサイトのトップから辿れるページを順にクロールして。\n" +
  "ページ遷移で popup や age-gate が出たら page.agent() で処理して。\n" +
  "各ページで page.capture() を呼んで HTML+画像+outline を保存して。\n" +
  "動画が見つかったページは page.agent() で動画を取得して。\n" +
  "\n" +
  "ガードレール:\n" +
  "  - **pap.walk() を必ず使うこと** (自前 BFS ループは禁止)\n" +
  "  - 同じ URL は 2 回開かない (pap.walk が内部で dedup する)\n" +
  "  - 最大 10000 ページで停止 (target_pages=10000)\n" +
  "  - page.agent() の max_steps は 3\n" +
  "  - 進捗は print() で stdout に出力 ('[N/10000] visited https://...')\n"
);

// Read the currently-selected AI engine ("codegen" or "simple").
// Defaults to "codegen" if no radio is checked.
function currentAiEngine() {
  const checked = document.querySelector('input[name="aiEngine"]:checked');
  return (checked && checked.value) || 'codegen';
}

// Toggle the AI / Code options panels + .selected class on mode cards
// based on the currently-picked radio. Also flips between the two
// sub-areas (codegen Goal textarea vs simple macro builder) when
// the engine radio changes inside the AI panel.
function syncSubmitMode() {
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
  const engine = currentAiEngine();
  const fetchOpts = document.getElementById('fetchOptions');
  if (fetchOpts) fetchOpts.style.display = (mode === 'fetch') ? 'block' : 'none';
  document.getElementById('aiOptions').style.display   = (mode === 'ai')   ? 'block' : 'none';
  document.getElementById('codeOptions').style.display = (mode === 'code') ? 'block' : 'none';
  // Phase 2a: when Fetch becomes visible, re-sync the sub-mode area
  // (handles initial paint + mode-flip back to Fetch).
  if (mode === 'fetch' && typeof syncFetchSubMode === 'function') {
    syncFetchSubMode();
  }
  // 3-card model: the "Script" virtual card is selected when EITHER
  // mode=code (Script>Code sub-tab) OR mode=ai+engine=simple
  // (Script>Macro sub-tab). The Script sub-tab strip is shown only
  // while Script is active so operators see Code/Macro as siblings
  // of one mode rather than top-level cards.
  const isScriptActive = (mode === 'code') || (mode === 'ai' && engine === 'simple');
  const subTabs = document.getElementById('scriptSubTabs');
  if (subTabs) subTabs.style.display = isScriptActive ? '' : 'none';
  if (isScriptActive) {
    const activeKind = (mode === 'code') ? 'code' : 'macro';
    document.querySelectorAll('#scriptSubTabs .script-tab').forEach(t => {
      t.classList.toggle('selected', t.dataset.scriptKind === activeKind);
    });
  }
  document.querySelectorAll('.mode-card').forEach(card => {
    const cMode = card.dataset.mode;
    let sel = false;
    if (cMode === 'fetch') {
      sel = (mode === 'fetch');
    } else if (cMode === 'script') {
      sel = isScriptActive;
    } else if (cMode === 'ai') {
      // The AI card now exclusively means codegen-loop (the LLM
      // crawler). mode=ai+engine=simple is Script>Macro, not AI.
      sel = (mode === 'ai' && engine === 'codegen');
    }
    card.classList.toggle('selected', sel);
  });
  // Mirror selected card's title to the "選択中: …" header so the
  // operator has an unmissable confirmation of the current state.
  const curLabel = document.getElementById('modeCardsCurrentLabel');
  if (curLabel) {
    const selCard = document.querySelector('.mode-card.selected .mode-title');
    if (selCard) curLabel.textContent = selCard.textContent;
  }

  if (mode === 'ai') {
    const goalArea  = document.getElementById('aiGoalArea');
    const macroArea = document.getElementById('aiMacroArea');
    if (engine === 'simple') {
      goalArea.style.display = 'none';
      macroArea.style.display = 'block';
      // Render rows if not yet rendered.
      if (typeof renderSimpleRows === 'function') renderSimpleRows();
    } else {
      goalArea.style.display = 'block';
      macroArea.style.display = 'none';
    }
  }

  // URL becomes a hint-only field for Code mode (the script chooses its
  // own initial_url); ditto for AI since it gets the URL injected into
  // the goal. Don't actually disable -- still useful as metadata --
  // just relax the required attribute so the user can submit without it.
  const urlInput = document.getElementById('urlInput');
  if (mode === 'fetch') {
    urlInput.required = true;
    urlInput.placeholder = 'https://example.com';
  } else {
    urlInput.required = false;
    urlInput.placeholder = (mode === 'code')
      ? '(任意, 表示用にしか使われない)'
      : 'https://example.com';
  }
}

// 3-card model (Fetch / Script / AI). The Script card is a virtual
// mode -- it sets the real {mode, aiEngine} dispatch based on which
// sub-tab is active (Code -> mode=code, Macro -> mode=ai+simple).
// Hidden radios stay in sync so presetBuildPayload / submit code is
// unchanged.
function currentScriptKind() {
  const sel = document.querySelector('#scriptSubTabs .script-tab.selected');
  return (sel && sel.dataset.scriptKind) || 'code';
}

function _selectModeCard(card) {
  if (!card) return;
  const m = card.dataset.mode || 'fetch';
  if (m === 'script') {
    // Script card: route to the active sub-tab (Code or Macro).
    let kind = currentScriptKind();
    try {
      kind = localStorage.getItem('paprika.submit.scriptKind') || kind;
    } catch (_) {}
    if (kind === 'macro') {
      const r = document.querySelector('input[name="mode"][value="ai"]');
      if (r) r.checked = true;
      const e = document.querySelector('input[name="aiEngine"][value="simple"]');
      if (e) e.checked = true;
      try { localStorage.setItem('paprika.submit.aiEngine', 'simple'); } catch (_) {}
    } else {
      const r = document.querySelector('input[name="mode"][value="code"]');
      if (r) r.checked = true;
    }
  } else {
    const modeRadio = document.querySelector(`input[name="mode"][value="${m}"]`);
    if (modeRadio) modeRadio.checked = true;
    if (m === 'ai') {
      // The AI card is unambiguous now: it always means codegen-loop
      // (LLM writes a script). Macro lives under Script.
      const ce = card.dataset.aiEngine || 'codegen';
      const er = document.querySelector(`input[name="aiEngine"][value="${ce}"]`);
      if (er) {
        er.checked = true;
        try { localStorage.setItem('paprika.submit.aiEngine', ce); } catch (_) {}
      }
    }
  }
  syncSubmitMode();
}

// Script sub-tab click: switch between Code and Macro under the
// Script card, persist the choice, and re-dispatch via the Script
// card so all the panel toggling re-fires.
function _selectScriptKind(kind) {
  document.querySelectorAll('#scriptSubTabs .script-tab').forEach(t => {
    t.classList.toggle('selected', t.dataset.scriptKind === kind);
  });
  try { localStorage.setItem('paprika.submit.scriptKind', kind); } catch (_) {}
  const scriptCard = document.querySelector('.mode-card[data-mode="script"]');
  if (scriptCard) _selectModeCard(scriptCard);
}
document.querySelectorAll('#scriptSubTabs .script-tab').forEach(btn => {
  btn.addEventListener('click', () => _selectScriptKind(btn.dataset.scriptKind));
});
document.querySelectorAll('.mode-card').forEach(card => {
  card.addEventListener('click', (e) => {
    // Don't double-fire when the click lands on a hidden radio inside
    // the card (the historical Fetch/Code cards still wrap one).
    if (e.target && e.target.tagName === 'INPUT') return;
    _selectModeCard(card);
  });
});
for (const r of document.querySelectorAll('input[name="mode"]')) {
  r.addEventListener('change', syncSubmitMode);
}
// Phase 2a: fetchSubMode radio change wakes up the inline-goal toggle.
for (const r of document.querySelectorAll('input[name="fetchSubMode"]')) {
  r.addEventListener('change', syncFetchSubMode);
}
// 動画をダウンロード <-> アセット保存 の相互制約 guard
{
  const _dv = document.getElementById('fetchDownloadVideo');
  if (_dv) _dv.addEventListener('change', syncFetchDlGuard);
}
// 初期描画時にも guard を効かせる (preset 復元など)
if (typeof syncFetchDlGuard === 'function') {
  try { syncFetchDlGuard(); } catch (_) {}
}
// Phase 2c: "Save as HostRecipe" modal logic. Opened from a button
// on the job detail panel for any completed codegen-loop / rerun job
// that captured actions.
window.openRecipeSaveModal = async function(jid) {
  if (!jid) return;
  const dlg = document.getElementById('recipeSaveModal');
  if (!dlg) {
    alert('recipe save modal element is missing');
    return;
  }
  const err = document.getElementById('recipeSaveError');
  if (err) { err.style.display = 'none'; err.textContent = ''; }
  // Fetch suggestion + prefill.
  let s;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(jid) + '/recipe_suggestion');
    if (!r.ok) {
      alert('recipe_suggestion fetch failed: HTTP ' + r.status);
      return;
    }
    s = await r.json();
  } catch (e) {
    alert('recipe_suggestion fetch crashed: ' + e);
    return;
  }
  if (!s.actions || s.actions.length === 0) {
    if (!confirm('この job は action trace を含みません。actions なしの recipe を保存しますか? (description / code のみ)')) {
      return;
    }
  }
  document.getElementById('recipeSaveHost').value = s.host || '';
  document.getElementById('recipeSavePattern').value = s.pattern || '*';
  document.getElementById('recipeSaveDescription').value = s.description || '';
  document.getElementById('recipeSaveActionCount').textContent = String((s.actions || []).length);
  document.getElementById('recipeSaveActionsPreview').textContent =
    JSON.stringify(s.actions || [], null, 2);
  document.getElementById('recipeSaveCodePreview').textContent = s.code || '(no script)';
  document.getElementById('recipeSaveGoalPreview').textContent = s.goal || '(no goal)';
  // Stash the full payload on the dialog so submit can read actions /
  // code / goal back without re-fetching.
  dlg.__suggestion = s;
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
};

(function _wireRecipeSaveModal() {
  const dlg = document.getElementById('recipeSaveModal');
  if (!dlg) return; // not on this page
  const form = document.getElementById('recipeSaveForm');
  const cancel = document.getElementById('recipeSaveCancel');
  if (cancel) {
    cancel.addEventListener('click', () => {
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
    });
  }
  if (!form) return;
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const host = document.getElementById('recipeSaveHost').value.trim();
    const pattern = document.getElementById('recipeSavePattern').value.trim() || '*';
    const description = document.getElementById('recipeSaveDescription').value.trim();
    const err = document.getElementById('recipeSaveError');
    err.style.display = 'none';
    if (!host) {
      err.textContent = 'host は必須です';
      err.style.display = 'block';
      return;
    }
    const s = dlg.__suggestion || {};
    const body = {
      pattern,
      description,
      actions: s.actions || [],
      code: s.code || null,
      goal: s.goal || null,
      created_from_job: s.created_from_job || null,
      created_by: s.created_by || 'ai',
    };
    try {
      const r = await fetch(
        '/hosts/' + encodeURIComponent(host) + '/recipes',
        {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        },
      );
      if (!r.ok) {
        const txt = await r.text();
        err.textContent = 'HTTP ' + r.status + ': ' + txt.slice(0, 400);
        err.style.display = 'block';
        return;
      }
      // Success: close modal + flash a toast.
      if (typeof dlg.close === 'function') dlg.close();
      else dlg.removeAttribute('open');
      if (typeof toast === 'function') {
        toast('recipe を ' + host + ' に追加しました', 'ok');
      } else {
        alert('recipe を ' + host + ' に追加しました');
      }
    } catch (e) {
      err.textContent = '送信失敗: ' + e;
      err.style.display = 'block';
    }
  });
})();

// Engine radio: persist + re-sync labels. Use localStorage so the
// operator's last choice survives a page reload.
for (const r of document.querySelectorAll('input[name="aiEngine"]')) {
  r.addEventListener('change', () => {
    try {
      localStorage.setItem('paprika.submit.aiEngine', r.value);
    } catch (_) {}
    syncSubmitMode();
  });
}
// Track when the user types into the count/timeout inputs so we
// don't clobber their value on engine switch. (Otherwise switching
// LLM -> Vision -> LLM would reset to defaults every time.)
for (const id of ['maxAttempts', 'attemptTimeout']) {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', () => { el.dataset.userTouched = '1'; });
}
// Restore last AI engine choice. Legacy "vision" value gets migrated
// to "simple" (renamed in the macro-builder rework).
try {
  let saved = localStorage.getItem('paprika.submit.aiEngine');
  if (saved === 'vision') saved = 'simple';
  if (saved === 'codegen' || saved === 'simple') {
    const r = document.querySelector('input[name="aiEngine"][value="' + saved + '"]');
    if (r) r.checked = true;
  }
} catch (_) {}

// =========================================================================
// Simple-engine macro builder
// =========================================================================
//
// Stack of rows that compile to a paprika-client Python script. Each
// row is one browser action; submit converts the list to source code
// and runs it as mode=rerun. The macro list is persisted in
// localStorage so a page reload doesn't lose the work-in-progress.
//
// Emit a Python string literal for ``s``. When ``s`` contains a
// {curly-brace} interpolation (e.g. ``{i}`` or ``{i+1}``), emit an
// f-string so the loop iteration variable resolves at runtime;
// otherwise emit a plain string. Backslashes and double-quotes are
// escaped either way; curly braces inside f-strings are kept as-is
// because that's where the substitution happens.
function _simpleEmitLit(s) {
  const str = String(s == null ? '' : s);
  const hasInterp = /\{[^{}]+\}/.test(str);
  const escaped = str.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  return hasInterp ? `f"${escaped}"` : `"${escaped}"`;
}

// SIMPLE_ACTIONS is the catalog: each entry defines the dropdown
// option (icon + label), the placeholder hint for the param input,
// and a compile() function returning ONE Python statement WITHOUT a
// leading indent. compileSimpleMacroToCode() prepends the
// depth-appropriate indent (12 spaces base + 4 per nesting level).
//
// Detail strings flow through _simpleEmitLit() so writing ``{i+1}``
// inside e.g. a Click (visual) description gets compiled to an
// f-string and resolves to the current Loop iteration index.
const SIMPLE_ACTIONS = [
  {
    value: 'navigate', category: '移動', icon: 'lucide:navigation', label: 'Navigate',
    placeholder: 'https://example.com/',
    compile: (d) => `await page.goto(${_simpleEmitLit(d.trim() || 'about:blank')})`,
  },
  {
    value: 'back', category: '移動', icon: 'lucide:arrow-left', label: 'Back',
    placeholder: '(なし)',
    compile: () => `await page.back()`,
  },
  {
    value: 'forward', category: '移動', icon: 'lucide:arrow-right', label: 'Forward',
    placeholder: '(なし)',
    compile: () => `await page.forward()`,
  },
  {
    value: 'history_first', category: '移動', icon: 'lucide:rewind', label: '履歴の最初へ',
    placeholder: '(なし)',
    compile: () => `await page.history_first()`,
  },
  {
    value: 'click', category: '操作', icon: 'lucide:mouse-pointer-click', label: 'Click (CSS)',
    placeholder: 'CSS selector (例: .btn-primary)',
    compile: (d) => `await page.click(${_simpleEmitLit(d.trim())})`,
  },
  {
    value: 'type', category: '操作', icon: 'lucide:type', label: 'Type',
    placeholder: 'focus 中の要素に挿入する文字列',
    compile: (d) => `await page.type(${_simpleEmitLit(d)})`,
  },
  {
    value: 'fill', category: '操作', icon: 'lucide:edit-3', label: 'Fill',
    placeholder: 'selector ⇒ value  (例: #search ⇒ pizza)',
    compile: (d) => {
      const parts = String(d).split(/⇒|=>|\|/);
      const sel = (parts[0] || '').trim();
      const val = parts.slice(1).join('|').trim();
      return `await page.fill(${_simpleEmitLit(sel)}, ${_simpleEmitLit(val)})`;
    },
  },
  {
    value: 'press', category: '操作', icon: 'lucide:keyboard', label: 'Press key',
    placeholder: 'Enter / Backspace x3 / Ctrl+A',
    compile: (d) => {
      const s = String(d).trim();
      const m = s.match(/^(.+?)\s*[xX]\s*(\d+)\s*$/);
      if (m) {
        return `await page.press(${_simpleEmitLit(m[1].trim())}, count=${parseInt(m[2],10)})`;
      }
      return `await page.press(${_simpleEmitLit(s)})`;
    },
  },
  {
    value: 'scroll', category: '操作', icon: 'lucide:scroll', label: 'Scroll',
    placeholder: 'down 800 / up 400 / left 200 / right 200',
    compile: (d) => {
      const parts = String(d).trim().split(/\s+/);
      const dir = (parts[0] || 'down').toLowerCase();
      const px  = parseInt(parts[1], 10) || 800;
      return `await page.scroll(${JSON.stringify(dir)}, ${px})`;
    },
  },
  {
    value: 'wait', category: '待ち', icon: 'lucide:clock', label: 'Wait',
    placeholder: 'seconds (例: 3)',
    compile: (d) => {
      const sec = parseFloat(d) || 1;
      return `await page.wait_for(seconds=${sec})`;
    },
  },
  {
    value: 'vision', category: 'AI', icon: 'lucide:eye', label: 'Agent (Visual)',
    placeholder: '日本語/英語の説明 (例: the {i+1}th thumbnail / 再生ボタン)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, engine="cogagent", max_steps=2)`,
  },
  {
    value: 'agent_dom', category: 'AI', icon: 'lucide:list-tree', label: 'Agent (DOM)',
    placeholder: '日本語/英語の説明 (DOM/アクセシビリティツリーから判断)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, engine="qwen", max_steps=2)`,
  },
  {
    value: 'agent', category: 'AI', icon: 'lucide:sparkles', label: 'Agent (multi-step)',
    placeholder: '多段操作の説明 (例: log in with my credentials)',
    compile: (d) => `await page.agent(${_simpleEmitLit(d.trim())}, max_steps=5)`,
  },
  {
    value: 'capture', category: '取り込み', icon: 'lucide:camera', label: 'Capture',
    placeholder: 'label (任意; 例: step-{i+1})',
    compile: (d) => `await page.capture(${_simpleEmitLit((d || 'capture').trim())})`,
  },
  {
    value: 'dlvideo', category: '取り込み', icon: 'lucide:download', label: 'Download video',
    placeholder: 'URL (任意; 省略時は現在ページ)',
    compile: (d) => {
      const u = String(d).trim();
      return u
        ? `await page.download_video(url=${_simpleEmitLit(u)})`
        : `await page.download_video()`;
    },
  },
  // -- tab management --------------------------------------------------
  // ``page`` in the generated script is a Session (= Page + tab
  // container). page.open(url) appends a tab; page[i] / page[-1] index
  // by position; page[i].close() smart-closes (last tab in the session
  // -> DELETE session, otherwise DELETE that tab only). No new API
  // needed -- these macros just stamp out the right one-liners.
  {
    value: 'open_tab', category: 'タブ', icon: 'lucide:square-plus', label: 'Open new tab',
    placeholder: 'https://example.com/  (空欄なら about:blank)',
    compile: (d) => {
      const u = String(d || '').trim();
      return u
        ? `await page.open(${_simpleEmitLit(u)})`
        : `await page.open()`;
    },
  },
  {
    value: 'switch_tab', category: 'タブ', icon: 'lucide:arrow-left-right', label: 'Switch to tab #N',
    placeholder: 'タブ番号 (0 始まり; -1 で最後のタブ)',
    compile: (d) => {
      const raw = String(d || '').trim();
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n)) {
        return `raise ValueError("switch_tab: invalid tab index " + ${_simpleEmitLit(raw)})`;
      }
      return `await page.switch(${n})`;
    },
  },
  {
    value: 'close_tab', category: 'タブ', icon: 'lucide:x-circle', label: 'Close tab #N',
    placeholder: 'タブ番号 (0 始まり; 例: 1)',
    compile: (d) => {
      const raw = String(d || '').trim();
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n) || n < 0) {
        return `raise ValueError("close_tab: invalid tab index " + ${_simpleEmitLit(raw)})`;
      }
      return `await page[${n}].close()`;
    },
  },
  {
    value: 'close_last_tab', category: 'タブ', icon: 'lucide:x', label: 'Close last tab',
    placeholder: '(なし)',
    compile: () => `await page[-1].close()`,
  },
  // -- control flow ----------------------------------------------------
  // Loop begin: opens a `for {var} in range(N):` block. Subsequent
  // rows are auto-indented (depth+1) until the matching `End loop`.
  // The iteration variable name (`i`, `j`, `k`, ...) is picked by
  // depth, so inner rows can reference it via {i} / {i+1} in their
  // detail string.
  {
    value: 'loop', category: '制御', icon: 'lucide:repeat', label: 'Loop (begin)',
    placeholder: '反復回数 (例: 5)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'loop_end', category: '制御', icon: 'lucide:corner-down-left', label: 'End loop',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  // -- conditional (if / else / end if) --------------------------------
  // 3 種類の条件タイプ:
  //   If (CSS)    -- CSS セレクタの存在チェック (確定的、LLM 不要)
  //   If (Agent)  -- 自然言語の yes/no 質問を LLM (Qwen) に投げる
  //   If (Visual) -- (将来) スクリーンショット + CogAgent で yes/no
  // detail 欄: CSS 版はセレクタ、Agent 版は質問文。
  // 後続の行は `if (...):` ブロック内にインデントされ、End if で閉じる。
  // 任意で間に `Else` を挟むと else 分岐になる。
  {
    value: 'if_css', category: '制御', icon: 'lucide:braces', label: 'If (CSS)',
    placeholder: 'CSS selector (例: .login-btn)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_agent', category: '制御', icon: 'lucide:help-circle', label: 'If (Agent)',
    placeholder: 'yes/no 質問 (例: ログイン画面が表示されているか?)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_else', category: '制御', icon: 'lucide:git-branch', label: 'Else',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
  {
    value: 'if_end', category: '制御', icon: 'lucide:corner-down-left', label: 'End if',
    placeholder: '(なし)',
    compile: () => '',   // handled specially by compileSimpleMacroToCode
  },
];

// Iteration variable name for a given loop nesting depth.
// 7+ deep is genuinely unusual; fall back to i7 / i8 / ... and let
// the user rename if they really need it.
const _SIMPLE_ITER_VARS = ['i', 'j', 'k', 'l', 'm', 'n'];
function _simpleIterVar(depth) {
  return _SIMPLE_ITER_VARS[depth] || ('i' + depth);
}

// Walk the row list and compute (depth_at_each_row, final_depth,
// warnings). depth starts at 0 and is incremented by a `loop` row
// (after the row itself is processed) and decremented by a
// `loop_end` (BEFORE the row is processed, so the end-marker
// renders at the outer depth). When `loop_end` appears without a
// matching `loop`, we clamp to 0 and emit a warning.
function _simpleComputeDepths() {
  // Tracks both `Loop ... End loop` and `If ... [Else] ... End if`
  // nesting. The two share a single ``depth`` counter:
  //   * Openers (loop / if_css / if_agent): emit at current depth, then ++
  //   * Closers (loop_end / if_end): -- then emit at the new (smaller) depth
  //   * Else (if_else): emits at depth-1 (the level of the matching `if:`),
  //     but does NOT change ``depth`` -- the rows that follow are still
  //     inside the else-branch body
  let depth = 0;
  const depths = [];
  const warns = [];
  _simpleRows.forEach((row, idx) => {
    if (row.action === 'loop_end' || row.action === 'if_end') {
      const label = row.action === 'loop_end' ? 'End loop' : 'End if';
      if (depth === 0) {
        warns.push(`row ${idx + 1}: extra "${label}" with no matching opener`);
        depths.push(0);
      } else {
        depth -= 1;
        depths.push(depth);
      }
    } else if (row.action === 'if_else') {
      // `else:` line sits one indent level outside the body (i.e. at
      // the matching `if:` depth).
      if (depth === 0) {
        warns.push(`row ${idx + 1}: "Else" outside any If block`);
        depths.push(0);
      } else {
        depths.push(depth - 1);
      }
    } else {
      depths.push(depth);
      if (row.action === 'loop' || row.action === 'if_css' || row.action === 'if_agent') {
        depth += 1;
      }
    }
  });
  if (depth > 0) {
    warns.push(`${depth} unmatched block opener(s)`);
  }
  return { depths, finalDepth: depth, warns };
}

const SIMPLE_ROWS_KEY = 'paprika.submit.simpleRows';

// In-memory macro state: array of {action: "vision"|..., detail: "..."}
let _simpleRows = (function loadSimpleRows() {
  try {
    const raw = localStorage.getItem(SIMPLE_ROWS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(r => ({
      action: typeof r.action === 'string' ? r.action : 'navigate',
      detail: typeof r.detail === 'string' ? r.detail : '',
    }));
  } catch (_) { return []; }
})();

function saveSimpleRows() {
  try { localStorage.setItem(SIMPLE_ROWS_KEY, JSON.stringify(_simpleRows)); }
  catch (_) {}
}

function _simpleActionByValue(v) {
  return SIMPLE_ACTIONS.find(a => a.value === v) || SIMPLE_ACTIONS[0];
}

// Build the action picker HTML for one row. Native <select> can't
// render icons inside <option>, so we use a button + popover combo:
//   .ap-button         the always-visible chip ([icon] label ▾)
//   .ap-popover        the dropdown panel; one .ap-group-header per
//                      category, then .ap-item buttons under it
// The current selection is highlighted via data-selected. Click on
// an .ap-item sets that row's action and triggers re-render.
function _simpleActionPicker(selectedValue) {
  const spec = _simpleActionByValue(selectedValue);
  const groups = new Map();
  for (const a of SIMPLE_ACTIONS) {
    const cat = a.category || 'その他';
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(a);
  }
  let body = '';
  for (const [cat, items] of groups.entries()) {
    body += `<div class="ap-group-header">${cat}</div>`;
    body += items.map(a =>
      `<button type="button" class="ap-item" data-value="${a.value}"`
      + `${a.value === selectedValue ? ' data-selected' : ''}>`
      + `<iconify-icon icon="${a.icon}"></iconify-icon>`
      + `<span>${a.label}</span>`
      + `</button>`
    ).join('');
  }
  return `
    <div class="simple-action-picker" data-value="${selectedValue}">
      <button type="button" class="ap-button">
        <iconify-icon icon="${spec.icon}" style="font-size:1.15em; min-width:1.4em; color:#555;"></iconify-icon>
        <span class="ap-current-label">${spec.label}</span>
        <span class="ap-caret">▾</span>
      </button>
      <div class="ap-popover" hidden>${body}</div>
    </div>
  `;
}

// One-shot global handlers (installed via the IIFE guard so a re-eval
// of this script doesn't stack listeners). Close any open popover on
// outside-click and on Escape.
(function _installSimpleActionPickerHandlers() {
  if (window.__simpleActionPickerHandlersInstalled) return;
  window.__simpleActionPickerHandlersInstalled = true;
  document.addEventListener('click', (ev) => {
    // If the click is inside an .ap-popover or its toggling button,
    // let the per-row handler deal with it. Otherwise close all.
    const inside = ev.target.closest('.simple-action-picker');
    document.querySelectorAll('.ap-popover').forEach(p => {
      if (!inside || !inside.contains(p)) p.hidden = true;
    });
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      document.querySelectorAll('.ap-popover').forEach(p => p.hidden = true);
    }
  });
})();

function renderSimpleRows() {
  const host = document.getElementById('simpleRows');
  if (!host) return;
  // Auto-add a starter row when the user first lands on simple mode
  // and has no saved macro yet -- friendlier than an empty box.
  if (_simpleRows.length === 0) {
    _simpleRows.push({ action: 'navigate', detail: '' });
  }
  // Compute per-row depth from loop/loop_end markers. Used to indent
  // rows visually (left-padding) and to warn about mismatched pairs.
  const { depths, warns } = _simpleComputeDepths();
  // Rebuild from scratch. Macros are small (typically < 20 rows) so
  // full re-render is cheaper than diff bookkeeping.
  host.innerHTML = '';
  if (warns.length) {
    const warnBar = document.createElement('div');
    warnBar.style.cssText = 'padding:6px 10px; background:#fff5e0; border:1px solid #e0b870; border-radius:4px; font-size:.85em; color:#8a5a00;';
    warnBar.textContent = '⚠ ' + warns.join('; ');
    host.appendChild(warnBar);
  }
  _simpleRows.forEach((row, idx) => {
    const d = depths[idx];
    const spec = _simpleActionByValue(row.action);
    const isLoopEnd = row.action === 'loop_end';
    const isLoopBegin = row.action === 'loop';
    const wrap = document.createElement('div');
    wrap.className = 'simple-row';
    // Visual indent per nesting depth so loop bodies stand out.
    // Loop-begin rows live at the OUTER depth (their body is the
    // indented part), so depth[idx] for the begin row is the outer
    // and the body starts at depth+1.
    const padLeft = d * 22;
    const isLoopMarker = isLoopBegin || isLoopEnd;
    wrap.style.cssText = `display:flex; gap:6px; align-items:center; padding-left:${padLeft}px; ` +
      (isLoopMarker ? 'background:#f0f4ff; border-left:3px solid #6a8ec7; padding-top:3px; padding-bottom:3px; border-radius:3px;' : '');
    wrap.innerHTML = `
      <span style="color:#888; font-family:ui-monospace,Consolas,monospace; font-size:.85em; min-width:1.6em; text-align:right;">${idx + 1}.</span>
      <iconify-icon class="simple-row-icon" icon="${spec.icon}" style="font-size:1.2em; min-width:1.4em; color:${isLoopMarker ? '#3a5ca8' : '#555'};"></iconify-icon>
      ${_simpleActionPicker(row.action)}
      <input type="text" class="simple-row-detail" value="${(row.detail || '').replace(/"/g, '&quot;')}" placeholder="${spec.placeholder}"${isLoopEnd ? ' disabled' : ''} style="flex:1; padding:4px 8px; font-family:inherit;${isLoopEnd ? 'background:#eee; color:#888;' : ''}">
      <button type="button" class="simple-row-up" title="上に移動" style="background:none; border:1px solid #ccd; padding:2px 6px; cursor:pointer; border-radius:4px;">↑</button>
      <button type="button" class="simple-row-down" title="下に移動" style="background:none; border:1px solid #ccd; padding:2px 6px; cursor:pointer; border-radius:4px;">↓</button>
      <button type="button" class="simple-row-insert" title="この直後に空の navigate 行を挿入" style="background:#eef8ee; border:1px solid #7ab68a; color:#196b2c; padding:2px 6px; cursor:pointer; border-radius:4px;">+</button>
      <button type="button" class="simple-row-remove" title="この step を削除" style="background:#fee; border:1px solid #c88; color:#933; padding:2px 8px; cursor:pointer; border-radius:4px;">×</button>
    `;

    const picker     = wrap.querySelector('.simple-action-picker');
    const pickerBtn  = picker.querySelector('.ap-button');
    const popover    = picker.querySelector('.ap-popover');
    const det    = wrap.querySelector('.simple-row-detail');
    const icon   = wrap.querySelector('.simple-row-icon');
    const upBtn  = wrap.querySelector('.simple-row-up');
    const dnBtn  = wrap.querySelector('.simple-row-down');
    const insBtn = wrap.querySelector('.simple-row-insert');
    const rmBtn  = wrap.querySelector('.simple-row-remove');

    // Toggle the popover. The global outside-click handler closes
    // popovers when the click falls outside any .simple-action-picker.
    pickerBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const wasHidden = popover.hidden;
      // Close any other open popover so only one is up at a time.
      document.querySelectorAll('.ap-popover').forEach(p => {
        if (p !== popover) p.hidden = true;
      });
      popover.hidden = !wasHidden;
    });
    // Item click -> set this row's action + re-render. Stop the click
    // from bubbling to the global outside-click handler (which would
    // close before we read the value).
    popover.querySelectorAll('.ap-item').forEach(item => {
      item.addEventListener('click', (ev) => {
        ev.stopPropagation();
        _simpleRows[idx].action = item.dataset.value;
        saveSimpleRows();
        // Changing to/from loop or loop_end shifts depth for every
        // subsequent row, so re-render the whole list. Re-render also
        // tears down this popover, so no explicit close needed.
        renderSimpleRows();
      });
    });
    det.addEventListener('input', () => {
      _simpleRows[idx].detail = det.value;
      saveSimpleRows();
    });
    upBtn.addEventListener('click', () => {
      if (idx === 0) return;
      [_simpleRows[idx-1], _simpleRows[idx]] = [_simpleRows[idx], _simpleRows[idx-1]];
      saveSimpleRows();
      renderSimpleRows();
    });
    dnBtn.addEventListener('click', () => {
      if (idx >= _simpleRows.length - 1) return;
      [_simpleRows[idx+1], _simpleRows[idx]] = [_simpleRows[idx], _simpleRows[idx+1]];
      saveSimpleRows();
      renderSimpleRows();
    });
    insBtn.addEventListener('click', () => {
      // Insert a fresh navigate row immediately AFTER this one so the
      // operator can pick the desired action from the dropdown without
      // having to mash ↑ a dozen times. Replaces the "add at the end
      // then move up" workflow.
      _simpleRows.splice(idx + 1, 0, { action: 'navigate', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
      // Best-effort focus on the action dropdown of the new row so the
      // operator can keyboard-pick the action immediately.
      setTimeout(() => {
        const newWrap = host.children[idx + 2]; // +1 for warns? +1 for new row
        // Resolve more robustly: find the row whose idx span matches idx+2 (= 1-based row number).
        const rows = host.querySelectorAll('.simple-row');
        const target = rows[idx + 1];
        if (target) {
          const dropdown = target.querySelector('.simple-row-action');
          if (dropdown) dropdown.focus();
        }
      }, 0);
    });
    rmBtn.addEventListener('click', () => {
      _simpleRows.splice(idx, 1);
      saveSimpleRows();
      renderSimpleRows();
    });

    host.appendChild(wrap);
  });
}

// Compile the macro rows + the URL field into a complete
// paprika-client script. Walks rows once; each row contributes one
// indented Python line. Loop rows emit `for {var} in range(N):` and
// bump the indent for subsequent rows until the matching End loop.
function compileSimpleMacroToCode(initialUrl) {
  const url = (initialUrl || '').trim() || 'about:blank';
  const { depths, warns } = _simpleComputeDepths();
  const lines = [];
  if (warns.length) {
    warns.forEach(w => lines.push(`            # WARN: ${w}`));
  }
  // Track whether each open block (loop or if) has emitted any body
  // lines yet. Empty blocks need an explicit `pass` to be valid
  // Python. The stack holds one entry per open block:
  //   {kind: 'loop'|'if', then_count, else_count, in_else}
  // Loops only use ``then_count``; ifs use both halves.
  const blockStack = [];

  function _bumpBodyCounters() {
    // A real action row contributes one body line to every block
    // currently on the stack. ifs split between then/else depending
    // on whether the matching `Else` row has been seen.
    for (let i = 0; i < blockStack.length; i++) {
      const blk = blockStack[i];
      if (blk.kind === 'if' && blk.in_else) {
        blk.else_count += 1;
      } else {
        blk.then_count += 1;
      }
    }
  }

  for (let idx = 0; idx < _simpleRows.length; idx++) {
    const row = _simpleRows[idx];
    const d = depths[idx];
    const indent = '    '.repeat(3 + d);  // 12 + 4*depth

    if (row.action === 'loop') {
      const count = parseInt(row.detail, 10) || 1;
      const varName = _simpleIterVar(d);
      lines.push(`${indent}for ${varName} in range(${count}):`);
      blockStack.push({ kind: 'loop', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'loop_end') {
      const blk = blockStack.pop();
      if (blk && blk.then_count === 0) {
        lines.push('    '.repeat(3 + d + 1) + 'pass  # empty loop body');
      }
      continue;
    }
    if (row.action === 'if_css') {
      const sel = (row.detail || '').trim();
      lines.push(`${indent}if await page.exists(${_simpleEmitLit(sel)}):`);
      blockStack.push({ kind: 'if', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'if_agent') {
      const q = (row.detail || '').trim();
      lines.push(`${indent}if await page.ask(${_simpleEmitLit(q)}, engine="qwen"):`);
      blockStack.push({ kind: 'if', then_count: 0, else_count: 0, in_else: false });
      continue;
    }
    if (row.action === 'if_else') {
      const blk = blockStack[blockStack.length - 1];
      if (!blk || blk.kind !== 'if') {
        lines.push(`${indent}# WARN: "Else" outside any If block`);
        continue;
      }
      // If the then-branch was empty, give it a `pass` before the else.
      if (blk.then_count === 0) {
        lines.push('    '.repeat(3 + d + 1) + 'pass  # empty then');
      }
      blk.in_else = true;
      lines.push(`${indent}else:`);
      continue;
    }
    if (row.action === 'if_end') {
      const blk = blockStack.pop();
      if (blk && blk.kind === 'if') {
        const inner = '    '.repeat(3 + d + 1);
        if (blk.in_else && blk.else_count === 0) {
          lines.push(inner + 'pass  # empty else');
        } else if (!blk.in_else && blk.then_count === 0) {
          lines.push(inner + 'pass  # empty if body');
        }
      }
      continue;
    }
    // Real action row.
    const spec = _simpleActionByValue(row.action);
    let line;
    try {
      line = spec.compile(row.detail || '');
    } catch (e) {
      line = `# !! compile failed for ${row.action}: ${e}`;
    }
    lines.push(indent + line);
    _bumpBodyCounters();
  }
  if (lines.length === 0) {
    lines.push('            pass  # empty macro');
  }
  return [
    `import asyncio`,
    `import paprika_client as pap`,
    `from paprika_client import async_paprika`,
    ``,
    `# connect() の引数省略 → PAPRIKA_HUB env (runner 内で自動注入) を読む。`,
    `# ローカル実行時のみ os.environ['PAPRIKA_HUB']=http://localhost:8000 を別途セット。`,
    `async def main():`,
    `    async with async_paprika.connect() as cli:`,
    `        async with cli.session(initial_url=${JSON.stringify(url)}) as page:`,
    ...lines,
    ``,
    `asyncio.run(main())`,
    ``,
  ].join('\n');
}

// Wire up the macro builder's buttons. Done after the DOM nodes
// exist (the surrounding script runs after the form HTML).
(function wireSimpleBuilder() {
  const addBtn     = document.getElementById('simpleAddRowBtn');
  const clearBtn   = document.getElementById('simpleClearBtn');
  const previewBtn = document.getElementById('simplePreviewBtn');
  const previewPre = document.getElementById('simplePreviewPre');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'navigate', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  const addLoopBtn = document.getElementById('simpleAddLoopBtn');
  if (addLoopBtn) {
    addLoopBtn.addEventListener('click', () => {
      // Add a Loop + End loop pair so the user can't accidentally
      // leave an unmatched marker. The body starts empty -- they
      // can drag/add rows in between.
      _simpleRows.push({ action: 'loop', detail: '5' });
      _simpleRows.push({ action: 'loop_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  // If (CSS) と If (Agent) の挿入ボタン -- どちらも開閉ペアを 1 セット挿入。
  // 中身は空 (= pass 1 行を Python に吐く)。ユーザーが任意で間に Else 行を
  // dropdown から差し込む。Loop と同じ階層モデルなのでネスト自由。
  const addIfCssBtn = document.getElementById('simpleAddIfCssBtn');
  if (addIfCssBtn) {
    addIfCssBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'if_css', detail: '' });
      _simpleRows.push({ action: 'if_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  const addIfAgentBtn = document.getElementById('simpleAddIfAgentBtn');
  if (addIfAgentBtn) {
    addIfAgentBtn.addEventListener('click', () => {
      _simpleRows.push({ action: 'if_agent', detail: '' });
      _simpleRows.push({ action: 'if_end', detail: '' });
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (_simpleRows.length === 0) return;
      if (!confirm('macro を全削除しますか?')) return;
      _simpleRows = [];
      saveSimpleRows();
      renderSimpleRows();
    });
  }
  if (previewBtn && previewPre) {
    previewBtn.addEventListener('click', () => {
      const url = (document.getElementById('urlInput') || {}).value || '';
      previewPre.textContent = compileSimpleMacroToCode(url);
      previewPre.style.display = (previewPre.style.display === 'none') ? 'block' : 'none';
    });
  }
})();
syncSubmitMode();

// =========================================================================
// Named Submit-form presets
// =========================================================================
//
// Presets are server-side snapshots of the Submit form so the
// operator can re-run common configurations without retyping. They
// also expose POST /presets/{name}/run for cron / external
// triggers; the dropdown above the Submit form lets a human pick
// one and inspect/edit before re-submitting.

const PRESET_LIST_URL = '/presets';
const PRESET_ONE_URL = (n) => '/presets/' + encodeURIComponent(n);
let _presetCurrentName = null;

function presetBuildPayload(name, category, description, opts) {
  // ``opts`` (optional) lets the caller force a specific execution
  // mode regardless of which radio is currently checked on the
  // Submit form. The save-preset modal uses this to honour the
  // operator's "this preset should always run as <X>" choice
  // instead of silently inheriting the current form mode.
  // Recognised opts keys:
  //   forceMode      'fetch'|'codegen-loop'|'code'|'rerun_from'
  //   rerunFromJob   job_id (or job_id/attempts/N) -- only when
  //                  forceMode === 'rerun_from'.
  //   codeOverride   inline Python to use as ``options.code`` and
  //                  ``code_script`` instead of #codeInput. Used by
  //                  the Macro mode's "save as generated code" path
  //                  so the compiled macro script gets stored
  //                  without smearing #codeInput.
  opts = opts || {};
  const url = (document.getElementById('urlInput') || {}).value || '';
  const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
  // Map forceMode → ui_mode for the Submit form (so re-loading the
  // preset later puts the form back into the right tab):
  //   'codegen-loop' -> 'ai' + ai_engine='codegen'
  //   'code'         -> 'code'
  //   'rerun_from'   -> 'code'  (the snapshot lives in options.rerun_from,
  //                              not the Code textarea; we treat it as a
  //                              flavour of code-mode for ui_mode purposes)
  let mode = formMode;
  if (opts.forceMode === 'fetch') mode = 'fetch';
  else if (opts.forceMode === 'codegen-loop') mode = 'ai';
  else if (opts.forceMode === 'code') mode = 'code';
  else if (opts.forceMode === 'rerun_from') mode = 'code';
  const engine = currentAiEngine();
  const goal = (document.getElementById('goalInput') || {}).value || '';
  // codeOverride wins so the Macro mode's "save as generated code"
  // path can hand in the compiled Python without first poking
  // #codeInput.
  const code = (typeof opts.codeOverride === 'string' && opts.codeOverride.length)
    ? opts.codeOverride
    : ((document.getElementById('codeInput') || {}).value || '');
  const maxAttempts = parseInt((document.getElementById('maxAttempts') || {}).value, 10) || 3;
  const attemptTimeout = parseInt((document.getElementById('attemptTimeout') || {}).value, 10) || 86400;
  const attemptTimeoutSimple = parseInt((document.getElementById('attemptTimeoutSimple') || {}).value, 10) || 600;
  const hostDedup = !!((document.getElementById('llmHostDedup') || {}).checked);
  let options = {};
  if (mode === 'fetch') {
    // Snapshot the full fetch-options block so saved presets round-trip
    // the operator's tuning (scroll toggle, timing knobs,
    // referer / cookies_from / attach_to_job text fields, ...). Falls
    // back to the historical hardcoded defaults if the helper isn't
    // wired yet (e.g. preset rendered before the form initialised).
    options = (typeof buildFetchOptionsFromForm === 'function')
      ? buildFetchOptionsFromForm()
      : { mode: 'fetch', scroll: true };
  } else if (mode === 'ai') {
    if (engine === 'simple') {
      const compiled = compileSimpleMacroToCode(url);
      options = {
        mode: 'rerun',
        code: compiled,
        attempt_timeout_s: attemptTimeoutSimple,
      };
    } else {
      let g = goal.trim() || DEFAULT_CRAWL_GOAL;
      if (!hostDedup) {
        g += '\n\n追加ガードレール:\n  - **pap.walk(..., host_dedup=False)** を必ず指定する (既訪問URLも再クロール)';
      }
      options = {
        mode: 'codegen-loop',
        goal: g,
        max_codegen_attempts: maxAttempts,
        attempt_timeout_s: attemptTimeout,
      };
    }
  } else if (mode === 'code') {
    if (opts.forceMode === 'rerun_from') {
      // Special-case: rerun-from-job uses the same ui_mode 'code'
      // for re-loading purposes but the options snapshot points at
      // an existing job's script instead of carrying inline code.
      options = {
        mode: 'rerun',
        rerun_from: String(opts.rerunFromJob || '').trim(),
        attempt_timeout_s: attemptTimeout,
      };
    } else {
      options = {
        mode: 'rerun',
        code,
        attempt_timeout_s: attemptTimeout,
      };
    }
  }
  return {
    name,
    category: category || '',
    description: description || '',
    ui_mode: mode,
    ai_engine: engine,
    url,
    goal,
    simple_rows: (typeof _simpleRows !== 'undefined') ? _simpleRows.slice() : [],
    code_script: code,
    max_attempts: maxAttempts,
    attempt_timeout_s: attemptTimeout,
    attempt_timeout_simple_s: attemptTimeoutSimple,
    host_dedup: hostDedup,
    options,
  };
}

function presetApplyToForm(rec) {
  if (!rec) return;
  document.getElementById('urlInput').value = rec.url || '';
  const modeRadio = document.querySelector(`input[name="mode"][value="${rec.ui_mode || 'fetch'}"]`);
  if (modeRadio) modeRadio.checked = true;
  const engineRadio = document.querySelector(`input[name="aiEngine"][value="${rec.ai_engine || 'codegen'}"]`);
  if (engineRadio) engineRadio.checked = true;
  const g = document.getElementById('goalInput');  if (g) g.value = rec.goal || '';
  const c = document.getElementById('codeInput');  if (c) c.value = rec.code_script || '';
  const m = document.getElementById('maxAttempts'); if (m) m.value = rec.max_attempts || 3;
  const t = document.getElementById('attemptTimeout'); if (t) t.value = rec.attempt_timeout_s || 86400;
  const ts = document.getElementById('attemptTimeoutSimple'); if (ts) ts.value = rec.attempt_timeout_simple_s || 600;
  const dd = document.getElementById('llmHostDedup');
  if (dd) dd.checked = (rec.host_dedup === undefined ? true : !!rec.host_dedup);
  // Restore fetch-options fields from rec.options (the snapshot the
  // preset-builder captured at save time). Missing keys mean "use the
  // form default that's already there" -- don't clobber.
  const fopt = rec.options || {};
  const setChk = (id, v, dflt) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = (v === undefined) ? dflt : !!v;
  };
  const setNum = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  const setTxt = (id, v) => {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = String(v);
  };
  if ((rec.ui_mode || 'fetch') === 'fetch') {
    setChk('fetchScroll',         fopt.scroll,          true);
    setChk('fetchDownloadVideo',  fopt.download_video,  false);
    setChk('fetchHeadless',       fopt.headless,        false);
    setChk('fetchCaptureAssets',  fopt.capture_assets,  true);
    setChk('fetchKeepSession',    fopt.keep_session,    false);
    setNum('fetchWaitSec',          fopt.wait_seconds);
    setNum('fetchIdleSec',          fopt.idle_seconds);
    setNum('fetchMaxWaitSec',       fopt.max_wait_seconds);
    setNum('fetchScrollMax',        fopt.scroll_max);
    setNum('fetchPostClickSec',     fopt.post_click_seconds);
    setNum('fetchMinAssetBytes',    fopt.min_asset_size_bytes);
    setTxt('fetchReferer',          fopt.referer || '');
    setTxt('fetchAttachToJob',      fopt.attach_to_job || '');
  }
  if (typeof _simpleRows !== 'undefined' && Array.isArray(rec.simple_rows)) {
    _simpleRows = rec.simple_rows.map(r => ({
      action: typeof r.action === 'string' ? r.action : 'navigate',
      detail: typeof r.detail === 'string' ? r.detail : '',
    }));
    if (typeof saveSimpleRows === 'function') saveSimpleRows();
  }
  syncSubmitMode();
  if (typeof renderSimpleRows === 'function') renderSimpleRows();
}

function presetSetLoaded(name) {
  _presetCurrentName = name || null;
  const lbl = document.getElementById('presetLoadedName');
  const ow  = document.getElementById('presetOverwriteBtn');
  if (lbl) {
    if (name) {
      lbl.textContent = `(loaded: ${name})`;
      lbl.style.color = '#3a5ca8';
    } else {
      lbl.textContent = '(none loaded — pick one from the Preset job tab)';
      lbl.style.color = '#888';
    }
  }
  if (ow)  ow.style.display = name ? '' : 'none';
}

// ---------------------------------------------------------------------------
// Preset-save modal (replaces the older 3x window.prompt chain).
//
// The modal lets the operator override "this preset should execute as
// <X> regardless of the Submit form's current state". Pre-modal this
// was a hidden footgun: clicking "save as" on a form that had drifted
// back to fetch silently produced a fetch-mode preset even when the
// operator's intent was "save the AI / Code workflow I just ran".
// ---------------------------------------------------------------------------
const _PRESET_MODAL = {
  open: false,
  mode: 'save-as',     // 'save-as' | 'overwrite'
  onSubmit: null,      // resolves the openPresetSaveModal() promise
};

function _presetModalSetExtraVisibility() {
  const mode = (document.querySelector('input[name="presetSaveModalMode"]:checked') || {}).value || 'inherit';
  const rerunBlock = document.getElementById('presetSaveModalRerunFromBlock');
  const codeNote = document.getElementById('presetSaveModalCodeNote');
  const cgNote = document.getElementById('presetSaveModalCodegenNote');
  if (rerunBlock) rerunBlock.style.display = (mode === 'rerun_from') ? 'flex' : 'none';
  if (codeNote)   codeNote.style.display   = (mode === 'code') ? 'block' : 'none';
  if (cgNote)     cgNote.style.display     = (mode === 'codegen-loop') ? 'block' : 'none';
}

function _presetModalFetchCategoriesInto(datalist) {
  // Best-effort category autocomplete. We swallow failures so the
  // modal stays usable even when /presets returns the malformed-JSON
  // edge case observed in prod.
  if (!datalist) return;
  fetch('/presets?limit=500').then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !Array.isArray(d.categories)) return;
    datalist.innerHTML = d.categories
      .map(c => `<option value="${(c || '').replace(/"/g, '&quot;')}"></option>`)
      .join('');
  }).catch(() => {});
}

function openPresetSaveModal({
  mode = 'save-as',
  initialName = '',
  initialCategory = '',
  initialDescription = '',
  // When set, the modal opens with the rerun_from radio already
  // checked and the Job ID field pre-populated. Used by the Live
  // panel's "save preset" button so the operator doesn't have to
  // copy/paste a job ID across the UI to save a successful run.
  prefillRerunFromJob = '',
  // Optional title override (e.g. "Save this job as preset" instead
  // of the generic "Save preset"). Falls back to the mode-based
  // default when empty.
  titleOverride = '',
} = {}) {
  return new Promise(resolve => {
    const modal = document.getElementById('presetSaveModal');
    if (!modal) { resolve(null); return; }
    const titleEl = document.getElementById('presetSaveModalTitle');
    const nameEl = document.getElementById('presetSaveModalName');
    const catEl  = document.getElementById('presetSaveModalCategory');
    const descEl = document.getElementById('presetSaveModalDescription');
    const errEl  = document.getElementById('presetSaveModalErr');
    const hintEl = document.getElementById('presetSaveModalHint');
    const inheritRadio  = document.querySelector('input[name="presetSaveModalMode"][value="inherit"]');
    const rerunFromRadio = document.querySelector('input[name="presetSaveModalMode"][value="rerun_from"]');
    if (titleEl) {
      titleEl.textContent = titleOverride
        || ((mode === 'overwrite') ? 'Overwrite preset' : 'Save preset');
    }
    if (nameEl)  { nameEl.value = initialName || ''; nameEl.readOnly = (mode === 'overwrite'); }
    if (catEl)   catEl.value = initialCategory || '';
    if (descEl)  descEl.value = initialDescription || '';
    if (errEl)   errEl.textContent = '';
    // When prefillRerunFromJob is set, default the modal to the
    // rerun_from path so the operator's first action is "name it
    // and click save". Otherwise stick with the inherit default.
    if (prefillRerunFromJob && rerunFromRadio) {
      rerunFromRadio.checked = true;
    } else if (inheritRadio) {
      inheritRadio.checked = true;
    }
    document.getElementById('presetSaveModalRerunFromJob').value = prefillRerunFromJob || '';
    // Surface the current form's mode so the operator can tell at a
    // glance what "inherit" would save.
    const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
    const formEngine = (document.querySelector('input[name="aiEngine"]:checked') || {}).value || '';
    let inheritLabel = formMode;
    if (formMode === 'ai') inheritLabel = `ai (engine=${formEngine || 'codegen'})`;
    if (hintEl) {
      hintEl.textContent = prefillRerunFromJob
        ? `rerun_from = ${prefillRerunFromJob}`
        : `現在のフォーム: ${inheritLabel}`;
    }
    _presetModalSetExtraVisibility();
    _presetModalFetchCategoriesInto(document.getElementById('presetSaveModalCategoryList'));
    _PRESET_MODAL.open = true;
    _PRESET_MODAL.mode = mode;
    _PRESET_MODAL.onSubmit = resolve;
    modal.style.display = 'flex';
    setTimeout(() => { if (nameEl && !nameEl.readOnly) nameEl.focus(); }, 0);
  });
}

function closePresetSaveModal(result) {
  const modal = document.getElementById('presetSaveModal');
  if (modal) modal.style.display = 'none';
  if (_PRESET_MODAL.onSubmit) {
    const r = _PRESET_MODAL.onSubmit;
    _PRESET_MODAL.onSubmit = null;
    _PRESET_MODAL.open = false;
    try { r(result); } catch (_) {}
  }
}

(function wirePresetSaveModal() {
  const modal = document.getElementById('presetSaveModal');
  if (!modal) return;
  // Wire radio change -> show/hide conditional blocks.
  document.querySelectorAll('input[name="presetSaveModalMode"]').forEach(r => {
    r.addEventListener('change', _presetModalSetExtraVisibility);
  });
  const closeBtn  = document.getElementById('presetSaveModalClose');
  const cancelBtn = document.getElementById('presetSaveModalCancel');
  const saveBtn   = document.getElementById('presetSaveModalSave');
  if (closeBtn)  closeBtn.addEventListener('click', () => closePresetSaveModal(null));
  if (cancelBtn) cancelBtn.addEventListener('click', () => closePresetSaveModal(null));
  // Backdrop click closes too (but only when clicking the overlay itself).
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closePresetSaveModal(null);
  });
  // Esc closes.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _PRESET_MODAL.open) closePresetSaveModal(null);
  });
  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const name = (document.getElementById('presetSaveModalName').value || '').trim();
      const category = (document.getElementById('presetSaveModalCategory').value || '').trim();
      const description = (document.getElementById('presetSaveModalDescription').value || '').trim();
      const mode = (document.querySelector('input[name="presetSaveModalMode"]:checked') || {}).value || 'inherit';
      const errEl = document.getElementById('presetSaveModalErr');
      const setErr = (msg) => { if (errEl) errEl.textContent = msg || ''; };
      setErr('');
      if (!name) { setErr('Name は必須です'); return; }
      let forceMode = null;
      let rerunFromJob = '';
      if (mode === 'fetch')         forceMode = 'fetch';
      else if (mode === 'codegen-loop') forceMode = 'codegen-loop';
      else if (mode === 'code')     forceMode = 'code';
      else if (mode === 'rerun_from') {
        forceMode = 'rerun_from';
        rerunFromJob = (document.getElementById('presetSaveModalRerunFromJob').value || '').trim();
        if (!rerunFromJob) { setErr('rerun_from モードでは Job ID が必須です'); return; }
      }
      closePresetSaveModal({ name, category, description, forceMode, rerunFromJob });
    });
  }
})();

(function wirePresetBar() {
  // The dropdown selector was removed because operators can have
  // 500+ presets; picking is now done from the Preset job tab.
  // We keep "save as" / "overwrite" on the Submit form since
  // those operate on the LIVE form state, not on a saved record.
  // ---- Shared save flow ------------------------------------------------
  //
  // Each entry point (Fetch / Code direct save, LLM dropdown items,
  // Macro dropdown items) decides WHAT it's saving and calls this
  // helper, which opens the simplified save modal (name / category /
  // description) and PUTs the result. The modal's old in-modal
  // mode-picker still works for callers that don't pre-decide; the
  // new flows skip that picker by passing forceMode / codeOverride
  // up front.
  async function _runSaveFlow({ forceMode, codeOverride, rerunFromJob, titleOverride, defaultName }) {
    const res = await openPresetSaveModal({
      mode: 'save-as',
      initialName: defaultName || '',
      titleOverride: titleOverride || '',
      prefillRerunFromJob: (forceMode === 'rerun_from') ? (rerunFromJob || '') : '',
    });
    if (!res) return;
    const finalForceMode = forceMode || res.forceMode;
    const finalRerunFromJob = (finalForceMode === 'rerun_from') ? (rerunFromJob || res.rerunFromJob) : '';
    const payload = presetBuildPayload(
      res.name, res.category, res.description,
      { forceMode: finalForceMode, rerunFromJob: finalRerunFromJob, codeOverride },
    );
    try {
      const r = await fetch(PRESET_ONE_URL(res.name), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        alert(`Save failed (HTTP ${r.status}): ${await r.text()}`);
        return;
      }
      presetSetLoaded(res.name);
      if (typeof renderPresets === 'function') renderPresets();
    } catch (e) {
      alert(`Save failed: ${e}`);
    }
  }

  // ---- Per-mode dropdown menu (LLM / Macro only) -----------------------
  //
  // Fetch and Code each have exactly one thing worth saving, so we
  // skip the menu and open the modal directly. LLM and Macro each
  // have TWO meaningful save types (process recipe vs frozen
  // generated code), so for those we pop a small menu below the
  // save button.
  let _savePopdown = null;
  function _closeSavePopdown() {
    if (_savePopdown) { _savePopdown.remove(); _savePopdown = null; }
  }
  function _openSavePopdown(anchor, items) {
    _closeSavePopdown();
    const rect = anchor.getBoundingClientRect();
    const pop = document.createElement('div');
    pop.id = 'presetSaveDropdown';
    pop.style.cssText = `
      position: fixed; z-index: 1100;
      left: ${Math.round(rect.left)}px; top: ${Math.round(rect.bottom + 4)}px;
      background: #fff; border: 1px solid #ccd; border-radius: 6px;
      box-shadow: 0 6px 18px rgba(0,0,0,.15);
      padding: 4px; min-width: 280px;
      display: flex; flex-direction: column; gap: 2px;
    `;
    for (const it of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.disabled = !!it.disabled;
      btn.style.cssText = `
        text-align: left; padding: 8px 10px; border: none; background: transparent;
        border-radius: 4px; cursor: ${it.disabled ? 'not-allowed' : 'pointer'};
        font-size: .9em; color: ${it.disabled ? '#aaa' : '#222'};
      `;
      btn.innerHTML = `
        <div style="font-weight:600;">${it.icon || ''} ${it.label}</div>
        <div style="color:#888; font-size:.82em; font-weight:400; margin-top:2px;">${it.hint || ''}</div>
      `;
      btn.addEventListener('mouseenter', () => { if (!it.disabled) btn.style.background = '#f5f5fa'; });
      btn.addEventListener('mouseleave', () => { btn.style.background = 'transparent'; });
      btn.addEventListener('click', () => {
        _closeSavePopdown();
        if (!it.disabled && it.onClick) it.onClick();
      });
      pop.appendChild(btn);
    }
    document.body.appendChild(pop);
    _savePopdown = pop;
    // Dismiss on outside click / Esc.
    setTimeout(() => {
      const onDoc = (e) => {
        if (!pop.contains(e.target) && e.target !== anchor) {
          _closeSavePopdown();
          document.removeEventListener('mousedown', onDoc, true);
        }
      };
      document.addEventListener('mousedown', onDoc, true);
    }, 0);
  }
  function _currentLiveLlmJobId() {
    // Live panel's currently-attached job, IFF it ran in codegen-loop
    // mode (= a real LLM-generated script exists at /jobs/{id}/script.py).
    // Used by the "save generated code" dropdown item.
    if (typeof LJP === 'undefined') return null;
    if (!LJP.jobId) return null;
    if (LJP.mode !== 'codegen-loop' && LJP.mode !== 'rerun') return null;
    return LJP.jobId;
  }

  const saveBtn = document.getElementById('presetSaveAsBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      // Dispatch by current Submit-form mode.
      const formMode = (document.querySelector('input[name="mode"]:checked') || {}).value || 'fetch';
      const aiEngine = currentAiEngine();
      if (formMode === 'fetch') {
        // One-shot save: opens the modal already decided.
        return _runSaveFlow({ forceMode: 'fetch', titleOverride: 'Save Fetch preset' });
      }
      if (formMode === 'code') {
        return _runSaveFlow({ forceMode: 'code', titleOverride: 'Save Code preset' });
      }
      if (formMode === 'ai' && aiEngine === 'codegen') {
        // LLM dropdown: 2 options.
        const liveJid = _currentLiveLlmJobId();
        _openSavePopdown(saveBtn, [
          {
            icon: '🎯',
            label: 'Goal を保存',
            hint: '実行ごとに LLM が新しいスクリプトを生成する設定 (Goal + 試行設定 + コード生成 LLM)',
            onClick: () => _runSaveFlow({
              forceMode: 'codegen-loop',
              titleOverride: 'Save Goal preset',
            }),
          },
          {
            icon: '📜',
            label: '生成コードを保存',
            hint: liveJid
              ? `Live ジョブ ${liveJid} の最終 script を固定保存 (LLM 呼ばずに同じスクリプトを再実行)`
              : '※ Live パネルに codegen-loop のジョブを開いてから利用してください',
            disabled: !liveJid,
            onClick: () => _runSaveFlow({
              forceMode: 'rerun_from',
              rerunFromJob: liveJid,
              titleOverride: `Save generated script from ${liveJid}`,
            }),
          },
        ]);
        return;
      }
      if (formMode === 'ai' && aiEngine === 'simple') {
        // Macro dropdown: 2 options.
        const urlVal = (document.getElementById('urlInput') || {}).value || '';
        const compiled = (typeof compileSimpleMacroToCode === 'function')
          ? compileSimpleMacroToCode(urlVal)
          : '';
        const hasRows = (typeof _simpleRows !== 'undefined') && _simpleRows && _simpleRows.length > 0;
        _openSavePopdown(saveBtn, [
          {
            icon: '⠿',
            label: 'Macro を保存',
            hint: '行構成 + コンパイル済み Python を一緒に保存 (後で Macro UI で再編集可)',
            disabled: !hasRows,
            onClick: () => _runSaveFlow({
              // inherit current form (= mode=ai + engine=simple) so
              // simple_rows + compiled code both round-trip.
              titleOverride: 'Save Macro preset',
            }),
          },
          {
            icon: '📜',
            label: '生成コードを保存',
            hint: 'コンパイル済み Python のみ保存 (Macro UI 復元不可、Code として再編集可)',
            disabled: !compiled,
            onClick: () => _runSaveFlow({
              forceMode: 'code',
              codeOverride: compiled,
              titleOverride: 'Save Macro compiled code',
            }),
          },
        ]);
        return;
      }
      // Fallback (shouldn't reach here but keeps old behaviour).
      _runSaveFlow({});
    });
  }
  const owBtn = document.getElementById('presetOverwriteBtn');
  if (owBtn) {
    owBtn.addEventListener('click', async () => {
      if (!_presetCurrentName) return;
      // Fetch current snapshot so the modal can prefill cat / desc.
      let oldCat = '', oldDesc = '';
      try {
        const r0 = await fetch(PRESET_ONE_URL(_presetCurrentName));
        if (r0.ok) {
          const old = await r0.json();
          oldCat = old.category || '';
          oldDesc = old.description || '';
        }
      } catch (_) {}
      const res = await openPresetSaveModal({
        mode: 'overwrite',
        initialName: _presetCurrentName,
        initialCategory: oldCat,
        initialDescription: oldDesc,
      });
      if (!res) return;
      const { name, category, description, forceMode, rerunFromJob } = res;
      const payload = presetBuildPayload(name, category, description, { forceMode, rerunFromJob });
      try {
        const r = await fetch(PRESET_ONE_URL(_presetCurrentName), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const err = await r.text();
          alert(`Overwrite failed (HTTP ${r.status}): ${err}`);
          return;
        }
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) {
        alert(`Overwrite failed: ${e}`);
      }
    });
  }
})();

// Make Tab in the Code textarea insert 4 spaces instead of moving focus.
// Without this, pasting Python in and trying to fix indentation is painful.
(function () {
  const ta = document.getElementById('codeInput');
  if (!ta) return;
  ta.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab' || e.shiftKey) return;
    e.preventDefault();
    const s = ta.selectionStart, en = ta.selectionEnd;
    ta.value = ta.value.substring(0, s) + '    ' + ta.value.substring(en);
    ta.selectionStart = ta.selectionEnd = s + 4;
  });
  // "Insert template" button -- only overwrites when textarea is empty,
  // so accidental clicks don't nuke in-progress code.
  const tplBtn = document.getElementById('codeLoadTemplate');
  if (tplBtn) tplBtn.addEventListener('click', () => {
    if (ta.value.trim() && !confirm('Overwrite the current code with the template?')) return;
    ta.value =
`import asyncio
import paprika_client as pap
from paprika_client import async_paprika

# connect() の引数省略 → PAPRIKA_HUB env (runner で自動注入される
# http://hub:8000) を SDK が読む。ローカル実行時のみ
# os.environ['PAPRIKA_HUB']=http://localhost:8000 を別途セット。

async def main():
    async with async_paprika.connect() as cli:
        async with cli.session(initial_url='https://example.com/') as page:
            # Clear any startup modal FIRST (age gate / consent dialog).
            await page.agent(
                'If an age verification or consent dialog appears, '
                'accept it. Otherwise return done immediately.',
                max_steps=2,
            )

            # BFS-walk the site, persisting state so retries resume.
            async for visit in pap.walk(
                page,
                target_pages=20,
                same_domain=True,
            ):
                print(f'[{visit.n}/{visit.target}] {visit.url}')
                # Optionally download a video on video pages:
                # if '/video' in visit.url:
                #     r = await page.download_video(timeout_s=600)
                #     print(f'  downloaded {r["file_count"]} file(s)')

asyncio.run(main())
`;
    ta.focus();
  });
})();

// --- inline live panel (log + noVNC) tied to the most-recently-submitted job
// Replaces the old "open /jobs/{id}/log in a new tab" UX. The panel lives
// in #liveJobPanel right under the Submit form.
const LJP = {
  jobId: null,
  ws: null,
  wsBackoff: 1000,
  seenLines: 0,
  // The most recent "[paprika] page.X(...)" call row that is still
  // awaiting its "  -> OK/ERR (Nms)" result line. When the result
  // arrives we append it to this row instead of starting a new line,
  // collapsing the 2-line call/result pair into one. Null when the
  // last row wasn't a pending call.
  _pendingCallEl: null,
  finished: false,
  pollTimer: null,
  statusTimer: null,
  codeTimer: null,
  // Map session_id -> the iframe wrapper element we mounted, so we can
  // diff against /jobs/{id}/sessions and only add/remove what changed.
  vncIframes: new Map(),
  // -1 = not yet polled; only re-render the thumbnail strip when the
  // count actually changes (avoids flicker on each 2.5s poll).
  galleryLastCount: -1,
  // Content signature (joined asset names) for change-detection.
  // Pure count-based dedup misses the case where an upload races a
  // pre-existing eviction (e.g. a video lands while an old asset
  // gets cleaned up): count stays the same but the visible set
  // shifts. Hashing the names catches that too.
  gallerySignature: "",
  // After the job hits a terminal status, we do one final gallery sweep
  // and then stop polling -- assets stop arriving anyway.
  galleryStopped: false,
  // Sticky flag set once ljpRefreshStatus has observed a terminal
  // status AND the job is NOT a keep_session crawl. After this is true
  // the periodic status / sessions / code timers are torn down --
  // the underlying job state can't change anymore so further polls
  // are pure noise (and noticeable hub load when many tabs are open).
  // Reset to false on every fresh ljpAttach.
  _terminalStopped: false,
  // JobOptions.mode of the attached job; needed so ljpSetStatus knows
  // whether ▶ resume should be enabled (only codegen-loop / rerun
  // have a saved script to re-rerun). Stashed by ljpRefreshStatus.
  mode: null,
};

// Detect class from line content so the log pane can colour stdout
// (green-ish), stderr (red-ish), and meta lines (blue, italic) without
// the caller having to know. ljpAppendLine still accepts an explicit
// override.
function ljpClassifyLine(text) {
  if (typeof text !== 'string') return null;
  // Orchestrator stamps these prefixes when streaming subprocess output.
  if (text.indexOf('  [stderr]') !== -1) return 'stderr';
  if (text.indexOf('  [stdout]') !== -1) return 'stdout';
  return null;
}
function ljpAppendLine(text, cls) {
  const el = document.getElementById('ljpLog');

  // --- collapse the paprika action result onto its call line ---------
  // The client SDK emits two consecutive lines per action:
  //   [paprika] page.goto('...')
  //   [paprika]   -> OK (3012ms)        (or NO_MATCH / ERR: ... )
  // Append the result to the preceding call row so the log reads
  // "page.goto('...')  -> OK (3012ms)" on ONE line. The call row still
  // renders the instant it's emitted (so a long action like
  // download_video is visibly in-flight); the result is merged in when
  // it arrives. The multi-line "step N" agent traces are left as their
  // own rows (they're not a single call's result).
  const resultMatch = (typeof text === 'string')
    ? text.match(/\[paprika\]\s+(->\s.*)$/)
    : null;
  if (resultMatch && LJP._pendingCallEl && el.contains(LJP._pendingCallEl)) {
    LJP._pendingCallEl.textContent += '  ' + resultMatch[1];
    // A result delivered on stderr (NO_MATCH / ERR / exception) means
    // the action failed -- recolour the merged row so it reads red.
    if (ljpClassifyLine(text) === 'stderr') {
      LJP._pendingCallEl.className = 'stderr';
    }
    LJP._pendingCallEl = null;
    LJP.seenLines += 1;   // keep the server cursor in step
    const d = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (d < 40) el.scrollTop = el.scrollHeight;
    return;
  }

  const line = document.createElement('div');
  const c = cls || ljpClassifyLine(text);
  if (c) line.className = c;
  line.textContent = text;
  el.appendChild(line);
  // Auto-scroll to bottom unless the user has scrolled up.
  const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
  if (distFromBottom < 40) el.scrollTop = el.scrollHeight;
  LJP.seenLines += 1;

  // Remember this row IF it's a paprika action CALL awaiting a result
  // -- i.e. "[paprika] page.X(...)" but not a "-> result" row and not
  // an indented "step N" trace row. The next "-> ..." line merges in.
  const isPaprika = (typeof text === 'string') && text.indexOf('[paprika] ') !== -1;
  const isCall = isPaprika
    && !/\[paprika\]\s+->/.test(text)
    && !/\[paprika\]\s+step /.test(text);
  LJP._pendingCallEl = isCall ? line : null;
}
function ljpAppendMeta(text) {
  const el = document.getElementById('ljpLog');
  const line = document.createElement('div');
  line.className = 'meta';
  line.textContent = text;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function ljpSetStatus(s, phase) {
  const el = document.getElementById('ljpStatus');
  // "downloading" phase: status is still running (the fetch finished but
  // a detached yt-dlp download is uploading the video). Show it as a
  // distinct label while keeping the running palette/pulse.
  const isDownloading = (s === 'running' && phase === 'downloading');
  el.textContent = isDownloading ? 'downloading' : (s || '…');
  // The status pill colour is driven by a CSS class -- swap the
  // class based on the current state so the palette stays in sync
  // with the rest of the panel.
  el.classList.remove(
    'status-queued', 'status-running', 'status-completed',
    'status-failed', 'status-cancelled',
  );
  let cls = 'status-queued';
  if (s === 'succeeded' || s === 'completed') cls = 'status-completed';
  else if (s === 'failed')                    cls = 'status-failed';
  else if (s === 'cancelled')                 cls = 'status-cancelled';
  else if (s === 'running')                   cls = 'status-running';
  el.classList.add(cls);
  // Toggle the .running class on the header so the live-dot pulses
  // when (and only when) the job is in flight.
  const hdr = document.getElementById('ljpHeader');
  if (hdr) hdr.classList.toggle('running', s === 'running' || s === 'queued');
  // Pause / resume button enablement.
  //   pause  : while running/queued
  //   resume : after a terminal state, AND only for modes that have a
  //            saved script (codegen-loop / rerun). fetch jobs can't
  //            be resumed because there's no script to re-run.
  const cancellable = (s === 'running' || s === 'queued');
  const terminal    = (s === 'completed' || s === 'succeeded' || s === 'failed' || s === 'cancelled');
  const resumable   = terminal && (LJP.mode === 'codegen-loop' || LJP.mode === 'rerun');
  const stopBtn = document.getElementById('ljpStop');
  if (stopBtn) {
    stopBtn.disabled = !cancellable;
    stopBtn.style.opacity = cancellable ? '1' : '0.45';
    stopBtn.style.cursor = cancellable ? 'pointer' : 'not-allowed';
  }
  const resumeBtn = document.getElementById('ljpResume');
  if (resumeBtn) {
    resumeBtn.disabled = !resumable;
    resumeBtn.style.opacity = resumable ? '1' : '0.45';
    resumeBtn.style.cursor = resumable ? 'pointer' : 'not-allowed';
  }
  // Save-as-recipe: only show in AI-investigation mode (codegen-loop).
  // Other modes can still use Jobs-tab → "recipe として保存" if needed.
  const recipeBtn = document.getElementById('ljpSaveRecipe');
  if (recipeBtn) {
    recipeBtn.style.display = (LJP.mode === 'codegen-loop') ? '' : 'none';
  }
}

// Start a new rerun-mode job from the last attempt of this job. State
// (pap.walk() visited / queue / etc.) is copied server-side so the new
// run resumes from where this one stopped. After submitting, the Live
// panel auto-attaches to the new job_id.
async function ljpResumeJob() {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpResume');
  if (!btn || btn.disabled) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> resuming…';
  try {
    // Find the latest attempt number to point rerun_from at.
    const attemptsResp = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/attempts');
    if (!attemptsResp.ok) {
      alert('cannot list attempts for resume (HTTP ' + attemptsResp.status + ')');
      return;
    }
    const att = await attemptsResp.json();
    if (!att.attempts || !att.attempts.length) {
      alert('no attempts found on this job; nothing to resume from.');
      return;
    }
    const lastN = att.attempts[att.attempts.length - 1].n;

    // Pull URL + attempt_timeout_s from the previous job so the new
    // one keeps the same shape.
    const infoResp = await fetch('/jobs/' + encodeURIComponent(LJP.jobId));
    const info = infoResp.ok ? await infoResp.json() : {};
    const prevOpts = (info.options || {});
    const prevUrl = info.url || 'about:blank';
    const prevTimeout = prevOpts.attempt_timeout_s || 180;

    const body = {
      url: prevUrl,
      options: {
        mode: 'rerun',
        rerun_from: `${LJP.jobId}/attempts/${lastN}`,
        attempt_timeout_s: prevTimeout,
      },
    };
    const r = await fetch('/jobs', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      const detail = err && (Array.isArray(err.detail) ? err.detail.map(d => d.msg).join('\n') : err.detail);
      alert('resume failed (' + r.status + '): ' + (detail || r.statusText));
      return;
    }
    const created = await r.json().catch(() => null);
    if (created && created.job_id) ljpAttach(created.job_id);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Stop the in-flight job. Confirms before firing so an accidental click
// doesn't nuke a long-running crawl. The hub-side cancel marks the job
// "cancelled", kills the runner subprocess, and force-ends any held
// sessions.
async function ljpStopJob() {
  if (!LJP.jobId) return;
  const btn = document.getElementById('ljpStop');
  if (!btn || btn.disabled) return;
  if (!confirm(
    `Cancel job ${LJP.jobId}?\n\n`
    + `This stops the running sandbox immediately and closes any open `
    + `sessions. In-flight work is lost; assets already captured are kept.`
  )) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="ljp-spinner"></span> stopping…';
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/cancel', {
      method: 'POST',
    });
    if (!r.ok) {
      const err = await r.json().catch(() => null);
      alert('cancel failed (' + r.status + '): ' + (err && err.detail || r.statusText));
      return;
    }
    const result = await r.json();
    if (!result.cancelled) {
      alert('Job was already ' + (result.reason || 'finished') + '; nothing to cancel.');
    }
  } finally {
    btn.innerHTML = orig;
    // Status will refresh on the next /jobs/{id} poll (which fires the
    // done event over WS shortly after), flipping the badge + disabling
    // the button automatically. No manual state reset needed here.
  }
}

function ljpCloseWs() {
  if (LJP.ws) {
    try { LJP.ws.close(); } catch (_) {}
    LJP.ws = null;
  }
}
function ljpStopTimers() {
  if (LJP.pollTimer) { clearInterval(LJP.pollTimer); LJP.pollTimer = null; }
  if (LJP.statusTimer) { clearInterval(LJP.statusTimer); LJP.statusTimer = null; }
  if (LJP.codeTimer) { clearInterval(LJP.codeTimer); LJP.codeTimer = null; }
}

function ljpOpenWs() {
  if (!LJP.jobId || LJP.finished) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/jobs/${encodeURIComponent(LJP.jobId)}/events?since=${LJP.seenLines}`;
  const ws = new WebSocket(url);
  LJP.ws = ws;
  ws.onopen = () => {
    LJP.wsBackoff = 1000;
    ljpAppendMeta(LJP.seenLines === 0 ? '— connected' : `— reconnected (line ${LJP.seenLines})`);
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { ljpAppendLine(e.data); return; }
    if (ev.type === 'log') {
      ljpAppendLine(ev.data && ev.data.line ? ev.data.line : '');
    } else if (ev.type === 'done') {
      const st = ev.data && ev.data.status;
      ljpSetStatus(st);
      ljpAppendMeta('— job ended: ' + st);
      LJP.finished = true;
      try { ws.close(); } catch (_) {}
      // Final session sweep so the user sees the last state of the
      // session(s) the runner had open at the end.
      ljpRefreshSessions();
    } else if (ev.type === 'error') {
      ljpAppendMeta('error: ' + (ev.data && ev.data.message));
    } else {
      ljpAppendLine(e.data);
    }
  };
  ws.onclose = () => {
    if (LJP.finished || !LJP.jobId) return;
    ljpAppendMeta(`— disconnected; reconnecting in ${(LJP.wsBackoff/1000)|0}s`);
    setTimeout(ljpOpenWs, LJP.wsBackoff);
    LJP.wsBackoff = Math.min(LJP.wsBackoff * 2, 15000);
  };
}

function ljpAutoconnect(url) {
  if (!url) return url;
  if (url.indexOf('autoconnect=') !== -1) return url;
  return url + (url.includes('?') ? '&' : '?') + 'autoconnect=1&resize=scale&reconnect=1';
}

async function ljpRefreshStatus() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId));
    if (!r.ok) return;
    const info = await r.json();
    // Stash mode so ljpSetStatus can decide whether ▶ resume should be
    // enabled (codegen-loop / rerun have script; fetch doesn't).
    LJP.mode = (info.options || {}).mode || null;
    ljpSetStatus(info.status, info.progress && info.progress.phase);
    // Asset counter -- visible as soon as the first asset lands.
    const saved = (info.progress && info.progress.assets_saved) || 0;
    const failed = (info.progress && info.progress.assets_failed) || 0;
    const pill = document.getElementById('ljpAssetCount');
    if (saved > 0 || failed > 0) {
      pill.style.display = '';
      pill.textContent = `${saved} assets` + (failed ? ` (${failed} failed)` : '');
    } else {
      pill.style.display = 'none';
    }
    // Fetch-mode jobs carry their noVNC URL directly on JobInfo.
    // noVNC is LIVE only when the hub rewrote novnc_url to its
    // session-rooted proxy form ("/sessions/{sid}/novnc/..."):
    // _proxy_info does that only while a real session is resolvable.
    // Once the session is gone -- a finished job, OR a keepalive job
    // whose session hit its idle/absolute TTL (the job then cascades to
    // "completed" but keep_session stays true) -- novnc_url falls back
    // to the raw ABSOLUTE worker URL (or null). So: a relative
    // ("/...") novnc_url == session alive; anything else == gone.
    // This is reliable where the old status/keep_session guess wasn't:
    // it kept a dead viewer up for reaped keepalive jobs (e.g. opening
    // #live/<id> for such a job still showed noVNC).
    const _novnc = info.novnc_url || '';
    const _vncLive = _novnc.charAt(0) === '/';
    // Status-aware iframe lifecycle. Removing the iframe based ONLY on
    // novnc_url's shape ran too eagerly: as soon as the SDK called
    // DELETE /sessions/{sid}, the hub did state.sessions.remove() and
    // _find_active_session_id stopped returning the relative proxy URL
    // (because the session is no longer in the registry) -- but the
    // worker's noVNC bridge is still ALIVE for the entire drain window
    // (passive m3u8 / mp4 listener can take 5-20 min to finish a
    // multi-GB iframe video). Removing the iframe at that moment hid
    // the in-progress download from the operator who specifically
    // opened the panel to watch it.
    //
    // New rule: keep the iframe mounted while job.status is queued OR
    // running. Only force-unmount when status hits a terminal state
    // (completed / failed / cancelled / succeeded), at which point the
    // worker really has torn the lane down and the bridge is gone.
    const _statusTerminal = info.status === 'completed' || info.status === 'failed'
      || info.status === 'cancelled' || info.status === 'succeeded';
    if (_vncLive && !LJP.vncIframes.has('__job__')) {
      ljpMountVncFrame('__job__', {
        novnc_url: info.novnc_url,
        novnc_url_autoconnect: ljpAutoconnect(info.novnc_url),
        label: 'job ' + LJP.jobId.slice(0, 12),
      });
    } else if (_statusTerminal && LJP.vncIframes.has('__job__')) {
      ljpRemoveVncFrame('__job__', 'セッションは終了しました（noVNC は利用できません）');
    }
    // Refresh the thumbnail strip once the job is past the queued
    // phase. Cheap fetch -- only the gallery HTML -- but rate-limit it
    // by terminal-status so we don't hammer the disk forever.
    if (info.status && info.status !== 'queued') {
      if (!LJP.galleryStopped) {
        await ljpRefreshGallery();
        if (info.status === 'completed' || info.status === 'succeeded' || info.status === 'failed') {
          LJP.galleryStopped = true; // one more pass after terminal status, then stop polling it
        }
      }
    }
    // Tear down the periodic status / sessions / code timers when the
    // job is fully terminal AND not a keep_session crawl. keep_session
    // jobs keep mutating session state (noVNC iframe lifecycle, cookie
    // dumps) until the operator closes the session, so we leave them
    // alone. Plain Fetch / codegen-loop / vision-agent jobs in a
    // terminal state can't change anymore -- continuing to poll wastes
    // ~3 req/2.5s per opened Live panel for nothing.
    const _isTerminal = info.status === 'completed' || info.status === 'succeeded'
      || info.status === 'failed' || info.status === 'cancelled';
    const _keepSession = !!(info.options && info.options.keep_session);
    if (_isTerminal && !_keepSession && !LJP._terminalStopped) {
      // One final sessions + code sweep so a last-second update doesn't
      // get lost, then halt.
      try { await ljpRefreshSessions(); } catch (_) {}
      try { await ljpRefreshCode(); } catch (_) {}
      ljpStopTimers();
      LJP._terminalStopped = true;
    }
  } catch (_) {}
}

// Pull the gallery JSON and render a thumbnail strip inside the panel.
// We re-use the gallery endpoint -- but to keep the inline view light we
// just parse the asset hrefs out of the rendered HTML rather than asking
// the server for a separate JSON shape.
async function ljpRefreshGallery() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/assets.json');
    if (!r.ok) return;
    const data = await r.json();
    const items = data.items || [];
    const grid = document.getElementById('ljpGalleryGrid');
    const cnt = document.getElementById('ljpGalleryCount');
    const empty = document.getElementById('ljpGalleryEmpty');
    cnt.textContent = String(items.length);
    // Mirror the count onto the header pill -- codegen-loop captures
    // don't increment JobProgress.assets_saved (that's a fetch-pipeline
    // counter), so without this fall-back the pill stays hidden even
    // when files actually landed via session uploads.
    const pill = document.getElementById('ljpAssetCount');
    if (items.length > 0) {
      pill.style.display = '';
      // Don't overwrite a richer label that ljpRefreshStatus may have
      // already set from progress.assets_saved.
      if (!pill.textContent.includes('failed')) {
        pill.textContent = `${items.length} assets`;
      }
    }
    // Only re-render if the asset set actually changed, to avoid
    // flicker on poll. Use a content signature (count + sorted name
    // list) so an addition AND a same-count swap both trigger a
    // re-render -- pure count-based dedup missed the case where a
    // 3 GB video upload landed while an old image was evicted: count
    // stayed at N but the user's tile for the video never appeared.
    const signature = items.length + '|' + items.map(a => a.name).join('\x1f');
    if (LJP.gallerySignature === signature) return;
    LJP.gallerySignature = signature;
    LJP.galleryLastCount = items.length;
    grid.innerHTML = '';
    if (items.length === 0) {
      empty.style.display = (LJP.galleryStopped ? '' : 'none');
      return;
    }
    empty.style.display = 'none';
    // Render tiles. Each tile's media area is wrapped in a fixed-height
    // <div> so the slot reserves space even before the <img>/<video>
    // has loaded -- without the wrapper, loading=lazy can collapse the
    // tile to ~25px (just the name/size text), which is what produced
    // the "300 grey bars and no images" screenshot.
    for (const a of items) {
      const tile = document.createElement('a');
      tile.href = a.href;       // fallback for middle-click / ctrl-click -> new tab
      tile.target = '_blank';
      tile.title = `${a.name} — ${a.size_h} — click for details`;
      // Plain click: open the detail modal instead of navigating.
      tile.addEventListener('click', (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return;
        ev.preventDefault();
        ljpOpenAssetModal(a);
      });
      tile.style.cssText = 'display:flex; flex-direction:column; background:#fff; border:1px solid #e5e5e5; border-radius:4px; padding:6px; text-decoration:none; color:inherit; overflow:hidden; box-sizing:border-box; cursor:pointer;';
      const mediaBox = 'flex:1 1 auto; min-height:0; display:flex; align-items:center; justify-content:center; border-radius:3px; overflow:hidden;';
      const captionStyle = 'display:block; font-size:11px; margin-top:5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
      const sizeStyle = 'display:block; font-size:10px; color:#888;';
      if (a.kind === 'image') {
        tile.innerHTML =
          `<div style="${mediaBox} background:#f0eee9;">` +
            `<img loading="lazy" src="${esc(a.href)}" alt="" style="max-width:100%; max-height:100%; object-fit:contain; display:block;">` +
          `</div>` +
          `<span style="${captionStyle}">${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      } else if (a.kind === 'video') {
        tile.innerHTML =
          `<div style="${mediaBox} background:#000;">` +
            `<video preload="none" src="${esc(a.href)}" muted style="max-width:100%; max-height:100%; object-fit:contain; display:block;"></video>` +
          `</div>` +
          `<span style="${captionStyle}">▶ ${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      } else {
        tile.innerHTML =
          `<div style="${mediaBox} background:#fafafa; color:#c0392b; font-family:monospace; font-weight:700; font-size:1.5em;">.${esc(a.ext || 'bin')}</div>` +
          `<span style="${captionStyle}">${esc(a.name)}</span>` +
          `<span style="${sizeStyle}">${esc(a.size_h)}</span>`;
      }
      grid.appendChild(tile);
    }
  } catch (_) {}
}

// --- asset detail modal ---------------------------------------------------
function ljpOpenAssetModal(a) {
  const modal = document.getElementById('ljpAssetModal');
  document.getElementById('ljpAssetModalName').textContent = a.name || '';
  const preview = document.getElementById('ljpAssetModalPreview');
  preview.innerHTML = '';
  const src = a.href;
  // Build preview matching the kind. Set width/height after the
  // resource loads so we can populate the "寸法" row, too.
  const dims = document.getElementById('ljpAssetModalDims');
  dims.innerHTML = '<span style="color:#888;">(loading…)</span>';
  if (a.kind === 'image') {
    const img = document.createElement('img');
    img.src = src;
    img.alt = a.name || '';
    img.style.cssText = 'display:block; max-width:100%; max-height:60vh; object-fit:contain;';
    img.addEventListener('load', () => {
      dims.textContent = `${img.naturalWidth} × ${img.naturalHeight} px`;
    });
    img.addEventListener('error', () => {
      dims.innerHTML = '<span style="color:#c33;">(image failed to load)</span>';
    });
    preview.appendChild(img);
  } else if (a.kind === 'video') {
    const v = document.createElement('video');
    v.src = src;
    v.controls = true;
    v.preload = 'metadata';
    v.style.cssText = 'display:block; max-width:100%; max-height:60vh;';
    v.addEventListener('loadedmetadata', () => {
      const dur = isFinite(v.duration) ? v.duration.toFixed(1) + 's' : '?';
      dims.textContent = `${v.videoWidth || '?'} × ${v.videoHeight || '?'} px · duration ${dur}`;
    });
    preview.appendChild(v);
  } else if (a.kind === 'audio') {
    const audio = document.createElement('audio');
    audio.src = src;
    audio.controls = true;
    audio.preload = 'metadata';
    audio.style.cssText = 'display:block; width:90%;';
    audio.addEventListener('loadedmetadata', () => {
      const dur = isFinite(audio.duration) ? audio.duration.toFixed(1) + 's' : '?';
      dims.textContent = `duration ${dur}`;
    });
    preview.appendChild(audio);
  } else {
    // Other / unknown -- show a stylised extension placeholder.
    const ph = document.createElement('div');
    ph.style.cssText = 'color:#c0392b; font-family:monospace; font-weight:700; font-size:3em;';
    ph.textContent = `.${a.ext || 'bin'}`;
    preview.appendChild(ph);
    dims.innerHTML = '<span style="color:#888;">(n/a)</span>';
  }

  // Metadata rows
  const pageCell = document.getElementById('ljpAssetModalPage');
  if (a.page_url) {
    pageCell.innerHTML = `<a href="${esc(a.page_url)}" target="_blank" style="color:#06a;">${esc(a.page_url)}</a>`;
  } else {
    pageCell.innerHTML = '<span style="color:#888;">(no page URL recorded -- legacy asset or fetch-mode upload)</span>';
  }
  const srcCell = document.getElementById('ljpAssetModalSrc');
  if (a.source_url) {
    srcCell.innerHTML = `<a href="${esc(a.source_url)}" target="_blank" style="color:#06a;">${esc(a.source_url)}</a>`;
  } else {
    srcCell.innerHTML = '<span style="color:#888;">(no source URL recorded)</span>';
  }
  const hub = document.getElementById('ljpAssetModalHubLink');
  hub.href = src;
  hub.textContent = src;
  document.getElementById('ljpAssetModalSize').textContent = a.size_h ? `${a.size_h} (${a.size} bytes)` : `${a.size} bytes`;
  document.getElementById('ljpAssetModalMime').textContent =
    (a.mime ? a.mime : '(unknown)') + ` · .${a.ext || 'bin'}`;

  modal.style.display = 'flex';
}
function ljpCloseAssetModal() {
  const modal = document.getElementById('ljpAssetModal');
  modal.style.display = 'none';
  // Stop any playing media so audio doesn't leak after close.
  document.getElementById('ljpAssetModalPreview').innerHTML = '';
}

async function ljpRefreshSessions() {
  if (!LJP.jobId) return;
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/sessions');
    if (!r.ok) return;
    const data = await r.json();
    const sessions = data.sessions || [];
    const seen = new Set();
    for (const s of sessions) {
      seen.add(s.session_id);
      if (!LJP.vncIframes.has(s.session_id)) {
        ljpMountVncFrame(s.session_id, {
          novnc_url: s.novnc_url,
          novnc_url_autoconnect: s.novnc_url_autoconnect,
          label: s.session_id,
          // Pre-fill the URL <input> with the session's initial URL so
          // the operator immediately sees "where we are" instead of a
          // bare placeholder. Subsequent navigates overwrite this via
          // the keydown handler in ljpMountVncFrame.
          initial_url: s.initial_url || '',
        });
      }
      // Reflect the actual current page URL into the address-bar input.
      // Fire-and-forget per session; throttled implicitly by this
      // function's 3 s setInterval. Skips if the operator is mid-edit.
      ljpRefreshSessionUrl(s.session_id).catch(() => {});
    }
    // Remove iframes for sessions that are no longer alive (closed by
    // worker / TTL). Keep the special '__job__' iframe (fetch mode);
    // its lifecycle is handled in ljpRefreshStatus.
    for (const sid of Array.from(LJP.vncIframes.keys())) {
      if (sid === '__job__') continue;
      if (!seen.has(sid)) {
        ljpRemoveVncFrame(sid, 'セッションが終了しました');
      }
    }
    ljpUpdateVncCount();
    // Toggle the "↻ refresh" + "↓ video" buttons: shown iff ≥1 live
    // session is bound to this job. Covers both keep_session Fetch
    // jobs (where the session lingers past completion) and
    // codegen-loop / rerun jobs (which have sessions while attempts
    // are running). The buttons stay HIDDEN for plain Fetch jobs
    // without keep_session (no session after WorkerJobComplete ->
    // nothing for refresh / video download to act on).
    const sessionPresent = sessions.length > 0;
    const refreshBtn = document.getElementById('ljpRefresh');
    if (refreshBtn) {
      refreshBtn.style.display = sessionPresent ? '' : 'none';
    }
    const videoBtn = document.getElementById('ljpVideoDl');
    if (videoBtn) {
      videoBtn.style.display = sessionPresent ? '' : 'none';
    }
  } catch (_) {}
}

// Pull the session's actual current page URL from the worker (via
// /sessions/{sid}/pages, which returns each tab + its URL + which is
// default) and reflect it into the noVNC header's URL <input>. Called
// once per session per ljpRefreshSessions cycle (= every 3 s).
//
// Two guards keep this from stomping operator input:
//   1. document.activeElement === inputEl -> operator is currently
//      typing; leave their unfinished URL alone.
//   2. inputEl.disabled -> a navigate is in flight (doNavigate sets
//      this); the post-navigate URL will come back on the next tick.
//
// Errors (closed session, worker unreachable, parse failure) are
// swallowed -- this is purely cosmetic feedback and any failure just
// means the URL stays at the last value the operator / initial_url
// pre-fill put there.
async function ljpRefreshSessionUrl(sid) {
  const wrap = LJP.vncIframes.get(sid);
  if (!wrap) return;
  const inputEl = wrap.querySelector('.ljp-vnc-url');
  if (!inputEl) return;
  if (document.activeElement === inputEl) return;
  if (inputEl.disabled) return;
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/pages');
    if (!r.ok) return;
    const d = await r.json();
    const pages = Array.isArray(d.pages) ? d.pages : [];
    if (pages.length === 0) return;
    // Default tab wins; fall back to the first listed tab.
    const def = pages.find(p => p && p.is_default) || pages[0];
    const url = def && def.url ? String(def.url) : '';
    if (!url) return;
    // Re-check focus/disabled after the await -- the operator may
    // have started typing while the fetch was in flight.
    if (document.activeElement === inputEl) return;
    if (inputEl.disabled) return;
    if (inputEl.value !== url) inputEl.value = url;
  } catch (_) {}
}

// Live noVNC zoom (CSS transform:scale on each iframe). The iframe
// renders at a fixed "logical" width × height (large enough to look
// good); zoom only changes the visual scale + the layout space we
// claim. Persisted across reloads as paprika.ljp.vncZoom.
// Base reference size at zoom=1.0 (= 100%). 1280x720 is a sensible
// "Live panel sits comfortably in a 1080p viewport" default. The
// actual Chrome window AND the iframe display size are now BOTH
// computed as round(base * zoom), so 100% means Chrome=1280x720 +
// iframe=1280x720 1:1, 50% means 640x360 both, etc. No CSS scale
// transform -- noVNC renders Chrome pixel-perfect into the iframe.
const LJP_VNC_BASE_W = 1280;
const LJP_VNC_BASE_H = 720;
// Chrome window resolution for the noVNC session. FIXED at 1.0 =
// 1280x720 (decoupled from the zoom dropdown, which now drives the
// in-browser PAGE zoom). The iframe itself fills the pane width and
// noVNC's resize=scale scales the 720p framebuffer to fit -- so the
// viewer always matches the panel width instead of leaving a black
// gap. The "↔ fit" button + per-mount resize use these dims.
function ljpVncZoom() {
  return 1.0;
}
// In-browser PAGE zoom (the dropdown's new job, 案A). Persisted under a
// dedicated key so it can't be confused with the old window-size value.
function ljpPageZoom() {
  try {
    const v = parseFloat(localStorage.getItem('paprika.ljp.pageZoom') || '1.0');
    if (v > 0.1 && v <= 5) return v;
  } catch (_) {}
  return 1.0;
}
// Real session_id for a vncIframes key (skip the synthetic '__job__'
// placeholder, which we can't address by session_id).
function ljpSessionKey(key) {
  return (key && key !== '__job__') ? key : null;
}
// Apply the current page zoom to ONE session via the dedicated /zoom
// API (worker CDP Emulation.setPageScaleFactor). This magnifies the
// actual paint output -- so it ALSO zooms full-viewport (100vw/100vh)
// cross-origin iframe players, which CSS `zoom` cannot. /zoom is
// allowed even on a fetch-owned session and is NOT recorded in the
// operator recipe trace (viewing aid only).
async function ljpApplyPageZoomToSession(sessionId) {
  const sid = ljpSessionKey(sessionId);
  if (!sid) return;
  const z = ljpPageZoom();
  try {
    await fetch('/sessions/' + encodeURIComponent(sid) + '/zoom', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({factor: z}),
    });
  } catch (_) { /* best-effort */ }
}
async function ljpApplyPageZoomAll() {
  if (!LJP || !LJP.vncIframes) return;
  await Promise.all([...LJP.vncIframes.keys()].map(ljpApplyPageZoomToSession));
}
// Forward an operator control action to the recording endpoint so the
// step lands in operator_actions.json (learn-from-operator). `action`
// is a {kind, ...} dict; `label` is the human tag stored in the trace.
async function ljpOpAction(sessionKey, action, label, opts) {
  const sid = ljpSessionKey(sessionKey);
  if (!sid) { alert('この pane はセッションIDが不明なため操作できません'); return null; }
  opts = opts || {};
  try {
    const r = await fetch('/sessions/' + encodeURIComponent(sid) + '/operator_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        action,
        label: label || action.kind,
        screenshot: opts.screenshot !== false,  // default: capture before-shot
      }),
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) {
      alert('操作失敗 (' + r.status + '): ' + (out.detail || r.statusText));
      return null;
    }
    return out;
  } catch (e) {
    alert('操作失敗: ' + e);
    return null;
  }
}
function ljpVncZoomDims() {
  const z = ljpVncZoom();
  return {
    w: Math.round(LJP_VNC_BASE_W * z),
    h: Math.round(LJP_VNC_BASE_H * z),
    z,
  };
}
// Apply the current zoom to one iframe's wrapper. No CSS scale: the
// iframe takes its actual pixel size, and Chrome's OS window is
// resized in parallel via POST /sessions/{sid}/resize (handled by
// ljpResizeChromeForSession). The net effect is pixel-perfect 1:1
// rendering at every zoom level instead of the previous
// "blurry / aliased noVNC image" produced by CSS transform: scale.
function ljpApplyVncZoomToBox(box) {
  const f = box.querySelector('iframe');
  if (!f) return;
  // Fit the noVNC ENTIRELY inside the pane height so nothing -- incl.
  // the remote page's bottom horizontal scrollbar -- is cut off (the
  // previous width:100% sizing overflowed the ~720px pane vertically
  // and hid the bottom). Size by HEIGHT with a 16:9 box, centered;
  // noVNC (resize=scale) scales the 1280x720 framebuffer to fit. The
  // dropdown drives the in-browser page zoom, not this display size.
  f.style.height = '100%';
  f.style.width = 'auto';
  f.style.aspectRatio = '16 / 9';
  f.style.transform = '';
  f.style.transformOrigin = '';
  const scaleBox = f.parentElement;
  // 684 ≈ grid height (720) minus the wrap header + borders, so the
  // full viewer (and any bottom scrollbar) stays on screen.
  scaleBox.style.height = '684px';
  scaleBox.style.width = '100%';
  scaleBox.style.display = 'flex';
  scaleBox.style.alignItems = 'center';
  scaleBox.style.justifyContent = 'center';
  scaleBox.style.overflow = 'hidden';
  scaleBox.style.background = '#000';
}
function ljpApplyVncZoom() {
  document.querySelectorAll('#ljpVncGrid > div').forEach(ljpApplyVncZoomToBox);
}

// Push the current zoom-derived dimensions to the worker so Chrome's
// OS window matches the iframe pixel-for-pixel. Iterates every
// mounted noVNC iframe; the '__job__' fetch-fallback iframe is
// skipped because we don't know its session_id locally.
async function ljpResizeChromeForSession(sessionId) {
  if (!sessionId || sessionId === '__job__') return;
  const {w, h} = ljpVncZoomDims();
  try {
    await fetch(
      '/sessions/' + encodeURIComponent(sessionId) + '/resize',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({width: w, height: h}),
      },
    );
  } catch (_) { /* best-effort */ }
}
async function ljpResizeAllVncChrome() {
  if (!LJP || !LJP.vncIframes) return;
  await Promise.all(
    [...LJP.vncIframes.keys()].map(ljpResizeChromeForSession),
  );
}

function ljpMountVncFrame(key, s) {
  // Hard guard: a session can appear in /jobs/{id}/sessions before the
  // worker has assigned a lane (novnc_url=null). Mounting an iframe
  // with src="" produces an empty pane AND an "↗ open" link that
  // navigates to "/" (the admin UI) -- exactly the bug the user hit.
  // Skip until the URL is populated; the next poll will retry.
  const src = s.novnc_url_autoconnect || s.novnc_url;
  if (!src) return;

  // URL-based dedup. A single worker lane often appears under TWO
  // identifiers in our polling loops:
  //   * the synthetic '__job__' key (from JobInfo.novnc_url, mounted
  //     by ljpRefreshStatus)
  //   * the real session_id key (from /jobs/{id}/sessions, mounted
  //     by ljpRefreshSessions)
  // For fetch-mode jobs only the first exists; for codegen-loop /
  // rerun jobs BOTH exist and point at the same URL, which produced
  // two identical noVNC panes ("job xxxxxx" + "ses_xxxxxx") stacked
  // on top of each other. Canonicalise to the URL's pathname (no
  // origin, no query, no hash) so a relative src from JobInfo and the
  // absolute iframe.src the browser has already resolved still
  // compare equal. Special case: if a real session arrives AFTER the
  // __job__ placeholder was mounted, swap the placeholder out so the
  // more informative session_id label wins.
  const canonOf = (u) => {
    try { return new URL(u, window.location.origin).pathname; }
    catch (_) { return (u || '').split('?')[0].split('#')[0]; }
  };
  const canon = canonOf(src);
  for (const [otherKey, existing] of LJP.vncIframes.entries()) {
    const f = existing.querySelector('iframe');
    if (!f || !f.src) continue;
    if (canonOf(f.src) !== canon) continue;
    if (key !== '__job__' && otherKey === '__job__') {
      if (existing.parentNode) existing.parentNode.removeChild(existing);
      LJP.vncIframes.delete('__job__');
      break;
    }
    return;  // duplicate -- keep the iframe already on screen
  }

  const grid = document.getElementById('ljpVncGrid');
  // Drop the placeholder on first mount.
  const empty = grid.querySelector('.empty');
  if (empty) empty.remove();
  const wrap = document.createElement('div');
  // No border / border-radius on the wrapper -- the only horizontal rule
  // we want is `.ljp-vnc-head { border-bottom }` as the head/iframe seam.
  // (Previously this wrapper had `border:1px solid #ccc; border-radius:6px`
  // which produced rounded corners + a left/top/right outline around the
  // head bar; operator feedback was that the side+top borders and the
  // rounding looked out of place.) `overflow:hidden` is kept so any iframe
  // scrollbar gutter doesn't poke past the wrapper edges.
  wrap.style.cssText = 'overflow:hidden; background:#000;';
  const head = document.createElement('div');
  // Light Chrome-chrome bar: matches the LJP top-header pill aesthetic
  // (cream/beige .pill + --la-* accent) instead of the previous dark
  // Chrome-tab bar. Sits above the dark noVNC iframe so the contrast
  // reads as "window chrome above viewport". The ``ljp-vnc-head`` class
  // hooks a scoped CSS rule (admin.css) that mirrors the LJP-top pill
  // behaviour -- per-button --la-bg accent applied at rest, gentle
  // lift on hover -- so the global .pill red-fill hover doesn't dominate.
  head.className = 'ljp-vnc-head';
  // Inline styling kept minimal -- visual identity now lives in CSS
  // (.ljp-vnc-head) so this bar matches #liveJobPanel h2 / .ljp-actions-group
  // exactly (same background gradient, same pill rules).
  head.style.cssText = 'display:flex; align-items:center; gap:6px;';
  // Operator control buttons (learn-from-operator Phase 1). Shown only
  // for real sessions (not the synthetic '__job__' fetch placeholder).
  // Each press is forwarded to /operator_action which executes it AND
  // records it to the per-job trace for later recipe distillation.
  const _opSid = ljpSessionKey(key);
  // Per-button accent: blueish for benign nav (戻る/進む/reload), red-ish
  // for destructive (popup close), green for affirmative (URL go).
  // Matches the LJP top-header convention (--la-bg / --la-bd / --la-fg
  // custom props applied by inline style; the .pill class reads them
  // on hover via the LJP override, with the global .pill as fallback
  // for visible default fill).
  // Per-button accent custom properties. Trailing "opacity:1; cursor:pointer;"
  // mirrors the LJP top-header button style block (e.g. #ljpStop) so the
  // markup is byte-for-byte interchangeable.
  const _navAccent   = '--la-bg: #eef0ff; --la-bd: #9bf; --la-fg: #0a4a7e; opacity: 1; cursor: pointer;';
  const _popupAccent = '--la-bg: #fde6e6; --la-bd: #d68080; --la-fg: #8a1d1d; opacity: 1; cursor: pointer;';
  const _shotAccent  = '--la-bg: #fff7e6; --la-bd: #e8c97a; --la-fg: #7a5a14; opacity: 1; cursor: pointer;';
  const _goAccent    = '--la-bg: #e6f6e6; --la-bd: #7fc77f; --la-fg: #1a5a1a; opacity: 1; cursor: pointer;';
  const _rightAccent = '--la-bg: #eef0f6; --la-bd: #bbc; --la-fg: #333; opacity: 1; cursor: pointer;';
  // Left-side nav cluster: 戻る / 進む / reload only. URL entry moved
  // to the dedicated <input> in the centre; popup-close moved to the
  // right next to fit / open since it's used less often than nav.
  // Each button uses the same structure as #ljpStop / #ljpResume in
  // the LJP top header: <button class="pill" data-i18n-title=... title=...
  // style="--la-bg/--la-bd/--la-fg; opacity:1; cursor:pointer;">
  // <iconify-icon>icon</iconify-icon> <span data-i18n=...>label</span>
  // </button>. Icon-only buttons omit the trailing span.
  const navBtns = _opSid ? (
    `<button class="pill ljp-op-back" data-i18n-title="ljp.vnc.back.title" title="戻る (記録)" style="${_navAccent}"><iconify-icon icon="lucide:chevron-left"></iconify-icon> <span data-i18n="ljp.vnc.back">戻る</span></button>` +
    `<button class="pill ljp-op-fwd" data-i18n-title="ljp.vnc.fwd.title" title="進む (記録)" style="${_navAccent}"><iconify-icon icon="lucide:chevron-right"></iconify-icon> <span data-i18n="ljp.vnc.fwd">進む</span></button>` +
    `<button class="pill ljp-vnc-reload" data-i18n-title="ljp.vnc.reload.title" title="このフレームを再読み込み" style="${_navAccent}"><iconify-icon icon="lucide:rotate-cw"></iconify-icon></button>`
  ) : (
    `<button class="pill ljp-vnc-reload" data-i18n-title="ljp.vnc.reload.title" title="このフレームを再読み込み" style="${_navAccent}"><iconify-icon icon="lucide:rotate-cw"></iconify-icon></button>`
  );
  // Centre: URL <input>. Operator types a URL + Enter to navigate via
  // /sessions/{sid}/operator_action {kind:navigate, url:...}. Pre-fills
  // with the session's initial_url so the operator sees where they
  // landed. Read-only synthetic placeholder ('__job__') sessions get
  // their session label instead since there's no real session to
  // navigate.
  // URL input / read-only label both stretch via flex:1; visual styling
  // (height, border, font) comes from .ljp-vnc-head CSS so the input
  // lines up with the .pill buttons.
  const _initialVal = _opSid ? (s.initial_url || s.label || '') : (s.label || '');
  const urlInput = _opSid ?
    `<input class="ljp-vnc-url" type="text" placeholder="https://example.com (Enter または → で移動)" value="${esc(_initialVal)}" style="flex:1; min-width:200px;" autocomplete="off" spellcheck="false">` :
    `<code style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; background:#fff; color:#5a5a68; padding:6px 10px; border:1px solid #d4cfca; border-radius:7px; font-size:.78em; font-family:ui-monospace,Consolas,monospace;" title="${esc(s.label)}">${esc(s.label)}</code>`;
  // Go button (icon-only "→"): same navigation action as pressing Enter
  // in the URL input. Sits immediately to the right of the input so it
  // reads as the input's submit affordance. Only shown for real
  // sessions (no point on the read-only '__job__' fetch placeholder).
  const goBtn = _opSid ?
    `<button class="pill ljp-vnc-go" data-i18n-title="ljp.vnc.go.title" title="URL へ移動" style="${_goAccent}"><iconify-icon icon="lucide:arrow-right"></iconify-icon></button>` :
    '';
  // Right cluster: screenshot, popup-close, zoom, fit, open.
  // Screenshot button keeps icon-only structure (no label span); popup
  // / fit / open follow the LJP top-header button structure with both
  // iconify-icon + <span data-i18n>.
  const shotBtn =
    `<button class="pill ljp-vnc-screenshot" data-i18n-title="ljp.vnc.screenshot.title" title="現在のフレームを保存" style="${_shotAccent}"><iconify-icon icon="lucide:camera"></iconify-icon></button>`;
  const popupBtn = _opSid ?
    `<button class="pill ljp-op-popups" data-i18n-title="ljp.vnc.popups.title" title="広告などのポップアップ・別タブを閉じる (記録)" style="${_popupAccent}"><iconify-icon icon="lucide:x"></iconify-icon> <span data-i18n="ljp.vnc.popups">popup</span></button>` :
    '';
  // Zoom select: styling comes from .ljp-vnc-head select.ljp-vnc-zoom
  // in CSS (height/border/font aligned with the .pill row).
  const zoomSelect =
    `<select class="ljp-vnc-zoom" title="ページズーム (Ctrl+/Ctrl- 相当)">` +
      `<option value="0.5">50%</option>` +
      `<option value="0.75">75%</option>` +
      `<option value="1.0" selected>100%</option>` +
      `<option value="1.25">125%</option>` +
      `<option value="1.5">150%</option>` +
      `<option value="2.0">200%</option>` +
    `</select>`;
  head.innerHTML =
    navBtns +
    urlInput +
    goBtn +
    shotBtn +
    popupBtn +
    zoomSelect +
    `<button class="pill ljp-vnc-fit" data-i18n-title="ljp.vnc.fit.title" title="Chrome のウィンドウサイズを現在の zoom 設定に再同期する" style="${_rightAccent}"><iconify-icon icon="lucide:maximize"></iconify-icon> <span data-i18n="ljp.vnc.fit">fit</span></button>` +
    `<a class="pill ljp-vnc-open" href="${esc(src)}" target="_blank" data-i18n-title="ljp.vnc.open.title" title="新しいタブで開く" style="${_rightAccent}"><iconify-icon icon="lucide:external-link"></iconify-icon> <span data-i18n="ljp.vnc.open">open</span></a>`;
  // The iframe lives inside a transform-scale-box so the layout
  // reserves the *visually-scaled* size, not the logical size.
  const scaleBox = document.createElement('div');
  scaleBox.style.cssText = 'background:#000; position:relative;';
  const frame = document.createElement('iframe');
  frame.src = src;
  frame.style.cssText = 'display:block; border:0; background:#000;';
  scaleBox.appendChild(frame);
  wrap.appendChild(head);
  wrap.appendChild(scaleBox);
  grid.appendChild(wrap);
  ljpApplyVncZoomToBox(wrap);
  LJP.vncIframes.set(key, wrap);
  ljpUpdateVncCount();
  // Wire the reload button: re-assign frame.src to itself to force a
  // fresh load (a cache-buster query param would also work but noVNC
  // is sensitive to URL changes -- the autoconnect/reconnect query
  // params must stay verbatim).
  const reloadBtn = head.querySelector('.ljp-vnc-reload');
  if (reloadBtn) {
    reloadBtn.addEventListener('click', () => {
      const cur = frame.src;
      frame.src = 'about:blank';
      // 50ms gap so the browser actually tears down the old viewer
      // before reattaching to the same URL.
      setTimeout(() => { frame.src = cur; }, 50);
    });
  }
  // Wire operator control buttons (recorded via /operator_action).
  if (_opSid) {
    const _flash = (btn, ok) => {
      const t = btn.textContent;
      btn.textContent = ok ? '✓' : '✕';
      setTimeout(() => { btn.textContent = t; }, 1200);
    };
    const backBtn = head.querySelector('.ljp-op-back');
    if (backBtn) backBtn.addEventListener('click', async () => {
      backBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'back'}, '戻る');
      backBtn.disabled = false; _flash(backBtn, !!r);
    });
    const fwdBtn = head.querySelector('.ljp-op-fwd');
    if (fwdBtn) fwdBtn.addEventListener('click', async () => {
      fwdBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'forward'}, '進む');
      fwdBtn.disabled = false; _flash(fwdBtn, !!r);
    });
    const popupsBtn = head.querySelector('.ljp-op-popups');
    if (popupsBtn) popupsBtn.addEventListener('click', async () => {
      popupsBtn.disabled = true;
      const r = await ljpOpAction(_opSid, {kind: 'close_popups'}, 'ポップアップ閉じる');
      popupsBtn.disabled = false; _flash(popupsBtn, !!r);
    });
    // URL input: Enter (or the adjacent → Go button) to navigate.
    // Replaces the old prompt()-driven .ljp-op-url button (button
    // removed from the header markup, the <input> sits in its place +
    // lets the operator see / edit the current URL inline like a real
    // browser address bar).
    const urlInputEl = head.querySelector('.ljp-vnc-url');
    const goBtnEl    = head.querySelector('.ljp-vnc-go');
    if (urlInputEl) {
      // Shared submit handler -- Enter keypress and Go click both end
      // up here. Disables the input while the navigate is in flight so
      // the operator can see the action is being processed, then
      // flashes ✓/✕ on the input itself for feedback.
      const doNavigate = async () => {
        const url = (urlInputEl.value || '').trim();
        if (!url) return;
        urlInputEl.disabled = true;
        if (goBtnEl) goBtnEl.disabled = true;
        try {
          const r = await ljpOpAction(_opSid, {kind: 'navigate', url: url}, 'URL移動: ' + url);
          _flash(urlInputEl, !!r);
        } finally {
          urlInputEl.disabled = false;
          if (goBtnEl) goBtnEl.disabled = false;
        }
      };
      urlInputEl.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        doNavigate();
      });
      if (goBtnEl) {
        goBtnEl.addEventListener('click', () => { doNavigate(); });
      }
    }
    // Screenshot button: hits POST /jobs/{id}/screenshot which captures
    // the current frame, saves it to data/jobs/{id}/assets/screenshot-*
    // (= filtered into the Screenshot sub-tab via screenshots.json),
    // and refreshes the Screenshot tab viewer so the new shot appears.
    const shotBtnEl = head.querySelector('.ljp-vnc-screenshot');
    if (shotBtnEl) {
      shotBtnEl.addEventListener('click', async () => {
        if (!LJP.jobId) { _flash(shotBtnEl, false); return; }
        shotBtnEl.disabled = true;
        let ok = false;
        try {
          const r = await fetch('/jobs/' + encodeURIComponent(LJP.jobId) + '/screenshot', { method: 'POST' });
          ok = r.ok;
        } catch (_) { ok = false; }
        shotBtnEl.disabled = false;
        _flash(shotBtnEl, ok);
        // Refresh the Screenshot tab viewer so the new shot shows up
        // without waiting for the next 5s poll.
        if (ok && typeof ljpShotRefreshScreenshots === 'function') {
          try { ljpShotRefreshScreenshots(); } catch (_) {}
        }
      });
    }
    // Apply the current page zoom to this freshly-mounted session
    // (best-effort; runs after noVNC has had a moment to connect).
    setTimeout(() => { ljpApplyPageZoomToSession(_opSid); }, 1500);
  }
  // Wire the "↔ fit" button -- POST /sessions/{sid}/resize with the
  // iframe's logical width/height. Only meaningful when `key` is a
  // real session_id (not the '__job__' placeholder used for fetch).
  const fitBtn = head.querySelector('.ljp-vnc-fit');
  if (fitBtn) {
    if (!key || key === '__job__') {
      // Fetch-mode synthetic iframe: we don't know the session_id
      // here (the URL has it but parsing is fragile). Hide the fit
      // button rather than firing unrouted requests.
      fitBtn.style.display = 'none';
    } else {
      fitBtn.addEventListener('click', async () => {
        const original = fitBtn.textContent;
        fitBtn.disabled = true;
        fitBtn.textContent = '↔ …';
        try {
          const {w, h} = ljpVncZoomDims();
          const r = await fetch(
            '/sessions/' + encodeURIComponent(key) + '/resize',
            {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({width: w, height: h}),
            },
          );
          if (!r.ok) {
            const detail = await r.json().catch(() => ({}));
            alert('resize failed (' + r.status + '): ' +
                  (detail.detail || r.statusText));
          } else {
            fitBtn.textContent = '↔ ✓';
            setTimeout(() => { fitBtn.textContent = original; }, 1500);
          }
        } catch (e) {
          alert('resize failed: ' + e);
        } finally {
          fitBtn.disabled = false;
          if (fitBtn.textContent === '↔ …') fitBtn.textContent = original;
        }
      });
    }
  }
  // Auto-fit on first mount: schedule a resize after noVNC has had
  // a moment to connect (the websocket handshake + initial RFB
  // exchange typically takes ~1 sec; firing CDP setWindowBounds
  // before that just gets queued, but doing it early-ish lets the
  // operator see the resized Chrome from the moment the screen
  // becomes visible). Skip for the '__job__' iframe (fetch fallback)
  // -- no session_id to target.
  if (key && key !== '__job__') {
    setTimeout(() => {
      ljpResizeChromeForSession(key);
    }, 1500);
  }
}

// Remove one noVNC iframe by key. When the grid empties out, restore a
// placeholder so the pane shows a message instead of a blank black box.
function ljpRemoveVncFrame(key, emptyMsg) {
  const wrap = LJP.vncIframes.get(key);
  if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
  LJP.vncIframes.delete(key);
  const grid = document.getElementById('ljpVncGrid');
  if (grid && LJP.vncIframes.size === 0 && !grid.querySelector('.empty')) {
    grid.innerHTML = '<div class="empty" style="padding:20px; text-align:center; '
      + 'color:#888; border:1px dashed #444; border-radius:6px;">'
      + (emptyMsg || 'noVNC will appear once a session opens…')
      + '</div>';
  }
  ljpUpdateVncCount();
}

function ljpUpdateVncCount() {
  const n = LJP.vncIframes.size;
  document.getElementById('ljpVncCount').textContent = String(n);
}

// --- tab switching for the Live panel -------------------------------------
function ljpSetTab(name) {
  const all = ['log', 'vnc', 'screenshot', 'links', 'network', 'code', 'gallery'];
  if (!all.includes(name)) name = 'log';
  document.querySelectorAll('.ljp-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.ljpTab === name);
  });
  document.querySelectorAll('#liveJobPanel .ljp-pane').forEach(p => {
    p.style.display = (p.dataset.ljpPane === name) ? '' : 'none';
  });
  try { localStorage.setItem('paprika.ljp.activeTab', name); } catch (_) {}
  // Activate / deactivate the live-screenshot refresh timer when
  // entering / leaving the screenshot tab. We don't burn CPU
  // re-fetching frames the operator can't see.
  if (typeof ljpShotOnTabChange === 'function') ljpShotOnTabChange(name);
  // Same idea for the Links tab -- only poll /sessions/{sid}/links
  // while the operator is actually looking at the URL list.
  if (typeof ljpLinksOnTabChange === 'function') ljpLinksOnTabChange(name);
  // Network tab -- only poll while visible.
  if (typeof ljpNetOnTabChange === 'function') ljpNetOnTabChange(name);
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
  ljpLinksResetTimer();
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
      LJP_NET.cache = flat;
      const cnt0 = document.getElementById('ljpNetCount');
      if (cnt0) cnt0.textContent = String(flat.length);
      ljpNetRender();
      if (status) {
        status.textContent = flat.length
          ? (flat.length + ' item(s) · from saved dump · ' + new Date().toLocaleTimeString())
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
  LJP._terminalStopped = false;
  // Reset the saved-screenshots viewer so a fresh attach starts at
  // index 0 (no shots) and follow-latest defaults back to true.
  LJP_SHOT.shots = [];
  LJP_SHOT.currentIndex = -1;
  LJP_SHOT.followLatest = true;
  LJP.mode = null;
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
  setChk('fetchDownloadVideo',  false);
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
        alert('AI調査モードは目標 (goal) が必須です。テキストエリアに記入してください。');
        _restoreSubmitBtn();
        return;
      }
      const _max = parseInt(document.getElementById('fetchInvestigateMaxAttempts').value, 10) || 3;
      const _tmo = parseInt(document.getElementById('fetchInvestigateTimeoutSec').value, 10) || 600;
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

// ---- hosts (per-host cookie registry) ------------------------------------
// State: cookies are stored server-side; the UI fetches a list of host
// summaries on demand (refresh / tab activation) and opens a modal to
// edit one host's full cookie array. The modal does the JSON parse
// client-side so we can show a helpful error before POSTing.

const HOST_LIST_URL = '/hosts';
const HOST_ONE_URL = (h) => '/hosts/' + encodeURIComponent(h);
const HOST_VISITED_URL = (h) => '/hosts/' + encodeURIComponent(h) + '/visited';

// Hosts list paging state: persisted only in-memory so a refresh
// returns to page 1. Search box is debounced so each keystroke
// doesn't fire a request.
const HOST_PAGE_SIZE = 50;
let _hostListState = { q: '', offset: 0, total: 0 };
let _hostSearchTimer = null;

async function fetchHostListPaged(q, offset, limit) {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (offset) params.set('offset', offset);
  if (limit) params.set('limit', limit);
  try {
    const r = await fetch(HOST_LIST_URL + '?' + params.toString());
    if (!r.ok) return { total: 0, hosts: [] };
    return await r.json();
  } catch (e) {
    return { total: 0, hosts: [] };
  }
}

// Backend timestamps are naive UTC (datetime.utcnow()) and serialize
// WITHOUT a zone designator, e.g. "2026-05-23T10:00:00.123456". JS
// Date.parse() treats such date-time strings as LOCAL time, skewing
// every relative display by the viewer's UTC offset (+9h in JST).
// Append 'Z' when no zone is present so they parse as UTC.
function parseServerTime(iso) {
  if (!iso) return NaN;
  let s = String(iso).trim();
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s += 'Z';
  return Date.parse(s);
}

// Absolute local wall-clock for a server (UTC) timestamp.
// "2026-05-23T10:00:00Z" -> "05/23 19:00:23" in JST.
function fmtClock(iso) {
  const t = parseServerTime(iso);
  if (!Number.isFinite(t)) return '';
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}/${p(d.getDate())} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtAgoOrNever(iso) {
  if (!iso) return '<span style="color:#999;">never</span>';
  const s = Math.max(0, Math.floor((Date.now() - parseServerTime(iso)) / 1000));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

async function renderHosts() {
  const { q, offset } = _hostListState;
  const data = await fetchHostListPaged(q, offset, HOST_PAGE_SIZE);
  const items = data.hosts || [];
  _hostListState.total = data.total || 0;
  const tbody = document.querySelector('#hostsTable tbody');
  const head = document.getElementById('hostCount');
  if (head) head.textContent = data.total || 0;
  const tabCnt = document.getElementById('cntHosts');
  if (tabCnt) tabCnt.textContent = data.total || 0;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML = '<tr><td colspan=7 class="empty">'
      + (q ? `no hosts matched ${esc(q)}` : 'no hosts registered')
      + '</td></tr>';
    renderHostsPager(0, 0);
    return;
  }
  tbody.innerHTML = items.map(h => {
    const host = esc(h.host || '');
    const notes = h.notes ? esc(h.notes) : '<span style="color:#999;">—</span>';
    const visited = h.visited_count || 0;
    // Show recrawl_patterns count alongside visited count so the
    // operator can see at a glance whether either is set.
    const patCount = (h.recrawl_patterns || []).length;
    const patHint = patCount > 0 ? ` <small style="color:#196b2c;" title="${patCount} recrawl pattern(s)">🎯${patCount}</small>` : '';
    const visitedBtn = visited > 0
      ? `<button class="pill" style="background:#eef8ff; border-color:#9bf; padding:1px 8px; font-size:.78em;" onclick="openVisitedModal('${host}')"><iconify-icon icon="lucide:filter"></iconify-icon> dedup (${visited})${patHint}</button>`
      : `<button class="pill" style="background:#f5f5fa; border-color:#bbc; color:#888; padding:1px 8px; font-size:.78em;" onclick="openVisitedModal('${host}')"><iconify-icon icon="lucide:filter"></iconify-icon> dedup (0)${patHint}</button>`;
    // Recipes column: count + cross-tab link. Clicking jumps to the
    // Recipes tab pre-filtered to this host so the operator can
    // inspect / delete entries without navigating to each host's
    // record manually.
    const rcpCount = (h.fetch_recipes || []).length;
    const rcpBtn = rcpCount > 0
      ? `<button class="pill" style="background:#fff7e6; border-color:#e8c97a; color:#7a5a14; padding:1px 8px; font-size:.78em;" onclick="openRecipesForHost('${host}')"><iconify-icon icon="lucide:bento"></iconify-icon> ${rcpCount}</button>`
      : `<span style="color:#999; font-size:.78em;">—</span>`;
    return `
      <tr>
        <td><code>${host}</code></td>
        <td>${h.cookie_count || 0}</td>
        <td>${visitedBtn}</td>
        <td>${rcpBtn}</td>
        <td>${notes}</td>
        <td><small>${fmtAgoOrNever(h.updated_at)}</small></td>
        <td><small>${fmtAgoOrNever(h.last_used_at)}</small></td>
        <td>
          <button class="pill" style="background:#eef8ff; border-color:#9bf;" onclick="openHostModal('${host}')"><iconify-icon icon="lucide:pencil"></iconify-icon> edit</button>
        </td>
      </tr>`;
  }).join('');
  renderHostsPager(_hostListState.total, _hostListState.offset);
}

function renderHostsPager(total, offset) {
  const el = document.getElementById('hostsPager');
  if (!el) return;
  if (total <= HOST_PAGE_SIZE) { el.innerHTML = ''; return; }
  const pageNo = Math.floor(offset / HOST_PAGE_SIZE) + 1;
  const pageCount = Math.ceil(total / HOST_PAGE_SIZE);
  const prevDisabled = offset <= 0 ? 'disabled' : '';
  const nextDisabled = (offset + HOST_PAGE_SIZE) >= total ? 'disabled' : '';
  el.innerHTML = `
    <button class="pill" ${prevDisabled} onclick="hostsPagerJump(-1)">‹ prev</button>
    <span>page <strong>${pageNo}</strong> / ${pageCount}  (${total} total)</span>
    <button class="pill" ${nextDisabled} onclick="hostsPagerJump(+1)">next ›</button>
  `;
}

// ===========================================================================
// Recipes tab: cross-host HostRecord.fetch_recipes browser
// ===========================================================================
// State for the Recipes tab's local filter / pagination. Unlike Hosts (which
// goes through the paged /hosts API), recipes are denormalized client-side
// from the full host list -- there's no /recipes endpoint yet, and the
// number of recipes in practice is small.

const RECIPE_PAGE_SIZE = 50;
const _recipeListState = {
  q: "",
  offset: 0,
  // The flat (host, recipe-index, recipe) tuples extracted from
  // every host's fetch_recipes. Refreshed by renderRecipes.
  flat: [],
};

// Pull every host page (limit=500 covers the practical fleet) and
// flatten ``fetch_recipes`` into a single browseable list.
async function fetchAllRecipes() {
  // /hosts caps at 500 per call; iterate until we have them all.
  const all = [];
  let off = 0;
  while (true) {
    let r;
    try {
      r = await fetch('/hosts?offset=' + off + '&limit=500');
    } catch (_) { break; }
    if (!r.ok) break;
    const d = await r.json();
    for (const h of (d.hosts || [])) {
      const recipes = h.fetch_recipes || [];
      for (let i = 0; i < recipes.length; i++) {
        all.push({ host: h.host, index: i, recipe: recipes[i] });
      }
    }
    const got = (d.hosts || []).length;
    off += got;
    if (got < (d.limit || 500)) break;   // last page
    if (off >= (d.total || 0)) break;
  }
  return all;
}

async function renderRecipes() {
  const tbody = document.querySelector('#recipesTable tbody');
  if (!tbody) return;
  // Reflect "loading" so the empty state doesn't flash before fetch.
  if (_recipeListState.flat.length === 0) {
    tbody.innerHTML = '<tr><td colspan=7 class="empty">loading…</td></tr>';
  }
  const flat = await fetchAllRecipes();
  _recipeListState.flat = flat;
  // Apply filter.
  const q = (_recipeListState.q || '').toLowerCase().trim();
  const filtered = q
    ? flat.filter(({ host, recipe }) => {
        return (host || '').toLowerCase().includes(q)
          || (recipe.pattern || '').toLowerCase().includes(q)
          || (recipe.description || '').toLowerCase().includes(q)
          || (recipe.goal || '').toLowerCase().includes(q);
      })
    : flat.slice();
  // Sort: host asc, then pattern asc, then created desc.
  filtered.sort((a, b) => {
    const hc = (a.host || '').localeCompare(b.host || '');
    if (hc) return hc;
    const pc = (a.recipe.pattern || '').localeCompare(b.recipe.pattern || '');
    if (pc) return pc;
    return (b.recipe.created_at || '').localeCompare(a.recipe.created_at || '');
  });
  // Update header + tab pill counts.
  document.getElementById('recipeCount').textContent = String(filtered.length);
  const tabCnt = document.getElementById('cntRecipes');
  if (tabCnt) tabCnt.textContent = String(flat.length);
  // Empty?
  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan=7 class="empty">'
      + (q ? `no recipes matched ${esc(q)}` : 'no recipes saved yet — Jobs から「recipe として保存」してください')
      + '</td></tr>';
    document.getElementById('recipesPager').innerHTML = '';
    return;
  }
  // Paginate.
  const off = Math.max(0, _recipeListState.offset);
  const page = filtered.slice(off, off + RECIPE_PAGE_SIZE);
  tbody.innerHTML = page.map(({ host, index, recipe }) => {
    const pattern = esc(recipe.pattern || '*');
    const desc = recipe.description
      ? esc(recipe.description)
      : '<span style="color:#999;">—</span>';
    const actCnt = Array.isArray(recipe.actions) ? recipe.actions.length : 0;
    const codeBytes = (recipe.code || '').length;
    const codeHint = codeBytes > 0
      ? `<small title="${codeBytes} bytes">${codeBytes.toLocaleString()} B</small>`
      : '<span style="color:#999;">—</span>';
    const created = fmtAgoOrNever(recipe.created_at);
    return `
      <tr style="cursor:pointer;" onclick="openRecipeDetail('${esc(host)}', ${index})">
        <td><code>${esc(host)}</code></td>
        <td><code style="font-size:.85em;">${pattern}</code></td>
        <td>${desc}</td>
        <td>${actCnt > 0 ? actCnt : '<span style="color:#999;">0</span>'}</td>
        <td>${codeHint}</td>
        <td><small>${created}</small></td>
        <td onclick="event.stopPropagation()">
          <div class="menu-wrap">
            <button class="action-btn" onclick="toggleMenu(this)" title="recipe actions">${ICONS.moreV}</button>
            <div class="menu">
              <button onclick="openRecipeEdit('${esc(host)}', ${index})"><span class="ico"><iconify-icon icon="lucide:pencil"></iconify-icon></span> 編集</button>
              <div class="divider"></div>
              <button class="danger" onclick="deleteRecipe('${esc(host)}', ${index})">${ico('trash')} 削除</button>
            </div>
          </div>
        </td>
      </tr>`;
  }).join('');
  renderRecipesPager(filtered.length, off);
}

function renderRecipesPager(total, offset) {
  const el = document.getElementById('recipesPager');
  if (!el) return;
  if (total <= RECIPE_PAGE_SIZE) { el.innerHTML = ''; return; }
  const pageNo = Math.floor(offset / RECIPE_PAGE_SIZE) + 1;
  const pageCount = Math.ceil(total / RECIPE_PAGE_SIZE);
  const prevDisabled = offset <= 0 ? 'disabled' : '';
  const nextDisabled = (offset + RECIPE_PAGE_SIZE) >= total ? 'disabled' : '';
  el.innerHTML = `
    <button class="pill" ${prevDisabled} onclick="recipesPagerJump(-1)">‹ prev</button>
    <span>page <strong>${pageNo}</strong> / ${pageCount}  (${total} total)</span>
    <button class="pill" ${nextDisabled} onclick="recipesPagerJump(+1)">next ›</button>
  `;
}

window.recipesPagerJump = function recipesPagerJump(dir) {
  _recipeListState.offset = Math.max(0, _recipeListState.offset + dir * RECIPE_PAGE_SIZE);
  renderRecipes();
};

// Cross-tab navigation: clicked on a host's recipes count in the Hosts
// table -> switch to Recipes tab and prefill the search box with that
// host so the listing is filtered.
window.openRecipesForHost = function openRecipesForHost(host) {
  _recipeListState.q = host || '';
  _recipeListState.offset = 0;
  const inp = document.getElementById('recipeSearch');
  if (inp) inp.value = host || '';
  // Switch tab. The admin UI's tab system uses data-tab attribute on
  // <button class="tab"> -- click()-ing the matching button is the
  // canonical way to navigate (also touches hash router state).
  const btn = document.querySelector('button.tab[data-tab="recipes"]');
  if (btn) btn.click();
  renderRecipes();
};

// Read-only detail modal: clicked on a recipe row.
// "view" mode = recipe 行クリックで開く読み取り専用表示。kebab → 編集
// は openRecipeEdit() (edit mode) を使う。両者は同じ modal を共有して
// 表示要素を出し分ける (recipeDetailMeta vs recipeDetailEditForm,
// recipeDetailSave の表示切替)。
function _setRecipeModalMode(mode) {
  const isEdit = mode === 'edit';
  const meta = document.getElementById('recipeDetailMeta');
  const form = document.getElementById('recipeDetailEditForm');
  const save = document.getElementById('recipeDetailSave');
  const code = document.getElementById('recipeDetailCode');
  const badge = document.getElementById('recipeDetailModeBadge');
  if (meta) meta.style.display = isEdit ? 'none' : 'grid';
  if (form) form.style.display = isEdit ? 'flex' : 'none';
  if (save) save.style.display = isEdit ? '' : 'none';
  if (code) {
    code.readOnly = !isEdit;
    code.style.background = isEdit ? '' : '#fafafa';
  }
  if (badge) badge.textContent = isEdit ? '(編集中)' : '';
}

window.openRecipeDetail = function openRecipeDetail(host, index) {
  const entry = _recipeListState.flat.find(
    (e) => e.host === host && e.index === index,
  );
  if (!entry) return;
  const r = entry.recipe;
  _setRecipeModalMode('view');
  document.getElementById('recipeDetailTitle').textContent =
    `${host} / ${r.pattern || '*'}`;
  const meta = document.getElementById('recipeDetailMeta');
  meta.innerHTML = `
    <span style="color:#666;">host</span>     <code>${esc(host)}</code>
    <span style="color:#666;">pattern</span>  <code>${esc(r.pattern || '*')}</code>
    <span style="color:#666;">description</span> <span>${esc(r.description || '—')}</span>
    <span style="color:#666;">goal</span>     <span>${esc(r.goal || '—')}</span>
    <span style="color:#666;">actions</span>  <small>${Array.isArray(r.actions) ? r.actions.length : 0} 件 (編集画面では非表示)</small>
    <span style="color:#666;">created</span>  <small>${fmtAgoOrNever(r.created_at)}</small>
    <span style="color:#666;">updated</span>  <small>${fmtAgoOrNever(r.updated_at)}</small>
  `;
  document.getElementById('recipeDetailCode').value = r.code || '';
  const errEl = document.getElementById('recipeDetailError');
  if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
  // Wire the delete button to the current host/index.
  const delBtn = document.getElementById('recipeDetailDelete');
  delBtn.onclick = () => {
    document.getElementById('recipeDetailModal').style.display = 'none';
    deleteRecipe(host, index);
  };
  document.getElementById('recipeDetailModal').style.display = 'flex';
};

// kebab menu の 編集。recipe の pattern / description / goal / code を
// 編集可能な状態で modal を開く。actions は不可視 + 不変 (operator が
// 直接弄るとレシピが壊れる、というのが要件の理由)。
window.openRecipeEdit = function openRecipeEdit(host, index) {
  const entry = _recipeListState.flat.find(
    (e) => e.host === host && e.index === index,
  );
  if (!entry) return;
  const r = entry.recipe;
  _setRecipeModalMode('edit');
  document.getElementById('recipeDetailTitle').textContent =
    `${host} / ${r.pattern || '*'} — 編集`;
  document.getElementById('recipeEditHostDisplay').textContent = host;
  document.getElementById('recipeEditPattern').value = r.pattern || '*';
  document.getElementById('recipeEditDescription').value = r.description || '';
  document.getElementById('recipeEditGoal').value = r.goal || '';
  document.getElementById('recipeDetailCode').value = r.code || '';
  const errEl = document.getElementById('recipeDetailError');
  if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
  const delBtn = document.getElementById('recipeDetailDelete');
  delBtn.onclick = () => {
    document.getElementById('recipeDetailModal').style.display = 'none';
    deleteRecipe(host, index);
  };
  const saveBtn = document.getElementById('recipeDetailSave');
  saveBtn.onclick = () => saveRecipeEdit(host, index);
  document.getElementById('recipeDetailModal').style.display = 'flex';
};

// Save edited recipe via PUT /hosts/{host} (entire fetch_recipes list
// rewrite). actions は元のレシピの値をそのまま温存する (UI 上は表示も
// 編集もしない)。
window.saveRecipeEdit = async function saveRecipeEdit(host, index) {
  const errEl = document.getElementById('recipeDetailError');
  const setErr = (m) => { if (errEl) { errEl.textContent = m; errEl.style.display = ''; } };
  const newPattern = document.getElementById('recipeEditPattern').value.trim() || '*';
  const newDesc = document.getElementById('recipeEditDescription').value.trim();
  const newGoal = document.getElementById('recipeEditGoal').value;
  const newCode = document.getElementById('recipeDetailCode').value;
  if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }

  // GET 最新の host record (他のレシピを潰さないようサーバ最新を取る)
  let rec;
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host));
    if (!r.ok) { setErr('GET /hosts failed: ' + r.status); return; }
    rec = await r.json();
  } catch (e) { setErr('GET /hosts crashed: ' + e); return; }
  const recipes = (rec.fetch_recipes || []).slice();
  if (index < 0 || index >= recipes.length) {
    setErr('recipe index out of range');
    return;
  }
  const orig = recipes[index] || {};
  // Merge: 編集対象のフィールドだけ書き換え、その他 (actions /
  // success_count / created_at など) は保持。updated_at もサーバ側で
  // 上書きされる想定だが、念のためここで stamp しておく。
  const merged = Object.assign({}, orig, {
    pattern: newPattern,
    description: newDesc,
    goal: newGoal,
    code: newCode,
    updated_at: new Date().toISOString(),
  });
  recipes[index] = merged;
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fetch_recipes: recipes }),
    });
    if (!r.ok) {
      const t = await r.text();
      setErr('PUT /hosts failed: ' + r.status + ' ' + t.slice(0, 240));
      return;
    }
  } catch (e) { setErr('PUT /hosts crashed: ' + e); return; }
  document.getElementById('recipeDetailModal').style.display = 'none';
  renderRecipes();
  if (typeof renderHosts === 'function') renderHosts();
};

// Delete a recipe via the existing PUT /hosts/{host} endpoint with the
// updated fetch_recipes array (the API doesn't have a granular
// DELETE for individual entries today; we just rewrite the list).
window.deleteRecipe = async function deleteRecipe(host, index) {
  if (!confirm(`Delete recipe ${index + 1} for ${host}?`)) return;
  // Pull the host's full record so we keep cookies / notes / etc.
  let rec;
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host));
    if (!r.ok) { alert('GET /hosts failed: ' + r.status); return; }
    rec = await r.json();
  } catch (e) { alert('GET /hosts crashed: ' + e); return; }
  const recipes = (rec.fetch_recipes || []).slice();
  if (index < 0 || index >= recipes.length) {
    alert('recipe index out of range');
    return;
  }
  recipes.splice(index, 1);
  // PUT /hosts/{host} accepts fetch_recipes in the body. Send only
  // that field so we don't accidentally wipe other state.
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fetch_recipes: recipes }),
    });
    if (!r.ok) {
      const t = await r.text();
      alert('PUT /hosts failed: ' + r.status + '\n' + t.slice(0, 300));
      return;
    }
  } catch (e) { alert('PUT /hosts crashed: ' + e); return; }
  renderRecipes();
  // Also refresh Hosts so the recipes column count updates.
  if (typeof renderHosts === 'function') renderHosts();
};

function hostsPagerJump(dir) {
  const off = _hostListState.offset + dir * HOST_PAGE_SIZE;
  _hostListState.offset = Math.max(0, off);
  renderHosts();
}

function _hostModalEl() { return document.getElementById('hostModal'); }

function _openHostModal() {
  const m = _hostModalEl();
  if (m) { m.style.display = 'flex'; }
}

function closeHostModal() {
  _entityHashClear('hosts');
  const m = _hostModalEl();
  if (m) { m.style.display = 'none'; }
  const err = document.getElementById('hostModalCookieErr');
  if (err) err.textContent = '';
}

async function openHostModal(host) {
  // host = '' / undefined  -> add new
  // The "host-match / all-cookies" refetch toolbar only makes sense
  // when the modal was reached via "save → host" on a live session,
  // so hide it for this plain add/edit path.
  if (typeof _hideCookieRefetchToggle === 'function') _hideCookieRefetchToggle();
  const titleEl = document.getElementById('hostModalTitle');
  const hostInput = document.getElementById('hostModalHost');
  const cookiesArea = document.getElementById('hostModalCookies');
  const notesInput = document.getElementById('hostModalNotes');
  const popupSel = document.getElementById('hostModalPopupPolicy');
  const delBtn = document.getElementById('hostModalDelete');
  if (host) {
    _entityHashSync('hosts', host);
    titleEl.textContent = 'Edit host: ' + host;
    hostInput.value = host;
    hostInput.disabled = true;
    delBtn.style.display = 'inline-block';
    try {
      const r = await fetch(HOST_ONE_URL(host));
      if (r.ok) {
        const rec = await r.json();
        cookiesArea.value = JSON.stringify(rec.cookies || [], null, 2);
        notesInput.value = rec.notes || '';
        if (popupSel) popupSel.value = rec.popup_policy || 'kill';
      } else {
        cookiesArea.value = '[]';
        notesInput.value = '';
        if (popupSel) popupSel.value = 'kill';
      }
    } catch (e) {
      cookiesArea.value = '[]';
      notesInput.value = '';
      if (popupSel) popupSel.value = 'kill';
    }
  } else {
    titleEl.textContent = 'Add host';
    hostInput.value = '';
    hostInput.disabled = false;
    cookiesArea.value = '[]';
    notesInput.value = '';
    if (popupSel) popupSel.value = 'kill';
    delBtn.style.display = 'none';
  }
  _openHostModal();
  if (!host) hostInput.focus();
}

async function saveHostModal() {
  const hostInput = document.getElementById('hostModalHost');
  const cookiesArea = document.getElementById('hostModalCookies');
  const notesInput = document.getElementById('hostModalNotes');
  const errEl = document.getElementById('hostModalCookieErr');
  errEl.textContent = '';
  const host = (hostInput.value || '').trim();
  if (!host) {
    errEl.textContent = 'host is required';
    hostInput.focus();
    return;
  }
  let cookies;
  try {
    cookies = JSON.parse(cookiesArea.value || '[]');
  } catch (e) {
    errEl.textContent = 'cookies JSON parse error: ' + e.message;
    return;
  }
  if (!Array.isArray(cookies)) {
    errEl.textContent = 'cookies must be a JSON array';
    return;
  }
  // Host edit modal no longer touches recrawl_patterns -- omitting
  // the field from the PUT preserves the existing patterns (managed
  // separately via the "📋 dedup" modal).
  const popupSel = document.getElementById('hostModalPopupPolicy');
  const body = {
    cookies: cookies,
    notes: (notesInput.value || '').trim() || null,
    popup_policy: popupSel ? popupSel.value : 'kill',
  };
  try {
    const r = await fetch(HOST_ONE_URL(host), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  closeHostModal();
  renderHosts();
}

async function deleteHostModal() {
  const hostInput = document.getElementById('hostModalHost');
  const host = (hostInput.value || '').trim();
  if (!host) { closeHostModal(); return; }
  if (!confirm("Delete host '" + host + "'? Sessions targeting this host will no longer get cookies auto-injected.")) return;
  try {
    const r = await fetch(HOST_ONE_URL(host), {method: 'DELETE'});
    if (!r.ok) {
      const t = await r.text();
      alert('delete failed (' + r.status + '): ' + t.slice(0, 200));
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  closeHostModal();
  renderHosts();
}

function _pasteCookieTemplate() {
  const cookiesArea = document.getElementById('hostModalCookies');
  if (!cookiesArea) return;
  const tmpl = [
    {
      "name": "session_token",
      "value": "REPLACE_ME",
      "domain": ".example.com",
      "path": "/",
      "secure": true,
      "httpOnly": true,
      "sameSite": "Lax"
    }
  ];
  cookiesArea.value = JSON.stringify(tmpl, null, 2);
  cookiesArea.focus();
}

// Wire up modal buttons + the tab-switch hook.
(function wireHosts() {
  const closeBtn = document.getElementById('hostModalClose');
  const cancelBtn = document.getElementById('hostModalCancel');
  const saveBtn = document.getElementById('hostModalSave');
  const delBtn = document.getElementById('hostModalDelete');
  const addBtn = document.getElementById('addHostBtn');
  const refreshBtn = document.getElementById('refreshHostsBtn');
  const pasteBtn = document.getElementById('hostModalPaste');
  const searchInput = document.getElementById('hostSearch');
  if (closeBtn) closeBtn.addEventListener('click', closeHostModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeHostModal);
  if (saveBtn) saveBtn.addEventListener('click', saveHostModal);
  if (delBtn) delBtn.addEventListener('click', deleteHostModal);
  if (addBtn) addBtn.addEventListener('click', () => openHostModal(''));
  if (refreshBtn) refreshBtn.addEventListener('click', renderHosts);
  if (pasteBtn) pasteBtn.addEventListener('click', _pasteCookieTemplate);
  // Debounce search input -- type-and-pause triggers a refetch.
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(_hostSearchTimer);
      _hostSearchTimer = setTimeout(() => {
        _hostListState.q = (searchInput.value || '').trim();
        _hostListState.offset = 0;
        renderHosts();
      }, 250);
    });
  }
  // Close on Escape, click-outside.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = _hostModalEl();
      if (m && m.style.display === 'flex') closeHostModal();
    }
  });
  const m = _hostModalEl();
  if (m) {
    m.addEventListener('click', (e) => {
      if (e.target === m) closeHostModal();
    });
  }

  // ===== Recipes tab wiring =====
  // Lazy: Recipes tab elements may not exist when this initialiser
  // runs (older admin.html builds without the panel), so guard.
  const _recipeSearchEl = document.getElementById('recipeSearch');
  const _recipeRefreshEl = document.getElementById('refreshRecipesBtn');
  const _recipeDetailModal = document.getElementById('recipeDetailModal');
  const _recipeDetailClose = document.getElementById('recipeDetailClose');
  const _recipeDetailOk = document.getElementById('recipeDetailOk');
  let _recipeSearchTimer = null;
  if (_recipeSearchEl) {
    _recipeSearchEl.addEventListener('input', () => {
      clearTimeout(_recipeSearchTimer);
      _recipeSearchTimer = setTimeout(() => {
        _recipeListState.q = (_recipeSearchEl.value || '').trim();
        _recipeListState.offset = 0;
        renderRecipes();
      }, 250);
    });
  }
  if (_recipeRefreshEl) {
    _recipeRefreshEl.addEventListener('click', () => {
      _recipeListState.flat = [];   // force re-fetch
      renderRecipes();
    });
  }
  function _closeRecipeModal() {
    if (_recipeDetailModal) _recipeDetailModal.style.display = 'none';
  }
  if (_recipeDetailClose) _recipeDetailClose.addEventListener('click', _closeRecipeModal);
  if (_recipeDetailOk) _recipeDetailOk.addEventListener('click', _closeRecipeModal);
  if (_recipeDetailModal) {
    _recipeDetailModal.addEventListener('click', (e) => {
      if (e.target === _recipeDetailModal) _closeRecipeModal();
    });
  }
  // Initial Recipes count load (silent in background so the tab pill
  // has a number even before the operator opens the tab).
  if (typeof renderRecipes === 'function') {
    renderRecipes();
  }
  // Refresh the table whenever the Hosts tab is activated.
  document.querySelectorAll('#tabs .tab').forEach(btn => {
    if (btn.dataset.tab === 'hosts') {
      btn.addEventListener('click', renderHosts);
    }
  });
})();

// ---- Chrome profile registry (admin UI) ----------------------------------

// ---- Chrome extension registry ------------------------------------------
//
// Mirrors the Profiles tab structure but simpler -- extensions don't
// have a "default" concept (all enabled extensions load on every
// lane), so there's no star-as-default UI here.

async function renderExtensions() {
  let data;
  try {
    const r = await fetch('/extensions');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    console.error('extensions: load failed', e);
    data = { extensions: [] };
  }
  const items = data.extensions || [];
  const tbody = document.querySelector('#extensionsTable tbody');
  const head = document.getElementById('extensionCount');
  const tabCnt = document.getElementById('cntExtensions');
  if (head) head.textContent = items.length;
  if (tabCnt) tabCnt.textContent = items.length;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="7" style="padding:12px; color:#888; text-align:center;">'
      + 'no extensions yet — click <em>upload</em> to add one</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(e => {
    const sizeKb = Math.round((e.size_bytes || 0) / 1024);
    const updated = (e.updated_at || '').slice(0, 16).replace('T', ' ');
    const enabledChecked = e.enabled !== false ? 'checked' : '';
    return `
      <tr style="border-bottom:1px solid #eee;">
        <td style="padding:8px;"><code>${esc(e.slug)}</code></td>
        <td style="padding:8px;">
          <div style="font-weight:600;">${esc(e.name || e.slug)}</div>
          ${e.description ? `<div style="color:#888; font-size:.85em;">${esc(e.description)}</div>` : ''}
        </td>
        <td style="padding:8px; color:#666; font-size:.88em;">${esc(e.version || '—')}</td>
        <td style="padding:8px; color:#888; font-size:.85em;">${sizeKb} KB</td>
        <td style="padding:8px; color:#888; font-size:.85em;">${esc(updated)}</td>
        <td style="padding:8px;">
          <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" class="ext-enabled-toggle" data-slug="${esc(e.slug)}" ${enabledChecked}>
            <span style="font-size:.85em; color:${e.enabled === false ? '#999' : '#196b2c'};">${e.enabled === false ? 'disabled' : 'enabled'}</span>
          </label>
        </td>
        <td style="padding:8px;">
          <a class="pill" href="/extensions/${encodeURIComponent(e.slug)}/download" target="_blank" style="background:#eef0ff; border-color:#6a8ec7; color:#3a5ca8;" title="ダウンロード (tar.gz)"><iconify-icon icon="lucide:download"></iconify-icon></a>
          <button class="pill ext-delete-btn" data-slug="${esc(e.slug)}" style="background:#fee; border-color:#c88; color:#933;" title="削除"><iconify-icon icon="lucide:trash-2"></iconify-icon></button>
        </td>
      </tr>`;
  }).join('');
  // Wire enable/disable toggle.
  tbody.querySelectorAll('.ext-enabled-toggle').forEach(cb => {
    cb.addEventListener('change', async () => {
      const slug = cb.dataset.slug;
      try {
        const r = await fetch('/extensions/' + encodeURIComponent(slug) + '/enabled', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: cb.checked }),
        });
        if (!r.ok) {
          alert('Toggle failed (HTTP ' + r.status + ')');
          cb.checked = !cb.checked;
          return;
        }
      } catch (e) {
        alert('Toggle failed: ' + e);
        cb.checked = !cb.checked;
        return;
      }
      renderExtensions();
    });
  });
  // Wire delete.
  tbody.querySelectorAll('.ext-delete-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const slug = b.dataset.slug;
      if (!confirm('Delete extension "' + slug + '"?')) return;
      try {
        const r = await fetch('/extensions/' + encodeURIComponent(slug), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) {
          alert('Delete failed (HTTP ' + r.status + ')');
          return;
        }
      } catch (e) { alert('Delete failed: ' + e); return; }
      renderExtensions();
    });
  });
}

(function wireExtensionUpload() {
  const btn  = document.getElementById('extUploadBtn');
  const file = document.getElementById('extUploadFile');
  if (!btn || !file) return;
  btn.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const f = file.files && file.files[0];
    if (!f) return;
    // Default slug from the filename (strip extension); operator
    // can edit before confirming.
    const base = (f.name || '').replace(/\.(zip|crx|tar\.gz|tgz)$/i, '');
    const suggested = base.toLowerCase().replace(/[^a-z0-9._\-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
    const slug = (prompt('Slug for this extension (kebab-case):', suggested) || '').trim();
    if (!slug) { file.value = ''; return; }
    const buf = await f.arrayBuffer();
    try {
      const r = await fetch('/extensions/' + encodeURIComponent(slug), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Filename': f.name,
        },
        body: buf,
      });
      if (!r.ok) {
        const err = await r.text();
        alert('Upload failed (HTTP ' + r.status + '): ' + err);
        return;
      }
    } catch (e) {
      alert('Upload failed: ' + e);
      return;
    } finally {
      file.value = '';
    }
    renderExtensions();
  });
})();

const refreshExtensionsBtn = document.getElementById('refreshExtensionsBtn');
if (refreshExtensionsBtn) refreshExtensionsBtn.addEventListener('click', renderExtensions);

document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'extensions') btn.addEventListener('click', renderExtensions);
});
renderExtensions();

async function renderProfiles() {
  // Pulls /profiles, paints the table, updates the tab count badge.
  // No pagination -- profile counts stay small (typically a handful
  // per operator), so a flat list is fine. fmtAgoOrNever is already
  // defined for the Hosts tab; reuse it for "uploaded N hours ago".
  let data;
  try {
    const r = await fetch('/profiles');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    data = await r.json();
  } catch (e) {
    console.error('profiles: load failed', e);
    data = { profiles: [] };
  }
  const items = data.profiles || [];
  const tbody = document.querySelector('#profilesTable tbody');
  const head = document.getElementById('profileCount');
  const tabCnt = document.getElementById('cntProfiles');
  if (head) head.textContent = items.length;
  if (tabCnt) tabCnt.textContent = items.length;
  if (!tbody) return;
  if (items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="empty">no profiles uploaded yet — '
      + 'use the upload area above, the CLI, or the Paprika Bridge '
      + 'extension</td></tr>';
    return;
  }
  const defaultName = data.default || null;
  // Stash the current default name on window so the Workers tab
  // can star-mark the same name without an extra /profiles round
  // trip on every render. Refreshed every time renderProfiles()
  // runs (= every time the Profiles tab is shown or the user
  // explicitly hits refresh).
  window._profilesDefaultName = defaultName;
  // Banner just above the table: which profile (if any) gets auto-
  // applied to jobs that don't set options.use_profile explicitly.
  const banner = document.getElementById('profileDefaultBanner');
  if (banner) {
    if (defaultName) {
      banner.innerHTML =
        `<iconify-icon icon="lucide:star"></iconify-icon> `
        + `Default profile: <code>${esc(defaultName)}</code> `
        + `<small>(auto-applied when <code>options.use_profile</code> is omitted)</small> `
        + `<button class="pill" style="margin-left:8px; padding:1px 8px; font-size:.78em;" `
        +   `onclick="clearDefaultProfile()">clear</button>`;
      banner.style.display = 'block';
    } else {
      banner.innerHTML =
        '<small>No default profile set. '
        + 'Jobs without <code>options.use_profile</code> use the lane\'s stock profile.</small>';
      banner.style.display = 'block';
    }
  }
  tbody.innerHTML = items.map(p => {
    const name = esc(p.name || '');
    const note = p.note ? esc(p.note) : '<span style="color:#999;">—</span>';
    const src = p.source_machine
      ? `<small>${esc(p.source_machine)}</small>`
      : '<span style="color:#999;">—</span>';
    const isDefault = !!p.is_default;
    const rowStyle = isDefault
      ? 'background:#fff8e1;'    // pale yellow tint for the default row
      : '';
    const nameCell = isDefault
      ? `<code>${name}</code> <span style="color:#d4a13d;" title="default profile">★</span>`
      : `<code>${name}</code>`;
    const defaultBtn = isDefault
      ? `<button class="pill" style="padding:1px 8px; font-size:.78em; opacity:.5; cursor:default;" disabled>default</button>`
      : `<button class="pill" style="background:#fef5e7; border-color:#d4a13d; color:#8a5a00; padding:1px 8px; font-size:.78em;" onclick="setDefaultProfile('${name}')" title="auto-apply to jobs without options.use_profile">set default</button>`;
    return `
      <tr style="${rowStyle}">
        <td>${nameCell}</td>
        <td style="text-align:right;"><small>${esc(p.size_human || '')}</small></td>
        <td><small title="${esc(p.uploaded_at || '')}">${fmtAgoOrNever(p.uploaded_at)}</small></td>
        <td>${src}</td>
        <td>${note}</td>
        <td style="text-align:right; white-space:nowrap;">
          ${defaultBtn}
          <button class="pill" style="background:#fef1f1; border-color:#e88; color:#a00; padding:1px 8px; font-size:.78em;"
                  onclick="deleteProfile('${name}')"
                  title="delete this profile">
            <iconify-icon icon="lucide:trash-2"></iconify-icon> delete
          </button>
        </td>
      </tr>`;
  }).join('');
}

async function setDefaultProfile(name) {
  try {
    const r = await fetch(`/profiles/${encodeURIComponent(name)}/default`, { method: 'POST' });
    if (!r.ok) {
      const t = await r.text();
      alert(`set default failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('set default failed: ' + e.message);
    return;
  }
  renderProfiles();
}

async function clearDefaultProfile() {
  if (!confirm('Clear the default profile? Subsequent jobs without options.use_profile will run with the lane\'s stock profile.')) {
    return;
  }
  try {
    const r = await fetch('/profiles/default', { method: 'DELETE' });
    if (!r.ok) {
      const t = await r.text();
      alert(`clear default failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('clear default failed: ' + e.message);
    return;
  }
  renderProfiles();
}

async function deleteProfile(name) {
  if (!confirm(`Delete profile "${name}"?  Jobs already running with this profile won't be affected.`)) {
    return;
  }
  try {
    const r = await fetch(`/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (!r.ok) {
      const t = await r.text();
      alert(`delete failed: HTTP ${r.status}: ${t.slice(0, 200)}`);
      return;
    }
  } catch (e) {
    alert('delete failed: ' + e.message);
    return;
  }
  renderProfiles();
}

(function setupProfileUpload() {
  const drop = document.getElementById('profileUploadDrop');
  const file = document.getElementById('profileUploadFile');
  const nameRow = document.getElementById('profileUploadNameRow');
  const nameInput = document.getElementById('profileUploadName');
  const startBtn = document.getElementById('profileUploadStartBtn');
  const cancelBtn = document.getElementById('profileUploadCancelBtn');
  const progress = document.getElementById('profileUploadProgress');
  const refresh = document.getElementById('profilesRefreshBtn');
  if (!drop || !file) return;     // panel not in DOM yet

  let pendingBlob = null;
  let pendingFileName = '';

  function reset() {
    pendingBlob = null;
    pendingFileName = '';
    file.value = '';
    if (nameRow) nameRow.style.display = 'none';
    if (progress) {
      progress.style.display = 'none';
      progress.textContent = '';
    }
  }

  function deriveName(fileName) {
    // "mydefault.tar.gz" -> "mydefault"; "Default.tgz" -> "Default";
    // "User Data.zip" -> "User_Data" (the hub transcodes ZIP -> tar.gz
    // on upload, so we accept .zip too).
    return (fileName || '')
      .replace(/\.tar\.gz$/i, '')
      .replace(/\.tgz$/i, '')
      .replace(/\.zip$/i, '')
      .replace(/\.gz$/i, '')
      .replace(/[^A-Za-z0-9._\-]/g, '_')
      .slice(0, 64);
  }

  // Read the first 4 bytes of the file synchronously-feeling (we
  // await on a Promise) so we can fail-fast on the common mistake
  // of uploading a Windows-zipped folder (.zip renamed to .tar.gz)
  // instead of a real gzip. Catches it before bytes go out, saves
  // the operator a 400 round-trip.
  async function readMagic(file) {
    const slice = file.slice(0, 4);
    const buf = await slice.arrayBuffer();
    return new Uint8Array(buf);
  }

  // Magic-byte check. Returns null when the format is acceptable
  // (gzip or ZIP -- the hub transcodes ZIP -> tar.gz server-side),
  // otherwise returns a hint string for the alert.
  function magicHint(m) {
    if (m.length < 2) return { err: 'file is too small to be an archive' };
    if (m[0] === 0x1f && m[1] === 0x8b) return { kind: 'gzip' };
    if (m[0] === 0x50 && m[1] === 0x4b) return { kind: 'zip' };
    if (m[0] === 0x7b || m[0] === 0x5b || m[0] === 0x3c) {
      return { err: 'this looks like text (JSON/XML), not an archive.' };
    }
    const hex = Array.from(m).map(b => b.toString(16).padStart(2,'0')).join(' ');
    return {
      err: `first bytes ${hex} -- expected 1f 8b (gzip) or 50 4b (zip).`,
    };
  }

  async function acceptFile(f) {
    if (!f) return;
    if (f.size === 0) {
      alert('selected file is empty');
      return;
    }
    if (f.size > 500 * 1024 * 1024) {
      alert(`file is ${(f.size/1024/1024).toFixed(1)} MB but limit is 500 MB. `
            + `Raise PAPRIKA_PROFILE_MAX_BYTES on the hub or trim the snapshot.`);
      return;
    }
    // Magic-byte check: catch the wrong-archive-format mistake
    // BEFORE the upload kicks off. Hub-side transcodes ZIP to tar.gz
    // automatically so we accept both; only unknown formats are
    // rejected here. Saves the operator a 400 round-trip after a
    // 100 MB upload of garbage.
    let detectedKind = 'gzip';
    try {
      const m = await readMagic(f);
      const r = magicHint(m);
      if (r.err) {
        alert(`Cannot use this file: ${r.err}`);
        return;
      }
      detectedKind = r.kind;     // 'gzip' or 'zip'
    } catch (e) {
      alert('Could not read file: ' + e.message);
      return;
    }
    pendingBlob = f;
    pendingFileName = f.name || '';
    if (nameInput && !nameInput.value) {
      nameInput.value = deriveName(pendingFileName);
    }
    if (nameRow) nameRow.style.display = 'block';
    if (progress) {
      progress.style.display = 'block';
      const note = detectedKind === 'zip'
        ? '✓ ZIP (will transcode to tar.gz on upload)'
        : '✓ gzip';
      progress.textContent =
        `selected: ${pendingFileName} (${(f.size/1024/1024).toFixed(1)} MB) ${note}`;
    }
  }

  // Click anywhere on the drop zone (except buttons / inputs) to pick a file.
  drop.addEventListener('click', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'button' || tag === 'input' || tag === 'a') return;
    file.click();
  });

  file.addEventListener('change', () => {
    if (file.files && file.files[0]) acceptFile(file.files[0]);
  });

  // Drag & drop highlight + accept.
  ['dragenter', 'dragover'].forEach(evt => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.style.background = '#f0f4ff';
      drop.style.borderColor = '#9bf';
    });
  });
  ['dragleave', 'drop'].forEach(evt => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.style.background = '#fafafd';
      drop.style.borderColor = '#c8c8d4';
    });
  });
  drop.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
      acceptFile(e.dataTransfer.files[0]);
    }
  });

  if (cancelBtn) {
    cancelBtn.addEventListener('click', reset);
  }
  if (refresh) {
    refresh.addEventListener('click', renderProfiles);
  }

  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      if (!pendingBlob) {
        alert('select a tarball first');
        return;
      }
      const name = (nameInput && nameInput.value || '').trim();
      if (!/^[A-Za-z0-9._\-]{1,64}$/.test(name)) {
        alert('invalid name: use A-Z a-z 0-9 . _ - only (max 64 chars)');
        return;
      }
      startBtn.disabled = true;
      cancelBtn.disabled = true;
      if (progress) {
        progress.textContent = `uploading ${name} ...`;
        progress.style.display = 'block';
      }
      try {
        // Use XHR rather than fetch so we get upload progress events
        // without having to chunk by hand.
        await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open('POST', `/profiles/${encodeURIComponent(name)}`);
          xhr.setRequestHeader('Content-Type', 'application/gzip');
          xhr.setRequestHeader('X-Paprika-Source-Machine', navigator.userAgent.slice(0, 120));
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && progress) {
              const pct = (e.loaded / e.total * 100).toFixed(0);
              progress.textContent = `uploading ${name} ... ${pct}%`;
            }
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.responseText);
            else reject(new Error('HTTP ' + xhr.status + ': ' + xhr.responseText));
          };
          xhr.onerror = () => reject(new Error('network error'));
          xhr.send(pendingBlob);
        });
        if (progress) progress.textContent = `uploaded ${name} ✓`;
        reset();
        await renderProfiles();
      } catch (e) {
        if (progress) progress.textContent = 'upload failed: ' + e.message;
        alert('upload failed: ' + e.message);
      } finally {
        startBtn.disabled = false;
        cancelBtn.disabled = false;
      }
    });
  }
})();

// Refresh the table whenever the Profiles tab is activated.
document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'profiles') {
    btn.addEventListener('click', renderProfiles);
  }
});

// Kick off a first paint so the count badge is accurate on page load
// even if the operator hasn't clicked the tab yet.
renderProfiles();

// ---- codegen engine dropdown (Submit form) -----------------------------
// Populates the engine selector under the Goal textarea with every
// engine from /engines that can speak chat-completions (kind=chat
// or vision-chat AND protocol=openai). Empty selection -> hub uses
// the env-var default (CODEGEN_LLM_URL + CODEGEN_MODEL_NAME).
// Refreshed every time the operator switches TO the Submit tab so
// engines they just added in the Engines tab show up without a
// page reload.
async function populateCodegenEngineSelect() {
  const sel = document.getElementById('codegenEngineSelect');
  if (!sel) return;
  // Remember the current selection so we can restore it after the
  // options are rebuilt -- otherwise switching tabs wipes the
  // operator's pick.
  const prev = sel.value || '';
  let engines = [];
  try {
    const r = await fetch('/engines');
    if (r.ok) {
      const d = await r.json();
      engines = (d && d.engines) || [];
    }
  } catch (_) {
    // Silently fall back -- the placeholder option still works.
  }
  // Operator opt-in: only show engines explicitly flagged as
  // codegen-capable in the Engines admin tab. Old records without
  // the field fall back to the legacy rule via EngineRecord.from_json
  // (kind in chat/vision-chat AND protocol=openai), so existing
  // deployments keep their selector populated.
  const usable = engines.filter(e => !!e.use_for_codegen);
  // Sort: promoted first, then by slug.
  usable.sort((a, b) => {
    if (a.promoted !== b.promoted) return a.promoted ? -1 : 1;
    return (a.slug || '').localeCompare(b.slug || '');
  });
  // Rebuild the option list. Keep the placeholder as the first
  // entry so "(default — env)" stays selectable.
  const opts = ['<option value="">(default — env)</option>'];
  for (const e of usable) {
    const slug = (e.slug || '').replace(/[<>"&]/g, '');
    const name = (e.name || e.slug || '').replace(/[<>"&]/g, '');
    const model = (e.model || '').replace(/[<>"&]/g, '');
    const star = e.promoted ? ' ★' : '';
    const label = `${slug}${star}  (${name}${model && model !== name ? ' / ' + model : ''})`;
    opts.push(`<option value="${slug}">${label}</option>`);
  }
  sel.innerHTML = opts.join('');
  // Restore the previous selection if still present (= the engine
  // wasn't deleted between renders); otherwise fall through to the
  // default option.
  if (prev && [...sel.options].some(o => o.value === prev)) {
    sel.value = prev;
  }
}
// Refresh on Submit-tab activation.
document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'submit') {
    btn.addEventListener('click', populateCodegenEngineSelect);
  }
});
// Initial paint so the dropdown is populated even if the operator
// lands on Submit directly (which is the default tab).
populateCodegenEngineSelect();

// ---- visited URLs modal (per-host) ---------------------------------------

const VISITED_PAGE_SIZE = 100;
let _visitedState = { host: '', q: '', offset: 0, total: 0 };
let _visitedSearchTimer = null;

function _visitedModalEl() { return document.getElementById('visitedModal'); }

// In-memory editor state for the pattern table.
let _patternRows = [];  // array of {value: string, matches: number|null}
let _patternMatchTimer = null;

async function openVisitedModal(host) {
  _visitedState = { host: host, q: '', offset: 0, total: 0 };
  _patternRows = [];
  const hostEl = document.getElementById('visitedModalHost');
  const searchEl = document.getElementById('visitedModalSearch');
  if (hostEl) hostEl.textContent = host;
  if (searchEl) searchEl.value = '';
  const patErr = document.getElementById('recrawlPatternsErr');
  if (patErr) patErr.textContent = '';
  // Load patterns from the host record. If host has none, we render
  // the "(no patterns)" placeholder.
  try {
    const r = await fetch(HOST_ONE_URL(host));
    if (r.ok) {
      const rec = await r.json();
      _patternRows = (rec.recrawl_patterns || []).map(p => ({ value: p, matches: null }));
    }
  } catch (e) { /* best-effort */ }
  renderPatternsTable();
  // Fire match-counts so the UI shows numbers right away.
  scheduleMatchCounts();
  const m = _visitedModalEl();
  if (m) m.style.display = 'flex';
  await refreshVisitedList();
}

function _matchCellHtml(row) {
  if (row.matches === null || row.matches === undefined) {
    return '<small style="color:#aaa;">…</small>';
  }
  if (row.matches === 0 && row.value) {
    return '<small style="color:#a06000;" title="no visited URL matches -- typo?">0 ⚠</small>';
  }
  return '<strong>' + row.matches + '</strong>';
}

// Patch only the per-row "matches" cells without rebuilding the
// <input> elements. Critical for keeping focus + caret position
// while the operator types -- the previous implementation
// re-innerHTML'd the whole tbody on every match-count fetch,
// destroying the focused input.
function updateMatchCellsOnly() {
  const cells = document.querySelectorAll('#patternsTbody td.match-cell');
  cells.forEach((cell, idx) => {
    if (_patternRows[idx]) {
      cell.innerHTML = _matchCellHtml(_patternRows[idx]);
    }
  });
}

function renderPatternsTable() {
  const tb = document.getElementById('patternsTbody');
  if (!tb) return;
  if (_patternRows.length === 0) {
    tb.innerHTML = '<tr><td colspan=3 style="padding:8px; color:#888; text-align:center;">(no patterns — click ➕ add row, or use ➕ pattern on a visited URL)</td></tr>';
    return;
  }
  tb.innerHTML = _patternRows.map((row, idx) => {
    const v = esc(row.value || '');
    return `
      <tr>
        <td style="padding:3px 8px;"><input type="text" value="${v}" data-pat-idx="${idx}"
          style="width:100%; box-sizing:border-box; font-family:ui-monospace, Consolas, monospace; font-size:12.5px; padding:3px 6px;"
          placeholder="https://www.example.com/category/*"></td>
        <td class="match-cell" style="padding:3px 8px; text-align:right;">${_matchCellHtml(row)}</td>
        <td style="padding:3px 8px; text-align:center;">
          <button class="pill" style="padding:0 6px; background:#fee; border-color:#c88; color:#933;" onclick="removePatternRow(${idx})" title="remove row">🗑</button>
        </td>
      </tr>`;
  }).join('');
  // Wire input change for each row -- update model + schedule the
  // match-count fetch. We deliberately DO NOT call
  // renderPatternsTable() from this handler; only the matches cell
  // gets refreshed so the input keeps focus + caret position.
  tb.querySelectorAll('input[data-pat-idx]').forEach(inp => {
    inp.addEventListener('input', (e) => {
      const i = parseInt(e.target.dataset.patIdx, 10);
      if (_patternRows[i]) {
        _patternRows[i].value = e.target.value;
        _patternRows[i].matches = null;   // invalidate cached count
        updateMatchCellsOnly();           // shows "…" immediately
        scheduleMatchCounts();            // debounced refetch
      }
    });
  });
}

function addPatternRow(value) {
  _patternRows.push({ value: value || '', matches: null });
  renderPatternsTable();
  // Focus the new row's input
  setTimeout(() => {
    const inputs = document.querySelectorAll('#patternsTbody input[data-pat-idx]');
    const last = inputs[inputs.length - 1];
    if (last) last.focus();
  }, 0);
  if (value) scheduleMatchCounts();
}

function removePatternRow(idx) {
  if (idx < 0 || idx >= _patternRows.length) return;
  _patternRows.splice(idx, 1);
  renderPatternsTable();
}

function scheduleMatchCounts() {
  clearTimeout(_patternMatchTimer);
  _patternMatchTimer = setTimeout(refreshMatchCounts, 350);
}

async function refreshMatchCounts() {
  const host = _visitedState.host;
  if (!host || _patternRows.length === 0) return;
  const patterns = _patternRows.map(r => r.value || '');
  // Skip the call entirely when every row is blank.
  if (patterns.every(p => !p)) return;
  try {
    const r = await fetch(HOST_VISITED_URL(host) + '/match_counts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ patterns }),
    });
    if (!r.ok) return;
    const d = await r.json();
    const counts = d.counts || [];
    counts.forEach((n, i) => {
      if (_patternRows[i]) _patternRows[i].matches = n;
    });
    // Only touch the matches cells -- the input fields stay intact
    // so the operator's focus + caret position are preserved.
    updateMatchCellsOnly();
  } catch (e) { /* best-effort */ }
}

async function saveRecrawlPatterns() {
  const host = _visitedState.host;
  if (!host) return;
  const patErr = document.getElementById('recrawlPatternsErr');
  const savedHint = document.getElementById('recrawlPatternsSaved');
  if (patErr) patErr.textContent = '';
  // Collect non-empty patterns in display order, dedup.
  const seen = new Set();
  const patterns = [];
  for (const row of _patternRows) {
    const v = (row.value || '').trim();
    if (v && !seen.has(v)) {
      seen.add(v);
      patterns.push(v);
    }
  }
  // Need full body (cookies + notes) because PUT replaces them.
  let existing = {};
  try {
    const r = await fetch(HOST_ONE_URL(host));
    if (r.ok) existing = await r.json();
  } catch (e) {}
  const body = {
    cookies: existing.cookies || [],
    notes: existing.notes || null,
    recrawl_patterns: patterns,
  };
  try {
    const r = await fetch(HOST_ONE_URL(host), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (patErr) patErr.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (patErr) patErr.textContent = 'save failed: ' + e.message;
    return;
  }
  if (savedHint) {
    savedHint.style.opacity = '1';
    setTimeout(() => { savedHint.style.opacity = '0'; }, 1200);
  }
  // Refresh in-memory model from the just-saved server-side state
  // so the dedup'd / cleaned-up patterns are visible immediately.
  _patternRows = patterns.map(p => ({ value: p, matches: null }));
  renderPatternsTable();
  scheduleMatchCounts();
  // The Hosts row pattern count badge should refresh too.
  renderHosts();
}

// Used by the "➕ pattern" button on a visited URL row.
function promoteUrlToPattern(url) {
  if (!url) return;
  // Reject duplicates silently to avoid stuffing.
  const trimmed = url.trim();
  if (_patternRows.some(r => (r.value || '').trim() === trimmed)) {
    return;
  }
  _patternRows.push({ value: trimmed, matches: null });
  renderPatternsTable();
  scheduleMatchCounts();
}

function closeVisitedModal() {
  const m = _visitedModalEl();
  if (m) m.style.display = 'none';
}

async function refreshVisitedList() {
  const { host, q, offset } = _visitedState;
  if (!host) return;
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (offset) params.set('offset', offset);
  params.set('limit', VISITED_PAGE_SIZE);
  const listEl = document.getElementById('visitedModalList');
  const countEl = document.getElementById('visitedModalCount');
  if (listEl) listEl.innerHTML = '<div style="color:#888; padding:14px;">loading…</div>';
  let data;
  try {
    const r = await fetch(HOST_VISITED_URL(host) + '?' + params.toString());
    if (!r.ok) {
      if (listEl) listEl.innerHTML = '<div style="color:#a00; padding:14px;">load failed</div>';
      return;
    }
    data = await r.json();
  } catch (e) {
    if (listEl) listEl.innerHTML = '<div style="color:#a00; padding:14px;">' + esc(e.message) + '</div>';
    return;
  }
  _visitedState.total = data.total || 0;
  if (countEl) {
    if (q) countEl.textContent = `${data.total || 0} match (of full set)`;
    else countEl.textContent = `${data.total || 0} URL(s)`;
  }
  const urls = data.urls || [];
  if (urls.length === 0) {
    if (listEl) listEl.innerHTML = '<div style="color:#888; padding:14px;">'
      + (q ? 'no matches' : 'no visited URLs yet')
      + '</div>';
    renderVisitedPager(0, 0);
    return;
  }
  if (listEl) {
    listEl.innerHTML = urls.map(u => {
      const url = esc(u.url || '');
      const sha = esc(u.hash || '');
      // Pass the URL via a data attribute so we don't need to
      // worry about quote escaping in onclick="" handlers.
      return `
        <div style="display:flex; align-items:center; gap:8px; padding:3px 0; border-bottom:1px solid #f3f3f3;">
          <a href="${url}" target="_blank" style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#06a;">${url}</a>
          <button class="pill visited-promote" data-url="${url}" style="padding:0 6px; background:#eef8ee; border-color:#7ab68a; color:#196b2c; font-size:.78em;" title="この URL を recrawl pattern に追加"><iconify-icon icon="lucide:target"></iconify-icon> pattern</button>
          <button class="pill" style="padding:0 6px; background:#fee; border-color:#c88; color:#933; font-size:.78em;" title="remove from visited set" onclick="removeVisitedUrl('${sha}')">🗑</button>
        </div>`;
    }).join('');
    // Wire the per-row promote buttons.
    listEl.querySelectorAll('.visited-promote').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const url = e.currentTarget.dataset.url || '';
        promoteUrlToPattern(url);
      });
    });
  }
  renderVisitedPager(_visitedState.total, _visitedState.offset);
}

function renderVisitedPager(total, offset) {
  const el = document.getElementById('visitedModalPager');
  if (!el) return;
  if (total <= VISITED_PAGE_SIZE) {
    el.innerHTML = '';
    return;
  }
  const pageNo = Math.floor(offset / VISITED_PAGE_SIZE) + 1;
  const pageCount = Math.ceil(total / VISITED_PAGE_SIZE);
  const prevDisabled = offset <= 0 ? 'disabled' : '';
  const nextDisabled = (offset + VISITED_PAGE_SIZE) >= total ? 'disabled' : '';
  el.innerHTML = `
    <button class="pill" ${prevDisabled} onclick="visitedPagerJump(-1)">‹ prev</button>
    <span>page <strong>${pageNo}</strong> / ${pageCount}  (${total} total)</span>
    <button class="pill" ${nextDisabled} onclick="visitedPagerJump(+1)">next ›</button>
  `;
}

function visitedPagerJump(dir) {
  _visitedState.offset = Math.max(0, _visitedState.offset + dir * VISITED_PAGE_SIZE);
  refreshVisitedList();
}

async function removeVisitedUrl(sha) {
  if (!_visitedState.host || !sha) return;
  try {
    const r = await fetch(HOST_VISITED_URL(_visitedState.host) + '/' + encodeURIComponent(sha), { method: 'DELETE' });
    if (!r.ok) {
      alert('delete failed (' + r.status + ')');
      return;
    }
  } catch (e) { alert('delete failed: ' + e.message); return; }
  // Stay on the same page but refresh; if the current page is now
  // empty, step back one.
  if (_visitedState.total - 1 <= _visitedState.offset && _visitedState.offset > 0) {
    _visitedState.offset = Math.max(0, _visitedState.offset - VISITED_PAGE_SIZE);
  }
  refreshVisitedList();
  // Also refresh the hosts table so the visited count badge updates.
  renderHosts();
}

async function clearVisitedAll() {
  const host = _visitedState.host;
  if (!host) return;
  if (!confirm("Clear ALL visited URLs for host '" + host + "'?\n\nNext pap.walk() on this host will re-crawl from scratch (still respecting recrawl_patterns).")) return;
  try {
    const r = await fetch(HOST_VISITED_URL(host), { method: 'DELETE' });
    if (!r.ok) { alert('clear failed (' + r.status + ')'); return; }
  } catch (e) { alert('clear failed: ' + e.message); return; }
  _visitedState.offset = 0;
  refreshVisitedList();
  renderHosts();
}

(function wireVisitedModal() {
  const closeBtn = document.getElementById('visitedModalClose');
  const clearBtn = document.getElementById('visitedModalClear');
  const searchEl = document.getElementById('visitedModalSearch');
  const patSaveBtn = document.getElementById('recrawlPatternsSave');
  const patAddBtn = document.getElementById('patternsAddRow');
  if (closeBtn) closeBtn.addEventListener('click', closeVisitedModal);
  if (clearBtn) clearBtn.addEventListener('click', clearVisitedAll);
  if (patSaveBtn) patSaveBtn.addEventListener('click', saveRecrawlPatterns);
  if (patAddBtn) patAddBtn.addEventListener('click', () => addPatternRow(''));
  if (searchEl) {
    searchEl.addEventListener('input', () => {
      clearTimeout(_visitedSearchTimer);
      _visitedSearchTimer = setTimeout(() => {
        _visitedState.q = (searchEl.value || '').trim();
        _visitedState.offset = 0;
        refreshVisitedList();
      }, 200);
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const m = _visitedModalEl();
      if (m && m.style.display === 'flex') closeVisitedModal();
    }
  });
  const m = _visitedModalEl();
  if (m) {
    m.addEventListener('click', (e) => {
      if (e.target === m) closeVisitedModal();
    });
  }
})();

// Initial render so the tab badge has the right count even before
// the user clicks into it.
renderHosts();

// ---- presets (saved Submit-form snapshots) -------------------------------
//
// Paginated list. Designed to scale to 500+ presets without dragging
// the whole catalog across the wire on every render.

const PRESET_PAGE_SIZE = 50;
let _presetListState = { q: '', category: '', offset: 0, total: 0 };
let _presetSearchTimer = null;

// ---- Preset edit modal --------------------------------------------------
//
// Lets the operator change every field the Preset job tab cares about
// without round-tripping through "load -> Submit form -> overwrite".
// Sharable for both new-blank and load-existing flows; the openness
// of which fields are visible depends on the selected mode.
const _PRESET_EDIT_MODAL = { open: false, originalName: null, originalRecord: null };

function _presetEditModalSyncBlocks() {
  const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value || 'fetch';
  document.getElementById('presetEditModalCodegenBlock').style.display    = (mode === 'codegen-loop') ? 'flex' : 'none';
  document.getElementById('presetEditModalCodeBlock').style.display       = (mode === 'code') ? 'flex' : 'none';
  document.getElementById('presetEditModalRerunFromBlock').style.display  = (mode === 'rerun_from') ? 'flex' : 'none';
  document.getElementById('presetEditModalFetchNote').style.display       = (mode === 'fetch') ? 'block' : 'none';
}

async function _presetEditModalPopulateEngines() {
  const sel = document.getElementById('presetEditModalEngine');
  if (!sel) return;
  const prev = sel.value || '';
  let engines = [];
  try {
    const r = await fetch('/engines');
    if (r.ok) {
      const d = await r.json();
      engines = (d && d.engines) || [];
    }
  } catch (_) {}
  const usable = engines.filter(e =>
    (e.kind === 'chat' || e.kind === 'vision-chat') && e.protocol === 'openai'
  );
  usable.sort((a, b) => {
    if (a.promoted !== b.promoted) return a.promoted ? -1 : 1;
    return (a.slug || '').localeCompare(b.slug || '');
  });
  const opts = ['<option value="">(default — env)</option>'];
  for (const e of usable) {
    const slug = (e.slug || '').replace(/[<>"&]/g, '');
    const name = (e.name || e.slug || '').replace(/[<>"&]/g, '');
    const star = e.promoted ? ' ★' : '';
    opts.push(`<option value="${slug}">${slug}${star}  (${name})</option>`);
  }
  sel.innerHTML = opts.join('');
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
}

function _presetEditModalFetchCategories() {
  const dl = document.getElementById('presetEditModalCategoryList');
  if (!dl) return;
  fetch('/presets?limit=500').then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !Array.isArray(d.categories)) return;
    dl.innerHTML = d.categories
      .map(c => `<option value="${(c || '').replace(/"/g, '&quot;')}"></option>`)
      .join('');
  }).catch(() => {});
}

// Pick the mode radio value the modal should default to, given a
// loaded preset record. The record stores ui_mode (form-mode) and
// options.mode (the actual mode that runs); together they map to one
// of the modal's four radio choices.
function _presetEditModalInferMode(rec) {
  const ui = rec.ui_mode || 'fetch';
  const opts = rec.options || {};
  const oMode = opts.mode || '';
  if (oMode === 'codegen-loop') return 'codegen-loop';
  if (oMode === 'rerun') {
    if (opts.rerun_from) return 'rerun_from';
    return 'code';
  }
  if (oMode === 'fetch') return 'fetch';
  // Fallback by ui_mode.
  if (ui === 'ai') return 'codegen-loop';
  if (ui === 'code') return 'code';
  return 'fetch';
}

async function openPresetEditModal(presetName) {
  const modal = document.getElementById('presetEditModal');
  if (!modal) return;
  // Pull the full record. The /presets/{name} endpoint returns the
  // operator-set fields plus the captured options snapshot.
  let rec = {};
  try {
    const r = await fetch(PRESET_ONE_URL(presetName));
    if (!r.ok) { alert(`Load failed (HTTP ${r.status})`); return; }
    rec = await r.json();
  } catch (e) { alert(`Load failed: ${e}`); return; }

  _entityHashSync('presets', presetName);
  document.getElementById('presetEditModalTitle').textContent = `Edit preset: ${presetName}`;
  document.getElementById('presetEditModalName').value        = rec.name || presetName;
  document.getElementById('presetEditModalCategory').value    = rec.category || '';
  document.getElementById('presetEditModalDescription').value = rec.description || '';
  document.getElementById('presetEditModalUrl').value         = rec.url || '';
  document.getElementById('presetEditModalGoal').value        = rec.goal || (rec.options && rec.options.goal) || '';
  document.getElementById('presetEditModalCode').value        = rec.code_script || (rec.options && rec.options.code) || '';
  document.getElementById('presetEditModalMaxAttempts').value = rec.max_attempts || (rec.options && rec.options.max_codegen_attempts) || 3;
  const fopt = rec.options || {};
  document.getElementById('presetEditModalTimeoutCodegen').value = fopt.attempt_timeout_s || rec.attempt_timeout_s || 200;
  document.getElementById('presetEditModalTimeoutCode').value    = fopt.attempt_timeout_s || rec.attempt_timeout_s || 86400;
  document.getElementById('presetEditModalTimeoutRerun').value   = fopt.attempt_timeout_s || rec.attempt_timeout_s || 200;
  document.getElementById('presetEditModalRerunFromJob').value   = (fopt && fopt.rerun_from) || '';
  document.getElementById('presetEditModalHostDedup').checked    = (rec.host_dedup === undefined ? true : !!rec.host_dedup);
  document.getElementById('presetEditModalErr').textContent      = '';
  document.getElementById('presetEditModalRenameHint').style.display = 'none';

  // Mode radio
  const mode = _presetEditModalInferMode(rec);
  const modeRadio = document.querySelector(`input[name="presetEditModalMode"][value="${mode}"]`);
  if (modeRadio) modeRadio.checked = true;
  _presetEditModalSyncBlocks();

  // Populate engine select then set the value.
  await _presetEditModalPopulateEngines();
  const engineSel = document.getElementById('presetEditModalEngine');
  const wantEngine = (fopt && fopt.codegen_engine) || rec.codegen_engine || '';
  if (engineSel && [...engineSel.options].some(o => o.value === wantEngine)) {
    engineSel.value = wantEngine;
  } else if (engineSel) {
    engineSel.value = '';
  }

  _presetEditModalFetchCategories();
  _PRESET_EDIT_MODAL.open = true;
  _PRESET_EDIT_MODAL.originalName = presetName;
  // Stash the loaded record so we can preserve the fetch-options
  // sub-keys when the operator keeps the preset in fetch mode (the
  // modal intentionally doesn't surface those for editing here).
  _PRESET_EDIT_MODAL.originalRecord = rec;
  modal.style.display = 'flex';
}

function closePresetEditModal() {
  _entityHashClear('presets');
  const modal = document.getElementById('presetEditModal');
  if (modal) modal.style.display = 'none';
  _PRESET_EDIT_MODAL.open = false;
  _PRESET_EDIT_MODAL.originalName = null;
  _PRESET_EDIT_MODAL.originalRecord = null;
}

function _presetEditModalBuildPayload() {
  const name = (document.getElementById('presetEditModalName').value || '').trim();
  const category = (document.getElementById('presetEditModalCategory').value || '').trim();
  const description = (document.getElementById('presetEditModalDescription').value || '').trim();
  const url = (document.getElementById('presetEditModalUrl').value || '').trim();
  const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value || 'fetch';
  const goal = (document.getElementById('presetEditModalGoal').value || '').trim();
  const code = (document.getElementById('presetEditModalCode').value || '');
  const engine = document.getElementById('presetEditModalEngine').value || '';
  const maxAttempts = parseInt(document.getElementById('presetEditModalMaxAttempts').value, 10) || 3;
  const hostDedup = !!document.getElementById('presetEditModalHostDedup').checked;
  const rerunFromJob = (document.getElementById('presetEditModalRerunFromJob').value || '').trim();
  // Pick the right timeout field for the active mode.
  let timeout = 200;
  if (mode === 'codegen-loop') timeout = parseInt(document.getElementById('presetEditModalTimeoutCodegen').value, 10) || 200;
  else if (mode === 'code')        timeout = parseInt(document.getElementById('presetEditModalTimeoutCode').value, 10) || 86400;
  else if (mode === 'rerun_from')  timeout = parseInt(document.getElementById('presetEditModalTimeoutRerun').value, 10) || 200;

  let uiMode = mode;
  let aiEngine = 'codegen';
  let options = {};
  if (mode === 'fetch') {
    uiMode = 'fetch';
    // Carry over the existing fetch-options sub-keys (scroll /
    // play_videos / timing / referer / cookies_from / attach_to_job
    // …) from the loaded record so renaming or tweaking
    // category/description doesn't accidentally wipe them. Only
    // when there's no original (= fresh-blank edit) do we fall
    // back to bare {mode:'fetch'}.
    const prevOpts = (_PRESET_EDIT_MODAL.originalRecord && _PRESET_EDIT_MODAL.originalRecord.options) || null;
    if (prevOpts && (prevOpts.mode === 'fetch' || prevOpts.mode === undefined)) {
      options = Object.assign({}, prevOpts, { mode: 'fetch' });
    } else {
      options = { mode: 'fetch' };
    }
  } else if (mode === 'codegen-loop') {
    uiMode = 'ai';
    aiEngine = 'codegen';
    let g = goal || '';
    if (!hostDedup) {
      g += '\n\n追加ガードレール:\n  - **pap.walk(..., host_dedup=False)** を必ず指定する (既訪問URLも再クロール)';
    }
    options = {
      mode: 'codegen-loop',
      goal: g,
      max_codegen_attempts: maxAttempts,
      attempt_timeout_s: timeout,
    };
    if (engine) options.codegen_engine = engine;
  } else if (mode === 'code') {
    uiMode = 'code';
    aiEngine = 'code';
    options = {
      mode: 'rerun',
      code,
      attempt_timeout_s: timeout,
    };
  } else if (mode === 'rerun_from') {
    uiMode = 'code';
    aiEngine = 'code';
    options = {
      mode: 'rerun',
      rerun_from: rerunFromJob,
      attempt_timeout_s: timeout,
    };
  }
  return {
    name, category, description, url, goal,
    ui_mode: uiMode, ai_engine: aiEngine,
    code_script: code,
    max_attempts: maxAttempts,
    attempt_timeout_s: timeout,
    attempt_timeout_simple_s: 600,
    host_dedup: hostDedup,
    options,
  };
}

(function wirePresetEditModal() {
  const modal = document.getElementById('presetEditModal');
  if (!modal) return;
  // Mode radio change → toggle conditional blocks.
  document.querySelectorAll('input[name="presetEditModalMode"]').forEach(r => {
    r.addEventListener('change', _presetEditModalSyncBlocks);
  });
  // Rename hint shows when name diverges from the loaded record.
  const nameEl = document.getElementById('presetEditModalName');
  if (nameEl) {
    nameEl.addEventListener('input', () => {
      const hint = document.getElementById('presetEditModalRenameHint');
      if (!hint) return;
      const diverged = _PRESET_EDIT_MODAL.originalName
        && nameEl.value.trim() !== _PRESET_EDIT_MODAL.originalName;
      hint.style.display = diverged ? 'block' : 'none';
    });
  }
  const closeBtn  = document.getElementById('presetEditModalClose');
  const cancelBtn = document.getElementById('presetEditModalCancel');
  const saveBtn   = document.getElementById('presetEditModalSave');
  const delBtn    = document.getElementById('presetEditModalDelete');
  if (closeBtn)  closeBtn.addEventListener('click', closePresetEditModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closePresetEditModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closePresetEditModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _PRESET_EDIT_MODAL.open) closePresetEditModal();
  });
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      const errEl = document.getElementById('presetEditModalErr');
      const setErr = (m) => { if (errEl) errEl.textContent = m || ''; };
      setErr('');
      const payload = _presetEditModalBuildPayload();
      if (!payload.name) { setErr('Name は必須です'); return; }
      const mode = (document.querySelector('input[name="presetEditModalMode"]:checked') || {}).value;
      if (mode === 'rerun_from' && !(payload.options && payload.options.rerun_from)) {
        setErr('rerun_from モードでは Job ID が必須です'); return;
      }
      const oldName = _PRESET_EDIT_MODAL.originalName;
      const renaming = oldName && payload.name !== oldName;
      try {
        const r = await fetch(PRESET_ONE_URL(payload.name), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!r.ok) { setErr(`Save failed (HTTP ${r.status}): ${await r.text()}`); return; }
        // On rename, drop the old record after the new one was saved
        // successfully -- abort-and-leave-old beats abort-and-orphan.
        if (renaming) {
          try { await fetch(PRESET_ONE_URL(oldName), { method: 'DELETE' }); } catch (_) {}
        }
        closePresetEditModal();
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) {
        setErr(`Save failed: ${e}`);
      }
    });
  }
  if (delBtn) {
    delBtn.addEventListener('click', async () => {
      const oldName = _PRESET_EDIT_MODAL.originalName;
      if (!oldName) return;
      if (!confirm(`Delete preset "${oldName}"?`)) return;
      try {
        const r = await fetch(PRESET_ONE_URL(oldName), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) { alert(`Delete failed (HTTP ${r.status})`); return; }
        closePresetEditModal();
        if (typeof renderPresets === 'function') renderPresets();
      } catch (e) { alert(`Delete failed: ${e}`); }
    });
  }
})();

async function renderPresets() {
  const tbody    = document.querySelector('#presetsTable tbody');
  const cntBadge = document.getElementById('presetCount');
  const cntTab   = document.getElementById('cntPresets');
  const pagerHost = document.getElementById('presetsPager');
  const catSel    = document.getElementById('presetCategoryFilter');

  const params = new URLSearchParams();
  if (_presetListState.q)        params.set('q', _presetListState.q);
  if (_presetListState.category !== '') params.set('category', _presetListState.category);
  params.set('offset', _presetListState.offset);
  params.set('limit', PRESET_PAGE_SIZE);

  let payload = {};
  try {
    const r = await fetch(PRESET_LIST_URL + '?' + params.toString());
    if (r.ok) payload = await r.json();
  } catch (_) {}

  const presets    = payload.presets || [];
  const total      = payload.total   || 0;
  const categories = payload.categories || [];

  _presetListState.total = total;

  if (cntBadge) cntBadge.textContent = total;
  if (cntTab)   cntTab.textContent   = total;

  // Refresh the category filter dropdown without nuking the
  // operator's current selection.
  if (catSel) {
    const prev = catSel.value;
    let html = '<option value="">(all categories)</option>';
    html += `<option value="" disabled>──────────</option>`;
    for (const c of categories) {
      html += `<option value="${esc(c)}">${esc(c)}</option>`;
    }
    catSel.innerHTML = html;
    if (categories.includes(prev) || prev === '') catSel.value = prev;
  }

  if (!tbody) return;
  if (presets.length === 0) {
    const msg = total === 0
      ? 'no presets yet — save one from the Submit form'
      : 'no preset matches the current filter';
    tbody.innerHTML = `<tr><td colspan="7" style="padding:12px; color:#888; text-align:center;">${esc(msg)}</td></tr>`;
    if (pagerHost) pagerHost.innerHTML = '';
    return;
  }
  tbody.innerHTML = presets.map(p => {
    const modeBadge = p.ui_mode === 'ai'
      ? (p.ai_engine === 'simple' ? 'AI · simple' : 'AI · LLM')
      : p.ui_mode;
    return `
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:8px;"><strong>${esc(p.name)}</strong><div style="color:#888; font-size:.85em;">${esc(p.description || '')}</div></td>
      <td style="padding:8px;">${esc(p.category || '—')}</td>
      <td style="padding:8px;"><code>${esc(modeBadge)}</code></td>
      <td style="padding:8px; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(p.url)}"><a href="${esc(p.url)}" target="_blank">${esc(p.url)}</a></td>
      <td style="padding:8px; color:#888; font-size:.85em;">${esc((p.updated_at || '').slice(0, 16))}</td>
      <td style="padding:8px; color:#888; font-size:.85em;">${esc((p.last_used_at || '').slice(0, 16) || '—')}</td>
      <td>
        <div class="menu-wrap">
          <button class="action-btn" onclick="toggleMenu(this)" title="${tt('presets.th.actions','actions')}">${ICONS.moreV}</button>
          <div class="menu">
            <button class="preset-run-btn" data-name="${esc(p.name)}" title="このプリセットを実行 (POST /run)"><iconify-icon icon="lucide:play"></iconify-icon> run</button>
            <button class="preset-load-btn" data-name="${esc(p.name)}" title="Submit form に読み込む"><iconify-icon icon="lucide:download"></iconify-icon> load</button>
            <button class="preset-edit-btn" data-name="${esc(p.name)}" title="モーダルで編集 (rename / mode / goal / code …)"><iconify-icon icon="lucide:pencil"></iconify-icon> edit</button>
            <div class="divider"></div>
            <button class="danger preset-delete-btn" data-name="${esc(p.name)}" title="削除"><iconify-icon icon="lucide:trash-2"></iconify-icon> delete</button>
          </div>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Wire up per-row buttons.
  tbody.querySelectorAll('.preset-run-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      try {
        const r = await fetch(PRESET_ONE_URL(name) + '/run', { method: 'POST' });
        if (!r.ok) {
          const err = await r.text();
          alert(`Run failed (HTTP ${r.status}): ${err}`);
          return;
        }
        const job = await r.json();
        if (job && job.job_id) {
          // Attach the live panel so the operator can watch.
          if (typeof ljpAttach === 'function') ljpAttach(job.job_id);
          // Close the More menu so the user sees the live panel.
          const more = document.querySelector('#tabs .more.open');
          if (more) more.classList.remove('open');
        }
        renderPresets();
      } catch (e) { alert(`Run failed: ${e}`); }
    });
  });
  tbody.querySelectorAll('.preset-load-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      try {
        const r = await fetch(PRESET_ONE_URL(name));
        if (!r.ok) { alert(`Load failed (HTTP ${r.status})`); return; }
        const rec = await r.json();
        presetApplyToForm(rec);
        presetSetLoaded(name);
        // Switch to the Submit tab so the operator sees the loaded
        // form right away.
        const submitTab = document.querySelector('#tabs .tab[data-tab="submit"]');
        if (submitTab) submitTab.click();
        const urlInput = document.getElementById('urlInput');
        if (urlInput) urlInput.scrollIntoView({behavior: 'smooth', block: 'center'});
      } catch (e) { alert(`Load failed: ${e}`); }
    });
  });
  tbody.querySelectorAll('.preset-edit-btn').forEach(b => {
    b.addEventListener('click', () => {
      // Open the edit modal -- it handles its own fetch + populate
      // + save (including rename via PUT-new + DELETE-old) and
      // calls renderPresets() on close.
      if (typeof openPresetEditModal === 'function') {
        openPresetEditModal(b.dataset.name);
      }
    });
  });
  tbody.querySelectorAll('.preset-delete-btn').forEach(b => {
    b.addEventListener('click', async () => {
      const name = b.dataset.name;
      if (!confirm(`Delete preset "${name}"?`)) return;
      try {
        const r = await fetch(PRESET_ONE_URL(name), { method: 'DELETE' });
        if (!r.ok && r.status !== 404) {
          alert(`Delete failed (HTTP ${r.status})`);
          return;
        }
        renderPresets();
      } catch (e) { alert(`Delete failed: ${e}`); }
    });
  });

  // ---- pager --------------------------------------------------
  if (pagerHost) {
    const total = _presetListState.total;
    const offset = _presetListState.offset;
    const start = total ? offset + 1 : 0;
    const end   = Math.min(offset + PRESET_PAGE_SIZE, total);
    const prevDisabled = offset <= 0;
    const nextDisabled = offset + PRESET_PAGE_SIZE >= total;
    pagerHost.innerHTML = `
      <span style="color:#666;">${start}-${end} / ${total}</span>
      <button class="pill" id="presetPagerPrev" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${prevDisabled ? 'disabled' : ''}><iconify-icon icon="lucide:chevron-left"></iconify-icon> prev</button>
      <button class="pill" id="presetPagerNext" style="background:#f5f5fa; border-color:#bbc; color:#444;" ${nextDisabled ? 'disabled' : ''}>next <iconify-icon icon="lucide:chevron-right"></iconify-icon></button>
    `;
    const prevBtn = document.getElementById('presetPagerPrev');
    const nextBtn = document.getElementById('presetPagerNext');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      _presetListState.offset = Math.max(0, offset - PRESET_PAGE_SIZE);
      renderPresets();
    });
    if (nextBtn) nextBtn.addEventListener('click', () => {
      _presetListState.offset = offset + PRESET_PAGE_SIZE;
      renderPresets();
    });
  }
}

// Wire search + category-filter inputs (debounced) so typing
// a query doesn't fire a request on every keystroke.
(function wirePresetFilters() {
  const search = document.getElementById('presetSearch');
  const cat    = document.getElementById('presetCategoryFilter');
  if (search) {
    search.addEventListener('input', () => {
      _presetListState.q = search.value;
      _presetListState.offset = 0;
      clearTimeout(_presetSearchTimer);
      _presetSearchTimer = setTimeout(renderPresets, 200);
    });
  }
  if (cat) {
    cat.addEventListener('change', () => {
      _presetListState.category = cat.value;
      _presetListState.offset = 0;
      renderPresets();
    });
  }
})();

const refreshPresetsBtn = document.getElementById('refreshPresetsBtn');
if (refreshPresetsBtn) refreshPresetsBtn.addEventListener('click', renderPresets);

document.querySelectorAll('#tabs .tab').forEach(btn => {
  if (btn.dataset.tab === 'presets') btn.addEventListener('click', renderPresets);
});
renderPresets();


// ---- AI Engines panel ---------------------------------------------------
//
// Master-detail UI:
//   * left:  list of all engines (built-in first, then user-added)
//   * right: form for the currently-selected engine (or empty add form)
//
// State: ENGINES_STATE.records holds the latest list, .selectedSlug
// the current selection. Saves / deletes round-trip through the
// /engines REST endpoints then re-fetch the list.

const ENGINES_STATE = {
  records: [],
  selectedSlug: null,
  isNew: false,
};

async function loadEngines() {
  try {
    const r = await fetch('/engines');
    if (!r.ok) return;
    const j = await r.json();
    ENGINES_STATE.records = j.engines || [];
    const cnt = document.getElementById('engineCount');
    if (cnt) cnt.textContent = String(ENGINES_STATE.records.length);
    const tabCnt = document.getElementById('cntEngines');
    if (tabCnt) tabCnt.textContent = String(ENGINES_STATE.records.length);
  } catch (e) {
    console.error('loadEngines:', e);
  }
}

function renderEnginesList() {
  const host = document.getElementById('enginesList');
  if (!host) return;
  if (ENGINES_STATE.records.length === 0) {
    host.innerHTML = '<div style="color:#888; padding:12px; text-align:center;">(none)</div>';
    return;
  }
  const kindBadge = (k) => {
    const colors = {
      'chat': ['#eef0ff','#3a5ca8'],
      'vision-chat': ['#fef5e7','#8a5a00'],
      'gui-agent': ['#f5edff','#5a3b8a'],
    };
    const [bg, fg] = colors[k] || ['#eee','#666'];
    return `<span style="display:inline-block; padding:0 5px; border-radius:3px; font-size:.78em; background:${bg}; color:${fg};">${esc(k)}</span>`;
  };
  host.innerHTML = ENGINES_STATE.records.map(rec => {
    const isSel = rec.slug === ENGINES_STATE.selectedSlug;
    const bg = isSel ? '#fff4d4' : '';
    const promoted = rec.promoted ? ' <span title="promoted" style="color:#d4a13d;">●</span>' : '';
    // ``builtin`` is a historical marker from the now-removed auto-seed
    // and is no longer surfaced in the list (all engines are operator-
    // managed). The flag may still be ``true`` in legacy JSON on disk.
    return `<div class="engine-row" data-slug="${esc(rec.slug)}" style="padding:6px 8px; border-radius:4px; cursor:pointer; background:${bg};">
      <div style="font-weight:600; font-size:.92em;">${esc(rec.slug)}${promoted}</div>
      <div style="font-size:.78em; color:#666; margin-top:1px;">${kindBadge(rec.kind)} ${esc(rec.model || '')}</div>
    </div>`;
  }).join('');
  host.querySelectorAll('.engine-row').forEach(el => {
    el.addEventListener('click', () => {
      selectEngine(el.dataset.slug);
    });
  });
}

function selectEngine(slug) {
  ENGINES_STATE.selectedSlug = slug;
  ENGINES_STATE.isNew = false;
  const rec = ENGINES_STATE.records.find(r => r.slug === slug);
  if (!rec) return;
  fillEngineForm(rec);
  renderEnginesList();
}

function newEngineForm() {
  ENGINES_STATE.selectedSlug = null;
  ENGINES_STATE.isNew = true;
  fillEngineForm({
    slug: '', name: '', kind: 'chat', protocol: 'openai',
    endpoint: '', model: '', api_key_env: '', api_key_set: false,
    api_key_direct_set: false,
    headers: {}, timeout_s: 60, promoted: false, notes: '',
    builtin: false, created_at: '', updated_at: '',
  });
  renderEnginesList();
}

function fillEngineForm(rec) {
  const empty = document.getElementById('enginesDetailEmpty');
  const form = document.getElementById('enginesDetailForm');
  if (empty) empty.style.display = 'none';
  if (form) form.style.display = '';
  document.getElementById('engineSlug').value = rec.slug || '';
  document.getElementById('engineSlug').disabled = !ENGINES_STATE.isNew;
  document.getElementById('engineName').value = rec.name || '';
  document.getElementById('engineKind').value = rec.kind || 'chat';
  document.getElementById('engineKind').disabled = false;
  document.getElementById('engineProtocol').value = rec.protocol || 'openai';
  document.getElementById('engineProtocol').disabled = false;
  document.getElementById('engineEndpoint').value = rec.endpoint || '';
  document.getElementById('engineModel').value = rec.model || '';
  document.getElementById('engineApiKeyEnv').value = rec.api_key_env || '';
  // The direct key is intentionally never echoed back -- we just
  // surface whether one is stored. Leaving the password field empty
  // during save preserves the existing value (see api_key body
  // convention in upsert_engine).
  document.getElementById('engineApiKey').value = '';
  const directStatus = document.getElementById('engineApiKeyDirectStatus');
  if (directStatus) {
    if (rec.api_key_direct_set) {
      directStatus.textContent = '✓ direct key stored';
      directStatus.style.color = '#196b2c';
    } else {
      directStatus.textContent = '(none)';
      directStatus.style.color = '#888';
    }
  }
  const status = document.getElementById('engineApiKeyStatus');
  if (status) {
    if (!rec.api_key_env) {
      status.textContent = '(none)';
      status.style.color = '#888';
    } else if (rec.api_key_set) {
      status.textContent = '✓ env set on hub';
      status.style.color = '#196b2c';
    } else {
      status.textContent = '⚠ env not set on hub';
      status.style.color = '#c00';
    }
  }
  document.getElementById('engineTimeout').value = rec.timeout_s || 60;
  document.getElementById('engineHeaders').value = JSON.stringify(rec.headers || {}, null, 2);
  document.getElementById('enginePromoted').checked = !!rec.promoted;
  document.getElementById('engineUseForCodegen').checked = !!rec.use_for_codegen;
  // Daily quota (0 = unlimited). Empty input = treat as 0 for save.
  document.getElementById('engineDailyTokenBudget').value =
    (rec.daily_token_budget || 0) || '';
  document.getElementById('engineDailyRequestBudget').value =
    (rec.daily_request_budget || 0) || '';
  // Today's usage display. usage_today = {prompt, completion, requests}.
  const usage = rec.usage_today || { prompt: 0, completion: 0, requests: 0 };
  const usageEl = document.getElementById('engineUsageToday');
  if (usageEl) {
    const tt = (usage.prompt || 0) + (usage.completion || 0);
    const cap = rec.daily_token_budget || 0;
    const reqCap = rec.daily_request_budget || 0;
    const tokenLine = cap > 0
      ? `${tt.toLocaleString()} / ${cap.toLocaleString()} tokens (${Math.round(tt * 100 / cap)}%)`
      : `${tt.toLocaleString()} tokens (制限なし)`;
    const reqLine = reqCap > 0
      ? `${usage.requests} / ${reqCap} requests (${Math.round(usage.requests * 100 / reqCap)}%)`
      : `${usage.requests} requests`;
    usageEl.textContent = `今日の利用量: ${tokenLine} · ${reqLine}`;
    // Warn colour at >=90%, error colour when exceeded.
    if (cap > 0 && tt >= cap) usageEl.style.color = '#c00';
    else if (cap > 0 && tt >= cap * 0.9) usageEl.style.color = '#a06000';
    else usageEl.style.color = '#666';
  }
  document.getElementById('engineNotes').value = rec.notes || '';
  document.getElementById('engineDeleteBtn').disabled = false;
  document.getElementById('engineDeleteBtn').title = '削除';
  const meta = document.getElementById('engineMeta');
  if (meta) {
    if (ENGINES_STATE.isNew) {
      meta.textContent = '(new engine)';
    } else {
      meta.textContent =
        `created: ${rec.created_at || '(unknown)'}\n` +
        `updated: ${rec.updated_at || '(unknown)'}`;
    }
  }
  const stat = document.getElementById('engineStatus');
  if (stat) stat.textContent = '';
}

async function saveEngine() {
  const stat = document.getElementById('engineStatus');
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) { if (stat) stat.textContent = '❌ slug required'; return; }
  let headers = {};
  try {
    const raw = document.getElementById('engineHeaders').value.trim();
    if (raw) headers = JSON.parse(raw);
  } catch (e) {
    if (stat) stat.textContent = '❌ headers must be valid JSON';
    return;
  }
  const body = {
    name: document.getElementById('engineName').value.trim(),
    kind: document.getElementById('engineKind').value,
    protocol: document.getElementById('engineProtocol').value,
    endpoint: document.getElementById('engineEndpoint').value.trim(),
    model: document.getElementById('engineModel').value.trim(),
    api_key_env: document.getElementById('engineApiKeyEnv').value.trim(),
    headers,
    timeout_s: parseInt(document.getElementById('engineTimeout').value, 10) || 60,
    promoted: document.getElementById('enginePromoted').checked,
    use_for_codegen: document.getElementById('engineUseForCodegen').checked,
    daily_token_budget:
      parseInt(document.getElementById('engineDailyTokenBudget').value, 10) || 0,
    daily_request_budget:
      parseInt(document.getElementById('engineDailyRequestBudget').value, 10) || 0,
    notes: document.getElementById('engineNotes').value,
  };
  // Direct API key: only include in body if the user typed something
  // OR explicitly clicked Clear (which leaves a sentinel). Skipping
  // the key altogether means "keep current value" on the hub side.
  const directInput = document.getElementById('engineApiKey');
  const directVal = directInput.value;
  if (directInput.dataset.cleared === '1') {
    body.api_key = '';   // explicit wipe
  } else if (directVal) {
    body.api_key = directVal;
  }
  // Reset the cleared flag so next save doesn't re-wipe unintentionally.
  directInput.dataset.cleared = '';
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (stat) stat.textContent = `❌ ${r.status}: ${t.slice(0, 200)}`;
      return;
    }
    if (stat) stat.textContent = '✓ saved';
    ENGINES_STATE.isNew = false;
    ENGINES_STATE.selectedSlug = slug;
    await loadEngines();
    renderEnginesList();
    const rec = ENGINES_STATE.records.find(r => r.slug === slug);
    if (rec) fillEngineForm(rec);
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

async function deleteEngine() {
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) return;
  if (!confirm(`engine "${slug}" を削除しますか?`)) return;
  const stat = document.getElementById('engineStatus');
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug), {method: 'DELETE'});
    if (!r.ok) {
      const t = await r.text();
      if (stat) stat.textContent = `❌ ${r.status}: ${t.slice(0, 200)}`;
      return;
    }
    ENGINES_STATE.selectedSlug = null;
    ENGINES_STATE.isNew = false;
    await loadEngines();
    renderEnginesList();
    document.getElementById('enginesDetailEmpty').style.display = '';
    document.getElementById('enginesDetailForm').style.display = 'none';
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

async function testEngine() {
  const slug = document.getElementById('engineSlug').value.trim();
  if (!slug) return;
  const stat = document.getElementById('engineStatus');
  if (stat) stat.textContent = '⏳ testing...';
  try {
    const r = await fetch('/engines/' + encodeURIComponent(slug) + '/test', {method: 'POST'});
    const j = await r.json();
    if (j.ok) {
      if (stat) stat.textContent = `✓ reachable (${j.elapsed_ms}ms, HTTP ${j.status_code || 200})`;
    } else {
      if (stat) stat.textContent = `❌ ${j.error || 'failed'} (${j.elapsed_ms}ms)`;
    }
  } catch (e) {
    if (stat) stat.textContent = `❌ ${e.message}`;
  }
}

(function wireEngines() {
  const newBtn = document.getElementById('enginesNewBtn');
  const refreshBtn = document.getElementById('enginesRefreshBtn');
  const saveBtn = document.getElementById('engineSaveBtn');
  const delBtn = document.getElementById('engineDeleteBtn');
  const testBtn = document.getElementById('engineTestBtn');
  const clearKeyBtn = document.getElementById('engineApiKeyClearBtn');
  if (newBtn) newBtn.addEventListener('click', newEngineForm);
  if (refreshBtn) refreshBtn.addEventListener('click', async () => {
    await loadEngines();
    renderEnginesList();
  });
  if (saveBtn) saveBtn.addEventListener('click', saveEngine);
  if (delBtn) delBtn.addEventListener('click', deleteEngine);
  if (testBtn) testBtn.addEventListener('click', testEngine);
  if (clearKeyBtn) clearKeyBtn.addEventListener('click', () => {
    // Marks the direct-key field for explicit wipe on next save.
    // We don't fire the PUT immediately so the operator can pair it
    // with other edits and review before committing.
    const inp = document.getElementById('engineApiKey');
    inp.value = '';
    inp.dataset.cleared = '1';
    const ds = document.getElementById('engineApiKeyDirectStatus');
    if (ds) {
      ds.textContent = '(will be cleared on save)';
      ds.style.color = '#c00';
    }
  });
})();

// Initial load -- the count badge needs to be populated even before
// the operator opens the AI Engines tab.
loadEngines().then(() => renderEnginesList());

// ---- settings panel (UI defaults + hub toggles) --------------------------

const SETTINGS_URL = '/settings';
const UI_DEFAULTS_KEY = 'paprika.ui.defaults';

// Built-in fallback defaults. These match the values hardcoded in the
// Submit form's HTML, so resetting really means "go back to the
// default-default".
const UI_DEFAULTS_FALLBACK = {
  defaultMode: 'fetch',
  llmMaxAttempts: 3,
  llmAttemptTimeout: 86400,
  llmGoal: '',                // empty = use DEFAULT_CRAWL_GOAL
  llmHostDedup: true,
  codeTimeout: 180,
};

function loadUiDefaults() {
  try {
    const raw = localStorage.getItem(UI_DEFAULTS_KEY);
    if (!raw) return { ...UI_DEFAULTS_FALLBACK };
    const parsed = JSON.parse(raw);
    return { ...UI_DEFAULTS_FALLBACK, ...parsed };
  } catch (e) {
    return { ...UI_DEFAULTS_FALLBACK };
  }
}

function saveUiDefaults(values) {
  try { localStorage.setItem(UI_DEFAULTS_KEY, JSON.stringify(values)); }
  catch (e) {}
}

// Apply current UI defaults to the Submit form. Called at page load
// and right after the operator saves new defaults so the change is
// instantly visible (no reload needed).
function applyUiDefaultsToSubmit() {
  const v = loadUiDefaults();
  // Default mode -- update the radio + visuals. Migrate legacy "llm"
  // (pre-AI-tab-rename) to "ai" so operators with a saved default
  // don't end up with no mode selected.
  if (v.defaultMode === 'llm') v.defaultMode = 'ai';
  const radio = document.querySelector(`input[name="mode"][value="${v.defaultMode}"]`);
  if (radio) {
    radio.checked = true;
    if (typeof syncSubmitMode === 'function') syncSubmitMode();
  }
  const m = document.getElementById('maxAttempts');
  if (m) m.value = v.llmMaxAttempts;
  const t = document.getElementById('attemptTimeout');
  if (t) t.value = v.llmAttemptTimeout;
  const g = document.getElementById('goalInput');
  if (g && !g.value) g.value = v.llmGoal || '';  // don't clobber operator edits mid-session
  const ct = document.getElementById('codeTimeout');
  if (ct) ct.value = v.codeTimeout;
  const dd = document.getElementById('llmHostDedup');
  if (dd) dd.checked = !!v.llmHostDedup;
}

function flashSavedHint() {
  const h = document.getElementById('settingsSavedHint');
  if (!h) return;
  h.style.opacity = '1';
  setTimeout(() => { h.style.opacity = '0'; }, 1200);
}

async function loadSettingsPanel() {
  // (A) UI defaults from localStorage
  const v = loadUiDefaults();
  // Same legacy "llm" -> "ai" migration as in applyUiDefaultsToSubmit
  // so the Settings panel shows the correct value rather than dropping
  // through to the <select>'s first option.
  if (v.defaultMode === 'llm') v.defaultMode = 'ai';
  document.getElementById('setDefaultMode').value      = v.defaultMode;
  document.getElementById('setLlmMaxAttempts').value   = v.llmMaxAttempts;
  document.getElementById('setLlmTimeout').value       = v.llmAttemptTimeout;
  document.getElementById('setLlmHostDedup').checked   = !!v.llmHostDedup;
  document.getElementById('setCodeTimeout').value      = v.codeTimeout;
  document.getElementById('setLlmGoal').value          = v.llmGoal || '';
  // (B + C) Hub settings + system info from server
  try {
    const r = await fetch(SETTINGS_URL);
    if (r.ok) {
      const d = await r.json();
      const hub = d.values || {};
      document.getElementById('setSkillAutoExtract').checked      = !!hub.skill_auto_extract_enabled;
      document.getElementById('setConventionAutoExtract').checked = !!hub.convention_auto_extract_enabled;
      document.getElementById('setSkillTopK').value               = hub.skill_retrieval_top_k ?? 3;
      document.getElementById('setMinAssetSize').value            = hub.min_asset_size_bytes ?? 0;
      // Fetch defaults
      document.getElementById('setFetchWait').value         = hub.fetch_wait_seconds       ?? 20;
      document.getElementById('setFetchSettle').value       = hub.fetch_settle_seconds     ?? 0;
      document.getElementById('setFetchIdle').value         = hub.fetch_idle_seconds       ?? 3;
      document.getElementById('setFetchMaxWait').value      = hub.fetch_max_wait_seconds   ?? 60;
      document.getElementById('setFetchScroll').checked     = !!hub.fetch_scroll;
      document.getElementById('setFetchScrollStep').value   = hub.fetch_scroll_step        ?? 50;
      document.getElementById('setFetchScrollMax').value    = hub.fetch_scroll_max         ?? 3000;
      document.getElementById('setFetchScrollEarly').value  = hub.fetch_scroll_early_after ?? 5;
      document.getElementById('setFetchPostClick').value    = hub.fetch_post_click_seconds ?? 5;
      // Codegen web_search tool. ?? '' so an empty-but-present value
      // renders an empty field (= operator turned the tool off) rather
      // than the placeholder.
      const sxEl = document.getElementById('setSearxngUrl');
      if (sxEl) sxEl.value = hub.searxng_url ?? '';
      const sxTo = document.getElementById('setSearxngTimeout');
      if (sxTo) sxTo.value = hub.searxng_timeout_s ?? 15;
      const sxMc = document.getElementById('setWebSearchMaxCalls');
      if (sxMc) sxMc.value = hub.web_search_max_calls ?? 5;
      // SMB connection settings
      const _setVal = (id, v) => { const e = document.getElementById(id); if (e) e.value = v ?? ''; };
      _setVal('setSmbServer', hub.smb_server);
      _setVal('setSmbShare', hub.smb_share);
      _setVal('setSmbUsername', hub.smb_username);
      _setVal('setSmbPassword', hub.smb_password);
      _setVal('setSmbMountPoint', hub.smb_mount_point || '/mnt/paprika');
      _setVal('setSmbMountOptions', hub.smb_mount_options);
      // SMB status banner + disk usage
      const smbSt = d.smb_status || {};
      _updateSmbStatusBanner(smbSt.mounted, smbSt.mount_point, hub.smb_server, hub.smb_share);
      // Fetch disk usage asynchronously
      _refreshSmbDiskUsage();

      // ---- Reasoning Judge ----
      const rjMode = hub.reasoning_judge_mode || 'off';
      const rjEngine = hub.reasoning_judge_engine || '';
      const rjModeEl = document.getElementById('setReasoningJudgeMode');
      if (rjModeEl) rjModeEl.value = rjMode;
      // Populate engine dropdown from engines list
      _populateReasoningJudgeEngines(rjEngine);

      // ---- MariaDB ----
      _setVal('setMariadbHost', hub.mariadb_host);
      const _mdbPort = document.getElementById('setMariadbPort');
      if (_mdbPort) _mdbPort.value = hub.mariadb_port || 3306;
      _setVal('setMariadbDatabase', hub.mariadb_database || 'paprika');
      _setVal('setMariadbUsername', hub.mariadb_username);
      _setVal('setMariadbPassword', hub.mariadb_password);
      // MariaDB status banner
      const mdbSt = d.mariadb_status || {};
      _updateMariadbStatusBanner(mdbSt);

      // Show migration section if MariaDB host is configured
      const migSec = document.getElementById('mariadbMigrationSection');
      if (migSec) {
        if ((hub.mariadb_host || '').trim() && (hub.mariadb_username || '').trim()) {
          migSec.style.display = 'block';
          mdbRefreshTableCounts();
        } else {
          migSec.style.display = 'none';
        }
      }

      const sys = d.system || {};
      const tbody = document.getElementById('setSystemInfoBody');
      if (tbody) {
        const rows = [
          ['codegen LLM URL',        sys.codegen_llm_url],
          ['codegen model',          sys.codegen_model],
          ['skill distill LLM URL',  sys.skill_distill_llm_url],
          ['skill distill model',    sys.skill_distill_model],
          ['skill retrieval URL',    sys.skill_retrieval_llm_url],
          ['skill retrieval model',  sys.skill_retrieval_model],
          ['convention distill URL', sys.convention_distill_llm_url],
          ['convention distill model', sys.convention_distill_model],
          ['data dir',               sys.data_dir],
          ['storage dir',            sys.storage_dir],
          ['store',                  sys.store],
        ];
        tbody.innerHTML = rows.map(([k, v]) =>
          `<tr><td style="padding:3px 8px; color:#666; white-space:nowrap;">${esc(k)}</td><td style="padding:3px 8px;"><code>${esc(v || '')}</code></td></tr>`
        ).join('');
      }
    }
  } catch (e) {}
}

async function saveSettingsUi() {
  const v = {
    defaultMode:      document.getElementById('setDefaultMode').value,
    llmMaxAttempts:   parseInt(document.getElementById('setLlmMaxAttempts').value, 10) || 3,
    llmAttemptTimeout:parseInt(document.getElementById('setLlmTimeout').value, 10) || 86400,
    llmHostDedup:     document.getElementById('setLlmHostDedup').checked,
    codeTimeout:      parseInt(document.getElementById('setCodeTimeout').value, 10) || 180,
    llmGoal:          document.getElementById('setLlmGoal').value,
  };
  saveUiDefaults(v);
  // Mirror to the Submit form right away.
  applyUiDefaultsToSubmit();
  flashSavedHint();
}

function resetSettingsUi() {
  saveUiDefaults({ ...UI_DEFAULTS_FALLBACK });
  loadSettingsPanel();
  applyUiDefaultsToSubmit();
  flashSavedHint();
}

async function saveSettingsHub() {
  const errEl = document.getElementById('setHubErr');
  if (errEl) errEl.textContent = '';
  const body = {
    skill_auto_extract_enabled:      document.getElementById('setSkillAutoExtract').checked,
    convention_auto_extract_enabled: document.getElementById('setConventionAutoExtract').checked,
    skill_retrieval_top_k:           parseInt(document.getElementById('setSkillTopK').value, 10) || 3,
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function saveSettingsAssetCapture() {
  const errEl = document.getElementById('setAssetErr');
  if (errEl) errEl.textContent = '';
  const raw = parseHumanBytes(document.getElementById('setMinAssetSize').value);
  const v = (Number.isFinite(raw) && raw >= 0) ? raw : 0;
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ min_asset_size_bytes: v }),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

function _num(id, fallback) {
  const raw = document.getElementById(id).value;
  const n = parseFloat(raw);
  if (isNaN(n) || n < 0) return fallback;
  return n;
}

async function saveSettingsFetchDefaults() {
  const errEl = document.getElementById('setFetchErr');
  if (errEl) errEl.textContent = '';
  const body = {
    fetch_wait_seconds:        Math.round(_num('setFetchWait', 20)),
    fetch_settle_seconds:      _num('setFetchSettle', 0),
    fetch_idle_seconds:        _num('setFetchIdle', 3),
    fetch_max_wait_seconds:    _num('setFetchMaxWait', 60),
    fetch_scroll:              document.getElementById('setFetchScroll').checked,
    fetch_scroll_step:         Math.round(_num('setFetchScrollStep', 50)),
    fetch_scroll_max:          Math.round(_num('setFetchScrollMax', 3000)),
    fetch_scroll_early_after:  _num('setFetchScrollEarly', 5),
    fetch_post_click_seconds:  _num('setFetchPostClick', 5),
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function saveSettingsWebSearch() {
  // Persist the SearXNG endpoint, timeout, and per-attempt call cap
  // that drive the Coder LLM's web_search tool. Empty URL or 0 calls
  // disables the tool (see server/hub/web_search.is_enabled).
  const errEl = document.getElementById('setWebSearchErr');
  if (errEl) errEl.textContent = '';
  const urlRaw  = (document.getElementById('setSearxngUrl').value || '').trim();
  const timeRaw = parseFloat(document.getElementById('setSearxngTimeout').value);
  const callRaw = parseInt(document.getElementById('setWebSearchMaxCalls').value, 10);
  const body = {
    searxng_url:           urlRaw,
    searxng_timeout_s:     (Number.isFinite(timeRaw) && timeRaw > 0) ? timeRaw : 15,
    web_search_max_calls:  (Number.isFinite(callRaw) && callRaw >= 0) ? callRaw : 5,
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

// ---- SMB Storage helpers ----

function _updateSmbStatusBanner(mounted, mountPoint, server, share) {
  const banner = document.getElementById('smbStatusBanner');
  if (!banner) return;
  if (!server && !share) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (mounted) {
    banner.style.background = '#e6f7e9';
    banner.style.border = '1px solid #7ab68a';
    banner.style.color = '#196b2c';
    banner.innerHTML = '<iconify-icon icon="lucide:check-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>接続中</strong>: //' + esc(server || '') + '/' + esc(share || '')
      + ' → <code>' + esc(mountPoint || '') + '</code>';
  } else {
    banner.style.background = '#fff3e0';
    banner.style.border = '1px solid #e8c97a';
    banner.style.color = '#7a5a14';
    banner.innerHTML = '<iconify-icon icon="lucide:alert-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>未接続</strong>: //' + esc(server || '') + '/' + esc(share || '')
      + ' (マウントボタンで接続)';
  }
}

function _updateMariadbStatusBanner(st) {
  const banner = document.getElementById('mariadbStatusBanner');
  if (!banner) return;
  if (!st || (!st.connected && !st.host)) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (st.connected) {
    banner.style.background = '#e6f7e9';
    banner.style.border = '1px solid #7ab68a';
    banner.style.color = '#196b2c';
    const storeLabel = st.store_kind === 'mariadb' ? 'プライマリストア' : 'メタデータのみ';
    banner.innerHTML = '<iconify-icon icon="lucide:check-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>接続中</strong>: '
      + esc(st.host || '') + ':' + (st.port || 3306) + '/' + esc(st.database || '')
      + ' <span style="margin-left:8px; padding:2px 8px; border-radius:4px; background:#d4edda; font-size:.85em;">'
      + esc(st.version || '') + '</span>'
      + ' <span style="margin-left:6px; padding:2px 8px; border-radius:4px; background:#cce5ff; color:#004085; font-size:.85em;">'
      + esc(storeLabel) + '</span>';
  } else {
    banner.style.background = '#fff3e0';
    banner.style.border = '1px solid #e8c97a';
    banner.style.color = '#7a5a14';
    banner.innerHTML = '<iconify-icon icon="lucide:alert-circle" style="font-size:1.2em;"></iconify-icon>'
      + ' <strong>未接続</strong>: MariaDB に接続できません。Redis / ファイルで動作中。';
  }
}

async function _refreshSmbDiskUsage() {
  const el = document.getElementById('smbDiskUsage');
  if (!el) return;
  try {
    const r = await fetch('/settings/smb/status');
    if (!r.ok) { el.textContent = ''; return; }
    const d = await r.json();
    if (d.mounted && d.usage) {
      // Prefer the server's unit-scaled strings (TB / GB / ...) so a
      // multi-TB NAS isn't shown as a huge GB number. Fall back to the
      // legacy *_gb fields for older hubs.
      const u = d.usage;
      const used  = u.used_h  || (u.used_gb  + ' GB');
      const total = u.total_h || (u.total_gb + ' GB');
      const free  = u.free_h  || (u.free_gb  + ' GB');
      // Surface that the watchdog will auto-reconnect a dropped mount.
      const auto = d.auto_mount
        ? ' <span style="color:#888; font-weight:normal;">· 自動再接続 ON</span>'
        : ' <span style="color:#c0792a; font-weight:normal;">· 自動再接続 OFF</span>';
      el.innerHTML = '<iconify-icon icon="lucide:database"></iconify-icon> '
        + '使用量: ' + used + ' / ' + total
        + ' (空き ' + free + ')' + auto;
      el.style.color = '#196b2c';
    } else if (d.mounted) {
      el.textContent = 'マウント済み (使用量取得不可)';
      el.style.color = '#888';
    } else {
      el.textContent = '';
    }
  } catch (e) {
    el.textContent = '';
  }
}

async function saveSettingsSmb() {
  const errEl = document.getElementById('setSmbErr');
  if (errEl) errEl.textContent = '';
  const body = {
    smb_server:        (document.getElementById('setSmbServer').value || '').trim(),
    smb_share:         (document.getElementById('setSmbShare').value || '').trim(),
    smb_username:      (document.getElementById('setSmbUsername').value || '').trim(),
    smb_password:      document.getElementById('setSmbPassword').value || '',
    smb_mount_point:   (document.getElementById('setSmbMountPoint').value || '/mnt/paprika').trim(),
    smb_mount_options: (document.getElementById('setSmbMountOptions').value || '').trim(),
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      if (errEl) errEl.textContent = 'save failed (' + r.status + '): ' + t.slice(0, 200);
      return;
    }
  } catch (e) {
    if (errEl) errEl.textContent = 'save failed: ' + e.message;
    return;
  }
  flashSavedHint();
}

async function smbMount() {
  const errEl = document.getElementById('setSmbErr');
  if (errEl) errEl.textContent = '';
  // Save settings first, then mount
  await saveSettingsSmb();
  const mountBtn = document.getElementById('setSmbMountBtn');
  if (mountBtn) { mountBtn.disabled = true; mountBtn.textContent = 'マウント中…'; }
  try {
    const r = await fetch('/settings/smb/mount', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) {
      if (errEl) errEl.textContent = d.detail || 'mount failed';
      return;
    }
    flashSavedHint();
    loadSettingsPanel();
  } catch (e) {
    if (errEl) errEl.textContent = 'mount failed: ' + e.message;
  } finally {
    if (mountBtn) { mountBtn.disabled = false; mountBtn.innerHTML = '<iconify-icon icon="lucide:plug"></iconify-icon> マウント'; }
  }
}

async function smbUnmount() {
  const errEl = document.getElementById('setSmbErr');
  if (errEl) errEl.textContent = '';
  try {
    const r = await fetch('/settings/smb/unmount', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) {
      if (errEl) errEl.textContent = d.detail || 'unmount failed';
      return;
    }
    flashSavedHint();
    loadSettingsPanel();
  } catch (e) {
    if (errEl) errEl.textContent = 'unmount failed: ' + e.message;
  }
}

async function resetFetchDefaults() {
  // Push the in-code defaults back to the server.
  const body = {
    fetch_wait_seconds: 20,
    fetch_settle_seconds: 0,
    fetch_idle_seconds: 3,
    fetch_max_wait_seconds: 60,
    fetch_scroll: false,
    fetch_scroll_step: 50,
    fetch_scroll_max: 3000,
    fetch_scroll_early_after: 5,
    fetch_post_click_seconds: 5,
  };
  try {
    await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) {}
  loadSettingsPanel();
  flashSavedHint();
}

// ---- Reasoning Judge ----
async function _populateReasoningJudgeEngines(currentSlug) {
  const sel = document.getElementById('setReasoningJudgeEngine');
  if (!sel) return;
  // Keep the first option (fallback)
  sel.innerHTML = '<option value="">(未設定 — env fallback)</option>';
  try {
    const r = await fetch('/engines');
    const data = await r.json();
    const engines = Array.isArray(data) ? data : (data.engines || []);
    engines.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.slug;
      opt.textContent = `${e.slug} (${e.model || e.name})`;
      if (e.slug === currentSlug) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (_) {}
}

async function saveSettingsReasoningJudge() {
  const statusEl = document.getElementById('setReasoningJudgeStatus');
  const mode = (document.getElementById('setReasoningJudgeMode')?.value || 'off').trim();
  const engine = (document.getElementById('setReasoningJudgeEngine')?.value || '').trim();
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        reasoning_judge_mode: mode,
        reasoning_judge_engine: engine,
      }),
    });
    if (r.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = '保存しました'; setTimeout(() => statusEl.textContent = '', 3000); }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `エラー: ${r.status}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = String(e); }
  }
}

// ---- MariaDB ----
async function saveSettingsMariadb() {
  const statusEl = document.getElementById('setMariadbStatus');
  if (statusEl) statusEl.textContent = '';
  const body = {
    mariadb_host: (document.getElementById('setMariadbHost')?.value || '').trim(),
    mariadb_port: parseInt(document.getElementById('setMariadbPort')?.value, 10) || 3306,
    mariadb_database: (document.getElementById('setMariadbDatabase')?.value || 'paprika').trim(),
    mariadb_username: (document.getElementById('setMariadbUsername')?.value || '').trim(),
    mariadb_password: document.getElementById('setMariadbPassword')?.value || '',
  };
  try {
    const r = await fetch(SETTINGS_URL, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = '保存しました'; setTimeout(() => statusEl.textContent = '', 3000); }
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `エラー: ${r.status}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = String(e); }
  }
}

async function testMariadbConnection() {
  const statusEl = document.getElementById('setMariadbStatus');
  const btn = document.getElementById('setMariadbTestBtn');
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> テスト中…';
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  const body = {
    host: (document.getElementById('setMariadbHost')?.value || '').trim(),
    port: parseInt(document.getElementById('setMariadbPort')?.value, 10) || 3306,
    database: (document.getElementById('setMariadbDatabase')?.value || 'paprika').trim(),
    username: (document.getElementById('setMariadbUsername')?.value || '').trim(),
    password: document.getElementById('setMariadbPassword')?.value || '',
  };
  try {
    const r = await fetch('/settings/mariadb/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    let d;
    try { d = await r.json(); } catch { d = { message: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = `✓ ${d.message} (${d.version})`; }
      // Show migration section on successful connection
      const migSec = document.getElementById('mariadbMigrationSection');
      if (migSec) migSec.style.display = 'block';
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.message}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = '接続失敗: ' + e.message; }
  } finally {
    if (btn) btn.innerHTML = origLabel;
  }
}

// ---- MariaDB Data Migration ----

async function mdbCreateSchema() {
  const statusEl = document.getElementById('mdbSchemaStatus');
  const btn = document.getElementById('mdbSchemaBtn');
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> 作成中…'; }
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  try {
    const r = await fetch('/settings/mariadb/schema', { method: 'POST' });
    let d;
    try { d = await r.json(); } catch { d = { detail: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) { statusEl.style.color = '#196b2c'; statusEl.textContent = `✓ ${d.tables.length} テーブル作成済み`; }
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.detail || d.message || 'エラー'}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = 'エラー: ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origLabel; }
  }
}

async function mdbMigrate(category) {
  // Map category -> status span suffix (capitalised first letter)
  const capMap = { jobs: 'Jobs', hosts: 'Hosts', visited_urls: 'Visited', skills: 'Skills', conventions: 'Conventions', engines: 'Engines', presets: 'Presets' };
  const cap = capMap[category] || category;
  const statusEl = document.getElementById('mdbMigrate' + cap + 'Status');
  const btnMap = { jobs: 'mdbMigrateJobsBtn', hosts: 'mdbMigrateHostsBtn', visited_urls: 'mdbMigrateVisitedBtn', skills: 'mdbMigrateSkillsBtn', conventions: 'mdbMigrateConventionsBtn', engines: 'mdbMigrateEnginesBtn', presets: 'mdbMigratePresetsBtn' };
  const btn = document.getElementById(btnMap[category]);
  const origLabel = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<iconify-icon icon="lucide:loader-2" class="spin"></iconify-icon> 移行中…'; }
  if (statusEl) { statusEl.style.color = '#888'; statusEl.textContent = ''; }
  try {
    const r = await fetch('/settings/mariadb/migrate/' + category, { method: 'POST' });
    let d;
    try { d = await r.json(); } catch { d = { detail: await r.text().catch(() => r.statusText) }; }
    if (d.ok) {
      if (statusEl) {
        statusEl.style.color = '#196b2c';
        statusEl.textContent = `✓ ${d.migrated} 件移行 / ${d.skipped} 件スキップ (全 ${d.total || d.total_hosts || '?'})`;
        if (d.purged > 0) {
          statusEl.textContent += ` / 元データ ${d.purged} 件削除`;
        }
        if (d.errors && d.errors.length > 0) {
          statusEl.textContent += ` ⚠ ${d.errors.length} 件エラー`;
        }
      }
      mdbRefreshTableCounts();
    } else {
      if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = `✗ ${d.detail || d.message || 'エラー'}`; }
    }
  } catch (e) {
    if (statusEl) { statusEl.style.color = '#a00'; statusEl.textContent = 'エラー: ' + e.message; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = origLabel; }
  }
}

async function mdbRefreshTableCounts() {
  const el = document.getElementById('mdbTableCounts');
  if (!el) return;
  try {
    const r = await fetch('/settings/mariadb/tables');
    let d;
    try { d = await r.json(); } catch { d = {}; }
    if (d.ok && d.tables) {
      const rows = Object.entries(d.tables).map(([name, count]) => {
        const label = count < 0 ? '<span style="color:#a00">未作成</span>' : count.toLocaleString() + ' 行';
        return `<span style="margin-right:14px;"><b>${name}</b>: ${label}</span>`;
      }).join('');
      el.innerHTML = '📊 ' + rows;
      el.style.display = 'block';
    }
  } catch (e) { /* ignore */ }
}

(function wireSettings() {
  const saveUi = document.getElementById('setSaveUiBtn');
  const resetUi = document.getElementById('setResetUiBtn');
  const saveHub = document.getElementById('setSaveHubBtn');
  const saveAsset = document.getElementById('setSaveAssetBtn');
  const saveFetch = document.getElementById('setSaveFetchBtn');
  const resetFetch = document.getElementById('setResetFetchBtn');
  if (saveUi) saveUi.addEventListener('click', saveSettingsUi);
  if (resetUi) resetUi.addEventListener('click', resetSettingsUi);
  if (saveHub) saveHub.addEventListener('click', saveSettingsHub);
  if (saveAsset) saveAsset.addEventListener('click', saveSettingsAssetCapture);
  if (saveFetch) saveFetch.addEventListener('click', saveSettingsFetchDefaults);
  if (resetFetch) resetFetch.addEventListener('click', resetFetchDefaults);
  const saveWs = document.getElementById('setSaveWebSearchBtn');
  if (saveWs) saveWs.addEventListener('click', saveSettingsWebSearch);
  const saveSmb = document.getElementById('setSaveSmbBtn');
  if (saveSmb) saveSmb.addEventListener('click', saveSettingsSmb);
  const mountSmb = document.getElementById('setSmbMountBtn');
  if (mountSmb) mountSmb.addEventListener('click', smbMount);
  const unmountSmb = document.getElementById('setSmbUnmountBtn');
  if (unmountSmb) unmountSmb.addEventListener('click', smbUnmount);
  // Reasoning Judge
  const saveRj = document.getElementById('setSaveReasoningJudgeBtn');
  if (saveRj) saveRj.addEventListener('click', saveSettingsReasoningJudge);
  // MariaDB
  const saveMdb = document.getElementById('setSaveMariadbBtn');
  if (saveMdb) saveMdb.addEventListener('click', saveSettingsMariadb);
  const testMdb = document.getElementById('setMariadbTestBtn');
  if (testMdb) testMdb.addEventListener('click', testMariadbConnection);
  // MariaDB Migration
  const mdbSchema = document.getElementById('mdbSchemaBtn');
  if (mdbSchema) mdbSchema.addEventListener('click', mdbCreateSchema);
  const mdbJobs = document.getElementById('mdbMigrateJobsBtn');
  if (mdbJobs) mdbJobs.addEventListener('click', () => mdbMigrate('jobs'));
  const mdbHosts = document.getElementById('mdbMigrateHostsBtn');
  if (mdbHosts) mdbHosts.addEventListener('click', () => mdbMigrate('hosts'));
  const mdbVisited = document.getElementById('mdbMigrateVisitedBtn');
  if (mdbVisited) mdbVisited.addEventListener('click', () => mdbMigrate('visited_urls'));
  const mdbSkills = document.getElementById('mdbMigrateSkillsBtn');
  if (mdbSkills) mdbSkills.addEventListener('click', () => mdbMigrate('skills'));
  const mdbConventions = document.getElementById('mdbMigrateConventionsBtn');
  if (mdbConventions) mdbConventions.addEventListener('click', () => mdbMigrate('conventions'));
  const mdbEngines = document.getElementById('mdbMigrateEnginesBtn');
  if (mdbEngines) mdbEngines.addEventListener('click', () => mdbMigrate('engines'));
  const mdbPresets = document.getElementById('mdbMigratePresetsBtn');
  if (mdbPresets) mdbPresets.addEventListener('click', () => mdbMigrate('presets'));
  // MariaDB password toggle
  const mdbPwToggle = document.getElementById('setMariadbPasswordToggle');
  if (mdbPwToggle) mdbPwToggle.addEventListener('click', () => {
    const pw = document.getElementById('setMariadbPassword');
    if (pw) pw.type = pw.type === 'password' ? 'text' : 'password';
  });
  // Password toggle
  const pwToggle = document.getElementById('setSmbPasswordToggle');
  if (pwToggle) pwToggle.addEventListener('click', () => {
    const pw = document.getElementById('setSmbPassword');
    if (pw) pw.type = pw.type === 'password' ? 'text' : 'password';
  });
  // Reload the panel each time the Settings tab is activated so the
  // hub-side info stays fresh.
  document.querySelectorAll('#tabs .tab').forEach(btn => {
    if (btn.dataset.tab === 'settings') {
      btn.addEventListener('click', loadSettingsPanel);
    }
  });
})();

// Apply UI defaults to the Submit form at page load. Defer so the
// form elements and syncSubmitMode are already wired.
setTimeout(() => {
  try { applyUiDefaultsToSubmit(); } catch (e) {}
}, 0);

// ---- Submit URL → host shortcuts ----------------------------------------
// When the operator types a URL into the Submit form, derive the host
// and offer one-click access to that host's Edit (cookies / notes)
// and Dedup (visited URLs / recrawl patterns) modals. Counts are
// pulled live from /hosts/{host} (404 = new host).

let _urlHostInfoTimer = null;
let _urlHostCurrent = '';

function _normaliseHostJs(raw) {
  if (!raw) return '';
  let h = raw.toLowerCase().trim();
  if (h.startsWith('www.')) h = h.substring(4);
  return h;
}

function _extractHostFromUrl(raw) {
  if (!raw) return '';
  const s = raw.trim();
  if (!s) return '';
  // Accept bare hosts like "javdock.com" too.
  try {
    const candidate = /^https?:\/\//i.test(s) ? s : 'https://' + s;
    const u = new URL(candidate);
    return _normaliseHostJs(u.hostname);
  } catch (e) {
    return '';
  }
}

async function refreshUrlHostInfo() {
  const urlInput = document.getElementById('urlInput');
  const row = document.getElementById('urlHostInfo');
  const nameEl = document.getElementById('urlHostName');
  const editCnt = document.getElementById('urlHostEditCount');
  const dedupCnt = document.getElementById('urlHostDedupCount');
  const statusEl = document.getElementById('urlHostStatus');
  if (!urlInput || !row) return;
  const host = _extractHostFromUrl(urlInput.value);
  _urlHostCurrent = host;
  if (!host) {
    row.style.display = 'none';
    return;
  }
  row.style.display = 'flex';
  nameEl.textContent = host;
  // Clear counts immediately so stale info doesn't linger.
  editCnt.textContent = '';
  dedupCnt.textContent = '';
  statusEl.textContent = 'loading…';
  try {
    const r = await fetch('/hosts/' + encodeURIComponent(host));
    // Race-safety: the operator may have typed more characters since
    // we issued this fetch -- only paint if we're still showing the
    // same host.
    if (host !== _urlHostCurrent) return;
    if (r.ok) {
      const rec = await r.json();
      const cookieCnt = (rec.cookies || []).length;
      const visitedCnt = rec.visited_count || 0;
      const patternCnt = (rec.recrawl_patterns || []).length;
      editCnt.textContent  = cookieCnt > 0 ? '(' + cookieCnt + ' cookies)' : '';
      let dedupLabel = '';
      if (visitedCnt > 0 || patternCnt > 0) {
        const parts = [];
        if (visitedCnt > 0) parts.push(visitedCnt + ' visited');
        if (patternCnt > 0) parts.push(patternCnt + ' patterns');
        dedupLabel = '(' + parts.join(', ') + ')';
      }
      dedupCnt.textContent = dedupLabel;
      statusEl.textContent = '✓ registered';
      statusEl.style.color = '#196b2c';
    } else if (r.status === 404) {
      statusEl.textContent = '(未登録)';
      statusEl.style.color = '#888';
    } else {
      statusEl.textContent = 'load failed (' + r.status + ')';
      statusEl.style.color = '#a00';
    }
  } catch (e) {
    statusEl.textContent = 'load failed';
    statusEl.style.color = '#a00';
  }
}

function scheduleUrlHostInfo() {
  clearTimeout(_urlHostInfoTimer);
  _urlHostInfoTimer = setTimeout(refreshUrlHostInfo, 250);
}

(function wireUrlHostShortcuts() {
  const urlInput = document.getElementById('urlInput');
  if (urlInput) urlInput.addEventListener('input', scheduleUrlHostInfo);
  const editBtn = document.getElementById('urlHostEditBtn');
  if (editBtn) editBtn.addEventListener('click', () => {
    if (_urlHostCurrent) openHostModal(_urlHostCurrent);
  });
  const dedupBtn = document.getElementById('urlHostDedupBtn');
  if (dedupBtn) dedupBtn.addEventListener('click', () => {
    if (_urlHostCurrent) openVisitedModal(_urlHostCurrent);
  });
  // Refresh once at page load so a pre-filled URL (browser autofill /
  // form restoration) already shows the buttons.
  setTimeout(() => {
    try { refreshUrlHostInfo(); } catch (e) {}
  }, 50);
})();

// Reflect host registry edits back into the URL-host info row. Wrap
// the existing renderHosts() so any code path that bumps the Host
// table also bumps our shortcut row.
const _orig_renderHosts_for_url_info = (typeof renderHosts === 'function') ? renderHosts : null;
if (_orig_renderHosts_for_url_info) {
  renderHosts = async function() {
    const result = await _orig_renderHosts_for_url_info.apply(this, arguments);
    try { refreshUrlHostInfo(); } catch (e) {}
    return result;
  };
}
