---
layout: doc
title: なぜ Paprika? — Playwright との違い
description: Playwright スタイルの API で書きやすく、分散ワーカー上の Chrome を AI とともに動かして画像・動画・リンクを集める。Paprika と Playwright・Selenium の違いを、シナリオ別に比較。
active: why-paprika
---

<div class="tldr">
<span class="tldr-label">概要</span>
<p><strong>Playwright スタイル API + 分散フリート + AI</strong> を 1 つに統合した、<strong>収集ワークロード</strong>向けプラットフォーム。複数ホストの Chrome を Hub が束ねて並列に動かし、未知サイトは LLM がスクリプトを生成、画像/動画はブラウザが読み込んだバイト列をそのまま回収します。</p>
</div>

<video class="shot" width="1096" height="664" autoplay loop muted playsinline preload="metadata" aria-label="Paprika 管理画面で複数の Chrome Lane が同時にページを取得している様子">
  <source src="img/admin-live.webm" type="video/webm">
  <source src="img/admin-live.mp4" type="video/mp4">
  <img src="img/admin-live.gif" alt="Paprika 管理画面で複数の Chrome Lane が同時にページを取得している様子" loading="lazy">
</video>
<p class="shot-cap">分散ワーカー上の Chrome が並列でページを取得している様子（管理画面 Live プレビュー）。</p>

> 採用判断のためのページです。導入は [Server インストール](quickstart.html)、SDK の使い方は [Client インストール](intro.html)、内部構造は [アーキテクチャ概要](architecture.html) を参照。

## 5 つの差分

Paprika が Playwright / Selenium と本質的に違うところは 5 つです。

- **分散フリート** — 複数ホストの Chrome（**Lane**）を Hub が束ね、ジョブを WebSocket でディスパッチ。1 台でも 100 台でも同じ API で書ける。
- **AI コード生成（`codegen-loop`）** — URL と自然言語の `goal` だけ渡せば、LLM がスクリプトを生成・実行・失敗時に再生成。成功したスクリプトは `mode: rerun` で次回から決定的に再利用。
- **二度取りしない passive 回収** — ブラウザが実際に読み込んだレスポンスを CDP の `Network.responseReceived` で横取り。`<img>` URL から再 GET しないので、帯域半分、認証/Referer 必須の画像も、JS で差し込まれた lazy-load・`background-image`・iframe 内も全部拾える。
- **収集に最適化** — `yt-dlp` + 通信トレース統合で動画も画像と同じ感覚で取得、スクロール・最小サイズ・遅延ロード対策が既定機能。管理画面・ライブ noVNC が標準付属。
- **普段使い Chrome の環境ごと持ち込み** — `--load-extension` で既存拡張 (uBlock / Bitwarden 等) が動き、`use_profile` で User Data フォルダごとアップロードできるので、**ログイン済みの状態でいきなり収集**を始められます。

書き味は Playwright とほぼ同じ（`locator` / `get_by_*` / `fill` / `press` / `wait_for` …）。違うのは「Hub に接続している」点と、結果が「ジョブ」単位で積み上がる点だけです。

## Playwright / Selenium との詳細比較

重要度順:

| 観点 | Paprika | Playwright | Selenium |
|---|---|---|---|
| **対象** | **収集ワークロードのプラットフォーム** | ブラウザ自動化のライブラリ | ブラウザ自動化のライブラリ |
| **分散実行** | **標準で分散**（Hub + N ワーカー × M レーン） | 自前で Grid / k8s を組む | Selenium Grid |
| **取得方式** | **ブラウザが読み込んだものを passive 回収**（再 GET なし） | URL を取り出して再 GET | URL を取り出して再 GET |
| **AI 駆動** | **`codegen-loop`（生成→実行→再生成）** | なし | なし |
| **画像 / アセット収集** | **既定機能**（min size / scroll / lazy 対応） | 自分で書く | 自分で書く |
| **動画取得** | **`yt-dlp` + 通信トレース統合** | 手作業で連携 | 手作業で連携 |
| **ライブ可観測性 / 管理画面** | **noVNC + 管理画面が標準付属** | デバッガ / 録画 | スクリーンショット |
| **ログイン継続** | **Bridge 拡張 / `use_profile` / Host レシピ** | 自前で Cookie / storage 注入 | 自前で Cookie / storage 注入 |
| **既存 Chrome 拡張・プロファイル** | **そのまま動く**（`--load-extension` / User Data ごと） | 別途エクスポート＆注入 | 別途エクスポート＆注入 |
| **API スタイル** | Playwright 風（同じ書き味） | Playwright | WebDriver |
| **ジョブモデル** | **REST/SDK で投入 → 完了待ち → アセット回収** | 自分でプロセス管理 | 自分でプロセス管理 |
| **JS 注入の範囲** | **拡張権限まで使える**（userScripts / declarativeNetRequest） | `addInitScript`（ページコンテキスト）のみ | `execute_script` のみ |
| **検出回避** | **`nodriver`**（webdriver シグナルを抑える） | パッチが必要 | パッチが必要 |

> Playwright や Selenium が悪い、という話ではありません。**「複数台で長く回す収集ワークロード」** が用途のとき、その上に組むべき配管が **標準で揃っている** のが Paprika の立ち位置です。

## いつ Paprika / いつ Playwright か

| やりたいこと | おすすめ |
|---|---|
| 複数サイト × 複数ホストで数千〜数百万ページを収集 | **Paprika** |
| 画像 / 動画 / リンクをまとめて回収（HLS/DASH 含む） | **Paprika** |
| 未知サイトにとりあえず投げて AI に拾ってもらう | **Paprika** |
| ログイン継続・年代/確認画面・ポップアップを乗り越える | **Paprika** |
| 何が起きているか目で見える運用にしたい | **Paprika** |
| アプリの E2E テスト（CI で 1 台 / 短時間） | Playwright / Selenium |
| 1 つのスクリプトを自前のジョブキューで十分に回せる | Playwright / Selenium |
| ブラウザ自動化を既存システムに組み込むライブラリとして使う | Playwright / Selenium |

## 移行の見当（Playwright を書いている人向け）

ほぼそのまま:

```python
# Playwright
page.locator("button.primary").click()
page.get_by_role("button", name="送信").click()
page.fill("#email", "alice@example.com")
```

Paprika:

```python
async with cli.session("https://...") as page:
    await page.locator("button.primary").click()
    await page.get_by_role("button", name="送信").click()
    await page.fill("#email", "alice@example.com")
```

違うのは「**Hub に接続している**」点と、**ジョブ単位**で結果が積み上がる点です。詳しくは [Locator リファレンス](locator.html) と [API リファレンス](api.html)。

## いま無いもの（正直に）

- **DRM 動画は取得不可、かつ取得を試みてはいけません** — Widevine / FairPlay / PlayReady などで保護された配信（Netflix・Amazon Prime Video・Disney+ など）は復号鍵がブラウザの保護領域（CDM）にあり、Paprika は DRM の回避・解除を一切行いません。著作権法第30条第1項第2号・第120条の2、不正アクセス禁止法、DMCA §1201、EU 著作権指令 第6条 で世界的に禁止されています。詳しくは [動画の取得と配信のしくみ](guides.html#video-mechanism) を参照。
- **既定で認証なし**（private LAN 想定）。外部公開する場合はリバプロ + 認証を手前に置いてください。
- **Windows ワーカー**は Linux フリートと別経路（noVNC ではなく CDP screencast）。

## 次のステップ

<div class="learning-paths">
  <div class="learning-path">
    <h3>動かしてみる</h3>
    <p class="who">まず手元で動くかを 5 分で確認</p>
    <ol>
      <li><a href="quickstart.html">Server インストール</a></li>
      <li><a href="admin.html">管理画面で URL を投げる</a></li>
      <li><a href="usecases.html">ユースケース集</a></li>
    </ol>
  </div>
  <div class="learning-path">
    <h3>SDK で書く</h3>
    <p class="who">スクリプトから自動化したい</p>
    <ol>
      <li><a href="intro.html">Client インストール</a></li>
      <li><a href="examples.html">サンプル集</a></li>
      <li><a href="api.html">API リファレンス</a></li>
    </ol>
  </div>
  <div class="learning-path">
    <h3>内部を知る</h3>
    <p class="who">なぜそう動くか・どこを変えれば良いか</p>
    <ol>
      <li><a href="architecture.html">アーキテクチャ概要</a></li>
      <li><a href="architecture.html#hub">Hub の仕組み</a></li>
      <li><a href="architecture.html#worker">Worker の仕組み</a></li>
    </ol>
  </div>
</div>
