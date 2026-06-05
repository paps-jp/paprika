---
layout: doc
title: アーキテクチャ概要
description: Paprika の全体構成と仕組み — Client/SDK → Router(nginx) → Hub → Worker → Lane(Chrome) のリクエストの流れ、3 つのストア(DB / オブジェクト / Redis)、ジョブの実行経路を図解。設計ドキュメントのリンク集。
active: architecture
---

Paprika は「**分散したワーカー上の Chrome を、API / SDK / AI から動かしてページの画像・動画・リンクを集める**」プラットフォームです。構成要素は 5 つだけです。

<img class="shot" src="img/admin-submit.png" alt="管理画面の Submit タブ — Client の入口の 1 つ">
<p class="shot-cap">クライアント面（管理画面の <strong>実行</strong> タブ）。ここから投入されたジョブが、下の図の Hub → Worker → Lane を経て返ってきます。</p>

<svg viewBox="0 0 860 380" role="img" aria-label="Paprika 全体構成図" style="display:block;max-width:100%;height:auto;margin:18px auto;font-family:ui-monospace,Consolas,monospace;">
  <defs>
    <marker id="ah" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" fill="#59636e"/></marker>
  </defs>
  <rect x="320" y="14" width="220" height="44" rx="9" fill="#eef1f5" stroke="#cdd6e0"/>
  <text x="430" y="41" text-anchor="middle" font-size="13">Client / SDK / curl</text>
  <line x1="430" y1="58" x2="430" y2="90" stroke="#59636e" stroke-width="1.6" marker-end="url(#ah)"/>
  <text x="442" y="78" font-size="10.5" fill="#59636e">POST /jobs ・ GET /jobs/{id} ・ /sessions/*</text>
  <rect x="320" y="90" width="220" height="54" rx="9" fill="#fff7e6" stroke="#f0d493"/>
  <text x="430" y="113" text-anchor="middle" font-size="13" font-weight="700">Router（nginx）</text>
  <text x="430" y="131" text-anchor="middle" font-size="10.5" fill="#8a6d00">複数 Hub のときだけ前段に立つ</text>
  <line x1="430" y1="144" x2="430" y2="176" stroke="#59636e" stroke-width="1.6" marker-end="url(#ah)"/>
  <rect x="320" y="176" width="220" height="80" rx="10" fill="#fdeaea" stroke="#e8a4a0"/>
  <text x="430" y="200" text-anchor="middle" font-size="13.5" font-weight="700" fill="#b1322c">Hub</text>
  <text x="430" y="220" text-anchor="middle" font-size="10.5" fill="#7a3b38">ジョブ受付・ディスパッチ</text>
  <text x="430" y="236" text-anchor="middle" font-size="10.5" fill="#7a3b38">セッション登録（Chrome は持たない）</text>
  <rect x="600" y="166" width="244" height="124" rx="10" fill="#ecf7e9" stroke="#99c98a"/>
  <text x="722" y="186" text-anchor="middle" font-size="12.5" font-weight="700" fill="#2a7a2a">Worker（別ホスト × 多数）</text>
  <rect x="616" y="200" width="212" height="76" rx="8" fill="#fff" stroke="#99c98a"/>
  <text x="722" y="220" text-anchor="middle" font-size="11.5" font-weight="700">Lane（並列 N 本）</text>
  <text x="722" y="238" text-anchor="middle" font-size="10.5" fill="#3a6">Xvfb + Chrome (CDP)</text>
  <text x="722" y="254" text-anchor="middle" font-size="10.5" fill="#3a6">x11vnc + noVNC</text>
  <text x="722" y="270" text-anchor="middle" font-size="9.5" fill="#888">= 画面を持つ本物の Chrome</text>
  <line x1="540" y1="212" x2="598" y2="212" stroke="#0969da" stroke-width="1.6" marker-end="url(#ah)"/>
  <line x1="598" y1="226" x2="540" y2="226" stroke="#0969da" stroke-width="1.6" marker-end="url(#ah)"/>
  <text x="569" y="206" text-anchor="middle" font-size="9.5" fill="#0969da">WebSocket</text>
  <text x="569" y="240" text-anchor="middle" font-size="9.5" fill="#0969da">assign / 結果</text>
  <line x1="430" y1="256" x2="430" y2="298" stroke="#59636e" stroke-width="1.6" marker-end="url(#ah)"/>
  <text x="442" y="282" font-size="10.5" fill="#59636e">保存</text>
  <rect x="300" y="298" width="260" height="48" rx="9" fill="#eef1f5" stroke="#cdd6e0"/>
  <text x="430" y="320" text-anchor="middle" font-size="12">ストア</text>
  <text x="430" y="337" text-anchor="middle" font-size="10.5" fill="#59636e">DB ・ オブジェクトストレージ ・ Redis</text>
</svg>

## 設計ドキュメント（リンク集）

Paprika の内部構造を、テーマ別の個別ページで解説しています。

| ページ | 内容 |
|---|---|
| [Hub の仕組み](architecture-hub.html) | ジョブのディスパッチ（空き Worker の選定・`503` 背圧）、`fetch` / `codegen-loop` / `rerun` の 3 モード、セッションレジストリ、Worker の登録と心拍、ストアとの関係 |
| [Worker・Lane・Chrome の仕組み](architecture-worker.html) | Lane プール、1 本の Lane の中身（Xvfb + Chrome(CDP) + x11vnc + noVNC とポート割当）、CDP 操作と noVNC 閲覧の違い、ジョブとセッション、自己回復・自己更新 |
| [Vision AI とマウス](vision-mouse.html) | 視覚エージェント（CogAgent / Qwen-VL）がスクリーンショットを見てピクセル座標でクリック・操作するしくみ |
| [VNC 埋め込み](vnc-embed.html) | hub-proxy の noVNC ライブ画面を、自前の Web ページに `iframe` で埋め込む実装 |
| [Hub スケーリング](scaling.html) | 複数 Hub に水平スケールするときの考え方とルーティング |
| [Worker 自己回復](worker-resilience.html) | Worker が壊れたら自分で気づいて作り直す自己回復ループ |

## 5 つの構成要素

| 要素 | 役割 |
|---|---|
| **Client / SDK** | あなたのコード。Python / PHP SDK、`curl`、または管理画面からジョブを投入します（[HTTP API](http-api.html)）。 |
| **Router（nginx）** | **複数 Hub 構成のときだけ**前段に立ち、リクエストを各 Hub に振り分けます。単一 Hub なら不要で、Client は Hub に直接話します。 |
| **Hub** | 司令塔。ジョブを受け付け、空いている Worker に割り当て（ディスパッチ）、セッションを登録し、結果をストアに保存します。Chrome は持ちません。→ [Hub の仕組み](architecture-hub.html) |
| **Worker** | 実際にブラウザを動かすホスト（多数）。起動時に **Lane** を N 本立ち上げ、Hub から WebSocket でジョブを受け取って実行します。→ [Worker・Lane の仕組み](architecture-worker.html) |
| **Lane（Chrome）** | 1 本 = 並列実行の 1 トラック。専用の Xvfb 画面 + Chrome + noVNC を持つ**長命のブラウザ**。クッキー/ログイン状態を保ったままジョブが通過します。 |

## リクエストの流れ（Fetch ジョブの例）

1. **投入** — Client が `POST /jobs`。(複数 Hub なら) Router がいずれかの Hub に振る。
2. **ディスパッチ** — Hub が空き Worker を選び、WebSocket で割り当てる（満杯なら `503`）。
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
