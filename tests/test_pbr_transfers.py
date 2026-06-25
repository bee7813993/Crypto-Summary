"""PbrTransfersCsvSource のテスト

重点検証:
- 2026-01-01 より前の行はスキップ（旧システム期間、日次レポートで記録済み）
- 2026-01-01～2026-03-02 の空白期間は記録される（日次レポートが存在しない）
- 2026-03-03 以降の新システム期間も記録される
- 入庫 → DEPOSIT、出庫 → WITHDRAW（abs値）
- システム移行・数量0 はスキップ
- 数量フィールドに桁区切りカンマがある場合 ("3,000") を正しく解析
- Shift_JIS / UTF-8(BOM) 両エンコーディングで読める
"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.sources.jp.pbr_transfers import PbrTransfersCsvSource
from crypto_summary.core.models import TxType

_HEADER = "日付,通貨種別,区分,数量,備考"
_SOURCE = "pbr_transfers"


def _write_cp932(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "transfers.csv"
    p.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="cp932")
    return p


def _load(tmp_path: Path, *rows: str):
    return PbrTransfersCsvSource(_SOURCE).load(_write_cp932(tmp_path, *rows))


# ---- スキップ条件 ----

def test_old_system_nyuko_skipped(tmp_path):
    """2026-01-01 より前の入庫は旧システム分としてスキップ（日次レポートで記録済み）。"""
    txs = _load(tmp_path, "2025-12-31,BTC,入庫,0.1,PBRLending 旧システム 貸出")
    assert txs == []


def test_old_system_syukko_skipped(tmp_path):
    """2026-01-01 より前の出庫もスキップ。"""
    txs = _load(tmp_path, "2025-12-30,BTC,出庫,-0.05,PBRLending 旧システム")
    assert txs == []


def test_system_migration_skipped(tmp_path):
    """システム移行はスキップ（日付によらず）。"""
    txs = _load(tmp_path, "2026-03-02,BTC,システム移行,0,PBRLending 貸出準備ウォレット履歴")
    assert txs == []


def test_zero_amount_skipped(tmp_path):
    """数量=0 の行はスキップ。"""
    txs = _load(tmp_path, "2026-04-01,BTC,入庫,0,備考")
    assert txs == []


# ---- 空白期間・新システム: 入庫 ----

def test_nyuko_after_cutoff_is_deposit(tmp_path):
    """2026-01-01 以降の入庫は DEPOSIT として記録される。"""
    txs = _load(tmp_path, "2026-03-31,BTC,入庫,0.1,PBRLending 貸出準備ウォレット履歴")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.1")
    assert tx.label == "pbr_deposit"


def test_cutoff_date_itself_is_recorded(tmp_path):
    """2026-01-01 当日の入庫は記録される（境界値確認）。"""
    txs = _load(tmp_path, "2026-01-01,USDC,入庫,1000,境界テスト")
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT


def test_gap_period_is_recorded(tmp_path):
    """日次レポートが存在しない空白期間 (2026-01-01～2026-03-02) の入庫は記録される。"""
    txs = _load(tmp_path, "2026-03-02,BTC,入庫,0.5,空白期間テスト")
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("0.5")


# ---- 新システム: 出庫 ----

def test_syukko_is_withdraw_with_abs_value(tmp_path):
    """出庫の負数量は WITHDRAW として絶対値で記録される。"""
    txs = _load(tmp_path, "2026-04-28,XRP,出庫,-50,PBRLending 貸出準備ウォレット履歴")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "XRP"
    assert tx.sent_amount == Decimal("50")
    assert tx.label == "pbr_withdrawal"


# ---- 桁区切りカンマ ----

def test_comma_formatted_amount(tmp_path):
    """数量が "3,000" のように桁区切りカンマを含む場合も正しく解析する。"""
    # CSV 内では: ...,入庫,3,000,備考 → 列が1つずれる
    p = tmp_path / "comma.csv"
    p.write_bytes(
        (_HEADER + "\n" + "2026-05-01,USDC,入庫,3,000,PBRLending\n").encode("cp932")
    )
    txs = PbrTransfersCsvSource(_SOURCE).load(p)
    assert len(txs) == 1
    assert txs[0].received_amount == Decimal("3000")


# ---- エンコーディング ----

@pytest.mark.parametrize("encoding", ["cp932", "utf-8-sig", "utf-8"])
def test_reads_multiple_encodings(tmp_path, encoding):
    """Shift_JIS / UTF-8(BOM付き・なし) いずれでも同じ結果になる。"""
    row = "2026-03-31,XRP,入庫,200,テスト"
    p = tmp_path / "enc.csv"
    p.write_text(_HEADER + "\n" + row + "\n", encoding=encoding)
    txs = PbrTransfersCsvSource(_SOURCE).load(p)
    assert len(txs) == 1
    assert txs[0].received_asset == "XRP"
    assert txs[0].received_amount == Decimal("200")


# ---- 複数行 ----

def test_multiple_rows(tmp_path):
    """複数の入庫・出庫が混在しても正しく処理される。"""
    txs = _load(
        tmp_path,
        "2025-09-29,BTC,入庫,0.1,旧システム",          # スキップ
        "2026-03-31,BTC,入庫,0.1,新システム",           # DEPOSIT
        "2026-03-31,USDC,入庫,13000,新システム",         # DEPOSIT
        "2026-04-28,XRP,出庫,-50,新システム",            # WITHDRAW
        "2026-03-02,ETH,システム移行,0,移行",            # スキップ
    )
    assert len(txs) == 3
    types = [t.type.value for t in txs]
    assert types.count("deposit") == 2
    assert types.count("withdraw") == 1
