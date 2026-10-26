"""crypto-summary inbox コマンドのテスト"""
import os
import time
from pathlib import Path

from click.testing import CliRunner

from crypto_summary.cli import cli
from crypto_summary.core import inbox
from crypto_summary.core.ledger import Ledger

HEADER = "timestamp,type,received_asset,received_amount,sent_asset,sent_amount,fee_asset,fee_amount,note\n"


def _run(db, *args):
    # rich の表が狭い端末幅で省略されないよう、幅を広く見せる
    return CliRunner().invoke(cli, ["--db", str(db), *args], env={"COLUMNS": "200"})


def _settled(path: Path, text: str, age: int = 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))


def test_inbox_requires_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    res = _run(tmp_path / "t.db", "inbox", "status")
    assert res.exit_code != 0 and "IMPORT_DIR" in res.output


def test_inbox_sync_status_and_forget(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    root = tmp_path / "inbox"
    monkeypatch.setenv(inbox.IMPORT_DIR_ENV, str(root))
    _settled(root / "universal" / "jan.csv",
             HEADER + "2024-01-01T00:00:00Z,deposit,BTC,1,,,,,\n")
    _settled(root / "dogecoin" / "x.csv", "x")

    res = _run(db, "inbox", "sync", "--dry-run")
    assert res.exit_code == 0, res.output
    assert "予定" in res.output and "universal/jan.csv" in res.output
    assert Ledger(db).count("universal") == 0

    res = _run(db, "inbox", "sync")
    assert res.exit_code == 0, res.output
    assert "新規 1" in res.output and "dogecoin" in res.output
    assert Ledger(db).count("universal") == 1

    res = _run(db, "inbox", "status")
    assert res.exit_code == 0, res.output
    assert "universal/jan.csv" in res.output and "imported" in res.output
    assert "universal" in res.output and "pbr_crawl" not in res.output.split("使えるフォルダ名")[1].split("\n")[0]

    res = _run(db, "inbox", "sync")
    assert "変更なし 1" in res.output

    res = _run(db, "inbox", "forget", "universal/jan.csv", "-y")
    assert res.exit_code == 0, res.output
    assert "1 件を削除" in res.output and "--retry" in res.output
    assert Ledger(db).count("universal") == 0

    res = _run(db, "inbox", "sync")
    assert "新規 0" in res.output  # 削除済みは入れ直さない
    res = _run(db, "inbox", "sync", "--retry", "universal/jan.csv")
    assert "新規 1" in res.output
    assert Ledger(db).count("universal") == 1


def test_inbox_forget_unknown(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    root.mkdir()
    monkeypatch.setenv(inbox.IMPORT_DIR_ENV, str(root))
    res = _run(tmp_path / "t.db", "inbox", "forget", "nope.csv", "-y")
    assert res.exit_code != 0 and "記録がありません" in res.output


def test_inbox_dir_option_overrides_env(tmp_path, monkeypatch):
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    root = tmp_path / "elsewhere"
    _settled(root / "universal" / "a.csv", HEADER)
    res = _run(tmp_path / "t.db", "inbox", "sync", "--dir", str(root))
    assert res.exit_code == 0, res.output
    assert "universal/a.csv" in res.output
