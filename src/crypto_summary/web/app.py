"""Crypto-Summary Web UI のFastAPIアプリ。

ダッシュボード（資産サマリー）を提供する。価格は CoinGecko（read-only）から取得。
取引履歴のインポート機能は今後追加予定。
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..core.ledger import Ledger
from ..core.models import CanonicalTx, TxType
from ..core.portfolio import assets_in_range, daily_balances
from ..core.price_history import fetch_price_history
from ..core.prices import COINGECKO_IDS, SUPPORTED_CURRENCIES, _coingecko_api_key, fetch_coin_icons, fetch_prices
from ..core.secrets import SecretStore, SecretStoreError
from ..sinks.cryptact_csv import to_cryptact_csv_string
from ..sinks.koinly_csv import to_koinly_csv_string
from ..sinks.summ_csv import to_summ_csv_string
from ..sources.api.bybit import BybitApiSource
from ..sources.csv_import import EXCHANGE_SOURCES

_STATIC_DIR = Path(__file__).parent / "static"
_DUST = Decimal("0.00000001")
# スパムエアドロップ判定：価格不明 ＋ 正の整数単位 ＋ 少量（≤10）
_SPAM_MAX_UNITS = Decimal("10")


def _is_spam_token(balance: Decimal, price: "Decimal | None") -> bool:
    """価格不明かつ少量整数残高のトークンをスパムエアドロップと見なす。"""
    if price is not None:
        return False
    if balance <= 0:
        return False
    return balance <= _SPAM_MAX_UNITS and balance == balance.to_integral_value()

# デフォルトのグルーピング設定（設定ファイルが存在しない場合に使われる）。
# ユーザーは Web UI から変更でき、DB と同じディレクトリの .accounts.json に保存される。
ACCOUNT_GROUPS: dict[str, list[str]] = {
    "bitFlyer": ["bitflyer"],
    "Nexo Pro": ["nexo", "nexo_dnw", "nexo_spot"],
}

# メモリキャッシュ（db_path → groups）。PUT 時に無効化する。
_groups_cache: dict[str, dict[str, list[str]]] = {}


def _groups_path(db_path: str) -> Path:
    """DB ファイルと同じディレクトリに <stem>.accounts.json を置く。"""
    p = Path(db_path)
    return p.with_name(p.stem + ".accounts.json")


def _load_groups(db_path: str) -> dict[str, list[str]]:
    """設定ファイルからグループを読み込む（キャッシュあり）。

    保存済みファイルを読み込んだ後、ACCOUNT_GROUPS に定義されているが
    まだどのグループにも割り当てられていない source_id をデフォルト設定で補完する。
    これにより、コードのデフォルト追加が既存ユーザーにも反映される。
    """
    if db_path in _groups_cache:
        return _groups_cache[db_path]
    path = _groups_path(db_path)
    try:
        groups: dict[str, list[str]] = json.loads(path.read_text(encoding="utf-8"))
        # デフォルトに新たに追加されたグループエントリを補完する
        assigned = {sid for ids in groups.values() for sid in ids}
        for name, ids in ACCOUNT_GROUPS.items():
            for sid in ids:
                if sid not in assigned:
                    groups.setdefault(name, []).append(sid)
    except (OSError, json.JSONDecodeError):
        groups = dict(ACCOUNT_GROUPS)
    _groups_cache[db_path] = groups
    return groups


def _save_groups(db_path: str, groups: dict[str, list[str]]) -> None:
    """グループ設定をファイルに保存してキャッシュを更新する。"""
    _groups_cache[db_path] = groups
    _groups_path(db_path).write_text(
        json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _display_name(source_id: str, groups: dict[str, list[str]]) -> str:
    for name, ids in groups.items():
        if source_id in ids:
            return name
    # 保存済み設定に含まれていない場合はビルトインのデフォルトで確認
    for name, ids in ACCOUNT_GROUPS.items():
        if source_id in ids:
            return name
    return source_id.replace("_", " ").title()


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

    # スパムエアドロップを除外
    assets = [
        a for a in assets
        if not _is_spam_token(Decimal(a["balance"]), prices.get(a["asset"].upper()))
    ]

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

    groups = _load_groups(db_path)

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
        counts = {src: cnt for src, cnt, _ in ledger.sources()}
        date_ranges = ledger.date_ranges_by_source()
    finally:
        ledger.close()

    all_assets = {a for bals in per_source.values() for a in bals}
    warnings: list[str] = []
    prices = fetch_prices(sorted(all_assets), currency, warn=warnings.append)

    group_bals: dict[str, dict[str, Decimal]] = {}
    group_tx: dict[str, int] = {}
    group_ids: dict[str, list[str]] = {}
    group_first: dict[str, str] = {}
    group_last: dict[str, str] = {}

    for src in sorted(per_source):
        name = _display_name(src, groups)
        if name not in group_bals:
            group_bals[name] = {}
            group_tx[name] = 0
            group_ids[name] = []
        group_ids[name].append(src)
        group_tx[name] += counts.get(src, 0)
        rng = date_ranges.get(src)
        if rng:
            lo, hi = rng
            if name not in group_first or lo < group_first[name]:
                group_first[name] = lo
            if name not in group_last or hi > group_last[name]:
                group_last[name] = hi
        for asset, bal in per_source[src].items():
            prev = group_bals[name].get(asset, Decimal("0"))
            group_bals[name][asset] = prev + bal

    sources = []
    for name, bals in group_bals.items():
        bals = {
            a: v for a, v in bals.items()
            if abs(v) >= _DUST and not _is_spam_token(v, prices.get(a.upper()))
        }
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
            "first_ts": group_first.get(name),
            "last_ts": group_last.get(name),
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

    groups = _load_groups(db_path)

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
    finally:
        ledger.close()

    target_ids: set[str] = set()
    for src in per_source:
        if _display_name(src, groups) == account:
            target_ids.add(src)

    if not target_ids:
        return {"currency": currency, "account": account, "assets": [], "total_value": "0"}

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

    # スパムエアドロップを除外
    assets = [
        a for a in assets
        if not _is_spam_token(Decimal(a["balance"]), prices.get(a["asset"].upper()))
    ]

    assets.sort(
        key=lambda a: (a["value"] is None, -(Decimal(a["value"]) if a["value"] else Decimal("0"))),
    )

    store = SecretStore(db_path)
    wallet_map = {w["source_id"]: w for w in store.list_wallets()}
    account_wallets = [
        {
            "source_id": sid,
            "address": wallet_map[sid]["address"],
            "chain": wallet_map[sid]["chain"],
            "chain_label": _WALLET_CHAIN_LABELS.get(
                wallet_map[sid]["chain"], wallet_map[sid]["chain"]
            ),
        }
        for sid in sorted(target_ids)
        if sid in wallet_map
    ]

    return {
        "currency": currency,
        "account": account,
        "assets": assets,
        "total_value": str(total),
        "warnings": warnings,
        "wallets": account_wallets,
    }


def _asset_accounts(asset: str, db_path: str, currency: str) -> dict:
    """指定資産の口座別内訳を返す。"""
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    groups = _load_groups(db_path)

    ledger = Ledger(db_path)
    try:
        per_source = ledger.balances_by_source()
    finally:
        ledger.close()

    warnings: list[str] = []
    prices = fetch_prices([asset], currency, warn=warnings.append)
    price = prices.get(asset.upper())

    group_bals: dict[str, Decimal] = {}
    for src, bals in per_source.items():
        bal = bals.get(asset, Decimal("0"))
        if abs(bal) < _DUST:
            continue
        name = _display_name(src, groups)
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


def _tx_asset_deltas(tx) -> dict[str, Decimal]:
    """1取引で動く全資産のデルタ（受取 +, 送付/手数料 -）を返す。"""
    d: dict[str, Decimal] = {}
    if tx.received_asset and tx.received_amount is not None:
        d[tx.received_asset] = d.get(tx.received_asset, Decimal("0")) + tx.received_amount
    if tx.sent_asset and tx.sent_amount is not None:
        d[tx.sent_asset] = d.get(tx.sent_asset, Decimal("0")) - tx.sent_amount
    if tx.fee_asset and tx.fee_amount is not None:
        d[tx.fee_asset] = d.get(tx.fee_asset, Decimal("0")) - tx.fee_amount
    return d


def _build_running_balances(
    all_txs: list,
    groups: dict[str, list[str]],
) -> dict[str, dict[str, dict[str, str]]]:
    """全取引を時系列走査し {tx_id: {asset: {"global": str, "account": str}}} を返す。

    全体残高はすべてのソース合算、口座内残高はその取引が属するグループ内の累計。
    """
    all_txs = sorted(all_txs, key=lambda t: (t.timestamp, t.id))
    global_bal: dict[str, Decimal] = {}
    account_bal: dict[str, Decimal] = {}  # f"{gname}\x00{asset}" → Decimal
    result: dict[str, dict] = {}
    for tx in all_txs:
        gname = _display_name(tx.source, groups)
        affected = {}
        for asset, delta in _tx_asset_deltas(tx).items():
            global_bal[asset] = global_bal.get(asset, Decimal("0")) + delta
            akey = f"{gname}\x00{asset}"
            account_bal[akey] = account_bal.get(akey, Decimal("0")) + delta
            affected[asset] = {
                "global": str(global_bal[asset]),
                "account": str(account_bal[akey]),
            }
        result[tx.id] = affected
    return result


def _resolve_source_ids(
    account: str | None, db_path: str, groups: dict[str, list[str]]
) -> list[str] | None:
    if not account:
        return None
    ledger = Ledger(db_path)
    try:
        all_ids = [src for src, *_ in ledger.sources()]
    finally:
        ledger.close()
    mapped = [s for s in all_ids if _display_name(s, groups) == account]
    return mapped if mapped else [account]


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _transactions(
    db_path: str,
    account: str | None,
    asset: str | None,
    since_str: str | None,
    until_str: str | None,
    page: int,
) -> dict:
    """取引履歴ページを返す。account は表示名（グループ名）で受け取る。"""
    groups = _load_groups(db_path)
    source_ids = _resolve_source_ids(account, db_path, groups)
    since = _parse_date(since_str)
    until = _parse_date(until_str)

    ledger = Ledger(db_path)
    try:
        # 残高計算用: アカウントフィルタなし・日付フィルタなし・資産フィルタのみ
        bal_txs, _ = ledger.transactions(asset=asset, limit=10_000_000)
        # 表示用: 全フィルタ適用
        offset = (max(page, 1) - 1) * _TX_PAGE_SIZE
        txs, total = ledger.transactions(
            source=source_ids,
            asset=asset,
            since=since,
            until=until,
            limit=_TX_PAGE_SIZE,
            offset=offset,
        )
    finally:
        ledger.close()

    running = _build_running_balances(bal_txs, groups)

    rows = []
    for tx in txs:
        row_balances = running.get(tx.id, {})
        rows.append({
            "id": tx.id,
            "timestamp": tx.timestamp.isoformat(),
            "account": _display_name(tx.source, groups),
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
            "running_balances": row_balances,
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


def _add_manual_transaction(db_path: str, body: dict[str, Any]) -> dict:
    """手動で取引を追加する。"""
    groups = _load_groups(db_path)
    # source は表示名 → ソースIDに変換
    account_name = body.get("account", "")
    src_ids = _resolve_source_ids(account_name, db_path, groups)
    source_id = src_ids[0] if src_ids else account_name

    ts_str = body.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid timestamp")

    type_str = body.get("type", "deposit").lower()
    try:
        tx_type = TxType(type_str)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid type: {type_str}")

    def _dec(v: Any) -> Decimal | None:
        return Decimal(str(v)) if v not in (None, "", 0, "0") else None

    tx_id = f"manual:{uuid.uuid4().hex[:12]}"
    tx = CanonicalTx(
        id=tx_id,
        source=source_id,
        timestamp=ts,
        type=tx_type,
        received_asset=body.get("received_asset") or None,
        received_amount=_dec(body.get("received_amount")),
        sent_asset=body.get("sent_asset") or None,
        sent_amount=_dec(body.get("sent_amount")),
        fee_asset=body.get("fee_asset") or None,
        fee_amount=_dec(body.get("fee_amount")),
        label=(body.get("label") or "手動追加"),
        raw={},
    )
    ledger = Ledger(db_path)
    try:
        ledger.upsert(tx)
    finally:
        ledger.close()
    return {"ok": True, "id": tx_id}


# CSV インポートで提示する取引所・サービスの表示名。
_EXCHANGE_LABELS: dict[str, str] = {
    "nexo": "Nexo Pro（自動判別: スポット/入出金）",
    "nexo_savings": "Nexo（貯蓄口座）",
    "nexo_spot": "Nexo Pro（スポット取引）",
    "nexo_dnw": "Nexo Pro（入出金）",
    "bitflyer": "bitFlyer（現物 TradeHistory）",
    "bitflyer_collateral": "bitFlyer（証拠金 CollateralHistory）",
    "bitflyer_conversion": "bitFlyer（両替 ConversionHistory）",
    "gmo": "GMOコイン",
    "bitlend": "BitLending",
    "pbr_lending": "PBR Lending（貸出日次レポート）",
    "pbr_transfers": "PBR Lending（入出金履歴）",
    "binance": "Binance（スポット）",
    "universal": "汎用CSV",
}

# 新規口座追加で提示する取引所の表示順。
_IMPORT_EXCHANGE_ORDER: list[str] = [
    "nexo", "nexo_savings", "nexo_spot", "nexo_dnw",
    "bitflyer", "bitflyer_collateral", "bitflyer_conversion",
    "gmo", "bitlend", "pbr_lending", "pbr_transfers", "binance", "universal",
]


def _import_exchanges() -> dict:
    """CSV インポートで選択できる取引所・サービスの一覧を返す。"""
    items: list[dict] = []
    seen: set[str] = set()
    for key in _IMPORT_EXCHANGE_ORDER:
        if key in EXCHANGE_SOURCES:
            items.append({"value": key, "label": _EXCHANGE_LABELS.get(key, key)})
            seen.add(key)
    for key in EXCHANGE_SOURCES:
        if key not in seen:
            items.append({"value": key, "label": _EXCHANGE_LABELS.get(key, key)})
    return {"exchanges": items}


def _import_csv(db_path: str, body: dict[str, Any]) -> dict:
    """base64エンコードされたCSVを取り込み、CSV単位削除用にバッチ記録する。"""
    exchange = (body.get("exchange") or "").strip()
    if exchange not in EXCHANGE_SOURCES:
        raise HTTPException(status_code=422, detail=f"未対応の取引所です: {exchange}")

    content_b64 = body.get("content_b64") or ""
    if not content_b64:
        raise HTTPException(status_code=422, detail="CSVファイルの内容がありません")
    try:
        raw = base64.b64decode(content_b64)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="ファイル内容のデコードに失敗しました")

    filename = (body.get("filename") or f"{exchange}.csv").strip()
    # source_id はインポート先の識別子（任意。未指定なら取引所名）。
    source_id = (body.get("account") or "").strip() or exchange

    adapter = EXCHANGE_SOURCES[exchange](source_id)
    suffix = Path(filename).suffix or ".csv"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            txs = adapter.load(tmp_path)
        except Exception as e:  # noqa: BLE001 - パースエラーをユーザーに返す
            raise HTTPException(status_code=422, detail=f"CSV の解析に失敗しました: {e}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if not txs:
        return {"ok": True, "imported": 0, "parsed": 0, "source": source_id,
                "message": "取引が見つかりませんでした"}

    ledger = Ledger(db_path)
    try:
        before = ledger.count(source_id)
        ledger.upsert_many(txs)
        after = ledger.count(source_id)

        latest_ts = max(t.timestamp for t in txs)
        cursor = ledger.get_cursor(source_id)
        if cursor is None or latest_ts > cursor:
            ledger.set_cursor(source_id, latest_ts)

        batch_id = f"batch:{uuid.uuid4().hex[:12]}"
        ledger.record_import_batch(
            batch_id, source_id, exchange, filename, [t.id for t in txs]
        )
    finally:
        ledger.close()

    return {
        "ok": True,
        "imported": after - before,
        "parsed": len(txs),
        "source": source_id,
        "batch_id": batch_id,
    }


def _list_import_batches(db_path: str) -> dict:
    """CSV取り込みバッチ一覧（表示名・取引所ラベル付き）を返す。"""
    groups = _load_groups(db_path)
    ledger = Ledger(db_path)
    try:
        batches = ledger.list_import_batches()
    finally:
        ledger.close()
    for b in batches:
        b["account"] = _display_name(b["source"], groups)
        b["exchange_label"] = _EXCHANGE_LABELS.get(b["exchange"], b["exchange"])
    return {"batches": batches}


# エクスポート形式の定義。value はクエリ用、label はUI表示用。
_EXPORT_FORMATS: list[dict] = [
    {"value": "koinly", "label": "Koinly（Universal CSV）", "ready": True},
    {"value": "cryptact", "label": "Cryptact（カスタムファイル）", "ready": True},
    {"value": "summ", "label": "SUMM（カスタムCSV）", "ready": True},
]


def _export_formats() -> dict:
    return {"formats": _EXPORT_FORMATS}


def _collect_export_txs(
    db_path: str, account: str | None, since: str | None, until: str | None
) -> list:
    """エクスポート対象の取引を時系列昇順で取得する。"""
    groups = _load_groups(db_path)
    source_ids = _resolve_source_ids(account, db_path, groups)
    since_dt = _parse_date(since)
    until_dt = _parse_date(until)

    ledger = Ledger(db_path)
    try:
        txs, _ = ledger.transactions(
            source=source_ids, since=since_dt, until=until_dt, limit=10_000_000
        )
    finally:
        ledger.close()
    txs.sort(key=lambda t: (t.timestamp, t.id))
    return txs


def _export_csv(
    db_path: str, fmt: str, account: str | None, since: str | None, until: str | None
) -> Response:
    """指定形式のCSVを生成して添付ファイルとして返す。"""
    fmt = (fmt or "").lower()
    known = {f["value"] for f in _EXPORT_FORMATS}
    if fmt not in known:
        raise HTTPException(status_code=422, detail=f"未対応の形式です: {fmt}")

    txs = _collect_export_txs(db_path, account, since, until)

    if fmt == "koinly":
        text = to_koinly_csv_string(txs)
    elif fmt == "cryptact":
        text, _skipped = to_cryptact_csv_string(txs)
    else:  # summ
        text = to_summ_csv_string(txs)

    # ファイル名: <format>_<account>_<today>.csv（ASCIIのみ）
    acc_part = ""
    if account:
        safe = "".join(c if c.isalnum() else "_" for c in account)
        acc_part = f"_{safe}"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"{fmt}{acc_part}_{today}.csv"

    # UTF-8 BOM 付きで返す（Excel での文字化け回避）
    body = "﻿" + text
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# API連携対応取引所。
_API_EXCHANGE_LABELS: dict[str, str] = {
    "bybit": "Bybit",
}


def _list_account_apis(db_path: str) -> dict:
    store = SecretStore(db_path)
    accounts = store.list_accounts()
    for a in accounts:
        a["exchange_label"] = _API_EXCHANGE_LABELS.get(a["exchange"], a["exchange"])
    return {"accounts": accounts}


def _register_account_api(db_path: str, body: dict[str, Any]) -> dict:
    exchange = (body.get("exchange") or "").strip().lower()
    source_id = (body.get("source_id") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    api_secret = (body.get("api_secret") or "").strip()
    category = (body.get("category") or "all").strip()

    if not exchange or exchange not in _API_EXCHANGE_LABELS:
        raise HTTPException(status_code=422, detail=f"未対応の取引所です: {exchange}")
    if not source_id:
        raise HTTPException(status_code=422, detail="ソースIDを指定してください")
    if not api_key or not api_secret:
        raise HTTPException(status_code=422, detail="APIキーとシークレットは必須です")

    store = SecretStore(db_path)
    try:
        store.set_account_api(source_id, exchange, api_key, api_secret, category=category)
    except SecretStoreError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "source_id": source_id, "exchange": exchange}


def _delete_account_api(db_path: str, source_id: str) -> dict:
    store = SecretStore(db_path)
    deleted = store.delete_account(source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="登録が見つかりません")
    return {"ok": True}


def _sync_account_api(db_path: str, source_id: str) -> dict:
    store = SecretStore(db_path)
    try:
        creds = store.get_account_api(source_id)
    except SecretStoreError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if creds is None:
        raise HTTPException(status_code=404, detail="登録が見つかりません")

    exchange = creds["exchange"]

    ledger = Ledger(db_path)
    try:
        cursor = ledger.get_cursor(source_id)
    finally:
        ledger.close()

    start_time_ms: int | None = None
    if cursor:
        start_time_ms = int(cursor.timestamp() * 1000)

    if exchange == "bybit":
        adapter = BybitApiSource(
            source_id,
            creds["api_key"],
            creds["api_secret"],
            category=creds.get("category", "spot"),
        )
        try:
            txs = adapter.fetch_all(start_time_ms)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Bybit API エラー: {e}")
    else:
        raise HTTPException(status_code=422, detail=f"API連携未対応の取引所です: {exchange}")

    if not txs:
        return {"ok": True, "fetched": 0, "imported": 0, "source_id": source_id}

    ledger = Ledger(db_path)
    try:
        before = ledger.count(source_id)
        ledger.upsert_many(txs)
        after = ledger.count(source_id)
        latest_ts = max(t.timestamp for t in txs)
        cur = ledger.get_cursor(source_id)
        if cur is None or latest_ts > cur:
            ledger.set_cursor(source_id, latest_ts)
    finally:
        ledger.close()

    return {
        "ok": True,
        "fetched": len(txs),
        "imported": after - before,
        "source_id": source_id,
    }


# ---------------------------------------------------------------------------
# サーバー設定ファイル（初回セットアップ用）
# ---------------------------------------------------------------------------
# data_dir/_server_config.json（マルチユーザー）または
# <db_dir>/_server_config.json（シングルユーザー）に保存する。
# CS_SECRET_KEY や ADMIN_EMAILS を Web セットアップ画面から設定した場合に使う。
# env 変数が設定されている場合は常に env が優先される。

_SERVER_CONFIG_FILE = "_server_config.json"


def _server_config_path(base_dir: str) -> Path:
    return Path(base_dir) / _SERVER_CONFIG_FILE


def _load_server_config(base_dir: str) -> dict:
    try:
        return json.loads(_server_config_path(base_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_server_config(base_dir: str, data: dict) -> None:
    path = _server_config_path(base_dir)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _needs_first_run_setup(base_dir: str) -> bool:
    """初回セットアップが必要かどうかを返す。

    CS_SECRET_KEY が env になく、かつ設定ファイルが未存在の場合 True。
    セットアップ完了（キー設定またはスキップ）後はファイルが存在するため False になる。
    """
    if os.environ.get("CS_SECRET_KEY"):
        return False
    return not _server_config_path(base_dir).exists()


def _apply_server_config(base_dir: str) -> None:
    """設定ファイルの値を環境変数に反映する（env が設定済みの場合はスキップ）。

    この関数を create_app の冒頭で呼ぶことで、
    SecretStore など既存のコードは変更なしに設定ファイルの値を使える。
    Docker では未設定の変数も空文字で渡るため、空文字は「未設定」として扱う。
    """
    cfg = _load_server_config(base_dir)
    # 設定ファイルのキー → 反映先の環境変数名
    mapping = {
        "cs_secret_key": "CS_SECRET_KEY",
        "admin_emails": "ADMIN_EMAILS",
        "google_client_id": "GOOGLE_CLIENT_ID",
        "google_client_secret": "GOOGLE_CLIENT_SECRET",
        "base_url": "BASE_URL",
        "coingecko_api_key": "COINGECKO_API_KEY",
    }
    for cfg_key, env_name in mapping.items():
        val = cfg.get(cfg_key, "")
        if val and not os.environ.get(env_name):
            os.environ[env_name] = val


# ---------------------------------------------------------------------------
# ウォレットアドレス連携
# ---------------------------------------------------------------------------

# チェーン表示名（同期対象のラベル）
_WALLET_CHAIN_LABELS: dict[str, str] = {
    "evm": "EVM（Ethereum / Arbitrum / Polygon / Base / Optimism）",
    "solana": "Solana",
}


# システム共通のスキャン用キー（管理者がWebまたはenvで設定するインフラキー）
_SYSTEM_PROVIDER_KEYS: dict[str, str] = {
    "etherscan": "ETHERSCAN_API_KEY",
    "helius": "HELIUS_API_KEY",
}

# .env.example のプレースホルダ値（コピーしたままの場合に誤認識しないよう除外）
_ENV_KEY_PLACEHOLDERS = frozenset({
    "your_api_key_here",
    "your_api_secret_here",
    "your_etherscan_api_key_here",
    "your_helius_api_key_here",
    "your-etherscan-api-key",
    "your-helius-api-key",
    "changeme",
})


def _env_key(env_name: str) -> str:
    """環境変数からキーを取得し、プレースホルダは空文字として扱う。"""
    val = os.environ.get(env_name, "").strip()
    if val.lower() in _ENV_KEY_PLACEHOLDERS:
        return ""
    return val


def _system_key_or_env(system_db: str, provider: str, env_name: str) -> str | None:
    """システム保存キー（暗号化）→ 環境変数 の順で解決する。

    マスター鍵未設定などで復号できない場合は環境変数にフォールバックする。
    """
    try:
        key = SecretStore(system_db).get_provider_key(provider)
    except SecretStoreError:
        key = None
    return key or _env_key(env_name) or None


def _system_key_status(system_db: str) -> dict:
    """システムキーの状態（Web保存済みか / env設定済みか）を返す（値は含まない）。"""
    try:
        stored = SecretStore(system_db).list_provider_keys()
    except SecretStoreError:
        stored = {}
    return {
        provider: {
            "stored": bool(stored.get(provider)),
            "env": bool(_env_key(env_name)),
        }
        for provider, env_name in _SYSTEM_PROVIDER_KEYS.items()
    }


def _set_system_keys(system_db: str, body: dict[str, Any]) -> dict:
    """システムキーを暗号化保存する。

    body に含まれるキーのみ更新する。値が空文字なら削除する。
    body に無いプロバイダーは変更しない。
    """
    store = SecretStore(system_db)
    updated: list[str] = []
    for provider in _SYSTEM_PROVIDER_KEYS:
        if provider not in body:
            continue
        val = (body.get(provider) or "").strip()
        try:
            store.set_provider_key(provider, val)
        except SecretStoreError as e:
            raise HTTPException(status_code=500, detail=str(e))
        updated.append(provider)
    return {"ok": True, "updated": updated}


def _detect_wallet_chain(address: str) -> str:
    """アドレス形式からチェーン種別を判定する（"evm" / "solana"）。

    0x で始まる 42 文字の 16 進数は EVM、それ以外（base58）は Solana とみなす。
    """
    a = address.strip()
    if a.lower().startswith("0x") and len(a) == 42:
        try:
            int(a[2:], 16)
            return "evm"
        except ValueError:
            pass
    # Solana は base58（32〜44 文字程度）。EVM 以外はすべて Solana 扱いにする。
    return "solana"


def _list_wallets(db_path: str) -> dict:
    store = SecretStore(db_path)
    wallets = store.list_wallets()
    for w in wallets:
        w["chain_label"] = _WALLET_CHAIN_LABELS.get(w["chain"], w["chain"])
    return {"wallets": wallets}


def _register_wallet(db_path: str, body: dict[str, Any]) -> dict:
    address = (body.get("address") or "").strip()
    source_id = (body.get("source_id") or "").strip()
    api_key = (body.get("api_key") or "").strip() or None
    helius_key = (body.get("helius_key") or "").strip() or None

    if not address:
        raise HTTPException(status_code=422, detail="ウォレットアドレスを入力してください")

    chain = _detect_wallet_chain(address)
    if not source_id:
        # 表示名未指定ならアドレス先頭から自動生成
        source_id = f"{chain}_{address[:8].lower()}"

    store = SecretStore(db_path)
    try:
        store.set_wallet(
            source_id, address, chain, api_key=api_key, helius_key=helius_key,
        )
    except SecretStoreError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "source_id": source_id, "chain": chain,
            "chain_label": _WALLET_CHAIN_LABELS.get(chain, chain)}


def _delete_wallet(db_path: str, source_id: str) -> dict:
    store = SecretStore(db_path)
    if not store.delete_wallet(source_id):
        raise HTTPException(status_code=404, detail="ウォレット登録が見つかりません")
    return {"ok": True}


def _sync_wallet(db_path: str, source_id: str, system_db: str | None = None) -> dict:
    sys_db = system_db or db_path

    store = SecretStore(db_path)
    try:
        wallet = store.get_wallet(source_id)
    except SecretStoreError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if wallet is None:
        raise HTTPException(status_code=404, detail="ウォレット登録が見つかりません")

    address = wallet["address"]
    chain = wallet["chain"]
    txs: list[CanonicalTx] = []

    if chain == "solana":
        from ..sources.solana.helius import HeliusApiSource

        key = wallet.get("helius_key") or _system_key_or_env(sys_db, "helius", "HELIUS_API_KEY")
        if not key:
            raise HTTPException(
                status_code=422,
                detail="Solana には Helius APIキーが必要です。設定画面の「システム設定」で登録するか、環境変数 HELIUS_API_KEY を設定してください。",
            )
        try:
            txs = HeliusApiSource(source_id, address, key).fetch_all(record_gas=True)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Helius API エラー: {e}")
    else:  # evm — 全 EVM チェーンをスキャンしてマージ
        from ..sources.api.etherscan import CHAIN_IDS, EtherscanApiSource

        key = wallet.get("api_key") or _system_key_or_env(sys_db, "etherscan", "ETHERSCAN_API_KEY")
        if not key:
            raise HTTPException(
                status_code=422,
                detail="EVM には Etherscan V2 APIキーが必要です。設定画面の「システム設定」で登録するか、環境変数 ETHERSCAN_API_KEY を設定してください。",
            )
        errors: list[str] = []
        for chain_name, chain_id in CHAIN_IDS.items():
            try:
                adapter = EtherscanApiSource(source_id, address, key, chain_id)
                txs.extend(adapter.fetch_all(record_gas=True))
            except Exception as e:  # noqa: BLE001 - 1チェーン失敗でも他は継続
                errors.append(f"{chain_name}: {e}")
        if errors and not txs:
            raise HTTPException(status_code=502, detail="Etherscan API エラー: " + "; ".join(errors))

    if not txs:
        return {"ok": True, "fetched": 0, "imported": 0, "source_id": source_id}

    ledger = Ledger(db_path)
    try:
        before = ledger.count(source_id)
        ledger.upsert_many(txs)
        after = ledger.count(source_id)
        latest_ts = max(t.timestamp for t in txs)
        cur = ledger.get_cursor(source_id)
        if cur is None or latest_ts > cur:
            ledger.set_cursor(source_id, latest_ts)
    finally:
        ledger.close()

    return {
        "ok": True,
        "fetched": len(txs),
        "imported": after - before,
        "source_id": source_id,
    }


def _sync_all(db_path: str, system_db: str | None = None) -> dict:
    """登録済みの全 API 口座・全ウォレットを順に同期する。

    1件の失敗で全体を止めず、各ソースの結果（成功/失敗）を集約して返す。
    """
    store = SecretStore(db_path)
    results: list[dict] = []

    def _run(source_id: str, kind: str, fn) -> None:
        try:
            r = fn(source_id)
            results.append({
                "source_id": source_id,
                "kind": kind,
                "ok": True,
                "fetched": r.get("fetched", 0),
                "imported": r.get("imported", 0),
            })
        except HTTPException as e:
            results.append({
                "source_id": source_id, "kind": kind,
                "ok": False, "error": str(e.detail),
            })
        except Exception as e:  # noqa: BLE001 - 1件失敗でも他は継続
            results.append({
                "source_id": source_id, "kind": kind,
                "ok": False, "error": str(e),
            })

    for acct in store.list_accounts():
        _run(acct["source_id"], "api", lambda sid: _sync_account_api(db_path, sid))
    for wallet in store.list_wallets():
        _run(wallet["source_id"], "wallet",
             lambda sid: _sync_wallet(db_path, sid, system_db=system_db))

    succeeded = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    total_imported = sum(r.get("imported", 0) for r in succeeded)
    total_fetched = sum(r.get("fetched", 0) for r in succeeded)

    return {
        "ok": True,
        "total": len(results),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "total_fetched": total_fetched,
        "total_imported": total_imported,
        "results": results,
    }


_RANGE_DAYS: dict[str, int | None] = {
    "7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None,
}


def _portfolio_history(
    db_path: str,
    currency: str,
    range_str: str,
    scope: str,
) -> dict:
    """ポートフォリオ価値の日次時系列を返す。

    scope: "total" | "account:<表示名>" | "asset:<シンボル>"
    range_str: "7d" | "30d" | "90d" | "1y" | "all"
    """
    currency = currency.upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = "USD"

    if range_str not in _RANGE_DAYS:
        range_str = "90d"

    today = date.today()
    days = _RANGE_DAYS[range_str]
    range_start = (today - timedelta(days=days)) if days is not None else None

    # scope 解析
    source_filter: list[str] | None = None
    asset_filter: str | None = None

    if scope.startswith("account:"):
        account_name = scope[len("account:"):]
        groups = _load_groups(db_path)
        ledger = Ledger(db_path)
        try:
            all_ids = [src for src, *_ in ledger.sources()]
        finally:
            ledger.close()
        source_filter = [s for s in all_ids if _display_name(s, groups) == account_name] or None
    elif scope.startswith("asset:"):
        asset_filter = scope[len("asset:"):].upper()

    ledger = Ledger(db_path)
    try:
        snapshots = daily_balances(
            ledger,
            source=source_filter,
            asset=asset_filter,
            start=range_start,
            end=today,
        )
    finally:
        ledger.close()

    if not snapshots:
        return {
            "currency": currency,
            "range": range_str,
            "scope": scope,
            "points": [],
            "unpriced": [],
            "warnings": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # 日付範囲の確定（all の場合は最初のスナップショット日から today まで）
    first_snap_date = date.fromisoformat(min(snapshots))
    effective_start = max(range_start, first_snap_date) if range_start else first_snap_date

    all_assets = assets_in_range(snapshots)
    if asset_filter:
        all_assets = {asset_filter} & all_assets

    warnings: list[str] = []
    price_hist = fetch_price_history(
        list(all_assets), currency, effective_start, today, warn=warnings.append
    )

    unpriced: set[str] = set()
    points: list[dict] = []

    d = effective_start
    prev_snapshot: dict[str, Decimal] = {}
    while d <= today:
        iso = d.isoformat()
        if iso in snapshots:
            prev_snapshot = snapshots[iso]
        elif not prev_snapshot:
            d += timedelta(days=1)
            continue

        total_value = Decimal("0")
        has_any_price = False
        asset_balance = Decimal("0")  # asset スコープ用の保有数量
        for asset, balance in prev_snapshot.items():
            if asset_filter and asset != asset_filter:
                continue
            if asset_filter:
                asset_balance += balance
            day_prices = price_hist.get(asset, {})
            price = day_prices.get(iso)
            if price is not None:
                total_value += balance * price
                has_any_price = True
            elif not _is_spam_token(balance, None):
                unpriced.add(asset)

        if has_any_price:
            point = {"t": iso, "value": str(total_value)}
            if asset_filter:
                point["balance"] = str(asset_balance)
            points.append(point)

        d += timedelta(days=1)

    # CoinGecko ID がない資産（スパム・未対応トークン）は unpriced から除外する。
    # 残るのは「ID はあるが取得失敗」の資産のみ（一時的な取得不完全）。
    unpriced_supported = sorted(a for a in unpriced if a.upper() in COINGECKO_IDS)
    is_partial = bool(warnings) or bool(unpriced_supported)

    return {
        "currency": currency,
        "range": range_str,
        "scope": scope,
        "points": points,
        "unpriced": unpriced_supported,
        "is_partial": is_partial,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _get_account_groups(db_path: str) -> dict:
    """UI設定用: 現在のグループ設定とDBの全ソースIDを返す。"""
    groups = _load_groups(db_path)

    ledger = Ledger(db_path)
    try:
        all_sources = [src for src, *_ in ledger.sources()]
    finally:
        ledger.close()

    # どのグループにも属していないソースID
    assigned = {sid for ids in groups.values() for sid in ids}
    unassigned = [s for s in all_sources if s not in assigned]

    return {
        "groups": groups,
        "all_source_ids": all_sources,
        "unassigned_source_ids": unassigned,
    }


def create_app(
    db_path: str = "ledger.db",
    data_dir: str | None = None,
) -> FastAPI:
    """FastAPI アプリを生成する。

    シングルユーザーモード: db_path を直接指定（CLIデフォルト、認証不要）。
    マルチユーザーモード: data_dir を指定すると Google OAuth が有効になり、
      ユーザーごとに {data_dir}/{google_sub}.db が使われる。
    """
    app = FastAPI(title="Crypto-Summary", docs_url="/api/docs")

    multi_user = data_dir is not None

    # base_dir: サーバー設定ファイルの置き場所
    if multi_user:
        _data_dir_path = Path(data_dir).expanduser()
        _data_dir_path.mkdir(parents=True, exist_ok=True)
        _base_dir = str(_data_dir_path)
    else:
        _base_dir = str(Path(db_path).resolve().parent)

    # 設定ファイルの値を env に反映（env 優先）
    _apply_server_config(_base_dir)

    def _admin_emails() -> set[str]:
        raw = os.environ.get("ADMIN_EMAILS", "")
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    if multi_user:
        # Google OAuth セッション + ルート
        from starlette.middleware.sessions import SessionMiddleware

        secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")
        app.add_middleware(SessionMiddleware, secret_key=secret_key, https_only=False)

        from .auth import require_user, router as auth_router

        app.include_router(auth_router)

        def get_db_path(user: dict = Depends(require_user)) -> str:
            return str(_data_dir_path / f"{user['sub']}.db")

        # システム共通シークレットは data_dir 内の専用ストアに保存する。
        _system_db = str(_data_dir_path / "_system.db")

        def system_store_path() -> str:
            return _system_db

        def is_admin_user(user: dict | None) -> bool:
            admins = _admin_emails()
            return bool(user and admins and user.get("email", "").lower() in admins)

        def require_admin(user: dict = Depends(require_user)) -> dict:
            if not is_admin_user(user):
                raise HTTPException(status_code=403, detail="管理者権限が必要です")
            return user

    else:
        # シングルユーザー: 固定 db_path、認証不要
        _fixed_db = db_path

        def get_db_path() -> str:  # type: ignore[misc]
            return _fixed_db

        # シングルユーザーではシステムキーもユーザーDBに保存し、所有者が管理者。
        def system_store_path() -> str:
            return _fixed_db

        def is_admin_user(user: dict | None) -> bool:
            return True

        def require_admin() -> dict:  # type: ignore[misc]
            return {"email": ""}

    # ------------------------------------------------------------------
    # API ルート（すべて get_db_path 依存で db パスを取得する）
    # ------------------------------------------------------------------

    @app.get("/api/summary")
    def summary(currency: str = Query("USD"), db: str = Depends(get_db_path)) -> dict:
        return _summary(db, currency)

    @app.get("/api/sources")
    def sources(currency: str = Query("USD"), db: str = Depends(get_db_path)) -> dict:
        return _sources(db, currency)

    @app.get("/api/account-assets")
    def account_assets(
        account: str = Query(...),
        currency: str = Query("USD"),
        db: str = Depends(get_db_path),
    ) -> dict:
        return _account_assets(account, db, currency)

    @app.get("/api/asset-accounts")
    def asset_accounts(
        asset: str = Query(...),
        currency: str = Query("USD"),
        db: str = Depends(get_db_path),
    ) -> dict:
        return _asset_accounts(asset, db, currency)

    @app.get("/api/transactions")
    def transactions_api(
        account: str | None = Query(None),
        asset: str | None = Query(None),
        since: str | None = Query(None),
        until: str | None = Query(None),
        page: int = Query(1),
        db: str = Depends(get_db_path),
    ) -> dict:
        return _transactions(db, account, asset, since, until, page)

    @app.post("/api/transactions")
    def add_transaction(body: dict[str, Any], db: str = Depends(get_db_path)) -> dict:
        return _add_manual_transaction(db, body)

    @app.delete("/api/transactions/{tx_id}")
    def delete_transaction(tx_id: str, db: str = Depends(get_db_path)) -> dict:
        ledger = Ledger(db)
        try:
            deleted = ledger.delete_by_id(tx_id)
        finally:
            ledger.close()
        if not deleted:
            raise HTTPException(status_code=404, detail="Transaction not found")
        return {"ok": True}

    @app.get("/api/import/exchanges")
    def import_exchanges() -> dict:
        return _import_exchanges()

    @app.post("/api/import/csv")
    def import_csv(body: dict[str, Any], db: str = Depends(get_db_path)) -> dict:
        return _import_csv(db, body)

    @app.get("/api/import/batches")
    def import_batches(db: str = Depends(get_db_path)) -> dict:
        return _list_import_batches(db)

    @app.delete("/api/import/batches/{batch_id}")
    def delete_import_batch(batch_id: str, db: str = Depends(get_db_path)) -> dict:
        ledger = Ledger(db)
        try:
            existing = {b["id"] for b in ledger.list_import_batches()}
            if batch_id not in existing:
                raise HTTPException(status_code=404, detail="バッチが見つかりません")
            deleted = ledger.delete_import_batch(batch_id)
        finally:
            ledger.close()
        return {"ok": True, "deleted": deleted}

    @app.get("/api/export/formats")
    def export_formats() -> dict:
        return _export_formats()

    @app.get("/api/export")
    def export_csv(
        format: str = Query("koinly"),
        account: str | None = Query(None),
        since: str | None = Query(None),
        until: str | None = Query(None),
        db: str = Depends(get_db_path),
    ) -> Response:
        return _export_csv(db, format, account, since, until)

    @app.delete("/api/sources/{account}")
    def clear_account(account: str, db: str = Depends(get_db_path)) -> dict:
        """口座（表示名）配下の全ソースIDの取引をまとめて削除する。"""
        groups = _load_groups(db)
        ledger = Ledger(db)
        try:
            all_ids = [src for src, *_ in ledger.sources()]
        finally:
            ledger.close()
        source_ids = [s for s in all_ids if _display_name(s, groups) == account]
        if not source_ids:
            raise HTTPException(status_code=404, detail="口座が見つかりません")
        ledger = Ledger(db)
        try:
            total = sum(ledger.clear(sid) for sid in source_ids)
        finally:
            ledger.close()
        return {"ok": True, "deleted": total, "source_ids": source_ids}

    @app.get("/api/account-apis")
    def list_account_apis(db: str = Depends(get_db_path)) -> dict:
        return _list_account_apis(db)

    @app.post("/api/account-apis")
    def register_account_api(body: dict[str, Any], db: str = Depends(get_db_path)) -> dict:
        return _register_account_api(db, body)

    @app.delete("/api/account-apis/{source_id}")
    def delete_account_api(source_id: str, db: str = Depends(get_db_path)) -> dict:
        return _delete_account_api(db, source_id)

    @app.post("/api/account-apis/{source_id}/sync")
    def sync_account_api(source_id: str, db: str = Depends(get_db_path)) -> dict:
        return _sync_account_api(db, source_id)

    @app.get("/api/wallets")
    def list_wallets(db: str = Depends(get_db_path)) -> dict:
        return _list_wallets(db)

    @app.post("/api/wallets")
    def register_wallet(body: dict[str, Any], db: str = Depends(get_db_path)) -> dict:
        return _register_wallet(db, body)

    @app.delete("/api/wallets/{source_id}")
    def delete_wallet(source_id: str, db: str = Depends(get_db_path)) -> dict:
        return _delete_wallet(db, source_id)

    @app.post("/api/wallets/{source_id}/sync")
    def sync_wallet(source_id: str, db: str = Depends(get_db_path)) -> dict:
        return _sync_wallet(db, source_id, system_db=system_store_path())

    @app.post("/api/sync-all")
    def sync_all(db: str = Depends(get_db_path)) -> dict:
        return _sync_all(db, system_db=system_store_path())

    @app.get("/api/system-keys")
    def get_system_keys(admin: dict = Depends(require_admin)) -> dict:
        return {
            "providers": _system_key_status(system_store_path()),
            "cs_secret_key": bool(os.environ.get("CS_SECRET_KEY")),
            "multi_user": multi_user,
            "admin_configured": (not multi_user) or bool(_admin_emails()),
        }

    @app.post("/api/system-keys")
    def set_system_keys(
        body: dict[str, Any], admin: dict = Depends(require_admin)
    ) -> dict:
        return _set_system_keys(system_store_path(), body)

    @app.get("/api/admin-config")
    def get_admin_config(admin: dict = Depends(require_admin)) -> dict:
        """管理者設定の現在値を返す（シークレット類は設定済みかどうかのみ）。"""
        cfg = _load_server_config(_base_dir)
        return {
            "multi_user": multi_user,
            "base_url": os.environ.get("BASE_URL") or cfg.get("base_url") or "",
            "admin_emails": os.environ.get("ADMIN_EMAILS") or cfg.get("admin_emails") or "",
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID") or cfg.get("google_client_id") or "",
            "google_client_id_set": bool(
                os.environ.get("GOOGLE_CLIENT_ID") or cfg.get("google_client_id")
            ),
            "google_client_secret_set": bool(
                os.environ.get("GOOGLE_CLIENT_SECRET") or cfg.get("google_client_secret")
            ),
            "cs_secret_key_set": bool(os.environ.get("CS_SECRET_KEY")),
            "coingecko_api_key_set": bool(_coingecko_api_key()),
            "providers": _system_key_status(system_store_path()),
        }

    @app.post("/api/admin-config")
    def set_admin_config(
        body: dict[str, Any], admin: dict = Depends(require_admin)
    ) -> dict:
        """管理者設定を保存する（SECRET_KEY / DATA_DIR 以外の全設定が対象）。"""
        cfg = _load_server_config(_base_dir)
        updated: list[str] = []
        oauth_changed = False

        if "admin_emails" in body:
            val = (body["admin_emails"] or "").strip()
            cfg["admin_emails"] = val
            os.environ["ADMIN_EMAILS"] = val
            updated.append("admin_emails")

        if "base_url" in body:
            val = (body["base_url"] or "").strip().rstrip("/")
            cfg["base_url"] = val
            os.environ["BASE_URL"] = val
            updated.append("base_url")
            oauth_changed = True

        if "google_client_id" in body:
            val = (body["google_client_id"] or "").strip()
            if val:
                cfg["google_client_id"] = val
                os.environ["GOOGLE_CLIENT_ID"] = val
                updated.append("google_client_id")
                oauth_changed = True

        if "google_client_secret" in body:
            val = (body["google_client_secret"] or "").strip()
            if val:
                cfg["google_client_secret"] = val
                os.environ["GOOGLE_CLIENT_SECRET"] = val
                updated.append("google_client_secret")
                oauth_changed = True

        if "coingecko_api_key" in body:
            val = (body["coingecko_api_key"] or "").strip()
            cfg["coingecko_api_key"] = val
            os.environ["COINGECKO_API_KEY"] = val
            updated.append("coingecko_api_key")

        if multi_user and oauth_changed:
            try:
                from .auth import reset_oauth_client
                reset_oauth_client()
            except Exception:  # noqa: BLE001
                pass

        _save_server_config(_base_dir, cfg)
        return {"ok": True, "updated": updated}

    @app.get("/api/account-groups")
    def get_account_groups(db: str = Depends(get_db_path)) -> dict:
        return _get_account_groups(db)

    @app.put("/api/account-groups")
    def put_account_groups(body: dict[str, Any], db: str = Depends(get_db_path)) -> dict:
        groups = body.get("groups")
        if not isinstance(groups, dict):
            raise HTTPException(status_code=422, detail="groups must be an object")
        for name, ids in groups.items():
            if not isinstance(name, str) or not isinstance(ids, list):
                raise HTTPException(status_code=422, detail="invalid groups format")
        _save_groups(db, groups)
        return {"ok": True, "groups": groups}

    @app.get("/api/portfolio-history")
    def portfolio_history(
        currency: str = Query("USD"),
        range: str = Query("90d"),
        scope: str = Query("total"),
        db: str = Depends(get_db_path),
    ) -> dict:
        return _portfolio_history(db, currency, range, scope)

    @app.get("/api/coin-icons")
    def coin_icons() -> dict:
        return fetch_coin_icons()

    # ------------------------------------------------------------------
    # 初回セットアップ（認証不要・設定完了後はロック）
    # ------------------------------------------------------------------

    def _oauth_in_env() -> bool:
        return bool(
            os.environ.get("GOOGLE_CLIENT_ID")
            and os.environ.get("GOOGLE_CLIENT_SECRET")
        )

    @app.get("/api/setup-status")
    def setup_status() -> dict:
        return {
            "needs_setup": _needs_first_run_setup(_base_dir),
            "multi_user": multi_user,
            # マルチユーザーで OAuth が未設定なら、ウィザードで入力が必要。
            "oauth_in_env": _oauth_in_env(),
            "base_url_in_env": bool(os.environ.get("BASE_URL")),
        }

    @app.get("/api/generate-key")
    def generate_key() -> dict:
        """新しい Fernet キーを生成して返す（セットアップ画面用）。"""
        from ..core.secrets import generate_master_key
        return {"key": generate_master_key()}

    @app.post("/api/setup")
    def do_setup(body: dict[str, Any]) -> dict:
        """初回セットアップ: 各種設定を設定ファイルに保存する。

        マルチユーザーで OAuth が env 未設定の場合は、
        GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / BASE_URL の入力が必須。
        セットアップ完了後（ファイルが存在する状態）はこのエンドポイントは 403 を返す。
        """
        if not _needs_first_run_setup(_base_dir):
            raise HTTPException(status_code=403, detail="セットアップは既に完了しています")

        cs_key = (body.get("cs_secret_key") or "").strip()
        admin_emails = (body.get("admin_emails") or "").strip()
        google_client_id = (body.get("google_client_id") or "").strip()
        google_client_secret = (body.get("google_client_secret") or "").strip()
        base_url = (body.get("base_url") or "").strip().rstrip("/")
        skipped = bool(body.get("skipped"))

        # マルチユーザーで OAuth が未設定の場合は、ログイン不能になるのを防ぐため
        # OAuth 情報を必須とし、スキップを禁止する。
        oauth_required = multi_user and not _oauth_in_env()
        if oauth_required:
            if skipped:
                raise HTTPException(
                    status_code=422,
                    detail="マルチユーザーモードでは Google OAuth の設定が必要です（スキップできません）。",
                )
            if not (google_client_id and google_client_secret and base_url):
                raise HTTPException(
                    status_code=422,
                    detail="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / BASE_URL をすべて入力してください。",
                )

        if cs_key:
            from cryptography.fernet import Fernet
            try:
                Fernet(cs_key.encode("ascii"))
            except Exception:
                raise HTTPException(status_code=422, detail="CS_SECRET_KEY の形式が正しくありません。「生成する」ボタンで生成したキーを使ってください。")

        cfg: dict[str, Any] = {}
        if cs_key:
            cfg["cs_secret_key"] = cs_key
            os.environ["CS_SECRET_KEY"] = cs_key  # 再起動なしに即反映
        if admin_emails:
            cfg["admin_emails"] = admin_emails
            if not os.environ.get("ADMIN_EMAILS"):
                os.environ["ADMIN_EMAILS"] = admin_emails
        if google_client_id:
            cfg["google_client_id"] = google_client_id
            if not os.environ.get("GOOGLE_CLIENT_ID"):
                os.environ["GOOGLE_CLIENT_ID"] = google_client_id
        if google_client_secret:
            cfg["google_client_secret"] = google_client_secret
            if not os.environ.get("GOOGLE_CLIENT_SECRET"):
                os.environ["GOOGLE_CLIENT_SECRET"] = google_client_secret
        if base_url:
            cfg["base_url"] = base_url
            if not os.environ.get("BASE_URL"):
                os.environ["BASE_URL"] = base_url
        if skipped:
            cfg["setup_skipped"] = True

        # OAuth 設定を変更したので、キャッシュ済みクライアントを破棄する。
        if multi_user and (google_client_id or google_client_secret):
            try:
                from .auth import reset_oauth_client
                reset_oauth_client()
            except Exception:  # noqa: BLE001
                pass

        _save_server_config(_base_dir, cfg)
        return {"ok": True, "key_set": bool(cs_key)}

    @app.get("/api/meta")
    def meta(request: Request) -> dict:
        if multi_user:
            user = request.session.get("user")
            admin = is_admin_user(user)
        else:
            admin = True
        return {
            "currencies": list(SUPPORTED_CURRENCIES),
            "multi_user": multi_user,
            "is_admin": admin,
            "needs_setup": _needs_first_run_setup(_base_dir),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        """静的アセット（index.html / /static/*）は常に再検証させる。"""
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app


# uvicorn / Docker 用のモジュールレベルアプリインスタンス。
# DATA_DIR が設定されていればマルチユーザーモード、なければシングルユーザーモード。
_env_data_dir = os.environ.get("DATA_DIR")
_env_db_path = os.environ.get("DB_PATH", "ledger.db")
app = create_app(
    db_path=_env_db_path,
    data_dir=_env_data_dir,
)
