"""PbrAutoCsvSource のテスト

Web UI は常に「PBR Lending（自動判定）」で取り込むため、
ヘッダー判定とスキップ件数の転送がここで壊れると利用者から見えなくなる。
"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.models import TxType
from crypto_summary.sources.jp.pbr_auto import PbrAutoCsvSource

_TRANSFERS_HEADER = "日付,通貨種別,区分,数量,備考"
_DAILY_HEADER = (
    "日付,通貨種別,貸出数量,総単日受取予定利息,総累計受取予定利息,"
    "返還数量,返還受取利息（利確数量）,手数料（送金・解約）,"
    "プレミアム移行数量,プレミアム移行受取利息（利確数量）,"
    "プレミアム満期数量,プレミアム満期受取利息（利確数量）,"
    "運営からの付与数量（利確数量）,総貸出元本残高,総受取数量,ご参考レート,備考"
)


def _write(tmp_path: Path, header: str, *rows: str) -> Path:
    p = tmp_path / "pbr.csv"
    p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="cp932")
    return p


def test_dispatch_transfers(tmp_path):
    """「区分」「数量」列 → 入出金履歴として解釈される。"""
    src = PbrAutoCsvSource("pbr")
    txs = src.load(_write(tmp_path, _TRANSFERS_HEADER, "2026-03-31,BTC,入庫,0.1,"))
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("0.1")


def test_dispatch_daily_report(tmp_path):
    """「貸出数量」列 → 日次レポートとして解釈され、利確のみ拾う。"""
    src = PbrAutoCsvSource("pbr")
    txs = src.load(_write(
        tmp_path, _DAILY_HEADER,
        "2025-11-04,BTC,0,0.0000274,0.0009590000,0,0,0,0.004,0.0009590000,0,0,0,0.1,0.0009590000,15597040.51,",
    ))
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert src.skipped == 0


def test_forwards_skipped_from_transfers(tmp_path):
    """サブアダプタのスキップ件数が転送される（転送を忘れると UI から消える）。"""
    src = PbrAutoCsvSource("pbr")
    txs = src.load(_write(
        tmp_path, _TRANSFERS_HEADER,
        "2026-03-31,BTC,入庫,0.1,",
        "2026-03-31,BTC,貸出,0.1,",
    ))
    assert len(txs) == 1
    assert src.skipped == 1
    assert src.skip_reasons == {"internal_move": 1}


def test_skip_counters_reset_between_loads(tmp_path):
    """同じインスタンスで2回 load しても件数が累積しない。"""
    src = PbrAutoCsvSource("pbr")
    path = _write(tmp_path, _TRANSFERS_HEADER, "2026-03-31,BTC,貸出,0.1,")
    src.load(path)
    src.load(path)
    assert src.skipped == 1


def test_unknown_format_rejected(tmp_path):
    """どちらの形式でもないCSVは検出ヘッダー付きでエラーになる。

    「利息付与」ログ (Timestamp,Action,Base,Lending,Volume) はまだ未対応。
    """
    p = tmp_path / "grant.csv"
    p.write_text(
        "(日時),(項目),(取扱銘柄),(利用サービス),(数量)\n"
        "Timestamp,Action,Base,Lending,Volume\n"
        "2026-01-01 00:00:00,利息付与,BTC,プレミアム,0.00000006\n",
        encoding="cp932",
    )
    with pytest.raises(ValueError, match="識別できませんでした"):
        PbrAutoCsvSource("pbr").load(p)
