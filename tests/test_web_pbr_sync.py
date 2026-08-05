"""PBR Lending クローラー同期の Web API テスト

重点検証:
- 未設定でもエラー画面にしない（status は configured:false、同期は 422）
- 正常系は洗い替え結果と残高照合を返す
- クロールが正常終了していない場合は 409（force で 200）
- 年次パージの入力検証
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.sources.jp.pbr_crawl import (  # noqa: E402
    ARTIFACT_NAME,
    ENV_VAR,
    MARKER_NAME,
    sync_state_path,
)
from crypto_summary.web import app as web_app  # noqa: E402

_VIEWER_ENV = "PBR_VIEWER_URL"

_ARTIFACT = {
    "start_date": "2026-01-01",
    "end_date": "2026-03-05",
    "currencies": ["BTC"],
    "daily_ranges": [{
        "date_from": "2026-03-03", "date_to": "2026-03-05",
        "currency": "BTC", "daily_expected_interest": "0.00001",
    }],
    "wallet_events": [],
    "transfer_events": [],
    "balance_snapshots": [{
        "date": "2026-03-05", "currency": "BTC",
        "amount": "0.00003", "accrued_interest": "0",
    }],
    "warnings": [],
}

_MARKER = {
    "runId": "2026-03-05T10:00:00.000Z",
    "phase": "done",
    "failedCurrencies": [],
    "finishedAt": "2026-03-05T10:03:00.000Z",
}


@pytest.fixture
def crawl_dir(tmp_path: Path) -> Path:
    d = tmp_path / "outputs"
    d.mkdir()
    (d / ARTIFACT_NAME).write_text(json.dumps(_ARTIFACT), encoding="utf-8")
    (d / MARKER_NAME).write_text(json.dumps(_MARKER), encoding="utf-8")
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db_path, monkeypatch) -> TestClient:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv(_VIEWER_ENV, raising=False)
    return TestClient(web_app.create_app(db_path))


def _configure(monkeypatch, crawl_dir: Path) -> None:
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))


# ---- 未設定 ----

def test_status_when_not_configured(client):
    d = client.get("/api/sync/pbr/status").json()
    assert d["configured"] is False
    assert d["crawl"] is None
    assert d["up_to_date"] is False


def test_sync_when_not_configured_is_422(client):
    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 422


# ---- 状態 ----

def test_status_when_configured(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    d = client.get("/api/sync/pbr/status").json()
    assert d["configured"] is True
    assert d["crawl"]["healthy"] is True
    assert d["crawl"]["run_id"] == _MARKER["runId"]
    assert d["viewer_url"] == "http://127.0.0.1:4173"
    assert d["last_sync"] is None
    assert d["up_to_date"] is False   # まだ取り込んでいない


def test_viewer_url_from_env(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    monkeypatch.setenv(_VIEWER_ENV, "http://127.0.0.1:4174")
    d = client.get("/api/sync/pbr/status").json()
    assert d["viewer_url"] == "http://127.0.0.1:4174"


def test_status_up_to_date_after_sync(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    client.post("/api/sync/pbr", json={})
    d = client.get("/api/sync/pbr/status").json()
    assert d["up_to_date"] is True
    assert d["last_sync"]["run_id"] == _MARKER["runId"]
    assert d["last_sync"]["parsed"] == 3


def test_status_not_up_to_date_after_new_crawl(client, crawl_dir, monkeypatch):
    """新しいクロール結果があれば up_to_date が False に戻る（自動取り込みの判定）。"""
    _configure(monkeypatch, crawl_dir)
    client.post("/api/sync/pbr", json={})
    (crawl_dir / MARKER_NAME).write_text(
        json.dumps({**_MARKER, "runId": "2026-03-06T10:00:00.000Z"}), encoding="utf-8")
    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is False


def test_status_not_up_to_date_when_format_is_old(
    client, crawl_dir, db_path, monkeypatch
):
    """取り込み規則が変わったら、同じクロール結果でも取り込み直す。"""
    _configure(monkeypatch, crawl_dir)
    client.post("/api/sync/pbr", json={})
    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is True

    path = sync_state_path(db_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["last_sync"]["format_version"] = 0      # 旧バージョンで同期した記録
    path.write_text(json.dumps(state), encoding="utf-8")

    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is False


# ---- 同期 ----

def test_sync_imports_rows(client, crawl_dir, db_path, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["parsed"] == 3
    assert d["inserted"] == 3
    assert d["batch_id"].startswith("batch:")
    assert d["reconciliation"][0]["currency"] == "BTC"
    assert d["reconciliation"][0]["status"] == "ok"

    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr_crawl") == 3
    finally:
        ledger.close()


def test_sync_dry_run_changes_nothing(client, crawl_dir, db_path, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    d = client.post("/api/sync/pbr", json={"dry_run": True}).json()
    assert d["dry_run"] is True
    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr_crawl") == 0
    finally:
        ledger.close()


def test_unhealthy_crawl_is_409_then_force_succeeds(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    (crawl_dir / MARKER_NAME).write_text(
        json.dumps({**_MARKER, "phase": "partial", "failedCurrencies": ["ETH"]}),
        encoding="utf-8")

    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 409
    assert "force" in r.json()["detail"]

    assert client.post("/api/sync/pbr", json={"force": True}).status_code == 200


def test_missing_artifact_is_422(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    (crawl_dir / ARTIFACT_NAME).unlink()
    r = client.post("/api/sync/pbr", json={"force": True})
    assert r.status_code == 422


# ---- 年次パージ ----

def test_purge_removes_year(client, crawl_dir, db_path, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    client.post("/api/sync/pbr", json={})

    r = client.post("/api/sync/pbr/purge", json={"year": 2026})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "year": 2026, "deleted": 3}
    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr_crawl") == 0
    finally:
        ledger.close()


@pytest.mark.parametrize("body", [{}, {"year": "abc"}, {"year": 1999}])
def test_purge_rejects_bad_year(client, body):
    assert client.post("/api/sync/pbr/purge", json=body).status_code == 422


# ---- インポート画面との整合 ----

def test_pbr_crawl_is_hidden_from_upload_dropdown(client):
    values = {e["value"] for e in client.get("/api/import/exchanges").json()["exchanges"]}
    assert "pbr_crawl" not in values
    assert "pbr" in values


def test_batch_list_labels_the_sync(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir)
    client.post("/api/sync/pbr", json={})
    batches = client.get("/api/import/batches").json()["batches"]
    assert [b["exchange_label"] for b in batches] == ["PBR Lending（クローラー同期）"]
