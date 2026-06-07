---
layout: doc
title: ジョブ分配と負荷分散
description: Paprika の Hub・Worker 間のネットワーク負荷分散の実装。pick_worker のアルゴリズム、キュー再駆動ループ、Stale job reconciler、Multi-hub での sticky ルーティングと Redis 協調、ハートビート設計まで、実コードに基づいて解説。
active: dispatch
---

Paprika の負荷分散は **3 つのレイヤ** で動いています:
クライアントから Hub までの **ネットワーク経路**、Hub から Worker への **ジョブ分配**、そして **失敗時の回復** です。

<div class="tldr">
<span class="tldr-label">概要</span>
<ul>
<li><strong>nginx 段</strong>: Worker の制御 WS だけ <code>worker_id</code> でハッシュ sticky、それ以外は round-robin。複数 Hub 間で WebSocket 接続だけ固定する。</li>
<li><strong>Hub 段</strong>: <code>pick_worker()</code> = (in_flight 昇順, capacity 降順) で並べて <strong>同点はランダム</strong>。「最初の 1 台にだけ偏る」を防ぐ。</li>
<li><strong>キュー再駆動ループ</strong>: 3 秒ごとに各 Hub が queued ジョブを CAS で奪い合い、空き Lane に置き直す。POST 時の一度きりディスパッチが取りこぼした分を救済。</li>
<li><strong>多層フェイルセーフ</strong>: POST inline → redrive (3 秒) → reconciler (90 秒) → 180 秒のリーパー の 4 段。WS 切断・自己更新・再接続を耐える。</li>
</ul>
</div>

> このページは内部実装の解説です。Paprika の <em>使い方</em> としては意識する必要はなく、SDK は 503 を自動リトライ、管理画面はそのまま使えます。仕組みを知りたい運用者向けです。設計の全体像は <a href="architecture.html">アーキテクチャ概要</a>。

## 全体像: 3 つのレイヤ

```text
クライアント
   │  HTTP / WS (POST /jobs ・ GET /jobs/{id} ・ /sessions/*)
   ▼
nginx (複数 Hub 構成時のみ)
   │  ルール 1: /workers/{id}/link → hash $worker_id consistent (sticky)
   │  ルール 2: それ以外            → round-robin
   ▼
Hub × N (ステートレス) ────── Redis (協調) ────── DB (job 情報) ── MinIO (アセット)
   │  pick_worker() で空き Worker 選定
   │  HubAssignJob を WebSocket で送る
   ▼
Worker × N
   ├─ Lane プール (Chrome × M 並列)
   └─ 15 秒ごと WorkerHeartbeat (in_flight + CPU/Mem/Disk)
```

| レイヤ | 目的 | 実装 |
|---|---|---|
| **ネットワーク** | リクエストを正しい Hub へ | nginx の **2 種類のルール** (sticky / round-robin) |
| **Hub のディスパッチ** | 空き Worker を選んで割り当て | `pick_worker()` + `assign()` |
| **失敗回復** | 取りこぼし・切断・再起動を耐える | redrive 3s / reconciler 90s / reaper 180s |

---

## レイヤ 1: ネットワーク経路 (nginx)

### sticky と round-robin の使い分け

複数 Hub 構成では nginx が前段に立ち、リクエストを 2 種類のルールで振り分けます。

| パス | ルール | 理由 |
|---|---|---|
| `/workers/{worker_id}/link` (WebSocket) | **`hash $worker_id consistent`** で sticky | Worker の制御 WS は **1 本** だけ。Hub をまたぐと WS の状態 (in_flight, send_lock, pending_screenshots) を分裂させてしまう |
| それ以外 (REST, 管理画面, /jobs, /sessions/*) | **round-robin** | Job 情報は MariaDB、アセットは MinIO、Worker レジストリは Redis にあるので、どの Hub に当たっても同じ答えを返せる |

### Worker_id の安定化

Sticky ハッシュ ($worker_id) は **入力が安定** していて初めて有効です。Paprika は **Worker の LAN IP から決定的に worker_id を導出** します:

```text
LAN IP 10.10.50.150  →  worker_id = w50150
LAN IP 10.10.50.151  →  worker_id = w50151
```

(コード: `server/hub/routes/workers.py::_ip_derived_worker_id`)

- 同じ IP は **再起動 / 自己更新 / id ファイル消失** をまたいで同じ id を持つ → Hub への再ホームが起きない → ハッシュ環の振り分けが安定
- /16 をまたぐ衝突 (10.10.5.150 と 10.10.51.50 が両方 `w50150` を欲しがる稀ケース) は full IP 形 `w10-10-5-150` にフォールバック

これがなかった頃 (`<hostname>-<rand4>` 形式) は、Worker が自己更新するたびに新しい id を取得 → ハッシュリングの担当 Hub が毎回ジャンプ → 一部 Hub に偏る、というドリフトが起きていました。

### セッションの転送

Round-robin で来たリクエストが「自分が持っていないセッション」の場合、Hub は Redis のセッションマップ (`sid → 所有 Hub`) を引いて **所有 Hub へ HTTP で転送** します。クライアントから見るとどの Hub に当たっても同じレスポンスです。

---

## レイヤ 2: Hub の `pick_worker()` ロジック

### 候補のフィルタ条件

```python
candidates = [
    w for w in alive_workers()              # 直近 120 秒以内に心拍があった
    if w.status == "active"                  # operator が drain / standby にしていない
    and w.in_flight < w.capabilities.max_concurrent   # まだ空きがある
    and (
        len(w.capabilities.lane_novnc_urls or []) > 0  # Lane プール有り
        or bool(w.capabilities.novnc_url)              # 単 Chrome (Windows portable)
    )
    and w.disk_pct < 90.0                    # ディスク逼迫 (>90%) はスキップ
]
```

(コード: `server/scheduler.py::pick_worker`)

各フィルタの実害は具体的:

- **Lane URLs 無しの Worker をスキップ** していなかった頃: ジョブが "no free lane" で連続 3 回 502 になる事故が起きていました (実例: コミット履歴 `71ec64da63c5`)
- **`disk_pct < 90` ゲート** は、Worker 側の preflight が disk full でジョブを拒絶する前に Hub 側で先に弾いて、ディスパッチ往復と `WorkerJobFailed` ハンドシェイクを省く

### ランキング基準

```python
def _key(w):
    return (w.in_flight, -w.capabilities.max_concurrent)

best  = min(_key(w) for w in candidates)
tied  = [w for w in candidates if _key(w) == best]
return random.choice(tied)        # ★同点はランダム
```

- **第 1 キー: `in_flight` 昇順** — 今一番暇な Worker を優先
- **第 2 キー: `capacity` 降順** — 同じ暇さなら、より大きい (= 並列で耐えられる) Worker
- **第 3 キー: ランダム** — 上の 2 つで同点になった候補からランダム選択

### なぜランダム同点解消が必要か

`in_flight = 0` の暇な Worker が複数いるとき、決定的に並べると **辞書順で最初の Worker** にだけ毎回ヒットしてしまいます。インタラクティブ用途 (Fetch / LLM / Macro を 1 つずつ叩く) では `in_flight` が 0 まで戻るので、**「特定 Worker だけ酷使される」** という運用者の苦情として顕在化していました。`random.choice(tied)` でアイドル群に均等に振り分けて解消しています。

### 割り当ての送信

```python
async def assign(self, worker, msg):
    await worker.send(msg)        # WebSocket で HubAssignJob 送信
    worker.in_flight += 1          # 即座に in_flight をバンプ
    return True
```

送信成功時に `in_flight` をその場でバンプするので、**同一パス内で続けて `pick_worker()` を呼んでも同じ Worker が選ばれない** = redrive ループ内のループでも正しく散らばります (キャパシティ正確性)。

---

## レイヤ 3: Worker レジストリ (Redis スキーマ)

各 Hub プロセスは自分が持っている WS だけを `connections: dict` で覚えています。フリート全体の状態は **Redis を介して共有** します:

| キー | 型 | TTL | 内容 |
|---|---|---|---|
| `paprika:workers` | Sorted Set | (無し) | worker_id → 最終ハートビート時刻 |
| `paprika:worker:{id}` | String (JSON) | (無し) | capabilities + アドレス + 所属 Hub + リソース情報 |
| `paprika:worker:{id}:online` | String | **120 秒** | 心拍が止まれば自動失効 = dead 判定 |
| `paprika:worker:{id}:owner` | String | **120 秒** | この Worker の WS を持っている Hub_id |

(コード: `server/scheduler.py` の `_k_index / _k_worker / _k_online / _k_owner`)

**心拍は 15 秒ごと**、TTL は **120 秒** (12 心拍ぶんの余裕)。yt-dlp サブプロセスや大きな Python 計算で event loop が一瞬詰まる程度では false-positive で「dead」にならない設計です。

### `:owner` が必要な理由

複数 Hub 構成で、Round-robin で hub-A に届いたセッション操作リクエストが、実は **hub-B が WS を持っている** Worker 宛だったとき、hub-A は `:owner` を引いて hub-B へ HTTP で転送します。Single-hub 運用では書かれるが読まれない (dormant) 値です。

### Owner のアトミックな手放し

Worker の WS が hub-A → hub-B に切り替わる瞬間、hub-A の unregister が遅延すると hub-B が書いた `:owner = hub-B` を hub-A の delete が上書きしてしまう競合があります。これを **Lua スクリプトの compare-and-delete** で防いでいます:

```lua
if redis.call('get', KEYS[1]) == ARGV[1]
  then return redis.call('del', KEYS[1])
  else return 0
end
```

「自分が書いた owner だったときだけ delete」が原子的に走るので、hub-B の有効な ownership は誤って消されません。

---

## レイヤ 4: キュー再駆動ループ (redrive)

POST /jobs は **1 回だけ** Worker への割り当てを試みます。クライアントが瞬断したり、その 8 秒間どの Lane も空かなかったりすると、ジョブは `status=queued` のまま残ります。180 秒の reaper がこれを「タイムアウト」で殺すまでに **空いた Lane が出ても再ディスパッチしない** 問題が 2026-06-06 に発生しました (**ジョブ失敗の 80% がこれだった**)。

これを解決するのが **redrive ループ** (`server/hub/_redrive.py`):

```text
3 秒ごと (= PAPRIKA_QUEUE_REDRIVE_INTERVAL_S)
  ├─ 自分の Hub に空き Lane が 1 つでもあるか? (pick_worker() 即返り)
  │     ↓ なし → 何もしない
  │     ↓ あり
  ├─ 全 queued ジョブを古い順に取得
  ├─ 90 秒未満のもの (= POST がまだディスパッチ中の可能性) はスキップ
  └─ 各ジョブについて:
       ├─ DB の CAS UPDATE: status=queued AND worker_id IS NULL → running + worker_id
       │      ↓ 負け → 別 Hub / 元 POST が掴んだ。次へ
       │      ↓ 勝ち
       ├─ HubAssignJob を WS 送信
       │      ↓ 失敗 → CAS を巻き戻して次のパスで再挑戦
       │      ↓ 成功
       └─ DB に running 状態を永続化 (novnc_url + session_id 込み)
```

### 二重ディスパッチを防ぐ 3 重ガード

1. **CAS Mutex** — DB の `UPDATE WHERE status='queued'` が原子的に走るので、**ただ 1 つの Hub** だけが勝つ。負けた Hub は静かにスキップ。
2. **`worker_id IS NULL` ガード** — POST が既に Worker に渡してから WS 送信に失敗したケース (worker_id だけ書かれて queued に巻き戻った) を redrive が拾わないようにする。
3. **90 秒の年齢ゲート** — POST の最悪滞留時間 (8 秒ディスパッチ猶予 + 60 秒クロス Hub 転送 + マージン) を超えるまで触らない。「まだ POST 側がやっているかも」の不確実性を消す。

### Kill switch

`PAPRIKA_QUEUE_REDRIVE_DISABLE=1` で完全停止 → 旧 POST inline-only 動作に戻る。ハードな運用変更時の緊急ロールバック用。

---

## レイヤ 5: Stale Job Reconciler

POST → redrive で取りこぼされた、より長期的な状態不整合を settle するのが **stale job reconciler** (`server/hub/_reaper.py::_stale_job_reconciler_loop`):

```text
90 秒ごと
  ├─ stats_async() で フリート全体 の alive worker 集合を取得
  │     (= local connections + Redis-known peers の和集合)
  │
  ├─ 実行中ジョブを全件スキャン
  │   └─ worker_id が alive 集合に居ない && 300 秒以上経過 → failed として settle
  │      (300 秒は「Worker が自己更新で一瞬切れて同じ id で戻ってくる」の窓を許す)
  │
  └─ queued ジョブを全件スキャン
      └─ 180 秒以上 queued && worker_id 未割り当て → failed (queued timeout)
```

### なぜ「fleet-wide」なのか

決定的に重要な設計点です。**この Hub の `state.registry.connections` だけを見て alive 判定すると失敗します**:

- nginx ハッシュ sticky の影響で、Worker は 3 Hub にだいたい 1:1:1 で分散
- → 各 Hub の `connections` には **フリート全体の 1/3** しか居ない
- → 「自分の connections に居ない = dead」と判定すると **健全な peer 所有ジョブを大量失敗させる**

`stats_async()` は local connections と Redis 上の peer 心拍を merge した「フリート全体ビュー」を返します。これを使うのが reconciler の正しさの肝です。

### `_last_known_extra` キャッシュ (60 秒 TTL)

`stats_async()` 内で Redis 取得が 1.5 秒タイムアウトすると、フォールバックとして **直前の GOOD な集約を 60 秒間まで再利用** します。これがないと、Redis が瞬間的に遅れた拍子に「ローカル接続だけ」に縮退して、reconciler が大量誤判定する事故が起きていました (admin UI の Workers 行数が 37 ↔ 30 ↔ 7 ↔ 0 で flap した症状)。

---

## レイヤ 6: 多層フェイルセーフ

ジョブの生死は **4 段階** のセーフティネットで守られています:

| # | 機構 | 周期 | 救う対象 |
|---|---|---|---|
| **1** | POST inline dispatch | 即時 | 通常時の 99% は ここで完結 |
| **2** | **redrive ループ** | 3 秒 | POST が取りこぼした queued (空き Lane あり) |
| **3** | **stale reconciler** | 90 秒 | Worker が消えた running、180 秒超えの queued |
| **4** | Queue reaper | 180 秒 | 最終的なタイムアウト (failed として閉じる) |

「ローカル recovery → cluster reconcile → 強制クローズ」の 3 段で、**ジョブが永遠に running / queued で取り残されることが原理的に無い** ように作られています。

---

## Hub レジストリ (Multi-hub 自動連携)

Worker と同じく、Hub も Redis に自己心拍を書きます (`server/hub/_hubs.py`):

| キー | TTL | 書き込み頻度 |
|---|---|---|
| `paprika:hubs:{hub_id}` | **90 秒** | 30 秒ごと (= TTL の 1/3) |
| `paprika:hubs:index` (ZSET) | 永続 | hub_id ごとの初出時刻 |

これにより:

- 新規 Hub が `REDIS_URL` を共有するだけで自動的に admin UI のリストに現れる (手動登録不要)
- TTL = 90 秒なので dead Hub は 1 分強で消える
- `paprika:hubs:index` は「ZSET に居るが live row が無い = 最近落ちた Hub」を可視化

### Multi-hub での recovery safety

Hub 起動時の "orphan running jobs を fail にする" 処理は、**alive な peer Hub が居る場合は実行しません**:

```python
peers = [h for h in await state.hubs.list_all()
         if h.get("alive") and not h.get("local")]
if peers:
    return  # peer が running を持っている可能性 → blanket-fail しない
```

クローン VM で新 Hub を立ち上げたときに、既存 Hub が走らせていたジョブを「自分の orchestrator は持ってない」と誤判定して全部 fail にする事故を防ぎます。

---

## ハートビート設計まとめ

| 主体 → 先 | 周期 | TTL | 用途 |
|---|---|---|---|
| Worker → Hub (WS) | **15 秒** | — | in_flight + CPU/Mem/Disk + プロファイルキャッシュ |
| Hub → Redis (worker key refresh) | 心拍受信時 | **120 秒** | 死活判定 (`alive`) |
| Hub → Redis (own `paprika:hubs:{id}`) | **30 秒** | **90 秒** | 複数 Hub の自動発見 |
| Worker self-watchdog (loop wedge) | 30 秒チェック | **300 秒 (+ジッタ)** | event loop が完全に止まった検知 |
| Worker self-watchdog (inbound liveness) | 30 秒チェック | **600 秒 (+ジッタ)** | ghost proxied WS の検知 |

**ジッタ** はフリート全体が「同じ閾値」で同時に exit するのを防ぐためのランダムオフセット (0-60 秒)。デプロイ直後の一斉再起動でも雪崩が起きないようにする。

---

## 主要パラメータ一覧

実運用で触る可能性のある環境変数とその意味:

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `PAPRIKA_QUEUE_REDRIVE_INTERVAL_S` | `3.0` | redrive ループの周期 (秒) |
| `PAPRIKA_QUEUE_REDRIVE_MIN_AGE_S` | `90.0` | redrive が触れる最低年齢 (POST との競合回避) |
| `PAPRIKA_QUEUE_REDRIVE_MAX_PER_PASS` | `0` | 1 パスでの最大配置数 (0 = 制限なし) |
| `PAPRIKA_QUEUE_REDRIVE_DISABLE` | `0` | `1` で redrive 完全停止 (緊急用) |
| `PAPRIKA_QUEUE_TIMEOUT_S` | `180.0` | queued の最終タイムアウト (reaper / reconciler 共通) |
| `PAPRIKA_STALE_RECONCILE_INTERVAL_S` | `90.0` | stale reconciler の周期 |
| `PAPRIKA_STALE_RUNNING_GRACE_S` | `300.0` | running が「dead 判定」されるまでの猶予 |
| `PAPRIKA_WORKER_WATCHDOG_THRESHOLD_S` | `300.0` | event loop wedge 検知の閾値 |
| `PAPRIKA_WORKER_WATCHDOG_LINK_THRESHOLD_S` | `600.0` | inbound-liveness watchdog の閾値 (0 で無効) |

---

## 関連

- [アーキテクチャ概要](architecture.html) — 5 つの構成要素と全体像
- [Hub の仕組み](architecture.html#hub) — ジョブモード (fetch / codegen-loop / rerun)
- [Worker の仕組み](architecture.html#worker) — Lane プール、self-healing、self-update
- [Hub スケーリング](scaling.html) — 複数 Hub の運用ルーティング
- [Worker 自己回復](worker-resilience.html) — Worker 側の watchdog 詳細
- [Worker 自動配信](worker-autodeploy.html) — 自己更新の流れ
