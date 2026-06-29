"""PBR Lending 日次レポート CSV アダプタ (Shift_JIS / UTF-8 自動判定)

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

貸出数量・返還数量の扱い（システム移行 2026-03-03 を境に変化）:
  旧システム（～2026-03-02）:
    入金＝即座に貸出開始だったため、貸出数量＝PBR への預け入れそのもの。
    - 貸出開始（貸出数量 >0）→ DEPOSIT（PBR Lending への預け入れ）
    - 返還（返還数量 >0）   → WITHDRAW（PBR Lending からの引き出し）
  新システム（2026-03-03～）:
    入金→貸出準備ウォレット→貸出 と段階が分かれた。貸出数量/返還数量は
    「貸出準備ウォレット ⇔ 貸出」の内部移動にすぎず、PBR 全体の保有残高は
    変わらない。実際の入出金は入出金履歴 (pbr_transfers) が記録する。
    ここで貸出数量を DEPOSIT 扱いすると入出金履歴の入庫と二重計上になるため、
    貸出数量/返還数量はスキップする（利確 REWARD は引き続き記録）。

pbr_lending ソースの残高は「PBR Lending に預けている資産」を表す。
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

# この日からシステムが分離: 入金→貸出準備ウォレット→貸出。
# 以降の貸出数量/返還数量は内部移動のため残高に計上しない（二重計上防止）。
_PREP_WALLET_DATE = date(2026, 3, 3)

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
        results: list[CanonicalTx] = []

        ts    = datetime.strptime(row["日付"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
        asset = row["通貨種別"].strip().upper()

        # 新システム（2026-03-03～）では貸出数量/返還数量は貸出準備ウォレットとの
        # 内部移動のため残高に計上しない（入出金履歴の入庫/出庫と二重計上になる）。
        count_lending = ts.date() < _PREP_WALLET_DATE

        # --- 貸出開始（預け入れ） → DEPOSIT（旧システムのみ） ---
        lend_qty = _d(row.get("貸出数量", ""))
        if count_lending and lend_qty > _ZERO:
            raw_key = f"{row['日付']}|貸出数量|{asset}|{row['貸出数量']}"
            results.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=asset,
                received_amount=lend_qty,
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

        # --- 返還（引き出し）→ WITHDRAW（旧システムのみ） ---
        return_qty = _d(row.get("返還数量", ""))
        if count_lending and return_qty > _ZERO:
            raw_key = f"{row['日付']}|返還数量|{asset}|{row['返還数量']}"
            results.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=asset,
                sent_amount=return_qty,
                label="lending_return",
                raw=dict(row),
            ))

        return results
