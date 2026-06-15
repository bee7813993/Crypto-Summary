"""ArbiscanCsvSource のテスト"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.sources.evm.arbiscan import ArbiscanCsvSource
from crypto_summary.core.models import TxType

WALLET = "0x2712c054ad7c38d152aa068847346dffbfd56543"
OTHER = "0xaabbccdd00000000000000000000000000000001"
ZERO = "0x0000000000000000000000000000000000000000"
WBTC = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"
WETH_ADDR = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"

NORMAL_HEADER = (
    '"Transaction Hash","Blockno","UnixTimestamp","DateTime (UTC)",'
    '"From","To","ContractAddress","Value_IN(ETH)","Value_OUT(ETH)",'
    '"CurrentValue @ $1/ETH","TxnFee(ETH)","TxnFee(USD)",'
    '"Historical $Price/ETH","Status","ErrCode","Method"'
)
ERC20_HEADER = (
    '"Transaction Hash","Blockno","UnixTimestamp","DateTime (UTC)",'
    '"From","To","TokenValue","USDValueDayOfTx","ContractAddress",'
    '"TokenName","TokenSymbol"'
)
INTERNAL_HEADER = (
    '"Transaction Hash","Blockno","UnixTimestamp","DateTime (UTC)",'
    '"ParentTxFrom","ParentTxTo","ParentTxETH_Value",'
    '"From","TxTo","ContractAddress","Value_IN(ETH)","Value_OUT(ETH)",'
    '"CurrentValue @ $1/ETH","Historical $Price/ETH","Status","ErrCode","Type"'
)


def _normal(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "normal.csv"
    p.write_text(NORMAL_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _erc20(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "erc20.csv"
    p.write_text(ERC20_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _internal(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "internal.csv"
    p.write_text(INTERNAL_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _src():
    return ArbiscanCsvSource("arb", WALLET)


def _hash(n: int) -> str:
    return f"0x{'0' * 63}{n}"


# ── 1. ETH 受取 ──────────────────────────────────────────────────────

def test_eth_deposit(tmp_path):
    h = _hash(1)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","1.5","0","0","0.0001","0.1","4000","","","Transfer"')
    txs = _src().load_multi(n)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "ETH"
    assert tx.received_amount == Decimal("1.5")
    assert tx.label == "transfer_in"
    assert tx.tx_hash == h


# ── 2. ETH 送出 ──────────────────────────────────────────────────────

def test_eth_withdraw(tmp_path):
    h = _hash(2)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0.5","0","0.0001","0.1","4000","","","Transfer"')
    txs = _src().load_multi(n)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "ETH"
    assert tx.sent_amount == Decimal("0.5")


# ── 3a. 失敗扱いでもトークン転送は記録される（Arbiscanの仕様）───────

def test_reverted_tx_with_token_is_recorded(tmp_path):
    """ErrCode が付いていても ERC20 エクスポートに載る転送は実際に成立済み。"""
    h = _hash(3)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0","0","0.001","1","4000","Error(1)","execution reverted","Create Redemption"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","0.05","$200","{WBTC}","Wrapped BTC","WBTC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "WBTC"
    assert tx.sent_amount == Decimal("0.05")


# ── 3b. 失敗かつトークン移動なし → 何も記録しない ───────────────────

def test_reverted_tx_no_token_skipped(tmp_path):
    h = _hash(33)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0","0","0.001","1","4000","Error(1)","execution reverted","Deposit"')
    txs = _src().load_multi(n)
    assert txs == []


# ── 4. Approve はスキップ ────────────────────────────────────────────

def test_approve_skipped(tmp_path):
    h = _hash(4)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0","0","0.0001","0.1","4000","","","Approve"')
    txs = _src().load_multi(n)
    assert txs == []


# ── 5. ETH → WBTC スワップ ───────────────────────────────────────────

def test_eth_to_token_swap(tmp_path):
    h = _hash(5)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0.2","0","0.0001","0.1","4000","","","0x5f575529"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","0.00721708","$791","{WBTC}","Wrapped BTC","WBTC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "ETH"
    assert tx.sent_amount == Decimal("0.2")
    assert tx.received_asset == "WBTC"
    assert tx.received_amount == Decimal("0.00721708")
    assert tx.label == "swap"


# ── 6. WETH → WBTC スワップ（Token→Token）───────────────────────────

def test_token_to_token_swap(tmp_path):
    h = _hash(6)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0","0","0.0001","0.1","4000","","","0x5f575529"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","0.305183","$1227","{WETH_ADDR}","Wrapped Ether","WETH"',
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","0.011072","$1214","{WBTC}","Wrapped BTC","WBTC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "WETH"
    assert tx.sent_amount == Decimal("0.305183")
    assert tx.received_asset == "WBTC"
    assert tx.received_amount == Decimal("0.011072")


# ── 7. ETH Wrap（ETH → WETH）────────────────────────────────────────

def test_eth_wrap(tmp_path):
    h = _hash(7)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{WETH_ADDR}","","0","0.4499","0","0.0001","0.1","4000","","","Deposit"')
    # WETH mint: from = zero address
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{ZERO}","{WALLET}","0.4499","$1809","{WETH_ADDR}","Wrapped Ether","WETH"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "ETH"
    assert tx.received_asset == "WETH"
    assert tx.label == "eth_wrap"


# ── 8. LP 追加（ETH + WBTC を送出）──────────────────────────────────

def test_lp_add(tmp_path):
    h = _hash(8)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0.565","0","0.0001","0.1","4000","","","Multicall"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","0.00716","$786","{WBTC}","Wrapped BTC","WBTC"')
    txs = _src().load_multi(n, e)
    # ETH TRANSFER + WBTC TRANSFER
    assert len(txs) == 2
    labels = {tx.label for tx in txs}
    assert labels == {"lp_add"}
    types = {tx.type for tx in txs}
    assert types == {TxType.TRANSFER}
    assets = {tx.sent_asset for tx in txs}
    assert assets == {"ETH", "WBTC"}


# ── 9. LP 撤退（WBTC + 内部 ETH を受取）─────────────────────────────

def test_lp_remove_with_internal_eth(tmp_path):
    h = _hash(9)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 14:04:00","{WALLET}","{OTHER}","","0","0","0","0.0001","0.1","4000","","","Multicall"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 14:04:00","{OTHER}","{WALLET}","0.007877","$863","{WBTC}","Wrapped BTC","WBTC"')
    i = _internal(tmp_path,
        f'"{h}","1","1","2025-09-28 14:04:00","{OTHER}","{WALLET}","0","{OTHER}","{WALLET}","","0.545760","0","947","4143","0","","call",""')
    txs = _src().load_multi(n, e, i)
    assert len(txs) == 2
    labels = {tx.label for tx in txs}
    assert labels == {"lp_remove"}
    assets_recv = {tx.received_asset for tx in txs}
    assert assets_recv == {"ETH", "WBTC"}


# ── 10. LP 撤退（WBTC + WETH 両トークン）────────────────────────────

def test_lp_remove_two_tokens(tmp_path):
    h = _hash(10)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 14:38:00","{OTHER}","{WALLET}","","0","0","0","0.0001","0.1","4000","","","Multicall"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 14:38:00","{OTHER}","{WALLET}","0.00792","$868","{WBTC}","Wrapped BTC","WBTC"',
        f'"{h}","1","1","2025-09-28 14:38:00","{OTHER}","{WALLET}","0.095675","$384","{WETH_ADDR}","Wrapped Ether","WETH"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 2
    assert all(tx.label == "lp_remove" for tx in txs)
    assert all(tx.type == TxType.TRANSFER for tx in txs)


# ── 11. スパムトークンはスキップ ─────────────────────────────────────

def test_spam_tokens_skipped(tmp_path):
    h = _hash(11)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","0","0","0","0.0001","0.1","4000","","","Transfer"')
    e = _erc20(tmp_path,
        # スパムトークン: TokenValue はあるが TokenName がマスク済み
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","5000","N/A","0xspam","ERC-20 TOKEN*","ERC-20 TOKEN*"')
    txs = _src().load_multi(n, e)
    # ERC20 がスパムのみなので ETH 移動なし → スキップ
    assert txs == []


# ── 12. Claim To → REWARD ────────────────────────────────────────────

def test_claim_to_is_reward(tmp_path):
    h = _hash(12)
    n = _normal(tmp_path,
        f'"{h}","1","1","2026-01-12 11:53:44","{WALLET}","{OTHER}","","0","0","0","0.000004","0.006","3092","","","Claim To"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2026-01-12 11:53:44","{OTHER}","{WALLET}","0.102963","$9335","0xsolvbtc","Solv BTC","SolvBTC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "SOLVBTC"
    assert tx.label == "claim_to"


# ── 13. ガス代の記録（オプション）───────────────────────────────────

def test_record_gas(tmp_path):
    h = _hash(13)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","1.5","0","0","0.00029","0.5","4000","","","Transfer"')
    txs = _src().load_multi(n, record_gas=True)
    # DEPOSIT + FEE
    assert len(txs) == 2
    fee_tx = next(t for t in txs if t.type == TxType.FEE)
    assert fee_tx.fee_asset == "ETH"
    assert fee_tx.fee_amount == Decimal("0.00029")


# ── 14. Token → ETH スワップ（internal ETH 経由）────────────────────

def test_token_to_eth_swap(tmp_path):
    h = _hash(14)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0","0","0.0001","0.1","4000","","","0x5f575529"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","0.305183","$1227","{WETH_ADDR}","Wrapped Ether","WETH"')
    i = _internal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","0","{OTHER}","{WALLET}","","0.71","0","2464","4000","0","","call",""')
    txs = _src().load_multi(n, e, i)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.TRADE
    assert tx.sent_asset == "WETH"
    assert tx.received_asset == "ETH"
    assert tx.received_amount == Decimal("0.71")
