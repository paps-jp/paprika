---
layout: doc
title: Paprika Bridge（Chrome 拡張）
description: 普段使いの Chrome で取ったログインの Cookie を、ワンクリックで Paprika Hub に push する Chrome 拡張機能の使い方と仕組み。
active: bridge-extension
---

**Paprika Bridge** は、普段使いの Chrome で取ったログインの **Cookie をワンクリックで Hub に保存**するための Chrome 拡張です。これにより、Paprika のジョブが**そのログイン済み状態**でサイトにアクセスできるようになります。

> ログイン継続の選択肢には他に **`use_profile`**（[プロファイル](profile.html)）と **Host レシピの自動再ログイン**（[Host レシピ](host-recipe.html)）があります。要件に応じて選びましょう。

## できること（v0.2）

- ブラウザの **Cookie 全体** を読んで、**ホスト別**にまとめて Hub の `/hosts/{host}` レジストリに PUT します。
- ジョブは `options.cookies_from="<host>"`、もしくは保存済みであれば**自動で**そのホストの Cookie を持っていきます。
- 最新 Chrome の **App-Bound 暗号化 Cookie**（v20）にも対応（拡張 API 経由で取得するため、復号鍵に依存しません）。

> Chrome 内の **保存パスワード / Local Storage / IndexedDB** は API 制約で対象外です。フルプロファイルが必要なら [`use_profile`](profile.html) を使ってください。

## インストール

二通りあります。

### A. Hub から配布されたものを使う

```text
http://<your-hub>/profiles/extension/install
```

を Chrome で開き、案内に従って zip をダウンロード → 展開 → `chrome://extensions` で「**デベロッパーモード**」をオンにして「**パッケージ化されていない拡張機能を読み込む**」で展開したフォルダを選びます。

### B. Git ソースから直接

```bash
git clone https://github.com/paps-jp/paprika.git
```

`chrome://extensions` → 「パッケージ化されていない拡張機能を読み込む」→ `server/web/extensions/paprika-bridge/` を選択。

## 使い方（3 ステップ）

1. ツールバーのアイコンをクリック（最初はパズルピースから固定しておくと楽）
2. **初回のみ** Hub の URL を入力（例: `http://paprika.lan:8000` / `http://localhost:8000`）。保存されます。
3. スコープを選んで **「Push cookies to hub」** をクリック
   - **active-host**（既定）: 今アクティブなタブのホストだけ送る
   - **all**: ブラウザに Cookie がある全ホストを送る

成功すると ✓ が出ます。**いつでも再 push 可能**（最後の書き込みが勝ち、同じ操作を何度繰り返しても安全）。

## ジョブから使う

push 後は、そのホスト宛のジョブに **自動で Cookie が注入**されます。明示したいときは `cookies_from` を指定します:

```python
job = await cli.fetch(
    "https://market.example.com/item/xxx",
    cookies_from="market.example.com",
    capture_assets=True,
)
```

HTTP から:

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url":"https://market.example.com/item/xxx",
  "options":{"mode":"fetch","cookies_from":"market.example.com","capture_assets":true}
}'
```

## 仕組み（参考）

- `chrome.cookies.getAll({})` で **全 Cookie ストア**（既定 + Incognito + プロファイル別）を読む
- ホスト別にグループ化（`www.` を外して小文字化 = Hub の `HostRegistry` の正規形に合わせる）
- ホストごとに **PUT `/hosts/{host}`**（既存の `notes` / `popup_policy` / `recrawl_patterns` などは保持、**Cookie 部分だけを置き換え**）

## 権限の説明

| 権限 | 用途 |
|---|---|
| `cookies` | `chrome.cookies.getAll()` で Cookie を読む |
| `storage` | Hub の URL を保存（次回からの入力を省略） |
| `activeTab` / `tabs` | active-host スコープで今のタブのホストを判定 |
| `clipboardWrite` | クリップボードへの状態テキスト書き込み（補助用） |

## いつ使うか

| シナリオ | 推奨 |
|---|---|
| 1 つのサイトに**手動でログインして取る** | **Bridge 拡張**（このページ） |
| **完全な Chrome プロファイル**を持ち込みたい（autofill 等） | [`use_profile`](profile.html) |
| 期限切れになる Cookie を**自動で再取得**したい | [Host レシピ（ログインレシピ）](host-recipe.html) |

## 関連

- [プロファイル `use_profile`](profile.html)
- [Host レシピ](host-recipe.html)
- [FAQ: ログイン必須サイト](faq.html)
- [サンプル: ログイン → 操作 → 動画](examples.html)
