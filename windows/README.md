# paprika Windows Edition

paprika を Windows PC 単機で動かすためのポータブル版。Linux fleet 版と
同じ機能を「ダウンロード → 展開 → ダブルクリック」で使えます。

## 配布物

```
paprika-windows-vX.Y.Z.zip   (~50MB、Chromium は初回 DL)
  └─ paprika/
       ├─ paprika.exe              ← ダブルクリックで起動
       ├─ _internal/               (Python ランタイム + paprika コード)
       ├─ redis/
       │    └─ redis-server.exe    (バンドル Redis ~5MB)
       ├─ vnc/
       │    └─ TightVNC + websockify  (Live タブの画面表示用)
       └─ data/                    (実行時に作成される; 全データはここに)
            ├─ chromium/           (初回起動で DL ~180MB)
            ├─ jobs/               (各ジョブの page.html / assets / log)
            ├─ redis/              (Redis 永続化 dump.rdb)
            ├─ hosts/              (cookies / ログイン状態)
            └─ engines/            (API キー設定)
```

`paprika/` フォルダごと USB メモリで持ち歩けます。アンインストールは
**フォルダ削除**だけ。レジストリも AppData も汚しません。

## ユーザの操作手順

### 初回

1. `paprika-windows-vX.Y.Z.zip` をダウンロード
2. お好きなフォルダ (`C:\paprika\` など) に展開
3. `paprika.exe` をダブルクリック
4. 初回のみ:
   - Chromium 自動ダウンロード (~180MB、進捗表示)
   - SmartScreen 警告が出る場合: `詳細情報` → `実行` をクリック
5. 独自ウィンドウが開いて paprika の admin UI が表示
6. 右上の **Settings** タブで OpenAI / Claude / Mistral の API キーを登録
   - API キー未設定でも fetch / capture / cookies 保存などの基本機能は使える
   - LLM 必須機能 (codegen-loop / page.agent など) は grey out

### 2 回目以降

- `paprika.exe` をダブルクリック → 即起動 (Chromium 再 DL なし)
- システムトレイにアイコンが常駐 (右下、ベルやネットワークアイコンの並び)
- ウィンドウを閉じても backend は動き続ける (ジョブが継続)
- トレイアイコン右クリック → **終了** で完全停止

### ログインが必要なサイト (Twitter / 銀行 / 社内ポータル等)

paprika は**独立した Chromium**を使うので、初回はそのサイトで手動ログインが必要です:

1. paprika の Live タブを開く (任意のジョブを fetch モードで起動)
2. その Chromium 内で対象サイトを開いてログイン
3. ログイン状態 (cookies) は自動で保存され、以降のジョブで再利用される

将来は v1.1 で「お使いの Chrome から ログイン状態をインポート」機能を追加予定。

## アップデート

1. 新しい `paprika-windows-vY.Y.Y.zip` をダウンロード
2. 古い `paprika/` を一旦リネーム (例: `paprika.old/`)
3. 新しい zip を展開
4. 古い `paprika.old/data/` を新しい `paprika/data/` にコピー
5. 古い `paprika.old/` を削除

v1.1 で「アプリ内アップデート (WinSparkle)」を予定。

## ポート

| 用途 | デフォ | 衝突時 |
|------|--------|--------|
| hub HTTP | 8000 | 8001, 8002, ... まで自動探索 |
| Redis | 6379 | 6380, 6381, ... まで自動探索 |
| Chrome CDP | 9223 | (同上) |
| noVNC | 6080 | (同上) |

ポート占有は paprika.exe プロセスツリーが終了すれば解放されます。

## API キー未設定での動作 (graceful degrade)

| 機能 | API キー無し | API キー有り |
|------|-------------|-------------|
| URL → page.html + assets + cookies | ✅ | ✅ |
| `pap.walk` (BFS crawl) | ✅ | ✅ |
| `page.download_video` (yt-dlp) | ✅ | ✅ |
| profiles / extensions / hosts 管理 | ✅ | ✅ |
| `page.agent()` | ❌ 503 | ✅ |
| `page.ask()` / `page.extract()` / `page.observe()` | ❌ 503 | ✅ |
| mode=codegen-loop / vision-agent | ❌ 503 | ✅ |

admin UI 右上のバナーに `LLM features disabled` と表示されます。

## トラブルシューティング

### SmartScreen で「不明な発行元」警告

- 「詳細情報」をクリック → 「実行」
- v1.0 はコード署名証明書なしで配布。署名付きビルドは v1.1 で導入予定。

### Chrome / Chromium が動かない

- `data\chromium\chrome.exe` が存在するか確認
- 削除してから paprika.exe を再起動 → 再 DL される
- Windows の Visual C++ 2015-2022 Redistributable が必要:
  https://aka.ms/vs/17/release/vc_redist.x64.exe

### Live タブで画面が真っ黒

- `vnc\` フォルダがバンドルに入っているか確認
- TightVNC / websockify の起動状況は paprika のログで確認
  (トレイアイコン右クリック → 設定 → Logs タブ)

### ポートが衝突して起動失敗

- 既存の Redis (Memurai 等) や hub が同じポートを掴んでいる
- paprika は自動で次の空きポートを試すが、20 個試して全部ダメだと諦める
- `paprika.exe --hub-port 9100` で明示指定すれば回避可能

### ポータブル先で起動できない

- USB メモリ等 read-only な場所では起動しない (data/ に書けないため)
- 一度ローカル HDD にコピーしてから起動

## 開発者向け: dev モード (= python から直接)

```powershell
# 必要パッケージ
pip install -r requirements.txt
pip install pywebview pystray pillow

# Redis を事前に Windows に置く (初回のみ)
# windows\bin\redis\ に redis-server.exe を配置

# 起動
python -m windows.main
```

`--no-ui` で UI なしヘッドレスモード (ブラウザで http://localhost:8000/ を開く)。

## ビルド (リリース zip を作る)

```powershell
# 必要パッケージ
pip install pyinstaller pywebview pystray pillow
pip install -r requirements.txt

# PowerShell でビルド
.\windows\build.ps1
# → dist\paprika-windows-vX.Y.Z.zip
```

`build.ps1 -Clean` で前回のキャッシュを破棄してフルビルド。
`build.ps1 -SkipRedisFetch` で `windows\bin\redis\` の再 DL を skip。

## ファイル構成 (windows/)

```
windows/
  __init__.py             モジュールドキュメント
  main.py                 paprika.exe エントリポイント
  preflight.py            Chromium / VC++ Redist 検出 + 初回 DL
  preflight_dialog.py     不足時の tkinter ダイアログ
  redis_supervisor.py     同梱 redis-server.exe の lifecycle
  worker_supervisor.py    Windows 単機 1 lane の worker (TODO: 本実装)
  runner_sandbox.py       Windows subprocess 版 sandbox (TODO: 本実装)
  ui_shell.py             pywebview ウィンドウ + System tray
  paprika.spec            PyInstaller spec
  build.ps1               リリースビルドスクリプト
  bin/                    バンドルバイナリ (gitignore、build.ps1 が DL)
    redis/                tporadowski/redis ~5MB
    vnc/                  TightVNC + websockify
```

## 関連

- Linux fleet 版: プロジェクトルートの `docker-compose.yml` で起動
- 設定の保存場所は `data/jobs/`, `data/hosts/`, `data/engines/` などフォルダ単位
- 単機 → fleet 移行: `data/jobs/` フォルダを fleet 側にコピーするだけ
  (Redis dump は移行不要、Job メタデータはファイルベース)
