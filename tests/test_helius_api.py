"""HeliusApiSource のテスト（HTTP / シンボル解決はモック）

Solana ウォレット取引の分類ロジックを検証する。
"""
from decimal import Decimal

from crypto_summary.sources.solana import helius as helius_module
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


# ── ブリッジ受取（CCTP: リレイヤー送信・ATA へ直接 mint）─────────────
#
# Portal Bridge の CCTP で受け取った USDC は、Circle の MessageTransmitter を
# リレイヤーが呼び出してウォレットの USDC トークンアカウント（ATA）へ直接
# mint される。ウォレット本体は署名者にも fee payer にも account keys にも
# 現れないため、履歴 API を既定のまま叩くとこの取引は返ってこない。
# 以下は実際の RECEIVE_MESSAGE 取引（CIRCLE_CCTP_CORE_V2）の形をなぞった fixture。

USDC_ATA = "3Nidkdoqhh8JiyckvawNApphSPLB7nCpwqyJBGwaFUNw"        # ウォレットの USDC ATA
RELAYER = "9BqVj1NhaohJsFAjQWAWcHiJWu48XwfzXe48US2AioNc"         # CCTP リレイヤー（fee payer）
MINT_AUTHORITY = "E1bQJ8eMMn3zmeSewW3HQ8zmJr7KR75JonbwAtWx2bux"  # TokenMessengerMinter 側
MINTER_TOKEN_ACCT = "6xTBTqJMBr5m7BKqVxmW2x11DfqUwtD3TJsqpxELx72L"


def _balance_change(account, user, mint, raw_amount, decimals, native=0):
    """accountData の 1 要素（トークン残高変化 1 件）を作る。"""
    return {
        "account": account,
        "nativeBalanceChange": native,
        "tokenBalanceChanges": [{
            "userAccount": user, "tokenAccount": account, "mint": mint,
            "rawTokenAmount": {"tokenAmount": str(raw_amount), "decimals": decimals},
        }],
    }


def _cctp_receive(sig, raw_usdc):
    amount = Decimal(raw_usdc) / Decimal(10**6)
    return {
        "signature": sig, "timestamp": _TS, "fee": 5452, "feePayer": RELAYER,
        "type": "RECEIVE_MESSAGE", "source": "CIRCLE_CCTP_CORE_V2",
        "transactionError": None,
        "nativeTransfers": [_native(RELAYER, OTHER, 695_960)],
        "tokenTransfers": [{
            "fromTokenAccount": MINTER_TOKEN_ACCT, "toTokenAccount": USDC_ATA,
            "fromUserAccount": MINT_AUTHORITY, "toUserAccount": WALLET,
            "tokenAmount": float(amount), "mint": USDC_MINT, "tokenStandard": "Fungible",
        }],
        "accountData": [
            {"account": RELAYER, "nativeBalanceChange": -701_412, "tokenBalanceChanges": []},
            _balance_change(USDC_ATA, WALLET, USDC_MINT, raw_usdc, 6),
            _balance_change(MINTER_TOKEN_ACCT, MINT_AUTHORITY, USDC_MINT, -raw_usdc, 6),
        ],
    }


def test_cctp_receive_is_deposit():
    """CCTP 受取（リレイヤー送信・ATA へ mint）は DEPOSIT として記録され、ガスは付かない。"""
    src = FakeHelius([_cctp_receive(SIG1, 505_247_212)])
    txs = src.fetch_all(record_gas=True)
    assert len(txs) == 1  # fee payer はリレイヤーなのでガスは計上しない
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("505.247212")
    assert tx.label == "receive_message"
    assert tx.tx_hash == SIG1


def test_cctp_receive_then_withdraw_nets_to_zero():
    """受取が記録されれば、その後の出金で残高がマイナスにならない。"""
    src = FakeHelius([
        {
            "signature": SIG2, "timestamp": _TS + 60, "fee": 5000, "feePayer": WALLET,
            "type": "TRANSFER", "source": "SOLANA_PROGRAM_LIBRARY",
            "nativeTransfers": [],
            "tokenTransfers": [_token(WALLET, OTHER, USDC_MINT, 100)],
            "accountData": [
                {"account": WALLET, "nativeBalanceChange": -5000, "tokenBalanceChanges": []},
                _balance_change(USDC_ATA, WALLET, USDC_MINT, -100_000_000, 6),
            ],
        },
        _cctp_receive(SIG1, 100_000_000),
    ])
    txs = src.fetch_all(record_gas=False)
    net = Decimal(0)
    for t in txs:
        if t.received_asset == "USDC":
            net += t.received_amount
        if t.sent_asset == "USDC":
            net -= t.sent_amount
    assert {t.type for t in txs} == {TxType.DEPOSIT, TxType.WITHDRAW}
    assert net == 0


# ── accountData（残高変化）が第一情報源 ──────────────────────────────

def test_balance_change_wins_over_unresolved_token_transfer():
    """accountData がある応答では残高変化を正とし、tokenTransfers の帰属漏れに影響されない。

    Helius は宛先オーナーを解決できず toUserAccount を空で返すことがある。
    残高変化にはウォレットの ATA への +100 USDC が出ているので DEPOSIT になる。
    """
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [],
        "tokenTransfers": [{
            "fromUserAccount": OTHER, "toUserAccount": "",
            "fromTokenAccount": "", "toTokenAccount": USDC_ATA,
            "mint": USDC_MINT, "tokenAmount": 100,
        }],
        "accountData": [_balance_change(USDC_ATA, WALLET, USDC_MINT, 100_000_000, 6)],
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("100")


def test_no_balance_change_means_no_flow():
    """accountData 上でウォレットの残高が動いていなければ、tokenTransfers があっても資産移動なし。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 100)],
        "accountData": [
            _balance_change("SomeOtherTokenAcct1111111111111111111111111", OTHER, USDC_MINT, 100_000_000, 6),
        ],
    }])
    assert src.fetch_all(record_gas=False) == []


def test_token_transfers_used_when_account_data_absent():
    """accountData の無い応答では従来どおり tokenTransfers から集計する（後方互換）。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
        "type": "TRANSFER",
        "nativeTransfers": [],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 7)],
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].received_amount == Decimal("7")


def test_wsol_balance_change_is_folded_into_sol():
    """accountData 経由でも WSOL は独立資産にならず、SOL は nativeTransfers で追跡する。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP", "source": "JUPITER",
        "nativeTransfers": [_native(WALLET, WSOL_TOKEN_ACCT, 1_000_000_000)],
        "tokenTransfers": [],
        "accountData": [
            {"account": WALLET, "nativeBalanceChange": -1_000_005_000, "tokenBalanceChanges": []},
            _balance_change(WSOL_TOKEN_ACCT, WALLET, _WSOL_MINT, 1_000_000_000, 9),
            _balance_change(USDC_ATA, WALLET, USDC_MINT, 150_000_000, 6),
        ],
    }])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    assert trade[0].sent_asset == "SOL"
    assert trade[0].sent_amount == Decimal("1")
    assert trade[0].received_asset == "USDC"
    assert trade[0].received_amount == Decimal("150")
    assert "WSOL" not in {t.received_asset for t in txs} | {t.sent_asset for t in txs}


def test_mint_only_in_balance_changes_is_resolved():
    """accountData だけに現れるミントもシンボル解決の対象になる。"""
    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    src = FakeHelius(
        [{
            "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": OTHER,
            "type": "TRANSFER",
            "nativeTransfers": [],
            "tokenTransfers": [],
            "accountData": [
                _balance_change("BonkAta111111111111111111111111111111111111", WALLET, mint, 1_234_500_000, 5),
            ],
        }],
        symbols={mint: ("BONK", "Bonk")},
    )
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].received_asset == "BONK"
    assert txs[0].received_amount == Decimal("12345")


# ── 失敗した取引 ─────────────────────────────────────────────────────

def test_failed_tx_records_only_gas():
    """失敗した取引は資産移動を記録しない。fee payer ならガスだけ計上する。"""
    src = FakeHelius([{
        "signature": SIG1, "timestamp": _TS, "fee": 5000, "feePayer": WALLET,
        "type": "SWAP",
        "transactionError": {"InstructionError": [2, {"Custom": 6001}]},
        "nativeTransfers": [_native(WALLET, OTHER, 1_000_000_000)],
        "tokenTransfers": [_token(OTHER, WALLET, USDC_MINT, 180)],
    }])
    txs = src.fetch_all(record_gas=True)
    assert [t.type for t in txs] == [TxType.FEE]
    assert txs[0].fee_amount == Decimal("0.000005")


# ── 履歴 API の呼び出しパラメータ ────────────────────────────────────

def test_request_includes_token_account_activity(monkeypatch):
    """履歴取得はトークンアカウント宛ての取引（ブリッジ受取・ATA への入金）も含める。

    token-accounts=balanceChanged を毎ページ付ける。付けないとウォレット本体が
    account keys に含まれる取引しか返らず、CCTP 受取のような取引が丸ごと落ちる。
    """
    calls = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        return _Resp()

    monkeypatch.setattr(helius_module.httpx, "get", fake_get)
    monkeypatch.setattr(helius_module.time, "sleep", lambda _s: None)

    src = HeliusApiSource("sol", WALLET, "KEY")
    assert src._request(None) == []
    assert src._request(SIG1) == []

    (url1, p1), (url2, p2) = calls
    assert url1.endswith(f"/addresses/{WALLET}/transactions")
    assert p1["token-accounts"] == "balanceChanged"
    assert "before" not in p1
    assert p2["token-accounts"] == "balanceChanged"
    assert p2["before"] == SIG1
    assert p2["limit"] == 100
