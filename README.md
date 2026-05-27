# Paprika

[![GitHub](https://img.shields.io/badge/github-paps--jp%2Fpaprika-181717?logo=github)](https://github.com/paps-jp/paprika)
[![Python](https://img.shields.io/badge/python-3.13-blue.svg?logo=python)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker)](https://www.docker.com/)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm--NC--1.0.0-orange.svg)](LICENSE)

ブラウザベースの **分散ワーカー + Web 自動化 + 動画収集** フレームワーク。

> 📖 **マニュアル**: [paps-jp.github.io/paprika](https://paps-jp.github.io/paprika/)
> (有効化前は [htmlpreview 経由](https://htmlpreview.github.io/?https://github.com/paps-jp/paprika/blob/main/docs/index.html))

---

## 背景

[**特定非営利活動法人 ぱっぷす**](https://paps.jp) — 性的搾取・デジタル性暴力被害の相談窓口。意に反して撮影・流出・拡散された性的画像・動画など、デジタル性暴力に遭った方からの相談を受け付け、サイト・プラットフォーム運営者への削除要請、被害者支援、社会への啓発活動を行っています。

ぱっぷすでは、意に反して投稿された性的画像・動画がインターネット上や SNS 上に拡散していないかを探索し、削除要請までを自動化するシステム **ProtectionAI** を開発しています。被害画像を見つけるためには、SNS / 画像共有サイト / 動画サイト / ファイル共有サービスなど、インターネット上のさまざまな場所を継続的に巡回する必要がありますが、これらのサイトは認証・動的描画・年齢確認などにより機械的な巡回が難しい場合が多くあります。

そこで ぱっぷす は、こうした複雑なサイトでも安定してページを開き、画像・動画・リンク URL を収集できる独自クローラー基盤として **Paprika** を開発しました。Paprika は ProtectionAI の探索基盤として動作する一方、汎用的な Web 自動化フレームワークとして整理し、オープンソースとして公開しています。

> **注:** 本リポジトリのコード自体に被害者画像の検出ロジックは含まれていません (それは ProtectionAI 側で実装)。ここで公開しているのは「ブラウザを開いてページを巡回し、画像・動画・リンクを収集する」ための汎用的な Web 自動化基盤です。

---

## 機能

- **分散ワーカー** — N 台の worker ホストに Lane (= 独立 Chrome + noVNC) を持たせて並列実行
- **3 つのジョブモード**:
  - `fetch` — 単発で URL を開いて HTML + assets を取得
  - `codegen-loop` — 自然言語 goal → LLM が paprika-client スクリプト生成 → 実行
  - `rerun` — 既存スクリプトを sandbox で実行
- **v2 brain / eye 分離** — Qwen-VL (perception / eye) + DeepSeek-R1 (judge & distiller / brain)
- **Plugin Registry** — `data/tools/installed/` + `catalog.json` の動的プラグイン (paprika-flare / paprika-proxy-fetch / paprika-ytdlp)
- **paprika-client SDK** — Playwright 互換の async Python API (`page.goto()`, `page.click()`, `page.links()`, ...)
- **管理 UI** — ジョブ投入 / Live ログ / noVNC / ギャラリー / Hosts / Presets / Plugins / AI ナレッジ
- **自動更新** — 全 worker が Hub のソース変更を tarball で自動取得
- **クローン自動検知** — LXC / Proxmox / VMware で worker を複製しても hub が自動 ID リネーム
- **動画取得** — passive CDP capture (.mp4/.webm) + `page.download_video()` (yt-dlp 経由 .m3u8/.mpd)

---

## クイックスタート

### A. Docker Compose で全部立ち上げ (一番ラク)

```bash
git clone https://github.com/paps-jp/paprika.git
cd paprika
cp .env.example .env             # 必要なら HUB_URL や NOVNC_PUBLIC_HOST を編集
docker compose up -d --build
```

→ Hub: <http://localhost:8000>  /  noVNC (Lane 0): <http://localhost:6080/vnc_lite.html>

### B. Worker ホストを追加する

新しい Linux ホスト (LXC / VM / 物理) で:

```bash
git clone https://github.com/paps-jp/paprika.git /opt/paprika
cd /opt/paprika
cp .env.example .env
# .env を編集:
#   HUB_URL=ws://<hub-host>:8000
#   NOVNC_PUBLIC_HOST=<this-host-lan-ip>
docker compose -f docker-compose-worker.yml up -d
```

Hub の `GET /workers` で `alive=true` として登場すれば OK。
詳細は [マニュアル §10 ワーカー運用](https://paps-jp.github.io/paprika/operations.html)。

### C. ジョブを投入

```bash
# 最小の fetch ジョブ
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# 自然言語タスクを LLM に丸投げ (codegen-loop)
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "options": {
      "mode": "codegen-loop",
      "goal": "トップから辿れるページを順にクロールして画像を保存"
    }
  }'
```

詳細パラメータと全エンドポイントは
[マニュアル §6 API リファレンス](https://paps-jp.github.io/paprika/api.html)。

### D. Python SDK (paprika-client)

```python
import asyncio
from paprika_client import async_paprika

async def main():
    # connect() は引数なしで PAPRIKA_HUB 環境変数 -> http://localhost:8000
    # の順で解決される。明示する場合は connect("http://paprika.lan") など。
    async with async_paprika.connect() as cli:
        async with cli.session(initial_url="https://example.com") as page:
            await page.wait_for(seconds=2)
            await page.capture("step-1")
            for url in await page.links(urls_only=True):
                print(url)

asyncio.run(main())
```

[マニュアル §7 SDK](https://paps-jp.github.io/paprika/api.html) と [§8 LLM 使用例](https://paps-jp.github.io/paprika/guides.html#llm) に多数の実例。

---

## アーキテクチャ

```
   Operator / SDK
        │ HTTP / WS
        ▼
   ┌─────────────────┐         ┌──────────────────┐
   │  paprika-hub    │ ◀────── │  Agent service   │
   │  FastAPI+Redis  │         │  (Qwen-VL / R1)  │
   └──────┬──────┬───┘         └──────────────────┘
   WS link│      │ docker spawn
          ▼      ▼
   ┌──────────┐ ┌────────────────┐
   │ Workers  │ │ Runners (使い  │
   │ (LAN×N)  │ │ 捨て sandbox)  │
   │ Xvfb +   │ │ codegen/rerun  │
   │ Chrome + │ └────────────────┘
   │ noVNC    │
   └──────────┘
```

- **paprika-hub** — 中央サーバ。ジョブキュー + worker WS + 管理 UI
- **paprika-worker** — Lane (Xvfb + Chrome + noVNC) を抱える実行ホスト
- **paprika-runner** — codegen-loop / rerun の sandbox。Hub が docker socket 経由で spawn → 終了で削除
- **paprika-client** — Playwright 互換 Python SDK

詳細は [マニュアル §2 アーキテクチャ](https://paps-jp.github.io/paprika/operations.html#arch)。

---

## ディレクトリ構成

```
paprika/
├── core/                     共通ライブラリ (fetcher logic)
├── server/
│   ├── hub/                  paprika-hub (FastAPI)
│   ├── worker/               paprika-worker (CDP / browser ops)
│   ├── protocol.py           hub ↔ worker WS プロトコル (Pydantic)
│   └── scheduler.py          worker registry + dispatch
├── client/python/            paprika-client SDK
├── agent_service/            LLM proxy (Qwen-VL via OpenAI-compatible API)
├── data/tools/               Plugin registry (catalog.json + installed/)
├── docker/                   Dockerfile 群 (hub / worker / runner)
├── docs/                     マニュアル (docs/index.html)
├── scripts/                  運用ヘルパー (deploy / sync / git-pull-workers)
├── docker-compose.yml        Hub 同居構成
├── docker-compose-worker.yml Worker 単体構成
└── .env.example              環境変数テンプレート
```

---

## CLI でも使える

API を介さず CLI で 1 URL ずつ実行することもできます:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# HTML + 全 assets を取得
python fetch_html.py https://example.com -o page.html -a assets/

# 無限スクロール + 動画再生も
python fetch_html.py https://example.com -o page.html -a assets/ \
  --scroll --play-videos --max-wait 180
```

主要オプション:

| フラグ | 説明 |
|---|---|
| `-o`, `--output` | HTML 出力先 (デフォルト stdout) |
| `-a`, `--assets` | 画像/動画/音声の保存先ディレクトリ |
| `--scroll` | lazy-load 発火用にスクロール |
| `--play-videos` | `<video>` を自動再生 (.ts セグメント取得) |
| `--cookies-from BROWSER` | yt-dlp に `--cookies-from-browser` を渡す |
| `--clone-chrome-profile [NAME]` | Chrome プロファイルを一時 dir にコピー (Cookie 引き継ぎ) |
| `--max-wait SEC` | アセット待ちのタイムアウト |
| `--headless` | ヘッドレス実行 (検出回避 OFF) |

`-a` 指定時は **HLS / DASH ストリーム URL を自動検出 → yt-dlp に流す**ところまでやります。

---

## 設定

すべて `.env` 経由。`.env.example` をコピーして編集。

主な変数:

| 変数 | 用途 | デフォルト |
|---|---|---|
| `HUB_URL` | Worker → Hub の WS URL | `ws://paprika.lan:8000` |
| `WORKER_SECRET` | Worker WS 認証 | (空) |
| `LANE_POOL` | Lane (Chrome) 数 | `2` |
| `NOVNC_PUBLIC_HOST` | Admin UI 用 noVNC ホスト | (空、Hub が自動補正) |
| `AGENT_LLM_URL` | Qwen 等のテキスト LLM | `http://agent:8001` |
| `DEEPSEEK_API_KEY` | DeepSeek-R1 (judge / distiller) | (空) |

全変数は [マニュアル §12 環境変数一覧](https://paps-jp.github.io/paprika/operations.html#env) を参照。

---

## ドキュメント

| 内容 | URL |
|---|---|
| 公式マニュアル (推奨) | <https://paps-jp.github.io/paprika/> (htmlpreview 経由でも閲覧可) |
| API リファレンス | [マニュアル §6](https://paps-jp.github.io/paprika/api.html) |
| 環境変数一覧 | [マニュアル §12](https://paps-jp.github.io/paprika/operations.html#env) |
| Swagger UI | `http://<hub-host>:8000/docs` (実行時) |

---

## ライセンス

[PolyForm Noncommercial License 1.0.0](LICENSE).

**個人利用・研究・教育・非営利組織での利用は自由**。商用利用は許諾されない
(商業目的で使いたい場合は別途連絡を)。詳細はライセンス本文を参照。
