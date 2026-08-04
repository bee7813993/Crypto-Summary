"""PBR Lending 入出金履歴 CSV アダプタ (Shift_JIS / UTF-8 自動判定)

列構成:
  日付, 通貨種別, 区分, 数量, 備考

このファイルが PBR 口座への入金・出金の唯一の情報源（全期間）。
日次レポート (pbr_lending) は利確利息のみを担当し、元本は一切扱わない。
日付による分岐は存在しない（旧システム／新システムで扱いを変えない）。

区分の取り扱い:
  入庫           : DEPOSIT（PBR への入金）
  出庫           : WITHDRAW（PBR からの出金）数量は負数で入っているので abs() を使う
  利息           : REWARD（日次の利息付与）※ record_daily_interest で無効化可能
  返還利息       : REWARD（貸出返還時に付与される利息）
  プレミアム満期 : REWARD（プレミアム満期時に付与される利息）
  貸出 / 返還    : スキップ。貸出準備ウォレット ⇔ 貸出 の内部移動であり、
                   PBR 全体の保有残高は変わらない。入出金履歴の入庫/出庫と
                   二重計上になる。
  システム移行   : スキップ（数量=0 の移行記録）

利息 3 区分が二重計上にならない理由:
  「利息」「返還利息」「プレミアム満期」は同一の日次利息付与を排他的に分割した
  ものであり、重複しない。ある資産・ある日に返還利息／プレミアム満期が付く場合、
  その分だけ同日の「利息」が減るか、「利息」行自体が存在しない。
  実データでの確認例:
    2026-03-24 BTC 利息 0.00002008 + 返還利息 0.00002393 = 0.00004401（日次額と一致）
    2026-06-02 ETH 利息 0.00006576 + 返還利息 0.00113916 = 0.00120492（日次額と一致）
    2026-05-07 XRP はプレミアム満期 0.041096 のみで「利息」行が無い
  したがって 3 区分すべてを記録してよい。

数量のカンマ区切り:
  "3,000" のように桁区切りカンマが入る場合、CSV パーサーが列をずらして
  しまう（5列 → 6列）。フィールド数で検出し数量列を結合して対応する。
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

# 貸出準備ウォレット ⇔ 貸出 の内部移動。PBR 全体の残高は動かない。
_INTERNAL_MOVE_KUBUN = frozenset({"貸出", "返還"})

# 利息系の区分 → CanonicalTx.label。
# ラベル文字列は pbr_lending（日次レポート）と揃えてある。同じ経済イベントが
# どちらのファイル由来でも同じラベルになり、シンクの分類も一致する。
_REWARD_KUBUN: dict[str, str] = {
    "利息":           "daily_interest",
    "返還利息":        "return_interest",
    "プレミアム満期":   "premium_maturity_interest",
}


class PbrTransfersCsvSource(CsvSourceAdapter):
    """PBR Lending 入出金履歴 CSV パーサー"""

    #: 日次の「利息」を記録するか。False にすると REWARD を作らず
    #: skip_reasons["daily_interest_disabled"] に計上する。
    #: 取引 ID は内容由来なので False → True に変えて再取込すれば
    #: 不足分が足されるだけで重複しない（逆方向は削除が必要）。
    record_daily_interest: bool = True

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        txs: list[CanonicalTx] = []
        seen_ids: set[str] = set()
        text = read_csv_text(path)  # Shift_JIS / UTF-8(BOM) 自動判定
        reader = csv.reader(io.StringIO(text))
        headers: list[str] | None = None
        for raw_row in reader:
            if headers is None:
                headers = [h.strip() for h in raw_row]
                # フォーマット検証: 入出金履歴は「区分」「数量」列を持つ。
                # 日次レポート (pbr_lending) を誤って選ぶと無言で0件になるため明示エラー。
                if "区分" not in headers or "数量" not in headers:
                    raise ValueError(
                        "入出金履歴の形式ではありません（「区分」「数量」列が見つかりません）。"
                        "日次レポートの場合は「PBR Lending（貸出日次レポート）」を選択してください。"
                    )
                continue
            tx = self._parse_row(raw_row, headers)
            if tx is None:
                continue
            # raw_key が 日付|区分|資産|数量 のため、同一資産・同日・同額の行が
            # 2件あると ID が衝突して 1 件に潰れる。無言で落とさず計上する。
            if tx.id in seen_ids:
                self._skip("duplicate_row")
                continue
            seen_ids.add(tx.id)
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

        if kubun in _INTERNAL_MOVE_KUBUN:
            self._skip("internal_move")
            return None

        label = _REWARD_KUBUN.get(kubun)
        if label is not None:
            if kubun == "利息" and not self.record_daily_interest:
                self._skip("daily_interest_disabled")
                return None
            return CanonicalTx(
                id=tx_id,
                source=self.source_id,
                timestamp=ts,
                type=TxType.REWARD,
                received_asset=asset,
                received_amount=abs(amount),
                label=label,
                raw=row,
            )

        self._skip(f"unknown_kubun:{kubun}")
        return None
