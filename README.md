# Crypto-Summary

暗号資産ポートフォリオの残高・評価額・推移を一元管理するセルフホスト型ツールです。複数の取引所・ウォレットの取引履歴を取り込んで正規化し、Web ダッシュボードで可視化したり、確定申告ソフト（Cryptact / Koinly）向けの CSV としてエクスポートできます。

> 価格データは [CoinGecko](https://www.coingecko.com/)（読み取り専用）から取得します。取引所・ウォレットの API キーは**読み取り専用権限のみ**で動作し、出金・送付の権限は一切不要です。

---

## 主な機能

- 📊 **Web ダッシュボード** — 総資産評価額、資産構成（円グラフ）、資産推移グラフ、口座別・資産別の内訳
- 🔌 **多様なデータソース** — 取引所 API / CSV インポート / EVM・Solana ウォレット取得に対応
- 💱 **マルチ通貨表示** — USD / JPY / EUR / GBP（日本円は「億万千円」表示にも対応）
- 🧾 **確定申告用エクスポート** — 年・口座を指定して Cryptact / Koinly 形式の CSV を出力
- 🔐 **マルチユーザー対応** — Google OAuth ログインでユーザーごとにデータを分離（Docker 運用時）
- 🌐 **日英 i18n / ダークモード / 金額マスクモード**
- 🪙 **暗号資産アイコン表示**（CoinGecko 画像）

---

## アーキテクチャ

「正規化された中間データ（Canonical Transaction）」を介して、**データソース（N種）とエクスポート形式（M種）を疎結合**にする2段構成です。

```
[Source Adapters]          [Core]                [Sink Adapters]
 取引所API / CSV       →  正規化(Canonical)  →  Cryptact CSV
 EVM / Solana          →  重複排除・台帳保存  →  Koinly CSV
                       →  SQLite 永続化       →  SUMM CSV
                                              ↓
                                       [Web UI / CLI]
```

- **Core** は外部 I/O を持たない純粋ロジック（テスト容易）
- **Source / Sink** は共通インターフェースのプラグイン（新規取引所 = 1ファイル追加）
- 設計の詳細は [`DESIGN.md`](./DESIGN.md) を参照

### 技術スタック

| 領域 | 使用技術 |
|---|---|
| バックエンド | Python 3.11+ / FastAPI / SQLite |
| フロントエンド | Vanilla JS / Chart.js |
| CLI | Click / Rich |
| 価格データ | CoinGecko API（read-only） |
| 認証（任意） | Google OAuth 2.0（authlib） |

---

## クイックスタート

### 1. セットアップ

Python 3.11 以上が必要です。

```bash
# 仮想環境を作成・有効化
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1

# インストール（dev には Web UI + テストツールが含まれる）
pip install -e ".[dev]"
```

> Windows PowerShell で `Activate.ps1` が拒否される場合は、一度だけ
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を実行してください。

### 2. データを取り込む

```bash
# 取引所の CSV を取り込む
crypto-summary import --file TradeHistory.csv --exchange bitflyer

# 利用可能なソース一覧
crypto-summary sources
```

### 3. ダッシュボードを起動

```bash
crypto-summary web
# → http://127.0.0.1:8000 をブラウザで開く

# 同じ Wi-Fi のスマホ等から見る場合
crypto-summary web --lan
```

詳しいコマンドは [`docs/commands.md`](./docs/commands.md) を参照してください。

---

## 対応データソース

### 取引所（CSV インポート）

| ソース名 | 説明 |
|---|---|
| `bitflyer` | bitFlyer 現物取引履歴 |
| `bitflyer_collateral` | bitFlyer FX/CFD 証拠金履歴 |
| `bitflyer_conversion` | bitFlyer 両替履歴 |
| `gmo` | GMOコイン取引履歴 |
| `binance` | Binance スポット履歴 |
| `nexo_spot` / `nexo_dnw` | Nexo Pro スポット取引 / 入出金 |
| `nexo_savings` | Nexo 貯蓄口座 |
| `bitlend` | BitLending 貸出履歴 |
| `pbr_lending` | PBR Lending 貸出履歴 |

### 取引所（API 直接取得）

- **bitFlyer** / **Bybit** — `crypto-summary fetch` で取得（読み取り専用キー）

### ブロックチェーン（API 直接取得）

- **EVM 5チェーン**（Ethereum / Arbitrum / Polygon / Base / Optimism）— Etherscan V2 API
- **Solana** — Helius API

```bash
crypto-summary fetch-wallet --chain arbitrum --wallet 0xABC...123
crypto-summary fetch-wallet --chain solana --wallet YOURWALLET...
```

> API 直接取得には各サービスの API キーが必要です。取得・設定方法は [`docs/api-keys.md`](./docs/api-keys.md) を参照してください。

---

## エクスポート形式

| 形式 | 用途 |
|---|---|
| **Cryptact** | 国内確定申告（カスタムファイル） |
| **Koinly** | 海外対応の損益計算 |
| **SUMM** | 国内税制向け |

Web UI の「取引履歴」ページから、年・口座・形式を指定してワンクリックでダウンロードできます。CLI では `crypto-summary export --sink koinly` を使用します。

---

## Docker での運用（マルチユーザー）

Google アカウントでログインし、ユーザーごとにデータを分離して運用できます。

### 1. 環境変数を設定

```bash
cp .env.example .env
# .env を編集して各種キーを設定
```

主な環境変数（詳細は `.env.example` 参照）:

| 変数 | 用途 |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth 認証 |
| `SECRET_KEY` | セッション署名キー（`python -c "import secrets; print(secrets.token_hex(32))"` で生成） |
| `CS_SECRET_KEY` | API キー暗号化マスター鍵（`crypto-summary account gen-key` で生成）。Web 画面からのキー登録に必要 |
| `BASE_URL` | アプリの公開 URL |
| `COINGECKO_API_KEY` | CoinGecko Demo キー（任意・レート制限緩和） |

> 取引所・ウォレットの API キーは Web の「インポート」画面から登録でき（暗号化保存）、テキスト編集は不要です。詳細は [`docs/api-keys.md`](./docs/api-keys.md) を参照。

### 2. 起動

```bash
docker compose up -d --build
```

> ⚠️ `.env` の変更を反映するには `docker compose down && docker compose up -d` が必要です（`restart` では再読み込みされません）。

---

## CoinGecko API キー（任意・高速化）

無料の **CoinGecko Demo API キー**を設定すると、レート制限が 30 req/分に緩和され、価格・推移グラフの取得が安定・高速化します。

1. [CoinGecko の料金ページ](https://www.coingecko.com/en/api/pricing) で「Demo」プランに登録 → キー発行
2. `.env` に `COINGECKO_API_KEY=CG-xxxx` を設定

未設定でもキーなしで動作します（429 が出やすくなります）。

---

## セキュリティについて

- 取引所・ウォレットの **API キーは読み取り専用権限のみ**を付与してください。**出金・送付・注文の権限は絶対に付与しないこと**。
  - 例：bitFlyer は「資産残高を見る」「取引履歴を見る」のみ
- シークレットは `.env` / 環境変数 に保存し、**リポジトリには絶対に含めないこと**（`.env` は `.gitignore` 済み）。
- API キーを暗号化保存する場合は `crypto-summary account gen-key` でマスター鍵を生成して `.env` の `CS_SECRET_KEY` に設定します。

---

## 開発

```bash
# テスト実行
pytest

# テスト（カバレッジ付き）
pytest --cov=crypto_summary
```

- プロジェクト設計: [`DESIGN.md`](./DESIGN.md)
- コマンドリファレンス: [`docs/commands.md`](./docs/commands.md)
- API キーのセットアップ: [`docs/api-keys.md`](./docs/api-keys.md)
