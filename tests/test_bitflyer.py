"""bitFlyer TradeHistory アダプタのテスト

特に「通貨1数量の符号で増減を判定する」ロジック（外部送付の返金など）を検証する。
"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.sources.jp.bitflyer import BitflyerTradeCsvSource
from crypto_summary.core.models import TxType

_HEADER = (
    '"取引日時","通貨","取引種別","取引価格","通貨1","通貨1数量","手数料",'
    '"通貨1の対円レート","通貨2","通貨2数量","手数料(JPY)","課税区分","自己・媒介","注文 ID","備考"'
)


def _write_csv(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "TradeHistory.csv"
    p.write_text("﻿" + _HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _load(tmp_path: Path, *rows: str):
    src = BitflyerTradeCsvSource("bitflyer")
    return src.load(_write_csv(tmp_path, *rows))


def test_buy_trade(tmp_path):
    txs = _load(tmp_path,
        '"2021/09/15 22:28:41","BTC/JPY","買い","410624","BTC","0.0486977","0",'
        '"410624","JPY","-20,000","0","","媒介","JOR1",""')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.0486977")
    assert tx.sent_asset == "JPY"
    assert tx.sent_amount == Decimal("20000")


def test_external_withdraw_negative_is_withdraw(tmp_path):
    txs = _load(tmp_path,
        '"2025/09/29 16:18:53","BTC","外部送付","0","BTC","-0.1","0",'
        '"0","","0","0","","","CWD1",""')
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "BTC"
    assert tx.sent_amount == Decimal("0.1")


def test_external_withdraw_positive_is_deposit(tmp_path):
    """正値の外部送付（キャンセル/返金）は残高が増えるので DEPOSIT 扱い。"""
    txs = _load(tmp_path,
        '"2025/09/29 16:13:40","BTC","外部送付","0","BTC","0.1","0",'
        '"0","","0","0","","","CWD2",""')
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.1")


def test_fee_negative_is_fee(tmp_path):
    txs = _load(tmp_path,
        '"2025/09/29 16:18:53","BTC","送付手数料","0","BTC","-0.0004","0",'
        '"0","","0","0","","","CWD1",""')
    tx = txs[0]
    assert tx.type == TxType.FEE
    assert tx.fee_asset == "BTC"
    assert tx.fee_amount == Decimal("0.0004")


def test_fee_positive_is_refund(tmp_path):
    """正値の送付手数料は返金なので増加扱い。"""
    txs = _load(tmp_path,
        '"2025/09/29 16:13:40","BTC","送付手数料","0","BTC","0.0004","0",'
        '"0","","0","0","","","CWD2",""')
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.0004")


def test_deposit_and_receive(tmp_path):
    txs = _load(tmp_path,
        '"2018/02/27 20:17:18","BTC","預入","0","BTC","0.0499","0",'
        '"0","","0","0","","","MDP1",""',
        '"2021/06/17 06:02:30","BTC","受取","0","BTC","0.00000116","0",'
        '"0","","0","0","","","RCV1",""')
    assert all(t.type == TxType.DEPOSIT for t in txs)
    assert txs[0].received_amount == Decimal("0.0499")


def test_collateral_transfer(tmp_path):
    """証拠金預入(負)=出, 引出(正)=入 の内部振替。"""
    txs = _load(tmp_path,
        '"2018/07/17 13:40:38","BTC","証拠金預入","0","BTC","-0.19069494","0",'
        '"0","","0","0","","","COL1",""',
        '"2024/02/22 12:59:52","BTC","証拠金引出","0","BTC","0.08820782","0",'
        '"0","","0","0","","","COL2",""')
    dep = next(t for t in txs if t.raw["注文 ID"] == "COL1")
    wd  = next(t for t in txs if t.raw["注文 ID"] == "COL2")
    assert dep.type == TxType.TRANSFER and dep.sent_asset == "BTC"
    assert wd.type == TxType.TRANSFER and wd.received_asset == "BTC"
