"""HeliusApiSource のテスト（HTTP / シンボル解決はモック）

Solana ウォレット取引の分類ロジックを検証する。
"""
from decimal import Decimal

from crypto_summary.sources.solana.helius import HeliusApiSource, _KNOWN_MINTS, _WSOL_MINT
from crypto_summary.core.models import TxType

WALLET = "AniMLiuHWAguMpBytchfKaC9rc6YpEuumGQhxAiX9Dt4"
OTHER = "OtherWalletAddress111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SPAM_MINT = "Sp4mM1ntAddr3ss111111111111111111111111111"
SIG1 = "5YE4abcdefghijklmnopqrstuvwxyz1234567890AB"
SIG2 = "6ZF5abcdefghijklmnopqrstuvwxyz1234567890CD"

_TS = 1733788800  # 2024-12-10 00:00:00 UTC


class FakeHelius(HeliusApiSource):
    """_request と _resolve_symbols をモックするテスト用サブクラス。"""

    def __init__(self, data: list[dict], symbols: dict[str, tuple[str, str]] | None = None):
        super().__init__("sol", WALLET, "FAKEKEY")
        self._data = data
        self._symbols = symbols or {}

    def _request(self, before):
        return self._data if before is None else []

    def _resolve_symbols(self, mints):
        # 既知ミントは本番同様に解決し、それ以外はテスト指定の symbols を使う
        return {
            m: (
                (_KNOWN_MINTS[m], _KNOWN_MINTS[m]) if m in _KNOWN_MINTS
                else self._symbols.get(m, ("", ""))
            )
            for m in mints
        }


def _native(frm, to, lamports):
    return {"fromUserAccount": frm, "toUserAccount": to, "amount": lamports}


def _token(frm, to, mint, amount):
    return {"fromUserAccount": frm, "toUserAccount": to, "mint": mint, "tokenAmount": amount}


# ── SOL 受取 ─────────────────────────────────────────────────────────

def test_sol_deposit():
    """SOL を受け取った場合は DEPOSIT になる。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [_native(OTHER, WALLET, 1_500_000_000)],
        "tokenTransfers": [],
    }])
    txs = src.fetch_all(record_gas=True)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "SOL"
    assert tx.received_amount == Decimal("1.5")


# ── ガス: 受取側（fee payer でない）は払わない ──────────────────────

def test_no_gas_for_receiver():
    """fee payer がウォレットでない取引はガス代を計上しない。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [_native(OTHER, WALLET, 1_000_000_000)],
        "tokenTransfers": [],
    }])
    txs = src.fetch_all(record_gas=True)
    assert all(t.type != TxType.FEE for t in txs)


# ── SOL 送出 + ガス ─────────────────────────────────────────────────

def test_sol_withdraw_with_gas():
    """SOL を送った場合は WITHDRAW + FEE になる（fee payer = wallet）。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "TRANSFER",
        "nativeTransfers": [_native(WALLET, OTHER, 500_000_000)],
        "tokenTransfers": [],
    }])
    txs = src.fetch_all(record_gas=True)
    wd = [t for t in txs if t.type == TxType.WITHDRAW]
    fee = [t for t in txs if t.type == TxType.FEE]
    assert len(wd) == 1
    assert wd[0].sent_asset == "SOL"
    assert wd[0].sent_amount == Decimal("0.5")
    assert len(fee) == 1
    assert fee[0].fee_amount == Decimal("0.000005")  # 5000 lamports


# ── SOL → USDC スワップ ──────────────────────────────────────────────

def test_sol_to_token_swap():
    """SOL 送出 + USDC 受取は TRADE になる（既知ミントでシンボル解決）。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP",
        "nativeTransfers": [_native(WALLET, OTHER, 1_000_000_000)],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 200)],
    }])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    tx = trade[0]
    assert tx.sent_asset == "SOL"
    assert tx.sent_amount == Decimal("1")
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("200")


# ── USDC → USDT スワップ ─────────────────────────────────────────────

def test_token_to_token_swap():
    """USDC → USDT は TRADE になる。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP",
        "nativeTransfers": [],
        "tokenTransfers": [
            _token(WALLET, OTHER, USDC_MINT, 100),
            _token(OTHER, WALLET, USDT_MINT, "99.9"),
        ],
    }])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    assert trade[0].sent_asset == "USDC"
    assert trade[0].received_asset == "USDT"


# ── 単一トークン受取 ─────────────────────────────────────────────────

def test_single_token_deposit():
    """SPL トークンのみ受取は DEPOSIT になる。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 5)],
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_asset == "USDC"
    assert txs[0].received_amount == Decimal("5")


# ── REWARD タイプ ────────────────────────────────────────────────────

def test_staking_reward_is_reward():
    """type に REWARD を含む単一受取は REWARD になる。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": OTHER,
        "type": "STAKE_REWARD",
        "nativeTransfers": [_native(OTHER, WALLET, 100_000_000)],
        "tokenTransfers": [],
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].type == TxType.REWARD
    assert txs[0].received_asset == "SOL"


# ── WSOL ラップ二重計上防止 ──────────────────────────────────────────

WSOL_TOKEN_ACCT = "CoqYCRCaWmmZ4NEYioAUxBaXeuN7CsUaXKhqwiBKwL8d"
WSOL_TOKEN_ACCT2 = "4ct7br2vTPzfdmY3S5HLtTxcGSBfn6pnw98hsS6v359A"


def _wsol_token(frm_ta, to_ta, frm_ua, to_ua, amount):
    return {
        "fromTokenAccount": frm_ta, "toTokenAccount": to_ta,
        "fromUserAccount": frm_ua, "toUserAccount": to_ua,
        "tokenAmount": amount, "mint": _WSOL_MINT, "tokenStandard": "Fungible",
    }


def test_wsol_wrap_not_double_counted():
    """SOL→WSOLラップ＋WSOLスワップは二重計上しない（JupiterのSOL→USDC swap相当）。

    WSOL の SOL 裏付けは nativeTransfers に既出なので、SOL は nativeTransfers の
    正味だけで集計し、WSOL の tokenTransfers は無視する。
    nativeTransfer: WALLET → WSOLアカウント 6.010297848 SOL  ← ラップ（OUT）
    nativeTransfer: WSOLアカウント → WALLET 0.059308774 SOL  ← 返却（IN）
    nativeTransfer: WALLET → 手数料先 0.003606178 SOL         ← 手数料（OUT）
    tokenTransfer(WSOL): 無視
    tokenTransfer(USDC): OTHER → WALLET 569.38 USDC           ← 受取
    正味SOL送出 = 6.010297848 - 0.059308774 + 0.003606178 = 5.954595252
    """
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP",
        "nativeTransfers": [
            {"fromUserAccount": WALLET, "toUserAccount": WSOL_TOKEN_ACCT, "amount": 6_010_297_848},
            {"fromUserAccount": WSOL_TOKEN_ACCT, "toUserAccount": WALLET, "amount": 59_308_774},
            {"fromUserAccount": WALLET, "toUserAccount": OTHER, "amount": 3_606_178},
        ],
        "tokenTransfers": [
            _wsol_token(WSOL_TOKEN_ACCT, WSOL_TOKEN_ACCT2, WALLET, OTHER, 5.953028354),
            _token(OTHER, WALLET, USDC_MINT, 569.38),
        ],
    }])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    tx = trade[0]
    assert tx.sent_asset == "SOL"
    assert tx.sent_amount == (
        Decimal("6010297848") - Decimal("59308774") + Decimal("3606178")
    ) / 10**9
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("569.38")


def test_wsol_no_double_count_in_balance():
    """WSOL の tokenTransfers は無視され、独立したWSOL残高が生じない。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": WALLET,
        "type": "TRANSFER",
        "nativeTransfers": [
            {"fromUserAccount": WALLET, "toUserAccount": WSOL_TOKEN_ACCT, "amount": 1_000_000_000},
        ],
        "tokenTransfers": [
            _wsol_token(WSOL_TOKEN_ACCT, WSOL_TOKEN_ACCT2, WALLET, OTHER, 0.99),
        ],
    }])
    txs = src.fetch_all(record_gas=False)
    assets = {t.sent_asset for t in txs if t.sent_asset}
    assert "WSOL" not in assets
    # SOL のみが送出として計上される
    assert assets == {"SOL"}


# ── スワップのおつり（net 相殺）──────────────────────────────────────

def test_sol_change_is_netted():
    """SOL を送りつつ少額のおつりが戻る場合、正味の送出額で TRADE になる。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP",
        "nativeTransfers": [
            _native(WALLET, OTHER, 1_000_000_000),  # 1.0 SOL 送出
            _native(OTHER, WALLET, 100_000_000),    # 0.1 SOL おつり
        ],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 180)],
    }])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    assert trade[0].sent_asset == "SOL"
    assert trade[0].sent_amount == Decimal("0.9")  # 1.0 - 0.1
    assert trade[0].received_asset == "USDC"


# ── Unicode ホモグラフスパムはスキップ ───────────────────────────────

def test_unicode_homograph_skipped():
    """非 ASCII シンボルのトークンはスパム扱いでスキップ。"""
    src = FakeHelius(
        [{
            "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": OTHER,
            "type": "TRANSFER",
            "nativeTransfers": [],
            "tokenTransfers": [_token(OTHER, WALLET, SPAM_MINT, 5000)],
        }],
        symbols={SPAM_MINT: ("UЅdС", "USD Coin")},  # Cyrillic lookalike
    )
    txs = src.fetch_all(record_gas=False)
    assert txs == []


# ── フィッシング URL スパムはスキップ ────────────────────────────────

def test_phishing_url_skipped():
    """URL を含む名称のトークンはスパム扱いでスキップ。"""
    src = FakeHelius(
        [{
            "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": OTHER,
            "type": "TRANSFER",
            "nativeTransfers": [],
            "tokenTransfers": [_token(OTHER, WALLET, SPAM_MINT, 5000)],
        }],
        symbols={SPAM_MINT: ("CLAIM", "Visit https://claim.xyz/ get reward")},
    )
    txs = src.fetch_all(record_gas=False)
    assert txs == []


# ── 未解決ミントは短縮表示にフォールバック ──────────────────────────

def test_unresolved_mint_uses_short_form():
    """シンボル未解決のミントは短縮ミント名で記録される。"""
    mint = "AbCdEf1234567890GhIjKlMnOpQrStUvWxYz999999"
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [],
        "tokenTransfers": [_token(OTHER, WALLET, mint, 42)],
    }])  # symbols 空 → 解決できない
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].received_asset == "AbCdEf…99999"


# ── ページング: limit 未満で停止 ─────────────────────────────────────

def test_single_page_stops():
    """1ページが limit 未満のとき 2ページ目を取得しない。"""
    calls = []

    class CountingHelius(HeliusApiSource):
        def __init__(self):
            super().__init__("sol", WALLET, "KEY")

        def _request(self, before):
            calls.append(before)
            if before is None:
                return [{
                    "signature": SIG1, "timestamp": _TS, "fee": 0, "feePayer": OTHER,
                    "type": "TRANSFER",
                    "nativeTransfers": [_native(OTHER, WALLET, 1_000_000_000)],
                    "tokenTransfers": [],
                }]
            return []

        def _resolve_symbols(self, mints):
            return {}

    src = CountingHelius()
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert calls == [None]  # 2ページ目は取得しない


# ── 空レスポンス ──────────────────────────────────────────────────────

def test_empty_result():
    src = FakeHelius([])
    assert src.fetch_all() == []
