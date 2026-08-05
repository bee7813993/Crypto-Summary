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
| `pbr` | PBR Lending（入出金履歴／貸出日次レポートを自動判定）|
| `pbr_crawl` | PBR Lending クローラーの正規化 JSON（取り込みは `sync-pbr`）|

#### PBR Lending の取り込み方

`pbr` を選べば入出金履歴・貸出日次レポートのどちらでもヘッダーから自動判定される。
役割は固定で、日付による分岐は無い:

- **入出金履歴** — PBR 口座への入金・出金の唯一の情報源（全期間）。
  入庫→DEPOSIT / 出庫→WITHDRAW、利息・返還利息・プレミアム満期→REWARD。
  貸出・返還は貸出準備ウォレットとの内部移動なので計上しない。
- **貸出日次レポート** — 利確利息（各種「利確数量」列）のみ。
  貸出数量／返還数量は入出金履歴と同一イベントのため計上しない。

保有残高を得るには入出金履歴の取り込みが必須（日次レポートだけでは利息しか入らない）。
2026-03-03 以降の日次レポートは取り込まないこと — 「返還受取利息（利確数量）」と
入出金履歴の「返還利息」は同一イベントで、重複排除が効かず二重計上になる。

日次の利息を残高・エクスポートに含めるかは、Web の「インポート」ページ末尾にある
「集計の設定」で切り替えられる（取り込み自体は常に行われる）。

#### 当年分をクローラーから取り込む（PBRLending-History-Check 連携）

PBR Lending は**当年分の公式取引履歴 CSV を出さない**。当年のデータはローカルの
クローラー [PBRLending-History-Check](https://github.com/) で取得し、その正規化 JSON
（`outputs/pbrlending_crawled.latest.json`）を直接読み込んで取り込む。

`.env` にクローラーの出力ディレクトリを設定すると連携が有効になる:

```bash
PBR_CRAWL_DIR=J:/Git/PBRLending-History-Check/outputs
PBR_VIEWER_URL=http://127.0.0.1:4173   # 省略可（既定値）
```

- 画面を開いたときに**新しいクロール結果があれば自動で取り込む**
- インポート画面の「PBR Lending クローラー同期」カードから手動実行もできる
- サイドバーの「PBR クローラー」タブにクローラーのビューア画面が開く
  （表示にはクローラー側のビューア起動が必要: `npm run viewer`）
- CLI: `crypto-summary sync-pbr`

取り込みは**対象期間の洗い替え**で行う（追記ではない）。クローラーは同じ期間を何度でも
取り直すため、毎回その期間のデータを入れ替えることで、何度実行しても結果が同じになり、
再クロールでの訂正もそのまま反映される。他ソースのデータには触れない。

##### クローラー画面へ手動インポートした分（過年度の公式 CSV など）

クロール結果は当年分しか持たない。公式 CSV をクローラー画面に手動インポートしていれば、
その内容（`viewer_ledger.json` / `viewer_transfers.json`）も同期で取り込む。
**クロールしていなくても取り込める**ので、PBR のデータをクローラー側で一元管理できる。

二重計上を防ぐため、取り込む範囲を 2 つの条件で絞っている:

- **クロールがカバーする期間は読まない**。その期間はクロール結果を正とする
  （クローラー画面の表示はクロール分と手動分が混ざった状態なので、そのまま全部
  取り込むと重複する）。期間の外側は前も後ろも取り込み、クロール結果が無ければ
  全期間を取り込む
- **公式 CSV を Crypto-Summary に直接取り込み済みの年は読まない**。その年に `pbr` の
  取引が 1 件でもあれば、ビューア側の同じ年は丸ごとスキップする

そのため公式 CSV は「クローラー画面に読ませる」「Crypto-Summary に直接取り込む」の
どちらでもよく、両方に読ませても重複しない。

自動取り込みは、クロール結果と `viewer_*.json` の更新をまとめて見ている。
クローラー画面に CSV を読ませるだけでも、次に Crypto-Summary を開いたときに反映される。

**年が明けたら**（公式の年間履歴 CSV が公開されたら）:

1. その年のクロール由来データを削除する
   （同期カードの「年次パージ」または `crypto-summary sync-pbr --purge-year 2026`）
2. 公式 CSV を取り込む。クローラー画面に読ませても、Crypto-Summary に `pbr` として
   直接取り込んでも、どちらでもよい
3. 翌年の同期は前年に触れない（クロールの JSON は当年分のみを持つため）

クロール由来データはソース `pbr_crawl`、公式 CSV を直接取り込んだものは `pbr` と
分かれているので、パージは公式データに影響しない。

> 当年の同期では、手作りの CSV で取り込んでいたクロール期間の `pbr` 行も
> 一緒に洗い替えられる（同じデータの重複を避けるため）。その結果、当時の
> インポート履歴バッチは残るが取引数が減る。
> **クロールがカバーしない期間を含むバッチを消すと、その分は同期では戻らない**
> （削除ダイアログで警告が出る）。過年度データはクローラー画面に手動インポート
> しておくと、同期で復元できる。

Crypto-Summary を HTTPS で公開している場合、`http://` のビューアは mixed content で
ブロックされて iframe に表示されない（「新しいタブで開く」は使える）。

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

各形式は対応サービスへのインポート用 CSV です。Cryptact・Koinly・SUMM はいずれも海外資産の損益計算と国内確定申告の資料作成に対応しています。

| 形式 | 対応サービス |
|---|---|
| **Cryptact** | [Cryptact](https://www.cryptact.com/)（カスタムファイル） |
| **Koinly** | [Koinly](https://koinly.io/)（Universal CSV） |
| **SUMM** | [SUMM](https://summ.com/)（旧 Crypto Tax Calculator・カスタム CSV） |

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
| `SECRET_KEY` | セッション署名キー。未設定なら `DATA_DIR/_session_key` に自動生成される（明示する場合は `python -c "import secrets; print(secrets.token_hex(32))"`） |
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

## クラウドで動かす

アプリだけをクラウドのコンテナサービスへ置き、PBR Lending のクローラーは手元に
残して、取得データ（`outputs`）をファイル同期で運ぶ構成にできます。

- 手順と注意点: [`docs/deploy.md`](./docs/deploy.md)
- 構成例: [`docker-compose.cloud.yml`](./docker-compose.cloud.yml)

台帳が SQLite なので、**永続ブロックボリュームが付くサービス**（Fly.io / Render /
Railway / 小さな VM）を選び、インスタンスは 1 台に固定します。ファイルシステムが
揮発する Cloud Run や App Runner はそのままでは使えません。

クローラーをクラウドに置かないのは、クロールが PBR Lending への手動ログインを
伴う対話作業であり、noVNC が口座にログイン済みブラウザの全操作権限を与えるためです。

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
