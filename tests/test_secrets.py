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


# ---- ウォレット ----

def test_wallet_no_key_does_not_require_master_key(tmp_path: Path):
    """アドレスは公開情報。APIキー未指定ならマスター鍵なしで登録・取得できる。"""
    store = SecretStore(tmp_path / "x.db", master_key=None)
    store.set_wallet("mywallet", "0xABC123", "evm")
    w = store.get_wallet("mywallet")
    assert w["address"] == "0xABC123"
    assert w["chain"] == "evm"
    assert "api_key" not in w


def test_wallet_address_stored_plaintext(tmp_path: Path):
    store = SecretStore(tmp_path / "x.db", master_key=None)
    store.set_wallet("w", "0xDEADBEEF", "evm")
    raw = (tmp_path / "x.secrets.json").read_text(encoding="utf-8")
    assert "0xDEADBEEF" in raw  # アドレスは公開情報なので平文でよい


def test_wallet_api_key_encrypted(tmp_path: Path, key: str):
    """APIキー指定時は暗号化され、平文では残らない。"""
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_wallet("w", "0xABC", "evm", api_key="SECRETKEY", helius_key="HELIUSK")
    raw = (tmp_path / "x.secrets.json").read_text(encoding="utf-8")
    assert "SECRETKEY" not in raw
    assert "HELIUSK" not in raw
    w = store.get_wallet("w")
    assert w["api_key"] == "SECRETKEY"
    assert w["helius_key"] == "HELIUSK"


def test_wallet_api_key_requires_master_key(tmp_path: Path):
    store = SecretStore(tmp_path / "x.db", master_key=None)
    with pytest.raises(SecretStoreError):
        store.set_wallet("w", "0xABC", "evm", api_key="SECRETKEY")


def test_wallet_list_and_delete(tmp_path: Path):
    store = SecretStore(tmp_path / "x.db", master_key=None)
    store.set_wallet("w1", "0xAAA", "evm")
    store.set_wallet("w2", "SoLaNaAddr", "solana")
    wallets = store.list_wallets()
    assert {w["source_id"] for w in wallets} == {"w1", "w2"}
    # 一覧にはキーを含めない
    assert all("api_key" not in w for w in wallets)
    assert store.delete_wallet("w1") is True
    assert {w["source_id"] for w in store.list_wallets()} == {"w2"}
    assert store.delete_wallet("nope") is False


def test_wallet_and_account_coexist(tmp_path: Path, key: str):
    """同じ secrets ファイルで API 口座とウォレットが共存できる。"""
    store = SecretStore(tmp_path / "x.db", master_key=key)
    store.set_account_api("bybit1", "bybit", "k", "s")
    store.set_wallet("w1", "0xABC", "evm")
    assert len(store.list_accounts()) == 1
    assert len(store.list_wallets()) == 1
