"""GMOコイン 取引レポートCSV アダプタ

対象: GMOコイン > 取引履歴 > CSVダウンロード (2026_trading_report.csv 形式)
エンコード: UTF-8 BOM付き

精算区分ごとのマッピング:
  取引所現物取引          → TRADE  (JPY建て現物売買)
  暗号資産預入・送付      → DEPOSIT / WITHDRAW
  日本円入出金            → DEPOSIT / WITHDRAW (JPY)
  取引所現物 取引手数料返金→ REWARD
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

_DATE_FMT = "%Y/%m/%d %H:%M"


def _d(value: str) -> Decimal | None:
    v = value.strip()
    if not v:
        return None
    try:
        return Decimal(v.replace(",", ""))
    except InvalidOperation:
        return None


_ZERO = Decimal("0")

# 売買として集約処理する精算区分（取引所現物・販売所）
_TRADE_SETTLEMENTS = {"取引所現物取引", "販売所取引"}


class GmoCsvSource(CsvSourceAdapter):
    """GMOコイン 取引レポートCSV パーサー

    注意: 1注文が複数の約定（fill）行に分割されて出力されるが、約定IDが空のため
    (日時, 注文ID, 銘柄名, 売買区分) でグルーピングして合算する。
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        # 全行を読み込んでから精算区分ごとに処理
        with open(path, encoding="utf-8-sig", newline="") as f:
            all_rows = list(csv.DictReader(f))

        txs: list[CanonicalTx] = []

        # 「取引所現物取引」「販売所取引」は注文単位に集約してから処理
        txs.extend(self._load_spot_trades(all_rows))

        # その他の精算区分は1行1トランザクション
        for i, row in enumerate(all_rows):
            settlement = row["精算区分"].strip()
            if settlement in _TRADE_SETTLEMENTS:
                continue  # 上で処理済み
            try:
                ts = datetime.strptime(row["日時"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)
                tx: CanonicalTx | None = None
                if settlement == "暗号資産預入・送付":
                    tx = self._parse_crypto_transfer(row, ts, i)
                elif settlement == "日本円入出金":
                    tx = self._parse_jpy_transfer(row, ts, i)
                elif settlement == "取引所現物 取引手数料返金":
                    tx = self._parse_fee_rebate(row, ts, i)
                # else: 未知の精算区分はスキップ
                if tx is not None:
                    txs.append(tx)
            except (KeyError, ValueError) as e:
                raise ValueError(f"Row {i + 1}: {e}\n  {dict(row)}") from e

        return txs

    def _load_spot_trades(self, all_rows: list[dict]) -> list[CanonicalTx]:
        """現物取引（取引所・販売所）を注文単位に集約して CanonicalTx を生成する。

        - 取引所現物取引: 注文ID あり / 約定ID 空（1注文が複数約定に分割される）
        - 販売所取引:     注文ID 空 / 約定ID あり（通常1約定）
        存在する方のID（注文ID優先、なければ約定ID）でグルーピングし、
        約定数量・正味JPY（日本円受渡金額）を合算する。
        """
        from collections import OrderedDict

        # 出現順を保持しつつグルーピング
        groups: dict[tuple, dict] = OrderedDict()

        for row in all_rows:
            settlement = row["精算区分"].strip()
            if settlement not in _TRADE_SETTLEMENTS:
                continue
            order_key = row["注文ID"].strip() or row.get("約定ID", "").strip()
            key = (
                settlement,
                row["日時"].strip(),
                order_key,
                row["銘柄名"].strip().upper(),
                row["売買区分"].strip(),
            )
            if key not in groups:
                groups[key] = {"rows": [], "qty": _ZERO, "net_jpy": _ZERO}
            g = groups[key]
            g["rows"].append(row)
            g["qty"]     += _d(row["約定数量"]) or _ZERO
            # 「日本円受渡金額」= 手数料・リベート込みの正味JPY (買=負, 売=正)。
            # これを正味JPYとして使うことでGMO本体の残高と一致する。
            g["net_jpy"] += _d(row["日本円受渡金額"]) or _ZERO

        txs = []
        for (settlement, ts_str, order_id, asset, side), g in groups.items():
            ts = datetime.strptime(ts_str, _DATE_FMT).replace(tzinfo=timezone.utc)
            qty     = g["qty"]
            jpy     = abs(g["net_jpy"])   # 正味JPY (手数料込み)

            if side == "買":
                # JPY を支払って暗号資産を受取 (手数料は取得価額に織り込み済み)
                recv_asset, recv_amount = asset, qty
                sent_asset, sent_amount = "JPY", jpy
            else:  # 売
                recv_asset, recv_amount = "JPY", jpy
                sent_asset, sent_amount = asset, qty

            # 精算区分+注文ID(or約定ID)で同一取引は常に同一IDになる（冪等性）
            raw_key = f"{settlement}|{ts_str}|{order_id}|{asset}|{side}"

            txs.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRADE,
                received_asset=recv_asset,
                received_amount=recv_amount,
                sent_asset=sent_asset,
                sent_amount=sent_amount,
                raw={"fills": len(g["rows"]), "first_row": g["rows"][0]},
            ))
        return txs

    def _parse_crypto_transfer(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        direction = row["授受区分"].strip()   # 預入 / 送付
        asset     = row["銘柄名"].strip().upper()
        qty       = _d(row["数量"])
        fee       = _d(row.get("送付手数料", ""))
        label     = row.get("送付先/送付元", "").strip() or None
        tx_hash   = row.get("トランザクションID", "").strip() or None

        raw_key = "|".join([row["日時"], row["銘柄名"], direction, row["数量"]])

        if direction == "預入":
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=asset,
                received_amount=qty,
                fee_asset=asset if fee else None,
                fee_amount=fee,
                label=label,
                tx_hash=tx_hash,
                raw=dict(row),
            )
        else:  # 送付
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=asset,
                sent_amount=qty,
                fee_asset=asset if fee else None,
                fee_amount=fee,
                label=label,
                tx_hash=tx_hash,
                raw=dict(row),
            )

    def _parse_jpy_transfer(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        sub_type = row.get("入出金区分", "").strip()   # 即時入金 / 出金 など
        amount   = _d(row.get("入出金金額", ""))
        raw_key  = "|".join([row["日時"], sub_type, row.get("入出金金額", "")])

        is_deposit = "入金" in sub_type

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.DEPOSIT if is_deposit else TxType.WITHDRAW,
            received_asset="JPY" if is_deposit else None,
            received_amount=amount if is_deposit else None,
            sent_asset=None if is_deposit else "JPY",
            sent_amount=None if is_deposit else amount,
            label=sub_type,
            raw=dict(row),
        )

    def _parse_fee_rebate(self, row: dict, ts: datetime, idx: int) -> CanonicalTx:
        amount  = _d(row.get("日本円受渡金額", ""))
        asset   = row["銘柄名"].strip().upper()
        raw_key = "|".join([row["日時"], row["精算区分"], row["銘柄名"], row.get("日本円受渡金額", "")])

        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=TxType.REWARD,
            received_asset="JPY",
            received_amount=amount,
            label=f"手数料返金({asset})",
            raw=dict(row),
        )
