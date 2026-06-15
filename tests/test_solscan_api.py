"""SolscanApiSource のテスト（HTTP はモック）

Solana ウォレット取引の分類ロジックを検証する。
"""
from decimal import Decimal

import pytest

from crypto_summary.sources.solana.solscan import SolscanApiSource
from crypto_summary.core.models import TxType

WALLET = "So1ExampleWalletAddressForTesting111111111"
OTHER  = "OtherWalletAddress111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SIG1 = "5YE4abcdefghijklmnopqrstuvwxyz1234567890AB"
SIG2 = "6ZF5abcdefghijklmnopqrstuvwxyz1234567890CD"

_TS = 1733788800  # 2024-12-10 00:00:00 UTC


class FakeSolscan(SolscanApiSource):
    """_request をモックして固定データを返すテスト用サブクラス。"""

    def __init__(self, data: list[dict]):
        super().__init__("sol", WALLET, "FAKEKEY")
        self._data = data

    def _request(self, page: int) -> list[dict]:
        return self._data if page == 1 else []


# ── SOL 受取 ─────────────────────────────────────────────────────────

def test_sol_deposit():
    """SOL を受け取った場合は DEPOSIT になる。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 5000,
        "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
        "from_address": OTHER, "to_address": WALLET,
        "token_address": "", "token_decimals": 9, "amount": 1_500_000_000,
        "flow": "in",
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "SOL"
    assert tx.received_amount == Decimal("1.5")


# ── SOL 送出 ─────────────────────────────────────────────────────────

def test_sol_withdraw():
    """SOL を送った場合は WITHDRAW + FEE になる。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 5000,
        "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
        "from_address": WALLET, "to_address": OTHER,
        "token_address": "", "token_decimals": 9, "amount": 500_000_000,
        "flow": "out",
    }])
    txs = src.fetch_all(record_gas=True)
    wd = [t for t in txs if t.type == TxType.WITHDRAW]
    fee = [t for t in txs if t.type == TxType.FEE]
    assert len(wd) == 1
    assert wd[0].sent_asset == "SOL"
    assert wd[0].sent_amount == Decimal("0.5")
    assert len(fee) == 1
    assert fee[0].fee_asset == "SOL"
    assert fee[0].fee_amount == Decimal("0.000005")  # 5000 lamports


# ── ガス: 受取側は払わない ──────────────────────────────────────────

def test_no_gas_for_receiver():
    """受取のみの取引はガス代を計上しない（from != wallet）。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 5000,
        "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
        "from_address": OTHER, "to_address": WALLET,
        "token_address": "", "token_decimals": 9, "amount": 1_000_000_000,
        "flow": "in",
    }])
    txs = src.fetch_all(record_gas=True)
    assert all(t.type != TxType.FEE for t in txs)


# ── SOL → USDC スワップ ──────────────────────────────────────────────

def test_sol_to_token_swap():
    """SOL 送出 + USDC 受取は TRADE（swap）になる。"""
    src = FakeSolscan([
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
            "from_address": WALLET, "to_address": OTHER,
            "token_address": "", "token_decimals": 9, "amount": 1_000_000_000,
            "flow": "out",
        },
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SPL_TRANSFER",
            "from_address": OTHER, "to_address": WALLET,
            "token_address": USDC_MINT, "token_decimals": 6, "amount": 200_000_000,
            "flow": "in",
            "token_symbol": "USDC",
        },
    ])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    tx = trade[0]
    assert tx.sent_asset == "SOL"
    assert tx.sent_amount == Decimal("1")
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("200")


# ── USDC → SOL スワップ ──────────────────────────────────────────────

def test_token_to_sol_swap():
    """USDC 送出 + SOL 受取は TRADE になる。"""
    src = FakeSolscan([
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SPL_TRANSFER",
            "from_address": WALLET, "to_address": OTHER,
            "token_address": USDC_MINT, "token_decimals": 6, "amount": 100_000_000,
            "flow": "out", "token_symbol": "USDC",
        },
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
            "from_address": OTHER, "to_address": WALLET,
            "token_address": "", "token_decimals": 9, "amount": 800_000_000,
            "flow": "in",
        },
    ])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    assert trade[0].sent_asset == "USDC"
    assert trade[0].received_asset == "SOL"


# ── Token → Token スワップ ──────────────────────────────────────────

def test_token_to_token_swap():
    """USDC → USDT は TRADE になる。"""
    src = FakeSolscan([
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SPL_TRANSFER",
            "from_address": WALLET, "to_address": OTHER,
            "token_address": USDC_MINT, "token_decimals": 6, "amount": 100_000_000,
            "flow": "out", "token_symbol": "USDC",
        },
        {
            "trans_id": SIG1, "block_time": _TS, "fee": 5000,
            "activity_type": "ACTIVITY_SPL_TRANSFER",
            "from_address": OTHER, "to_address": WALLET,
            "token_address": USDT_MINT, "token_decimals": 6, "amount": 99_900_000,
            "flow": "in", "token_symbol": "USDT",
        },
    ])
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    assert trade[0].sent_asset == "USDC"
    assert trade[0].received_asset == "USDT"


# ── 単一トークン受取 ─────────────────────────────────────────────────

def test_single_token_deposit():
    """SPL トークンのみ受取は DEPOSIT になる。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 5000,
        "activity_type": "ACTIVITY_SPL_TRANSFER",
        "from_address": OTHER, "to_address": WALLET,
        "token_address": USDC_MINT, "token_decimals": 6, "amount": 5_000_000,
        "flow": "in", "token_symbol": "USDC",
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_asset == "USDC"
    assert txs[0].received_amount == Decimal("5")


# ── Unicode ホモグラフスパムはスキップ ───────────────────────────────

def test_unicode_homograph_skipped():
    """非 ASCII 文字を含むシンボルはスパム扱いでスキップ。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 0,
        "activity_type": "ACTIVITY_SPL_TRANSFER",
        "from_address": OTHER, "to_address": WALLET,
        "token_address": "0xfake", "token_decimals": 6, "amount": 5_000_000_000,
        "flow": "in",
        "token_symbol": "UЅdС",  # Cyrillic lookalike
        "token_name": "USD Coin",
    }])
    txs = src.fetch_all(record_gas=False)
    assert txs == []


# ── flow フィールドがない場合のフォールバック ──────────────────────

def test_flow_derived_from_addresses():
    """flow フィールドがなくても from/to アドレスから方向を導出する。"""
    src = FakeSolscan([{
        "trans_id": SIG1, "block_time": _TS, "fee": 5000,
        "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
        "from_address": OTHER, "to_address": WALLET,
        "token_address": "", "token_decimals": 9, "amount": 2_000_000_000,
        # flow フィールドなし
    }])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_amount == Decimal("2")


# ── ページング: 2ページ目が空なら1ページで終了 ─────────────────────

def test_single_page_stops():
    """1ページが page_size 未満のとき 2ページ目を取得しない。"""
    calls = []

    class CountingSolscan(SolscanApiSource):
        def __init__(self):
            super().__init__("sol", WALLET, "KEY")

        def _request(self, page: int) -> list[dict]:
            calls.append(page)
            if page == 1:
                return [{
                    "trans_id": SIG1, "block_time": _TS, "fee": 0,
                    "activity_type": "ACTIVITY_SYSTEM_TRANSFER",
                    "from_address": OTHER, "to_address": WALLET,
                    "token_address": "", "token_decimals": 9, "amount": 1_000_000_000,
                    "flow": "in",
                }]
            return []

    src = CountingSolscan()
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    assert calls == [1]  # 2ページ目は取得しない


# ── 空レスポンス ──────────────────────────────────────────────────────

def test_empty_result():
    src = FakeSolscan([])
    assert src.fetch_all() == []
