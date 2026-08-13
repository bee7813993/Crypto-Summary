# 読み取り専用 API（Asset Summary 連携）

Crypto-Summary は、兄弟アプリ **Asset Summary (AS)** からサーバー間で残高・評価額を
読み取るための **read-only API** を持ちます。この文書はその**外部契約**を定義します。

> **この文書の範囲**
> ここに載っているのは、サービストークンで読める 5 本 + ヘルスチェックだけです。
> 取り込み・同期・設定などの書き込み系エンドポイントは **Web UI 専用の内部 API** で、
> 予告なく変わります。外部から依存しないでください。

---

## 1. 認証

CS は 2 つの動作モードを持ちます。

| モード | 起動 | 認証 |
|---|---|---|
| シングルユーザー | `crypto-summary web` | 認証なし。API はそのまま開いている |
| マルチユーザー | `create_app(data_dir=...)`（Docker 構成） | Google OAuth セッション、または**サービストークン** |

### サービストークン（マルチユーザー時）

`.env` の `CS_SERVICE_TOKEN` に共有シークレットを設定すると有効になります。
**未設定なら機能ごと無効**で、従来どおりの挙動です。

```bash
# 生成
python -c "import secrets; print(secrets.token_hex(32))"
```

リクエストには 2 つのヘッダーが要ります。

```
Authorization: Bearer <CS_SERVICE_TOKEN>
X-CS-User: <Google の sub（数字のみ・最大64桁）>
```

`X-CS-User` は読み取り対象ユーザーの台帳（`<sub>.db`）を選びます。

### エラー

| 状況 | ステータス |
|---|---|
| トークン未設定・不一致・`Bearer` 以外 | `401`（セッション認証へフォールスルー。「トークンが違う」ことは教えない） |
| `X-CS-User` が数字以外・空 | `400` |
| その sub の台帳が存在しない | `404`（**DB を暗黙作成しない**） |
| 書き込み系・管理系にトークンで到達 | `401` / `403`（サービス主体は決して管理者にならない） |

シングルユーザーモードでは、これらのヘッダーは**単に無視**されます。

---

## 2. 共通の約束事

- **数値はすべて文字列**です（`Decimal` を欠損なく渡すため）。`Number()` /
  `parseFloat` / `Decimal()` でそのまま読めます。極小値では
  指数表記（`"1E-8"` など）になり得ます。
- **`null` と `"0"` は別の意味**です。`null` は「判らない・取れなかった」、
  `"0"` は「ゼロだと判っている」。前日比の分母などで取り違えないでください。
- `currency` は `USD` / `JPY` / `EUR` / `GBP`。**未対応の値は `USD` に丸められます**
  （エラーにはなりません）。応答の `currency` に実際に使われた通貨が入ります。
- `warnings` は人間向けの文字列配列です。**入っていても応答は `200`** で、
  数値は「取れた範囲で正しい」状態です。空でなければ「部分的」と扱ってください。
- `generated_at` は ISO-8601（UTC）。
- 価格は CoinGecko 由来で、現在価格は 5 分・日次終値は永続キャッシュを挟みます。

### 集計から除外されるもの

`/api/summary`・`/api/sources`・`/api/account-assets` は共通で以下を落とします。

- **ダスト**: 絶対値が `0.00000001` 未満の残高
- **スパムエアドロップ**: 価格不明 ＋ 正の整数残高 ＋ 10 単位以下
- **表示設定で除外中のラベル**: 現在は「日次利息」トグル（`daily_interest`）

負の残高は落としません（ショート相当としてそのまま符号を持ちます）。

---

## 3. `GET /api/summary`

全ソース合算の資産サマリー。**AS が主に使うエンドポイント**です。

**クエリ**: `currency`（既定 `USD`）

```jsonc
{
  "currency": "JPY",
  "total_value": "4500000",        // 現在価格が取れた資産の評価額合計
  "total_prev_value": "4200000",   // 前日終値ベースの合計。1件も取れなければ null
  "asset_count": 12,               // スパム除外後の資産数
  "priced_count": 10,              // うち現在価格が取れたもの
  "unpriced": ["MYSTERY"],         // 現在価格が取れなかった資産
  "prev_missing": ["USD"],         // value はあるが prev_value が無い資産（後述）
  "assets": [
    {
      "asset": "BTC",
      "balance": "0.3",
      "price": "15000000",         // 現在価格。取れなければ null
      "value": "4500000",          // balance × price。取れなければ null
      "has_price": true,
      "prev_price": "14000000",    // 前営業日の終値。取れなければ null
      "prev_value": "4200000",     // balance × prev_price。取れなければ null
      "prev_date": "2026-08-12"    // prev_price の基準日 (YYYY-MM-DD)。取れなければ null
    }
  ],
  "warnings": [],
  "generated_at": "2026-08-13T03:25:13.707716+00:00"
}
```

`assets` は**評価額の降順**で、価格が取れなかった資産が末尾に来ます。
`asset` は台帳に入っているままの表記（通常は大文字）です。

---

## 4. 前日比（`prev_*` / `total_prev_value`）の意味論

ここが一番間違えやすいので、独立して定義します。

### 定義

```
prev_value = いまの残高 × 前営業日の終値
day_change = value − prev_value
```

**「前日時点の残高」ではなく「いまの残高」**を使います。
これにより `day_change` は **価格が動いたぶんだけ**を表し、前日の入出金は混ざりません。
AS 内の銘柄が `当日の数量 ×（現在値 − 前日終値）` で計算しているのと定義が揃います。

前日に 1 BTC 入金していても、その入金額は前日比に現れません。

### `prev_date` — 基準日は「昨日」とは限らない

`prev_date` は **当日より前で価格が取れた最新の日**です。取得漏れの日があり得るため、
数日ぶんの窓を遡って探します。必ず**当日より過去**（UTC で完全に閉じた日）です。

利用側は `prev_date` を見て「古すぎる基準日なら前日比として出さない」判定をしてください。
CS 側はこの棄却をしません（ポリシーは利用側に委ねます）。

### `null` になる条件

`prev_price` / `prev_value` / `prev_date` は**3 つセット**で `null` になります。

- CoinGecko に登録の無い資産
- 履歴価格の取得に失敗した資産（このとき `warnings` に理由が載ります）
- **現在価格が取れていない資産**（`has_price: false`）
- 表示通貨と異なる法定通貨（例: `currency=JPY` で保有する `USD`）。
  現在価格はクロスレートで出せますが、**過去の為替は持っていない**ため前日値は出せません

### `total_prev_value` と `prev_missing`

`total_prev_value` は `prev_value` が取れた資産だけの合計です。1 件も無ければ `null`。

`prev_missing` は **`value` はあるのに `prev_value` が無い**資産の一覧です。
この資産は `total_value` に入るが `total_prev_value` には入らないため、
`total_value − total_prev_value` がその資産のぶんだけ過大になります。
**空でなければ「前日比は部分的」と表示してください。**

### 設計上の保証

- **現在価格が取れない資産には前日値を出しません。** これにより `total_value` と
  `total_prev_value` の対象資産が常に一致します。CoinGecko が落ちて価格が全滅しても、
  `total_value` が `"0"` に落ちる一方で `total_prev_value` が満額残り
  「前日比 −100%」になる、という事故は起きません（このとき両方 `"0"` と `null`）。
- **`prev_date` が当日になることはありません。** 窓の終端が構造的に前日以前です。
  当日の場中価格を掴んで前日比が常に 0 になることはありません。

### 注意点

- `prev_value` は負になり得ます（負残高＝ショート相当）。
- `prev_price` が `"0"` の資産（無価値化したトークン）では `total_prev_value` が
  `"0"` になり得ます。**割り算の前に 0 を弾いてください**（`"0"` と `null` は別物）。
- 資産が 0 件のとき `total_value` は `"0"`、`total_prev_value` は `null` です。

---

## 5. `GET /api/sources`

口座（グルーピング済み）ごとの評価額内訳。**前日値は含みません。**

**クエリ**: `currency`

```jsonc
{
  "currency": "USD",
  "sources": [
    {
      "source": "bitFlyer",              // 表示名（グルーピング後）
      "source_ids": ["bitflyer"],        // 実際のソースID
      "tx_count": 128,
      "asset_count": 3,
      "total_value": "31500",
      "first_ts": "2024-01-01T00:00:00+00:00",  // 取引が無ければ null
      "last_ts": "2026-08-10T12:00:00+00:00"
    }
  ],
  "warnings": [],
  "generated_at": "..."
}
```

`sources` は `total_value` の降順です。

---

## 6. `GET /api/account-assets`

1 口座内の資産内訳。**前日値は含みません。**

**クエリ**: `account`（必須・表示名）、`currency`

```jsonc
{
  "currency": "USD",
  "account": "bitFlyer",
  "assets": [
    { "asset": "BTC", "balance": "0.5", "price": "60000",
      "value": "30000", "has_price": true }
  ],
  "total_value": "30000",
  "warnings": [],
  "wallets": [                      // この口座に紐づくウォレット（あれば）
    { "source_id": "...", "address": "0x...", "chain": "arbitrum",
      "chain_label": "Arbitrum" }
  ]
}
```

`generated_at` はありません。

---

## 7. `GET /api/asset-accounts`

1 資産を、どの口座にどれだけ持っているか。**前日値は含みません。**

**クエリ**: `asset`（必須・シンボル）、`currency`

```jsonc
{
  "currency": "USD",
  "asset": "BTC",
  "price": "60000",                 // 取れなければ null
  "accounts": [
    { "account": "bitFlyer", "balance": "0.5", "value": "30000" }
  ],
  "total_balance": "0.8",
  "total_value": "48000",
  "warnings": []
}
```

`accounts` は残高の絶対値の降順です。`generated_at` はありません。

---

## 8. `GET /api/portfolio-history`

評価額の日次時系列。

**クエリ**
- `currency`
- `range`: `7d` | `30d` | `90d` | `1y` | `all`（不正な値は `90d` に丸め）
- `scope`: `total` | `account:<表示名>` | `asset:<シンボル>`

```jsonc
{
  "currency": "USD",
  "range": "90d",
  "scope": "total",
  "points": [
    { "t": "2026-08-12", "value": "44500" }
    // scope=asset:<SYM> のときは "balance" も付く
  ],
  "unpriced": [],
  "is_partial": false,   // warnings があるか、価格未取得の資産があれば true
  "warnings": [],
  "generated_at": "..."
}
```

注意点:

- **その日どの資産も価格が取れなければ、その日は `points` から丸ごと落ちます。**
  日付が連続する保証はなく、`points[0]` が範囲の開始日とも限りません。
- 取引がまったく無い場合は `points: []` を返しますが、
  このときだけ **`is_partial` キーが存在しません**。`data.is_partial ?? false`
  のように読んでください。
- `range` を長くすると CoinGecko の粒度が変わります（90 日以内は時間足、
  超えると日足）。長期レンジは 1 日ぶんずれ得ます。

前日比だけが欲しい場合は、このエンドポイントを資産ごとに叩かず
**`/api/summary` の `prev_value` を使ってください**（リクエスト 1 本で済みます）。

---

## 9. `GET /api/health`

死活監視用。**認証不要**で、個人情報を返しません。

```json
{ "status": "ok" }
```

---

## 10. 互換性の方針

- 応答には**フィールドを追加することがあります**。利用側は未知のキーを
  無視できるようにしてください。
- 既存フィールドの**意味と型は変えません**。変える必要が出たら新しい名前を足します。
- 追加フィールドは古い利用側で壊れないよう、常に「無ければ従来動作」になる形で入れます。
  逆に**新しい利用側は、古い CS が新フィールドを返さない前提のフォールバックを持って**ください
  （AS と CS は別々にデプロイされます）。

例: `total_prev_value` が無い応答を受け取ったら、AS は
`scope=total` の履歴から前日比を求める従来経路にフォールバックします。

---

## 関連

- 環境変数の一覧: [`../README.md`](../README.md)
- Docker / リバースプロキシ構成: [`deploy.md`](./deploy.md)
- CLI コマンド: [`commands.md`](./commands.md)
- 手動確認の項目: [`verification_checklist.md`](./verification_checklist.md)
