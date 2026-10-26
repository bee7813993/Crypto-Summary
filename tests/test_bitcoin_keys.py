"""Bitcoin のアドレス検証・拡張公開鍵からの導出のテスト（BIP の公開テストベクタ）。

ネットワークも外部ライブラリも使わない。導出が 1 ビットでも違えば
別のアドレスになり取引が 1 件も見つからなくなるため、BIP32 / 44 / 49 / 84 / 86 の
公式ベクタと突き合わせる。
"""
import hashlib

import pytest

from crypto_summary.sources.bitcoin import keys
from crypto_summary.sources.bitcoin.keys import (
    P2PKH,
    P2SH_P2WPKH,
    P2TR,
    P2WPKH,
    is_bitcoin_address,
    is_bitcoin_wallet,
    parse_extended_pubkey,
    parse_wallet_spec,
    segwit_decode,
)

# BIP84 テストベクタ（mnemonic: abandon ×11 about）m/84'/0'/0'
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868"
        "RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")
# BIP86 テストベクタ m/86'/0'/0'
XPUB_TR = ("xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9Qyr"
           "LprQ3VCECoY49yfdDEHGCtMMj92pReUsQ")
# BIP49 テストベクタ m/49'/0'/0'
YPUB = ("ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979"
        "FfaHxHcrArf3zbeJJJUZPf663zsP")
# BIP44 m/44'/0'/0'（同じ mnemonic）
XPUB_44 = ("xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3"
           "DSSSSoekkudhUd9yLb6qx39T9nMdj")
XPRV = ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kej"
        "MRNNU3TGtRBeJgk33yuGBxrMPHi")


def test_bip32_public_child_derivation_matches_vector_1():
    """BIP32 Test vector 1: m/0H の xpub から子 1 を公開導出すると m/0H/1 の xpub になる。"""
    parent = parse_extended_pubkey(
        "xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw"
    )
    assert parent is not None
    child = parent.child(1)
    fingerprint = hashlib.new("ripemd160", hashlib.sha256(parent.pubkey).digest()).digest()[:4]
    assert keys.serialize_extended_pubkey(child, bytes.fromhex("0488B21E"), fingerprint, 1) == (
        "xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ"
    )


def test_bip84_zpub_addresses():
    key = parse_extended_pubkey(ZPUB)
    assert key is not None
    assert key.prefix == "zpub"
    assert key.script_types == (P2WPKH,)
    assert key.address(0, 0, P2WPKH) == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    assert key.address(0, 1, P2WPKH) == "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"
    assert key.address(1, 0, P2WPKH) == "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"


def test_bip86_taproot_addresses():
    key = parse_extended_pubkey(XPUB_TR)
    assert key is not None
    assert key.address(0, 0, P2TR) == "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
    assert key.address(0, 1, P2TR) == "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh"
    assert key.address(1, 0, P2TR) == "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7"


def test_bip49_ypub_address():
    key = parse_extended_pubkey(YPUB)
    assert key is not None
    assert key.script_types == (P2SH_P2WPKH,)
    assert key.address(0, 0, P2SH_P2WPKH) == "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"


def test_bip44_xpub_address():
    key = parse_extended_pubkey(XPUB_44)
    assert key is not None
    assert key.address(0, 0, P2PKH) == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"


def test_xpub_scans_all_script_types():
    """xpub はウォレットにより BIP44 / 86 / Ledger の全種別で使われるため、1 つに決めない。"""
    key = parse_extended_pubkey(XPUB_TR)
    assert key is not None
    assert set(key.script_types) == {P2PKH, P2SH_P2WPKH, P2WPKH, P2TR}


def test_hardened_child_rejected():
    key = parse_extended_pubkey(ZPUB)
    with pytest.raises(ValueError):
        key.child(0x80000000)


@pytest.mark.parametrize("address", [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",                      # P2PKH（genesis）
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",                      # P2SH
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",              # P2WPKH（BIP173）
    "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",              # 大文字も可
    "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",  # P2WSH
    "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr",  # P2TR
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",  # BIP350 vector
])
def test_valid_mainnet_addresses(address):
    assert is_bitcoin_address(address)


@pytest.mark.parametrize("address, why", [
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb", "チェックサム不一致"),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7Divf", "欠け"),
    ("tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "testnet"),
    ("bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "v1 を bech32 で符号化（bech32m 必須）"),
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", "bech32 チェックサム不一致"),
    ("bc1QW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "大文字小文字の混在"),
    ("DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L", "Dogecoin（バージョンバイト違い）"),
    ("LTC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "Litecoin の hrp"),
    ("0x" + "a" * 40, "EVM"),
    ("So11111111111111111111111111111111111111112", "Solana"),
    ("", "空"),
])
def test_invalid_addresses(address, why):
    assert not is_bitcoin_address(address), why


def test_segwit_decode_program():
    assert segwit_decode("bc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4") == (
        0, bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6"))


def test_parse_wallet_spec_address():
    spec = parse_wallet_spec("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert spec is not None and spec.kind == "address"
    assert spec.address == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def test_parse_wallet_spec_xpub():
    spec = parse_wallet_spec(f"  {ZPUB}\n")
    assert spec is not None and spec.kind == "xpub"
    assert spec.script_types == (P2WPKH,)


@pytest.mark.parametrize("descriptor, expected", [
    (f"wpkh([d34db33f/84h/0h/0h]{ZPUB}/<0;1>/*)#abcdefgh", (P2WPKH,)),
    (f"wpkh({XPUB_TR}/0/*)", (P2WPKH,)),
    (f"tr([d34db33f/86'/0'/0']{XPUB_TR}/<0;1>/*)", (P2TR,)),
    (f"pkh({XPUB_44})", (P2PKH,)),
    (f"sh(wpkh({YPUB}))", (P2SH_P2WPKH,)),
])
def test_descriptor_forces_script_type(descriptor, expected):
    """ディスクリプタで包まれていれば xpub でも種別が一意に決まる。"""
    spec = parse_wallet_spec(descriptor)
    assert spec is not None and spec.kind == "xpub"
    assert spec.script_types == expected


def test_multisig_descriptor_not_supported():
    assert parse_wallet_spec(f"wsh(sortedmulti(1,{XPUB_TR}/0/*,{ZPUB}/0/*))") is None


def test_xprv_rejected_loudly():
    """秘密鍵は「判定できない」ではなく明示的に拒否する。"""
    with pytest.raises(ValueError, match="秘密"):
        parse_wallet_spec(XPRV)
    assert not is_bitcoin_wallet(XPRV)


@pytest.mark.parametrize("text", [
    ZPUB[:-1] + "t",            # チェックサム不一致
    "xpub" + "1" * 107,         # でたらめ
    "0x" + "a" * 40,
])
def test_not_a_wallet(text):
    assert not is_bitcoin_wallet(text)
