"""EtherscanApiSource のテスト（HTTP はモック）

API JSON → CanonicalTx 変換と、ArbiscanCsvSource の分類ロジック再利用を検証する。
"""
from decimal import Decimal

from crypto_summary.sources.api.etherscan import EtherscanApiSource, CHAIN_IDS
from crypto_summary.core.models import TxType

WALLET = "0xaabbccdd00000000000000000000000000000002"
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


def test_native_asset_arbitrum():
    """Arbitrum (chainid=42161) のネイティブ通貨は ETH。"""
    src = EtherscanApiSource("arb", WALLET, "KEY", 42161)
    assert src.native_asset == "ETH"


def test_native_asset_polygon():
    """Polygon (chainid=137) のネイティブ通貨は MATIC。"""
    src = EtherscanApiSource("poly", WALLET, "KEY", 137)
    assert src.native_asset == "MATIC"


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


# ── ページング（無料枠 1,000 件/リクエスト上限への対応）────────────────

from crypto_summary.sources.api import etherscan as _es


class PagingEtherscan(EtherscanApiSource):
    """_request をモックし、ブロック窓ページングの挙動を検証する。"""

    def __init__(self, pages):
        super().__init__("arb", WALLET, "FAKEKEY", 42161)
        # pages: action -> list[list[record]]（呼び出し順に返すページ群）
        self._pages = {a: list(p) for a, p in pages.items()}
        self.requests: list[tuple[str, int]] = []

    def _request(self, action, startblock):
        self.requests.append((action, startblock))
        queue = self._pages.get(action, [])
        return queue.pop(0) if queue else []


def _rec(block, h):
    return {
        "hash": h, "blockNumber": str(block), "timeStamp": "1759062315",
        "from": OTHER, "to": WALLET, "value": "1000000000000000000",
        "gasUsed": "21000", "gasPrice": "100000000",
        "isError": "0", "functionName": "", "methodId": "0x",
    }


def test_pagination_advances_startblock(monkeypatch):
    """1 ページ満杯が返る限り startblock を進めて次ページを取得する。"""
    monkeypatch.setattr(_es, "_PAGE_SIZE", 2)  # テスト簡略化のためページサイズ 2
    monkeypatch.setattr(_es.time, "sleep", lambda *_: None)
    full = [_rec(10, "0x01"), _rec(20, "0x02")]  # 2 件 = ページ満杯
    tail = [_rec(30, "0x03")]                    # 1 件 = 最終ページ
    src = PagingEtherscan({"txlist": [full, tail]})
    records = src._get("txlist")
    assert [r["hash"] for r in records] == ["0x01", "0x02", "0x03"]
    # 2 回目のリクエストは直前ページ最終ブロック(20)から開始
    assert src.requests == [("txlist", 0), ("txlist", 20)]


def test_pagination_dedupes_boundary_block(monkeypatch):
    """境界ブロックのレコードが次ページと重複しても排除される。"""
    monkeypatch.setattr(_es, "_PAGE_SIZE", 2)
    monkeypatch.setattr(_es.time, "sleep", lambda *_: None)
    page1 = [_rec(10, "0x01"), _rec(20, "0x02")]
    # 次ページは startblock=20 から。ブロック20の 0x02 が再掲される。
    page2 = [_rec(20, "0x02"), _rec(30, "0x03")]
    src = PagingEtherscan({"txlist": [page1, page2]})
    records = src._get("txlist")
    assert [r["hash"] for r in records] == ["0x01", "0x02", "0x03"]


def test_pagination_stops_when_block_not_advancing(monkeypatch):
    """同一ブロックがページを満たし続けても無限ループしない。"""
    monkeypatch.setattr(_es, "_PAGE_SIZE", 2)
    monkeypatch.setattr(_es.time, "sleep", lambda *_: None)
    # 全件同じブロック10。startblock が進まないため打ち切られる。
    page = [_rec(10, "0x01"), _rec(10, "0x02")]
    src = PagingEtherscan({"txlist": [page, page, page]})
    records = src._get("txlist")
    assert len(records) == 2  # 重複排除で 2 件のみ
    # 1 回進めた後 startblock が同じブロックに留まるため 2 回で打ち切り（無限ループしない）
    assert len(src.requests) == 2


# ── タイムアウトとリトライ ────────────────────────────────────────────
#
# Etherscan は初回（キャッシュ未ヒット）の応答が重く、polygon で 26〜30 秒
# かかることを実測している。1 回のタイムアウトで諦めるとそのチェーンの取引が
# 丸ごと欠け、しかも他チェーンが取れていれば同期は成功として報告されるため
# 欠落に気づけない。

import httpx
import pytest


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


def _install_fake_get(monkeypatch, outcomes):
    """httpx.get を差し替える。outcomes は応答か送出する例外を順に返す。"""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"timeout": timeout, "action": (params or {}).get("action")})
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(_es.httpx, "get", fake_get)
    monkeypatch.setattr(_es.time, "sleep", lambda *_: None)
    return calls


_OK = {"status": "1", "message": "OK", "result": [{"hash": "0x01"}]}


def test_default_timeout_has_headroom_over_cold_response():
    """既定タイムアウトは実測の初回応答（26〜30秒）より十分長いこと。"""
    src = EtherscanApiSource("poly", WALLET, "KEY", 137)
    assert src.timeout >= 60.0


def test_retries_after_timeout(monkeypatch):
    """タイムアウトしても再試行し、次が通れば取得できる。"""
    calls = _install_fake_get(monkeypatch, [
        httpx.ReadTimeout("The read operation timed out"),
        _FakeResponse(_OK),
    ])
    src = EtherscanApiSource("poly", WALLET, "KEY", 137)
    assert src._request("txlist", 0) == [{"hash": "0x01"}]
    assert len(calls) == 2


def test_gives_up_after_max_attempts(monkeypatch):
    """繰り返しタイムアウトしたら試行回数と待ち時間が分かるエラーにする。"""
    calls = _install_fake_get(
        monkeypatch, [httpx.ReadTimeout("The read operation timed out")]
    )
    src = EtherscanApiSource("poly", WALLET, "KEY", 137)
    with pytest.raises(RuntimeError, match=r"3 回試行しても応答がありませんでした"):
        src._request("txlist", 0)
    assert len(calls) == _es._MAX_ATTEMPTS


def test_retries_server_error(monkeypatch):
    """5xx も時間を置けば直る可能性があるのでリトライする。"""
    calls = _install_fake_get(monkeypatch, [
        _FakeResponse({}, status_code=502),
        _FakeResponse(_OK),
    ])
    src = EtherscanApiSource("poly", WALLET, "KEY", 137)
    assert src._request("txlist", 0) == [{"hash": "0x01"}]
    assert len(calls) == 2


def test_does_not_retry_plan_limitation(monkeypatch):
    """無料プラン非対応など API が返したエラーは待っても変わらないので即座に落とす。

    呼び出し側が「失敗」ではなく「対象外」と区別できるよう専用の例外にする。
    """
    calls = _install_fake_get(monkeypatch, [_FakeResponse({
        "status": "0", "message": "NOTOK",
        "result": "Free API access is not supported for this chain.",
    })])
    src = EtherscanApiSource("base", WALLET, "KEY", 8453)
    with pytest.raises(_es.ChainNotSupportedError, match="Free API access is not supported"):
        src._request("txlist", 0)
    assert len(calls) == 1  # リトライで無駄打ちしない


def test_other_api_errors_are_plain_runtime_errors(monkeypatch):
    """プラン以外の API エラーは従来通り RuntimeError（対象外扱いにしない）。"""
    _install_fake_get(monkeypatch, [_FakeResponse({
        "status": "0", "message": "NOTOK", "result": "Invalid API Key",
    })])
    src = EtherscanApiSource("eth", WALLET, "KEY", 1)
    with pytest.raises(RuntimeError, match="Invalid API Key") as exc:
        src._request("txlist", 0)
    assert not isinstance(exc.value, _es.ChainNotSupportedError)


def test_no_transactions_is_not_retried(monkeypatch):
    """「取引なし」は正常応答。リトライせず空リストを返す。"""
    calls = _install_fake_get(monkeypatch, [_FakeResponse({
        "status": "0", "message": "No transactions found", "result": [],
    })])
    src = EtherscanApiSource("eth", WALLET, "KEY", 1)
    assert src._request("txlist", 0) == []
    assert len(calls) == 1
