"""Crypto-Summary Web UI のFastAPIアプリ。

ダッシュボード（資産サマリー）を提供する。価格は CoinGecko（read-only）から取得。
取引履歴のインポート機能は今後追加予定。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core.ledger import Ledger
from ..core.prices import SUPPORTED_CURRENCIES, fetch_prices

_STATIC_DIR = Path(__file__).parent / "static"
_DUST = Decimal("0.00000001")


def _summary(db_path: str, currency: str) -> dict:
    """全ソース合算の資産サマリーを計算して返す。"""
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    ledger = Ledger(db_path)
    try:
        bals = ledger.balances()
    finally:
        ledger.close()

    # ダスト除去（±0.00000001 未満）
    bals = {a: v for a, v in bals.items() if abs(v) >= _DUST}

    warnings: list[str] = []
    prices = fetch_prices(list(bals.keys()), currency, warn=warnings.append)

    assets = []
    total = Decimal("0")
    for asset in sorted(bals):
        balance = bals[asset]
        price = prices.get(asset.upper())
        value = (balance * price) if price is not None else None
        if value is not None:
            total += value
        assets.append({
            "asset": asset,
            "balance": str(balance),
            "price": (str(price) if price is not None else None),
            "value": (str(value) if value is not None else None),
            "has_price": price is not None,
        })

    # 評価額の大きい順（価格なしは末尾）に並べ替え
    assets.sort(
        key=lambda a: (a["value"] is None, -(Decimal(a["value"]) if a["value"] else Decimal("0"))),
    )

    priced = [a for a in assets if a["has_price"]]
    unpriced = [a["asset"] for a in assets if not a["has_price"]]

    return {
        "currency": currency,
        "total_value": str(total),
        "asset_count": len(assets),
        "priced_count": len(priced),
        "unpriced": unpriced,
        "assets": assets,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sources(db_path: str, currency: str) -> dict:
    """ソース（口座）ごとの評価額内訳を返す。"""
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
        counts = {src: cnt for src, cnt, _ in ledger.sources()}
    finally:
        ledger.close()

    all_assets = {a for bals in per_source.values() for a in bals}
    warnings: list[str] = []
    prices = fetch_prices(sorted(all_assets), currency, warn=warnings.append)

    sources = []
    for src in sorted(per_source):
        bals = {a: v for a, v in per_source[src].items() if abs(v) >= _DUST}
        total = Decimal("0")
        for asset, balance in bals.items():
            price = prices.get(asset.upper())
            if price is not None:
                total += balance * price
        sources.append({
            "source": src,
            "tx_count": counts.get(src, 0),
            "asset_count": len(bals),
            "total_value": str(total),
        })

    sources.sort(key=lambda s: -Decimal(s["total_value"]))

    return {
        "currency": currency,
        "sources": sources,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def create_app(db_path: str = "ledger.db") -> FastAPI:
    """FastAPI アプリを生成する。db_path はクロージャで束縛する。"""
    app = FastAPI(title="Crypto-Summary", docs_url="/api/docs")

    @app.get("/api/summary")
    def summary(currency: str = Query("USD")) -> dict:
        return _summary(db_path, currency)

    @app.get("/api/sources")
    def sources(currency: str = Query("USD")) -> dict:
        return _sources(db_path, currency)

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "currencies": list(SUPPORTED_CURRENCIES),
            "db_path": str(db_path),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
