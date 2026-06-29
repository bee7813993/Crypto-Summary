"""SUMM カスタムCSV エクスポート

SUMM 公式仕様: https://help.summ.com/en/articles/5777675-custom-csv-import

列（14列・順序固定。1列も削除してはいけない）:
  Timestamp (UTC), Type, Base Currency, Base Amount,
  Quote Currency (Optional), Quote Amount (Optional),
  Fee Currency (Optional), Fee Amount (Optional),
  From (Optional), To (Optional), Blockchain (Optional), ID (Optional),
  Reference Price Per Unit (Optional), Reference Price Currency (Optional)

  Timestamp 形式: "YYYY-MM-DD HH:mm:ss"（UTC）
  必須列: Timestamp, Type, Base Currency, Base Amount（取引は Quote も）

Type マッピング（CanonicalTx → SUMM）:
  TRADE     → buy   : Base=受取, Quote=送付（受取を取得する売買）
  DEPOSIT   → fiat-deposit（法定通貨）/ receive（暗号資産）
  WITHDRAW  → fiat-withdrawal（法定通貨）/ send（暗号資産）
  REWARD    → staking（label に stak）/ interest（lend・interest・利息）/ income
  FEE       → fee   : Base=手数料資産
  TRANSFER  → send（送付あり）/ receive（受取のみ）
             ただし term_deposit_lock/unlock, dual_investment_lock/unlock は
             取引所内部移動のためスキップ（課税イベントではない）
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ..core.models import CanonicalTx, TxType

_NEXO_LINK_WINDOW_SEC = 120  # nexo_savings ↔ nexo Pro の振替マッチ許容秒数

_SUMM_HEADERS = [
    "Timestamp (UTC)", "Type", "Base Currency", "Base Amount",
    "Quote Currency (Optional)", "Quote Amount (Optional)",
    "Fee Currency (Optional)", "Fee Amount (Optional)",
    "From (Optional)", "To (Optional)", "Blockchain (Optional)", "ID (Optional)",
    "Reference Price Per Unit (Optional)", "Reference Price Currency (Optional)",
]

_FIAT = {"JPY", "USD", "EUR", "GBP", "AUD", "CAD", "CHF"}

# TRANSFER のうち取引所内部サブウォレット間移動は Summ に出力しない。
# Nexo 定期預金のロック/アンロックは同一口座内の移動に過ぎず課税イベントではない。
_INTERNAL_TRANSFER_LABELS = {
    "term_deposit_lock",
    "term_deposit_unlock",
    "dual_investment_lock",
    "dual_investment_unlock",
}


def link_nexo_transfers(txs: Sequence[CanonicalTx]) -> list[CanonicalTx]:
    """nexo_savings ↔ nexo (Pro) 間の内部振替 ID を統一して Summ の transfer マッチを可能にする。

    Nexo は貯蓄口座 CSV と Pro CSV で同一振替に別の ID を付けるため、
    そのままでは Summ が両端を照合できない。
    資産・金額（完全一致）・タイムスタンプ（±120秒で最近傍）で対応ペアを探し、
    nexo_savings 側の ID を nexo (Pro) 側にも適用する。
    nexo_savings と nexo の両方が txs に含まれているときのみ機能する。
    """
    txs_list = list(txs)

    # nexo_savings 側の送出 (savings → pro)
    savings_sends = sorted(
        (tx for tx in txs_list
         if tx.source == "nexo_savings"
         and tx.type == TxType.WITHDRAW
         and tx.label == "to_pro_wallet"),
        key=lambda t: t.timestamp,
    )
    # nexo_savings 側の受取 (pro → savings)
    savings_recvs = sorted(
        (tx for tx in txs_list
         if tx.source == "nexo_savings"
         and tx.type == TxType.DEPOSIT
         and tx.label == "from_pro_wallet"),
        key=lambda t: t.timestamp,
    )
    # nexo Pro 側の入金 / 出金
    nexo_deposits  = [tx for tx in txs_list if tx.source == "nexo" and tx.type == TxType.DEPOSIT]
    nexo_withdraws = [tx for tx in txs_list if tx.source == "nexo" and tx.type == TxType.WITHDRAW]

    if not savings_sends and not savings_recvs:
        return txs_list

    def _best_match(
        candidates: list[CanonicalTx],
        used_ids: set[str],
        asset: str | None,
        amount: Decimal | None,
        ts: object,
    ) -> CanonicalTx | None:
        """資産・金額（完全一致）・タイムスタンプ（120秒以内の最近傍）で最良候補を返す。"""
        if asset is None or amount is None:
            return None
        best: CanonicalTx | None = None
        best_dt = float("inf")
        for c in candidates:
            if c.id in used_ids:
                continue
            c_asset  = c.received_asset  if c.type == TxType.DEPOSIT  else c.sent_asset
            c_amount = c.received_amount if c.type == TxType.DEPOSIT  else c.sent_amount
            if c_asset != asset or c_amount is None:
                continue
            if abs(c_amount - amount) > Decimal("0.000001"):
                continue
            dt = abs((c.timestamp - ts).total_seconds())  # type: ignore[operator]
            if dt <= _NEXO_LINK_WINDOW_SEC and dt < best_dt:
                best = c
                best_dt = dt
        return best

    id_remap: dict[str, str] = {}  # nexo Pro 側 old_id → 共通ID
    used: set[str] = set()

    def _native_id(tx: CanonicalTx) -> str:
        """nexo_savings CSVのネイティブ Nexo トランザクションID（NXT...）を返す。
        なければ sha256 ベースの tx.id にフォールバックする。
        Summ に直接 Nexo CSV をインポートした場合、ネイティブ ID を使わないと
        両端の ID が一致しない。"""
        native = (tx.raw or {}).get("Transaction", "").strip()
        return native if native else tx.id

    for s in savings_sends:
        match = _best_match(nexo_deposits, used, s.sent_asset, s.sent_amount, s.timestamp)
        if match:
            id_remap[match.id] = _native_id(s)
            used.add(match.id)

    used.clear()
    for s in savings_recvs:
        match = _best_match(nexo_withdraws, used, s.received_asset, s.received_amount, s.timestamp)
        if match:
            id_remap[match.id] = _native_id(s)
            used.add(match.id)

    if not id_remap:
        return txs_list

    return [
        tx.model_copy(update={"id": id_remap[tx.id]}) if tx.id in id_remap else tx
        for tx in txs_list
    ]


def _fmt_decimal(v: Decimal | None) -> str:
    if v is None:
        return ""
    return format(v.normalize(), "f")


def _reward_type(tx: CanonicalTx) -> str:
    label = (tx.label or "").lower()
    if "stak" in label:
        return "staking"
    if "lend" in label or "interest" in label or "利息" in label:
        return "interest"
    return "income"


def _empty_row() -> dict[str, str]:
    return {h: "" for h in _SUMM_HEADERS}


def to_summ_rows(txs: Sequence[CanonicalTx]) -> list[dict[str, str]]:
    """CanonicalTx を SUMM カスタムCSV 行へ変換する。

    nexo_savings と nexo の両方が含まれる場合、内部振替の ID を自動統一する。
    """
    rows: list[dict[str, str]] = []

    txs = link_nexo_transfers(txs)

    for tx in txs:
        row = _empty_row()
        row["Timestamp (UTC)"] = tx.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        row["ID (Optional)"] = tx.id

        # 手数料（FEE 行以外で付与）
        if tx.type != TxType.FEE and tx.fee_asset and tx.fee_amount is not None:
            row["Fee Currency (Optional)"] = tx.fee_asset
            row["Fee Amount (Optional)"] = _fmt_decimal(tx.fee_amount)

        if tx.type == TxType.TRADE and tx.received_asset and tx.sent_asset \
                and tx.received_amount is not None and tx.sent_amount is not None:
            row["Type"] = "buy"
            row["Base Currency"] = tx.received_asset
            row["Base Amount"] = _fmt_decimal(tx.received_amount)
            row["Quote Currency (Optional)"] = tx.sent_asset
            row["Quote Amount (Optional)"] = _fmt_decimal(tx.sent_amount)

        elif tx.type == TxType.REWARD and tx.received_asset and tx.received_amount is not None:
            row["Type"] = _reward_type(tx)
            row["Base Currency"] = tx.received_asset
            row["Base Amount"] = _fmt_decimal(tx.received_amount)

        elif tx.type == TxType.FEE and tx.fee_asset and tx.fee_amount is not None:
            row["Type"] = "fee"
            row["Base Currency"] = tx.fee_asset
            row["Base Amount"] = _fmt_decimal(tx.fee_amount)

        elif tx.type == TxType.DEPOSIT and tx.received_asset and tx.received_amount is not None:
            row["Type"] = "fiat-deposit" if tx.received_asset.upper() in _FIAT else "receive"
            row["Base Currency"] = tx.received_asset
            row["Base Amount"] = _fmt_decimal(tx.received_amount)

        elif tx.type == TxType.WITHDRAW and tx.sent_asset and tx.sent_amount is not None:
            row["Type"] = "fiat-withdrawal" if tx.sent_asset.upper() in _FIAT else "send"
            row["Base Currency"] = tx.sent_asset
            row["Base Amount"] = _fmt_decimal(tx.sent_amount)

        elif tx.type == TxType.TRANSFER:
            if (tx.label or "").lower() in _INTERNAL_TRANSFER_LABELS:
                continue
            if tx.sent_asset and tx.sent_amount is not None:
                row["Type"] = "send"
                row["Base Currency"] = tx.sent_asset
                row["Base Amount"] = _fmt_decimal(tx.sent_amount)
            elif tx.received_asset and tx.received_amount is not None:
                row["Type"] = "receive"
                row["Base Currency"] = tx.received_asset
                row["Base Amount"] = _fmt_decimal(tx.received_amount)
            else:
                continue
        else:
            # 不完全なデータ（必須の Base を決められない）はスキップ
            continue

        rows.append(row)

    return rows


def to_summ_csv_string(txs: Sequence[CanonicalTx]) -> str:
    """SUMM カスタムCSV 文字列を返す。"""
    rows = to_summ_rows(txs)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_SUMM_HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_summ_csv(txs: Sequence[CanonicalTx], out_path: Path) -> int:
    """SUMM カスタムCSV を書き出す。書き出した行数を返す。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = to_summ_csv_string(txs)
    out_path.write_text(text, encoding="utf-8", newline="")
    return len(to_summ_rows(txs))
