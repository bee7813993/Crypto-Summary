"""取り込みフォルダの Web API テスト。

- 未設定でも状態は取れ（configured:false）、走査は 422
- シングルユーザー: 入口は IMPORT_DIR そのもの。置く → 走査 → 取り込み → 履歴に origin=inbox
- マルチユーザー: 入口名はメールのローカル部から決まり、利用者ごとに分かれる
- ポーラは IMPORT_DIR と間隔が設定されたときだけ起動時に走る
"""
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core import inbox  # noqa: E402
from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.web import app as web_app  # noqa: E402
from crypto_summary.web.auth import require_user  # noqa: E402

HEADER = "timestamp,type,received_asset,received_amount,sent_asset,sent_amount,fee_asset,fee_amount,note\n"


def _csv(*rows):
    return HEADER + "".join(f"{ts},deposit,{asset},{amt},,,,,\n" for ts, asset, amt in rows)


def _settled(path: Path, text: str, age: int = 120) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))


def _balances(db_path: Path, source: str) -> dict:
    ledger = Ledger(db_path)
    try:
        return {k: float(v) for k, v in ledger.balances(source=source).items()}
    finally:
        ledger.close()


@pytest.fixture
def import_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "inbox"
    monkeypatch.setenv(inbox.IMPORT_DIR_ENV, str(d))
    monkeypatch.setenv(inbox.POLL_ENV, "0")
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(web_app.create_app(str(db_path)))


# ---- 未設定 ----

def test_status_unconfigured(client, monkeypatch):
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    r = client.get("/api/inbox/status")
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] is False and d["root"] is None
    assert "universal" in d["exchanges"]
    assert d["exchange_labels"]["gmo"] == "GMOコイン"
    assert client.post("/api/inbox/sync", json={}).status_code == 422


# ---- シングルユーザー ----

def test_status_creates_root_and_lists_files(client, import_dir: Path):
    assert not import_dir.exists()
    r = client.get("/api/inbox/status")
    assert r.status_code == 200 and import_dir.is_dir()
    d = r.json()
    assert d["configured"] and d["root"] == str(import_dir) and d["multi_user"] is False
    assert d["inbox_name"] is None
    assert d["poller"]["active"] is False
    _settled(import_dir / "universal" / "a.csv", _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    d = client.get("/api/inbox/status").json()
    assert [(f["path"], f["status"]) for f in d["files"]] == [("universal/a.csv", "new")]


def test_sync_imports_and_records_origin(client, import_dir: Path, db_path: Path):
    _settled(import_dir / "universal" / "a.csv", _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    r = client.post("/api/inbox/sync", json={})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == ["universal/a.csv"]
    batches = client.get("/api/import/batches").json()["batches"]
    assert len(batches) == 1
    assert batches[0]["origin"] == "inbox" and batches[0]["filename"] == "universal/a.csv"
    status = client.get("/api/inbox/status").json()
    f = status["files"][0]
    assert f["status"] == "imported" and f["tx_count"] == 1 and f["batch_id"] == batches[0]["id"]
    assert status["counts"]["imported"] == 1
    assert _balances(db_path, "universal") == {"BTC": 1.0}


def test_changed_file_is_replaced(client, import_dir: Path, db_path: Path):
    path = import_dir / "universal" / "a.csv"
    _settled(path, _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    client.post("/api/inbox/sync", json={})
    _settled(path, _csv(("2024-01-01T00:00:00Z", "BTC", "3")), age=60)
    assert client.get("/api/inbox/status").json()["files"][0]["changed"] is True
    r = client.post("/api/inbox/sync", json={})
    assert r.json()["replaced"] == ["universal/a.csv"]
    assert len(client.get("/api/import/batches").json()["batches"]) == 1
    assert _balances(db_path, "universal") == {"BTC": 3.0}


def test_retry_and_forget(client, import_dir: Path):
    path = import_dir / "universal" / "bad.csv"
    _settled(path, "not,a,universal,csv\n1,2,3,4\n")
    r = client.post("/api/inbox/sync", json={})
    assert [e["path"] for e in r.json()["errors"]] == ["universal/bad.csv"]
    assert client.get("/api/inbox/status").json()["files"][0]["status"] == "error"

    r = client.post("/api/inbox/files/retry", json={"path": "universal/bad.csv"})
    assert r.status_code == 200 and r.json()["ok"] is False and "解析" in r.json()["error"]

    _settled(path, _csv(("2024-01-01T00:00:00Z", "ETH", "2")), age=60)
    r = client.post("/api/inbox/files/retry", json={"path": "universal/bad.csv"})
    assert r.json()["ok"] and r.json()["imported"]

    r = client.post("/api/inbox/files/forget", json={"path": "universal/bad.csv"})
    assert r.status_code == 200 and r.json()["deleted"] == 1 and r.json()["kept_row"]
    assert client.get("/api/import/batches").json()["batches"] == []
    assert client.get("/api/inbox/status").json()["files"][0]["status"] == "deleted"
    assert client.post("/api/inbox/files/forget", json={"path": "universal/none.csv"}).status_code == 404
    assert client.post("/api/inbox/files/retry", json={}).status_code == 422


def test_dry_run(client, import_dir: Path):
    _settled(import_dir / "universal" / "a.csv", _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    r = client.post("/api/inbox/sync", json={"dry_run": True})
    assert r.json()["dry_run"] and r.json()["imported"] == ["universal/a.csv"]
    assert client.get("/api/import/batches").json()["batches"] == []


def test_rename_not_available_in_single_user(client, import_dir: Path):
    assert client.put("/api/inbox/name", json={"name": "x"}).status_code == 422


def test_admin_overview_single_user(client, import_dir: Path):
    d = client.get("/api/admin/inbox").json()
    assert d["import_dir"] == str(import_dir) and d["poll_seconds"] == 0
    assert d["inboxes"] == [] and d["poller"]["active"] is False


def test_syncthing_overview_for_inbox(client, import_dir: Path, monkeypatch):
    monkeypatch.delenv("SYNCTHING_API_KEY", raising=False)
    d = client.get("/api/inbox/syncthing").json()
    assert d["configured"] is False  # Syncthing 未設定
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    d = client.get("/api/inbox/syncthing").json()
    assert d["configured"] is False and "IMPORT_DIR" in d["error"]
    assert client.post("/api/inbox/syncthing/pair", json={"device_id": "X"}).status_code == 422


# ---- ポーラの起動 ----

def test_poller_starts_only_when_configured(tmp_path: Path, monkeypatch):
    d = tmp_path / "inbox"
    monkeypatch.setenv(inbox.IMPORT_DIR_ENV, str(d))
    monkeypatch.setenv(inbox.POLL_ENV, "0")
    app = web_app.create_app(str(tmp_path / "a.db"))
    with TestClient(app):
        assert app.state.inbox_poller.active is False

    monkeypatch.setenv(inbox.POLL_ENV, "60")
    _settled(d / "universal" / "a.csv", _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    app = web_app.create_app(str(tmp_path / "b.db"))
    with TestClient(app) as c:
        poller = app.state.inbox_poller
        assert poller.active is True and poller.interval == 60
        deadline = time.time() + 5
        while poller.runs == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert poller.runs >= 1
        assert c.get("/api/inbox/status").json()["files"][0]["status"] == "imported"
    assert poller.active is False

    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    app = web_app.create_app(str(tmp_path / "c.db"))
    with TestClient(app):
        assert app.state.inbox_poller.active is False


# ---- マルチユーザー ----

ALICE = {"sub": "111", "email": "Alice.Smith@example.com", "name": "Alice"}
BOB = {"sub": "222", "email": "bob@example.com", "name": "Bob"}


@pytest.fixture
def mu(tmp_path: Path, import_dir: Path, monkeypatch):
    data_dir = tmp_path / "data"
    app = web_app.create_app(data_dir=str(data_dir))
    client = TestClient(app)

    def as_user(user):
        app.dependency_overrides[require_user] = lambda: user
        return client

    return {"app": app, "as": as_user, "data_dir": data_dir, "import_dir": import_dir}


def test_multi_user_names_and_roots(mu):
    c = mu["as"](ALICE)
    d = c.get("/api/inbox/status").json()
    assert d["multi_user"] is True and d["inbox_name"] == "alice-smith"
    assert d["root"] == str(mu["import_dir"] / "alice-smith")
    assert (mu["import_dir"] / "alice-smith").is_dir()
    c = mu["as"](BOB)
    d = c.get("/api/inbox/status").json()
    assert d["inbox_name"] == "bob" and d["root"].endswith("/bob")

    # それぞれの入口に置いたものは、それぞれの台帳に入る
    _settled(mu["import_dir"] / "alice-smith" / "universal" / "a.csv",
             _csv(("2024-01-01T00:00:00Z", "BTC", "1")))
    _settled(mu["import_dir"] / "bob" / "universal" / "b.csv",
             _csv(("2024-01-01T00:00:00Z", "ETH", "5")))
    results = mu["app"].state.inbox_poller.run_once()
    assert set(results) == {str(mu["import_dir"] / "alice-smith"), str(mu["import_dir"] / "bob")}
    assert _balances(mu["data_dir"] / "111.db", "universal") == {"BTC": 1.0}
    assert _balances(mu["data_dir"] / "222.db", "universal") == {"ETH": 5.0}


def test_multi_user_rename(mu):
    c = mu["as"](ALICE)
    c.get("/api/inbox/status")
    r = c.put("/api/inbox/name", json={"name": "Cold-Wallet"})
    assert r.status_code == 200 and r.json()["inbox_name"] == "cold-wallet"
    assert c.get("/api/inbox/status").json()["inbox_name"] == "cold-wallet"
    mu["as"](BOB).get("/api/inbox/status")
    r = mu["as"](ALICE).put("/api/inbox/name", json={"name": "bob"})
    assert r.status_code == 422 and "他の利用者" in r.json()["detail"]
    assert c.put("/api/inbox/name", json={"name": "../x"}).status_code == 422


def test_multi_user_admin_overview_and_syncthing(mu, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ALICE["email"].lower())
    mu["as"](BOB).get("/api/inbox/status")
    c = mu["as"](ALICE)
    c.get("/api/inbox/status")
    d = c.get("/api/admin/inbox").json()
    assert {e["name"] for e in d["inboxes"]} >= {"bob", "alice-smith"}
    # 管理者は入口を指定して Syncthing を設定できる（未設定なら configured:false）
    monkeypatch.delenv("SYNCTHING_API_KEY", raising=False)
    assert c.get("/api/inbox/syncthing?inbox=bob").json()["configured"] is False
    assert c.get("/api/inbox/syncthing?inbox=nobody").status_code == 404
    assert mu["as"](BOB).get("/api/admin/inbox").status_code == 403


def test_multi_user_unconfigured_keeps_name_only(mu, monkeypatch):
    c = mu["as"](ALICE)
    c.get("/api/inbox/status")
    monkeypatch.delenv(inbox.IMPORT_DIR_ENV, raising=False)
    d = c.get("/api/inbox/status").json()
    assert d["configured"] is False and d["inbox_name"] == "alice-smith"
    assert mu["app"].state.inbox_poller.run_once() == {}
