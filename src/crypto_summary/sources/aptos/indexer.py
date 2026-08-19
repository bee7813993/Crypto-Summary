"""Aptos Indexer GraphQL API アダプタ（Aptos ウォレット用、read-only）

Aptos Labs の Indexer（Hasura GraphQL）から `fungible_asset_activities` を取得し
CanonicalTx に変換する。フルノード REST の `/accounts/{addr}/transactions` は
「そのアカウントが送信した」トランザクションしか返さず受取が漏れるため、
残高変動イベントをそのまま持つ Indexer を採用している。

`fungible_asset_activities` は Coin (v1) と Fungible Asset (v2) の残高変動を
1つのテーブルに統合しているため、APT のような旧 Coin 資産と
Aptos ネイティブ USDC のような FA 資産を同じクエリで拾える。

API キー権限:
  ✅ 読み取りのみ（ブロックチェーンデータは公開情報）
  ❌ 送金・出金権限は存在しない（秘密鍵と無関係）
  キーは任意。未設定でも動作するが、匿名アクセスは IP 単位の厳しい
  レート制限（40,000 CU / 300 秒）があり、履歴が多いと 429 で失敗する。
  https://geomi.dev （Aptos Build）で無料発行し、.env の APTOS_API_KEY に設定。

アドレス表記:
  Indexer は Aptos アドレスを「0x + 64桁ゼロ埋め」に正規化して保存する。
  ウォレットが短縮形（先頭ゼロ省略）で表示するアドレスをそのまま渡すと
  1件もヒットしないため、問い合わせ前に必ずゼロ埋めする。

トランザクション識別子:
  `fungible_asset_activities` はトランザクションハッシュを持たず
  `transaction_version`（チェーン全体で一意の連番）で識別する。
  Aptos Explorer も version で参照できる（explorer.aptoslabs.com/txn/<version>）ため
  これを tx_hash として記録する。

ページング:
  transaction_version 昇順のキーセットページング。1ページ最大 _PAGE_SIZE 件を
  取得し、最後の version を次ページの下限（_gte）にする。境界の version は
  次ページと重複しうるので (version, event_index) で重複排除する。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...core.models import CanonicalTx, TxType

_ENDPOINT = "https://api.mainnet.aptoslabs.com/v1/graphql"
_ZERO = Decimal("0")
_DUST = Decimal("0.000001")
_PAGE_SIZE = 100
_MAX_PAGES = 500      # 100 件 × 500 ページ = 最大 50,000 件
# 1つの version に _PAGE_SIZE 件超のアクティビティがあるとキーセットが進まなくなる。
# そのときだけ version 指定で一括取得して次へ進む（下記 _drain_version）。
_DRAIN_LIMIT = 1000
_RATE_LIMIT_SLEEP = 0.2

# APT のネイティブ資産。Coin(v1) 型と FA(v2) メタデータアドレスは同一資産なので統合する。
# Indexer は移行済み Coin のアクティビティを Coin 型（0x1::aptos_coin::AptosCoin）
# 側に寄せて記録するが、FA アドレスで来ても APT として扱えるようにしておく。
_APT_COIN_TYPE = "0x1::aptos_coin::AptosCoin"
_APT_FA_ADDRESS = "0x" + "0" * 63 + "a"

# metadata が引けなかったときのフォールバック（ネイティブ APT のみ確実に名付ける）
_KNOWN_ASSETS: dict[str, tuple[str, int]] = {
    _APT_COIN_TYPE: ("APT", 8),
    _APT_FA_ADDRESS: ("APT", 8),
}

# スパム判定: 非 ASCII 文字（ホモグラフ攻撃）or URL / 宣伝文句。EVM / Solana 版と同じ方針。
_PHISHING_RE = re.compile(
    r"t\.me/|https?://|\.(com|io|net|org|xyz|top)/|"
    r"get\s+reward|claim\s+at|visit\s+|free\s*airdrop",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^[0-9a-f]{1,64}$")

_ACTIVITY_FIELDS = """
    transaction_version
    event_index
    asset_type
    amount
    type
    is_gas_fee
    storage_refund_amount
    entry_function_id_str
    transaction_timestamp
    metadata { symbol name decimals }
"""

# 並べ替えは transaction_version だけにする。event_index の第2キーを足すと
# アクティビティの多いアドレスで Indexer 側がソートしきれず 408 になることがあり、
# こちらは version 単位で集計するので同一 version 内の順序に依存しない。
# 1 version が 1 ページに収まらない場合だけ _drain_version で取り切る。
_QUERY = """
query WalletActivities($owner: String!, $from: bigint!, $limit: Int!) {
  fungible_asset_activities(
    where: {owner_address: {_eq: $owner}, transaction_version: {_gte: $from}}
    order_by: {transaction_version: asc}
    limit: $limit
  ) {%s}
}
""" % _ACTIVITY_FIELDS

_DRAIN_QUERY = """
query VersionActivities($owner: String!, $version: bigint!, $limit: Int!) {
  fungible_asset_activities(
    where: {owner_address: {_eq: $owner}, transaction_version: {_eq: $version}}
    order_by: [{event_index: asc}]
    limit: $limit
  ) {%s}
}
""" % _ACTIVITY_FIELDS


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return _ZERO


def normalize_address(addr: str) -> str:
    """Aptos アドレスを Indexer の保存形式（0x + 64桁ゼロ埋め小文字）に正規化する。

    ウォレットは先頭ゼロを省いた短縮形を表示することがあり
    （例: 0x75b4… の実体は 0x075b4…）、そのまま問い合わせるとヒットしない。
    """
    a = addr.strip().lower()
    if a.startswith("0x"):
        a = a[2:]
    if not _HEX_RE.match(a):
        raise ValueError(f"Aptos アドレスとして解釈できません: {addr}")
    return "0x" + a.rjust(64, "0")


def _short_asset(asset_type: str) -> str:
    """シンボル未解決時の表示用に asset_type を短縮する。"""
    if not asset_type:
        return "UNKNOWN"
    return asset_type if len(asset_type) <= 13 else f"{asset_type[:8]}…{asset_type[-5:]}"


def _is_spam(symbol: str, name: str) -> bool:
    """スパム / フィッシングトークンを判定する。"""
    for text in (symbol, name):
        if not text:
            continue
        if not text.isascii():
            return True
        if _PHISHING_RE.search(text):
            return True
    return False


def _parse_ts(s: str) -> datetime:
    """Indexer の timestamp（UTC・タイムゾーン表記なし）を aware な datetime にする。"""
    txt = (s or "").strip().replace(" ", "T")
    if txt.endswith("Z"):
        txt = txt[:-1]
    try:
        return datetime.fromisoformat(txt).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _server_message(resp: httpx.Response) -> str:
    """レスポンスからサーバーの説明を取り出す（GraphQL の errors[].message 優先）。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:200] or f"HTTP {resp.status_code}"
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        return "; ".join(
            str(e.get("message", e)) if isinstance(e, dict) else str(e)
            for e in errors
        )
    return str(data)[:200]


def _masked_key(api_key: str | None) -> str:
    """キーの取り違えを見分けられる範囲だけ見せる（全体はログに出さない）。

    先頭は種別（aptoslabs_ = サーバー用 / AG- = ブラウザ用）の判別に要る。
    末尾はコピー時の欠けに気づくために要る。
    """
    if not api_key:
        return "なし（匿名アクセス）"
    if len(api_key) <= 12:
        return f"{api_key[:4]}…（{len(api_key)}文字）"
    return f"{api_key[:10]}…{api_key[-4:]}（{len(api_key)}文字）"


def _method_name(entry_function: str) -> str:
    """entry_function_id_str ("0x1::aptos_account::transfer") から関数名を取り出す。"""
    fn = (entry_function or "").strip()
    if not fn:
        return "transfer"
    return fn.rsplit("::", 1)[-1] or "transfer"


def _direction(event_type: str) -> int:
    """イベント型から残高の増減方向を返す（+1 受取 / −1 送出 / 0 変化なし）。

    Coin(v1) は 0x1::coin::DepositEvent / WithdrawEvent と新形式の
    0x1::coin::CoinDeposit / CoinWithdraw、FA(v2) は
    0x1::fungible_asset::Deposit / Withdraw。いずれも型名の末尾で判別できる。
    """
    t = event_type.rsplit("::", 1)[-1]
    if t.endswith("Deposit") or t.endswith("DepositEvent"):
        return 1
    if t.endswith("Withdraw") or t.endswith("WithdrawEvent"):
        return -1
    return 0


class AptosIndexerSource:
    """Aptos Indexer GraphQL API で Aptos ウォレットの取引履歴を取得するアダプタ。"""

    def __init__(
        self,
        source_id: str,
        wallet_address: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.source_id = source_id
        self.wallet = normalize_address(wallet_address)
        self.api_key = api_key or None
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fetch_all(self, record_gas: bool = True) -> list[CanonicalTx]:
        """全取引履歴を取得して CanonicalTx リストを返す。"""
        return self._build(self._fetch_all_pages(), record_gas)

    # ------------------------------------------------------------------
    # HTTP（テストでオーバーライド可能）
    # ------------------------------------------------------------------

    def _post(self, query: str, variables: dict[str, Any]) -> list[dict[str, Any]]:
        """GraphQL を 1 回投げて fungible_asset_activities の配列を返す。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        time.sleep(_RATE_LIMIT_SLEEP)
        resp = httpx.post(
            _ENDPOINT,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            # サーバーの言い分（例: "Unauthorized: API key not found"）をそのまま見せる。
            # 握りつぶすと「キーが違う」のか「キーが届いていない」のか切り分けられない。
            raise RuntimeError(
                f"Aptos Indexer 認証エラー: {_server_message(resp)}\n"
                f"  送ったキー: {_masked_key(self.api_key)}\n"
                "  Aptos Build (https://geomi.dev) のキーを次の点で確認してください。\n"
                "  - Mainnet 用に作ったキーか（Testnet/Devnet 用は mainnet では見つからない）\n"
                "  - サーバー用キー（aptoslabs_ で始まる）か。ブラウザ用の Client key\n"
                "    （AG- で始まる）は Origin 制限があり、サーバーからは使えない\n"
                "  - コピー時に前後が欠けたり改行が混じったりしていないか\n"
                "  キーを外せば匿名アクセスで取得はできます（レート制限は厳しくなります）。"
            )
        if resp.status_code == 429:
            raise RuntimeError(
                f"Aptos Indexer のレート制限に達しました: {_server_message(resp)}\n"
                "  APIキーなしの匿名アクセスは IP 単位で厳しく制限されます。\n"
                "  .env に APTOS_API_KEY を設定してください（無料）: https://geomi.dev"
            )
        if resp.status_code in (408, 504):
            raise RuntimeError(
                "Aptos Indexer がタイムアウトしました（アクティビティが極端に多いアドレス）。\n"
                "  時間をおいて再実行してください。"
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Aptos Indexer error: {_server_message(resp)}")
        return (data.get("data") or {}).get("fungible_asset_activities") or []

    def _request(self, from_version: int) -> list[dict[str, Any]]:
        """1 ページ分（version 昇順・最大 _PAGE_SIZE 件）を取得する。"""
        return self._post(
            _QUERY,
            {"owner": self.wallet, "from": from_version, "limit": _PAGE_SIZE},
        )

    def _drain_version(self, version: int) -> list[dict[str, Any]]:
        """1 つの version のアクティビティをまとめて取得する（ページ跨ぎ対策）。"""
        return self._post(
            _DRAIN_QUERY,
            {"owner": self.wallet, "version": version, "limit": _DRAIN_LIMIT},
        )

    def _fetch_all_pages(self) -> list[dict[str, Any]]:
        """全ページを取得してまとめる（version 昇順キーセット + 重複排除）。"""
        all_records: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        from_version = 0

        def _collect(batch: list[dict[str, Any]]) -> None:
            for rec in batch:
                key = (int(rec.get("transaction_version") or 0),
                       int(rec.get("event_index") or 0))
                if key in seen:
                    continue
                seen.add(key)
                all_records.append(rec)

        for _ in range(_MAX_PAGES):
            page = self._request(from_version)
            if not page:
                break
            _collect(page)
            if len(page) < _PAGE_SIZE:
                break  # 最終ページ
            last_version = int(page[-1].get("transaction_version") or from_version)
            if last_version <= from_version:
                # 同一 version に _PAGE_SIZE 件超 → キーセットが進まないので
                # その version だけ一括取得してから次へ進める。
                _collect(self._drain_version(from_version))
                from_version += 1
            else:
                from_version = last_version
        return all_records

    # ------------------------------------------------------------------
    # 分類ロジック
    # ------------------------------------------------------------------

    def _build(
        self, records: list[dict[str, Any]], record_gas: bool
    ) -> list[CanonicalTx]:
        """アクティビティを transaction_version 単位にまとめて CanonicalTx にする。"""
        grouped: dict[int, list[dict[str, Any]]] = {}
        for rec in records:
            grouped.setdefault(int(rec.get("transaction_version") or 0), []).append(rec)

        txs: list[CanonicalTx] = []
        for version in sorted(grouped):
            txs.extend(self._process(version, grouped[version], record_gas))
        return txs

    def _process(
        self, version: int, rows: list[dict[str, Any]], record_gas: bool
    ) -> list[CanonicalTx]:
        """1 トランザクション（= 1 version）を CanonicalTx に変換する。"""
        vid = str(version)
        ts = _parse_ts(rows[0].get("transaction_timestamp", ""))
        entry_function = ""

        # 資産ごとの正味フロー（+ = 受取 / − = 送出）。
        # スワップの往復や DEX 経由の中継も相殺されて正味だけが残る。
        flows: dict[str, Decimal] = {}
        gas = _ZERO

        for r in rows:
            entry_function = entry_function or (r.get("entry_function_id_str") or "")
            symbol, decimals = self._asset_meta(r.get("asset_type") or "", r.get("metadata"))
            if symbol is None:
                continue  # スパム / メタデータ不明で数量を確定できない
            scale = Decimal(10) ** decimals
            amount = _d(r.get("amount")) / scale

            if r.get("is_gas_fee"):
                # ガス代は「請求額 − ストレージ返金」が正味の負担。
                gas += amount - _d(r.get("storage_refund_amount")) / scale
                continue

            sign = _direction(r.get("type") or "")
            if sign == 0:
                continue  # Frozen 等、残高が動かないイベント
            flows[symbol] = flows.get(symbol, _ZERO) + sign * amount

        results: list[CanonicalTx] = []

        if record_gas and gas > _ZERO:
            results.append(self._tx(
                vid + "|gas", ts, TxType.FEE,
                fee_asset="APT", fee_amount=gas, label="gas", tx_hash=vid,
            ))

        received = sorted(
            ((a, v) for a, v in flows.items() if v > _DUST), key=lambda x: x[0])
        sent = sorted(
            ((a, -v) for a, v in flows.items() if v < -_DUST), key=lambda x: x[0])

        if not received and not sent:
            return results  # ガスのみ（承認・失敗トランザクション等）

        label = _method_name(entry_function)

        # ── 単一受取 ─────────────────────────────────────────────────
        if len(received) == 1 and not sent:
            a, v = received[0]
            results.append(self._tx(vid, ts, TxType.DEPOSIT,
                received_asset=a, received_amount=v, label=label, tx_hash=vid))
            return results

        # ── 単一送出 ─────────────────────────────────────────────────
        if len(sent) == 1 and not received:
            a, v = sent[0]
            results.append(self._tx(vid, ts, TxType.WITHDRAW,
                sent_asset=a, sent_amount=v, label=label, tx_hash=vid))
            return results

        # ── 1 送出 + 1 受取 → スワップ（TRADE）──────────────────────
        if len(received) == 1 and len(sent) == 1:
            ra, rv = received[0]
            sa, sv = sent[0]
            results.append(self._tx(vid, ts, TxType.TRADE,
                sent_asset=sa, sent_amount=sv,
                received_asset=ra, received_amount=rv,
                label="swap", tx_hash=vid))
            return results

        # ── 複数送出のみ → LP 流動性追加 ────────────────────────────
        if sent and not received:
            for i, (a, v) in enumerate(sent):
                results.append(self._tx(vid + f"|o{i}", ts, TxType.TRANSFER,
                    sent_asset=a, sent_amount=v, label="lp_add", tx_hash=vid))
            return results

        # ── 複数受取のみ → LP 流動性撤退 ────────────────────────────
        if received and not sent:
            for i, (a, v) in enumerate(received):
                results.append(self._tx(vid + f"|i{i}", ts, TxType.TRANSFER,
                    received_asset=a, received_amount=v, label="lp_remove", tx_hash=vid))
            return results

        # ── 複合（複数送出 + 複数受取）→ 個別記録 ───────────────────
        for i, (a, v) in enumerate(received):
            results.append(self._tx(vid + f"|i{i}", ts, TxType.DEPOSIT,
                received_asset=a, received_amount=v, label="token_in", tx_hash=vid))
        for i, (a, v) in enumerate(sent):
            results.append(self._tx(vid + f"|o{i}", ts, TxType.WITHDRAW,
                sent_asset=a, sent_amount=v, label="token_out", tx_hash=vid))
        return results

    def _asset_meta(
        self, asset_type: str, metadata: dict[str, Any] | None
    ) -> tuple[str | None, int]:
        """(シンボル, 小数桁) を返す。除外すべき資産なら (None, 0)。

        Indexer の metadata 結合で symbol / decimals が返る。APT は Coin(v1) と
        FA(v2) のどちらの asset_type で来ても APT に統合する。
        小数桁が引けない未知資産は数量を確定できないため取り込まない
        （小数桁を誤ると残高が桁違いになるので、落として気づける方を選ぶ）。
        シンボルだけ空なら asset_type の短縮形で名付けて数量は活かす。

        シンボルは大文字に寄せる。Aptos ネイティブ Tether は "USDt" を名乗り、
        そのままだと取引所由来の "USDT" と別資産として二重に並ぶ。EVM アダプタも
        ERC20 シンボルを大文字化しており、価格解決も asset.upper() で引くため揃う。
        """
        md = metadata or {}
        symbol = (md.get("symbol") or "").strip()
        name = (md.get("name") or "").strip()
        if _is_spam(symbol, name):
            return None, 0
        known = _KNOWN_ASSETS.get(asset_type)
        if known:
            return known
        try:
            decimals = int(md["decimals"])
        except (KeyError, TypeError, ValueError):
            return None, 0
        return symbol.upper() or _short_asset(asset_type), decimals

    def _tx(self, raw_key: str, ts: datetime, tx_type: TxType, **kw) -> CanonicalTx:
        return CanonicalTx(
            id=CanonicalTx.make_id(self.source_id, raw_key),
            source=self.source_id,
            timestamp=ts,
            type=tx_type,
            received_asset=kw.get("received_asset"),
            received_amount=kw.get("received_amount"),
            sent_asset=kw.get("sent_asset"),
            sent_amount=kw.get("sent_amount"),
            fee_asset=kw.get("fee_asset"),
            fee_amount=kw.get("fee_amount"),
            label=kw.get("label"),
            tx_hash=kw.get("tx_hash"),
            raw={},
        )
