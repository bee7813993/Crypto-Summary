"""Etherscan V2 マルチチェーン API アダプタ（read-only）

Etherscan V2 API は単一の API キー + `chainid` パラメータで Ethereum / Arbitrum /
Polygon / Base / Optimism など 50+ チェーンの取引履歴を取得できる。
CSV のダウンロードが不要になり、ウォレットアドレスだけで自動取得できる。

必要な API キー権限:
  ✅ 読み取りのみ（ブロックエクスプローラーのデータは公開情報）
  ❌ 出金・送金権限という概念は存在しない（鍵の流出リスクが低い）
  api キーは https://etherscan.io/myapikey で無料発行。
  .env の ETHERSCAN_API_KEY か OS 環境変数で渡し、リポジトリには含めないこと。

取得アクション（module=account）:
  txlist          — 通常トランザクション（ETH 送受信・コントラクト呼び出し）
  txlistinternal  — 内部トランザクション（コントラクト内 ETH 転送）
  tokentx         — ERC-20 転送（スワップ内容）

API の JSON を Arbiscan CSV と同じ列名の dict に変換し、ArbiscanCsvSource の
分類ロジック（_build）をそのまま再利用する。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ...core.models import CanonicalTx
from ..evm.arbiscan import ArbiscanCsvSource

_BASE = "https://api.etherscan.io/v2/api"

# CLI のチェーン名 → Etherscan V2 chainid
CHAIN_IDS: dict[str, int] = {
    "ethereum": 1,
    "arbitrum": 42161,
    "polygon": 137,
    "base": 8453,
    "optimism": 10,
}

_WEI = Decimal(10) ** 18
_RATE_LIMIT_SLEEP = 0.25  # 無料枠は 5 req/sec


def _ts(unix: str) -> str:
    """Unix 秒 → Arbiscan CSV と同じ "YYYY-MM-DD HH:MM:SS" (UTC)。"""
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _method_name(rec: dict[str, Any]) -> str:
    """functionName ("claimTo(address)") から呼び出しメソッド名を取り出す。"""
    fn = (rec.get("functionName") or "").strip()
    if fn:
        return fn.split("(")[0]
    return (rec.get("methodId") or "").strip()


class EtherscanApiSource(ArbiscanCsvSource):
    """Etherscan V2 API で EVM ウォレットの取引履歴を取得するアダプタ。"""

    def __init__(
        self,
        source_id: str,
        wallet_address: str,
        api_key: str,
        chainid: int,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(source_id, wallet_address)
        self.api_key = api_key
        self.chainid = chainid
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fetch_all(self, record_gas: bool = True) -> list[CanonicalTx]:
        """3 アクションを取得して CanonicalTx リストを返す。"""
        normal = self._to_normal_rows(self._get("txlist"))
        internal = self._to_internal_rows(self._get("txlistinternal"))
        erc20 = self._to_erc20_rows(self._get("tokentx"))
        return self._build(normal, erc20, internal, record_gas)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, action: str) -> list[dict[str, Any]]:
        """account モジュールの 1 アクションを取得（結果リストを返す）。"""
        params = {
            "chainid": self.chainid,
            "module": "account",
            "action": action,
            "address": self.wallet,
            "startblock": 0,
            "endblock": 99999999,
            "sort": "asc",
            "apikey": self.api_key,
        }
        time.sleep(_RATE_LIMIT_SLEEP)
        resp = httpx.get(_BASE, params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        # status="0" は「取引なし」(message="No transactions found") かエラー
        if data.get("status") != "1":
            if isinstance(result, list):
                return []
            # result が文字列ならエラーメッセージ
            raise RuntimeError(f"Etherscan API error ({action}): {result}")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # JSON → CSV列名 dict 変換
    # ------------------------------------------------------------------

    def _to_normal_rows(self, recs: list[dict]) -> list[dict[str, str]]:
        rows = []
        for r in recs:
            value = Decimal(r.get("value", "0")) / _WEI
            to = (r.get("to") or "").lower()
            frm = (r.get("from") or "").lower()
            gas_used = Decimal(r.get("gasUsed", "0"))
            gas_price = Decimal(r.get("gasPrice", "0"))
            fee = gas_used * gas_price / _WEI
            rows.append({
                "Transaction Hash": r.get("hash", ""),
                "DateTime (UTC)": _ts(r.get("timeStamp", "0")),
                "From": frm,
                "To": to,
                "Value_IN(ETH)": str(value) if to == self.wallet else "0",
                "Value_OUT(ETH)": str(value) if frm == self.wallet else "0",
                "TxnFee(ETH)": str(fee),
                "ErrCode": "execution reverted" if r.get("isError") == "1" else "",
                "Method": _method_name(r),
            })
        return rows

    def _to_internal_rows(self, recs: list[dict]) -> list[dict[str, str]]:
        rows = []
        for r in recs:
            if r.get("isError") == "1":
                continue  # 失敗した内部呼び出しは ETH 移動なし
            value = Decimal(r.get("value", "0")) / _WEI
            to = (r.get("to") or "").lower()
            frm = (r.get("from") or "").lower()
            rows.append({
                "Transaction Hash": r.get("hash", ""),
                "DateTime (UTC)": _ts(r.get("timeStamp", "0")),
                "From": frm,
                "TxTo": to,
                "Value_IN(ETH)": str(value) if to == self.wallet else "0",
                "Value_OUT(ETH)": str(value) if frm == self.wallet else "0",
            })
        return rows

    def _to_erc20_rows(self, recs: list[dict]) -> list[dict[str, str]]:
        rows = []
        for r in recs:
            try:
                dec = int(r.get("tokenDecimal") or 0)
            except ValueError:
                dec = 0
            raw = Decimal(r.get("value", "0"))
            amount = raw / (Decimal(10) ** dec) if dec else raw
            rows.append({
                "Transaction Hash": r.get("hash", ""),
                "DateTime (UTC)": _ts(r.get("timeStamp", "0")),
                "From": (r.get("from") or "").lower(),
                "To": (r.get("to") or "").lower(),
                "TokenValue": str(amount),
                "ContractAddress": (r.get("contractAddress") or "").lower(),
                "TokenName": r.get("tokenName") or "",
                "TokenSymbol": r.get("tokenSymbol") or "",
            })
        return rows
