# Syncthing ペアリング仕様（両側共通）

PBR Lending のクロール結果を、クローラーを動かす機械から Crypto-Summary へ
ファイル同期で運ぶ。その同期相手の登録を、Syncthing の GUI を触らずに
それぞれのアプリの画面から済ませるための取り決め。

**両側が同じ取り決めに従わないと繋がらない。** この文書がその契約。

- 受信側の実装: [`src/crypto_summary/core/syncthing.py`](../src/crypto_summary/core/syncthing.py)
- 送信側の実装: PBRLending-History-Check の `tools/syncthing.mjs`

両実装を実際に繋いで、契約どおり噛み合うことを確認済み（送信側で登録 →
受信側で承認 → 接続 → 4 ファイルだけ転送 → 台帳へ 612 件取り込み）。

## 役割

| | 送信側 | 受信側 |
|---|---|---|
| アプリ | PBRLending-History-Check | Crypto-Summary |
| 同期するディレクトリ | `<repo>/outputs` | `PBR_CRAWL_DIR`（例 `/data/pbr-outputs`） |
| フォルダ種別 | `sendonly` | `receiveonly` |
| 除外設定 | **入れる**（下記） | 入れない |

受信側は読むだけで書かない。だから片方向で足りる。

## 共有する値（両側で一致させる）

| 項目 | 値 |
|---|---|
| フォルダ ID | `pbr-lending-outputs` |
| フォルダラベル | `PBR Lending outputs` |

Syncthing はフォルダ**名ではなく ID** で結び付く。ID が違うと、繋がっても
同期は始まらない。

## 除外設定（送信側だけが入れる）

取り込みに使う 4 ファイルだけを送る。`captures/` は生のスクレイピング結果で
容量が大きく、取り込みには使わない。

```
!pbrlending_crawled.latest.json
!last_crawl.json
!viewer_transfers.json
!viewer_ledger.json
*
```

`POST /rest/db/ignores?folder=pbr-lending-outputs` に
`{"ignore": [...]}` で書く。

## ペアリングの手順

Syncthing は**双方が相手を登録しないと繋がらない**。「どんな相手でも自動承認」
という設定は存在しない（あればセキュリティホールなので当然）。したがって
到達できるのは次の形。

1. どちらか一方の画面で、相手のデバイス ID を貼り付けて「登録」
   → デバイス登録 + 自分側のフォルダ作成 + 相手への共有
2. もう一方の画面に「承認待ち」として現れる → 「承認」を 1 回押す
   → 同じくデバイス登録 + 自分側のフォルダ作成
3. 接続が成立し、同期が始まる

どちらの側から始めてもよい。承認する側も、自分のパス・種別・除外設定は
自分が知っているので、承認時に自動で正しく設定できる。

## 使う API

接続先は環境変数で持つ（`SYNCTHING_URL` / `SYNCTHING_API_KEY`）。
API キーは Syncthing 全体を操作できるので、**その Syncthing を持つ側の
アプリにだけ**渡す。

| 用途 | 呼び出し |
|---|---|
| 自分のデバイス ID | `GET /rest/system/status` → `myID` |
| 版 | `GET /rest/system/version` |
| 承認待ちの一覧 | `GET /rest/cluster/pending/devices` → `{deviceID: {name, time}}` |
| 承認待ちの却下 | `DELETE /rest/cluster/pending/devices?device=<id>` |
| デバイス一覧 | `GET /rest/config/devices` |
| デバイス追加 | `POST /rest/config/devices` |
| デバイス更新 | `PUT /rest/config/devices/<id>` |
| フォルダ一覧 | `GET /rest/config/folders` |
| フォルダ追加 | `POST /rest/config/folders` |
| フォルダ更新 | `PUT /rest/config/folders/<id>` |
| 除外設定 | `POST /rest/db/ignores?folder=<id>` |
| 接続状況 | `GET /rest/system/connections` → `connections[<id>].connected` |
| 新規オブジェクトの雛形 | `GET /rest/config/defaults/device` `.../folder` |

認証は `X-API-Key` ヘッダ。

## 実装で踏んだ落とし穴

Syncthing v2.0.16 / v2.1.2 で実機確認した内容。

**デバイス ID は 7 文字 × 8 組（ハイフン込み 63 文字、除くと 56 文字）。**
8 文字 × 7 組ではない。貼り付け時の空白・改行・ハイフンの有無を吸収して
正規化してから使う。

**フォルダ作成時、`devices` に自分自身を含める必要がある。**
含めないとフォルダは作られるが同期が始まらない。

```json
{ "id": "pbr-lending-outputs", "path": "...", "type": "receiveonly",
  "devices": [{"deviceID": "<自分>"}, {"deviceID": "<相手>"}] }
```

**新規オブジェクトは `GET /rest/config/defaults/{device,folder}` の雛形を
土台にする。** 必須項目を自前で並べると版差で壊れる。

**既存フォルダのパス・種別は上書きしない。** 利用者が意図して変えている
可能性がある。共有先の追加だけ行う。

**未設定・未接続を例外にしない。** 画面が壊れないよう、状態として返す
（`configured` / `reachable` / エラーコード）。

## 画面に出すもの

両側で同じ構成にすると迷わない。

- 自分のデバイス ID（コピーボタン付き。相手に貼ってもらう）
- 相手のデバイス ID の入力欄と「登録」
- 承認待ちがあれば「〜が接続を求めています」＋「承認」「却下」
- 登録済みの相手と接続状況（接続中／未接続）
- 同期フォルダのパスと種別。あるべき設定と食い違っていれば警告

## Syncthing をコンテナで動かす場合

同期先がコンテナの中にあるなら、Syncthing もコンテナで動かすのが素直。
`docker-compose.cloud.yml` の `syncthing` サービスがその形。実測で分かった点:

**GUI が `0.0.0.0` 待受ならホストヘッダ検査に引っかからない。**
公式イメージは既定で `STGUIADDRESS=0.0.0.0:8384` なので、別コンテナから
`http://syncthing:8384` を叩いて 200 が返る。逆にホストで `127.0.0.1:8384`
に束ねた Syncthing へ `host.docker.internal` 経由で繋ぐと、API キーを付けても
403 になる（DNS リバインディング対策のホストヘッダ検査）。ホスト側の
Syncthing を使いたいなら `insecureSkipHostcheck` を有効にする必要がある。

**マウント点は `/var/syncthing`。** `/var/syncthing/config` に当てると
証明書を書けずに起動失敗する。

**名前付きボリュームは root 所有で作られる。** イメージ既定の `PUID=1000` の
ままだと書き込めないので `PUID=0` / `PGID=0` を渡す。

**フォルダのパスは Syncthing コンテナから見た形で渡す。** アプリと
Syncthing でマウント位置が違うと、存在しないパスのフォルダを作ってしまう。
同じパスに見せるのが一番簡単。違う場合は `SYNCTHING_FOLDER_PATH` で上書きする。

**同期サイドカーに台帳を触らせない。** ボリュームを分け、アプリ側は
`:ro` で読むだけにする。同期で届くファイル以外は共有しない。

## 繋がっているのに同期が始まらないとき

デバイスが「接続中」でも、フォルダが空振りしていることがある。**最も多いのは
フォルダのパスの指定違い**で、Syncthing から見て存在しないディレクトリを指すと、
Syncthing はそこを作ってしまい、エラーも出さずに 0 件のまま止まる。

`GET /rest/db/status?folder=<id>` の `globalFiles` が 0 なら、相手側のフォルダが
空か、パスが噛み合っていない。画面には状態と件数を出し、「接続しているのに
0 件」のときは警告する（受信側の実装では `stalled`）。

コンテナで動かす構成では特に起きやすい。アプリのコンテナと Syncthing の
コンテナでマウント位置が違うのに、アプリが**自分から見たパス**を渡すと、
Syncthing 側には無いパスになる。渡すのは必ず **Syncthing から見たパス**。

## 動作確認のしかた

使い捨ての Syncthing を 2 台立てて確認できる。利用者の設定には触らない。

```bash
syncthing serve --home <dirA> --gui-address=127.0.0.1:18384 --gui-apikey=keyA --no-browser
syncthing serve --home <dirB> --gui-address=127.0.0.1:18385 --gui-apikey=keyB --no-browser
```

片側で相手の ID を登録 → もう片側の承認待ちに出る → 承認 → 接続 →
ファイルを置いて転送されること、除外対象が転送されないこと、を確認する。
