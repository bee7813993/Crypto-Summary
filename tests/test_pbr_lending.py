"""PbrLendingCsvSource のテスト

重点検証:
- 貸出開始 が DEPOSIT として残高に積み上がること (TRANSFER送出ではない)
- 返還 が WITHDRAW として残高から引かれること
- 予定利息(未受取)はスキップし、利確数量のみ REWARD として計上すること
- cp932 エンコーディングで読めること
"""
from decimal import Decimal
from pathlib import Path

from crypto_summary.sources.jp.pbr_lending import PbrLendingCsvSource
from crypto_summary.core.models import TxType

_HEADER = (
    "日付,通貨種別,貸出数量,総単日受取予定利息,総累計受取予定利息,"
    "返還数量,返還受取利息（利確数量）,手数料（送金・解約）,"
    "プレミアム移行数量,プレミアム移行受取利息（利確数量）,"
    "プレミアム満期数量,プレミアム満期受取利息（利確数量）,"
    "運営からの付与数量（利確数量）,総貸出元本残高,総受取数量,ご参考レート,備考"
)


def _write_csv(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "pbr.csv"
    p.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="cp932")
    return p


def _load(tmp_path: Path, *rows: str):
    src = PbrLendingCsvSource("pbr_lending")
    return src.load(_write_csv(tmp_path, *rows))


def test_lending_start_is_deposit(tmp_path):
    """貸出数量 >0 は DEPOSIT。残高が増える。"""
    txs = _load(tmp_path,
        "2025-09-30,BTC,0.1000000000,0,0,0,0,0,0,0,0,0,0,0,0,16877648.58,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.1000000000")
    assert tx.label == "lending_start"


def test_planned_interest_skipped(tmp_path):
    """予定利息(未受取)の行は何も生成しない。"""
    txs = _load(tmp_path,
        "2025-10-01,BTC,0,0.0000274000,0.0000274000,0,0,0,0,0,0,0,0,0.1,0,17450607.57,")
    assert txs == []


def test_realized_interest_is_reward(tmp_path):
    """利確数量 >0 (プレミアム移行受取利息) は REWARD。"""
    txs = _load(tmp_path,
        "2025-11-04,BTC,0,0.0000274000,0.0009590000,0,0,0,0.0040000000,0.0009590000,0,0,0,0.1,0.0009590000,15597040.51,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.0009590000")
    assert tx.label == "premium_migration_interest"


def test_return_is_withdraw(tmp_path):
    """返還数量 >0 は WITHDRAW。残高が減る。"""
    txs = _load(tmp_path,
        "2026-01-15,BTC,0,0,0,0.1018889500,0,0,0,0,0,0,0,0,0,14000000,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "BTC"
    assert tx.sent_amount == Decimal("0.1018889500")
    assert tx.label == "lending_return"


def test_balance_principal_plus_interest(tmp_path):
    """貸出開始 + 利確 で残高が総貸出元本残高に一致する。"""
    txs = _load(tmp_path,
        "2025-09-30,BTC,0.1000000000,0,0,0,0,0,0,0,0,0,0,0,0,16877648.58,",
        "2025-11-04,BTC,0,0.0000274,0.0009590000,0,0,0,0.004,0.0009590000,0,0,0,0.1,0.0009590000,15597040.51,",
        "2025-12-09,BTC,0,0.0000278,0.0009299500,0,0,0,0.004,0.0009299500,0,0,0,0.1009590000,0.0009299500,14537141.73,")
    balance = Decimal("0")
    for tx in txs:
        if tx.received_amount:
            balance += tx.received_amount
        if tx.sent_amount:
            balance -= tx.sent_amount
    assert balance == Decimal("0.1018889500")
