"""Nexo Pro CSV アダプタ

対象ファイル:
  SpotHistory*.csv  : スポット取引履歴
  DnWHistory*.csv   : 入出金履歴
  (InterestHistory*.csv は現時点で空のためスキップ)

timestamp フォーマット: "2026-01-05 11:20:45.843" (ミリ秒付き)
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter
from .nexo_savings import NexoSavingsCsvSource

_DATE_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _parse_ts(value: str) -> datetime:
    # マイクロ秒が3桁(ミリ秒)しかない場合に対応
    v = value.strip()
    try:
        return datetime.strptime(v, _DATE_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        # ミリ秒なし
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _d(value: str) -> Decimal | None:
    v = value.strip()
    return Decimal(v) if v else None


# ---------------------------------------------------------------------------
# Spot 取引
# ---------------------------------------------------------------------------

class NexoSpotCsvSource(CsvSourceAdapter):
    """
    Nexo Pro SpotHistory CSV パーサー

    Columns:
        id, timestamp, pair, side, type, price, executedPrice,
        triggerPrice, requestedAmount, filledAmount, tradingFee,
        feeCurrency, status, orderId

    - status=cancelled または filledAmount=0 はスキップ
    - pair = "BASE/QUOTE" 形式
    """

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
        filled = _d(row.get("filledAmount", ""))
        if not filled or filled == 0:
            return None  # 未約定（キャンセル含む）はスキップ
        # 一部約定後キャンセルも filledAmount > 0 なら実取引として記録

        exec_price = _d(row.get("executedPrice", ""))
        if exec_price is None:
            return None

        ts         = _parse_ts(row["timestamp"])
        pair       = row["pair"].strip()
        side       = row["side"].strip().lower()   # buy / sell
        fee_amount = _d(row.get("tradingFee", ""))
        fee_asset  = row.get("feeCurrency", "").strip().upper() or None
        tx_id_raw  = row.get("id", "").strip()
        tx_type    = row.get("type", "").strip().lower()

        base, quote = pair.split("/")
        base, quote = base.upper(), quote.upper()

        quote_amount = filled * exec_price

        if side == "buy":
            recv_asset, recv_amount = base,  filled
            sent_asset, sent_amount = quote, quote_amount
        else:  # sell
            recv_asset, recv_amount = quote, quote_amount
            sent_asset, sent_amount = base,  filled

        # dust convert は TRANSFER ラベルを付与
        label = "dust_convert" if tx_type == "dust convert" else None

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, tx_id_raw or f"{row['timestamp']}|{pair}|{side}|{row['filledAmount']}"),
            source=self.source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=recv_asset,
            received_amount=recv_amount,
            sent_asset=sent_asset,
            sent_amount=sent_amount,
            fee_asset=fee_asset if fee_amount else None,
            fee_amount=fee_amount,
            label=label,
            raw=dict(row),
        )


# ---------------------------------------------------------------------------
# 入出金
# ---------------------------------------------------------------------------

class NexoDnWCsvSource(CsvSourceAdapter):
    """
    Nexo Pro DnWHistory CSV パーサー

    Columns: timestamp, amount, asset, side
    side: DEPOSIT / WITHDRAW
    """

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
        side   = row["side"].strip().upper()    # DEPOSIT / WITHDRAW
        asset  = row["asset"].strip().upper()
        amount = _d(row["amount"])
        ts     = _parse_ts(row["timestamp"])

        if amount is None:
            return None

        raw_key = f"{row['timestamp']}|{asset}|{side}|{row['amount']}"

        if side == "DEPOSIT":
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=asset,
                received_amount=amount,
                raw=dict(row),
            )
        else:  # WITHDRAW
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=asset,
                sent_amount=amount,
                raw=dict(row),
            )


# ---------------------------------------------------------------------------
# 自動判別ディスパッチャ
# ---------------------------------------------------------------------------

class NexoProCsvSource(CsvSourceAdapter):
    """Nexo の CSV 種別をヘッダーから自動判別して取り込むディスパッチャ。

    SpotHistory（スポット取引）/ DnWHistory（入出金）/ 取引明細（貯蓄口座）
    のいずれかを判別し、対応するパーサーへ委譲する。利用者は CSV 種別を
    選ばずに、すべての Nexo CSV を同一口座（同じ source_id）へ取り込める。
    """

    @staticmethod
    def detect(fieldnames: list[str] | None) -> str | None:
        """ヘッダー列から種別 ("spot" / "dnw" / "savings") を返す。不明なら None。"""
        f = {(name or "").strip() for name in (fieldnames or [])}
        if {"pair", "filledAmount"} <= f:
            return "spot"
        if {"asset", "side", "amount"} <= f and "pair" not in f:
            return "dnw"
        if {"Transaction", "Input Currency"} <= f or "Date / Time (UTC)" in f:
            return "savings"
        return None

    def load(self, path: Path) -> list[CanonicalTx]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            try:
                header = next(csv.reader(f))
            except StopIteration:
                return []  # 空ファイル

        kind = self.detect(header)
        if kind == "spot":
            return NexoSpotCsvSource(self.source_id).load(path)
        if kind == "dnw":
            return NexoDnWCsvSource(self.source_id).load(path)
        if kind == "savings":
            return NexoSavingsCsvSource(self.source_id).load(path)

        raise ValueError(
            "Nexo CSV の種別を判別できませんでした。"
            "対応形式: SpotHistory / DnWHistory / 取引明細(Download Statement)。"
            f" ヘッダー: {', '.join(header)}"
        )
