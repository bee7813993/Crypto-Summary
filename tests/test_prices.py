"""core.prices のテスト（CoinGecko HTTP はモック）。

法定通貨クロスレート換算と MATIC のミントID修正を検証する。
"""
from decimal import Decimal

import pytest

from crypto_summary.core import prices


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """価格キャッシュを一時ファイルに隔離する。"""
    monkeypatch.setattr(prices, "_cache_path", lambda: tmp_path / "prices.json")


def _install_fake_http(monkeypatch, simple=None, fx=None):
    import httpx

    def fake_get(url, *args, **kwargs):
        if "exchange_rates" in url:
            return FakeResp({"rates": fx or {}})
        return FakeResp(simple or {})

    monkeypatch.setattr(httpx, "get", fake_get)


def test_matic_uses_current_id(monkeypatch):
    """MATIC は polygon-ecosystem-token で価格取得できる。"""
    _install_fake_http(monkeypatch, simple={"polygon-ecosystem-token": {"usd": 0.08}})
    p = prices.fetch_prices(["MATIC"], "USD")
    assert p["MATIC"] == Decimal("0.08")


def test_same_fiat_is_one(monkeypatch):
    """対象通貨と同じ法定通貨は 1.0。"""
    _install_fake_http(monkeypatch, simple={})
    p = prices.fetch_prices(["JPY"], "JPY")
    assert p["JPY"] == Decimal("1")


def test_fiat_cross_rate(monkeypatch):
    """異なる法定通貨は exchange_rates からクロスレートで換算する。"""
    fx = {
        "rates": {
            "usd": {"value": 66000.0, "type": "fiat"},
            "jpy": {"value": 9900000.0, "type": "fiat"},
        }
    }
    # exchange_rates だけ使う（simple/price は呼ばれない想定だが念のため空）
    _install_fake_http(monkeypatch, simple={}, fx=fx["rates"])
    p = prices.fetch_prices(["JPY"], "USD")
    # 1 JPY in USD = usd.value / jpy.value = 66000 / 9900000
    assert p["JPY"] == Decimal("66000") / Decimal("9900000")


def test_unknown_asset_missing(monkeypatch):
    """ID 未登録の資産は結果に含まれない。"""
    _install_fake_http(monkeypatch, simple={})
    p = prices.fetch_prices(["MYSTERYTOKEN"], "USD")
    assert "MYSTERYTOKEN" not in p


def test_warn_called_on_error(monkeypatch):
    """取得失敗時は warn コールバックが呼ばれる。"""
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "get", boom)
    msgs = []
    p = prices.fetch_prices(["BTC"], "USD", warn=msgs.append)
    assert p == {}
    assert msgs and "失敗" in msgs[0]


def test_coingecko_key_read_dynamically(monkeypatch):
    """COINGECKO_API_KEY は呼び出しごとに env から読まれる（Web 設定の即時反映）。"""
    monkeypatch.setenv("COINGECKO_API_KEY", "REALKEY")
    assert prices._coingecko_api_key() == "REALKEY"
    assert prices._request_headers() == {"x-cg-demo-api-key": "REALKEY"}


def test_coingecko_placeholder_key_ignored(monkeypatch):
    """.env.example のプレースホルダ値は未設定として無視される（401 回避）。"""
    monkeypatch.setenv("COINGECKO_API_KEY", "your-coingecko-demo-api-key")
    assert prices._coingecko_api_key() == ""
    assert prices._request_headers() == {}


def test_coingecko_no_key_no_header(monkeypatch):
    """キー未設定ならヘッダーは付かない（無料枠で動作）。"""
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    assert prices._coingecko_api_key() == ""
    assert prices._request_headers() == {}
