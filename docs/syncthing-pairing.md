# Syncthing ペアリング仕様（両側共通）

PBR Lending のクロール結果を、クローラーを動かす機械から Crypto-Summary へ
ファイル同期で運ぶ。その同期相手の登録を、Syncthing の GUI を触らずに
それぞれのアプリの画面から済ませるための取り決め。

**両側が同じ取り決めに従わないと繋がらない。** この文書がその契約。

- 受信側の参照実装: [`src/crypto_summary/core/syncthing.py`](../src/crypto_summary/core/syncthing.py)
- 送信側（PBRLending-History-Check）は未実装。この文書に従って実装する。

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

## 動作確認のしかた

使い捨ての Syncthing を 2 台立てて確認できる。利用者の設定には触らない。

```bash
syncthing serve --home <dirA> --gui-address=127.0.0.1:18384 --gui-apikey=keyA --no-browser
syncthing serve --home <dirB> --gui-address=127.0.0.1:18385 --gui-apikey=keyB --no-browser
```

片側で相手の ID を登録 → もう片側の承認待ちに出る → 承認 → 接続 →
ファイルを置いて転送されること、除外対象が転送されないこと、を確認する。
