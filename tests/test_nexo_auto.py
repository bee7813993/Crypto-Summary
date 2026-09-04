"""NexoAutoCsvSource（Nexo 本体の自動判定）のテスト。

取引明細（貯蓄口座）と先物取引履歴を種別指定なしで取り込め、同じ口座へ
まとまることを検証する。別サービスの Nexo Pro は選び直しを案内して落とす。
"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.models import TxType
from crypto_summary.sources.nexo_auto import NexoAutoCsvSource

_SAVINGS = (
    "Transaction,Type,Input Currency,Input Amount,Output Currency,Output Amount,"
    "USD Equivalent,Fee,Fee Currency,Details,Date / Time (UTC)\n"
    "NXT1,Interest,NEXO,1.5,NEXO,1.5,$1.00,-,-,approved,2026-01-08 06:00:00\n"
    'NXT2,Advanced Trading Add Bonus Funds,USDT,500.00000000,-,0.00000000,$499.95,-,-,"completed",2026-08-28 16:58:20\n'
    'NXT3,Transfer To Advanced,USDT,-10000.00000000,-,0.00000000,$9999.10,-,-,"completed",2026-08-28 16:59:16\n'
)

# 実ファイル同様、先頭列は " Id"（先頭に半角スペース）
_FUTURES = (
    " Id,Timestamp,Symbol,Type,Position,Price,Amount,Status,Asset,Side,"
    "DealId,InternalTransactionType,TradingFeeAmount,TradingFeeAsset,"
    "ExecutionPriceAsset,ExchangeRate,SourceAsset,SourceAmount\n"
    "1,1788000000000,SOLUSDT,REALIZED_PNL,close,null,51.57,null,USDT,long,"
    "d1,TRANSACTION,null,USDT,USDT,null,null,null\n"
    "2,1788000100000,null,TRANSFER_TO_WALLET,null,null,10000,null,USDT,null,"
    "d2,TRANSACTION,null,USDT,,null,null,null\n"
)

_SPOT = (
    "id,timestamp,pair,side,type,price,executedPrice,triggerPrice,"
    "requestedAmount,filledAmount,tradingFee,feeCurrency,status,orderId\n"
    "abc,2026-01-05 11:20:45.843,BTC/USDT,buy,market,0,50000,0,"
    "0.01,0.01,0.1,USDT,filled,o1\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_savings(tmp_path):
    p = _write(tmp_path, "nexo_transactions.csv", _SAVINGS)
    src = NexoAutoCsvSource("Nexo")
    txs = src.load(p)
    assert {t.type for t in txs} == {TxType.REWARD}
    assert all(t.source == "Nexo" for t in txs)
    # 貯蓄アダプタのスキップ理由が自動判定経由でも見える
    assert src.skip_reasons == {"貯蓄⇔先物ウォレットの振替": 1}


def test_detect_futures(tmp_path):
    p = _write(tmp_path, "nexo_futures_transactions.csv", _FUTURES)
    src = NexoAutoCsvSource("Nexo")
    txs = src.load(p)
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].received_amount == Decimal("51.57")
    assert txs[0].label == "futures_realized_profit"
    assert src.skip_reasons == {"貯蓄⇔先物ウォレットの振替": 1}


def test_both_files_land_in_one_account(tmp_path):
    """貯蓄と先物を同じ source_id で取り込むと1口座にまとまる。"""
    all_txs = []
    for name, content in [("s.csv", _SAVINGS), ("f.csv", _FUTURES)]:
        all_txs.extend(NexoAutoCsvSource("Nexo").load(_write(tmp_path, name, content)))
    assert {t.source for t in all_txs} == {"Nexo"}
    assert len(all_txs) == 3  # Interest + ボーナス + 先物実現損益


def test_pro_file_says_which_exchange_to_pick(tmp_path):
    p = _write(tmp_path, "SpotHistory.csv", _SPOT)
    with pytest.raises(ValueError, match="「Nexo Pro」を選択してください"):
        NexoAutoCsvSource("Nexo").load(p)


def test_unknown_header_raises(tmp_path):
    p = _write(tmp_path, "x.csv", "foo,bar,baz\n1,2,3\n")
    with pytest.raises(ValueError, match="判別できませんでした"):
        NexoAutoCsvSource("Nexo").load(p)


def test_empty_file_returns_empty(tmp_path):
    p = _write(tmp_path, "empty.csv", "")
    assert NexoAutoCsvSource("Nexo").load(p) == []


def test_registered_in_registry():
    from crypto_summary.sources.csv_import import EXCHANGE_SOURCES
    assert EXCHANGE_SOURCES["nexo_auto"] is NexoAutoCsvSource
