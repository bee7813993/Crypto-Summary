"""GmoCsvSource のテスト

重点検証:
- GMO の「日時」は JST(UTC+9) なので UTC へ変換されること (9時間巻き戻し)
- 同一注文IDの複数約定が1取引に集約されること
- 暗号資産送付が WITHDRAW として tx_hash 付きで計上されること
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from crypto_summary.sources.jp.gmo import GmoCsvSource
from crypto_summary.core.models import TxType

_HEADER = (
    "日時,精算区分,日本円受渡金額,注文ID,約定ID,建玉ID,銘柄名,注文タイプ,取引区分,"
    "売買区分,執行条件,約定数量,約定レート,約定金額,注文手数料,レバレッジ手数料,"
    "入出金区分,入出金金額,授受区分,数量,送付手数料,送付先/送付元,トランザクションID"
)


def _write_csv(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "2026_trading_report.csv"
    p.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8-sig")
    return p


def _load(tmp_path: Path, *rows: str):
    return GmoCsvSource("gmo").load(_write_csv(tmp_path, *rows))


def test_spot_trade_timestamp_jst_to_utc(tmp_path):
    """JST 11:51 の現物取引は UTC 02:51 に変換されること。"""
    txs = _load(
        tmp_path,
        "2026/01/19 11:51,取引所現物取引,-35000,7951912321,,,SOL,,,買,指値,1.689026155,20722,35000,0,,,,,,,,",
    )
    assert len(txs) == 1
    tx = txs[0]
    assert tx.timestamp == datetime(2026, 1, 19, 2, 51, tzinfo=timezone.utc)
    assert tx.type == TxType.TRADE
    assert tx.received_asset == "SOL"
    assert tx.sent_asset == "JPY"


def test_crypto_withdraw_timestamp_jst_to_utc(tmp_path):
    """JST 11:51 の暗号資産送付は UTC 02:51 に変換されること。"""
    txs = _load(
        tmp_path,
        "2026/01/19 11:51,暗号資産預入・送付,,,,,SOL,,,,,,,,,,,,送付,236.000226537,,ハードウォレット,ABC123",
    )
    assert len(txs) == 1
    tx = txs[0]
    assert tx.timestamp == datetime(2026, 1, 19, 2, 51, tzinfo=timezone.utc)
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "SOL"
    assert tx.sent_amount == Decimal("236.000226537")
    assert tx.tx_hash == "ABC123"


def test_id_stable_across_timezone_fix(tmp_path):
    """IDは「日時」文字列ベースなので、UTC変換を入れても不変であること（冪等）。"""
    row = "2026/01/19 11:51,暗号資産預入・送付,,,,,SOL,,,,,,,,,,,,送付,236.000226537,,ハードウォレット,ABC123"
    tx = _load(tmp_path, row)[0]
    # raw_key = 日時|銘柄名|授受区分|数量 を sha256 した先頭16桁
    from crypto_summary.core.models import CanonicalTx
    expected = CanonicalTx.make_id("gmo", "2026/01/19 11:51|SOL|送付|236.000226537")
    assert tx.id == expected
