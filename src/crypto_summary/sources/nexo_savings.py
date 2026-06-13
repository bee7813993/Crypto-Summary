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
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter

_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# --- タイプ別マッピング (tx_type → (canonical_type, label)) -----------------
# SKIP: 税務上不要 or 他トランザクションと重複するもの
_SKIP_TYPES = {
    "Manual Sell Order",              # Exchange Liquidation と重複
    "Manual Repayment",               # ローン返済の内部処理
    "Assimilation",                   # 残高調整
    "Interest Additional",            # 利息調整 (複雑なので個別対応)
    "Credit Card Withdrawal Credit",  # カード与信
    "Nexo Card Transaction Fee",      # カード手数料 (xUSD建て内部)
}

_TYPE_MAP: dict[str, tuple[TxType, str | None]] = {
    # --- 報酬系 ---
    "Interest":                 (TxType.REWARD,   "interest"),
    "Fixed Term Interest":      (TxType.REWARD,   "fixed_term_interest"),
    "Dual Investment Interest": (TxType.REWARD,   "dual_investment_interest"),
    "Exchange Cashback":        (TxType.REWARD,   "exchange_cashback"),
    "Cashback":                 (TxType.REWARD,   "cashback"),

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
    "Exchange Liquidation":                  (TxType.TRADE, "loan_liquidation"),
    "Nexo Card Purchase":                    (TxType.TRADE, "card_purchase"),
    "Exchange Credit":                       (TxType.TRADE, "loan_credit"),
    "Credit Card Fiatx Exchange To Withdraw":(TxType.TRADE, "card_fx"),

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

    def load(self, path: Path) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                tx = self._parse_row(row, i)
                if tx is not None:
                    txs.append(tx)
        return txs

    def _parse_row(self, row: dict[str, str], idx: int) -> CanonicalTx | None:
        tx_type_str = row["Type"].strip()

        if tx_type_str in _SKIP_TYPES:
            return None

        if tx_type_str not in _TYPE_MAP:
            return None  # 未知タイプはスキップ

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
            # Input: 送出した通貨 (金額は負)
            # Output: 受取った通貨 (金額は正)
            sent_amount = abs(in_amt) if in_amt else None
            recv_amount = out_amt
            recv_asset  = out_asset or in_asset

            # Output Amount が 0 の場合(Exchange Liquidation等)はスキップ
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
                fee_asset=fee_asset if fee_amt else None,
                fee_amount=fee_amt,
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
