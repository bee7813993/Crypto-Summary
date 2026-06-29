"""Tests for SUMM custom CSV export sink.

仕様: https://help.summ.com/en/articles/5777675-custom-csv-import
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sinks.summ_csv import (
    _SUMM_HEADERS,
    to_summ_csv_string,
    to_summ_rows,
    write_summ_csv,
)


def _tx(**kwargs) -> CanonicalTx:
    defaults = dict(
        id="abc123",
        source="bitflyer",
        timestamp=datetime(2024, 3, 2, 10, 37, 29, tzinfo=timezone.utc),
        type=TxType.TRADE,
        raw={},
    )
    defaults.update(kwargs)
    return CanonicalTx(**defaults)


def test_header_is_14_columns_in_order():
    text = to_summ_csv_string([])
    header = text.splitlines()[0]
    assert header == (
        "Timestamp (UTC),Type,Base Currency,Base Amount,"
        "Quote Currency (Optional),Quote Amount (Optional),"
        "Fee Currency (Optional),Fee Amount (Optional),"
        "From (Optional),To (Optional),Blockchain (Optional),ID (Optional),"
        "Reference Price Per Unit (Optional),Reference Price Currency (Optional)"
    )
    assert len(_SUMM_HEADERS) == 14


def test_trade_buy_matches_spec_example():
    # 公式例: 2024-03-02 10:37:29 | buy | ETH | 1 | USDC | 3000 | USDC | 1
    tx = _tx(
        type=TxType.TRADE,
        received_asset="ETH", received_amount=Decimal("1"),
        sent_asset="USDC", sent_amount=Decimal("3000"),
        fee_asset="USDC", fee_amount=Decimal("1"),
    )
    r = to_summ_rows([tx])[0]
    assert r["Timestamp (UTC)"] == "2024-03-02 10:37:29"
    assert r["Type"] == "buy"
    assert r["Base Currency"] == "ETH"
    assert r["Base Amount"] == "1"
    assert r["Quote Currency (Optional)"] == "USDC"
    assert r["Quote Amount (Optional)"] == "3000"
    assert r["Fee Currency (Optional)"] == "USDC"
    assert r["Fee Amount (Optional)"] == "1"
    assert r["ID (Optional)"] == "abc123"


def test_send_transfer_matches_spec_example():
    # 公式例: 2024-03-02 11:33:17 | send | ETH | 1 | | | ETH | 0.001
    tx = _tx(
        type=TxType.WITHDRAW,
        timestamp=datetime(2024, 3, 2, 11, 33, 17, tzinfo=timezone.utc),
        sent_asset="ETH", sent_amount=Decimal("1"),
        fee_asset="ETH", fee_amount=Decimal("0.001"),
    )
    r = to_summ_rows([tx])[0]
    assert r["Type"] == "send"
    assert r["Base Currency"] == "ETH"
    assert r["Base Amount"] == "1"
    assert r["Quote Currency (Optional)"] == ""
    assert r["Fee Currency (Optional)"] == "ETH"
    assert r["Fee Amount (Optional)"] == "0.001"


def test_reward_types():
    staking = _tx(type=TxType.REWARD, label="staking_reward",
                  received_asset="ETH", received_amount=Decimal("0.1"))
    interest = _tx(type=TxType.REWARD, label="lending_interest",
                   received_asset="USDC", received_amount=Decimal("5"))
    income = _tx(type=TxType.REWARD, label="campaign",
                 received_asset="NEXO", received_amount=Decimal("2"))
    assert to_summ_rows([staking])[0]["Type"] == "staking"
    assert to_summ_rows([interest])[0]["Type"] == "interest"
    assert to_summ_rows([income])[0]["Type"] == "income"


def test_fiat_deposit_and_crypto_receive():
    fiat = _tx(type=TxType.DEPOSIT, received_asset="JPY", received_amount=Decimal("100000"))
    crypto = _tx(type=TxType.DEPOSIT, received_asset="BTC", received_amount=Decimal("0.5"))
    assert to_summ_rows([fiat])[0]["Type"] == "fiat-deposit"
    assert to_summ_rows([crypto])[0]["Type"] == "receive"


def test_fee_row():
    tx = _tx(type=TxType.FEE, fee_asset="ETH", fee_amount=Decimal("0.002"))
    r = to_summ_rows([tx])[0]
    assert r["Type"] == "fee"
    assert r["Base Currency"] == "ETH"
    assert r["Base Amount"] == "0.002"
    # FEE 行では手数料を base に出すので Fee 列は空
    assert r["Fee Currency (Optional)"] == ""


def test_write_summ_csv(tmp_path: Path):
    txs = [
        _tx(type=TxType.TRADE, received_asset="ETH", received_amount=Decimal("1"),
            sent_asset="USDC", sent_amount=Decimal("3000")),
        _tx(type=TxType.DEPOSIT, received_asset="BTC", received_amount=Decimal("0.5")),
    ]
    out = tmp_path / "summ.csv"
    n = write_summ_csv(txs, out)
    assert n == 2
    with open(out, encoding="utf-8", newline="") as f:
        reader = list(csv.DictReader(f))
    assert len(reader) == 2
    assert reader[0]["Type"] == "buy"
    assert reader[1]["Type"] == "receive"
