"""crypto-summary remove コマンドのテスト"""
from click.testing import CliRunner

from crypto_summary.cli import cli
from crypto_summary.core.ledger import Ledger


def _run(db, *args, input=None):
    return CliRunner().invoke(cli, ["--db", str(db), *args], input=input)


def _add(db, **kwargs):
    defaults = dict(
        source="s", tx_type="deposit", date_str="2026-01-01", received=("BTC", "0.5")
    )
    defaults.update(kwargs)
    return _run(
        db, "add",
        "--source", defaults["source"],
        "--type", defaults["tx_type"],
        "--date", defaults["date_str"],
        "--received", defaults["received"][0], defaults["received"][1],
    )


def test_remove_by_id(tmp_path):
    db = tmp_path / "t.db"
    res = _add(db)
    assert res.exit_code == 0
    # ID は "id=xxxx" の形式で出力される
    tx_id = res.output.split("id=")[-1].strip()

    res2 = _run(db, "remove", "--id", tx_id, "--yes")
    assert res2.exit_code == 0
    assert "削除しました" in res2.output

    ledger = Ledger(db)
    assert ledger.count("s") == 0
    ledger.close()


def test_remove_unknown_id(tmp_path):
    db = tmp_path / "t.db"
    res = _run(db, "remove", "--id", "nonexistentid", "--yes")
    assert res.exit_code == 0
    assert "見つかりません" in res.output


def test_remove_cancel(tmp_path):
    db = tmp_path / "t.db"
    _add(db)

    ledger = Ledger(db)
    txs = ledger.all(limit=1)
    tx_id = txs[0].id
    ledger.close()

    # confirm に "n" を渡してキャンセル
    res = _run(db, "remove", "--id", tx_id, input="n\n")
    assert "キャンセル" in res.output

    ledger = Ledger(db)
    assert ledger.count("s") == 1
    ledger.close()
