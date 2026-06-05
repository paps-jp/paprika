---
layout: doc
title: Locator — 要素の指定
description: Paprika の Locator(Playwright スタイル)で DOM 要素を指定する完全リファレンス — CSS/role/text/testid/placeholder/title/alt の取得、操作・入力デバイス・取得・状態判定・チェーン・wait_for の全メソッド。
active: locator
---

`Locator` は **要素の指し方**だけを保持して、`click()` や `wait_for()` のタイミングで初めて DOM を探しに行く**遅延評価**の参照です。Playwright と同じ感覚で書けます。

> 全体 API は [API リファレンス](api.html)。基本ガイドは [ガイド: DOM 操作](guides.html)。

## 作り方

`Page` の以下のメソッドが `Locator` を返します。

| メソッド | 例 | マッチするもの |
|---|---|---|
| `page.locator(css)` | `page.locator("button.primary")` | 任意の **CSS セレクタ** |
| `page.get_by_role(role, *, name=None)` | `page.get_by_role("button", name="購入")` | `role=` 属性で書かれた要素 |
| `page.get_by_text(text)` | `page.get_by_text("ログイン")` | **テキスト一致**（可視テキスト） |
| `page.get_by_test_id(id)` | `page.get_by_test_id("submit-btn")` | `data-testid="..."` 属性 |
| `page.get_by_placeholder(text)` | `page.get_by_placeholder("メール")` | `placeholder="..."` |
| `page.get_by_title(text)` | `page.get_by_title("ヘルプ")` | `title="..."` |
| `page.get_by_alt_text(text)` | `page.get_by_alt_text("ロゴ")` | `<img alt="...">` |

> **解決はアクション時**です。`Locator` を作っても DOM 検索はまだ走りません。`click()` / `wait_for()` などを呼んだときに初めて探します。

## メソッド一覧（完全版）

### 操作（クリック・入力）

| メソッド | 戻り値 | 説明 |
|---|---|---|
| `await loc.click()` | `dict` | クリック |
| `await loc.dblclick()` | `bool` | ダブルクリック |
| `await loc.hover()` | `bool` | ホバー |
| `await loc.focus()` | `bool` | フォーカス |
| `await loc.fill(value)` | `dict` | 入力欄に値を **置換** |
| `await loc.type(text)` | `dict` | キー入力をエミュレート |
| `await loc.press(key, *, modifiers=None)` | `dict` | 単一キー押下（`"Enter"` / `"ArrowDown"` 等） |
| `await loc.check()` | `bool` | チェックボックス ON |
| `await loc.uncheck()` | `bool` | チェックボックス OFF |
| `await loc.select_option(value)` | `bool` | `<select>` の値選択 |
| `await loc.set_input_files(files)` | `dict` | `<input type=file>` にファイル添付 |
| `await loc.scroll_into_view_if_needed()` | `bool` | 表示位置までスクロール |

### 取得（テキスト / 属性）

| メソッド | 戻り値 | 説明 |
|---|---|---|
| `await loc.text_content()` | `str \| None` | テキスト内容（`textContent`） |
| `await loc.inner_text()` | `str \| None` | 可視テキスト（`innerText`） |
| `await loc.inner_html()` | `str \| None` | HTML 内容（`innerHTML`） |
| `await loc.input_value()` | `str \| None` | フォーム入力の値 |
| `await loc.get_attribute(name)` | `str \| None` | 属性の値 |

### 状態判定

| メソッド | 戻り値 | 説明 |
|---|---|---|
| `await loc.is_visible()` | `bool` | 表示中か |
| `await loc.is_checked()` | `bool` | チェック済みか |
| `await loc.is_enabled()` | `bool` | 操作可能か |
| `await loc.is_disabled()` | `bool` | 無効化されているか |
| `await loc.is_editable()` | `bool` | 編集可能か |

### チェーン（複数要素の絞り込み）

| メソッド | 戻り値 | 説明 |
|---|---|---|
| `loc.nth(i)` | `Locator` | i 番目（0 始まり、負も可） |
| `loc.first` | `Locator` | 先頭 |
| `loc.last` | `Locator` | 末尾 |
| `await loc.count()` | `int` | マッチ数 |
| `await loc.all()` | `list[Locator]` | 全要素を `Locator` のリストで取り出し |

### 待機

| メソッド | 戻り値 | 説明 |
|---|---|---|
| `await loc.wait_for(*, state="visible", timeout=30.0)` | `bool` | 要素が `state` になるまで待つ。`state` は `visible` / `hidden` / `attached` / `detached`。`timeout` は秒（**既定 30 秒**） |

## 実例

### 基本

```python
btn = page.locator("button.primary")
await btn.click()
await btn.wait_for(state="visible", timeout=10)
```

### テキスト・ロール・属性で指す

```python
await page.get_by_text("カートに入れる").click()
await page.get_by_role("button", name="送信").click()
await page.get_by_test_id("login-form").wait_for()
await page.get_by_placeholder("メールアドレス").fill("alice@example.com")
await page.get_by_alt_text("メイン画像").wait_for()
```

### 複数ヒット

```python
items = page.locator("ul.products > li")
n = await items.count()
print(n, "items")

# 先頭・末尾・i 番目
first  = items.first
last   = items.last
third  = items.nth(2)

# 全部取り出して処理
for it in await items.all():
    title = await it.locator(".title").inner_text()
    print(title)
```

### 入力 → 送信

```python
inp = page.locator("input[name='q']")
await inp.fill("paprika")
await inp.press("Enter")
```

### 属性・値の取得

```python
el = page.locator(".item-name").first
print(await el.inner_text())
print(await el.get_attribute("data-id"))

inp = page.locator("input[name='email']")
print(await inp.input_value())
```

### 連鎖（子要素を絞り込む）

```python
card = page.locator(".card").first
title = card.locator("h2.title")
await title.click()
```

### 状態判定

```python
if await page.locator(".loading").is_visible():
    await page.locator(".loading").wait_for(state="hidden", timeout=10)

if await page.locator("input[type=checkbox]").is_checked():
    print("already checked")
```

### ファイル添付

```python
await page.locator("input[type=file]").set_input_files("/path/to/photo.jpg")
```

### `<select>` の選択

```python
await page.locator("select[name='country']").select_option("JP")
```

## 同期版（`sync_paprika`）

`await` を外すだけで同じ API が使えます。

```python
from paprika_client import sync_paprika

with sync_paprika() as cli:
    with cli.session("https://example.com") as page:
        page.locator(".item").first.click()
        for r in page.locator(".item").all():
            print(r.get_attribute("data-id"))
```

## PHP SDK

> **Locator は Phase 2 で対応予定**です（チェーン API `$page->locator(...)->first()->click()` を実装予定）。現在の Phase 1 では `Job` / `Session` の基本操作までが対象です。

## よくあるハマりどころ

- **複数ヒットでは `nth()` か `first` を使う** — マッチ数が 2 以上ある状態で `click()` を呼ぶと、SDK は最初の要素に対して操作します（明示的に絞り込むのが安全）。
- **`get_by_text` は部分一致**。完全一致したいときは CSS や属性で指す。
- **動的に出る要素**は `wait_for(state="visible")` で待ってから操作する。`timeout` の既定は 30 秒。
- **CSS が効かない画面**（Canvas、Shadow DOM の深い所など）は [Vision AI（`page.agent`）](vision-mouse.html) に逃がす。

## 関連

- [API リファレンス](api.html) — 全 API
- [ガイド: DOM 操作](guides.html)
- [サンプル: クリック・入力・キー操作](examples.html)
