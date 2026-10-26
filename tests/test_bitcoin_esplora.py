"""EsploraSource のテスト（HTTP はモック）

Esplora JSON → CanonicalTx の UTXO 畳み込みと、xpub の gap limit 走査を検証する。
"""
from decimal import Decimal

import pytest

from crypto_summary.core.models import TxType
from crypto_summary.sources.bitcoin import esplora as es
from crypto_summary.sources.bitcoin.esplora import EsploraSource
from crypto_summary.sources.bitcoin.keys import P2WPKH, parse_extended_pubkey

ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868"
        "RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")
# ZPUB から導出される実アドレス（BIP84 テストベクタ）
RECV0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
RECV1 = "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"
CHANGE0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
MINE = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
OTHER = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
OTHER2 = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def _tx(txid, vin, vout, fee=1000, height=800_000, time=1_700_000_000, confirmed=True):
    """(address, sats) のリストから Esplora 形式の tx を組み立てる。"""
    return {
        "txid": txid,
        "vin": [
            {"txid": "p" * 64, "vout": 0, "is_coinbase": addr is None,
             "prevout": None if addr is None else {"scriptpubkey_address": addr, "value": sats}}
            for addr, sats in vin
        ],
        "vout": [{"scriptpubkey_address": addr, "value": sats} for addr, sats in vout],
        "fee": fee,
        "status": {"confirmed": confirmed, "block_height": height, "block_time": time},
    }


class FakeEsplora(EsploraSource):
    """_get をモックして固定 JSON を返す。requests に呼び出しパスを記録する。"""

    def __init__(self, wallet, txs_by_address=None, counts=None, source_id="btc"):
        super().__init__(source_id, wallet, base_url="https://esplora.test/api")
        self._txs = txs_by_address or {}
        self._counts = counts
        self.requests: list[str] = []

    def _get(self, path):
        self.requests.append(path)
        parts = path.strip("/").split("/")
        address = parts[1]
        if len(parts) == 2:  # /address/{addr}
            if self._counts is not None:
                n = self._counts.get(address, 0)
            else:
                n = len(self._txs.get(address, []))
            return {"chain_stats": {"tx_count": n}, "mempool_stats": {"tx_count": 0}}
        # /address/{addr}/txs/chain[/{last_seen}]
        txs = self._txs.get(address, [])
        if len(parts) == 5:
            idx = [t["txid"] for t in txs].index(parts[4])
            txs = txs[idx + 1:]
        return txs[:es._PAGE_SIZE]


def test_receive_is_deposit():
    src = FakeEsplora(MINE, {MINE: [_tx("t1", [(OTHER, 200_000)], [(MINE, 150_000), (OTHER, 49_000)])]})
    txs = src.fetch_all()
    assert len(txs) == 1
    assert txs[0].type == TxType.DEPOSIT
    assert txs[0].received_asset == "BTC"
    assert txs[0].received_amount == Decimal("0.0015")
    assert txs[0].tx_hash == "t1"
    assert txs[0].label == "receive"
    assert src.warnings == []


def test_coinbase_is_reward():
    src = FakeEsplora(MINE, {MINE: [_tx("cb", [(None, 0)], [(MINE, 625_000_000)], fee=0)]})
    txs = src.fetch_all()
    assert [t.type for t in txs] == [TxType.REWARD]
    assert txs[0].received_amount == Decimal("6.25")


def test_send_with_change_records_withdraw_and_fee():
    """全入力が自分 → 相手に渡った額が WITHDRAW、手数料は別の FEE 行。"""
    src = FakeEsplora(ZPUB, {
        RECV0: [_tx("s1", [(RECV0, 1_000_000)], [(OTHER, 600_000), (CHANGE0, 399_000)], fee=1000)],
        CHANGE0: [_tx("s1", [(RECV0, 1_000_000)], [(OTHER, 600_000), (CHANGE0, 399_000)], fee=1000)],
    })
    txs = src.fetch_all()
    by_type = {t.type: t for t in txs}
    assert set(by_type) == {TxType.WITHDRAW, TxType.FEE}
    assert by_type[TxType.WITHDRAW].sent_amount == Decimal("0.006")
    assert by_type[TxType.WITHDRAW].label == "send"
    assert by_type[TxType.FEE].fee_asset == "BTC"
    assert by_type[TxType.FEE].fee_amount == Decimal("0.00001")
    # 残高: 0.01 − 0.006 − 0.00001 = 0.00399 = おつり
    assert Decimal("0.01") - Decimal("0.006") - Decimal("0.00001") == Decimal("0.00399")
    # 同じ txid が 2 アドレスから返っても 1 回しか処理しない
    assert len(txs) == 2
    assert src.warnings == []


def test_send_without_gas_flag_skips_fee():
    src = FakeEsplora(MINE, {MINE: [_tx("s1", [(MINE, 100_000)], [(OTHER, 90_000), (MINE, 9_000)], fee=1000)]})
    txs = src.fetch_all(record_gas=False)
    assert [t.type for t in txs] == [TxType.WITHDRAW]


def test_self_transfer_records_only_fee():
    """UTXO 統合など全出力が自分 → 手数料しか減らない。"""
    src = FakeEsplora(MINE, {MINE: [_tx("c1", [(MINE, 50_000), (MINE, 50_000)], [(MINE, 99_000)], fee=1000)]})
    txs = src.fetch_all()
    assert [t.type for t in txs] == [TxType.FEE]
    assert txs[0].fee_amount == Decimal("0.00001")


def test_mixed_inputs_coinjoin_records_net_only():
    """他人の入力が混ざる（CoinJoin）→ 手数料の帰属が決められないので正味だけ。"""
    tx = _tx("cj", [(MINE, 100_000), (OTHER, 100_000)],
             [(OTHER2, 99_000), (MINE, 99_000)], fee=2000)
    src = FakeEsplora(MINE, {MINE: [tx]})
    txs = src.fetch_all()
    assert len(txs) == 1
    assert txs[0].type == TxType.WITHDRAW
    assert txs[0].sent_amount == Decimal("0.00001")
    assert txs[0].label == "coinjoin"


def test_mixed_inputs_net_zero_records_nothing():
    tx = _tx("cj0", [(MINE, 100_000), (OTHER, 100_000)],
             [(OTHER2, 100_000), (MINE, 100_000)], fee=0)
    assert FakeEsplora(MINE, {MINE: [tx]}).fetch_all() == []


def test_single_address_change_leak_warns():
    """単一アドレス登録で、おつりが戻らない送金 → xpub 登録を促す警告。"""
    tx = _tx("s", [(MINE, 1_000_000)], [(OTHER, 600_000), (OTHER2, 399_000)], fee=1000)
    src = FakeEsplora(MINE, {MINE: [tx]})
    txs = src.fetch_all()
    # おつり（別アドレス）も出金として記録される（追えないので仕方ない）
    assert {t.type for t in txs} == {TxType.WITHDRAW, TxType.FEE}
    assert next(t for t in txs if t.type == TxType.WITHDRAW).sent_amount == Decimal("0.00999")
    assert len(src.warnings) == 1
    assert "xpub" in src.warnings[0]


def test_single_address_full_spend_to_one_output_does_not_warn():
    """出力が 1 つだけ（全額送金）はおつり漏れではない。"""
    tx = _tx("s", [(MINE, 1_000_000)], [(OTHER, 999_000)], fee=1000)
    src = FakeEsplora(MINE, {MINE: [tx]})
    src.fetch_all()
    assert src.warnings == []


def test_unconfirmed_excluded():
    src = FakeEsplora(MINE, {MINE: [
        _tx("ok", [(OTHER, 10_000)], [(MINE, 10_000)]),
        _tx("pending", [(OTHER, 10_000)], [(MINE, 10_000)], confirmed=False),
    ]})
    assert [t.tx_hash for t in src.fetch_all()] == ["ok"]


def test_sorted_by_block_height():
    src = FakeEsplora(MINE, {MINE: [
        _tx("new", [(OTHER, 1)], [(MINE, 1)], height=900_000, time=2_000_000_000),
        _tx("old", [(OTHER, 1)], [(MINE, 1)], height=100_000, time=1_300_000_000),
    ]})
    txs = src.fetch_all()
    assert [t.tx_hash for t in txs] == ["old", "new"]
    assert txs[0].timestamp.year == 2011


def test_pagination_uses_last_seen_txid():
    """25 件ずつ、最後の txid を次ページに渡して取り切る。"""
    page = [_tx(f"t{i:03d}", [(OTHER, 1000)], [(MINE, 1000)]) for i in range(60)]
    src = FakeEsplora(MINE, {MINE: page})
    txs = src.fetch_all()
    assert len(txs) == 60
    chain_calls = [r for r in src.requests if "/txs/chain" in r]
    assert chain_calls == [
        f"/address/{MINE}/txs/chain",
        f"/address/{MINE}/txs/chain/t024",
        f"/address/{MINE}/txs/chain/t049",
    ]


def test_single_address_does_not_probe_tx_count():
    src = FakeEsplora(MINE, {MINE: []})
    assert src.fetch_all() == []
    assert src.requests == [f"/address/{MINE}/txs/chain"]
    assert src.scanned_addresses == 1


def test_xpub_gap_limit_scan_and_change_chain():
    """受取 0・1 とおつり 0 に取引 → gap limit 20 で走査が止まり、全アドレスが 1 口座になる。"""
    recv = _tx("r0", [(OTHER, 1_000_000)], [(RECV0, 1_000_000)], height=1)
    recv1 = _tx("r1", [(OTHER, 500_000)], [(RECV1, 500_000)], height=2)
    spend = _tx("s0", [(RECV0, 1_000_000)], [(OTHER, 600_000), (CHANGE0, 399_000)], fee=1000, height=3)
    src = FakeEsplora(ZPUB, {RECV0: [recv, spend], RECV1: [recv1], CHANGE0: [spend]})
    txs = src.fetch_all()
    types = sorted(t.type.value for t in txs)
    assert types == ["deposit", "deposit", "fee", "withdraw"]
    # 残高 = 0.01 + 0.005 − 0.006 − 0.00001 = 0.00899（おつりは自分なので出金にならない）
    assert src.used_addresses == 3
    # 受取: 2 使用 + 20 未使用、おつり: 1 使用 + 20 未使用 = 43 アドレス
    assert src.scanned_addresses == 43
    assert src.warnings == []


def test_xpub_probes_addresses_in_order():
    key = parse_extended_pubkey(ZPUB)
    src = FakeEsplora(ZPUB, {})
    src.fetch_all()
    probes = [r.split("/")[2] for r in src.requests if r.count("/") == 2]
    assert probes[:2] == [RECV0, RECV1]
    assert probes[:20] == [key.address(0, i, P2WPKH) for i in range(20)]
    # 受取が 1 つも使われていなければ、おつりチェーンは見ない
    assert len(probes) == 20
    assert len(src.warnings) == 1
    assert "取引がありません" in src.warnings[0]


def test_xpub_scans_four_script_types_for_plain_xpub():
    xpub = ("xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9Qyr"
            "LprQ3VCECoY49yfdDEHGCtMMj92pReUsQ")
    src = FakeEsplora(xpub, {})
    src.fetch_all()
    probes = [r.split("/")[2] for r in src.requests if r.count("/") == 2]
    assert len(probes) == 80
    assert any(p.startswith("bc1p") for p in probes)
    assert any(p.startswith("bc1q") for p in probes)
    assert any(p.startswith("3") for p in probes)
    assert any(p.startswith("1") for p in probes)


def test_descriptor_limits_scan_to_one_type():
    xpub = ("xpub6BgBgsespWvERF3LHQu6CnqdvfEvtMcQjYrcRzx53QJjSxarj2afYWcLteoGVky7D3UKDP9Qyr"
            "LprQ3VCECoY49yfdDEHGCtMMj92pReUsQ")
    src = FakeEsplora(f"tr([d34db33f/86h/0h/0h]{xpub}/<0;1>/*)", {})
    src.fetch_all()
    probes = [r.split("/")[2] for r in src.requests if r.count("/") == 2]
    assert len(probes) == 20
    assert probes[0] == "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"


def test_invalid_wallet_rejected():
    with pytest.raises(ValueError, match="解釈できません"):
        EsploraSource("btc", "notanaddress")


def test_xprv_rejected():
    xprv = ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kej"
            "MRNNU3TGtRBeJgk33yuGBxrMPHi")
    with pytest.raises(ValueError, match="秘密"):
        EsploraSource("btc", xprv)


def test_esplora_url_env(monkeypatch):
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    assert es.esplora_url() == es.DEFAULT_ESPLORA_URL
    assert EsploraSource("btc", MINE).hosts == list(es.DEFAULT_ESPLORA_URLS)
    monkeypatch.setenv(es.ESPLORA_URL_ENV, "https://blockstream.info/api/ ")
    assert es.esplora_url() == "https://blockstream.info/api"
    src = EsploraSource("btc", MINE)
    assert src.base_url == "https://blockstream.info/api"
    assert src.hosts == ["https://blockstream.info/api"]  # 1 つだけならフォールバック無し
    monkeypatch.setenv(es.ESPLORA_URL_ENV, " http://umbrel.local:3006/api , https://mempool.space/api/")
    assert EsploraSource("btc", MINE).hosts == ["http://umbrel.local:3006/api", "https://mempool.space/api"]


# ---- ホスト切り替え（HTTP 層をモック） ----

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def http(monkeypatch):
    """httpx.get と time.sleep を差し替え、(URL → 応答) の表とスリープ記録を返す。"""
    calls: list[str] = []
    sleeps: list[float] = []
    table: dict[str, list] = {}

    def fake_get(url, timeout=None):
        calls.append(url)
        host = url.split("/api", 1)[0] + "/api"
        queue = table.get(host)
        if not queue:
            return _Resp(200, [])
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(es.httpx, "get", fake_get)
    monkeypatch.setattr(es.time, "sleep", lambda s: sleeps.append(s))
    return {"calls": calls, "sleeps": sleeps, "table": table}


MEMPOOL = "https://mempool.space/api"
BLOCKSTREAM = "https://blockstream.info/api"


def test_persistent_429_switches_to_fallback_host(http, monkeypatch):
    """主ホストが 429 を返し続けたら Blockstream に切り替え、その後も戻らない。"""
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    tx = _tx("t1", [(OTHER, 1000)], [(MINE, 1000)])
    http["table"][MEMPOOL] = [_Resp(429)]
    http["table"][BLOCKSTREAM] = [_Resp(200, [tx])]
    src = EsploraSource("btc", MINE)
    txs = src.fetch_all()
    assert [t.tx_hash for t in txs] == ["t1"]
    assert src.base_url == BLOCKSTREAM
    mempool_calls = [c for c in http["calls"] if c.startswith(MEMPOOL)]
    assert len(mempool_calls) == es._MAX_RETRIES
    assert len(src.warnings) == 1
    assert "mempool.space" in src.warnings[0] and "blockstream.info" in src.warnings[0]


def test_switch_sticks_for_rest_of_sync(http, monkeypatch):
    """切り替え後のページングも同じ txid でフォールバック先から続く。"""
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    page1 = [_tx(f"t{i:03d}", [(OTHER, 1000)], [(MINE, 1000)]) for i in range(25)]
    page2 = [_tx("t025", [(OTHER, 1000)], [(MINE, 1000)])]
    http["table"][MEMPOOL] = [_Resp(503)]
    http["table"][BLOCKSTREAM] = [_Resp(200, page1), _Resp(200, page2)]
    src = EsploraSource("btc", MINE)
    assert len(src.fetch_all()) == 26
    after_switch = [c for c in http["calls"][es._MAX_RETRIES:]]
    assert all(c.startswith(BLOCKSTREAM) for c in after_switch)
    assert after_switch[-1].endswith("/txs/chain/t024")
    assert len(src.warnings) == 1  # 切り替えの警告は 1 回だけ


def test_connection_error_also_falls_back(http, monkeypatch):
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    http["table"][MEMPOOL] = [es.httpx.ConnectError("boom")]
    http["table"][BLOCKSTREAM] = [_Resp(200, [])]
    src = EsploraSource("btc", MINE)
    assert src.fetch_all() == []
    assert src.base_url == BLOCKSTREAM


def test_all_hosts_failing_raises_with_both_named(http, monkeypatch):
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    http["table"][MEMPOOL] = [_Resp(429)]
    http["table"][BLOCKSTREAM] = [_Resp(502)]
    with pytest.raises(RuntimeError) as ei:
        EsploraSource("btc", MINE).fetch_all()
    msg = str(ei.value)
    assert "mempool.space" in msg and "blockstream.info" in msg
    assert "BITCOIN_ESPLORA_URL" in msg


def test_bad_request_does_not_fall_back(http, monkeypatch):
    """400（アドレス不正）はどのホストでも同じなので切り替えない。"""
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    http["table"][MEMPOOL] = [_Resp(400)]
    http["table"][BLOCKSTREAM] = [_Resp(200, [])]
    with pytest.raises(RuntimeError, match="受け付けません"):
        EsploraSource("btc", MINE).fetch_all()
    assert all(c.startswith(MEMPOOL) for c in http["calls"])


def test_custom_single_host_has_no_fallback(http, monkeypatch):
    monkeypatch.setenv(es.ESPLORA_URL_ENV, "http://umbrel.local:3006/api")
    http["table"]["http://umbrel.local:3006/api"] = [_Resp(429)]
    with pytest.raises(RuntimeError):
        EsploraSource("btc", MINE).fetch_all()
    assert all(c.startswith("http://umbrel.local:3006/api") for c in http["calls"])
    assert len(http["calls"]) == es._MAX_RETRIES


def test_retry_after_header_is_respected(http, monkeypatch):
    monkeypatch.setenv(es.ESPLORA_URL_ENV, MEMPOOL)
    http["table"][MEMPOOL] = [_Resp(429, headers={"retry-after": "7"}), _Resp(200, [])]
    EsploraSource("btc", MINE).fetch_all()
    assert 7.0 in http["sleeps"]


def test_transient_429_recovers_on_same_host(http, monkeypatch):
    """1 回 429 → 次で 200 なら切り替えない（警告も出ない）。"""
    monkeypatch.delenv(es.ESPLORA_URL_ENV, raising=False)
    http["table"][MEMPOOL] = [_Resp(429), _Resp(200, [])]
    src = EsploraSource("btc", MINE)
    src.fetch_all()
    assert src.base_url == MEMPOOL
    assert src.warnings == []


def test_rate_limit_interval_is_conservative():
    assert es._RATE_LIMIT_SLEEP >= 0.25


def test_tx_ids_stable_and_distinct():
    tx = _tx("s", [(MINE, 100_000)], [(OTHER, 90_000), (MINE, 9_000)], fee=1000)
    a = FakeEsplora(MINE, {MINE: [tx]}).fetch_all()
    b = FakeEsplora(MINE, {MINE: [tx]}).fetch_all()
    assert [t.id for t in a] == [t.id for t in b]
    assert len({t.id for t in a}) == 2
