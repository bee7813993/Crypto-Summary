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

# 口座表示名とグルーピング設定。
# キー: 表示名 / 値: ソースIDのリスト（複数なら合算表示）
# ここにないソースIDは source_id をタイトルケースに変換して表示する。
ACCOUNT_GROUPS: dict[str, list[str]] = {
    "bitFlyer": ["bitflyer"],
    "Nexo Pro": ["nexo_dnw", "nexo_spot"],
}


def _display_name(source_id: str) -> str:
    for name, ids in ACCOUNT_GROUPS.items():
        if source_id in ids:
            return name
    return source_id.replace("_", " ").title()


def _group_to_source_ids() -> dict[str, list[str]]:
    """表示名 → ソースIDリスト（ACCOUNT_GROUPS + 未登録ソースのフォールバック）を返す。"""
    return {name: ids for name, ids in ACCOUNT_GROUPS.items()}


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
    """口座（グルーピング済み）ごとの評価額内訳を返す。"""
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

    # グループ名 → 合算残高・取引数を集計
    group_bals: dict[str, dict[str, Decimal]] = {}
    group_tx: dict[str, int] = {}
    group_ids: dict[str, list[str]] = {}

    for src in sorted(per_source):
        name = _display_name(src)
        if name not in group_bals:
            group_bals[name] = {}
            group_tx[name] = 0
            group_ids[name] = []
        group_ids[name].append(src)
        group_tx[name] += counts.get(src, 0)
        for asset, bal in per_source[src].items():
            prev = group_bals[name].get(asset, Decimal("0"))
            group_bals[name][asset] = prev + bal

    sources = []
    for name, bals in group_bals.items():
        bals = {a: v for a, v in bals.items() if abs(v) >= _DUST}
        total = Decimal("0")
        for asset, balance in bals.items():
            price = prices.get(asset.upper())
            if price is not None:
                total += balance * price
        sources.append({
            "source": name,
            "source_ids": group_ids[name],
            "tx_count": group_tx[name],
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


def _account_assets(account: str, db_path: str, currency: str) -> dict:
    """指定口座（グループ）の資産内訳を返す。"""
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
    finally:
        ledger.close()

    # 口座名に対応するソースIDを収集
    target_ids: set[str] = set()
    for src in per_source:
        if _display_name(src) == account:
            target_ids.add(src)

    if not target_ids:
        return {"currency": currency, "account": account, "assets": [], "total_value": "0"}

    # 合算残高
    merged: dict[str, Decimal] = {}
    for src in target_ids:
        for asset, bal in per_source[src].items():
            merged[asset] = merged.get(asset, Decimal("0")) + bal
    merged = {a: v for a, v in merged.items() if abs(v) >= _DUST}

    warnings: list[str] = []
    prices = fetch_prices(sorted(merged.keys()), currency, warn=warnings.append)

    assets = []
    total = Decimal("0")
    for asset in sorted(merged):
        balance = merged[asset]
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

    assets.sort(
        key=lambda a: (a["value"] is None, -(Decimal(a["value"]) if a["value"] else Decimal("0"))),
    )

    return {
        "currency": currency,
        "account": account,
        "assets": assets,
        "total_value": str(total),
        "warnings": warnings,
    }


def _asset_accounts(asset: str, db_path: str, currency: str) -> dict:
    """指定資産の口座別内訳を返す。"""
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
    finally:
        ledger.close()

    warnings: list[str] = []
    prices = fetch_prices([asset], currency, warn=warnings.append)
    price = prices.get(asset.upper())

    # グループ名 → 合算残高
    group_bals: dict[str, Decimal] = {}
    for src, bals in per_source.items():
        bal = bals.get(asset, Decimal("0"))
        if abs(bal) < _DUST:
            continue
        name = _display_name(src)
        group_bals[name] = group_bals.get(name, Decimal("0")) + bal

    accounts = []
    total_balance = Decimal("0")
    total_value = Decimal("0")
    for name, balance in sorted(group_bals.items(), key=lambda x: -abs(x[1])):
        value = (balance * price) if price is not None else None
        if value is not None:
            total_value += value
        total_balance += balance
        accounts.append({
            "account": name,
            "balance": str(balance),
            "value": (str(value) if value is not None else None),
        })

    return {
        "currency": currency,
        "asset": asset,
        "price": (str(price) if price is not None else None),
        "accounts": accounts,
        "total_balance": str(total_balance),
        "total_value": str(total_value),
        "warnings": warnings,
    }


_TX_TYPE_JA: dict[str, str] = {
    "trade": "売買",
    "deposit": "入金",
    "withdraw": "出金",
    "fee": "手数料",
    "reward": "報酬",
    "transfer": "振替",
}

_TX_PAGE_SIZE = 50


def _transactions(
    db_path: str,
    account: str | None,
    asset: str | None,
    page: int,
) -> dict:
    """取引履歴ページを返す。account は表示名（グループ名）で受け取る。"""
    # 表示名 → ソースIDリストに変換
    source_ids: list[str] | None = None
    if account:
        ledger_tmp = Ledger(db_path)
        try:
            all_source_ids = [src for src, *_ in ledger_tmp.sources()]
        finally:
            ledger_tmp.close()
        mapped = [s for s in all_source_ids if _display_name(s) == account]
        source_ids = mapped if mapped else [account]  # フォールバック

    offset = (max(page, 1) - 1) * _TX_PAGE_SIZE
    ledger = Ledger(db_path)
    try:
        txs, total = ledger.transactions(
            source=source_ids,
            asset=asset,
            limit=_TX_PAGE_SIZE,
            offset=offset,
        )
    finally:
        ledger.close()

    rows = []
    for tx in txs:
        rows.append({
            "id": tx.id,
            "timestamp": tx.timestamp.isoformat(),
            "account": _display_name(tx.source),
            "source_id": tx.source,
            "type": tx.type.value,
            "type_ja": _TX_TYPE_JA.get(tx.type.value, tx.type.value),
            "received_asset": tx.received_asset,
            "received_amount": str(tx.received_amount) if tx.received_amount is not None else None,
            "sent_asset": tx.sent_asset,
            "sent_amount": str(tx.sent_amount) if tx.sent_amount is not None else None,
            "fee_asset": tx.fee_asset,
            "fee_amount": str(tx.fee_amount) if tx.fee_amount is not None else None,
            "label": tx.label,
            "tx_hash": tx.tx_hash,
        })

    total_pages = max(1, (total + _TX_PAGE_SIZE - 1) // _TX_PAGE_SIZE)
    return {
        "transactions": rows,
        "total": total,
        "page": max(page, 1),
        "total_pages": total_pages,
        "page_size": _TX_PAGE_SIZE,
        "filter_account": account,
        "filter_asset": asset,
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

    @app.get("/api/account-assets")
    def account_assets(account: str = Query(...), currency: str = Query("USD")) -> dict:
        return _account_assets(account, db_path, currency)

    @app.get("/api/asset-accounts")
    def asset_accounts(asset: str = Query(...), currency: str = Query("USD")) -> dict:
        return _asset_accounts(asset, db_path, currency)

    @app.get("/api/transactions")
    def transactions_api(
        account: str | None = Query(None),
        asset: str | None = Query(None),
        page: int = Query(1),
    ) -> dict:
        return _transactions(db_path, account, asset, page)

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
