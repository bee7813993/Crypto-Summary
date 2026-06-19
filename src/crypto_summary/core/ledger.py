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
CREATE TABLE IF NOT EXISTS import_batches (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    filename    TEXT,
    imported_at TEXT NOT NULL,
    tx_count    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_txs (
    batch_id TEXT NOT NULL,
    tx_id    TEXT NOT NULL,
    PRIMARY KEY (batch_id, tx_id)
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

        削除した件数を返す。cursors / exports / import_batches も併せて削除する。
        """
        if source:
            n = self.count(source)
            # exports は transactions を参照するサブクエリで消すため先に実行する
            self._conn.execute(
                "DELETE FROM exports WHERE tx_id IN "
                "(SELECT id FROM transactions WHERE source=?)", (source,)
            )
            self._conn.execute("DELETE FROM transactions WHERE source=?", (source,))
            self._conn.execute("DELETE FROM cursors WHERE source=?", (source,))
            # このソースのバッチに含まれる tx_id 紐付けを削除し、
            # 孤立した import_batches レコードも合わせて消す
            self._conn.execute(
                "DELETE FROM batch_txs WHERE batch_id IN "
                "(SELECT id FROM import_batches WHERE source=?)", (source,)
            )
            self._conn.execute(
                "DELETE FROM import_batches WHERE source=?", (source,)
            )
        else:
            n = self.count()
            self._conn.execute("DELETE FROM exports")
            self._conn.execute("DELETE FROM transactions")
            self._conn.execute("DELETE FROM cursors")
            self._conn.execute("DELETE FROM batch_txs")
            self._conn.execute("DELETE FROM import_batches")
        self._conn.commit()
        return n

    def delete_by_id(self, tx_id: str) -> bool:
        """指定したIDのトランザクションを1件削除する。削除できた場合 True を返す。"""
        cur = self._conn.execute(
            "DELETE FROM transactions WHERE id=?", (tx_id,)
        )
        self._conn.execute(
            "DELETE FROM exports WHERE tx_id=?", (tx_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_exported(self, tx_ids: list[str], sink: str) -> None:
        now = datetime.utcnow().isoformat()
        self._conn.executemany(
            "INSERT OR REPLACE INTO exports VALUES (?,?,?)",
            [(tx_id, sink, now) for tx_id in tx_ids],
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Import batches（CSV単位の取り込み記録 / CSV単位削除に使う）
    # ------------------------------------------------------------------

    def record_import_batch(
        self, batch_id: str, source: str, exchange: str,
        filename: str | None, tx_ids: list[str],
    ) -> None:
        """1回のCSV取り込みを1バッチとして記録する。

        tx_ids にはそのCSVから生成された全取引IDを渡す（既存・新規問わず）。
        後で delete_import_batch(batch_id) するとこのバッチ由来の取引を削除できる。
        """
        self._conn.execute(
            "INSERT INTO import_batches VALUES (?,?,?,?,?,?)",
            (batch_id, source, exchange, filename,
             datetime.utcnow().isoformat(), len(tx_ids)),
        )
        self._conn.executemany(
            "INSERT OR IGNORE INTO batch_txs VALUES (?,?)",
            [(batch_id, tid) for tid in tx_ids],
        )
        self._conn.commit()

    def list_import_batches(self) -> list[dict]:
        """取り込みバッチ一覧を新しい順で返す。

        existing_count は ledger に現存する取引数（個別削除後の現状）。
        """
        rows = self._conn.execute(
            "SELECT id, source, exchange, filename, imported_at, tx_count "
            "FROM import_batches ORDER BY imported_at DESC"
        ).fetchall()
        result: list[dict] = []
        for bid, source, exchange, filename, imported_at, tx_count in rows:
            existing = self._conn.execute(
                "SELECT COUNT(*) FROM batch_txs bt "
                "JOIN transactions t ON bt.tx_id = t.id "
                "WHERE bt.batch_id = ?",
                (bid,),
            ).fetchone()[0]
            result.append({
                "id": bid,
                "source": source,
                "exchange": exchange,
                "filename": filename,
                "imported_at": imported_at,
                "tx_count": tx_count,
                "existing_count": existing,
            })
        return result

    def delete_import_batch(self, batch_id: str) -> int:
        """バッチ由来の取引を削除する（他バッチと共有する取引は残す）。削除件数を返す。"""
        tx_ids = [
            r[0] for r in self._conn.execute(
                "SELECT tx_id FROM batch_txs WHERE batch_id=?", (batch_id,)
            ).fetchall()
        ]
        deleted = 0
        for tid in tx_ids:
            shared = self._conn.execute(
                "SELECT COUNT(*) FROM batch_txs WHERE tx_id=? AND batch_id<>?",
                (tid, batch_id),
            ).fetchone()[0]
            if shared == 0:
                cur = self._conn.execute(
                    "DELETE FROM transactions WHERE id=?", (tid,)
                )
                self._conn.execute("DELETE FROM exports WHERE tx_id=?", (tid,))
                deleted += cur.rowcount
        self._conn.execute("DELETE FROM batch_txs WHERE batch_id=?", (batch_id,))
        self._conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
        self._conn.commit()
        return deleted

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

    @staticmethod
    def _source_clause(source: str | list[str] | None) -> tuple[str | None, list]:
        """source を単一/複数どちらでも受け取り WHERE 句の断片を返す。"""
        if not source:
            return None, []
        if isinstance(source, str):
            return "source=?", [source]
        placeholders = ",".join("?" for _ in source)
        return f"source IN ({placeholders})", list(source)

    def balances(
        self,
        source: str | list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Decimal]:
        """資産ごとの純残高を返す (受取 - 送出 - 手数料)。

        source は単一文字列・複数リスト・None(全ソース合算) のいずれも可。
        """
        clauses, params = [], []
        src_clause, src_params = self._source_clause(source)
        if src_clause:
            clauses.append(src_clause)
            params.extend(src_params)
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

    def balances_by_source(
        self,
        source: str | list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, dict[str, Decimal]]:
        """ソースごとの資産別純残高を返す: {source: {asset: balance}}。"""
        clauses, params = [], []
        src_clause, src_params = self._source_clause(source)
        if src_clause:
            clauses.append(src_clause)
            params.extend(src_params)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT source, received_asset, received_amount, sent_asset, sent_amount, fee_asset, fee_amount "
            f"FROM transactions {where}",
            params,
        ).fetchall()

        result: dict[str, dict[str, Decimal]] = {}
        for src, ra, rv, sa, sv, fa, fv in rows:
            bal = result.setdefault(src, {})
            if ra and rv:
                bal[ra] = bal.get(ra, Decimal(0)) + Decimal(rv)
            if sa and sv:
                bal[sa] = bal.get(sa, Decimal(0)) - Decimal(sv)
            if fa and fv:
                bal[fa] = bal.get(fa, Decimal(0)) - Decimal(fv)
        return result

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

    def transactions(
        self,
        source: str | list[str] | None = None,
        asset: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CanonicalTx], int]:
        """取引履歴をフィルタ付きで返す。戻り値: (取引リスト, 総件数)。

        source: 単一文字列 / リスト / None(全ソース)
        asset:  received_asset / sent_asset / fee_asset のいずれかに一致
        """
        clauses, params = [], []
        src_clause, src_params = self._source_clause(source)
        if src_clause:
            clauses.append(src_clause)
            params.extend(src_params)
        if asset:
            clauses.append(
                "(received_asset=? OR sent_asset=? OR fee_asset=?)"
            )
            params.extend([asset, asset, asset])
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total: int = self._conn.execute(
            f"SELECT COUNT(*) FROM transactions {where}", params
        ).fetchone()[0]

        rows = self._conn.execute(
            f"SELECT * FROM transactions {where} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [self._row_to_tx(r) for r in rows], total

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
