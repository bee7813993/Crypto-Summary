"""SecretStore（口座APIキーの暗号化保存）のテスト。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_summary.core.secrets import (
    SecretStore,
    SecretStoreError,
    generate_master_key,
)


@pytest.fixture
def key() -> str:
    return generate_master_key()


def test_set_get_roundtrip(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("mybybit", "bybit", "KEY123", "SECRET456", category="spot")
    creds = store.get_account_api("mybybit")
    assert creds["exchange"] == "bybit"
    assert creds["category"] == "spot"
    assert creds["api_key"] == "KEY123"
    assert creds["api_secret"] == "SECRET456"


def test_secrets_file_has_no_plaintext(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("mybybit", "bybit", "PLAINKEY", "PLAINSECRET")
    raw = (tmp_path / "x.secrets.json").read_text(encoding="utf-8")
    assert "PLAINKEY" not in raw
    assert "PLAINSECRET" not in raw
    # 暗号文フィールドは存在する
    data = json.loads(raw)
    rec = next(iter(data["accounts"].values()))
    assert "api_key_enc" in rec and "api_secret_enc" in rec


def test_get_missing_returns_none(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    assert store.get_account_api("nope") is None


def test_list_hides_secrets(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("a", "bybit", "k1", "s1")
    store.set_account_api("b", "bitflyer", "k2", "s2")
    rows = store.list_accounts()
    assert {r["source_id"] for r in rows} == {"a", "b"}
    # メタ情報のみ（秘密は含まない）
    assert all("api_key" not in r and "api_key_enc" not in r for r in rows)


def test_delete(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("a", "bybit", "k1", "s1")
    assert store.delete_account("a") is True
    assert store.get_account_api("a") is None
    assert store.delete_account("a") is False


def test_multi_user_separation(tmp_path: Path, key: str):
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("acct", "bybit", "ALICEK", "ALICES", user_id="alice")
    store.set_account_api("acct", "bybit", "BOBK", "BOBS", user_id="bob")
    assert store.get_account_api("acct", user_id="alice")["api_key"] == "ALICEK"
    assert store.get_account_api("acct", user_id="bob")["api_key"] == "BOBK"
    # 既定ユーザーには存在しない
    assert store.get_account_api("acct") is None
    # 全ユーザー一覧
    assert len(store.list_accounts(user_id=None)) == 2


def test_wrong_master_key_fails_decrypt(tmp_path: Path, key: str):
    SecretStore(tmp_path / "x.db", master_key=key).set_account_api(
        "a", "bybit", "k1", "s1")
    other = SecretStore(tmp_path / "x.db", master_key=generate_master_key())
    with pytest.raises(SecretStoreError):
        other.get_account_api("a")


def test_missing_master_key_raises_on_use(tmp_path: Path):
    store = SecretStore(tmp_path / "x.db", master_key=None)
    with pytest.raises(SecretStoreError):
        store.set_account_api("a", "bybit", "k1", "s1")
    # 一覧・削除は鍵なしでも動く（暗号化を伴わない）
    assert store.list_accounts() == []
    assert store.delete_account("a") is False


def test_invalid_master_key_format(tmp_path: Path):
    store = SecretStore(tmp_path / "x.db", master_key="not-a-valid-fernet-key")
    with pytest.raises(SecretStoreError):
        store.set_account_api("a", "bybit", "k1", "s1")
