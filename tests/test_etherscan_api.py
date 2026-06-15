"""EtherscanApiSource のテスト（HTTP はモック）

API JSON → CanonicalTx 変換と、ArbiscanCsvSource の分類ロジック再利用を検証する。
"""
from decimal import Decimal

from crypto_summary.sources.api.etherscan import EtherscanApiSource, CHAIN_IDS
from crypto_summary.core.models import TxType

WALLET = "0x2712c054ad7c38d152aa068847346dffbfd56543"
OTHER = "0xaabbccdd00000000000000000000000000000001"
WBTC = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"
WETH = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"


class FakeEtherscan(EtherscanApiSource):
    """_get をモックして固定の JSON を返すテスト用サブクラス。"""

    def __init__(self, responses):
        super().__init__("arb", WALLET, "FAKEKEY", 42161)
        self._responses = responses

    def _get(self, action):
        return self._responses.get(action, [])


def test_chain_ids():
    assert CHAIN_IDS["ethereum"] == 1
    assert CHAIN_IDS["arbitrum"] == 42161
    assert CHAIN_IDS["polygon"] == 137


def test_eth_deposit_from_api():
    """value (wei) が ETH に変換され DEPOSIT になる。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x01", "timeStamp": "1759062315",
            "from": OTHER, "to": WALLET,
            "value": "1500000000000000000",  # 1.5 ETH
            "gasUsed": "21000", "gasPrice": "100000000",
            "isError": "0", "functionName": "", "methodId": "0x",
        }],
    })
    txs = src.fetch_all()
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "ETH"
    assert tx.received_amount == Decimal("1.5")


def test_no_gas_for_incoming_tx():
    """受取のみの取引はガスを払っていない（送信者でない）ので FEE を出さない。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x01", "timeStamp": "1759062315",
            "from": OTHER, "to": WALLET,
            "value": "1500000000000000000",
            "gasUsed": "21000", "gasPrice": "100000000",
            "isError": "0", "functionName": "", "methodId": "0x",
        }],
    })
    txs = src.fetch_all(record_gas=True)
    assert all(t.type != TxType.FEE for t in txs)


def test_gas_for_outgoing_tx():
    """送出取引はウォレットがガスを払うので FEE を出す。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x02", "timeStamp": "1759062315",
            "from": WALLET, "to": OTHER,
            "value": "500000000000000000",  # 0.5 ETH
            "gasUsed": "21000", "gasPrice": "1000000000",  # 0.000021 ETH
            "isError": "0", "functionName": "", "methodId": "0x",
        }],
    })
    txs = src.fetch_all(record_gas=True)
    fee = [t for t in txs if t.type == TxType.FEE]
    assert len(fee) == 1
    assert fee[0].fee_asset == "ETH"
    assert fee[0].fee_amount == Decimal("0.000021")


def test_erc20_decimals_applied():
    """tokenDecimal に従って raw value が人間可読量に変換される。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x03", "timeStamp": "1759063378",
            "from": WALLET, "to": OTHER, "value": "200000000000000000",  # 0.2 ETH out
            "gasUsed": "100000", "gasPrice": "100000000",
            "isError": "0", "functionName": "swap(uint256)", "methodId": "0x5f57",
        }],
        "tokentx": [{
            "hash": "0x03", "timeStamp": "1759063378",
            "from": OTHER, "to": WALLET,
            "value": "721708",  # 8 decimals → 0.00721708
            "tokenDecimal": "8", "contractAddress": WBTC,
            "tokenName": "Wrapped BTC", "tokenSymbol": "WBTC",
        }],
    })
    txs = src.fetch_all(record_gas=False)
    trade = [t for t in txs if t.type == TxType.TRADE]
    assert len(trade) == 1
    tx = trade[0]
    assert tx.sent_asset == "ETH"
    assert tx.sent_amount == Decimal("0.2")
    assert tx.received_asset == "WBTC"
    assert tx.received_amount == Decimal("0.00721708")


def test_claim_function_is_reward():
    """functionName に claim を含む単一トークン受取は REWARD。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x04", "timeStamp": "1768218824",
            "from": WALLET, "to": OTHER, "value": "0",
            "gasUsed": "50000", "gasPrice": "100000000",
            "isError": "0", "functionName": "claimTo(address)", "methodId": "0x",
        }],
        "tokentx": [{
            "hash": "0x04", "timeStamp": "1768218824",
            "from": OTHER, "to": WALLET,
            "value": "102963878870591118", "tokenDecimal": "18",
            "contractAddress": "0xsolv", "tokenName": "Solv BTC",
            "tokenSymbol": "SolvBTC",
        }],
    })
    txs = src.fetch_all(record_gas=False)
    reward = [t for t in txs if t.type == TxType.REWARD]
    assert len(reward) == 1
    assert reward[0].received_asset == "SOLVBTC"


def test_reverted_tx_with_token_recorded():
    """isError=1 でもトークン転送は実際に成立しているため記録する。"""
    src = FakeEtherscan({
        "txlist": [{
            "hash": "0x05", "timeStamp": "1768222032",
            "from": WALLET, "to": OTHER, "value": "0",
            "gasUsed": "50000", "gasPrice": "100000000",
            "isError": "1", "functionName": "createRedemption()", "methodId": "0x",
        }],
        "tokentx": [{
            "hash": "0x05", "timeStamp": "1768222032",
            "from": WALLET, "to": OTHER,
            "value": "102963878870591118", "tokenDecimal": "18",
            "contractAddress": "0xsolv", "tokenName": "Solv BTC",
            "tokenSymbol": "SolvBTC",
        }],
    })
    txs = src.fetch_all(record_gas=False)
    wd = [t for t in txs if t.type == TxType.WITHDRAW]
    assert len(wd) == 1
    assert wd[0].sent_asset == "SOLVBTC"


def test_empty_result():
    src = FakeEtherscan({})
    assert src.fetch_all() == []
