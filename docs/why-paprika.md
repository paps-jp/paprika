---
layout: doc
title: なぜ Paprika? — Playwright / Selenium との違い
description: Playwright スタイルの API で書きやすく、分散ワーカー上の Chrome を AI とともに動かして画像・動画・リンクを集める。Paprika と Playwright・Selenium の違いを、シナリオ別に比較。
active: why-paprika
---

**Playwright と書き味は同じまま、フリート（複数ホストの Chrome）と AI で動かす**。Paprika は「ブラウザ自動化のフレームワーク」ではなく、**収集ワークロードを丸ごと面倒見るプラットフォーム**です。

<video class="shot" autoplay loop muted playsinline preload="metadata" aria-label="Paprika 管理画面で複数の Chrome Lane が同時にページを取得している様子">
  <source src="img/admin-live.webm" type="video/webm">
  <source src="img/admin-live.mp4" type="video/mp4">
  <img src="img/admin-live.gif" alt="Paprika 管理画面で複数の Chrome Lane が同時にページを取得している様子">
</video>
<p class="shot-cap">分散ワーカー上の Chrome が並列でページを取得している様子（管理画面 Live プレビュー）。</p>

> 採用判断のためのページです。導入は [Server インストール](quickstart.html)、SDK の使い方は [Client インストール](intro.html)、内部構造は [アーキテクチャ概要](architecture.html) を参照。

## 何が違うのか（要点）

- **Playwright 風 API**（`locator` / `get_by_*` / `fill` / `press` / `wait_for` …）。書き味は Playwright とほぼ同じ。
- **分散フリート**: 複数ホストの Chrome（**Lane**）を Hub が束ね、ジョブを **WebSocket でディスパッチ**。1 台でも、100 台でも同じ API。
- **AI コード生成**（`codegen-loop`）: URL と自然言語の **`goal` だけ**渡せば、LLM がスクリプトを生成・実行・失敗時に再生成。成功したスクリプトは **そのまま `mode: rerun` で再利用**できる（次回からは LLM 不要・決定的）。
- **収集に最適化**: スクロール・ネットワークトレース・**`yt-dlp` 連携**で動画も画像と同じ感覚で取得（[動画の仕組み](video.html)）。
- **二度取りしない**: **ブラウザが実際に読み込んだレスポンス**をそのまま回収（CDP `Network.responseReceived` を passive にサブスクライブ）。`<img src=>` を見て **URL から再取得しない**ので、(a) 帯域・サーバ負荷が半分、(b) **Cookie / Referer / 認証ヘッダーが必要な画像**もそのまま取れる、(c) **JS で動的に差し込まれた画像・lazy-load・CSS `background-image`・iframe 内**もまとめて拾えます。
- **ライブ可観測性**: 各 Chrome に **noVNC ライブ画面**が紐づき、管理画面で何が起きているか目で確認できる。
- **検出されにくい起動**: `nodriver` を採用し、`navigator.webdriver` などの典型的なシグナルを出さない。

## Playwright / Selenium との比較

| 観点 | Paprika | Playwright | Selenium |
|---|---|---|---|
| **対象** | **収集ワークロードのプラットフォーム** | ブラウザ自動化のライブラリ | ブラウザ自動化のライブラリ |
| **API スタイル** | Playwright 風（同じ書き味） | Playwright | WebDriver |
| **分散実行** | **標準で分散**（Hub + N ワーカー × M レーン） | 自前で Grid/k8s を組む | Selenium Grid |
| **ジョブモデル** | **REST/SDK で投入 → 完了待ち → アセット回収** | 自分でプロセス管理 | 自分でプロセス管理 |
| **AI 駆動** | **`codegen-loop`（生成→実行→再生成）** | なし | なし |
| **動画取得** | **`yt-dlp` + 通信トレース統合** | 手作業で連携 | 手作業で連携 |
| **画像/アセット収集** | **既定機能**（min size / scroll / lazy 対応） | 自分で書く | 自分で書く |
| **取得方式** | **ブラウザが読み込んだものを passive 回収**（再 GET なし） | URL を取り出して再 GET | URL を取り出して再 GET |
| **ライブ可観測性** | **noVNC + 管理画面** | デバッガ / 録画 | スクリーンショット |
| **ログイン継続** | **Bridge 拡張 / `use_profile` / Host レシピ** | 自前で Cookie / storage 注入 | 同左 |
| **検出回避** | **`nodriver`**（webdriver シグナルを抑える） | パッチが必要 | パッチが必要 |
| **管理画面** | **既定で付属** | 自前 | 自前 |

> Playwright や Selenium が悪い、という話ではありません。**「複数台で長く回す収集ワークロード」**が用途のとき、その上に組むべき配管が**標準で揃っている**のが Paprika の立ち位置です。

## こんなときに Paprika

- **複数サイト × 複数ホスト**で **数千〜数百万ページ**を収集したい
- **画像 / 動画 / リンク**をまとめて回収したい（HLS/DASH 含む）
- **未知サイト**にとりあえず投げて、AI に拾ってもらいたい
- ログイン継続・年代/確認画面・ポップアップなどの **「ブラウザならではの障壁」** を乗り越えたい
- 何が起きているか **目で見える**運用にしたい

## こんなときは Playwright / Selenium が素直

- **アプリの E2E テスト**（CI で 1 台 / 短時間 / 結果は pass/fail）
- 1 つのスクリプトを **自前のジョブキュー** で十分に回せる
- ブラウザ自動化を **既存システムに組み込む**ライブラリとして使いたい

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

- **DRM 動画**は取得不可（Widevine / FairPlay / PlayReady）。仕様。
- 既定で **認証なし**（private LAN 想定）。外部公開する場合はリバプロ + 認証を手前に置いてください。
- **Windows ワーカー**は Linux フリートと別経路（noVNC ではなく CDP screencast）。

## 次のステップ

- [Server インストール](quickstart.html) — まず動かしてみる（5 分）
- [Client インストール](intro.html) — SDK で接続する
- [ユースケース集](usecases.html) — 目的別の通し
- [アーキテクチャ概要](architecture.html) — 内部構造の地図
