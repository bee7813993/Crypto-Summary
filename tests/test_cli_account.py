"""crypto-summary account（API キー暗号化登録）コマンドのテスト。"""
from click.testing import CliRunner

from crypto_summary.cli import cli
from crypto_summary.core.secrets import SecretStore, generate_master_key


def _run(db, key, *args):
    env = {"CS_SECRET_KEY": key} if key else {}
    return CliRunner().invoke(cli, ["--db", str(db), *args], env=env)


def test_gen_key_outputs_env_line(tmp_path):
    res = _run(tmp_path / "t.db", None, "account", "gen-key")
    assert res.exit_code == 0
    assert "CS_SECRET_KEY=" in res.output


def test_add_and_list_api(tmp_path):
    db = tmp_path / "t.db"
    key = generate_master_key()
    res = _run(db, key, "account", "add-api", "--exchange", "bybit",
               "--source-id", "mybybit", "--api-key", "K", "--api-secret", "S")
    assert res.exit_code == 0
    assert "登録しました" in res.output

    # 暗号化保存され、復号で元の値が戻る
    store = SecretStore(db, master_key=key)
    creds = store.get_account_api("mybybit")
    assert creds["exchange"] == "bybit"
    assert creds["api_key"] == "K"

    res2 = _run(db, key, "account", "list-api")
    assert "mybybit" in res2.output
    assert "bybit" in res2.output
    # 秘密は出力に現れない
    assert "K" not in res2.output.split("mybybit")[0]


def test_remove_api(tmp_path):
    db = tmp_path / "t.db"
    key = generate_master_key()
    _run(db, key, "account", "add-api", "--exchange", "bybit",
         "--source-id", "x", "--api-key", "K", "--api-secret", "S")
    res = _run(db, key, "account", "remove-api", "--source-id", "x")
    assert res.exit_code == 0
    assert "削除しました" in res.output
    assert SecretStore(db, master_key=key).get_account_api("x") is None


def test_add_api_without_master_key_fails(tmp_path):
    res = _run(tmp_path / "t.db", None, "account", "add-api", "--exchange", "bybit",
               "--source-id", "x", "--api-key", "K", "--api-secret", "S")
    assert res.exit_code != 0
    assert "マスター鍵" in res.output


def test_fetch_requires_exchange_or_source(tmp_path):
    res = _run(tmp_path / "t.db", None, "fetch")
    assert res.exit_code != 0
    assert "--exchange" in res.output


def test_env_api_value_filters_placeholder(monkeypatch):
    """.env.example のプレースホルダ値は未設定として扱う（誤った認証を防ぐ）。"""
    from crypto_summary.cli import _env_api_value

    monkeypatch.setenv("BITFLYER_API_KEY", "your_api_key_here")
    assert _env_api_value("BITFLYER_API_KEY") is None

    monkeypatch.setenv("BITFLYER_API_SECRET", "your_api_secret_here")
    assert _env_api_value("BITFLYER_API_SECRET") is None


def test_env_api_value_returns_real_key(monkeypatch):
    """実キーはそのまま返す（前後空白は除去）。"""
    from crypto_summary.cli import _env_api_value

    monkeypatch.setenv("BITFLYER_API_KEY", "  REALKEY  ")
    assert _env_api_value("BITFLYER_API_KEY") == "REALKEY"


def test_env_api_value_unset(monkeypatch):
    """未設定は None。"""
    from crypto_summary.cli import _env_api_value

    monkeypatch.delenv("BITFLYER_API_KEY", raising=False)
    assert _env_api_value("BITFLYER_API_KEY") is None
