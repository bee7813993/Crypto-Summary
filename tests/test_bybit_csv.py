"""BybitCsvSource（資金調達アカウント履歴 / UTA 取引ログ / 入出金履歴）のテスト。

フィクスチャは実際のエクスポートと同じ書式（前置き行・新しい順・桁の長い小数・
指数表記・該当なしの "--"・同じ秒の中で前後する行）を写したもの。UID・Tx ID・
アドレス・金額はすべて架空の値で、各行の残高列は前後の行と整合させてある。
"""
import base64
import itertools
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sinks.summ_csv import to_summ_rows
from crypto_summary.sources.bybit_csv import BybitCsvSource
from crypto_summary.sources.csv_import import EXCHANGE_SOURCES

_PREAMBLE = "UID: 1234567,Company Name: ,Country: \n"

_FUND = _PREAMBLE + (
    "Uid,Date & Time(UTC),Coin,QTY,Type,Account Balance,Description\n"
    "1234567,2026-03-02 01:35:01,USDT,-1300.000000000000000000,Withdraw,0.000000000000000000,Withdrawal\n"
    "1234567,2026-03-02 01:30:01,USDT,-19.321100000000000000,Withdraw,1300.000000000000000000,Withdrawal\n"
    "1234567,2026-03-02 01:12:01,MNT,5.5E-7,Convert,0.000000550000000000,Small Balance Conversion\n"
    "1234567,2026-03-02 01:12:00,SOL,-4.000000000E-9,Convert,0.000000000000000000,Small Balance Conversion\n"
    "1234567,2026-03-02 01:11:01,USDT,1319.321100000000000000,Transfer in,1319.321100000000000000,Transfer from Unified Trading Account\n"
    "1234567,2026-03-02 01:01:01,BTC,-0.002020410000000000,Transfer out,0.000000000000000000,Transfer to Unified Trading Account\n"
    "1234567,2026-03-02 01:00:30,BTC,1.0000000000E-8,Earn,0.002020410000000000,Easy Earn | Flexible Interest Distribution\n"
    "1234567,2026-03-02 01:00:30,BTC,0.002000000000000000,Earn,0.002020400000000000,Easy Earn | Flexible Redemption\n"
    "1234567,2026-03-02 00:30:00,BTC,2.00000000000E-7,Earn,0.000020400000000000,Easy Earn | Flexible Interest Distribution\n"
    "1234567,2026-03-01 10:00:40,USDC,500.000000000000000000,Deposit,500.000000000000000000,Deposit\n"
    "1234567,2026-03-01 00:30:00,BTC,2.00000000000E-7,Earn,0.000020200000000000,Easy Earn | Flexible Interest Distribution\n"
)

_UTA_HEADER = (
    "Uid,Currency,Contract,Type,Direction,Quantity,Position,Filled Price,Funding,"
    "Fee Paid,Cash Flow,Change,Wallet Balance,Action,Time(UTC)\n"
)
_Z = "0.00000000000000000000"

_UTA = _PREAMBLE + _UTA_HEADER + (
    f"1234567,USDT,,TRANSFER_OUT,--,{_Z},{_Z},{_Z},{_Z},{_Z},-1319.32110000000000000000,-1319.32110000000000000000,0.00009338800000000000,--,2026-03-02 01:11:00\n"
    # 少額資産の両替（種別 "--"）。同じ秒の中では行の並びが適用順と一致しない
    f"1234567,USDT,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},1.80420000000000000000,1.80420000000000000000,1319.32119338800000000000,--,2026-03-02 01:10:01\n"
    f"1234567,ETH,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},-0.00062000000000000000,-0.00062000000000000000,{_Z},--,2026-03-02 01:10:01\n"
    f"1234567,USDT,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},0.03579300000000000000,0.03579300000000000000,1317.51699300000000000000,--,2026-03-02 01:10:00\n"
    f"1234567,USDT,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},0.00000038800000000000,0.00000038800000000000,1317.51699338800000000000,--,2026-03-02 01:10:00\n"
    f"1234567,USDC,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},-0.00000040000000000000,-0.00000040000000000000,{_Z},--,2026-03-02 01:10:00\n"
    f"1234567,BTC,,--,--,{_Z},{_Z},{_Z},{_Z},{_Z},-0.00000041000000000000,-0.00000041000000000000,0.00000000100000000000,--,2026-03-02 01:10:00\n"
    # 現物の売り（基軸通貨と決済通貨の 2 行）
    f"1234567,USDT,BTCUSDT,TRADE,SELL,181.80000000000000000000,{_Z},90000.00000000000000000000,{_Z},-0.18180000000000000000,181.80000000000000000000,181.61820000000000000000,1317.48120000000000000000,--,2026-03-02 01:06:00\n"
    f"1234567,BTC,BTCUSDT,TRADE,SELL,-0.00202000000000000000,{_Z},90000.00000000000000000000,{_Z},{_Z},-0.00202000000000000000,-0.00202000000000000000,0.00000041100000000000,--,2026-03-02 01:06:00\n"
    f"1234567,ETH,ETHUSDT,TRADE,SELL,-0.37900000000000000000,{_Z},3000.00000000000000000000,{_Z},{_Z},-0.37900000000000000000,-0.37900000000000000000,0.00062000000000000000,--,2026-03-02 01:05:00\n"
    f"1234567,USDT,ETHUSDT,TRADE,SELL,1137.00000000000000000000,{_Z},3000.00000000000000000000,{_Z},-1.13700000000000000000,1137.00000000000000000000,1135.86300000000000000000,1135.86300000000000000000,--,2026-03-02 01:05:00\n"
    f"1234567,BTC,,TRANSFER_IN,--,{_Z},{_Z},{_Z},{_Z},{_Z},0.00202041000000000000,0.00202041000000000000,0.00202041100000000000,--,2026-03-02 01:01:00\n"
    # 逆数無期限（ETHUSD・ETH 建て）の決済
    f"1234567,ETH,ETHUSD,TRADE,SELL,1500.00000000000000000000,{_Z},3000.00000000000000000000,{_Z},-0.00024000000000000000,-0.09000000000000000000,-0.09024000000000000000,0.37962000000000000000,CLOSE,2026-03-02 01:00:00\n"
    f"1234567,ETH,ETHUSD,TRADE,SELL,500.00000000000000000000,1500.00000000000000000000,3000.00000000000000000000,{_Z},-0.00008000000000000000,-0.03000000000000000000,-0.03008000000000000000,0.46986000000000000000,CLOSE,2026-03-02 01:00:00\n"
    # 資金調達料
    f"1234567,ETH,ETHUSD,SETTLEMENT,BUY,--,2000.00000000000000000000,--,0.00004000000000000000,{_Z},{_Z},0.00004000000000000000,0.49994000000000000000,SETTLEMENT,2026-03-01 08:00:00\n"
    f"1234567,ETH,ETHUSD,SETTLEMENT,BUY,--,2000.00000000000000000000,--,-0.00010000000000000000,{_Z},{_Z},-0.00010000000000000000,0.49990000000000000000,SETTLEMENT,2026-03-01 00:00:00\n"
)

_DW = _PREAMBLE + (
    "Uid,Date,Type,Asset,Chain,Amount,Tx ID,Status,Received Address\n"
    "1234567,2026-03-02 01:40:00,Withdraw,USDT,TRX,25.000000000000000000,--,Failed,TXyz0000000000000000000000000000000\n"
    "1234567,2026-03-02 01:35:00,Withdraw,USDT,PLASMA,1300.000000000000000000,0xbbbb000000000000000000000000000000000000000000000000000000000002,Transferred successfully,0x1111000000000000000000000000000000000001\n"
    "1234567,2026-03-02 01:30:00,Withdraw,USDT,PLASMA,19.321100000000000000,0xaaaa000000000000000000000000000000000000000000000000000000000001,Transferred successfully,0x1111000000000000000000000000000000000001\n"
    "1234567,2026-03-01 10:00:00,Deposit,USDC,SOL,500.000000000000000000,5Hq1111111111111111111111111111111111111111111111111111111111111111111111111111111111,Completed,9Fx2222222222222222222222222222222222222222\n"
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _load(tmp_path: Path, name: str, content: str) -> tuple[BybitCsvSource, list[CanonicalTx]]:
    src = BybitCsvSource("bybit")
    return src, src.load(_write(tmp_path, name, content))


def _net(txs) -> dict[str, Decimal]:
    """取引が残高に与える効果（受取 − 送出 − 手数料）を資産別に合計する。"""
    bal: dict[str, Decimal] = {}
    for t in txs:
        for asset, amount in ((t.received_asset, t.received_amount),
                              (t.sent_asset, -(t.sent_amount or 0)),
                              (t.fee_asset, -(t.fee_amount or 0))):
            if asset and amount:
                bal[asset] = bal.get(asset, Decimal(0)) + amount
    return {a: v for a, v in bal.items() if v != 0}


def _balance_deltas(content: str, coin_col: str, qty_col: str, bal_col: str) -> dict[str, Decimal]:
    """CSV の残高列から、期間中の資産別の増減（最後の残高 − 最初の行の前の残高）を求める。"""
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(content.split("\n", 1)[1])))
    first: dict[str, Decimal] = {}
    last: dict[str, Decimal] = {}
    for r in rows:  # 新しい順
        coin = r[coin_col]
        last.setdefault(coin, Decimal(r[bal_col]))
        first[coin] = Decimal(r[bal_col]) - Decimal(r[qty_col])
    deltas = {c: last[c] - first[c] for c in last}
    return {c: v for c, v in deltas.items() if v != 0}


# ---------------------------------------------------------------------------
# 形式の判定
# ---------------------------------------------------------------------------

def test_registered_as_exchange():
    assert EXCHANGE_SOURCES["bybit"] is BybitCsvSource


def test_detects_each_format(tmp_path):
    _, fund = _load(tmp_path, "AssetChangeDetails_fund.csv", _FUND)
    _, uta = _load(tmp_path, "AssetChangeDetails_uta.csv", _UTA)
    _, dw = _load(tmp_path, "withdrawDepositHistory.csv", _DW)
    assert {t.type for t in fund} >= {TxType.WITHDRAW, TxType.TRANSFER, TxType.REWARD}
    assert {t.type for t in uta} >= {TxType.TRADE, TxType.FEE, TxType.TRANSFER}
    assert {t.type for t in dw} == {TxType.DEPOSIT, TxType.WITHDRAW}
    assert all(t.source == "bybit" for t in fund + uta + dw)


def test_reads_without_preamble_and_with_bom_crlf(tmp_path):
    """前置き行が無くても、BOM 付き・CRLF でも同じ結果になる。"""
    _, plain = _load(tmp_path, "a.csv", _UTA)
    p = tmp_path / "b.csv"
    p.write_bytes(("﻿" + _UTA.split("\n", 1)[1]).replace("\n", "\r\n").encode("utf-8"))
    other = BybitCsvSource("bybit").load(p)
    assert sorted(t.id for t in other) == sorted(t.id for t in plain)


def test_unknown_format_raises(tmp_path):
    p = _write(tmp_path, "binance.csv",
               "Date(UTC),Pair,Side,Price,Executed,Amount,Fee\n"
               "2024-01-01 00:00:00,BTCUSDT,BUY,1,1 BTC,1 USDT,0 BTC\n")
    with pytest.raises(ValueError, match="判別できませんでした"):
        BybitCsvSource("bybit").load(p)


def test_ids_are_stable(tmp_path):
    for name, content in (("f.csv", _FUND), ("u.csv", _UTA), ("d.csv", _DW)):
        _, a = _load(tmp_path, name, content)
        _, b = _load(tmp_path, name, content)
        assert [t.id for t in a] == [t.id for t in b]
        assert len({t.id for t in a}) == len(a)


# ---------------------------------------------------------------------------
# UTA 取引ログ
# ---------------------------------------------------------------------------

def test_uta_reproduces_wallet_balance(tmp_path):
    """各行の効果の合計が、Wallet Balance 列の増減と一致する。"""
    src, txs = _load(tmp_path, "uta.csv", _UTA)
    assert src.skipped == 0
    assert _net(txs) == _balance_deltas(_UTA, "Currency", "Change", "Wallet Balance")


def test_uta_spot_trades_pair_both_legs(tmp_path):
    _, txs = _load(tmp_path, "uta.csv", _UTA)
    spot = sorted((t for t in txs if t.type == TxType.TRADE and t.label is None),
                  key=lambda t: t.timestamp)
    assert [(t.sent_asset, t.sent_amount, t.received_asset, t.received_amount,
             t.fee_asset, t.fee_amount) for t in spot] == [
        ("ETH", Decimal("0.379"), "USDT", Decimal("1137"), "USDT", Decimal("1.137")),
        ("BTC", Decimal("0.00202"), "USDT", Decimal("181.8"), "USDT", Decimal("0.1818")),
    ]
    # 現物の売却額を先物の損益と取り違えない
    assert not any((t.label or "").startswith("futures_") and t.timestamp.minute in (5, 6)
                   for t in txs)


def test_uta_derivatives_are_pnl_based(tmp_path):
    """先物の決済は実現損益と手数料、資金調達料は符号で REWARD / FEE。"""
    _, txs = _load(tmp_path, "uta.csv", _UTA)
    by_label: dict[str, list[CanonicalTx]] = {}
    for t in txs:
        by_label.setdefault(t.label or "", []).append(t)

    loss = by_label["futures_realized_loss"]
    assert sorted(t.fee_amount for t in loss) == [Decimal("0.03"), Decimal("0.09")]
    assert {t.type for t in loss} == {TxType.FEE}
    fees = by_label["futures_fee"]
    assert sorted(t.fee_amount for t in fees) == [Decimal("0.00008"), Decimal("0.00024")]
    assert {t.fee_asset for t in loss + fees} == {"ETH"}

    assert [t.fee_amount for t in by_label["futures_funding_loss"]] == [Decimal("0.0001")]
    gain = by_label["futures_funding_profit"]
    assert [(t.type, t.received_amount) for t in gain] == [(TxType.REWARD, Decimal("0.00004"))]


def test_uta_small_balance_conversions_pair_by_currency(tmp_path):
    """種別 "--" の両替は減った通貨と増えた通貨を組にする（同じ秒に複数通貨あり）。"""
    _, txs = _load(tmp_path, "uta.csv", _UTA)
    conv = {t.sent_asset: (t.received_asset, t.received_amount, t.sent_amount)
            for t in txs if t.label == "convert"}
    assert conv == {
        "BTC": ("USDT", Decimal("0.035793"), Decimal("0.00000041")),
        "USDC": ("USDT", Decimal("3.88E-7"), Decimal("4E-7")),
        "ETH": ("USDT", Decimal("1.8042"), Decimal("0.00062")),
    }


def test_uta_unknown_type_is_skipped_with_reason(tmp_path):
    content = _PREAMBLE + _UTA_HEADER + (
        f"1234567,USDT,,SOMETHING_NEW,--,{_Z},{_Z},{_Z},{_Z},{_Z},1.0,1.0,1.0,--,2026-03-01 00:00:00\n"
        f"1234567,USDT,,FLEXIBLE_STAKING_SUBSCRIPTION,--,{_Z},{_Z},{_Z},{_Z},{_Z},-1.0,-1.0,0,--,2026-03-01 00:00:01\n"
    )
    src, txs = _load(tmp_path, "uta.csv", content)
    assert txs == []
    assert src.skip_reasons == {
        "未対応の種別: SOMETHING_NEW": 1,
        "Earn 等の申込・解約（元本の移動）": 1,
    }


# ---------------------------------------------------------------------------
# 資金調達アカウント履歴
# ---------------------------------------------------------------------------

def test_fund_reproduces_balance_except_earn_principal(tmp_path):
    """Earn の解約（元本の戻り）だけを除いて Account Balance の増減と一致する。

    運用中の元本も保有として数え続ける設計のため、申込・解約は記録しない。
    """
    src, txs = _load(tmp_path, "fund.csv", _FUND)
    expected = _balance_deltas(_FUND, "Coin", "QTY", "Account Balance")
    expected["BTC"] -= Decimal("0.002")  # Easy Earn | Flexible Redemption
    assert _net(txs) == expected
    assert src.skip_reasons == {"Earn 等の申込・解約（元本の移動）": 1}


def test_fund_earn_interest_is_reward(tmp_path):
    _, txs = _load(tmp_path, "fund.csv", _FUND)
    interest = [t for t in txs if t.label == "earn_interest"]
    assert len(interest) == 3
    assert {t.type for t in interest} == {TxType.REWARD}
    assert sum(t.received_amount for t in interest) == Decimal("4.1E-7")


def test_fund_small_balance_conversion_spans_one_second(tmp_path):
    _, txs = _load(tmp_path, "fund.csv", _FUND)
    conv = [t for t in txs if t.type == TxType.TRADE]
    assert [(t.sent_asset, t.sent_amount, t.received_asset, t.received_amount, t.label)
            for t in conv] == [
        ("SOL", Decimal("4E-9"), "MNT", Decimal("5.5E-7"), "small_balance_conversion"),
    ]


def test_transfers_are_recorded_on_both_sides_and_cancel(tmp_path):
    """資金調達⇔UTA の振替は両側に TRANSFER で残り、同じ口座では相殺される。"""
    _, fund = _load(tmp_path, "fund.csv", _FUND)
    _, uta = _load(tmp_path, "uta.csv", _UTA)
    transfers = [t for t in fund + uta if t.type == TxType.TRANSFER]
    assert {t.label for t in transfers} == {"account_transfer"}
    assert len(transfers) == 4
    assert _net(transfers) == {}


def test_fund_unknown_type_is_skipped_with_reason(tmp_path):
    content = _PREAMBLE + (
        "Uid,Date & Time(UTC),Coin,QTY,Type,Account Balance,Description\n"
        "1234567,2026-03-01 00:00:00,USDT,5.0,P2P,5.0,P2P Buy\n"
        "1234567,2026-03-01 00:00:01,USDT,1.0,Airdrop,6.0,Airdrop\n"
    )
    src, txs = _load(tmp_path, "fund.csv", content)
    assert [(t.type, t.label, t.received_amount) for t in txs] == [
        (TxType.REWARD, "airdrop", Decimal("1"))
    ]
    assert src.skip_reasons == {"未対応の種別: P2P（P2P Buy）": 1}


# ---------------------------------------------------------------------------
# 入出金履歴
# ---------------------------------------------------------------------------

def test_deposit_withdraw_history(tmp_path):
    src, txs = _load(tmp_path, "dw.csv", _DW)
    assert sorted((t.type, t.received_asset or t.sent_asset,
                   t.received_amount or t.sent_amount) for t in txs) == [
        (TxType.DEPOSIT, "USDC", Decimal("500")),
        (TxType.WITHDRAW, "USDT", Decimal("19.3211")),
        (TxType.WITHDRAW, "USDT", Decimal("1300")),
    ]
    assert all(t.tx_hash for t in txs)
    # 失敗した出金は残高に影響しないので記録しない
    assert src.skip_reasons == {"未完了の入出金（Failed）": 1}


# ---------------------------------------------------------------------------
# 資金調達アカウント履歴と入出金履歴の突き合わせ
# ---------------------------------------------------------------------------

_FILES = {"fund": _FUND, "uta": _UTA, "dw": _DW}


def _import(ledger: Ledger, tmp_path: Path, name: str) -> list[CanonicalTx]:
    src = BybitCsvSource("bybit")
    txs = src.load(_write(tmp_path, f"{name}.csv", _FILES[name]))
    txs = src.reconcile(txs, ledger)
    ledger.upsert_many(txs)
    return txs


@pytest.mark.parametrize("order", list(itertools.permutations(_FILES)))
def test_all_files_in_any_order_count_each_event_once(tmp_path, order):
    """どの順に取り込んでも、取り込み直しても、入出金は 1 件ずつにまとまる。"""
    ledger = Ledger(tmp_path / "t.db")
    for name in order + order:
        _import(ledger, tmp_path, name)

    _, fund = _load(tmp_path, "f.csv", _FUND)
    _, uta = _load(tmp_path, "u.csv", _UTA)
    assert ledger.count("bybit") == len(fund) + len(uta)
    # 残高は資金調達アカウント＋UTA の効果どおり（入出金履歴の分が二重にならない）
    assert {a: v for a, v in ledger.balances().items() if v != 0} == _net(fund + uta)

    moves = ledger.all(tx_type="withdraw", limit=None) + ledger.all(tx_type="deposit", limit=None)
    assert len(moves) == 3
    # 金額は資金調達側、Tx ID は入出金履歴側
    assert sorted((t.received_amount or t.sent_amount, t.tx_hash[:6]) for t in moves) == [
        (Decimal("19.3211"), "0xaaaa"),
        (Decimal("500"), "5Hq111"),
        (Decimal("1300"), "0xbbbb"),
    ]
    ledger.close()


def _fund_withdraw(ts: str, coin: str, qty: str) -> str:
    return _PREAMBLE + (
        "Uid,Date & Time(UTC),Coin,QTY,Type,Account Balance,Description\n"
        f"1234567,{ts},{coin},{qty},Withdraw,0,Withdrawal\n"
    )


def _dw_withdraw(ts: str, coin: str, amount: str, tx: str = "0xcccc") -> str:
    return _PREAMBLE + (
        "Uid,Date,Type,Asset,Chain,Amount,Tx ID,Status,Received Address\n"
        f"1234567,{ts},Withdraw,{coin},ETH,{amount},{tx},Transferred successfully,0x1\n"
    )


@pytest.mark.parametrize("fund_first", [True, False])
def test_withdrawal_fee_included_on_fund_side_still_merges(tmp_path, fund_first):
    """資金調達側が手数料込みで多くても、時刻が近ければ同じ出金としてまとめる。"""
    _FILES_LOCAL = {
        "fund": _fund_withdraw("2026-04-01 12:00:01", "ETH", "-0.501200000000000000"),
        "dw": _dw_withdraw("2026-04-01 12:00:00", "ETH", "0.500000000000000000"),
    }
    ledger = Ledger(tmp_path / "t.db")
    for name in (("fund", "dw") if fund_first else ("dw", "fund")):
        src = BybitCsvSource("bybit")
        txs = src.reconcile(src.load(_write(tmp_path, f"{name}.csv", _FILES_LOCAL[name])), ledger)
        ledger.upsert_many(txs)
    [w] = ledger.all(tx_type="withdraw", limit=None)
    assert w.sent_amount == Decimal("0.5012")
    assert w.tx_hash == "0xcccc"
    ledger.close()


def test_far_apart_withdrawals_are_not_merged(tmp_path):
    ledger = Ledger(tmp_path / "t.db")
    for name, content in (("fund", _fund_withdraw("2026-04-01 12:00:00", "USDT", "-50")),
                          ("dw", _dw_withdraw("2026-04-03 12:00:00", "USDT", "50"))):
        src = BybitCsvSource("bybit")
        ledger.upsert_many(src.reconcile(src.load(_write(tmp_path, f"{name}.csv", content)), ledger))
    assert len(ledger.all(tx_type="withdraw", limit=None)) == 2
    ledger.close()


def test_other_accounts_are_not_merged(tmp_path):
    """突き合わせは同じ口座（source）の中だけで行う。"""
    ledger = Ledger(tmp_path / "t.db")
    for sid, name, content in (
        ("bybit_a", "fund", _fund_withdraw("2026-04-01 12:00:01", "USDT", "-50")),
        ("bybit_b", "dw", _dw_withdraw("2026-04-01 12:00:00", "USDT", "50")),
    ):
        src = BybitCsvSource(sid)
        ledger.upsert_many(src.reconcile(src.load(_write(tmp_path, f"{name}.csv", content)), ledger))
    assert ledger.count("bybit_a") == 1 and ledger.count("bybit_b") == 1
    ledger.close()


# ---------------------------------------------------------------------------
# Web / CLI からの取り込み・エクスポート
# ---------------------------------------------------------------------------

def _b64(content: str) -> str:
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


def test_web_import_merges_and_batches_survive_deletion(tmp_path):
    from crypto_summary.web import app as web_app

    client = TestClient(web_app.create_app(str(tmp_path / "web.db")))
    labels = {e["value"]: e["label"] for e in client.get("/api/import/exchanges").json()["exchanges"]}
    assert "Bybit" in labels["bybit"]

    def post(name: str, content: str) -> dict:
        r = client.post("/api/import/csv", json={
            "exchange": "bybit", "filename": name, "content_b64": _b64(content),
        })
        assert r.status_code == 200, r.text
        return r.json()

    fund = post("AssetChangeDetails_fund.csv", _FUND)
    assert fund["source"] == "bybit"
    assert fund["skipped"] == 1
    dw = post("withdrawDepositHistory.csv", _DW)
    # 入出金はすべて資金調達側にまとまり、新しい取引は増えない
    assert dw["parsed"] == 3 and dw["imported"] == 0

    txs = client.get("/api/transactions?account=Bybit").json()
    assert txs["total"] == fund["parsed"]
    assert sum(1 for t in txs["transactions"] if t["tx_hash"]) == 3

    # 資金調達側の CSV を消しても、入出金履歴の CSV が覆う入出金は残る
    assert client.delete(f"/api/import/batches/{fund['batch_id']}").status_code == 200
    left = client.get("/api/transactions?account=Bybit").json()
    assert sorted(t["type"] for t in left["transactions"]) == ["deposit", "withdraw", "withdraw"]
    client.delete(f"/api/import/batches/{dw['batch_id']}")
    assert client.get("/api/transactions?account=Bybit").json()["total"] == 0


def test_cli_import_merges_and_reports_skips(tmp_path):
    from click.testing import CliRunner

    from crypto_summary.cli import cli

    db = tmp_path / "t.db"
    run = lambda name, content: CliRunner().invoke(  # noqa: E731
        cli, ["--db", str(db), "import", "--file", str(_write(tmp_path, name, content)),
              "--exchange", "bybit"])
    res = run("fund.csv", _FUND)
    assert res.exit_code == 0, res.output
    assert "Earn 等の申込・解約（元本の移動）" in res.output
    res = run("dw.csv", _DW)
    assert res.exit_code == 0, res.output
    assert "+0 new" in res.output

    ledger = Ledger(db)
    assert len(ledger.all(source="bybit", tx_type="withdraw", limit=None)) == 2
    ledger.close()


def test_summ_export_skips_account_transfers(tmp_path):
    _, fund = _load(tmp_path, "fund.csv", _FUND)
    rows = to_summ_rows(fund)
    assert not any(r["Type"] in ("send", "receive") and r["Base Currency"] in ("BTC",)
                   for r in rows)
    # 出金は send として出る（振替だけが落ちる）
    assert sum(1 for r in rows if r["Type"] == "send") == 2
