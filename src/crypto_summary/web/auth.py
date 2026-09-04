"""Google OAuth 2.0 認証ルート・依存関係。

マルチユーザーモード（data_dir を指定して起動したとき）でのみ有効になる。
シングルユーザーモード（db_path 直指定）では require_user は使われない。
"""
from __future__ import annotations

import os
import re
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


DEFAULT_BASE_URL = "http://localhost:8000"


def allowed_base_urls() -> list[str]:
    """BASE_URL に列挙された公開 URL（カンマ区切り・空なら制限なし）。

    同じアプリを複数の入口（手元の localhost とリバースプロキシの公開 URL 等）で
    開けるようにするための一覧。OAuth のリダイレクト URI はリクエストごとに
    「今開いている入口」から組み立てるが、Host ヘッダは送信側が自由に名乗れる
    ため、ここに挙げた URL 以外は採用しない。
    初回セットアップで保存した _server_config.json の base_url は
    _apply_server_config() が env に反映してからここへ届く（env 優先のまま）。
    """
    raw = os.environ.get("BASE_URL") or ""
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def _normalize_root_path(raw: str) -> str:
    p = "/" + raw.strip().strip("/")
    return "" if p == "/" else p


def root_path() -> str:
    """サブパス配信の接頭辞（"/crypto" 形式。ルート直下なら空文字）。

    CS_ROOT_PATH が優先。空文字は「未設定」扱い — docker-compose は
    ${CS_ROOT_PATH:-} で常に空文字を渡すため（"/" を書くとルート配信を
    明示的に強制できる）。未設定なら BASE_URL 先頭エントリのパス部分を使う
    （https://例.com/crypto と書けば接頭辞も決まる、という一本化）。
    """
    raw = (os.environ.get("CS_ROOT_PATH") or "").strip()
    if raw:
        return _normalize_root_path(raw)
    urls = allowed_base_urls()
    return _normalize_root_path(urlsplit(urls[0]).path) if urls else ""


def https_only() -> bool:
    """セッション Cookie に Secure を付けるか。

    Cookie の設定はミドルウェア登録時に1度だけ決まるためリクエストごとに
    切り替えられない。入口が1つでも http なら、Secure を付けると
    そちらでログインできなくなるので付けない。
    """
    urls = allowed_base_urls()
    return bool(urls) and all(u.startswith("https://") for u in urls)


def resolve_base_url(request: Request | None = None) -> str:
    """このリクエストが名乗っている入口の URL を返す。

    ブラウザが実際に開いた URL でリダイレクト URI を組み立てるので、
    localhost でも公開 URL でも、設定を変えずに同じようにログインできる
    （使う入口すべての /auth/callback を Google に登録してあることが前提）。

    Host ヘッダは詐称できるため、BASE_URL を設定してあるときはその一覧に
    無いものを採用しない（一致しなければ先頭エントリへ倒す）。TLS 終端
    プロキシの内側ではスキームが http に見えるので、ホスト:ポートが一致する
    登録済み URL があればそちらを優先する（スキームとパスは登録値に揃う）。
    """
    allowed = allowed_base_urls()
    if request is None:
        return allowed[0] if allowed else DEFAULT_BASE_URL

    derived = str(request.base_url).rstrip("/")
    if not allowed:
        return derived
    if derived in allowed:
        return derived
    netloc = urlsplit(derived).netloc
    for url in allowed:
        if urlsplit(url).netloc == netloc:
            return url
    return allowed[0]


def _get_oauth():
    """authlib OAuth クライアントを返す（遅延初期化）。"""
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


_oauth = None


def _oauth_client():
    global _oauth
    if _oauth is None:
        _oauth = _get_oauth()
    return _oauth.google


def reset_oauth_client() -> None:
    """キャッシュ済み OAuth クライアントを破棄する。

    初回セットアップで GOOGLE_CLIENT_ID/SECRET を変更した後に呼び、
    次回ログイン時に新しい資格情報で再初期化させる。
    """
    global _oauth
    _oauth = None


@router.get("/auth/login")
async def login(request: Request):
    # 開いている入口に応じたリダイレクト URI（authlib が state と一緒に
    # セッションへ保存し、コールバックのトークン交換でも同じ値を使う）。
    redirect_uri = resolve_base_url(request) + "/auth/callback"
    return await _oauth_client().authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    from authlib.integrations.starlette_client import OAuthError

    try:
        token = await _oauth_client().authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = token.get("userinfo")
    if not user:
        raise HTTPException(status_code=400, detail="userinfo not found")
    request.session["user"] = {
        "sub": user["sub"],
        "email": user["email"],
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
    }
    return RedirectResponse(url=(request.scope.get("root_path") or "") + "/")


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=(request.scope.get("root_path") or "") + "/")


@router.get("/auth/me")
async def me(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, **user}


def require_user(request: Request) -> dict:
    """FastAPI Depends — 未認証なら 401 を返す。"""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ---------------------------------------------------------------------------
# サービス間アクセス（Asset Summary からの読み取り専用）
# ---------------------------------------------------------------------------

# Google の sub は数字列。ここで形式を縛ることで
# f"{sub}.db" のパス組み立てにトラバーサル文字列が混ざるのを防ぐ。
_SUB_RE = re.compile(r"^[0-9]{1,64}$")


def _service_token() -> str:
    return os.environ.get("CS_SERVICE_TOKEN", "").strip()


def service_request_sub(request: Request) -> str | None:
    """有効なサービストークンならば X-CS-User の sub を返す。それ以外は None。

    CS_SERVICE_TOKEN 未設定なら常に None（機能自体が無効で挙動不変）。
    トークン不一致も None（セッション認証へフォールスルーし 401 になる —
    「トークンが違う」ことを外部に教えない）。sub の形式不正のみ 400。
    """
    token = _service_token()
    if not token:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    # bytes で比較する — str 版 compare_digest は非 ASCII で TypeError を投げるため、
    # 細工したヘッダーで 500 にさせない（不一致は静かに 401 へフォールスルー）。
    candidate = auth[len("Bearer "):].strip().encode("utf-8", "replace")
    if not secrets.compare_digest(candidate, token.encode("utf-8")):
        return None
    sub = request.headers.get("X-CS-User", "").strip()
    if not _SUB_RE.fullmatch(sub):
        raise HTTPException(status_code=400, detail="X-CS-User が不正です")
    return sub


def require_user_or_service(request: Request) -> dict:
    """セッションユーザー、またはサービストークンの主体を返す（読み取り用）。

    サービス主体は email 空のため require_admin には決して通らない。
    """
    sub = service_request_sub(request)
    if sub is not None:
        return {"sub": sub, "email": "", "name": "", "picture": "", "service": True}
    return require_user(request)
