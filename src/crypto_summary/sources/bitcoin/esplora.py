"""Esplora 互換 API アダプタ（Bitcoin Self-custody ウォレット用、read-only）

mempool.space / Blockstream が公開している Esplora 互換 REST API でアドレスの
取引履歴を取得し CanonicalTx に変換する。API キーは不要（匿名・読み取りのみ）。

接続先とレート制限:
  既定は mempool.space → blockstream.info の順。公開 API の制限値はどちらも
  非公開で、mempool.space は「連打すると IP 単位で黙って弾く」運用なので、
  リクエスト間隔を 0.25 秒（4 req/s）に抑え、429 / 5xx / 接続失敗が再試行しても
  続いたら次のホストへ切り替える（同じ Esplora API なので txid ページングも
  そのまま続けられる）。切り替えたことは warnings に残して同期結果に出す。
  環境変数 BITCOIN_ESPLORA_URL にカンマ区切りで URL を書くと接続先を差し替えられる
  （自前の mempool / electrs を 1 つだけ書けばフォールバックは無効になる）。

登録できるもの（keys.py 参照）:
  - 単一アドレス（1… / 3… / bc1q… / bc1p…）
  - 拡張公開鍵（xpub / ypub / zpub）またはそれを包んだディスクリプタ
    → BIP32 の 0/i（受取）・1/i（おつり）を gap limit 20 で走査し、
      使われているアドレスを全部まとめて 1 つの口座として扱う。

UTXO モデルの読み替え:
  Bitcoin の 1 トランザクションは複数の入力（vin）と出力（vout）を持つ。
  自分のアドレス集合 S に対して
    in  = S に属する vin の prevout 合計（自分が使ったコイン）
    out = S に属する vout 合計（自分が受け取ったコイン。おつり含む）
  として
    in = 0            → DEPOSIT（受取。coinbase なら REWARD）
    in > 0, 全 vout∈S → 自己送金（統合 / 内部移動）。FEE だけ記録
    in > 0, 全 vin∈S  → WITHDRAW（相手に渡った額 = in − out − fee）+ FEE（手数料）
    in > 0, vin 混在  → CoinJoin / PayJoin。手数料の帰属が決められないので
                        正味の増減だけを DEPOSIT / WITHDRAW にし、手数料は別記しない
  と畳む。取引所アダプタと同じく、手数料は送金額とは別の FEE 行にする
  （EVM のガス代と同じ扱い。残高は in − out = sent + fee で一致する）。

単一アドレス登録の限界:
  Self-custody ウォレットは送金のおつりを新しいアドレスへ戻すため、1 アドレス
  だけ見ていると「送金したら全額なくなった」ように見える。そのため、おつりが
  外部へ出ていく送金を見つけたら警告を返す（xpub 登録を促す）。

未確認トランザクション:
  mempool にある未確認分は含めない（/txs/chain は確認済みのみ返す）。
  承認された時点で次回の同期に入る。

ページング:
  GET /address/{addr}/txs/chain[/{last_seen_txid}] は確認済みを新しい順に 25 件ずつ
  返す。最後の txid を次ページに渡し、25 件未満になったら終了。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ...core.models import CanonicalTx, TxType
from .keys import SCRIPT_TYPE_LABELS, WalletSpec, address_from_pubkey, parse_wallet_spec

DEFAULT_ESPLORA_URLS: tuple[str, ...] = (
    "https://mempool.space/api",
    "https://blockstream.info/api",
)
DEFAULT_ESPLORA_URL = DEFAULT_ESPLORA_URLS[0]
ESPLORA_URL_ENV = "BITCOIN_ESPLORA_URL"

_SATS = Decimal(10) ** 8
_ZERO = Decimal("0")
_PAGE_SIZE = 25
_MAX_PAGES = 400          # 25 件 × 400 ページ = アドレスあたり最大 10,000 件
# 公開 API の制限値は非公開。非公式に言われる値（10 req/s 〜 10 req/min）のうち
# 「10 req/s」説より十分下、かつ xpub 1 本の走査（100〜200 リクエスト）が
# 1 分以内に終わる 4 req/s にしている。
_RATE_LIMIT_SLEEP = 0.25
GAP_LIMIT = 20            # BIP44 の既定。未使用アドレスがこれだけ続いたら走査を止める
_MAX_INDEX = 10_000       # 暴走防止（gap limit で普通はここまで来ない）
_MAX_RETRIES = 3          # 1 ホストあたりの試行回数。使い切ったら次のホストへ


class _HostUnavailable(RuntimeError):
    """このホストでは再試行しても取れなかった（別ホストに切り替えてよい失敗）。"""


def esplora_urls() -> list[str]:
    """利用する Esplora API のベース URL 一覧（先頭が主、以降がフォールバック）。

    env にカンマ区切りで並べると差し替えられる。1 つだけ書けばフォールバック無し。
    """
    raw = os.environ.get(ESPLORA_URL_ENV) or ""
    urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    return urls or list(DEFAULT_ESPLORA_URLS)


def esplora_url() -> str:
    """主に使う Esplora API のベース URL。"""
    return esplora_urls()[0]


def _host(url: str) -> str:
    """警告文用にホスト名だけ取り出す（https://mempool.space/api → mempool.space）。"""
    return url.split("//", 1)[-1].split("/", 1)[0]


def _btc(sats: Any) -> Decimal:
    try:
        return Decimal(int(sats)) / _SATS
    except (TypeError, ValueError):
        return _ZERO


class EsploraSource:
    """Esplora 互換 API で Bitcoin ウォレットの取引履歴を取得するアダプタ。"""

    def __init__(
        self,
        source_id: str,
        wallet: str,
        base_url: str | None = None,
        timeout: float = 30.0,
        gap_limit: int = GAP_LIMIT,
    ) -> None:
        try:
            spec = parse_wallet_spec(wallet)
        except ValueError as e:
            raise ValueError(str(e)) from None
        if spec is None:
            raise ValueError(f"Bitcoin のアドレス / 拡張公開鍵として解釈できません: {wallet[:12]}…")
        self.source_id = source_id
        self.spec: WalletSpec = spec
        # base_url を明示されたらそれだけを使う（テスト / 自前ノード）。
        # 未指定なら env か既定の一覧で、先頭から順に試す。
        self.hosts: list[str] = [base_url.rstrip("/")] if base_url else esplora_urls()
        self.base_url = self.hosts[0]
        self.timeout = timeout
        self.gap_limit = gap_limit
        # 同期結果に添える注意書き（取引は取れたが読み方に注意が要るもの）
        self.warnings: list[str] = []
        # 走査したアドレス数（xpub のとき。UI / ログ用）
        self.scanned_addresses: int = 0
        self.used_addresses: int = 0

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fetch_all(self, record_gas: bool = True) -> list[CanonicalTx]:
        """全取引履歴を取得して CanonicalTx リストを返す。"""
        self.warnings = []
        self.base_url = self.hosts[0]
        if self.spec.kind == "address":
            addresses = [self.spec.address or ""]
            self.scanned_addresses = 1
        else:
            addresses = self._discover_addresses()
        records = self._fetch_txs(addresses)
        self.used_addresses = len(addresses) if self.spec.kind == "address" else self.used_addresses
        return self._build(records, set(addresses), record_gas)

    # ------------------------------------------------------------------
    # HTTP（テストでオーバーライド可能）
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        """現在のホストで取得し、再試行しても駄目なら次のホストに切り替えて続ける。

        切り替えはこのインスタンスの残りの同期にも効く（弾かれたホストに戻って
        また弾かれるのを避ける）。400（アドレス不正）はどのホストでも同じなので
        切り替えずにそのまま失敗にする。
        """
        failures: list[str] = []
        while True:
            try:
                return self._get_from(self.base_url, path)
            except _HostUnavailable as e:
                failures.append(f"{_host(self.base_url)}: {e}")
                nxt = self._next_host()
                if nxt is None:
                    break
                self.warnings.append(
                    f"{_host(self.base_url)} が応答しないかレート制限中のため、"
                    f"{_host(nxt)} に切り替えて続行しました（{e}）"
                )
                self.base_url = nxt
        raise RuntimeError(
            "Esplora API に接続できません: " + " / ".join(failures) + "\n"
            "  時間をおいて再実行するか、環境変数 BITCOIN_ESPLORA_URL で別の "
            "Esplora 互換サーバー（自前の mempool / electrs など）を指定してください。"
        )

    def _next_host(self) -> str | None:
        """現在のホストの次に試すホスト。もう無ければ None。"""
        try:
            idx = self.hosts.index(self.base_url)
        except ValueError:
            return None
        return self.hosts[idx + 1] if idx + 1 < len(self.hosts) else None

    def _get_from(self, base_url: str, path: str) -> Any:
        """1 ホストに対して最大 _MAX_RETRIES 回まで試す。"""
        url = f"{base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            time.sleep(_RATE_LIMIT_SLEEP)
            try:
                resp = httpx.get(url, timeout=self.timeout)
            except httpx.HTTPError as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code == 429:
                # Retry-After があれば従う（上限 30 秒）。無ければ 2, 4, 6 秒。
                wait = 2.0 * (attempt + 1)
                try:
                    wait = min(float(resp.headers.get("retry-after", wait)), 30.0)
                except (TypeError, ValueError):
                    pass
                time.sleep(wait)
                last_err = RuntimeError("レート制限（HTTP 429）")
                continue
            if resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code == 400:
                raise RuntimeError(
                    f"Esplora API がアドレスを受け付けませんでした: {resp.text.strip()[:120]}"
                )
            resp.raise_for_status()
            return resp.json()
        raise _HostUnavailable(str(last_err))

    def _address_tx_count(self, address: str) -> int:
        """確認済み + 未確認の取引件数（未使用判定に使う）。"""
        info = self._get(f"/address/{address}") or {}
        chain = info.get("chain_stats") or {}
        mem = info.get("mempool_stats") or {}
        return int(chain.get("tx_count") or 0) + int(mem.get("tx_count") or 0)

    def _address_txs_page(self, address: str, last_seen: str | None) -> list[dict[str, Any]]:
        path = f"/address/{address}/txs/chain"
        if last_seen:
            path += f"/{last_seen}"
        return self._get(path) or []

    # ------------------------------------------------------------------
    # アドレス走査（xpub）
    # ------------------------------------------------------------------

    def _discover_addresses(self) -> list[str]:
        """拡張公開鍵から使用済みアドレスを gap limit で走査する。

        受取チェーン（0）が 1 つも使われていないスクリプト種別は、おつりチェーン（1）も
        走査しない（受け取ったことのない鍵からおつりは生まれない）。xpub は種別を
        特定できず 4 種別を見るため、この省略で未使用種別のコストを gap limit 分に抑える。
        """
        key = self.spec.key
        assert key is not None
        used: list[str] = []
        scanned = 0
        for script_type in key.script_types:
            receive_used = self._scan_chain(key, 0, script_type, used)
            scanned += receive_used[1]
            if receive_used[0]:
                change = self._scan_chain(key, 1, script_type, used)
                scanned += change[1]
        self.scanned_addresses = scanned
        self.used_addresses = len(used)
        if not used:
            labels = "・".join(SCRIPT_TYPE_LABELS[t] for t in key.script_types)
            self.warnings.append(
                f"この拡張公開鍵から導出した先頭 {self.gap_limit} 件のアドレス"
                f"（{labels}）に取引がありません。ウォレットの「アカウント」の鍵か、"
                "アドレスの種別（zpub = Native SegWit など）を確認してください。"
            )
        return used

    def _scan_chain(
        self, key, change: int, script_type: str, used: list[str]
    ) -> tuple[bool, int]:
        """1 チェーン（受取 or おつり）を走査し、(使用あり, 走査数) を返す。"""
        branch = key.child(change)
        gap = 0
        index = 0
        any_used = False
        scanned = 0
        while gap < self.gap_limit and index < _MAX_INDEX:
            try:
                leaf = branch.child(index)
            except ValueError:
                index += 1  # 無効な子鍵（確率 2^-127）は BIP32 どおり飛ばす
                continue
            address = address_from_pubkey(leaf.pubkey, script_type)
            scanned += 1
            if self._address_tx_count(address) > 0:
                used.append(address)
                any_used = True
                gap = 0
            else:
                gap += 1
            index += 1
        return any_used, scanned

    # ------------------------------------------------------------------
    # 取引取得
    # ------------------------------------------------------------------

    def _fetch_txs(self, addresses: list[str]) -> list[dict[str, Any]]:
        """各アドレスの確認済み取引を集めて txid で重複排除する。"""
        seen: set[str] = set()
        records: list[dict[str, Any]] = []
        for address in addresses:
            last_seen: str | None = None
            for _ in range(_MAX_PAGES):
                page = self._address_txs_page(address, last_seen)
                if not page:
                    break
                for tx in page:
                    txid = tx.get("txid")
                    if not txid or txid in seen:
                        continue
                    if not (tx.get("status") or {}).get("confirmed"):
                        continue
                    seen.add(txid)
                    records.append(tx)
                if len(page) < _PAGE_SIZE:
                    break
                last_seen = page[-1].get("txid")
                if not last_seen:
                    break
        return records

    # ------------------------------------------------------------------
    # 分類
    # ------------------------------------------------------------------

    def _build(
        self, records: list[dict[str, Any]], mine: set[str], record_gas: bool
    ) -> list[CanonicalTx]:
        txs: list[CanonicalTx] = []
        change_leak = False
        for tx in sorted(records, key=lambda t: (
            int((t.get("status") or {}).get("block_height") or 0), t.get("txid") or "")):
            out, leak = self._process(tx, mine, record_gas)
            txs.extend(out)
            change_leak = change_leak or leak
        if change_leak and self.spec.kind == "address":
            self.warnings.append(
                "単一アドレスの登録です。送金のおつりが別のアドレスに戻る Self-custody "
                "ウォレットでは、おつり分が出金として記録され残高が合いません。"
                "ウォレットの拡張公開鍵（xpub / ypub / zpub）で登録し直してください。"
            )
        return txs

    def _process(
        self, tx: dict[str, Any], mine: set[str], record_gas: bool
    ) -> tuple[list[CanonicalTx], bool]:
        """1 トランザクションを CanonicalTx に変換する。(行, おつり漏れの疑い) を返す。"""
        txid = str(tx.get("txid") or "")
        status = tx.get("status") or {}
        ts = datetime.fromtimestamp(int(status.get("block_time") or 0), tz=timezone.utc)
        vin = tx.get("vin") or []
        vout = tx.get("vout") or []

        coinbase = any(v.get("is_coinbase") for v in vin)
        in_mine = _ZERO
        in_other = _ZERO
        for v in vin:
            prev = v.get("prevout") or {}
            amount = _btc(prev.get("value"))
            if prev.get("scriptpubkey_address") in mine:
                in_mine += amount
            else:
                in_other += amount
        out_mine = _ZERO
        out_other = _ZERO
        for v in vout:
            amount = _btc(v.get("value"))
            if v.get("scriptpubkey_address") in mine:
                out_mine += amount
            else:
                out_other += amount
        fee = _btc(tx.get("fee"))

        results: list[CanonicalTx] = []

        # ── 受取（自分の入力なし）────────────────────────────────────
        if in_mine == _ZERO:
            if out_mine <= _ZERO:
                return results, False
            if coinbase:
                results.append(self._tx(txid, ts, TxType.REWARD,
                    received_asset="BTC", received_amount=out_mine,
                    label="coinbase", tx_hash=txid))
            else:
                results.append(self._tx(txid, ts, TxType.DEPOSIT,
                    received_asset="BTC", received_amount=out_mine,
                    label="receive", tx_hash=txid))
            return results, False

        # ── 入力が混在（CoinJoin / PayJoin）→ 正味の増減だけ ─────────
        if in_other > _ZERO:
            net = out_mine - in_mine
            if net > _ZERO:
                results.append(self._tx(txid, ts, TxType.DEPOSIT,
                    received_asset="BTC", received_amount=net,
                    label="coinjoin", tx_hash=txid))
            elif net < _ZERO:
                results.append(self._tx(txid, ts, TxType.WITHDRAW,
                    sent_asset="BTC", sent_amount=-net,
                    label="coinjoin", tx_hash=txid))
            return results, False

        # ── 全入力が自分 ────────────────────────────────────────────
        if record_gas and fee > _ZERO:
            results.append(self._tx(txid + "|fee", ts, TxType.FEE,
                fee_asset="BTC", fee_amount=fee, label="miner_fee", tx_hash=txid))

        if out_other <= _ZERO:
            # 自己送金（UTXO 統合・自分のアドレス間の移動）。手数料しか減らない。
            return results, False

        # 相手に渡った額。fee を vout に含めない API（Esplora は含めない）なので
        # in − out_mine − fee = out_other のはずだが、丸めや fee 欠落に備えて実測値を使う。
        results.append(self._tx(txid, ts, TxType.WITHDRAW,
            sent_asset="BTC", sent_amount=out_other,
            label="send", tx_hash=txid))
        # 単一アドレス登録で、自分へ戻る出力が無い送金 → おつりが別アドレスへ出た疑い
        leak = out_mine == _ZERO and len(vout) >= 2
        return results, leak

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
