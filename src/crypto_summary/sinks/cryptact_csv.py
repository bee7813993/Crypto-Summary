"""Cryptact カスタムファイル CSV エクスポート

Cryptact（クリプタクト）のカスタムファイル形式:
  Timestamp, Action, Source, Base, Volume, Price, Counter, Fee, FeeCcy, Comment

  Timestamp 形式: YYYY/MM/DD HH:MM:SS（UTC）
  Action: BUY / SELL / PAY / BONUS / STAKING / LENDING / SENDFEE など

マッピング方針（best-effort・申告前に要確認）:
  TRADE    → BUY    : Base=受取資産, Volume=受取数量, Counter=送付資産,
                      Price=送付数量/受取数量（1単位あたりの取得価格）
  REWARD   → BONUS（既定）/ STAKING（label に stak）/ LENDING（lend・interest・利息）
                    : Base=受取資産, Volume=受取数量, Counter=JPY, Price=0（時価補完）
  FEE      → SENDFEE: Base=手数料資産, Volume=手数料数量
  DEPOSIT / WITHDRAW / TRANSFER → 自己資金の移動（非課税）とみなし出力しない

Cryptact はカスタムファイルで損益計算するため、取得原価に影響しない
入出金・振替は通常記録しない。出力対象外の件数は skipped として返す。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ..core.models import CanonicalTx, TxType

_CRYPTACT_HEADERS = [
    "Timestamp", "Action", "Source", "Base", "Volume",
    "Price", "Counter", "Fee", "FeeCcy", "Comment",
]

# 取得原価に影響しない（出力対象外の）取引種別
_SKIP_TYPES = {TxType.DEPOSIT, TxType.WITHDRAW, TxType.TRANSFER}

_DEFAULT_COUNTER = "JPY"


def _fmt_decimal(v: Decimal | None) -> str:
    if v is None:
        return ""
    return format(v.normalize(), "f")


def _reward_action(tx: CanonicalTx) -> str:
    label = (tx.label or "").lower()
    if "stak" in label:
        return "STAKING"
    if "lend" in label or "interest" in label or "利息" in label:
        return "LENDING"
    return "BONUS"


def to_cryptact_rows(txs: Sequence[CanonicalTx]) -> tuple[list[dict[str, str]], int]:
    """CanonicalTx を Cryptact カスタムファイル行へ変換する。

    戻り値: (行リスト, スキップ件数)。スキップは入出金・振替など出力対象外の件数。
    """
    rows: list[dict[str, str]] = []
    skipped = 0

    for tx in txs:
        if tx.type in _SKIP_TYPES:
            skipped += 1
            continue

        ts = tx.timestamp.strftime("%Y/%m/%d %H:%M:%S")
        comment = tx.label or ""

        if tx.type == TxType.TRADE and tx.received_asset and tx.sent_asset \
                and tx.received_amount and tx.sent_amount:
            price = (tx.sent_amount / tx.received_amount) if tx.received_amount else Decimal("0")
            rows.append({
                "Timestamp": ts,
                "Action": "BUY",
                "Source": tx.source,
                "Base": tx.received_asset,
                "Volume": _fmt_decimal(tx.received_amount),
                "Price": _fmt_decimal(price),
                "Counter": tx.sent_asset,
                "Fee": _fmt_decimal(tx.fee_amount),
                "FeeCcy": tx.fee_asset or "",
                "Comment": comment,
            })
        elif tx.type == TxType.REWARD and tx.received_asset and tx.received_amount:
            rows.append({
                "Timestamp": ts,
                "Action": _reward_action(tx),
                "Source": tx.source,
                "Base": tx.received_asset,
                "Volume": _fmt_decimal(tx.received_amount),
                "Price": "0",
                "Counter": _DEFAULT_COUNTER,
                "Fee": _fmt_decimal(tx.fee_amount),
                "FeeCcy": tx.fee_asset or "",
                "Comment": comment,
            })
        elif tx.type == TxType.FEE and tx.fee_asset and tx.fee_amount:
            rows.append({
                "Timestamp": ts,
                "Action": "SENDFEE",
                "Source": tx.source,
                "Base": tx.fee_asset,
                "Volume": _fmt_decimal(tx.fee_amount),
                "Price": "0",
                "Counter": _DEFAULT_COUNTER,
                "Fee": "",
                "FeeCcy": "",
                "Comment": comment,
            })
        else:
            # 想定外の組み合わせ（受取/送付が欠けた TRADE 等）は安全のためスキップ
            skipped += 1

    return rows, skipped


def to_cryptact_csv_string(txs: Sequence[CanonicalTx]) -> tuple[str, int]:
    """Cryptact CSV 文字列を返す。戻り値: (csv文字列, スキップ件数)。"""
    rows, skipped = to_cryptact_rows(txs)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CRYPTACT_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue(), skipped


def write_cryptact_csv(txs: Sequence[CanonicalTx], out_path: Path) -> int:
    """Cryptact カスタムファイル CSV を書き出す。書き出した行数を返す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text, _ = to_cryptact_csv_string(txs)
    out_path.write_text(text, encoding="utf-8", newline="")
    rows, _ = to_cryptact_rows(txs)
    return len(rows)
