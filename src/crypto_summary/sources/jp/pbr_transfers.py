"""PBR Lending 入出金履歴 CSV アダプタ (Shift_JIS / UTF-8 自動判定)

列構成:
  日付, 通貨種別, 区分, 数量, 備考

区分の取り扱い:
  - 入庫  : DEPOSIT（PBR への入金）
  - 出庫  : WITHDRAW（PBR からの出金）数量は負数で入っているので abs() を使う
  - システム移行: スキップ（数量=0 の移行記録）

スキップ対象期間:
  旧システム（～2025-12-31）では「入金＝即座に貸出開始」だったため、
  入出金履歴の入庫/出庫は日次レポート (pbr_lending) の
  貸出数量/返還数量 と同一の取引を指す。重複を避けるためスキップする。

  2026-01-01～2026-03-02 は日次レポートが存在しない空白期間のため、
  入出金履歴のみが保有資産の記録となる。この期間は記録する。

  2026-03-03 以降は入金と貸出処理が分離した新システムのため記録する。

数量のカンマ区切り:
  "3,000" のように桁区切りカンマが入る場合、CSV パーサーが列をずらして
  しまう（5列 → 6列）。フィールド数で検出し数量列を結合して対応する。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter, read_csv_text

_DATE_FMT = "%Y-%m-%d"
# この日以前は旧システム期間（入庫=貸出開始）。pbr_lending 日次レポートで記録済みのためスキップ。
# 2026-01-01 からは日次レポートが存在しない空白期間のため記録する。
_SKIP_BEFORE = date(2026, 1, 1)


class PbrTransfersCsvSource(CsvSourceAdapter):
    """PBR Lending 入出金履歴 CSV パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        text = read_csv_text(path)  # Shift_JIS / UTF-8(BOM) 自動判定
        reader = csv.reader(io.StringIO(text))
        headers: list[str] | None = None
        for raw_row in reader:
            if headers is None:
                headers = [h.strip() for h in raw_row]
                continue
            tx = self._parse_row(raw_row, headers)
            if tx is not None:
                txs.append(tx)
        return txs

    def _parse_row(
        self, fields: list[str], headers: list[str]
    ) -> CanonicalTx | None:
        n = len(headers)
        # 数量フィールドに桁区切りカンマが入ると列が1つ増える (例: "3,000" → "3" / "000")
        # 5列期待なのに6列ある場合: 数量(idx=3)と次のフィールド(idx=4)を結合
        if len(fields) == n + 1:
            fields = fields[:3] + [fields[3] + fields[4]] + fields[5:]

        if len(fields) < n:
            return None

        row = {h: v.strip() for h, v in zip(headers, fields)}

        date_str = row.get("日付", "")
        if not date_str:
            return None

        try:
            ts = datetime.strptime(date_str, _DATE_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        # 旧システム期間はスキップ（日次レポートで記録済み）
        if ts.date() < _SKIP_BEFORE:
            return None

        asset = row.get("通貨種別", "").upper()
        kubun = row.get("区分", "")
        amount_str = row.get("数量", "").replace(",", "")

        # システム移行はスキップ
        if kubun == "システム移行":
            return None

        try:
            amount = Decimal(amount_str)
        except InvalidOperation:
            return None

        if amount == 0:
            return None

        raw_key = f"{date_str}|{kubun}|{asset}|{amount_str}"
        tx_id = CanonicalTx.make_id(self.source_id, raw_key)

        if kubun == "入庫":
            return CanonicalTx(
                id=tx_id,
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=asset,
                received_amount=amount,
                label="pbr_deposit",
                raw=row,
            )

        if kubun == "出庫":
            return CanonicalTx(
                id=tx_id,
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=asset,
                sent_amount=abs(amount),
                label="pbr_withdrawal",
                raw=row,
            )

        return None
