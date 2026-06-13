# Crypto-Summary 設計書

暗号資産取引所の取引履歴を取得し、Koinly / SUMM(Cryptact) 等の集計アプリへ
自動で取り込むためのシステム設計。

- 対象ソース: 国内取引所・海外取引所・DEX/オンチェーン（汎用拡張前提）
- 取込方式: CSV 自動生成を中核に、Koinly API 連携を補完として併用
- 実行形態: CLI 先行 → 後からスケジューラ自動化（GitHub Actions / cron）
- 実装言語: Python 3.11+

---

## 1. 全体方針

### 1.1 なぜ「CSV 中核 + API 補完」か

| 方式 | 長所 | 短所 | 採否 |
|------|------|------|------|
| 各アプリの汎用CSV生成 | 全アプリ対応・監査可能・冪等再実行が容易・APIなしでも動く | フォーマット差異の保守 | **中核** |
| Koinly API 直接投入 | 完全自動・取込確認まで一気通貫 | 対応API範囲が限定的・rate limit | 補完 |
| 国内アプリ(SUMM/Cryptact)カスタムCSV | 国内税制・銘柄対応が手厚い | アプリ固有仕様 | アダプタで対応 |

→ **「正規化された中間データ(Canonical Transaction)」を一度作り、そこから各アプリ形式へ変換**する
2 段構えにする。これによりソース追加とアプリ追加が独立して行える（N×M を N+M に削減）。

### 1.2 パイプライン

```
[Source Adapters]            [Core]                 [Sink Adapters]
 取引所API / CSV取込   →   正規化(Canonical)   →   Koinly CSV
 DEX / チェーン        →   重複排除・補正      →   Cryptact カスタムCSV
                       →   永続化(履歴台帳)    →   SUMM形式
                                                →   Koinly API
```

---

## 2. アーキテクチャ

### 2.1 レイヤ構成

```
src/crypto_summary/
├── core/
│   ├── models.py          # Canonical Transaction スキーマ(pydantic)
│   ├── normalize.py       # ソース固有 → Canonical 変換ヘルパ
│   ├── dedup.py           # 重複排除・冪等キー
│   ├── ledger.py          # 取得済みデータの永続化(SQLite)
│   └── fx.py              # 法定通貨換算(任意/国内税制向け)
├── sources/               # === 取得側アダプタ ===
│   ├── base.py            # SourceAdapter インターフェース
│   ├── ccxt_source.py     # 海外取引所(Binance/Bybit/OKX...) ccxt経由
│   ├── jp/
│   │   ├── bitflyer.py
│   │   ├── coincheck.py
│   │   ├── bitbank.py
│   │   └── gmo.py
│   ├── csv_import.py      # 取引所の手動DL CSVを取り込む汎用ローダ
│   └── onchain/
│       ├── evm.py         # Etherscan系API / RPC でアドレス履歴取得
│       └── dex.py         # Uniswap等のスワップ解釈
├── sinks/                 # === 出力側アダプタ ===
│   ├── base.py            # SinkAdapter インターフェース
│   ├── koinly_csv.py
│   ├── koinly_api.py
│   ├── cryptact.py        # カスタムファイル形式
│   └── summ_mcp.py        # MCP JSON-RPC 直接 push (https://mcp.summ.com/mcp)
├── config.py              # 設定・シークレット読込
├── pipeline.py            # 取得→正規化→出力のオーケストレーション
└── cli.py                 # CLI エントリポイント
```

### 2.2 設計上の要点

- **Source / Sink は共通インターフェースのプラグイン**。新規取引所＝`sources/`に1ファイル追加。
- **Core は外部I/Oを持たない純粋ロジック**にしてテスト容易性を確保。
- 海外取引所は **ccxt** に寄せて実装コストを最小化。国内・特殊仕様は個別アダプタ。

---

## 3. データモデル（Canonical Transaction）

集計アプリは概ね「いつ・何を・いくら・手数料」を要求する。最大公約数を正規形にする。

```python
class TxType(str, Enum):
    TRADE     = "trade"      # 売買(同時に2通貨が動く)
    DEPOSIT   = "deposit"    # 入金
    WITHDRAW  = "withdraw"   # 出金
    FEE        = "fee"       # 単独手数料
    REWARD    = "reward"     # ステーキング/レンディング報酬・エアドロップ
    TRANSFER  = "transfer"   # 自ウォレット間移動

class CanonicalTx(BaseModel):
    id: str               # 冪等キー(source + 取引所txid のハッシュ)
    source: str           # "binance" / "bitflyer" / "evm:0x.." 等
    timestamp: datetime   # UTC 保持(出力時にJST等へ変換)
    type: TxType
    # 受領側 / 支払側 を分けて表現(売買は両方埋まる)
    received_asset: str | None
    received_amount: Decimal | None
    sent_asset: str | None
    sent_amount: Decimal | None
    fee_asset: str | None
    fee_amount: Decimal | None
    label: str | None     # reward/airdrop/staking 等のヒント
    tx_hash: str | None   # オンチェーンの場合
    raw: dict             # 監査用に元データを保持
```

- 金額はすべて **`Decimal`**（浮動小数点誤差を排除）。
- `id` を冪等キーにして、再取得しても重複登録しない（§5）。
- `raw` を保持し、変換ミスの追跡と再変換を可能にする。

---

## 4. 取得（Source）の方式別ポイント

### 4.1 海外取引所（ccxt）
- `ccxt` の `fetch_my_trades` / `fetch_deposits` / `fetch_withdrawals` を利用。
- ページング・rate limit は `enableRateLimit=True` と `since`/`until` カーソルで対応。
- 先物・証拠金・資金調達料(funding)は取引所ごとに別エンドポイントになるため、
  必要に応じて段階対応（まず現物 → 先物）。

### 4.2 国内取引所
- API のある取引所(bitbank, GMO, bitFlyer 等)は個別アダプタで実装。
- API が貧弱/未提供の機能は **公式CSVエクスポートの取込** で補完（`csv_import.py`）。
- 日本円建ての損益計算が必要な場合は §6 を参照。

### 4.3 DEX / オンチェーン
- EVM 系: Etherscan/各チェーンエクスプローラAPI または RPC でアドレスの
  `normal/internal/erc20/erc721` トランザクションを取得。
- スワップは送出/受領 ERC20 transfer の差分から `TRADE` として解釈。
- ガス代は `FEE`(ネイティブ通貨建て) として付与。
- 注意: アドレスを指定する読み取り専用。秘密鍵は一切扱わない。

---

## 5. 冪等性・重複排除・差分取得

集計アプリへの二重計上が最大のリスクのため、ここを最優先で堅牢化する。

- **冪等キー** `id = hash(source, exchange_txid)`。オンチェーンは `tx_hash + log_index`。
- 取得済みデータは **SQLite 台帳(ledger)** に保存し、`id` で UPSERT。
- 各ソースごとに **最終取得時刻カーソル** を保存し、次回はそれ以降のみ取得（差分）。
- 出力は「未エクスポート分のみ」または「期間指定」を選べるようにし、
  アプリ側へ渡したレコードに `exported_at` を記録 → 再アップロード事故を防止。

---

## 6. 法定通貨換算（任意 / 国内税制向け）

- Koinly はアプリ側で時価を補完できるため通常は不要。
- SUMM/Cryptact 等で円建て損益が必要な場合のみ、`core/fx.py` で
  約定時刻の対円レート（取引所建値 or 外部レートAPI）を付与。
- レート取得元は差し替え可能なインターフェースにする。

---

## 7. Sink（出力）

| Sink | モジュール | 形式 | 備考 |
|------|-----------|------|------|
| Koinly CSV | `koinly_csv.py` | Universal CSV | **最初に実装。**最も汎用・監査しやすい |
| Koinly API | `koinly_api.py` | REST API 投入 | 対応範囲確認後に追加。失敗時は CSV へフォールバック |
| Cryptact | `cryptact.py` | カスタムファイル | アクション(BUY/SELL/BONUS...)へマッピング |
| **SUMM MCP** | `summ_mcp.py` | **MCP (JSON-RPC)** | **最もモダンな直接 push 方式（後述）** |

- 各 Sink は `Iterable[CanonicalTx] → ファイル/POST` の単純関数。
- マッピングは表形式の設定で管理し、コード変更なしで微調整可能にする。

### 7.1 SUMM MCP 連携の詳細

SUMM (旧 Crypto Tax Calculator) は MCP サーバー (`https://mcp.summ.com/mcp`) を公開しており、
JSON-RPC 2.0 (POST) で直接トランザクションを push できる。CSV 不要・即時反映が特長。

```
[Canonical Tx] → summ_mcp.py → POST https://mcp.summ.com/mcp
                                Authorization: Bearer <SUMM_API_TOKEN>
```

**実装メモ**:
- 認証: Bearer トークン（SUMM アカウントから発行）
- プロトコル確認: `initialize` → `tools/list` で利用可能メソッドを動的取得
- 冪等性: `id`（冪等キー）を MCP リクエストに含め、SUMM 側での重複登録を防ぐ
- エラー時: MCP 失敗 → Cryptact 互換 CSV に自動フォールバック

```python
# summ_mcp.py のインターフェースイメージ
class SummMcpSink(SinkAdapter):
    endpoint = "https://mcp.summ.com/mcp"

    def export(self, txs: Iterable[CanonicalTx]) -> ExportResult:
        # 1. initialize handshake
        # 2. tools/list でサポートメソッド確認
        # 3. バッチ push（失敗時は CSV フォールバック）
        ...
```

---

## 8. 設定・シークレット管理

```yaml
# config.yaml （APIキー本体は環境変数 or .env、ここには参照のみ）
sources:
  - id: binance
    type: ccxt
    exchange: binance
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
  - id: my_eth_wallet
    type: evm
    address: "0x...."
    chain: ethereum
sinks:
  - id: koinly
    type: koinly_csv
    out: ./out/koinly.csv
  - id: summ
    type: summ_mcp
    token_env: SUMM_API_TOKEN
    fallback: cryptact_csv       # MCP 失敗時のフォールバック先
    fallback_out: ./out/cryptact.csv
```

- **APIキーは読み取り専用権限のみ**を発行（出金権限は付与しない）を必須要件として明記。
- シークレットは `.env` / 環境変数 / GitHub Actions Secrets。リポジトリには絶対に含めない。

---

## 9. CLI（先行実装）

```
crypto-summary fetch  --source binance --since 2024-01-01   # 取得して台帳へ
crypto-summary fetch  --all                                  # 全ソース差分取得
crypto-summary export --sink koinly --since 2024-01-01      # 台帳→CSV出力
crypto-summary run    --all --sink koinly                    # fetch+export一括
crypto-summary status                                         # 台帳の件数/カーソル確認
```

## 10. 自動化（後段）

- GitHub Actions の `schedule` (cron) で定期 `run`。Secrets に APIキー、
  生成物は Artifact 保存 or Koinly API 投入。
- もしくはローカル cron / コンテナ。Core は同一なので実行層だけ差し替え。

---

## 11. 段階的な実装ロードマップ

1. **M1 — 骨格**: models / ledger / CLI 雛形 / dedup。テスト整備。
2. **M2 — 海外1取引所(ccxt, 例: Binance現物) + Koinly CSV** の一気通貫を完成。
3. **M3 — 国内取引所1つ**（API or CSV取込）を追加。
4. **M4 — オンチェーン(EVM 1チェーン)** 対応。
5. **M5 — Cryptact / SUMM Sink** 追加、必要なら円換算。
6. **M6 — 自動化(GitHub Actions)** とエラー通知。

各マイルストーンで「取得→正規化→出力」が end-to-end で動く状態を保つ。

---

## 12. リスクと非機能要件

- **二重計上の防止**: §5 を最優先。出力前に件数・期間のサマリを表示し確認を挟む。
- **数値精度**: Decimal 徹底、丸めは出力直前のみ。
- **セキュリティ**: 読み取り専用APIキー、秘密鍵不使用、シークレットの分離。
- **API変更耐性**: `raw` 保持と再変換可能設計で、マッピング修正後の再生成を容易に。
- **税務責任**: 本ツールは集計補助。最終的な税務判断は利用者/税理士が行う旨を明記。
