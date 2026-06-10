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
    "tab.ai":          "AI 学習機能",
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
    "workers.th.cpu":      "CPU",
    "workers.th.mem":      "MEM",
    "workers.th.disk":     "DISK",
    "workers.th.profiles": "プロファイル",
    "workers.th.version":  "バージョン",
    "workers.th.labels":   "ラベル",
    "workers.th.actions":  "操作",
    "workers.empty":       "接続中のワーカーなし",
    // Workers panel sub-tabs (Workers / Hubs / Features)
    "workers.subtab.workers":  "ワーカー",
    "workers.subtab.hubs":     "ハブ",
    "workers.subtab.features": "機能",
    // Hubs sub-tab
    "hubs.heading":         "ハブ",
    "hubs.refresh":         "再読み込み",
    "hubs.refresh.title":   "ハブ一覧を再取得",
    "hubs.intro":           "共有 Redis を介して各ハブが自分を 30 秒ごとに登録します。TTL は 90 秒なので、停止したハブは ~1 分でオフラインになります。別ホストで hub-b 等を起動すると、同じ Redis に向けるだけで自動的にこの一覧に出現します (設定不要)。",
    "hubs.th.id":           "hub_id",
    "hubs.th.status":       "ステータス",
    "hubs.th.public_base":  "public_base",
    "hubs.th.version":      "バージョン",
    "hubs.th.last_seen":    "最終ハートビート",
    "hubs.th.actions":      "操作",
    "hubs.empty":           "登録されているハブなし",
    // Features sub-tab
    "features.heading":          "機能",
    "features.intro":            "これまで scripts/deploy.sh を SSH 経由で叩いていたオペレーションを admin UI から実行できるようにする場所。",
    "features.restart.title":    "このハブを再起動",
    "features.restart.btn":      "このハブを再起動",
    "features.restart.help":     "ハブの Python プロセスを os._exit(42) で落とし、docker の restart: unless-stopped ポリシーで再起動させます。ホストの ./server bind-mount に反映済みの変更 (= git pull 後) はこの再起動でピックアップされます。10〜20 秒で接続が戻ります。",
    // Jobs panel
    "jobs.cleanup":      "古いジョブを削除…",
    "jobs.deleteall":    "すべて削除",
    "jobs.cols":         "列",
    "jobs.tab.all":      "全部",
    "jobs.tab.success":  "成功",
    "jobs.tab.error":    "エラー",
    "jobs.tab.running":  "実行中",
    "jobs.tab.review":   "課題",
    "jobs.loading":      "読み込み中…",
    "jobs.th.id":        "ID",
    "jobs.th.mode":      "モード",
    "jobs.th.status":    "ステータス",
    "jobs.th.worker":    "ワーカー/レーン",
    "jobs.th.started":   "開始",
    "jobs.th.ended":     "終了",
    "jobs.th.duration":  "所要",
    "jobs.th.actions":   "操作",
    "jobs.empty":        "ジョブなし",
    "jobs.loading":      "読み込み中…",
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
    // 3-card layout: Fetch / Script (= Code + Macro) / AI (= codegen-loop + vision-agent).
    "submit.mode.script":      "Script",
    "submit.mode.script.desc": "スクリプトを手で組んで実行 (Code 直書き / Macro UI ビルダー)。LLM 不使用。",
    "submit.mode.ai":          "AI",
    "submit.mode.ai.desc":     "Goal を LLM に渡してスクリプト自動生成 → sandbox 実行 → 失敗時 retry。",
    // Legacy keys (Script / AI に統合される前の名前) -- 旧テンプレや
    // 旧 docs から参照される可能性があるので暫定的に残す。
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
    "ljp.vnc.screenshot.title":"スクリーンショット撮影",
    "ljp.vnc.fit":            "fit",
    "ljp.vnc.fit.title":      "Chrome のウィンドウサイズを現在の zoom 設定に再同期する",
    "ljp.vnc.open":           "open",
    "ljp.vnc.open.title":     "新しいタブで開く",
    "ljp.tab.log":            "ログ",
    "ljp.tab.screenshot":     "スクリーンショット",
    "ljp.tab.links":          "リンク",
    "ljp.tab.code":           "コード",
    "ljp.tab.gallery":        "ギャラリー",
    "ljp.tab.runconfig":      "実行",
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
    "knowledge.subtitle":           "— v2 がホストごとに学習した結果（障壁 / 抽出 / 統計）",
    "knowledge.search.placeholder": "ホストで絞り込み",
    "knowledge.tier.all":           "全 tier",
    "knowledge.tier.high":          "高信頼",
    "knowledge.tier.medium":        "中信頼",
    "knowledge.tier.low":           "低信頼",
    "knowledge.tier.stale":         "古い",
    "knowledge.refresh":            "再読み込み",
    "knowledge.tile.hosts":         " ホスト",
    "knowledge.tile.high":          " 高信頼",
    "knowledge.tile.medium":        " 中信頼",
    "knowledge.tile.low":           " 低信頼",
    "knowledge.tile.stale":         " 古い",
    "knowledge.tile.barriers":      " 障壁を学習済",
    "knowledge.tile.extractions":   " 抽出を学習済",
    "knowledge.ai.heading":         "AI インサイト",
    "knowledge.ai.sub":             "影判定 ＆ 推論 AI による蒸留",
    "knowledge.ai.rawapi":          "raw API",
    "knowledge.ai.paired":          "Judge 判定ペア数",
    "knowledge.ai.paired.sub":      "従来 vs 推論 AI シャドウ",
    "knowledge.ai.paired.agree":    "一致",
    "knowledge.ai.paired.disagree": "不一致",
    "knowledge.ai.paired.disabled": "PAPRIKA_R1_JUDGE_MODE=shadow を有効化してください",
    "knowledge.ai.agree":           "一致率",
    "knowledge.ai.agree.sub":       "高いほど推論 AI への切替が安全",
    "knowledge.ai.distilled":       "推論 AI 蒸留の最近の更新",
    "knowledge.ai.distilled.sub":   "全ホスト合計",
    "knowledge.ai.distilled.recent": "直近 24 時間以内",
    "knowledge.ai.distilled.none":  "直近 24 時間以内の更新なし",
    "knowledge.ai.r1hosts":         "推論 AI が学習に貢献したホスト",
    "knowledge.ai.r1hosts.sub":     "provenance: distiller-r1",
    "knowledge.th.host":            "ホスト",
    "knowledge.th.tier":            "信頼度",
    "knowledge.th.jobs":            "ジョブ数",
    "knowledge.th.success":         "成功率",
    "knowledge.th.barriers":        "障壁",
    "knowledge.th.extractions":     "抽出",
    "knowledge.th.updated":         "最終更新",
    "knowledge.th.by":              "更新元",
    "knowledge.empty":              "HostKnowledge はまだありません — ジョブを実行すると学習が始まります",
    "knowledge.badge.r1":           "推論AI",
    "knowledge.badge.r1.title":     "推論 AI が直近更新",
    "knowledge.pager.size":         "表示件数",
    // AI tab common (sub-tabs, ticker, GPU gauge)
    "ai.subtab.knowledge":          "ホスト知識",
    "ai.subtab.skills":             "スキル",
    "ai.subtab.conventions":        "作法",
    "ai.subtab.grooming":           "整理",
    "ai.subtab.oracle":             "オラクル",
    "ai.ticker.label":              "最新の 推論 AI 学習",
    "ai.ticker.loading":            "読み込み中…",
    "ai.ticker.empty":              "推論 AI の更新はまだありません",
    "ai.ticker.what.update":        "更新",
    "ai.gpu.title":                 "hub-side ビジョン LLM の同時推論数 / ピーク / 累計",
    "ai.gpu.busy":                  "稼働中",
    "ai.gpu.idle":                  "アイドル",
    "ai.gpu.na":                    "不明",
    "ai.gpu.active":                "実行中",
    "ai.gpu.peak":                  "ピーク",
    "ai.gpu.total":                 "累計",
    "ai.reason.label":              "推論 AI",
    "ai.reason.distill":            "蒸留",
    "ai.reason.judge":              "判定",
    "ai.reason.loading":            "推論 AI: 読込中…",
    "ai.reason.na":                 "推論 AI: 未設定",
    "ai.reason.title":              "現在の推論 AI バックエンド (PAPRIKA_R1_DISTILLER_ENGINE)",
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
    "plugins.install":             "インストール",
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
    // Operation recorder (oprec) -- demo-recorder panel under #submit
    "oprec.title":            "操作デモ記録 (実験)",
    "oprec.subtitle":         "noVNC で手動操作した内容をキャプチャ → 後で AI 解析の few-shot に使う",
    "oprec.start_url":        "開始 URL:",
    "oprec.worker":           "ワーカー (空 = 自動、要 v0.4.0):",
    "oprec.worker.hint":      "未指定だと最も古い Chrome v0.3.1 拡張のワーカーに当たることがあります",
    "oprec.start_btn":        "記録開始 (新セッション)",
    "oprec.open_novnc":       "noVNC を開く",
    "oprec.stop_btn":         "停止 & 結果表示",
    "oprec.howto":            "noVNC を開いて手動でクリック/入力すると裏で記録されます。終わったら「停止」を押すと結果が下に出ます。",
    "oprec.result":           "結果",
    "oprec.save_demo":        "デモとして保存",
    "oprec.clips":            "画面クロップ",
    "oprec.saved":            "保存済みデモ",
    "oprec.filter_host":      "host で絞り込み:",
    "oprec.refresh":          "更新",
    "oprec.save_modal_title": "デモとして保存",
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
    "tab.ai":          "AI Learning",
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
    // Workers panel sub-tabs
    "workers.subtab.workers":  "Workers",
    "workers.subtab.hubs":     "Hubs",
    "workers.subtab.features": "Features",
    // Hubs sub-tab
    "hubs.heading":         "Hubs",
    "hubs.refresh":         "Refresh",
    "hubs.refresh.title":   "Reload hubs list",
    "hubs.intro":           "Each hub heartbeats itself into shared Redis every 30 s (TTL 90 s). Stopped hubs go offline within ~1 minute. Starting a new hub (hub-b, etc.) on another host with the same Redis auto-registers it here -- no config needed.",
    "hubs.th.id":           "hub_id",
    "hubs.th.status":       "status",
    "hubs.th.public_base":  "public_base",
    "hubs.th.version":      "version",
    "hubs.th.last_seen":    "last seen",
    "hubs.th.actions":      "actions",
    "hubs.empty":           "no hubs registered",
    // Features sub-tab
    "features.heading":          "Features",
    "features.intro":            "Admin operations that used to need scripts/deploy.sh + SSH, exposed in the admin UI.",
    "features.restart.title":    "Restart this hub",
    "features.restart.btn":      "Restart this hub",
    "features.restart.help":     "Exits the hub Python process with code 42; docker's restart policy brings it back up on the latest bind-mounted code (so `git pull` + this button = apply update without SSH). 10-20 s of downtime.",
    "workers.th.id":       "worker_id",
    "workers.th.address":  "address",
    "workers.th.status":   "status",
    "workers.th.load":     "load",
    "workers.th.cpu":      "CPU",
    "workers.th.mem":      "MEM",
    "workers.th.disk":     "DISK",
    "workers.th.profiles": "profiles",
    "workers.th.version":  "version",
    "workers.th.labels":   "labels",
    "workers.th.actions":  "actions",
    "workers.empty":       "no workers connected",
    // Jobs panel
    "jobs.cleanup":      "cleanup old…",
    "jobs.deleteall":    "delete all",
    "jobs.cols":         "columns",
    "jobs.tab.all":      "all",
    "jobs.tab.success":  "success",
    "jobs.tab.error":    "errors",
    "jobs.tab.running":  "running",
    "jobs.tab.review":   "review",
    "jobs.loading":      "loading…",
    "jobs.th.id":        "id",
    "jobs.th.mode":      "mode",
    "jobs.th.status":    "status",
    "jobs.th.worker":    "worker/lane",
    "jobs.th.started":   "started",
    "jobs.th.ended":     "ended",
    "jobs.th.duration":  "duration",
    "jobs.th.actions":   "actions",
    "jobs.empty":        "no jobs yet",
    "jobs.loading":      "Loading…",
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
    "submit.mode.script":      "Script",
    "submit.mode.script.desc": "Write a script by hand (Code paste / Macro UI builder). No LLM.",
    "submit.mode.ai":          "AI",
    "submit.mode.ai.desc":     "Give a Goal to the LLM → auto-generate script → sandbox run → retry on failure.",
    // Legacy keys, kept for old templates / docs that may still reference them.
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
    "ljp.vnc.screenshot.title":"Take screenshot",
    "ljp.vnc.fit":            "fit",
    "ljp.vnc.fit.title":      "Re-sync Chrome window size to the current zoom",
    "ljp.vnc.open":           "open",
    "ljp.vnc.open.title":     "Open in a new tab",
    "ljp.tab.log":            "Log",
    "ljp.tab.screenshot":     "Screenshot",
    "ljp.tab.links":          "Links",
    "ljp.tab.code":           "Code",
    "ljp.tab.gallery":        "Gallery",
    "ljp.tab.runconfig":      "Run",
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
    "knowledge.tier.high":          "high",
    "knowledge.tier.medium":        "medium",
    "knowledge.tier.low":           "low",
    "knowledge.tier.stale":         "stale",
    "knowledge.refresh":            "refresh",
    "knowledge.tile.hosts":         " hosts",
    "knowledge.tile.high":          " high",
    "knowledge.tile.medium":        " medium",
    "knowledge.tile.low":           " low",
    "knowledge.tile.stale":         " stale",
    "knowledge.tile.barriers":      " barriers learned",
    "knowledge.tile.extractions":   " extractions learned",
    "knowledge.ai.heading":         "AI Insights",
    "knowledge.ai.sub":             "shadow judge & Reasoning AI distiller",
    "knowledge.ai.rawapi":          "raw API",
    "knowledge.ai.paired":          "Judge verdicts paired",
    "knowledge.ai.paired.sub":      "legacy vs Reasoning AI shadow",
    "knowledge.ai.paired.agree":    "agree",
    "knowledge.ai.paired.disagree": "disagree",
    "knowledge.ai.paired.disabled": "enable PAPRIKA_R1_JUDGE_MODE=shadow",
    "knowledge.ai.agree":           "Agreement rate",
    "knowledge.ai.agree.sub":       "higher = Reasoning AI ready to promote",
    "knowledge.ai.distilled":       "Recent Reasoning AI distiller updates",
    "knowledge.ai.distilled.sub":   "across all hosts",
    "knowledge.ai.distilled.recent": "in last 24h",
    "knowledge.ai.distilled.none":  "no updates in last 24h",
    "knowledge.ai.r1hosts":         "Hosts with Reasoning-AI-learned data",
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
    "knowledge.badge.r1":           "Reasoning",
    "knowledge.badge.r1.title":     "Reasoning AI updated this most recently",
    "knowledge.pager.size":         "rows per page",
    // AI tab common
    "ai.subtab.knowledge":          "Host Knowledge",
    "ai.subtab.skills":             "Skills",
    "ai.subtab.conventions":        "Conventions",
    "ai.subtab.grooming":           "Grooming",
    "ai.subtab.oracle":             "Oracle",
    "ai.ticker.label":              "Latest Reasoning AI learning",
    "ai.ticker.loading":            "loading…",
    "ai.ticker.empty":              "No Reasoning AI updates yet",
    "ai.ticker.what.update":        "update",
    "ai.gpu.title":                 "hub-side vision LLM concurrency / peak / total",
    "ai.gpu.busy":                  "busy",
    "ai.gpu.idle":                  "idle",
    "ai.gpu.na":                    "n/a",
    "ai.gpu.active":                "active",
    "ai.gpu.peak":                  "peak",
    "ai.gpu.total":                 "total",
    "ai.reason.label":              "Reasoning AI",
    "ai.reason.distill":            "distill",
    "ai.reason.judge":              "judge",
    "ai.reason.loading":            "Reasoning AI: loading…",
    "ai.reason.na":                 "Reasoning AI: not configured",
    "ai.reason.title":              "Current Reasoning AI backend (PAPRIKA_R1_DISTILLER_ENGINE)",
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
    "plugins.install":             "install",
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
    // Operation recorder (oprec) -- demo-recorder panel under #submit
    "oprec.title":            "Operation demo recorder (experimental)",
    "oprec.subtitle":         "Capture what you do manually over noVNC → reuse it later as few-shot examples for AI analysis",
    "oprec.start_url":        "Start URL:",
    "oprec.worker":           "Worker (empty = auto, requires v0.4.0):",
    "oprec.worker.hint":      "If left empty you may hit a worker running the oldest Chrome v0.3.1 extension",
    "oprec.start_btn":        "Start recording (new session)",
    "oprec.open_novnc":       "Open noVNC",
    "oprec.stop_btn":         "Stop & show results",
    "oprec.howto":            "Open noVNC and click/type manually — it is recorded in the background. When you are done, press \"Stop\" and the results appear below.",
    "oprec.result":           "Results",
    "oprec.save_demo":        "Save as demo",
    "oprec.clips":            "Screen crops",
    "oprec.saved":            "Saved demos",
    "oprec.filter_host":      "Filter by host:",
    "oprec.refresh":          "Refresh",
    "oprec.save_modal_title": "Save as demo",
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
  // Settings tab: load hub settings + MariaDB/S3 status on activation
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
  // #ai/<kind>/<slug> -> open a skill/host-knowledge entity on the AI tab.
  // entityId carries the FULL "<kind>/<slug>" (everything after "#ai/"),
  // since _parseHash's "(.+)" grabs the rest incl. slashes. Dispatch lives
  // in admin-knowledge.js (window.aiOpenEntity) where the openers + the
  // host-knowledge list are in scope. typeof-guarded so load order is moot.
  ai:          (id) => { if (typeof aiOpenEntity === 'function') aiOpenEntity(id); },
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

