"""リバースプロキシ／サブパス配信まわりのテスト。

- BASE_URL はカンマ区切りの許可リストとして解釈され、OAuth のリダイレクト URI
  は「今開いている入口」から組み立てる（Host 詐称はリスト先頭へ倒す）。
- root_path（CS_ROOT_PATH または BASE_URL 先頭エントリのパス）を設定すると
  接頭辞付き URL で配信され、index には <base> が差し込まれる。

BASE_URL / CS_ROOT_PATH は conftest の autouse フィクスチャが毎テスト削除する
ので、必要なテストだけ monkeypatch で設定する。
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from crypto_summary.web import app as web_app  # noqa: E402
from crypto_summary.web import auth as cs_auth  # noqa: E402


def _req(host: str, scheme: str = "http", root_path: str = "") -> Request:
    """指定した Host ヘッダで来たリクエスト（scope を直接組む）。"""
    return Request({
        "type": "http", "method": "GET", "path": "/auth/login",
        "scheme": scheme, "query_string": b"", "root_path": root_path,
        "headers": [(b"host", host.encode())],
        "server": (host.split(":")[0], int(host.split(":")[1]) if ":" in host else 80),
    })


# ---- BASE_URL の許可リスト解釈 ----


def test_allowed_base_urls_parsing(monkeypatch):
    monkeypatch.setenv(
        "BASE_URL", " https://example.com/crypto/ , http://localhost:8000 ,, "
    )
    assert cs_auth.allowed_base_urls() == [
        "https://example.com/crypto", "http://localhost:8000",
    ]


def test_resolve_base_url_derived_when_unrestricted():
    # リストが空なら、開いた入口をそのまま使う（従来の既定に相当）。
    assert cs_auth.resolve_base_url(_req("localhost:8000")) == "http://localhost:8000"
    assert cs_auth.resolve_base_url(_req("example.net:1000")) == "http://example.net:1000"


def test_resolve_base_url_accepts_listed_entries(monkeypatch):
    monkeypatch.setenv("BASE_URL", "http://localhost:8000, http://example.net:1000/")
    # 同じ設定のまま、開いた入口ごとに違うリダイレクト URI になる
    assert cs_auth.resolve_base_url(_req("localhost:8000")) == "http://localhost:8000"
    assert cs_auth.resolve_base_url(_req("example.net:1000")) == "http://example.net:1000"


def test_resolve_base_url_spoofed_host_falls_back(monkeypatch):
    monkeypatch.setenv("BASE_URL", "http://localhost:8000,http://example.net:1000")
    assert cs_auth.resolve_base_url(_req("evil.example:80")) == "http://localhost:8000"


def test_resolve_base_url_tls_proxy_keeps_registered_scheme(monkeypatch):
    """TLS 終端プロキシの内側は http に見えるが、登録済みの https を使う。"""
    monkeypatch.setenv("BASE_URL", "https://example.com/crypto")
    assert cs_auth.resolve_base_url(_req("example.com")) == "https://example.com/crypto"


def test_resolve_base_url_root_path_via_netloc_match(monkeypatch):
    # root_path 設定時は request.base_url にも接頭辞が乗るが、
    # ホスト:ポート一致で登録エントリ（パス無し）へ正しく倒れる。
    monkeypatch.setenv("BASE_URL", "https://example.com/crypto,http://localhost:8000")
    req = _req("localhost:8000", root_path="/crypto")
    assert cs_auth.resolve_base_url(req) == "http://localhost:8000"


def test_resolve_base_url_without_request(monkeypatch):
    monkeypatch.setenv("BASE_URL", "http://example.net:1000,http://localhost:8000")
    assert cs_auth.resolve_base_url(None) == "http://example.net:1000"
    monkeypatch.delenv("BASE_URL", raising=False)
    assert cs_auth.resolve_base_url(None) == cs_auth.DEFAULT_BASE_URL


def test_https_only_requires_every_entry_https(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://a.example.com,https://b.example.com/crypto")
    assert cs_auth.https_only() is True
    # 1つでも http があれば Secure を付けない（そちらで入れなくなるため）
    monkeypatch.setenv("BASE_URL", "https://a.example.com,http://localhost:8000")
    assert cs_auth.https_only() is False
    monkeypatch.delenv("BASE_URL", raising=False)
    assert cs_auth.https_only() is False


# ---- root_path（サブパス接頭辞）の決定 ----


def test_root_path_derived_from_base_url(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.com/crypto")
    assert cs_auth.root_path() == "/crypto"


def test_root_path_env_override(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.com/crypto")
    # 明示指定が優先。スラッシュの有無は正規化する
    monkeypatch.setenv("CS_ROOT_PATH", "sub/")
    assert cs_auth.root_path() == "/sub"
    # "/" はルート配信の明示（BASE_URL のパスを打ち消す）
    monkeypatch.setenv("CS_ROOT_PATH", "/")
    assert cs_auth.root_path() == ""


def test_root_path_empty_env_means_unset(monkeypatch):
    # docker-compose は ${CS_ROOT_PATH:-} で常に空文字を渡す — 空は未設定扱い。
    monkeypatch.setenv("CS_ROOT_PATH", "")
    monkeypatch.setenv("BASE_URL", "https://example.com/crypto")
    assert cs_auth.root_path() == "/crypto"


def test_root_path_default_empty():
    assert cs_auth.root_path() == ""


# ---- サブパス配信（シングルユーザー） ----


@pytest.fixture
def sub_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CS_ROOT_PATH", "/crypto")
    return TestClient(web_app.create_app(str(tmp_path / "t.db")))


def test_subpath_serves_api_and_index(sub_client):
    assert sub_client.get("/crypto/api/health").json() == {"status": "ok"}
    r = sub_client.get("/crypto/")
    assert r.status_code == 200
    # 相対 URL の基準を差し込む（スキーム・ホストは書かない）
    assert '<base href="/crypto/">' in r.text
    assert 'src="static/app.js"' in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_subpath_static_files(sub_client):
    r = sub_client.get("/crypto/static/app.js")
    assert r.status_code == 200
    # _no_cache_static が接頭辞を外して判定できていることの検証
    assert r.headers["cache-control"] == "no-cache"


def test_subpath_redirects_missing_slash(sub_client):
    r = sub_client.get("/crypto", follow_redirects=False)
    assert r.status_code in (301, 307, 308)
    assert r.headers["location"].endswith("/crypto/")


def test_subpath_plain_routes_still_resolve(sub_client):
    # Route は接頭辞なしでも素通しで解決する（Docker の HEALTHCHECK が
    # http://127.0.0.1:8000/api/health を直接叩くため、この性質に依存）。
    assert sub_client.get("/api/health").status_code == 200
    # 一方 StaticFiles の Mount は接頭辞付きでしか一致しない（404 が正しい）。
    # プロキシは接頭辞を落とさず転送する前提なので実運用には影響しない。
    assert sub_client.get("/static/app.js").status_code == 404


def test_root_serving_unaffected(tmp_path):
    """接頭辞なし（従来どおり）でも base はルートになる。"""
    client = TestClient(web_app.create_app(str(tmp_path / "t.db")))
    r = client.get("/")
    assert r.status_code == 200
    assert '<base href="/">' in r.text
    assert client.get("/api/health").status_code == 200
    r = client.get("/static/app.js")
    assert r.status_code == 200
    # 従来構成でも静的ファイルの no-cache が維持されること（Mount が scope の
    # root_path を書き換えるため、判定の読み取り位置を誤ると静かに消える）
    assert r.headers["cache-control"] == "no-cache"


# ---- サブパス配信（マルチユーザー） ----


@pytest.fixture
def mu_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("CS_ROOT_PATH", "/crypto")
    return TestClient(web_app.create_app(data_dir=str(tmp_path / "data")))


def test_multi_user_subpath_public_paths(mu_client):
    # 未ログインでも画面の骨組み・静的ファイル・ヘルスは読める
    # （でないとログイン画面自体を表示できない）。
    assert mu_client.get("/crypto/").status_code == 200
    assert mu_client.get("/crypto/static/app.js").status_code == 200
    assert mu_client.get("/crypto/api/health").status_code == 200
    assert mu_client.get("/crypto/auth/me").json() == {"authenticated": False}
    # API は認証必須のまま
    assert mu_client.get("/crypto/api/summary").status_code == 401


def test_multi_user_subpath_logout_redirect(mu_client):
    # ログアウト後はドメイン直下ではなく接頭辞付きのトップへ戻る
    r = mu_client.get("/crypto/auth/logout", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/crypto/"
