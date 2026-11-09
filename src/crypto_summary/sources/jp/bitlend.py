"""BitLending 貸出履歴 CSV アダプタ

Columns: タイムスタンプ, 貸出ID, 銘柄名, 種別, 数量, レート, 申請日

種別:
  貸出開始   → DEPOSIT  (BitLending に資産を預け入れ)
  貸借料付与 → REWARD   (貸借料の受取)
  返還       → WITHDRAW (元本と貸借料の引き出し。CSV の数量は負)
  返還手数料 → FEE      (返還時の手数料。数量 0 の行は記録しない)

bitlend ソースの残高は「BitLending に預けている資産」を表す。
貸出開始は預け入れ(入金)、貸借料付与は利息収入として残高に積み上がり、
返還で元本と貸借料の合計が引き出されて 0 に戻る。

返還の日時:
  返還行の「タイムスタンプ」は最後に貸借料が付いた月初の日付で、
  返還を申請した日（「申請日」列。"[1]YYYY/MM/DD hh:mm:ss" の形）より前になる。
  タイムスタンプのままだと申請より前に資産が消えるため、申請日があればそちらを使う。
  申請日は時刻付きの日本時間として読む。返還手数料の行には申請日が無いので、
  同じ貸出・同じタイムスタンプの返還行の申請日に揃える。
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y/%m/%d %H:%M:%S"
_JST = timezone(timedelta(hours=9))
_APPLIED_RE = re.compile(r"\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}")

_KINDS = ("貸出開始", "貸借料付与", "返還", "返還手数料")

# 銘柄名の正規化: ERC20等のサフィックスを除去
_ASSET_NORMALIZE = {
    "USDC_ERC_20": "USDC",
    "USDT_ERC_20": "USDT",
    "ETH_ERC_20":  "ETH",
}


def _normalize_asset(name: str) -> str:
    return _ASSET_NORMALIZE.get(name.strip(), name.strip().upper())


def _applied_at(value: str | None) -> datetime | None:
    """申請日列から申請日時（UTC）を取り出す。複数並んでいれば最後のもの。"""
    found = _APPLIED_RE.findall(value or "")
    if not found:
        return None
    return (datetime.strptime(found[-1], _DATE_FMT)
            .replace(tzinfo=_JST).astimezone(timezone.utc))


def _return_key(row: dict[str, str]) -> tuple[str, str]:
    """返還行と返還手数料行を対応付けるキー（貸出ID, タイムスタンプ）。"""
    return row["貸出ID"].strip(), row["タイムスタンプ"].strip()


class BitLendCsvSource(CsvSourceAdapter):
    """BitLending 貸出履歴 CSV パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        returned_at: dict[tuple[str, str], datetime] = {}
        for row in rows:
            if row["種別"].strip() == "返還":
                at = _applied_at(row.get("申請日"))
                if at is not None:
                    returned_at[_return_key(row)] = at

        txs: list[CanonicalTx] = []
        for row in rows:
            tx = self._parse_row(row, returned_at)
            if tx is not None:
                txs.append(tx)
        return txs

    def _parse_row(
        self, row: dict[str, str], returned_at: dict[tuple[str, str], datetime],
    ) -> CanonicalTx | None:
        kind = row["種別"].strip()
        if kind not in _KINDS:
            self._skip(f"未対応の種別: {kind}")
            return None

        ts    = datetime.strptime(row["タイムスタンプ"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
        asset = _normalize_asset(row["銘柄名"])
        qty_s = row["数量"].strip()
        qty   = Decimal(qty_s) if qty_s else None
        loan_id = row["貸出ID"].strip()

        raw_key = f"{row['タイムスタンプ']}|{loan_id}|{kind}|{qty_s}"
        common = dict(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            raw=dict(row),
        )

        if kind == "貸借料付与":
            return CanonicalTx(
                **common, timestamp=ts, type=TxType.REWARD,
                received_asset=asset, received_amount=qty,
                label="lending_interest",
            )
        if kind == "貸出開始":
            return CanonicalTx(
                **common, timestamp=ts, type=TxType.DEPOSIT,
                received_asset=asset, received_amount=qty,
                label="lending_start",
            )

        # 返還・返還手数料: 申請日が後ろにあればそちらを日時にする
        at = returned_at.get(_return_key(row))
        if at is not None and at > ts:
            ts = at
        if kind == "返還":
            return CanonicalTx(
                **common, timestamp=ts, type=TxType.WITHDRAW,
                sent_asset=asset, sent_amount=abs(qty) if qty is not None else None,
                label="lending_return",
            )
        # 返還手数料
        if not qty:
            return None
        return CanonicalTx(
            **common, timestamp=ts, type=TxType.FEE,
            fee_asset=asset, fee_amount=abs(qty),
            label="lending_return_fee",
        )
