---
layout: doc
title: Host レシピ — ホスト固有設定の自動化
description: ホストごとに「ナビ後に決まった操作（クリック・入力・スクロール・待機）を実行する」プレイブックを登録して fetch を高速化。アクションの形・ログインレシピ・自動再ログイン・複数 Hub 間の共有を解説。
active: host-recipe
---

**Host レシピ** は、特定のホスト（サイト）にアクセスしたとき「ナビゲーション後に決まった操作を実行する」**プレイブック**です。クリック・入力・待機・スクロールといった**決定的な手順**を登録しておくと、`fetch` モードが**LLM 不要**で同じことを毎回安定して再現できます。

> ログイン継続の他の選択肢: [Bridge 拡張](bridge-extension.html)（Cookie 手動 push）、[`use_profile`](profile.html)（フルプロファイル）。

## 何ができるか

- ホストごとに **URL パターン**（glob）と**アクション列**を登録
- ジョブが該当ホストにアクセスすると、Hub が最適マッチのレシピを選び、**Worker のナビ直後に実行**
- ログイン手順を登録しておけば、Cookie 失効時に**自動再ログイン**で救える
- 失敗したらレシピ無しのフェッチに**フォールバック**

## レシピのスキーマ

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

## アクションの種類 {#actions}

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

## 登録（HTTP API）

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

## 確認・更新

```bash
# ホストの全レシピ + 設定
curl "$PAPRIKA_HUB/hosts/example.com"

# 全ホスト
curl "$PAPRIKA_HUB/hosts"
```

レシピは **追加**されます。古いものを消したいときは `PUT /hosts/{host}` でレシピ全体を置換するか、管理画面の **Hosts** タブから編集してください。

## ジョブからの利用

レシピが登録済みのホストには、**fetch ジョブが自動でレシピを適用**します。明示的に外したい場合は `fetch_strategy: "normal"` を指定:

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url":"https://example.com/items/123",
  "options":{"mode":"fetch","fetch_strategy":"normal"}
}'
```

## ログインレシピ → 自動再ログイン {#login-recipe}

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

## AI モードで「観察 → 保存」する流れ

AI モード（`codegen-loop`）でうまくいった操作は、**そのまま Host レシピに保存**して次回以降決定的に再生できます（**Phase 2c**: 管理画面の Live パネル → 「recipe として保存」）。

```text
未知サイト
  └─ codegen-loop で観察＆操作（LLM）
        └─ うまくいった action 列を Host レシピに保存
              └─ 次回からは fetch + 保存済みレシピで決定的に実行（LLM 不要）
```

## 複数 Hub 間の共有

ホスト設定（Cookie・レシピ・login_recipe・popup_policy・notes）は **MariaDB で共有**されます。1 つの Hub で登録すれば**他のどの Hub からも同じレシピが使われます**（[Hub の仕組み](architecture-hub.html)）。

## トラブルシュート

- **レシピが効かない** → `pattern` が URL パスにマッチしているか確認（一致は **glob・最長一致**）。
- **途中で止まる** → `actions` のセレクタが見つかっていない可能性。`wait` を入れる。`timeout_s` を伸ばす。
- **動作の確認** → 管理画面の **Live パネル**でログを確認。失敗時はレシピ無しのフェッチに**フォールバック**します。
- **書きづらい** → まず AI モード（`codegen-loop`）で動かして、成功した action 列を Host レシピとして保存する流れが楽です。

## 関連

- [Bridge 拡張](bridge-extension.html)
- [プロファイル `use_profile`](profile.html)
- [API リファレンス](api.html)
- [管理画面ガイド: Hosts](admin.html)
