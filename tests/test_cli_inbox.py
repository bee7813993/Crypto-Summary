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


def test_inbox_sync_includes_legacy_pbr(tmp_path, monkeypatch):
    import json
    from crypto_summary.sources.jp.pbr_crawl import ARTIFACT_NAME, ENV_VAR, MARKER_NAME, SOURCE_ID

    outputs = tmp_path / "outputs"
    _settled(outputs / ARTIFACT_NAME, json.dumps({
        "start_date": "2026-01-01", "end_date": "2026-03-05", "currencies": ["BTC"],
        "daily_ranges": [{"date_from": "2026-03-03", "date_to": "2026-03-05",
                          "currency": "BTC", "daily_expected_interest": "0.00001"}],
        "wallet_events": [], "transfer_events": [], "balance_snapshots": [], "warnings": [],
    }))
    _settled(outputs / MARKER_NAME, json.dumps({
        "runId": "r1", "phase": "done", "failedCurrencies": [],
        "finishedAt": "2026-03-05T10:03:00.000Z"}))
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    monkeypatch.setenv(ENV_VAR, str(outputs))
    db = tmp_path / "t.db"

    res = _run(db, "inbox", "status")
    assert res.exit_code == 0, res.output
    assert "PBR_CRAWL_DIR" in res.output and f"{SOURCE_ID}/@{ENV_VAR}" in res.output

    res = _run(db, "inbox", "sync")
    assert res.exit_code == 0, res.output
    assert "新規 1" in res.output and Ledger(db).count(SOURCE_ID) == 3

    # 互換コマンドも同じ記録を更新する（二重には入らない）
    res = _run(db, "sync-pbr", "--yes")
    assert res.exit_code == 0, res.output
    assert Ledger(db).count(SOURCE_ID) == 3
    res = _run(db, "inbox", "sync")
    assert "変更なし 1" in res.output
