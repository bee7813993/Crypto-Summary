# AI エージェント連携（MCP）

Crypto-Summary は [MCP（Model Context Protocol）](https://modelcontextprotocol.io/) の
サーバーを内蔵しています。Claude・Codex・Gemini・Cursor・VS Code など、MCP に対応した
AI エージェントから、台帳の残高・評価額・推移・取引履歴を**読み取り専用**で参照できます。

- 受け口: Web アプリと同じポートの `/mcp`（Streamable HTTP）
- 認証: Web 画面で発行する **MCP 接続トークン**を `Authorization: Bearer <トークン>` で送る
- できること: 読み取りだけ。売買・送金・データの変更をするツールはありません

---

## 1. トークンを発行する

1. Web 画面のサイドバーから「管理者設定」を開き（管理者以外の利用者には「設定」と表示されます）、
   「AI エージェント連携（MCP）」へ進む
2. 名前（どのエージェント用か。例: `Codex CLI（ノート PC）`）を入れて「トークンを発行」
3. 表示されたトークンをコピーして、エージェントの設定に書く

- トークンは**発行した直後にしか表示されません**。サーバーはハッシュしか持たないので、
  あとから見ることはできません。なくしたら失効させて発行し直してください。
- **エージェントごとに 1 本**発行してください。使わなくなったエージェントのぶんだけを
  「失効」で止められます。一覧の「最終利用」で、使われているかを確かめられます。
- マルチユーザー構成（Google ログイン）では、トークンは発行した人の台帳だけを読めます。
  Asset Summary 連携用の `CS_SERVICE_TOKEN` では `/mcp` に入れません。
- シングルユーザー構成（ログインなし）でも `/mcp` はトークン必須です。

---

## 2. 接続先 URL

```
http://<CS を開いているアドレス>/mcp
```

カードの「接続先 URL」に、いま画面を開いている URL から組み立てたものが出ます。
エージェントを動かす PC から見たアドレスを使ってください。同じ PC なら
`http://127.0.0.1:8000/mcp` です。サブパスで配信している場合は
`https://example.com/crypto/mcp` のように接頭辞が付きます。

`/mcp/` のように末尾にスラッシュを付けないでください（リダイレクトしません）。

---

## 3. エージェント別の設定

どのエージェントも「Streamable HTTP の URL」と「`Authorization` ヘッダー」の 2 つを
渡すだけです。トークンは設定ファイルに直接書かず、環境変数から読ませると、設定を
共有・コミットしたときに漏れません（以下の例の `CS_MCP_TOKEN`）。

サーバー名は `cryptosummary` を勧めます。Gemini CLI は名前に `_` があると権限の設定が
効かなくなり、Gemini API は `-` を使えないためです。

### Claude Code

```bash
claude mcp add --transport http cryptosummary http://127.0.0.1:8000/mcp --header "Authorization: Bearer <トークン>" --scope user
```

`--scope project` にすると `.mcp.json` に書かれてリポジトリに入るので、トークンを
書くときは `user`（全プロジェクト共通・自分だけ）にしてください。

### Codex CLI（OpenAI）

`~/.codex/config.toml`:

```toml
[mcp_servers.cryptosummary]
url = "http://127.0.0.1:8000/mcp"
bearer_token_env_var = "CS_MCP_TOKEN"
```

コマンドで足すなら:

```bash
codex mcp add cryptosummary --url http://127.0.0.1:8000/mcp --bearer-token-env-var CS_MCP_TOKEN
```

### Gemini CLI

`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "cryptosummary": {
      "httpUrl": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer $CS_MCP_TOKEN" }
    }
  }
}
```

キーは **`httpUrl`** です。`url` と書くと旧方式（SSE）で接続しようとして失敗します。

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "cryptosummary": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer ${env:CS_MCP_TOKEN}" }
    }
  }
}
```

### VS Code（GitHub Copilot のエージェントモード）

`.vscode/mcp.json`（トップレベルのキーは `servers`）:

```json
{
  "inputs": [
    { "type": "promptString", "id": "cs-token", "description": "Crypto-Summary MCP token", "password": true }
  ],
  "servers": {
    "cryptosummary": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer ${input:cs-token}" }
    }
  }
}
```

### その他

| エージェント | 書き方の要点 |
|---|---|
| Cline | `"type": "streamableHttp"` を必ず書く（省くと SSE になる）。`url` と `headers` |
| LM Studio | Cursor と同じ `mcp.json` 形式（`url` + `headers`） |
| Open WebUI | 接続の種類に Streamable HTTP、認証に Bearer を選んでトークンを入れる |
| n8n（MCP Client Tool） | 認証に Bearer を選ぶ。トランスポートは Streamable HTTP |
| 自作（OpenAI Agents SDK / Google ADK など） | Streamable HTTP のクライアントに `Authorization` ヘッダーを渡す |

### 確認済みの範囲

| 接続元 | 確認 |
|---|---|
| MCP Python SDK のクライアント（旧手順 2025-11-25 / 新手順 2026-07-28） | 全ツールを実データで呼び出して確認 |
| Codex CLI 0.160 | 上の設定（`bearer_token_env_var`）でツールを呼び、回答まで確認 |
| 上記以外 | 各エージェントの公式ドキュメントに沿って書いたもの（未確認） |

### クラウドから接続するもの（claude.ai・ChatGPT など）

claude.ai のカスタムコネクタ、ChatGPT、OpenAI Responses API のリモート MCP、Gemini API の
MCP は、**各社のクラウドから**接続してきます。`127.0.0.1` は使えず、インターネットから
届く公開 URL が必要です。

- claude.ai（Web・スマホ・デスクトップアプリのコネクタ）: 「Request headers」に
  `Authorization: Bearer <トークン>` を入れると、このトークン方式のまま使えます。
- ChatGPT（Web）: 接続できるのは OAuth か認証なしのサーバーだけで、固定トークンを送れません。
  加えて https が必須です。**現時点では対応していません。**

公開 URL が平文の http だと、トークンも資産データも経路上で読める状態になります。
公開するなら TLS を終端するリバースプロキシを前に置くことを検討してください
（[`deploy.md`](./deploy.md)）。

---

## 4. ツール一覧

すべて読み取り専用です（`readOnlyHint: true`）。結果は JSON の文字列で返ります。
数値は文字列、`null` は「判らない」、`"0"` は「ゼロだと判っている」で、意味は
[`api.md`](./api.md) の読み取り API と同じです。

| ツール | 内容 | 主な引数 |
|---|---|---|
| `get_portfolio_summary` | 全口座合算の評価額と資産ごとの内訳、前日比 | `currency` |
| `list_accounts` | 口座（取引所・ウォレット）ごとの評価額 | `currency` |
| `get_account_assets` | 1 口座の中の資産 | `account`, `currency` |
| `get_asset_accounts` | 1 資産をどの口座にどれだけ持っているか | `asset`, `currency` |
| `get_portfolio_history` | 評価額（または 1 資産の数量・価格）の日次推移 | `range`, `scope`, `metric`, `currency` |
| `get_balances_at` | 指定日の終わり時点の数量（価格なし） | `as_of`, `scope` |
| `list_transactions` | 取引履歴（50 件ずつ、または 1 か月ぶん） | `account`, `asset`, `since`, `until`, `month`, `page`, `order`, `timezone`, `include_running_balances` |
| `list_import_batches` | 取り込んだ CSV の一覧 | なし |

- `currency` は `USD` / `JPY` / `EUR` / `GBP`。既定は `USD`。
- `account` は `list_accounts` の表示名です。ソース ID（`bitflyer` など）や大文字・小文字の
  違いも同じ口座として受け付けます。見つからないときは候補の一覧を添えたエラーを返します。
- `scope` は `total` / `account:<表示名>` / `asset:<シンボル>`。
- `list_transactions` の `month`（`YYYY-MM`）は、その月を全件返します（500 件で打ち切り、
  打ち切ったときは `truncated: true`）。月の区切りは `timezone`（例 `Asia/Tokyo`）、
  省略時はサーバーの `TZ` です。行ごとの残高は `include_running_balances: true` のときだけ付けます。

---

## 5. 動作確認

トークンが通るかは、ツール一覧を取るだけで確かめられます。

```bash
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer $CS_MCP_TOKEN" -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

トークンが無い・違う・失効済みなら `401` が返ります。

特定のベンダーに依存しない確認には MCP Inspector（Node.js 22.19 以上）が使えます。

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp --transport http --header "Authorization: Bearer $CS_MCP_TOKEN" --method tools/list
```

---

## 6. 仕組みのメモ

- MCP Python SDK（`mcp` 2.x）の `MCPServer` を使い、ステートレス・JSON 応答で動かしています。
  要求ごとに完結するので、サーバーを再起動してもエージェント側の接続は切れません。
  旧手順（`initialize` から始める 2025-11-25 まで）と新手順（2026-07-28 の `server/discover`）の
  両方に応答します。
- `/mcp` はマウントではなくルートとして置いています（マウントだと `/mcp` → `/mcp/` の
  リダイレクトが入り、POST のリダイレクトを追わないクライアントが失敗するため）。
- トークンが必須なので、SDK の DNS リバインディング対策（Host ヘッダーの許可リスト）は
  使っていません。公開 URL のホスト名を設定に並べなくても動きます。
- トークンの保存先は、マルチユーザーでは `DATA_DIR/_mcp_tokens.json`、シングルユーザーでは
  台帳の隣の `<台帳名>.mcp_tokens.json` です。中身は SHA-256 のハッシュと名前・日時だけです。
- `mcp` パッケージが入っていない環境では `/mcp` を作らず、画面のカードも出ません
  （`pip install -e ".[web]"` で入れ直すと有効になります）。
