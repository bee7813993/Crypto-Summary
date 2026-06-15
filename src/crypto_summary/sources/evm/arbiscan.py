"""Arbiscan / Etherscan EVM ウォレット取引アダプタ

Arbiscan (arbiscan.io) や Etherscan (etherscan.io) でエクスポートした
以下の CSV を tx_hash 単位でマージして CanonicalTx に変換する:

  1. Normal Transactions (必須)  : ETH 送受信・コントラクト呼び出し
  2. ERC-20 Token Txns  (推奨)  : トークン転送・スワップ内容
  3. Internal Transactions (任意): コントラクト内部 ETH 転送

判定ロジック:
  DEPOSIT  : 純 ETH 受取 / 単一トークン受取 (Claim含む)
  WITHDRAW : 純 ETH 送出 / 単一トークン送出
  TRADE    : ETH⇔Token / Token⇔Token スワップ・ETH Wrap
  TRANSFER : LP 流動性追加 (lp_add) / 撤退 (lp_remove) — 複数資産が同時移動
  FEE      : ガス代 (--record-gas 指定時のみ)

スパムトークン (ERC-20 TOKEN* / ERC20 ***) と失敗取引は自動スキップ。
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.models import CanonicalTx, TxType

_ZERO = Decimal("0")
_DUST = Decimal("0.000001")
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Arbiscan がマスクする未検証トークン名
_SPAM_RE = re.compile(r"ERC-?\d+\s+TOKEN\*|ERC\d*\s+\*+", re.IGNORECASE)


def _d(v: str) -> Decimal:
    v = v.strip().lstrip("$").replace(",", "")
    if not v or v in ("-", "N/A"):
        return _ZERO
    try:
        return Decimal(v)
    except InvalidOperation:
        return _ZERO


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), _DATE_FMT).replace(tzinfo=timezone.utc)


def _is_spam(token_name: str) -> bool:
    return bool(_SPAM_RE.search(token_name))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class ArbiscanCsvSource:
    """Arbiscan / Etherscan EVM ウォレット複数CSV アダプタ。

    import コマンドではなく import-wallet コマンドから呼び出す。
    """

    ZERO_ADDR = "0x0000000000000000000000000000000000000000"

    def __init__(self, source_id: str, wallet_address: str) -> None:
        self.source_id = source_id
        self.wallet = wallet_address.lower()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def load_multi(
        self,
        normal_path: Path,
        erc20_path: Path | None = None,
        internal_path: Path | None = None,
        record_gas: bool = False,
    ) -> list[CanonicalTx]:
        """3種の CSV を統合して CanonicalTx リストを返す。"""
        normal_rows = _read_csv(normal_path)
        erc20_rows = _read_csv(erc20_path) if erc20_path else []
        internal_rows = _read_csv(internal_path) if internal_path else []

        normal: dict[str, dict] = {
            r["Transaction Hash"].lower(): r for r in normal_rows
        }
        erc20: dict[str, list[dict]] = {}
        for r in erc20_rows:
            erc20.setdefault(r["Transaction Hash"].lower(), []).append(r)
        internal: dict[str, list[dict]] = {}
        for r in internal_rows:
            internal.setdefault(r["Transaction Hash"].lower(), []).append(r)

        all_hashes = sorted(set(normal) | set(erc20) | set(internal))
        txs: list[CanonicalTx] = []
        for h in all_hashes:
            txs.extend(
                self._process(
                    h, normal.get(h), erc20.get(h, []), internal.get(h, []), record_gas
                )
            )
        return txs

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _process(
        self,
        tx_hash: str,
        norm: dict | None,
        erc: list[dict],
        intern: list[dict],
        record_gas: bool,
    ) -> list[CanonicalTx]:

        # 失敗した取引（execution reverted）の扱い:
        #   ネイティブ ETH は移動せず（ガスのみ消費）→ ETH 値は無視する。
        #   ただし Arbiscan の ERC20 エクスポートに載るトークン転送は実際に
        #   成立しているため通常どおり処理する。ガスは失敗時も消費される。
        reverted = bool(norm and norm.get("ErrCode", "").strip())

        if norm:
            ts = _parse_ts(norm["DateTime (UTC)"])
        elif erc:
            ts = _parse_ts(erc[0]["DateTime (UTC)"])
        else:
            return []

        gas = _d(norm.get("TxnFee(ETH)", "0")) if norm else _ZERO
        if reverted:
            eth_in = eth_out = int_eth_in = _ZERO
        else:
            eth_in = _d(norm.get("Value_IN(ETH)", "0")) if norm else _ZERO
            eth_out = _d(norm.get("Value_OUT(ETH)", "0")) if norm else _ZERO
            int_eth_in = sum(_d(r.get("Value_IN(ETH)", "0")) for r in intern)
        method = (norm.get("Method", "") if norm else "").strip()

        # トークンフロー（スパム・ダスト除去）
        t_recv = [
            (r["TokenSymbol"].upper(), _d(r["TokenValue"].replace(",", "")))
            for r in erc
            if r["To"].lower() == self.wallet
            and not _is_spam(r.get("TokenName", ""))
        ]
        t_sent = [
            (r["TokenSymbol"].upper(), _d(r["TokenValue"].replace(",", "")))
            for r in erc
            if r["From"].lower() == self.wallet
            and not _is_spam(r.get("TokenName", ""))
        ]
        t_recv = [(s, a) for s, a in t_recv if a > _DUST]
        t_sent = [(s, a) for s, a in t_sent if a > _DUST]

        results: list[CanonicalTx] = []

        # ガス代（オプション）
        if record_gas and gas > _ZERO:
            results.append(self._tx(
                tx_hash + "|gas", ts, TxType.FEE,
                fee_asset="ETH", fee_amount=gas, label="gas", tx_hash=tx_hash,
            ))

        has_eth = eth_in > _DUST or eth_out > _DUST or int_eth_in > _DUST
        has_tok = bool(t_recv or t_sent)

        if not has_eth and not has_tok:
            return results  # Approve 等、資産移動なし

        # ── 1. 純粋 ETH 受取 ──────────────────────────────────────────
        if eth_in > _DUST and not has_tok and int_eth_in <= _DUST:
            results.append(self._tx(
                tx_hash, ts, TxType.DEPOSIT,
                received_asset="ETH", received_amount=eth_in,
                label="transfer_in", tx_hash=tx_hash,
            ))
            return results

        # ── 2. 純粋 ETH 送出 ──────────────────────────────────────────
        if eth_out > _DUST and not has_tok and int_eth_in <= _DUST:
            results.append(self._tx(
                tx_hash, ts, TxType.WITHDRAW,
                sent_asset="ETH", sent_amount=eth_out,
                label="transfer_out", tx_hash=tx_hash,
            ))
            return results

        # ── 3. ETH Wrap（ETH → WETH）────────────────────────────────
        # WETH が zero address から mint された場合
        weth_minted = any(
            r["From"].lower() == self.ZERO_ADDR
            and r["To"].lower() == self.wallet
            and r["TokenSymbol"].upper() == "WETH"
            for r in erc
        )
        if eth_out > _DUST and weth_minted:
            weth = next(((s, a) for s, a in t_recv if s == "WETH"), None)
            if weth:
                results.append(self._tx(
                    tx_hash, ts, TxType.TRADE,
                    sent_asset="ETH", sent_amount=eth_out,
                    received_asset=weth[0], received_amount=weth[1],
                    label="eth_wrap", tx_hash=tx_hash,
                ))
                return results

        # ── 4. ETH → Token スワップ ──────────────────────────────────
        if eth_out > _DUST and len(t_recv) == 1 and not t_sent:
            sym, amt = t_recv[0]
            results.append(self._tx(
                tx_hash, ts, TxType.TRADE,
                sent_asset="ETH", sent_amount=eth_out,
                received_asset=sym, received_amount=amt,
                label="swap", tx_hash=tx_hash,
            ))
            return results

        # ── 5. Token → ETH スワップ（内部 ETH 経由）─────────────────
        if len(t_sent) == 1 and int_eth_in > _DUST and not t_recv and eth_out <= _DUST:
            sym, amt = t_sent[0]
            results.append(self._tx(
                tx_hash, ts, TxType.TRADE,
                sent_asset=sym, sent_amount=amt,
                received_asset="ETH", received_amount=int_eth_in,
                label="swap", tx_hash=tx_hash,
            ))
            return results

        # ── 6. Token → Token スワップ ────────────────────────────────
        if len(t_sent) == 1 and len(t_recv) == 1 and eth_out <= _DUST and int_eth_in <= _DUST:
            ssym, samt = t_sent[0]
            rsym, ramt = t_recv[0]
            results.append(self._tx(
                tx_hash, ts, TxType.TRADE,
                sent_asset=ssym, sent_amount=samt,
                received_asset=rsym, received_amount=ramt,
                label="swap", tx_hash=tx_hash,
            ))
            return results

        # ── 7. LP 流動性追加（複数資産を送出）───────────────────────
        # ETH + トークン、またはトークン2種以上を同時送出
        total_out = len(t_sent) + (1 if eth_out > _DUST else 0)
        if total_out >= 2 and not t_recv and int_eth_in <= _DUST:
            if eth_out > _DUST:
                results.append(self._tx(
                    tx_hash + "|eth", ts, TxType.TRANSFER,
                    sent_asset="ETH", sent_amount=eth_out,
                    label="lp_add", tx_hash=tx_hash,
                ))
            for i, (sym, amt) in enumerate(t_sent):
                results.append(self._tx(
                    tx_hash + f"|t{i}", ts, TxType.TRANSFER,
                    sent_asset=sym, sent_amount=amt,
                    label="lp_add", tx_hash=tx_hash,
                ))
            return results

        # ── 8. LP 流動性撤退（複数資産を受取）───────────────────────
        total_in = len(t_recv) + (1 if int_eth_in > _DUST else 0)
        if total_in >= 2 and not t_sent and eth_out <= _DUST:
            if int_eth_in > _DUST:
                results.append(self._tx(
                    tx_hash + "|eth", ts, TxType.TRANSFER,
                    received_asset="ETH", received_amount=int_eth_in,
                    label="lp_remove", tx_hash=tx_hash,
                ))
            for i, (sym, amt) in enumerate(t_recv):
                results.append(self._tx(
                    tx_hash + f"|t{i}", ts, TxType.TRANSFER,
                    received_asset=sym, received_amount=amt,
                    label="lp_remove", tx_hash=tx_hash,
                ))
            return results

        # ── 9. 単一トークン受取（Claim / Reward / Deposit 等）────────
        if len(t_recv) == 1 and not t_sent and eth_out <= _DUST and int_eth_in <= _DUST:
            sym, amt = t_recv[0]
            tx_type = TxType.REWARD if "Claim" in method else TxType.DEPOSIT
            results.append(self._tx(
                tx_hash, ts, tx_type,
                received_asset=sym, received_amount=amt,
                label=method.lower().replace(" ", "_") or "token_in",
                tx_hash=tx_hash,
            ))
            return results

        # ── 10. 単一トークン送出（売却・DeFi預け入れ等）──────────────
        if len(t_sent) == 1 and not t_recv and eth_out <= _DUST and int_eth_in <= _DUST:
            sym, amt = t_sent[0]
            results.append(self._tx(
                tx_hash, ts, TxType.WITHDRAW,
                sent_asset=sym, sent_amount=amt,
                label="token_out", tx_hash=tx_hash,
            ))
            return results

        # ── フォールバック: 個別に記録（未分類の複合取引）──────────
        if eth_in > _DUST:
            results.append(self._tx(tx_hash + "|ei", ts, TxType.DEPOSIT,
                received_asset="ETH", received_amount=eth_in,
                label="eth_in", tx_hash=tx_hash))
        if eth_out > _DUST:
            results.append(self._tx(tx_hash + "|eo", ts, TxType.WITHDRAW,
                sent_asset="ETH", sent_amount=eth_out,
                label="eth_out", tx_hash=tx_hash))
        if int_eth_in > _DUST:
            results.append(self._tx(tx_hash + "|ie", ts, TxType.DEPOSIT,
                received_asset="ETH", received_amount=int_eth_in,
                label="internal_eth_in", tx_hash=tx_hash))
        for i, (sym, amt) in enumerate(t_recv):
            results.append(self._tx(tx_hash + f"|r{i}", ts, TxType.DEPOSIT,
                received_asset=sym, received_amount=amt,
                label="token_in", tx_hash=tx_hash))
        for i, (sym, amt) in enumerate(t_sent):
            results.append(self._tx(tx_hash + f"|s{i}", ts, TxType.WITHDRAW,
                sent_asset=sym, sent_amount=amt,
                label="token_out", tx_hash=tx_hash))

        return results

    def _tx(self, raw_key: str, ts: datetime, tx_type: TxType, **kw) -> CanonicalTx:
        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=tx_type,
            received_asset=kw.get("received_asset"),
            received_amount=kw.get("received_amount"),
            sent_asset=kw.get("sent_asset"),
            sent_amount=kw.get("sent_amount"),
            fee_asset=kw.get("fee_asset"),
            fee_amount=kw.get("fee_amount"),
            label=kw.get("label"),
            tx_hash=kw.get("tx_hash"),
            raw={},
        )
