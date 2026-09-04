"""crypto-summary add コマンドのテスト"""
from click.testing import CliRunner

from crypto_summary.cli import cli
from crypto_summary.core.ledger import Ledger


def _run(db, *args):
    return CliRunner().invoke(cli, ["--db", str(db), *args])


def test_add_deposit(tmp_path):
    db = tmp_path / "t.db"
    res = _run(db, "add", "--source", "pbr_lending", "--type", "deposit",
               "--date", "2026-01-13", "--received", "usdc", "3000")
    assert res.exit_code == 0
    assert "追加しました" in res.output

    ledger = Ledger(db)
    bals = ledger.balances(source="pbr_lending")
    ledger.close()
    assert bals["USDC"] == 3000  # 資産は大文字に正規化される


def test_add_withdraw(tmp_path):
    db = tmp_path / "t.db"
    _run(db, "add", "--source", "s", "--type", "withdraw",
         "--date", "2026-06-02", "--sent", "XRP", "50")
    ledger = Ledger(db)
    bals = ledger.balances(source="s")
    ledger.close()
    assert bals["XRP"] == -50


def test_add_is_idempotent(tmp_path):
    """同一内容を2回追加してもスキップされる。"""
    db = tmp_path / "t.db"
    args = ["add", "--source", "s", "--type", "deposit",
            "--date", "2026-01-13", "--received", "BTC", "0.1"]
    _run(db, *args)
    res2 = _run(db, *args)
    assert "既に存在します" in res2.output

    ledger = Ledger(db)
    assert ledger.count("s") == 1
    ledger.close()


def test_add_requires_amount(tmp_path):
    """received/sent/fee いずれも無いとエラー。"""
    db = tmp_path / "t.db"
    res = _run(db, "add", "--source", "s", "--type", "deposit",
               "--date", "2026-01-01")
    assert res.exit_code != 0
    assert "いずれか1つは指定" in res.output


def test_add_with_fee_and_note(tmp_path):
    db = tmp_path / "t.db"
    res = _run(db, "add", "--source", "s", "--type", "trade",
               "--date", "2026-02-01T12:30:00",
               "--received", "BTC", "0.01", "--sent", "JPY", "100000",
               "--fee", "JPY", "500", "--note", "test trade")
    assert res.exit_code == 0

    ledger = Ledger(db)
    tx = ledger.all(source="s")[0]
    ledger.close()
    assert tx.type.value == "trade"
    assert tx.received_asset == "BTC"
    assert tx.fee_asset == "JPY"
    assert tx.fee_amount == 500
    assert tx.label == "test trade"


def test_add_updates_cursor(tmp_path):
    db = tmp_path / "t.db"
    _run(db, "add", "--source", "s", "--type", "deposit",
         "--date", "2026-03-15", "--received", "ETH", "1")
    ledger = Ledger(db)
    cursor = ledger.get_cursor("s")
    ledger.close()
    assert cursor is not None
    assert cursor.year == 2026 and cursor.month == 3 and cursor.day == 15
