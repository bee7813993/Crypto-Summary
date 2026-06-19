"""Google OAuth 2.0 認証ルート・依存関係。

マルチユーザーモード（data_dir を指定して起動したとき）でのみ有効になる。
シングルユーザーモード（db_path 直指定）では require_user は使われない。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def _base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


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
