"""GMOコイン 取引レポートCSV アダプタ

対象: GMOコイン > 取引履歴 > CSVダウンロード (2026_trading_report.csv 形式)
エンコード: UTF-8 BOM付き

精算区分ごとのマッピング:
  取引所現物取引          → TRADE  (JPY建て現物売買)
  暗号資産預入・送付      → DEPOSIT / WITHDRAW
  日本円入出金            → DEPOSIT / WITHDRAW (JPY)
  取引所現物 取引手数料返金→ REWARD
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y/%m/%d %H:%M"


def _d(value: str) -> Decimal | None:
    v = value.strip()
    if not v:
        return None
    try:
        return Decimal(v.replace(",", ""))
    except InvalidOperation:
        return None


class GmoCsvSource(CsvSourceAdapter):
    """GMOコイン 取引レポートCSV パーサー"""

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
        try:
            settlement = row["精算区分"].strip()
            ts = datetime.strptime(row["日時"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)

            if settlement == "取引所現物取引":
                return self._parse_spot_trade(row, ts, idx)
            elif settlement == "暗号資産預入・送付":
                return self._parse_crypto_transfer(row, ts, idx)
            elif settlement == "日本円入出金":
                return self._parse_jpy_transfer(row, ts, idx)
            elif settlement == "取引所現物 取引手数料返金":
                return self._parse_fee_rebate(row, ts, idx)
            else:
                return None  # 未知の精算区分はスキップ
        except (KeyError, ValueError) as e:
            raise ValueError(f"Row {idx + 1}: {e}\n  {dict(row)}") from e

    def _parse_spot_trade(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        side    = row["売買区分"].strip()   # 買 / 売
        asset   = row["銘柄名"].strip().upper()
        qty     = _d(row["約定数量"])       # 暗号資産の数量
        rate    = _d(row["約定レート"])     # JPY/暗号資産
        amount  = _d(row["約定金額"])       # JPY金額 (= qty × rate)
        fee     = _d(row["注文手数料"])     # 手数料 JPY

        if side == "買":
            recv_asset, recv_amount = asset, qty
            sent_asset, sent_amount = "JPY", amount
        else:  # 売
            recv_asset, recv_amount = "JPY", amount
            sent_asset, sent_amount = asset, qty

        raw_key = "|".join([
            row["日時"], row["注文ID"], row["銘柄名"],
            row["売買区分"], row["約定数量"], row["約定金額"],
        ])

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=recv_asset,
            received_amount=recv_amount,
            sent_asset=sent_asset,
            sent_amount=sent_amount,
            fee_asset="JPY" if fee else None,
            fee_amount=fee,
            raw=dict(row),
        )

    def _parse_crypto_transfer(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        direction = row["授受区分"].strip()   # 預入 / 送付
        asset     = row["銘柄名"].strip().upper()
        qty       = _d(row["数量"])
        fee       = _d(row.get("送付手数料", ""))
        label     = row.get("送付先/送付元", "").strip() or None
        tx_hash   = row.get("トランザクションID", "").strip() or None

        raw_key = "|".join([row["日時"], row["銘柄名"], direction, row["数量"]])

        if direction == "預入":
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=asset,
                received_amount=qty,
                fee_asset=asset if fee else None,
                fee_amount=fee,
                label=label,
                tx_hash=tx_hash,
                raw=dict(row),
            )
        else:  # 送付
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=asset,
                sent_amount=qty,
                fee_asset=asset if fee else None,
                fee_amount=fee,
                label=label,
                tx_hash=tx_hash,
                raw=dict(row),
            )

    def _parse_jpy_transfer(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        sub_type = row.get("入出金区分", "").strip()   # 即時入金 / 出金 など
        amount   = _d(row.get("入出金金額", ""))
        raw_key  = "|".join([row["日時"], sub_type, row.get("入出金金額", "")])

        is_deposit = "入金" in sub_type

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.DEPOSIT if is_deposit else TxType.WITHDRAW,
            received_asset="JPY" if is_deposit else None,
            received_amount=amount if is_deposit else None,
            sent_asset=None if is_deposit else "JPY",
            sent_amount=None if is_deposit else amount,
            label=sub_type,
            raw=dict(row),
        )

    def _parse_fee_rebate(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        amount  = _d(row.get("日本円受渡金額", ""))
        asset   = row["銘柄名"].strip().upper()
        raw_key = "|".join([row["日時"], row["精算区分"], row["銘柄名"], row.get("日本円受渡金額", "")])

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.REWARD,
            received_asset="JPY",
            received_amount=amount,
            label=f"手数料返金({asset})",
            raw=dict(row),
        )
