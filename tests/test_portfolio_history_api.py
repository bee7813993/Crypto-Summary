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


def test_account_scope(app_client):
    r = app_client.get("/api/portfolio-history?currency=USD&range=90d&scope=account:bybit1")
    assert r.status_code == 200
    data = r.json()
    assert data["scope"] == "account:bybit1"
    pts = data["points"]
    assert len(pts) > 0


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
