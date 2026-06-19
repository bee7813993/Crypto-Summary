"""Web UI API のテスト（CoinGecko はモック）。

価格取得をモンキーパッチして決定的に検証する。
"""
from datetime import datetime, timezone
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
            "gmo", "bitlend", "pbr_lending"} <= values
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
