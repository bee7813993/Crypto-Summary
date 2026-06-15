from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .models import CanonicalTx, TxType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    type           TEXT NOT NULL,
    received_asset TEXT,
    received_amount TEXT,
    sent_asset     TEXT,
    sent_amount    TEXT,
    fee_asset      TEXT,
    fee_amount     TEXT,
    label          TEXT,
    tx_hash        TEXT,
    raw            TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    source   TEXT PRIMARY KEY,
    last_ts  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    tx_id       TEXT NOT NULL,
    sink        TEXT NOT NULL,
    exported_at TEXT NOT NULL,
    PRIMARY KEY (tx_id, sink)
);
"""

_COLS = [
    "id", "source", "timestamp", "type",
    "received_asset", "received_amount",
    "sent_asset", "sent_amount",
    "fee_asset", "fee_amount",
    "label", "tx_hash", "raw", "created_at",
]


class Ledger:
    def __init__(self, db_path: str | Path = "ledger.db"):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(self, tx: CanonicalTx) -> None:
        self._conn.execute(
            """
            INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                type            = excluded.type,
                received_asset  = excluded.received_asset,
                received_amount = excluded.received_amount,
                sent_asset      = excluded.sent_asset,
                sent_amount     = excluded.sent_amount,
                fee_asset       = excluded.fee_asset,
                fee_amount      = excluded.fee_amount,
                raw             = excluded.raw
            """,
            (
                tx.id, tx.source, tx.timestamp.isoformat(), tx.type.value,
                tx.received_asset,
                str(tx.received_amount) if tx.received_amount is not None else None,
                tx.sent_asset,
                str(tx.sent_amount) if tx.sent_amount is not None else None,
                tx.fee_asset,
                str(tx.fee_amount) if tx.fee_amount is not None else None,
                tx.label, tx.tx_hash,
                json.dumps(tx.raw, default=str),
                datetime.utcnow().isoformat(),
            ),
        )
        self._conn.commit()

    def upsert_many(self, txs: list[CanonicalTx]) -> int:
        for tx in txs:
            self.upsert(tx)
        return len(txs)

    def set_cursor(self, source: str, ts: datetime) -> None:
        self._conn.execute(
            "INSERT INTO cursors VALUES (?,?) ON CONFLICT(source) DO UPDATE SET last_ts=excluded.last_ts",
            (source, ts.isoformat()),
        )
        self._conn.commit()

    def clear(self, source: str | None = None) -> int:
        """トランザクションを削除する。source指定時はそのソースのみ。

        削除した件数を返す。cursors も併せて削除する。
        """
        if source:
            n = self.count(source)
            self._conn.execute("DELETE FROM transactions WHERE source=?", (source,))
            self._conn.execute("DELETE FROM cursors WHERE source=?", (source,))
            self._conn.execute(
                "DELETE FROM exports WHERE tx_id IN "
                "(SELECT id FROM transactions WHERE source=?)", (source,)
            )
        else:
            n = self.count()
            self._conn.execute("DELETE FROM transactions")
            self._conn.execute("DELETE FROM cursors")
            self._conn.execute("DELETE FROM exports")
        self._conn.commit()
        return n

    def mark_exported(self, tx_ids: list[str], sink: str) -> None:
        now = datetime.utcnow().isoformat()
        self._conn.executemany(
            "INSERT OR REPLACE INTO exports VALUES (?,?,?)",
            [(tx_id, sink, now) for tx_id in tx_ids],
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def count(self, source: str | None = None) -> int:
        if source:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE source=?", (source,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
        return row[0]

    def get_cursor(self, source: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT last_ts FROM cursors WHERE source=?", (source,)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def sources(self) -> list[tuple[str, int, str | None]]:
        """Returns (source, count, last_cursor_ts) per source."""
        rows = self._conn.execute(
            "SELECT source, COUNT(*) FROM transactions GROUP BY source ORDER BY source"
        ).fetchall()
        result = []
        for src, cnt in rows:
            cur = self.get_cursor(src)
            result.append((src, cnt, cur.isoformat() if cur else None))
        return result

    def balances(
        self,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Decimal]:
        """資産ごとの純残高を返す (受取 - 送出 - 手数料)。"""
        clauses, params = [], []
        if source:
            clauses.append("source=?")
            params.append(source)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT received_asset, received_amount, sent_asset, sent_amount, fee_asset, fee_amount "
            f"FROM transactions {where}",
            params,
        ).fetchall()

        bal: dict[str, Decimal] = {}
        for ra, rv, sa, sv, fa, fv in rows:
            if ra and rv:
                bal[ra] = bal.get(ra, Decimal(0)) + Decimal(rv)
            if sa and sv:
                bal[sa] = bal.get(sa, Decimal(0)) - Decimal(sv)
            if fa and fv:
                bal[fa] = bal.get(fa, Decimal(0)) - Decimal(fv)
        return bal

    def all(
        self,
        source: str | None = None,
        tx_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = 50,
    ) -> list[CanonicalTx]:
        clauses, params = [], []
        if source:
            clauses.append("source=?")
            params.append(source)
        if tx_type:
            clauses.append("type=?")
            params.append(tx_type)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        rows = self._conn.execute(
            f"SELECT * FROM transactions {where} ORDER BY timestamp ASC {limit_clause}",
            params,
        ).fetchall()
        return [self._row_to_tx(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _row_to_tx(self, row: tuple) -> CanonicalTx:
        d = dict(zip(_COLS, row))
        return CanonicalTx(
            id=d["id"],
            source=d["source"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            type=TxType(d["type"]),
            received_asset=d["received_asset"],
            received_amount=Decimal(d["received_amount"]) if d["received_amount"] else None,
            sent_asset=d["sent_asset"],
            sent_amount=Decimal(d["sent_amount"]) if d["sent_amount"] else None,
            fee_asset=d["fee_asset"],
            fee_amount=Decimal(d["fee_amount"]) if d["fee_amount"] else None,
            label=d["label"],
            tx_hash=d["tx_hash"],
            raw=json.loads(d["raw"]),
        )
