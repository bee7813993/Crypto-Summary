"""Nexo (Savings/Earn) 取引履歴 CSV アダプタ

対象ファイル: nexo_transactions_*.csv
ダウンロード: Nexo > Profile > Download Statement

Columns:
    Transaction, Type, Input Currency, Input Amount,
    Output Currency, Output Amount, USD Equivalent,
    Fee, Fee Currency, Details, Date / Time (UTC)

Input Amount の符号:
    正  = この資産が口座に入ってきた (受取)
    負  = この資産が口座から出ていった (送出)

先物 (Advanced Trading) 関連:
    先物ウォレットは同じ Nexo アカウント内のウォレット (Nexo Pro とは別)。
    そのため貯蓄⇔先物の資金振替 (Transfer To/From Advanced) とボーナス
    ウォレットの内部移動 (Transfer To/From Advanced Trading Bonus) は
    記録しない (先物側 nexo_futures が損益・手数料を計上する)。
    キャンペーンボーナス付与 (Advanced Trading Add Bonus Funds) のみ
    REWARD として計上する。詳細は _FUTURES_TRANSFER_TYPES を参照。
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter, require_columns
from .nexo import NexoProCsvSource
from .nexo_futures import looks_like_futures

_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_REQUIRED = (
    "Transaction", "Type", "Input Currency", "Input Amount", "Date / Time (UTC)",
)


def looks_like_savings(fieldnames: list[str] | None) -> bool:
    """ヘッダーが貯蓄口座の取引明細CSVのものなら True。"""
    fields = {(name or "").strip() for name in (fieldnames or [])}
    return set(_REQUIRED) <= fields

# --- タイプ別マッピング (tx_type → (canonical_type, label)) -----------------
# SKIP: 税務上不要 or 他トランザクションと重複するもの
_SKIP_TYPES = {
    "Manual Sell Order",              # Exchange Liquidation と重複 (Transfer Out/In で計上済み)
    "Manual Repayment",               # ローン返済の内部処理
    "Assimilation",                   # 残高調整
    "Interest Additional",            # 利息調整 (複雑なので個別対応)
    "Credit Card Withdrawal Credit",  # カード与信
    "Nexo Card Transaction Fee",      # カード手数料 (xUSD建て内部)
    # ローン関連: Transfer Out/In が貯蓄ウォレットへの影響を正確に捉えるためスキップ
    "Exchange Liquidation",           # Manual Sell Order の対になる行 (クレジットライン内部)
    "Exchange Credit",                # ローン実行時の xUSD→通貨変換 (Top up Crypto と重複)
}

# 内部会計単位 (実資産ではない):
#   xUSD : クレジットライン(ローン)/カードの内部建玉単位
#   USD  : Nexoカードの法定通貨決済単位
# これらを Input Currency に持つ行はローン・カードの内部レッグであり、
# 貯蓄ウォレットの実残高に影響しない (実際のローン入金は Top up Crypto,
# 担保売却は Transfer Out/In で計上済み)。幻影残高を避けるためスキップする。
_INTERNAL_ASSETS = {"XUSD", "USD"}

# 先物 (Advanced Trading) 関連の振替。理由付きでスキップ計上する。
#   Transfer To/From Advanced : 貯蓄⇔先物ウォレットの資金振替。先物側 CSV の
#     TRANSFER_TO/FROM_WALLET と同時刻・同額の鏡像で、両側とも記録しない設計。
#     振替の入出差額 = ボーナス付与 + 先物損益 − 手数料 であり、それぞれ
#     Advanced Trading Add Bonus Funds (本アダプタ) と実現損益・手数料
#     (nexo_futures) の記録で過不足なく捕捉される。
#   Transfer To/From Advanced Trading Bonus : キャンペーン条件達成まで先物利益を
#     ボーナスウォレットへ退避→まとめて解放する内部移動 (貯蓄残高には影響しない)。
_FUTURES_TRANSFER_TYPES: dict[str, str] = {
    "Transfer To Advanced":                 "貯蓄⇔先物ウォレットの振替",
    "Transfer From Advanced":               "貯蓄⇔先物ウォレットの振替",
    "Transfer To Advanced Trading Bonus":   "先物⇔ボーナスウォレットの内部移動",
    "Transfer From Advanced Trading Bonus": "先物⇔ボーナスウォレットの内部移動",
}

_TYPE_MAP: dict[str, tuple[TxType, str | None]] = {
    # --- 報酬系 ---
    "Interest":                 (TxType.REWARD,   "interest"),
    "Fixed Term Interest":      (TxType.REWARD,   "fixed_term_interest"),
    "Dual Investment Interest": (TxType.REWARD,   "dual_investment_interest"),
    "Exchange Cashback":        (TxType.REWARD,   "exchange_cashback"),
    "Cashback":                 (TxType.REWARD,   "cashback"),
    # 先物キャンペーンボーナスの付与 (承諾時点で先物ウォレットに入る実 USDT)
    "Advanced Trading Add Bonus Funds": (TxType.REWARD, "advanced_trading_bonus"),

    # --- 入金系 ---
    "Top up Crypto":            (TxType.DEPOSIT,  None),
    "Loan Withdrawal":          (TxType.DEPOSIT,  "loan"),
    "Transfer In":              (TxType.DEPOSIT,  "internal_transfer"),
    "Transfer From Pro Wallet": (TxType.DEPOSIT,  "from_pro_wallet"),

    # --- 出金系 ---
    "Withdrawal":               (TxType.WITHDRAW, None),
    "Withdraw Exchanged":       (TxType.WITHDRAW, None),
    "Transfer Out":             (TxType.WITHDRAW, "internal_transfer"),
    "Transfer To Pro Wallet":   (TxType.WITHDRAW, "to_pro_wallet"),

    # --- 取引系 ---
    "Exchange":                              (TxType.TRADE, None),
    "Dual Investment Exchange":              (TxType.TRADE, "dual_investment"),
    "Nexo Card Purchase":                    (TxType.TRADE, "card_purchase"),
    "Credit Card Fiatx Exchange To Withdraw":(TxType.TRADE, "card_fx"),

    # --- 手数料系 (取引と別行で課金されるもの) ---
    # Advanced spot trading の手数料は Exchange 行に内包されず別行で引かれる
    "Exchange Sell Fee":        (TxType.FEE, "exchange_fee"),

    # --- 内部振替系 (ロック/アンロック) ---
    "Locking Term Deposit":    (TxType.TRANSFER, "term_deposit_lock"),
    "Unlocking Term Deposit":  (TxType.TRANSFER, "term_deposit_unlock"),
    "Dual Investment Lock":    (TxType.TRANSFER, "dual_investment_lock"),
    "Dual Investment Unlock":  (TxType.TRANSFER, "dual_investment_unlock"),
}


def _d(value: str) -> Decimal | None:
    v = value.strip().replace(",", "")
    if not v or v == "-":
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


class NexoSavingsCsvSource(CsvSourceAdapter):
    """Nexo 取引明細 CSV パーサー (1419件対応)"""

    @staticmethod
    def _check_header(fieldnames: list[str] | None) -> None:
        """別形式を選んでいたら、選ぶべき取引所まで含めて知らせる。

        Nexo 系は CSV が4種類あり取り違えやすい。この検証がないと、
        全行が「未知タイプ」として落ちて「取引が見つかりませんでした」に
        しか見えず、何を選び直せばよいか分からない。
        """
        if looks_like_futures(fieldnames):
            raise ValueError(
                "これは Nexo の先物取引履歴CSVです。"
                "取引所に「Nexo（先物取引）」を選択してください。"
            )
        kind = NexoProCsvSource.detect(fieldnames)
        if kind is not None:
            label = "スポット取引履歴" if kind == "spot" else "入出金履歴"
            raise ValueError(
                f"これは Nexo Pro の{label}CSVです（貯蓄口座とは別サービス）。"
                "取引所に「Nexo Pro」を選択してください。"
            )
        require_columns(
            fieldnames, _REQUIRED,
            "Nexo 貯蓄口座の取引明細CSV（nexo_transactions_*.csv）",
        )

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self._check_header(reader.fieldnames)
            for i, row in enumerate(reader):
                tx = self._parse_row(row, i)
                if tx is not None:
                    txs.append(tx)
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> CanonicalTx | None:
        tx_type_str = row["Type"].strip()

        if tx_type_str in _SKIP_TYPES:
            return None

        if tx_type_str in _FUTURES_TRANSFER_TYPES:
            self._skip(_FUTURES_TRANSFER_TYPES[tx_type_str])
            return None

        if tx_type_str not in _TYPE_MAP:
            # 黙って落とすと課税イベント (ボーナス付与など) を見逃すため計上する
            self._skip(f"未知タイプ: {tx_type_str}")
            return None

        # ローン/カードの内部単位 (xUSD/USD) 建ての行はスキップ (幻影残高回避)。
        # 実際の入金/担保売却は別の行 (Top up Crypto, Transfer Out/In) で計上済み。
        if row["Input Currency"].strip().upper() in _INTERNAL_ASSETS:
            return None

        canonical_type, label = _TYPE_MAP[tx_type_str]
        ts = datetime.strptime(row["Date / Time (UTC)"].strip(), _DATE_FMT).replace(tzinfo=timezone.utc)

        in_asset  = row["Input Currency"].strip().upper()
        out_asset = row.get("Output Currency", "").strip().upper() or None
        in_amt    = _d(row["Input Amount"])
        out_amt   = _d(row.get("Output Amount", ""))
        fee_amt   = _d(row.get("Fee", ""))
        fee_asset = row.get("Fee Currency", "").strip().upper() or None
        tx_id     = row["Transaction"].strip()

        if canonical_type == TxType.REWARD:
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, tx_id),
                source=self.source_id,
                timestamp=ts,
                type=TxType.REWARD,
                received_asset=in_asset,
                received_amount=in_amt,
                fee_asset=fee_asset if fee_amt else None,
                fee_amount=fee_amt,
                label=label,
                raw=dict(row),
            )

        elif canonical_type == TxType.DEPOSIT:
            # Transfer From Pro Wallet: Input Amount が正
            # Top up Crypto: Input Amount が正
            amount = in_amt if (in_amt and in_amt > 0) else out_amt
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, tx_id),
                source=self.source_id,
                timestamp=ts,
                type=TxType.DEPOSIT,
                received_asset=in_asset,
                received_amount=amount,
                fee_asset=fee_asset if fee_amt else None,
                fee_amount=fee_amt,
                label=label,
                raw=dict(row),
            )

        elif canonical_type == TxType.FEE:
            # 取引と別行で課金される手数料 (Exchange Sell Fee など)。
            # Input Amount が負値・Input Currency 建てで引かれる。
            amount = abs(in_amt) if in_amt else fee_amt
            if not amount:
                return None
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, tx_id),
                source=self.source_id,
                timestamp=ts,
                type=TxType.FEE,
                fee_asset=in_asset,
                fee_amount=amount,
                label=label,
                raw=dict(row),
            )

        elif canonical_type == TxType.WITHDRAW:
            # Transfer To Pro Wallet: Input Amount が負
            amount = abs(in_amt) if in_amt else out_amt
            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, tx_id),
                source=self.source_id,
                timestamp=ts,
                type=TxType.WITHDRAW,
                sent_asset=in_asset,
                sent_amount=amount,
                fee_asset=fee_asset if fee_amt else None,
                fee_amount=fee_amt,
                label=label,
                raw=dict(row),
            )

        elif canonical_type == TxType.TRADE:
            # Input: 送出した通貨 (金額は負 or 正)
            # Output: 受取った通貨 (金額は正)
            # 注意: Nexo CSV の Fee は Input Amount に内包されているため別途減算しない
            sent_amount = abs(in_amt) if in_amt else None
            recv_amount = out_amt
            recv_asset  = out_asset or in_asset

            # Output Amount が 0 の場合はスキップ
            if not recv_amount or recv_amount == 0:
                return None

            return CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, tx_id),
                source=self.source_id,
                timestamp=ts,
                type=TxType.TRADE,
                received_asset=recv_asset,
                received_amount=recv_amount,
                sent_asset=in_asset,
                sent_amount=sent_amount,
                label=label,
                raw=dict(row),
            )

        elif canonical_type == TxType.TRANSFER:
            # Lock: Input Amount 負(資産が出ていく)
            # Unlock: Input Amount 正(資産が戻ってくる)
            amount = abs(in_amt) if in_amt else out_amt
            if in_amt and in_amt < 0:
                # ロック: Savings から出ていく
                return CanonicalTx(
                    id=CanonicalTx.make_id(self.source_id, tx_id),
                    source=self.source_id,
                    timestamp=ts,
                    type=TxType.TRANSFER,
                    sent_asset=in_asset,
                    sent_amount=amount,
                    received_asset=out_asset,
                    received_amount=out_amt,
                    label=label,
                    raw=dict(row),
                )
            else:
                # アンロック: Savings に戻ってくる
                return CanonicalTx(
                    id=CanonicalTx.make_id(self.source_id, tx_id),
                    source=self.source_id,
                    timestamp=ts,
                    type=TxType.TRANSFER,
                    received_asset=in_asset,
                    received_amount=amount,
                    sent_asset=out_asset,
                    sent_amount=out_amt,
                    label=label,
                    raw=dict(row),
                )

        return None
