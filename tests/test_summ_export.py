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
    link_nexo_transfers,
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


def test_internal_transfer_labels_are_skipped():
    """取引所内部サブウォレット間移動は Summ CSV に出力されないこと。"""
    internal_labels = [
        "term_deposit_lock",
        "term_deposit_unlock",
        "dual_investment_lock",
        "dual_investment_unlock",
    ]
    for label in internal_labels:
        tx = _tx(
            type=TxType.TRANSFER,
            label=label,
            sent_asset="USDT",
            sent_amount=Decimal("1000"),
        )
        assert to_summ_rows([tx]) == [], f"label={label!r} should be skipped"

    # 内部ラベル以外の TRANSFER（例: cross-exchange 送金）は通常通り出力される
    tx = _tx(
        type=TxType.TRANSFER,
        label="external_send",
        sent_asset="BTC",
        sent_amount=Decimal("0.1"),
    )
    rows = to_summ_rows([tx])
    assert len(rows) == 1
    assert rows[0]["Type"] == "send"


def test_link_nexo_transfers_unifies_ids():
    """nexo_savings ↔ nexo (Pro) 振替ペアの ID が nexo_savings 側に統一されること。"""
    ts_s = datetime(2026, 4, 30, 20, 4, 49, tzinfo=timezone.utc)
    ts_n = datetime(2026, 4, 30, 20, 4, 51, tzinfo=timezone.utc)

    savings_send = _tx(
        id="savings_id_aaa",
        source="nexo_savings",
        type=TxType.WITHDRAW,
        label="to_pro_wallet",
        timestamp=ts_s,
        sent_asset="SOL",
        sent_amount=Decimal("38.3999946"),
    )
    nexo_recv = _tx(
        id="nexo_pro_id_bbb",
        source="nexo",
        type=TxType.DEPOSIT,
        label=None,
        timestamp=ts_n,
        received_asset="SOL",
        received_amount=Decimal("38.3999946"),
    )

    linked = link_nexo_transfers([savings_send, nexo_recv])
    ids = [t.id for t in linked]

    # nexo Pro 側が nexo_savings 側の ID に統一される
    assert ids.count("savings_id_aaa") == 2
    assert "nexo_pro_id_bbb" not in ids

    # to_summ_rows でも両行が同一 ID を持つ
    rows = to_summ_rows([savings_send, nexo_recv])
    assert rows[0]["ID (Optional)"] == rows[1]["ID (Optional)"] == "savings_id_aaa"


def test_link_nexo_transfers_disambiguates_same_asset_by_timestamp():
    """同一資産・同一金額が複数ある場合にタイムスタンプで正しくペアを選ぶこと。"""
    t1s = datetime(2026, 1, 13, 0, 20, 46, tzinfo=timezone.utc)
    t1n = datetime(2026, 1, 13, 0, 20, 50, tzinfo=timezone.utc)
    t2s = datetime(2026, 1, 19, 3, 2, 2, tzinfo=timezone.utc)
    t2n = datetime(2026, 1, 19, 3, 2, 3, tzinfo=timezone.utc)

    s1 = _tx(id="s1", source="nexo_savings", type=TxType.WITHDRAW, label="to_pro_wallet",
             timestamp=t1s, sent_asset="NEXO", sent_amount=Decimal("150"))
    n1 = _tx(id="n1", source="nexo", type=TxType.DEPOSIT, timestamp=t1n,
             received_asset="NEXO", received_amount=Decimal("150"))
    s2 = _tx(id="s2", source="nexo_savings", type=TxType.WITHDRAW, label="to_pro_wallet",
             timestamp=t2s, sent_asset="NEXO", sent_amount=Decimal("150"))
    n2 = _tx(id="n2", source="nexo", type=TxType.DEPOSIT, timestamp=t2n,
             received_asset="NEXO", received_amount=Decimal("150"))

    linked = link_nexo_transfers([s1, n1, s2, n2])
    all_ids = [t.id for t in linked]

    # n1 → s1, n2 → s2 に統一され、old ID は残らない
    assert "n1" not in all_ids
    assert "n2" not in all_ids
    # s1, s2 それぞれに2件（savings側+nexo側）
    assert all_ids.count("s1") == 2
    assert all_ids.count("s2") == 2


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
