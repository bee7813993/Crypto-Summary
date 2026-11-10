"""AI エージェント向け MCP（/mcp）と接続トークンのテスト。

- トークンの発行・一覧・失効（平文は発行時だけ・ファイルにはハッシュだけ）
- /mcp はトークン必須（無効・失効・サービストークンは 401）
- ツールが他のクライアントでも壊れない形になっている（名前・スキーマ・注釈）
- ツールの中身が Web の読み取り API と同じ値を返す
- マルチユーザーでは自分の台帳だけを読み、無い台帳を作らない
- MCP の新旧どちらの手順（initialize あり / 2026-07-28）でも呼べる
"""
import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.core.models import CanonicalTx, TxType  # noqa: E402
from crypto_summary.web import app as web_app  # noqa: E402
from crypto_summary.web.auth import require_user  # noqa: E402

SUB_A = "102071876055427770412"
SUB_B = "109999999999999999999"

ACCEPT = "application/json, text/event-stream"


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _tx(source: str, key: str, ts: datetime, **kw) -> CanonicalTx:
    return CanonicalTx(id=CanonicalTx.make_id(source, key), source=source, timestamp=ts, **kw)


def _seed(db_path: Path) -> None:
    db = Ledger(db_path)
    db.upsert(_tx("bitflyer", "btc1", datetime(2024, 1, 5, tzinfo=timezone.utc),
                  type=TxType.DEPOSIT, received_asset="BTC", received_amount=Decimal("0.5")))
    db.upsert(_tx("gmo", "eth1", datetime(2024, 2, 10, tzinfo=timezone.utc),
                  type=TxType.DEPOSIT, received_asset="ETH", received_amount=Decimal("2")))
    db.upsert(_tx("gmo", "eth2", datetime(2024, 2, 20, tzinfo=timezone.utc),
                  type=TxType.WITHDRAW, sent_asset="ETH", sent_amount=Decimal("0.5"),
                  fee_asset="ETH", fee_amount=Decimal("0.01")))
    db.close()


@pytest.fixture(autouse=True)
def _fixed_prices(monkeypatch):
    prices = {"BTC": Decimal("60000"), "ETH": Decimal("3000")}

    def fake_prices(assets, currency, warn=None):
        return {a.upper(): prices[a.upper()] for a in assets if a.upper() in prices}

    def fake_history(assets, currency, start, end, warn=None):
        return {a: {_days_ago(1): prices[a] - 100} for a in assets if a in prices}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    monkeypatch.setattr(web_app, "fetch_price_history", fake_history)


@pytest.fixture
def su_app(tmp_path: Path):
    db = tmp_path / "ledger.db"
    _seed(db)
    return web_app.create_app(db_path=str(db))


def _issue(client: TestClient, label: str = "Codex CLI") -> str:
    r = client.post("/api/mcp/tokens", json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _rpc(client: TestClient, token: str | None, method: str, params: dict | None = None):
    headers = {"Accept": ACCEPT}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers,
    )


def _call(client: TestClient, token: str, name: str, args: dict | None = None) -> dict:
    r = _rpc(client, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text"
    return result


def _call_json(client: TestClient, token: str, name: str, args: dict | None = None) -> dict:
    result = _call(client, token, name, args)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


# ---- 接続トークンの発行・失効 ----


def test_issue_list_revoke(su_app, tmp_path):
    with TestClient(su_app) as c:
        r = c.post("/api/mcp/tokens", json={"label": "Gemini CLI"})
        assert r.status_code == 200
        body = r.json()
        token, record = body["token"], body["record"]
        assert token.startswith("csmcp_") and len(token) > 40
        assert record["label"] == "Gemini CLI" and record["hint"] == token[-4:]

        listed = c.get("/api/mcp/tokens").json()
        assert listed["available"] is True
        assert [t["id"] for t in listed["tokens"]] == [record["id"]]
        # 一覧に平文もハッシュも出さない
        assert token not in json.dumps(listed)
        assert "hash" not in listed["tokens"][0]

        # ファイルにはハッシュだけ
        stored = (tmp_path / "ledger.mcp_tokens.json").read_text(encoding="utf-8")
        assert token not in stored

        assert c.delete(f"/api/mcp/tokens/{record['id']}").json() == {"ok": True}
        assert c.get("/api/mcp/tokens").json()["tokens"] == []
        assert c.delete(f"/api/mcp/tokens/{record['id']}").status_code == 404


def test_issue_requires_label(su_app):
    with TestClient(su_app) as c:
        assert c.post("/api/mcp/tokens", json={"label": "  "}).status_code == 422
        assert c.post("/api/mcp/tokens", json={"label": "x" * 61}).status_code == 422


# ---- /mcp の認証 ----


def test_mcp_requires_token(su_app):
    with TestClient(su_app) as c:
        r = _rpc(c, None, "tools/list")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert _rpc(c, "csmcp_wrong", "tools/list").status_code == 401
        assert _rpc(c, "", "tools/list").status_code == 401


def test_revoked_token_is_rejected(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        assert _rpc(c, token, "tools/list").status_code == 200
        token_id = c.get("/api/mcp/tokens").json()["tokens"][0]["id"]
        c.delete(f"/api/mcp/tokens/{token_id}")
        assert _rpc(c, token, "tools/list").status_code == 401


def test_no_redirect_on_mcp_path(su_app):
    """/mcp → /mcp/ のリダイレクトを出さない（POST のリダイレクトを追わないクライアント対策）。"""
    with TestClient(su_app, follow_redirects=False) as c:
        token = _issue(c)
        assert _rpc(c, token, "tools/list").status_code == 200


# ---- ツールの形（他のクライアントとの互換） ----


EXPECTED_TOOLS = {
    "get_portfolio_summary", "list_accounts", "get_account_assets", "get_asset_accounts",
    "get_portfolio_history", "get_balances_at", "list_transactions", "list_import_batches",
}


def test_tools_are_portable(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        tools = _rpc(c, token, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        # Codex（[A-Za-z0-9_] 以外を置換）・OpenAI（64 文字）・Gemini（接頭辞込み 63 文字）
        assert re.fullmatch(r"[a-z0-9_]{1,40}", t["name"]), t["name"]
        assert t.get("description")
        ann = t["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False
        schema = json.dumps(t["inputSchema"])
        # Gemini CLI で壊れた前例のある $ref / null を許す型を使わない
        assert "$ref" not in schema and "$defs" not in schema and "anyOf" not in schema
        assert "ctx" not in t["inputSchema"].get("properties", {})
        # 結果は本文の JSON 文字列だけ（structuredContent と食い違わせない）
        assert "outputSchema" not in t


def test_server_instructions_lead_with_essentials(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        r = _rpc(c, token, "initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        })
    info = r.json()["result"]
    assert info["serverInfo"]["name"] == "cryptosummary"
    head = info["instructions"][:512]
    # 先頭 512 文字だけを読むクライアントでも、数値と null の約束が伝わる
    assert "decimal strings" in head and "null" in head and "warnings" in head


# ---- ツールの中身 ----


def test_summary_matches_rest_api(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        via_mcp = _call_json(c, token, "get_portfolio_summary", {"currency": "USD"})
        via_rest = c.get("/api/summary?currency=USD").json()
    via_mcp.pop("generated_at"), via_rest.pop("generated_at")
    assert via_mcp == via_rest
    # 0.5 BTC × 60000 + (2 − 0.5 − 0.01) ETH × 3000
    assert Decimal(via_mcp["total_value"]) == Decimal("30000") + Decimal("1.49") * 3000


def test_accounts_and_breakdowns(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        accounts = _call_json(c, token, "list_accounts", {"currency": "JPY"})
        assert accounts["currency"] == "JPY"
        names = {s["source"] for s in accounts["sources"]}
        assert names == {"bitFlyer", "Gmo"}

        gmo = _call_json(c, token, "get_account_assets", {"account": "Gmo"})
        assert gmo["account"] == "Gmo"
        assert [a["asset"] for a in gmo["assets"]] == ["ETH"]
        # ソース ID や大文字・小文字の違いでも同じ口座に解決する
        assert _call_json(c, token, "get_account_assets", {"account": "gmo"}) == gmo
        assert _call_json(c, token, "get_account_assets", {"account": "BITFLYER"})["account"] == "bitFlyer"

        btc = _call_json(c, token, "get_asset_accounts", {"asset": "btc"})
        assert btc["asset"] == "BTC" and btc["total_balance"] == "0.5"


def test_unknown_account_lists_candidates(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        result = _call(c, token, "get_account_assets", {"account": "binance"})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "binance" in text and "bitFlyer" in text and "Gmo" in text


def test_invalid_argument_is_a_tool_error(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        bad_currency = _call(c, token, "list_accounts", {"currency": "BTC"})
        bad_date = _call(c, token, "list_transactions", {"since": "yesterday"})
    assert bad_currency["isError"] is True
    assert bad_date["isError"] is True and "since" in bad_date["content"][0]["text"]


def test_transactions(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        page = _call_json(c, token, "list_transactions", {"account": "gmo", "order": "asc"})
        assert page["total"] == 2
        assert [t["type"] for t in page["transactions"]] == ["deposit", "withdraw"]
        # 既定では行ごとの残高を付けない（出力を小さく保つ）
        assert "running_balances" not in page["transactions"][0]

        with_bal = _call_json(c, token, "list_transactions",
                              {"account": "gmo", "include_running_balances": True})
        assert "running_balances" in with_bal["transactions"][0]

        month = _call_json(c, token, "list_transactions", {"month": "2024-02"})
        assert month["month"] == "2024-02" and month["total"] == 2

        # 解決できないタイムゾーンは黙って UTC に倒さず、エラーで返す
        bad_tz = _call(c, token, "list_transactions", {"month": "2024-02", "timezone": "Mars/Base"})
        assert bad_tz["isError"] is True and "Mars/Base" in bad_tz["content"][0]["text"]


def test_history_balances_and_batches(su_app):
    with TestClient(su_app) as c:
        token = _issue(c)
        hist = _call_json(c, token, "get_portfolio_history",
                          {"range": "7d", "scope": "asset:btc", "metric": "balance"})
        assert hist["scope"] == "asset:BTC" and hist["metric"] == "balance"
        assert hist["points"][-1]["balance"] == "0.5"

        bal = _call_json(c, token, "get_balances_at", {"as_of": "2024-02-15"})
        assert {a["asset"]: a["balance"] for a in bal["assets"]} == {"BTC": "0.5", "ETH": "2"}

        batches = _call_json(c, token, "list_import_batches")
        assert "batches" in batches


# ---- MCP の新旧の手順 ----


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_sdk_client_both_protocol_eras(su_app, mode):
    """initialize で始める旧手順と、2026-07-28 版（server/discover）の両方で呼べる。"""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    # lifespan（SDK のセッションマネージャー）は 1 アプリにつき 1 回しか回せないので、
    # トークンの発行は lifespan を回さずに行う。
    token = _issue(TestClient(su_app))

    async def run() -> tuple[set[str], dict]:
        async with su_app.router.lifespan_context(su_app):
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=su_app),
                base_url="http://testserver",
                headers={"Authorization": f"Bearer {token}"},
            )
            async with Client(streamable_http_client("http://testserver/mcp", http_client=http),
                              mode=mode) as client:
                tools = await client.list_tools()
                result = await client.call_tool("get_portfolio_summary", {"currency": "USD"})
                return {t.name for t in tools.tools}, json.loads(result.content[0].text)

    names, summary = asyncio.run(run())
    assert names == EXPECTED_TOOLS
    assert summary["currency"] == "USD"


# ---- マルチユーザー ----


@pytest.fixture
def mu_app(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _seed(data / f"{SUB_A}.db")
    db_b = Ledger(data / f"{SUB_B}.db")
    db_b.upsert(_tx("nexo", "usdt1", datetime(2024, 3, 1, tzinfo=timezone.utc),
                    type=TxType.DEPOSIT, received_asset="USDT", received_amount=Decimal("100")))
    db_b.close()
    monkeypatch.setenv("CS_SERVICE_TOKEN", "service-token-for-as")
    app = web_app.create_app(data_dir=str(data))
    return app, data


def _as_user(app, sub: str) -> None:
    app.dependency_overrides[require_user] = lambda: {
        "sub": sub, "email": f"{sub[-4:]}@example.com", "name": "", "picture": ""}


def test_multi_user_reads_own_ledger_only(mu_app):
    app, data = mu_app
    with TestClient(app) as c:
        _as_user(app, SUB_A)
        token_a = _issue(c, "A")
        _as_user(app, SUB_B)
        token_b = _issue(c, "B")
        # 他人のトークンは一覧に出ず、失効もできない
        b_tokens = c.get("/api/mcp/tokens").json()["tokens"]
        assert [t["label"] for t in b_tokens] == ["B"]
        app.dependency_overrides.clear()

        a = _call_json(c, token_a, "get_portfolio_summary")
        b = _call_json(c, token_b, "get_portfolio_summary")
    assert {x["asset"] for x in a["assets"]} == {"BTC", "ETH"}
    assert {x["asset"] for x in b["assets"]} == {"USDT"}
    assert (data / "_mcp_tokens.json").exists()


def test_multi_user_token_management_needs_login(mu_app):
    app, _ = mu_app
    with TestClient(app) as c:
        assert c.get("/api/mcp/tokens").status_code == 401
        assert c.post("/api/mcp/tokens", json={"label": "x"}).status_code == 401
        # Asset Summary 用のサービストークンでは発行できない
        r = c.post("/api/mcp/tokens", json={"label": "x"},
                   headers={"Authorization": "Bearer service-token-for-as", "X-CS-User": SUB_A})
        assert r.status_code == 401


def test_service_token_does_not_open_mcp(mu_app):
    app, _ = mu_app
    with TestClient(app) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": ACCEPT, "Authorization": "Bearer service-token-for-as",
                            "X-CS-User": SUB_A})
    assert r.status_code == 401


def test_missing_ledger_is_not_created(mu_app):
    app, data = mu_app
    ghost = "100000000000000000001"
    with TestClient(app) as c:
        _as_user(app, ghost)
        token = _issue(c, "ghost")
        app.dependency_overrides.clear()
        result = _call(c, token, "get_portfolio_summary")
    assert result["isError"] is True
    assert not (data / f"{ghost}.db").exists()
