"""Nexo 先物（Advanced Trading）取引履歴 CSV アダプタ

対象ファイル: nexo_futures_transactions*.csv
ダウンロード: Nexo アプリ > Futures > 取引履歴

先物ウォレットは Nexo 本体アカウント内のウォレットであり、別サービスの
Nexo Pro（SpotHistory / DnWHistory）とは無関係。資金は貯蓄ウォレットとの
間で出入りする（実データで同時刻・同額の対応を確認済み）:

    先物CSV  TRANSFER_TO_WALLET      ← 貯蓄CSV  Transfer To Advanced
    先物CSV  TRANSFER_FROM_WALLET    ← 貯蓄CSV  Transfer From Advanced

このため口座グループ上も貯蓄（nexo_savings）と同じ「Nexo」に属する。

timestamp フォーマット: ミリ秒エポック（例: 1788047574849）
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter, require_columns

_REQUIRED = ("Id", "Timestamp", "Type", "Amount", "Asset")

# 他形式と取り違えたときの判別に使う、この形式に固有の列。
_SIGNATURE_COLUMNS = {"Symbol", "DealId", "InternalTransactionType"}


def looks_like_futures(fieldnames: list[str] | None) -> bool:
    """ヘッダーが先物取引履歴CSVのものなら True。

    他の Nexo アダプタが「別のファイルを選んでいる」と気づくために使う。
    """
    fields = {(name or "").strip() for name in (fieldnames or [])}
    return _SIGNATURE_COLUMNS <= fields


def _fnull(value: str | None) -> str:
    """先物CSVの "null" 文字列を空として扱う。"""
    v = (value or "").strip()
    return "" if v.lower() == "null" else v


def _d(value: str | None) -> Decimal | None:
    v = _fnull(value)
    if not v:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


class NexoFuturesCsvSource(CsvSourceAdapter):
    """
    Nexo 先物取引履歴 CSV パーサー

    Columns:
        Id, Timestamp(ミリ秒エポック), Symbol, Type, Position, Price, Amount,
        Status, Asset, Side, DealId, InternalTransactionType,
        TradingFeeAmount, TradingFeeAsset, ExecutionPriceAsset,
        ExchangeRate, SourceAsset, SourceAmount

    証拠金取引のため建玉自体は資産の増減にならない。bitFlyer 証拠金口座と
    同じく実現損益ベースで記録する:
      REALIZED_PNL     : 利益 → REWARD / 損失 → FEE（手数料控除前の粗損益）
      FUNDING_FEE      : 受取 → REWARD / 支払 → FEE
      *_OPEN / *_CLOSE : 建玉・決済注文。実際に残高から引かれる
                         TradingFeeAmount のみ FEE として記録
      TRANSFER_TO_WALLET / TRANSFER_FROM_WALLET :
        貯蓄ウォレットとの資金振替。名前に反して TO_WALLET が先物への入金、
        FROM_WALLET が先物からの出金（貯蓄側 Transfer To/From Advanced と対応）。
        貯蓄側と両側とも記録しない設計で、振替の入出差額はボーナス付与 REWARD
        （nexo_savings）と本アダプタの損益・手数料で過不足なく捕捉される。
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            require_columns(
                reader.fieldnames, _REQUIRED,
                "Nexo の先物取引履歴CSV（nexo_futures_transactions）",
            )
            for row in reader:
                # 実ファイルはヘッダー先頭列が " Id"（先頭に半角スペース）。
                # キーを strip しないと Id が取れず全行の ID が衝突する。
                row = {(k or "").strip(): v for k, v in row.items()}
                txs.extend(self._parse_row(row))
        return txs

    def _parse_row(self, row: dict[str, str]) -> list[CanonicalTx]:
        tx_type = _fnull(row.get("Type")).upper()
        ts_raw = _fnull(row.get("Timestamp"))
        try:
            ts = datetime.fromtimestamp(int(ts_raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            self._skip("タイムスタンプ不明")
            return []

        asset = _fnull(row.get("Asset")).upper()
        amount = _d(row.get("Amount"))
        row_id = _fnull(row.get("Id"))

        def _pnl_entry(kind: str) -> list[CanonicalTx]:
            """符号付き損益を 利益→REWARD / 損失→FEE の1件として返す。"""
            if amount is None or amount == 0 or not asset:
                return []
            base = dict(
                id=CanonicalTx.make_id(self.source_id, f"futures|{row_id}"),
                source=self.source_id,
                timestamp=ts,
                raw=dict(row),
            )
            if amount > 0:
                return [CanonicalTx(
                    **base, type=TxType.REWARD,
                    received_asset=asset, received_amount=amount,
                    label=f"futures_{kind}_profit",
                )]
            return [CanonicalTx(
                **base, type=TxType.FEE,
                fee_asset=asset, fee_amount=-amount,
                label=f"futures_{kind}_loss",
            )]

        if tx_type == "REALIZED_PNL":
            return _pnl_entry("realized")

        if tx_type == "FUNDING_FEE":
            return _pnl_entry("funding")

        if tx_type in ("TRANSFER_FROM_WALLET", "TRANSFER_TO_WALLET"):
            self._skip("貯蓄⇔先物ウォレットの振替")
            return []

        if tx_type.endswith("_OPEN") or tx_type.endswith("_CLOSE") \
                or "LIQUIDATION" in tx_type:
            fee = _d(row.get("TradingFeeAmount"))
            fee_asset = _fnull(row.get("TradingFeeAsset")).upper()
            if fee and fee != 0 and fee_asset:
                return [CanonicalTx(
                    id=CanonicalTx.make_id(self.source_id, f"futures_fee|{row_id}"),
                    source=self.source_id,
                    timestamp=ts,
                    type=TxType.FEE,
                    fee_asset=fee_asset, fee_amount=abs(fee),
                    label="futures_fee",
                    raw=dict(row),
                )]
            return []

        self._skip(f"未知の種別: {tx_type or '(空)'}")
        return []
