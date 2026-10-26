"""取引履歴ファイル 1 つを台帳へ取り込む共通処理。

画面のアップロード（/api/import/csv）と取り込みフォルダ（core/inbox.py）の
両方がここを通る。どちらから入れても同じ結果になるのが要点:
  load()（形式ごとのアダプタ）→ reconcile()（既存台帳との突き合わせ）
  → upsert_many()（取引 ID は内容から決まるので二重にならない）
  → record_import_batch()（ファイル単位で消せるようにバッチを記録）

同じ台帳への取り込みはプロセス内のロックで直列化する。アップロードと
フォルダ走査が重なっても順に処理される（SQLite 自体のロック待ちは Ledger 側）。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger import Ledger


class FileImportError(ValueError):
    """利用者に見せる取り込み失敗（未対応の取引所、形式が違う、列が足りない等）。"""


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def import_lock(db_path: str | Path) -> threading.RLock:
    """台帳ファイルごとの取り込みロック。"""
    key = str(Path(db_path).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@dataclass
class ImportResult:
    exchange: str
    source: str
    filename: str
    parsed: int = 0          # 台帳へ入れた（既存・新規問わず）取引数
    imported: int = 0        # そのうち新規
    replaced: int = 0        # reconcile で置き換えた既存取引数
    skipped: int = 0         # 読めたが記録対象外として落とした行数
    skip_reasons: dict[str, int] = field(default_factory=dict)
    batch_id: str | None = None
    message: str | None = None  # 取り込む取引が無かった理由

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": True,
            "imported": self.imported,
            "parsed": self.parsed,
            "replaced": self.replaced,
            "skipped": self.skipped,
            "skip_reasons": dict(self.skip_reasons),
            "source": self.source,
        }
        if self.batch_id:
            d["batch_id"] = self.batch_id
        if self.message:
            d["message"] = self.message
        return d


def import_file(
    db_path: str | Path,
    exchange: str,
    source_id: str,
    path: Path,
    *,
    filename: str | None = None,
    origin: str = "upload",
) -> ImportResult:
    """ファイルを読み、台帳へ取り込み、バッチとして記録する。

    形式の問題は FileImportError（利用者向けの文）。それ以外の例外はそのまま上げる。
    """
    # sources パッケージは core を読み込むので、循環を避けるためここで import する
    from ..sources.csv_import import EXCHANGE_SOURCES

    if exchange not in EXCHANGE_SOURCES:
        raise FileImportError(f"未対応の取引所です: {exchange}")
    name = filename or path.name
    adapter = EXCHANGE_SOURCES[exchange](source_id)
    try:
        txs = adapter.load(path)
    except Exception as e:  # noqa: BLE001 - パースエラーを利用者向けの文にする
        raise FileImportError(f"CSV の解析に失敗しました: {e}") from e

    result = ImportResult(
        exchange=exchange, source=source_id, filename=name,
        skipped=getattr(adapter, "skipped", 0),
        skip_reasons=dict(getattr(adapter, "skip_reasons", {}) or {}),
    )
    if not txs:
        result.message = "取引が見つかりませんでした"
        return result

    with import_lock(db_path):
        ledger = Ledger(db_path)
        try:
            # 既存の取引と突き合わせる（SUMM のレポートがある期間を飛ばす、
            # Bybit の入出金をまとめる、など。アダプタごと）。
            txs = adapter.reconcile(txs, ledger)
            result.replaced = getattr(adapter, "replaced", 0)
            # reconcile で落とした行（SUMM の期間など）もスキップとして数える
            result.skipped = getattr(adapter, "skipped", 0)
            result.skip_reasons = dict(getattr(adapter, "skip_reasons", {}) or {})
            if not txs:
                result.message = "取り込む取引はありませんでした"
                return result
            before = ledger.count(source_id)
            ledger.upsert_many(txs)
            after = ledger.count(source_id)

            latest_ts = max(t.timestamp for t in txs)
            cursor = ledger.get_cursor(source_id)
            if cursor is None or latest_ts > cursor:
                ledger.set_cursor(source_id, latest_ts)

            batch_id = f"batch:{uuid.uuid4().hex[:12]}"
            ledger.record_import_batch(
                batch_id, source_id, exchange, name, [t.id for t in txs],
                origin=origin,
            )
        finally:
            ledger.close()

    result.parsed = len(txs)
    result.imported = after - before
    result.batch_id = batch_id
    return result
