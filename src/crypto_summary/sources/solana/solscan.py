"""Solscan Pro API v2.0 アダプタ（Solana ウォレット用、read-only）

Solscan Pro API で Solana ウォレットの転送履歴を取得し CanonicalTx に変換する。

API キー権限:
  ✅ 読み取りのみ（ブロックエクスプローラーデータは公開情報）
  ❌ 送金・出金権限は存在しない（秘密鍵と無関係）
  https://pro.solscan.io/api-pro で発行し、.env の SOLSCAN_API_KEY に設定。

取得エンドポイント:
  /account/transfer — SOL およびSPLトークンの転送履歴（送受金別）

ページング:
  page / page_size（最大 100 件/ページ）。取得件数が page_size 未満になったら終了。

レート制限:
  Solscan Pro 無料枠: 3 req/s。安全のため 0.4 秒間隔（2.5 req/s）で送出する。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...core.models import CanonicalTx, TxType

_BASE = "https://pro-api.solscan.io/v2.0"
_LAMPORTS = Decimal(10) ** 9  # 1 SOL = 10^9 lamports
_ZERO = Decimal("0")
_DUST = Decimal("0.000001")
_PAGE_SIZE = 100
_RATE_LIMIT_SLEEP = 0.4   # Solscan Pro 無料枠 ≈ 3 req/s
_MAX_PAGES = 500           # 100 件 × 500 ページ = 最大 50,000 件

# スパム判定: 非 ASCII 文字 or URL / 宣伝文句
_PHISHING_RE = re.compile(
    r"t\.me/|https?://|\.(com|io|net|org|xyz|top)/|"
    r"get\s+reward|claim\s+at|visit\s+|free\s*airdrop",
    re.IGNORECASE,
)


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return _ZERO


def _is_sol(r: dict) -> bool:
    """ネイティブ SOL 転送かどうか判定する。"""
    return r.get("activity_type") == "ACTIVITY_SYSTEM_TRANSFER" or not r.get("token_address")


def _token_sym(r: dict) -> str:
    """トークンシンボルを返す（大文字化）。SOL の場合は "SOL"。"""
    if _is_sol(r):
        return "SOL"
    return (r.get("token_symbol") or r.get("symbol") or "UNKNOWN").upper()


def _token_amt(r: dict) -> Decimal:
    """トークン量（人間可読 Decimal）を返す。"""
    raw = _d(r.get("amount", 0))
    if _is_sol(r):
        return raw / _LAMPORTS
    dec = int(r.get("token_decimals") or 0)
    return raw / (Decimal(10) ** dec) if dec > 0 else raw


def _is_spam(r: dict) -> bool:
    """スパム / フィッシングトークンを判定する。

    EVM と同様に:
    1. 非 ASCII 文字（Unicode ホモグラフ攻撃）
    2. URL・宣伝文句パターン
    """
    for field in ("token_symbol", "symbol", "token_name", "name"):
        text = r.get(field) or ""
        if not text:
            continue
        if not text.isascii():
            return True
        if _PHISHING_RE.search(text):
            return True
    return False


def _flow(r: dict, wallet: str) -> str:
    """転送の向きを "in" / "out" / "" で返す。

    API が flow フィールドを返す場合はそれを優先し、
    ない場合は from_address / to_address から導出する。
    """
    explicit = (r.get("flow") or "").lower()
    if explicit in ("in", "out"):
        return explicit
    if r.get("to_address") == wallet:
        return "in"
    if r.get("from_address") == wallet:
        return "out"
    return ""


class SolscanApiSource:
    """Solscan Pro API v2.0 で Solana ウォレット取引履歴を取得するアダプタ。"""

    def __init__(
        self,
        source_id: str,
        wallet_address: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        # Solana アドレスは base58 で大文字小文字を区別する（小文字化しない）
        self.source_id = source_id
        self.wallet = wallet_address
        self.api_key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fetch_all(self, record_gas: bool = True) -> list[CanonicalTx]:
        """全転送履歴を取得して CanonicalTx リストを返す。"""
        records = self._fetch_all_pages()
        return self._build(records, record_gas)

    # ------------------------------------------------------------------
    # HTTP（テストでオーバーライド可能）
    # ------------------------------------------------------------------

    def _request(self, page: int) -> list[dict[str, Any]]:
        """1 ページ分の転送記録を HTTP で取得する。"""
        params = {
            "address": self.wallet,
            "page": page,
            "page_size": _PAGE_SIZE,
            "sort_by": "block_time",
            "sort_order": "asc",
            "exclude_amount_zero": "true",
        }
        time.sleep(_RATE_LIMIT_SLEEP)
        resp = httpx.get(
            f"{_BASE}/account/transfer",
            params=params,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Solscan API error (page {page}): {data}")
        return data.get("data") or []

    def _fetch_all_pages(self) -> list[dict[str, Any]]:
        """全ページを取得してまとめる（重複シグネチャを dedup）。"""
        all_records: list[dict[str, Any]] = []
        seen_sigs: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            batch = self._request(page)
            if not batch:
                break
            for r in batch:
                sig = r.get("trans_id") or r.get("signature") or ""
                # 同一ページ内重複排除（API が同じ sig を複数行返す場合あり）
                # ※ 同一 sig 内の複数転送は保持する（残高計算に必要）
                all_records.append(r)
                seen_sigs.add(sig)
            if len(batch) < _PAGE_SIZE:
                break  # 最終ページ
        return all_records

    # ------------------------------------------------------------------
    # 分類ロジック
    # ------------------------------------------------------------------

    def _build(
        self, records: list[dict[str, Any]], record_gas: bool
    ) -> list[CanonicalTx]:
        """転送記録を tx_signature 単位でグループ化・分類する。"""
        grouped: dict[str, list[dict]] = {}
        for r in records:
            sig = r.get("trans_id") or r.get("signature") or ""
            grouped.setdefault(sig, []).append(r)

        txs: list[CanonicalTx] = []
        for sig, recs in grouped.items():
            # block_time でソート（API 順序に依存しない）
            recs_sorted = sorted(recs, key=lambda x: x.get("block_time", 0))
            txs.extend(self._process(sig, recs_sorted, record_gas))
        return txs

    def _process(
        self, sig: str, recs: list[dict], record_gas: bool
    ) -> list[CanonicalTx]:
        """1 トランザクション分の転送リストを CanonicalTx に変換する。"""
        if not recs:
            return []

        ts = datetime.fromtimestamp(recs[0]["block_time"], tz=timezone.utc)

        # ガス代（lamports → SOL）。API の fee は tx レベルなので先頭レコードを使う。
        fee_lam = _d(recs[0].get("fee", 0))
        fee_sol = fee_lam / _LAMPORTS

        # ウォレットが送信者（fee payer）かどうか: いずれかの転送で from = wallet
        wallet_is_sender = any(r.get("from_address") == self.wallet for r in recs)

        # SOL / トークン フロー集計（スパム・ダスト除去）
        sol_in = sol_out = _ZERO
        tok_recv: list[tuple[str, Decimal]] = []
        tok_sent: list[tuple[str, Decimal]] = []

        for r in recs:
            direction = _flow(r, self.wallet)
            if not direction:
                continue

            if _is_sol(r):
                amt = _token_amt(r)
                if amt <= _DUST:
                    continue
                if direction == "in":
                    sol_in += amt
                else:
                    sol_out += amt
            else:
                if _is_spam(r):
                    continue
                sym = _token_sym(r)
                amt = _token_amt(r)
                if amt <= _DUST:
                    continue
                if direction == "in":
                    tok_recv.append((sym, amt))
                else:
                    tok_sent.append((sym, amt))

        results: list[CanonicalTx] = []

        # ガス（オプション、送信者のみ）
        if record_gas and fee_sol > _ZERO and wallet_is_sender:
            results.append(self._tx(
                sig + "|gas", ts, TxType.FEE,
                fee_asset="SOL", fee_amount=fee_sol, label="gas", tx_hash=sig,
            ))

        has_sol = sol_in > _DUST or sol_out > _DUST
        has_tok = bool(tok_recv or tok_sent)

        if not has_sol and not has_tok:
            return results  # Approve 等、資産移動なし

        # ── 1. 純 SOL 受取 ──────────────────────────────────────────
        if sol_in > _DUST and not has_tok and sol_out <= _DUST:
            results.append(self._tx(sig, ts, TxType.DEPOSIT,
                received_asset="SOL", received_amount=sol_in,
                label="transfer_in", tx_hash=sig))
            return results

        # ── 2. 純 SOL 送出 ──────────────────────────────────────────
        if sol_out > _DUST and not has_tok and sol_in <= _DUST:
            results.append(self._tx(sig, ts, TxType.WITHDRAW,
                sent_asset="SOL", sent_amount=sol_out,
                label="transfer_out", tx_hash=sig))
            return results

        # ── 3. SOL → Token スワップ ──────────────────────────────────
        if sol_out > _DUST and len(tok_recv) == 1 and not tok_sent and sol_in <= _DUST:
            sym, amt = tok_recv[0]
            results.append(self._tx(sig, ts, TxType.TRADE,
                sent_asset="SOL", sent_amount=sol_out,
                received_asset=sym, received_amount=amt,
                label="swap", tx_hash=sig))
            return results

        # ── 4. Token → SOL スワップ ──────────────────────────────────
        if sol_in > _DUST and len(tok_sent) == 1 and not tok_recv and sol_out <= _DUST:
            sym, amt = tok_sent[0]
            results.append(self._tx(sig, ts, TxType.TRADE,
                sent_asset=sym, sent_amount=amt,
                received_asset="SOL", received_amount=sol_in,
                label="swap", tx_hash=sig))
            return results

        # ── 5. Token → Token スワップ ────────────────────────────────
        if len(tok_sent) == 1 and len(tok_recv) == 1 and not has_sol:
            ssym, samt = tok_sent[0]
            rsym, ramt = tok_recv[0]
            results.append(self._tx(sig, ts, TxType.TRADE,
                sent_asset=ssym, sent_amount=samt,
                received_asset=rsym, received_amount=ramt,
                label="swap", tx_hash=sig))
            return results

        # ── 6. 単一トークン受取 ──────────────────────────────────────
        if len(tok_recv) == 1 and not tok_sent and sol_out <= _DUST and sol_in <= _DUST:
            sym, amt = tok_recv[0]
            results.append(self._tx(sig, ts, TxType.DEPOSIT,
                received_asset=sym, received_amount=amt,
                label="token_in", tx_hash=sig))
            return results

        # ── 7. 単一トークン送出 ──────────────────────────────────────
        if len(tok_sent) == 1 and not tok_recv and sol_out <= _DUST and sol_in <= _DUST:
            sym, amt = tok_sent[0]
            results.append(self._tx(sig, ts, TxType.WITHDRAW,
                sent_asset=sym, sent_amount=amt,
                label="token_out", tx_hash=sig))
            return results

        # ── 8. LP 流動性追加（複数資産送出）─────────────────────────
        total_out = len(tok_sent) + (1 if sol_out > _DUST else 0)
        if total_out >= 2 and not tok_recv and sol_in <= _DUST:
            if sol_out > _DUST:
                results.append(self._tx(sig + "|s", ts, TxType.TRANSFER,
                    sent_asset="SOL", sent_amount=sol_out, label="lp_add", tx_hash=sig))
            for i, (sym, amt) in enumerate(tok_sent):
                results.append(self._tx(sig + f"|t{i}", ts, TxType.TRANSFER,
                    sent_asset=sym, sent_amount=amt, label="lp_add", tx_hash=sig))
            return results

        # ── 9. LP 流動性撤退（複数資産受取）─────────────────────────
        total_in = len(tok_recv) + (1 if sol_in > _DUST else 0)
        if total_in >= 2 and not tok_sent and sol_out <= _DUST:
            if sol_in > _DUST:
                results.append(self._tx(sig + "|r", ts, TxType.TRANSFER,
                    received_asset="SOL", received_amount=sol_in, label="lp_remove", tx_hash=sig))
            for i, (sym, amt) in enumerate(tok_recv):
                results.append(self._tx(sig + f"|t{i}", ts, TxType.TRANSFER,
                    received_asset=sym, received_amount=amt, label="lp_remove", tx_hash=sig))
            return results

        # ── フォールバック: 個別に記録 ───────────────────────────────
        if sol_in > _DUST:
            results.append(self._tx(sig + "|si", ts, TxType.DEPOSIT,
                received_asset="SOL", received_amount=sol_in, label="sol_in", tx_hash=sig))
        if sol_out > _DUST:
            results.append(self._tx(sig + "|so", ts, TxType.WITHDRAW,
                sent_asset="SOL", sent_amount=sol_out, label="sol_out", tx_hash=sig))
        for i, (sym, amt) in enumerate(tok_recv):
            results.append(self._tx(sig + f"|ri{i}", ts, TxType.DEPOSIT,
                received_asset=sym, received_amount=amt, label="token_in", tx_hash=sig))
        for i, (sym, amt) in enumerate(tok_sent):
            results.append(self._tx(sig + f"|ti{i}", ts, TxType.WITHDRAW,
                sent_asset=sym, sent_amount=amt, label="token_out", tx_hash=sig))
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
