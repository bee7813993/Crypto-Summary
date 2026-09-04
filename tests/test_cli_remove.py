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


def test_remove_requires_id_or_file(tmp_path):
    db = tmp_path / "t.db"
    res = _run(db, "remove", "--yes")
    assert res.exit_code != 0
    assert "--id か --file" in res.output


_UNIVERSAL_HEADER = (
    "timestamp,type,received_asset,received_amount,"
    "sent_asset,sent_amount,fee_asset,fee_amount,note\n"
)


def _write_csv(tmp_path, name, *rows):
    p = tmp_path / name
    p.write_text(_UNIVERSAL_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_remove_by_file(tmp_path):
    """CSVを指定するとそのCSV由来の取引だけ削除される。"""
    db = tmp_path / "t.db"
    csv_a = _write_csv(tmp_path, "a.csv",
        "2026-01-01T00:00:00Z,deposit,BTC,0.1,,,,,a1",
        "2026-01-02T00:00:00Z,deposit,ETH,1,,,,,a2")
    csv_b = _write_csv(tmp_path, "b.csv",
        "2026-02-01T00:00:00Z,deposit,XRP,100,,,,,b1")

    # 同一ソースIDで2本インポート
    _run(db, "import", "--file", str(csv_a), "--exchange", "universal", "--source-id", "x")
    _run(db, "import", "--file", str(csv_b), "--exchange", "universal", "--source-id", "x")

    ledger = Ledger(db)
    assert ledger.count("x") == 3
    ledger.close()

    # a.csv 由来だけ削除
    res = _run(db, "remove", "--file", str(csv_a),
               "--exchange", "universal", "--source-id", "x", "--yes")
    assert res.exit_code == 0
    assert "2 件を削除" in res.output

    ledger = Ledger(db)
    assert ledger.count("x") == 1
    bals = ledger.balances(source="x")
    ledger.close()
    assert bals == {"XRP": 100}  # b.csv の取引は残る


def test_remove_by_file_requires_exchange(tmp_path):
    db = tmp_path / "t.db"
    csv_a = _write_csv(tmp_path, "a.csv",
        "2026-01-01T00:00:00Z,deposit,BTC,0.1,,,,,a1")
    res = _run(db, "remove", "--file", str(csv_a), "--yes")
    assert res.exit_code != 0
    assert "--exchange" in res.output


def test_remove_by_file_not_imported(tmp_path):
    """ledger に無いCSVを指定しても何も削除しない。"""
    db = tmp_path / "t.db"
    csv_a = _write_csv(tmp_path, "a.csv",
        "2026-01-01T00:00:00Z,deposit,BTC,0.1,,,,,a1")
    res = _run(db, "remove", "--file", str(csv_a),
               "--exchange", "universal", "--source-id", "x", "--yes")
    assert res.exit_code == 0
    assert "見つかりませんでした" in res.output
