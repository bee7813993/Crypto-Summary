"""暗号資産の日次・履歴価格取得（CoinGecko market_chart、read-only）

保有資産の推移グラフ用に、各資産の過去の日次終値を currency 建てで取得する。

設計方針:
  - CoinGecko `/coins/{id}/market_chart/range`（vs_currency 直指定）から取得し、
    返ってきた時系列を UTC 日次の終値（その日の最後の値）へ正規化する。
  - 過去日の価格は不変なので積極的にキャッシュする。当日のみ揮発するため
    キャッシュからは常に除外して取り直す。
  - キャッシュは ~/.crypto_summary_pricehist.json に
    `currency -> coin_id -> {YYYY-MM-DD: price}` で保存する。
  - CoinGecko ID 未登録の資産は結果から欠落する（呼び出し側で「履歴価格なし」扱い）。
  - 法定通貨は、対象通貨と一致すれば 1.0。異なる法定通貨の過去為替は未対応
    （結果から欠落 ＝ 呼び出し側で unpriced 扱い）。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .prices import COINGECKO_IDS, FIAT_ASSETS

WarnFn = Callable[[str], None]

_BASE = "https://api.coingecko.com/api/v3"
_TODAY_TTL = 300  # 当日価格を再取得する間隔（秒）。本モジュールでは当日は常に取り直す。


def _hist_cache_path() -> Path:
    return Path.home() / ".crypto_summary_pricehist.json"


def _load_hist_cache() -> dict:
    try:
        return json.loads(_hist_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_hist_cache(data: dict) -> None:
    try:
        _hist_cache_path().write_text(json.dumps(data))
    except OSError:
        pass


def _date_range(start: date, end: date) -> list[str]:
    """start〜end（両端含む）の ISO 日付文字列リストを返す。"""
    if start > end:
        return []
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bucket_daily(prices_ms: list) -> dict[str, Decimal]:
    """[[ms, price], ...] を UTC 日次の終値（その日の最後の値）へ畳み込む。"""
    out: dict[str, Decimal] = {}
    for entry in prices_ms:
        try:
            ms, price = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        if price is None:
            continue
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
        out[d] = Decimal(str(price))  # 時系列昇順前提で上書き＝その日の最後の値
    return out


def _fetch_coin_range(
    coin_id: str,
    currency: str,
    start: date,
    end: date,
    warn: WarnFn | None,
) -> dict[str, Decimal]:
    """1コインの [start, end] 日次終値を取得する。失敗時は空 dict。"""
    import httpx

    def _warn(msg: str) -> None:
        if warn:
            warn(msg)

    # start 当日 00:00 〜 end 翌日 00:00（end の丸一日を含める）。
    from_ts = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    to_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    to_ts = int(to_dt.timestamp())

    url = (
        f"{_BASE}/coins/{coin_id}/market_chart/range"
        f"?vs_currency={currency.lower()}&from={from_ts}&to={to_ts}"
    )

    for attempt in range(3):
        try:
            resp = httpx.get(url, timeout=15)
            if resp.status_code == 429:
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
                    continue
                _warn("CoinGecko のレート制限 (429) です。しばらく待って再実行してください。")
                return {}
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:  # noqa: BLE001 - ネットワーク全般を許容
            _warn(f"CoinGecko履歴価格の取得に失敗しました ({coin_id}): {e}")
            return {}
    else:
        return {}

    return _bucket_daily(data.get("prices") or [])


def fetch_price_history(
    assets: list[str],
    currency: str,
    start: date,
    end: date,
    warn: WarnFn | None = None,
) -> dict[str, dict[str, Decimal]]:
    """指定資産の日次履歴価格を currency 建てで返す。

    返り値: {ASSET(大文字): {"YYYY-MM-DD": Decimal}}。
      - CoinGecko ID 未登録の資産・取得失敗の資産は欠落する。
      - 過去日はキャッシュから再利用し、不足分のみ取得する（当日は常に取り直す）。
    """
    currency = currency.upper()
    today = date.today()
    if end > today:
        end = today
    req_dates = _date_range(start, end)
    if not req_dates:
        return {}

    cache = _load_hist_cache()
    cur_cache = cache.setdefault(currency, {})
    changed = False
    result: dict[str, dict[str, Decimal]] = {}

    today_iso = today.isoformat()

    for asset in {a.upper() for a in assets}:
        # 法定通貨: 対象通貨と一致すれば 1.0。異なる法定通貨は未対応（欠落）。
        if asset in FIAT_ASSETS:
            if asset == currency:
                result[asset] = {d: Decimal("1") for d in req_dates}
            continue

        coin_id = COINGECKO_IDS.get(asset)
        if not coin_id:
            continue

        series = {k: Decimal(v) for k, v in cur_cache.get(coin_id, {}).items()}
        # 当日はキャッシュから除外して必ず取り直す（揮発するため）。
        cached_dates = set(series.keys()) - {today_iso}
        missing = [d for d in req_dates if d not in cached_dates]

        if missing:
            fetch_start = date.fromisoformat(min(missing))
            fetched = _fetch_coin_range(coin_id, currency, fetch_start, end, warn)
            if fetched:
                series.update(fetched)
                cur_cache[coin_id] = {k: str(v) for k, v in series.items()}
                changed = True

        sub = {d: series[d] for d in req_dates if d in series}
        if sub:
            result[asset] = sub

    if changed:
        _save_hist_cache(cache)
    return result
