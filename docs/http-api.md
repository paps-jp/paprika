---
layout: doc
title: HTTP API（任意の言語から）
description: Python / PHP SDK を使わずに curl・JavaScript・Go など任意の言語から Paprika を叩く REST/JSON API ガイド。ジョブ投入 → 進捗ポーリング → アセット取得の流れと、503 リトライ・主要オプション。
active: http-api
---

Paprika SDK（Python / PHP）を使わなくても、Paprika は素の **HTTP / JSON API** で操作できます。`curl` でも JavaScript / Go / Ruby / どの言語からでも、やることは「**ジョブを投げて、結果を取る**」だけです。

> SDK の使い方は [API リファレンス](api.html)、概念は [はじめに](intro.html) を参照。困ったら [FAQ](faq.html) へ。

## ベース URL と認証

すべてのエンドポイントは Hub のベース URL からの相対パスです。本ページの例では `http://localhost:8000`（環境変数 `PAPRIKA_HUB` を使う想定）。

```bash
export PAPRIKA_HUB=http://localhost:8000
```

既定では **認証なし**（Hub は private LAN 前提）。外部公開する場合は手前にリバースプロキシ + 認証を置いてください。

## 基本フロー（4 ステップ）

1. `POST /jobs` でジョブ投入 → `job_id` を受け取る
2. `GET /jobs/{job_id}` を **ポーリング** して `status` が終端になるのを待つ
3. `GET /jobs/{job_id}/assets.json`（または `/result`）で取得物の一覧を得る
4. 各アセットの `href` から **ダウンロード**

## 1. ジョブ投入 — `POST /jobs`

```bash
curl -X POST "$PAPRIKA_HUB/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","options":{"mode":"fetch","capture_assets":true}}'
```

レスポンス（`200`）:

```json
{ "job_id": "ccea8ea9dc2a", "status": "queued", "url": "https://example.com", "options": { } }
```

フリート（ワーカー）が満杯のときは **`503`** が返ります。Hub は在庫待ちせず即返すので、**クライアント側で指数バックオフ再試行**してください（[下記](#retry-503)）。

> ショートカット: `GET /<URL>` でも投入できます — `curl "$PAPRIKA_HUB/https://example.com"`

## 2. 進捗ポーリング — `GET /jobs/{id}`

```bash
curl "$PAPRIKA_HUB/jobs/ccea8ea9dc2a"
```

`status` は `queued` → `running` → **`completed` / `failed` / `cancelled`**（後ろ 3 つが終端）。1〜2 秒間隔のポーリングで十分です。

## 3. 取得物の一覧 — `GET /jobs/{id}/assets.json`

```json
{
  "job_id": "ccea8ea9dc2a",
  "count": 24,
  "items": [
    {
      "name": "06.jpg",
      "href": "/jobs/ccea8ea9dc2a/assets/06.jpg",
      "kind": "image",
      "mime": "image/jpeg",
      "size": 7160,
      "size_h": "7.0 KB",
      "ext": "jpg",
      "source_url": "https://.../06.jpg",
      "page_url": "https://..."
    }
  ]
}
```

`kind` は `image` / `video` / `audio` / `other`。
`GET /jobs/{id}/result` でも `html_href`・`log_href` + アセット一覧（やや簡易版）が得られます。

## 4. ダウンロード

`href` は Hub 相対パスなので、ベース URL を前置して取得します:

```bash
curl -O "$PAPRIKA_HUB/jobs/ccea8ea9dc2a/assets/06.jpg"
```

## まとめて：エンドツーエンドの例（bash）

```bash
#!/usr/bin/env bash
set -euo pipefail
HUB=${PAPRIKA_HUB:-http://localhost:8000}

# 1) 投入（503 は数回リトライ）
for i in 1 2 3 4 5; do
  resp=$(curl -s -o /tmp/j.json -w '%{http_code}' -X POST "$HUB/jobs" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com","options":{"mode":"fetch","capture_assets":true}}')
  [ "$resp" = "503" ] || break
  sleep $((i*2))
done
jid=$(python -c 'import json;print(json.load(open("/tmp/j.json"))["job_id"])')
echo "job=$jid"

# 2) 完了までポーリング
while :; do
  st=$(curl -s "$HUB/jobs/$jid" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "status=$st"; case "$st" in completed|failed|cancelled) break;; esac; sleep 2
done

# 3) アセットを全部ダウンロード
curl -s "$HUB/jobs/$jid/assets.json" \
  | python -c 'import sys,json;[print(i["href"]) for i in json.load(sys.stdin)["items"]]' \
  | while read -r href; do curl -sO "$HUB$href"; done
```

## AI に任せる（`mode: codegen-loop`）

URL と「やりたいこと（`goal`）」を渡すと、LLM がスクリプトを生成して実行します（未知サイト・複雑な操作・動画向け。LLM が走るので課金あり）:

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url": "https://example.com",
  "options": {"mode":"codegen-loop","goal":"メイン動画を再生してダウンロードして保存して","max_codegen_attempts":3}
}'
```

## ライブログ（WebSocket）

実行ログをリアルタイム受信できます:

```
ws://localhost:8000/jobs/{job_id}/events?since=0
```

各メッセージは JSON 1 行（`{"type":"log"|"done"|"error","data": ... }`）。

## その他

| メソッド・パス | 用途 |
|---|---|
| `GET /jobs` | ジョブ一覧 |
| `GET /jobs/{id}/page.html` | 取得した HTML |
| `GET /jobs/{id}/log.txt` | 実行ログ（全文） |
| `DELETE /jobs/{id}` | ジョブとファイルを削除 |
| `GET /workers` | ワーカー（フリート）状況 |

## 503 リトライ（重要） {#retry-503}

満杯時の `503` は正常な背圧です。必ずバックオフ再試行を入れてください:

```javascript
async function submit(body, hub = process.env.PAPRIKA_HUB) {
  for (let i = 0; i < 6; i++) {
    const r = await fetch(`${hub}/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status !== 503) return r.json();
    await new Promise(s => setTimeout(s, 1000 * 2 ** i)); // 1,2,4,8,16,32s
  }
  throw new Error('fleet busy — retried 6x');
}
```

## 主な `options`

| キー | 既定 | 説明 |
|---|---|---|
| `mode` | `fetch` | `fetch`（高速・レシピ）/ `codegen-loop`（AI）/ `rerun`（コード直接実行） |
| `download_video` | `false` | 通信トレース + yt-dlp で動画を取得 |
| `capture_assets` | `true` | 取得物をサーバに保存 |
| `scroll` | 設定依存 | 最後までスクロールして遅延ロード（lazy）を拾う |
| `headless` | `false` | 画面を出さずに実行 |
| `min_asset_size_bytes` | 設定依存 | これ未満の画像を除外（`0` で全部拾う） |
| `use_profile` | — | アップロード済み Chrome プロファイル名（ログイン状態の持ち込み） |
| `goal` | — | `codegen-loop` 時の目標（自然言語） |
| `max_codegen_attempts` | `3` | `codegen-loop` の再試行回数 |
| `attempt_timeout_s` | — | 1 試行のタイムアウト（秒） |

オプションの完全な一覧と SDK での指定方法は [API リファレンス](api.html) を参照してください。
