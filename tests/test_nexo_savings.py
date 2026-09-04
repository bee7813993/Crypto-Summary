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


def test_loan_withdrawal_xusd_skipped(tmp_path):
    """Loan Withdrawal は xUSD 建て (内部債務単位) なのでスキップ。実入金は Top up Crypto。"""
    txs = _load(tmp_path,
        'NXT009,Loan Withdrawal,xUSD,-799.16000000,xUSD,799.16000000,799.16,0,,approved / USDT Loan Withdrawal,2026-01-02 14:25:13')
    assert txs == []


def test_nexo_card_purchase_skipped(tmp_path):
    """Nexo Card Purchase は xUSD/USD 建て (担保への借入) なのでスキップ。"""
    txs = _load(tmp_path,
        'NXT010,Nexo Card Purchase,USD,-3.65000000,EUR,3.11000000,3.65,0,,approved / MCDONALDS,2026-01-02 00:53:34')
    assert txs == []


def test_internal_unit_interest_skipped(tmp_path):
    """xUSD 建てのローン利息は内部単位なのでスキップ (通常の実資産 Interest は計上)。"""
    txs = _load(tmp_path,
        'NXT011,Interest,xUSD,-0.01000000,xUSD,0.01000000,0.01,0,,approved / Interest,2026-01-19 00:00:00')
    assert txs == []


def test_real_asset_interest_still_recorded(tmp_path):
    """実資産 (NEXO等) の Interest は引き続き REWARD として計上される。"""
    txs = _load(tmp_path,
        'NXT012,Interest,NEXO,0.80193140,NEXO,0.80193140,0.72,0,,approved / NEXO Interest Earned,2026-01-02 06:00:00')
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].received_asset == "NEXO"


def test_advanced_trading_bonus_is_reward(tmp_path):
    """先物キャンペーンボーナスの付与は REWARD (所得) として計上する。"""
    txs = _load(tmp_path,
        'NXT013,Advanced Trading Add Bonus Funds,USDT,500.00000000,-,0.00000000,$499.95,-,-,"completed",2026-08-28 16:58:20')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("500")
    assert tx.label == "advanced_trading_bonus"


def test_exchange_sell_fee_recorded(tmp_path):
    """別行で課金される Advanced spot trading 手数料は FEE として計上する。"""
    txs = _load(tmp_path,
        'NXT014,Exchange Sell Fee,NEXO,-11.11058484,-,0.00000000,$9.35,-,-,"approved / Advanced spot trading fee",2026-08-29 23:56:10')
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.FEE
    assert tx.fee_asset == "NEXO"
    assert tx.fee_amount == Decimal("11.11058484")
    assert tx.label == "exchange_fee"


def test_futures_transfers_skipped_with_reason(tmp_path):
    """貯蓄⇔先物の振替とボーナスウォレットの内部移動は理由付きスキップ。"""
    src = NexoSavingsCsvSource("nexo_savings")
    txs = src.load(_write_csv(tmp_path,
        'NXT015,Transfer To Advanced,USDT,-10000.00000000,-,0.00000000,$9999.10,-,-,"completed / USDT Transfer from Savings Wallet to Futures Wallet",2026-08-28 16:59:16',
        'NXT016,Transfer From Advanced,USDT,10471.56974700,-,0.00000000,$10471.22,-,-,"completed / USDT Transfer from Futures Wallet to Savings Wallet",2026-08-29 23:52:54',
        'NXT017,Transfer To Advanced Trading Bonus,USDT,-51.57000000,-,0.00000000,$51.57,-,-,"completed",2026-08-28 17:13:07',
        'NXT018,Transfer From Advanced Trading Bonus,USDT,963.11461500,-,0.00000000,$963.07,-,-,"completed",2026-08-29 23:20:48',
    ))
    assert txs == []
    assert src.skip_reasons == {
        "貯蓄⇔先物ウォレットの振替": 2,
        "先物⇔ボーナスウォレットの内部移動": 2,
    }


def test_unknown_type_counted(tmp_path):
    """未知タイプは黙って落とさず件数を計上する (課税イベントの見逃し防止)。"""
    src = NexoSavingsCsvSource("nexo_savings")
    txs = src.load(_write_csv(tmp_path,
        'NXT019,Mystery New Type,USDT,1.00000000,-,0.00000000,$1.00,-,-,"completed",2026-08-28 00:00:00'))
    assert txs == []
    assert src.skip_reasons == {"未知タイプ: Mystery New Type": 1}


def test_futures_file_says_which_exchange_to_pick(tmp_path):
    """先物CSVを貯蓄口座で読んだら、選び直すべき取引所を案内して落とす。

    実際に起きた取り違え: 全24行が「未知タイプ」で落ちるだけで、
    「取引が見つかりませんでした（24件スキップ）」としか表示されなかった。
    """
    p = tmp_path / "nexo_futures_transactions.csv"
    p.write_text(
        " Id,Timestamp,Symbol,Type,Position,Price,Amount,Status,Asset,Side,"
        "DealId,InternalTransactionType,TradingFeeAmount,TradingFeeAsset,"
        "ExecutionPriceAsset,ExchangeRate,SourceAsset,SourceAmount\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="「Nexo（先物取引）」を選択してください"):
        NexoSavingsCsvSource("nexo_savings").load(p)


def test_pro_file_says_which_exchange_to_pick(tmp_path):
    p = tmp_path / "DnWHistory.csv"
    p.write_text("timestamp,amount,asset,side\n", encoding="utf-8")
    with pytest.raises(ValueError, match="「Nexo Pro」を選択してください"):
        NexoSavingsCsvSource("nexo_savings").load(p)


def test_unrelated_file_names_missing_columns(tmp_path):
    p = tmp_path / "whatever.csv"
    p.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必要な列が見つかりません"):
        NexoSavingsCsvSource("nexo_savings").load(p)
