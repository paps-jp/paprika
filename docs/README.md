# docs/

Paprika のドキュメントディレクトリ。

## ファイル

Playwright のドキュメントサイト風の複数ページ構成（利用者向けと運用者向けを分離）。

| ファイル | 内容 |
|---|---|
| [`index.html`](./index.html) | **Home** — Paprika 概要・最短の例（GitHub Pages のエントリ） |
| [`intro.html`](./intro.html) | **はじめに** — インストール / 接続 / 最初のスクリプト / コア概念 |
| [`guides.html`](./guides.html) | **ガイド** — 画像/動画取得・ログイン・ブラウザ操作・LLM のタスク別レシピ |
| [`api.html`](./api.html) | **API リファレンス** — paprika_client の全関数 |
| [`operations.html`](./operations.html) | **運用** — アーキテクチャ / HTTP API / デプロイ / 環境変数 / トラブルシュート |
| [`worker-autodeploy.html`](./worker-autodeploy.html) | Worker 自動配信の仕組み（運用者向け詳細） |
| [`vnc-embed.html`](./vnc-embed.html) | VNC 埋め込み API（iframe 等） |
| `style.css` | 全ページ共有スタイル |
| `.nojekyll` | GitHub Pages の Jekyll 処理を無効化 (HTML を素のまま配信) |

関数ごとの詳細な仕様は [`api.html`](./api.html)、
画像・動画取得の実践レシピは [`guides.html`](./guides.html) を参照。

## マニュアルを見る

### GitHub Pages 有効時 (公開済み)

公開設定後のアクセス先:

```
https://paps-jp.github.io/paprika/
```

### GitHub Pages 未有効のとき (即座に見たい)

`htmlpreview.github.io` 経由:

```
https://htmlpreview.github.io/?https://github.com/paps-jp/paprika/blob/main/docs/index.html
```

### ローカルで開く

```bash
git clone https://github.com/paps-jp/paprika.git
open paprika/docs/index.html       # macOS
xdg-open paprika/docs/index.html   # Linux
start paprika\docs\index.html      # Windows
```

## GitHub Pages を有効にする (初回 1 回だけ)

1. GitHub の repo ページ → **Settings** → **Pages**
2. **Source**: "Deploy from a branch"
3. **Branch**: `main` / **Folder**: `/docs`
4. **Save** → 数分後に公開 URL が表示される

`.nojekyll` を置いてあるので Jekyll 処理はスキップ。HTML がそのまま配信されます。

## マニュアルを更新するには

`index.html` を直接編集 → commit → push。GitHub Pages 有効時は数分で反映。

```bash
# プレビュー (ローカル)
python -m http.server -d docs 8080
# → http://localhost:8080/
```
