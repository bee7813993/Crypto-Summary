# 取り込みフォルダ（inbox）設計 — PBR クローラー連携の一般化

> 状態: 第 1 段（汎用層）・第 2 段（PBR クローラーの載せ替え）実装済み。
> §12 の論点はすべて推奨案で決定した。
> PBR Lending クローラー専用になっている「フォルダに置かれたファイルを取り込む」
> 仕組みを、任意の取引所の取引履歴ファイルに使える形へ作り替える。
>
> 実装の対応: `core/inbox.py`（走査・意味論・ポーラ・入口名）、`core/importer.py`
> （アップロードと共通の取り込み本体）、`web/app.py` の `/api/inbox/*`、
> CLI `crypto-summary inbox`、インポート画面の「取り込みフォルダ」カード。

## 1. 目的

**特定のフォルダに取引履歴ファイルが置かれると、対応する取引所として台帳に反映される**
ようにする。置く手段は問わない（Syncthing / rsync / Dropbox / SMB / 手コピー /
自作スクリプトの書き出し先）。Web のアップロード画面でできることは、ファイルを
置くだけで同じ結果になる。

PBR Lending クローラーは、この仕組みの上に載る「取引所の一つ」になる。

## 2. 現状の立てつけと、一般化を阻んでいる点

| 現状（PBR 専用） | 一般化に向けた問題 |
|---|---|
| `PBR_CRAWL_DIR` という **サーバー共通** の 1 ディレクトリ | 取引所ごと・利用者ごとに置き場所を分けられない。マルチユーザーでは全員が同じクロール結果を自分の台帳に取り込んでしまう |
| 固定 4 ファイル名（`pbrlending_crawled.latest.json` ほか） | 取引所の CSV は名前が自由（日付入りなど）で、複数ファイルが溜まる |
| `last_crawl.json` のヘルスチェック、署名（mtime+size）による最新判定 | PBR の作法。取引所 CSV には存在しない |
| 「期間の洗い替え」（`replace_windows`） | クローラーが同じ期間を取り直す前提の意味論。CSV は「1 ファイル = 1 バッチ」の追記・置換が自然 |
| 自動取り込みは **ブラウザで画面を開いたとき** に走る | 置いただけでは反映されない。クラウド運用で誰も開かなければ止まったまま |
| Syncthing のフォルダ ID が `pbr-lending-outputs` 固定、除外設定に 4 ファイル名を列挙 | 送る側がファイル名を知っている必要がある |
| 利用者ごとの `pbr_sync_enabled` 設定で UI を出し分け | フォルダが設定されていれば出せばよく、個別設定は不要になる |

一方で **そのまま活かせる資産** も多い。

- `EXCHANGE_SOURCES`（取引所キー → アダプタ）: 取り込み形式の登録簿はこれで足りる
- `CsvSourceAdapter.load()` / `reconcile()` / `skip_reasons`: ファイル → 取引の変換と、既存台帳との突き合わせ
- `import_batches` / `batch_txs`: 「どのファイルからどの取引が入ったか」とバッチ単位の削除
- `CanonicalTx.id` が内容から決まる: 同じファイルを何度入れても二重にならない（upsert）
- `_is_settling()`: ファイル同期で複数ファイルが順に届く間は待つ
- `core/syncthing.py`: ペアリングの実装（フォルダ ID と除外設定を差し替えれば使える）

## 3. 全体像

```
  手元の機械                         サーバー（Crypto-Summary）
 ┌─────────────────┐   Syncthing    ┌──────────────────────────────────────┐
 │ 取引所からDL    │   rsync など   │ IMPORT_DIR（例 /data/inbox）          │
 │ クローラー出力  │ ─────────────▶ │   bybit/        2025-01.csv …        │
 │ 自作スクリプト  │                │   gmo/          取引履歴.csv         │
 └─────────────────┘                │   pbr_crawl/    pbrlending_crawled…  │
                                    │   nexo_auto/    main/  *.csv         │
                                    └──────────────┬───────────────────────┘
                                                   │ 30 秒ごとに走査（stat のみ）
                                                   ▼
                                    ┌──────────────────────────────────────┐
                                    │ inbox スキャナ                         │
                                    │  新規 → 取り込み（1 ファイル = 1 バッチ）│
                                    │  更新 → 旧バッチ削除 → 取り込み直し     │
                                    │  同一 → 何もしない                      │
                                    │  消失 → データは残す（画面で知らせる）   │
                                    └──────────────┬───────────────────────┘
                                                   ▼
                                       台帳（ledger.db）＋ inbox_files 表
```

## 4. フォルダの規約

### 4.1 配置

```
IMPORT_DIR/
  <exchange>/                 ← EXCHANGE_SOURCES のキー（bybit, gmo, pbr, pbr_crawl, nexo_auto, universal …）
    *.csv / *.json            ← source_id = <exchange>
    <account>/                ← 同じ取引所の別口座。source_id = <account>
      *.csv
```

- `<exchange>` は Web のアップロード画面で選ぶ取引所と同じキー。未知の名前の
  フォルダは取り込まず、画面に「対応するキー一覧」と一緒に表示する
- 1 段下の `<account>/` は任意。Web のアップロードで「ソース ID」に入れる名前と
  同じ意味（省略時は取引所名）。既存の運用で `bybit_sub` のように付けている
  利用者はその名前のフォルダを切ればよい
- ルート直下のファイルは取り込まない（どの取引所か決められないため）。
  将来、ヘッダーから形式を推定する `_auto/` を足す余地は残す（§10）

### 4.2 マルチユーザー

`DATA_DIR` 運用（Google ログイン）では台帳が利用者ごとに分かれるので、
フォルダも利用者ごとに分ける。

```
IMPORT_DIR/
  <inbox-name>/               ← 利用者ごとの入口
    <exchange>/ …
```

- `<inbox-name>` は利用者が設定画面で決める短い名前（英数字と `-_`）。
  既定値はメールのローカル部から生成する。`google_sub` は数字の羅列で
  フォルダ名に向かない
- 名前 → 利用者（db ファイル）の対応は `_system.db` に保存する（管理者が一覧できる）
- シングルユーザー運用（`DB_PATH`）では `IMPORT_DIR/<exchange>/` を直接使う

### 4.3 無視するもの

- 先頭が `.` のファイル・ディレクトリ（`.stfolder` `.stversions` `.syncthing.*.tmp` `.DS_Store`）
- `*.tmp` `*.part` `*.crdownload`
- シンボリックリンク（ルートの外を指せるため）
- 既定で 50 MB を超えるファイル（取引所 CSV の常識的な上限。超過は「大きすぎる」として表示）
- 更新から 30 秒以内のファイル（書き込み途中・同期途中。既存の `SETTLE_SECONDS`）

## 5. 取り込みの意味論

### 5.1 ファイル単位（既定）

**1 ファイル = 1 インポートバッチ。** ファイルの同一性は
「相対パス」+「内容の SHA-256」で見る。

| 状況 | 動作 |
|---|---|
| 新しいパス | 取り込み。`import_batches` に `filename = 相対パス` で記録 |
| 同じパス・同じハッシュ | 何もしない（毎回の走査はコストゼロに近い） |
| 同じパス・違うハッシュ | **そのファイルの旧バッチを削除してから取り込み直す**（ファイル粒度の洗い替え）。取引所が同じ期間を再出力して訂正した場合に、置き換えるだけで反映される |
| パスが消えた | **台帳のデータは残す**（既定）。画面に「ファイルは無くなったが取引は残っている」と出し、バッチ削除ボタンを添える |
| 別名にリネーム | 旧パスは「消えた」扱い、新パスは新規取り込み。取引 ID は内容から決まるので二重にはならず、1 つの取引が 2 バッチに紐付くだけ（既存の `batch_txs` の仕組み） |

「消えたら消す」（ミラー）モードは意図せぬ削除の事故が大きいので既定にしない。
必要なら取引所フォルダ単位の設定として後から足す。

取り込み処理そのものは Web のアップロード（`_import_csv`）と **同じ関数** を通す。
`load()` → `reconcile()` → `upsert_many()` → `record_import_batch()`。
エラーメッセージ（列が足りない、形式を識別できない等）もそのまま流用する。

### 5.2 フォルダ単位（PBR クローラーなど）

PBR クローラーは「クロール結果 + マーカー + ビューア手動インポート 2 ファイル」を
**まとめて 1 回で** 評価する（ビューア側のどの期間を読むかがクロール結果に依存する）。
こういう取り込み元のために、アダプタが **フォルダ単位** を宣言できるようにする。

```python
class PbrCrawlFolderSource(FolderSourceAdapter):
    granularity = "folder"
    def inspect(self, directory) -> FolderStatus   # 健全か・待つべきか・警告
    def load_folder(self, directory) -> list[CanonicalTx]
    def reconcile(self, txs, ledger) -> list[CanonicalTx]   # 期間の洗い替え・legacy pbr の掃除
```

- 同一性は「対象ファイル群のハッシュを合成した署名」。変われば旧バッチを消して入れ直す
- `last_crawl.json` のヘルスチェック、`force`、残高照合（reconciliation）、
  年次パージは **このアダプタの中** に閉じる。汎用層は `inspect()` の結果
  （`blocked` / `warnings` / 追加表示）を受け取って画面に出すだけ
- 現在の `sync_pbr_crawl()` の中身はほぼそのまま `load_folder()` + `reconcile()` に移る

### 5.3 順序と同時実行

- 1 回の走査で複数ファイルを取り込むときは「取引所 → 更新時刻 → 名前」の順。
  `summ`（取引レポートで他の期間を置き換える）は最後に回す
- 同じ台帳に対する取り込みはプロセス内ロックで直列化する。Web アップロードと
  走査が重なっても順に処理される
- CLI とサーバーが同時に走った場合は SQLite のロック待ち（busy_timeout）に任せる

## 6. 起動のさせ方

| 経路 | 役割 |
|---|---|
| **サーバー内のポーラ**（既定 30 秒間隔） | 「置けば反映される」の本体。`stat` だけで変化を見るので軽い。`INBOX_POLL_SECONDS=0` で無効化 |
| 画面の「今すぐ確認」 | 待たずに走査する。結果はその場で表示 |
| CLI `crypto-summary inbox sync` | cron や手動用。`--watch` で常駐、`--dry-run` で予定だけ表示 |
| 画面を開いたとき | ポーラ無効の構成（`INBOX_POLL_SECONDS=0`）のための保険として 1 回だけ走査を蹴る（現在の PBR と同じ挙動） |

inotify / watchdog は使わない。Docker のボリュームや同期ツールの書き込みでは
イベントが届かないことがあり、ポーリングの方が確実で、コストも十分小さい。

uvicorn を複数ワーカーで動かす構成に備え、ポーラは `IMPORT_DIR/.inbox.lock`
のファイルロックで 1 プロセスだけが走るようにする。

## 7. 状態の持ち方

利用者の台帳 DB に表を 1 つ足す。

```sql
CREATE TABLE IF NOT EXISTS inbox_files (
    path         TEXT PRIMARY KEY,   -- IMPORT_DIR（利用者の入口）からの相対パス
    exchange     TEXT NOT NULL,
    source       TEXT NOT NULL,      -- source_id
    sha256       TEXT,
    size         INTEGER,
    mtime        TEXT,
    status       TEXT NOT NULL,      -- imported | error | unsupported | too_large | missing | pending
    message      TEXT,               -- エラー理由（利用者向けの文）
    batch_id     TEXT,               -- import_batches.id（取り込めたとき）
    first_seen   TEXT NOT NULL,
    imported_at  TEXT,
    result_json  TEXT                -- parsed / imported / skipped / skip_reasons / warnings
);
```

- `import_batches` に `origin TEXT`（`upload` / `inbox`）を足す。既存 DB には
  起動時に `ALTER TABLE` で補う（値が無ければ `upload`）
- PBR 専用の `sync_state.json`（最終同期・署名・最終パージ）は、フォルダ単位
  バッチの `inbox_files` 行と `result_json` に吸収する。`last_purge` だけは
  PBR アダプタの状態として残す
- `status = error` のファイルは **内容が変わるまで再試行しない**（毎回同じ失敗を
  繰り返してログを埋めない）。画面の「再試行」で明示的にやり直せる

## 8. 画面と API

### 8.1 画面（インポート画面）

「PBR Lending クローラー同期」カードを **「取り込みフォルダ」カード** に置き換える。

- 冒頭に「あなたの取り込みフォルダ: `/data/inbox/<inbox-name>/`」と、
  取引所キーの一覧（フォルダ名として使える名前）
- ファイル一覧: パス / 取引所 / ソース ID / 状態 / 取引数 / 最終取り込み。
  状態ごとの操作: エラー → 再試行、消失 → このファイルのデータを削除、
  取り込み済み → バッチ（取り込み履歴）へのリンク
- 「今すぐ確認」ボタン
- **取引所固有のパネル**: フォルダ単位アダプタが `inspect()` で返す追加情報を
  そのフォルダの行の下に出す。PBR なら「最終クロール / 健全性 / 残高照合 /
  年次パージ / force」が入る。つまり PBR 固有 UI は無くならず、置き場所が変わる
- Syncthing のペアリング欄はカード内にそのまま残す（§9）
- 既存の「PBR Lending クローラー連携を使う」設定は廃止する。フォルダが
  設定されていればカードを出し、PBR パネルは `pbr_crawl/` に何かあるときだけ出す

### 8.2 API

| | |
|---|---|
| `GET  /api/inbox/status` | 入口のパス、走査の最終時刻、ファイル一覧（`inbox_files`）、取引所別の `inspect()` 結果 |
| `POST /api/inbox/sync` | 今すぐ走査。`{"dry_run": true}` で予定だけ。`{"exchange": "pbr_crawl", "force": true}` のように対象と取引所固有オプションを渡せる |
| `POST /api/inbox/files/retry` | `{"path": …}` エラーのファイルをやり直す |
| `DELETE /api/inbox/files` | `{"path": …}` そのファイル由来のバッチを削除し、行も消す |
| `PUT  /api/inbox/name` | マルチユーザーの `<inbox-name>` を設定（重複チェック） |
| 管理者: `GET /api/admin/inbox` | 全利用者の入口名と最終走査 |

`/api/sync/pbr*` は **互換エイリアス** として一世代残す（`/api/inbox/sync?exchange=pbr_crawl` へ転送）。

### 8.3 CLI

```
crypto-summary inbox status
crypto-summary inbox sync [--dir DIR] [--exchange KEY] [--dry-run] [--force] [--watch]
crypto-summary inbox forget PATH        # 行とバッチを削除
crypto-summary sync-pbr …               # = inbox sync --exchange pbr_crawl（非推奨表示）
```

## 9. ファイル同期（Syncthing）との契約の変更

| 項目 | 現在 | 変更後 |
|---|---|---|
| 受信側フォルダ | `PBR_CRAWL_DIR` | `IMPORT_DIR`（マルチユーザーでは `IMPORT_DIR/<inbox-name>`） |
| フォルダ ID | `pbr-lending-outputs` | `crypto-summary-inbox`（マルチユーザーは `cs-inbox-<inbox-name>`） |
| 種別 | 受信のみ | 変わらず |
| 除外設定 | 送信側に 4 ファイル名を列挙 | 送信側が自分で決める（`captures/` 除外など）。受信側は無指定 |

PBRLending-History-Check 側は、`outputs` の下に `pbr_crawl/` を切ってそこへ
書き出すか、送信フォルダのパスを `outputs` のままにして **受信側で**
`IMPORT_DIR/<inbox-name>/pbr_crawl` に受ける。後者なら送信側の変更は
フォルダ ID の差し替えだけで済む。

## 10. 移行と互換

1. **`PBR_CRAWL_DIR` は当面そのまま動く。** 設定されていれば
   「`pbr_crawl` 取引所の追加フォルダ」として走査対象に含める
   （シングルユーザーはその利用者、マルチユーザーは `pbr_sync_enabled` が
   真だった利用者に紐付ける）。新規セットアップの文書からは消す
2. 既存の `pbr_crawl` バッチと `sync_state.json` は初回走査で `inbox_files`
   行に取り込む（署名が一致すれば再取り込みしない）
3. テスト: `test_pbr_crawl_sync.py`（52 件）と `test_web_pbr_sync.py`（39 件）
   の大半はフォルダ単位アダプタの振る舞いとして残る。汎用層のテストを新設する
   （新規 / 同一 / 更新 / 消失 / 整定中 / 無視パターン / 未知フォルダ /
   エラー後の再試行抑止 / マルチユーザーの入口解決 / 取り込みの冪等性）
4. `docs/pbr-setup.md` `docs/deploy.md` `docs/syncthing-pairing.md` を
   「取り込みフォルダ」前提に書き換え、PBR はその一例として節を残す

## 11. 実装の段階

**第 1 段（汎用層。PBR は触らない）**

- `core/inbox.py`: 走査（規約 §4、意味論 §5.1、状態 §7）、ポーラ、ロック
- `IMPORT_DIR` / `INBOX_POLL_SECONDS`、compose と `.env.example`
- API（§8.2）と CLI（§8.3）、画面のカード（§8.1。PBR パネルは既存カードを並存）
- Syncthing のフォルダ ID と除外設定の一般化（§9）
- マルチユーザーの入口名（§4.2）

**第 2 段（PBR をこの上に載せる）— 実装済み**

- フォルダ単位アダプタ（`FOLDER_ADAPTER`）の導入。`sync_pbr_crawl()` 自体は残し、
  アダプタがそれを呼ぶ（期間の洗い替え・残高照合・年次パージは従来のまま）
- PBR パネルを取引所固有パネルとして汎用カードに吸収。`pbr_sync_enabled` は
  「PBR_CRAWL_DIR を自分の取り込みフォルダに含める」設定として存続
- `/api/sync/pbr*` と `sync-pbr` を `sync_unit()` 経由の互換経路に

**その後（任意）**

- `_auto/` フォルダ: ヘッダーから形式を推定（`nexo_auto` / `pbr` / Bybit の
  `_detect_kind` に相当する判定を登録簿にまとめる）
- `.zip` の展開（取引所のエクスポートが zip で来る場合）
- ミラーモード（ファイル消失でバッチも消す）の取引所別設定

## 12. 決定事項

| 論点 | 決定 | 退けた案 |
|---|---|---|
| 別口座の表し方 | `<exchange>/<account>/` の入れ子 | `<exchange>.<account>/` のような 1 階層 |
| ファイル消失時 | データは残す。消すのは別操作（「データを削除」/ `inbox forget`） | ミラー（消す） |
| 起動 | サーバー内ポーラ 30 秒（`INBOX_POLL_SECONDS`、0 で無効） | 画面を開いたときだけ |
| マルチユーザーの入口名 | 利用者が決める短い名前（既定はメールのローカル部） | `google_sub` をそのまま |
| 更新ファイルの扱い | 旧バッチを消して入れ直す | 追記のみ |

実装で足した細則:

- 取り込み履歴からバッチを消された（利用者の削除）ファイルは、内容が同じ限り
  入れ直さない（`status = deleted`）。入れ直すのは「再取り込み」/ `--retry` のとき
- ポーラのロックファイルは受信専用の同期フォルダを汚さないよう `DATA_DIR`
  （シングルユーザーは台帳と同じディレクトリ）の `.inbox.lock` に置く

第 2 段（PBR クローラーの載せ替え）で決めた細則:

- フォルダ単位アダプタは `inspect()` / `sync()` / `last_state()` を持つオブジェクト
  （`sources/jp/pbr_crawl.py` の `FOLDER_ADAPTER`）。汎用層は署名（run_id と各ファイルの
  mtime/size、取り込み規則の版）が変わったときだけ `sync()` を呼び、`inbox_files` に
  `pbr_crawl/` の 1 行で記録する
- `pbr_sync.json`（最終同期・残高照合・最終パージ）は **アダプタの状態として残す**。
  §7 で「吸収する」としたが、残高照合など PBR 固有の情報は汎用の行に押し込むより
  アダプタに閉じた方が見通しがよく、既存の画面・テストもそのまま使えるため
- 従来の `PBR_CRAWL_DIR` は「入口の外にあるフォルダ単位」（`legacy_folders`）として
  同じ経路で扱う。行のキーは `pbr_crawl/@PBR_CRAWL_DIR`。サーバー共通の設定なので、
  マルチユーザーでは連携を使う利用者（`pbr_sync_enabled`、または使った実績）にだけ付ける。
  この判定のため `pbr_sync_enabled` の設定は廃止せず、意味を「PBR_CRAWL_DIR を自分の
  取り込みフォルダに含める」に改めた（§8.1 の「廃止する」からの変更）
- ブロック状態（クロール異常終了）は `status = blocked` として見せ、自動では取り込まない。
  画面の「強制取り込み」/ API の `force` / CLI の `--force` で上書きできる。整定中は `pending`
- `/api/sync/pbr` と `sync-pbr` は `sync_unit()` を通す互換経路。応答は従来どおりで、
  `inbox_files` の行も更新する（画面の一覧と食い違わない）
- 画面の「PBR Lending クローラー同期」カードは、取り込みフォルダカード内の PBR パネルに
  なった。入口（IMPORT_DIR）が無く従来の PBR 連携だけの構成でも、カードは PBR パネル
  だけを出す。起動時のクライアント側の PBR 自動同期は廃止し、サーバー内ポーラ
  （無い構成では起動時の 1 回走査）に置き換えた

## 13. 安全性

- フォルダの中身は外部から届く **信頼できない入力** として扱う。パーサは既存どおり
  例外を捕まえて利用者向けの文にする。サイズ上限とシンボリックリンク不追従で
  メモリ枯渇とルート外読み出しを防ぐ
- 取り込みは「置かれたら入る」ので、**削除は自動で行わない**（§5.1）。消すのは
  常に利用者の操作（バッチ削除・`inbox forget`）
- 受信側 Syncthing のフォルダは受信のみ。アプリは `IMPORT_DIR` に書かない
  （ロックファイル `.inbox.lock` を除く。ロックは `DATA_DIR` 側に置く選択肢もある）
