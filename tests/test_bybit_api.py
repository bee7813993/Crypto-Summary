"""BybitApiSource のテスト（HTTP はモック）。

V5 JSON → CanonicalTx 変換、シンボル分解、cursor ページングを検証する。
"""
from decimal import Decimal

from crypto_summary.core.models import TxType
from crypto_summary.sources.api.bybit import (
    BybitApiSource,
    deposit_to_tx,
    execution_to_tx,
    split_symbol,
    withdrawal_to_tx,
)


class FakeBybit(BybitApiSource):
    """_get をモックして固定の result(dict) を返すテスト用サブクラス。

    pages: endpoint -> list[result_dict]（呼び出し順に返す）
    """

    def __init__(self, pages):
        super().__init__("bybit", "KEY", "SECRET")
        self._pages = {e: list(p) for e, p in pages.items()}
        self.calls: list[tuple[str, dict]] = []

    def _get(self, endpoint, params):
        self.calls.append((endpoint, dict(params)))
        queue = self._pages.get(endpoint, [])
        return queue.pop(0) if queue else {"list": [], "nextPageCursor": ""}


def test_split_symbol():
    assert split_symbol("BTCUSDT") == ("BTC", "USDT")
    assert split_symbol("ETHBTC") == ("ETH", "BTC")
    assert split_symbol("SOLUSDC") == ("SOL", "USDC")
    # 判別不能
    assert split_symbol("FOOBAR") == ("FOOBAR", "")


def test_execution_buy():
    item = {
        "symbol": "BTCUSDT", "side": "Buy", "execId": "e1",
        "execQty": "0.01", "execPrice": "60000", "execValue": "600",
        "execFee": "0.00001", "feeCurrency": "BTC",
        "execTime": "1710000000000",
    }
    tx = execution_to_tx(item, "bybit")
    assert tx.type == TxType.TRADE
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.01")
    assert tx.sent_asset == "USDT"
    assert tx.sent_amount == Decimal("600")
    assert tx.fee_asset == "BTC"
    assert tx.fee_amount == Decimal("0.00001")


def test_execution_sell():
    item = {
        "symbol": "ETHUSDT", "side": "Sell", "execId": "e2",
        "execQty": "2", "execPrice": "3000", "execValue": "6000",
        "execFee": "6", "feeCurrency": "USDT",
        "execTime": "1710000000000",
    }
    tx = execution_to_tx(item, "bybit")
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("6000")
    assert tx.sent_asset == "ETH"
    assert tx.sent_amount == Decimal("2")
    assert tx.fee_asset == "USDT"


def test_execution_unknown_quote_skipped():
    item = {"symbol": "FOOBAR", "side": "Buy", "execId": "e3",
            "execQty": "1", "execPrice": "1", "execTime": "1710000000000"}
    assert execution_to_tx(item, "bybit") is None


def test_deposit():
    item = {"coin": "USDT", "amount": "500", "txID": "0xabc",
            "successAt": "1710000000000"}
    tx = deposit_to_tx(item, "bybit")
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("500")
    assert tx.tx_hash == "0xabc"


def test_withdrawal_with_fee():
    item = {"coin": "ETH", "amount": "1.5", "withdrawFee": "0.001",
            "txID": "0xdef", "withdrawId": "w1", "updateTime": "1710000000000"}
    tx = withdrawal_to_tx(item, "bybit")
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "ETH"
    assert tx.sent_amount == Decimal("1.5")
    assert tx.fee_asset == "ETH"
    assert tx.fee_amount == Decimal("0.001")


def test_pagination_follows_cursor():
    src = FakeBybit({
        "/v5/execution/list": [
            {"list": [
                {"symbol": "BTCUSDT", "side": "Buy", "execId": "a",
                 "execQty": "0.01", "execPrice": "60000", "execTime": "1710000000000"},
            ], "nextPageCursor": "CUR1"},
            {"list": [
                {"symbol": "BTCUSDT", "side": "Sell", "execId": "b",
                 "execQty": "0.01", "execPrice": "61000", "execTime": "1710000100000"},
            ], "nextPageCursor": ""},
        ],
    })
    rows = src.fetch_executions()
    assert [r["execId"] for r in rows] == ["a", "b"]
    # 2ページ目は cursor=CUR1 で取得
    assert src.calls[1][1].get("cursor") == "CUR1"


def test_fetch_all_combines_sources():
    src = FakeBybit({
        "/v5/execution/list": [{"list": [
            {"symbol": "BTCUSDT", "side": "Buy", "execId": "a",
             "execQty": "0.01", "execPrice": "60000", "execTime": "1710000000000"},
        ], "nextPageCursor": ""}],
        "/v5/asset/deposit/query-record": [{"list": [
            {"coin": "USDT", "amount": "500", "txID": "0xabc", "successAt": "1710000000000"},
        ], "nextPageCursor": ""}],
        "/v5/asset/withdraw/query-record": [{"list": [
            {"coin": "ETH", "amount": "1", "withdrawFee": "0.001",
             "withdrawId": "w1", "updateTime": "1710000200000"},
        ], "nextPageCursor": ""}],
    })
    txs = src.fetch_all()
    types = sorted(t.type.value for t in txs)
    assert types == ["deposit", "trade", "withdraw"]


def test_fetch_all_empty():
    src = FakeBybit({})
    assert src.fetch_all() == []
