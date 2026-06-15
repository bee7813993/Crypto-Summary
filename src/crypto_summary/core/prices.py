"""暗号資産・法定通貨の現在価格取得（CoinGecko、read-only）

CLI と Web UI の両方から使う共通ロジック。
- 法定通貨資産（USD/JPY/EUR/GBP）は為替として扱い、対象通貨と一致すれば 1.0。
- 連続実行による 429 を避けるため TTL 付きファイルキャッシュを使う。
- 429 が出た場合は指数バックオフで最大 3 回リトライする。
- CoinGecko ID 未登録 / 取得失敗の資産は結果から欠落する（呼び出し側で "-" 表示）。
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "WETH": "weth", "SOL": "solana",
    "MATIC": "matic-network", "POL": "matic-network",
    "USDC": "usd-coin", "USDT": "tether", "DAI": "dai", "BNB": "binancecoin",
    "ARB": "arbitrum", "OP": "optimism", "AVAX": "avalanche-2",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot",
    "LINK": "chainlink", "UNI": "uniswap", "AAVE": "aave",
    "NEXO": "nexo", "TRX": "tron", "LPT": "livepeer", "MONA": "monacoin",
}

# 法定通貨は CoinGecko の暗号資産IDではなく為替として扱う。
FIAT_ASSETS = frozenset({"USD", "JPY", "EUR", "GBP"})

SUPPORTED_CURRENCIES = ("USD", "JPY", "EUR", "GBP")

_PRICE_CACHE_TTL = 300  # 秒。連続実行時の 429 を避けるためのキャッシュ有効期間。

WarnFn = Callable[[str], None]


def _cache_path() -> Path:
    return Path.home() / ".crypto_summary_prices.json"


def _load_cache(currency: str) -> dict[str, Decimal] | None:
    """TTL 内のキャッシュ価格を返す（なければ None）。"""
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(currency.upper())
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > _PRICE_CACHE_TTL:
        return None
    return {k: Decimal(v) for k, v in entry.get("prices", {}).items()}


def _save_cache(currency: str, prices: dict[str, Decimal]) -> None:
    path = _cache_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data[currency.upper()] = {
        "ts": time.time(),
        "prices": {k: str(v) for k, v in prices.items()},
    }
    try:
        path.write_text(json.dumps(data))
    except OSError:
        pass


def fetch_prices(
    assets: list[str],
    currency: str,
    warn: WarnFn | None = None,
) -> dict[str, Decimal]:
    """指定資産の現在価格を currency 建てで返す（CoinGecko）。

    warn: 警告メッセージを受け取るコールバック（None なら無音）。
    """
    import httpx

    def _warn(msg: str) -> None:
        if warn:
            warn(msg)

    result: dict[str, Decimal] = {}

    # 法定通貨は為替として処理（対象通貨と同じなら等価）
    for asset in assets:
        if asset.upper() in FIAT_ASSETS and asset.upper() == currency.upper():
            result[asset.upper()] = Decimal("1")

    # CoinGecko ID にマップできる暗号資産だけ収集
    id_to_asset: dict[str, str] = {}
    for asset in assets:
        cg_id = COINGECKO_IDS.get(asset.upper())
        if cg_id and cg_id not in id_to_asset:
            id_to_asset[cg_id] = asset.upper()

    if not id_to_asset:
        return result

    # キャッシュ確認（必要な資産がすべて揃っていれば API を呼ばない）
    cached = _load_cache(currency)
    if cached is not None:
        needed = set(id_to_asset.values())
        if needed.issubset(cached.keys()):
            for a in needed:
                result[a] = cached[a]
            return result

    ids_str = ",".join(id_to_asset.keys())
    currency_lower = currency.lower()
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids_str}&vs_currencies={currency_lower}"
    )

    data = None
    for attempt in range(3):
        try:
            response = httpx.get(url, timeout=10)
            if response.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
                    continue
                _warn("CoinGecko のレート制限 (429) です。しばらく待って再実行してください。")
                return result
            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:  # noqa: BLE001 - ネットワーク全般を許容
            _warn(f"CoinGecko価格の取得に失敗しました: {e}")
            return result

    if data is None:
        return result

    fetched: dict[str, Decimal] = {}
    for cg_id, asset in id_to_asset.items():
        price = (data.get(cg_id) or {}).get(currency_lower)
        if price is not None:
            fetched[asset] = Decimal(str(price))

    if fetched:
        merged = _load_cache(currency) or {}
        merged.update(fetched)
        _save_cache(currency, merged)
        result.update(fetched)

    return result
