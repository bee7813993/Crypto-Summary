"""Helius Enhanced Transactions API アダプタ（Solana ウォレット用、read-only）

Helius の Enhanced Transactions API でパース済みの取引履歴を取得し
CanonicalTx に変換する。Solscan 無料プランでは取引履歴 API が使えないため
（有料 Lite プラン以上が必須）、無料枠で利用できる Helius を採用している。

API キー権限:
  ✅ 読み取りのみ（ブロックエクスプローラーデータは公開情報）
  ❌ 送金・出金権限は存在しない（秘密鍵と無関係）
  https://dev.helius.xyz で無料発行し、.env の HELIUS_API_KEY に設定。

取得エンドポイント:
  GET /v0/addresses/{address}/transactions
    — SOL / SPL トークンの転送がパース済みで返る（nativeTransfers / tokenTransfers）
  POST / (RPC getAssetBatch)
    — トークンミント → シンボル / 名称の解決（DAS API）

ページング:
  limit（最大 100 件）。降順で取得し、最後の signature を `before` に渡して次ページへ。
  取得件数が limit 未満になったら終了。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...core.models import CanonicalTx, TxType

_TX_BASE = "https://api.mainnet.helius-rpc.com/v0/addresses"
_RPC_BASE = "https://mainnet.helius-rpc.com/"
_LAMPORTS = Decimal(10) ** 9  # 1 SOL = 10^9 lamports
_ZERO = Decimal("0")
_DUST = Decimal("0.000001")
_PAGE_SIZE = 100
_RATE_LIMIT_SLEEP = 0.15   # Helius 無料枠 ≈ 10 req/s。余裕を見て送出する。
_MAX_PAGES = 500            # 100 件 × 500 ページ = 最大 50,000 件
_DAS_BATCH = 1000          # getAssetBatch は 1 リクエストで最大 1000 ミント

# Helius type → REWARD 判定（ステーキング報酬等）
_REWARD_HINT = "REWARD"

# よく使うミントは DAS 解決をスキップ（オフラインでも確実に名付けできる）
_KNOWN_MINTS: dict[str, str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "WSOL",
}

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


def _short_mint(mint: str) -> str:
    """シンボル未解決時の表示用にミントを短縮する（例: EPjFWd…TDt1v）。"""
    if not mint:
        return "UNKNOWN"
    return mint if len(mint) <= 11 else f"{mint[:6]}…{mint[-5:]}"


def _is_spam(symbol: str, name: str) -> bool:
    """スパム / フィッシングトークンを判定する。

    EVM 版と同様に:
    1. 非 ASCII 文字（Unicode ホモグラフ攻撃）
    2. URL・宣伝文句パターン
    """
    for text in (symbol, name):
        if not text:
            continue
        if not text.isascii():
            return True
        if _PHISHING_RE.search(text):
            return True
    return False


class HeliusApiSource:
    """Helius Enhanced Transactions API で Solana 取引履歴を取得するアダプタ。"""

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
        """全取引履歴を取得して CanonicalTx リストを返す。"""
        records = self._fetch_all_pages()
        mints = {
            tt.get("mint", "")
            for tx in records
            for tt in (tx.get("tokenTransfers") or [])
            if tt.get("mint")
        }
        meta = self._resolve_symbols(sorted(mints))
        return self._build(records, meta, record_gas)

    # ------------------------------------------------------------------
    # HTTP（テストでオーバーライド可能）
    # ------------------------------------------------------------------

    def _request(self, before: str | None) -> list[dict[str, Any]]:
        """1 ページ分の取引（降順）を HTTP で取得する。"""
        params: dict[str, Any] = {"api-key": self.api_key, "limit": _PAGE_SIZE}
        if before:
            params["before"] = before
        time.sleep(_RATE_LIMIT_SLEEP)
        resp = httpx.get(
            f"{_TX_BASE}/{self.wallet}/transactions",
            params=params,
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(
                "Helius API 認証エラー。\n"
                "  .env の HELIUS_API_KEY が正しいか確認してください。\n"
                "  発行: https://dev.helius.xyz"
            )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Helius API error: {data['error']}")
        return data if isinstance(data, list) else []

    def _resolve_symbols(self, mints: list[str]) -> dict[str, tuple[str, str]]:
        """ミント → (シンボル, 名称) を解決する。DAS getAssetBatch を使用。"""
        out: dict[str, tuple[str, str]] = {}
        unknown: list[str] = []
        for m in mints:
            if m in _KNOWN_MINTS:
                sym = _KNOWN_MINTS[m]
                out[m] = (sym, sym)
            elif m:
                unknown.append(m)

        for i in range(0, len(unknown), _DAS_BATCH):
            chunk = unknown[i:i + _DAS_BATCH]
            try:
                assets = self._das_get_asset_batch(chunk)
            except Exception:
                assets = []  # 解決失敗時は短縮ミントにフォールバック
            for a in assets:
                if not a:
                    continue
                mid = a.get("id")
                md = (a.get("content") or {}).get("metadata") or {}
                if mid:
                    out[mid] = (md.get("symbol") or "", md.get("name") or "")
        return out

    def _das_get_asset_batch(self, mints: list[str]) -> list[dict[str, Any]]:
        """DAS getAssetBatch RPC でミントのメタデータをまとめて取得する。"""
        time.sleep(_RATE_LIMIT_SLEEP)
        resp = httpx.post(
            _RPC_BASE,
            params={"api-key": self.api_key},
            json={
                "jsonrpc": "2.0",
                "id": "crypto-summary",
                "method": "getAssetBatch",
                "params": {"ids": mints},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("result") or []

    def _fetch_all_pages(self) -> list[dict[str, Any]]:
        """全ページを取得してまとめる（降順、signature カーソル）。"""
        all_records: list[dict[str, Any]] = []
        before: str | None = None
        for _ in range(_MAX_PAGES):
            batch = self._request(before)
            if not batch:
                break
            all_records.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break  # 最終ページ
            before = batch[-1].get("signature")
            if not before:
                break  # カーソルが取れない場合は打ち切り
        return all_records

    # ------------------------------------------------------------------
    # 分類ロジック
    # ------------------------------------------------------------------

    def _build(
        self,
        records: list[dict[str, Any]],
        meta: dict[str, tuple[str, str]],
        record_gas: bool,
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        for tx in records:
            txs.extend(self._process(tx, meta, record_gas))
        return txs

    def _process(
        self,
        tx: dict[str, Any],
        meta: dict[str, tuple[str, str]],
        record_gas: bool,
    ) -> list[CanonicalTx]:
        """1 トランザクションを CanonicalTx に変換する。"""
        sig = tx.get("signature", "")
        ts = datetime.fromtimestamp(int(tx.get("timestamp") or 0), tz=timezone.utc)
        htype = (tx.get("type") or "").upper()

        # ウォレットが fee payer のときのみガス代を計上する
        is_payer = tx.get("feePayer") == self.wallet
        fee_sol = _d(tx.get("fee", 0)) / _LAMPORTS

        # 資産ごとの正味フロー（+ = 受取 / − = 送出）を集計する。
        # nativeTransfers / tokenTransfers はプログラムレベルの実移動なので、
        # スワップの差額（おつり）も自動的に相殺される。
        flows: dict[str, Decimal] = {}

        for nt in tx.get("nativeTransfers") or []:
            amt = _d(nt.get("amount")) / _LAMPORTS
            if nt.get("toUserAccount") == self.wallet:
                flows["SOL"] = flows.get("SOL", _ZERO) + amt
            if nt.get("fromUserAccount") == self.wallet:
                flows["SOL"] = flows.get("SOL", _ZERO) - amt

        for tt in tx.get("tokenTransfers") or []:
            mint = tt.get("mint", "")
            sym, name = meta.get(mint, ("", ""))
            if _is_spam(sym, name):
                continue
            asset = sym or _short_mint(mint)
            amt = _d(tt.get("tokenAmount"))  # 既に小数調整済み
            if tt.get("toUserAccount") == self.wallet:
                flows[asset] = flows.get(asset, _ZERO) + amt
            if tt.get("fromUserAccount") == self.wallet:
                flows[asset] = flows.get(asset, _ZERO) - amt

        results: list[CanonicalTx] = []

        # ガス（オプション、fee payer のみ）
        if record_gas and is_payer and fee_sol > _ZERO:
            results.append(self._tx(
                sig + "|gas", ts, TxType.FEE,
                fee_asset="SOL", fee_amount=fee_sol, label="gas", tx_hash=sig,
            ))

        received = sorted(
            ((a, v) for a, v in flows.items() if v > _DUST), key=lambda x: x[0])
        sent = sorted(
            ((a, -v) for a, v in flows.items() if v < -_DUST), key=lambda x: x[0])

        if not received and not sent:
            return results  # Approve 等、正味の資産移動なし

        label = htype.lower() or "transfer"

        # ── 単一受取 ─────────────────────────────────────────────────
        if len(received) == 1 and not sent:
            a, v = received[0]
            ttype = TxType.REWARD if _REWARD_HINT in htype else TxType.DEPOSIT
            results.append(self._tx(sig, ts, ttype,
                received_asset=a, received_amount=v, label=label, tx_hash=sig))
            return results

        # ── 単一送出 ─────────────────────────────────────────────────
        if len(sent) == 1 and not received:
            a, v = sent[0]
            results.append(self._tx(sig, ts, TxType.WITHDRAW,
                sent_asset=a, sent_amount=v, label=label, tx_hash=sig))
            return results

        # ── 1 送出 + 1 受取 → スワップ（TRADE）──────────────────────
        if len(received) == 1 and len(sent) == 1:
            ra, rv = received[0]
            sa, sv = sent[0]
            results.append(self._tx(sig, ts, TxType.TRADE,
                sent_asset=sa, sent_amount=sv,
                received_asset=ra, received_amount=rv,
                label="swap", tx_hash=sig))
            return results

        # ── 複数送出のみ → LP 流動性追加 ────────────────────────────
        if sent and not received:
            for i, (a, v) in enumerate(sent):
                results.append(self._tx(sig + f"|o{i}", ts, TxType.TRANSFER,
                    sent_asset=a, sent_amount=v, label="lp_add", tx_hash=sig))
            return results

        # ── 複数受取のみ → LP 流動性撤退 ────────────────────────────
        if received and not sent:
            for i, (a, v) in enumerate(received):
                results.append(self._tx(sig + f"|i{i}", ts, TxType.TRANSFER,
                    received_asset=a, received_amount=v, label="lp_remove", tx_hash=sig))
            return results

        # ── 複合（複数送出 + 複数受取）→ 個別記録 ───────────────────
        for i, (a, v) in enumerate(received):
            results.append(self._tx(sig + f"|i{i}", ts, TxType.DEPOSIT,
                received_asset=a, received_amount=v, label="token_in", tx_hash=sig))
        for i, (a, v) in enumerate(sent):
            results.append(self._tx(sig + f"|o{i}", ts, TxType.WITHDRAW,
                sent_asset=a, sent_amount=v, label="token_out", tx_hash=sig))
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
