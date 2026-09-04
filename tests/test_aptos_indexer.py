"""AptosIndexerSource のテスト（HTTP はモック）

Aptos ウォレット取引の分類ロジックとアドレス正規化を検証する。
"""
import json
from decimal import Decimal

import pytest

import crypto_summary.sources.aptos.indexer as indexer_mod
from crypto_summary.core.models import TxType
from crypto_summary.sources.aptos.indexer import (
    AptosIndexerSource,
    _APT_FA_ADDRESS,
    _PAGE_SIZE,
    _masked_key,
    normalize_address,
)

# 先頭ゼロを含むアドレス（短縮表記との差が出るもの）
WALLET_SHORT = "0xaabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccd"
WALLET = "0x0aabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccddaabbccd"

APT_COIN = "0x1::aptos_coin::AptosCoin"
USDC = "0xbae207659db88bea0cbead6da0ed00aac12edcdda169e591cd41c94180b46f3b"
SPAM = "0xdeadbeef00000000000000000000000000000000000000000000000000000001"

_TS = "2026-08-18T23:17:53"


class FakeAptos(AptosIndexerSource):
    """_request / _drain_version をモックするテスト用サブクラス。"""

    def __init__(self, pages: list[list[dict]], wallet: str = WALLET):
        super().__init__("aptos", wallet)
        self._pages = list(pages)
        self.drained: list[int] = []

    def _request(self, from_version):
        return self._pages.pop(0) if self._pages else []

    def _drain_version(self, version):
        self.drained.append(version)
        return []


def _act(version, event_index, asset, amount, kind, *, symbol, decimals,
         name=None, gas=False, refund=0, entry="0x1::aptos_account::transfer"):
    return {
        "transaction_version": version,
        "event_index": event_index,
        "asset_type": asset,
        "amount": amount,
        "type": kind,
        "is_gas_fee": gas,
        "storage_refund_amount": refund,
        "entry_function_id_str": entry,
        "transaction_timestamp": _TS,
        "metadata": {"symbol": symbol, "name": name or symbol, "decimals": decimals},
    }


def _deposit(version, idx, asset, amount, **kw):
    return _act(version, idx, asset, amount, "0x1::fungible_asset::Deposit", **kw)


def _withdraw(version, idx, asset, amount, **kw):
    return _act(version, idx, asset, amount, "0x1::fungible_asset::Withdraw", **kw)


def _gas(version, octas, refund=0):
    return _act(version, -1, APT_COIN, octas, "0x1::aptos_coin::GasFeeEvent",
                symbol="APT", name="Aptos Coin", decimals=8, gas=True, refund=refund)


# ── アドレス正規化 ───────────────────────────────────────────────────

def test_normalize_pads_short_address():
    """短縮表記のアドレスは Indexer 保存形式（0x + 64桁）にゼロ埋めされる。"""
    assert normalize_address(WALLET_SHORT) == WALLET
    assert len(normalize_address("0x1")) == 66


def test_normalize_accepts_missing_prefix_and_uppercase():
    assert normalize_address("0xABCD") == "0x" + "0" * 60 + "abcd"
    assert normalize_address("ABCD") == "0x" + "0" * 60 + "abcd"


def test_normalize_rejects_non_hex():
    with pytest.raises(ValueError):
        normalize_address("SoLaNaAddress")
    with pytest.raises(ValueError):
        normalize_address("0x")


def test_constructor_normalizes_wallet():
    assert AptosIndexerSource("aptos", WALLET_SHORT).wallet == WALLET


# ── USDC 受取・送出 ─────────────────────────────────────────────────

def test_usdc_deposit():
    """USDC を受け取った場合は DEPOSIT になる（小数6桁で換算）。"""
    src = FakeAptos([[
        _deposit(100, 5, USDC, 3_999_838, symbol="USDC", decimals=6),
    ]])
    txs = src.fetch_all(record_gas=True)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type is TxType.DEPOSIT
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("3.999838")
    assert tx.tx_hash == "100"
    assert tx.label == "transfer"


def test_usdc_withdraw_with_gas():
    """USDC 送出はガス（APT）と本体の 2 件になる。"""
    src = FakeAptos([[
        _withdraw(101, 0, USDC, 1_500_000, symbol="USDC", decimals=6),
        _gas(101, 6_300),
    ]])
    txs = src.fetch_all(record_gas=True)
    assert len(txs) == 2
    fee = next(t for t in txs if t.type is TxType.FEE)
    body = next(t for t in txs if t.type is TxType.WITHDRAW)
    assert fee.fee_asset == "APT"
    assert fee.fee_amount == Decimal("0.000063")
    assert body.sent_asset == "USDC"
    assert body.sent_amount == Decimal("1.5")


def test_no_gas_option_drops_fee():
    src = FakeAptos([[
        _withdraw(101, 0, USDC, 1_500_000, symbol="USDC", decimals=6),
        _gas(101, 6_300),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert [t.type for t in txs] == [TxType.WITHDRAW]


def test_gas_is_net_of_storage_refund():
    """ガス代はストレージ返金を差し引いた正味を記録する。"""
    src = FakeAptos([[_gas(102, 100_000, refund=40_000)]])
    txs = src.fetch_all(record_gas=True)
    assert len(txs) == 1
    assert txs[0].fee_amount == Decimal("0.0006")


# ── APT（Coin v1 / FA v2 の統合）────────────────────────────────────

def test_apt_fa_and_coin_types_merge_into_one_symbol():
    """APT は FA アドレスでも Coin 型でも同じ APT として扱う。

    転送は FA(0x…0a)、ガスは Coin 型で来るのが実際の Indexer の形。
    """
    src = FakeAptos([[
        _deposit(200, 1, _APT_FA_ADDRESS, 507_000, symbol="APT",
                 name="Aptos Coin", decimals=8),
        _gas(200, 6_300),
    ]])
    txs = src.fetch_all(record_gas=True)
    assert {t.type for t in txs} == {TxType.DEPOSIT, TxType.FEE}
    dep = next(t for t in txs if t.type is TxType.DEPOSIT)
    assert dep.received_asset == "APT"
    assert dep.received_amount == Decimal("0.00507")


def test_self_transfer_nets_to_zero():
    """同一ウォレット内の出入りは相殺され、ガスだけが残る。"""
    src = FakeAptos([[
        _withdraw(201, 0, _APT_FA_ADDRESS, 1, symbol="APT", decimals=8),
        _deposit(201, 1, _APT_FA_ADDRESS, 1, symbol="APT", decimals=8),
        _gas(201, 37_400),
    ]])
    txs = src.fetch_all(record_gas=True)
    assert [t.type for t in txs] == [TxType.FEE]


# ── スワップ・複合 ──────────────────────────────────────────────────

def test_swap_becomes_trade():
    """1送出 + 1受取 は TRADE（swap）になる。"""
    src = FakeAptos([[
        _withdraw(300, 0, _APT_FA_ADDRESS, 100_000_000, symbol="APT", decimals=8,
                  entry="0x1c32::panora_swap::router_entry"),
        _deposit(300, 5, USDC, 4_000_000, symbol="USDC", decimals=6,
                 entry="0x1c32::panora_swap::router_entry"),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type is TxType.TRADE
    assert tx.label == "swap"
    assert tx.sent_asset == "APT"
    assert tx.sent_amount == Decimal("1")
    assert tx.received_asset == "USDC"
    assert tx.received_amount == Decimal("4")


def test_multi_out_is_lp_add():
    src = FakeAptos([[
        _withdraw(301, 0, _APT_FA_ADDRESS, 100_000_000, symbol="APT", decimals=8),
        _withdraw(301, 1, USDC, 4_000_000, symbol="USDC", decimals=6),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 2
    assert all(t.type is TxType.TRANSFER and t.label == "lp_add" for t in txs)


def test_multi_in_is_lp_remove():
    src = FakeAptos([[
        _deposit(302, 0, _APT_FA_ADDRESS, 100_000_000, symbol="APT", decimals=8),
        _deposit(302, 1, USDC, 4_000_000, symbol="USDC", decimals=6),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == 2
    assert all(t.type is TxType.TRANSFER and t.label == "lp_remove" for t in txs)


def test_mixed_multi_flows_recorded_individually():
    """複数送出 + 複数受取 は資産ごとに個別記録する。"""
    src = FakeAptos([[
        _withdraw(303, 0, _APT_FA_ADDRESS, 100_000_000, symbol="APT", decimals=8),
        _withdraw(303, 1, USDC, 4_000_000, symbol="USDC", decimals=6),
        _deposit(303, 2, "0xaaa", 1_000_000, symbol="USDT", decimals=6),
        _deposit(303, 3, "0xbbb", 2_000_000, symbol="THL", decimals=6),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert sorted(t.label for t in txs) == [
        "token_in", "token_in", "token_out", "token_out"]


# ── 除外ルール ──────────────────────────────────────────────────────

def test_spam_token_is_skipped():
    """URL / 宣伝文句を含むトークンは取り込まない。"""
    src = FakeAptos([[
        _deposit(400, 0, SPAM, 1_000_000, symbol="CLAIM", decimals=6,
                 name="Claim at https://evil.example/"),
    ]])
    assert src.fetch_all(record_gas=False) == []


def test_non_ascii_token_is_skipped():
    src = FakeAptos([[
        _deposit(401, 0, SPAM, 1_000_000, symbol="USDС", decimals=6),  # 'С' はキリル文字
    ]])
    assert src.fetch_all(record_gas=False) == []


def test_unknown_decimals_is_skipped():
    """小数桁が分からない資産は数量を確定できないので取り込まない。"""
    rec = _deposit(402, 0, "0xccc", 1_000_000, symbol="???", decimals=6)
    rec["metadata"] = None
    src = FakeAptos([[rec]])
    assert src.fetch_all(record_gas=False) == []


def test_symbol_is_uppercased():
    """Aptos ネイティブ Tether の "USDt" は USDT に寄せる（取引所側と揃える）。"""
    src = FakeAptos([[
        _deposit(405, 0, "0x357b", 55_000_000, symbol="USDt", name="Tether USD",
                 decimals=6),
    ]])
    txs = src.fetch_all(record_gas=False)
    assert txs[0].received_asset == "USDT"
    assert txs[0].received_amount == Decimal("55")


def test_missing_symbol_falls_back_to_short_asset_type():
    """シンボルだけ空なら asset_type の短縮形で名付けて数量は活かす。"""
    rec = _deposit(403, 0, "0x" + "c" * 64, 1_000_000, symbol="", decimals=6)
    rec["metadata"] = {"symbol": "", "name": "", "decimals": 6}
    txs = FakeAptos([[rec]]).fetch_all(record_gas=False)
    assert len(txs) == 1
    assert "…" in txs[0].received_asset
    assert txs[0].received_amount == Decimal("1")


def test_balance_neutral_event_is_ignored():
    """Frozen など残高が動かないイベントは無視する。"""
    src = FakeAptos([[
        _act(404, 0, USDC, 0, "0x1::fungible_asset::Frozen",
             symbol="USDC", decimals=6),
    ]])
    assert src.fetch_all(record_gas=False) == []


# ── ページング ──────────────────────────────────────────────────────

def _page(start_version, count):
    return [
        _deposit(start_version + i, 0, USDC, 1_000_000, symbol="USDC", decimals=6)
        for i in range(count)
    ]


def test_pagination_follows_version_cursor():
    """満杯のページが続く限り次ページを取りにいく。"""
    src = FakeAptos([_page(1000, _PAGE_SIZE), _page(2000, 3)])
    txs = src.fetch_all(record_gas=False)
    assert len(txs) == _PAGE_SIZE + 3


def test_pagination_dedupes_boundary_records():
    """ページ境界で重複したレコードは 1 件に畳まれる。"""
    first = _page(1000, _PAGE_SIZE)
    second = [first[-1]] + _page(2000, 2)  # 境界の 1 件が再登場
    src = FakeAptos([first, second])
    assert len(src.fetch_all(record_gas=False)) == _PAGE_SIZE + 2


# ── エラー応答 ──────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _stub_http(monkeypatch, status_code: int, payload: dict):
    monkeypatch.setattr(indexer_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        indexer_mod.httpx, "post",
        lambda *a, **kw: _FakeResponse(status_code, payload),
    )


def test_auth_error_reports_server_reason(monkeypatch):
    """401 のときサーバーの説明をそのまま見せる（原因の切り分けに要る）。"""
    _stub_http(monkeypatch, 401,
               {"errors": [{"message": "Unauthorized: API key not found"}]})
    src = AptosIndexerSource("aptos", WALLET, "aptoslabs_verySecretValue_abcdef")
    with pytest.raises(RuntimeError) as e:
        src.fetch_all()
    msg = str(e.value)
    assert "Unauthorized: API key not found" in msg
    # キーの種別と桁数は分かるが、値そのものは出さない
    assert "aptoslabs_" in msg
    assert "verySecretValue" not in msg


def test_rate_limit_error_reports_server_reason(monkeypatch):
    _stub_http(monkeypatch, 429,
               {"errors": [{"message": "Per anonymous IP rate limit exceeded."}]})
    with pytest.raises(RuntimeError, match="rate limit exceeded"):
        AptosIndexerSource("aptos", WALLET).fetch_all()


def test_masked_key_shows_type_and_length_only():
    masked = _masked_key("aptoslabs_verySecretValue_abcdef")
    assert masked.startswith("aptoslabs_")
    assert "verySecretValue" not in masked
    assert "32" in masked  # 桁数が出るのでコピー欠けに気づける
    assert _masked_key(None) == "なし（匿名アクセス）"


def test_pagination_drains_stuck_version():
    """1 version にページ上限を超えるイベントがあってもカーソルが止まらない。

    カーソルを last_version に進めても同じ version で埋まったページが返る状況。
    その version だけ一括取得してから次へ進める。
    """
    stuck = [
        _deposit(1000, i, USDC, 1_000_000, symbol="USDC", decimals=6)
        for i in range(_PAGE_SIZE)
    ]
    src = FakeAptos([stuck, list(stuck), _page(2000, 1)])
    src.fetch_all(record_gas=False)
    assert src.drained == [1000]
