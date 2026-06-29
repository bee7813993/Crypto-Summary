"""core.portfolio のテスト（Ledger は in-memory SQLite を使用）。

日次残高スナップショット計算・資産セット抽出を検証する。
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.core import portfolio as pf


def _dt(y, m, d, h=12):
    return datetime(y, m, d, h, 0, 0, tzinfo=timezone.utc)


def _tx(
    tx_id, source, ts, tx_type=TxType.TRADE,
    ra=None, rv=None, sa=None, sv=None, fa=None, fv=None,
):
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


@pytest.fixture()
def ledger(tmp_path):
    db = Ledger(tmp_path / "test.db")
    yield db
    db.close()


# ---- daily_balances ----

def test_empty_ledger_returns_empty(ledger):
    out = pf.daily_balances(ledger)
    assert out == {}


def test_single_deposit_accumulates(ledger):
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="BTC", rv=1.0))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 3))
    # 1/1 に +1 BTC → 1/1〜1/3 すべて 1 BTC
    assert out["2024-01-01"]["BTC"] == Decimal("1")
    assert out["2024-01-02"]["BTC"] == Decimal("1")
    assert out["2024-01-03"]["BTC"] == Decimal("1")


def test_trade_updates_two_assets(ledger):
    # 1/1: USDT 入金 1000
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="USDT", rv=1000))
    # 1/2: USDT 500 → BTC 0.01
    ledger.upsert(_tx("t1", "ex", _dt(2024, 1, 2), TxType.TRADE,
                      ra="BTC", rv=0.01, sa="USDT", sv=500))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 2))
    assert out["2024-01-01"] == {"USDT": Decimal("1000")}
    assert out["2024-01-02"]["BTC"] == Decimal("0.01")
    assert out["2024-01-02"]["USDT"] == Decimal("500")


def test_fee_reduces_balance(ledger):
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="ETH", rv=2))
    ledger.upsert(_tx("w1", "ex", _dt(2024, 1, 2), TxType.WITHDRAW,
                      sa="ETH", sv=1, fa="ETH", fv=0.01))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 2))
    assert out["2024-01-01"]["ETH"] == Decimal("2")
    # 2 - 1 - 0.01 = 0.99
    assert out["2024-01-02"]["ETH"] == Decimal("0.99")


def test_zero_balance_omitted(ledger):
    """残高がゼロになった日はスナップショットから除外される。"""
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="BTC", rv=1))
    ledger.upsert(_tx("w1", "ex", _dt(2024, 1, 2), TxType.WITHDRAW,
                      sa="BTC", sv=1))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 2))
    assert out["2024-01-01"]["BTC"] == Decimal("1")
    # BTC は 1/2 でゼロになる → 除外
    assert "BTC" not in out.get("2024-01-02", {})


def test_no_txs_in_range_fills_forward(ledger):
    """範囲内に取引がなくても前日残高を引き継ぐ。"""
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="BTC", rv=0.5))
    # 取引なし 1/2〜1/5 → 毎日 0.5 BTC が続く
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 5))
    for iso in ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]:
        assert out[iso]["BTC"] == Decimal("0.5")


def test_source_filter(ledger):
    """source フィルタで特定口座の残高だけ集計できる。"""
    ledger.upsert(_tx("d1", "a", _dt(2024, 1, 1), TxType.DEPOSIT, ra="BTC", rv=1))
    ledger.upsert(_tx("d2", "b", _dt(2024, 1, 1), TxType.DEPOSIT, ra="ETH", rv=5))
    out_a = pf.daily_balances(ledger, source="a", start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert "BTC" in out_a["2024-01-01"]
    assert "ETH" not in out_a["2024-01-01"]


def test_multiple_txs_same_day_accumulate(ledger):
    """同日に複数取引があれば合算される。"""
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1, 10), TxType.DEPOSIT,
                      ra="USDT", rv=500))
    ledger.upsert(_tx("d2", "ex", _dt(2024, 1, 1, 14), TxType.DEPOSIT,
                      ra="USDT", rv=300))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 1))
    assert out["2024-01-01"]["USDT"] == Decimal("800")


def test_start_before_first_tx(ledger):
    """start が最初の取引より前でも正常に動作する。"""
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 5), TxType.DEPOSIT,
                      ra="BTC", rv=1))
    out = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 6))
    # 1/1〜1/4 は取引なし → スナップショットなし
    for iso in ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]:
        assert iso not in out
    assert out["2024-01-05"]["BTC"] == Decimal("1")


def test_range_includes_pre_range_balance(ledger):
    """range_start より前の取引で形成された残高がグラフ範囲内でも正しく反映される。

    これは 7D/30D などの期間フィルタで発生していたバグの回帰テスト。
    以前は range_start 以前の取引が無視され、グラフが「期間内の純増分」
    だけを示す誤った値になっていた。
    """
    # 1月1日に 1 BTC 入金（グラフ範囲外）
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="BTC", rv=1))
    # 3月1日に 0.5 BTC 追加入金（グラフ範囲外）
    ledger.upsert(_tx("d2", "ex", _dt(2024, 3, 1), TxType.DEPOSIT,
                      ra="BTC", rv=0.5))
    # 6月1日に 0.1 BTC 入金（グラフ範囲内）
    ledger.upsert(_tx("d3", "ex", _dt(2024, 6, 1), TxType.DEPOSIT,
                      ra="BTC", rv=0.1))

    # 5月29日〜6月1日の7日間 のグラフを要求
    out = pf.daily_balances(ledger, start=date(2024, 5, 29), end=date(2024, 6, 1))

    # 5/29: 1月+3月の残高 1.5 BTC が開始残高として正しく引き継がれている
    assert out["2024-05-29"]["BTC"] == Decimal("1.5")
    assert out["2024-05-30"]["BTC"] == Decimal("1.5")
    assert out["2024-05-31"]["BTC"] == Decimal("1.5")
    # 6/1: 0.1 BTC 追加 → 1.6 BTC
    assert out["2024-06-01"]["BTC"] == Decimal("1.6")


def test_assets_in_range(ledger):
    ledger.upsert(_tx("d1", "ex", _dt(2024, 1, 1), TxType.DEPOSIT,
                      ra="BTC", rv=1))
    ledger.upsert(_tx("d2", "ex", _dt(2024, 1, 2), TxType.DEPOSIT,
                      ra="ETH", rv=2))
    snaps = pf.daily_balances(ledger, start=date(2024, 1, 1), end=date(2024, 1, 2))
    assets = pf.assets_in_range(snaps)
    assert assets == {"BTC", "ETH"}
