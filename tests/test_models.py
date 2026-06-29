from datetime import datetime, timezone
from decimal import Decimal

import pytest

from crypto_summary.core.models import CanonicalTx, TxType


def make_trade() -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id("binance", "row:1"),
        source="binance",
        timestamp=datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc),
        type=TxType.TRADE,
        received_asset="BTC",
        received_amount=Decimal("0.00238095"),
        sent_asset="USDT",
        sent_amount=Decimal("100"),
        fee_asset="BNB",
        fee_amount=Decimal("0.0001"),
    )


def test_canonical_tx_fields():
    tx = make_trade()
    assert tx.type == TxType.TRADE
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.00238095")
    assert tx.sent_amount == Decimal("100")


def test_make_id_is_deterministic():
    id1 = CanonicalTx.make_id("binance", "row:1")
    id2 = CanonicalTx.make_id("binance", "row:1")
    assert id1 == id2


def test_make_id_differs_by_source():
    assert CanonicalTx.make_id("binance", "row:1") != CanonicalTx.make_id("bybit", "row:1")


def test_make_id_differs_by_key():
    assert CanonicalTx.make_id("binance", "row:1") != CanonicalTx.make_id("binance", "row:2")


def test_make_id_length():
    assert len(CanonicalTx.make_id("binance", "x")) == 16


def test_tx_type_enum_values():
    assert TxType.TRADE.value == "trade"
    assert TxType.DEPOSIT.value == "deposit"
    assert TxType.REWARD.value == "reward"


def test_optional_fields_default_none():
    tx = CanonicalTx(
        id="abc",
        source="test",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        type=TxType.DEPOSIT,
        received_asset="BTC",
        received_amount=Decimal("1.0"),
    )
    assert tx.sent_asset is None
    assert tx.fee_asset is None
    assert tx.tx_hash is None
    assert tx.raw == {}
