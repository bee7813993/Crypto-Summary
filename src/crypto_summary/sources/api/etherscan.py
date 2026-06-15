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

レート制限・ページング:
  無料枠は 5 calls/sec、かつ 2026-07-01 以降は 1 リクエスト最大 1,000 件に
  縮小される（https://docs.etherscan.io/changelog）。これに対応するため
  リクエスト間隔を空け、ブロック窓ページングで全件取得する。
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
# 無料枠レート制限: 5 calls/sec → 安全側に 0.21 秒間隔（約 4.7 req/s）。
_RATE_LIMIT_SLEEP = 0.21
# 1 リクエストあたりの最大取得件数。
# 2026-07-01 以降、無料枠は 10,000 → 1,000 件/リクエストに縮小されるため
# ブロック窓ページングで 1,000 件ずつ取得する。
# https://docs.etherscan.io/changelog
_PAGE_SIZE = 1000
# ページング暴走の上限（1,000 件 × 1,000 ページ = 100 万件で打ち切り）。
_MAX_PAGES = 1000


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
        """account モジュールの 1 アクションを全件取得する（ページング込み）。

        無料枠の 1,000 件/リクエスト上限を超える履歴に対応するため、
        ブロック窓ページングを行う。startblock を直前ページ最終ブロックに
        進めながら、1 ページ未満が返るまで繰り返し全件を集約する。

        page/offset 方式はページ深度に上限（合計 10,000 件）があるため使わず、
        startblock を進める方式で件数無制限に取得できるようにしている。
        境界ブロックのレコードは次ページと重複しうるため重複排除する。
        """
        all_records: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        startblock = 0
        for _ in range(_MAX_PAGES):
            page = self._request(action, startblock)
            if not page:
                break
            for rec in page:
                key = tuple(sorted(rec.items()))
                if key in seen:
                    continue
                seen.add(key)
                all_records.append(rec)
            if len(page) < _PAGE_SIZE:
                break  # 最終ページ
            last_block = int(page[-1].get("blockNumber", startblock) or startblock)
            if last_block <= startblock:
                # 同一ブロックに 1,000 件超 → これ以上ブロック窓を進められない。
                # 通常は起こり得ないため安全に打ち切る。
                break
            startblock = last_block
        return all_records

    def _request(self, action: str, startblock: int) -> list[dict[str, Any]]:
        """1 ページ分（最大 _PAGE_SIZE 件）を HTTP で取得する。"""
        params = {
            "chainid": self.chainid,
            "module": "account",
            "action": action,
            "address": self.wallet,
            "startblock": startblock,
            "endblock": 99999999,
            "page": 1,
            "offset": _PAGE_SIZE,
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
