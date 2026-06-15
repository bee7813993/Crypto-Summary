"""Web UI API のテスト（CoinGecko はモック）。

価格取得をモンキーパッチして決定的に検証する。
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.core.models import CanonicalTx, TxType  # noqa: E402
from crypto_summary.web import app as web_app  # noqa: E402


def _deposit(source: str, asset: str, amount: str, day: int) -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"{asset}{day}"),
        source=source,
        timestamp=datetime(2024, 1, day, tzinfo=timezone.utc),
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=Decimal(amount),
    )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> str:
    db = Ledger(tmp_path / "web.db")
    db.upsert(_deposit("acct_a", "BTC", "0.5", 1))
    db.upsert(_deposit("acct_a", "ETH", "2", 2))
    db.upsert(_deposit("acct_b", "SOL", "10", 3))
    db.upsert(_deposit("acct_b", "MYSTERY", "999", 4))  # 価格なし
    db.close()

    # CoinGecko を固定価格でモック
    def fake_prices(assets, currency, warn=None):
        table = {"BTC": Decimal("60000"), "ETH": Decimal("3000"), "SOL": Decimal("150")}
        return {a.upper(): table[a.upper()] for a in assets if a.upper() in table}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db_path) -> TestClient:
    return TestClient(web_app.create_app(db_path))


def test_summary_totals(client):
    r = client.get("/api/summary?currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["currency"] == "USD"
    # 0.5*60000 + 2*3000 + 10*150 = 30000 + 6000 + 1500 = 37500
    assert Decimal(d["total_value"]) == Decimal("37500")
    assert d["asset_count"] == 4
    assert d["priced_count"] == 3
    assert "MYSTERY" in d["unpriced"]


def test_summary_sorted_by_value(client):
    d = client.get("/api/summary?currency=USD").json()
    # 評価額降順: BTC(30000) > ETH(6000) > SOL(1500) > MYSTERY(価格なし末尾)
    assets = [a["asset"] for a in d["assets"]]
    assert assets == ["BTC", "ETH", "SOL", "MYSTERY"]


def test_summary_asset_fields(client):
    d = client.get("/api/summary?currency=USD").json()
    btc = next(a for a in d["assets"] if a["asset"] == "BTC")
    assert btc["has_price"] is True
    assert Decimal(btc["value"]) == Decimal("30000")
    mystery = next(a for a in d["assets"] if a["asset"] == "MYSTERY")
    assert mystery["has_price"] is False
    assert mystery["value"] is None


def test_sources_breakdown(client):
    r = client.get("/api/sources?currency=USD")
    assert r.status_code == 200
    d = r.json()
    # source_id は _display_name でタイトルケース変換される: acct_a → "Acct A"
    by_name = {s["source"]: s for s in d["sources"]}
    assert Decimal(by_name["Acct A"]["total_value"]) == Decimal("36000")
    assert Decimal(by_name["Acct B"]["total_value"]) == Decimal("1500")
    # source_ids フィールドに元のIDが含まれる
    assert "acct_a" in by_name["Acct A"]["source_ids"]
    # 評価額降順
    assert d["sources"][0]["source"] == "Acct A"


def test_invalid_currency_falls_back_to_usd(client):
    d = client.get("/api/summary?currency=XXX").json()
    assert d["currency"] == "USD"


def test_meta(client):
    d = client.get("/api/meta").json()
    assert "USD" in d["currencies"]
    assert "JPY" in d["currencies"]


def test_account_assets_drilldown(client):
    r = client.get("/api/account-assets?account=Acct+A&currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["account"] == "Acct A"
    assets_by_name = {a["asset"]: a for a in d["assets"]}
    assert "BTC" in assets_by_name
    assert Decimal(assets_by_name["BTC"]["value"]) == Decimal("30000")
    assert Decimal(d["total_value"]) == Decimal("36000")


def test_asset_accounts_drilldown(client):
    r = client.get("/api/asset-accounts?asset=BTC&currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["asset"] == "BTC"
    assert len(d["accounts"]) == 1
    assert d["accounts"][0]["account"] == "Acct A"
    assert Decimal(d["total_balance"]) == Decimal("0.5")


def test_transactions_all(client):
    r = client.get("/api/transactions")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 4
    assert d["page"] == 1
    assert d["total_pages"] == 1
    # 新しい順
    assert d["transactions"][0]["timestamp"] > d["transactions"][-1]["timestamp"]


def test_transactions_filter_account(client):
    r = client.get("/api/transactions?account=Acct+A")
    d = r.json()
    assert d["total"] == 2
    assert all(t["account"] == "Acct A" for t in d["transactions"])


def test_transactions_filter_asset(client):
    r = client.get("/api/transactions?asset=BTC")
    d = r.json()
    assert d["total"] == 1
    assert d["transactions"][0]["received_asset"] == "BTC"


def test_transactions_filter_account_and_asset(client):
    r = client.get("/api/transactions?account=Acct+B&asset=SOL")
    d = r.json()
    assert d["total"] == 1
    assert d["transactions"][0]["received_asset"] == "SOL"


def test_transactions_type_ja(client):
    d = client.get("/api/transactions").json()
    types = {t["type_ja"] for t in d["transactions"]}
    assert "入金" in types


def test_account_groups_get(client):
    r = client.get("/api/account-groups")
    assert r.status_code == 200
    d = r.json()
    assert "groups" in d
    assert "all_source_ids" in d
    assert "unassigned_source_ids" in d
    # acct_a / acct_b は ACCOUNT_GROUPS に未登録 → unassigned
    assert "acct_a" in d["unassigned_source_ids"]
    assert "acct_b" in d["unassigned_source_ids"]


def test_account_groups_put_and_effect(client, db_path):
    # グループを更新: acct_a → "My Exchange"
    r = client.put("/api/account-groups", json={"groups": {"My Exchange": ["acct_a"], "Acct B": ["acct_b"]}})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # 口座一覧に新しい名前が反映される
    sources = client.get("/api/sources?currency=USD").json()
    by_name = {s["source"]: s for s in sources["sources"]}
    assert "My Exchange" in by_name
    assert "Acct B" in by_name
    assert "Acct A" not in by_name


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
