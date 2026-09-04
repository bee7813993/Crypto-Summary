"""Nexo CSV 自動判定アダプタ（貯蓄口座 / 先物）

Nexo 本体アカウントの2形式をヘッダーから振り分ける。形式を意識せずに
同じ口座へアップロードできる。

  Transaction / Input Currency 列あり        → NexoSavingsCsvSource（取引明細）
  Symbol / DealId / InternalTransactionType  → NexoFuturesCsvSource（先物）

別サービスの Nexo Pro（SpotHistory / DnWHistory）はここでは扱わず、
選ぶべき取引所を案内して落とす。Pro の取引を同じ口座に混ぜると、
資金が出入りしている貯蓄口座との残高が合わなくなるため。
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..core.models import CanonicalTx
from .base import CsvSourceAdapter
from .nexo import NexoProCsvSource
from .nexo_futures import NexoFuturesCsvSource, looks_like_futures
from .nexo_savings import NexoSavingsCsvSource, looks_like_savings


class NexoAutoCsvSource(CsvSourceAdapter):
    """Nexo CSV（取引明細／先物取引履歴）自動判定パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        with open(path, encoding="utf-8-sig", newline="") as f:
            try:
                header = next(csv.reader(f))
            except StopIteration:
                return []  # 空ファイル

        sub: CsvSourceAdapter
        if looks_like_futures(header):
            sub = NexoFuturesCsvSource(self.source_id)
        elif looks_like_savings(header):
            sub = NexoSavingsCsvSource(self.source_id)
        elif NexoProCsvSource.detect(header) is not None:
            raise ValueError(
                "これは Nexo Pro のCSVです（貯蓄・先物とは別サービス）。"
                "取引所に「Nexo Pro」を選択してください。"
            )
        else:
            raise ValueError(
                "Nexo CSV の種別を判別できませんでした。"
                "対応形式: 取引明細（nexo_transactions_*.csv）/ "
                "先物取引履歴（nexo_futures_transactions*.csv）。"
                f" ヘッダー: {', '.join(h.strip() for h in header) or '（空）'}"
            )

        txs = sub.load(path)
        # 自動判定で取り込むのが既定のため、転送しないとスキップ件数が消える
        self.skipped = sub.skipped
        self.skip_reasons = dict(sub.skip_reasons)
        return txs
