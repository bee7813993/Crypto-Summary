# API キーのセットアップ

Crypto-Summary は外部サービスからデータを取得するために、いくつかの API キーを使います。すべて **読み取り専用** で動作し、出金・送付の権限は一切不要です。

キーは `.env` ファイル（または OS 環境変数）に設定します。`.env.example` をコピーして使ってください。

```bash
cp .env.example .env
# .env を編集して各キーを設定
```

> ⚠️ `.env` は `.gitignore` 済みです。**API キーをリポジトリにコミットしないでください**。
> Docker 運用時は `.env` 変更後に `docker compose down && docker compose up -d` で再読み込みが必要です。

---

## キー一覧（どれが必要か）

| 用途 | 環境変数 | 必須？ | 発行元 |
|---|---|---|---|
| EVM ウォレット取得 | `ETHERSCAN_API_KEY` | EVM ウォレットを使うなら必須 | Etherscan |
| Solana ウォレット取得 | `HELIUS_API_KEY` | Solana ウォレットを使うなら必須 | Helius |
| bitFlyer API 取得 | `BITFLYER_API_KEY` / `BITFLYER_API_SECRET` | bitFlyer を API 連携するなら | bitFlyer |
| Bybit API 取得 | `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit を API 連携するなら | Bybit |
| 価格取得の高速化 | `COINGECKO_API_KEY` | 任意（推奨） | CoinGecko |

> CSV インポートだけを使う場合は、API キーは不要です（CoinGecko キーも任意）。

---

## Etherscan V2 API キー（EVM ウォレット）

Ethereum / Arbitrum / Polygon / Base / Optimism などの EVM チェーンのウォレット取引履歴を、**1つのキー**で取得できます。

### 取得手順

1. [https://etherscan.io/register](https://etherscan.io/register) でアカウント登録（無料）
2. [https://etherscan.io/myapikey](https://etherscan.io/myapikey) で「Add」→ API キーを発行
3. `.env` に設定：

```ini
ETHERSCAN_API_KEY=ここに発行されたキー
```

### ⚠️ 無料プランの対応チェーン

Etherscan V2 の **無料プランでは一部チェーンが非対応**です。

| チェーン | 無料プラン |
|---|---|
| Ethereum | ✅ 対応 |
| Arbitrum | ✅ 対応 |
| Polygon | ✅ 対応 |
| Base | ❌ 有料プランが必要 |
| Optimism | ❌ 有料プランが必要 |

無料プランで Base / Optimism のウォレットを同期しようとすると
`Free API access is not supported for this chain` というエラーになります。
これはキーが正しくても発生する制限です（対応チェーンのデータは正常に取得されます）。

> EVM ウォレットの同期は全 EVM チェーンを横断スキャンします。`Invalid API Key` が出る場合はキーの値が間違っているか未設定です。

---

## Helius API キー（Solana ウォレット）

Solana ウォレットの取引履歴取得に使います。

### 取得手順

1. [https://dev.helius.xyz](https://dev.helius.xyz) でアカウント登録（無料枠あり）
2. ダッシュボードで API キーを発行
3. `.env` に設定：

```ini
HELIUS_API_KEY=ここに発行されたキー
```

> Solscan の無料プランは取引履歴 API が使えないため Helius を採用しています。

---

## 取引所 API キー（bitFlyer / Bybit）

取引所の残高・取引履歴を API で自動取得します。**必ず読み取り専用権限のみ**を付与してください。

### 🔐 セキュリティ要件（厳守）

- **出金・送付・注文の権限は絶対に付与しないこと**
- bitFlyer：「資産残高を見る」「取引履歴を見る」**のみ**を付与
- Bybit：Wallet（参照）/ Trade（参照）のみ。Withdraw / Transfer は付与しない

### bitFlyer

1. bitFlyer の [API キー発行ページ](https://lightning.bitflyer.com/developer) でキーを発行（権限は参照系のみ）
2. `.env` に設定：

```ini
BITFLYER_API_KEY=your_api_key
BITFLYER_API_SECRET=your_api_secret
```

### Bybit

1. Bybit の API 管理画面で読み取り専用キーを発行
2. `.env` に設定：

```ini
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
```

### キーを暗号化保存する場合（任意）

`.env` に平文で置く代わりに、口座ごとに暗号化して保存できます。

```bash
# マスター鍵を生成して .env の CS_SECRET_KEY に設定
crypto-summary account gen-key

# 口座を登録（キーは暗号化されて <db名>.secrets.json に保存される）
crypto-summary account add-api --exchange bybit --source-id mybybit \
    --api-key xxx --api-secret yyy

# 取得
crypto-summary fetch --source-id mybybit
```

---

## CoinGecko Demo API キー（任意・高速化）

価格・推移グラフの取得に使う CoinGecko のレート制限を緩和します（30 req/分）。**未設定でも動作します**が、設定すると 429 エラーが出にくくなり高速化します。読み取り専用です。

### 取得手順

1. [https://www.coingecko.com/en/api/pricing](https://www.coingecko.com/en/api/pricing) で「Demo」プランに登録（無料）
2. API キーを発行
3. `.env` に設定：

```ini
COINGECKO_API_KEY=CG-xxxxxxxxxxxx
```

---

## 設定後の確認

```bash
# EVM ウォレットを取得してみる（CLI）
crypto-summary fetch-wallet --chain arbitrum --wallet 0xABC...123

# 取引所を取得してみる（CLI）
crypto-summary fetch --exchange bitflyer
```

Web UI の場合は、各口座・ウォレットの「同期」ボタンから取得できます。
`Invalid API Key` が出る場合は、対応する環境変数が `.env` に正しく設定されているか、
Docker なら `down → up` で再読み込みしたかを確認してください。
