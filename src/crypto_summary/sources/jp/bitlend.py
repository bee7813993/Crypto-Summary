"""BitLending 貸出履歴 CSV アダプタ

Columns: タイムスタンプ, 貸出ID, 銘柄名, 種別, 数量, レート, 申請日

種別:
  貸出開始   → TRANSFER (暗号資産を貸し出し)
  貸借料付与 → REWARD   (貸借料の受取)
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y/%m/%d %H:%M:%S"

# 銘柄名の正規化: ERC20等のサフィックスを除去
_ASSET_NORMALIZE = {
    "USDC_ERC_20": "USDC",
    "USDT_ERC_20": "USDT",
    "ETH_ERC_20":  "ETH",
}


def _normalize_asset(name: str) -> str:
    return _ASSET_NORMALIZE.get(name.strip(), name.strip().upper())


class BitLendCsvSource(CsvSourceAdapter):
    """BitLending 貸出履歴 CSV パーサー"""

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
        kind = row["種別"].strip()
        if kind not in ("貸出開始", "貸借料付与"):
            return None

        ts    = datetime.strptime(row["タイムスタンプ"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
        asset = _normalize_asset(row["銘柄名"])
        qty_s = row["数量"].strip()
        qty   = Decimal(qty_s) if qty_s else None
        loan_id = row["貸出ID"].strip()

        raw_key = f"{row['タイムスタンプ']}|{loan_id}|{kind}|{qty_s}"

        if kind == "貸借料付与":
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.REWARD,
                received_asset=asset,
                received_amount=qty,
                label="lending_interest",
                raw=dict(row),
            )
        else:  # 貸出開始
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRANSFER,
                sent_asset=asset,
                sent_amount=qty,
                label="lending_start",
                raw=dict(row),
            )
