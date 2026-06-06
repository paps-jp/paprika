---
layout: doc
title: ログイン・プロファイル管理
description: ログイン継続の 3 方式 (Paprika Bridge 拡張 / use_profile / Host レシピ) を 1 ページに統合。Cookie 同期、フルプロファイル持ち込み、自動再ログインの選び方と設定。
active: auth
redirect_from:
  - /bridge-extension.html
  - /profile.html
  - /host-recipe.html
---

Paprika でログイン済みサイトを継続的に収集する方法は 3 通りあります。
このページで 3 つの違いと使い分けを整理し、各方式の手順を順に解説します。

<div class="tldr">
<span class="tldr-label">概要</span>
<ul>
<li><strong>Bridge 拡張</strong> — 普段使い Chrome から Cookie をワンクリック push。最も手軽。</li>
<li><strong>use_profile</strong> — Chrome User Data フォルダごとアップロードして、autofill / 保存パスワード / 拡張機能まで一式持ち込み。</li>
<li><strong>Host レシピ</strong> — ナビゲーション直後の決定的な操作列を登録。Cookie 失効時に自動再ログインも可。</li>
</ul>
</div>

## 方式の比較

| 方式 | 何を持ち込む | いつ使う | 設定の手間 |
|---|---|---|---|
| **[Bridge 拡張](#bridge)** | Cookie のみ | 1 サイトに手動ログインして取りたい | 拡張インストール → ワンクリック |
| **[use_profile](#use-profile)** | Chrome 1 プロファイル丸ごと（autofill / 保存 PW / 拡張機能含む） | 普段使い Chrome の環境ごと持ち込みたい | tar.gz でアップロード |
| **[Host レシピ](#host-recipe)** | 「クリック・入力・待機」の決定的な操作列 | サイトの確認画面通過・自動再ログイン | JSON でアクション列を登録 |

組み合わせも可能です（例: `use_profile` + Host レシピで「ログイン済み環境 + 期限切れ時に自動再ログイン」）。

---

## Bridge 拡張 {#bridge}

**Paprika Bridge** は、普段使いの Chrome で取ったログインの **Cookie をワンクリックで Hub に保存**するための Chrome 拡張です。Paprika のジョブは保存された Cookie を使って **そのログイン済み状態**でサイトにアクセスします。

### できること

- ブラウザの **Cookie 全体** を読んで、**ホスト別**にまとめて Hub の `/hosts/{host}` レジストリに PUT
- ジョブは `options.cookies_from="<host>"`、もしくは保存済みであれば**自動で**そのホストの Cookie を持っていく
- 最新 Chrome の **App-Bound 暗号化 Cookie**（v20）にも対応（拡張 API 経由で取得するため、復号鍵に依存しない）

> Chrome 内の **保存パスワード / Local Storage / IndexedDB** は API 制約で対象外です。フルプロファイルが必要なら [`use_profile`](#use-profile) を使ってください。

### インストール

二通りあります。

**A. Hub から配布されたものを使う**

```text
http://<your-hub>/profiles/extension/install
```

を Chrome で開き、案内に従って zip をダウンロード → 展開 → `chrome://extensions` で「**デベロッパーモード**」をオンにして「**パッケージ化されていない拡張機能を読み込む**」で展開したフォルダを選びます。

**B. Git ソースから直接**

```bash
git clone https://github.com/paps-jp/paprika.git
```

`chrome://extensions` → 「パッケージ化されていない拡張機能を読み込む」→ `server/web/extensions/paprika-bridge/` を選択。

### 使い方（3 ステップ）

1. ツールバーのアイコンをクリック（最初はパズルピースから固定しておくと楽）
2. **初回のみ** Hub の URL を入力（例: `http://your-hub.example:8000` / `http://localhost:8000`）。保存されます。
3. スコープを選んで **「Push cookies to hub」** をクリック
   - **active-host**（既定）: 今アクティブなタブのホストだけ送る
   - **all**: ブラウザに Cookie がある全ホストを送る

成功すると ✓ が出ます。**いつでも再 push 可能**（最後の書き込みが勝ち、同じ操作を何度繰り返しても安全）。

### ジョブから使う

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

### 仕組み（参考）

- `chrome.cookies.getAll({})` で **全 Cookie ストア**（既定 + Incognito + プロファイル別）を読む
- ホスト別にグループ化（`www.` を外して小文字化 = Hub の `HostRegistry` の正規形に合わせる）
- ホストごとに **PUT `/hosts/{host}`**（既存の `notes` / `popup_policy` / `recrawl_patterns` などは保持、**Cookie 部分だけを置き換え**）

### 権限の説明

| 権限 | 用途 |
|---|---|
| `cookies` | `chrome.cookies.getAll()` で Cookie を読む |
| `storage` | Hub の URL を保存（次回からの入力を省略） |
| `activeTab` / `tabs` | active-host スコープで今のタブのホストを判定 |
| `clipboardWrite` | クリップボードへの状態テキスト書き込み（補助用） |

---

## use_profile — Chrome プロファイルの持ち込み {#use-profile}

`use_profile` は、自分の **Chrome の User Data フォルダ（プロファイル）をアップロード**して、そのログイン状態を含むまま Paprika のジョブに使う仕組みです。Cookie だけでなく **autofill / 保存パスワード / 拡張機能 / 設定**まで丸ごと持ち込めます。

> **uBlock Origin / Bitwarden など普段使っている Chrome 拡張**もそのまま動きます。広告ブロック付きで収集、保存パスワードで自動ログインなど、**手元の Chrome と同じ環境**でジョブが走らせられます。

### アップロードする中身

Chrome の **User Data 配下の 1 プロファイル**（典型は `Default/`）。Paprika は以下の形を受け付けます:

| 形 | 説明 |
|---|---|
| `Default/...` を含む tar.gz | 既定。**Hub がそのまま採用** |
| `Profile N/...` を含む tar.gz | **`Default/` に正規化**して受け付け |
| Cookies などが**ルートに直接** | **`Default/` で包んで**正規化 |

> 必要なら `Local State`（ルート直下）も同梱できます。

### アップロード

**CLI（推奨）:**

```bash
paprika-client upload-profile <name> <path/to/profile_dir_or_tarball>
```

例:

```bash
# Linux/Mac: User Data の 1 プロファイルを直接渡す
paprika-client upload-profile work ~/.config/google-chrome/Default
# tar.gz を渡してもOK
paprika-client upload-profile work ./profile-snapshot.tar.gz
```

**HTTP API:**

```bash
curl -X POST "$PAPRIKA_HUB/profiles/work" \
  -H 'Content-Type: application/x-tar' --data-binary @profile.tar.gz
```

`name` は a-z0-9 と `-` `_` で、サイト識別しやすいものを（例: `work` / `paps-staging` / `tw-bob`）。

### ジョブから使う

```python
job = await cli.fetch(
    "https://example.com",
    use_profile="work",          # 名前で指定
)
```

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url":"https://example.com",
  "options":{"mode":"fetch","use_profile":"work"}
}'
```

ジョブは **その Worker の Lane に対し、プロファイルを差し込んだ状態**で Chrome を立ち上げ、終わったら**元の素の状態に戻します**（Lane 自体は壊しません）。

### 既定プロファイル

`options.use_profile` を**省略したジョブ**は、Hub に登録した「既定プロファイル」が（あれば）自動で使われます。常にログインで取りたいときに便利です。

```bash
# 既定を設定
curl -X POST "$PAPRIKA_HUB/profiles/work/default"

# 確認
curl "$PAPRIKA_HUB/profiles/default"

# 解除
curl -X DELETE "$PAPRIKA_HUB/profiles/default"
```

### 一覧 / 削除

```bash
# 一覧
curl "$PAPRIKA_HUB/profiles"

# 中身（メタ情報）
curl "$PAPRIKA_HUB/profiles/work"

# 削除
curl -X DELETE "$PAPRIKA_HUB/profiles/work"
```

### 複数 Hub 間の共有

複数 Hub 構成では、プロファイルは **MariaDB(メタ) + MinIO(本体)** で共有され、**どの Hub からも同じ名前で使えます**。`work` を 1 Hub にアップロードすれば、別 Hub で投げたジョブからも `use_profile: "work"` で参照可能です（[アーキテクチャ概要 → Hub](architecture.html#hub)）。

### 注意

- アップロードする tar.gz の **先頭ディレクトリ名**は気にしなくて構いません（Hub が `Default/` に正規化します）。
- **拡張機能**は通常の Chrome の場所だと Profile 移動後に無効化されることがあります（PreferenceVerifier）。Paprika 側は `--load-extension` で再注入して持ち込みます。
- 既定プロファイルは Hub 側の設定なので、**運用全体に効く**点に注意。

---

## Host レシピ — ホスト固有設定の自動化 {#host-recipe}

**Host レシピ** は、特定のホスト（サイト）にアクセスしたとき「ナビゲーション後に決まった操作を実行する」**プレイブック**です。クリック・入力・待機・スクロールといった**決定的な手順**を登録しておくと、`fetch` モードが**LLM 不要**で同じことを毎回安定して再現できます。

<img class="shot" src="img/cap-knowledge.png" alt="管理画面の AI Knowledge / Hosts タブ — ホスト固有設定の管理" loading="lazy">
<p class="shot-cap">登録されたレシピや Cookie、ログイン手順は管理画面の <strong>AI Knowledge</strong> / <strong>Hosts</strong> タブから確認・編集できます。</p>

### 何ができるか

- ホストごとに **URL パターン**（glob）と**アクション列**を登録
- ジョブが該当ホストにアクセスすると、Hub が最適マッチのレシピを選び、**Worker のナビ直後に実行**
- ログイン手順を登録しておけば、Cookie 失効時に**自動再ログイン**で救える
- 失敗したらレシピ無しのフェッチに**フォールバック**

### レシピのスキーマ

```json
{
  "pattern":     "/items/*",
  "description": "商品ページの年代確認を通す",
  "actions": [
    {"kind":"wait",  "selector":".loaded",  "timeout_s": 5},
    {"kind":"click", "selector":"button#agree"},
    {"kind":"scroll","amount": 1000}
  ],
  "created_by":  "operator",
  "created_from_job": null
}
```

主要フィールド:

| フィールド | 既定 | 意味 |
|---|---|---|
| `pattern` | `"*"` | URL パスの **glob**（複数ヒットは最長一致が勝つ） |
| `description` | `""` | 人間向けメモ |
| `actions` | `[]` | 実行する操作の列（[後述](#actions)） |
| `goal` | `null` | （将来）LLM agent のゴール |
| `code` | `null` | （将来）Python スニペット |
| `timeout_s` | `30.0` | レシピ全体のタイムアウト |
| `created_by` | `"operator"` | `operator` / `ai` |

### アクションの種類 {#actions}

各アクションは `kind` と種類別フィールドで指定します。

| `kind` | 例 | 用途 |
|---|---|---|
| `wait` | `{"kind":"wait","selector":".loaded","timeout_s":5}` | 要素表示まで待つ |
| `click` | `{"kind":"click","selector":"button#agree"}` | クリック |
| `fill` | `{"kind":"fill","selector":"#email","text":"alice@..."}` | テキスト入力 |
| `type` | `{"kind":"type","text":"Hello"}` | キー入力 |
| `press` | `{"kind":"press","key":"Enter"}` | 単一キー押下 |
| `scroll` | `{"kind":"scroll","amount":1000}` | スクロール |
| `navigate` | `{"kind":"navigate","url":"https://..."}` | 別 URL へ |
| `evaluate` | `{"kind":"evaluate","code":"document.title"}` | JS 実行 |

### 登録（HTTP API）

```bash
curl -X POST "$PAPRIKA_HUB/hosts/example.com/recipes" \
  -H 'Content-Type: application/json' \
  -d '{
    "pattern": "/items/*",
    "description": "確認ダイアログを通す",
    "actions": [
      {"kind":"wait", "selector":"#agree","timeout_s":5},
      {"kind":"click","selector":"#agree"}
    ]
  }'
```

ホストが未登録でも自動で作成されます。

### 確認・更新

```bash
# ホストの全レシピ + 設定
curl "$PAPRIKA_HUB/hosts/example.com"

# 全ホスト
curl "$PAPRIKA_HUB/hosts"
```

レシピは **追加**されます。古いものを消したいときは `PUT /hosts/{host}` でレシピ全体を置換するか、管理画面の **Hosts** タブから編集してください。

### ジョブからの利用

レシピが登録済みのホストには、**fetch ジョブが自動でレシピを適用**します。明示的に外したい場合は `fetch_strategy: "normal"` を指定:

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url":"https://example.com/items/123",
  "options":{"mode":"fetch","fetch_strategy":"normal"}
}'
```

### ログインレシピ → 自動再ログイン {#login-recipe}

ログイン手順を特別なレシピ（**login_recipe**）として登録すると、ホストの Cookie が失効したときに **fetch 投入の直前に自動再ログイン**します。

```bash
curl -X PUT "$PAPRIKA_HUB/hosts/market.example.com/login_recipe" \
  -H 'Content-Type: application/json' \
  -d '{
    "actions": [
      {"kind":"navigate","url":"https://market.example.com/login"},
      {"kind":"fill","selector":"#email","text":"alice@..."},
      {"kind":"fill","selector":"#password","text":"secret"},
      {"kind":"click","selector":"button[type=submit]"}
    ]
  }'
```

セッション Cookie の TTL を過ぎたジョブが投入されると、Hub がこのレシピを実行し、新しい Cookie を保存してから本来のジョブを進めます。

> 認証情報は環境変数 / シークレットストア経由で渡し、レシピに**直接書かない**運用を推奨します。

### AI モードで「観察 → 保存」する流れ

AI モード（`codegen-loop`）でうまくいった操作は、**そのまま Host レシピに保存**して次回以降決定的に再生できます（管理画面の Live パネル → 「recipe として保存」）。

```text
未知サイト
  └─ codegen-loop で観察＆操作（LLM）
        └─ うまくいった action 列を Host レシピに保存
              └─ 次回からは fetch + 保存済みレシピで決定的に実行（LLM 不要）
```

### 複数 Hub 間の共有

ホスト設定（Cookie・レシピ・login_recipe・popup_policy・notes）は **MariaDB で共有**されます。1 つの Hub で登録すれば**他のどの Hub からも同じレシピが使われます**（[アーキテクチャ概要 → Hub](architecture.html#hub)）。

### トラブルシュート

- **レシピが効かない** → `pattern` が URL パスにマッチしているか確認（一致は **glob・最長一致**）。
- **途中で止まる** → `actions` のセレクタが見つかっていない可能性。`wait` を入れる。`timeout_s` を伸ばす。
- **動作の確認** → 管理画面の **Live パネル**でログを確認。失敗時はレシピ無しのフェッチに**フォールバック**します。
- **書きづらい** → まず AI モード（`codegen-loop`）で動かして、成功した action 列を Host レシピとして保存する流れが楽です。

---

## 関連

- [サンプル: ログイン → 操作 → 動画](examples.html#login-flow)
- [FAQ: ログイン必須サイト](faq.html#ログインが必要なサイト)
- [管理画面ガイド: Hosts タブ](admin.html)
- [API リファレンス](api.html)
