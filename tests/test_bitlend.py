"""BitLendCsvSource のテスト

重点検証:
- 貸出開始 が DEPOSIT として残高に積み上がること (TRANSFER送出ではない)
- 貸借料付与 が REWARD として計上されること
- 銘柄名の正規化 (USDC_ERC_20 → USDC)
- 返還 が WITHDRAW として残高から引かれ、申請日の日時になること
"""
from datetime import datetime, timezone
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
    """未知の種別はスキップし、取り込み結果に出るよう件数を数える。"""
    src = BitLendCsvSource("bitlend")
    txs = src.load(_write_csv(tmp_path, "2026/04/15 00:00:00,1001,BTC,未知の種別,0.1,,"))
    assert txs == []
    assert src.skipped == 1
    assert src.skip_reasons == {"未対応の種別: 未知の種別": 1}


def test_return_is_withdraw_at_applied_date(tmp_path):
    """返還は出金(WITHDRAW)。CSV の負の数量を正の送出量にし、日時は申請日(JST)を使う。"""
    txs = _load(tmp_path,
        "2026/10/01 00:00:00,1001,BTC,返還,-0.1007,,[1]2026/10/26 10:28:12")
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.WITHDRAW
    assert tx.sent_asset == "BTC"
    assert tx.sent_amount == Decimal("0.1007")
    assert tx.received_asset is None
    assert tx.label == "lending_return"
    # 2026/10/26 10:28:12 JST = 01:28:12 UTC
    assert tx.timestamp == datetime(2026, 10, 26, 1, 28, 12, tzinfo=timezone.utc)


def test_return_without_applied_date_uses_timestamp(tmp_path):
    """申請日が空ならタイムスタンプのまま。"""
    txs = _load(tmp_path, "2026/10/01 00:00:00,1001,BTC,返還,-0.1007,,")
    assert txs[0].timestamp == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_zero_return_fee_not_recorded(tmp_path):
    """数量 0 の返還手数料は記録しない（スキップとしても数えない）。"""
    src = BitLendCsvSource("bitlend")
    txs = src.load(_write_csv(tmp_path,
        "2026/10/01 00:00:00,1001,BTC,返還,-0.1007,,[1]2026/10/26 10:28:12",
        "2026/10/01 00:00:00,1001,BTC,返還手数料,0,13000000,"))
    assert [t.type for t in txs] == [TxType.WITHDRAW]
    assert src.skipped == 0


def test_return_fee_follows_return_applied_date(tmp_path):
    """返還手数料は手数料(FEE)。日時は同じ貸出の返還行の申請日に揃える。"""
    txs = _load(tmp_path,
        "2026/10/01 00:00:00,1001,BTC,返還,-0.1,,[1]2026/10/26 10:28:12",
        "2026/10/01 00:00:00,1001,BTC,返還手数料,-0.0007,13000000,",
        "2026/10/01 00:00:00,1002,BTC,返還手数料,-0.0001,13000000,")
    fee, other = txs[1], txs[2]
    assert fee.type == TxType.FEE
    assert fee.fee_asset == "BTC"
    assert fee.fee_amount == Decimal("0.0007")
    assert fee.label == "lending_return_fee"
    assert fee.timestamp == txs[0].timestamp
    # 別の貸出の手数料は引きずられない
    assert other.timestamp == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_balance_returns_to_zero_after_return(tmp_path):
    """貸出開始 → 貸借料付与 → 返還 で残高が 0 に戻る。"""
    txs = _load(tmp_path,
        "2026/04/15 00:00:00,1001,BTC,貸出開始,0.1,,",
        "2026/05/01 00:00:00,1001,BTC,貸借料付与,0.0003,12000000,",
        "2026/06/01 00:00:00,1001,BTC,貸借料付与,0.0004,12000000,",
        "2026/06/01 00:00:00,1001,BTC,返還,-0.1007,,[1]2026/06/20 09:00:00",
        "2026/06/01 00:00:00,1001,BTC,返還手数料,0,12000000,")
    balance = Decimal("0")
    for tx in txs:
        balance += tx.received_amount or 0
        balance -= tx.sent_amount or 0
        balance -= tx.fee_amount or 0
    assert balance == Decimal("0")
