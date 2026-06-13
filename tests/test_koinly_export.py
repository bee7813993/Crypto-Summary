"""Tests for Koinly CSV export sink."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sinks.koinly_csv import to_koinly_rows, write_koinly_csv


def _tx(**kwargs) -> CanonicalTx:
    defaults = dict(
        id="test_id",
        source="test",
        timestamp=datetime(2024, 3, 15, 10, 30, 0, tzinfo=timezone.utc),
        type=TxType.TRADE,
        raw={},
    )
    defaults.update(kwargs)
    return CanonicalTx(**defaults)


class TestLabelMapping:
    def test_trade_no_label(self):
        tx = _tx(type=TxType.TRADE)
        rows = to_koinly_rows([tx])
        assert rows[0]["Label"] == ""

    def test_reward_interest(self):
        tx = _tx(type=TxType.REWARD, label="lending_interest",
                 received_asset="BTC", received_amount=Decimal("0.001"))
        rows = to_koinly_rows([tx])
        assert rows[0]["Label"] == "reward"

    def test_reward_staking(self):
        tx = _tx(type=TxType.REWARD, label="staking_reward",
                 received_asset="ETH", received_amount=Decimal("0.1"))
        rows = to_koinly_rows([tx])
        assert rows[0]["Label"] == "staking"

    def test_transfer_ignored(self):
        tx = _tx(type=TxType.TRANSFER, label="term_deposit_lock",
                 sent_asset="USDC", sent_amount=Decimal("1000"))
        rows = to_koinly_rows([tx])
        assert rows[0]["Label"] == "ignored"

    def test_fx_realized_loss_cost(self):
        tx = _tx(type=TxType.FEE, label="fx_realized_loss",
                 fee_asset="JPY", fee_amount=Decimal("5000"))
        rows = to_koinly_rows([tx])
        assert rows[0]["Label"] == "cost"


class TestRowFields:
    def test_trade_fields(self):
        tx = _tx(
            type=TxType.TRADE,
            received_asset="BTC", received_amount=Decimal("0.00238095"),
            sent_asset="USDT", sent_amount=Decimal("100"),
            fee_asset="BNB", fee_amount=Decimal("0.001"),
        )
        rows = to_koinly_rows([tx])
        r = rows[0]
        assert r["Date"] == "2024-03-15 10:30:00 UTC"
        assert r["Received Amount"] == "0.00238095"
        assert r["Received Currency"] == "BTC"
        assert r["Sent Amount"] == "100"
        assert r["Sent Currency"] == "USDT"
        assert r["Fee Amount"] == "0.001"
        assert r["Fee Currency"] == "BNB"

    def test_deposit_no_sent(self):
        tx = _tx(type=TxType.DEPOSIT,
                 received_asset="ETH", received_amount=Decimal("1.5"))
        rows = to_koinly_rows([tx])
        r = rows[0]
        assert r["Received Amount"] == "1.5"
        assert r["Sent Amount"] == ""
        assert r["Sent Currency"] == ""

    def test_tx_hash_included(self):
        tx = _tx(tx_hash="0xabc123")
        rows = to_koinly_rows([tx])
        assert rows[0]["TxHash"] == "0xabc123"

    def test_description_from_label(self):
        tx = _tx(type=TxType.REWARD, label="fixed_term_interest",
                 received_asset="NEXO", received_amount=Decimal("2"))
        rows = to_koinly_rows([tx])
        assert rows[0]["Description"] == "fixed_term_interest"


class TestWriteCsv:
    def test_write_creates_file(self, tmp_path: Path):
        txs = [
            _tx(id="t1", type=TxType.DEPOSIT,
                received_asset="BTC", received_amount=Decimal("1")),
            _tx(id="t2", type=TxType.TRADE,
                received_asset="ETH", received_amount=Decimal("10"),
                sent_asset="BTC", sent_amount=Decimal("0.5")),
        ]
        out = tmp_path / "koinly.csv"
        n = write_koinly_csv(txs, out)
        assert n == 2
        assert out.exists()

        with open(out, encoding="utf-8", newline="") as f:
            reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert reader[0]["Received Currency"] == "BTC"
        assert reader[1]["Sent Currency"] == "BTC"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        out = tmp_path / "subdir" / "nested" / "koinly.csv"
        write_koinly_csv([_tx()], out)
        assert out.exists()
