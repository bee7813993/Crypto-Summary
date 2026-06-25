"""PbrLendingCsvSource のテスト

重点検証:
- 旧システム（～2026-03-02）: 貸出開始 が DEPOSIT、返還 が WITHDRAW
- 新システム（2026-03-03～）: 貸出数量/返還数量は貸出準備ウォレットとの内部移動の
  ため計上しない（入出金履歴と二重計上になる）。利確 REWARD は引き続き記録。
- 予定利息(未受取)はスキップし、利確数量のみ REWARD として計上すること
- cp932 / UTF-8(BOM) いずれのエンコーディングでも読めること
"""
from decimal import Decimal
from pathlib import Path

import pytest

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


def test_wrong_format_transfers_rejected(tmp_path):
    """入出金履歴形式（「貸出数量」「返還数量」列なし）を渡すと明示エラーになる。"""
    p = tmp_path / "transfers.csv"
    p.write_text(
        "日付,通貨種別,区分,数量,備考\n2026-03-31,BTC,入庫,0.1,入出金履歴\n",
        encoding="cp932",
    )
    with pytest.raises(ValueError, match="貸出日次レポートの形式ではありません"):
        PbrLendingCsvSource("pbr_lending").load(p)


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


@pytest.mark.parametrize("encoding", ["cp932", "utf-8-sig", "utf-8"])
def test_reads_multiple_encodings(tmp_path, encoding):
    """Shift_JIS / UTF-8(BOM付き・なし) のいずれでも同じ結果になる。"""
    row = "2025-09-30,BTC,0.1000000000,0,0,0,0,0,0,0,0,0,0,0,0,16877648.58,"
    p = tmp_path / "pbr.csv"
    p.write_text(_HEADER + "\n" + row + "\n", encoding=encoding)
    txs = PbrLendingCsvSource("pbr_lending").load(p)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_asset == "BTC"
    assert txs[0].received_amount == Decimal("0.1000000000")


# ---- 新システム（2026-03-03～）: 貸出数量/返還数量は内部移動でスキップ ----

def test_new_system_lending_not_deposit(tmp_path):
    """2026-03-03 以降の貸出数量は内部移動のため DEPOSIT を生成しない。

    入金は入出金履歴 (pbr_transfers) の入庫で計上されるため、ここで計上すると
    二重計上になる。
    """
    txs = _load(tmp_path,
        "2026-03-31,BTC,0.1000000000,0,0,0,0,0,0,0,0,0,0,0,0,16877648.58,")
    assert txs == []


def test_new_system_return_not_withdraw(tmp_path):
    """2026-03-03 以降の返還数量は内部移動のため WITHDRAW を生成しない。"""
    txs = _load(tmp_path,
        "2026-04-15,BTC,0,0,0,0.1018889500,0,0,0,0,0,0,0,0,0,14000000,")
    assert txs == []


def test_new_system_reward_still_recorded(tmp_path):
    """新システムでも利確 (REWARD) は引き続き記録される。"""
    txs = _load(tmp_path,
        "2026-04-20,BTC,0,0.0000274,0.0009590000,0,0,0,0.004,0.0009590000,0,0,0,0.1,0.0009590000,15597040.51,")
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].label == "premium_migration_interest"


def test_cutoff_boundary_2026_03_02_still_counts(tmp_path):
    """境界値: 2026-03-02 の貸出数量は旧システムとして DEPOSIT 計上される。"""
    txs = _load(tmp_path,
        "2026-03-02,BTC,0.1000000000,0,0,0,0,0,0,0,0,0,0,0,0,16877648.58,")
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT


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
