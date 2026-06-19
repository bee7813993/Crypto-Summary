"""日次残高スナップショット計算

台帳の取引履歴を時系列に走査して各日の資産残高を計算する。
CoinGecko API は叩かない（取引データのみ使用）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .ledger import Ledger
from .models import CanonicalTx


def _tx_date(tx: CanonicalTx) -> str:
    return tx.timestamp.date().isoformat()


def _apply_tx(tx: CanonicalTx, running: dict[str, Decimal]) -> None:
    ra, rv = tx.received_asset, tx.received_amount
    sa, sv = tx.sent_asset, tx.sent_amount
    fa, fv = tx.fee_asset, tx.fee_amount
    if ra and rv:
        running[ra] = running.get(ra, Decimal(0)) + rv
    if sa and sv:
        running[sa] = running.get(sa, Decimal(0)) - sv
    if fa and fv:
        running[fa] = running.get(fa, Decimal(0)) - fv


def daily_balances(
    ledger: Ledger,
    source: str | list[str] | None = None,
    asset: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, dict[str, Decimal]]:
    """日付ごとの資産残高スナップショットを返す。

    返り値: {"YYYY-MM-DD": {"ASSET": Decimal}}
      - 各日の値は「その日の終わりの時点の残高」（累積）。
      - 取引が存在しない日は前日の残高を引き継ぐ。
      - start/end 省略時は最初の取引日〜今日を対象とする。
      - asset 指定時はその資産のみ（マルチアセットポートフォリオの
        部分集合として使う場合、呼び出し側でフィルタする）。
    """
    # すべての取引を昇順で取得（limit=None で全件）
    txs, _ = ledger.transactions(source=source, asset=asset, limit=999_999)
    txs = sorted(txs, key=lambda t: t.timestamp)

    if not txs:
        return {}

    first_tx_date = txs[0].timestamp.date()
    today = date.today()
    range_start = start if start is not None else first_tx_date
    range_end = end if end is not None else today

    if range_start > range_end:
        return {}

    # 日付→取引リスト のマップを構築
    tx_by_date: dict[str, list[CanonicalTx]] = {}
    for tx in txs:
        d = _tx_date(tx)
        tx_by_date.setdefault(d, []).append(tx)

    # range_start より前の取引を先にすべて適用して開始残高を求める。
    # これがないと「当該期間の純増分」しかグラフに現れず、
    # 期間外の取引で形成された残高が丸ごと欠落する。
    running: dict[str, Decimal] = {}
    range_start_iso = range_start.isoformat()
    for tx in txs:
        if _tx_date(tx) >= range_start_iso:
            break
        _apply_tx(tx, running)

    # range_start〜range_end の日次スナップショットを計算
    result: dict[str, dict[str, Decimal]] = {}

    d = range_start
    while d <= range_end:
        iso = d.isoformat()
        for tx in tx_by_date.get(iso, []):
            _apply_tx(tx, running)

        # ゼロ残高は含めない（グラフのノイズになるため）
        snapshot = {k: v for k, v in running.items() if v != Decimal(0)}
        if snapshot:
            result[iso] = dict(snapshot)

        d += timedelta(days=1)

    return result


def assets_in_range(
    snapshots: dict[str, dict[str, Decimal]],
) -> set[str]:
    """スナップショット全体に登場する資産セットを返す。"""
    out: set[str] = set()
    for snap in snapshots.values():
        out.update(snap.keys())
    return out
