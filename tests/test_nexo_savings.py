"""NexoSavingsCsvSource のテスト

重点検証:
- Exchange Liquidation と Exchange Credit がスキップされること
- Fee が sent_amount に内包されているため二重控除されないこと
- Dual Investment Exchange が TRADE として正しく処理されること
"""
from decimal import Decimal
from pathlib import Path
import csv, io

import pytest

from crypto_summary.sources.nexo_savings import NexoSavingsCsvSource
from crypto_summary.core.models import TxType

_HEADER = "Transaction,Type,Input Currency,Input Amount,Output Currency,Output Amount,USD Equivalent,Fee,Fee Currency,Details,Date / Time (UTC)"


def _write_csv(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "nexo_transactions.csv"
    p.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _load(tmp_path: Path, *rows: str):
    src = NexoSavingsCsvSource("nexo_savings")
    return src.load(_write_csv(tmp_path, *rows))


def test_exchange_liquidation_is_skipped(tmp_path):
    """Exchange Liquidation は Transfer Out/In で計上済みなのでスキップ。"""
    txs = _load(tmp_path,
        'NXT001,Exchange Liquidation,USDT,822.79095800,xUSD,818.14000000,822.79,0,,approved / Crypto repayment,2026-01-02 18:34:22')
    assert txs == []


def test_exchange_credit_is_skipped(tmp_path):
    """Exchange Credit は Top up Crypto と重複するためスキップ。"""
    txs = _load(tmp_path,
        'NXT002,Exchange Credit,xUSD,-799.16000000,USDT,800.00000000,800.00,0,,approved / Exchange xUSD to USDT,2026-01-02 14:25:13')
    assert txs == []


def test_top_up_crypto_deposit(tmp_path):
    """Top up Crypto はローン実行・オンチェーン入金ともに DEPOSIT として処理。"""
    txs = _load(tmp_path,
        'NXT003,Top up Crypto,USDT,800.00000000,USDT,800.00000000,800.00,0,,approved / Credit Granting Top Up,2026-01-02 14:25:14')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("800")


def test_exchange_trade_fee_not_double_deducted(tmp_path):
    """Fee は Input Amount に内包されているため CanonicalTx に fee_amount を設定しない。"""
    txs = _load(tmp_path,
        'NXT004,Exchange,USDT,-100.00000000,BTC,0.00111721,100.00,1.98995500,USDT,approved / Exchange Tether to Bitcoin,2025-12-01 07:00:47')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "USDT"
    assert tx.sent_amount == Decimal("100")
    # fee_amount は None (Input Amount 内包)
    assert tx.fee_amount is None


def test_dual_investment_exchange_is_trade(tmp_path):
    """Dual Investment Exchange は USDT 送出・BTC 受取の TRADE。"""
    txs = _load(tmp_path,
        'NXT005,Dual Investment Exchange,USDT,1000.00000000,BTC,0.01536302,1000.00,0,,approved / Exchange USDT to BTC,2026-06-04 08:00:23')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "USDT"
    assert tx.sent_amount == Decimal("1000")
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.01536302")


def test_transfer_from_pro_wallet_deposit(tmp_path):
    """Transfer From Pro Wallet は DEPOSIT。"""
    txs = _load(tmp_path,
        'NXT006,Transfer From Pro Wallet,USDT,1146.75583400,USDT,1146.75583440,1146.76,0,,approved / USDT Transfer from Nexo Pro Wallet to Savings Wallet,2026-05-08 02:56:39')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("1146.75583400")


def test_dual_investment_interest_reward(tmp_path):
    """Dual Investment Interest は REWARD。"""
    txs = _load(tmp_path,
        'NXT007,Dual Investment Interest,USDT,1.14529000,USDT,1.14529000,1.15,0,,completed / USDT Interest Earned,2026-06-13 08:00:22')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("1.14529")


def test_manual_sell_order_skipped(tmp_path):
    """Manual Sell Order は Exchange Liquidation の重複なのでスキップ。"""
    txs = _load(tmp_path,
        'NXT008,Manual Sell Order,USDT,-822.79095800,USDT,0.00000000,822.79,0,,approved / Crypto Repayment,2026-01-02 18:34:22')
    assert txs == []
