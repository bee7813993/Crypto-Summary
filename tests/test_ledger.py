import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType


def _tx(suffix: str = "1", source: str = "test") -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, suffix),
        source=source,
        timestamp=datetime(2024, 1, int(suffix), tzinfo=timezone.utc),
        type=TxType.TRADE,
        received_asset="BTC",
        received_amount=Decimal("0.1"),
        sent_asset="USDT",
        sent_amount=Decimal("4200"),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    db = Ledger(tmp_path / "test.db")
    yield db
    db.close()


def test_upsert_and_count(ledger):
    ledger.upsert(_tx("1"))
    assert ledger.count() == 1


def test_upsert_is_idempotent(ledger):
    tx = _tx("1")
    ledger.upsert(tx)
    ledger.upsert(tx)   # same id → no duplicate
    assert ledger.count() == 1


def test_upsert_many(ledger):
    txs = [_tx(str(i)) for i in range(1, 6)]
    ledger.upsert_many(txs)
    assert ledger.count() == 5


def test_count_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    assert ledger.count("binance") == 2
    assert ledger.count("bybit") == 1
    assert ledger.count() == 3


def test_set_and_get_cursor(ledger):
    assert ledger.get_cursor("binance") is None
    ts = datetime(2024, 3, 1, tzinfo=timezone.utc)
    ledger.set_cursor("binance", ts)
    assert ledger.get_cursor("binance") == ts


def test_cursor_overwrites(ledger):
    t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    ledger.set_cursor("binance", t1)
    ledger.set_cursor("binance", t2)
    assert ledger.get_cursor("binance") == t2


def test_date_ranges_by_source(ledger):
    """ソースごとの取引期間 (最古, 最新) を返す。"""
    ledger.upsert(_tx("1", source="binance"))   # 2024-01-01
    ledger.upsert(_tx("5", source="binance"))   # 2024-01-05
    ledger.upsert(_tx("3", source="bybit"))     # 2024-01-03
    ranges = ledger.date_ranges_by_source()
    assert ranges["binance"][0].startswith("2024-01-01")
    assert ranges["binance"][1].startswith("2024-01-05")
    assert ranges["bybit"][0].startswith("2024-01-03")
    assert ranges["bybit"][1].startswith("2024-01-03")


def test_date_ranges_empty(ledger):
    """取引が無ければ空の辞書。"""
    assert ledger.date_ranges_by_source() == {}


def test_list_import_batches_includes_period(ledger):
    """インポートバッチに取引期間 (first_ts/last_ts) が含まれる。"""
    txs = [_tx("2", source="bf"), _tx("8", source="bf")]  # 2024-01-02, 2024-01-08
    ledger.upsert_many(txs)
    ledger.record_import_batch(
        "batch1", "bf", "bitflyer", "trades.csv", [t.id for t in txs]
    )
    batches = ledger.list_import_batches()
    assert len(batches) == 1
    b = batches[0]
    assert b["first_ts"].startswith("2024-01-02")
    assert b["last_ts"].startswith("2024-01-08")


def test_all_returns_txs(ledger):
    for i in range(1, 4):
        ledger.upsert(_tx(str(i)))
    results = ledger.all()
    assert len(results) == 3
    assert all(isinstance(t, CanonicalTx) for t in results)


def test_all_filter_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="bybit"))
    assert len(ledger.all(source="binance")) == 1
    assert len(ledger.all(source="bybit")) == 1


def test_roundtrip_decimal_precision(ledger):
    tx = CanonicalTx(
        id="precise-test",
        source="test",
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        type=TxType.TRADE,
        received_asset="BTC",
        received_amount=Decimal("0.00238095"),
        sent_asset="USDT",
        sent_amount=Decimal("100.00000000"),
        fee_asset="BNB",
        fee_amount=Decimal("0.00010000"),
    )
    ledger.upsert(tx)
    result = ledger.all()[0]
    assert result.received_amount == Decimal("0.00238095")
    assert result.sent_amount == Decimal("100.00000000")
    assert result.fee_amount == Decimal("0.00010000")


def test_sources_summary(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    rows = ledger.sources()
    src_map = {r[0]: r[1] for r in rows}
    assert src_map["binance"] == 2
    assert src_map["bybit"] == 1


def test_clear_by_source(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("2", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    n = ledger.clear(source="binance")
    assert n == 2
    assert ledger.count("binance") == 0
    assert ledger.count("bybit") == 1


def test_clear_all(ledger):
    ledger.upsert(_tx("1", source="binance"))
    ledger.upsert(_tx("1", source="bybit"))
    n = ledger.clear()
    assert n == 2
    assert ledger.count() == 0


def test_balances(ledger):
    ledger.upsert(_tx("1"))  # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("2"))  # +0.1 BTC, -4200 USDT
    bals = ledger.balances()
    assert bals["BTC"] == Decimal("0.2")
    assert bals["USDT"] == Decimal("-8400")


def test_balances_multiple_sources(ledger):
    ledger.upsert(_tx("1", source="nexo_spot"))   # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("2", source="nexo_dnw"))    # +0.1 BTC, -4200 USDT
    ledger.upsert(_tx("3", source="binance"))     # +0.1 BTC, -4200 USDT

    # 単一ソース
    one = ledger.balances(source="nexo_spot")
    assert one["BTC"] == Decimal("0.1")

    # 複数ソース合算 (nexo_spot + nexo_dnw)
    combined = ledger.balances(source=["nexo_spot", "nexo_dnw"])
    assert combined["BTC"] == Decimal("0.2")
    assert combined["USDT"] == Decimal("-8400")

    # 全ソース
    all_bal = ledger.balances()
    assert all_bal["BTC"] == Decimal("0.3")


def test_balances_by_source(ledger):
    ledger.upsert(_tx("1", source="nexo_spot"))
    ledger.upsert(_tx("2", source="nexo_dnw"))
    ledger.upsert(_tx("3", source="nexo_dnw"))

    per = ledger.balances_by_source(source=["nexo_spot", "nexo_dnw"])
    assert set(per) == {"nexo_spot", "nexo_dnw"}
    assert per["nexo_spot"]["BTC"] == Decimal("0.1")
    assert per["nexo_dnw"]["BTC"] == Decimal("0.2")


# ---- ラベル除外（UI トグル） ----

def _reward_tx(suffix: str, label: str, source: str = "pbr") -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"{label}{suffix}"),
        source=source,
        timestamp=datetime(2026, 3, int(suffix), tzinfo=timezone.utc),
        type=TxType.REWARD,
        received_asset="BTC",
        received_amount=Decimal("0.001"),
        label=label,
    )


def test_balances_exclude_labels(ledger):
    ledger.upsert(_reward_tx("3", "daily_interest"))
    ledger.upsert(_reward_tx("4", "return_interest"))

    assert ledger.balances()["BTC"] == Decimal("0.002")
    assert ledger.balances(exclude_labels={"daily_interest"})["BTC"] == Decimal("0.001")


def test_balances_by_source_exclude_labels(ledger):
    ledger.upsert(_reward_tx("3", "daily_interest"))
    ledger.upsert(_reward_tx("4", "return_interest"))

    per = ledger.balances_by_source(exclude_labels={"daily_interest"})
    assert per["pbr"]["BTC"] == Decimal("0.001")


def test_exclude_labels_keeps_null_label_rows(ledger):
    """label が NULL の取引は除外の対象外（大半の取引はラベルを持たない）。"""
    ledger.upsert(_tx("1"))                      # label なし
    ledger.upsert(_reward_tx("3", "daily_interest"))

    bal = ledger.balances(exclude_labels={"daily_interest"})
    assert bal["BTC"] == Decimal("0.1")


def test_transactions_exclude_labels(ledger):
    ledger.upsert(_reward_tx("3", "daily_interest"))
    ledger.upsert(_reward_tx("4", "return_interest"))

    txs, total = ledger.transactions(exclude_labels={"daily_interest"})
    assert total == 1
    assert [t.label for t in txs] == ["return_interest"]


def test_exclude_labels_none_is_noop(ledger):
    ledger.upsert(_reward_tx("3", "daily_interest"))
    assert ledger.balances(exclude_labels=None)["BTC"] == Decimal("0.001")
    assert ledger.balances(exclude_labels=set())["BTC"] == Decimal("0.001")


# ---- 期間洗い替え（外部ツール連携の再取得） ----

def _dated_tx(day: int, source: str = "pbr", month: int = 3,
              tx_id: str | None = None) -> CanonicalTx:
    """2026-<month>-<day> UTC 深夜の REWARD 行。"""
    return CanonicalTx(
        id=tx_id or CanonicalTx.make_id(source, f"{month}-{day}"),
        source=source,
        timestamp=datetime(2026, month, day, tzinfo=timezone.utc),
        type=TxType.REWARD,
        received_asset="BTC",
        received_amount=Decimal("0.001"),
        label="daily_interest",
    )


_W_START = datetime(2026, 3, 3, tzinfo=timezone.utc)
_W_END = datetime(2026, 3, 6, tzinfo=timezone.utc)


def test_count_in_window_by_source(ledger):
    ledger.upsert(_dated_tx(3))                        # 窓内
    ledger.upsert(_dated_tx(4))                        # 窓内
    ledger.upsert(_dated_tx(4, source="pbr_crawl"))    # 窓内 別ソース
    ledger.upsert(_dated_tx(9))                        # 窓外
    counts = ledger.count_in_window(["pbr", "pbr_crawl"], _W_START, _W_END)
    assert counts == {"pbr": 2, "pbr_crawl": 1}


def test_window_boundaries_are_start_inclusive_end_exclusive(ledger):
    ledger.upsert(_dated_tx(2))   # 窓の直前
    ledger.upsert(_dated_tx(3))   # start ちょうど → 対象
    ledger.upsert(_dated_tx(5))   # 窓内
    ledger.upsert(_dated_tx(6))   # end ちょうど → 対象外
    deleted = ledger.delete_by_source_window(["pbr"], _W_START, _W_END)
    assert deleted == 2
    remaining = sorted(t.timestamp.day for t in ledger.all())
    assert remaining == [2, 6]


def test_delete_by_source_window_ignores_other_sources(ledger):
    ledger.upsert(_dated_tx(4))
    ledger.upsert(_dated_tx(4, source="gmo"))
    ledger.delete_by_source_window(["pbr"], _W_START, _W_END)
    assert ledger.count("pbr") == 0
    assert ledger.count("gmo") == 1


def test_delete_by_source_window_cleans_exports_and_batch_txs(ledger):
    tx = _dated_tx(4)
    ledger.upsert(tx)
    ledger.mark_exported([tx.id], "koinly")
    ledger.record_import_batch("b1", "pbr", "pbr", "old.csv", [tx.id])

    ledger.delete_by_source_window(["pbr"], _W_START, _W_END)

    assert ledger._conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0
    assert ledger._conn.execute("SELECT COUNT(*) FROM batch_txs").fetchone()[0] == 0
    # バッチ記録自体は履歴として残る
    assert len(ledger.list_import_batches()) == 1


def test_delete_by_source_window_keeps_manual_rows(ledger):
    ledger.upsert(_dated_tx(4, tx_id="manual:abc123"))
    ledger.upsert(_dated_tx(5))
    deleted = ledger.delete_by_source_window(["pbr"], _W_START, _W_END)
    assert deleted == 1
    assert [t.id for t in ledger.all()] == ["manual:abc123"]


def test_replace_windows_swaps_rows(ledger):
    ledger.upsert(_dated_tx(1))                       # 窓外: 残る
    ledger.upsert(_dated_tx(4))                       # 窓内 レガシー: 消える
    fresh = [_dated_tx(4, source="pbr_crawl"), _dated_tx(5, source="pbr_crawl")]

    stats = ledger.replace_windows(
        [(["pbr", "pbr_crawl"], _W_START, _W_END)], fresh,
        batch_id="b1", source="pbr_crawl", exchange="pbr_crawl",
        filename="crawl.json", prune_batch_source="pbr_crawl",
    )

    assert stats == {"deleted": 1, "inserted": 2, "parsed": 2}
    assert ledger.count("pbr") == 1
    assert ledger.count("pbr_crawl") == 2
    batches = ledger.list_import_batches()
    assert [b["id"] for b in batches] == ["b1"]
    assert batches[0]["existing_count"] == 2


def test_replace_windows_uses_different_range_per_source(ledger):
    """ソースごとに削除範囲を変えられる（自前は広く、他ソースは狭く）。"""
    ledger.upsert(_dated_tx(1, source="pbr_crawl"))   # 広い窓の中: 消える
    ledger.upsert(_dated_tx(1))                        # 狭い窓の外: 残る
    ledger.upsert(_dated_tx(4))                        # 狭い窓の中: 消える
    fresh = [_dated_tx(4, source="pbr_crawl")]

    stats = ledger.replace_windows(
        [
            (["pbr_crawl"], datetime(2026, 3, 1, tzinfo=timezone.utc), _W_END),
            (["pbr"], _W_START, _W_END),
        ],
        fresh,
        batch_id="b1", source="pbr_crawl", exchange="pbr_crawl",
        filename="crawl.json", prune_batch_source="pbr_crawl",
    )

    assert stats["deleted"] == 2
    assert [t.timestamp.day for t in ledger.all(source="pbr")] == [1]
    assert [t.timestamp.day for t in ledger.all(source="pbr_crawl")] == [4]


def test_replace_windows_is_idempotent(ledger):
    fresh = [_dated_tx(4, source="pbr_crawl"), _dated_tx(5, source="pbr_crawl")]
    spec = [(["pbr_crawl"], _W_START, _W_END)]
    ledger.replace_windows(
        spec, fresh, batch_id="b1", source="pbr_crawl", exchange="pbr_crawl",
        filename="crawl.json", prune_batch_source="pbr_crawl",
    )
    stats = ledger.replace_windows(
        spec, fresh, batch_id="b2", source="pbr_crawl", exchange="pbr_crawl",
        filename="crawl.json", prune_batch_source="pbr_crawl",
    )
    assert stats["deleted"] == 2
    assert stats["inserted"] == 2
    assert ledger.count("pbr_crawl") == 2
    # 古いバッチは prune され常に 1 件
    assert [b["id"] for b in ledger.list_import_batches()] == ["b2"]


def test_replace_windows_rolls_back_on_failure(ledger):
    ledger.upsert(_dated_tx(4))
    ledger.record_import_batch("dup", "pbr", "pbr", "x.csv", [])
    fresh = [_dated_tx(4, source="pbr_crawl")]

    with pytest.raises(Exception):
        # batch_id が既存と衝突して INSERT に失敗する
        ledger.replace_windows(
            [(["pbr", "pbr_crawl"], _W_START, _W_END)], fresh,
            batch_id="dup", source="pbr_crawl", exchange="pbr_crawl",
            filename="crawl.json",
        )

    # 削除も投入も巻き戻っている
    assert ledger.count("pbr") == 1
    assert ledger.count("pbr_crawl") == 0
