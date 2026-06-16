# 動作確認チェックリスト（SUMM対応〜API連携Web UI）

環境で確認できるようになった際に、上から順に確認してください。
各項目に「確認手順」と「期待結果」を記載しています。

> 前提: Web UI は `crypto-summary serve`（または該当の起動コマンド）で起動し、
> ブラウザで開いた状態を想定しています。CLI コマンドは `crypto-summary ...` で実行します。

---

## 0. 事前準備

- [ ] 依存パッケージのインストール
  ```bash
  pip install -e ".[dev]"   # cryptography / fastapi / uvicorn を含む
  ```
- [ ] テストが全て通ること
  ```bash
  python -m pytest -q        # 200 passed を確認
  ```
- [ ] マスター鍵を生成して環境変数に設定（API連携を試す場合に必須）
  ```bash
  crypto-summary account gen-key      # 出力された CS_SECRET_KEY=... を控える
  export CS_SECRET_KEY='（生成された鍵）'
  ```
  - ⚠ この鍵は `.env` / 環境変数 / Secrets に保存し、**リポジトリには絶対に含めない**こと。
  - ⚠ 鍵を紛失すると登録済みAPIキーは復号できなくなり、再登録が必要。

---

## 1. CSVエクスポート（Koinly / Cryptact / SUMM）

### 1-1. UIからのエクスポート
- [ ] 取引履歴ページを開く
- [ ] 右上の「CSVエクスポート」形式セレクトに 3 形式が表示される
  - Koinly（Universal CSV）
  - Cryptact（カスタムファイル）
  - SUMM（カスタムCSV）
- [ ] 形式を選んで「⬇ CSVエクスポート」を押すとファイルがダウンロードされる
- [ ] ファイル名が `<形式>_<今日の日付>.csv` 形式（口座フィルタ時は口座名入り）

### 1-2. フィルタ連動
- [ ] 口座フィルタを設定した状態でエクスポート → その口座の取引のみ含まれる
- [ ] 開始日・終了日を設定した状態でエクスポート → 期間内の取引のみ含まれる
- [ ] 結果メッセージに対象範囲（口座名 or 全口座）が表示される

### 1-3. 文字化け対策
- [ ] ダウンロードしたCSVをExcelで開いて日本語が文字化けしない（UTF-8 BOM付き）

---

## 2. SUMM カスタムCSV（正式仕様準拠）

参照: https://help.summ.com/en/articles/5777675-custom-csv-import

- [ ] ヘッダーが **14列すべて** 揃っている（1列でも欠けるとSUMM側で取込失敗）
  ```
  Timestamp (UTC), Type, Base Currency, Base Amount,
  Quote Currency (Optional), Quote Amount (Optional),
  Fee Currency (Optional), Fee Amount (Optional),
  From (Optional), To (Optional), Blockchain (Optional),
  ID (Optional), Reference Price Per Unit (Optional),
  Reference Price Currency (Optional)
  ```
- [ ] Type のマッピングが妥当か（サンプル取引で確認）
  - 売買(TRADE) → `buy`
  - 報酬(REWARD) → `staking` / `interest` / `income`
  - 手数料(FEE) → `fee`
  - 入金(DEPOSIT) → 法定通貨は `fiat-deposit` / 暗号資産は `receive`
  - 出金(WITHDRAW) → 法定通貨は `fiat-withdrawal` / 暗号資産は `send`
  - 振替(TRANSFER) → `send` / `receive`
- [ ] 法定通貨（JPY/USD/EUR/GBP/AUD/CAD/CHF）が fiat-deposit/withdrawal になる
- [ ] 可能であれば **SUMM に実際にインポートして取込エラーが出ない**ことを確認（最重要）

---

## 3. Cryptact カスタムファイル

- [ ] TRADE → `BUY`、REWARD → `BONUS`/`STAKING`/`LENDING`、FEE → `SENDFEE`
- [ ] DEPOSIT / WITHDRAW / TRANSFER は出力されない（自己資金移動＝非課税のためスキップ）
- [ ] 可能であれば Cryptact に取り込んで取込エラーが出ないことを確認

---

## 4. Koinly Universal CSV

- [ ] 各取引が Koinly の Universal フォーマット列に正しくマッピングされている
- [ ] 可能であれば Koinly に取り込んで取込エラーが出ないことを確認

---

## 5. 口座単位の全消去

- [ ] インポートページ →「既存の口座」テーブルで各口座に「全消去」ボタンがある
- [ ] 押すと確認ダイアログ（取引件数・口座名・ソースID表示）が出る
- [ ] 削除実行後、その口座配下の全ソースIDの取引が消える
- [ ] ダッシュボード/取引履歴の口座一覧からも消える
- [ ] 存在しない口座を指定した場合は 404（直接 API を叩く場合）

---

## 6. CSV単位の削除（インポート履歴）

- [ ] CSVインポート後、「インポート履歴（CSV単位で削除）」テーブルに行が追加される
  - 取込日時 / 口座 / 取引所ラベル / ファイル名 / 取引数
- [ ] 一部の取引が他CSVと重複していた場合、残存件数が `残存 / 取込` 形式で表示される
- [ ] 「CSVごと削除」でそのCSV由来の取引だけが削除される（他CSVの取引は残る）

---

## 7. Bybit V5 API 連携（read-only / CLI）

> ⚠ 登録するAPIキーは **読み取り専用** のみ。出金・送付・注文権限は絶対に付与しない。

### 7-1. CLI からの登録・一覧・削除
- [ ] 登録
  ```bash
  crypto-summary account add-api --exchange bybit \
    --source-id mybybit --api-key XXX --api-secret YYY
  # → 「登録しました」
  ```
- [ ] 一覧（秘密は表示されない）
  ```bash
  crypto-summary account list-api
  # → mybybit / bybit が出る。APIキー本体は表示されない
  ```
- [ ] 削除
  ```bash
  crypto-summary account remove-api --source-id mybybit
  # → 「削除しました」
  ```
- [ ] `CS_SECRET_KEY` 未設定で add-api するとエラー（「マスター鍵が未設定」）

### 7-2. 暗号化保存の確認
- [ ] `<db名>.secrets.json` が生成される
- [ ] そのファイルを開いて **APIキー/シークレットの平文が含まれない**こと
  （`api_key_enc` / `api_secret_enc` の暗号文のみ）
- [ ] ファイル権限が 600（所有者のみ）になっている

### 7-3. 実際の取得
- [ ] 実APIキー（読み取り専用）で fetch して約定・入金・出金が CanonicalTx に変換される
  ```bash
  crypto-summary fetch --source-id mybybit   # exchange はストアから解決される
  ```
- [ ] BTCUSDT 等のシンボルが base/quote に正しく分解される
- [ ] 2回目の fetch では cursor 以降の差分のみ取得される（重複は upsert で吸収）

---

## 8. API連携 Web UI（インポートページ「🔑 取引所 API」タブ）

> サーバー起動前に `CS_SECRET_KEY` を環境変数に設定しておくこと。

### 8-1. 登録フォーム
- [ ] APIタブに登録フォームが表示される（取引所/ソースID/APIキー/シークレット/カテゴリ）
- [ ] 冒頭に「読み取り専用キーのみ登録」「平文では保存されない」旨の注意書きがある
- [ ] APIキー/シークレット入力欄が `password` タイプでマスクされる
- [ ] 「登録して暗号化保存」を押すと成功メッセージが出る
- [ ] **登録成功後、APIキー/シークレット入力欄が即座にクリアされる**

### 8-2. 登録済み一覧
- [ ] 「登録済み API 口座」テーブルにソースID/取引所/カテゴリ/登録日時が表示される
- [ ] 秘密情報（APIキー本体）は一切表示されない

### 8-3. 同期
- [ ] 各行の「同期」ボタンを押すとサーバー側で fetch が走る
- [ ] 同期中はボタンが「同期中…」になり無効化される
- [ ] 完了後「N 件取得 / M 件新規追加」が表示される
- [ ] 取引履歴ページにデータが反映される

### 8-4. 削除
- [ ] 各行の「削除」ボタンで確認ダイアログが出る
- [ ] 削除すると **API登録だけが消え、取引データは残る**旨がメッセージに出る
- [ ] 一覧から消える

### 8-5. 鍵未設定時の挙動
- [ ] `CS_SECRET_KEY` 未設定でサーバー起動 → 登録時に 500 エラー + ガイダンス文言が出る

---

## 9. セキュリティ確認（横断）

- [ ] `.gitignore` に `*.secrets.json` と `.env` が含まれている
- [ ] `git status` で `.secrets.json` / `.env` が追跡対象になっていない
- [ ] リポジトリ内のどのコミットにも APIキー・シークレット・マスター鍵が含まれない
- [ ] 登録したBybit APIキーが読み取り専用権限のみであること（取引所側の権限設定で確認）

---

## 補足: 既知の未実装・今後

- ウォレットアドレス連携タブはUIのみ（スキャン処理は未実装、押すと「近日対応」表示）
- API連携は現状 Bybit のみ。bitFlyer 等は今後追加予定。
- 定期同期スケジュール（自動同期）は未実装（手動「同期」ボタンのみ）。
