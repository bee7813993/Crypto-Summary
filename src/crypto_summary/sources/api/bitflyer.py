"""bitFlyer Lightning Private API アダプタ

必要な API キー権限: 読み取り専用（資産残高・取引履歴）
  ✅ 資産残高を見る
  ✅ 取引履歴を見る
  ❌ 出金 / 送付 / 注文 は付与しないこと（セキュリティ要件）

環境変数 (`.env` または OS 環境変数):
  BITFLYER_API_KEY    : APIキー
  BITFLYER_API_SECRET : APIシークレット

取得エンドポイント:
  /v1/me/getexecutions  — 現物の約定履歴 (product_code 別)
  /v1/me/getdeposits    — JPY 入金
  /v1/me/getwithdrawals — JPY 出金
  /v1/me/getcoinins     — 暗号資産 入金
  /v1/me/getcoinouts    — 暗号資産 出金（送付）

ページング: `before` (ID) パラメータで逆順フェッチ、cursor (最終ID) で差分更新。
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

_BASE = "https://api.bitflyer.com"

# 取得する現物マーケット一覧
_SPOT_PRODUCTS = ["BTC_JPY", "ETH_JPY", "XRP_JPY", "BCH_JPY", "LTC_JPY", "MONA_JPY", "ETH_BTC"]

_FETCH_LIMIT = 500   # 1リクエストあたりの最大件数


def _parse_ts(value: str) -> datetime:
    # "2024-01-15T12:34:56.789" のような ISO 形式
    v = value.rstrip("Z").split(".")[0]
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


class BitflyerApiClient:
    """bitFlyer REST API クライアント（read-only）"""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._key = api_key
        self._secret = api_secret.encode()
        self._http = httpx.Client(base_url=_BASE, timeout=30)

    def _sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        ts = str(int(time.time()))
        msg = (ts + method + path + body).encode()
        sig = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return {
            "ACCESS-KEY":       self._key,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-SIGN":      sig,
        }

    def _get(self, path: str, params: dict | None = None) -> list[dict]:
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + qs
        headers = self._sign("GET", full_path)
        resp = self._http.get(path, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        raise ValueError(f"Unexpected response from {path}: {data!r}")

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Public fetch methods
    # ------------------------------------------------------------------

    def fetch_executions(
        self,
        product_code: str,
        after_id: int | None = None,
    ) -> list[dict]:
        """現物約定履歴を全件フェッチ（`after_id` より新しいものだけ）。

        bitFlyer の ID は新しいほど大きい。`before` で「IDより古い」を取得するため、
        最新から遡りながら `after_id` に達するまで繰り返す。
        """
        results: list[dict] = []
        before: int | None = None

        while True:
            params: dict = {"product_code": product_code, "count": _FETCH_LIMIT}
            if before is not None:
                params["before"] = before

            page = self._get("/v1/me/getexecutions", params)
            if not page:
                break

            for item in page:
                if after_id is not None and item["id"] <= after_id:
                    return results  # これより古いものは不要
                results.append(item)

            if len(page) < _FETCH_LIMIT:
                break  # 最後のページ
            before = page[-1]["id"]

        return results

    def fetch_deposits(self, after_id: int | None = None) -> list[dict]:
        """JPY 入金履歴"""
        return self._paginate("/v1/me/getdeposits", after_id)

    def fetch_withdrawals(self, after_id: int | None = None) -> list[dict]:
        """JPY 出金履歴"""
        return self._paginate("/v1/me/getwithdrawals", after_id)

    def fetch_coin_ins(self, after_id: int | None = None) -> list[dict]:
        """暗号資産 入金（受取）履歴"""
        return self._paginate("/v1/me/getcoinins", after_id)

    def fetch_coin_outs(self, after_id: int | None = None) -> list[dict]:
        """暗号資産 出金（送付）履歴"""
        return self._paginate("/v1/me/getcoinouts", after_id)

    def _paginate(self, path: str, after_id: int | None) -> list[dict]:
        results: list[dict] = []
        before: int | None = None

        while True:
            params: dict = {"count": _FETCH_LIMIT}
            if before is not None:
                params["before"] = before

            page = self._get(path, params)
            if not page:
                break

            for item in page:
                if after_id is not None and item["id"] <= after_id:
                    return results
                results.append(item)

            if len(page) < _FETCH_LIMIT:
                break
            before = page[-1]["id"]

        return results


# ---------------------------------------------------------------------------
# CanonicalTx 変換
# ---------------------------------------------------------------------------

def _product_to_assets(product_code: str) -> tuple[str, str]:
    """例: "BTC_JPY" → ("BTC", "JPY")"""
    parts = product_code.split("_")
    return parts[0].upper(), parts[1].upper()


def execution_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    """約定1件 → CanonicalTx (TRADE)"""
    side = item.get("side", "").upper()    # "BUY" / "SELL"
    if side not in ("BUY", "SELL"):
        return None

    ts = _parse_ts(item["exec_date"])
    product = item.get("product_code", "BTC_JPY")
    base, quote = _product_to_assets(product)

    filled = _d(item.get("size"))
    price  = _d(item.get("price"))
    fee    = _d(item.get("commission"))   # 手数料（負値）

    if not filled or not price:
        return None

    quote_amount = filled * price
    fee_amount   = abs(fee) if fee and fee != Decimal(0) else None

    raw_key = str(item["id"])

    if side == "BUY":
        return CanonicalTx(
            id=CanonicalTx.make_id(source_id, raw_key),
            source=source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=base,  received_amount=filled,
            sent_asset=quote,     sent_amount=quote_amount,
            fee_asset=quote if fee_amount else None, fee_amount=fee_amount,
            raw=item,
        )
    else:  # SELL
        return CanonicalTx(
            id=CanonicalTx.make_id(source_id, raw_key),
            source=source_id,
            timestamp=ts,
            type=TxType.TRADE,
            received_asset=quote, received_amount=quote_amount,
            sent_asset=base,      sent_amount=filled,
            fee_asset=quote if fee_amount else None, fee_amount=fee_amount,
            raw=item,
        )


def deposit_to_tx(item: dict, source_id: str, asset: str = "JPY") -> CanonicalTx | None:
    amount = _d(item.get("amount"))
    if not amount or amount <= 0:
        return None
    ts = _parse_ts(item["event_date"])
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"dep_{asset}_{item['id']}"),
        source=source_id,
        timestamp=ts,
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=amount,
        raw=item,
    )


def withdrawal_to_tx(item: dict, source_id: str, asset: str = "JPY") -> CanonicalTx | None:
    amount = _d(item.get("amount"))
    if not amount or amount <= 0:
        return None
    ts = _parse_ts(item["event_date"])
    fee = _d(item.get("fee"))
    fee_amount = abs(fee) if fee and fee != Decimal(0) else None
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"wd_{asset}_{item['id']}"),
        source=source_id,
        timestamp=ts,
        type=TxType.WITHDRAW,
        sent_asset=asset,
        sent_amount=amount,
        fee_asset=asset if fee_amount else None,
        fee_amount=fee_amount,
        raw=item,
    )


def coinin_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    asset  = item.get("currency_code", "").upper()
    amount = _d(item.get("amount"))
    if not asset or not amount or amount <= 0:
        return None
    ts = _parse_ts(item["event_date"])
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"ci_{asset}_{item['id']}"),
        source=source_id,
        timestamp=ts,
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=amount,
        tx_hash=item.get("transaction_id"),
        raw=item,
    )


def coinout_to_tx(item: dict, source_id: str) -> CanonicalTx | None:
    asset  = item.get("currency_code", "").upper()
    amount = _d(item.get("amount"))
    if not asset or not amount or amount <= 0:
        return None
    ts = _parse_ts(item["event_date"])
    fee = _d(item.get("fee"))
    fee_amount = abs(fee) if fee and fee != Decimal(0) else None
    return CanonicalTx(
        id=CanonicalTx.make_id(source_id, f"co_{asset}_{item['id']}"),
        source=source_id,
        timestamp=ts,
        type=TxType.WITHDRAW,
        sent_asset=asset,
        sent_amount=amount,
        fee_asset=asset if fee_amount else None,
        fee_amount=fee_amount,
        tx_hash=item.get("transaction_id"),
        raw=item,
    )


# ---------------------------------------------------------------------------
# 高レベル fetch（CLI から呼ぶ）
# ---------------------------------------------------------------------------

class BitflyerApiSource:
    """bitFlyer API から全履歴を取得して CanonicalTx リストを返す。"""

    def __init__(self, source_id: str, api_key: str, api_secret: str) -> None:
        self.source_id = source_id
        self._client = BitflyerApiClient(api_key, api_secret)

    def fetch_all(
        self,
        products: list[str] | None = None,
        after_exec_id: int | None = None,
        after_deposit_id: int | None = None,
        after_withdrawal_id: int | None = None,
        after_coinin_id: int | None = None,
        after_coinout_id: int | None = None,
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []

        # 現物取引
        for product in (products or _SPOT_PRODUCTS):
            for item in self._client.fetch_executions(product, after_exec_id):
                tx = execution_to_tx(item, self.source_id)
                if tx:
                    txs.append(tx)

        # JPY 入出金
        for item in self._client.fetch_deposits(after_deposit_id):
            tx = deposit_to_tx(item, self.source_id, "JPY")
            if tx:
                txs.append(tx)

        for item in self._client.fetch_withdrawals(after_withdrawal_id):
            tx = withdrawal_to_tx(item, self.source_id, "JPY")
            if tx:
                txs.append(tx)

        # 暗号資産 入出金
        for item in self._client.fetch_coin_ins(after_coinin_id):
            tx = coinin_to_tx(item, self.source_id)
            if tx:
                txs.append(tx)

        for item in self._client.fetch_coin_outs(after_coinout_id):
            tx = coinout_to_tx(item, self.source_id)
            if tx:
                txs.append(tx)

        self._client.close()
        return txs
