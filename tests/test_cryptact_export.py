"""Tests for Cryptact custom-file CSV export sink."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sinks.cryptact_csv import (
    to_cryptact_csv_string,
    to_cryptact_rows,
    write_cryptact_csv,
)


def _tx(**kwargs) -> CanonicalTx:
    defaults = dict(
        id="test_id",
        source="bitflyer",
        timestamp=datetime(2024, 3, 15, 10, 30, 0, tzinfo=timezone.utc),
        type=TxType.TRADE,
        raw={},
    )
    defaults.update(kwargs)
    return CanonicalTx(**defaults)


class TestActionMapping:
    def test_trade_is_buy(self):
        tx = _tx(
            type=TxType.TRADE,
            received_asset="BTC", received_amount=Decimal("0.5"),
            sent_asset="JPY", sent_amount=Decimal("5000000"),
        )
        rows, skipped = to_cryptact_rows([tx])
        assert skipped == 0
        r = rows[0]
        assert r["Action"] == "BUY"
        assert r["Base"] == "BTC"
        assert r["Volume"] == "0.5"
        assert r["Counter"] == "JPY"
        # Price = 5000000 / 0.5 = 10000000
        assert Decimal(r["Price"]) == Decimal("10000000")
        assert r["Timestamp"] == "2024/03/15 10:30:00"

    def test_reward_bonus_default(self):
        tx = _tx(type=TxType.REWARD, label="campaign",
                 received_asset="NEXO", received_amount=Decimal("2"))
        rows, _ = to_cryptact_rows([tx])
        assert rows[0]["Action"] == "BONUS"

    def test_reward_staking(self):
        tx = _tx(type=TxType.REWARD, label="staking_reward",
                 received_asset="ETH", received_amount=Decimal("0.1"))
        rows, _ = to_cryptact_rows([tx])
        assert rows[0]["Action"] == "STAKING"

    def test_reward_lending(self):
        tx = _tx(type=TxType.REWARD, label="lending_interest",
                 received_asset="USDC", received_amount=Decimal("3"))
        rows, _ = to_cryptact_rows([tx])
        assert rows[0]["Action"] == "LENDING"

    def test_fee_is_sendfee(self):
        tx = _tx(type=TxType.FEE, fee_asset="ETH", fee_amount=Decimal("0.001"))
        rows, _ = to_cryptact_rows([tx])
        assert rows[0]["Action"] == "SENDFEE"
        assert rows[0]["Base"] == "ETH"
        assert rows[0]["Volume"] == "0.001"

    def test_deposit_withdraw_transfer_skipped(self):
        txs = [
            _tx(type=TxType.DEPOSIT, received_asset="BTC", received_amount=Decimal("1")),
            _tx(type=TxType.WITHDRAW, sent_asset="BTC", sent_amount=Decimal("1")),
            _tx(type=TxType.TRANSFER, sent_asset="USDC", sent_amount=Decimal("100")),
        ]
        rows, skipped = to_cryptact_rows(txs)
        assert rows == []
        assert skipped == 3


def test_csv_string_has_header():
    text, _ = to_cryptact_csv_string([])
    assert text.splitlines()[0] == "Timestamp,Action,Source,Base,Volume,Price,Counter,Fee,FeeCcy,Comment"


def test_write_cryptact_csv(tmp_path: Path):
    txs = [
        _tx(type=TxType.TRADE, received_asset="ETH", received_amount=Decimal("10"),
            sent_asset="JPY", sent_amount=Decimal("3000000")),
        _tx(type=TxType.DEPOSIT, received_asset="BTC", received_amount=Decimal("1")),
    ]
    out = tmp_path / "cryptact.csv"
    n = write_cryptact_csv(txs, out)
    assert n == 1  # DEPOSIT はスキップ
    with open(out, encoding="utf-8", newline="") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 1
    assert reader[0]["Action"] == "BUY"
