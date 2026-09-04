"""PBR Lending クローラー出力 (正規化 JSON) アダプタ

対象ファイル: PBRLending-History-Check が出力する
``outputs/pbrlending_crawled.latest.json``

PBR Lending は当年分の公式取引履歴 CSV を提供しないため、当年のデータは
ローカルのクローラーで取得する。その正規化 JSON を直接読み込み、
入出金履歴 CSV (pbr_transfers) と同じ意味論・同じラベルで台帳へ展開する。

CSV ではなく JSON を読む理由:
  クローラーが出力する CSV(公式フォーマット互換)には日次利息が
  「総単日受取予定利息」として入っているが、既存の日次レポートアダプタは
  予定利息を記録しない。JSON なら日次利息・満期利息・入出金がすべて揃う。

JSON の構造と本アダプタの対応:
  daily_ranges[]    : 契約ごとの日次利息を期間で圧縮した表現。
                      {date_from, date_to, currency, daily_expected_interest}
                      → 日付×通貨で展開・合算し REWARD(daily_interest)
                      返還日・満期日は区間から除かれており、その日の利息は
                      wallet_events 側に現れる。よって二重計上しない。
  wallet_events[]   : {date, currency, type, amount}
                      返還利息 → REWARD(return_interest)
                      満期     → REWARD(premium_maturity_interest)
                      貸出/返還 → スキップ(貸出準備ウォレット ⇔ 貸出 の内部移動)
  transfer_events[] : {date, currency, type, amount}
                      入庫 → DEPOSIT(pbr_deposit)
                      出庫 → WITHDRAW(pbr_withdrawal) ※数量は負で入っている
                      システム移行 → スキップ(数量 0 の移行記録)

金額の正規化:
  JSON には ``"8.3E-7"`` のような指数表記が現れる。取引 ID は raw_key の
  ハッシュなので、表記が揺れると同じイベントが別 ID になる。
  ``f"{Decimal(v):f}"`` で常に固定小数点表記へ正規化してから使う。

同一日・同一額のイベント:
  実データに「2026-04-28 BTC 返還 0.0025」が 2 件あるように、同じ日に同額の
  イベントが複数回起きる。ソートしてから 2 件目以降にのみ ``|#n`` を付けて
  ID を分ける(入力順に依存しない)。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date as _date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...core.ledger import Ledger
from ...core.models import CanonicalTx, TxType
from ..base import CsvSourceAdapter

#: クローラーの出力ファイル名(いずれも outputs ディレクトリ直下)
ARTIFACT_NAME = "pbrlending_crawled.latest.json"
MARKER_NAME = "last_crawl.json"

#: ビューアに手動インポートした公式 CSV の保存先。
#: クロールは当年分しか持たないため、過年度の公式データはここにしか無い。
VIEWER_LEDGER_NAME = "viewer_ledger.json"
VIEWER_TRANSFERS_NAME = "viewer_transfers.json"

#: outputs ディレクトリを指す環境変数
ENV_VAR = "PBR_CRAWL_DIR"

#: このアダプタが書き込むソース ID(公式 CSV 由来の "pbr" とは分離する)
SOURCE_ID = "pbr_crawl"

#: 洗い替え時に同じ期間から取り除く旧ソース。
#: 手作りの CSV で取り込んだクロール期間のデータを一度だけ掃除するためのもの。
#: 当年の窓でのみ適用する（_delete_sources を参照）。
LEGACY_SOURCES: tuple[str, ...] = ("pbr",)

#: 残高照合の許容誤差(丸め差)
RECON_TOLERANCE = Decimal("0.000001")

#: 取り込み元ファイルの更新からこの秒数は自動取り込みを見送る。
#: ファイル同期でディレクトリへ運ぶ場合、複数ファイルが順に届くため。
#: 手動の同期ボタン・CLI はこの待ちを無視する（利用者が明示的に選んでいる）。
SETTLE_SECONDS = 30

#: 取り込み結果の形式。取り込む対象や変換規則を変えたらこれを上げる。
#: 同じクロール結果でも結果が変わるため、記録済みの版が古ければ再同期する。
#:   1: クロール結果 (pbrlending_crawled.latest.json) のみ
#:   2: ビューアへ手動インポートした過年度データ (viewer_*.json) も取り込む
SYNC_FORMAT_VERSION = 2

_DATE_FMT = "%Y-%m-%d"
_ZERO = Decimal("0")

# 貸出準備ウォレット ⇔ 貸出 の内部移動。PBR 全体の残高は動かない。
_INTERNAL_MOVE_TYPES = frozenset({"貸出", "返還"})

# wallet_events の利息系 → CanonicalTx.label。
# ラベルは pbr_transfers / pbr_lending と揃えてある(シンクの分類が一致する)。
_WALLET_REWARD_TYPES: dict[str, str] = {
    "返還利息": "return_interest",
    "満期":     "premium_maturity_interest",
}

# transfer_events の入出金 → (TxType, label)
_TRANSFER_TYPES: dict[str, tuple[TxType, str]] = {
    "入庫": (TxType.DEPOSIT, "pbr_deposit"),
    "出庫": (TxType.WITHDRAW, "pbr_withdrawal"),
}

# 数量 0 の移行記録。データとして意味がないので黙って落とす。
_TRANSFER_SKIP_TYPES = frozenset({"システム移行"})

_EVENT_KEYS = ("daily_ranges", "wallet_events", "transfer_events")

# ビューアの入出金 (viewer_transfers.json) の区分 → (TxType, label)。
# 公式の入出金履歴 CSV と同じ 5 列なので pbr_transfers と同じ扱いにする。
_VIEWER_TRANSFER_TYPES: dict[str, tuple[TxType, str]] = {
    "入庫": (TxType.DEPOSIT, "pbr_deposit"),
    "出庫": (TxType.WITHDRAW, "pbr_withdrawal"),
    "利息": (TxType.REWARD, "daily_interest"),
    "返還利息": (TxType.REWARD, "return_interest"),
    "プレミアム満期": (TxType.REWARD, "premium_maturity_interest"),
}

# ビューアの日次レポート (viewer_ledger.json) の利確列 → label。
# 予定利息の列は「未受取」なので取らない (pbr_lending と同じ判断)。
_VIEWER_LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("返還受取利息（利確数量）",          "return_interest"),
    ("プレミアム移行受取利息（利確数量）", "premium_migration_interest"),
    ("プレミアム満期受取利息（利確数量）", "premium_maturity_interest"),
    ("運営からの付与数量（利確数量）",     "admin_grant"),
)


def normalize_amount(value: str | Decimal | int | float) -> str:
    """指数表記を含む数量を固定小数点の文字列へ正規化する。

    ``"8.3E-7"`` → ``"0.00000083"``。取引 ID の安定性のために使う。
    """
    return f"{Decimal(str(value)):f}"


def _utc_midnight(date_str: str) -> datetime:
    """``YYYY-MM-DD`` を UTC 深夜の datetime にする(pbr_transfers と同じ規約)。"""
    return datetime.strptime(date_str, _DATE_FMT).replace(tzinfo=timezone.utc)


def read_artifact(path: Path) -> dict:
    """正規化 JSON を読み込む。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _display_amount(value: Decimal) -> str:
    """表示用に丸めた数量文字列。

    台帳の残高は取引ごとの Decimal を足し上げた値なので
    ``0.26294604999999999880`` や ``0E-10`` のような読みにくい表記になる。
    クローラーと同じ 10 桁で丸めてから末尾ゼロを落とす。
    """
    quantized = value.quantize(Decimal("1E-10")) if value.as_tuple().exponent < -10 else value
    return f"{quantized.normalize():f}"


class PbrCrawlJsonSource(CsvSourceAdapter):
    """PBR Lending クローラー正規化 JSON パーサー"""

    #: 日次の利息を記録するか。False にすると REWARD を作らず
    #: skip_reasons["daily_interest_disabled"] に計上する(pbr_transfers と同じ)。
    record_daily_interest: bool = True

    def load(self, path: Path) -> list[CanonicalTx]:
        return self.load_data(read_artifact(Path(path)))

    def load_data(self, data: dict) -> list[CanonicalTx]:
        """パース済みの JSON から取引を組み立てる（同期処理からの再パースを避ける）。"""
        self._reset_skips()
        if not isinstance(data, dict) or not any(k in data for k in _EVENT_KEYS):
            raise ValueError(
                "PBR Lending クローラーの正規化 JSON ではありません"
                f"（{' / '.join(_EVENT_KEYS)} のいずれの項目も見つかりません）。"
            )

        # 同一日・同一額の重複に連番を振るためのカウンタ。
        # wallet と transfer で種別が重ならないので共有して問題ない。
        occurrences: dict[str, int] = {}

        txs: list[CanonicalTx] = []
        txs.extend(self._daily_interest_txs(data.get("daily_ranges") or []))
        txs.extend(self._wallet_event_txs(
            data.get("wallet_events") or [], occurrences))
        txs.extend(self._transfer_event_txs(
            data.get("transfer_events") or [], occurrences))
        txs.sort(key=lambda t: (t.timestamp, t.id))
        return txs

    # ------------------------------------------------------------------
    # daily_ranges → 日次利息
    # ------------------------------------------------------------------

    def _daily_interest_txs(self, ranges: list[dict]) -> list[CanonicalTx]:
        totals: dict[tuple[str, str], Decimal] = {}
        for entry in ranges:
            try:
                start = _date.fromisoformat(str(entry["date_from"]))
                end = _date.fromisoformat(str(entry["date_to"]))
                amount = Decimal(str(entry["daily_expected_interest"]))
            except (KeyError, ValueError, InvalidOperation):
                self._skip("invalid_daily_range")
                continue
            asset = str(entry.get("currency", "")).upper()
            if not asset or end < start:
                self._skip("invalid_daily_range")
                continue
            day = start
            while day <= end:
                key = (day.isoformat(), asset)
                totals[key] = totals.get(key, _ZERO) + amount
                day += timedelta(days=1)

        txs: list[CanonicalTx] = []
        for (day_str, asset), total in sorted(totals.items()):
            if total <= _ZERO:
                continue
            if not self.record_daily_interest:
                self._skip("daily_interest_disabled")
                continue
            amount_str = normalize_amount(total)
            # 区分名は入出金履歴 CSV の「利息」に合わせる(raw_key の構造も同じ)。
            raw_key = f"{day_str}|利息|{asset}|{amount_str}"
            txs.append(CanonicalTx(
                id=CanonicalTx.make_id(self.source_id, raw_key),
                source=self.source_id,
                timestamp=_utc_midnight(day_str),
                type=TxType.REWARD,
                received_asset=asset,
                received_amount=Decimal(amount_str),
                label="daily_interest",
                raw={
                    "日付": day_str,
                    "通貨種別": asset,
                    "区分": "利息",
                    "数量": amount_str,
                    "由来": "daily_ranges",
                },
            ))
        return txs

    # ------------------------------------------------------------------
    # wallet_events / transfer_events
    # ------------------------------------------------------------------

    def _wallet_event_txs(
        self, events: list[dict], occurrences: dict[str, int]
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        for kind, asset, amount, day_str, event in self._iter_events(events):
            if kind in _INTERNAL_MOVE_TYPES:
                self._skip("internal_move")
                continue
            label = _WALLET_REWARD_TYPES.get(kind)
            if label is None:
                self._skip(f"unknown_wallet_event:{kind}")
                continue
            txs.append(self._build_tx(
                day_str, kind, asset, abs(amount), occurrences, event,
                tx_type=TxType.REWARD, label=label,
            ))
        return txs

    def _transfer_event_txs(
        self, events: list[dict], occurrences: dict[str, int]
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        for kind, asset, amount, day_str, event in self._iter_events(events):
            if kind in _TRANSFER_SKIP_TYPES:
                continue
            mapped = _TRANSFER_TYPES.get(kind)
            if mapped is None:
                self._skip(f"unknown_transfer_event:{kind}")
                continue
            tx_type, label = mapped
            txs.append(self._build_tx(
                day_str, kind, asset, abs(amount), occurrences, event,
                tx_type=tx_type, label=label,
            ))
        return txs

    def _iter_events(self, events: list[dict]):
        """イベントを検証しつつ (種別, 資産, 数量, 日付, 元データ) で返す。

        ID を入力順に依存させないため、日付・通貨・種別・数量でソートしてから返す。
        """
        parsed: list[tuple[str, str, Decimal, str, dict]] = []
        for event in events:
            try:
                day_str = _date.fromisoformat(str(event["date"])).isoformat()
                amount = Decimal(str(event["amount"]))
            except (KeyError, ValueError, InvalidOperation):
                self._skip("invalid_event")
                continue
            asset = str(event.get("currency", "")).upper()
            kind = str(event.get("type", ""))
            if not asset or not kind:
                self._skip("invalid_event")
                continue
            if amount == _ZERO:
                continue
            parsed.append((kind, asset, amount, day_str, event))
        parsed.sort(key=lambda e: (e[3], e[1], e[0], normalize_amount(e[2])))
        return parsed

    def _build_tx(
        self, day_str: str, kind: str, asset: str, amount: Decimal,
        occurrences: dict[str, int], event: dict, *,
        tx_type: TxType, label: str,
    ) -> CanonicalTx:
        amount_str = normalize_amount(amount)
        base_key = f"{day_str}|{kind}|{asset}|{amount_str}"
        seen = occurrences.get(base_key, 0) + 1
        occurrences[base_key] = seen
        # 1 件目は素の base_key。後から同額の兄弟が増えても既存 ID は変わらない。
        raw_key = base_key if seen == 1 else f"{base_key}|#{seen}"

        raw = {
            "日付": day_str,
            "通貨種別": asset,
            "区分": kind,
            "数量": normalize_amount(event.get("amount", amount)),
            "由来": "wallet_events" if kind in _WALLET_REWARD_TYPES
                    or kind in _INTERNAL_MOVE_TYPES else "transfer_events",
        }
        if seen > 1:
            raw["重複連番"] = seen

        fields: dict = {
            "id": CanonicalTx.make_id(self.source_id, raw_key),
            "source": self.source_id,
            "timestamp": _utc_midnight(day_str),
            "type": tx_type,
            "label": label,
            "raw": raw,
        }
        if tx_type is TxType.WITHDRAW:
            fields["sent_asset"] = asset
            fields["sent_amount"] = Decimal(amount_str)
        else:
            fields["received_asset"] = asset
            fields["received_amount"] = Decimal(amount_str)
        return CanonicalTx(**fields)


class PbrViewerStateSource(CsvSourceAdapter):
    """ビューアに手動インポートされた公式データ (viewer_*.json) を読む。

    クロール結果は当年分しか持たないため、過年度の公式データはビューアに
    手動インポートしたものが唯一の情報源になる。ファイルは公式 CSV と同じ
    列構成なので、pbr_transfers / pbr_lending と同じ意味論で変換する。

    二重計上を避けるための 2 つの制約:
      1. ``crawl_window`` の期間は読まない。クロールがカバーする期間は
         クロール結果を正とする（viewer_ledger.json はクロール分と手動分が
         混在した表示用の状態なので、そのまま全部読むと重複する）。
         その期間の外側は前後どちらも取り込む。クロール結果が無ければ
         (``crawl_window is None``) 全期間を取り込む。
      2. ``skip_years`` に入っている年は丸ごと読まない。呼び出し側が
         「公式 CSV を Crypto-Summary に直接取り込み済みの年」を渡す。
    """

    def load(self, path: Path) -> list[CanonicalTx]:  # pragma: no cover - 未使用
        raise NotImplementedError("load_viewer_state を使ってください")

    def load_viewer_state(
        self, crawl_dir: Path, *,
        crawl_window: tuple[datetime, datetime] | None,
        skip_years: frozenset[int],
    ) -> list[CanonicalTx]:
        self._reset_skips()
        occurrences: dict[str, int] = {}
        txs: list[CanonicalTx] = []
        txs.extend(self._transfer_txs(
            _viewer_rows(crawl_dir / VIEWER_TRANSFERS_NAME),
            crawl_window, skip_years, occurrences,
        ))
        txs.extend(self._ledger_txs(
            _viewer_rows(crawl_dir / VIEWER_LEDGER_NAME),
            crawl_window, skip_years, occurrences,
        ))
        txs.sort(key=lambda t: (t.timestamp, t.id))
        return txs

    def _in_scope(
        self, row: dict, crawl_window: tuple[datetime, datetime] | None,
        skip_years: frozenset[int],
    ) -> str | None:
        """採用する行なら日付文字列を、対象外なら None を返す。"""
        raw_date = str(row.get("日付", "")).strip()
        try:
            day = _date.fromisoformat(raw_date)
        except ValueError:
            return None
        if day.year in skip_years:
            # 公式 CSV を直接取り込み済みの年。二重計上になるので読まない。
            self._skip(f"official_import_exists:{day.year}")
            return None
        if crawl_window is not None:
            crawl_start, crawl_end = crawl_window
            if crawl_start <= _utc_midnight(day.isoformat()) < crawl_end:
                # クロールがカバーする期間。クロール結果を正とする。
                return None
        return day.isoformat()

    def _transfer_txs(
        self, rows: list[dict], crawl_window: tuple[datetime, datetime] | None,
        skip_years: frozenset[int], occurrences: dict[str, int],
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        for row in sorted(rows, key=_viewer_sort_key):
            day_str = self._in_scope(row, crawl_window, skip_years)
            if day_str is None:
                continue
            kind = str(row.get("区分", "")).strip()
            if kind in _TRANSFER_SKIP_TYPES:
                continue
            if kind in _INTERNAL_MOVE_TYPES:
                self._skip("internal_move")
                continue
            mapped = _VIEWER_TRANSFER_TYPES.get(kind)
            if mapped is None:
                self._skip(f"unknown_kubun:{kind}")
                continue
            amount = _decimal_or_none(row.get("数量"))
            if amount is None or amount == _ZERO:
                continue
            tx_type, label = mapped
            txs.append(self._build_viewer_tx(
                day_str, kind, row.get("通貨種別"), abs(amount), occurrences,
                tx_type=tx_type, label=label, origin=VIEWER_TRANSFERS_NAME,
            ))
        return txs

    def _ledger_txs(
        self, rows: list[dict], crawl_window: tuple[datetime, datetime] | None,
        skip_years: frozenset[int], occurrences: dict[str, int],
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        for row in sorted(rows, key=_viewer_sort_key):
            day_str = self._in_scope(row, crawl_window, skip_years)
            if day_str is None:
                continue
            for column, label in _VIEWER_LEDGER_COLUMNS:
                amount = _decimal_or_none(row.get(column))
                if amount is None or amount <= _ZERO:
                    continue
                txs.append(self._build_viewer_tx(
                    day_str, column, row.get("通貨種別"), amount, occurrences,
                    tx_type=TxType.REWARD, label=label,
                    origin=VIEWER_LEDGER_NAME,
                ))
        return txs

    def _build_viewer_tx(
        self, day_str: str, kind: str, currency, amount: Decimal,
        occurrences: dict[str, int], *,
        tx_type: TxType, label: str, origin: str,
    ) -> CanonicalTx:
        asset = str(currency or "").upper()
        amount_str = normalize_amount(amount)
        # raw_key の構造はクロール由来と同じ。同じ日・同じ区分・同じ額の行が
        # 複数あっても潰れないよう 2 件目以降に連番を付ける。
        base_key = f"{day_str}|{kind}|{asset}|{amount_str}"
        seen = occurrences.get(base_key, 0) + 1
        occurrences[base_key] = seen
        raw_key = base_key if seen == 1 else f"{base_key}|#{seen}"

        fields: dict = {
            "id": CanonicalTx.make_id(self.source_id, raw_key),
            "source": self.source_id,
            "timestamp": _utc_midnight(day_str),
            "type": tx_type,
            "label": label,
            "raw": {
                "日付": day_str, "通貨種別": asset, "区分": kind,
                "数量": amount_str, "由来": origin,
            },
        }
        if tx_type is TxType.WITHDRAW:
            fields["sent_asset"] = asset
            fields["sent_amount"] = Decimal(amount_str)
        else:
            fields["received_asset"] = asset
            fields["received_amount"] = Decimal(amount_str)
        return CanonicalTx(**fields)


def _file_stamp(path: Path) -> str | None:
    """ファイルの更新時刻とサイズ。内容が変わったかの判定に使う。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return f"{int(st.st_mtime)}:{st.st_size}"


#: 取り込み元として見るファイルと、その役割。
#: ファイル同期で運ぶ場合、この一覧が「同期すべきファイル」でもある。
_SOURCE_FILES: tuple[tuple[str, str], ...] = (
    (ARTIFACT_NAME, "crawl"),        # クロール結果（当年分）
    (MARKER_NAME, "marker"),         # クロールの成否
    (VIEWER_TRANSFERS_NAME, "viewer"),  # 手動インポート（入出金）
    (VIEWER_LEDGER_NAME, "viewer"),     # 手動インポート（日次レポート）
)


def _source_file_info(directory: Path, name: str, role: str) -> dict:
    """取り込み元ファイル 1 件の状態（届いているか・いつ・サイズ）。"""
    path = directory / name
    try:
        st = path.stat()
    except OSError:
        return {"name": name, "role": role, "found": False,
                "mtime": None, "size": None}
    return {
        "name": name,
        "role": role,
        "found": True,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "size": st.st_size,
    }


def _is_settling(paths: list[Path], within_seconds: int = SETTLE_SECONDS) -> bool:
    """直近に更新されたファイルがあるか。

    ファイル同期でディレクトリへ運ぶ運用では、複数ファイルが順に届く。
    片方だけ新しい状態や書き込み途中を読まないよう、更新直後は待つ。
    ローカルのクロール直後にも効くが、クロールは数分かかるので実害はない。
    """
    now = time.time()
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if 0 <= now - mtime < within_seconds:
            return True
    return False


def _viewer_rows(path: Path) -> list[dict]:
    """ビューアの状態ファイルを読む。

    ``{"version": 2, "rows": [...]}`` と、旧形式の素の配列の両方に対応する。
    ファイルが無い・壊れている場合は空リストを返す（連携は任意機能なので
    ビューアを使っていなくても同期を止めない）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict)]


def _viewer_sort_key(row: dict) -> tuple:
    """入力順に依存しない ID を作るための並び順。"""
    return (
        str(row.get("日付", "")),
        str(row.get("通貨種別", "")),
        str(row.get("区分", "")),
        str(row.get("数量", "")),
    )


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


# ======================================================================
# 同期処理
#
# クローラーは同じ期間を何度でも取り直す（契約の訂正・利率変更が過去日に
# 反映されることがある）。そのため追記ではなく「クロールが扱う期間を丸ごと
# 置き換える」方式にしてある。何度実行しても結果が同じになり、行が消えた
# 場合にも追随できる。
# ======================================================================

class PbrSyncError(Exception):
    """同期を中断した理由を機械可読なコード付きで表す。

    code: not_configured | artifact_missing | artifact_invalid |
          marker_missing | unhealthy | no_rows | state_unwritable
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_crawl_dir(crawl_dir: str | Path | None = None) -> Path | None:
    """引数 → 環境変数 の順に outputs ディレクトリを解決する。"""
    if crawl_dir:
        return Path(crawl_dir)
    env = os.environ.get(ENV_VAR, "").strip()
    return Path(env) if env else None


def _parse_iso(value: str | None) -> datetime | None:
    """クローラーが書く ISO 文字列（末尾 Z）を datetime にする。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_crawl_status(crawl_dir: str | Path) -> dict:
    """クロール結果の状態を読む（同期はしない）。

    ``healthy`` が False の場合、少なくとも 1 通貨の利息が欠けている可能性が
    あるため、既定では同期しない（force で上書き可）。
    """
    directory = Path(crawl_dir)
    artifact = directory / ARTIFACT_NAME
    marker = directory / MARKER_NAME
    warnings: list[str] = []

    viewer_files = [
        name for name in (VIEWER_TRANSFERS_NAME, VIEWER_LEDGER_NAME)
        if (directory / name).is_file()
    ]

    status: dict = {
        "crawl_dir": str(directory),
        # 取り込み元ファイルの到着状況。ファイル同期で運ぶ運用で、
        # 何が届いていて何が来ていないかを画面で確かめられるようにする。
        "files": [_source_file_info(directory, name, role)
                  for name, role in _SOURCE_FILES],
        "artifact_found": artifact.is_file(),
        "artifact_mtime": None,
        "marker_found": marker.is_file(),
        "run_id": None,
        "phase": None,
        "failed_currencies": [],
        "started_at": None,
        "finished_at": None,
        "end_date": None,
        "currencies": [],
        "crawl_warnings": [],
        "viewer_files": viewer_files,
        "healthy": False,
        "warnings": warnings,
    }

    if status["artifact_found"]:
        status["artifact_mtime"] = datetime.fromtimestamp(
            artifact.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        try:
            data = read_artifact(artifact)
        except (OSError, json.JSONDecodeError):
            warnings.append(f"{ARTIFACT_NAME} を読み取れませんでした")
        else:
            if isinstance(data, dict):
                status["end_date"] = data.get("end_date")
                status["currencies"] = list(data.get("currencies") or [])
                status["crawl_warnings"] = list(data.get("warnings") or [])

    if status["marker_found"]:
        try:
            marker_data = read_artifact(marker)
        except (OSError, json.JSONDecodeError):
            warnings.append(f"{MARKER_NAME} を読み取れませんでした")
            marker_data = {}
        if isinstance(marker_data, dict):
            status["run_id"] = marker_data.get("runId")
            status["phase"] = marker_data.get("phase")
            status["failed_currencies"] = list(
                marker_data.get("failedCurrencies") or [])
            status["started_at"] = marker_data.get("startedAt")
            status["finished_at"] = marker_data.get("finishedAt")

    status["healthy"] = bool(
        status["artifact_found"]
        and status["marker_found"]
        and status["phase"] == "done"
        and not status["failed_currencies"]
    )

    if status["phase"] and status["phase"] != "done":
        warnings.append(f"クロールが完了していません（phase={status['phase']}）")
    if status["failed_currencies"]:
        warnings.append(
            "取得に失敗した通貨があります: "
            + ", ".join(status["failed_currencies"])
        )

    # マーカーの方が新しい = クロール中にファイルがまだ書き換わっていない可能性。
    finished = _parse_iso(status["finished_at"])
    mtime = _parse_iso(status["artifact_mtime"])
    if finished and mtime and mtime < finished:
        warnings.append(
            f"{ARTIFACT_NAME} が最後のクロールより古いままです（実行中の可能性）"
        )

    # 取り込める材料があるか。クロール結果が無くても、ビューアへの手動
    # インポートだけで取り込めることがある。
    status["has_data"] = bool(status["artifact_found"] or viewer_files)

    # ファイル同期（Syncthing 等）で運ぶ場合、複数のファイルが順に届く。
    # 届いた直後は片方だけ新しい・書き込み途中ということがあるので、
    # 更新が落ち着くまで自動取り込みを見送る。
    status["settling"] = _is_settling(
        [artifact, *(directory / name for name in viewer_files)])
    if status["settling"]:
        warnings.append("取り込み元のファイルが更新された直後です（整定待ち）")

    # 自動取り込みを止める条件。クロール結果があるのに正常終了していない場合と、
    # ファイルがまだ落ち着いていない場合。
    status["blocked"] = bool(
        (status["artifact_found"] and not status["healthy"])
        or status["settling"]
    )

    # 取り込み元の状態をまとめた指紋。前回同期時から変わっていれば取り込み直す。
    status["signature"] = "|".join([
        str(status["run_id"] or "-"),
        _file_stamp(artifact) or "-",
        *(f"{name}:{_file_stamp(directory / name) or '-'}" for name in viewer_files),
    ])

    return status


# ---- 最終同期の記録（DB と同じディレクトリのサイドカー） ----

def sync_state_path(db_path: str | Path) -> Path:
    """DB ファイルと同じディレクトリに <stem>.pbr_sync.json を置く。"""
    p = Path(db_path)
    return p.with_name(p.stem + ".pbr_sync.json")


def load_sync_state(db_path: str | Path) -> dict:
    """最終同期・最終パージの記録を返す（無ければ空）。"""
    try:
        state = json.loads(sync_state_path(db_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_sync_state(db_path: str | Path, **updates) -> dict:
    state = load_sync_state(db_path)
    state["version"] = 1
    state.update(updates)
    path = sync_state_path(db_path)
    try:
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        # 台帳の更新はこの時点で完了している。記録だけが残らないと毎回
        # 「未取り込み」と判定されて同じ処理を繰り返すため、原因（多くは
        # データディレクトリの書き込み権限）が分かる形で伝える。
        raise PbrSyncError(
            "state_unwritable",
            f"取り込み状況の記録を保存できませんでした: {path}"
            f"（{e.strerror or e}）。台帳の更新は完了していますが、"
            "この記録が残らないため次回も同じ処理を繰り返します。"
            "データディレクトリの書き込み権限を確認してください。",
        ) from e
    return state


# ---- 残高照合 ----

def _reconcile(ledger: Ledger, snapshots: list[dict]) -> list[dict]:
    """台帳の PBR 残高とサイト表示の実残高を突き合わせる（情報提供のみ）。

    差分 (サイト − 台帳) は「契約内で発生済みだが、まだウォレットに
    付与されていない利息」で説明できることが多い。説明できる範囲かどうかを
    accrued_interest と比べて status に落とす。
    """
    # 表示設定（日次利息を残高に含めるか）に左右されないよう exclude_labels は使わない。
    balances = ledger.balances(source=[SOURCE_ID, *LEGACY_SOURCES])
    rows: list[dict] = []
    for snap in snapshots or []:
        currency = str(snap.get("currency", "")).upper()
        if not currency:
            continue
        try:
            site = Decimal(str(snap.get("amount", "0")))
            accrued = Decimal(str(snap.get("accrued_interest") or "0"))
        except InvalidOperation:
            continue
        book = balances.get(currency, _ZERO)
        drift = site - book
        if abs(drift) <= RECON_TOLERANCE:
            state = "ok"
        elif _ZERO < drift <= accrued + RECON_TOLERANCE:
            state = "accrued_pending"
        else:
            state = "warn"
        rows.append({
            "currency": currency,
            "ledger": _display_amount(book),
            "snapshot": _display_amount(site),
            "snapshot_date": snap.get("date"),
            "drift": _display_amount(drift),
            "accrued_interest": _display_amount(accrued),
            "status": state,
        })
    rows.sort(key=lambda r: r["currency"])
    return rows


def _delete_sources(window_end: datetime, today: _date | None = None) -> list[str]:
    """洗い替えで削除するソースを決める。

    公式 CSV（source=pbr）を消してよいのは当年の窓だけ。当年は公式データが
    存在しえず、そこにあるのは手作り CSV で取り込んだクロール期間のデータなので、
    重複を避けるために掃除する。

    過去年の窓では公式データが正なので触れない。年次パージ→公式CSV取り込みの
    あとに古いクロール結果を同期しても、公式データを失わない。
    """
    current_year = (today or _date.today()).year
    if window_end.year >= current_year:
        return [SOURCE_ID, *LEGACY_SOURCES]
    return [SOURCE_ID]


def sync_pbr_crawl(
    db_path: str | Path,
    crawl_dir: str | Path | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """クローラーの出力を台帳へ取り込む（対象期間を洗い替える）。

    削除対象は「展開後の取引が実在する期間」だけに限る。生イベントの最小日付
    （システム移行の 2026-03-02 など）を使うと、旧システム期間の取引を巻き込む。
    """
    directory = resolve_crawl_dir(crawl_dir)
    if directory is None:
        raise PbrSyncError(
            "not_configured",
            f"クロール出力ディレクトリが未設定です（環境変数 {ENV_VAR}）。",
        )

    status = read_crawl_status(directory)
    if not status["has_data"]:
        raise PbrSyncError(
            "artifact_missing",
            f"{directory} に取り込めるデータがありません。"
            f"クローラーを実行するか、クローラー画面から公式 CSV を"
            f"インポートしてください。",
        )

    artifact = directory / ARTIFACT_NAME
    sync_warnings: list[str] = []
    crawl_txs: list[CanonicalTx] = []
    crawl_window: tuple[datetime, datetime] | None = None
    data: dict = {}
    adapter = PbrCrawlJsonSource(SOURCE_ID)

    if status["artifact_found"]:
        # ヘルスチェックはクロール結果を取り込むときだけ意味がある。
        if not status["marker_found"] and not force:
            raise PbrSyncError(
                "marker_missing",
                f"{MARKER_NAME} が見つからず、クロールの成否を確認できません。",
            )
        if status["marker_found"] and not status["healthy"] and not force:
            raise PbrSyncError(
                "unhealthy",
                "直近のクロールが正常終了していません: "
                + "／".join(status["warnings"] or ["原因不明"]),
            )
        if not status["healthy"]:
            sync_warnings.append("ヘルスチェックを無視して同期しました（force）")
            sync_warnings.extend(status["warnings"])

        try:
            data = read_artifact(artifact)
        except (OSError, json.JSONDecodeError) as e:
            raise PbrSyncError("artifact_invalid", f"{artifact} を読み取れません: {e}")
        try:
            crawl_txs = adapter.load_data(data)
        except ValueError as e:
            raise PbrSyncError("artifact_invalid", str(e))
        if crawl_txs:
            crawl_start = min(t.timestamp for t in crawl_txs)
            last_row = max(t.timestamp for t in crawl_txs)
            end_date = data.get("end_date")
            end_ts = (_utc_midnight(end_date)
                      if isinstance(end_date, str) and end_date else last_row)
            crawl_window = (crawl_start, max(end_ts, last_row) + timedelta(days=1))
    else:
        sync_warnings.append(
            "クロール結果が無いため、クローラー画面へ手動インポートした分だけを"
            "取り込みました。"
        )

    batch_id = f"batch:{uuid.uuid4().hex[:12]}"

    ledger = Ledger(db_path)
    try:
        # 公式 CSV を直接取り込み済みの年は、ビューア側の同じデータを読まない。
        official_years = frozenset(
            ledger.years_with_source(list(LEGACY_SOURCES)))
        viewer = PbrViewerStateSource(SOURCE_ID)
        viewer_txs = viewer.load_viewer_state(
            directory, crawl_window=crawl_window, skip_years=official_years)
        txs = crawl_txs + viewer_txs
        if not txs:
            # 壊れた入力で期間を空にしてしまわないための安全弁。
            raise PbrSyncError(
                "no_rows",
                "取り込める取引が 1 件もありませんでした（洗い替えは中止）。",
            )

        start = min(t.timestamp for t in txs)
        end_exclusive = max(
            max(t.timestamp for t in txs) + timedelta(days=1),
            crawl_window[1] if crawl_window else datetime.min.replace(
                tzinfo=timezone.utc),
        )
        last_row = max(t.timestamp for t in txs)
        skip_reasons = dict(adapter.skip_reasons)
        for reason, count in viewer.skip_reasons.items():
            skip_reasons[reason] = skip_reasons.get(reason, 0) + count

        result: dict = {
            "ok": True,
            "dry_run": dry_run,
            "forced": bool(force and status["artifact_found"]
                           and not status["healthy"]),
            "run_id": status["run_id"],
            "signature": status["signature"],
            "crawl_dir": str(directory),
            "window": {
                "start": start.isoformat(),
                "end_exclusive": end_exclusive.isoformat(),
            },
            "crawl_window_start": (
                crawl_window[0].isoformat() if crawl_window else None),
            "parsed": len(txs),
            "parsed_viewer": len(viewer_txs),
            "skipped": adapter.skipped + viewer.skipped,
            "skip_reasons": skip_reasons,
            "crawl_warnings": status["crawl_warnings"],
            "sync_warnings": sync_warnings,
        }

        # 自前のデータ (pbr_crawl) は取り込む全期間を洗い替える。
        # 公式 CSV 由来 (pbr) はクロールがカバーする期間だけに限る。
        # ビューア由来の期間まで消すと、直接取り込んだ過年度データを失う。
        delete_specs: list[tuple[list[str], datetime, datetime]] = [
            ([SOURCE_ID], start, end_exclusive)
        ]
        if crawl_window:
            legacy = [s for s in _delete_sources(crawl_window[1])
                      if s != SOURCE_ID]
            if legacy:
                delete_specs.append((legacy, *crawl_window))

        counts: dict[str, int] = {}
        for sources, spec_start, spec_end in delete_specs:
            for src, n in ledger.count_in_window(
                sources, spec_start, spec_end
            ).items():
                counts[src] = counts.get(src, 0) + n

        if dry_run:
            result["deleted"] = {**counts, "total": sum(counts.values())}
            result["inserted"] = 0
            result["reconciliation"] = _reconcile(
                ledger, data.get("balance_snapshots") or [])
            return result

        stats = ledger.replace_windows(
            delete_specs, txs,
            batch_id=batch_id,
            source=SOURCE_ID,
            exchange=SOURCE_ID,
            filename=ARTIFACT_NAME,
            prune_batch_source=SOURCE_ID,
        )
        cursor = ledger.get_cursor(SOURCE_ID)
        if cursor is None or last_row > cursor:
            ledger.set_cursor(SOURCE_ID, last_row)
        result["deleted"] = {**counts, "total": stats["deleted"]}
        result["inserted"] = stats["inserted"]
        result["batch_id"] = batch_id
        result["reconciliation"] = _reconcile(
            ledger, data.get("balance_snapshots") or [])
    finally:
        ledger.close()

    _save_sync_state(db_path, last_sync={
        "run_id": status["run_id"],
        "signature": status["signature"],
        "format_version": SYNC_FORMAT_VERSION,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "window": result["window"],
        "crawl_window_start": result["crawl_window_start"],
        "parsed": result["parsed"],
        "parsed_viewer": result["parsed_viewer"],
        "deleted": result["deleted"],
        "inserted": result["inserted"],
        "batch_id": batch_id,
        "forced": result["forced"],
        "skipped": result["skipped"],
        "skip_reasons": result["skip_reasons"],
        "crawl_warnings": result["crawl_warnings"],
        "sync_warnings": result["sync_warnings"],
        "reconciliation": result["reconciliation"],
    })
    return result


def purge_pbr_crawl_year(db_path: str | Path, year: int) -> dict:
    """指定年のクロール由来データだけを削除する（年次の公式 CSV へ移行する時）。

    公式の年間履歴が公開されたら、その年はクロールの推定値ではなく公式データを
    正とする。削除対象は source=pbr_crawl のみなので、公式 CSV 由来の "pbr" や
    他ソースには一切触れない。
    """
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end_exclusive = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    ledger = Ledger(db_path)
    try:
        deleted = ledger.delete_by_source_window(
            [SOURCE_ID], start, end_exclusive)
    finally:
        ledger.close()
    _save_sync_state(db_path, last_purge={
        "year": year,
        "purged_at": datetime.now(timezone.utc).isoformat(),
        "deleted": deleted,
    })
    return {"ok": True, "year": year, "deleted": deleted}
