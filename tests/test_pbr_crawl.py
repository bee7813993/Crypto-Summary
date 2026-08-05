"""PbrCrawlJsonSource のテスト

重点検証:
- daily_ranges を日付×通貨で展開し、重なる区間は合算して 1 行にする
- 区間は返還日・満期日を含まないため wallet_events の利息と二重計上しない
- 指数表記 ("8.3E-7") を固定小数点へ正規化してから ID・金額に使う
- 入庫 → DEPOSIT、出庫 → WITHDRAW（abs値）。システム移行・数量0 は黙って落とす
- 貸出 / 返還 は内部移動としてスキップし件数を計上する
- 同一日・同一額のイベントが複数あっても ID が衝突せず、入力順に依存しない
- 未知の種別はスキップしつつ件数を計上する（無言で落とさない）
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sources.jp.pbr_crawl import PbrCrawlJsonSource

_SOURCE = "pbr_crawl"


def _write(tmp_path: Path, **sections) -> Path:
    payload = {
        "start_date": "2026-01-01",
        "end_date": "2026-08-04",
        "currencies": ["BTC", "ETH"],
        "daily_ranges": [],
        "wallet_events": [],
        "transfer_events": [],
        "balance_snapshots": [],
        "warnings": [],
    }
    payload.update(sections)
    p = tmp_path / "pbrlending_crawled.latest.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _load(tmp_path: Path, **sections):
    return PbrCrawlJsonSource(_SOURCE).load(_write(tmp_path, **sections))


def _range(date_from, date_to, currency, amount):
    return {
        "date_from": date_from, "date_to": date_to,
        "currency": currency, "daily_expected_interest": amount,
    }


# ---- フォーマット検証 ----

def test_wrong_format_rejected(tmp_path):
    """イベント項目を持たない JSON は明示エラーになる。"""
    p = tmp_path / "other.json"
    p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    with pytest.raises(ValueError, match="正規化 JSON ではありません"):
        PbrCrawlJsonSource(_SOURCE).load(p)


# ---- daily_ranges の展開 ----

def test_daily_range_expands_to_one_row_per_day(tmp_path):
    txs = _load(tmp_path, daily_ranges=[
        _range("2026-03-03", "2026-03-05", "BTC", "0.00001"),
    ])
    assert len(txs) == 3
    assert [t.timestamp.date().isoformat() for t in txs] == [
        "2026-03-03", "2026-03-04", "2026-03-05",
    ]
    assert all(t.type == TxType.REWARD for t in txs)
    assert all(t.label == "daily_interest" for t in txs)
    assert all(t.received_amount == Decimal("0.00001") for t in txs)


def test_overlapping_ranges_are_summed_into_one_row(tmp_path):
    """契約が複数あっても、同じ日・同じ通貨は 1 行に合算される。"""
    txs = _load(tmp_path, daily_ranges=[
        _range("2026-03-24", "2026-03-24", "BTC", "0.00000132"),
        _range("2026-03-24", "2026-03-24", "BTC", "0.00000137"),
        _range("2026-03-24", "2026-03-24", "BTC", "0.00001739"),
    ])
    assert len(txs) == 1
    assert txs[0].received_amount == Decimal("0.00002008")


def test_daily_interest_and_return_interest_are_not_double_counted(tmp_path):
    """実データ準拠: 区間合算 0.00002008 + 返還利息 0.00002393 = 日次額 0.00004401。

    返還日の利息は区間から除かれ wallet_events 側に現れるので、
    両方を記録しても合計が日次額を超えない。
    """
    txs = _load(
        tmp_path,
        daily_ranges=[_range("2026-03-24", "2026-03-24", "BTC", "0.00002008")],
        wallet_events=[{
            "date": "2026-03-24", "currency": "BTC",
            "type": "返還利息", "amount": "0.00002393",
        }],
    )
    total = sum(t.received_amount for t in txs)
    assert total == Decimal("0.00004401")
    assert sorted(t.label for t in txs) == ["daily_interest", "return_interest"]


def test_scientific_notation_is_normalized(tmp_path):
    """"8.3E-7" は 0.00000083 として扱われ、ID は固定小数点表記から作られる。

    Decimal の str() は指数が小さいと科学的記数法になるため、表示形は揃わない。
    揃える必要があるのは ID の材料（raw_key）で、そちらは固定小数点に正規化する。
    """
    txs = _load(tmp_path, daily_ranges=[
        _range("2026-06-03", "2026-06-03", "BTC", "8.3E-7"),
    ])
    assert txs[0].received_amount == Decimal("0.00000083")
    assert txs[0].raw["数量"] == "0.00000083"
    assert txs[0].id == CanonicalTx.make_id(
        _SOURCE, "2026-06-03|利息|BTC|0.00000083")


def test_id_is_stable_across_notations(tmp_path):
    """同じ値なら JSON 側の表記が変わっても ID が変わらない（再取得で重複しない）。"""
    sci = _load(tmp_path, daily_ranges=[
        _range("2026-06-03", "2026-06-03", "BTC", "8.3E-7"),
    ])
    plain = _load(tmp_path, daily_ranges=[
        _range("2026-06-03", "2026-06-03", "BTC", "0.00000083"),
    ])
    assert sci[0].id == plain[0].id


def test_maturity_gap_day_keeps_other_contracts_interest(tmp_path):
    """満期日は当該契約の区間が途切れるが、他契約の日次利息と満期利息は残る。"""
    txs = _load(
        tmp_path,
        daily_ranges=[
            # 満期契約: 07-01 まで、07-02 は欠落し 07-03 から再開
            _range("2026-07-01", "2026-07-01", "BTC", "8.3E-7"),
            _range("2026-07-03", "2026-07-03", "BTC", "8.3E-7"),
            # 別契約: 07-02 も継続
            _range("2026-07-01", "2026-07-03", "BTC", "0.00007766"),
        ],
        wallet_events=[{
            "date": "2026-07-02", "currency": "BTC",
            "type": "満期", "amount": "8.3E-7",
        }],
    )
    by_day = {t.timestamp.date().isoformat(): t for t in txs if t.label == "daily_interest"}
    assert by_day["2026-07-01"].received_amount == Decimal("0.00007849")
    assert by_day["2026-07-02"].received_amount == Decimal("0.00007766")
    maturity = [t for t in txs if t.label == "premium_maturity_interest"]
    assert len(maturity) == 1
    assert maturity[0].received_amount == Decimal("0.00000083")


def test_zero_total_day_is_not_recorded(tmp_path):
    txs = _load(tmp_path, daily_ranges=[
        _range("2026-03-03", "2026-03-03", "BTC", "0"),
    ])
    assert txs == []


def test_invalid_range_is_counted_as_skip(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    txs = src.load(_write(tmp_path, daily_ranges=[
        _range("2026-03-05", "2026-03-03", "BTC", "0.1"),   # 逆順
        _range("2026-03-03", "2026-03-03", "BTC", "abc"),   # 数値でない
    ]))
    assert txs == []
    assert src.skip_reasons == {"invalid_daily_range": 2}


def test_record_daily_interest_disabled(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    src.record_daily_interest = False
    txs = src.load(_write(tmp_path, daily_ranges=[
        _range("2026-03-03", "2026-03-04", "BTC", "0.00001"),
    ]))
    assert txs == []
    assert src.skip_reasons == {"daily_interest_disabled": 2}


def test_timestamps_are_utc_midnight(tmp_path):
    txs = _load(tmp_path, daily_ranges=[
        _range("2026-03-03", "2026-03-03", "BTC", "0.00001"),
    ])
    ts = txs[0].timestamp
    assert ts.hour == 0 and ts.minute == 0
    assert ts.utcoffset().total_seconds() == 0


# ---- wallet_events ----

def test_wallet_reward_types(tmp_path):
    txs = _load(tmp_path, wallet_events=[
        {"date": "2026-06-02", "currency": "ETH", "type": "返還利息", "amount": "0.00113916"},
        {"date": "2026-05-07", "currency": "XRP", "type": "満期", "amount": "0.041096"},
    ])
    by_label = {t.label: t for t in txs}
    assert by_label["return_interest"].type == TxType.REWARD
    assert by_label["return_interest"].received_amount == Decimal("0.00113916")
    assert by_label["premium_maturity_interest"].received_asset == "XRP"


def test_internal_moves_are_skipped_and_counted(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    txs = src.load(_write(tmp_path, wallet_events=[
        {"date": "2026-06-02", "currency": "BTC", "type": "貸出", "amount": "-0.003"},
        {"date": "2026-06-02", "currency": "BTC", "type": "返還", "amount": "0.001"},
    ]))
    assert txs == []
    assert src.skip_reasons == {"internal_move": 2}


def test_unknown_wallet_event_is_counted(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    src.load(_write(tmp_path, wallet_events=[
        {"date": "2026-06-02", "currency": "BTC", "type": "新種別", "amount": "1"},
    ]))
    assert src.skip_reasons == {"unknown_wallet_event:新種別": 1}


# ---- transfer_events ----

def test_deposit_and_withdrawal(tmp_path):
    txs = _load(tmp_path, transfer_events=[
        {"date": "2026-03-31", "currency": "BTC", "type": "入庫", "amount": "0.1"},
        {"date": "2026-07-07", "currency": "XRP", "type": "出庫", "amount": "-50"},
    ])
    dep = next(t for t in txs if t.type == TxType.DEPOSIT)
    wit = next(t for t in txs if t.type == TxType.WITHDRAW)
    assert dep.received_asset == "BTC" and dep.received_amount == Decimal("0.1")
    assert dep.label == "pbr_deposit"
    assert wit.sent_asset == "XRP" and wit.sent_amount == Decimal("50")
    assert wit.received_amount is None
    assert wit.label == "pbr_withdrawal"


def test_system_migration_and_zero_amount_are_dropped_silently(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    txs = src.load(_write(tmp_path, transfer_events=[
        {"date": "2026-03-02", "currency": "BTC", "type": "システム移行", "amount": "0"},
        {"date": "2026-03-02", "currency": "ETH", "type": "入庫", "amount": "0"},
    ]))
    assert txs == []
    assert src.skip_reasons == {}


def test_unknown_transfer_event_is_counted(tmp_path):
    src = PbrCrawlJsonSource(_SOURCE)
    src.load(_write(tmp_path, transfer_events=[
        {"date": "2026-03-02", "currency": "BTC", "type": "謎の区分", "amount": "1"},
    ]))
    assert src.skip_reasons == {"unknown_transfer_event:謎の区分": 1}


# ---- 重複イベントの ID 決定性 ----

_DUP_EVENTS = [
    {"date": "2026-04-28", "currency": "BTC", "type": "返還利息", "amount": "0.0025"},
    {"date": "2026-04-28", "currency": "BTC", "type": "返還利息", "amount": "0.0025"},
]


def test_identical_events_get_distinct_ids(tmp_path):
    """同一日・同一額のイベントが 2 件あっても 2 行として残る。"""
    txs = _load(tmp_path, wallet_events=_DUP_EVENTS)
    assert len(txs) == 2
    assert len({t.id for t in txs}) == 2
    assert sum(t.received_amount for t in txs) == Decimal("0.005")


def test_duplicate_ids_do_not_depend_on_input_order(tmp_path):
    a = _load(tmp_path, wallet_events=_DUP_EVENTS + [
        {"date": "2026-04-28", "currency": "BTC", "type": "返還利息", "amount": "0.0031"},
    ])
    b = _load(tmp_path, wallet_events=[
        {"date": "2026-04-28", "currency": "BTC", "type": "返還利息", "amount": "0.0031"},
    ] + _DUP_EVENTS)
    assert {t.id for t in a} == {t.id for t in b}


def test_different_amounts_same_day_have_no_suffix(tmp_path):
    """金額が違えば連番は付かない（既存 ID が後から変わらない）。"""
    single = _load(tmp_path, wallet_events=[
        {"date": "2026-06-02", "currency": "BTC", "type": "返還利息", "amount": "0.00001394"},
    ])
    with_sibling = _load(tmp_path, wallet_events=[
        {"date": "2026-06-02", "currency": "BTC", "type": "返還利息", "amount": "0.00001394"},
        {"date": "2026-06-02", "currency": "BTC", "type": "返還利息", "amount": "0.00002232"},
    ])
    assert single[0].id in {t.id for t in with_sibling}
