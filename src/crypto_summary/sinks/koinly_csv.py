"""Koinly Universal CSV エクスポート

Koinly Universal CSV format:
  Date, Sent Amount, Sent Currency, Received Amount, Received Currency,
  Fee Amount, Fee Currency, Net Worth Amount, Net Worth Currency,
  Label, Description, TxHash

Date format: YYYY-MM-DD HH:MM:SS UTC
Label values (Koinly reserved): reward, staking, income, ignored,
  realized gain, cost (blank = no special treatment)
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ..core.models import CanonicalTx, TxType

# Koinly の予約 Label 値
_LABEL_REWARD       = "reward"
_LABEL_STAKING      = "staking"
_LABEL_IGNORED      = "ignored"
_LABEL_REALIZED_GAIN = "realized gain"
_LABEL_COST         = "cost"

_KOINLY_HEADERS = [
    "Date",
    "Sent Amount", "Sent Currency",
    "Received Amount", "Received Currency",
    "Fee Amount", "Fee Currency",
    "Net Worth Amount", "Net Worth Currency",
    "Label", "Description", "TxHash",
]


def _map_label(tx: CanonicalTx) -> str:
    """CanonicalTx の type / label → Koinly Label 文字列に変換。"""
    if tx.type == TxType.REWARD:
        label = (tx.label or "").lower()
        if "staking" in label:
            return _LABEL_STAKING
        return _LABEL_REWARD

    if tx.type == TxType.TRANSFER:
        return _LABEL_IGNORED

    if tx.type == TxType.FEE:
        label = (tx.label or "").lower()
        if "realized" in label or "fx_realized" in label:
            # FX 実現損失は cost として扱う（Koinly では負の realized gain）
            return _LABEL_COST
        return ""

    # TRADE / DEPOSIT / WITHDRAW → ラベルなし
    return ""


def _fmt_decimal(v: Decimal | None) -> str:
    if v is None:
        return ""
    # 末尾ゼロを除去しつつ十分な精度を維持
    return format(v.normalize(), "f")


def to_koinly_rows(txs: Sequence[CanonicalTx]) -> list[dict[str, str]]:
    """CanonicalTx のリストを Koinly CSV 行 (dict) に変換して返す。"""
    rows: list[dict[str, str]] = []
    for tx in txs:
        date_str = tx.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        label = _map_label(tx)

        # Description: internal label があれば付与
        description = tx.label or ""

        row: dict[str, str] = {
            "Date":                  date_str,
            "Sent Amount":           _fmt_decimal(tx.sent_amount),
            "Sent Currency":         tx.sent_asset or "",
            "Received Amount":       _fmt_decimal(tx.received_amount),
            "Received Currency":     tx.received_asset or "",
            "Fee Amount":            _fmt_decimal(tx.fee_amount),
            "Fee Currency":          tx.fee_asset or "",
            "Net Worth Amount":      "",
            "Net Worth Currency":    "",
            "Label":                 label,
            "Description":           description,
            "TxHash":                tx.tx_hash or "",
        }
        rows.append(row)
    return rows


def to_koinly_csv_string(txs: Sequence[CanonicalTx]) -> str:
    """Koinly Universal CSV 文字列を返す。"""
    rows = to_koinly_rows(txs)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_KOINLY_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_koinly_csv(txs: Sequence[CanonicalTx], out_path: Path) -> int:
    """Koinly Universal CSV を書き出す。書き出した行数を返す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = to_koinly_csv_string(txs)
    out_path.write_text(text, encoding="utf-8", newline="")
    return len(to_koinly_rows(txs))
