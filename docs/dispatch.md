---
layout: doc
title: ジョブ分配と負荷分散
description: Paprika の Hub・Worker 間のネットワーク負荷分散の実装。空き Worker の選び方、滞留ジョブの再配置ループ、取り残されたジョブの整合化処理、複数 Hub 構成での経路設計まで、実コードに基づいて解説。
active: dispatch
---

Paprika の負荷分散は **3 つのレイヤ** で動いています:
クライアントから Hub までの **ネットワーク経路**、Hub から Worker への **ジョブの割り当て**、そして **失敗時の回復** です。

<div class="tldr">
<span class="tldr-label">概要</span>
<ul>
<li><strong>nginx 段</strong>: 同じ Worker への通信は常に同じ Hub に届くよう <code>worker_id</code> で固定振り分け、それ以外は順番に振り分け (round-robin)。</li>
<li><strong>Hub 段</strong>: 実行中ジョブの本数が少ない Worker を優先、同点はランダム選択で「特定 Worker だけに偏る」を防ぐ。</li>
<li><strong>再配置ループ</strong>: 3 秒ごとに各 Hub が <code>queued</code> ジョブを「期待値一致時のみ更新」で奪い合い、空き Lane に置き直す。最初の割り当て時に取りこぼした分を救う。</li>
<li><strong>多層フェイルセーフ</strong>: 即時割り当て → 3 秒で再配置 → 90 秒で整合化 → 180 秒で強制終了 の 4 段。切断・自己更新・再接続を耐える。</li>
</ul>
</div>

> このページは内部実装の解説です。Paprika の <em>使い方</em> としては意識する必要はなく、SDK は 503 を自動で再試行、管理画面はそのまま使えます。仕組みを知りたい運用者向けです。設計の全体像は <a href="architecture.html">アーキテクチャ概要</a>。

## 全体像: 3 つのレイヤ

```text
クライアント
   │  HTTP / WebSocket (POST /jobs ・ GET /jobs/{id} ・ /sessions/*)
   ▼
nginx (複数 Hub 構成時のみ)
   │  ルール 1: /workers/{id}/link → worker_id ごとに固定の Hub へ
   │  ルール 2: それ以外            → 順番に振り分け (round-robin)
   ▼
Hub × N (ステートレス) ────── Redis (協調) ────── DB (job 情報) ── MinIO (アセット)
   │  空いている Worker を選んで割り当てる
   │  HubAssignJob を WebSocket で送る
   ▼
Worker × N
   ├─ Lane プール (Chrome × M 並列)
   └─ 15 秒ごとに状態通知 (実行中ジョブの本数 + CPU/Mem/Disk)
```

| レイヤ | 目的 | 実装 |
|---|---|---|
| **ネットワーク** | リクエストを正しい Hub に届ける | nginx の **2 種類の振り分けルール** |
| **Hub の割り当て** | 空き Worker を選んで割り当てる | `pick_worker()` + `assign()` |
| **失敗回復** | 取りこぼし・切断・再起動を耐える | 再配置 3 秒 / 整合化 90 秒 / 強制終了 180 秒 |

---

## レイヤ 1: ネットワーク経路 (nginx)

### 2 種類の振り分けルール

複数 Hub 構成では nginx が前段に立ち、リクエストを 2 通りに振り分けます。

| パス | ルール | 理由 |
|---|---|---|
| `/workers/{worker_id}/link` (WebSocket) | **`hash $worker_id consistent`** で **同じ Worker は常に同じ Hub へ届ける** | Worker と Hub をつなぐ WebSocket は 1 本だけ。途中で Hub をまたぐと、その Hub だけが持っている状態 (送信中ジョブ、送信ロック、スクリーンショット待ち) が分裂してしまう |
| それ以外 (REST, 管理画面, `/jobs`, `/sessions/*`) | 順番に振り分け (round-robin) | ジョブ情報は MariaDB、アセットは MinIO、Worker 一覧は Redis にあるので、どの Hub に当たっても同じ答えを返せる |

### `worker_id` の安定化

「同じ Worker は常に同じ Hub へ届ける」振り分けは、`worker_id` 自体が **安定している** ことが前提です。Paprika は **Worker の LAN IP から決定的に `worker_id` を導出** します:

```text
LAN IP 192.168.50.150  →  worker_id = w50150
LAN IP 192.168.50.151  →  worker_id = w50151
```

(コード: `server/hub/routes/workers.py::_ip_derived_worker_id`)

- 同じ IP は **再起動 / 自己更新 / id ファイル消失** をまたいでも同じ id を持つ → 担当 Hub が変わらず、振り分けが安定。
- ごく稀に `/16` をまたいで衝突するケース (`192.168.5.150` と `192.168.51.50` が両方 `w50150` を欲しがる) では、IP を全部つなげた `w10-10-5-150` 形式に切り替える。

これがなかった頃 (`<hostname>-<rand4>` 形式) は、Worker が自己更新するたびに新しい id を取る → 担当 Hub が毎回ジャンプ → 一部 Hub に偏る、というドリフトが起きていました。

### セッションの転送

順番に振り分けて届いたリクエストが「自分の Hub では持っていないセッション宛」だった場合、Hub は Redis のセッション表 (セッション ID → 所有 Hub) を引いて、**所有 Hub へ HTTP で転送** します。クライアントから見ると、どの Hub に当たっても同じ結果が返ります。

---

## レイヤ 2: Hub の `pick_worker()` ロジック

### 候補のフィルタ条件

```python
candidates = [
    w for w in alive_workers()              # 直近 120 秒以内に通知があった
    if w.status == "active"                  # operator が drain / standby にしていない
    and w.in_flight < w.capabilities.max_concurrent   # まだ空きがある
    and (
        len(w.capabilities.lane_novnc_urls or []) > 0  # Lane プール有り
        or bool(w.capabilities.novnc_url)              # 単 Chrome (Windows portable)
    )
    and w.disk_pct < 90.0                    # ディスク使用率 >90% は除外
]
```

(コード: `server/scheduler.py::pick_worker`)

ここで読まれる Worker 側の値は、Worker が定期通知 (`WorkerHeartbeat`) と接続時の自己申告 (`capabilities`) で送ってきたものです:

- `in_flight` = その Worker で実行中のジョブの本数
- `capabilities.max_concurrent` = その Worker が並列で持てる本数 (Lane の数)
- `disk_pct` = Worker ホストのディスク使用率
- `status` = 運用者が管理画面で設定する状態 (`active` / `drain` / `standby`)

各フィルタの実害は具体的:

- **Lane URL を持たない Worker をスキップ** していなかった頃: ジョブが「空き Lane が無い」で連続 3 回 502 になる事故が起きていました (実例: コミット履歴 `71ec64da63c5`)。
- **ディスク使用率 90% 超を除外** することで、Worker 側がジョブ受信後に「ディスクが満杯です」と拒絶する前に Hub 側で先に弾いて、割り当ての往復と失敗報告のやり取りを省きます。

### 並び順

```python
def _key(w):
    return (w.in_flight, -w.capabilities.max_concurrent)

best  = min(_key(w) for w in candidates)
tied  = [w for w in candidates if _key(w) == best]
return random.choice(tied)        # ★同点はランダム
```

- **第 1 キー: 実行中ジョブの本数が少ない順** — 今いちばん暇な Worker を優先。
- **第 2 キー: 並列キャパが大きい順** — 同じ暇さなら、大きい (= 並列に耐えられる) Worker を優先。
- **第 3 キー: ランダム** — 上 2 つで同点になった候補の中からランダムに選ぶ。

### なぜランダムで同点を解消するのか

実行中の本数が `0` の暇な Worker が複数いるとき、決定的に並べると **辞書順で先頭の Worker** にだけ毎回当たってしまいます。1 つずつ叩く使い方 (Fetch / LLM / Macro) では実行中の本数が `0` まで戻るので、**「特定の Worker だけ酷使されている」** という運用者の苦情として顕在化しました。`random.choice(tied)` で暇な群に均等に振り分けて解消しています。

### 割り当ての送信

```python
async def assign(self, worker, msg):
    await worker.send(msg)             # WebSocket で HubAssignJob 送信
    worker.in_flight += 1               # 即座に実行中本数を +1
    return True
```

送信成功時にその場で実行中本数を 1 増やすので、**同じパス内で続けて `pick_worker()` を呼んでも同じ Worker は選ばれない** = 後述の再配置ループでも自然に分散します。

---

## レイヤ 3: Worker レジストリ (Redis スキーマ)

各 Hub プロセスは自分が持っている WebSocket だけを `connections: dict` で覚えています。フリート全体の状態は **Redis を介して共有** します:

| キー | 型 | 有効期限 | 内容 |
|---|---|---|---|
| `paprika:workers` | Sorted Set | (無し) | `worker_id` → 最終通知時刻 |
| `paprika:worker:{id}` | String (JSON) | (無し) | 機能情報 + アドレス + 所属 Hub + リソース情報 |
| `paprika:worker:{id}:online` | String | **120 秒** | 通知が止まれば自動失効 = 停止判定 |
| `paprika:worker:{id}:owner` | String | **120 秒** | この Worker の WebSocket を持っている Hub の id |

(コード: `server/scheduler.py` の `_k_index / _k_worker / _k_online / _k_owner`)

**状態通知は 15 秒ごと**、有効期限は **120 秒** (12 回ぶんの猶予)。yt-dlp のサブプロセスや大きな Python 計算で event loop が一瞬詰まる程度では「停止」と誤判定しない設計です。

### `:owner` キーが必要な理由

複数 Hub 構成で、順番に振り分けて hub-A に届いたセッション操作リクエストが、実は **hub-B が WebSocket を持っている** Worker 宛だったとき、hub-A は `:owner` を見て hub-B へ HTTP で転送します。Hub が 1 台しかない構成では「書かれるが読まれない」値です。

### `owner` キーを安全に手放す仕組み

Worker の WebSocket が hub-A → hub-B に切り替わる瞬間、hub-A の登録解除が遅れると、hub-B がすでに書いた `:owner = hub-B` を hub-A の delete が誤って上書きしてしまう競合が起きます。これを **Lua スクリプト** で防ぎます:

```lua
if redis.call('get', KEYS[1]) == ARGV[1]
  then return redis.call('del', KEYS[1])
  else return 0
end
```

「自分が書いた owner のときだけ delete する」が**途中で割り込まれずに 1 操作で完結**するので、hub-B の有効な所有権が誤って消えることはありません。

---

## レイヤ 4: 滞留ジョブの再配置ループ

`POST /jobs` は Worker への割り当てを **1 回だけ** 試みます。クライアントが瞬断したり、その 8 秒間にどの Lane も空かなかったりすると、ジョブは `status=queued` のまま残ります。180 秒の強制終了がこれを「タイムアウト」で殺すまでに **空いた Lane が出ても割り当て直さない** 問題が 2026-06-06 に発生しました (**ジョブ失敗の 80% がこれだった**)。

これを解決するのが **再配置ループ** (コード: `server/hub/_redrive.py`):

```text
3 秒ごと (= PAPRIKA_QUEUE_REDRIVE_INTERVAL_S)
  ├─ 自分の Hub に空き Lane が 1 つでもあるか? (pick_worker() 即返り)
  │     ↓ なし → 何もしない
  │     ↓ あり
  ├─ 全 queued ジョブを古い順に取得
  ├─ 90 秒未満のもの (= まだ POST 側が処理中の可能性) はスキップ
  └─ 各ジョブについて:
       ├─ DB に「条件付き UPDATE」: status=queued AND worker_id IS NULL → running + worker_id
       │      ↓ 負け → 別の Hub / 元の POST が掴んだ。次へ
       │      ↓ 勝ち
       ├─ HubAssignJob を WebSocket 送信
       │      ↓ 失敗 → 条件付き UPDATE を巻き戻して次のパスで再挑戦
       │      ↓ 成功
       └─ DB に running 状態を永続化 (noVNC URL + セッション ID 込み)
```

「条件付き UPDATE」 = SQL の `UPDATE ... WHERE status='queued'` が、**条件に一致した行だけを 1 命令で書き換える** こと。Hub が同時に複数走っても、この 1 命令の中では割り込まれずに、ただ 1 つの Hub だけが「勝者」になります。

### 二重割り当てを防ぐ 3 重ガード

1. **条件付き UPDATE** — DB の `UPDATE ... WHERE status='queued'` が途中で割り込まれない 1 操作なので、**ただ 1 つの Hub** だけが勝ち、負けた Hub は静かに次へ進む。
2. **`worker_id IS NULL` チェック** — POST がすでに Worker に渡したあと WebSocket 送信に失敗したケース (`worker_id` だけ書かれて `queued` に巻き戻った) を再配置が拾わないようにする。
3. **90 秒の年齢ゲート** — POST が最悪滞留するであろう時間 (8 秒の割り当て猶予 + 60 秒の Hub 間転送 + 余裕) を超えるまで触らない。「まだ POST 側が処理中かもしれない」可能性を消す。

### Kill switch

`PAPRIKA_QUEUE_REDRIVE_DISABLE=1` で完全停止 → 旧 POST inline-only 動作に戻る。ハードな運用変更時の緊急ロールバック用。

---

## レイヤ 5: 取り残されたジョブの整合化処理

POST と再配置で取りこぼされた、より長期的な状態の不整合を**終わった状態に確定させる**のが、整合化処理 (コード: `server/hub/_reaper.py::_stale_job_reconciler_loop`) です:

```text
90 秒ごと
  ├─ フリート全体で稼働中の Worker 一覧を取得 (stats_async())
  │     (= 自分の Hub の接続 + Redis 上の他 Hub の通知 を合わせたもの)
  │
  ├─ 実行中ジョブを全件スキャン
  │   └─ worker_id が稼働中一覧に居ない && 300 秒以上経過 → failed に確定
  │      (300 秒は「Worker が自己更新で一瞬切れて同じ id で戻ってくる」窓を許す)
  │
  └─ queued ジョブを全件スキャン
      └─ 180 秒以上 queued && worker_id 未割り当て → failed (queued タイムアウト) に確定
```

### なぜ「フリート全体」を見るのか

決定的に重要な設計点です。**この Hub の `state.registry.connections` だけを見て稼働判定すると失敗します**:

- nginx の振り分けで Worker は 3 Hub にだいたい 1:1:1 で分散
- → 各 Hub の `connections` には **フリート全体の 1/3** しか居ない
- → 「自分の `connections` に居ない = 停止」と判定すると **健全な他 Hub 所有ジョブを大量失敗させる**

`stats_async()` は自 Hub の接続と Redis 上の他 Hub の通知を合わせて **フリート全体のビュー** を返します。これを使うのが正しさの肝です。

### `_last_known_extra` キャッシュ (60 秒)

`stats_async()` の中で Redis 取得が 1.5 秒タイムアウトしたとき、フォールバックとして **直前の良好な集約結果を 60 秒間まで再利用** します。これがないと、Redis が瞬間的に遅れた拍子に「自 Hub の接続だけ」に縮退して、整合化処理が大量に誤判定する事故が起きていました (admin UI の Workers 行数が 37 ↔ 30 ↔ 7 ↔ 0 で点滅した症状)。

---

## レイヤ 6: 多層フェイルセーフ

ジョブの生死は **4 段階** のセーフティネットで守られています:

| # | 機構 | 周期 | 救う対象 |
|---|---|---|---|
| **1** | `POST /jobs` の即時割り当て | 即時 | 通常時の 99% はここで完結 |
| **2** | **再配置ループ** | 3 秒 | POST が取りこぼした `queued` (空き Lane あり) |
| **3** | **整合化処理** | 90 秒 | Worker が消えた `running` / 180 秒超えの `queued` |
| **4** | キューの強制終了 | 180 秒 | 最終的なタイムアウト (`failed` で閉じる) |

「ローカル復旧 → クラスタ整合化 → 強制クローズ」の 3 段で、**ジョブが永遠に `running` / `queued` で取り残されることが原理的に無い** ように作られています。

---

## Hub レジストリ (Multi-hub 自動連携)

Worker と同じく、Hub も Redis に自分の状態を定期通知します (コード: `server/hub/_hubs.py`):

| キー | 有効期限 | 書き込み頻度 |
|---|---|---|
| `paprika:hubs:{hub_id}` | **90 秒** | 30 秒ごと (= 有効期限の 1/3) |
| `paprika:hubs:index` (ZSET) | 永続 | `hub_id` ごとの初出時刻 |

これにより:

- 新規 Hub が `REDIS_URL` を共有するだけで自動的に admin UI のリストに現れる (手動登録不要)
- 有効期限 = 90 秒なので、停止した Hub は 1 分強で消える
- `paprika:hubs:index` は「ZSET に居るが live row が無い = 最近落ちた Hub」を可視化

### 複数 Hub 起動時の安全策

Hub 起動時の「`running` のまま取り残されたジョブを `failed` にする」処理は、**他に稼働中の Hub が居る場合は実行しません**:

```python
peers = [h for h in await state.hubs.list_all()
         if h.get("alive") and not h.get("local")]
if peers:
    return  # peer が running を持っている可能性 → まとめて失敗にしない
```

クローン VM で新 Hub を立ち上げたときに、既存 Hub が走らせていたジョブを「自分のオーケストレータには無い」と誤判定して全部失敗にしてしまう事故を防ぎます。

---

## 定期通知の設計まとめ

| 主体 → 先 | 周期 | 有効期限 | 用途 |
|---|---|---|---|
| Worker → Hub (WebSocket) | **15 秒** | — | 実行中本数 + CPU/Mem/Disk + プロファイルキャッシュ |
| Hub → Redis (Worker キー更新) | 通知受信時 | **120 秒** | 稼働判定 (`alive`) |
| Hub → Redis (自身の `paprika:hubs:{id}`) | **30 秒** | **90 秒** | 複数 Hub の自動発見 |
| Worker 自己監視 (event loop の停止検知) | 30 秒チェック | **300 秒 (+ジッタ)** | event loop が完全に止まったときの自殺 |
| Worker 自己監視 (受信側の生存チェック) | 30 秒チェック | **600 秒 (+ジッタ)** | プロキシ経由の幽霊 WebSocket の検知 |

**ジッタ** はフリート全体が「同じ閾値」で同時に exit するのを防ぐためのランダムオフセット (0-60 秒)。デプロイ直後の一斉再起動でも雪崩が起きないようにする。

---

## 主要パラメータ一覧

実運用で触る可能性のある環境変数とその意味:

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `PAPRIKA_QUEUE_REDRIVE_INTERVAL_S` | `3.0` | 再配置ループの周期 (秒) |
| `PAPRIKA_QUEUE_REDRIVE_MIN_AGE_S` | `90.0` | 再配置が触る最低年齢 (POST との競合回避) |
| `PAPRIKA_QUEUE_REDRIVE_MAX_PER_PASS` | `0` | 1 周ごとの最大配置数 (0 = 制限なし) |
| `PAPRIKA_QUEUE_REDRIVE_DISABLE` | `0` | `1` で再配置を完全停止 (緊急用) |
| `PAPRIKA_QUEUE_TIMEOUT_S` | `180.0` | `queued` の最終タイムアウト (強制終了 / 整合化共通) |
| `PAPRIKA_STALE_RECONCILE_INTERVAL_S` | `90.0` | 整合化処理の周期 |
| `PAPRIKA_STALE_RUNNING_GRACE_S` | `300.0` | `running` が「停止」と判定されるまでの猶予 |
| `PAPRIKA_WORKER_WATCHDOG_THRESHOLD_S` | `300.0` | event loop の停止検知の閾値 |
| `PAPRIKA_WORKER_WATCHDOG_LINK_THRESHOLD_S` | `600.0` | 受信側の生存チェックの閾値 (0 で無効) |

---

## 関連

- [アーキテクチャ概要](architecture.html) — 5 つの構成要素と全体像
- [Hub の仕組み](architecture.html#hub) — ジョブモード (`fetch` / `codegen-loop` / `rerun`)
- [Worker の仕組み](architecture.html#worker) — Lane プール、自己回復、自己更新
- [Hub スケーリング](scaling.html) — 複数 Hub の運用ルーティング
- [Worker 自己回復](worker-resilience.html) — Worker 側の自己監視の詳細
- [Worker 自動配信](worker-autodeploy.html) — 自己更新の流れ
