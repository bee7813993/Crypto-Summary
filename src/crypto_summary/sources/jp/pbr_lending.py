"""PBR Lending 日次レポート CSV アダプタ (cp932)

列構成:
  日付, 通貨種別, 貸出数量, 総単日受取予定利息, 総累計受取予定利息,
  返還数量, 返還受取利息（利確数量）, 手数料（送金・解約）,
  プレミアム移行数量, プレミアム移行受取利息（利確数量）,
  プレミアム満期数量, プレミアム満期受取利息（利確数量）,
  運営からの付与数量（利確数量）, 総貸出元本残高, 総受取数量,
  ご参考レート, 備考

税務上の取り扱い:
  - 「予定利息」列は日次発生額（未受取）→ スキップ
  - 「利確数量」列が >0 の行のみ REWARD として記録（実際の受取）
  - 貸出開始（貸出数量 >0）→ TRANSFER
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y-%m-%d"

_ZERO = Decimal("0")


def _d(value: str) -> Decimal:
    v = value.strip()
    if not v:
        return _ZERO
    try:
        return Decimal(v)
    except InvalidOperation:
        return _ZERO


class PbrLendingCsvSource(CsvSourceAdapter):
    """PBR Lending 日次レポート CSV パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="cp932", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                txs.extend(self._parse_row(row, i))
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> list[CanonicalTx]:
        results: list[CanonicalTx] = []

        ts    = datetime.strptime(row["日付"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
        asset = row["通貨種別"].strip().upper()

        # --- 貸出開始（入金） → TRANSFER ---
        lend_qty = _d(row.get("貸出数量", ""))
        if lend_qty > _ZERO:
            raw_key = f"{row['日付']}|貸出数量|{asset}|{row['貸出数量']}"
            results.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRANSFER,
                sent_asset=asset,
                sent_amount=lend_qty,
                label="lending_start",
                raw=dict(row),
            ))

        # --- 確定利息（利確）→ REWARD のみ記録 ---
        confirmed_cols = [
            ("返還受取利息（利確数量）",           "return_interest"),
            ("プレミアム移行受取利息（利確数量）",  "premium_migration_interest"),
            ("プレミアム満期受取利息（利確数量）",  "premium_maturity_interest"),
            ("運営からの付与数量（利確数量）",       "admin_grant"),
        ]
        for col, label in confirmed_cols:
            qty = _d(row.get(col, ""))
            if qty > _ZERO:
                raw_key = f"{row['日付']}|{col}|{asset}|{row.get(col,'')}"
                results.append(CanonicalTx(
                    id=CanonicalTx.make_id(self.source_id, raw_key),
                    source=self.source_id,
                    timestamp=ts,
                    type=TxType.REWARD,
                    received_asset=asset,
                    received_amount=qty,
                    label=label,
                    raw=dict(row),
                ))

        # --- 返還（出金）→ TRANSFER ---
        return_qty = _d(row.get("返還数量", ""))
        if return_qty > _ZERO:
            raw_key = f"{row['日付']}|返還数量|{asset}|{row['返還数量']}"
            results.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRANSFER,
                received_asset=asset,
                received_amount=return_qty,
                label="lending_return",
                raw=dict(row),
            ))

        return results
