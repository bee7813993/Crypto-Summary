"""Web UI API のテスト（CoinGecko はモック）。

価格取得をモンキーパッチして決定的に検証する。
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crypto_summary.core.ledger import Ledger  # noqa: E402
from crypto_summary.core.models import CanonicalTx, TxType  # noqa: E402
from crypto_summary.web import app as web_app  # noqa: E402


def _deposit(source: str, asset: str, amount: str, day: int) -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"{asset}{day}"),
        source=source,
        timestamp=datetime(2024, 1, day, tzinfo=timezone.utc),
        type=TxType.DEPOSIT,
        received_asset=asset,
        received_amount=Decimal(amount),
    )


def _withdraw(source: str, asset: str, amount: str, day: int) -> CanonicalTx:
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"w{asset}{day}"),
        source=source,
        timestamp=datetime(2024, 1, day, tzinfo=timezone.utc),
        type=TxType.WITHDRAW,
        sent_asset=asset,
        sent_amount=Decimal(amount),
    )


def _interest(source: str, asset: str, amount: str, day: int) -> CanonicalTx:
    """日次利息ラベル付きの受取（表示設定で除外できる取引）。"""
    return CanonicalTx(
        id=CanonicalTx.make_id(source, f"i{asset}{day}"),
        source=source,
        timestamp=datetime(2024, 1, day, tzinfo=timezone.utc),
        type=TxType.REWARD,
        received_asset=asset,
        received_amount=Decimal(amount),
        label="daily_interest",
    )


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _fake_history(table: "dict | None" = None):
    """web_app.fetch_price_history のスタブを作る。

    table: {"BTC": {"YYYY-MM-DD": Decimal, ...}}。省略時は履歴なし（空 dict）。
    本物と同じく、渡されなかった資産・未登録の資産はキーごと欠落させる。
    """
    table = {k.upper(): v for k, v in (table or {}).items()}

    def _hist(assets, currency, start, end, warn=None):
        return {a.upper(): dict(table[a.upper()]) for a in assets if a.upper() in table}

    return _hist


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> str:
    db = Ledger(tmp_path / "web.db")
    db.upsert(_deposit("acct_a", "BTC", "0.5", 1))
    db.upsert(_deposit("acct_a", "ETH", "2", 2))
    db.upsert(_deposit("acct_b", "SOL", "10", 3))
    db.upsert(_deposit("acct_b", "MYSTERY", "999", 4))  # 価格なし
    db.close()

    # CoinGecko を固定価格でモック
    def fake_prices(assets, currency, warn=None):
        table = {"BTC": Decimal("60000"), "ETH": Decimal("3000"), "SOL": Decimal("150")}
        return {a.upper(): table[a.upper()] for a in assets if a.upper() in table}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history())
    return str(tmp_path / "web.db")


@pytest.fixture
def client(db_path) -> TestClient:
    return TestClient(web_app.create_app(db_path))


def test_summary_totals(client):
    r = client.get("/api/summary?currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["currency"] == "USD"
    # 0.5*60000 + 2*3000 + 10*150 = 30000 + 6000 + 1500 = 37500
    assert Decimal(d["total_value"]) == Decimal("37500")
    assert d["asset_count"] == 4
    assert d["priced_count"] == 3
    assert "MYSTERY" in d["unpriced"]


def test_summary_sorted_by_value(client):
    d = client.get("/api/summary?currency=USD").json()
    # 評価額降順: BTC(30000) > ETH(6000) > SOL(1500) > MYSTERY(価格なし末尾)
    assets = [a["asset"] for a in d["assets"]]
    assert assets == ["BTC", "ETH", "SOL", "MYSTERY"]


def test_summary_asset_fields(client):
    d = client.get("/api/summary?currency=USD").json()
    btc = next(a for a in d["assets"] if a["asset"] == "BTC")
    assert btc["has_price"] is True
    assert Decimal(btc["value"]) == Decimal("30000")
    mystery = next(a for a in d["assets"] if a["asset"] == "MYSTERY")
    assert mystery["has_price"] is False
    assert mystery["value"] is None


def test_sources_breakdown(client):
    r = client.get("/api/sources?currency=USD")
    assert r.status_code == 200
    d = r.json()
    # source_id は _display_name でタイトルケース変換される: acct_a → "Acct A"
    by_name = {s["source"]: s for s in d["sources"]}
    assert Decimal(by_name["Acct A"]["total_value"]) == Decimal("36000")
    assert Decimal(by_name["Acct B"]["total_value"]) == Decimal("1500")
    # source_ids フィールドに元のIDが含まれる
    assert "acct_a" in by_name["Acct A"]["source_ids"]
    # 評価額降順
    assert d["sources"][0]["source"] == "Acct A"


def test_sources_include_tx_period(client):
    """口座リスト用に各口座の取引期間 (first_ts/last_ts) を返す。"""
    d = client.get("/api/sources?currency=USD").json()
    by_name = {s["source"]: s for s in d["sources"]}
    # acct_a: 2024-01-01〜01-02, acct_b: 2024-01-03〜01-04
    assert by_name["Acct A"]["first_ts"].startswith("2024-01-01")
    assert by_name["Acct A"]["last_ts"].startswith("2024-01-02")
    assert by_name["Acct B"]["first_ts"].startswith("2024-01-03")
    assert by_name["Acct B"]["last_ts"].startswith("2024-01-04")


def test_invalid_currency_falls_back_to_usd(client):
    d = client.get("/api/summary?currency=XXX").json()
    assert d["currency"] == "USD"


def test_meta(client):
    d = client.get("/api/meta").json()
    assert "USD" in d["currencies"]
    assert "JPY" in d["currencies"]


def test_account_assets_drilldown(client):
    r = client.get("/api/account-assets?account=Acct+A&currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["account"] == "Acct A"
    assets_by_name = {a["asset"]: a for a in d["assets"]}
    assert "BTC" in assets_by_name
    assert Decimal(assets_by_name["BTC"]["value"]) == Decimal("30000")
    assert Decimal(d["total_value"]) == Decimal("36000")


def test_asset_accounts_drilldown(client):
    r = client.get("/api/asset-accounts?asset=BTC&currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["asset"] == "BTC"
    assert len(d["accounts"]) == 1
    assert d["accounts"][0]["account"] == "Acct A"
    assert Decimal(d["total_balance"]) == Decimal("0.5")


def test_transactions_all(client):
    r = client.get("/api/transactions")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 4
    assert d["page"] == 1
    assert d["total_pages"] == 1
    # 新しい順
    assert d["transactions"][0]["timestamp"] > d["transactions"][-1]["timestamp"]


def test_transactions_filter_account(client):
    r = client.get("/api/transactions?account=Acct+A")
    d = r.json()
    assert d["total"] == 2
    assert all(t["account"] == "Acct A" for t in d["transactions"])


def test_transactions_filter_asset(client):
    r = client.get("/api/transactions?asset=BTC")
    d = r.json()
    assert d["total"] == 1
    assert d["transactions"][0]["received_asset"] == "BTC"


def test_transactions_filter_account_and_asset(client):
    r = client.get("/api/transactions?account=Acct+B&asset=SOL")
    d = r.json()
    assert d["total"] == 1
    assert d["transactions"][0]["received_asset"] == "SOL"


def test_transactions_type_ja(client):
    d = client.get("/api/transactions").json()
    types = {t["type_ja"] for t in d["transactions"]}
    assert "入金" in types


def test_transactions_running_balance(client):
    # 資産フィルタありなら取引後残高を返す
    d = client.get("/api/transactions?asset=BTC").json()
    tx = d["transactions"][0]
    # BTC は acct_a に 0.5 入金の1件のみ → 全体・口座内とも 0.5
    assert "BTC" in tx["running_balances"]
    assert Decimal(tx["running_balances"]["BTC"]["global"]) == Decimal("0.5")
    assert Decimal(tx["running_balances"]["BTC"]["account"]) == Decimal("0.5")


def test_transactions_running_balance_without_asset_filter(client):
    # 資産フィルタなしでも running_balances が返る
    d = client.get("/api/transactions").json()
    tx = d["transactions"][0]
    assert "running_balances" in tx
    # 何らかの資産が含まれている
    assert len(tx["running_balances"]) > 0


def test_running_balance_cumulative(tmp_path, monkeypatch):
    """同一資産の複数取引で累計残高（全体・口座内）が正しく積み上がる。"""
    db = Ledger(tmp_path / "rb.db")
    # acct_a: SOL +10(d1), +5(d3) / acct_b: SOL +3(d2)
    db.upsert(_deposit("acct_a", "SOL", "10", 1))
    db.upsert(_deposit("acct_b", "SOL", "3", 2))
    db.upsert(_deposit("acct_a", "SOL", "5", 3))
    db.close()
    monkeypatch.setattr(web_app, "fetch_prices", lambda a, c, warn=None: {})

    client = TestClient(web_app.create_app(str(tmp_path / "rb.db")))
    d = client.get("/api/transactions?asset=SOL").json()
    # 新しい順: d3(acct_a +5), d2(acct_b +3), d1(acct_a +10)
    by_amount = {Decimal(t["received_amount"]): t for t in d["transactions"]}
    # d3(+5): 全体 = 10+3+5=18, Acct A 内 = 10+5=15
    assert Decimal(by_amount[Decimal("5")]["running_balances"]["SOL"]["global"]) == Decimal("18")
    assert Decimal(by_amount[Decimal("5")]["running_balances"]["SOL"]["account"]) == Decimal("15")
    # d2(+3): 全体 = 10+3=13, Acct B 内 = 3
    assert Decimal(by_amount[Decimal("3")]["running_balances"]["SOL"]["global"]) == Decimal("13")
    assert Decimal(by_amount[Decimal("3")]["running_balances"]["SOL"]["account"]) == Decimal("3")
    # d1(+10): 全体 = 10, Acct A 内 = 10
    assert Decimal(by_amount[Decimal("10")]["running_balances"]["SOL"]["global"]) == Decimal("10")
    assert Decimal(by_amount[Decimal("10")]["running_balances"]["SOL"]["account"]) == Decimal("10")


def test_account_groups_get(client):
    r = client.get("/api/account-groups")
    assert r.status_code == 200
    d = r.json()
    assert "groups" in d
    assert "all_source_ids" in d
    assert "unassigned_source_ids" in d
    # acct_a / acct_b は ACCOUNT_GROUPS に未登録 → unassigned
    assert "acct_a" in d["unassigned_source_ids"]
    assert "acct_b" in d["unassigned_source_ids"]


def test_account_groups_put_and_effect(client, db_path):
    # グループを更新: acct_a → "My Exchange"
    r = client.put("/api/account-groups", json={"groups": {"My Exchange": ["acct_a"], "Acct B": ["acct_b"]}})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # 口座一覧に新しい名前が反映される
    sources = client.get("/api/sources?currency=USD").json()
    by_name = {s["source"]: s for s in sources["sources"]}
    assert "My Exchange" in by_name
    assert "Acct B" in by_name
    assert "Acct A" not in by_name


def test_import_exchanges(client):
    d = client.get("/api/import/exchanges").json()
    values = {e["value"] for e in d["exchanges"]}
    # 主要な取引所・サービスが提示される
    assert {"nexo_savings", "nexo_spot", "nexo_dnw", "bitflyer",
            "gmo", "bitlend", "pbr"} <= values
    # PBR Lending は自動判定 (pbr) に集約し、個別形式は選択肢に出さない
    assert "pbr_lending" not in values
    assert "pbr_transfers" not in values
    # ラベルが付いている
    by_value = {e["value"]: e["label"] for e in d["exchanges"]}
    assert by_value["gmo"] == "GMOコイン"


def _universal_csv_b64() -> str:
    import base64
    csv = (
        "timestamp,type,received_asset,received_amount,sent_asset,sent_amount,fee_asset,fee_amount,note\n"
        "2024-05-01T00:00:00Z,deposit,DOGE,100,,,,,test1\n"
        "2024-05-02T00:00:00Z,deposit,DOGE,50,,,,,test2\n"
    )
    return base64.b64encode(csv.encode("utf-8")).decode("ascii")


def test_import_csv_and_batch_delete(client):
    # CSVを取り込む
    r = client.post("/api/import/csv", json={
        "exchange": "universal",
        "filename": "my_doge.csv",
        "account": "my_wallet",
        "content_b64": _universal_csv_b64(),
    })
    assert r.status_code == 200
    d = r.json()
    assert d["parsed"] == 2
    assert d["imported"] == 2
    assert d["source"] == "my_wallet"
    batch_id = d["batch_id"]

    # 取引履歴に反映される（source_id my_wallet → 表示名 "My Wallet"）
    txs = client.get("/api/transactions?account=My+Wallet").json()
    assert txs["total"] == 2

    # バッチ一覧に出る
    batches = client.get("/api/import/batches").json()["batches"]
    target = next(b for b in batches if b["id"] == batch_id)
    assert target["tx_count"] == 2
    assert target["existing_count"] == 2
    assert target["filename"] == "my_doge.csv"
    assert target["exchange_label"] == "汎用CSV"

    # CSV単位で削除
    dr = client.delete(f"/api/import/batches/{batch_id}")
    assert dr.status_code == 200
    assert dr.json()["deleted"] == 2

    # 取引が消える
    txs2 = client.get("/api/transactions?account=My+Wallet").json()
    assert txs2["total"] == 0
    # バッチも消える
    batches2 = client.get("/api/import/batches").json()["batches"]
    assert all(b["id"] != batch_id for b in batches2)


def test_import_csv_unknown_exchange(client):
    r = client.post("/api/import/csv", json={
        "exchange": "does_not_exist",
        "content_b64": _universal_csv_b64(),
    })
    assert r.status_code == 422


def test_delete_unknown_batch(client):
    r = client.delete("/api/import/batches/batch:nonexistent")
    assert r.status_code == 404


def test_clear_account(client):
    # acct_b には SOL と MYSTERY が入っている（fixture より）
    r = client.delete("/api/sources/Acct%20B")
    assert r.status_code == 200
    d = r.json()
    assert d["deleted"] == 2  # SOL 1件 + MYSTERY 1件
    assert "acct_b" in d["source_ids"]
    # 残高から消える
    summary = client.get("/api/summary?currency=USD").json()
    remaining_assets = {a["asset"] for a in summary["assets"]}
    assert "SOL" not in remaining_assets
    assert "MYSTERY" not in remaining_assets
    # BTC/ETH（acct_a）は残る
    assert "BTC" in remaining_assets


def test_clear_account_not_found(client):
    r = client.delete("/api/sources/NoSuchAccount")
    assert r.status_code == 404


def test_export_formats(client):
    d = client.get("/api/export/formats").json()
    values = {f["value"] for f in d["formats"]}
    assert {"koinly", "cryptact", "summ"} <= values
    by_value = {f["value"]: f for f in d["formats"]}
    assert by_value["koinly"]["ready"] is True
    assert by_value["summ"]["ready"] is True


def test_export_koinly(client):
    r = client.get("/api/export?format=koinly")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert "koinly" in r.headers["content-disposition"]
    body = r.content.decode("utf-8")
    # ヘッダー行（BOM を除去して確認）
    first = body.lstrip("﻿").splitlines()[0]
    assert first.startswith("Date,Sent Amount")
    # fixture の4件（deposit）が出力される
    assert "BTC" in body


def test_export_cryptact_account_filter(client):
    # Acct A（BTC 入金, ETH 入金）— どちらも DEPOSIT なので Cryptact ではスキップ
    r = client.get("/api/export?format=cryptact&account=Acct+A")
    assert r.status_code == 200
    body = r.content.decode("utf-8").lstrip("﻿")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    # ヘッダーのみ（入金はスキップされる）
    assert lines[0].startswith("Timestamp,Action")
    assert len(lines) == 1
    assert "Acct_A" in r.headers["content-disposition"]


def test_export_summ(client):
    r = client.get("/api/export?format=summ")
    assert r.status_code == 200
    body = r.content.decode("utf-8").lstrip("﻿")
    first = body.splitlines()[0]
    assert first.startswith("Timestamp (UTC),Type,Base Currency,Base Amount")
    assert "summ" in r.headers["content-disposition"]


def test_export_unknown_format(client):
    r = client.get("/api/export?format=bogus")
    assert r.status_code == 422


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---- ウォレットアドレス連携 ----

def test_wallet_register_evm_autodetect(client):
    """0x...42文字 は EVM と自動判定される（APIキー不要で登録可）。"""
    addr = "0x" + "a" * 40
    r = client.post("/api/wallets", json={"address": addr, "source_id": "mywallet"})
    assert r.status_code == 200
    d = r.json()
    assert d["source_id"] == "mywallet"
    assert d["chain"] == "evm"


def test_wallet_register_solana_autodetect(client):
    """0x で始まらないアドレスは Solana と判定される。"""
    r = client.post("/api/wallets", json={"address": "So11111111111111111111111111111111111111112"})
    assert r.status_code == 200
    assert r.json()["chain"] == "solana"
    # source_id 未指定なら自動生成される
    assert r.json()["source_id"].startswith("solana_")


def test_wallet_register_requires_address(client):
    r = client.post("/api/wallets", json={"address": ""})
    assert r.status_code == 422


def test_wallet_list_and_delete(client):
    addr = "0x" + "b" * 40
    client.post("/api/wallets", json={"address": addr, "source_id": "w1"})
    r = client.get("/api/wallets")
    assert r.status_code == 200
    wallets = r.json()["wallets"]
    assert any(w["source_id"] == "w1" for w in wallets)
    # chain_label が付与される
    assert all("chain_label" in w for w in wallets)

    r = client.delete("/api/wallets/w1")
    assert r.status_code == 200
    assert all(w["source_id"] != "w1" for w in client.get("/api/wallets").json()["wallets"])


def test_wallet_delete_missing(client):
    assert client.delete("/api/wallets/nope").status_code == 404


def test_wallet_sync_missing_returns_404(client):
    assert client.post("/api/wallets/nope/sync").status_code == 404


def test_wallet_sync_solana_without_key_errors(client, monkeypatch):
    """Helius キーが環境にもなければ 422 を返す。"""
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    client.post("/api/wallets", json={"address": "SoLaNaWalletAddr", "source_id": "sol1"})
    r = client.post("/api/wallets/sol1/sync")
    assert r.status_code == 422
    assert "Helius" in r.json()["detail"]


def test_wallet_sync_evm_calls_all_chains(client, monkeypatch):
    """EVM 同期は全 EVM チェーンをスキャンしてマージする。"""
    addr = "0x" + "c" * 40
    monkeypatch.setenv("ETHERSCAN_API_KEY", "DUMMYKEY")
    client.post("/api/wallets", json={"address": addr, "source_id": "evm1"})

    scanned_chains = []

    class FakeEtherscan:
        def __init__(self, source_id, address, key, chain_id):
            scanned_chains.append(chain_id)

        def fetch_all(self, record_gas=True):
            return []

    import crypto_summary.sources.api.etherscan as es
    monkeypatch.setattr(es, "EtherscanApiSource", FakeEtherscan)

    r = client.post("/api/wallets/evm1/sync")
    assert r.status_code == 200
    # 5 つの EVM チェーンすべてがスキャンされる
    assert len(scanned_chains) == len(es.CHAIN_IDS)


def test_sync_all_empty(client):
    """登録ゼロなら total=0 で正常終了する。"""
    r = client.post("/api/sync-all")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 0
    assert d["succeeded"] == 0
    assert d["failed"] == 0


def test_sync_all_syncs_wallets(client, monkeypatch):
    """登録済みウォレットを一括同期し、結果を集約する。"""
    monkeypatch.setenv("ETHERSCAN_API_KEY", "DUMMYKEY")
    client.post("/api/wallets", json={"address": "0x" + "a" * 40, "source_id": "w1"})
    client.post("/api/wallets", json={"address": "0x" + "b" * 40, "source_id": "w2"})

    class FakeEtherscan:
        def __init__(self, source_id, address, key, chain_id):
            pass

        def fetch_all(self, record_gas=True):
            return []

    import crypto_summary.sources.api.etherscan as es
    monkeypatch.setattr(es, "EtherscanApiSource", FakeEtherscan)

    r = client.post("/api/sync-all")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 2
    assert d["succeeded"] == 2
    assert d["failed"] == 0
    assert {x["source_id"] for x in d["results"]} == {"w1", "w2"}


def test_sync_all_continues_on_failure(client, monkeypatch):
    """1件の同期失敗で全体を止めず、失敗を集計に含める。"""
    # Solana ウォレットを鍵なしで登録 → 同期は 422 で失敗するはず
    client.post("/api/wallets", json={"address": "SoLaNaAddrXXXXXXXXXXXX", "source_id": "sol1"})
    monkeypatch.setenv("ETHERSCAN_API_KEY", "DUMMYKEY")
    client.post("/api/wallets", json={"address": "0x" + "e" * 40, "source_id": "evm1"})
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)

    class FakeEtherscan:
        def __init__(self, source_id, address, key, chain_id):
            pass

        def fetch_all(self, record_gas=True):
            return []

    import crypto_summary.sources.api.etherscan as es
    monkeypatch.setattr(es, "EtherscanApiSource", FakeEtherscan)

    r = client.post("/api/sync-all")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 2
    assert d["succeeded"] == 1
    assert d["failed"] == 1
    failed = next(x for x in d["results"] if not x["ok"])
    assert failed["source_id"] == "sol1"
    assert "error" in failed


# ---- スパムトークンフィルター ----

@pytest.fixture
def spam_client(tmp_path: Path, monkeypatch) -> TestClient:
    """スパムトークン（価格なし・整数1単位）を含む DB のクライアント。"""
    db = Ledger(tmp_path / "spam.db")
    # 正規トークン
    db.upsert(_deposit("wallet", "BTC", "0.5", 1))
    # 価格なしだが小数残高 → スパムではない
    db.upsert(_deposit("wallet", "SENTUSD", "806.1", 2))
    # スパム：価格なし・整数1単位
    for i, tok in enumerate(["CAT", "DOG", "SHIB", "REKT"], start=3):
        db.upsert(_deposit("wallet", tok, "1", i))
    # スパム境界：10単位ちょうど（スパム）
    db.upsert(_deposit("wallet", "SPAM10", "10", 10))
    # 非スパム：11単位（閾値超え）
    db.upsert(_deposit("wallet", "LEGIT11", "11", 11))
    db.close()

    def fake_prices(assets, currency, warn=None):
        return {"BTC": Decimal("60000")} if "BTC" in [a.upper() for a in assets] else {}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history())
    return TestClient(web_app.create_app(str(tmp_path / "spam.db")))


def test_spam_tokens_hidden_from_summary(spam_client):
    """スパムトークン（価格なし・整数≤10）は /api/summary から除外される。"""
    d = spam_client.get("/api/summary?currency=USD").json()
    asset_names = [a["asset"] for a in d["assets"]]
    # スパム（CAT DOG SHIB REKT SPAM10）が含まれない
    for spam in ("CAT", "DOG", "SHIB", "REKT", "SPAM10"):
        assert spam not in asset_names, f"{spam} should be filtered as spam"
    # 正規トークンは残る
    assert "BTC" in asset_names
    assert "SENTUSD" in asset_names   # 小数残高 → スパムではない
    assert "LEGIT11" in asset_names   # 11単位 → 閾値超えでスパムでない
    # unpriced リストにもスパムは出ない
    assert not any(s in d["unpriced"] for s in ("CAT", "DOG", "SHIB", "REKT", "SPAM10"))


def test_spam_tokens_hidden_from_account_assets(spam_client):
    """スパムトークンは /api/account-assets からも除外される。"""
    d = spam_client.get("/api/account-assets?account=Wallet&currency=USD").json()
    asset_names = [a["asset"] for a in d["assets"]]
    for spam in ("CAT", "DOG", "SHIB", "REKT", "SPAM10"):
        assert spam not in asset_names
    assert "BTC" in asset_names
    assert "SENTUSD" in asset_names


def test_spam_not_counted_in_sources_asset_count(spam_client):
    """スパムトークンは /api/sources の asset_count に含まれない。"""
    d = spam_client.get("/api/sources?currency=USD").json()
    wallet_src = next(s for s in d["sources"] if "wallet" in s["source_ids"])
    # BTC, SENTUSD, LEGIT11 の 3 つ（スパム 5 つは除外）
    assert wallet_src["asset_count"] == 3


# ---- システムキー（管理者設定） ----

def test_meta_is_admin_single_user(client):
    """シングルユーザーでは常に管理者扱い。"""
    d = client.get("/api/meta").json()
    assert d["multi_user"] is False
    assert d["is_admin"] is True


def test_system_keys_status_single_user(client, monkeypatch):
    """シングルユーザーではシステムキーの状態を誰でも取得できる。"""
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    d = client.get("/api/system-keys").json()
    assert d["providers"]["etherscan"] == {"stored": False, "env": False}
    assert d["providers"]["helius"] == {"stored": False, "env": False}
    assert d["cs_secret_key"] is False


def test_system_keys_status_reflects_env(client, monkeypatch):
    monkeypatch.setenv("ETHERSCAN_API_KEY", "ENVKEY")
    monkeypatch.setenv("CS_SECRET_KEY", "x")
    d = client.get("/api/system-keys").json()
    assert d["providers"]["etherscan"]["env"] is True
    assert d["cs_secret_key"] is True


def test_system_keys_status_ignores_placeholder(client, monkeypatch):
    """.env.example のダミー値は「未設定」として扱う（誤って設定済み表示しない）。"""
    monkeypatch.setenv("ETHERSCAN_API_KEY", "your_etherscan_api_key_here")
    monkeypatch.setenv("HELIUS_API_KEY", "your_helius_api_key_here")
    d = client.get("/api/system-keys").json()
    assert d["providers"]["etherscan"]["env"] is False
    assert d["providers"]["helius"]["env"] is False


def test_system_keys_set_and_persist(client, monkeypatch):
    """マスター鍵があればシステムキーを暗号化保存でき、状態に反映される。"""
    from crypto_summary.core.secrets import generate_master_key

    monkeypatch.setenv("CS_SECRET_KEY", generate_master_key())
    r = client.post("/api/system-keys", json={"etherscan": "MYETHKEY"})
    assert r.status_code == 200
    assert r.json()["updated"] == ["etherscan"]

    d = client.get("/api/system-keys").json()
    assert d["providers"]["etherscan"]["stored"] is True
    assert d["providers"]["helius"]["stored"] is False


def test_system_keys_set_without_master_key_fails(client, monkeypatch):
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    r = client.post("/api/system-keys", json={"etherscan": "MYETHKEY"})
    assert r.status_code == 500


def test_system_keys_admin_gated_in_multi_user(tmp_path, monkeypatch):
    """マルチユーザーで未認証ならシステムキーAPIは 401。"""
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    mu = TestClient(web_app.create_app(data_dir=str(tmp_path / "data")))
    assert mu.get("/api/system-keys").status_code == 401
    assert mu.get("/api/meta").json()["is_admin"] is False


# ---- 初回セットアップウィザード ----

# do_setup は os.environ を直接書き換えるため、各セットアップ系テストの
# 前後でブートストラップ系の環境変数を退避・復元してテスト間の汚染を防ぐ。
_SETUP_ENV_VARS = (
    "CS_SECRET_KEY", "ADMIN_EMAILS",
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolate_setup_env():
    saved = {k: os.environ.get(k) for k in _SETUP_ENV_VARS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def fresh_client(tmp_path: Path, monkeypatch) -> TestClient:
    """CS_SECRET_KEY が未設定・設定ファイルなしの新規環境。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    return TestClient(web_app.create_app(db_path=str(tmp_path / "fresh.db")))


def test_setup_status_needs_setup(fresh_client, monkeypatch):
    """新規環境では needs_setup=True。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    d = fresh_client.get("/api/setup-status").json()
    assert d["needs_setup"] is True
    assert d["multi_user"] is False


def test_setup_status_not_needed_when_env_set(tmp_path: Path, monkeypatch):
    """CS_SECRET_KEY が env にある場合は needs_setup=False。"""
    monkeypatch.setenv("CS_SECRET_KEY", "SOMEKEY")
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "x.db")))
    assert c.get("/api/setup-status").json()["needs_setup"] is False


def test_generate_key_returns_valid_fernet_key(fresh_client):
    """/api/generate-key は有効な Fernet キーを返す。"""
    from cryptography.fernet import Fernet
    d = fresh_client.get("/api/generate-key").json()
    assert "key" in d
    Fernet(d["key"].encode("ascii"))  # 形式チェック（例外なし）


def test_setup_sets_key_and_locks(tmp_path: Path, monkeypatch):
    """セットアップ後は鍵が保存され、再度セットアップはできない。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    from crypto_summary.core.secrets import generate_master_key
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "s.db")))

    # セットアップ前
    assert c.get("/api/setup-status").json()["needs_setup"] is True

    key = generate_master_key()
    r = c.post("/api/setup", json={"cs_secret_key": key})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # セットアップ後はロック
    r2 = c.post("/api/setup", json={"cs_secret_key": generate_master_key()})
    assert r2.status_code == 403


def test_setup_skip_locks_wizard(tmp_path: Path, monkeypatch):
    """スキップ後も needs_setup=False になりセットアップは開けない。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "skip.db")))

    r = c.post("/api/setup", json={"skipped": True})
    assert r.status_code == 200
    assert c.get("/api/setup-status").json()["needs_setup"] is False
    # 再セットアップは 403
    assert c.post("/api/setup", json={"skipped": True}).status_code == 403


def test_setup_invalid_key_rejected(tmp_path: Path, monkeypatch):
    """不正な Fernet キーは 422。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "bad.db")))
    r = c.post("/api/setup", json={"cs_secret_key": "not-a-fernet-key"})
    assert r.status_code == 422


def test_meta_needs_setup_field(fresh_client, monkeypatch):
    """/api/meta に needs_setup フィールドが含まれる。"""
    monkeypatch.delenv("CS_SECRET_KEY", raising=False)
    d = fresh_client.get("/api/meta").json()
    assert "needs_setup" in d
    assert d["needs_setup"] is True


# ---- セットアップウィザードでの OAuth 設定（マルチユーザー）----

def _clear_oauth_env(monkeypatch):
    for k in ("CS_SECRET_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
              "BASE_URL", "ADMIN_EMAILS"):
        monkeypatch.delenv(k, raising=False)


def test_setup_status_multi_user_oauth_missing(tmp_path, monkeypatch):
    """マルチユーザーで OAuth 未設定なら oauth_in_env=False。"""
    _clear_oauth_env(monkeypatch)
    c = TestClient(web_app.create_app(data_dir=str(tmp_path / "d")))
    d = c.get("/api/setup-status").json()
    assert d["needs_setup"] is True
    assert d["multi_user"] is True
    assert d["oauth_in_env"] is False


def test_setup_multi_user_requires_oauth(tmp_path, monkeypatch):
    """マルチユーザーで OAuth 未入力だと 422。"""
    _clear_oauth_env(monkeypatch)
    from crypto_summary.core.secrets import generate_master_key
    c = TestClient(web_app.create_app(data_dir=str(tmp_path / "d")))
    r = c.post("/api/setup", json={"cs_secret_key": generate_master_key()})
    assert r.status_code == 422


def test_setup_multi_user_cannot_skip(tmp_path, monkeypatch):
    """マルチユーザーで OAuth 未設定ならスキップ不可（422）。"""
    _clear_oauth_env(monkeypatch)
    c = TestClient(web_app.create_app(data_dir=str(tmp_path / "d")))
    r = c.post("/api/setup", json={"skipped": True})
    assert r.status_code == 422


def test_setup_multi_user_saves_oauth(tmp_path, monkeypatch):
    """OAuth 一式を入力するとセットアップ完了し、env と設定ファイルに反映される。"""
    _clear_oauth_env(monkeypatch)
    from crypto_summary.core.secrets import generate_master_key
    data_dir = tmp_path / "d"
    c = TestClient(web_app.create_app(data_dir=str(data_dir)))
    r = c.post("/api/setup", json={
        "cs_secret_key": generate_master_key(),
        "google_client_id": "myid.apps.googleusercontent.com",
        "google_client_secret": "mysecret",
        "base_url": "https://example.com/",
        "admin_emails": "admin@example.com",
    })
    assert r.status_code == 200
    # 即座に env に反映される
    assert os.environ["GOOGLE_CLIENT_ID"] == "myid.apps.googleusercontent.com"
    assert os.environ["BASE_URL"] == "https://example.com"  # 末尾スラッシュ除去
    # 設定ファイルに永続化される
    import json as _json
    cfg = _json.loads((data_dir / "_server_config.json").read_text())
    assert cfg["google_client_secret"] == "mysecret"
    assert cfg["admin_emails"] == "admin@example.com"
    # 再セットアップはロック
    assert c.post("/api/setup", json={"skipped": True}).status_code == 403


def test_apply_server_config_skips_empty_env(tmp_path, monkeypatch):
    """env が空文字でも設定ファイルの値が反映される（Docker の空文字対策）。"""
    _clear_oauth_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")  # Docker の未設定時を再現
    data_dir = tmp_path / "d"
    data_dir.mkdir()
    import json as _json
    (data_dir / "_server_config.json").write_text(
        _json.dumps({"google_client_id": "fromfile"})
    )
    web_app._apply_server_config(str(data_dir))
    assert os.environ["GOOGLE_CLIENT_ID"] == "fromfile"


# ---- 管理者設定 API (/api/admin-config) ----

def test_admin_config_get_single_user(tmp_path, monkeypatch):
    """/api/admin-config GET はシングルユーザーで multi_user=False を返す。"""
    monkeypatch.setenv("CS_SECRET_KEY", "")
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "a.db")))
    r = c.get("/api/admin-config")
    assert r.status_code == 200
    d = r.json()
    assert d["multi_user"] is False
    assert "cs_secret_key_set" in d
    assert "coingecko_api_key_set" in d
    assert "providers" in d


def test_admin_config_set_coingecko(tmp_path, monkeypatch):
    """/api/admin-config POST で COINGECKO_API_KEY を保存できる。"""
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "a.db")))
    r = c.post("/api/admin-config", json={"coingecko_api_key": "CGKEY"})
    assert r.status_code == 200
    assert "coingecko_api_key" in r.json()["updated"]
    # env に即座に反映される
    assert os.environ.get("COINGECKO_API_KEY") == "CGKEY"


def test_admin_config_set_admin_emails(tmp_path, monkeypatch):
    """/api/admin-config POST で ADMIN_EMAILS を更新できる。"""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    c = TestClient(web_app.create_app(db_path=str(tmp_path / "a.db")))
    r = c.post("/api/admin-config", json={"admin_emails": "a@x.com,b@x.com"})
    assert r.status_code == 200
    assert os.environ.get("ADMIN_EMAILS") == "a@x.com,b@x.com"


def test_admin_config_gated_in_multi_user(tmp_path, monkeypatch):
    """マルチユーザーで未認証なら /api/admin-config は 401。"""
    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    mu = TestClient(web_app.create_app(data_dir=str(tmp_path / "data")))
    assert mu.get("/api/admin-config").status_code == 401
    assert mu.post("/api/admin-config", json={}).status_code == 401


def test_admin_config_coingecko_applied_via_server_config(tmp_path, monkeypatch):
    """coingecko_api_key が _server_config.json に保存され、起動時に env に反映される。"""
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    db = str(tmp_path / "a.db")
    c = TestClient(web_app.create_app(db_path=db))
    r = c.post("/api/admin-config", json={"coingecko_api_key": "MYGECKO"})
    assert r.status_code == 200
    # 設定ファイルに保存される
    import json as _json
    base_dir = str(Path(db).parent)
    cfg = _json.loads(web_app._server_config_path(base_dir).read_text())
    assert cfg.get("coingecko_api_key") == "MYGECKO"


# ---- PBR: スキップ件数の可視化 ----

def _pbr_transfers_b64(*rows: str) -> str:
    import base64
    csv = "日付,通貨種別,区分,数量,備考\n" + "\n".join(rows) + "\n"
    return base64.b64encode(csv.encode("utf-8")).decode("ascii")


def test_import_csv_reports_skipped(client):
    """記録対象外として落とした行を件数と理由で返す。"""
    r = client.post("/api/import/csv", json={
        "exchange": "pbr",
        "filename": "transfers.csv",
        "content_b64": _pbr_transfers_b64(
            "2026-03-31,BTC,入庫,0.1,",
            "2026-03-31,BTC,貸出,0.1,",
            "2026-03-31,BTC,謎,1,",
        ),
    })
    assert r.status_code == 200
    d = r.json()
    assert d["parsed"] == 1
    assert d["skipped"] == 2
    assert d["skip_reasons"]["internal_move"] == 1
    assert d["skip_reasons"]["unknown_kubun:謎"] == 1


def test_import_csv_all_skipped_still_reports(client):
    """全行スキップでも件数を返す（parsed=0 のときが一番説明が必要）。"""
    r = client.post("/api/import/csv", json={
        "exchange": "pbr",
        "filename": "internal.csv",
        "content_b64": _pbr_transfers_b64(
            "2026-03-31,BTC,貸出,0.1,",
            "2026-04-28,BTC,返還,0.1,",
        ),
    })
    assert r.status_code == 200
    d = r.json()
    assert d["parsed"] == 0
    assert d["skipped"] == 2
    assert d["skip_reasons"] == {"internal_move": 2}


# ---- 集計の表示設定（日次利息トグル） ----

def test_prefs_default_and_roundtrip(client):
    assert client.get("/api/prefs").json()["prefs"]["include_daily_interest"] is True

    r = client.put("/api/prefs", json={"prefs": {"include_daily_interest": False}})
    assert r.status_code == 200
    assert r.json()["prefs"]["include_daily_interest"] is False
    assert client.get("/api/prefs").json()["prefs"]["include_daily_interest"] is False


def test_prefs_unknown_key_rejected(client):
    r = client.put("/api/prefs", json={"prefs": {"nope": True}})
    assert r.status_code == 422


def test_daily_interest_toggle_changes_balance(client):
    """トグルを切ると残高から日次利息が外れ、取引履歴には残る。"""
    client.post("/api/import/csv", json={
        "exchange": "pbr",
        "filename": "transfers.csv",
        "content_b64": _pbr_transfers_b64(
            "2026-03-31,BTC,入庫,0.1,",
            "2026-04-01,BTC,利息,0.01,",
            "2026-04-02,BTC,返還利息,0.001,",
        ),
    })

    def _btc_balance() -> Decimal:
        assets = client.get("/api/account-assets?account=Pbr").json()["assets"]
        return Decimal(next(a["balance"] for a in assets if a["asset"] == "BTC"))

    assert _btc_balance() == Decimal("0.111")

    client.put("/api/prefs", json={"prefs": {"include_daily_interest": False}})
    assert _btc_balance() == Decimal("0.101")

    # 除外中でも取引履歴の行としては残る
    txs = client.get("/api/transactions?account=Pbr").json()
    assert txs["total"] == 3

    client.put("/api/prefs", json={"prefs": {"include_daily_interest": True}})
    assert _btc_balance() == Decimal("0.111")


# ---- 前日終値ベースの評価額（前日比） ----

@pytest.fixture
def prev_client(tmp_path: Path, monkeypatch) -> TestClient:
    """前日終値つきのクライアント。

    BTC は前日、ETH は3日前が最新（欠測日をまたぐ確認用）、
    SOL は現在価格だけあって履歴が無い資産。
    """
    db = Ledger(tmp_path / "prev.db")
    db.upsert(_deposit("acct_a", "BTC", "0.5", 1))
    db.upsert(_deposit("acct_a", "ETH", "2", 2))
    db.upsert(_deposit("acct_b", "SOL", "10", 3))
    db.close()

    def fake_prices(assets, currency, warn=None):
        table = {"BTC": Decimal("60000"), "ETH": Decimal("3000"), "SOL": Decimal("150")}
        return {a.upper(): table[a.upper()] for a in assets if a.upper() in table}

    monkeypatch.setattr(web_app, "fetch_prices", fake_prices)
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history({
        "BTC": {_days_ago(2): Decimal("50000"), _days_ago(1): Decimal("55000")},
        "ETH": {_days_ago(5): Decimal("2000"), _days_ago(3): Decimal("2500")},
    }))
    return TestClient(web_app.create_app(str(tmp_path / "prev.db")))


def test_summary_prev_fields_present(prev_client):
    """全資産に前日値の3フィールドが存在する。"""
    d = prev_client.get("/api/summary?currency=USD").json()
    for a in d["assets"]:
        assert "prev_price" in a and "prev_value" in a and "prev_date" in a


def test_summary_prev_value_uses_current_balance(prev_client):
    """prev_value = いまの残高 × 前営業日の終値（前日の残高ではない）。"""
    d = prev_client.get("/api/summary?currency=USD").json()
    btc = next(a for a in d["assets"] if a["asset"] == "BTC")
    assert btc["prev_date"] == _days_ago(1)
    assert Decimal(btc["prev_price"]) == Decimal("55000")
    assert Decimal(btc["prev_value"]) == Decimal("27500")   # 0.5 * 55000


def test_summary_prev_date_is_latest_available(prev_client):
    """直近が欠測でも、当日より前で価格が取れた最新日を採る。"""
    d = prev_client.get("/api/summary?currency=USD").json()
    eth = next(a for a in d["assets"] if a["asset"] == "ETH")
    assert eth["prev_date"] == _days_ago(3)
    assert Decimal(eth["prev_value"]) == Decimal("5000")    # 2 * 2500


def test_summary_prev_null_when_history_missing(prev_client):
    """前日終値が引けない資産は3つとも null（0 ではない）。"""
    d = prev_client.get("/api/summary?currency=USD").json()
    sol = next(a for a in d["assets"] if a["asset"] == "SOL")
    assert sol["prev_price"] is None
    assert sol["prev_value"] is None
    assert sol["prev_date"] is None
    # 評価額はあるので「取りこぼし」として明示される
    assert "SOL" in d["prev_missing"]


def test_summary_total_prev_value_sums_available(prev_client):
    """total_prev_value は前日値が取れた資産だけの合計。"""
    d = prev_client.get("/api/summary?currency=USD").json()
    # BTC 27500 + ETH 5000。SOL は前日終値が無いので含まない
    assert Decimal(d["total_prev_value"]) == Decimal("32500")


def test_summary_prev_date_is_not_today(prev_client):
    """当日の値を掴んで前日比が常に 0 になる事故を防ぐ。"""
    today = date.today().isoformat()
    d = prev_client.get("/api/summary?currency=USD").json()
    assert any(a["prev_date"] for a in d["assets"])   # そもそも入っていること
    for a in d["assets"]:
        assert a["prev_date"] != today


def test_summary_prev_window_never_includes_today(client, monkeypatch):
    """履歴の窓の終端はローカル日付・UTC日付のどちらから見ても過去。"""
    seen = {}

    def _capture(assets, currency, start, end, warn=None):
        seen["start"], seen["end"] = start, end
        return {}

    monkeypatch.setattr(web_app, "fetch_price_history", _capture)
    assert client.get("/api/summary?currency=USD").status_code == 200
    assert seen["end"] < date.today()
    assert seen["end"] < datetime.now(timezone.utc).date()
    assert seen["start"] == seen["end"] - timedelta(days=web_app._PREV_LOOKBACK_DAYS)


def test_summary_prev_currency_forwarded(client, monkeypatch):
    """表示通貨が履歴取得にもそのまま渡る（price と prev_price の通貨を揃える）。"""
    seen = {}

    def _capture(assets, currency, start, end, warn=None):
        seen["currency"] = currency
        return {}

    monkeypatch.setattr(web_app, "fetch_price_history", _capture)
    client.get("/api/summary?currency=JPY")
    assert seen["currency"] == "JPY"


def test_summary_total_prev_value_null_when_no_history(client):
    """履歴が1件も取れなければ total_prev_value は null（0 ではない）。"""
    d = client.get("/api/summary?currency=USD").json()
    assert d["total_prev_value"] is None
    assert Decimal(d["total_value"]) == Decimal("37500")   # 既存の値は変わらない


def test_summary_survives_price_history_failure(tmp_path, monkeypatch):
    """履歴取得が失敗しても 500 にせず、理由を warnings に載せる。"""
    db = Ledger(tmp_path / "hf.db")
    db.upsert(_deposit("acct_a", "BTC", "0.5", 1))
    db.close()

    monkeypatch.setattr(web_app, "fetch_prices",
                        lambda a, c, warn=None: {"BTC": Decimal("60000")})

    def _boom(assets, currency, start, end, warn=None):
        if warn:
            warn("CoinGecko履歴価格の取得に失敗しました (bitcoin): boom")
        return {}

    monkeypatch.setattr(web_app, "fetch_price_history", _boom)
    r = TestClient(web_app.create_app(str(tmp_path / "hf.db"))).get("/api/summary?currency=USD")
    assert r.status_code == 200
    d = r.json()
    assert d["total_prev_value"] is None
    assert any("失敗" in w for w in d["warnings"])


def test_summary_no_prev_total_when_all_prices_fail(tmp_path, monkeypatch):
    """現在価格が全滅したら前日値も出さない（前日比 -100% 事故の回帰テスト）。"""
    db = Ledger(tmp_path / "pf.db")
    db.upsert(_deposit("acct_a", "BTC", "0.5", 1))
    db.close()

    monkeypatch.setattr(web_app, "fetch_prices", lambda a, c, warn=None: {})

    called = {"n": 0}

    def _hist(assets, currency, start, end, warn=None):
        called["n"] += 1
        return {"BTC": {_days_ago(1): Decimal("55000")}}

    monkeypatch.setattr(web_app, "fetch_price_history", _hist)
    d = TestClient(web_app.create_app(str(tmp_path / "pf.db"))).get(
        "/api/summary?currency=USD").json()
    assert Decimal(d["total_value"]) == Decimal("0")
    assert d["total_prev_value"] is None   # total_value との差で -100% にならない
    assert called["n"] == 0                # 価格が無いので履歴も引きに行かない


def test_summary_prev_negative_balance_is_negative(tmp_path, monkeypatch):
    """負残高（ショート相当）では前日評価額も負になる。"""
    db = Ledger(tmp_path / "neg.db")
    db.upsert(_withdraw("acct_a", "BTC", "0.5", 1))
    db.close()

    monkeypatch.setattr(web_app, "fetch_prices",
                        lambda a, c, warn=None: {"BTC": Decimal("60000")})
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history({
        "BTC": {_days_ago(1): Decimal("50000")},
    }))
    d = TestClient(web_app.create_app(str(tmp_path / "neg.db"))).get(
        "/api/summary?currency=USD").json()
    btc = next(a for a in d["assets"] if a["asset"] == "BTC")
    assert Decimal(btc["prev_value"]) == Decimal("-25000")
    assert Decimal(d["total_prev_value"]) == Decimal("-25000")


def test_spam_token_excluded_from_total_prev_value(tmp_path, monkeypatch):
    """スパムトークンは前日評価額の合計にも混ざらない。"""
    db = Ledger(tmp_path / "sp.db")
    db.upsert(_deposit("wallet", "BTC", "0.5", 1))
    db.upsert(_deposit("wallet", "SPAM10", "10", 2))   # 価格なし整数10 → スパム
    db.close()

    monkeypatch.setattr(web_app, "fetch_prices",
                        lambda a, c, warn=None: {"BTC": Decimal("60000")})
    # 履歴側がスパムトークンぶんも返してくる意地悪なケース
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history({
        "BTC": {_days_ago(1): Decimal("50000")},
        "SPAM10": {_days_ago(1): Decimal("999")},
    }))
    d = TestClient(web_app.create_app(str(tmp_path / "sp.db"))).get(
        "/api/summary?currency=USD").json()
    assert "SPAM10" not in [a["asset"] for a in d["assets"]]
    assert Decimal(d["total_prev_value"]) == Decimal("25000")   # BTC 0.5 * 50000 のみ


def test_summary_excluded_label_not_in_total_prev_value(tmp_path, monkeypatch):
    """日次利息を除外すると total_prev_value も連動して減る（value と同じ集合）。"""
    db = Ledger(tmp_path / "lbl.db")
    db.upsert(_deposit("pbr", "BTC", "1", 1))
    db.upsert(_interest("pbr", "BTC", "0.1", 2))
    db.close()

    monkeypatch.setattr(web_app, "fetch_prices",
                        lambda a, c, warn=None: {"BTC": Decimal("60000")})
    monkeypatch.setattr(web_app, "fetch_price_history", _fake_history({
        "BTC": {_days_ago(1): Decimal("50000")},
    }))
    c = TestClient(web_app.create_app(str(tmp_path / "lbl.db")))

    d = c.get("/api/summary?currency=USD").json()
    assert Decimal(d["total_prev_value"]) == Decimal("55000")   # 1.1 * 50000

    c.put("/api/prefs", json={"prefs": {"include_daily_interest": False}})
    d = c.get("/api/summary?currency=USD").json()
    assert Decimal(d["total_prev_value"]) == Decimal("50000")   # 1.0 * 50000
