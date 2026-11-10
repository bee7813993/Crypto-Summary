"""AI エージェント向けの MCP サーバー（読み取り専用）。

Web アプリの /mcp に Streamable HTTP で載せる。Claude に限らず、MCP を話す
エージェント（Codex CLI・Gemini CLI・Cursor・VS Code など）から同じように
使えることを優先して、次の約束を守る。

  - ツール名は小文字英数字と `_` だけで 40 文字以内（Codex は `-` や `.` を
    置き換え、Gemini は接頭辞込みで 63 文字に切り詰めるため）。
  - 引数は単純な型と列挙だけにし、`$ref` や null を許す型を使わない（省略可能な
    引数は空文字などの既定値で表す）。
  - 結果は常に JSON の文字列として本文（content の text）に入れる。
    structuredContent を読まないクライアントがあるため。
  - すべてのツールに readOnlyHint を付ける（付いていないと、書き込みとみなして
    毎回確認を求めるクライアントがある）。

認証は Web 画面で発行する MCP 接続トークン（mcp_tokens.py）。HTTP の入口で
`Authorization: Bearer` を確かめて 401 を返し、ツールの中でももう一度同じ
ヘッダーから利用者を引いて、その人の台帳だけを読む。トークンが必須なので、
SDK の DNS リバインディング対策（Host ヘッダーの許可リスト）は使わない —
公開 URL の Host を列挙しなくても動くようにするため。
"""
from __future__ import annotations

import json
import os
from typing import Annotated, Any, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .mcp_tokens import McpTokenStore

#: 月を指定したときに返す取引の上限。それを超えたら打ち切りを明示する。
_MONTH_ROW_LIMIT = 500

SERVER_NAME = "cryptosummary"

# 先頭 512 文字だけで意味が通るように書く（そこまでしか読まないクライアントがある）。
INSTRUCTIONS = """\
Read-only access to the user's self-hosted Crypto-Summary ledger: crypto balances, \
valuations, daily history and transactions across exchanges and wallets. Numbers are \
decimal strings - parse them, do not round. null means unknown; "0" means a known zero. \
A non-empty "warnings" list means the figures are partial. Accounts are named by the \
display names from list_accounts; assets by ticker symbol. Start with get_portfolio_summary.

Details:
- currency is one of USD, JPY, EUR, GBP. Use the one the user works in.
- Day-over-day change per asset is value - prev_value (prev_* use the balance and closing \
price of prev_date, so deposits and trades show up in the change). total_value - \
total_prev_value is overstated by the assets listed in prev_missing; say so when it is \
not empty.
- Prices come from CoinGecko. Assets without a price have value null and are excluded \
from totals.
- Nothing here can trade, move funds or change data.
"""

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

Currency = Annotated[
    Literal["USD", "JPY", "EUR", "GBP"],
    Field(description="Valuation currency."),
]
ScopeArg = Annotated[
    str,
    Field(description='"total", "account:<display name>" or "asset:<symbol>".'),
]


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        return ""
    return value[len("Bearer "):].strip()


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _web():
    # app.py がこのモジュールを読み込むので、循環を避けて呼ぶ時に参照する。
    from . import app as web_app

    return web_app


class _BearerGate:
    """MCP の入口。有効なトークンが無い要求は MCP の処理まで通さずに 401 を返す。"""

    def __init__(self, app: ASGIApp, store: McpTokenStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = _bearer(Headers(scope=scope).get("authorization"))
            if self.store.verify(token) is None:
                response = JSONResponse(
                    {
                        "error": "invalid_token",
                        "error_description": (
                            "Crypto-Summary の MCP 接続トークンが必要です。"
                            "画面の「管理者設定」（管理者以外は「設定」）にある「AI エージェント連携（MCP）」で発行し、"
                            "Authorization: Bearer <トークン> で送ってください。"
                        ),
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="crypto-summary"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_mcp(
    *,
    store: McpTokenStore,
    resolve_db: Callable[[str], str],
) -> tuple[MCPServer, ASGIApp]:
    """MCP サーバーと、/mcp に置く ASGI アプリを作る。

    resolve_db(sub) は利用者の台帳のパスを返す。台帳が無ければ LookupError を
    投げること（読み取りのために DB を暗黙作成しない）。
    返したサーバーの session_manager.run() は、Web アプリの lifespan で回すこと。
    """
    mcp = MCPServer(
        SERVER_NAME,
        title="Crypto-Summary",
        instructions=INSTRUCTIONS,
        log_level="WARNING",
    )

    def db_for(ctx: Context) -> str:
        sub = store.verify(_bearer((ctx.headers or {}).get("authorization")))
        if sub is None:
            raise ToolError("MCP 接続トークンが無効です")
        try:
            return resolve_db(sub)
        except LookupError as e:
            raise ToolError(str(e) or "この利用者の台帳がありません") from e

    def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except HTTPException as e:
            raise ToolError(str(e.detail)) from e

    def require_account(db: str, account: str) -> str:
        """口座の指定を表示名にそろえる。見つからなければ候補を添えてエラーにする。

        エージェントは表示名（bitFlyer）ではなくソース ID（bitflyer）や大文字・小文字の
        違う名前を渡しがちなので、表示名 → 大文字・小文字を無視した表示名 →
        ソース ID の順に探す。
        """
        web = _web()
        groups = web._load_groups(db)
        ledger = web.Ledger(db)
        try:
            ids = [src for src, *_ in ledger.sources()]
        finally:
            ledger.close()
        by_id = {s: web._display_name(s, groups) for s in ids}
        names = sorted(set(by_id.values()))
        wanted = account.strip()
        folded = wanted.casefold()
        for candidates in (
            [n for n in names if n == wanted],
            [n for n in names if n.casefold() == folded],
            sorted({by_id[s] for s in ids if s.casefold() == folded}),
        ):
            if len(candidates) == 1:
                return candidates[0]
        raise ToolError(
            f"口座「{wanted}」はありません。list_accounts の名前を使ってください"
            f"（候補: {', '.join(names) or 'なし'}）"
        )

    def require_date(name: str, value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if _web()._parse_date(value) is None:
            raise ToolError(f"{name} は YYYY-MM-DD で指定してください（{value!r}）")
        return value

    # -- ツール ------------------------------------------------------------

    @mcp.tool(
        title="Portfolio summary",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def get_portfolio_summary(ctx: Context, currency: Currency = "USD") -> str:
        """Total value of all accounts and the per-asset breakdown, with day-over-day values.

        Returns total_value, total_prev_value (previous close), prev_missing, and assets[]
        sorted by value (asset, balance, price, value, prev_balance, prev_price, prev_value,
        prev_date). Rows with balance "0" were emptied today and are kept only for the
        day-over-day comparison.
        """
        db = db_for(ctx)
        return _dump(call(_web()._summary, db, currency))

    @mcp.tool(
        title="Accounts",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def list_accounts(ctx: Context, currency: Currency = "USD") -> str:
        """Accounts (exchanges and wallets, grouped as shown in the app) with their total value.

        Each entry has source (the display name used by the other tools), source_ids,
        tx_count, asset_count, total_value and the first/last transaction time.
        """
        db = db_for(ctx)
        return _dump(call(_web()._sources, db, currency))

    @mcp.tool(
        title="Assets in an account",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def get_account_assets(
        ctx: Context,
        account: Annotated[str, Field(description="Account display name from list_accounts.")],
        currency: Currency = "USD",
    ) -> str:
        """Assets held in one account: balance, price and value per asset, plus linked wallets."""
        db = db_for(ctx)
        name = require_account(db, account)
        return _dump(call(_web()._account_assets, name, db, currency))

    @mcp.tool(
        title="Accounts holding an asset",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def get_asset_accounts(
        ctx: Context,
        asset: Annotated[str, Field(description="Ticker symbol, e.g. BTC.")],
        currency: Currency = "USD",
    ) -> str:
        """Where one asset is held: balance and value per account, and the totals."""
        db = db_for(ctx)
        symbol = asset.strip().upper()
        if not symbol:
            raise ToolError("asset を指定してください")
        return _dump(call(_web()._asset_accounts, symbol, db, currency))

    @mcp.tool(
        title="Portfolio history",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def get_portfolio_history(
        ctx: Context,
        range: Annotated[
            Literal["7d", "30d", "90d", "1y", "all"],
            Field(description="Time window."),
        ] = "30d",
        scope: ScopeArg = "total",
        metric: Annotated[
            Literal["value", "balance", "price"],
            Field(description='"value" (valuation). "balance" (quantity) and "price" need scope "asset:<symbol>".'),
        ] = "value",
        currency: Currency = "USD",
    ) -> str:
        """Daily time series of value, or of one asset's balance or price.

        points[] holds {"t": "YYYY-MM-DD", ...}. Days with no price at all are missing, so
        dates are not guaranteed to be contiguous. is_partial true means some assets had
        no price.
        """
        db = db_for(ctx)
        if scope.startswith("account:"):
            scope = "account:" + require_account(db, scope[len("account:"):])
        elif scope.startswith("asset:"):
            scope = "asset:" + scope[len("asset:"):].strip().upper()
        return _dump(call(_web()._portfolio_history, db, currency, range, scope, metric))

    @mcp.tool(
        title="Balances on a date",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def get_balances_at(
        ctx: Context,
        as_of: Annotated[str, Field(description="YYYY-MM-DD (end of that day, UTC). Empty for today.")] = "",
        scope: ScopeArg = "total",
    ) -> str:
        """Quantity held per asset at the end of a day. No prices, so it is fast.

        Call it for two dates to see how holdings changed, separate from price moves.
        """
        db = db_for(ctx)
        if scope.startswith("account:"):
            scope = "account:" + require_account(db, scope[len("account:"):])
        elif scope.startswith("asset:"):
            scope = "asset:" + scope[len("asset:"):].strip().upper()
        return _dump(call(_web()._balances_at, db, as_of.strip() or None, scope))

    @mcp.tool(
        title="Transactions",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def list_transactions(
        ctx: Context,
        account: Annotated[str, Field(description="Account display name. Empty for all accounts.")] = "",
        asset: Annotated[str, Field(description="Ticker symbol. Empty for all assets.")] = "",
        since: Annotated[str, Field(description="YYYY-MM-DD, inclusive. Empty for no lower bound.")] = "",
        until: Annotated[str, Field(description="YYYY-MM-DD. Empty for no upper bound.")] = "",
        month: Annotated[str, Field(description="YYYY-MM: return that whole month instead of a page.")] = "",
        page: Annotated[int, Field(ge=1, description="Page number (50 per page). Ignored with month.")] = 1,
        order: Annotated[Literal["desc", "asc"], Field(description="desc = newest first.")] = "desc",
        timezone: Annotated[
            str,
            Field(description="IANA time zone that defines month boundaries, e.g. Asia/Tokyo. Empty for the server's zone."),
        ] = "",
        include_running_balances: Annotated[
            bool, Field(description="Add the balance after each transaction (larger output).")
        ] = False,
    ) -> str:
        """Transaction history (deposits, withdrawals, trades, fees, rewards), paged.

        Each row has timestamp (UTC), account, type, received_asset/amount,
        sent_asset/amount, fee_asset/amount, label and tx_hash. total and total_pages
        describe the whole filtered set.
        """
        web = _web()
        db = db_for(ctx)
        acct = require_account(db, account) if account.strip() else None
        sym = asset.strip().upper() or None
        since_v = require_date("since", since)
        until_v = require_date("until", until)
        tz_name = timezone.strip()
        if tz_name:
            # 明示された名前は解決できなければ伝える（黙って UTC で月を切ると、
            # 月末の夜の取引が別の月に入ったまま気づけない）。
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, OSError) as e:
                raise ToolError(f"timezone「{tz_name}」を解決できません") from e
        else:
            tz = web._client_tz(os.environ.get("TZ") or None)
        result = call(
            web._transactions, db, acct, sym, since_v, until_v, page,
            order=order, month=month.strip() or None, tz=tz,
        )
        rows = result.get("transactions", [])
        if not include_running_balances:
            for row in rows:
                row.pop("running_balances", None)
        if result.get("month") and len(rows) > _MONTH_ROW_LIMIT:
            result["transactions"] = rows[:_MONTH_ROW_LIMIT]
            result["truncated"] = True
            result["note"] = (
                f"Only the first {_MONTH_ROW_LIMIT} of {len(rows)} transactions are included. "
                "Narrow the query with account, asset or since/until."
            )
        return _dump(result)

    @mcp.tool(
        title="Import history",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def list_import_batches(ctx: Context) -> str:
        """CSV files imported into the ledger: when, which account and exchange, and how many transactions."""
        db = db_for(ctx)
        return _dump(call(_web()._list_import_batches, db))

    starlette_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # 要求ごとに完結させる（セッションを持たない）。再起動しても接続が切れず、
        # 1 往復で済むので SSE を扱わないクライアントにも優しい。
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # Starlette の Mount を使うと /mcp が /mcp/ へのリダイレクトになり、POST の
    # リダイレクトを追わないクライアントが失敗する。SDK が作ったルートの中身
    # （ASGI アプリ）だけを取り出して、Web アプリの /mcp に直接置く。
    endpoint = starlette_app.routes[0].endpoint
    return mcp, _BearerGate(endpoint, store)
