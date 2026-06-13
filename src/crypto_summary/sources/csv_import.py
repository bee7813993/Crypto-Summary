"""CSV import adapters for exchange trade history files.

Registry (EXCHANGE_SOURCES) maps CLI --exchange values to adapter classes.
To add a new exchange, create an adapter in sources/jp/ or sources/,
then add it here.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter


def _parse_amount_asset(value: str) -> tuple[Decimal, str]:
    """Parse '0.00238095 BTC' into (Decimal('0.00238095'), 'BTC')."""
    parts = value.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Cannot parse amount+asset from: {value!r}")
    return Decimal(parts[0]), parts[1].upper()


class BinanceCsvSource(CsvSourceAdapter):
    """
    Parses Binance Spot Trade History CSV.

    Expected columns:
        Date(UTC), Pair, Side, Price, Executed, Amount, Fee

    Download from:
        Binance > Orders > Spot Order > Export Trade History
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                tx = self._parse_row(row, i)
                if tx:
                    txs.append(tx)
        return txs

    def _parse_row(self, row: dict[str, str], row_index: int) -> CanonicalTx | None:
        try:
            # --- timestamp ---
            ts_str = row.get("Date(UTC)", "").strip()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

            pair  = row["Pair"].strip().upper()
            side  = row["Side"].strip().upper()    # BUY / SELL

            exec_amount, exec_asset   = _parse_amount_asset(row["Executed"])
            quote_amount, quote_asset = _parse_amount_asset(row["Amount"])
            fee_amount, fee_asset     = _parse_amount_asset(row["Fee"])

            # BUY  → received base (BTC), sent quote (USDT)
            # SELL → received quote (USDT), sent base (BTC)
            if side == "BUY":
                recv_asset, recv_amount = exec_asset, exec_amount
                sent_asset, sent_amount = quote_asset, quote_amount
            else:
                recv_asset, recv_amount = quote_asset, quote_amount
                sent_asset, sent_amount = exec_asset, exec_amount

            # idempotency key: hash of raw line content
            raw_key = f"{ts_str}|{pair}|{side}|{row.get('Price','')}|{row['Executed']}|{row['Amount']}"
            tx_id = CanonicalTx.make_id(self.source_id, raw_key)

            return CanonicalTx(
                id=tx_id,
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRADE,
                received_asset=recv_asset,
                received_amount=recv_amount,
                sent_asset=sent_asset,
                sent_amount=sent_amount,
                fee_asset=fee_asset,
                fee_amount=fee_amount,
                raw=dict(row),
            )
        except (KeyError, ValueError, InvalidOperation) as e:
            raise ValueError(f"Row {row_index + 1} parse error: {e}\n  row={dict(row)}") from e


class UniversalCsvSource(CsvSourceAdapter):
    """
    Parses the project's own universal CSV format. Useful for testing
    and for exchanges that don't have a dedicated adapter yet.

    Columns:
        timestamp, type, received_asset, received_amount,
        sent_asset, sent_amount, fee_asset, fee_amount, note

    timestamp format: ISO 8601 (e.g. 2024-01-15T10:30:00Z)
    type values: trade | deposit | withdraw | fee | reward | transfer
    """

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                tx = self._parse_row(row, i)
                if tx:
                    txs.append(tx)
        return txs

    def _parse_row(self, row: dict[str, str], row_index: int) -> CanonicalTx | None:
        try:
            ts_str = row["timestamp"].strip()
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

            def opt_decimal(v: str) -> Decimal | None:
                v = v.strip()
                return Decimal(v) if v else None

            def opt_str(v: str) -> str | None:
                v = v.strip().upper()
                return v if v else None

            raw_key = f"{ts_str}|{row.get('type','')}|{row.get('received_asset','')}|{row.get('received_amount','')}|{row.get('sent_asset','')}|{row.get('sent_amount','')}"
            tx_id = CanonicalTx.make_id(self.source_id, raw_key)

            return CanonicalTx(
                id=tx_id,
                source=self.source_id,
                timestamp=ts,
                type=TxType(row["type"].strip().lower()),
                received_asset=opt_str(row.get("received_asset", "")),
                received_amount=opt_decimal(row.get("received_amount", "")),
                sent_asset=opt_str(row.get("sent_asset", "")),
                sent_amount=opt_decimal(row.get("sent_amount", "")),
                fee_asset=opt_str(row.get("fee_asset", "")),
                fee_amount=opt_decimal(row.get("fee_amount", "")),
                label=row.get("note", "").strip() or None,
                raw=dict(row),
            )
        except (KeyError, ValueError, InvalidOperation) as e:
            raise ValueError(f"Row {row_index + 1} parse error: {e}\n  row={dict(row)}") from e


# registry for CLI lookup
from .jp.gmo import GmoCsvSource  # noqa: E402

EXCHANGE_SOURCES: dict[str, type[CsvSourceAdapter]] = {
    "binance":   BinanceCsvSource,
    "gmo":       GmoCsvSource,
    "universal": UniversalCsvSource,
}
