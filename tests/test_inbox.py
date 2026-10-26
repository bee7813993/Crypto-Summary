"""取り込みフォルダ（core/inbox.py）のテスト。

汎用 CSV（universal）を置いて、設計どおりの意味論になることを確かめる:
  新規 → 取り込み / 同一 → 何もしない / 更新 → 旧バッチを消して入れ直す /
  消失 → データは残す / エラー → 内容が変わるまで再試行しない /
  履歴から消されたバッチは勝手に入れ直さない
"""
import os
import time
from pathlib import Path

import pytest

from crypto_summary.core import inbox
from crypto_summary.core.inbox import (
    InboxPoller,
    InboxRegistry,
    forget_file,
    scan,
    status,
    sync_inbox,
)
from crypto_summary.core.ledger import Ledger

HEADER = "timestamp,type,received_asset,received_amount,sent_asset,sent_amount,fee_asset,fee_amount,note\n"


def _csv(rows: list[tuple[str, str, str]]) -> str:
    """(日時, 資産, 数量) の入金行を汎用 CSV にする。"""
    return HEADER + "".join(f"{ts},deposit,{asset},{amt},,,,,\n" for ts, asset, amt in rows)


def _settled(path: Path, text: str, age: int = 120) -> None:
    """ファイルを書き、更新直後の「整定待ち」に入らないよう時刻を戻す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "inbox"
    r.mkdir()
    return r


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def _balance(db: str, source: str, asset: str):
    ledger = Ledger(db)
    try:
        return ledger.balances(source=source).get(asset)
    finally:
        ledger.close()


def _count(db: str, source: str) -> int:
    ledger = Ledger(db)
    try:
        return ledger.count(source)
    finally:
        ledger.close()


# ---- 走査 ----

def test_scan_conventions(root: Path):
    _settled(root / "universal" / "a.csv", "x")
    _settled(root / "universal" / "sub1" / "b.csv", "x")
    _settled(root / "universal" / "sub1" / "deeper" / "c.csv", "x")
    _settled(root / "universal" / ".hidden.csv", "x")
    _settled(root / "universal" / "partial.csv.part", "x")
    _settled(root / "universal" / ".syncthing.a.csv.tmp", "x")
    _settled(root / "dogecoin" / "d.csv", "x")
    _settled(root / "pbr_crawl" / "e.json", "x")
    _settled(root / "stray.csv", "x")
    (root / ".stfolder").mkdir()
    res = scan(root)
    assert [(c.rel, c.exchange, c.source) for c in res.files] == [
        ("universal/a.csv", "universal", "universal"),
        ("universal/sub1/b.csv", "universal", "sub1"),
    ]
    assert res.unknown_folders == ["dogecoin"]
    assert res.reserved_folders == ["pbr_crawl"]
    assert res.root_files == ["stray.csv"]
    assert res.ignored == ["universal/sub1/deeper/"]


def test_scan_settling_and_too_large(root: Path, monkeypatch):
    fresh = root / "universal" / "fresh.csv"
    fresh.parent.mkdir()
    fresh.write_text("x")  # いま書いた → 整定待ち
    big = root / "universal" / "big.csv"
    _settled(big, "xxx")
    monkeypatch.setattr(inbox, "MAX_FILE_BYTES", 2)
    res = scan(root)
    assert [c.rel for c in res.settling] == ["universal/fresh.csv"]
    assert [c.rel for c in res.too_large] == ["universal/big.csv"]
    assert res.files == []


def test_scan_missing_root(tmp_path: Path):
    res = scan(tmp_path / "nope")
    assert res.exists is False and res.files == []


def test_scan_skips_symlinks(root: Path, tmp_path: Path):
    outside = tmp_path / "outside.csv"
    outside.write_text("x")
    (root / "universal").mkdir()
    (root / "universal" / "link.csv").symlink_to(outside)
    assert scan(root).files == []


# ---- 同期の意味論 ----

def test_new_file_is_imported_as_batch(root: Path, db: str):
    _settled(root / "universal" / "jan.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    report = sync_inbox(db, root)
    assert report["imported"] == ["universal/jan.csv"]
    assert report["replaced"] == [] and report["errors"] == []
    assert _balance(db, "universal", "BTC") == 1
    ledger = Ledger(db)
    try:
        rows = ledger.inbox_files()
        batches = ledger.list_import_batches()
    finally:
        ledger.close()
    assert rows[0]["status"] == "imported" and rows[0]["batch_id"] == batches[0]["id"]
    assert batches[0]["origin"] == "inbox" and batches[0]["filename"] == "universal/jan.csv"


def test_unchanged_file_is_not_reread(root: Path, db: str, monkeypatch):
    _settled(root / "universal" / "jan.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    calls = []
    monkeypatch.setattr(inbox, "_sha256", lambda p: calls.append(p) or "never")
    report = sync_inbox(db, root)
    assert report["unchanged"] == 1 and report["imported"] == []
    assert calls == []  # size/mtime が同じなら読まない
    assert _count(db, "universal") == 1


def test_touched_but_same_content_is_unchanged(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    text = _csv([("2024-01-01T00:00:00Z", "BTC", "1")])
    _settled(path, text)
    first = sync_inbox(db, root)
    _settled(path, text, age=60)  # コピーし直し: mtime だけ変わる
    report = sync_inbox(db, root)
    assert report["unchanged"] == 1 and report["replaced"] == []
    ledger = Ledger(db)
    try:
        assert len(ledger.list_import_batches()) == 1
    finally:
        ledger.close()
    assert first["imported"] == ["universal/jan.csv"]


def test_changed_file_replaces_previous_batch(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    # 取引所が訂正版を出した: 1 → 0.5
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "0.5")]), age=60)
    report = sync_inbox(db, root)
    assert report["replaced"] == ["universal/jan.csv"]
    assert _balance(db, "universal", "BTC") == pytest.approx(0.5)
    assert _count(db, "universal") == 1  # 旧行は消えている
    ledger = Ledger(db)
    try:
        assert len(ledger.list_import_batches()) == 1
    finally:
        ledger.close()


def test_missing_file_keeps_data(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    path.unlink()
    report = sync_inbox(db, root)
    assert report["missing"] == ["universal/jan.csv"]
    assert _balance(db, "universal", "BTC") == 1
    st = status(db, root)
    assert st["files"][0]["status"] == "missing"
    # 2 回目の走査では missing を繰り返し報告しない
    assert sync_inbox(db, root)["missing"] == []


def test_missing_file_returning_unchanged_restores_status(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    text = _csv([("2024-01-01T00:00:00Z", "BTC", "1")])
    _settled(path, text)
    sync_inbox(db, root)
    path.unlink()
    sync_inbox(db, root)
    _settled(path, text, age=60)
    report = sync_inbox(db, root)
    assert report["unchanged"] == 1 and report["imported"] == []
    assert status(db, root)["files"][0]["status"] == "imported"


def test_error_is_recorded_and_not_retried_until_changed(root: Path, db: str, monkeypatch):
    path = root / "universal" / "bad.csv"
    _settled(path, "not,a,universal,csv\n1,2,3,4\n")
    report = sync_inbox(db, root)
    assert [e["path"] for e in report["errors"]] == ["universal/bad.csv"]
    assert "解析" in report["errors"][0]["message"]
    calls = []
    monkeypatch.setattr(inbox, "_sha256", lambda p: calls.append(p) or "x")
    again = sync_inbox(db, root)
    assert again["errors"] == [] and again["unchanged"] == 1 and calls == []
    monkeypatch.undo()
    # 直して置き直したら取り込まれる
    _settled(path, _csv([("2024-01-01T00:00:00Z", "ETH", "2")]), age=60)
    fixed = sync_inbox(db, root)
    assert fixed["imported"] == ["universal/bad.csv"]
    assert status(db, root)["files"][0]["status"] == "imported"


def test_retry_forces_reimport(root: Path, db: str):
    path = root / "universal" / "bad.csv"
    _settled(path, "not,a,universal,csv\n1,2,3,4\n")
    sync_inbox(db, root)
    report = sync_inbox(db, root, retry={"universal/bad.csv"})
    assert [e["path"] for e in report["errors"]] == ["universal/bad.csv"]  # 再試行された


def test_batch_deleted_elsewhere_is_respected(root: Path, db: str):
    """取り込み履歴からバッチを消した利用者の意図を、次の走査で覆さない。"""
    path = root / "universal" / "jan.csv"
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    ledger = Ledger(db)
    try:
        ledger.delete_import_batch(ledger.list_import_batches()[0]["id"])
    finally:
        ledger.close()
    report = sync_inbox(db, root)
    assert report["imported"] == [] and report["unchanged"] == 1
    assert _count(db, "universal") == 0
    st = status(db, root)
    assert st["files"][0]["status"] == "deleted"
    # 明示的な再取り込みなら入る
    report = sync_inbox(db, root, retry={"universal/jan.csv"})
    assert report["imported"] == ["universal/jan.csv"]
    assert _count(db, "universal") == 1


def test_empty_file_is_recorded_as_empty(root: Path, db: str):
    _settled(root / "universal" / "empty.csv", HEADER)
    report = sync_inbox(db, root)
    assert report["imported"] == ["universal/empty.csv"]
    st = status(db, root)
    assert st["files"][0]["status"] == "empty"
    assert st["files"][0]["batch_id"] is None


def test_account_folder_sets_source_id(root: Path, db: str):
    _settled(root / "universal" / "main" / "a.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    _settled(root / "universal" / "sub" / "b.csv", _csv([("2024-01-02T00:00:00Z", "BTC", "2")]))
    sync_inbox(db, root)
    assert _balance(db, "main", "BTC") == 1
    assert _balance(db, "sub", "BTC") == 2


def test_same_content_in_two_files_does_not_duplicate(root: Path, db: str):
    text = _csv([("2024-01-01T00:00:00Z", "BTC", "1")])
    _settled(root / "universal" / "a.csv", text)
    _settled(root / "universal" / "a-copy.csv", text)
    sync_inbox(db, root)
    assert _count(db, "universal") == 1  # 取引 ID は内容から決まる
    ledger = Ledger(db)
    try:
        assert len(ledger.list_import_batches()) == 2
    finally:
        ledger.close()


def test_dry_run_changes_nothing(root: Path, db: str):
    _settled(root / "universal" / "jan.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    report = sync_inbox(db, root, dry_run=True)
    assert report["imported"] == ["universal/jan.csv"] and report["dry_run"]
    assert _count(db, "universal") == 0
    assert Ledger(db).inbox_files() == []


def test_summ_is_processed_last(root: Path, db: str, monkeypatch):
    order = []
    real = inbox.import_file

    def spy(db_path, exchange, source, path, **kw):
        order.append(exchange)
        return real(db_path, exchange, source, path, **kw)

    monkeypatch.setattr(inbox, "import_file", spy)
    _settled(root / "summ" / "report.csv", "x", age=300)      # 古いが最後に回る
    _settled(root / "universal" / "a.csv", HEADER, age=100)
    sync_inbox(db, root)
    assert order == ["universal", "summ"]


def test_status_overlay(root: Path, db: str, monkeypatch):
    _settled(root / "universal" / "done.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    (root / "universal" / "fresh.csv").write_text(HEADER)  # 整定待ち
    _settled(root / "universal" / "new.csv", HEADER)
    _settled(root / "unknownex" / "z.csv", "x")
    st = status(db, root)
    by = {e["path"]: e for e in st["files"]}
    assert by["universal/done.csv"]["status"] == "imported"
    assert by["universal/done.csv"]["tx_count"] == 1
    assert by["universal/fresh.csv"]["status"] == "pending"
    assert by["universal/new.csv"]["status"] == "new"
    assert st["unknown_folders"] == ["unknownex"]
    assert "universal" in st["exchanges"] and "pbr_crawl" not in st["exchanges"]
    assert st["counts"]["imported"] == 1 and st["counts"]["pending"] == 2


def test_forget_present_file_marks_deleted(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    r = forget_file(db, "universal/jan.csv", root)
    assert r["ok"] and r["deleted"] == 1 and r["kept_row"]
    assert _count(db, "universal") == 0
    # ファイルが残っているので次の走査でも入れ直さない
    assert sync_inbox(db, root)["imported"] == []
    assert status(db, root)["files"][0]["status"] == "deleted"


def test_forget_missing_file_removes_row(root: Path, db: str):
    path = root / "universal" / "jan.csv"
    _settled(path, _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    sync_inbox(db, root)
    path.unlink()
    sync_inbox(db, root)
    r = forget_file(db, "universal/jan.csv", root)
    assert r["ok"] and r["deleted"] == 1 and not r["kept_row"]
    assert status(db, root)["files"] == []


def test_forget_unknown_path(db: str, root: Path):
    assert forget_file(db, "universal/none.csv", root)["ok"] is False


# ---- 入口名 ----

def test_registry_default_name_and_uniqueness(tmp_path: Path):
    reg = InboxRegistry(tmp_path)
    assert reg.ensure("111", "Taro.Yamada+cs@example.com") == "taro-yamada-cs"
    assert reg.ensure("111", "other@example.com") == "taro-yamada-cs"  # 2 回目は同じ
    assert reg.ensure("222", "taro.yamada+cs@example.com") == "taro-yamada-cs-2"
    assert reg.ensure("333", None) == "user"
    assert reg.resolve("taro-yamada-cs-2") == "222"
    assert reg.name_for("333") == "user"
    assert [e["name"] for e in reg.entries()] == ["taro-yamada-cs", "taro-yamada-cs-2", "user"]


def test_registry_rename_validation(tmp_path: Path):
    reg = InboxRegistry(tmp_path)
    reg.ensure("111", "a@example.com")
    reg.ensure("222", "b@example.com")
    assert reg.rename("111", "Cold-Storage") == "cold-storage"
    assert reg.name_for("111") == "cold-storage" and reg.resolve("a") is None
    with pytest.raises(ValueError, match="他の利用者"):
        reg.rename("111", "b")
    with pytest.raises(ValueError, match="入口名"):
        reg.rename("111", "../etc")
    with pytest.raises(ValueError, match="入口名"):
        reg.rename("111", "")


# ---- ポーラ ----

def test_poller_run_once(root: Path, db: str):
    _settled(root / "universal" / "jan.csv", _csv([("2024-01-01T00:00:00Z", "BTC", "1")]))
    p = InboxPoller(lambda: [(db, root)], interval=1)
    results = p.run_once()
    assert results[str(root)]["imported"] == ["universal/jan.csv"]
    assert p.info()["runs"] == 1 and p.info()["last_run"]
    assert p.run_once()[str(root)]["unchanged"] == 1


def test_poller_lock_allows_one_process(root: Path, db: str, tmp_path: Path):
    lock = tmp_path / ".inbox.lock"
    p1 = InboxPoller(lambda: [(db, root)], interval=60, lock_path=lock)
    assert p1.start() is True and p1.active
    try:
        p2 = InboxPoller(lambda: [(db, root)], interval=60, lock_path=lock)
        assert p2.start() is False and p2.reason and not p2.active
    finally:
        p1.stop()
    assert p1.active is False
    # 解放後は取れる
    p3 = InboxPoller(lambda: [(db, root)], interval=60, lock_path=lock)
    assert p3.start() is True
    p3.stop()


def test_poller_survives_root_failure(db: str, tmp_path: Path):
    p = InboxPoller(lambda: [(db, tmp_path / "nope"), ("/nonexistent/dir/x.db", tmp_path)], interval=1)
    results = p.run_once()
    assert results[str(tmp_path / "nope")]["exists"] is False
    assert "error" in results[str(tmp_path)]


def test_env_helpers(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    monkeypatch.delenv(inbox.POLL_ENV, raising=False)
    assert inbox.resolve_import_dir() is None
    assert inbox.poll_seconds() == inbox.DEFAULT_POLL_SECONDS
    monkeypatch.setenv(inbox.IMPORT_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(inbox.POLL_ENV, "0")
    assert inbox.resolve_import_dir() == tmp_path
    assert inbox.poll_seconds() == 0
    monkeypatch.setenv(inbox.POLL_ENV, "abc")
    assert inbox.poll_seconds() == inbox.DEFAULT_POLL_SECONDS
