"""Bitcoin のアドレス検証と、拡張公開鍵（xpub / ypub / zpub）からのアドレス導出。

Self-custody ウォレット（ハードウェアウォレット、Sparrow、BlueWallet など）は
送金のたびに新しい受取アドレス・おつりアドレスを使うため、1 つのアドレスを
登録しただけでは「送金したら残高がゼロになった（おつりが別アドレスに戻った）」
ように見えてしまう。ウォレットが書き出す拡張公開鍵を登録すれば、そこから
全アドレスを導出して追跡できる。

扱うもの（すべて公開情報。秘密鍵・シード・xprv は一切扱わない）:
  - 単一アドレス: P2PKH（1…）/ P2SH（3…）/ P2WPKH・P2WSH（bc1q…）/ P2TR（bc1p…）
  - 拡張公開鍵: xpub（BIP44 legacy / BIP86 taproot）、ypub（BIP49 nested segwit）、
    zpub（BIP84 native segwit）。アカウントレベル（m/purpose'/0'/account'）の鍵を想定し、
    そこから 0/i（受取）・1/i（おつり）を非強化導出する。
  - 出力ディスクリプタ: pkh(…) / sh(wpkh(…)) / wpkh(…) / tr(…) で包んだ単一鍵。
    Sparrow / Bitcoin Core が書き出す `[fingerprint/path]` と `/<0;1>/*` は読み飛ばす。
    マルチシグ（wsh(sortedmulti(…)) 等）は対象外。

外部ライブラリに依存せず、secp256k1 の点演算と BIP32 の公開鍵導出を自前で持つ。
署名はしないので定数時間性は不要で、正しさだけを BIP32 / 84 / 86 のテストベクタで
確認している（tests/test_bitcoin_keys.py）。

参照: BIP32（HD 鍵）, BIP44/49/84/86（導出パス）, BIP173（bech32）, BIP350（bech32m）,
      SLIP-0132（ypub / zpub のバージョンバイト）。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# ハッシュ
# ---------------------------------------------------------------------------


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", _sha256(b)).digest()


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    """BIP340 のタグ付きハッシュ（taproot の tweak 計算に使う）。"""
    t = _sha256(tag.encode())
    return _sha256(t + t + msg)


# ---------------------------------------------------------------------------
# Base58Check
# ---------------------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}


def b58decode_check(s: str) -> bytes | None:
    """Base58Check を復号して payload（チェックサムを除く）を返す。不正なら None。"""
    if not s or any(c not in _B58_INDEX for c in s):
        return None
    value = 0
    for c in s:
        value = value * 58 + _B58_INDEX[c]
    leading = len(s) - len(s.lstrip("1"))
    body = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    raw = b"\x00" * leading + body
    if len(raw) < 5:
        return None
    payload, checksum = raw[:-4], raw[-4:]
    if _sha256(_sha256(payload))[:4] != checksum:
        return None
    return payload


def b58encode_check(payload: bytes) -> str:
    raw = payload + _sha256(_sha256(payload))[:4]
    value = int.from_bytes(raw, "big")
    out = ""
    while value:
        value, rem = divmod(value, 58)
        out = _B58[rem] + out
    leading = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading + out


# ---------------------------------------------------------------------------
# Bech32 / Bech32m（BIP173 / BIP350）
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify(hrp: str, data: list[int]) -> str | None:
    """チェックサムが bech32 / bech32m のどちらとして正しいかを返す。不正なら None。"""
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        return "bech32"
    if const == _BECH32M_CONST:
        return "bech32m"
    return None


def _bech32_create_checksum(hrp: str, data: list[int], const: int) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0] * 6) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes | list[int], frombits: int, tobits: int, pad: bool) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def segwit_decode(hrp: str, addr: str) -> tuple[int, bytes] | None:
    """segwit アドレスを (witness version, program) に復号する。不正なら None。"""
    if any(ord(c) < 33 or ord(c) > 126 for c in addr):
        return None
    if addr.lower() != addr and addr.upper() != addr:
        return None  # 大文字小文字の混在は不正
    a = addr.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90:
        return None
    if a[:pos] != hrp:
        return None
    data_part = a[pos + 1:]
    if any(c not in _BECH32_CHARSET for c in data_part):
        return None
    data = [_BECH32_CHARSET.find(c) for c in data_part]
    encoding = _bech32_verify(hrp, data)
    if encoding is None:
        return None
    payload = data[:-6]
    if not payload:
        return None
    version = payload[0]
    program = _convertbits(payload[1:], 5, 8, False)
    if program is None or version > 16:
        return None
    if not (2 <= len(program) <= 40):
        return None
    if version == 0 and len(program) not in (20, 32):
        return None
    if (version == 0) != (encoding == "bech32"):
        return None  # v0 は bech32、v1 以降は bech32m でなければならない
    return version, bytes(program)


def segwit_encode(hrp: str, version: int, program: bytes) -> str:
    data = [version] + (_convertbits(program, 8, 5, True) or [])
    const = _BECH32_CONST if version == 0 else _BECH32M_CONST
    checksum = _bech32_create_checksum(hrp, data, const)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


# ---------------------------------------------------------------------------
# secp256k1（公開鍵の点演算のみ。署名・秘密鍵は扱わない）
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_Point = tuple[int, int] | None  # None = 無限遠点


def _pt_add(a: _Point, b: _Point) -> _Point:
    if a is None:
        return b
    if b is None:
        return a
    ax, ay = a
    bx, by = b
    if ax == bx:
        if (ay + by) % _P == 0:
            return None
        lam = (3 * ax * ax) * pow(2 * ay, -1, _P) % _P
    else:
        lam = (by - ay) * pow(bx - ax, -1, _P) % _P
    x = (lam * lam - ax - bx) % _P
    y = (lam * (ax - x) - ay) % _P
    return x, y


def _pt_mul(k: int, pt: _Point) -> _Point:
    result: _Point = None
    addend = pt
    while k:
        if k & 1:
            result = _pt_add(result, addend)
        addend = _pt_add(addend, addend)
        k >>= 1
    return result


def _pt_decode(b: bytes) -> tuple[int, int] | None:
    """圧縮公開鍵（33 バイト）を点に戻す。曲線上に無ければ None。"""
    if len(b) != 33 or b[0] not in (2, 3):
        return None
    x = int.from_bytes(b[1:], "big")
    if x >= _P:
        return None
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if y * y % _P != y_sq:
        return None
    if (y & 1) != (b[0] & 1):
        y = _P - y
    return x, y


def _pt_encode(pt: tuple[int, int]) -> bytes:
    x, y = pt
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


# ---------------------------------------------------------------------------
# 拡張公開鍵（BIP32 / SLIP-0132）
# ---------------------------------------------------------------------------

# スクリプト種別。拡張鍵のバージョンバイトとディスクリプタの包み方で決まる。
P2PKH = "p2pkh"          # BIP44  1…
P2SH_P2WPKH = "p2sh-p2wpkh"  # BIP49  3…
P2WPKH = "p2wpkh"        # BIP84  bc1q…
P2TR = "p2tr"            # BIP86  bc1p…

SCRIPT_TYPE_LABELS: dict[str, str] = {
    P2PKH: "Legacy (1…)",
    P2SH_P2WPKH: "Nested SegWit (3…)",
    P2WPKH: "Native SegWit (bc1q…)",
    P2TR: "Taproot (bc1p…)",
}

# バージョンバイト → (表記, 示唆されるスクリプト種別)。
# xpub はウォレットによって BIP44 / BIP86 どちらにも、また Ledger Live のように
# 全アカウント種別で使われるため、1 つに決めず全種別を走査する（None）。
_XPUB_VERSIONS: dict[bytes, tuple[str, str | None]] = {
    bytes.fromhex("0488B21E"): ("xpub", None),
    bytes.fromhex("049D7CB2"): ("ypub", P2SH_P2WPKH),
    bytes.fromhex("04B24746"): ("zpub", P2WPKH),
}
_XPRV_PREFIXES = ("xprv", "yprv", "zprv")

_HARDENED = 0x80000000


@dataclass(frozen=True)
class ExtendedPubKey:
    """アカウントレベルの拡張公開鍵。"""

    depth: int
    chain_code: bytes
    point: tuple[int, int]
    prefix: str                      # xpub / ypub / zpub
    script_types: tuple[str, ...]    # 導出するアドレスの種別（複数なら全部走査）

    @property
    def pubkey(self) -> bytes:
        return _pt_encode(self.point)

    def child(self, index: int) -> "ExtendedPubKey":
        """非強化の子鍵を導出する（CKDpub）。"""
        if index >= _HARDENED:
            raise ValueError("公開鍵から強化導出はできません")
        data = self.pubkey + index.to_bytes(4, "big")
        i = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        il = int.from_bytes(i[:32], "big")
        if il >= _N:
            raise ValueError("導出結果が無効（確率 2^-127、インデックスを飛ばす）")
        pt = _pt_add(_pt_mul(il, _G), self.point)
        if pt is None:
            raise ValueError("導出結果が無効（無限遠点）")
        return ExtendedPubKey(self.depth + 1, i[32:], pt, self.prefix, self.script_types)

    def address(self, change: int, index: int, script_type: str) -> str:
        """m/<change>/<index> のアドレスを返す。"""
        leaf = self.child(change).child(index)
        return address_from_pubkey(leaf.pubkey, script_type)


def parse_extended_pubkey(text: str) -> ExtendedPubKey | None:
    """xpub / ypub / zpub 文字列を解釈する。拡張公開鍵でなければ None。

    xprv などの秘密鍵は、形式が合っていても ValueError で明示的に拒否する
    （黙って None を返すと「形式を判定できない」という誤ったメッセージになる）。
    """
    s = text.strip()
    if s[:4].lower() in _XPRV_PREFIXES and len(s) > 100:
        raise ValueError(
            "拡張【秘密】鍵（xprv / yprv / zprv）が入力されました。絶対に登録しないでください。"
            "ウォレットの「拡張公開鍵（xpub / ypub / zpub）」を書き出して登録してください。"
        )
    payload = b58decode_check(s)
    if payload is None or len(payload) != 78:
        return None
    ver = payload[:4]
    if ver not in _XPUB_VERSIONS:
        return None
    prefix, script_type = _XPUB_VERSIONS[ver]
    depth = payload[4]
    chain_code = payload[13:45]
    point = _pt_decode(payload[45:78])
    if point is None:
        return None
    types = (script_type,) if script_type else (P2WPKH, P2TR, P2SH_P2WPKH, P2PKH)
    return ExtendedPubKey(depth, chain_code, point, prefix, types)


def serialize_extended_pubkey(key: ExtendedPubKey, version: bytes, parent_fp: bytes, child: int) -> str:
    """テスト用: BIP32 形式で直列化する。"""
    payload = version + bytes([key.depth]) + parent_fp + child.to_bytes(4, "big") + key.chain_code + key.pubkey
    return b58encode_check(payload)


# ---------------------------------------------------------------------------
# 公開鍵 → アドレス
# ---------------------------------------------------------------------------

HRP = "bc"


def address_from_pubkey(pubkey: bytes, script_type: str) -> str:
    """圧縮公開鍵（33 バイト）から各スクリプト種別のアドレスを作る。"""
    if script_type == P2PKH:
        return b58encode_check(b"\x00" + _hash160(pubkey))
    if script_type == P2SH_P2WPKH:
        redeem = b"\x00\x14" + _hash160(pubkey)
        return b58encode_check(b"\x05" + _hash160(redeem))
    if script_type == P2WPKH:
        return segwit_encode(HRP, 0, _hash160(pubkey))
    if script_type == P2TR:
        return segwit_encode(HRP, 1, taproot_output_key(pubkey))
    raise ValueError(f"未対応のスクリプト種別: {script_type}")


def taproot_output_key(pubkey: bytes) -> bytes:
    """BIP86: スクリプトパス無しの taproot 出力鍵（x のみ 32 バイト）。"""
    internal = _pt_decode(pubkey)
    if internal is None:
        raise ValueError("公開鍵が不正です")
    # x-only 内部鍵は y が偶数の方を採る
    if internal[1] & 1:
        internal = (internal[0], _P - internal[1])
    x_only = internal[0].to_bytes(32, "big")
    tweak = int.from_bytes(_tagged_hash("TapTweak", x_only), "big")
    if tweak >= _N:
        raise ValueError("tweak が無効です")
    out = _pt_add(internal, _pt_mul(tweak, _G))
    if out is None:
        raise ValueError("出力鍵が無効です")
    return out[0].to_bytes(32, "big")


# ---------------------------------------------------------------------------
# 単一アドレスの検証
# ---------------------------------------------------------------------------


def is_bitcoin_address(text: str) -> bool:
    """mainnet の Bitcoin アドレスとして妥当か（チェックサムまで確認する）。"""
    s = text.strip()
    if not s:
        return False
    if s[:1] in ("1", "3"):
        payload = b58decode_check(s)
        return payload is not None and len(payload) == 21 and payload[0] in (0x00, 0x05)
    if s.lower().startswith(HRP + "1"):
        return segwit_decode(HRP, s) is not None
    return False


# ---------------------------------------------------------------------------
# 出力ディスクリプタ（単一鍵のみ）
# ---------------------------------------------------------------------------

# 例: wpkh([d34db33f/84h/0h/0h]zpub6r…/<0;1>/*)#checksum
#     pkh(xpub…/0/*)   sh(wpkh(ypub…))   tr([fp/86'/0'/0']xpub…/<0;1>/*)
_DESCRIPTOR_RE = re.compile(
    r"^(?P<wrap>pkh|wpkh|sh\(wpkh|tr)\("
    r"(?:\[[0-9a-fA-F]{8}(?:/[0-9]+['hH]?)*\])?"
    r"(?P<key>[1-9A-HJ-NP-Za-km-z]{100,120})"
    r"(?:/(?:<0;1>|[01]))?(?:/\*)?"
    r"\)\)?(?:#[0-9a-z]{8})?$"
)
_DESCRIPTOR_TYPES = {"pkh": P2PKH, "sh(wpkh": P2SH_P2WPKH, "wpkh": P2WPKH, "tr": P2TR}


@dataclass(frozen=True)
class WalletSpec:
    """登録文字列の解釈結果。単一アドレスか、拡張公開鍵（+ スクリプト種別）か。"""

    kind: str                        # "address" | "xpub"
    address: str | None = None
    key: ExtendedPubKey | None = None

    @property
    def script_types(self) -> tuple[str, ...]:
        return self.key.script_types if self.key else ()


def parse_wallet_spec(text: str) -> WalletSpec | None:
    """登録文字列を解釈する。Bitcoin のアドレス / 拡張公開鍵 / ディスクリプタでなければ None。

    xprv が渡された場合は ValueError（parse_extended_pubkey 参照）。
    """
    s = text.strip()
    if not s:
        return None
    if is_bitcoin_address(s):
        return WalletSpec("address", address=s)
    m = _DESCRIPTOR_RE.match(s)
    if m:
        key = parse_extended_pubkey(m.group("key"))
        if key is None:
            return None
        forced = _DESCRIPTOR_TYPES[m.group("wrap")]
        key = ExtendedPubKey(key.depth, key.chain_code, key.point, key.prefix, (forced,))
        return WalletSpec("xpub", key=key)
    key = parse_extended_pubkey(s)
    if key is not None:
        return WalletSpec("xpub", key=key)
    return None


def is_bitcoin_wallet(text: str) -> bool:
    """登録可能な Bitcoin のアドレス / 拡張公開鍵 / ディスクリプタか。xprv は False。"""
    try:
        return parse_wallet_spec(text) is not None
    except ValueError:
        return False
