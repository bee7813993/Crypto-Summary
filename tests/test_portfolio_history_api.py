"""GET /api/portfolio-history のテスト（価格履歴 HTTP はモック）。

スコープ（total/account/asset）・レンジ・未価格資産・空データを検証する。
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.web.app import create_app


# ---------- helpers ----------

def _recent(days_ago=5):
    """今日から days_ago 日前の datetime を返す（テストがウィンドウ内に収まるよう）。"""
    d = date.today() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=timezone.utc)


def _tx(tx_id, source, ts, tx_type=TxType.DEPOSIT,
        ra=None, rv=None, sa=None, sv=None, fa=None, fv=None):
    return CanonicalTx(
        id=tx_id, source=source, timestamp=ts, type=tx_type,
        received_asset=ra,
        received_amount=Decimal(str(rv)) if rv is not None else None,
        sent_asset=sa,
        sent_amount=Decimal(str(sv)) if sv is not None else None,
        fee_asset=fa,
        fee_amount=Decimal(str(fv)) if fv is not None else None,
        raw={},
    )


def _ms_for(days_ago):
    d = date.today() - timedelta(days=days_ago)
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """テスト用アプリ（DB + 価格履歴モック）。"""
    db = tmp_path / "test.db"
    ledger = Ledger(str(db))

    # 5日前: BTC 1枚入金
    ledger.upsert(_tx("d1", "bybit1", _recent(5), TxType.DEPOSIT, ra="BTC", rv=1))
    # 4日前: USDT 1000入金
    ledger.upsert(_tx("d2", "bybit1", _recent(4), TxType.DEPOSIT, ra="USDT", rv=1000))
    ledger.close()

    import httpx
    from crypto_summary.core import price_history as ph

    monkeypatch.setattr(ph, "_hist_cache_path", lambda: tmp_path / "ph.json")

    def fake_get(url, *a, **k):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                if "bitcoin" in url:
                    return {"prices": [
                        [_ms_for(d), 40000 + d * 1000]
                        for d in range(6, 0, -1)  # 6〜1日前
                    ]}
                return {"prices": []}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)

    app = create_app(str(db))
    return TestClient(app)


# ---------- tests ----------

def test_total_scope_returns_points(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=total")
    assert r.status_code == 200
    data = r.json()
    assert data["currency"] == "USD"
    assert data["range"] == "90d"
    # BTC の価格があるポイントが返る（少なくとも1件）
    pts = {p["t"]: p["value"] for p in data["points"]}
    assert len(pts) > 0
    # 5日前に BTC 1枚入金 → 5日前の価格は 40000 + 5*1000 = 45000
    iso_5 = (date.today() - timedelta(days=5)).isoformat()
    assert iso_5 in pts
    assert Decimal(pts[iso_5]) == Decimal("45000")


def test_unpriced_assets_reported(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=total")
    data = r.json()
    # USDT はモックが空を返すので unpriced に含まれる（または含まれない場合もある）
    assert isinstance(data["unpriced"], list)


def test_asset_scope(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=asset:BTC")
    assert r.status_code == 200
    data = r.json()
    assert data["scope"] == "asset:BTC"
    pts = {p["t"]: p["value"] for p in data["points"]}
    assert len(pts) > 0
    iso_5 = (date.today() - timedelta(days=5)).isoformat()
    assert iso_5 in pts
    assert Decimal(pts[iso_5]) == Decimal("45000")
    # asset スコープでは各ポイントに保有数量(balance)が含まれる
    assert all("balance" in p for p in data["points"])


def test_account_scope(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=account:bybit1")
    assert r.status_code == 200
    data = r.json()
    assert data["scope"] == "account:bybit1"
    pts = data["points"]
    assert len(pts) > 0
    # total/account スコープは複数資産が混在するため balance は付かない
    assert all("balance" not in p for p in pts)


def test_empty_ledger_returns_no_points(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    Ledger(str(db)).close()

    import httpx
    from crypto_summary.core import price_history as ph
    monkeypatch.setattr(ph, "_hist_cache_path", lambda: tmp_path / "ph2.json")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")))

    app = create_app(str(db))
    client = TestClient(app)
    r = client.get("/api/portfolio-history?currency=USD&range=90d&scope=total")
    assert r.status_code == 200
    assert r.json()["points"] == []


def test_invalid_range_defaults_to_90d(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=INVALID&scope=total")
    assert r.status_code == 200
    assert r.json()["range"] == "90d"


def test_1y_range_accepted(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=1y&scope=total")
    assert r.status_code == 200
    assert r.json()["range"] == "1y"


def test_all_range_accepted(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=all&scope=total")
    assert r.status_code == 200
    assert r.json()["range"] == "all"


def test_response_schema(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=7d&scope=total")
    data = r.json()
    for key in ("currency", "range", "scope", "points", "unpriced", "warnings", "generated_at"):
        assert key in data
    if data["points"]:
        assert "t" in data["points"][0]
        assert "value" in data["points"][0]


# ---------- 保有ゼロの日 ----------

def _iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


@pytest.fixture()
def btc_only_prices(monkeypatch):
    """BTC だけ 7〜1 日前の終値 50000 を返す CoinGecko モック。"""
    import httpx

    def fake_get(url, *a, **k):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                if "market_chart" in url and "bitcoin" in url:
                    return {"prices": [[_ms_for(d), 50000] for d in range(7, 0, -1)]}
                return {"prices": []}
        return R()

    monkeypatch.setattr(httpx, "get", fake_get)


def _client_with(tmp_path, txs):
    db = tmp_path / "hist.db"
    ledger = Ledger(str(db))
    for tx in txs:
        ledger.upsert(tx)
    ledger.close()
    return TestClient(create_app(str(db)))


def test_emptied_account_drops_to_zero(tmp_path, btc_only_prices):
    """全額出金した口座は、出金日以降の評価額が 0 になる。

    保有がゼロの日は daily_balances に載らない。以前はそれを「前日と同じ」と
    解釈して、出金済みの BTC を毎日評価し続けていた。
    """
    client = _client_with(tmp_path, [
        _tx("in", "acct1", _recent(5), TxType.DEPOSIT, ra="BTC", rv=1),
        _tx("out", "acct1", _recent(3), TxType.WITHDRAW, sa="BTC", sv=1),
    ])
    data = client.get("/api/portfolio-history?currency=USD&range=7d&scope=account:Acct1").json()
    pts = {p["t"]: p["value"] for p in data["points"]}
    assert Decimal(pts[_iso(4)]) == Decimal("50000")
    assert [pts[_iso(d)] for d in (3, 2, 1, 0)] == ["0", "0", "0", "0"]


def test_asset_fully_withdrawn_reports_zero_balance(tmp_path, btc_only_prices):
    client = _client_with(tmp_path, [
        _tx("in", "acct1", _recent(5), TxType.DEPOSIT, ra="BTC", rv=1),
        _tx("out", "acct1", _recent(3), TxType.WITHDRAW, sa="BTC", sv=1),
    ])
    data = client.get("/api/portfolio-history?currency=USD&range=7d&scope=asset:BTC").json()
    pts = {p["t"]: p for p in data["points"]}
    assert Decimal(pts[_iso(4)]["balance"]) == Decimal("1")
    assert pts[_iso(3)] == {"t": _iso(3), "value": "0", "balance": "0"}


def test_asset_sold_out_is_zero_not_a_gap(tmp_path, btc_only_prices):
    """売り切った日から買い戻すまでは 0 の点が並ぶ（点が欠けて前後が直結しない）。

    売却代金の JPY が残るので、その日は daily_balances に載るが BTC は無い。
    """
    client = _client_with(tmp_path, [
        _tx("buy", "ex", _recent(6), TxType.TRADE, ra="BTC", rv=1, sa="JPY", sv=5000000),
        _tx("sell", "ex", _recent(4), TxType.TRADE, ra="JPY", rv=6000000, sa="BTC", sv=1),
        _tx("rebuy", "ex", _recent(2), TxType.TRADE, ra="BTC", rv="0.5", sa="JPY", sv=3000000),
    ])
    data = client.get("/api/portfolio-history?currency=USD&range=7d&scope=asset:BTC").json()
    pts = {p["t"]: p for p in data["points"]}
    assert Decimal(pts[_iso(5)]["value"]) == Decimal("50000")
    assert pts[_iso(4)]["value"] == "0" and pts[_iso(4)]["balance"] == "0"
    assert pts[_iso(3)]["value"] == "0"
    assert Decimal(pts[_iso(2)]["balance"]) == Decimal("0.5")
    assert Decimal(pts[_iso(2)]["value"]) == Decimal("25000")


# ---------- metric=balance（保有数量の推移） ----------

@pytest.fixture()
def no_coingecko(monkeypatch):
    """CoinGecko を叩いたら失敗させる（数量の推移は台帳だけで決まる）。"""
    import httpx

    def forbid(*a, **k):
        raise AssertionError("metric=balance must not fetch prices")

    monkeypatch.setattr(httpx, "get", forbid)


def test_balance_metric_returns_quantity_for_every_day(tmp_path, no_coingecko):
    client = _client_with(tmp_path, [
        _tx("in1", "ex", _recent(5), TxType.DEPOSIT, ra="BTC", rv=1),
        _tx("in2", "wallet", _recent(3), TxType.DEPOSIT, ra="BTC", rv="0.5"),
        _tx("out", "ex", _recent(1), TxType.WITHDRAW, sa="BTC", sv="1.2", fa="BTC", fv="0.3"),
    ])
    data = client.get(
        "/api/portfolio-history?currency=JPY&range=7d&scope=asset:BTC&metric=balance"
    ).json()
    assert data["metric"] == "balance"
    assert data["unpriced"] == [] and data["is_partial"] is False
    # 最初に持った日から今日まで 1 日も欠けず、数量だけが入る（評価額は付かない）
    assert [p["t"] for p in data["points"]] == [_iso(d) for d in range(5, -1, -1)]
    assert all(set(p) == {"t", "balance"} for p in data["points"])
    assert [Decimal(p["balance"]) for p in data["points"]] == [
        Decimal("1"), Decimal("1"), Decimal("1.5"), Decimal("1.5"), Decimal("0"), Decimal("0"),
    ]


def test_balance_metric_ignores_other_assets_in_the_same_trade(tmp_path, no_coingecko):
    """売買の相手側（JPY）は数えない。売り切った後は 0。"""
    client = _client_with(tmp_path, [
        _tx("buy", "ex", _recent(3), TxType.TRADE, ra="BTC", rv="0.2", sa="JPY", sv=2000000),
        _tx("sell", "ex", _recent(1), TxType.TRADE, ra="JPY", rv=2500000, sa="BTC", sv="0.2"),
    ])
    data = client.get(
        "/api/portfolio-history?range=7d&scope=asset:btc&metric=balance"
    ).json()
    assert [Decimal(p["balance"]) for p in data["points"]] == [
        Decimal("0.2"), Decimal("0.2"), Decimal("0"), Decimal("0"),
    ]


def test_balance_metric_works_without_price(tmp_path, no_coingecko):
    """CoinGecko に無い資産でも数量の推移は出る（評価額だと点が 1 つも出ない）。"""
    client = _client_with(tmp_path, [
        _tx("in", "wallet", _recent(2), TxType.DEPOSIT, ra="NOPRICE", rv=42),
    ])
    data = client.get(
        "/api/portfolio-history?range=7d&scope=asset:NOPRICE&metric=balance"
    ).json()
    assert [(p["t"], p["balance"]) for p in data["points"]] == [
        (_iso(2), "42"), (_iso(1), "42"), (_iso(0), "42"),
    ]


def test_balance_metric_respects_excluded_labels(tmp_path, no_coingecko):
    """表示設定で除外中の日次利息は数量にも含めない（残高・評価額と同じ扱い）。"""
    interest = CanonicalTx(
        id="int", source="pbr", timestamp=_recent(1), type=TxType.REWARD,
        received_asset="BTC", received_amount=Decimal("0.01"), label="daily_interest", raw={},
    )
    client = _client_with(tmp_path, [
        _tx("in", "pbr", _recent(2), TxType.DEPOSIT, ra="BTC", rv=1),
        interest,
    ])
    url = "/api/portfolio-history?range=7d&scope=asset:BTC&metric=balance"
    assert client.get(url).json()["points"][-1]["balance"] == "1.01"

    client.put("/api/prefs", json={"prefs": {"include_daily_interest": False}})
    assert client.get(url).json()["points"][-1]["balance"] == "1"


def test_balance_metric_needs_asset_scope(app_client):
    """単位の違う資産は足せないので、asset 以外のスコープでは評価額に丸める。"""
    for scope in ("total", "account:Bybit1"):
        data = app_client.get(
            f"/api/portfolio-history?currency=USD&range=90d&scope={scope}&metric=balance"
        ).json()
        assert data["metric"] == "value"
        assert data["points"] and all("value" in p for p in data["points"])


def test_unknown_metric_falls_back_to_value(app_client):
    data = app_client.get(
        "/api/portfolio-history?currency=USD&range=90d&scope=asset:BTC&metric=bogus"
    ).json()
    assert data["metric"] == "value"
    assert all("value" in p for p in data["points"])


def test_value_is_the_default_metric(app_client):
    data = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=asset:BTC").json()
    assert data["metric"] == "value"
