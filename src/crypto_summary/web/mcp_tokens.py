"""MCP 接続トークン（AI エージェントから /mcp を読むための鍵）の保管庫。

利用者が Web 画面で発行し、エージェントの設定に `Authorization: Bearer <トークン>`
として書いてもらう。エージェントごとに 1 本ずつ発行し、要らなくなったものだけを
失効できるようにする。

設計方針:
  - 平文のトークンは発行した瞬間に 1 度だけ返し、保存はしない。保存するのは
    SHA-256 のハッシュだけ（トークンは 256 bit の乱数なので、総当たりで元に
    戻せない。鍵導出関数で遅くする必要はない）。
  - 1 つのファイルに全利用者ぶんを持つ。トークンから利用者（Google の sub）を
    引くのが主な使い方で、利用者ごとのファイルに分けると、どのファイルを
    見ればよいかが分からないため。
  - 書き込みは一時ファイルからの置き換えで行い、途中で落ちても壊れたファイルを
    残さない。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: 発行するトークンの接頭辞。漏洩検知（シークレットスキャン）で見つけやすくするため。
TOKEN_PREFIX = "csmcp_"

#: 1 利用者が持てるトークンの数。エージェントごとに 1 本でも十分足りる。
MAX_TOKENS_PER_USER = 20

#: ラベルの最大文字数。
MAX_LABEL_LENGTH = 60

#: 最終利用日時を書き戻す間隔。リクエストのたびにファイルを書かないための間引き。
_LAST_USED_RESOLUTION = timedelta(minutes=10)


class McpTokenError(ValueError):
    """発行・失効の要求が不正なとき（画面に出すメッセージを持つ）。"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(record: dict[str, Any]) -> dict[str, Any]:
    """画面・API に返してよい項目だけにする（ハッシュと利用者 ID は出さない）。"""
    return {
        "id": record["id"],
        "label": record.get("label", ""),
        "hint": record.get("hint", ""),
        "created_at": record.get("created_at"),
        "last_used_at": record.get("last_used_at"),
    }


class McpTokenStore:
    """MCP 接続トークンをファイルに保存する。スレッドから同時に呼ばれてよい。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._mtime: float | None = None

    # -- 読み書き ----------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """ファイルを読む。別プロセスが書き換えていたら読み直す。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._data is not None and mtime == self._mtime:
            return self._data
        data: dict[str, Any] = {"tokens": []}
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("tokens"), list):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                # 読めないファイルで全トークンが通らなくなるより、空として扱う方が
                # 安全側（認証が通らないだけで、他人の台帳が読めるようにはならない）。
                pass
        self._data = data
        self._mtime = mtime
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._data = data
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    # -- 公開 API ----------------------------------------------------------

    def issue(self, sub: str, label: str) -> tuple[str, dict[str, Any]]:
        """新しいトークンを発行し、(平文のトークン, 公開してよい記録) を返す。"""
        label = (label or "").strip()
        if not label:
            raise McpTokenError("名前を入れてください（どのエージェント用か分かるように）")
        if len(label) > MAX_LABEL_LENGTH:
            raise McpTokenError(f"名前は {MAX_LABEL_LENGTH} 文字以内にしてください")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            mine = [r for r in data["tokens"] if r.get("sub") == sub]
            if len(mine) >= MAX_TOKENS_PER_USER:
                raise McpTokenError(
                    f"トークンは {MAX_TOKENS_PER_USER} 本までです。使っていないものを失効させてください"
                )
            record = {
                "id": secrets.token_hex(8),
                "sub": sub,
                "label": label,
                # 一覧で見分けるための末尾 4 文字。ハッシュと違い総当たりの足しにはならない。
                "hint": token[-4:],
                "hash": _hash(token),
                "created_at": _now().isoformat(),
                "last_used_at": None,
            }
            tokens = [*data["tokens"], record]
            self._save({**data, "tokens": tokens})
        return token, _public(record)

    def list(self, sub: str) -> list[dict[str, Any]]:
        """その利用者のトークン一覧（新しい順）。"""
        with self._lock:
            data = self._load()
            mine = [_public(r) for r in data["tokens"] if r.get("sub") == sub]
        mine.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return mine

    def revoke(self, sub: str, token_id: str) -> bool:
        """その利用者のトークンを失効させる。見つからなければ False。"""
        with self._lock:
            data = self._load()
            kept = [r for r in data["tokens"]
                    if not (r.get("sub") == sub and r.get("id") == token_id)]
            if len(kept) == len(data["tokens"]):
                return False
            self._save({**data, "tokens": kept})
        return True

    def verify(self, token: str) -> str | None:
        """トークンが有効ならその利用者（sub）を返す。無効なら None。"""
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        digest = _hash(token)
        with self._lock:
            data = self._load()
            record = next((r for r in data["tokens"]
                           if secrets.compare_digest(str(r.get("hash", "")), digest)), None)
            if record is None:
                return None
            self._touch(data, record)
            return str(record.get("sub", "")) or None

    def _touch(self, data: dict[str, Any], record: dict[str, Any]) -> None:
        """最終利用日時を更新する（間引いて書く。書けなくても認証は通す）。"""
        now = _now()
        last = record.get("last_used_at")
        try:
            if last and now - datetime.fromisoformat(last) < _LAST_USED_RESOLUTION:
                return
        except ValueError:
            pass
        tokens = [({**r, "last_used_at": now.isoformat()} if r is record else r)
                  for r in data["tokens"]]
        try:
            self._save({**data, "tokens": tokens})
        except OSError:
            # 読み取り専用のファイル（所有者の違うボリュームなど）でも認証は止めない。
            pass
