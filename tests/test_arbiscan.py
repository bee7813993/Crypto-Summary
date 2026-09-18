"""ArbiscanCsvSource のテスト"""
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.sources.evm.arbiscan import ArbiscanCsvSource, _is_bridge_out_method
from crypto_summary.core.models import TxType

WALLET = "0xaabbccdd00000000000000000000000000000002"
OTHER = "0xaabbccdd00000000000000000000000000000001"
ZERO = "0x0000000000000000000000000000000000000000"
WBTC = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"
WETH_ADDR = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"
USDC_ADDR = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"

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


# ── 8b. ブリッジ送信（トークン + ETH リレイヤー手数料を送出、受取なし）──
#
# CCTP depositForBurn / Portal Bridge transferTokensWithRelay は LP 追加と同じ
# 「複数資産送出・受取なし」の形になるため、メソッド名で判別して bridge_out にする。

def test_cctp_deposit_for_burn_is_bridge_out(tmp_path):
    """CSV の Method 列 "Deposit For Burn" → bridge_out（TxType・行構成は lp_add と同じ）。"""
    h = _hash(80)
    n = _normal(tmp_path,
        f'"{h}","1","1","2026-09-09 10:00:00","{WALLET}","{OTHER}","","0","0.0003","0","0.00001","0.04","4000","","","Deposit For Burn"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2026-09-09 10:00:00","{WALLET}","{OTHER}","212.851555","$212","{USDC_ADDR}","USD Coin","USDC"')
    txs = _src().load_multi(n, e)
    # ETH TRANSFER + USDC TRANSFER（残高計算は lp_add のときと変わらない）
    assert len(txs) == 2
    assert {tx.label for tx in txs} == {"bridge_out"}
    assert {tx.type for tx in txs} == {TxType.TRANSFER}
    assert {tx.sent_asset for tx in txs} == {"ETH", "USDC"}
    usdc = next(tx for tx in txs if tx.sent_asset == "USDC")
    assert usdc.sent_amount == Decimal("212.851555")
    assert all(tx.tx_hash == h for tx in txs)


def test_portal_bridge_transfer_with_relay_is_bridge_out(tmp_path):
    """Portal Bridge (Wormhole) の "Transfer Tokens With Relay" も bridge_out。"""
    h = _hash(81)
    n = _normal(tmp_path,
        f'"{h}","1","1","2026-09-17 09:00:00","{WALLET}","{OTHER}","","0","0.0005","0","0.00001","0.04","4000","","","Transfer Tokens With Relay"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2026-09-17 09:00:00","{WALLET}","{OTHER}","505.247212","$505","{USDC_ADDR}","USD Coin","USDC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 2
    assert {tx.label for tx in txs} == {"bridge_out"}
    assert {tx.type for tx in txs} == {TxType.TRANSFER}


def test_add_liquidity_stays_lp_add(tmp_path):
    """通常の Add Liquidity（ETH + トークン送出）は引き続き lp_add。"""
    h = _hash(82)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","0.565","0","0.0001","0.1","4000","","","Add Liquidity ETH"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","0.00716","$786","{WBTC}","Wrapped BTC","WBTC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 2
    assert {tx.label for tx in txs} == {"lp_add"}
    assert {tx.type for tx in txs} == {TxType.TRANSFER}


@pytest.mark.parametrize("method", [
    "depositForBurn",                                  # API: _method_name() 後
    "depositForBurn(uint256,uint32,bytes32,address)",  # API: 生の functionName
    "Deposit For Burn",                                # CSV: Arbiscan の Method 列
    "Deposit For Burn With Caller",
    "transferTokens",
    "transferTokensWithRelay",
    "Transfer Tokens With Payload",
])
def test_bridge_out_method_forms(method):
    """API / CSV どちらの表記でも既知ブリッジメソッドとして一致する。"""
    assert _is_bridge_out_method(method)


@pytest.mark.parametrize("method", [
    "", "Multicall", "addLiquidity", "Add Liquidity ETH", "Transfer",
    "0x12345678",  # 未知のセレクタ（既知のものは BRIDGE_OUT_SELECTORS で一致させる）
])
def test_non_bridge_methods_not_matched(method):
    assert not _is_bridge_out_method(method)


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
    # 送出取引（From=WALLET）のみガスを払う
    h = _hash(13)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{OTHER}","","0","1.5","0","0.00029","0.5","4000","","","Transfer"')
    txs = _src().load_multi(n, record_gas=True)
    # WITHDRAW + FEE
    assert len(txs) == 2
    fee_tx = next(t for t in txs if t.type == TxType.FEE)
    assert fee_tx.fee_asset == "ETH"
    assert fee_tx.fee_amount == Decimal("0.00029")


def test_no_gas_for_incoming(tmp_path):
    """受取取引はウォレットがガスを払わないので FEE を出さない。"""
    h = _hash(130)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","1.5","0","0","0.00029","0.5","4000","","","Transfer"')
    txs = _src().load_multi(n, record_gas=True)
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT


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


# ── 15. フィッシング系トークン名・シンボルはスキップ ─────────────────

def test_phishing_in_token_name_skipped(tmp_path):
    """TokenName にフィッシングパターンを含む場合はスパム扱い。"""
    h = _hash(15)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","0","0","0","0","0","4000","","","Transfer"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","5000","N/A","0xspam","ARB | T.ME/S/CLAIMARB | GET REWARD","SPAM"')
    txs = _src().load_multi(n, e)
    assert txs == []


def test_phishing_in_token_symbol_skipped(tmp_path):
    """API が TokenSymbol にフィッシング文字列を埋め込む実例（T.ME/）をスキップ。"""
    h = _hash(16)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","0","0","0","0","0","4000","","","Transfer"')
    # 実際の API 応答: tokenName="ARB", tokenSymbol="ARB | T.ME/S/CLAIMARB | GET REWARD"
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","5000","N/A","0xspam","ARB","ARB | T.ME/S/CLAIMARB | GET REWARD"')
    txs = _src().load_multi(n, e)
    assert txs == []


def test_phishing_url_in_name_skipped(tmp_path):
    """URL パターンを含むトークン名もスパム扱い。"""
    h = _hash(17)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","","0","0","0","0","0","4000","","","Transfer"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{OTHER}","{WALLET}","10000","N/A","0xspam2","Visit https://scam.io/claim to get free airdrop","SCAM"')
    txs = _src().load_multi(n, e)
    assert txs == []


# ── 16. ETH シンボルの ERC20 はブリッジアーティファクトとして除外 ─────

def test_erc20_named_eth_is_filtered_as_bridge_artifact(tmp_path):
    """ERC20 シンボルが 'ETH' かつコントラクトアドレスありの場合はブリッジ内部
    トークンとして除外し、ETH ネイティブ残高を汚染しない。"""
    h = _hash(18)
    # 通常 TX では ETH 移動なし（コントラクト呼び出しのみ）
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-29 11:57:00","{WALLET}","{OTHER}","","0","0","0","0.001","1","4000","","","bridge"')
    # ERC20: bridge token with symbol "ETH" sent from wallet
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-29 11:57:00","{WALLET}","{OTHER}","0.75066","N/A","0xbridgeaddr","Bridged ETH","ETH"')
    txs = _src().load_multi(n, e)
    # Bridge artifact は除外 → ETH 移動なし → 資産移動なし → スキップ
    assert txs == []


def test_weth_from_zero_not_filtered(tmp_path):
    """ゼロアドレスから mint された WETH（ETH Wrap）は除外しない。"""
    h = _hash(19)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{WALLET}","{WETH_ADDR}","","0","0.4499","0","0.0001","0.1","4000","","","Deposit"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-09-28 12:00:00","{ZERO}","{WALLET}","0.4499","$1809","{WETH_ADDR}","Wrapped Ether","WETH"')
    txs = _src().load_multi(n, e)
    # ETH Wrap は TRADE として記録される
    assert len(txs) == 1
    assert txs[0].type == TxType.TRADE


# ── 17. Unicode ホモグラフ攻撃トークンはスキップ ─────────────────────

def test_unicode_homograph_symbol_skipped(tmp_path):
    """Cyrillic 等の非 ASCII 文字で USDC/USDT を偽装するトークンをスキップ。"""
    h = _hash(20)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-11-30 07:28:00","{WALLET}","{OTHER}","","0","0","0","0.001","0.01","1","","","Transfer"')
    # UЅDС: 'Ѕ'(U+0455 Cyrillic) と 'С'(U+0421 Cyrillic) で USDC を偽装
    e = _erc20(tmp_path,
        f'"{h}","1","1","2025-11-30 07:28:00","{WALLET}","{OTHER}","3148.87","N/A","0xfake","USD Coin (Polygon)","UЅdС"')
    txs = _src().load_multi(n, e)
    assert txs == []


# ── 18. native_asset 指定（Polygon = MATIC）────────────────────────────

def test_native_asset_polygon_deposit(tmp_path):
    """native_asset="MATIC" を指定すると MATIC 受取として分類される。"""
    h = _hash(21)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-11-30 01:39:00","{OTHER}","{WALLET}","","1.8","0","0","0","0","1","","","Transfer"')
    txs = ArbiscanCsvSource("poly", WALLET, "MATIC").load_multi(n)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "MATIC"


def test_native_asset_polygon_fee(tmp_path):
    """Polygon の gas fee は MATIC として記録される。"""
    h = _hash(22)
    n = _normal(tmp_path,
        f'"{h}","1","1","2025-11-30 01:40:00","{WALLET}","{OTHER}","","0","1.5","0","0.035","1","1","","","Transfer"')
    txs = ArbiscanCsvSource("poly", WALLET, "MATIC").load_multi(n, record_gas=True)
    fee = next(t for t in txs if t.type == TxType.FEE)
    assert fee.fee_asset == "MATIC"
    withdraw = next(t for t in txs if t.type == TxType.WITHDRAW)
    assert withdraw.sent_asset == "MATIC"


def test_bridge_out_selector_in_method_column(tmp_path):
    """CSV の Method 列がセレクタ表記（"0xd01cbba9"）でも bridge_out。

    エクスプローラがメソッド名を解決できないと Method 列には methodId が入る。
    """
    h = _hash(83)
    n = _normal(tmp_path,
        f'"{h}","1","1","2026-09-17 02:18:47","{WALLET}","{OTHER}","","0","0.00009","0","0.00001","0.04","4000","","","0xd01cbba9"')
    e = _erc20(tmp_path,
        f'"{h}","1","1","2026-09-17 02:18:47","{WALLET}","{OTHER}","505.247212","$505","{USDC_ADDR}","USD Coin","USDC"')
    txs = _src().load_multi(n, e)
    assert len(txs) == 2
    assert {tx.label for tx in txs} == {"bridge_out"}
    assert {tx.type for tx in txs} == {TxType.TRANSFER}


def test_is_bridge_out_method_accepts_selectors():
    """既知セレクタは大文字小文字を問わず一致し、未知セレクタ・空文字は一致しない。"""
    assert _is_bridge_out_method("0xd01cbba9")
    assert _is_bridge_out_method("0xD01CBBA9")
    assert _is_bridge_out_method("0x6fd3504e")  # CCTP v1 depositForBurn
    assert _is_bridge_out_method("0x1019d654")  # Portal transferTokensWithRelay
    assert not _is_bridge_out_method("0x12345678")
    assert not _is_bridge_out_method("")
