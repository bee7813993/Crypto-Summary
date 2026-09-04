"""bitFlyer CSV アダプタ

対象ファイル:
  TradeHistory.csv       : 現物の総合台帳（売買・入出金・手数料・証拠金移動）→ メイン
  CollateralHistory.csv  : 証拠金口座。FX/CFD の実現損益 → 「決済」のみ記録

スキップするファイル:
  Lightning_TradeHistory.csv : FX個別約定(実現損益で集計するため不要) + 現物はTradeHistoryと重複
  ConversionHistory.csv      : 別アダプタ(bitflyer_conversion)で対応

数値は "239,983" のようにカンマ区切り。符号で入出を判定。
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y/%m/%d %H:%M:%S"
_DATE_FMT_SHORT = "%Y/%m/%d"
_ZERO = Decimal("0")


def _d(value: str) -> Decimal:
    v = (value or "").strip().replace(",", "")
    if not v:
        return _ZERO
    try:
        return Decimal(v)
    except InvalidOperation:
        return _ZERO


def _parse_ts(value: str) -> datetime:
    v = value.strip()
    for fmt in (_DATE_FMT, _DATE_FMT_SHORT):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unknown date format: {value!r}")


# ---------------------------------------------------------------------------
# TradeHistory（現物総合台帳）
# ---------------------------------------------------------------------------

# 取引種別 → カテゴリ
_DEPOSIT_KINDS  = {"預入", "受取", "入金"}
_WITHDRAW_KINDS = {"外部送付", "出金"}
_FEE_KINDS      = {"手数料", "送付手数料", "出金手数料"}
_TRADE_KINDS    = {"買い", "売り"}
_COLLATERAL_KINDS = {"証拠金預入", "証拠金引出"}   # spot ↔ 証拠金口座 の内部振替


class BitflyerTradeCsvSource(CsvSourceAdapter):
    """bitFlyer TradeHistory.csv パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                tx = self._parse_row(row, i)
                if tx is not None:
                    txs.append(tx)
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> CanonicalTx | None:
        kind = row["取引種別"].strip()
        ts   = _parse_ts(row["取引日時"])

        cur1     = row["通貨1"].strip().upper()
        amt1     = _d(row["通貨1数量"])
        fee1     = _d(row.get("手数料", ""))   # 通貨1建ての手数料(負値)
        cur2     = row.get("通貨2", "").strip().upper() or None
        amt2     = _d(row.get("通貨2数量", ""))
        order_id = row.get("注文 ID", "").strip()

        raw_key = f"{row['取引日時']}|{kind}|{cur1}|{row['通貨1数量']}|{order_id}"
        base = dict(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            raw=dict(row),
        )

        if kind in _TRADE_KINDS:
            fee_amount = abs(fee1) if fee1 != _ZERO else None
            if kind == "買い":
                # 通貨1 を受取 (+), 通貨2(JPY) を支払 (-)
                return CanonicalTx(
                    **base, type=TxType.TRADE,
                    received_asset=cur1, received_amount=abs(amt1),
                    sent_asset=cur2, sent_amount=abs(amt2),
                    fee_asset=cur1 if fee_amount else None, fee_amount=fee_amount,
                )
            else:  # 売り
                return CanonicalTx(
                    **base, type=TxType.TRADE,
                    received_asset=cur2, received_amount=abs(amt2),
                    sent_asset=cur1, sent_amount=abs(amt1),
                    fee_asset=cur1 if fee_amount else None, fee_amount=fee_amount,
                )

        elif kind in _DEPOSIT_KINDS or kind in _WITHDRAW_KINDS:
            # 通貨1数量は符号付き（正=増, 負=減）。種別だけで方向を決めず符号で判定する。
            # 例: 「外部送付」でも正値はキャンセル/返金のため残高が増える。
            if amt1 >= _ZERO:
                return CanonicalTx(
                    **base, type=TxType.DEPOSIT,
                    received_asset=cur1, received_amount=abs(amt1),
                    label=kind,
                )
            else:
                return CanonicalTx(
                    **base, type=TxType.WITHDRAW,
                    sent_asset=cur1, sent_amount=abs(amt1),
                    label=kind,
                )

        elif kind in _FEE_KINDS:
            # 通常は負値（手数料）。正値の場合は手数料返金なので増加扱い。
            if amt1 <= _ZERO:
                return CanonicalTx(
                    **base, type=TxType.FEE,
                    fee_asset=cur1, fee_amount=abs(amt1),
                    label=kind,
                )
            else:
                return CanonicalTx(
                    **base, type=TxType.DEPOSIT,
                    received_asset=cur1, received_amount=abs(amt1),
                    label=f"{kind}_返金",
                )

        elif kind in _COLLATERAL_KINDS:
            # spot ↔ 証拠金口座 の内部振替
            if amt1 < _ZERO:   # 証拠金預入: spot から出ていく
                return CanonicalTx(
                    **base, type=TxType.TRANSFER,
                    sent_asset=cur1, sent_amount=abs(amt1),
                    label="collateral_deposit",
                )
            else:              # 証拠金引出: spot に戻ってくる
                return CanonicalTx(
                    **base, type=TxType.TRANSFER,
                    received_asset=cur1, received_amount=abs(amt1),
                    label="collateral_withdraw",
                )

        return None  # 未知種別はスキップ


# ---------------------------------------------------------------------------
# CollateralHistory（FX/CFD 実現損益）
# ---------------------------------------------------------------------------

class BitflyerCollateralCsvSource(CsvSourceAdapter):
    """
    bitFlyer CollateralHistory.csv パーサー

    「決済」行の実現損益(取引損益)とファンディングレートのみを記録する。
    証拠金預入/引出はTradeHistory側で内部振替として記録済みのためスキップ。

    実現損益(JPY)の表現:
      利益 → REWARD (received JPY)
      損失 → FEE    (fee JPY, label=fx_realized_loss)
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                txs.extend(self._parse_row(row, i))
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> list[CanonicalTx]:
        op = row["操作"].strip()
        if op != "決済":
            return []   # 証拠金預入/引出/充当/SFD はスキップ

        ts    = _parse_ts(row["日時"])
        asset = row["通貨等"].strip().upper()
        pnl   = _d(row.get("取引損益", ""))
        funding = _d(row.get("ファンディングレート", ""))
        fee   = _d(row.get("手数料", ""))
        pair  = row.get("ペア", "").strip()

        results: list[CanonicalTx] = []

        def _pnl_entry(amount: Decimal, kind: str) -> CanonicalTx:
            raw_key = f"{row['日時']}|{op}|{pair}|{kind}|{amount}"
            base = dict(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                raw=dict(row),
            )
            if amount >= _ZERO:
                return CanonicalTx(
                    **base, type=TxType.REWARD,
                    received_asset=asset, received_amount=amount,
                    label=f"fx_{kind}_profit",
                )
            else:
                return CanonicalTx(
                    **base, type=TxType.FEE,
                    fee_asset=asset, fee_amount=abs(amount),
                    label=f"fx_{kind}_loss",
                )

        if pnl != _ZERO:
            results.append(_pnl_entry(pnl, "realized"))
        if funding != _ZERO:
            results.append(_pnl_entry(funding, "funding"))
        if fee != _ZERO:
            raw_key = f"{row['日時']}|{op}|{pair}|fee|{fee}"
            results.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.FEE,
                fee_asset=asset, fee_amount=abs(fee),
                label="fx_fee",
                raw=dict(row),
            ))

        return results


# ---------------------------------------------------------------------------
# ConversionHistory（旧・両替履歴）
# ---------------------------------------------------------------------------

class BitflyerConversionCsvSource(CsvSourceAdapter):
    """
    bitFlyer ConversionHistory.csv パーサー

    列: 日時, 通貨等, 変動量
    同一日時の2行(JPY +/-, BTC -/+)を1つのTRADEに統合する。
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        # 日時でグルーピング
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["日時"].strip(), []).append(r)

        txs: list[CanonicalTx] = []
        for ts_str, group in groups.items():
            tx = self._build_trade(ts_str, group)
            if tx is not None:
                txs.append(tx)
        return txs

    def _build_trade(self, ts_str: str, group: list[dict]) -> CanonicalTx | None:
        ts = _parse_ts(ts_str)
        received_asset = received_amount = None
        sent_asset = sent_amount = None

        for r in group:
            asset = r["通貨等"].strip().upper()
            amt = _d(r["変動量"])
            if amt > _ZERO:
                received_asset, received_amount = asset, amt
            elif amt < _ZERO:
                sent_asset, sent_amount = asset, abs(amt)

        if received_asset is None and sent_asset is None:
            return None

        raw_key = f"{ts_str}|conversion"
        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=received_asset, received_amount=received_amount,
            sent_asset=sent_asset, sent_amount=sent_amount,
            label="conversion",
            raw={"group": group},
        )
