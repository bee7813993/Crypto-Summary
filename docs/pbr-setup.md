# PBR Lending 連携 セットアップ手順

Crypto-Summary と PBR Lending のクローラー（PBRLending-History-Check）を
つないで運用するまでの手順。構成は 2 つある。

- 関連: [`deploy.md`](./deploy.md)（配備先の選び方・バックアップ）、
  [`syncthing-pairing.md`](./syncthing-pairing.md)（両側の実装が守る取り決め）

## 構成の選び方

| | 構成 A：同一マシン | 構成 B：クラウド + 手元 |
|---|---|---|
| Crypto-Summary | 手元 | クラウド／別サーバー |
| クローラー | 手元 | 手元（変わらない） |
| データの渡し方 | ディレクトリを直接共有 | Syncthing でファイル同期 |
| Syncthing | **不要** | 両側に 1 つずつ |
| 向き | まず動かしてみる | 外出先から見たい・常時稼働させたい |

**クローラーは常に手元に置く。** クロールは PBR Lending への手動ログインを
挟む対話作業で、noVNC は口座にログイン済みブラウザの全操作権限を与える。
ブラウザプロファイルにはセッション Cookie が残る。クラウドには置かない。

---

# 構成 A：同一マシンで動かす

Crypto-Summary の compose からクローラーの compose を取り込み、1 つの
プロジェクトとしてまとめて起動する。`outputs` を直接マウントするので
ファイル同期は要らない。

```
┌─ docker compose（プロジェクト crypto-summary）─────────┐
│  app      :8000   ← /pbr-outputs (ro)  ┐               │
│  viewer   :4174, :6080 (noVNC)         ├ 同じ outputs  │
└────────────────────────────────────────┘               │
        ホストの J:/Git/PBRLending-History-Check/outputs ─┘
```

## 1. リポジトリを両方置く

```bash
git clone https://github.com/bee7813993/Crypto-Summary.git
git clone https://github.com/bee7813993/PBRLending-History-Check.git
```

## 2. Crypto-Summary の `.env` を作る

`.env.example` をコピーして編集する。同一マシン構成で要るのは次の 5 つ。

```bash
DATA_DIR=/data
PBR_REPO_DIR=J:/Git/PBRLending-History-Check
PBR_CRAWL_DIR=/pbr-outputs
PBR_VIEWER_URL=http://127.0.0.1:4174
PBR_VNC_PASSWORD=<好きなパスワード>
```

`PBR_REPO_DIR` はクローラーのリポジトリの場所。**Windows でも `/` 区切り**で書く。
`PBR_CRAWL_DIR` はコンテナ内から見たマウント先なので `/pbr-outputs` のままでよい。

`PBR_VNC_PASSWORD` はクローラーの noVNC のログインパスワード。
**未設定だと compose の解決自体が失敗する**（クローラー側が必須にしている）。

Google ログインを使う場合は `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` /
`BASE_URL` / `ADMIN_EMAILS` も設定する。手元だけで使うなら `DATA_DIR` を
空にするとログイン無しで動く（[構成 B の該当節](#ログインをどうするか)を参照）。

## 3. `docker-compose.override.yml` を作る

クローラーの取り込みと `outputs` のマウントは、このマシン固有の設定なので
**別ファイルに置く。** Compose が `docker-compose.yml` に自動で重ねてくれる。
`.gitignore` 済みなので、リポジトリを更新しても消えず、環境ごとのパスも混ざらない。

```yaml
include:
  - path: ${PBR_REPO_DIR}/docker-compose.yml
    project_directory: ${PBR_REPO_DIR}

services:
  app:
    volumes:
      - "${PBR_REPO_DIR}/outputs:/pbr-outputs:ro"
```

読み取り専用（`:ro`）にしているのは、アプリが取り込み元を書き換えないため。

`volumes:` は本体の設定を置き換えず**追記**されるので、`./data:/data`（台帳の
保存先）はここに書かなくても残る。

> `docker-compose.yml` 本体の先頭にも同じ `include:` がコメントで入っている。
> そちらのコメントを外しても動くが、更新のたびに書き戻す手間が出る。

## 4. 起動

既にクローラーを単独のプロジェクトとして起動しているなら、先に落とす
（同じ 4174 / 6080 を使うため衝突する）。

```bash
docker compose -p pbrlending-history-check down
```

```bash
docker compose up -d --build
```

`-f` を付けない。付けると `docker-compose.override.yml` が読まれない。

| URL | 中身 |
|---|---|
| http://localhost:8000 | Crypto-Summary |
| http://localhost:4174 | クローラーのビューア |
| http://localhost:6080 | noVNC（クロール中のブラウザ操作） |

## 5. 連携を有効にする

Crypto-Summary のインポート画面 →「集計の設定」→
**「PBR Lending クローラー連携を使う」をオン**。

既に PBR のクロールデータがある利用者は自動で有効になる。全員が PBR の口座を
持つとは限らないので、既定では出さない設計にしている。

## 6. 動作を確かめる

1. サイドバーに「PBR クローラー」タブが出る（クローラー画面が埋め込まれる）
2. インポート画面に「PBR Lending クローラー同期」カードが出る
3. 「取り込み元ファイル」を開くと 4 ファイルが「届いている」と出る
4. クロール後に画面を開き直すと自動で取り込まれる

---

# 構成 B：クラウドの Crypto-Summary ＋ 手元のクローラー

両側に Syncthing を置き、クローラーの `outputs` をクラウドへ送る。

```
手元（Windows など）                    クラウド／別サーバー
┌──────────────────────────┐          ┌──────────────────────────┐
│ viewer   :4174           │          │ app      :8000           │
│ syncthing  ─ outputs     │──22000──▶│ syncthing ─ pbr-outputs  │
│   (sendonly)             │          │   (receiveonly)          │
└──────────────────────────┘          └──────────────────────────┘
```

送るのは 4 ファイルだけ。`captures/`（生のスクレイピング結果）は送らない。

| ファイル | 役割 |
|---|---|
| `pbrlending_crawled.latest.json` | クロール結果（当年分） |
| `last_crawl.json` | クロールの成否 |
| `viewer_transfers.json` | クローラー画面へ手動インポートした入出金 |
| `viewer_ledger.json` | 同上（日次レポート） |

## ログインをどうするか

`DATA_DIR` の有無でモードが決まる。

| 設定 | モード | 向き |
|---|---|---|
| `DATA_DIR=/data` | マルチユーザー（Google ログイン） | 本番・公開 |
| `DATA_DIR=`（空） | シングルユーザー（ログイン無し） | LAN での試用 |

**LAN の http では Google ログインを使えない。** Google は localhost 以外の
リダイレクト URI に http を認めないため、`http://192.168.1.50:8000` を登録できない。
LAN ではシングルユーザーにする。その場合 **LAN 上の誰でも台帳を開ける**ので、
信頼できるネットワークに限ること。

## B-1. クラウド側（受信）

### 1. リポジトリと `.env`

```bash
git clone https://github.com/bee7813993/Crypto-Summary.git
cd Crypto-Summary
```

```bash
BASE_URL=http://192.168.1.50:8000     # 公開 URL。https なら Cookie に Secure が付く
DATA_DIR=                             # 空 = ログイン無し（LAN での試用）
SYNCTHING_API_KEY=<openssl rand -hex 24 の出力>
```

`SYNCTHING_API_KEY` はサイドカー専用に生成する。**手元の個人 Syncthing の
キーを使わないこと** — このキーはその Syncthing の全フォルダ・全デバイスを
操作できる。

### 2. 起動

```bash
docker compose -f docker-compose.cloud.yml up -d --build
```

`app`（8000）と `syncthing` が立ち上がる。ボリュームは 3 つに分かれている。

| ボリューム | 中身 |
|---|---|
| `cs-data` | 台帳・設定・セッション鍵。**バックアップ対象** |
| `pbr-outputs` | 同期で届くファイル。app からは読み取り専用 |
| `syncthing-config` | Syncthing 自身の設定と鍵 |

**同期サイドカーに台帳を触らせない**ためにボリュームを分けてある。

### 3. 開けるポート

| ポート | 用途 |
|---|---|
| 8000/tcp | アプリの画面 |
| 22000/tcp・22000/udp | Syncthing の同期 |
| 21027/udp | ローカル探索（同一 LAN なら相手を自動発見） |

**8384（Syncthing の GUI）は公開しない。** 設定はアプリの画面から行う。

### 4. 連携を有効にする

インポート画面 →「集計の設定」→「PBR Lending クローラー連携を使う」をオン。

## B-2. 手元側（送信）

### 1. クローラーと Syncthing を起動

Crypto-Summary の `docker-compose.yml` には送信用の `syncthing` サービスを
用意してある。[構成 A の手順 3](#3-docker-composeoverrideyml-を作る) と同じく
`docker-compose.override.yml` でクローラーを取り込み、まとめて起動する。

`syncthing` サービスもクローラーの `outputs` をマウントする。参照するのは同じ
`PBR_REPO_DIR` なので、`.env` に書いてあれば追加の設定は要らない。

`.env`（手元側）:

```bash
PBR_VNC_PASSWORD=<好きなパスワード>
SYNCTHING_API_KEY=<openssl rand -hex 24 の出力>   # クラウド側とは別のもの
```

```bash
docker compose --profile syncthing up -d viewer syncthing
```

**`app` を名指ししない。** 構成 B ではアプリはクラウドにあるので、手元で
起動する必要はない。名指しを省くと `app` も一緒に立ち上がる。

`--profile syncthing` を付けないと Syncthing は起動しない。同期が不要な場面で
余計なコンテナを立てないため。同期ポートは 32000 にしてある（ホストで Syncthing を
常用していると既定の 22000 が埋まっているため）。

### 2. クローラー画面で Syncthing に繋ぐ

http://localhost:4174 →「設定」→「ファイル同期の相手を登録（Syncthing）」

| 項目 | 値 |
|---|---|
| 接続先 | `http://syncthing:8384` |
| API キー | 手元 `.env` の `SYNCTHING_API_KEY` |
| 同期フォルダのパス | `/pbr-outputs` |

**「同期フォルダのパス」は Syncthing コンテナから見たパス**であって、
ビューアから見たパス（`/app/outputs`）ではない。ここを間違えると、
繋がっているのに 1 件も転送されない状態になる（後述）。

## B-3. ペアリング

Syncthing は**双方が相手を登録しないと繋がらない**。「どんな相手でも自動承認」
という設定は存在しない。したがって次の形になる。

1. **どちらか一方**の画面で、相手のデバイス ID を貼って「登録」
2. **もう一方**の画面に「〜が接続を求めています」が出る → 「承認」を 1 回押す
3. 接続が成立し、同期が始まる

どちら側から始めてもよい。パス・種別（送信のみ／受信のみ）・除外設定は、
それぞれのアプリが自動で正しく設定する。

承認待ちは、欄を開いている間 5 秒ごとに自動で確認する。相手の操作を待つ間、
画面を開いたままにしておけばよい。

## B-4. 動作を確かめる

**クラウド側**の同期カードで:

```
登録済みの相手
• PBRLending-History-Check — 接続中
同期フォルダ: pbr-lending-outputs / /data/pbr-outputs（receiveonly）
状態: 待機中 / 相手が持つファイル 4 件・未受信 0 件
```

「取り込み元ファイル」を開くと 4 ファイルが「届いている」と出る。
画面を開き直すと自動で取り込まれ、残高照合が表示される。

---

# 日々の運用

1. 手元でクロールする（クローラー画面 →「取得開始」→ 手動ログイン → 続行）
2. 構成 A ならそのまま、構成 B なら Syncthing が自動で運ぶ
3. Crypto-Summary の画面を開くと**自動で取り込まれる**

**「クロール結果を同期」ボタンは普段不要。** すぐ反映したいときだけ押す。

取り込みは対象期間の**洗い替え**で行う。クローラーは同じ期間を何度でも取り直し、
過去日の利率訂正も反映されるため、追記ではなく期間ごと置き換える。何度実行しても
結果は同じで、重複しない。

## 年が明けたら

公式の年間履歴 CSV が公開されたら、推定値であるクロール由来データを公式データに
置き換える。

1. 同期カードの「年次パージ」でその年を指定して削除
   （または `crypto-summary sync-pbr --purge-year 2026`）
2. 公式 CSV を取り込む。クローラー画面に読ませても、Crypto-Summary に `pbr` として
   直接取り込んでも、どちらでもよい（両方に読ませても重複しない）
3. 翌年の同期は前年に触れない

クロール由来は `pbr_crawl`、公式 CSV 由来は `pbr` とソースが分かれているので、
パージが公式データに影響することはない。

## インポート履歴からバッチを消すとき

インポート画面のバッチ一覧から削除すると、**そのバッチで入った行がすべて消える。**
公式 CSV で入れた過年度分をここで消すと、クロールでは取り直せない期間
（旧システムなど）が台帳から失われる。

削除ダイアログが対象件数と期間を出すので、消してよいバッチか確かめてから
実行すること。クロール由来（`pbr_crawl`）のバッチは、消しても次の同期で
入り直すので影響はない。

## 更新するとき

```bash
git pull
docker compose up -d --build
```

**`--build` が要る。** ソースはイメージに焼き込まれるので、`restart` や
`up -d` だけでは新しいコードにならない。

台帳はボリューム（構成 A なら `./data`）に残るので、更新で消えることはない。
テーブルは起動時に無ければ作られる形なので、更新のための手作業は要らない。
とはいえ**更新前にバックアップを取っておくこと**（下記）。

---

# つまずきやすい点

この連携を組む中で実際に踏んだもの。

### コードを変えたのに反映されない

`docker compose restart` はイメージを作り直さない。Dockerfile がソースを
イメージに焼き込んでいるので、**コード変更には `--build` が要る**。

```bash
docker compose -f docker-compose.yml up -d --build
```

`.env` や `environment` の変更だけなら `up -d`（コンテナ再作成）で足りる。

### 画面だけ古いまま

ブラウザが JS をキャッシュしていることがある。**Ctrl + Shift + R** で読み込み直す。

### 繋がっているのにファイルが 1 件も来ない

**同期フォルダのパスが Syncthing から見て存在しない**のが最も多い。Syncthing は
そのパスを黙って作ってしまい、エラーも出さずに 0 件のまま止まる。

両側の画面が警告を出す。

```
状態: 待機中 / 相手が持つファイル 0 件・未受信 0 件
⚠ 接続できているのにファイルが 1 件も見えていません。…
```

コンテナ構成で起きやすい。アプリと Syncthing でマウント位置が違うのに
**自分から見たパス**を渡すと、Syncthing 側には無いパスになる。

### ホストの Syncthing にコンテナから繋がらない（403）

Syncthing は DNS リバインディング対策で Host ヘッダを検査する。GUI が
`127.0.0.1:8384` 待受だと、`host.docker.internal` 経由の要求は API キーを
付けても 403 になる。**Syncthing もコンテナで動かす**のが素直
（公式イメージは `0.0.0.0:8384` 待受なので、コンテナ間なら検査に掛からない）。

### 同じディレクトリを送受信させない

構成 A に Syncthing を足して「送信元と受信先が同じディレクトリ」にすると、
receive-only 側が「送信元に無いファイル」を余計なものと判断して削除しうる。
**構成 A では Syncthing を使わない。** 直接マウントで足りている。

### 初回セットアップ画面が出たまま同期が走らない

`CS_SECRET_KEY` が未設定で、サーバー設定ファイル（`_server_config.json`）も
無いときは、初回セットアップウィザードが先に出る。この間は画面の初期化が
そこで止まるため、**PBR の自動取込も走らない**。

ウィザードを完了するか、スキップすれば通常画面に進む。`.env` に
`CS_SECRET_KEY` を設定してあれば最初から出ない。

### ポートが衝突する

| ポート | 使うもの |
|---|---|
| 4174 / 6080 | クローラーのビューア / noVNC |
| 22000 / 21027 | クラウド側 Syncthing |
| 32000 | 手元側 Syncthing |
| 8384 | Syncthing の GUI（公開しない） |

ホストで Syncthing を常用している場合、既定の 22000 と衝突する。手元側を
32000 にずらしてあるのはそのため。

---

# バックアップと安全

## 取るもの

構成 B なら `cs-data` ボリュームだけ。台帳（`*.db`）、口座グループ・表示設定、
暗号化された API キー、セッション鍵、同期記録が入っている。
構成 A なら `./data` ディレクトリ。

**`CS_SECRET_KEY` はボリュームとは別に控える。** 失うと登録済みの取引所 API
キーを復号できなくなる。

`pbr-outputs`（同期で届くファイル）はクローラー側にあるので、取らなくてよい。

## 気をつけること

**シングルユーザーモードには認証が無い。** LAN 上の誰でも台帳を開ける。
公開するならマルチユーザー（Google ログイン）＋ https にする。

**Syncthing の API キーは、その Syncthing の全操作権限を持つ。** 手元用と
クラウド用を分け、手元の個人 Syncthing のキーをクラウドへ渡さない。

**noVNC は口座にログイン済みブラウザを操作できる。** 手元に閉じたまま使い、
外部に公開しない。パスワードは推測されにくいものにする。

**`data/` はリポジトリに入れない。** `_server_config.json` に Google の
クライアントシークレットと暗号化マスター鍵が平文で入る（`.gitignore` 済み）。

---

# 困ったときの確認手順

上から順に見ると切り分けられる。

**1. コンテナは動いているか**

```bash
docker compose ps
```

**2. アプリは応答するか**（認証不要）

```bash
curl http://localhost:8000/api/health
```

**3. 取り込み元ファイルは届いているか**

インポート画面 → 同期カード →「取り込み元ファイル」。
4 ファイルの最終更新・サイズ・未着が出る。

**4. Syncthing は繋がっているか**（構成 B）

同期カード →「ファイル同期の相手を登録」。相手の接続状態とフォルダの状態
（待機中／同期中、相手が持つファイル数）が出る。0 件なら上の「繋がっているのに
ファイルが来ない」を見る。

**5. 取り込みは走ったか**

同期カードの「最終同期」。未取り込みなら「未取り込みのクロール結果があります」と
出る。画面を開き直すか、ボタンを押す。

**6. 残高が合わない**

同期後の照合表を見る。**サイト実残高との差は正常**で、契約内で発生済みだが
まだ付与されていない利息の分。「未収利息の範囲内」ならその範囲。「要確認」は
未収利息を超える差なので、データの欠落・重複を疑う。

CLI でも同じことを確認できる。

```bash
crypto-summary sync-pbr --dry-run
```
