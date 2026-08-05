# クラウドへの配備

アプリ (Crypto-Summary) をクラウドのコンテナサービスで動かし、PBR Lending の
クローラー (PBRLending-History-Check) は手元に残して、取得データをファイル同期で
運ぶ構成の手順。

## なぜクローラーを持っていかないか

技術的に動かないのではなく、設計上そもそも合わない。

- **クロールに人間の手が要る。** PBR Lending へのログインは手動で、
  「ログインして続行を押す」まで待つ実装になっている。無人実行できない。
- **noVNC は口座にログイン済みブラウザの全操作権限**を与える。手元では
  127.0.0.1 に閉じているが、クラウドでは公開せざるを得ず、守りはパスワード 1 枚になる。
- **ブラウザプロファイルに PBR Lending のセッション Cookie が残る。**
  これをクラウドに置くのは、貸出口座のセッションを他人のサーバーに預けるのと同じ。

クローラーは手元で動かし、`outputs` ディレクトリだけを同期すれば十分。
アプリ側はディレクトリを読むだけなので、同期の手段は問わない。

## 配備先の選び方

台帳は SQLite (WAL) なので、**永続ブロックボリュームが付くサービス**を選ぶ。

| 種別 | 例 | 可否 |
|---|---|---|
| ブロックボリュームが付く | Fly.io / Render / Railway / 小さな VM | そのまま動く |
| ファイルシステムが揮発 | Cloud Run / App Runner | 不可（外部 DB への移行が必要） |
| ネットワークストレージ | EFS / Azure Files | 非推奨（SQLite のロックが不安定） |

**インスタンスは 1 台に固定する。** SQLite は複数プロセスからの同時書き込みを
前提にしていない。水平スケールさせない設定にすること。

## 環境変数

```bash
BASE_URL=https://cs.example.com        # 必須。OAuth と Cookie の Secure 判定に使う
DATA_DIR=/data                         # 必須。ボリュームのマウント先
GOOGLE_CLIENT_ID=...                   # ログインに必要
GOOGLE_CLIENT_SECRET=...
ADMIN_EMAILS=you@example.com
CS_SECRET_KEY=...                      # API キーの暗号化マスター鍵
PBR_CRAWL_DIR=/data/pbr-outputs        # ファイル同期の宛先
COINGECKO_API_KEY=                     # 任意
SECRET_KEY=                            # 任意（未設定なら自動生成して永続化）
```

Google Cloud Console 側で、リダイレクト URI に `${BASE_URL}/auth/callback` を
登録しておくこと。

秘密情報 (`GOOGLE_CLIENT_SECRET` / `CS_SECRET_KEY`) は配備先のシークレット管理へ。
`.env` をイメージやリポジトリに含めないこと。

## 起動

```bash
docker compose -f docker-compose.cloud.yml up -d --build
```

ヘルスチェックは `GET /api/health`（認証不要、`{"status":"ok"}` を返す）。

## PBR データのファイル同期

手元のクローラーの `outputs` を、アプリのボリューム内 `/data/pbr-outputs` へ
同期する。Syncthing でも rsync でも rclone でもよい。

| 同期元（手元） | 同期先（クラウド） |
|---|---|
| `<PBRLending-History-Check>/outputs/` | `<データボリューム>/pbr-outputs/` |

同期するファイル:

| ファイル | 役割 |
|---|---|
| `pbrlending_crawled.latest.json` | クロール結果（当年分） |
| `last_crawl.json` | クロールの成否（これが無いと既定では取り込まない） |
| `viewer_ledger.json` / `viewer_transfers.json` | クローラー画面へ手動インポートした公式データ |

`captures/` は同期しなくてよい（生のスクレイピング結果で、容量が大きい）。

**片方向で十分。** アプリはこのディレクトリを読むだけで、書き込まない。
Syncthing なら同期元を「送信のみ」、同期先を「受信のみ」にしておくと事故が減る。

**ファイルが順に届くことへの対応は入っている。** 取り込み元ファイルの更新から
30 秒間は自動取り込みを見送る（`SETTLE_SECONDS`）。片方だけ新しい状態や
書き込み途中を読まないため。画面の同期ボタンと CLI はこの待ちを無視する。

同期が届くと、次にアプリの画面を開いたときに自動で取り込まれる。取り込みの
判定は「取り込み元ファイルの更新時刻とサイズ」の指紋で行うので、クロールして
いなくても、手動インポートしたファイルが届いただけで反映される。

**連携の表示は利用者ごとの設定。** `PBR_CRAWL_DIR` を設定してもそれだけでは
UI に出ず、各利用者がインポート画面の「集計の設定」でオンにしたときに出る。
全員が PBR Lending の口座を持つとは限らないため（マルチユーザーで運用する場合に
効いてくる）。すでに PBR のデータがある利用者は設定しなくても有効になる。

## バックアップ

`/data` ボリュームだけ取ればよい。中身は台帳 (`*.db`)、口座グループ・表示設定・
暗号化された API キー (`*.json`)、セッション鍵 (`_session_key`)、
同期記録 (`*.pbr_sync.json`)、および同期されてきた `pbr-outputs/`。

`CS_SECRET_KEY` を失うと登録済み API キーを復号できなくなる。ボリュームとは
別に控えておくこと。

## 手元に残すもの

- クローラー本体（Docker でもホストでも）
- クロールの実行と、公式 CSV の手動インポート
- `outputs` を同期する仕組み

アプリのクローラータブ (`PBR_VIEWER_URL`) は利用者のブラウザから解決されるので、
手元のビューアを指したままでよい。ただしアプリを https で公開している場合、
`http://` のビューアは mixed content でブロックされ iframe には表示されない
（「新しいタブで開く」は使える）。
