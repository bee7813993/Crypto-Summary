"""Crypto-Summary Web UI のFastAPIアプリ。

ダッシュボード（資産サマリー）を提供する。価格は CoinGecko（read-only）から取得。
取引履歴のインポート機能は今後追加予定。
"""
from __future__ import annotations

import base64
import json
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..core.ledger import Ledger
from ..core.models import CanonicalTx, TxType
from ..core.portfolio import assets_in_range, daily_balances
from ..core.price_history import fetch_price_history
from ..core.prices import SUPPORTED_CURRENCIES, fetch_prices
from ..core.secrets import SecretStore, SecretStoreError
from ..sinks.cryptact_csv import to_cryptact_csv_string
from ..sinks.koinly_csv import to_koinly_csv_string
from ..sinks.summ_csv import to_summ_csv_string
from ..sources.api.bybit import BybitApiSource
from ..sources.csv_import import EXCHANGE_SOURCES

_STATIC_DIR = Path(__file__).parent / "static"
_DUST = Decimal("0.00000001")

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
    finally:
        ledger.close()

    all_assets = {a for bals in per_source.values() for a in bals}
    warnings: list[str] = []
    prices = fetch_prices(sorted(all_assets), currency, warn=warnings.append)

    group_bals: dict[str, dict[str, Decimal]] = {}
    group_tx: dict[str, int] = {}
    group_ids: dict[str, list[str]] = {}

    for src in sorted(per_source):
        name = _display_name(src, groups)
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
    "pbr_lending": "PBR Lending",
    "binance": "Binance（スポット）",
    "universal": "汎用CSV",
}

# 新規口座追加で提示する取引所の表示順。
_IMPORT_EXCHANGE_ORDER: list[str] = [
    "nexo", "nexo_savings", "nexo_spot", "nexo_dnw",
    "bitflyer", "bitflyer_collateral", "bitflyer_conversion",
    "gmo", "bitlend", "pbr_lending", "binance", "universal",
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
            else:
                unpriced.add(asset)

        if has_any_price:
            point = {"t": iso, "value": str(total_value)}
            if asset_filter:
                point["balance"] = str(asset_balance)
            points.append(point)

        d += timedelta(days=1)

    return {
        "currency": currency,
        "range": range_str,
        "scope": scope,
        "points": points,
        "unpriced": sorted(unpriced),
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
        since: str | None = Query(None),
        until: str | None = Query(None),
        page: int = Query(1),
    ) -> dict:
        return _transactions(db_path, account, asset, since, until, page)

    @app.post("/api/transactions")
    def add_transaction(body: dict[str, Any]) -> dict:
        return _add_manual_transaction(db_path, body)

    @app.delete("/api/transactions/{tx_id}")
    def delete_transaction(tx_id: str) -> dict:
        ledger = Ledger(db_path)
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
    def import_csv(body: dict[str, Any]) -> dict:
        return _import_csv(db_path, body)

    @app.get("/api/import/batches")
    def import_batches() -> dict:
        return _list_import_batches(db_path)

    @app.delete("/api/import/batches/{batch_id}")
    def delete_import_batch(batch_id: str) -> dict:
        ledger = Ledger(db_path)
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
    ) -> Response:
        return _export_csv(db_path, format, account, since, until)

    @app.delete("/api/sources/{account}")
    def clear_account(account: str) -> dict:
        """口座（表示名）配下の全ソースIDの取引をまとめて削除する。"""
        groups = _load_groups(db_path)
        ledger = Ledger(db_path)
        try:
            all_ids = [src for src, *_ in ledger.sources()]
        finally:
            ledger.close()
        # account に一致するソースIDを厳密に検索（存在しなければ404）
        source_ids = [s for s in all_ids if _display_name(s, groups) == account]
        if not source_ids:
            raise HTTPException(status_code=404, detail="口座が見つかりません")
        ledger = Ledger(db_path)
        try:
            total = sum(ledger.clear(sid) for sid in source_ids)
        finally:
            ledger.close()
        return {"ok": True, "deleted": total, "source_ids": source_ids}

    @app.get("/api/account-apis")
    def list_account_apis() -> dict:
        return _list_account_apis(db_path)

    @app.post("/api/account-apis")
    def register_account_api(body: dict[str, Any]) -> dict:
        return _register_account_api(db_path, body)

    @app.delete("/api/account-apis/{source_id}")
    def delete_account_api(source_id: str) -> dict:
        return _delete_account_api(db_path, source_id)

    @app.post("/api/account-apis/{source_id}/sync")
    def sync_account_api(source_id: str) -> dict:
        return _sync_account_api(db_path, source_id)

    @app.get("/api/account-groups")
    def get_account_groups() -> dict:
        return _get_account_groups(db_path)

    @app.put("/api/account-groups")
    def put_account_groups(body: dict[str, Any]) -> dict:
        groups = body.get("groups")
        if not isinstance(groups, dict):
            raise HTTPException(status_code=422, detail="groups must be an object")
        for name, ids in groups.items():
            if not isinstance(name, str) or not isinstance(ids, list):
                raise HTTPException(status_code=422, detail="invalid groups format")
        _save_groups(db_path, groups)
        return {"ok": True, "groups": groups}

    @app.get("/api/portfolio-history")
    def portfolio_history(
        currency: str = Query("USD"),
        range: str = Query("90d"),
        scope: str = Query("total"),
    ) -> dict:
        return _portfolio_history(db_path, currency, range, scope)

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "currencies": list(SUPPORTED_CURRENCIES),
            "db_path": str(db_path),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @app.middleware("http")
    async def _no_cache_static(request, call_next):
        """静的アセット（index.html / /static/*）は常に再検証させる。

        Cache-Control を付けないとブラウザのヒューリスティックキャッシュで
        古い app.js / style.css が使われ続けることがある。no-cache は
        ETag による 304 を維持しつつ「使う前に必ず確認」を強制する。
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    return app
