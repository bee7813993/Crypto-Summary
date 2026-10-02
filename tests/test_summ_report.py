"""SummReportCsvSource（SUMM 取引レポート）と、SUMM が覆う期間の扱いのテスト。

フィクスチャは実際の日本語版レポートの書式（複数行の説明文のレコード、
Asia/Tokyo の現地時刻、符号なしの数量、送金の相手側の口座の行、SUMM 内の
重複送金・手動の残高調整など）を写したもの。ID・Tx ハッシュ・アドレス・
金額はすべて架空の値。
"""
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sources import summ_report
from crypto_summary.sources.csv_import import EXCHANGE_SOURCES
from crypto_summary.sources.summ_report import SummReportCsvSource

_HASH_USDT = "0x" + "a1" * 32
_HASH_SOL = "5Sig" + "x" * 84
_HASH_POL = "0x" + "b2" * 32
_HASH_BTC = "f1" * 32

_PREAMBLE = (
    '"2nd October 2026, 3:14:31 pmにSumm（https://summ.com）が生成したフィルター適用済み取引レポート\n'
    "\n"
    "レポート詳細：\n"
    "特に明記されていない限り、すべての法定通貨の価値と価格はJPYで表示されています。"
    "すべての日付と時刻は「Asia/Tokyo」タイムゾーンで表示されています。\n"
    "\n"
    "免責事項：\n"
    '結果を必ず確認してください。",,,,,,,,,,,\n'
)
_HEADER = "通貨,タイムスタンプ,取引タイプ,価格,数量,価値,送信元,送信先,アカウント,取引ID,コメント,メモ\n"

_ROWS = (
    # 出金（Tx ハッシュ）と、送金先の口座側の入金
    f"Tether (USDT),2026-03-02 10:30:00,送金出金,150,19.3211,2898.17,Bybit,Nexo,Bybit,{_HASH_USDT},,\n"
    f"Tether (USDT),2026-03-02 10:32:00,入金,150,19.3211,2898.17,Bybit,Nexo,Nexo,nexo-rx-1,,\n"
    # 現物の売り（購入・売却・手数料の 3 本）
    "Tether (USDT),2026-03-02 10:06:00,購入,150,181.8,27270,Bybit,Bybit,Bybit,9000000000000000001,,Spot Trade\n"
    "Bitcoin (BTC),2026-03-02 10:06:00,売却,13500000,0.00202,27270,Bybit,Bybit,Bybit,9000000000000000001,,Spot Trade\n"
    "Tether (USDT),2026-03-02 10:06:00,手数料,150,0.1818,27.27,Bybit,Bybit,Bybit,9000000000000000001,,\n"
    # 先物の実現損益・ステーキング報酬
    "Ethereum (ETH),2026-03-02 10:00:00,実現損失,450000,0.12,54000,Bybit,Bybit,Bybit,11111111-2222-3333-4444-555555555555,,\n"
    "Bitcoin (BTC),2026-03-02 09:30:00,Staking報酬,13500000,0.0000002,2.7,Bybit,Bybit,Bybit,1234567890,,"
    "Earned 2e-7 BTC from 0.002 BTC using flexible staking\n"
    # 手動の残高調整（無視）
    "Bitcoin (BTC),2026-03-01 00:00:00,無視（出金）,13500000,0.0005,6750,Bybit,Unknown,Bybit,"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-manual,Balance Adjustment,\n"
    # 出金と出金手数料、送金先の入金
    f"Solana (SOL),2026-02-20 11:55:56,送金出金,19000,10.5,199500,Bybit,Nexo,Bybit,{_HASH_SOL},,\n"
    f"Solana (SOL),2026-02-20 11:55:56,手数料,19000,0.008,152,Bybit,Bybit,Bybit,{_HASH_SOL},,\n"
    f"Solana (SOL),2026-02-20 11:59:08,入金,19000,10.5,199500,Bybit,Nexo,Nexo,NXTfake,,{_HASH_SOL}\n"
    "USDC,2026-02-20 11:44:06,購入,150,1500,225000,Bybit,Bybit,Bybit,9000000000000000002,,Spot Trade\n"
    "Solana (SOL),2026-02-20 11:44:06,売却,19000,10.0,190000,Bybit,Bybit,Bybit,9000000000000000002,,Spot Trade\n"
    "USDC,2026-02-20 11:44:06,手数料,150,1.5,225,Bybit,Bybit,Bybit,9000000000000000002,,\n"
    # 外から入ってきた SOL を「購入」に分類したもの（相方の売却が無い）
    "Solana (SOL),2026-02-20 10:49:42,購入,19000,20.508,389652,Unknown,Bybit,Bybit,228000001,,\n"
    # SUMM 内で同じ送金が 2 回載っている（相手ウォレットの入金から組み立てた分）
    f"Polygon Ecos... (POL),2026-02-10 10:39:11,送金出金,20,1.8,36,Bybit,MetaMask (0xabc) (Polygon (Matic)),Bybit,{_HASH_POL},,\n"
    f"Polygon Ecos... (POL),2026-02-10 10:39:11,入金,20,1.8,36,Bybit,MetaMask (0xabc) (Polygon (Matic)),MetaMask (0xabc) (Polygon (Matic)),{_HASH_POL},,\n"
    f"Polygon Ecos... (POL),2026-02-10 10:38:48,送金出金,20,1.8,36,Bybit,Unknown,Bybit,{_HASH_POL},,\n"
    f"Polygon Ecos... (POL),2026-02-10 10:38:48,手数料,20,0.2,4,Bybit,Bybit,Bybit,{_HASH_POL},,\n"
    "Polygon Ecos... (POL),2026-02-10 10:36:48,購入,21,2,42,Bybit,Bybit,Bybit,1048000000000000000000001,,Convert\n"
    "Ethereum (ETH),2026-02-10 10:36:48,売却,470000,0.00009,42,Bybit,Bybit,Bybit,1048000000000000000000001,,Convert\n"
    # 入金（取引所内の ID）
    "Bitcoin (BTC),2026-02-01 12:00:00,入金,13000000,0.004,52000,Unknown,Bybit,Bybit,99999999,,\n"
    "Ethereum (ETH),2026-02-01 12:00:00,入金,450000,0.5,225000,Unknown,Bybit,Bybit,99999998,,\n"
    # 過去の先物・入金・手動の調整
    "Bitcoin (BTC),2021-03-04 01:01:33,収入,5400000,0.00000126,6.8,Bybit,Bybit,Bybit,2063001448,,Bonus\n"
    "Bitcoin (BTC),2021-03-01 09:30:55,実現利益,6300000,0.00014182,893,Bybit,Bybit,Bybit,774dcef5-8c4a-4f32-a477-9bc03f2e7b92,,PnL\n"
    f"Bitcoin (BTC),2021-02-24 12:59:25,入金,5394100,0.04887266,263624,BitMEX,Bybit,Bybit,{_HASH_BTC},,DW Crypto deposit\n"
    f"Bitcoin (BTC),2021-02-24 12:59:25,送金出金,5394100,0.04887266,263624,BitMEX,Bybit,BitMEX,{_HASH_BTC},,DW Crypto deposit\n"
    "Bitcoin (BTC),2021-02-01 12:00:59,入金,3531315,0.001,3531,Unknown,Bybit,Bybit,"
    "ef934d59-2626-4392-a2d3-85fdc853884e-manual,価格調整,\n"
    "Bitcoin (BTC),2021-02-01 13:00:00,エアドロップ,3531315,0.5,1765657,Unknown,Bybit,Bybit,77777777,,\n"
)

_REPORT = _PREAMBLE + _HEADER + _ROWS


def _write(tmp_path: Path, content: str, name: str = "summ.csv") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _load(tmp_path: Path, source_id: str = "bybit", content: str = _REPORT):
    src = SummReportCsvSource(source_id)
    return src, src.load(_write(tmp_path, content))


def _net(txs) -> dict[str, Decimal]:
    bal: dict[str, Decimal] = {}
    for t in txs:
        for asset, amount in ((t.received_asset, t.received_amount),
                              (t.sent_asset, -(t.sent_amount or 0)),
                              (t.fee_asset, -(t.fee_amount or 0))):
            if asset and amount:
                bal[asset] = bal.get(asset, Decimal(0)) + amount
    return {a: v for a, v in bal.items() if v != 0}


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------

def test_registered_as_exchange():
    assert EXCHANGE_SOURCES["summ"] is SummReportCsvSource


def test_only_the_selected_account_and_balances(tmp_path):
    src, txs = _load(tmp_path)
    assert all(t.source == "bybit" and t.id.startswith("summ:") for t in txs)
    assert len({t.id for t in txs}) == len(txs)
    # 送金の相手側（Nexo / MetaMask / BitMEX）の行は入らない。
    # SOL（購入 20.508 − 売却 10 − 出金 10.5 − 手数料 0.008）と
    # POL（購入 2 − 出金 1.8 − 手数料 0.2。重複した送金は 1 回だけ）はちょうど 0
    assert _net(txs) == {
        "USDT": Decimal("181.8") - Decimal("0.1818") - Decimal("19.3211"),
        "BTC": (Decimal("0.004") - Decimal("0.00202") + Decimal("0.0000002")
                + Decimal("0.00000126") + Decimal("0.00014182") + Decimal("0.04887266")
                + Decimal("0.001")),
        "ETH": Decimal("0.5") - Decimal("0.12") - Decimal("0.00009"),
        "USDC": Decimal("1500") - Decimal("1.5"),
    }
    assert src.skip_reasons == {
        "SUMM で「無視」に分類された取引": 1,
        "SUMM のレポート内で重複した送金": 1,
        "未対応の取引タイプ: エアドロップ": 1,
    }


def test_times_are_converted_from_report_timezone(tmp_path):
    _, txs = _load(tmp_path)
    usdt_out = next(t for t in txs if t.type == TxType.WITHDRAW and t.sent_asset == "USDT")
    assert usdt_out.timestamp == _utc(2026, 3, 2, 1, 30)  # 10:30 JST


def test_timezone_falls_back_to_fixed_offset(tmp_path, monkeypatch):
    """tzdata の無い環境でも、夏時間の無い主なタイムゾーンは時差で読める。"""
    def no_zoneinfo(name):
        raise summ_report.ZoneInfoNotFoundError(name)
    monkeypatch.setattr(summ_report, "ZoneInfo", no_zoneinfo)
    _, txs = _load(tmp_path)
    usdt_out = next(t for t in txs if t.type == TxType.WITHDRAW and t.sent_asset == "USDT")
    assert usdt_out.timestamp == _utc(2026, 3, 2, 1, 30)


def test_trades_combine_buy_sell_and_fee_legs(tmp_path):
    _, txs = _load(tmp_path)
    trades = sorted((t for t in txs if t.type == TxType.TRADE), key=lambda t: t.timestamp)
    assert [(t.received_asset, t.received_amount, t.sent_asset, t.sent_amount,
             t.fee_asset, t.fee_amount) for t in trades] == [
        ("POL", Decimal("2"), "ETH", Decimal("0.00009"), None, None),
        ("USDC", Decimal("1500"), "SOL", Decimal("10.0"), "USDC", Decimal("1.5")),
        ("USDT", Decimal("181.8"), "BTC", Decimal("0.00202"), "USDT", Decimal("0.1818")),
    ]


def test_withdrawals_carry_fee_and_tx_hash(tmp_path):
    _, txs = _load(tmp_path)
    out = {t.sent_asset: t for t in txs if t.type == TxType.WITHDRAW}
    assert (out["SOL"].sent_amount, out["SOL"].fee_asset, out["SOL"].fee_amount) == (
        Decimal("10.5"), "SOL", Decimal("0.008"))
    assert out["SOL"].tx_hash == _HASH_SOL
    # SUMM 内の重複送金は 1 件にし、早い方（手数料の付いた取引所側）を残す
    assert (out["POL"].sent_amount, out["POL"].fee_amount) == (Decimal("1.8"), Decimal("0.2"))
    assert out["POL"].timestamp == _utc(2026, 2, 10, 1, 38, 48)
    assert out["USDT"].tx_hash == _HASH_USDT


def test_deposits_and_single_legs(tmp_path):
    _, txs = _load(tmp_path)
    deposits = {(t.received_asset, t.received_amount): t for t in txs if t.type == TxType.DEPOSIT}
    # 取引所内の ID は Tx ハッシュとして扱わない
    assert deposits[("BTC", Decimal("0.004"))].tx_hash is None
    assert deposits[("BTC", Decimal("0.04887266"))].tx_hash == _HASH_BTC
    # 相方の無い購入は入金として残す。手動入力はコメントをラベルにする
    assert deposits[("SOL", Decimal("20.508"))].label == "purchase"
    assert deposits[("BTC", Decimal("0.001"))].label == "価格調整"

    labels = {(t.type, t.label) for t in txs}
    assert {(TxType.FEE, "futures_realized_loss"), (TxType.REWARD, "futures_realized_profit"),
            (TxType.REWARD, "staking"), (TxType.REWARD, "income")} <= labels


def test_multiple_accounts_require_source_id(tmp_path):
    with pytest.raises(ValueError, match="「ソースID」") as e:
        _load(tmp_path, source_id="summ")
    assert "Bybit（" in str(e.value) and "Nexo（" in str(e.value)


@pytest.mark.parametrize("source_id", ["Bybit", "bybit", "bybit_main"])
def test_account_name_matching(tmp_path, source_id):
    _, txs = _load(tmp_path, source_id=source_id)
    assert txs and all(t.source == source_id for t in txs)


def test_single_account_report_ignores_source_id(tmp_path):
    """口座が 1 つだけのレポートは、ソースID の名前を問わず取り込む。"""
    bybit_only = "".join(
        line + "\n" for line in _ROWS.splitlines() if line.split(",")[8] == "Bybit")
    _, txs = _load(tmp_path, source_id="anything", content=_PREAMBLE + _HEADER + bybit_only)
    assert txs and all(t.source == "anything" for t in txs)


def test_ids_are_stable(tmp_path):
    _, a = _load(tmp_path)
    _, b = _load(tmp_path)
    assert [t.id for t in a] == [t.id for t in b]


def test_not_a_summ_report(tmp_path):
    p = _write(tmp_path, "timestamp,type,received_asset\n2024-01-01T00:00:00Z,deposit,BTC\n")
    with pytest.raises(ValueError, match="SUMM の取引レポート"):
        SummReportCsvSource("bybit").load(p)


# ---------------------------------------------------------------------------
# SUMM が覆う期間の扱い
# ---------------------------------------------------------------------------

_UNIVERSAL = (
    "timestamp,type,received_asset,received_amount,sent_asset,sent_amount,fee_asset,fee_amount,note\n"
    "2026-02-15T00:00:00Z,deposit,BTC,1,,,,,inside\n"    # レポートの期間内
    "2026-04-01T00:00:00Z,deposit,BTC,2,,,,,after\n"     # レポートより後
)


def _import(ledger: Ledger, tmp_path: Path, exchange: str, content: str, name: str):
    src = EXCHANGE_SOURCES[exchange]("bybit")
    txs = src.reconcile(src.load(_write(tmp_path, content, name)), ledger)
    ledger.upsert_many(txs)
    return src


def _manual(ts: datetime) -> CanonicalTx:
    return CanonicalTx(id="manual:keep", source="bybit", timestamp=ts,
                       type=TxType.DEPOSIT, received_asset="ETH", received_amount=Decimal("3"))


def test_summ_replaces_other_imports_in_its_period(tmp_path):
    ledger = Ledger(tmp_path / "t.db")
    _import(ledger, tmp_path, "universal", _UNIVERSAL, "u.csv")
    ledger.upsert(_manual(_utc(2026, 2, 16)))

    summ = _import(ledger, tmp_path, "summ", _REPORT, "s.csv")
    assert summ.replaced == 1  # 期間内の 2026-02-15 の行だけ
    notes = {t.label for t in ledger.all(source="bybit", limit=None)}
    assert "after" in notes and "inside" not in notes
    # 手動で追加した取引は残す
    assert any(t.id == "manual:keep" for t in ledger.all(source="bybit", limit=None))
    ledger.close()


def test_later_imports_skip_the_summ_period(tmp_path):
    ledger = Ledger(tmp_path / "t.db")
    _import(ledger, tmp_path, "summ", _REPORT, "s.csv")
    uni = _import(ledger, tmp_path, "universal", _UNIVERSAL, "u.csv")
    assert uni.skipped == 1
    [reason] = uni.skip_reasons
    assert reason.startswith("SUMM の取引レポートがある期間（2021-02-01〜2026-03-02）")
    notes = {t.label for t in ledger.all(source="bybit", limit=None)}
    assert "after" in notes and "inside" not in notes
    ledger.close()


def test_result_does_not_depend_on_import_order(tmp_path):
    def state(order):
        ledger = Ledger(tmp_path / f"{'-'.join(order)}.db")
        for exchange in order:
            content = _REPORT if exchange == "summ" else _UNIVERSAL
            _import(ledger, tmp_path, exchange, content, f"{exchange}.csv")
        ids = sorted(t.id for t in ledger.all(source="bybit", limit=None))
        bals = ledger.balances("bybit")
        ledger.close()
        return ids, bals

    assert state(("summ", "universal")) == state(("universal", "summ"))


def test_reimport_replaces_rows_fixed_in_summ(tmp_path):
    """SUMM で分類を直したレポートを入れ直すと、古い形の取引は残らない。

    購入 → 入金（送金元とつなげた）に直すと id が変わるため、前の取り込み分を
    消さないと SOL が二重になる。
    """
    ledger = Ledger(tmp_path / "t.db")
    _import(ledger, tmp_path, "summ", _REPORT, "a.csv")
    gmo_hash = "4Gmo" + "y" * 84
    fixed = _REPORT.replace(
        "Solana (SOL),2026-02-20 10:49:42,購入,19000,20.508,389652,Unknown,Bybit,Bybit,228000001,,",
        f"Solana (SOL),2026-02-20 10:49:42,入金,19000,20.508,389652,GMO Coin,Bybit,Bybit,{gmo_hash},,",
    )
    assert fixed != _REPORT
    summ = _import(ledger, tmp_path, "summ", fixed, "b.csv")

    sol_in = [t for t in ledger.all(source="bybit", tx_type="deposit", limit=None)
              if t.received_asset == "SOL"]
    assert [(t.received_amount, t.label, t.tx_hash) for t in sol_in] == [
        (Decimal("20.508"), None, gmo_hash)]
    assert summ.replaced == 1
    _, expected = _load(tmp_path, content=fixed)
    assert sorted(t.id for t in ledger.all(source="bybit", limit=None)) == sorted(
        t.id for t in expected)
    ledger.close()


def test_narrower_report_keeps_summ_rows_outside_it(tmp_path):
    """期間の狭いレポートを入れても、その期間の外の SUMM の取引は消さない。"""
    ledger = Ledger(tmp_path / "t.db")
    _import(ledger, tmp_path, "summ", _REPORT, "a.csv")
    before = sorted(t.id for t in ledger.all(source="bybit", limit=None))
    only_2021 = _PREAMBLE + _HEADER + "".join(
        line + "\n" for line in _ROWS.splitlines() if line.split(",")[1].startswith("2021"))
    summ = _import(ledger, tmp_path, "summ", only_2021, "b.csv")
    assert summ.replaced == 0
    assert sorted(t.id for t in ledger.all(source="bybit", limit=None)) == before
    ledger.close()


def test_bybit_csv_inside_summ_period_is_skipped(tmp_path):
    """Bybit の CSV も SUMM の期間の行は飛ばす（入出金のまとめより前に）。"""
    from tests.test_bybit_csv import _DW

    ledger = Ledger(tmp_path / "t.db")
    _import(ledger, tmp_path, "summ", _REPORT, "s.csv")
    bybit = _import(ledger, tmp_path, "bybit", _DW, "dw.csv")
    # 2026-03-01〜02 の入出金はすべて SUMM の期間内（失敗した出金はもともと対象外）
    assert sum(n for r, n in bybit.skip_reasons.items() if r.startswith("SUMM")) == 3
    assert not [t for t in ledger.all(source="bybit", limit=None) if not t.id.startswith("summ:")]
    ledger.close()


def test_ledger_helpers_for_prefixed_ids(tmp_path):
    ledger = Ledger(tmp_path / "t.db")
    assert ledger.id_prefix_time_range("bybit", "summ:") is None
    for i, (tid, ts) in enumerate((("summ:a", _utc(2026, 1, 1)), ("summ:b", _utc(2026, 1, 3)),
                                   ("x1", _utc(2026, 1, 2)), ("manual:m", _utc(2026, 1, 2)))):
        ledger.upsert(CanonicalTx(id=tid, source="bybit", timestamp=ts, type=TxType.DEPOSIT,
                                  received_asset="BTC", received_amount=Decimal(i + 1)))
    assert ledger.id_prefix_time_range("bybit", "summ:") == (_utc(2026, 1, 1), _utc(2026, 1, 3))
    deleted = ledger.delete_by_source_window(
        ["bybit"], _utc(2025, 12, 31), _utc(2026, 1, 4), keep_prefixes=("summ:",))
    assert deleted == 1
    assert sorted(t.id for t in ledger.all(limit=None)) == ["manual:m", "summ:a", "summ:b"]
    ledger.close()


# ---------------------------------------------------------------------------
# CLI / Web からの取り込み
# ---------------------------------------------------------------------------

def test_cli_import(tmp_path):
    from click.testing import CliRunner

    from crypto_summary.cli import cli

    db, report = tmp_path / "t.db", _write(tmp_path, _REPORT)
    run = lambda *extra: CliRunner().invoke(  # noqa: E731
        cli, ["--db", str(db), "import", "--file", str(report), "--exchange", "summ", *extra])

    res = run()
    assert res.exit_code != 0
    assert "--source-id" in res.output and "Traceback" not in res.output

    res = run("--source-id", "bybit")
    assert res.exit_code == 0, res.output
    assert "SUMM で「無視」に分類された取引" in res.output
    ledger = Ledger(db)
    assert ledger.count("bybit") > 0
    ledger.close()


def _b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def test_web_import(tmp_path):
    from crypto_summary.web import app as web_app

    client = TestClient(web_app.create_app(str(tmp_path / "web.db")))
    labels = {e["value"]: e["label"] for e in client.get("/api/import/exchanges").json()["exchanges"]}
    assert "SUMM" in labels["summ"]

    def post(exchange: str, content: str, account: str | None) -> dict:
        r = client.post("/api/import/csv", json={
            "exchange": exchange, "filename": f"{exchange}.csv",
            "account": account, "content_b64": _b64(content),
        })
        return r

    # 口座が複数あるレポートは、ソースID を入れないと取り込めない
    r = post("summ", _REPORT, None)
    assert r.status_code == 422 and "ソースID" in r.json()["detail"]

    assert post("universal", _UNIVERSAL, "bybit").status_code == 200
    d = post("summ", _REPORT, "bybit").json()
    assert d["source"] == "bybit" and d["replaced"] == 1 and d["skipped"] == 3

    # 期間内の行だけの CSV は、取り込む取引なしで返る（エラーにしない）
    inside_only = _UNIVERSAL.split("\n")[0] + "\n" + _UNIVERSAL.split("\n")[1] + "\n"
    d2 = post("universal", inside_only, "bybit").json()
    assert d2["parsed"] == 0 and d2["skipped"] == 1
