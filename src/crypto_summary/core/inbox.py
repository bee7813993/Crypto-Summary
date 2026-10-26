"""取り込みフォルダ（inbox）— フォルダに置かれた取引履歴ファイルを台帳へ反映する。

設計: docs/inbox-design.md

フォルダの規約:
  IMPORT_DIR/
    <exchange>/              EXCHANGE_SOURCES のキー（bybit, gmo, pbr, nexo_auto, universal …）
      *.csv / *.json         source_id = <exchange>
      <account>/             同じ取引所の別口座。source_id = <account>
        *.csv
  マルチユーザーでは IMPORT_DIR/<inbox-name>/ が利用者ごとの入口（InboxRegistry）。

意味論（1 ファイル = 1 インポートバッチ）:
  新しいパス            → 取り込み
  同じパス・同じ内容     → 何もしない（size/mtime が同じなら読まずに済ませる）
  同じパス・違う内容     → 旧バッチを消してから取り込み直す（ファイル粒度の洗い替え）
  パスが消えた          → データは残す（status=missing）。消すのは利用者の操作
  取り込みに失敗         → status=error。内容が変わるか「再試行」されるまで繰り返さない
  バッチが別経路で消された → status=deleted。勝手に入れ直さない（利用者の削除を尊重）

無視するもの:
  先頭が . のファイル・ディレクトリ（.stfolder .syncthing.*.tmp .DS_Store）、
  *.tmp *.part *.crdownload、シンボリックリンク、50 MB 超、更新から 30 秒以内。

取り込み本体は core/importer.py（画面のアップロードと同じ関数）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .importer import FileImportError, import_file, import_lock
from .ledger import Ledger

log = logging.getLogger("crypto_summary.inbox")

IMPORT_DIR_ENV = "IMPORT_DIR"
POLL_ENV = "INBOX_POLL_SECONDS"
DEFAULT_POLL_SECONDS = 30
#: 更新からこの秒数は読まない（書き込み途中・同期途中）。
SETTLE_SECONDS = 30
#: これより大きいファイルは取り込まない（取引所 CSV の常識的な上限）。
MAX_FILE_BYTES = 50 * 1024 * 1024
#: 第 2 段で「フォルダ単位」として載せる取引所。今は従来の連携（PBR_CRAWL_DIR）を使う。
RESERVED_EXCHANGES: frozenset[str] = frozenset({"pbr_crawl"})
IGNORED_SUFFIXES: tuple[str, ...] = (".tmp", ".part", ".crdownload")
#: 利用者ごとの入口名（フォルダ名）に許す形。
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
#: 1 回の走査で取り込む順。SUMM の取引レポートは他の期間を置き換えるので最後。
_LAST_EXCHANGES: frozenset[str] = frozenset({"summ"})

#: inbox_files.status の値
ST_IMPORTED = "imported"    # 取り込み済み（バッチあり）
ST_EMPTY = "empty"          # 読めたが取り込む取引が無かった
ST_ERROR = "error"          # 取り込み失敗（内容が変わるまで再試行しない）
ST_TOO_LARGE = "too_large"
ST_MISSING = "missing"      # ファイルが消えた（データは残っている）
ST_DELETED = "deleted"      # データを削除済み（ファイルが変わるまで入れ直さない）
ST_PENDING = "pending"      # 整定待ち（走査結果の上書き表示のみ。保存しない）


def resolve_import_dir(value: str | Path | None = None) -> Path | None:
    raw = str(value).strip() if value is not None else (os.environ.get(IMPORT_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


def poll_seconds() -> int:
    raw = (os.environ.get(POLL_ENV) or "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_POLL_SECONDS


def exchange_keys() -> list[str]:
    """フォルダ名として使える取引所キー。"""
    from ..sources.csv_import import EXCHANGE_SOURCES

    return [k for k in EXCHANGE_SOURCES if k not in RESERVED_EXCHANGES]


# ---------------------------------------------------------------------------
# 走査
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    path: Path
    rel: str            # 入口からの相対パス（/ 区切り）
    exchange: str
    source: str
    size: int
    mtime: float

    @property
    def mtime_iso(self) -> str:
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).isoformat()


@dataclass
class ScanResult:
    root: Path
    exists: bool = True
    files: list[Candidate] = field(default_factory=list)       # 取り込み候補（整定済み）
    settling: list[Candidate] = field(default_factory=list)    # 更新直後
    too_large: list[Candidate] = field(default_factory=list)
    unknown_folders: list[str] = field(default_factory=list)   # 対応外の取引所名
    reserved_folders: list[str] = field(default_factory=list)  # pbr_crawl など
    root_files: list[str] = field(default_factory=list)        # ルート直下（取引所が決められない）
    ignored: list[str] = field(default_factory=list)           # 深すぎる階層など
    errors: list[str] = field(default_factory=list)

    def all_present(self) -> dict[str, Candidate]:
        out: dict[str, Candidate] = {}
        for c in (*self.files, *self.settling, *self.too_large):
            out[c.rel] = c
        return out


def _is_ignored_name(name: str) -> bool:
    return name.startswith(".") or name.lower().endswith(IGNORED_SUFFIXES)


def _iter_dir(path: Path, errors: list[str]) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda p: p.name)
    except OSError as e:
        errors.append(f"{path}: {e.strerror or e}")
        return []


def scan(root: Path, *, now: float | None = None,
         settle_seconds: int = SETTLE_SECONDS) -> ScanResult:
    """入口の下を 2 階層まで見て、取り込み候補を集める（ファイルは読まない）。"""
    res = ScanResult(root=root)
    if not root.is_dir():
        res.exists = False
        return res
    now = time.time() if now is None else now
    keys = set(exchange_keys())

    def _consider(file: Path, exchange: str, source: str) -> None:
        if _is_ignored_name(file.name) or file.is_symlink():
            return
        try:
            st = file.stat()
        except OSError as e:
            res.errors.append(f"{file}: {e.strerror or e}")
            return
        if not file.is_file():
            return
        rel = file.relative_to(root).as_posix()
        cand = Candidate(file, rel, exchange, source, st.st_size, st.st_mtime)
        if st.st_size > MAX_FILE_BYTES:
            res.too_large.append(cand)
        elif 0 <= now - st.st_mtime < settle_seconds:
            res.settling.append(cand)
        else:
            res.files.append(cand)

    for entry in _iter_dir(root, res.errors):
        if _is_ignored_name(entry.name) or entry.is_symlink():
            continue
        if entry.is_file():
            res.root_files.append(entry.name)
            continue
        if not entry.is_dir():
            continue
        exchange = entry.name
        if exchange in RESERVED_EXCHANGES:
            res.reserved_folders.append(exchange)
            continue
        if exchange not in keys:
            res.unknown_folders.append(exchange)
            continue
        for sub in _iter_dir(entry, res.errors):
            if _is_ignored_name(sub.name) or sub.is_symlink():
                continue
            if sub.is_file():
                _consider(sub, exchange, exchange)
            elif sub.is_dir():
                for leaf in _iter_dir(sub, res.errors):
                    if _is_ignored_name(leaf.name) or leaf.is_symlink():
                        continue
                    if leaf.is_file():
                        _consider(leaf, exchange, sub.name)
                    elif leaf.is_dir():
                        res.ignored.append(leaf.relative_to(root).as_posix() + "/")
    return res


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _order_key(c: Candidate) -> tuple:
    return (1 if c.exchange in _LAST_EXCHANGES else 0, c.mtime, c.rel)


# ---------------------------------------------------------------------------
# 同期
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_inbox(
    db_path: str | Path,
    root: Path,
    *,
    dry_run: bool = False,
    retry: set[str] | frozenset[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """入口を走査し、必要なファイルだけ取り込む。結果の要約を返す。

    retry: 内容が変わっていなくても取り込み直すパス（エラーの再試行、削除後の入れ直し）。
    """
    retry = set(retry or ())
    res = scan(root, now=now)
    report: dict[str, Any] = {
        "root": str(root),
        "exists": res.exists,
        "dry_run": dry_run,
        "imported": [],      # 新規に取り込んだ
        "replaced": [],      # 内容が変わって入れ直した
        "unchanged": 0,
        "errors": [],        # [{path, message}]
        "pending": [c.rel for c in res.settling],
        "too_large": [c.rel for c in res.too_large],
        "missing": [],
        "unknown_folders": res.unknown_folders,
        "reserved_folders": res.reserved_folders,
        "root_files": res.root_files,
        "scan_errors": res.errors,
        "scanned_at": _now_iso(),
    }
    if not res.exists:
        return report

    with import_lock(db_path):
        ledger = Ledger(db_path)
        try:
            rows = {r["path"]: r for r in ledger.inbox_files()}
            present = res.all_present()

            for cand in sorted(res.files, key=_order_key):
                row = rows.get(cand.rel)
                forced = cand.rel in retry
                settled_status = row and row["status"] in (ST_IMPORTED, ST_EMPTY, ST_ERROR, ST_DELETED)
                same_stamp = (row is not None and row.get("size") == cand.size
                              and row.get("mtime") == cand.mtime_iso)

                if row and not forced and settled_status and same_stamp:
                    # 触られていない。取り込み済みなのにバッチが消えていれば、
                    # 利用者が取り込み履歴から消したということなので入れ直さない。
                    if row["status"] == ST_IMPORTED and row.get("batch_id") \
                            and not ledger.has_import_batch(row["batch_id"]):
                        if not dry_run:
                            ledger.inbox_upsert_file({
                                "path": cand.rel, "status": ST_DELETED,
                                "message": "取り込み履歴から削除済み。入れ直すには「再取り込み」",
                            })
                    report["unchanged"] += 1
                    continue

                digest = _sha256(cand.path)
                same_content = row is not None and row.get("sha256") == digest
                if row and not forced and same_content and settled_status:
                    # mtime だけ変わった（コピーし直し等）。印だけ更新する。
                    if not dry_run:
                        ledger.inbox_upsert_file({
                            "path": cand.rel, "size": cand.size, "mtime": cand.mtime_iso,
                        })
                    report["unchanged"] += 1
                    continue
                if row and not forced and same_content and row["status"] == ST_MISSING:
                    # 一度消えて同じ内容で戻ってきた。バッチが残っていればそのまま復帰。
                    if row.get("batch_id") and ledger.has_import_batch(row["batch_id"]):
                        if not dry_run:
                            ledger.inbox_upsert_file({
                                "path": cand.rel, "status": ST_IMPORTED, "message": None,
                                "size": cand.size, "mtime": cand.mtime_iso,
                            })
                        report["unchanged"] += 1
                        continue

                old_batch = row.get("batch_id") if row else None
                had_batch = bool(old_batch and ledger.has_import_batch(old_batch))
                kind = "replaced" if had_batch else "imported"
                if dry_run:
                    report[kind].append(cand.rel)
                    continue

                if had_batch:
                    ledger.delete_import_batch(old_batch)
                base = {
                    "path": cand.rel, "exchange": cand.exchange, "source": cand.source,
                    "sha256": digest, "size": cand.size, "mtime": cand.mtime_iso,
                    "batch_id": None,
                }
                try:
                    result = import_file(
                        db_path, cand.exchange, cand.source, cand.path,
                        filename=cand.rel, origin="inbox",
                    )
                except FileImportError as e:
                    ledger.inbox_upsert_file({**base, "status": ST_ERROR, "message": str(e),
                                              "imported_at": None, "result_json": None})
                    report["errors"].append({"path": cand.rel, "message": str(e)})
                    continue
                except Exception as e:  # noqa: BLE001 - 1 ファイルの失敗で走査を止めない
                    log.exception("inbox: %s の取り込みに失敗", cand.rel)
                    msg = f"取り込みに失敗しました: {e}"
                    ledger.inbox_upsert_file({**base, "status": ST_ERROR, "message": msg,
                                              "imported_at": None, "result_json": None})
                    report["errors"].append({"path": cand.rel, "message": msg})
                    continue
                ledger.inbox_upsert_file({
                    **base,
                    "status": ST_IMPORTED if result.batch_id else ST_EMPTY,
                    "message": result.message,
                    "batch_id": result.batch_id,
                    "imported_at": _now_iso(),
                    "result_json": json.dumps(result.as_dict(), ensure_ascii=False),
                })
                report[kind].append(cand.rel)

            for cand in res.too_large:
                row = rows.get(cand.rel)
                if not dry_run and (row is None or row["status"] != ST_TOO_LARGE):
                    ledger.inbox_upsert_file({
                        "path": cand.rel, "exchange": cand.exchange, "source": cand.source,
                        "size": cand.size, "mtime": cand.mtime_iso, "status": ST_TOO_LARGE,
                        "message": f"ファイルが大きすぎます（上限 {MAX_FILE_BYTES // (1024 * 1024)} MB）",
                    })

            for rel, row in rows.items():
                if rel in present or row["status"] == ST_MISSING:
                    continue
                report["missing"].append(rel)
                if not dry_run:
                    ledger.inbox_upsert_file({
                        "path": rel, "status": ST_MISSING,
                        "message": "ファイルが無くなりました（取引は残っています）",
                    })
        finally:
            ledger.close()
    return report


def status(db_path: str | Path, root: Path, *, now: float | None = None) -> dict[str, Any]:
    """画面・CLI 用: 保存済みの状態に、いまの走査結果を重ねて返す。"""
    res = scan(root, now=now)
    ledger = Ledger(db_path)
    try:
        rows = {r["path"]: r for r in ledger.inbox_files()}
        batches = {b["id"]: b for b in ledger.list_import_batches()}
    finally:
        ledger.close()
    present = res.all_present()
    settling = {c.rel for c in res.settling}
    too_large = {c.rel for c in res.too_large}

    entries: list[dict[str, Any]] = []
    for rel in sorted(set(rows) | set(present)):
        row = rows.get(rel)
        cand = present.get(rel)
        entry: dict[str, Any] = {
            "path": rel,
            "exchange": (cand.exchange if cand else row["exchange"]),
            "source": (cand.source if cand else row["source"]),
            "size": (cand.size if cand else (row or {}).get("size")),
            "mtime": (cand.mtime_iso if cand else (row or {}).get("mtime")),
            "status": row["status"] if row else "new",
            "message": (row or {}).get("message"),
            "batch_id": (row or {}).get("batch_id"),
            "imported_at": (row or {}).get("imported_at"),
            "result": None,
            "tx_count": None,
            "changed": False,
        }
        if row and row.get("result_json"):
            try:
                entry["result"] = json.loads(row["result_json"])
            except ValueError:
                pass
        if row and row.get("batch_id"):
            b = batches.get(row["batch_id"])
            entry["tx_count"] = b["existing_count"] if b else 0
            if b is None and row["status"] == ST_IMPORTED:
                entry["status"] = ST_DELETED
        if cand is None:
            if row and row["status"] not in (ST_MISSING,):
                entry["status"] = ST_MISSING
        elif rel in settling:
            entry["status"] = ST_PENDING
        elif rel in too_large:
            entry["status"] = ST_TOO_LARGE
        elif row and row["status"] in (ST_IMPORTED, ST_EMPTY, ST_ERROR, ST_DELETED) and (
                row.get("size") != cand.size or row.get("mtime") != cand.mtime_iso):
            # 触られている。内容が同じかは次の走査で判る。
            entry["changed"] = True
        elif row and row["status"] == ST_MISSING:
            entry["status"] = "new"
        entries.append(entry)

    return {
        "root": str(root),
        "exists": res.exists,
        "files": entries,
        "unknown_folders": res.unknown_folders,
        "reserved_folders": res.reserved_folders,
        "root_files": res.root_files,
        "ignored": res.ignored,
        "scan_errors": res.errors,
        "exchanges": exchange_keys(),
        "counts": {
            "imported": sum(1 for e in entries if e["status"] == ST_IMPORTED),
            "pending": sum(1 for e in entries if e["status"] in (ST_PENDING, "new") or e["changed"]),
            "error": sum(1 for e in entries if e["status"] == ST_ERROR),
            "missing": sum(1 for e in entries if e["status"] == ST_MISSING),
        },
    }


def forget_file(db_path: str | Path, rel: str, root: Path | None = None) -> dict[str, Any]:
    """そのファイル由来の取引を削除する。

    ファイルがまだフォルダにあれば行を status=deleted で残す（次の走査で
    入れ直さないため）。無ければ行ごと消す。
    """
    with import_lock(db_path):
        ledger = Ledger(db_path)
        try:
            row = ledger.inbox_file(rel)
            if row is None:
                return {"ok": False, "deleted": 0, "message": "そのファイルの記録がありません"}
            deleted = 0
            if row.get("batch_id") and ledger.has_import_batch(row["batch_id"]):
                deleted = ledger.delete_import_batch(row["batch_id"])
            still_there = bool(root and (root / rel).is_file())
            if still_there:
                ledger.inbox_upsert_file({
                    "path": rel, "status": ST_DELETED,
                    "message": "データを削除済み。入れ直すには「再取り込み」",
                })
            else:
                ledger.inbox_delete_file(rel)
        finally:
            ledger.close()
    return {"ok": True, "deleted": deleted, "kept_row": still_there}


# ---------------------------------------------------------------------------
# 利用者ごとの入口名（マルチユーザー）
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    s = re.sub(r"-{2,}", "-", s)
    return s[:32] or "user"


class InboxRegistry:
    """入口名 ↔ 利用者（Google の sub）の対応。{data_dir}/_inbox_names.json に保存する。"""

    FILENAME = "_inbox_names.json"

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / self.FILENAME
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        names = data.get("names") if isinstance(data, dict) else None
        return dict(names) if isinstance(names, dict) else {}

    def _save(self, names: dict[str, dict[str, str]]) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "names": names}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def entries(self) -> list[dict[str, str]]:
        return [{"name": n, **v} for n, v in sorted(self._load().items())]

    def name_for(self, sub: str) -> str | None:
        for name, v in self._load().items():
            if v.get("sub") == sub:
                return name
        return None

    def resolve(self, name: str) -> str | None:
        return (self._load().get(name) or {}).get("sub")

    def ensure(self, sub: str, email: str | None) -> str:
        """この利用者の入口名を返す。無ければメールのローカル部から作る。"""
        with self._lock:
            names = self._load()
            for name, v in names.items():
                if v.get("sub") == sub:
                    return name
            base = _slug((email or "").split("@", 1)[0])
            name = base
            n = 2
            while name in names:
                name = f"{base[:29]}-{n}"
                n += 1
            names[name] = {"sub": sub, "email": email or "",
                           "created_at": _now_iso()}
            self._save(names)
            return name

    def rename(self, sub: str, new_name: str, email: str | None = None) -> str:
        new_name = new_name.strip().lower()
        if not NAME_RE.match(new_name):
            raise ValueError("入口名は英小文字・数字・- _ で 1〜32 文字にしてください（先頭は英数字）")
        with self._lock:
            names = self._load()
            owner = (names.get(new_name) or {}).get("sub")
            if owner and owner != sub:
                raise ValueError("その名前は他の利用者が使っています")
            for name in [n for n, v in names.items() if v.get("sub") == sub]:
                del names[name]
            names[new_name] = {"sub": sub, "email": email or "",
                               "created_at": _now_iso()}
            self._save(names)
            return new_name


# ---------------------------------------------------------------------------
# ポーラ
# ---------------------------------------------------------------------------

RootsProvider = Callable[[], list[tuple[str, Path]]]  # [(db_path, root), ...]


class InboxPoller:
    """一定間隔で全入口を走査するバックグラウンドスレッド。

    複数プロセス（uvicorn のワーカー）で同じディレクトリを見る構成に備え、
    lock_path の排他ロックを取れた 1 プロセスだけが走る。
    """

    def __init__(self, roots: RootsProvider, interval: int,
                 lock_path: Path | None = None) -> None:
        self.roots = roots
        self.interval = max(1, int(interval))
        self.lock_path = lock_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_fh = None
        self.active = False
        self.reason: str | None = None
        self.last_run: str | None = None
        self.last_results: dict[str, dict[str, Any]] = {}  # root → report
        self.runs = 0

    def _acquire_lock(self) -> bool:
        if self.lock_path is None:
            return True
        try:
            import fcntl
        except ImportError:  # Windows: ロック無しで動かす
            return True
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.lock_path, "a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.reason = "別のプロセスが走査しています"
            return False
        self._lock_fh = fh
        return True

    def start(self) -> bool:
        if self._thread is not None:
            return self.active
        if not self._acquire_lock():
            return False
        self.active = True
        self._thread = threading.Thread(target=self._loop, name="inbox-poller", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._lock_fh is not None:
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None
        self.active = False

    def run_once(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        try:
            roots = self.roots()
        except Exception:  # noqa: BLE001
            log.exception("inbox: 入口の一覧を取れません")
            roots = []
        for db_path, root in roots:
            try:
                results[str(root)] = sync_inbox(db_path, root)
            except Exception as e:  # noqa: BLE001 - 1 入口の失敗で止めない
                log.exception("inbox: %s の走査に失敗", root)
                results[str(root)] = {"root": str(root), "error": str(e)}
        self.last_results = results
        self.last_run = _now_iso()
        self.runs += 1
        return results

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval)

    def info(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "interval": self.interval,
            "reason": self.reason,
            "last_run": self.last_run,
            "runs": self.runs,
        }
