"""NexoFuturesCsvSource（Nexo 先物取引履歴）のテスト。

先物ウォレットは Nexo 本体アカウント内のウォレットで、Nexo Pro とは別サービス。
証拠金取引のため建玉（MARKET_OPEN/CLOSE）自体は資産の増減として記録せず、
bitFlyer 証拠金口座と同じ実現損益ベース（利益→REWARD / 損失→FEE）で
取り込むことを検証する。
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.models import TxType
from crypto_summary.sources.nexo import NexoProCsvSource
from crypto_summary.sources.nexo_futures import NexoFuturesCsvSource

_HEADER = (
    "Id,Timestamp,Symbol,Type,Position,Price,Amount,Status,Asset,Side,"
    "DealId,InternalTransactionType,TradingFeeAmount,TradingFeeAsset,"
    "ExecutionPriceAsset,ExchangeRate,SourceAsset,SourceAmount\n"
)


def _row(
    row_id: str,
    ts: str,
    tx_type: str,
    amount: str,
    *,
    symbol: str = "SOLUSDT",
    asset: str = "USDT",
    fee: str = "null",
    fee_asset: str = "USDT",
) -> str:
    return (
        f"{row_id},{ts},{symbol},{tx_type},null,null,{amount},PLACED,{asset},"
        f"long,deal_{row_id},ORDER,{fee},{fee_asset},USDT,null,null,null\n"
    )


def _write(tmp_path: Path, content: str, name: str = "nexo_futures_transactions.csv") -> Path:
    p = tmp_path / name
    p.write_text(_HEADER + content, encoding="utf-8")
    return p


def test_realized_pnl_profit_is_reward(tmp_path):
    p = _write(tmp_path, _row("1", "1788000000000", "REALIZED_PNL", "51.57"))
    txs = NexoFuturesCsvSource("nexo").load(p)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.REWARD
    assert tx.received_asset == "USDT"
    assert tx.received_amount == Decimal("51.57")
    assert tx.label == "futures_realized_profit"
    assert tx.timestamp == datetime(2026, 8, 29, 10, 40, tzinfo=timezone.utc)


def test_realized_pnl_loss_is_fee(tmp_path):
    p = _write(tmp_path, _row("2", "1788000000000", "REALIZED_PNL", "-12.34"))
    txs = NexoFuturesCsvSource("nexo").load(p)
    assert len(txs) == 1
    tx = txs[0]
    assert tx.type == TxType.FEE
    assert tx.fee_asset == "USDT"
    assert tx.fee_amount == Decimal("12.34")
    assert tx.label == "futures_realized_loss"


def test_funding_fee_signs(tmp_path):
    p = _write(
        tmp_path,
        _row("3", "1788000000000", "FUNDING_FEE", "-6.62653")
        + _row("4", "1788000001000", "FUNDING_FEE", "0.5"),
    )
    txs = NexoFuturesCsvSource("nexo").load(p)
    by_label = {t.label: t for t in txs}
    assert by_label["futures_funding_loss"].type == TxType.FEE
    assert by_label["futures_funding_loss"].fee_amount == Decimal("6.62653")
    assert by_label["futures_funding_profit"].type == TxType.REWARD
    assert by_label["futures_funding_profit"].received_amount == Decimal("0.5")


def test_open_close_record_only_trading_fee(tmp_path):
    """建玉・決済注文は現物の売買として記録せず、取引手数料のみ FEE で記録する。"""
    p = _write(
        tmp_path,
        _row("o1", "1788000000000", "MARKET_OPEN", "473.39",
             asset="SOL", fee="30.009024")
        + _row("c1", "1788000100000", "MARKET_CLOSE", "473.39",
               asset="SOL", fee="30.019148"),
    )
    txs = NexoFuturesCsvSource("nexo").load(p)
    assert len(txs) == 2
    assert all(t.type == TxType.FEE for t in txs)
    assert all(t.label == "futures_fee" for t in txs)
    assert all(t.fee_asset == "USDT" for t in txs)
    assert {t.fee_amount for t in txs} == {Decimal("30.009024"), Decimal("30.019148")}
    # SOL（建玉数量）が資産として現れないこと
    assert all(t.received_asset is None and t.sent_asset is None for t in txs)


def test_open_without_fee_records_nothing(tmp_path):
    p = _write(tmp_path, _row("o2", "1788000000000", "MARKET_OPEN", "10",
                              asset="SOL", fee="null"))
    assert NexoFuturesCsvSource("nexo").load(p) == []


def test_wallet_transfers_are_skipped(tmp_path):
    """貯蓄⇔先物ウォレットの振替は記録しない (貯蓄側 CSV と両側スキップの設計)。"""
    p = _write(
        tmp_path,
        _row("t1", "1788000000000", "TRANSFER_FROM_WALLET", "10000.5", symbol="null")
        + _row("t2", "1788000100000", "TRANSFER_TO_WALLET", "10000", symbol="null"),
    )
    src = NexoFuturesCsvSource("nexo")
    assert src.load(p) == []
    assert src.skipped == 2
    assert src.skip_reasons == {"貯蓄⇔先物ウォレットの振替": 2}


def test_unknown_type_is_counted(tmp_path):
    p = _write(tmp_path, _row("x1", "1788000000000", "SOMETHING_NEW", "1"))
    src = NexoFuturesCsvSource("nexo")
    assert src.load(p) == []
    assert src.skip_reasons == {"未知の種別: SOMETHING_NEW": 1}


def test_idempotent_ids(tmp_path):
    p = _write(tmp_path, _row("1", "1788000000000", "REALIZED_PNL", "51.57"))
    ids1 = [t.id for t in NexoFuturesCsvSource("nexo").load(p)]
    ids2 = [t.id for t in NexoFuturesCsvSource("nexo").load(p)]
    assert ids1 == ids2


def test_leading_space_header_yields_unique_ids(tmp_path):
    """実ファイルはヘッダー先頭列が " Id"（先頭スペース付き）。

    キーを strip せず Id が取れないと全行の ID が衝突し、台帳の upsert で
    上書きされて 22件が2件に潰れる実障害があった。ID の一意性を固定する。
    """
    content = " " + _HEADER + (  # ヘッダー先頭にスペース → 先頭列名が " Id"
        _row("1", "1788000000000", "REALIZED_PNL", "51.57")
        + _row("2", "1788000001000", "REALIZED_PNL", "73.49")
        + _row("3", "1788000002000", "FUNDING_FEE", "-6.62")
        + _row("o1", "1788000003000", "MARKET_OPEN", "10", asset="SOL", fee="1.5")
        + _row("o2", "1788000004000", "MARKET_CLOSE", "10", asset="SOL", fee="1.6")
    )
    p = tmp_path / "nexo_futures_transactions.csv"
    p.write_text(content, encoding="utf-8")
    txs = NexoFuturesCsvSource("nexo").load(p)
    assert len(txs) == 5
    assert len({t.id for t in txs}) == 5  # 全行ユニーク


def test_wrong_format_raises_helpful_error(tmp_path):
    """別形式（Spot 履歴など）を選んだとき、必要な列が分かるエラーになる。"""
    p = tmp_path / "SpotHistory.csv"
    p.write_text(
        "id,timestamp,pair,side,type,price,executedPrice,triggerPrice,"
        "requestedAmount,filledAmount,tradingFee,feeCurrency,status,orderId\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="先物取引履歴CSV（nexo_futures_transactions）ではないようです"):
        NexoFuturesCsvSource("nexo").load(p)


def test_pro_adapter_rejects_futures_with_guidance(tmp_path):
    """先物は Nexo Pro とは別サービス。Pro を選んだら選び直しを案内して落とす。

    黙って取り込むと Pro 口座に先物の損益が混ざり、資金が出入りしている
    貯蓄口座との残高が合わなくなる。
    """
    p = _write(tmp_path, _row("1", "1788000000000", "REALIZED_PNL", "51.57"))
    with pytest.raises(ValueError, match="「Nexo（先物取引）」を選択してください"):
        NexoProCsvSource("nexo").load(p)


def test_registered_in_registry():
    from crypto_summary.sources.csv_import import EXCHANGE_SOURCES
    assert EXCHANGE_SOURCES["nexo_futures"] is NexoFuturesCsvSource


def test_reconciles_with_savings_side():
    """先物CSV + 貯蓄CSV の記録合計 = 実際の振替差額 (2026-08 実データの検証)。

    実測: 貯蓄→先物 10,000 入金、全決済後に 10,471.569747 を貯蓄へ出金。
    差額 +471.569747 = ボーナス +500 + 実現損益 +479.987619
                       − 資金調達料 14.838695 − 手数料 493.579177
    が両アダプタの記録から小数6桁まで一致することを固定する。
    """
    from crypto_summary.sources.nexo_savings import NexoSavingsCsvSource
    import tempfile, os

    futures_csv = _HEADER + (
        _row("t1", "1787936356654", "TRANSFER_TO_WALLET", "10000", symbol="null")
        + _row("p1", "1788045448658", "MARKET_CLOSE", "3900.78",
               asset="SOL", fee="493.579177")
        + _row("p2", "1788045449504", "REALIZED_PNL", "479.987619")
        + _row("p3", "1787990446977", "FUNDING_FEE", "-14.838695")
        + _row("t2", "1788047574849", "TRANSFER_FROM_WALLET", "10471.569747",
               symbol="null")
    )
    savings_header = (
        "Transaction,Type,Input Currency,Input Amount,Output Currency,"
        "Output Amount,USD Equivalent,Fee,Fee Currency,Details,Date / Time (UTC)\n"
    )
    savings_csv = savings_header + "\n".join([
        'NXTa,Advanced Trading Add Bonus Funds,USDT,500.00000000,-,0.00000000,$499.95,-,-,"completed",2026-08-28 16:58:20',
        'NXTb,Transfer To Advanced,USDT,-10000.00000000,-,0.00000000,$9999.10,-,-,"completed / USDT Transfer from Savings Wallet to Futures Wallet",2026-08-28 16:59:16',
        'NXTc,Transfer To Advanced Trading Bonus,USDT,-463.11461500,-,0.00000000,$463.11,-,-,"completed",2026-08-29 23:20:15',
        'NXTd,Transfer From Advanced Trading Bonus,USDT,963.11461500,-,0.00000000,$963.07,-,-,"completed",2026-08-29 23:20:48',
        'NXTe,Transfer From Advanced,USDT,10471.56974700,-,0.00000000,$10471.22,-,-,"completed / USDT Transfer from Futures Wallet to Savings Wallet",2026-08-29 23:52:54',
    ]) + "\n"

    def _net(txs) -> Decimal:
        net = Decimal(0)
        for t in txs:
            if t.received_amount:
                net += t.received_amount
            if t.fee_amount:
                net -= t.fee_amount
        return net

    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "nexo_futures_transactions.csv"
        fp.write_text(futures_csv, encoding="utf-8")
        sp = Path(d) / "nexo_transactions.csv"
        sp.write_text(savings_csv, encoding="utf-8")

        futures_net = _net(NexoFuturesCsvSource("nexo").load(fp))
        savings_net = _net(NexoSavingsCsvSource("nexo_savings").load(sp))

    assert futures_net == Decimal("-28.430253")
    assert savings_net == Decimal("500")
    # 合計が実際の振替差額 (10471.569747 − 10000) と一致する
    assert futures_net + savings_net == Decimal("471.569747")


# 取引所からダウンロードした CSV は個人データなのでリポジトリに含めない
# （.gitignore の samples/）。手元に置いている人だけこのテストが走る。
SAMPLE = Path(__file__).parent.parent / "samples" / "nexo_futures.csv"


@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/ が無い")
def test_sample_file_net_balance():
    """サンプルCSV: 取り込み結果の純増減 = PnL − funding損 − 手数料 になる。"""
    txs = NexoFuturesCsvSource("nexo").load(SAMPLE)
    assert len({t.id for t in txs}) == len(txs)  # ID がユニーク (実ヘッダーは " Id")
    net = Decimal(0)
    for t in txs:
        if t.received_amount:
            net += t.received_amount
        if t.fee_amount:
            net -= t.fee_amount
    # 16.873004 - 12.34 - 6.62653 - 30.019148 - 30.009024 = -62.121698
    assert net == Decimal("-62.121698")
