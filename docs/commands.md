# crypto-summary コマンドリファレンス

## グローバルオプション

すべてのコマンドに共通して使えます。

```
crypto-summary [--db PATH] <command> [options]
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--db PATH` | `ledger.db` | 使用する SQLite ファイルのパス |

---

## コマンド一覧

| コマンド | 用途 |
|---|---|
| `web` | Web UI（ダッシュボード）を起動する |
| `status` | ledger の概要を確認する |
| `balance` | 資産残高を表示する |
| `show` | 取引履歴を表示する |
| `import` | 取引所CSVを取り込む |
| `import-wallet` | Arbiscan CSVを取り込む（EVM ウォレット） |
| `fetch` | 取引所APIから最新データを取得する（bitFlyer / Bybit） |
| `fetch-wallet` | ブロックチェーンAPIから取引履歴を取得する（EVM / Solana） |
| `add` | 手動でトランザクションを1件追加する |
| `remove` | トランザクションを削除する |
| `export` | 外部形式（Koinly 等）にエクスポートする |
| `clear` | ledger のデータを削除する（再取込前のリセット） |
| `sources` | `import --exchange` で使えるソース一覧を表示する |
| `account` | 口座の API キーを暗号化登録・管理する |

---

## web — Web UI 起動

```
crypto-summary web [--host HOST] [--port PORT] [--lan] [--reload]
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--host HOST` | `127.0.0.1` | バインドするホスト |
| `--port PORT` | `8000` | ポート番号 |
| `--lan` | — | 同じ Wi-Fi の端末（スマホ等）からアクセス可能にする |
| `--reload` | — | 開発用オートリロード（ファイル変更を自動反映） |

```bash
# 基本（ローカルのみ）
crypto-summary web

# 別のDBファイルを使う
crypto-summary --db my.db web

# スマホなど同じLANの端末から見る
crypto-summary web --lan

# ポート変更
crypto-summary web --port 8080
```

---

## status — 概要確認

```
crypto-summary status
```

ソース（口座）ごとのトランザクション件数と最終取込日時を表示します。

```bash
crypto-summary status
crypto-summary --db my.db status
```

---

## balance — 残高表示

```
crypto-summary balance [options]
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--source SRC` | 全ソース | 特定ソースに絞り込む（複数指定可） |
| `--by-source` | — | ソース（口座）ごとに分けて表示 |
| `--currency {USD,JPY,EUR,GBP}` | — | CoinGecko から価格取得して評価額を表示 |
| `--since YYYY-MM-DD` | — | この日以降の取引で集計 |
| `--until YYYY-MM-DD` | — | この日以前の取引で集計 |
| `--hide-dust` | 有効 | 微小残高（±0.00000001未満）を非表示 |

```bash
# 全資産の残高
crypto-summary balance

# JPY建て評価額つきで表示
crypto-summary balance --currency JPY

# 口座別
crypto-summary balance --by-source

# 特定ソースのみ
crypto-summary balance --source nexo_spot
```

---

## show — 取引履歴表示

```
crypto-summary show [options]
```

| オプション | デフォルト | 説明 |
|---|---|---|
| `--source SRC` | — | ソースで絞り込み |
| `--type TYPE` | — | 種別で絞り込み（`trade` / `deposit` / `withdraw` / `fee` / `reward` / `transfer`） |
| `--since YYYY-MM-DD` | — | 開始日 |
| `--until YYYY-MM-DD` | — | 終了日 |
| `--limit N` | `30` | 表示件数 |
| `--running-balance` | — | 各取引後の資産残高を表示 |

```bash
# 直近30件
crypto-summary show

# 特定ソースの最新50件
crypto-summary show --source bitflyer --limit 50

# 特定期間の入金だけ
crypto-summary show --type deposit --since 2025-01-01 --until 2025-12-31

# IDを確認してから削除する
crypto-summary show --source pbr_lending
crypto-summary remove --id <ID>
```

---

## import — CSV 取り込み

```
crypto-summary import --file FILE --exchange EXCHANGE [--source-id ID]
```

| オプション | 説明 |
|---|---|
| `--file PATH` | 取り込むCSVファイルのパス（必須） |
| `--exchange NAME` | 取引所・フォーマット名（必須）。`sources` コマンドで一覧確認 |
| `--source-id ID` | カスタムのソースID（省略時は exchange 名が使われる） |

```bash
# bitFlyer の取引履歴
crypto-summary import --file TradeHistory.csv --exchange bitflyer

# 同じ取引所を口座名つきで区別する
crypto-summary import --file nexo_txs.csv --exchange nexo_spot --source-id nexo_main

# 利用可能な exchange 名の一覧
crypto-summary sources
```

**主な exchange 名：**

| exchange | 対応ファイル |
|---|---|
| `bitflyer` | TradeHistory.csv（現物台帳） |
| `bitflyer_collateral` | CollateralHistory.csv（FX証拠金） |
| `bitflyer_conversion` | ConversionHistory.csv（両替） |
| `gmo` | GMOコイン取引履歴 |
| `binance` | Binanceスポット履歴 |
| `nexo_spot` | Nexo Pro スポット取引 |
| `nexo_dnw` | Nexo Pro 入出金 |
| `nexo_savings` | Nexo 貯蓄口座 |
| `bitlend` | BitLending 貸出履歴 |
| `pbr_lending` | PBR Lending 貸出履歴 |

---

## import-wallet — Arbiscan CSV 取り込み（EVMウォレット）

```
crypto-summary import-wallet --exchange arbiscan --wallet 0x... \
    --normal FILE [--erc20 FILE] [--internal FILE] [--source-id ID]
```

Arbiscan (arbiscan.io) のアドレスページから各タブの CSV をダウンロードして指定します。

```bash
crypto-summary import-wallet \
  --exchange arbiscan \
  --wallet 0xABC...123 \
  --normal export_normal.csv \
  --erc20 export_erc20.csv \
  --internal export_internal.csv \
  --source-id my_arbitrum
```

---

## fetch — 取引所API取得（bitFlyer / Bybit）

```
crypto-summary fetch [--exchange EXCHANGE] [--source-id ID] \
    [--api-key KEY] [--api-secret SECRET]
```

APIキーは **読み取り専用権限のみ** 付与してください（出金・送付権限は絶対に付与しないこと）。

```bash
# 事前に口座登録してから（推奨）
crypto-summary account add-api --exchange bitflyer --source-id bf \
    --api-key xxx --api-secret yyy
crypto-summary fetch --source-id bf

# 環境変数から（.env に BITFLYER_API_KEY / BITFLYER_API_SECRET を設定）
crypto-summary fetch --exchange bitflyer

# 直接指定
crypto-summary fetch --exchange bybit --api-key xxx --api-secret yyy
```

---

## fetch-wallet — ブロックチェーンAPI取得（EVM / Solana）

```
crypto-summary fetch-wallet --chain CHAIN --wallet ADDRESS [options]
```

| オプション | 説明 |
|---|---|
| `--chain` | `ethereum` / `arbitrum` / `polygon` / `base` / `optimism` / `solana` |
| `--wallet` | ウォレットアドレス（EVM: `0x...` / Solana: base58） |
| `--source-id ID` | カスタムのソースID |
| `--api-key KEY` | Etherscan V2 APIキー（EVM用、または環境変数 `ETHERSCAN_API_KEY`） |
| `--helius-api-key KEY` | Helius APIキー（Solana用、または環境変数 `HELIUS_API_KEY`） |
| `--no-gas` | ガス代を記録しない |

```bash
# Arbitrum ウォレット
crypto-summary fetch-wallet --chain arbitrum \
    --wallet 0xABC...123 --source-id my_arb

# Solana ウォレット
crypto-summary fetch-wallet --chain solana \
    --wallet YOURWALLET... --source-id my_sol
```

---

## add — 手動でトランザクション追加

CSVが出力されない期間の入出金などを手作業で補う場合に使います。

```
crypto-summary add --source ID --type TYPE --date DATE \
    [--received ASSET AMOUNT] [--sent ASSET AMOUNT] [--fee ASSET AMOUNT] [--note TEXT]
```

| `--type` の値 | 説明 |
|---|---|
| `deposit` | 入金・受取 |
| `withdraw` | 出金・送付 |
| `trade` | 売買 |
| `reward` | 報酬（レンディング利息など） |
| `fee` | 手数料 |
| `transfer` | 内部移動 |

```bash
# 入金
crypto-summary add --source pbr_lending --type deposit \
    --date 2026-01-13 --received USDC 3000

# 出金（メモつき）
crypto-summary add --source pbr_lending --type withdraw \
    --date 2026-06-02 --sent XRP 50 --note "返還"

# 売買
crypto-summary add --source manual --type trade \
    --date 2025-09-01 --sent JPY 500000 --received BTC 0.01

# 時刻まで指定
crypto-summary add --source myexchange --type reward \
    --date 2025-12-31T23:59:00 --received USDT 12.5
```

---

## remove — トランザクション削除

```
# 1件を ID で削除
crypto-summary remove --id TX_ID [--yes]

# CSV単位で削除（import したファイルと同じものを指定）
crypto-summary remove --file FILE --exchange EXCHANGE [--source-id ID] [--yes]
```

```bash
# IDを確認する
crypto-summary show --source pbr_lending

# 1件削除（確認プロンプトあり）
crypto-summary remove --id a1b2c3d4e5f6...

# 確認スキップ
crypto-summary remove --id a1b2c3d4e5f6 --yes

# CSVファイル全体を取り消す
crypto-summary remove --file TradeHistory.csv --exchange bitflyer
```

---

## export — 外部形式エクスポート

```
crypto-summary export --sink FORMAT [options]
```

| オプション | 説明 |
|---|---|
| `--sink FORMAT` | 出力形式。現在は `koinly` のみ（Web UIから Cryptact / Koinly を選択してエクスポートも可能） |
| `--source SRC` | ソース絞り込み |
| `--since YYYY-MM-DD` | 開始日 |
| `--until YYYY-MM-DD` | 終了日 |
| `--out PATH` | 出力ファイルパス（省略時: `out/koinly.csv`） |

```bash
crypto-summary export --sink koinly --since 2025-01-01 --until 2025-12-31
```

> **Web UI から確定申告用エクスポート**も可能です（Cryptact / Koinly 形式、年・口座・形式を選択）。

---

## clear — データ削除

```
crypto-summary clear [--source SRC] [--yes]
```

```bash
# 特定ソースだけ削除
crypto-summary clear --source nexo_spot

# 全データ削除（確認スキップ）
crypto-summary clear --yes
```

---

## account — APIキー管理

口座のAPIキーを暗号化してローカルに保存します。

### account gen-key — マスター鍵の生成

```
crypto-summary account gen-key
```

生成したキーを `.env` の `CS_SECRET_KEY` に設定してください。

### account add-api — 口座を登録

```
crypto-summary account add-api \
    --exchange EXCHANGE --source-id ID \
    --api-key KEY --api-secret SECRET \
    [--category CATEGORY]
```

```bash
crypto-summary account add-api \
    --exchange bitflyer --source-id bf \
    --api-key xxx --api-secret yyy

# Bybit（カテゴリ指定）
crypto-summary account add-api \
    --exchange bybit --source-id bybit_main \
    --api-key xxx --api-secret yyy --category spot
```

### account list-api — 登録済み口座一覧

```
crypto-summary account list-api
```

### account remove-api — 口座を削除

```
crypto-summary account remove-api --source-id ID
```

---

## 環境変数（.env）

| 変数名 | 用途 |
|---|---|
| `COINGECKO_API_KEY` | CoinGecko Demo APIキー（任意。設定するとレート制限が緩和される） |
| `BITFLYER_API_KEY` / `BITFLYER_API_SECRET` | bitFlyer API（読み取り専用） |
| `ETHERSCAN_API_KEY` | Etherscan V2 APIキー（EVM ウォレット取得用） |
| `HELIUS_API_KEY` | Helius APIキー（Solana ウォレット取得用） |
| `CS_SECRET_KEY` | 口座APIキーの暗号化マスター鍵（`account gen-key` で生成） |

---

## よくある使い方の流れ

### 初回セットアップ

```bash
# 1. DB を初期化しながら最初のデータを取り込む
crypto-summary import --file TradeHistory.csv --exchange bitflyer

# 2. 残高を確認
crypto-summary balance --currency JPY

# 3. Web UI を起動
crypto-summary web
```

### 定期更新（API連携）

```bash
# 登録済み口座から最新データを取得
crypto-summary fetch --source-id bf

# Web UI で確認
crypto-summary web
```

### シングルユーザー動作確認（開発・テスト）

```bash
# .env を読み込んで起動（カレントディレクトリの .env が自動読み込みされる）
crypto-summary web --lan

# 別のDBで試す
crypto-summary --db test.db import --file sample.csv --exchange bitflyer
crypto-summary --db test.db web --port 9000
```
