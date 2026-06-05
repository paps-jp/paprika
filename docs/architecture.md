---
layout: doc
title: アーキテクチャ概要
description: Paprika の全体構成と仕組み — Client/SDK → Router(nginx) → Hub → Worker → Lane(Chrome) のリクエストの流れ、3 つのストア(DB / オブジェクト / Redis)、ジョブの実行経路を図解。
active: architecture
---

Paprika は「**分散したワーカー上の Chrome を、API / SDK / AI から動かしてページの画像・動画・リンクを集める**」プラットフォームです。構成要素は 5 つだけです。

```text
   Client / SDK / curl
        │   POST /jobs ・ GET /jobs/{id} ・ /sessions/* ・ 管理画面
        ▼
   ┌──────────────┐    ← 複数 Hub のときだけ前段に立つ
   │  Router       │      nginx: リクエストを各 Hub へ振り分け
   │  (nginx)      │
   └──────┬───────┘
          ▼
   ┌──────────────┐          ┌────────────────────────────┐
   │    Hub        │◀── WS ──▶│   Worker（別ホスト × 多数）     │
   │  ジョブ受付      │  assign  │   Lane プール(N 本)            │
   │  ディスパッチ    │  job     │   ┌─────────────────────┐   │
   │  セッション登録   │          │   │ Lane i               │   │
   └──────┬───────┘          │   │  Xvfb + Chrome (CDP)  │   │
          │ 保存                │   │  x11vnc + noVNC       │   │
          ▼                    │   └─────────────────────┘   │
   ストア(DB / オブジェクト / Redis)  └────────────────────────────┘
```

## 5 つの構成要素

| 要素 | 役割 |
|---|---|
| **Client / SDK** | あなたのコード。Python / PHP SDK、`curl`、または管理画面からジョブを投入します（[HTTP API](http-api.html)）。 |
| **Router（nginx）** | **複数 Hub 構成のときだけ**前段に立ち、リクエストを各 Hub に振り分けます。単一 Hub なら不要で、Client は Hub に直接話します。→ 詳細は下記 |
| **Hub** | 司令塔。ジョブを受け付け、空いている Worker に割り当て（ディスパッチ）、セッションを登録し、結果をストアに保存します。Chrome は持ちません。→ [Hub の仕組み](architecture-hub.html) |
| **Worker** | 実際にブラウザを動かすホスト（多数）。起動時に **Lane** を N 本立ち上げ、Hub から WebSocket でジョブを受け取って実行します。→ [Worker・Lane の仕組み](architecture-worker.html) |
| **Lane（Chrome）** | 1 本 = 並列実行の 1 トラック。専用の Xvfb 画面 + Chrome + noVNC を持つ**長命のブラウザ**。クッキー/ログイン状態を保ったままジョブが通過します。 |

## リクエストの流れ（Fetch ジョブの例）

1. **投入** — Client が `POST /jobs`。(複数 Hub なら) Router がいずれかの Hub に振る。
2. **ディスパッチ** — Hub が空き Worker を選び、WebSocket で `HubAssignJob` を送る（満杯なら `503`）。
3. **Lane 確保** — Worker が空き Lane を 1 本確保し、その Chrome でページを開く。
4. **取得** — スクロール・待機しながら画像/動画/リンクを収集（動画は通信トレース + yt-dlp、[動画の仕組み](video.html)）。
5. **保存** — 集めたアセットを Hub に **アップロード** → ストアへ保存。
6. **取得** — Client は `GET /jobs/{id}` で完了を待ち、`assets.json` から結果を取る。

AI モード（`codegen-loop`）では、Hub 自身が「LLM がスクリプトを生成 → サンドボックスで実行 → 失敗時に再生成」のループを回し、生成スクリプトが Hub の `/sessions/*` に接続して Worker の Lane を駆動します（[Hub の仕組み](architecture-hub.html#codegen-loop)）。

## 3 つのストア

単一 Hub では Hub のローカルディスク + 任意の DB で完結します。**複数 Hub** では状態を共有するため 3 つに分かれます:

| ストア | 持つもの |
|---|---|
| **DB（MariaDB 等）** | ジョブ情報、各種レジストリ（ホスト設定・プリセット・スキル等） |
| **オブジェクトストレージ（MinIO / S3）** | 収集アセット（画像・動画・HTML）の本体 |
| **Redis** | 協調用の揮発状態 — ワーカー登録、セッションマップ（sid→所有 Hub）、リース、ライブプレビューのフレーム |

これにより **Hub は水平スケール可能（クローン安全）** で、どの Hub に当たっても同じジョブ・アセットが見えます。

## Router（nginx）と複数 Hub

Worker の制御 WebSocket（`/workers/{id}/link`）だけは **worker_id のハッシュで特定の Hub に固定**（sticky）。それ以外のリクエストは各 Hub に**ラウンドロビン**で分散します。

- ワーカー登録・ジョブ情報・アセットは共有ストアにあるので、どの Hub でも一貫して見えます。
- セッションのライブ状態（noVNC など）は所有 Hub にあるため、所有 Hub へ**転送**して整合させます。

> スケールの考え方は [Hub スケーリング](scaling.html)、サーバー構成は [サーバー構成](operations.html) も参照。

---

次へ: [Hub の仕組み](architecture-hub.html) / [Worker・Lane・Chrome の仕組み](architecture-worker.html)
