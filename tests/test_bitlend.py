"""BitLendCsvSource のテスト

重点検証:
- 貸出開始 が DEPOSIT として残高に積み上がること (TRANSFER送出ではない)
- 貸借料付与 が REWARD として計上されること
- 銘柄名の正規化 (USDC_ERC_20 → USDC)
"""
from decimal import Decimal
from pathlib import Path

from crypto_summary.sources.jp.bitlend import BitLendCsvSource
from crypto_summary.core.models import TxType

_HEADER = "タイムスタンプ,貸出ID,銘柄名,種別,数量,レート,申請日"


def _write_csv(tmp_path: Path, *rows: str) -> Path:
    p = tmp_path / "BitLending.csv"
    p.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def _load(tmp_path: Path, *rows: str):
    src = BitLendCsvSource("bitlend")
    return src.load(_write_csv(tmp_path, *rows))


def test_lending_start_is_deposit(tmp_path):
    """貸出開始は預け入れ(DEPOSIT)。残高が増える。"""
    txs = _load(tmp_path, "2026/04/15 00:00:00,43906,BTC,貸出開始,0.15219,,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.DEPOSIT
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.15219")
    assert tx.sent_asset is None


def test_interest_is_reward(tmp_path):
    """貸借料付与は利息収入(REWARD)。"""
    txs = _load(tmp_path, "2026/05/01 00:00:00,43906,BTC,貸借料付与,0.00051508,11977641.04,")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "BTC"
    assert tx.received_amount == Decimal("0.00051508")


def test_asset_normalization(tmp_path):
    """USDC_ERC_20 → USDC に正規化される。"""
    txs = _load(tmp_path, "2026/04/17 00:00:00,44010,USDC_ERC_20,貸出開始,10000,,")
    assert txs[0].received_asset == "USDC"


def test_balance_principal_plus_interest(tmp_path):
    """貸出開始 + 貸借料付与 = 元本 + 利息 で残高がプラスになる。"""
    txs = _load(tmp_path,
        "2026/04/17 00:00:00,44010,USDC_ERC_20,貸出開始,10000,,",
        "2026/05/01 00:00:00,44010,USDC_ERC_20,貸借料付与,36.70289293,156.96,")
    balance = Decimal("0")
    for tx in txs:
        if tx.received_amount:
            balance += tx.received_amount
        if tx.sent_amount:
            balance -= tx.sent_amount
    assert balance == Decimal("10036.70289293")


def test_unknown_kind_skipped(tmp_path):
    """未知の種別はスキップ。"""
    txs = _load(tmp_path, "2026/04/15 00:00:00,43906,BTC,返還,0.15219,,")
    assert txs == []
