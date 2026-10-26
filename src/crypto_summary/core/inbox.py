"""取り込みフォルダ（inbox）— フォルダに置かれた取引履歴ファイルを台帳へ反映する。

設計: docs/inbox-design.md

フォルダの規約:
  IMPORT_DIR/
    <exchange>/              EXCHANGE_SOURCES のキー（bybit, gmo, pbr, nexo_auto, universal …）
      *.csv / *.json         source_id = <exchange>
      <account>/             同じ取引所の別口座。source_id = <account>
        *.csv
  マルチユーザーでは IMPORT_DIR/<inbox-name>/ が利用者ごとの入口（InboxRegistry）。

フォルダ単位の取引所:
  PBR Lending クローラーのように複数ファイルをまとめて 1 回で評価する取引所は、
  <exchange>/ ディレクトリ全体を 1 単位として扱う（_folder_adapters）。アダプタが
  inspect()（取り込める材料・署名・ブロック判定）と sync()（期間の洗い替え等）を持ち、
  汎用層は署名が変わったときだけ sync() を呼んで inbox_files に 1 行で記録する。
  従来の環境変数（PBR_CRAWL_DIR）が指すディレクトリも「入口の外にあるフォルダ単位」
  として同じ経路で扱う（legacy_folders）。

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
ST_BLOCKED = "blocked"      # フォルダ単位: 取り込みを止める状態（クロール異常終了など）。force で上書き可


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


def _folder_adapters() -> dict[str, Any]:
    """フォルダ単位で扱う取引所 → アダプタ。sources は core を読むのでここで遅延 import。"""
    from ..sources.jp.pbr_crawl import FOLDER_ADAPTER as pbr

    return {pbr.exchange: pbr}


def folder_exchange_keys() -> list[str]:
    """フォルダ単位で取り込む取引所キー（<exchange>/ ディレクトリ全体で 1 単位）。"""
    return list(_folder_adapters())


def exchange_keys() -> list[str]:
    """ファイル単位で取り込める取引所キー（フォルダ名として使える名前）。"""
    from ..sources.csv_import import EXCHANGE_SOURCES

    folder = set(_folder_adapters())
    return [k for k in EXCHANGE_SOURCES if k not in folder]


def _is_folder_key(rel: str) -> bool:
    """inbox_files.path がフォルダ単位の行か（"pbr_crawl/" または "pbr_crawl/@PBR_CRAWL_DIR"）。"""
    return rel.endswith("/") or "/@" in rel


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
class FolderUnit:
    """フォルダ単位で取り込む取引所の 1 単位。"""
    exchange: str
    directory: Path
    key: str            # inbox_files.path。入口内なら "pbr_crawl/"、外なら "pbr_crawl/@<ラベル>"
    legacy: bool = False
    label: str | None = None   # 入口の外にある場合の由来（環境変数名など）


#: 入口の外にあるフォルダ単位の指定: (取引所, ディレクトリ, 由来のラベル)
LegacyFolder = tuple[str, Path, str]


@dataclass
class ScanResult:
    root: Path | None
    exists: bool = True
    files: list[Candidate] = field(default_factory=list)       # 取り込み候補（整定済み）
    settling: list[Candidate] = field(default_factory=list)    # 更新直後
    too_large: list[Candidate] = field(default_factory=list)
    folders: list[FolderUnit] = field(default_factory=list)    # フォルダ単位の取引所
    unknown_folders: list[str] = field(default_factory=list)   # 対応外の取引所名
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


def scan(root: Path | None, *, now: float | None = None,
         settle_seconds: int = SETTLE_SECONDS,
         legacy_folders: list[LegacyFolder] | None = None) -> ScanResult:
    """入口の下を 2 階層まで見て、取り込み候補を集める（ファイルは読まない）。

    legacy_folders は入口の外にあるフォルダ単位（従来の PBR_CRAWL_DIR など）。
    入口が無くてもそれらは評価する。
    """
    res = ScanResult(root=root)
    for exchange, directory, label in legacy_folders or []:
        res.folders.append(FolderUnit(exchange, Path(directory), f"{exchange}/@{label}",
                                      legacy=True, label=label))
    if root is None or not root.is_dir():
        res.exists = False
        return res
    now = time.time() if now is None else now
    keys = set(exchange_keys())
    folder_keys = set(_folder_adapters())

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
        if exchange in folder_keys:
            res.folders.append(FolderUnit(exchange, entry, f"{exchange}/"))
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


def _sig_stamp(adapter: Any, signature: str) -> str:
    """フォルダ単位の署名。取り込み規則の版も含める（版が上がれば同じ入力でも取り直す）。"""
    return f"v{adapter.format_version}:{signature}"


def _folder_result_json(result: dict[str, Any]) -> str:
    keep = {k: result.get(k) for k in (
        "parsed", "parsed_viewer", "inserted", "deleted", "skipped", "skip_reasons",
        "window", "crawl_window_start", "forced", "sync_warnings", "crawl_warnings",
        "run_id", "batch_id",
    ) if k in result}
    return json.dumps(keep, ensure_ascii=False, default=str)


def _run_folder_unit(
    ledger: Ledger, db_path: str | Path, unit: FolderUnit, adapter: Any, row: dict | None,
    *, forced: bool, force: bool, dry_run: bool, report: dict[str, Any],
) -> None:
    """フォルダ単位の 1 取引所を評価し、必要なら sync() する。"""
    key = unit.key
    try:
        info = adapter.inspect(unit.directory)
    except Exception as e:  # noqa: BLE001
        report["errors"].append({"path": key, "message": f"状態を読めません: {e}"})
        return
    if not info.get("has_data"):
        if row is not None and row["status"] != ST_MISSING:
            report["missing"].append(key)
            if not dry_run:
                ledger.inbox_upsert_file({
                    "path": key, "status": ST_MISSING,
                    "message": "取り込めるデータがありません（取引は残っています）",
                })
        return
    stamp = _sig_stamp(adapter, info.get("signature") or "")
    settled = row is not None and row["status"] in (ST_IMPORTED, ST_ERROR, ST_DELETED)
    if row is not None and not forced and settled and row.get("sha256") == stamp:
        if row["status"] == ST_IMPORTED and row.get("batch_id") \
                and not ledger.has_import_batch(row["batch_id"]):
            if not dry_run:
                ledger.inbox_upsert_file({
                    "path": key, "status": ST_DELETED,
                    "message": "取り込み履歴から削除済み。入れ直すには「再取り込み」",
                })
        report["unchanged"] += 1
        return
    if info.get("blocked") and not force:
        # クロール異常終了 / 整定中。自動では取り込まない（画面・CLI から force できる）。
        if info.get("settling"):
            report["pending"].append(key)
        else:
            report["blocked"].append({"path": key, "message": "／".join(info.get("warnings") or ["取り込みを見送りました"])})
        return
    had_batch = bool(row is not None and row.get("batch_id")
                     and ledger.has_import_batch(row["batch_id"]))
    kind = "replaced" if had_batch else "imported"
    if dry_run:
        report[kind].append(key)
        return
    base = {"path": key, "exchange": unit.exchange, "source": adapter.source,
            "sha256": stamp, "size": None, "mtime": None}
    try:
        result = adapter.sync(db_path, unit.directory, force=force)
    except adapter.error_class as e:
        msg = getattr(e, "message", str(e))
        ledger.inbox_upsert_file({**base, "status": ST_ERROR, "message": msg,
                                  "batch_id": row.get("batch_id") if row else None})
        report["errors"].append({"path": key, "message": msg})
        return
    except Exception as e:  # noqa: BLE001
        log.exception("inbox: %s の取り込みに失敗", key)
        msg = f"取り込みに失敗しました: {e}"
        ledger.inbox_upsert_file({**base, "status": ST_ERROR, "message": msg,
                                  "batch_id": row.get("batch_id") if row else None})
        report["errors"].append({"path": key, "message": msg})
        return
    ledger.inbox_upsert_file({
        **base, "status": ST_IMPORTED, "message": None,
        "batch_id": result.get("batch_id"),
        "imported_at": _now_iso(),
        "result_json": _folder_result_json(result),
    })
    report[kind].append(key)


def sync_unit(
    db_path: str | Path, root: Path | None, key: str, *,
    force: bool = False, dry_run: bool = False,
    legacy_folders: list[LegacyFolder] | None = None,
) -> dict[str, Any]:
    """フォルダ単位の 1 取引所を、状態に関係なく今すぐ sync() する（従来 API の互換経路）。

    アダプタの結果をそのまま返し、アダプタのエラーもそのまま上げる。
    成功・失敗は inbox_files にも記録して、画面の一覧と食い違わないようにする。
    """
    res = scan(root, legacy_folders=legacy_folders)
    unit = next((u for u in res.folders if u.key == key), None)
    if unit is None:
        raise KeyError(key)
    adapter = _folder_adapters()[unit.exchange]
    with import_lock(db_path):
        if dry_run:
            return adapter.sync(db_path, unit.directory, force=force, dry_run=True)
        ledger = Ledger(db_path)
        try:
            row = ledger.inbox_file(key)
            base = {"path": key, "exchange": unit.exchange, "source": adapter.source,
                    "size": None, "mtime": None}
            try:
                result = adapter.sync(db_path, unit.directory, force=force)
            except adapter.error_class as e:
                code = getattr(e, "code", "")
                if code not in ("not_configured",):
                    ledger.inbox_upsert_file({
                        **base, "status": ST_ERROR, "message": getattr(e, "message", str(e)),
                        "batch_id": row.get("batch_id") if row else None,
                    })
                raise
            try:
                info = adapter.inspect(unit.directory)
                stamp = _sig_stamp(adapter, info.get("signature") or "")
            except Exception:  # noqa: BLE001
                stamp = None
            ledger.inbox_upsert_file({
                **base, "sha256": stamp, "status": ST_IMPORTED, "message": None,
                "batch_id": result.get("batch_id"),
                "imported_at": _now_iso(),
                "result_json": _folder_result_json(result),
            })
        finally:
            ledger.close()
    return result


def sync_inbox(
    db_path: str | Path,
    root: Path | None,
    *,
    dry_run: bool = False,
    retry: set[str] | frozenset[str] | None = None,
    force: bool = False,
    legacy_folders: list[LegacyFolder] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """入口を走査し、必要なファイルだけ取り込む。結果の要約を返す。

    retry: 内容が変わっていなくても取り込み直すパス（エラーの再試行、削除後の入れ直し）。
    force: フォルダ単位の取引所で、ブロック状態（クロール異常終了など）でも取り込む。
    legacy_folders: 入口の外にあるフォルダ単位（従来の PBR_CRAWL_DIR など）。
    """
    retry = set(retry or ())
    res = scan(root, now=now, legacy_folders=legacy_folders)
    report: dict[str, Any] = {
        "root": str(root) if root is not None else None,
        "exists": res.exists,
        "dry_run": dry_run,
        "imported": [],      # 新規に取り込んだ
        "replaced": [],      # 内容が変わって入れ直した
        "unchanged": 0,
        "errors": [],        # [{path, message}]
        "blocked": [],       # [{path, message}] フォルダ単位で取り込みを見送った
        "pending": [c.rel for c in res.settling],
        "too_large": [c.rel for c in res.too_large],
        "missing": [],
        "unknown_folders": res.unknown_folders,
        "root_files": res.root_files,
        "scan_errors": res.errors,
        "scanned_at": _now_iso(),
    }
    if not res.exists and not res.folders:
        return report

    adapters = _folder_adapters()
    with import_lock(db_path):
        ledger = Ledger(db_path)
        try:
            rows = {r["path"]: r for r in ledger.inbox_files()}
            present = res.all_present()
            for unit in res.folders:
                present[unit.key] = unit  # type: ignore[assignment]
                _run_folder_unit(
                    ledger, db_path, unit, adapters[unit.exchange], rows.get(unit.key),
                    forced=unit.key in retry, force=force, dry_run=dry_run, report=report,
                )

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


def _folder_entry(db_path: str | Path, unit: FolderUnit, adapter: Any,
                  row: dict | None, batches: dict[str, dict]) -> dict[str, Any]:
    """status() に載せるフォルダ単位の 1 行。"""
    try:
        info = adapter.inspect(unit.directory)
    except Exception as e:  # noqa: BLE001
        info = {"has_data": False, "signature": "", "blocked": False, "settling": False,
                "healthy": False, "warnings": [f"状態を読めません: {e}"], "detail": {}}
    entry: dict[str, Any] = {
        "path": unit.key,
        "kind": "folder",
        "legacy": unit.legacy,
        "legacy_label": unit.label,
        "directory": str(unit.directory),
        "exchange": unit.exchange,
        "source": adapter.source,
        "size": None, "mtime": None,
        "status": row["status"] if row else "new",
        "message": (row or {}).get("message"),
        "batch_id": (row or {}).get("batch_id"),
        "imported_at": (row or {}).get("imported_at"),
        "result": None,
        "tx_count": None,
        "changed": False,
        "blocked": bool(info.get("blocked")),
        "warnings": list(info.get("warnings") or []),
        "detail": info.get("detail") or {},
        "state": adapter.last_state(db_path),
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
    stamp = _sig_stamp(adapter, info.get("signature") or "")
    if not info.get("has_data"):
        entry["status"] = ST_MISSING if row else ST_EMPTY
        if not row:
            entry["message"] = "取り込めるデータがありません"
    elif info.get("settling"):
        entry["status"] = ST_PENDING
    elif row is None or row.get("sha256") != stamp:
        if row is not None and row["status"] in (ST_IMPORTED, ST_ERROR, ST_DELETED, ST_MISSING):
            entry["changed"] = True
            if row["status"] == ST_MISSING:
                entry["status"] = "new"
        if info.get("blocked"):
            entry["status"] = ST_BLOCKED
            entry["message"] = "／".join(info.get("warnings") or [])
    return entry


def status(db_path: str | Path, root: Path | None, *, now: float | None = None,
           legacy_folders: list[LegacyFolder] | None = None) -> dict[str, Any]:
    """画面・CLI 用: 保存済みの状態に、いまの走査結果を重ねて返す。"""
    res = scan(root, now=now, legacy_folders=legacy_folders)
    ledger = Ledger(db_path)
    try:
        rows = {r["path"]: r for r in ledger.inbox_files()}
        batches = {b["id"]: b for b in ledger.list_import_batches()}
    finally:
        ledger.close()
    present = res.all_present()
    settling = {c.rel for c in res.settling}
    too_large = {c.rel for c in res.too_large}
    adapters = _folder_adapters()
    units = {u.key: u for u in res.folders}

    entries: list[dict[str, Any]] = []
    for rel in sorted(set(rows) | set(present) | set(units)):
        row = rows.get(rel)
        if rel in units:
            entries.append(_folder_entry(db_path, units[rel], adapters[units[rel].exchange], row, batches))
            continue
        if _is_folder_key(rel):
            # 記録はあるが、いまはフォルダが無い / 従来の設定が外れた
            entries.append({
                "path": rel, "kind": "folder", "legacy": "/@" in rel,
                "exchange": row["exchange"], "source": row["source"],
                "size": None, "mtime": None,
                "status": ST_MISSING, "message": row.get("message"),
                "batch_id": row.get("batch_id"), "imported_at": row.get("imported_at"),
                "result": None, "tx_count": None, "changed": False,
            })
            continue
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
        "root": str(root) if root is not None else None,
        "exists": res.exists,
        "files": entries,
        "unknown_folders": res.unknown_folders,
        "root_files": res.root_files,
        "ignored": res.ignored,
        "scan_errors": res.errors,
        "exchanges": exchange_keys(),
        "folder_exchanges": folder_exchange_keys(),
        "counts": {
            "imported": sum(1 for e in entries if e["status"] == ST_IMPORTED),
            "pending": sum(1 for e in entries if e["status"] in (ST_PENDING, "new") or e["changed"]),
            "blocked": sum(1 for e in entries if e["status"] == ST_BLOCKED),
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
            if _is_folder_key(rel):
                # フォルダ単位: 入口内ならディレクトリの有無、従来の設定なら設定が
                # 残っている前提で「まだある」とみなし、次の走査で入れ直さない。
                still_there = ("/@" in rel) or bool(root and (root / rel).is_dir())
            else:
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

#: [(db_path, root), ...] または [(db_path, root, legacy_folders), ...]。root は None でもよい。
RootsProvider = Callable[[], list[tuple]]


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
        for item in roots:
            db_path, root = item[0], item[1]
            legacy = list(item[2]) if len(item) > 2 and item[2] else None
            label = str(root) if root is not None else f"{db_path}#legacy"
            try:
                results[label] = sync_inbox(db_path, root, legacy_folders=legacy)
            except Exception as e:  # noqa: BLE001 - 1 入口の失敗で止めない
                log.exception("inbox: %s の走査に失敗", label)
                results[label] = {"root": str(root), "error": str(e)}
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
