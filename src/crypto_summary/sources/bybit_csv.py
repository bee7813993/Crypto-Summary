"""Bybit CSV アダプタ（資金調達アカウント履歴 / UTA 取引ログ / 入出金履歴）

Bybit V5 API は期間を指定しないと約定履歴を直近 7 日、入出金を直近 30 日しか
返さない（1 回に指定できる期間もその長さまで）。過去の履歴は「データの
エクスポート」画面の CSV から取り込む。

対象ファイル（どれも 1 行目に "UID: …,Company Name: …" の前置き行がある）:

  AssetChangeDetails_fund_*.csv              資金調達アカウントの残高変動
      Uid, Date & Time(UTC), Coin, QTY, Type, Account Balance, Description
  AssetChangeDetails_uta_*.csv               統合取引アカウント（UTA）の取引ログ
      Uid, Currency, Contract, Type, Direction, Quantity, Position,
      Filled Price, Funding, Fee Paid, Cash Flow, Change, Wallet Balance,
      Action, Time(UTC)
  assetHistory_withdrawDepositHistory_*.csv  入出金履歴
      Uid, Date, Type, Asset, Chain, Amount, Tx ID, Status, Received Address

種別はヘッダーから自動判定する。同じ口座に取り込めば口座全体の保有が組み上がる。

方針:
  - 各行が残高に与える効果は、その CSV の残高列（Account Balance /
    Wallet Balance）の増減と一致させる。資金調達⇔UTA の振替は両側とも
    TRANSFER で記録するので、片方だけ取り込んでもそのアカウントの残高が
    再現でき、両方取り込めば振替は相殺される。
  - Earn（Easy Earn など）の申込・解約は記録しない。運用中の元本も保有として
    数え続けるため（Earn 側の残高を表す CSV が無く、申込で減らすと運用中は
    保有から消えてしまう）。利息の受取は REWARD。
  - 入出金は資金調達アカウント履歴と入出金履歴の両方に載る（時刻は 1 秒ほど
    ずれる）。取り込み時に台帳の既存取引と突き合わせて 1 件にまとめる
    （reconcile）。金額は資金調達側（実際に残高から引かれた額）を、
    Tx ID は入出金履歴側を採る。
  - 知らない種別は推測で分類せず、理由付きでスキップ計上する。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.models import CanonicalTx, TxType
from .base import CsvSourceAdapter, read_csv_text

if TYPE_CHECKING:
    from ..core.ledger import Ledger

_UTC = timezone.utc
_ZERO = Decimal(0)

# 1 回の約定・両替の相手方の行を探す幅。実データでは同時刻か 1 秒違い。
_PAIR_WINDOW = timedelta(seconds=2)

# 資金調達アカウント履歴と入出金履歴の同じ入出金を対応付ける時間幅。
# 金額が一致するなら広めに取り、出金手数料ぶん資金調達側が多いときは狭くする。
_MATCH_WINDOW_EXACT = timedelta(hours=24)
_MATCH_WINDOW_FEE = timedelta(hours=1)

# 種別判定に使う列（各形式に固有の組み合わせ）
_FUND_COLUMNS = ("Date & Time(UTC)", "Coin", "QTY", "Type", "Account Balance", "Description")
_UTA_COLUMNS = (
    "Currency", "Contract", "Type", "Direction", "Funding", "Fee Paid",
    "Cash Flow", "Change", "Wallet Balance", "Time(UTC)",
)
_DW_COLUMNS = ("Date", "Type", "Asset", "Amount", "Tx ID", "Status")

_FORMAT_HINT = (
    "対応形式: 資金調達アカウント履歴（AssetChangeDetails_fund_*.csv）/ "
    "統合取引アカウントの取引ログ（AssetChangeDetails_uta_*.csv）/ "
    "入出金履歴（assetHistory_withdrawDepositHistory_*.csv）"
)

# raw に入れる、入出金の突き合わせ用の情報のキー
_LINK = "_bybit"

# 資金調達アカウント: Earn 等の商品の行。説明文の語で元本の移動か利息かを分ける。
_PRODUCT_TYPES = ("earn", "launchpool", "staking", "savings", "wealth", "dual asset")
_PRINCIPAL_WORDS = (
    "subscription", "subscribe", "redemption", "redeem", "refund", "principal",
    "stake", "unstake", "lock", "unlock",
)
_YIELD_WORDS = ("interest", "yield", "reward", "bonus", "airdrop", "income", "profit", "earning")

# 資金調達アカウント: 受取なら REWARD として記録する種別
_FUND_REWARD_TYPES = ("airdrop", "bonus", "reward", "referral", "commission", "cashback", "rebate")

# UTA: 建玉・決済に伴う損益・資金調達料・手数料を持つ種別（TRADE は現物と分けて扱う）
_UTA_DERIVATIVE_TYPES = {"SETTLEMENT", "DELIVERY", "LIQUIDATION", "ADL"}
# UTA: 両替の片側。CSV では種別が "--" で出るもの（少額資産の両替など）を含む。
_UTA_CONVERT_TYPES = {
    "--", "CONVERT", "CURRENCY_BUY", "CURRENCY_SELL",
    "SPOT_REPAYMENT_BUY", "SPOT_REPAYMENT_SELL",
}
# UTA: 符号で REWARD（増）/ FEE（減）に振り分ける種別 → (増のラベル, 減のラベル)
_UTA_SIGNED_TYPES = {
    "AIRDROP": ("airdrop", "airdrop"),
    "FEE_REFUND": ("fee_refund", "fee_refund"),
    "INTEREST": ("interest", "borrow_interest"),
}
# UTA: Earn 等の商品への申込・解約（元本の移動）
_UTA_PRINCIPAL_WORDS = ("SUBSCRIPTION", "REDEMPTION", "STAKING", "REFUND")

_SKIP_EARN_PRINCIPAL = "Earn 等の申込・解約（元本の移動）"


# ---------------------------------------------------------------------------
# 読み込み・値の解釈
# ---------------------------------------------------------------------------

def _detect_kind(header: list[str]) -> str | None:
    """ヘッダーから CSV の種別（"fund" / "uta" / "dw"）を返す。"""
    cols = {c.strip() for c in header}
    for kind, required in (("uta", _UTA_COLUMNS), ("fund", _FUND_COLUMNS), ("dw", _DW_COLUMNS)):
        if set(required) <= cols:
            return kind
    return None


def _read(path: Path) -> tuple[str | None, list[str], list[dict[str, str]]]:
    """前置き行を飛ばしてヘッダーを探し、(種別, ヘッダー, 行) を返す。

    Bybit の CSV は 1 行目が "UID: 1234567,Company Name: ,Country: " の
    前置き行で、2 行目がヘッダー。前置き行の有無に依らず読めるよう、
    先頭の数行から既知のヘッダーを探す。
    """
    text = read_csv_text(path)
    lines = list(csv.reader(io.StringIO(text)))
    for i, cells in enumerate(lines[:10]):
        header = [c.strip() for c in cells]
        kind = _detect_kind(header)
        if kind is None:
            continue
        rows: list[dict[str, str]] = []
        for values in lines[i + 1:]:
            if not any(v.strip() for v in values):
                continue
            rows.append({
                name: (values[j].strip() if j < len(values) else "")
                for j, name in enumerate(header)
            })
        return kind, header, rows
    # 判別できなかったときの案内用に、前置き行ではなさそうな最初の行を返す
    shown = next(
        (cells for cells in lines[:10] if cells and not cells[0].strip().upper().startswith("UID:")),
        lines[0] if lines else [],
    )
    return None, [c.strip() for c in shown], []


def _dec(value: str | None) -> Decimal | None:
    """数値列を Decimal にする。"--" や空欄は None（Bybit は該当なしを "--" で出す）。"""
    v = (value or "").strip().replace(",", "")
    if not v or v == "--":
        return None
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _clean(d: Decimal) -> Decimal:
    """末尾の 0 を落とす（"1006.81960000000000000000" → 1006.8196）。

    normalize() は 10 を 1E+1 にしてしまうので、整数は指数を戻す。
    """
    n = d.normalize()
    return n.quantize(Decimal(1)) if n.as_tuple().exponent > 0 else n


def _parse_ts(value: str | None) -> datetime | None:
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=_UTC)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt.astimezone(_UTC)


def _norm(value: str) -> str:
    """冪等キー用の正規化。数値は表記の揺れ（末尾の 0 や指数表記）を吸収する。"""
    v = (value or "").strip()
    d = _dec(v)
    return str(d.normalize()) if d is not None else v


class _Keys:
    """行の内容から冪等キーを作る。

    Bybit の CSV には行 ID が無いので、残高列を含む全列を連結してキーにする。
    完全に同じ内容の行が複数あるときは出現順の連番を足して区別する。
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def make(self, *parts: str) -> str:
        base = "|".join(_norm(p) for p in parts)
        n = self._seen.get(base, 0)
        self._seen[base] = n + 1
        return base if n == 0 else f"{base}#{n}"


@dataclass
class _Leg:
    """両替の片側（1 行ぶんの 1 通貨の増減）。"""
    ts: datetime
    asset: str
    amount: Decimal   # 符号付き（正 = 受取）
    key: str
    row: dict[str, str]
    order: int        # ファイル内の行番号（同時刻の並びを決めるため）


@dataclass
class _UtaRow:
    ts: datetime
    currency: str
    contract: str     # 区切り記号を除いた大文字（"BTC/USDT" → "BTCUSDT"）
    type: str         # 大文字。空欄は "--"
    direction: str
    action: str
    position: Decimal | None
    price: str        # 約定のまとめ用（正規化済みの文字列）
    cash_flow: Decimal
    funding: Decimal
    fee: Decimal      # Fee Paid（負 = 支払）
    change: Decimal
    key: str
    row: dict[str, str]
    order: int


def _pair_legs(legs: list[_Leg]) -> tuple[list[tuple[_Leg, _Leg]], list[_Leg]]:
    """両替の行を (送出, 受取) の組にする。組めなかった行は 2 つ目で返す。

    1 回の両替は「送出通貨の減少」と「受取通貨の増加」の 2 行で、同時刻か
    1 秒違いに並ぶ。同じ秒に複数通貨をまとめて両替したとき（少額資産の一括
    両替など）は、CSV からはどの減少とどの増加が対応するか確定できないため、
    直前の未対応の行と組む。どう組んでも各通貨の残高は変わらない。
    """
    pending: list[_Leg] = []
    pairs: list[tuple[_Leg, _Leg]] = []
    for leg in sorted(legs, key=lambda x: (x.ts, -x.order)):
        mate = next(
            (p for p in reversed(pending)
             if (p.amount > 0) != (leg.amount > 0)
             and p.asset != leg.asset
             and leg.ts - p.ts <= _PAIR_WINDOW),
            None,
        )
        if mate is None:
            pending.append(leg)
            continue
        pending.remove(mate)
        pairs.append((mate, leg) if mate.amount < 0 else (leg, mate))
    return pairs, pending


# ---------------------------------------------------------------------------
# 資金調達アカウント履歴と入出金履歴の入出金の突き合わせ
# ---------------------------------------------------------------------------

def _links(tx: CanonicalTx) -> dict:
    link = (tx.raw or {}).get(_LINK)
    return link if isinstance(link, dict) else {}


def _strip_link(raw: dict) -> dict:
    return {k: v for k, v in (raw or {}).items() if k != _LINK}


def _flow(tx: CanonicalTx) -> tuple[str | None, Decimal | None]:
    if tx.type == TxType.DEPOSIT:
        return tx.received_asset, tx.received_amount
    return tx.sent_asset, tx.sent_amount


def _match_score(fund: CanonicalTx, dw: CanonicalTx) -> tuple[float, Decimal] | None:
    """同じ入出金とみなせるなら (時刻差, 金額差) を返す。小さいほど有力。

    入金は金額が一致するもの。出金は、資金調達側が手数料込みの減少額で
    入出金履歴側より多いこともあるため、時刻が近ければ多い側も許す。
    """
    if fund.type != dw.type:
        return None
    f_asset, f_amt = _flow(fund)
    d_asset, d_amt = _flow(dw)
    if not f_asset or f_asset != d_asset or f_amt is None or d_amt is None:
        return None
    gap = abs(fund.timestamp - dw.timestamp)
    if f_amt == d_amt:
        ok = gap <= _MATCH_WINDOW_EXACT
    else:
        ok = (fund.type == TxType.WITHDRAW and d_amt < f_amt <= d_amt * 2
              and gap <= _MATCH_WINDOW_FEE)
    return (gap.total_seconds(), f_amt - d_amt) if ok else None


def _merge(new: CanonicalTx, kind: str, existing: CanonicalTx) -> CanonicalTx:
    """new（いま取り込む側）を existing（台帳の取引）にまとめた取引を返す。

    ID は既存のものを使い、内容は資金調達側、Tx ID は入出金履歴側を採る。
    """
    old = _links(existing)
    if kind == "fund_key":
        link = {"fund_key": _links(new)["fund_key"]}
        if old.get("dw_key"):
            link["dw_key"] = old["dw_key"]
            link["dw_row"] = old.get("dw_row") or _strip_link(existing.raw)
        return new.model_copy(update={
            "id": existing.id,
            "tx_hash": new.tx_hash or existing.tx_hash,
            "raw": {**_strip_link(new.raw), _LINK: link},
        })
    if old.get("fund_key"):
        link = {**old, "dw_key": _links(new)["dw_key"], "dw_row": _strip_link(new.raw)}
        return existing.model_copy(update={
            "tx_hash": new.tx_hash or existing.tx_hash,
            "raw": {**_strip_link(existing.raw), _LINK: link},
        })
    # 既存も入出金履歴由来（同じ CSV の取り込み直し）
    return new.model_copy(update={"id": existing.id})


def merge_deposits_withdrawals(
    new_txs: list[CanonicalTx], existing: list[CanonicalTx]
) -> list[CanonicalTx]:
    """取り込む入出金を、同じ口座の台帳にある同じ入出金へまとめる。

    資金調達アカウント履歴と入出金履歴は同じ入出金を別々に載せている。
    どちらを先に取り込んでも、何度取り込み直しても 1 件にまとまるよう、
    まとめた取引の raw に両方のキーを残し、次からはキーで引き当てる。
    初めて出会う組は資産・金額・時刻で対応付ける。
    """
    pool = [
        e for e in existing
        if e.type in (TxType.DEPOSIT, TxType.WITHDRAW) and _links(e)
    ]
    by_key: dict[tuple[str, str], CanonicalTx] = {}
    for e in pool:
        for k in ("fund_key", "dw_key"):
            if _links(e).get(k):
                by_key[(k, _links(e)[k])] = e

    def kind_of(t: CanonicalTx) -> str | None:
        if t.type not in (TxType.DEPOSIT, TxType.WITHDRAW):
            return None
        link = _links(t)
        return "fund_key" if link.get("fund_key") else "dw_key" if link.get("dw_key") else None

    out = list(new_txs)
    claimed: set[str] = set()
    # 1) 以前にまとめた（または同じ CSV から入れた）取引はキーで引き当てる
    for i, t in enumerate(out):
        kind = kind_of(t)
        e = by_key.get((kind, _links(t)[kind])) if kind else None
        if e is not None and e.id not in claimed:
            claimed.add(e.id)
            out[i] = _merge(t, kind, e)
    # 2) 初めての組は、まだ相方のいない反対側の取引から最も近いものを探す
    for i, t in enumerate(out):
        kind = kind_of(t)
        if kind is None or t.id in claimed:
            continue  # 入出金以外か、1) で引き当て済み
        other = "dw_key" if kind == "fund_key" else "fund_key"
        best: tuple[tuple[float, Decimal], CanonicalTx] | None = None
        for e in pool:
            if e.id in claimed or not _links(e).get(other) or _links(e).get(kind):
                continue
            fund, dw = (t, e) if kind == "fund_key" else (e, t)
            score = _match_score(fund, dw)
            if score is not None and (best is None or score < best[0]):
                best = (score, e)
        if best is not None:
            claimed.add(best[1].id)
            out[i] = _merge(t, kind, best[1])
    return out


# ---------------------------------------------------------------------------
# アダプタ
# ---------------------------------------------------------------------------

class BybitCsvSource(CsvSourceAdapter):
    """Bybit CSV（資金調達アカウント履歴 / UTA 取引ログ / 入出金履歴）自動判定パーサー"""

    def load(self, path: Path) -> list[CanonicalTx]:
        self._reset_skips()
        kind, header, rows = _read(path)
        if kind == "fund":
            return self._load_fund(rows)
        if kind == "uta":
            return self._load_uta(rows)
        if kind == "dw":
            return self._load_dw(rows)
        raise ValueError(
            "Bybit の CSV の種別を判別できませんでした。"
            f"{_FORMAT_HINT}。 ヘッダー: {', '.join(header) or '（空）'}"
        )

    def reconcile(self, txs: list[CanonicalTx], ledger: Ledger) -> list[CanonicalTx]:
        txs = super().reconcile(txs, ledger)  # SUMM のレポートがある期間を飛ばす
        existing = [
            *ledger.all(source=self.source_id, tx_type=TxType.DEPOSIT.value, limit=None),
            *ledger.all(source=self.source_id, tx_type=TxType.WITHDRAW.value, limit=None),
        ]
        return merge_deposits_withdrawals(txs, existing)

    # -- 共通 ---------------------------------------------------------------

    def _tx(self, key: str, ts: datetime, row: dict, **fields) -> CanonicalTx:
        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, key),
            source=self.source_id,
            timestamp=ts,
            raw=fields.pop("raw", None) or dict(row),
            **fields,
        )

    def _signed(
        self, key: str, ts: datetime, row: dict, asset: str, amount: Decimal,
        gain_label: str, loss_label: str,
    ) -> list[CanonicalTx]:
        """符号付きの増減を 増→REWARD / 減→FEE の 1 件にする（0 なら何も返さない）。"""
        if amount > 0:
            return [self._tx(key, ts, row, type=TxType.REWARD,
                             received_asset=asset, received_amount=_clean(amount),
                             label=gain_label)]
        if amount < 0:
            return [self._tx(key, ts, row, type=TxType.FEE,
                             fee_asset=asset, fee_amount=_clean(-amount),
                             label=loss_label)]
        return []

    def _transfer(self, key: str, ts: datetime, row: dict, asset: str, amount: Decimal) -> list[CanonicalTx]:
        """アカウント間の振替。増えた側は受取、減った側は送出として残す。"""
        if amount > 0:
            return [self._tx(key, ts, row, type=TxType.TRANSFER,
                             received_asset=asset, received_amount=_clean(amount),
                             label="account_transfer")]
        if amount < 0:
            return [self._tx(key, ts, row, type=TxType.TRANSFER,
                             sent_asset=asset, sent_amount=_clean(-amount),
                             label="account_transfer")]
        return []

    def _conversions(self, legs: list[_Leg], label_of, lone_reason: str) -> list[CanonicalTx]:
        """両替の行を組にして TRADE にする。相方の無い行はスキップ計上する。"""
        pairs, lone = _pair_legs(legs)
        txs = []
        for out, inc in pairs:
            txs.append(self._tx(
                f"convert|{out.key}|{inc.key}", out.ts, out.row,
                type=TxType.TRADE,
                received_asset=inc.asset, received_amount=_clean(inc.amount),
                sent_asset=out.asset, sent_amount=_clean(-out.amount),
                label=label_of(out),
                raw={"legs": [out.row, inc.row]},
            ))
        for _ in lone:
            self._skip(lone_reason)
        return txs

    # -- 資金調達アカウント履歴 ---------------------------------------------

    def _load_fund(self, rows: list[dict[str, str]]) -> list[CanonicalTx]:
        keys = _Keys()
        txs: list[CanonicalTx] = []
        legs: list[_Leg] = []
        for i, row in enumerate(rows):
            ts = _parse_ts(row.get("Date & Time(UTC)"))
            coin = row.get("Coin", "").upper()
            qty = _dec(row.get("QTY"))
            if ts is None or not coin or qty is None:
                self._skip("読み取れない行")
                continue
            if qty == 0:
                continue  # 残高が動かない行
            typ = row.get("Type", "")
            desc = row.get("Description", "")
            key = keys.make(
                "fund", row.get("Uid", ""), row.get("Date & Time(UTC)", ""), coin,
                row.get("QTY", ""), typ, row.get("Account Balance", ""), desc,
            )
            if typ.strip().lower() == "convert":
                legs.append(_Leg(ts, coin, qty, key, row, i))
                continue
            txs.extend(self._fund_row(key, ts, row, coin, qty, typ, desc))
        txs.extend(self._conversions(
            legs,
            lambda leg: ("small_balance_conversion"
                         if "small" in leg.row.get("Description", "").lower() else "convert"),
            "相手方の無い両替の行",
        ))
        return txs

    def _fund_row(
        self, key: str, ts: datetime, row: dict, coin: str, qty: Decimal, typ: str, desc: str,
    ) -> list[CanonicalTx]:
        t, d = typ.strip().lower(), desc.strip().lower()
        amount = _clean(abs(qty))

        if t == "deposit" or t == "withdraw":
            if t == "withdraw" and "fee" in d:
                return self._signed(key, ts, row, coin, qty, "withdrawal_fee_refund", "withdrawal_fee")
            # 通常の入出金だけを入出金履歴との突き合わせ対象にする
            linkable = (t == "deposit") == (qty > 0)
            raw = {**row, _LINK: {"fund_key": key}} if linkable else dict(row)
            if qty > 0:
                return [self._tx(key, ts, row, type=TxType.DEPOSIT,
                                 received_asset=coin, received_amount=amount,
                                 label=None if t == "deposit" else "withdrawal_refund", raw=raw)]
            return [self._tx(key, ts, row, type=TxType.WITHDRAW,
                             sent_asset=coin, sent_amount=amount,
                             label=None if t == "withdraw" else "deposit_reversal", raw=raw)]

        if t.startswith("transfer"):
            if any(p in d for p in _PRODUCT_TYPES):
                self._skip(_SKIP_EARN_PRINCIPAL)
                return []
            return self._transfer(key, ts, row, coin, qty)

        if any(p in t for p in _PRODUCT_TYPES):
            principal = any(w in d for w in _PRINCIPAL_WORDS)
            earning = any(w in d for w in _YIELD_WORDS)
            if principal and not earning:
                self._skip(_SKIP_EARN_PRINCIPAL)
                return []
            if earning and not principal and qty > 0:
                label = "earn_interest" if "interest" in d else "earn_reward"
                return [self._tx(key, ts, row, type=TxType.REWARD,
                                 received_asset=coin, received_amount=amount, label=label)]
            self._skip(f"未対応の区分: {typ}（{desc}）")
            return []

        reward = next((w for w in _FUND_REWARD_TYPES if w in t), None)
        if reward and qty > 0:
            return [self._tx(key, ts, row, type=TxType.REWARD,
                             received_asset=coin, received_amount=amount, label=reward)]

        self._skip(f"未対応の種別: {typ}（{desc}）")
        return []

    # -- UTA 取引ログ -------------------------------------------------------

    def _load_uta(self, rows: list[dict[str, str]]) -> list[CanonicalTx]:
        keys = _Keys()
        parsed: list[_UtaRow] = []
        for i, row in enumerate(rows):
            ts = _parse_ts(row.get("Time(UTC)"))
            currency = row.get("Currency", "").upper()
            if ts is None or not currency:
                self._skip("読み取れない行")
                continue
            cash_flow = _dec(row.get("Cash Flow")) or _ZERO
            funding = _dec(row.get("Funding")) or _ZERO
            fee = _dec(row.get("Fee Paid")) or _ZERO
            change = _dec(row.get("Change"))
            if change is None:
                change = cash_flow + funding + fee
            elif cash_flow + funding + fee != change:
                # 内訳が残高の増減（Change）と合わない行は、残高を合わせることを優先する
                cash_flow = change - funding - fee
            key = keys.make(
                "uta", row.get("Uid", ""), row.get("Time(UTC)", ""), currency,
                row.get("Contract", ""), row.get("Type", ""), row.get("Direction", ""),
                row.get("Quantity", ""), row.get("Position", ""), row.get("Filled Price", ""),
                row.get("Funding", ""), row.get("Fee Paid", ""), row.get("Cash Flow", ""),
                row.get("Change", ""), row.get("Wallet Balance", ""), row.get("Action", ""),
            )
            parsed.append(_UtaRow(
                ts=ts,
                currency=currency,
                contract="".join(ch for ch in row.get("Contract", "").upper() if ch.isalnum()),
                type=row.get("Type", "").strip().upper() or "--",
                direction=row.get("Direction", "").strip().upper(),
                action=row.get("Action", "").strip().upper(),
                position=_dec(row.get("Position")),
                price=_norm(row.get("Filled Price", "")),
                cash_flow=cash_flow, funding=funding, fee=fee, change=change,
                key=key, row=row, order=i,
            ))

        txs: list[CanonicalTx] = []
        trades: list[_UtaRow] = []
        legs: list[_Leg] = []
        for r in parsed:
            if r.type == "TRADE":
                trades.append(r)
            elif r.type in _UTA_CONVERT_TYPES:
                if r.change != 0:
                    legs.append(_Leg(r.ts, r.currency, r.change, r.key, r.row, r.order))
            elif r.type in ("TRANSFER_IN", "TRANSFER_OUT"):
                txs.extend(self._transfer(r.key, r.ts, r.row, r.currency, r.change))
            elif r.type in _UTA_DERIVATIVE_TYPES:
                txs.extend(self._derivative(r))
            elif r.type in _UTA_SIGNED_TYPES:
                gain, loss = _UTA_SIGNED_TYPES[r.type]
                txs.extend(self._signed(r.key, r.ts, r.row, r.currency, r.change, gain, loss))
            elif any(w in r.type for w in _UTA_PRINCIPAL_WORDS):
                self._skip(_SKIP_EARN_PRINCIPAL)
            else:
                self._skip(f"未対応の種別: {r.type}")
        txs.extend(self._uta_trades(trades))
        txs.extend(self._conversions(legs, lambda leg: "convert", "相手方の無い両替の行（種別なし）"))
        return txs

    def _uta_trades(self, trades: list[_UtaRow]) -> list[CanonicalTx]:
        """TRADE 行を、現物の約定（2 通貨の行の組）と先物等の約定に分ける。

        現物の約定は基軸通貨と決済通貨の 2 行に分かれて載る（例: ETHUSDT の売りは
        ETH の減少と USDT の増加）。同じ銘柄・売買方向・約定価格・時刻の行を束ね、
        通貨 2 つを連結すると銘柄名になる組を現物の約定とみなす。
        """
        buckets: dict[tuple[str, str, str, str], list[_UtaRow]] = {}
        for r in trades:
            buckets.setdefault((r.contract, r.direction, r.price, r.action), []).append(r)

        txs: list[CanonicalTx] = []
        for rows in buckets.values():
            rows.sort(key=lambda x: (x.ts, -x.order))
            clusters: list[list[_UtaRow]] = []
            for r in rows:
                if clusters and r.ts - clusters[-1][0].ts <= _PAIR_WINDOW:
                    clusters[-1].append(r)
                else:
                    clusters.append([r])
            for cluster in clusters:
                spot = self._spot_trade(cluster)
                if spot is not None:
                    txs.extend(spot)
                else:
                    for r in cluster:
                        txs.extend(self._trade_row(r))
        return txs

    def _spot_trade(self, cluster: list[_UtaRow]) -> list[CanonicalTx] | None:
        currencies = sorted({r.currency for r in cluster})
        contract = cluster[0].contract
        pair = next(
            ((a, b) for a in currencies for b in currencies if a != b and a + b == contract),
            None,
        )
        if pair is None:
            return None
        legs = [r for r in cluster if r.currency in pair]
        cash: dict[str, Decimal] = {c: _ZERO for c in pair}
        fees: dict[str, Decimal] = {c: _ZERO for c in pair}
        funding: dict[str, Decimal] = {c: _ZERO for c in pair}
        for r in legs:
            cash[r.currency] += r.cash_flow
            fees[r.currency] += r.fee
            funding[r.currency] += r.funding
        recv = [c for c in pair if cash[c] > 0]
        sent = [c for c in pair if cash[c] < 0]
        if len(recv) != 1 or len(sent) != 1:
            return None
        recv_cur, sent_cur = recv[0], sent[0]

        key = "spot|" + "|".join(sorted(r.key for r in legs))
        ts, row = legs[0].ts, legs[0].row
        trade: dict = dict(
            type=TxType.TRADE,
            received_asset=recv_cur, received_amount=_clean(cash[recv_cur]),
            sent_asset=sent_cur, sent_amount=_clean(-cash[sent_cur]),
            raw={"legs": [r.row for r in legs]},
        )
        extra: list[CanonicalTx] = []
        for cur in (recv_cur, sent_cur):
            paid = -fees[cur]
            if paid > 0 and "fee_asset" not in trade:
                trade.update(fee_asset=cur, fee_amount=_clean(paid))
            else:
                extra += self._signed(f"{key}|fee|{cur}", ts, row, cur, fees[cur],
                                      "fee_rebate", "trading_fee")
            extra += self._signed(f"{key}|funding|{cur}", ts, row, cur, funding[cur],
                                  "futures_funding_profit", "futures_funding_loss")
        # 手数料を第 3 の通貨で払った行など、組に入らない行
        for r in cluster:
            if r.currency not in pair:
                extra += self._signed(r.key, r.ts, r.row, r.currency, r.change,
                                      "fee_rebate", "trading_fee")
        return [self._tx(key, ts, row, **trade), *extra]

    def _trade_row(self, r: _UtaRow) -> list[CanonicalTx]:
        """組にならなかった TRADE 行。先物等の約定なら損益・手数料を記録する。"""
        derivative = (
            r.action in ("OPEN", "CLOSE")
            or (r.position is not None and r.position != 0)
            or r.funding != 0
        )
        spot_like = r.contract != r.currency and (
            r.contract.startswith(r.currency) or r.contract.endswith(r.currency)
        )
        if derivative or not spot_like:
            return self._derivative(r)
        # 現物の約定の片側だけが見つかった行。損益と取り違えないよう売買の片側として残す
        side = (dict(received_asset=r.currency, received_amount=_clean(r.cash_flow))
                if r.cash_flow > 0 else
                dict(sent_asset=r.currency, sent_amount=_clean(-r.cash_flow)))
        txs = [] if r.cash_flow == 0 else [self._tx(r.key, r.ts, r.row, type=TxType.TRADE, **side)]
        return txs + self._signed(f"{r.key}|fee", r.ts, r.row, r.currency, r.fee,
                                  "fee_rebate", "trading_fee")

    def _derivative(self, r: _UtaRow) -> list[CanonicalTx]:
        """先物・無期限の約定／決済／資金調達料の行。

        証拠金取引のため建玉自体は資産の増減にしない（Nexo 先物・bitFlyer 証拠金と
        同じ実現損益ベース）。Cash Flow を実現損益、Funding を資金調達料、
        Fee Paid を手数料として、それぞれ 利益→REWARD / 損失→FEE で記録する。
        """
        return [
            *self._signed(f"{r.key}|pnl", r.ts, r.row, r.currency, r.cash_flow,
                          "futures_realized_profit", "futures_realized_loss"),
            *self._signed(f"{r.key}|funding", r.ts, r.row, r.currency, r.funding,
                          "futures_funding_profit", "futures_funding_loss"),
            *self._signed(f"{r.key}|fee", r.ts, r.row, r.currency, r.fee,
                          "futures_fee_rebate", "futures_fee"),
        ]

    # -- 入出金履歴 ---------------------------------------------------------

    def _load_dw(self, rows: list[dict[str, str]]) -> list[CanonicalTx]:
        keys = _Keys()
        txs: list[CanonicalTx] = []
        for row in rows:
            status = row.get("Status", "")
            if not any(w in status.lower() for w in ("success", "complete")):
                self._skip(f"未完了の入出金（{status or '状態なし'}）")
                continue
            ts = _parse_ts(row.get("Date"))
            asset = row.get("Asset", "").upper()
            amount = _dec(row.get("Amount"))
            kind = row.get("Type", "").strip().lower()
            if ts is None or not asset or amount is None:
                self._skip("読み取れない行")
                continue
            if amount == 0:
                continue  # 残高が動かない行
            tx_hash = row.get("Tx ID", "").strip()
            key = keys.make(
                "dw", row.get("Uid", ""), row.get("Date", ""), kind, asset,
                row.get("Chain", ""), row.get("Amount", ""), tx_hash,
                row.get("Received Address", ""),
            )
            common = dict(
                tx_hash=None if tx_hash in ("", "--") else tx_hash,
                raw={**row, _LINK: {"dw_key": key}},
            )
            if kind.startswith("deposit"):
                txs.append(self._tx(key, ts, row, type=TxType.DEPOSIT,
                                    received_asset=asset, received_amount=_clean(abs(amount)),
                                    **common))
            elif kind.startswith("withdraw"):
                txs.append(self._tx(key, ts, row, type=TxType.WITHDRAW,
                                    sent_asset=asset, sent_amount=_clean(abs(amount)),
                                    **common))
            else:
                self._skip(f"未対応の種別: {row.get('Type', '')}")
        return txs
