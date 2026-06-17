"""core.price_history のテスト（CoinGecko HTTP はモック）。

日次バケット化・差分キャッシュ・法定通貨/未登録資産の扱いを検証する。
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from crypto_summary.core import price_history as ph


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
    """履歴価格キャッシュを一時ファイルに隔離する。"""
    monkeypatch.setattr(ph, "_hist_cache_path", lambda: tmp_path / "pricehist.json")


def _ms(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


def _install_http(monkeypatch, prices_by_id):
    """coin_id -> [[ms, price], ...] を返すモックを仕込み、呼び出し回数を数える。"""
    import httpx

    calls = {"n": 0}

    def fake_get(url, *args, **kwargs):
        calls["n"] += 1
        for cid, series in prices_by_id.items():
            if f"/coins/{cid}/" in url:
                return FakeResp({"prices": series})
        return FakeResp({"prices": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


# ---- _bucket_daily ----

def test_bucket_daily_takes_last_per_day():
    series = [
        [_ms(2024, 1, 1, 0), 100],
        [_ms(2024, 1, 1, 12), 110],   # 同日後半 → こちらが採用される
        [_ms(2024, 1, 2, 6), 120],
    ]
    out = ph._bucket_daily(series)
    assert out == {"2024-01-01": Decimal("110"), "2024-01-02": Decimal("120")}


def test_bucket_daily_skips_none():
    out = ph._bucket_daily([[_ms(2024, 1, 1), None], [_ms(2024, 1, 2), 5]])
    assert out == {"2024-01-02": Decimal("5")}


# ---- fetch_price_history ----

def test_fetch_maps_dates(monkeypatch):
    _install_http(monkeypatch, {
        "bitcoin": [[_ms(2024, 1, 1), 40000], [_ms(2024, 1, 2), 41000]],
    })
    out = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert out["BTC"] == {"2024-01-01": Decimal("40000"), "2024-01-02": Decimal("41000")}


def test_unknown_asset_omitted(monkeypatch):
    _install_http(monkeypatch, {})
    out = ph.fetch_price_history(["MYSTERY"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert "MYSTERY" not in out


def test_same_currency_fiat_is_one(monkeypatch):
    _install_http(monkeypatch, {})
    out = ph.fetch_price_history(["USD"], "USD", date(2024, 1, 1), date(2024, 1, 3))
    assert out["USD"] == {
        "2024-01-01": Decimal("1"),
        "2024-01-02": Decimal("1"),
        "2024-01-03": Decimal("1"),
    }


def test_other_fiat_omitted(monkeypatch):
    _install_http(monkeypatch, {})
    out = ph.fetch_price_history(["JPY"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert "JPY" not in out


def test_cache_avoids_refetch_for_past_range(monkeypatch):
    """過去レンジは2回目以降キャッシュから返り HTTP を叩かない。"""
    calls = _install_http(monkeypatch, {
        "bitcoin": [[_ms(2024, 1, 1), 40000], [_ms(2024, 1, 2), 41000]],
    })
    a = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert calls["n"] == 1
    b = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert calls["n"] == 1  # 追加コールなし
    assert a == b


def test_cache_fetches_only_missing_tail(monkeypatch):
    """既存キャッシュの後ろに不足日があれば不足分だけ取得して結合する。"""
    calls = _install_http(monkeypatch, {
        "bitcoin": [[_ms(2024, 1, 1), 40000], [_ms(2024, 1, 2), 41000]],
    })
    ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 2))
    assert calls["n"] == 1

    # 範囲を 1/3 まで延長 → 不足分を取得（モックは 1/3 を返す）
    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, *a, **k: FakeResp(
        {"prices": [[_ms(2024, 1, 3), 42000]]}))
    out = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 3))
    assert out["BTC"] == {
        "2024-01-01": Decimal("40000"),
        "2024-01-02": Decimal("41000"),
        "2024-01-03": Decimal("42000"),
    }


def test_future_end_clamped_to_today(monkeypatch):
    """end が未来でも今日までにクランプされる（未来日は要求しない）。"""
    _install_http(monkeypatch, {"bitcoin": [[_ms(2024, 1, 1), 40000]]})
    future = date(2999, 1, 1)
    out = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), future)
    # 2999 の日付は含まれない
    assert all(d <= date.today().isoformat() for d in out.get("BTC", {}))


def test_http_error_warns_and_omits(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    msgs = []
    out = ph.fetch_price_history(["BTC"], "USD", date(2024, 1, 1), date(2024, 1, 2), warn=msgs.append)
    assert "BTC" not in out
    assert msgs and "失敗" in msgs[0]
