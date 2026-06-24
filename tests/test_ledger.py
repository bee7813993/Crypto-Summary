import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType


def _tx(suffix: str = "1", source: str = "test") -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, suffix),
        source=source,
        timestamp=datetime(2024, 1, int(suffix), tzinfo=timezone.utc),
        type=TxType.TRADE,
        received_asset="BTC",
        received_amount=Decimal("0.1"),
        sent_asset="USDT",
        sent_amount=Decimal("4200"),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    db = Ledger(tmp_path / "test.db")
    yield db
    db.close()


def test_upsert_and_count(ledger):
    ledger.upsert(_tx("1"))
    assert ledger.count() == 1


def test_upsert_is_idempotent(ledger):
    tx = _tx("1")
    ledger.upsert(tx)
    ledger.upsert(tx)   # same id → no duplicate
    assert ledger.count() == 1


def test_upsert_many(ledger):
    txs = [_tx(str(i)) for i in range(1, 6)]
    ledger.upsert_many(txs)
    assert ledger.count() == 5


def test_count_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    assert ledger.count("binance") == 2
    assert ledger.count("bybit") == 1
    assert ledger.count() == 3


def test_set_and_get_cursor(ledger):
    assert ledger.get_cursor("binance") is None
    ts = datetime(2024, 3, 1, tzinfo=timezone.utc)
    ledger.set_cursor("binance", ts)
    assert ledger.get_cursor("binance") == ts


def test_cursor_overwrites(ledger):
    t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    ledger.set_cursor("binance", t1)
    ledger.set_cursor("binance", t2)
    assert ledger.get_cursor("binance") == t2


def test_date_ranges_by_source(ledger):
    """ソースごとの取引期間 (最古, 最新) を返す。"""
    ledger.upsert(_tx("1", source="binance"))   # 2024-01-01
    ledger.upsert(_tx("5", source="binance"))   # 2024-01-05
    ledger.upsert(_tx("3", source="bybit"))     # 2024-01-03
    ranges = ledger.date_ranges_by_source()
    assert ranges["binance"][0].startswith("2024-01-01")
    assert ranges["binance"][1].startswith("2024-01-05")
    assert ranges["bybit"][0].startswith("2024-01-03")
    assert ranges["bybit"][1].startswith("2024-01-03")


def test_date_ranges_empty(ledger):
    """取引が無ければ空の辞書。"""
    assert ledger.date_ranges_by_source() == {}


def test_list_import_batches_includes_period(ledger):
    """インポートバッチに取引期間 (first_ts/last_ts) が含まれる。"""
    txs = [_tx("2", source="bf"), _tx("8", source="bf")]  # 2024-01-02, 2024-01-08
    ledger.upsert_many(txs)
    ledger.record_import_batch(
        "batch1", "bf", "bitflyer", "trades.csv", [t.id for t in txs]
    )
    batches = ledger.list_import_batches()
    assert len(batches) == 1
    b = batches[0]
    assert b["first_ts"].startswith("2024-01-02")
    assert b["last_ts"].startswith("2024-01-08")


def test_all_returns_txs(ledger):
    for i in range(1, 4):
        ledger.upsert(_tx(str(i)))
    results = ledger.all()
    assert len(results) == 3
    assert all(isinstance(t, CanonicalTx) for t in results)


def test_all_filter_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="bybit"))
    assert len(ledger.all(source="binance")) == 1
    assert len(ledger.all(source="bybit")) == 1


def test_roundtrip_decimal_precision(ledger):
    tx = CanonicalTx(
        id="precise-test",
        source="test",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        type=TxType.TRADE,
        received_asset="BTC",
        received_amount=Decimal("0.00238095"),
        sent_asset="USDT",
        sent_amount=Decimal("100.00000000"),
        fee_asset="BNB",
        fee_amount=Decimal("0.00010000"),
    )
    ledger.upsert(tx)
    result = ledger.all()[0]
    assert result.received_amount == Decimal("0.00238095")
    assert result.sent_amount == Decimal("100.00000000")
    assert result.fee_amount == Decimal("0.00010000")


def test_sources_summary(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    rows = ledger.sources()
    src_map = {r[0]: r[1] for r in rows}
    assert src_map["binance"] == 2
    assert src_map["bybit"] == 1


def test_clear_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    n = ledger.clear(source="binance")
    assert n == 2
    assert ledger.count("binance") == 0
    assert ledger.count("bybit") == 1


def test_clear_all(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    n = ledger.clear()
    assert n == 2
    assert ledger.count() == 0


def test_balances(ledger):
    ledger.upsert(_tx("1"))  # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("2"))  # +0.1 BTC, -4200 USDT
    bals = ledger.balances()
    assert bals["BTC"] == Decimal("0.2")
    assert bals["USDT"] == Decimal("-8400")


def test_balances_multiple_sources(ledger):
    ledger.upsert(_tx("1", source="nexo_spot"))   # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("2", source="nexo_dnw"))    # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("3", source="binance"))     # +0.1 BTC, -4200 USDT

    # 単一ソース
    one = ledger.balances(source="nexo_spot")
    assert one["BTC"] == Decimal("0.1")

    # 複数ソース合算 (nexo_spot + nexo_dnw)
    combined = ledger.balances(source=["nexo_spot", "nexo_dnw"])
    assert combined["BTC"] == Decimal("0.2")
    assert combined["USDT"] == Decimal("-8400")

    # 全ソース
    all_bal = ledger.balances()
    assert all_bal["BTC"] == Decimal("0.3")


def test_balances_by_source(ledger):
    ledger.upsert(_tx("1", source="nexo_spot"))
    ledger.upsert(_tx("2", source="nexo_dnw"))
    ledger.upsert(_tx("3", source="nexo_dnw"))

    per = ledger.balances_by_source(source=["nexo_spot", "nexo_dnw"])
    assert set(per) == {"nexo_spot", "nexo_dnw"}
    assert per["nexo_spot"]["BTC"] == Decimal("0.1")
    assert per["nexo_dnw"]["BTC"] == Decimal("0.2")
