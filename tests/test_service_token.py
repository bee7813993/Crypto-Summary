"""サービストークン（Asset Summary 連携用の読み取り専用アクセス）のテスト。

マルチユーザーモードで CS_SERVICE_TOKEN による server-to-server 読み取りを検証する。
- トークン未設定なら機能ごと無効（挙動不変）
- 有効トークン + X-CS-User で該当ユーザーの台帳を読める
- 書き込み・管理系には決して通らない
- 未知 sub で DB ファイルを暗黙作成しない
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.core.models import CanonicalTx, TxType  # noqa: E402
from crypto_summary.web import app as web_app  # noqa: E402

TOKEN = "test-service-token"
SUB = "102071876055427770412"


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _deposit(source: str, asset: str, amount: str, day: int) -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"{asset}{day}"),
        source=source,
        timestamp=datetime(2024, 1, day, tzinfo=timezone.utc),
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=Decimal(amount),
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    db = Ledger(d / f"{SUB}.db")
    db.upsert(_deposit("bitflyer", "BTC", "0.5", 1))
    db.close()

    def fake_prices(assets, currency, warn=None):
        return {"BTC": Decimal("60000")}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    monkeypatch.setattr(
        web_app,
        "fetch_price_history",
        lambda a, c, s, e, warn=None: {"BTC": {_days_ago(1): Decimal("55000")}},
    )
    return d


@pytest.fixture
def mu_client(data_dir: Path, monkeypatch) -> TestClient:
    """マルチユーザーモード + トークン設定済みのクライアント。"""
    monkeypatch.setenv("CS_SERVICE_TOKEN", TOKEN)
    return TestClient(web_app.create_app(data_dir=str(data_dir)))


def _headers(token: str = TOKEN, sub: str | None = SUB) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if sub is not None:
        h["X-CS-User"] = sub
    return h


def _db_files(data_dir: Path) -> set[str]:
    return {p.name for p in data_dir.glob("*.db")}


# ---- 有効なトークン ----


def test_valid_token_reads_summary(mu_client):
    r = mu_client.get("/api/summary?currency=USD", headers=_headers())
    assert r.status_code == 200
    d = r.json()
    assert Decimal(d["total_value"]) == Decimal("30000")
    assert d["assets"][0]["asset"] == "BTC"


def test_valid_token_reads_all_read_routes(mu_client):
    assert mu_client.get("/api/sources", headers=_headers()).status_code == 200
    assert mu_client.get("/api/balances", headers=_headers()).status_code == 200
    assert (
        mu_client.get(
            "/api/account-assets?account=bitflyer", headers=_headers()
        ).status_code
        == 200
    )
    assert (
        mu_client.get("/api/asset-accounts?asset=BTC", headers=_headers()).status_code
        == 200
    )
    # portfolio-history は価格履歴取得に踏み込むため fetch を短絡させる
    r = mu_client.get(
        "/api/portfolio-history?range=7d&scope=total", headers=_headers()
    )
    assert r.status_code == 200


# ---- 無効・不正なリクエスト ----


def test_token_unset_means_feature_off(data_dir: Path, monkeypatch):
    monkeypatch.delenv("CS_SERVICE_TOKEN", raising=False)
    client = TestClient(web_app.create_app(data_dir=str(data_dir)))
    r = client.get("/api/summary", headers=_headers())
    assert r.status_code == 401


def test_wrong_token_401(mu_client):
    r = mu_client.get("/api/summary", headers=_headers(token="wrong"))
    assert r.status_code == 401


def test_non_ascii_token_401_not_500(mu_client):
    """非 ASCII のトークンでも 500 にせず 401（compare_digest の TypeError 対策）。

    httpx は str ヘッダーを ASCII 強制するため、bytes で生の latin-1 値を送る。
    """
    r = mu_client.get(
        "/api/summary",
        headers={
            b"Authorization": "Bearer caf\xe9".encode("latin-1"),
            b"X-CS-User": SUB.encode(),
        },
    )
    assert r.status_code == 401


def test_missing_sub_400(mu_client):
    r = mu_client.get("/api/summary", headers=_headers(sub=None))
    assert r.status_code == 400


def test_traversal_sub_400_and_no_file(mu_client, data_dir: Path):
    before = _db_files(data_dir)
    r = mu_client.get(
        "/api/summary",
        headers={"Authorization": f"Bearer {TOKEN}", "X-CS-User": "../_system"},
    )
    assert r.status_code == 400
    assert _db_files(data_dir) == before


def test_unknown_sub_404_and_no_db_created(mu_client, data_dir: Path):
    before = _db_files(data_dir)
    r = mu_client.get("/api/summary", headers=_headers(sub="999999999"))
    assert r.status_code == 404
    assert _db_files(data_dir) == before


# ---- 権限の境界 ----


def test_token_cannot_write(mu_client):
    r = mu_client.post(
        "/api/transactions",
        headers=_headers(),
        json={
            "timestamp": "2024-01-01T00:00:00Z",
            "type": "deposit",
            "received_asset": "BTC",
            "received_amount": "1",
        },
    )
    assert r.status_code == 401


def test_token_cannot_admin(mu_client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    r = mu_client.get("/api/system-keys", headers=_headers())
    assert r.status_code in (401, 403)


# ---- シングルユーザーモード ----


def test_single_user_ignores_headers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CS_SERVICE_TOKEN", TOKEN)

    def fake_prices(assets, currency, warn=None):
        return {}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    client = TestClient(web_app.create_app(db_path=str(tmp_path / "single.db")))
    # ヘッダーなしでも従来どおり開いている
    assert client.get("/api/summary").status_code == 200
    # ヘッダー付き（不正 sub 含む）でも無視されて同じ DB
    r = client.get(
        "/api/summary",
        headers={"Authorization": f"Bearer {TOKEN}", "X-CS-User": "../evil"},
    )
    assert r.status_code == 200


def test_service_token_summary_exposes_prev_fields(mu_client):
    """AS が実際に触る経路で前日値のフィールドが返る。"""
    d = mu_client.get("/api/summary?currency=USD", headers=_headers()).json()
    assert Decimal(d["total_prev_value"]) == Decimal("27500")   # 0.5 * 55000
    btc = next(a for a in d["assets"] if a["asset"] == "BTC")
    assert btc["prev_date"] == _days_ago(1)
    assert Decimal(btc["prev_price"]) == Decimal("55000")
    assert Decimal(btc["prev_value"]) == Decimal("27500")
