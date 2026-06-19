"""口座ごとの API キーを暗号化して保存する SecretStore。

設計方針:
  - 平文の API キー/シークレットは追跡ファイルに一切残さない。
  - マスター鍵 CS_SECRET_KEY（Fernet 鍵）は .env / 環境変数 / Secrets に置く。
  - 暗号文は <dbname>.secrets.json（.gitignore 済み）に保存する。
  - レコードは user_id 次元を持ち、将来のマルチユーザーに備える
    （当面は "local" 固定で運用できる）。

セキュリティ要件:
  - 登録する API キーは読み取り専用のみ（出金/送付権限を付与しないこと）。
  - マスター鍵を紛失すると復号できない（再登録が必要）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_ENV_MASTER_KEY = "CS_SECRET_KEY"
_DEFAULT_USER = "local"
_SEP = "\x00"


class SecretStoreError(RuntimeError):
    """マスター鍵未設定・復号失敗など、秘密情報まわりの例外。"""


def generate_master_key() -> str:
    """新しいマスター鍵（Fernet 鍵）を生成して文字列で返す。"""
    return Fernet.generate_key().decode("ascii")


def _secrets_path(db_path: str | Path) -> Path:
    p = Path(db_path)
    return p.with_name(p.stem + ".secrets.json")


def _acct_key(user_id: str, source_id: str) -> str:
    return f"{user_id}{_SEP}{source_id}"


class SecretStore:
    """口座API資格情報の暗号化ストア。

    master_key 省略時は環境変数 CS_SECRET_KEY を使用する。
    暗号化/復号を伴わない一覧・削除は鍵なしでも動作する。
    """

    def __init__(self, db_path: str | Path, master_key: str | None = None) -> None:
        self.path = _secrets_path(db_path)
        self._master_key = master_key or os.environ.get(_ENV_MASTER_KEY)
        self._fernet: Fernet | None = None

    # -- 内部ユーティリティ ------------------------------------------------

    def _fernet_or_raise(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        if not self._master_key:
            raise SecretStoreError(
                f"マスター鍵が未設定です。環境変数 {_ENV_MASTER_KEY} を設定してください。\n"
                "  生成: crypto-summary account gen-key"
            )
        try:
            self._fernet = Fernet(self._master_key.encode("ascii"))
        except (ValueError, TypeError) as e:
            raise SecretStoreError(f"マスター鍵の形式が不正です: {e}") from e
        return self._fernet

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "accounts": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 所有者のみ読み書き可能にする（ベストエフォート）
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # -- 公開 API ----------------------------------------------------------

    def set_account_api(
        self,
        source_id: str,
        exchange: str,
        api_key: str,
        api_secret: str,
        *,
        category: str = "spot",
        user_id: str = _DEFAULT_USER,
    ) -> None:
        """口座の API 資格情報を暗号化して保存する（既存は上書き）。"""
        f = self._fernet_or_raise()
        data = self._load()
        data.setdefault("accounts", {})
        data["accounts"][_acct_key(user_id, source_id)] = {
            "user_id": user_id,
            "source_id": source_id,
            "exchange": exchange,
            "category": category,
            "api_key_enc": f.encrypt(api_key.encode()).decode("ascii"),
            "api_secret_enc": f.encrypt(api_secret.encode()).decode("ascii"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def get_account_api(
        self, source_id: str, *, user_id: str = _DEFAULT_USER
    ) -> dict[str, str] | None:
        """口座の復号済み資格情報を返す。未登録なら None。"""
        rec = self._load().get("accounts", {}).get(_acct_key(user_id, source_id))
        if not rec:
            return None
        f = self._fernet_or_raise()
        try:
            api_key = f.decrypt(rec["api_key_enc"].encode()).decode()
            api_secret = f.decrypt(rec["api_secret_enc"].encode()).decode()
        except (InvalidToken, KeyError) as e:
            raise SecretStoreError(
                "資格情報の復号に失敗しました。マスター鍵が登録時と異なる可能性があります。"
            ) from e
        return {
            "source_id": rec["source_id"],
            "exchange": rec["exchange"],
            "category": rec.get("category", "spot"),
            "api_key": api_key,
            "api_secret": api_secret,
        }

    def list_accounts(self, *, user_id: str | None = _DEFAULT_USER) -> list[dict[str, str]]:
        """登録済み口座のメタ情報（秘密情報を含まない）を返す。

        user_id=None で全ユーザー分。
        """
        out: list[dict[str, str]] = []
        for rec in self._load().get("accounts", {}).values():
            if user_id is not None and rec.get("user_id") != user_id:
                continue
            out.append({
                "user_id": rec.get("user_id", _DEFAULT_USER),
                "source_id": rec.get("source_id", ""),
                "exchange": rec.get("exchange", ""),
                "category": rec.get("category", "spot"),
                "created_at": rec.get("created_at", ""),
            })
        out.sort(key=lambda r: (r["user_id"], r["source_id"]))
        return out

    def delete_account(self, source_id: str, *, user_id: str = _DEFAULT_USER) -> bool:
        """口座の資格情報を削除する。削除できたら True。"""
        data = self._load()
        accounts = data.get("accounts", {})
        key = _acct_key(user_id, source_id)
        if key not in accounts:
            return False
        del accounts[key]
        self._save(data)
        return True

    # -- ウォレット（公開アドレス）----------------------------------------
    #
    # ウォレットアドレスとチェーンは公開情報のため平文で保存する。
    # スキャン用 API キー（Etherscan/Helius）は任意。指定された場合のみ
    # 暗号化して保存し（マスター鍵が必要）、未指定なら同期時に環境変数を使う。

    def set_wallet(
        self,
        source_id: str,
        address: str,
        chain: str,
        *,
        api_key: str | None = None,
        helius_key: str | None = None,
        user_id: str = _DEFAULT_USER,
    ) -> None:
        """ウォレットを登録する（既存は上書き）。"""
        data = self._load()
        data.setdefault("wallets", {})
        rec: dict[str, Any] = {
            "user_id": user_id,
            "source_id": source_id,
            "address": address,
            "chain": chain,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if api_key:
            rec["api_key_enc"] = self._fernet_or_raise().encrypt(api_key.encode()).decode("ascii")
        if helius_key:
            rec["helius_key_enc"] = self._fernet_or_raise().encrypt(helius_key.encode()).decode("ascii")
        data["wallets"][_acct_key(user_id, source_id)] = rec
        self._save(data)

    def get_wallet(
        self, source_id: str, *, user_id: str = _DEFAULT_USER
    ) -> dict[str, str] | None:
        """ウォレット情報を返す（API キーは登録時のみ復号して含める）。未登録なら None。"""
        rec = self._load().get("wallets", {}).get(_acct_key(user_id, source_id))
        if not rec:
            return None
        out: dict[str, str] = {
            "source_id": rec["source_id"],
            "address": rec["address"],
            "chain": rec["chain"],
        }
        if rec.get("api_key_enc") or rec.get("helius_key_enc"):
            f = self._fernet_or_raise()
            try:
                if rec.get("api_key_enc"):
                    out["api_key"] = f.decrypt(rec["api_key_enc"].encode()).decode()
                if rec.get("helius_key_enc"):
                    out["helius_key"] = f.decrypt(rec["helius_key_enc"].encode()).decode()
            except (InvalidToken, KeyError) as e:
                raise SecretStoreError(
                    "ウォレットAPIキーの復号に失敗しました。マスター鍵が登録時と異なる可能性があります。"
                ) from e
        return out

    def list_wallets(self, *, user_id: str | None = _DEFAULT_USER) -> list[dict[str, str]]:
        """登録済みウォレットのメタ情報（API キーは含まない）を返す。"""
        out: list[dict[str, str]] = []
        for rec in self._load().get("wallets", {}).values():
            if user_id is not None and rec.get("user_id") != user_id:
                continue
            out.append({
                "user_id": rec.get("user_id", _DEFAULT_USER),
                "source_id": rec.get("source_id", ""),
                "address": rec.get("address", ""),
                "chain": rec.get("chain", ""),
                "created_at": rec.get("created_at", ""),
            })
        out.sort(key=lambda r: (r["user_id"], r["source_id"]))
        return out

    def delete_wallet(self, source_id: str, *, user_id: str = _DEFAULT_USER) -> bool:
        """ウォレット登録を削除する。削除できたら True。"""
        data = self._load()
        wallets = data.get("wallets", {})
        key = _acct_key(user_id, source_id)
        if key not in wallets:
            return False
        del wallets[key]
        self._save(data)
        return True
