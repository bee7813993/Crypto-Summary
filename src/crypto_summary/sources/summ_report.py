"""SUMM 取引レポート CSV アダプタ

SUMM（旧 Crypto Tax Calculator）の取引レポートのエクスポート（取引の一覧を
CSV にしたもの）から、指定した口座の取引を取り込む。取引所の CSV では
もう取れない過去の履歴を、SUMM が API で集めていたデータで補う用途。

ファイルの形（日本語版）:
  1 件目のレコードは複数行の説明文（生成日時・タイムゾーン・免責事項）。
  その後にヘッダーと、取引の足（通貨ごとの増減）1 本ずつの行が続く。

    通貨, タイムスタンプ, 取引タイプ, 価格, 数量, 価値, 送信元, 送信先,
    アカウント, 取引ID, コメント, メモ

  - 通貨は "Bitcoin (BTC)" のように名前の後ろの括弧にティッカーがある
  - 数量は符号なし。増減の向きは取引タイプで決まる
    （購入・入金・報酬は増、売却・送金出金・手数料は減）
  - 時刻は説明文にあるタイムゾーン（日本語版は Asia/Tokyo）の現地時刻
  - 同じ取引の足（売買の購入・売却・手数料、出金と出金手数料）は取引ID が同じ
  - 口座で絞ったレポートにも、送金の相手側（入金先のウォレットなど）の行が入る

口座の選び方:
  取り込み先の口座名（ソースID）と同じ名前の「アカウント」の行だけを使う
  （大文字・小文字や記号の違いは無視する）。レポートに口座が 1 つしかなければ
  名前を問わずそれを使う。

既存のデータとの関係:
  SUMM のレポートは、その口座の、そのレポートが覆う期間（最初〜最後の取引の
  前後 1 時間）の正とする。取り込むと、同じ口座の SUMM 由来でない取引のうち
  その期間に入るものを置き換える（手動で追加した取引は残す）。後から取り込む
  他の CSV も、その期間の行は飛ばす（CsvSourceAdapter.reconcile）。同じ出来事を
  取引所の CSV と SUMM の両方から入れて二重計上しないため。

SUMM 由来の取引の id は "summ:" で始まる。台帳からその口座でレポートが覆う
期間を引くのに使う（Web の手動入力の "manual:" と同じ考え方）。
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter, read_csv_text

if TYPE_CHECKING:
    from ..core.ledger import Ledger

SUMM_ID_PREFIX = "summ:"

# レポートが覆う期間の前後の余白。同じ出来事でも取引所の CSV と SUMM とで
# 時刻が数秒〜数十秒ずれる（出金の受付と残高の反映、入金の検知と着金など）。
COVERAGE_MARGIN = timedelta(hours=1)

_REQUIRED = ("通貨", "タイムスタンプ", "取引タイプ", "数量", "アカウント", "取引ID")

# 取引タイプ
_BUY = {"購入", "クロスチェーン購入"}
_SELL = {"売却", "クロスチェーン売却"}
_FEE = {"手数料"}
_SEND = {"送金出金"}
_RECEIVE = {"入金"}
_IGNORE = {"無視（入金）", "無視（出金）"}
# 1 本で 1 件になる足: 取引タイプ → (種別, ラベル)
_SINGLE: dict[str, tuple[TxType, str]] = {
    "実現利益": (TxType.REWARD, "futures_realized_profit"),
    "実現損失": (TxType.FEE, "futures_realized_loss"),
    "Staking報酬": (TxType.REWARD, "staking"),
    "収入": (TxType.REWARD, "income"),
}

# zoneinfo の時刻帯データが無い環境（tzdata の無い Windows など）向けに、
# 夏時間の無い主なタイムゾーンの UTC からの時差を持っておく
_FIXED_OFFSETS = {
    "Asia/Tokyo": 9, "Asia/Seoul": 9, "Asia/Shanghai": 8, "Asia/Hong_Kong": 8,
    "Asia/Singapore": 8, "Asia/Taipei": 8, "UTC": 0, "Etc/UTC": 0,
}


def summ_coverage(ledger: Ledger, source: str) -> tuple[datetime, datetime] | None:
    """その口座で SUMM のレポートが覆う期間（余白込み）。SUMM の取引が無ければ None。"""
    span = ledger.id_prefix_time_range(source, SUMM_ID_PREFIX)
    if span is None:
        return None
    return span[0] - COVERAGE_MARGIN, span[1] + COVERAGE_MARGIN


def _report_tz(preamble: str) -> tzinfo:
    """説明文に書かれたタイムゾーン。書かれていなければ日本語版の既定の Asia/Tokyo。"""
    m = (re.search(r"「([^」]+)」\s*タイムゾーン", preamble)
         or re.search(r"\b([A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+(?:/[A-Z][A-Za-z_]+)?)\b", preamble))
    name = m.group(1).strip() if m else "Asia/Tokyo"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if name in _FIXED_OFFSETS:
            return timezone(timedelta(hours=_FIXED_OFFSETS[name]))
    raise ValueError(
        f"レポートのタイムゾーン {name} を解決できませんでした"
        "（Python の tzdata パッケージを入れてください）"
    )


def _parse_local(value: str, tz: tzinfo) -> datetime | None:
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _dec(value: str | None) -> Decimal | None:
    v = (value or "").strip().replace(",", "")
    if not v:
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _asset(name: str) -> str:
    """"Bitcoin (BTC)" → "BTC"。括弧が無ければそのまま（"USDC"）。

    名前が長いと途中で切られる（"Polygon Ecos... (POL)"）ので最後の括弧を使う。
    """
    found = re.findall(r"\(([^()]+)\)", name)
    return (found[-1] if found else name).strip().upper()


def _tx_hash(tid: str) -> str | None:
    """取引ID がオンチェーンの Tx ハッシュらしければ返す。

    出金や外部からの入金の取引ID は Tx ハッシュ（16 進・Base58 の 40 文字以上）。
    取引所内の ID（数字だけの短いもの、UUID、"-manual" 付きの手動入力）は除く。
    """
    return tid if len(tid) >= 40 and tid.isalnum() else None


def _norm_name(s: str) -> str:
    return "".join(ch for ch in s.casefold() if ch.isalnum())


@dataclass
class _Leg:
    ts: datetime
    asset: str
    qty: Decimal      # 符号なし
    kind: str         # 取引タイプ
    row: dict[str, str]


class SummReportCsvSource(CsvSourceAdapter):
    """SUMM 取引レポート CSV パーサー（指定した口座の分だけ取り込む）"""

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        records = list(csv.reader(io.StringIO(read_csv_text(path))))
        hdr_i = next(
            (i for i, rec in enumerate(records[:30])
             if set(_REQUIRED) <= {c.strip() for c in rec}),
            None,
        )
        if hdr_i is None:
            raise ValueError(
                "SUMM の取引レポート（日本語版）ではないようです。"
                f"必要な列: {'、'.join(_REQUIRED)}"
            )
        tz = _report_tz("\n".join(c for rec in records[:hdr_i] for c in rec))
        header = [c.strip() for c in records[hdr_i]]
        rows = [
            {name: (rec[j].strip() if j < len(rec) else "") for j, name in enumerate(header)}
            for rec in records[hdr_i + 1:]
            if any(c.strip() for c in rec)
        ]
        if not rows:
            return []
        account = self._pick_account(rows)

        groups: dict[str, list[_Leg]] = {}
        for i, row in enumerate(rows):
            if row.get("アカウント", "") != account:
                continue  # 送金の相手側など、別の口座の行
            kind = row.get("取引タイプ", "")
            if kind in _IGNORE:
                # SUMM で計算から外すよう指定された取引（残高の手動調整など）
                self._skip("SUMM で「無視」に分類された取引")
                continue
            ts = _parse_local(row.get("タイムスタンプ", ""), tz)
            qty = _dec(row.get("数量"))
            asset = _asset(row.get("通貨", ""))
            if ts is None or qty is None or not asset:
                self._skip("読み取れない行")
                continue
            if qty == 0:
                continue
            tid = row.get("取引ID", "") or f"row{i}"
            groups.setdefault(tid, []).append(_Leg(ts, asset, abs(qty), kind, row))

        self._seen_keys: dict[str, int] = {}
        txs: list[CanonicalTx] = []
        for tid, legs in groups.items():
            txs.extend(self._group(account, tid, legs))
        return txs

    def reconcile(self, txs: list[CanonicalTx], ledger: Ledger) -> list[CanonicalTx]:
        """レポートが覆う期間の、同じ口座の SUMM 由来でない取引を置き換える。"""
        if not txs:
            return txs
        lo = min(t.timestamp for t in txs)
        hi = max(t.timestamp for t in txs)
        prior = ledger.id_prefix_time_range(self.source_id, SUMM_ID_PREFIX)
        if prior is not None:
            lo, hi = min(lo, prior[0]), max(hi, prior[1])
        self.replaced = ledger.delete_by_source_window(
            [self.source_id],
            lo - COVERAGE_MARGIN,
            hi + COVERAGE_MARGIN + timedelta(microseconds=1),
            keep_manual=True,
            keep_prefixes=(SUMM_ID_PREFIX,),
        )
        return txs

    def _pick_account(self, rows: list[dict[str, str]]) -> str:
        counts = Counter(r.get("アカウント", "") for r in rows)
        if len(counts) == 1:
            return next(iter(counts))
        want = _norm_name(self.source_id)
        exact = [a for a in counts if _norm_name(a) == want]
        if len(exact) == 1:
            return exact[0]
        partial = [a for a in counts
                   if _norm_name(a) and (_norm_name(a) in want or want in _norm_name(a))]
        if len(partial) == 1:
            return partial[0]
        top = counts.most_common(1)[0][0]
        listing = "、".join(f"{a}（{n} 行）" for a, n in counts.most_common())
        raise ValueError(
            "このレポートには複数の口座が含まれています。取り込む口座の名前を"
            f"「ソースID」（CLI では --source-id）に入れてください（例: {_norm_name(top) or top}）。"
            f" このレポートの口座: {listing}"
        )

    # -- 取引ID ごとの足をまとめる ------------------------------------------

    def _id(self, account: str, tid: str, *parts: str) -> str:
        base = "|".join((account, tid, *parts))
        n = self._seen_keys.get(base, 0)
        self._seen_keys[base] = n + 1
        key = base if n == 0 else f"{base}#{n}"
        return SUMM_ID_PREFIX + CanonicalTx.make_id(self.source_id, key)

    def _group(self, account: str, tid: str, legs: list[_Leg]) -> list[CanonicalTx]:
        legs = sorted(legs, key=lambda x: x.ts)

        # SUMM は同じ送金を 2 回載せることがある（取引所 API の出金と、相手の
        # ウォレットの入金から組み立てた送金）。取引ID・向き・通貨・数量が同じ
        # 送金は 1 件にする（時刻の早い方を残す）。
        seen: set[tuple[str, str, Decimal]] = set()
        unique: list[_Leg] = []
        for leg in legs:
            if leg.kind in _SEND or leg.kind in _RECEIVE:
                k = (leg.kind, leg.asset, leg.qty)
                if k in seen:
                    self._skip("SUMM のレポート内で重複した送金")
                    continue
                seen.add(k)
            unique.append(leg)
        legs = unique

        buys = [x for x in legs if x.kind in _BUY]
        sells = [x for x in legs if x.kind in _SELL]
        sends = [x for x in legs if x.kind in _SEND]
        receives = [x for x in legs if x.kind in _RECEIVE]
        # 手数料は通貨ごとに合計し、同じ取引の売買・出金に付ける
        fees: dict[str, Decimal] = {}
        for x in legs:
            if x.kind in _FEE:
                fees[x.asset] = fees.get(x.asset, Decimal(0)) + x.qty
        fee_rows = [x.row for x in legs if x.kind in _FEE]

        manual = tid.endswith("-manual")
        note = (legs[0].row.get("コメント") or "manual") if manual else None
        hash_ = _tx_hash(tid)
        out: list[CanonicalTx] = []

        def tx(leg_or_ts, raw: dict, *parts: str, **fields) -> CanonicalTx:
            ts = leg_or_ts.ts if isinstance(leg_or_ts, _Leg) else leg_or_ts
            return CanonicalTx(
                id=self._id(account, tid, *parts), source=self.source_id,
                timestamp=ts, raw=raw, **fields,
            )

        def take_fee(asset: str | None = None) -> dict:
            """未使用の手数料を 1 通貨ぶん取り出して取引の手数料欄にする。"""
            pick = asset if asset in fees else (None if asset else next(iter(fees), None))
            if pick is None:
                return {}
            return {"fee_asset": pick, "fee_amount": fees.pop(pick)}

        buy_assets = {x.asset for x in buys}
        sell_assets = {x.asset for x in sells}
        if buys and sells and len(buy_assets) == 1 and len(sell_assets) == 1 \
                and buy_assets != sell_assets:
            recv, sent = next(iter(buy_assets)), next(iter(sell_assets))
            out.append(tx(
                buys[0], {"legs": [x.row for x in buys + sells] + fee_rows},
                "trade", recv, sent,
                type=TxType.TRADE,
                received_asset=recv, received_amount=sum(x.qty for x in buys),
                sent_asset=sent, sent_amount=sum(x.qty for x in sells),
                label=note, **take_fee(),
            ))
        else:
            # 相方の無い購入・売却（取引所の外から入ってきた資産を購入に分類した
            # ものなど）は、残高の増減として入出金で残す
            for x in buys:
                out.append(tx(x, x.row, "purchase", x.asset, type=TxType.DEPOSIT,
                              received_asset=x.asset, received_amount=x.qty,
                              label=note or "purchase"))
            for x in sells:
                out.append(tx(x, x.row, "sale", x.asset, type=TxType.WITHDRAW,
                              sent_asset=x.asset, sent_amount=x.qty,
                              label=note or "sale"))

        for x in sends:
            out.append(tx(x, x.row, "send", x.asset, type=TxType.WITHDRAW,
                          sent_asset=x.asset, sent_amount=x.qty,
                          tx_hash=hash_, label=note, **take_fee(x.asset)))
        for x in receives:
            out.append(tx(x, x.row, "receive", x.asset, type=TxType.DEPOSIT,
                          received_asset=x.asset, received_amount=x.qty,
                          tx_hash=hash_, label=note))
        # どの売買・出金にも付かなかった手数料
        for asset in list(fees):
            out.append(tx(legs[0], {"legs": fee_rows}, "fee", asset, type=TxType.FEE,
                          fee_asset=asset, fee_amount=fees.pop(asset), label="fee"))

        for x in legs:
            if x.kind in _BUY or x.kind in _SELL or x.kind in _FEE \
                    or x.kind in _SEND or x.kind in _RECEIVE:
                continue
            if x.kind not in _SINGLE:
                self._skip(f"未対応の取引タイプ: {x.kind or '(空)'}")
                continue
            tx_type, label = _SINGLE[x.kind]
            side = ({"received_asset": x.asset, "received_amount": x.qty}
                    if tx_type == TxType.REWARD else
                    {"fee_asset": x.asset, "fee_amount": x.qty})
            out.append(tx(x, x.row, x.kind, x.asset, type=tx_type, label=label, **side))
        return out
