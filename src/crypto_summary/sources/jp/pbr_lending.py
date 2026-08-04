"""PBR Lending 日次レポート CSV アダプタ (Shift_JIS / UTF-8 自動判定)

列構成:
  日付, 通貨種別, 貸出数量, 総単日受取予定利息, 総累計受取予定利息,
  返還数量, 返還受取利息（利確数量）, 手数料（送金・解約）,
  プレミアム移行数量, プレミアム移行受取利息（利確数量）,
  プレミアム満期数量, プレミアム満期受取利息（利確数量）,
  運営からの付与数量（利確数量）, 総貸出元本残高, 総受取数量,
  ご参考レート, 備考

このアダプタは *利確した利息だけ* を記録する。元本（預入・引き出し）は
入出金履歴 (pbr_transfers) が全期間を通じて唯一の情報源。

税務上の取り扱い:
  - 「予定利息」列は日次発生額（未受取）→ スキップ
  - 「利確数量」列が >0 の行のみ REWARD として記録（実際の受取）

貸出数量・返還数量を記録しない理由（どの日付でも記録しない）:
  旧システム（～2026-03-02）:
    入金＝即座に貸出開始だったため、貸出数量は入出金履歴の「入庫」と同一イベント。
  新システム（2026-03-03～）:
    貸出数量/返還数量は「貸出準備ウォレット ⇔ 貸出」の内部移動にすぎず、
    PBR 全体の保有残高は変わらない。
  いずれの場合も記録すると入出金履歴と二重計上になる。

  なお旧システムでも「入庫したがその日は貸し出されなかった」ケースが実在し
  （2025-12-30 の入庫は 2026-01-03 まで貸出にならなかった）、貸出数量を
  預入の代用にすると資産が丸ごと欠落する。日付で切り替える方式が破綻する理由。

注意: 2026-03-03 以降の日次レポートは取り込まないこと。
  「返還受取利息（利確数量）」は入出金履歴の「返還利息」と同一イベントだが
  raw_key が異なるため ID による重複排除が効かず、利息が二重計上になる。

pbr_lending ソース単体の残高は「受け取った利息の累計」であって預入資産ではない。
保有残高を得るには入出金履歴も取り込むこと。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter, read_csv_text

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
        self._reset_skips()
        txs: list[CanonicalTx] = []
        text = read_csv_text(path)  # Shift_JIS / UTF-8(BOM) 自動判定
        reader = csv.DictReader(io.StringIO(text))
        # フォーマット検証: 日次レポートは「貸出数量」「返還数量」列を持つ。
        # 入出金履歴 (pbr_transfers) を誤って選ぶと無言で0件になるため明示エラー。
        fieldnames = [f.strip() for f in (reader.fieldnames or [])]
        if "貸出数量" not in fieldnames and "返還数量" not in fieldnames:
            raise ValueError(
                "貸出日次レポートの形式ではありません（「貸出数量」「返還数量」列が見つかりません）。"
                "入出金履歴の場合は「PBR Lending（入出金履歴）」を選択してください。"
            )
        for i, row in enumerate(reader):
            txs.extend(self._parse_row(row, i))
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> list[CanonicalTx]:
        # このレポートは全日付・全通貨の行を持ち、大半は利確が無い（予定利息のみ）。
        # 構造上の埋め草なので skip カウンタには計上しない。
        results: list[CanonicalTx] = []

        ts    = datetime.strptime(row["日付"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
        asset = row["通貨種別"].strip().upper()

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

        return results
