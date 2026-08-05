"""PBR Lending クローラー同期の Web API テスト

重点検証:
- 未設定でもエラー画面にしない（status は configured:false、同期は 422）
- 正常系は洗い替え結果と残高照合を返す
- クロールが正常終了していない場合は 409（force で 200）
- 年次パージの入力検証
"""
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.sources.jp.pbr_crawl import (  # noqa: E402
    ARTIFACT_NAME,
    ENV_VAR,
    MARKER_NAME,
    SETTLE_SECONDS,
    VIEWER_TRANSFERS_NAME,
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


def _write_settled(path: Path, text: str) -> None:
    """ファイルを書き、更新直後の「整定待ち」に入らないよう時刻を戻す。"""
    path.write_text(text, encoding="utf-8")
    old = time.time() - SETTLE_SECONDS - 60
    os.utime(path, (old, old))


@pytest.fixture
def crawl_dir(tmp_path: Path) -> Path:
    d = tmp_path / "outputs"
    d.mkdir()
    _write_settled(d / ARTIFACT_NAME, json.dumps(_ARTIFACT))
    _write_settled(d / MARKER_NAME, json.dumps(_MARKER))
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db_path, monkeypatch) -> TestClient:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv(_VIEWER_ENV, raising=False)
    return TestClient(web_app.create_app(db_path))


def _configure(monkeypatch, crawl_dir: Path, client=None) -> None:
    """サーバー側の設定を入れ、利用者側の連携も有効にする。

    連携は利用者ごとの設定で、既定では出さない（全員が PBR Lending の口座を
    持つわけではないため）。テストの大半は「使う利用者」の視点なので有効にする。
    """
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    if client is not None:
        _enable(client)


def _enable(client, enabled: bool = True) -> None:
    r = client.put("/api/prefs", json={"prefs": {"pbr_sync_enabled": enabled}})
    assert r.status_code == 200


def _pbr_row(source: str = "pbr"):
    """PBR の取引 1 件（連携の既定値の自動判定に使う）。

    source="pbr" は公式 CSV 由来、"pbr_crawl" はクローラー由来。
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from crypto_summary.core.models import CanonicalTx, TxType

    return CanonicalTx(
        id=CanonicalTx.make_id(source, "seed"),
        source=source,
        timestamp=datetime(2025, 12, 30, tzinfo=timezone.utc),
        type=TxType.DEPOSIT,
        received_asset="BTC",
        received_amount=Decimal("0.1"),
        label="pbr_deposit",
    )


# ---- 配備まわり ----

def test_health_needs_no_auth(client):
    """クラウドのヘルスチェックはログイン前に叩かれるので認証を通さない。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_session_key_is_generated_and_persisted(tmp_path, monkeypatch):
    """SECRET_KEY 未設定でも固定値へフォールバックせず、生成して保存する。"""
    from crypto_summary.web.app import _session_secret

    monkeypatch.delenv("SECRET_KEY", raising=False)
    base = tmp_path / "data"
    base.mkdir()

    first = _session_secret(str(base))
    assert len(first) == 64
    assert "dev-secret" not in first
    # 再起動してもログインが切れないよう、同じ鍵を読み直す
    assert _session_secret(str(base)) == first
    assert (base / "_session_key").read_text(encoding="utf-8").strip() == first


def test_session_key_prefers_env(tmp_path, monkeypatch):
    from crypto_summary.web.app import _session_secret

    monkeypatch.setenv("SECRET_KEY", "explicit-key")
    base = tmp_path / "data"
    base.mkdir()
    assert _session_secret(str(base)) == "explicit-key"
    assert not (base / "_session_key").exists()


# ---- 未設定 ----

def test_status_when_not_configured(client):
    d = client.get("/api/sync/pbr/status").json()
    assert d["configured"] is False
    assert d["crawl"] is None
    assert d["up_to_date"] is False


def test_sync_when_not_configured_is_422(client):
    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 422


# ---- 利用者ごとの有効／無効 ----
#
# 全員が PBR Lending の口座を持つわけではないので、既定では連携の UI を出さない。

def test_hidden_by_default_for_a_user_without_pbr_data(client, crawl_dir, monkeypatch):
    """サーバー側に設定があっても、PBR を使っていない利用者には出さない。"""
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))

    d = client.get("/api/sync/pbr/status").json()

    assert d["available"] is True      # サーバー側の設定はある
    assert d["enabled"] is False       # この利用者は使わない
    assert d["configured"] is False    # → UI は出さない
    assert d["has_pbr_data"] is False
    assert d["crawl"] is None          # クロール結果の中身も返さない


def test_shown_after_user_enables_it(client, crawl_dir, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    _enable(client)

    d = client.get("/api/sync/pbr/status").json()

    assert d["enabled"] is True
    assert d["configured"] is True
    assert d["crawl"]["healthy"] is True


def test_hidden_for_a_user_who_only_imports_the_annual_csv(
    client, crawl_dir, db_path, monkeypatch
):
    """年次の公式 CSV を取り込むだけの利用者にはクローラーの UI を出さない。

    口座を持っていても、年に一度 CSV を取り込むだけならクローラーは要らない。
    """
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    ledger = Ledger(db_path)
    try:
        ledger.upsert(_pbr_row())          # 公式 CSV 由来（source=pbr）
    finally:
        ledger.close()

    d = client.get("/api/sync/pbr/status").json()

    assert d["has_pbr_data"] is True    # PBR 固有の設定は出してよい
    assert d["used_crawl"] is False     # クローラーは使っていない
    assert d["enabled"] is False
    assert d["configured"] is False


def test_shown_automatically_for_a_user_who_used_the_crawler(
    client, crawl_dir, db_path, monkeypatch
):
    """クローラー連携を使ったことがある利用者は、設定しなくても出す。"""
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    ledger = Ledger(db_path)
    try:
        ledger.upsert(_pbr_row(source="pbr_crawl"))
    finally:
        ledger.close()

    d = client.get("/api/sync/pbr/status").json()

    assert d["used_crawl"] is True
    assert d["enabled"] is True
    assert d["configured"] is True


def test_still_shown_after_purging_the_crawl_year(client, crawl_dir, monkeypatch):
    """年次パージで pbr_crawl が空になっても、同期の記録があれば出し続ける。"""
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    client.post("/api/sync/pbr/purge", json={"year": 2026})
    # 設定を未指定へ戻し、自動判定だけで決めさせる
    client.put("/api/prefs", json={"prefs": {"pbr_sync_enabled": None}})

    d = client.get("/api/sync/pbr/status").json()

    assert d["used_crawl"] is True
    assert d["configured"] is True


def test_user_can_turn_it_off_even_with_data(client, crawl_dir, db_path, monkeypatch):
    """データがあっても、明示的にオフにすれば出さない（データは消えない）。"""
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    ledger = Ledger(db_path)
    try:
        ledger.upsert(_pbr_row(source="pbr_crawl"))
    finally:
        ledger.close()
    _enable(client, False)

    d = client.get("/api/sync/pbr/status").json()
    assert d["enabled"] is False
    assert d["configured"] is False
    assert d["has_pbr_data"] is True   # 設定は表示だけの話で、データは残る

    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr_crawl") == 1
    finally:
        ledger.close()


def test_available_is_false_without_server_setting(client):
    d = client.get("/api/sync/pbr/status").json()
    assert d["available"] is False
    assert d["configured"] is False


def test_pref_roundtrip(client):
    assert client.get("/api/prefs").json()["prefs"]["pbr_sync_enabled"] is None
    _enable(client)
    assert client.get("/api/prefs").json()["prefs"]["pbr_sync_enabled"] is True
    _enable(client, False)
    assert client.get("/api/prefs").json()["prefs"]["pbr_sync_enabled"] is False


# ---- 状態 ----

def test_status_when_configured(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    d = client.get("/api/sync/pbr/status").json()
    assert d["configured"] is True
    assert d["crawl"]["healthy"] is True
    assert d["crawl"]["run_id"] == _MARKER["runId"]
    assert d["last_sync"] is None
    assert d["up_to_date"] is False   # まだ取り込んでいない


def test_viewer_url_from_env(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    monkeypatch.setenv(_VIEWER_ENV, "http://127.0.0.1:4174")
    d = client.get("/api/sync/pbr/status").json()
    assert d["viewer_url"] == "http://127.0.0.1:4174"


def test_viewer_tab_is_off_without_url(client, crawl_dir, monkeypatch):
    """URL を書かなければクローラー操作のタブを出さない。

    クラウドに置く場合、クロールは手元の機械の役目で、結果はファイル同期で届く。
    """
    _configure(monkeypatch, crawl_dir, client)
    assert client.get("/api/sync/pbr/status").json()["viewer_url"] is None


def test_source_files_show_arrival(client, crawl_dir, monkeypatch):
    """取り込み元ファイルの到着状況を返す（ファイル同期の確認用）。"""
    _configure(monkeypatch, crawl_dir, client)

    files = {f["name"]: f for f in client.get("/api/sync/pbr/status").json()["crawl"]["files"]}

    assert files[ARTIFACT_NAME]["found"] is True
    assert files[ARTIFACT_NAME]["role"] == "crawl"
    assert files[ARTIFACT_NAME]["size"] > 0
    assert files[ARTIFACT_NAME]["mtime"]
    assert files[MARKER_NAME]["found"] is True
    # まだ同期されていないファイルは未着として出す
    assert files[VIEWER_TRANSFERS_NAME]["found"] is False
    assert files[VIEWER_TRANSFERS_NAME]["mtime"] is None


def test_status_up_to_date_after_sync(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    d = client.get("/api/sync/pbr/status").json()
    assert d["up_to_date"] is True
    assert d["last_sync"]["run_id"] == _MARKER["runId"]
    assert d["last_sync"]["parsed"] == 3


def test_status_not_up_to_date_after_new_crawl(client, crawl_dir, monkeypatch):
    """新しいクロール結果があれば up_to_date が False に戻る（自動取り込みの判定）。"""
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    _write_settled(crawl_dir / MARKER_NAME,
                   json.dumps({**_MARKER, "runId": "2026-03-06T10:00:00.000Z"}))
    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is False


def test_status_not_up_to_date_after_manual_import(client, crawl_dir, monkeypatch):
    """クロールせずに手動インポートしただけでも、取り込み対象として検知する。"""
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is True

    _write_settled(crawl_dir / VIEWER_TRANSFERS_NAME,
                       json.dumps({"version": 2, "rows": [{
            "日付": "2025-09-29", "通貨種別": "BTC", "区分": "入庫",
            "数量": "0.1", "備考": "",
        }]}, ensure_ascii=False))

    d = client.get("/api/sync/pbr/status").json()
    assert d["up_to_date"] is False
    assert d["blocked"] is False
    assert d["has_data"] is True


def test_status_without_crawl_but_with_manual_import(client, crawl_dir, monkeypatch):
    """クロール結果が無くても、手動インポートがあれば取り込める状態と判定する。"""
    _configure(monkeypatch, crawl_dir, client)
    (crawl_dir / ARTIFACT_NAME).unlink()
    _write_settled(crawl_dir / VIEWER_TRANSFERS_NAME,
                       json.dumps({"version": 2, "rows": [{
            "日付": "2025-09-29", "通貨種別": "BTC", "区分": "入庫",
            "数量": "0.1", "備考": "",
        }]}, ensure_ascii=False))

    d = client.get("/api/sync/pbr/status").json()
    assert d["has_data"] is True
    assert d["blocked"] is False       # クロールが無いだけなので止めない
    assert d["up_to_date"] is False

    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 200
    assert r.json()["parsed_viewer"] == 1


def test_status_has_no_data_when_directory_is_empty(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    (crawl_dir / ARTIFACT_NAME).unlink()
    (crawl_dir / MARKER_NAME).unlink()
    d = client.get("/api/sync/pbr/status").json()
    assert d["has_data"] is False
    assert d["up_to_date"] is False


def test_status_not_up_to_date_when_format_is_old(
    client, crawl_dir, db_path, monkeypatch
):
    """取り込み規則が変わったら、同じクロール結果でも取り込み直す。"""
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is True

    path = sync_state_path(db_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["last_sync"]["format_version"] = 0      # 旧バージョンで同期した記録
    path.write_text(json.dumps(state), encoding="utf-8")

    assert client.get("/api/sync/pbr/status").json()["up_to_date"] is False


# ---- 同期 ----

def test_sync_imports_rows(client, crawl_dir, db_path, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
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
    _configure(monkeypatch, crawl_dir, client)
    d = client.post("/api/sync/pbr", json={"dry_run": True}).json()
    assert d["dry_run"] is True
    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr_crawl") == 0
    finally:
        ledger.close()


def test_unhealthy_crawl_is_409_then_force_succeeds(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    _write_settled(crawl_dir / MARKER_NAME,
                   json.dumps({**_MARKER, "phase": "partial",
                               "failedCurrencies": ["ETH"]}))

    r = client.post("/api/sync/pbr", json={})
    assert r.status_code == 409
    assert "force" in r.json()["detail"]

    assert client.post("/api/sync/pbr", json={"force": True}).status_code == 200


def test_missing_artifact_is_422(client, crawl_dir, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
    (crawl_dir / ARTIFACT_NAME).unlink()
    r = client.post("/api/sync/pbr", json={"force": True})
    assert r.status_code == 422


# ---- 年次パージ ----

def test_purge_removes_year(client, crawl_dir, db_path, monkeypatch):
    _configure(monkeypatch, crawl_dir, client)
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
    _configure(monkeypatch, crawl_dir, client)
    client.post("/api/sync/pbr", json={})
    batches = client.get("/api/import/batches").json()["batches"]
    assert [b["exchange_label"] for b in batches] == ["PBR Lending（クローラー同期）"]
