"""Google OAuth 2.0 認証ルート・依存関係。

マルチユーザーモード（data_dir を指定して起動したとき）でのみ有効になる。
シングルユーザーモード（db_path 直指定）では require_user は使われない。
"""
from __future__ import annotations

import os
import re
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def _base_url() -> str:
    # 空文字（Docker で未設定時に渡る）も既定値にフォールバックする。
    return (os.environ.get("BASE_URL") or "http://localhost:8000").rstrip("/")


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
    redirect_uri = _base_url() + "/auth/callback"
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
    return RedirectResponse(url="/")


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


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
