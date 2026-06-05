---
layout: doc
title: paprika-client CLI
description: paprika-client CLI コマンドの完全リファレンス — upload-profile / list-profiles / set-default-profile / delete-profile の全引数と典型ワークフロー。
active: cli
---

`paprika-client` パッケージは Python SDK のほかに、**Hub の管理を CLI でこなすためのサブコマンド群**を提供します。主にローカル Chrome の **プロファイル登録**（ログイン状態の持ち込み）に使います。

> プロファイルの仕組みと使い方は [`use_profile` リファレンス](profile.html) を参照。

## インストール

```bash
pip install paprika-client
```

成功すると `paprika-client` コマンドが PATH に追加されます。

## グローバル引数

すべてのサブコマンドに先行して指定できます。

| 引数 | 既定 | 説明 |
|---|---|---|
| `--hub <URL>` | `$PAPRIKA_HUB` または `http://localhost:8000` | 操作対象の Hub の URL |

```bash
paprika-client --hub http://paprika.lan:8000 list-profiles
# あるいは環境変数で
export PAPRIKA_HUB=http://paprika.lan:8000
paprika-client list-profiles
```

## サブコマンド一覧

| サブコマンド | 用途 |
|---|---|
| [`upload-profile`](#upload-profile) | 手元の Chrome プロファイルを **スナップショット**して Hub にアップロード |
| [`list-profiles`](#list-profiles) | Hub に登録済みのプロファイル一覧 |
| [`set-default-profile`](#set-default-profile) | 既定プロファイルを設定 / 解除 |
| [`delete-profile`](#delete-profile) | 登録済みプロファイルを削除 |

## `upload-profile` {#upload-profile}

手元の Chrome の **User Data フォルダ** のうち、指定プロファイル（既定 `Default/`）をスナップショットして Hub に登録します。以後 `options.use_profile=<name>` でジョブが利用できます。

```bash
paprika-client upload-profile --name <name> [options]
```

| 引数 | 必須 | 既定 | 説明 |
|---|---|---|---|
| `--name` | ✅ | — | Hub に登録する名前。`options.use_profile` で参照する |
| `--chrome-profile` | | `Default` | スナップショット対象のローカル Chrome プロファイル名。マルチプロファイル環境では `chrome://version` の **「プロファイル パス」** を確認 |
| `--include <name>` | | — | 既定のセットに**追加で同梱**するファイル / ディレクトリ名（`Bookmarks` / `History` など）。**繰り返し可** |
| `--note <text>` | | — | 管理画面で表示する自由メモ |
| `--no-decrypt-cookies` | | `false` | 操作者側での **Cookie 復号をスキップ**。同じ Chrome キーリングバックエンド（両方とも `peanuts`）の Linux→Linux のときだけ使う。通常は付けない |

### 例

```bash
# 普段使いの Chrome (Default プロファイル) をスナップショットして 'work' で登録
paprika-client upload-profile --name work

# 別プロファイル（Profile 1）を 'paps-staging' として登録、メモ付き
paprika-client upload-profile \
  --name paps-staging \
  --chrome-profile "Profile 1" \
  --note "paps 検証用アカウント"

# 既定セットに加えて Bookmarks / History も同梱
paprika-client upload-profile --name browse \
  --include Bookmarks --include History
```

### Cookie の扱い（Chrome v20 App-Bound 暗号化）

Chrome 127+ は **App-Bound 暗号化（v20）** で Cookie を暗号化します。`upload-profile` は既定で **操作者側で復号**して plaintext を `value` に詰めるので、Linux ワーカー上の Chrome がそのまま読めます。

別経路として **Bridge 拡張**（[Bridge 拡張](bridge-extension.html)）で `chrome.cookies.getAll()` 経由で Cookie だけ送る方法もあります（軽量・ログイン状態の更新が手軽）。

## `list-profiles` {#list-profiles}

Hub に登録済みのプロファイル一覧を表示します。

```bash
paprika-client list-profiles
```

出力例（タブ区切り）:

```text
NAME            SIZE       UPDATED                  NOTE
work            45.2 MB    2026-06-05T12:34:56Z     (default)
paps-staging    38.1 MB    2026-06-04T09:00:00Z     paps 検証用アカウント
browse          61.8 MB    2026-06-02T22:15:30Z
```

`NOTE` の `(default)` は [既定プロファイル](#set-default-profile)を意味します。

## `set-default-profile` {#set-default-profile}

`options.use_profile` を省略したジョブに自動適用する **既定プロファイル**を設定 / 解除します。

```bash
# 設定
paprika-client set-default-profile --name work

# 解除（--name を省略）
paprika-client set-default-profile
```

| 引数 | 既定 | 説明 |
|---|---|---|
| `--name <name>` | （無指定で解除） | 既定にするプロファイル名。省略すると現在の既定を**解除** |

## `delete-profile` {#delete-profile}

Hub からプロファイルを削除します。

```bash
paprika-client delete-profile --name <name>
```

| 引数 | 必須 | 説明 |
|---|---|---|
| `--name <name>` | ✅ | 削除するプロファイル名 |

## 典型ワークフロー

```bash
# 1) 普段使いの Chrome をアップロード
paprika-client upload-profile --name work --note "office account"

# 2) 既定に設定（以後 use_profile 省略ジョブが自動で work を使う）
paprika-client set-default-profile --name work

# 3) 後で確認
paprika-client list-profiles

# 4) いらなくなったら削除
paprika-client delete-profile --name work
```

## 関連

- [`use_profile` リファレンス](profile.html) — プロファイルの仕組み・正規化・複数 Hub 間共有
- [Bridge 拡張](bridge-extension.html) — 軽量に Cookie だけ送る別経路
- [Host レシピ](host-recipe.html) — ログインを自動化（期限切れの自動再ログインも）
- [JobOptions](job-options.html) — `use_profile` を指定するオプション
