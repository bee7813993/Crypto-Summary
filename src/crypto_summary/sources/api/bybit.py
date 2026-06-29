"""Bybit V5 API アダプタ（read-only）

必要な API キー権限: 読み取り専用のみ
  ✅ Wallet（資産・入出金履歴の参照）
  ✅ Trade（約定履歴の参照） ※読み取り専用キーとして発行すること
  ❌ Withdraw / Transfer は絶対に付与しないこと（セキュリティ要件）

環境変数 (`.env` または OS 環境変数):
  BYBIT_API_KEY    : APIキー
  BYBIT_API_SECRET : APIシークレット

取得エンドポイント（V5・Unified Trading Account 前提）:
  /v5/execution/list            — 約定履歴（category=spot / linear / all）
  /v5/asset/deposit/query-record  — 入金履歴（category=linear 以外で取得）
  /v5/asset/withdraw/query-record — 出金履歴（category=linear 以外で取得）

  category="all" を指定すると spot + linear の約定を1回の登録で両方取得する。
  入出金はウォレット全体の操作で category に依存しないため、
  linear 単体登録のみスキップし二重取得を防ぐ。

認証（GET）:
  X-BAPI-API-KEY / X-BAPI-TIMESTAMP(ms) / X-BAPI-RECV-WINDOW / X-BAPI-SIGN
  sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + queryString)
ページング: result.nextPageCursor を cursor に渡して走査。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ...core.models import CanonicalTx, TxType

_BASE = "https://api.bybit.com"
_RECV_WINDOW = "5000"
_PAGE_LIMIT = 50          # 1ページあたりの取得件数（Bybit 最大 50〜100）
_MAX_PAGES = 1000         # 安全弁（無限ループ防止）

# シンボル分解に使う代表的な決済通貨（長いものから順にマッチ）
_QUOTE_ASSETS = [
    "USDT", "USDC", "USDD", "TUSD", "DAI",
    "BTC", "ETH", "EUR", "USD", "BRZ", "TRY", "PLN",
]


def _parse_ms(value: Any) -> datetime:
    """ミリ秒エポック（文字列/数値）→ UTC datetime。"""
    ms = int(value)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def split_symbol(symbol: str) -> tuple[str, str]:
    """"BTCUSDT" → ("BTC", "USDT")。判別不能なら (symbol, "")。"""
    s = symbol.upper()
    for q in sorted(_QUOTE_ASSETS, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, ""


# ---------------------------------------------------------------------------
# CanonicalTx 変換
# ---------------------------------------------------------------------------

def execution_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    """約定1件（spot）→ CanonicalTx (TRADE)。"""
    side = (item.get("side") or "").upper()      # "BUY" / "SELL"
    if side not in ("BUY", "SELL"):
        return None

    symbol = item.get("symbol", "")
    base, quote = split_symbol(symbol)
    if not quote:
        return None  # 決済通貨を判別できない場合はスキップ

    qty = _d(item.get("execQty"))
    price = _d(item.get("execPrice"))
    if not qty or price is None:
        return None

    # execValue があれば優先（数量×価格の丸め差を避ける）
    quote_amount = _d(item.get("execValue")) or (qty * price)

    fee = _d(item.get("execFee"))
    fee_amount = abs(fee) if fee and fee != Decimal(0) else None
    # feeCurrency があれば使用。spot は BUY→base, SELL→quote で徴収されるのが既定。
    fee_ccy = item.get("feeCurrency") or (base if side == "BUY" else quote)

    ts = _parse_ms(item.get("execTime"))
    raw_key = f"exec_{item.get('execId')}"

    if side == "BUY":
        return CanonicalTx(
            id=CanonicalTx.make_id(source_id, raw_key),
            source=source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=base, received_amount=qty,
            sent_asset=quote, sent_amount=quote_amount,
            fee_asset=fee_ccy if fee_amount else None, fee_amount=fee_amount,
            raw=item,
        )
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, raw_key),
        source=source_id,
        timestamp=ts,
        type=TxType.TRADE,
        received_asset=quote, received_amount=quote_amount,
        sent_asset=base, sent_amount=qty,
        fee_asset=fee_ccy if fee_amount else None, fee_amount=fee_amount,
        raw=item,
    )


def deposit_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    asset = (item.get("coin") or "").upper()
    amount = _d(item.get("amount"))
    if not asset or not amount or amount <= 0:
        return None
    ts_raw = item.get("successAt") or item.get("createTime") or item.get("updatedTime")
    if ts_raw is None:
        return None
    txid = item.get("txID") or item.get("txId") or ""
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"dep_{asset}_{txid}_{ts_raw}"),
        source=source_id,
        timestamp=_parse_ms(ts_raw),
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=amount,
        tx_hash=txid or None,
        raw=item,
    )


def withdrawal_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    asset = (item.get("coin") or "").upper()
    amount = _d(item.get("amount"))
    if not asset or not amount or amount <= 0:
        return None
    ts_raw = item.get("updateTime") or item.get("createTime")
    if ts_raw is None:
        return None
    fee = _d(item.get("withdrawFee"))
    fee_amount = abs(fee) if fee and fee != Decimal(0) else None
    wid = item.get("withdrawId") or item.get("txID") or ""
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"wd_{asset}_{wid}_{ts_raw}"),
        source=source_id,
        timestamp=_parse_ms(ts_raw),
        type=TxType.WITHDRAW,
        sent_asset=asset,
        sent_amount=amount,
        fee_asset=asset if fee_amount else None,
        fee_amount=fee_amount,
        tx_hash=(item.get("txID") or None),
        raw=item,
    )


# ---------------------------------------------------------------------------
# API ソース
# ---------------------------------------------------------------------------

class BybitApiSource:
    """Bybit V5 API から履歴を取得して CanonicalTx を返す（read-only）。"""

    # category="all" は内部で spot + linear を両方取得する仮想カテゴリ。
    # Bybit API に "all" は存在しないため fetch_executions で展開する。
    _EXEC_CATEGORIES = ("spot", "linear")

    def __init__(
        self,
        source_id: str,
        api_key: str,
        api_secret: str,
        *,
        category: str = "all",
        base_url: str = _BASE,
    ) -> None:
        self.source_id = source_id
        self.category = category
        self._key = api_key
        self._secret = api_secret.encode()
        self._http = httpx.Client(base_url=base_url, timeout=30)

    # -- 認証付き GET -------------------------------------------------------

    def _sign(self, ts: str, query_string: str) -> str:
        msg = (ts + self._key + _RECV_WINDOW + query_string).encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def _get(self, endpoint: str, params: dict) -> dict:
        """エンドポイントを叩き result(dict) を返す。retCode != 0 は例外。"""
        # クエリ文字列はキー昇順で安定化（署名と送信で一致させる）
        items = sorted((k, v) for k, v in params.items() if v is not None and v != "")
        qs = "&".join(f"{k}={v}" for k, v in items)
        ts = str(int(time.time() * 1000))
        headers = {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "X-BAPI-SIGN": self._sign(ts, qs),
        }
        url = endpoint + (f"?{qs}" if qs else "")
        resp = self._http.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            raise ValueError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result") or {}

    def _paginate(self, endpoint: str, params: dict) -> list[dict]:
        """nextPageCursor を辿って list 要素を全件集める。"""
        results: list[dict] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            result = self._get(endpoint, page_params)
            results.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return results

    # -- 高レベル fetch -----------------------------------------------------

    def fetch_executions(self, start_time_ms: int | None = None) -> list[dict]:
        """category="all" のときは spot + linear を順に取得して結合する。"""
        cats = self._EXEC_CATEGORIES if self.category == "all" else (self.category,)
        results: list[dict] = []
        for cat in cats:
            params: dict = {"category": cat, "limit": _PAGE_LIMIT}
            if start_time_ms is not None:
                params["startTime"] = start_time_ms
            results.extend(self._paginate("/v5/execution/list", params))
        return results

    def fetch_deposits(self, start_time_ms: int | None = None) -> list[dict]:
        params: dict = {"limit": _PAGE_LIMIT}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        return self._paginate("/v5/asset/deposit/query-record", params)

    def fetch_withdrawals(self, start_time_ms: int | None = None) -> list[dict]:
        params: dict = {"limit": _PAGE_LIMIT}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        return self._paginate("/v5/asset/withdraw/query-record", params)

    def fetch_all(self, start_time_ms: int | None = None) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []

        for item in self.fetch_executions(start_time_ms):
            tx = execution_to_tx(item, self.source_id)
            if tx:
                txs.append(tx)

        # 入出金はウォレット全体の操作で category に属さない。
        # linear 単体登録では取得せず、spot / all 登録でのみ取得して重複を防ぐ。
        if self.category != "linear":
            for item in self.fetch_deposits(start_time_ms):
                tx = deposit_to_tx(item, self.source_id)
                if tx:
                    txs.append(tx)

            for item in self.fetch_withdrawals(start_time_ms):
                tx = withdrawal_to_tx(item, self.source_id)
                if tx:
                    txs.append(tx)

        self.close()
        return txs

    def close(self) -> None:
        self._http.close()
