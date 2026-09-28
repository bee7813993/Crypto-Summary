"""取引履歴の並び順（古い順／新しい順）と月ごとの表示の API テスト。

- /api/transactions?order=asc|desc: 並び順とページ送り
- /api/transactions/months: 月の見出し（件数・種類ごとの件数・資産の増減）
- /api/transactions?month=YYYY-MM: 開いた月の取引（全件）
月はブラウザのタイムゾーン（tz）で区切る。
"""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.core.models import CanonicalTx, TxType  # noqa: E402
from crypto_summary.web.app import create_app  # noqa: E402


def _tx(tx_id, ts, tx_type=TxType.DEPOSIT, source="acct_a",
        ra=None, rv=None, sa=None, sv=None, fa=None, fv=None):
    return CanonicalTx(
        id=tx_id, source=source, timestamp=ts, type=tx_type,
        received_asset=ra, received_amount=Decimal(rv) if rv is not None else None,
        sent_asset=sa, sent_amount=Decimal(sv) if sv is not None else None,
        fee_asset=fa, fee_amount=Decimal(fv) if fv is not None else None,
        raw={},
    )


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _client(tmp_path, txs):
    db = tmp_path / "tx.db"
    ledger = Ledger(str(db))
    for tx in txs:
        ledger.upsert(tx)
    ledger.close()
    return TestClient(create_app(str(db)))


@pytest.fixture()
def client(tmp_path):
    return _client(tmp_path, [
        _tx("aug", _utc(2026, 8, 15, 9), ra="BTC", rv="1"),
        _tx("sep1", _utc(2026, 9, 2, 9), TxType.TRADE, ra="ETH", rv="2", sa="BTC", sv="0.1",
            fa="BTC", fv="0.001"),
        _tx("sep2", _utc(2026, 9, 10, 9), TxType.REWARD, ra="BTC", rv="0.01"),
        # 日本時間では 10/1 05:00。UTC では 9 月
        _tx("edge", _utc(2026, 9, 30, 20), TxType.WITHDRAW, sa="BTC", sv="0.5",
            source="acct_b"),
        _tx("oct", _utc(2026, 10, 3, 9), TxType.REWARD, ra="BTC", rv="0.02"),
    ])


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [t["id"] for t in resp.json()["transactions"]]


# ---------- 並び順 ----------

def test_newest_first_by_default_and_oldest_first_on_request(client):
    newest = _ids(client.get("/api/transactions"))
    assert newest == ["oct", "edge", "sep2", "sep1", "aug"]
    assert client.get("/api/transactions").json()["order"] == "desc"
    oldest = client.get("/api/transactions?order=asc")
    assert _ids(oldest) == list(reversed(newest))
    assert oldest.json()["order"] == "asc"


def test_unknown_order_is_rejected(client):
    assert client.get("/api/transactions?order=up").status_code == 422
    assert client.get("/api/transactions/months?order=up").status_code == 422


def test_same_time_rows_page_without_gaps_or_duplicates(tmp_path):
    """同じ時刻の取引は取り込んだ順（新しい順ではその逆）で、ページ送りで重複・欠落しない。"""
    ts = _utc(2026, 9, 1, 0)
    ids = [f"t{i:02d}" for i in range(60)]  # 1 ページ 50 件をまたぐ
    client = _client(tmp_path, [_tx(i, ts, ra="BTC", rv="1") for i in ids])

    asc = _ids(client.get("/api/transactions?order=asc&page=1")) + _ids(
        client.get("/api/transactions?order=asc&page=2"))
    desc = _ids(client.get("/api/transactions?order=desc&page=1")) + _ids(
        client.get("/api/transactions?order=desc&page=2"))
    assert asc == ids
    assert desc == list(reversed(ids))


# ---------- 月の見出し ----------

def test_months_are_cut_in_the_browsers_time_zone(client):
    """UTC の 9/30 20:00 は日本時間では 10 月。画面の日付と同じ月にまとめる。"""
    utc = client.get("/api/transactions/months").json()
    assert [(m["month"], m["count"]) for m in utc["months"]] == [
        ("2026-10", 1), ("2026-09", 3), ("2026-08", 1),
    ]
    jst = client.get("/api/transactions/months?tz=Asia/Tokyo").json()
    assert [(m["month"], m["count"]) for m in jst["months"]] == [
        ("2026-10", 2), ("2026-09", 2), ("2026-08", 1),
    ]
    assert jst["total"] == 5


def test_unknown_time_zone_falls_back_to_offset_then_utc(client):
    by_offset = client.get("/api/transactions/months?tz=Nope/Zone&tz_offset=540").json()
    assert [m["count"] for m in by_offset["months"]] == [2, 2, 1]
    utc = client.get("/api/transactions/months?tz=Nope/Zone").json()
    assert [m["count"] for m in utc["months"]] == [1, 3, 1]


def test_months_follow_the_order_and_count_each_type(client):
    data = client.get("/api/transactions/months?order=asc&tz=Asia/Tokyo").json()
    assert data["order"] == "asc"
    assert [m["month"] for m in data["months"]] == ["2026-08", "2026-09", "2026-10"]
    october = data["months"][2]
    # 多い種類から（同数は種類名の順）
    assert october["types"] == [
        {"type": "reward", "type_ja": "報酬", "count": 1},
        {"type": "withdraw", "type_ja": "出金", "count": 1},
    ]
    assert october["net"] is None  # 資産で絞り込まないと増減は出さない


def test_months_net_is_the_assets_change_in_the_month(client):
    data = client.get("/api/transactions/months?asset=BTC&tz=Asia/Tokyo").json()
    net = {m["month"]: Decimal(m["net"]) for m in data["months"]}
    assert net == {
        "2026-08": Decimal("1"),
        "2026-09": Decimal("-0.1") - Decimal("0.001") + Decimal("0.01"),
        "2026-10": Decimal("-0.5") + Decimal("0.02"),
    }


def test_months_use_the_same_filters_as_the_list(client):
    by_account = client.get("/api/transactions/months?account=Acct+B").json()
    assert [(m["month"], m["count"]) for m in by_account["months"]] == [("2026-09", 1)]
    by_dates = client.get(
        "/api/transactions/months?since=2026-09-01&until=2026-09-20").json()
    assert [(m["month"], m["count"]) for m in by_dates["months"]] == [("2026-09", 2)]


# ---------- 開いた月の取引 ----------

def test_month_returns_every_row_of_that_local_month(tmp_path):
    ts = [_utc(2026, 9, 1 + i % 28, 12) for i in range(70)]
    txs = [_tx(f"s{i:02d}", t, ra="BTC", rv="1") for i, t in enumerate(ts)]
    txs.append(_tx("next", _utc(2026, 10, 1, 12), ra="BTC", rv="1"))
    client = _client(tmp_path, txs)

    data = client.get("/api/transactions?month=2026-09&order=asc").json()
    assert data["total"] == 70 and len(data["transactions"]) == 70  # ページ送りしない
    assert data["total_pages"] == 1 and data["month"] == "2026-09"
    stamps = [t["timestamp"] for t in data["transactions"]]
    assert stamps == sorted(stamps)


def test_month_boundaries_use_the_time_zone(client):
    assert _ids(client.get("/api/transactions?month=2026-10&tz=Asia/Tokyo")) == ["oct", "edge"]
    assert _ids(client.get("/api/transactions?month=2026-10")) == ["oct"]
    assert _ids(client.get("/api/transactions?month=2026-09&tz=Asia/Tokyo&order=asc")) == [
        "sep1", "sep2",
    ]


def test_month_combines_with_other_filters(client):
    assert _ids(client.get(
        "/api/transactions?month=2026-09&since=2026-09-05&account=Acct+A")) == ["sep2"]


def test_unreadable_month_is_rejected(client):
    assert client.get("/api/transactions?month=2026-13").status_code == 422
    assert client.get("/api/transactions?month=Sept").status_code == 422


# ---------- 台帳 ----------

def test_ledger_before_is_exclusive_and_timeline_is_light(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.upsert(_tx("a", _utc(2026, 9, 30, 23, 59), ra="BTC", rv="1"))
    ledger.upsert(_tx("b", _utc(2026, 10, 1), ra="BTC", rv="2"))
    txs, total = ledger.transactions(before=_utc(2026, 10, 1))
    assert [t.id for t in txs] == ["a"] and total == 1
    rows = ledger.timeline(asset="BTC")
    assert sorted(r[3] for r in rows) == ["1", "2"]
    assert {r[1] for r in rows} == {"deposit"}
    ledger.close()


def test_local_month_bounds_in_tokyo():
    from crypto_summary.web.app import _month_bounds

    start, end = _month_bounds("2026-12", ZoneInfo("Asia/Tokyo"))
    assert start == _utc(2026, 11, 30, 15)
    assert end == _utc(2026, 12, 31, 15)
