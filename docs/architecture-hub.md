---
layout: doc
title: Hub の仕組み
description: Paprika の Hub の内部 — ジョブのディスパッチ(空き Worker への割り当て)、3 つのジョブモード(fetch / codegen-loop / rerun)、セッションレジストリ、Worker の登録と心拍、ストアとの関係を解説。
active: architecture-hub
---

Hub は司令塔です。**Chrome は持ちません** — クライアント API・Worker の WebSocket・管理画面を束ね、ジョブを Worker に割り当て、結果をストアに保存します。全体像は [アーキテクチャ概要](architecture.html) を参照。

<img class="shot" src="img/cap-jobs.png" alt="管理画面のジョブ一覧 — Hub が受け付けたジョブの状態が一覧表示">
<p class="shot-cap">Hub が受け付けたジョブは <strong>最近のジョブ</strong> タブで一覧できます。状態・ワーカー・所要時間・取得物数を一目で確認。</p>

## ジョブのディスパッチ

```text
 POST /jobs
   │  ① URL 検査・JobInfo を queued で保存
   ▼
 空き Worker を選ぶ (pick_worker)
   │  満杯なら数秒待つ → それでも無ければ 503
   ▼
 WebSocket で HubAssignJob を送信 ──▶ Worker が Lane を確保して実行
   │
   ▼  worker_id・noVNC URL を JobInfo に記録 → 返却
```

- Hub は **Chrome を持たない**ので、空き Worker が無ければ在庫待ちせず **`503`（fleet at capacity）** を返します。クライアントはバックオフ再試行を（[FAQ](faq.html)）。
- `pick_worker` は WebSocket 接続中で空き容量のある Worker を選びます。`docker compose restart hub` 直後など一瞬 Worker が居ない時間帯のために、数秒の **猶予ウィンドウ**を設けて待ってから 503 にします。

## 3 つのジョブモード

| モード | Hub が何をするか |
|---|---|
| **fetch** | その場で Worker にディスパッチ。Worker の取得エンジン（nodriver）がページを開いて画像/動画/リンクを収集。レシピがあれば適用。 |
| **codegen-loop**（AI） | **Hub 自身がループを回す** — LLM がスクリプトを生成 → サンドボックスで実行 → 失敗なら再生成。生成スクリプトは Hub の `/sessions/*` に接続して Worker の Lane を駆動。→ 下記 |
| **rerun**（Code） | 保存済み / インラインのスクリプトを LLM 抜きで実行（codegen-loop と同じ実行経路）。 |

fetch は同期的にディスパッチして即返し、codegen-loop / rerun は Hub 内の非同期タスクとして起動して即 `job_id` を返します（だから投入は速い）。

## codegen-loop（AI モード）{#codegen-loop}

```text
 goal（自然言語）
   │
   ▼
 ┌─ planner(LLM) → スクリプト生成
 │      ▼
 │  サンドボックスで実行 ──▶ /sessions/* ──▶ Worker の Lane(Chrome)
 │      ▼
 └─ 失敗なら judge(LLM) で原因分析 → 再生成（max_codegen_attempts まで）
```

Hub がブラウザを直接触るのではなく、**生成されたスクリプトが SDK と同じ経路（`/sessions/*`）で Worker の Lane を操作**します。これにより AI モードも手書きスクリプトも同じ実行基盤に乗ります。

## セッションレジストリ

「セッション」は **Lane の予約**です。`session_id → (worker, lane)` を Hub が管理し、`/sessions/{sid}/click` などの操作を**所有 Worker の Lane へ WebSocket で転送**します。

- セッションは、keep_session な fetch・codegen-loop・SDK の `Page` / `Session` などが開きます。
- **複数 Hub**ではセッションのライブ状態は所有 Hub にしか無いので、別 Hub に届いたリクエストは Redis の **セッションマップ（sid → 所有 Hub）** を引いて所有 Hub へ転送します。

## Worker の登録と心拍

Worker は Hub の `/workers/{id}/link` に WebSocket でつなぎ、**capabilities**（Lane 数・各 Lane の noVNC URL・バージョンハッシュなど）を送って登録します。以後 **約 15 秒ごとに心拍**を送り、`in_flight`（実行中ジョブ数）を更新します。

- バージョンハッシュが Hub の配布版と食い違うと、Worker は **ソースを取得して自己更新**します（[Worker の仕組み](architecture-worker.html#self-update)）。
- 心拍が途絶えた Worker は Hub が一覧から自動で掃除（reap）します。

## ストアとの関係

- **JobInfo** は DB に保存（複数 Hub で共有）。`GET /jobs/{id}` はどの Hub からでも一貫して見えます。
- **アセット**は Worker から Hub にアップロードされ、オブジェクトストレージ（MinIO/S3）に保存。
- **Redis** はワーカー登録・セッションマップ・リース・ライブプレビューのフレームなどの揮発状態を共有。

> スケール時の挙動は [Hub スケーリング](scaling.html)、構築は [サーバー構成](operations.html)。

---

次へ: [Worker・Lane・Chrome の仕組み](architecture-worker.html) / 戻る: [アーキテクチャ概要](architecture.html)
