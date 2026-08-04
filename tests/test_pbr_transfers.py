"""PbrTransfersCsvSource のテスト

重点検証:
- 入庫 → DEPOSIT、出庫 → WITHDRAW（abs値）。日付による分岐は無い
- 利息 / 返還利息 / プレミアム満期 → REWARD（3区分は排他的な内訳で重複しない）
- 貸出 / 返還 は内部移動としてスキップし件数を計上する
- システム移行・数量0 はスキップ（件数には数えない）
- 未知の区分はスキップしつつ件数を計上する（無言で落とさない）
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


# ---- フォーマット検証 ----

def test_wrong_format_daily_report_rejected(tmp_path):
    """日次レポート形式（「区分」「数量」列なし）を渡すと明示エラーになる。"""
    p = tmp_path / "daily.csv"
    p.write_text(
        "日付,通貨種別,貸出数量,返還数量,備考\n2026-03-31,BTC,0.1,0,日次レポート\n",
        encoding="cp932",
    )
    with pytest.raises(ValueError, match="入出金履歴の形式ではありません"):
        PbrTransfersCsvSource(_SOURCE).load(p)


# ---- 旧システム期間も記録する ----

def test_old_system_nyuko_is_deposit(tmp_path):
    """旧システム期間の入庫も DEPOSIT として記録される。"""
    txs = _load(tmp_path, "2025-12-31,BTC,入庫,0.1,PBRLending 旧システム 貸出")
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("0.1")
    assert txs[0].label == "pbr_deposit"


def test_old_system_syukko_is_withdraw(tmp_path):
    """旧システム期間の出庫も WITHDRAW として記録される。"""
    txs = _load(tmp_path, "2025-12-30,BTC,出庫,-0.05,PBRLending 旧システム")
    assert len(txs) == 1
    assert txs[0].type == TxType.WITHDRAW
    assert txs[0].sent_amount == Decimal("0.05")


def test_2025_12_30_deposit_recorded(tmp_path):
    """回帰: 2025-12-30 の入庫が台帳から完全に欠落していた。

    「旧システムは入庫＝即貸出開始なので日次レポート側が記録済み」という前提で
    2026-01-01 より前を一律スキップしていたが、この入庫が実際に貸出になったのは
    2026-01-03 で、その期間の日次レポートは存在しない。結果どちらのCSVからも
    拾われなかった。
    """
    txs = _load(tmp_path,
        "2025-12-30,BTC,入庫,0.0499302699999999988,PBRLending 旧システム 貸出")
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("0.0499302699999999988")


# ---- スキップ条件 ----

def test_system_migration_skipped(tmp_path):
    """システム移行はスキップ（日付によらず）。"""
    txs = _load(tmp_path, "2026-03-02,BTC,システム移行,0,PBRLending 貸出準備ウォレット履歴")
    assert txs == []


def test_zero_amount_skipped(tmp_path):
    """数量=0 の行はスキップ。"""
    txs = _load(tmp_path, "2026-04-01,BTC,入庫,0,備考")
    assert txs == []


# ---- 入庫 ----

def test_nyuko_is_deposit(tmp_path):
    """入庫は DEPOSIT として記録される。"""
    txs = _load(tmp_path, "2026-03-31,BTC,入庫,0.1,PBRLending 貸出準備ウォレット履歴")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.1")
    assert tx.label == "pbr_deposit"


def test_gap_period_is_recorded(tmp_path):
    """日次レポートが存在しない空白期間 (2026-01-01～2026-03-02) の入庫も記録される。"""
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


# ---- 利息系（新システムの入出金履歴） ----

def test_interest_is_reward(tmp_path):
    """区分「利息」は日次の利息付与。REWARD として記録する。"""
    txs = _load(tmp_path, "2026-03-03,USDC,利息,1.232877,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("1.232877")
    assert tx.label == "daily_interest"


def test_return_interest_is_reward(tmp_path):
    """区分「返還利息」は返還時に付与される利息。REWARD として記録する。"""
    txs = _load(tmp_path, "2026-04-28,ETH,返還利息,0.00116522,")
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].received_amount == Decimal("0.00116522")
    assert txs[0].label == "return_interest"


def test_premium_maturity_is_reward(tmp_path):
    """区分「プレミアム満期」も利息付与。REWARD として記録する。"""
    txs = _load(tmp_path, "2026-05-07,XRP,プレミアム満期,0.041096,")
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].received_amount == Decimal("0.041096")
    assert txs[0].label == "premium_maturity_interest"


def test_interest_slices_sum_to_daily_grant(tmp_path):
    """利息 / 返還利息 / プレミアム満期 は同じ日次付与の排他的な内訳。

    同日同資産に両方が並ぶ場合、合計がその時期の日次額に一致する
    （2026-03-24 BTC: 0.00002008 + 0.00002393 = 0.00004401）。
    重複していないので3区分すべて記録してよい。
    """
    txs = _load(
        tmp_path,
        "2026-03-24,BTC,利息,0.00002008,",
        "2026-03-24,BTC,返還利息,0.00002393,",
    )
    assert len(txs) == 2
    assert all(t.type == TxType.REWARD for t in txs)
    assert sum(t.received_amount for t in txs) == Decimal("0.00004401")


def test_daily_interest_switch_off(tmp_path):
    """record_daily_interest=False で日次利息だけ落とし、件数を計上する。"""
    src = PbrTransfersCsvSource(_SOURCE)
    src.record_daily_interest = False
    txs = src.load(_write_cp932(
        tmp_path,
        "2026-03-03,USDC,利息,1.232877,",
        "2026-04-28,ETH,返還利息,0.00116522,",
    ))
    assert len(txs) == 1
    assert txs[0].label == "return_interest"
    assert src.skip_reasons == {"daily_interest_disabled": 1}


# ---- 内部移動・未知の区分 ----

def test_lending_kubun_skipped_and_counted(tmp_path):
    """区分「貸出」は貸出準備ウォレットとの内部移動。残高を動かさない。"""
    src = PbrTransfersCsvSource(_SOURCE)
    txs = src.load(_write_cp932(tmp_path, "2026-03-31,BTC,貸出,0.1,"))
    assert txs == []
    assert src.skipped == 1
    assert src.skip_reasons == {"internal_move": 1}


def test_return_kubun_skipped_and_counted(tmp_path):
    """区分「返還」も内部移動としてスキップする。"""
    src = PbrTransfersCsvSource(_SOURCE)
    txs = src.load(_write_cp932(tmp_path, "2026-06-02,XRP,返還,50,"))
    assert txs == []
    assert src.skip_reasons == {"internal_move": 1}


def test_unknown_kubun_counted(tmp_path):
    """未知の区分は無言で落とさず、値つきで理由に残す。"""
    src = PbrTransfersCsvSource(_SOURCE)
    txs = src.load(_write_cp932(tmp_path, "2026-06-02,XRP,謎,1,"))
    assert txs == []
    assert src.skip_reasons == {"unknown_kubun:謎": 1}


def test_skip_counters_reset_between_loads(tmp_path):
    """同じインスタンスで2回 load しても件数が累積しない。"""
    src = PbrTransfersCsvSource(_SOURCE)
    path = _write_cp932(tmp_path, "2026-03-31,BTC,貸出,0.1,")
    src.load(path)
    src.load(path)
    assert src.skipped == 1


def test_duplicate_rows_counted(tmp_path):
    """同一日・同一資産・同一額の行は ID が衝突して1件になる。件数に残す。"""
    src = PbrTransfersCsvSource(_SOURCE)
    txs = src.load(_write_cp932(
        tmp_path,
        "2026-03-03,USDC,利息,1.232877,",
        "2026-03-03,USDC,利息,1.232877,",
    ))
    assert len(txs) == 1
    assert src.skip_reasons == {"duplicate_row": 1}


# ---- 複数行 ----

def test_multiple_rows(tmp_path):
    """入庫・出庫・利息・内部移動が混在しても正しく処理される。"""
    src = PbrTransfersCsvSource(_SOURCE)
    txs = src.load(_write_cp932(
        tmp_path,
        "2025-09-29,BTC,入庫,0.1,旧システム",          # DEPOSIT
        "2026-03-31,BTC,入庫,0.1,新システム",           # DEPOSIT
        "2026-03-31,USDC,入庫,13000,新システム",         # DEPOSIT
        "2026-04-28,XRP,出庫,-50,新システム",            # WITHDRAW
        "2026-03-02,ETH,システム移行,0,移行",            # スキップ（数えない）
        "2026-03-03,USDC,利息,1.232877,",                # REWARD
        "2026-03-31,BTC,貸出,0.1,",                      # スキップ（数える）
    ))
    types = [t.type.value for t in txs]
    assert types.count("deposit") == 3
    assert types.count("withdraw") == 1
    assert types.count("reward") == 1
    assert len(txs) == 5
    assert src.skip_reasons == {"internal_move": 1}
