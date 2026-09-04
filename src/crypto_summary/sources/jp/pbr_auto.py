"""PBR Lending CSV 自動判定アダプタ

日次レポートと入出金履歴の両形式を自動識別して処理する。
ヘッダー列を読んで振り分けるため、形式を意識せずアップロードできる。

  貸出数量 / 返還数量 列あり → PbrLendingCsvSource（日次レポート）
  区分 / 数量 列あり         → PbrTransfersCsvSource（入出金履歴）
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ...core.models import CanonicalTx
from ..base import CsvSourceAdapter, read_csv_text
from .pbr_lending import PbrLendingCsvSource
from .pbr_transfers import PbrTransfersCsvSource


class PbrAutoCsvSource(CsvSourceAdapter):
    """PBR Lending CSV（日次レポート／入出金履歴）自動判定パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        text = read_csv_text(path)
        first_row = next(csv.reader(io.StringIO(text)), [])
        headers = [h.strip() for h in first_row]

        sub: CsvSourceAdapter
        if "貸出数量" in headers or "返還数量" in headers:
            sub = PbrLendingCsvSource(self.source_id)
        elif "区分" in headers and "数量" in headers:
            sub = PbrTransfersCsvSource(self.source_id)
        else:
            raise ValueError(
                "PBR Lending のCSV形式を識別できませんでした。"
                f"検出されたヘッダー: {', '.join(headers) or '（空）'}"
            )

        txs = sub.load(path)
        # Web UI は常に「自動判定」で取り込むため、転送しないとスキップ件数が消える
        self.skipped = sub.skipped
        self.skip_reasons = dict(sub.skip_reasons)
        return txs
