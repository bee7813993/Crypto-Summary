"""NexoProCsvSource の自動判別テスト。

SpotHistory / DnWHistory / 取引明細(Savings) の各CSVを種別指定なしで
取り込め、同一 source_id へまとまることを検証する。
"""
from pathlib import Path

import pytest

from crypto_summary.core.models import TxType
from crypto_summary.sources.nexo import NexoProCsvSource

_SPOT = (
    "id,timestamp,pair,side,type,price,executedPrice,triggerPrice,"
    "requestedAmount,filledAmount,tradingFee,feeCurrency,status,orderId\n"
    "abc,2026-01-05 11:20:45.843,BTC/USDT,buy,market,0,50000,0,"
    "0.01,0.01,0.1,USDT,filled,o1\n"
)

_DNW = (
    "timestamp,amount,asset,side\n"
    "2026-01-06 09:00:00,1.5,ETH,DEPOSIT\n"
    "2026-01-07 09:00:00,0.5,ETH,WITHDRAW\n"
)

_SAVINGS = (
    "Transaction,Type,Input Currency,Input Amount,Output Currency,Output Amount,"
    "USD Equivalent,Fee,Fee Currency,Details,Date / Time (UTC)\n"
    "tx1,Interest,NEXO,1.5,,,$1.00,,,approved,2026-01-08 06:00:00\n"
)

_FUTURES = (
    "Id,Timestamp,Symbol,Type,Position,Price,Amount,Status,Asset,Side,"
    "DealId,InternalTransactionType,TradingFeeAmount,TradingFeeAsset,"
    "ExecutionPriceAsset,ExchangeRate,SourceAsset,SourceAmount\n"
    "1,1788000000000,SOLUSDT,REALIZED_PNL,close,null,51.57,null,USDT,long,"
    "d1,TRANSACTION,null,USDT,USDT,null,null,null\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_spot(tmp_path):
    p = _write(tmp_path, "SpotHistory.csv", _SPOT)
    txs = NexoProCsvSource("Nexo Pro").load(p)
    assert len(txs) == 1
    assert txs[0].type == TxType.TRADE
    assert txs[0].source == "Nexo Pro"


def test_detect_dnw(tmp_path):
    p = _write(tmp_path, "DnWHistory.csv", _DNW)
    txs = NexoProCsvSource("Nexo Pro").load(p)
    assert {t.type for t in txs} == {TxType.DEPOSIT, TxType.WITHDRAW}
    assert all(t.source == "Nexo Pro" for t in txs)


def test_futures_raises_with_guidance(tmp_path):
    """先物CSVは Nexo 本体側のため Pro では取り込まず、選び直しを案内する。"""
    p = _write(tmp_path, "nexo_futures_transactions.csv", _FUTURES)
    with pytest.raises(ValueError, match="「Nexo（先物取引）」を選択してください"):
        NexoProCsvSource("Nexo Pro").load(p)


def test_detect_savings_raises(tmp_path):
    """貯蓄口座CSVをNexo Proアダプタで読み込むとエラーになる。"""
    p = _write(tmp_path, "nexo_transactions.csv", _SAVINGS)
    with pytest.raises(ValueError, match="判別できませんでした"):
        NexoProCsvSource("Nexo Pro").load(p)


def test_spot_adapter_rejects_other_format(tmp_path):
    """形式を明示指定したときも、別形式なら不足列が分かるエラーになる。"""
    from crypto_summary.sources.nexo import NexoDnWCsvSource, NexoSpotCsvSource

    p = _write(tmp_path, "DnWHistory.csv", _DNW)
    with pytest.raises(ValueError, match="必要な列が見つかりません"):
        NexoSpotCsvSource("Nexo Pro").load(p)

    # 以前は生の KeyError('side') になっていた
    p2 = _write(tmp_path, "nexo_futures_transactions.csv", _FUTURES)
    with pytest.raises(ValueError, match="必要な列が見つかりません"):
        NexoDnWCsvSource("Nexo Pro").load(p2)


def test_all_share_same_source_id(tmp_path):
    """SpotとDnWを同じ source_id で取り込むと1口座にまとまる。"""
    paths = [
        _write(tmp_path, "s.csv", _SPOT),
        _write(tmp_path, "d.csv", _DNW),
    ]
    all_txs = []
    for p in paths:
        all_txs.extend(NexoProCsvSource("Nexo Pro").load(p))
    assert {t.source for t in all_txs} == {"Nexo Pro"}
    assert len(all_txs) == 3


def test_unknown_header_raises(tmp_path):
    p = _write(tmp_path, "x.csv", "foo,bar,baz\n1,2,3\n")
    with pytest.raises(ValueError, match="判別できませんでした"):
        NexoProCsvSource("Nexo Pro").load(p)


def test_empty_file_returns_empty(tmp_path):
    p = _write(tmp_path, "empty.csv", "")
    assert NexoProCsvSource("Nexo Pro").load(p) == []


def test_registered_in_registry():
    from crypto_summary.sources.csv_import import EXCHANGE_SOURCES
    assert EXCHANGE_SOURCES["nexo"] is NexoProCsvSource
