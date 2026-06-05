---
layout: doc
title: use_profile — Chrome プロファイルの持ち込み
description: ログイン状態を含む Chrome の User Data フォルダ(プロファイル)をアップロードしてジョブに使う方法。tar.gz 形式・Default/ 正規化・既定プロファイル・複数 Hub 間の共有を解説。
active: profile
---

`use_profile` は、自分の **Chrome の User Data フォルダ（プロファイル）をアップロード**して、そのログイン状態を含むまま Paprika のジョブに使う仕組みです。Cookie だけでなく **autofill / 保存パスワード / 拡張機能 / 設定**まで丸ごと持ち込めます。

> **uBlock Origin / Bitwarden / MetaMask など普段使っている Chrome 拡張**もそのまま動きます。広告ブロック付きで収集、保存パスワードで自動ログイン、Web3 サインなど、**手元の Chrome と同じ環境**でジョブが走らせられます。

> 軽い用途（Cookie だけ）なら [Bridge 拡張](bridge-extension.html) のほうが手軽です。期限切れの自動更新は [Host レシピ](host-recipe.html) を参照。

## いつ使うか

| シナリオ | 推奨 |
|---|---|
| Cookie だけで足りる | [Bridge 拡張](bridge-extension.html) |
| **autofill / 保存パスワード / 拡張機能** も欲しい | **`use_profile`**（このページ） |
| 何度もログインし直したくない | **`use_profile`** + [Host レシピ](host-recipe.html) |

## アップロードする中身

Chrome の **User Data 配下の 1 プロファイル**（典型は `Default/`）。Paprika は以下の形を受け付けます:

| 形 | 説明 |
|---|---|
| `Default/...` を含む tar.gz | 既定。**Hub がそのまま採用** |
| `Profile N/...` を含む tar.gz | **`Default/` に正規化**して受け付け |
| Cookies などが**ルートに直接** | **`Default/` で包んで**正規化 |

> 必要なら `Local State`（ルート直下）も同梱できます。

## アップロード

### CLI（推奨）

```bash
paprika-client upload-profile <name> <path/to/profile_dir_or_tarball>
```

例:

```bash
# Linux/Mac: User Data の 1 プロファイルを直接渡す
paprika-client upload-profile work ~/.config/google-chrome/Default
# tar.gz を渡してもOK
paprika-client upload-profile work ./profile-snapshot.tar.gz
```

### HTTP API

```bash
curl -X POST "$PAPRIKA_HUB/profiles/work" \
  -H 'Content-Type: application/x-tar' --data-binary @profile.tar.gz
```

`name` は a-z0-9 と `-` `_` で、サイト識別しやすいものを（例: `work` / `paps-staging` / `tw-bob`）。

## ジョブから使う

```python
job = await cli.fetch(
    "https://example.com",
    use_profile="work",          # 名前で指定
)
```

```bash
curl -X POST "$PAPRIKA_HUB/jobs" -H 'Content-Type: application/json' -d '{
  "url":"https://example.com",
  "options":{"mode":"fetch","use_profile":"work"}
}'
```

ジョブは **その Worker の Lane に対し、プロファイルを差し込んだ状態**で Chrome を立ち上げ、終わったら**元の素の状態に戻します**（Lane 自体は壊しません）。

## 既定プロファイル

`options.use_profile` を**省略したジョブ**は、Hub に登録した「既定プロファイル」が（あれば）自動で使われます。常にログインで取りたいときに便利です。

```bash
# 既定を設定
curl -X POST "$PAPRIKA_HUB/profiles/work/default"

# 確認
curl "$PAPRIKA_HUB/profiles/default"

# 解除
curl -X DELETE "$PAPRIKA_HUB/profiles/default"
```

## 一覧 / 削除

```bash
# 一覧
curl "$PAPRIKA_HUB/profiles"

# 中身（メタ情報）
curl "$PAPRIKA_HUB/profiles/work"

# 削除
curl -X DELETE "$PAPRIKA_HUB/profiles/work"
```

## 複数 Hub 間の共有

複数 Hub 構成では、プロファイルは **MariaDB(メタ) + MinIO(本体)** で共有され、**どの Hub からも同じ名前で使えます**。`work` を 1 Hub にアップロードすれば、別 Hub で投げたジョブからも `use_profile: "work"` で参照可能です（[Hub の仕組み](architecture-hub.html)）。

## 注意

- アップロードする tar.gz の **先頭ディレクトリ名**は気にしなくて構いません（Hub が `Default/` に正規化します）。
- **拡張機能**は通常の Chrome の場所だと Profile 移動後に無効化されることがあります（PreferenceVerifier）。Paprika 側は `--load-extension` で再注入して持ち込みます。
- 既定プロファイルは Hub 側の設定なので、**運用全体に効く**点に注意。

## 関連

- [Bridge 拡張](bridge-extension.html)
- [Host レシピ](host-recipe.html)
- [API リファレンス](api.html)
- [FAQ: ログイン必須サイト](faq.html)
