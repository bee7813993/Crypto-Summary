"""PBR Lending クローラー同期（洗い替え）のテスト

重点検証:
- クロールが扱う期間だけを洗い替え、期間外（旧システム期間・他ソース）は残す
- 削除期間は「展開後の取引が実在する期間」から決まる（システム移行日を含まない）
- 何度実行しても結果が同じ（冪等）
- クロールが正常終了していなければ既定では同期しない（force で上書き可）
- 取引が 0 件になる入力では洗い替えを中止する（期間を空にしない）
- 年次パージは pbr_crawl の当該年だけを消す
- 残高照合は情報提供のみ（同期は失敗させない）
"""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_summary.core.ledger import Ledger
from crypto_summary.core.models import CanonicalTx, TxType
from crypto_summary.sources.jp.pbr_crawl import (
    ARTIFACT_NAME,
    ENV_VAR,
    MARKER_NAME,
    SETTLE_SECONDS,
    VIEWER_LEDGER_NAME,
    VIEWER_TRANSFERS_NAME,
    PbrSyncError,
    load_sync_state,
    purge_pbr_crawl_year,
    read_crawl_status,
    sync_pbr_crawl,
    sync_state_path,
)


# ---- フィクスチャ ----

def _artifact(**sections) -> dict:
    payload = {
        "start_date": "2026-01-01",
        "end_date": "2026-03-05",
        "currencies": ["BTC"],
        "daily_ranges": [{
            "date_from": "2026-03-03", "date_to": "2026-03-05",
            "currency": "BTC", "daily_expected_interest": "0.00001",
        }],
        "wallet_events": [],
        "transfer_events": [{
            "date": "2026-03-02", "currency": "BTC",
            "type": "システム移行", "amount": "0",
        }],
        "balance_snapshots": [],
        "warnings": [],
    }
    payload.update(sections)
    return payload


def _marker(**overrides) -> dict:
    data = {
        "runId": "2026-03-05T10:00:00.000Z",
        "year": 2026,
        "startedAt": "2026-03-05T10:00:00.000Z",
        "finishedAt": "2026-03-05T10:03:00.000Z",
        "phase": "done",
        "failedCurrencies": [],
    }
    data.update(overrides)
    return data


def _write_settled(path: Path, text: str) -> None:
    """ファイルを書き、更新直後の「整定待ち」に入らないよう時刻を戻す。

    実運用でファイルを見るのは大抵は書かれてしばらく後なので、そちらを既定に
    してテストを決定的にする。整定待ち自体は専用のテストで確認する。
    """
    path.write_text(text, encoding="utf-8")
    old = time.time() - SETTLE_SECONDS - 60
    os.utime(path, (old, old))


@pytest.fixture
def crawl_dir(tmp_path: Path) -> Path:
    d = tmp_path / "outputs"
    d.mkdir()
    _write_settled(d / ARTIFACT_NAME, json.dumps(_artifact(), ensure_ascii=False))
    _write_settled(d / MARKER_NAME, json.dumps(_marker()))
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def _write_artifact(crawl_dir: Path, **sections) -> None:
    _write_settled(crawl_dir / ARTIFACT_NAME,
                   json.dumps(_artifact(**sections), ensure_ascii=False))


def _write_marker(crawl_dir: Path, **overrides) -> None:
    _write_settled(crawl_dir / MARKER_NAME, json.dumps(_marker(**overrides)))


def _write_viewer_transfers(crawl_dir: Path, rows: list[dict], *, wrap=True) -> None:
    body = {"version": 2, "rows": rows} if wrap else rows
    _write_settled(crawl_dir / VIEWER_TRANSFERS_NAME,
                   json.dumps(body, ensure_ascii=False))


def _write_viewer_ledger(crawl_dir: Path, rows: list[dict], *, wrap=True) -> None:
    body = {"version": 2, "rows": rows} if wrap else rows
    _write_settled(crawl_dir / VIEWER_LEDGER_NAME,
                   json.dumps(body, ensure_ascii=False))


def _transfer_row(date, currency, kubun, amount) -> dict:
    return {"日付": date, "通貨種別": currency, "区分": kubun,
            "数量": amount, "備考": "PBRLending 旧システム 貸出"}


def _ledger_row(date, currency, **columns) -> dict:
    row = {"日付": date, "通貨種別": currency, "貸出数量": "0",
           "総単日受取予定利息": "0.00001", "返還受取利息（利確数量）": "0",
           "プレミアム移行受取利息（利確数量）": "0",
           "プレミアム満期受取利息（利確数量）": "0",
           "運営からの付与数量（利確数量）": "0", "備考": ""}
    row.update(columns)
    return row


def _seed(db_path: Path, *rows: CanonicalTx) -> None:
    ledger = Ledger(db_path)
    try:
        ledger.upsert_many(list(rows))
    finally:
        ledger.close()


def _tx(day: str, source: str, label: str = "daily_interest",
        tx_id: str | None = None) -> CanonicalTx:
    return CanonicalTx(
        id=tx_id or CanonicalTx.make_id(source, f"{label}|{day}"),
        source=source,
        timestamp=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
        type=TxType.REWARD,
        received_asset="BTC",
        received_amount=Decimal("0.5"),
        label=label,
    )


def _sources(db_path: Path) -> dict[str, int]:
    ledger = Ledger(db_path)
    try:
        return {src: cnt for src, cnt, _ in ledger.sources()}
    finally:
        ledger.close()


def _all_ids(db_path: Path) -> set[str]:
    ledger = Ledger(db_path)
    try:
        return {t.id for t in ledger.all(limit=None)}
    finally:
        ledger.close()


# ---- 状態の読み取り ----

def test_read_crawl_status_healthy(crawl_dir):
    status = read_crawl_status(crawl_dir)
    assert status["healthy"] is True
    assert status["run_id"] == "2026-03-05T10:00:00.000Z"
    assert status["phase"] == "done"
    assert status["end_date"] == "2026-03-05"
    assert status["warnings"] == []


def test_read_crawl_status_partial_is_unhealthy(crawl_dir):
    _write_marker(crawl_dir, phase="partial", failedCurrencies=["ETH"])
    status = read_crawl_status(crawl_dir)
    assert status["healthy"] is False
    assert any("ETH" in w for w in status["warnings"])


def test_settling_blocks_auto_sync_but_not_manual(crawl_dir, db_path):
    """ファイル同期で届いた直後は自動取り込みを見送る（手動は実行できる）。"""
    now = time.time()
    for name in (ARTIFACT_NAME, MARKER_NAME):
        os.utime(crawl_dir / name, (now, now))

    status = read_crawl_status(crawl_dir)
    assert status["settling"] is True
    assert status["blocked"] is True
    assert any("整定" in w for w in status["warnings"])

    # 手動の同期は整定待ちに関係なく実行できる
    assert sync_pbr_crawl(db_path, crawl_dir)["parsed"] == 3


def test_not_settling_once_files_are_old(crawl_dir):
    old = time.time() - SETTLE_SECONDS - 60
    for name in (ARTIFACT_NAME, MARKER_NAME):
        os.utime(crawl_dir / name, (old, old))
    status = read_crawl_status(crawl_dir)
    assert status["settling"] is False
    assert status["blocked"] is False


def test_read_crawl_status_missing_dir(tmp_path):
    status = read_crawl_status(tmp_path / "nope")
    assert status["artifact_found"] is False
    assert status["marker_found"] is False
    assert status["healthy"] is False


# ---- 洗い替えの範囲 ----

def test_sync_replaces_window_and_keeps_everything_else(crawl_dir, db_path):
    _seed(
        db_path,
        _tx("2025-12-30", "pbr", "pbr_deposit"),        # 旧システム: 残る
        _tx("2026-02-15", "pbr", "premium_migration_interest"),  # 旧システム: 残る
        _tx("2026-03-04", "pbr"),                        # 手作り CSV 由来: 消える
        _tx("2026-03-04", "gmo"),                        # 別ソース: 残る
    )

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["ok"] is True
    assert result["window"]["start"] == "2026-03-03T00:00:00+00:00"
    assert result["window"]["end_exclusive"] == "2026-03-06T00:00:00+00:00"
    assert result["deleted"]["pbr"] == 1
    assert result["parsed"] == 3
    assert _sources(db_path) == {"gmo": 1, "pbr": 2, "pbr_crawl": 3}


def test_window_starts_at_first_emitted_row_not_raw_event(crawl_dir, db_path):
    """システム移行(2026-03-02)は取引にならないので、その日は削除対象外。"""
    _seed(db_path, _tx("2026-03-02", "pbr", "pbr_deposit"))
    sync_pbr_crawl(db_path, crawl_dir)
    assert _sources(db_path)["pbr"] == 1


def test_sync_is_idempotent(crawl_dir, db_path):
    first = sync_pbr_crawl(db_path, crawl_dir)
    ids_after_first = _all_ids(db_path)

    second = sync_pbr_crawl(db_path, crawl_dir)

    assert second["deleted"]["pbr_crawl"] == first["parsed"]
    assert second["parsed"] == first["parsed"]
    assert _all_ids(db_path) == ids_after_first
    assert _sources(db_path)["pbr_crawl"] == 3


def test_sync_follows_corrections_in_recrawled_data(crawl_dir, db_path):
    """再クロールで金額が変わったら、古い行は残らず新しい行に置き換わる。"""
    sync_pbr_crawl(db_path, crawl_dir)
    _write_artifact(crawl_dir, daily_ranges=[{
        "date_from": "2026-03-03", "date_to": "2026-03-05",
        "currency": "BTC", "daily_expected_interest": "0.00002",
    }])

    sync_pbr_crawl(db_path, crawl_dir)

    ledger = Ledger(db_path)
    try:
        amounts = {t.received_amount for t in ledger.all(limit=None)}
    finally:
        ledger.close()
    assert amounts == {Decimal("0.00002")}


def test_past_year_sync_keeps_official_rows(crawl_dir, db_path):
    """過去年の同期では公式CSV由来（pbr）に触れない。

    年次パージ → 公式CSV取り込み のあとに古いクロール結果を同期しても、
    公式データを失わないための保護。
    """
    _write_artifact(
        crawl_dir,
        end_date="2020-03-05",
        daily_ranges=[{
            "date_from": "2020-03-03", "date_to": "2020-03-05",
            "currency": "BTC", "daily_expected_interest": "0.00001",
        }],
        transfer_events=[],
    )
    _seed(db_path, _tx("2020-03-04", "pbr"))   # 公式CSV由来

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert "pbr" not in result["deleted"]
    assert _sources(db_path) == {"pbr": 1, "pbr_crawl": 3}


# ---- ビューアの手動インポート（過年度の公式データ） ----

_OLD_DEPOSITS = [
    _transfer_row("2025-09-29", "BTC", "入庫", "0.1"),
    _transfer_row("2026-01-13", "USDC", "入庫", "3000"),
    _transfer_row("2026-03-02", "BTC", "システム移行", "0"),
]


def test_viewer_transfers_fill_in_pre_crawl_period(crawl_dir, db_path):
    """クロールがカバーしない旧システム期間はビューアの手動インポートから補う。"""
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["parsed_viewer"] == 2       # システム移行は記録しない
    assert result["window"]["start"] == "2025-09-29T00:00:00+00:00"
    assert result["crawl_window_start"] == "2026-03-03T00:00:00+00:00"

    ledger = Ledger(db_path)
    try:
        bal = ledger.balances(source="pbr_crawl")
        labels = {t.label for t in ledger.all(limit=None)}
    finally:
        ledger.close()
    assert bal["BTC"] == Decimal("0.10003")   # 入庫 0.1 + 日次利息 0.00003
    assert bal["USDC"] == Decimal("3000")
    assert "pbr_deposit" in labels


def test_viewer_ledger_realized_interest_is_imported(crawl_dir, db_path):
    """日次レポート側は利確列だけを取り込む（予定利息は取らない）。"""
    _write_viewer_ledger(crawl_dir, [
        _ledger_row("2025-11-04", "BTC",
                    **{"プレミアム移行受取利息（利確数量）": "0.00188895"}),
        _ledger_row("2025-11-05", "BTC"),   # 予定利息のみ → 記録しない
    ])

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["parsed_viewer"] == 1
    ledger = Ledger(db_path)
    try:
        rows = [t for t in ledger.all(limit=None)
                if t.label == "premium_migration_interest"]
    finally:
        ledger.close()
    assert len(rows) == 1
    assert rows[0].received_amount == Decimal("0.00188895")


def test_viewer_rows_inside_crawl_window_are_ignored(crawl_dir, db_path):
    """クロールがカバーする期間はクロール結果が正。ビューア側は読まない。"""
    _write_viewer_ledger(crawl_dir, [
        _ledger_row("2026-03-04", "BTC",
                    **{"返還受取利息（利確数量）": "9.99"}),
    ])
    _write_viewer_transfers(crawl_dir, [
        _transfer_row("2026-03-04", "BTC", "入庫", "5"),
    ])

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["parsed_viewer"] == 0
    ledger = Ledger(db_path)
    try:
        assert ledger.balances(source="pbr_crawl")["BTC"] == Decimal("0.00003")
    finally:
        ledger.close()


def test_official_import_year_is_not_read_from_viewer(crawl_dir, db_path):
    """公式 CSV を直接取り込み済みの年は、ビューア側から二重に読まない。"""
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)
    _seed(db_path, _tx("2025-09-29", "pbr", "pbr_deposit"))   # 公式CSV由来

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["skip_reasons"].get("official_import_exists:2025") == 1
    assert result["parsed_viewer"] == 1       # 2026-01-13 の USDC だけ
    assert _sources(db_path) == {"pbr": 1, "pbr_crawl": 4}


def test_official_import_year_rows_survive_sync(crawl_dir, db_path):
    """過年度に直接取り込んだ公式データは洗い替えで消えない。"""
    _seed(db_path, _tx("2025-09-29", "pbr", "pbr_deposit"))
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)

    sync_pbr_crawl(db_path, crawl_dir)
    sync_pbr_crawl(db_path, crawl_dir)

    ledger = Ledger(db_path)
    try:
        assert ledger.count("pbr") == 1
    finally:
        ledger.close()


def test_viewer_only_sync_without_crawl_results(crawl_dir, db_path):
    """クロール結果が無くても、手動インポート分だけで取り込める。"""
    (crawl_dir / ARTIFACT_NAME).unlink()
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["parsed"] == 2
    assert result["parsed_viewer"] == 2
    assert result["crawl_window_start"] is None
    assert any("クロール結果が無い" in w for w in result["sync_warnings"])
    assert _sources(db_path) == {"pbr_crawl": 2}


def test_viewer_only_sync_leaves_crawl_rows_alone(crawl_dir, db_path):
    """クロール結果を消しても、取り込み済みのクロール期間は洗い替えない。"""
    sync_pbr_crawl(db_path, crawl_dir)              # クロール分 3 件
    (crawl_dir / ARTIFACT_NAME).unlink()
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)

    sync_pbr_crawl(db_path, crawl_dir)

    assert _sources(db_path) == {"pbr_crawl": 5}    # 3 件はそのまま + 2 件追加


def test_nothing_to_import_raises(crawl_dir, db_path):
    (crawl_dir / ARTIFACT_NAME).unlink()
    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir)
    assert exc.value.code == "artifact_missing"


def test_viewer_rows_after_crawl_window_are_imported(crawl_dir, db_path):
    """クロール期間の後ろ側にある手動インポートも取り込む。"""
    _write_viewer_transfers(crawl_dir, [
        _transfer_row("2026-06-01", "BTC", "入庫", "0.5"),   # クロール期間より後
    ])

    result = sync_pbr_crawl(db_path, crawl_dir)

    assert result["parsed_viewer"] == 1
    ledger = Ledger(db_path)
    try:
        assert ledger.balances(source="pbr_crawl")["BTC"] == Decimal("0.50003")
    finally:
        ledger.close()


def test_viewer_sync_is_idempotent(crawl_dir, db_path):
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)
    _write_viewer_ledger(crawl_dir, [
        _ledger_row("2025-11-04", "BTC",
                    **{"プレミアム移行受取利息（利確数量）": "0.00188895"}),
    ])

    first = sync_pbr_crawl(db_path, crawl_dir)
    ids = _all_ids(db_path)
    second = sync_pbr_crawl(db_path, crawl_dir)

    assert second["parsed"] == first["parsed"]
    assert second["deleted"]["pbr_crawl"] == first["parsed"]
    assert _all_ids(db_path) == ids


def test_viewer_legacy_bare_array_is_supported(crawl_dir, db_path):
    """旧形式（version なしの素の配列）でも読める。"""
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS, wrap=False)
    assert sync_pbr_crawl(db_path, crawl_dir)["parsed_viewer"] == 2


def test_missing_viewer_files_are_not_an_error(crawl_dir, db_path):
    result = sync_pbr_crawl(db_path, crawl_dir)
    assert result["parsed_viewer"] == 0
    assert result["ok"] is True


def test_broken_viewer_file_is_ignored(crawl_dir, db_path):
    (crawl_dir / VIEWER_TRANSFERS_NAME).write_text("{ broken", encoding="utf-8")
    result = sync_pbr_crawl(db_path, crawl_dir)
    assert result["parsed_viewer"] == 0
    assert result["ok"] is True


def test_viewer_withdrawal_sign(crawl_dir, db_path):
    _write_viewer_transfers(crawl_dir, [
        _transfer_row("2025-10-01", "XRP", "出庫", "-50"),
    ])
    sync_pbr_crawl(db_path, crawl_dir)
    ledger = Ledger(db_path)
    try:
        row = next(t for t in ledger.all(limit=None) if t.label == "pbr_withdrawal")
    finally:
        ledger.close()
    assert row.type == TxType.WITHDRAW
    assert row.sent_amount == Decimal("50")


def test_purge_year_removes_viewer_rows_too(crawl_dir, db_path):
    """年次パージは、その年のビューア由来行も含めて削除する。"""
    _write_viewer_transfers(crawl_dir, _OLD_DEPOSITS)
    sync_pbr_crawl(db_path, crawl_dir)

    result = purge_pbr_crawl_year(db_path, 2025)

    assert result["deleted"] == 1             # 2025-09-29 の入庫
    assert _sources(db_path)["pbr_crawl"] == 4


def test_manual_rows_in_window_are_kept(crawl_dir, db_path):
    _seed(db_path, _tx("2026-03-04", "pbr", tx_id="manual:abc123"))
    sync_pbr_crawl(db_path, crawl_dir)
    assert "manual:abc123" in _all_ids(db_path)


def test_batches_stay_at_one_per_sync(crawl_dir, db_path):
    sync_pbr_crawl(db_path, crawl_dir)
    sync_pbr_crawl(db_path, crawl_dir)
    ledger = Ledger(db_path)
    try:
        batches = ledger.list_import_batches()
    finally:
        ledger.close()
    assert len(batches) == 1
    assert batches[0]["exchange"] == "pbr_crawl"
    assert batches[0]["filename"] == ARTIFACT_NAME
    assert batches[0]["existing_count"] == 3


def test_cursor_is_advanced(crawl_dir, db_path):
    sync_pbr_crawl(db_path, crawl_dir)
    ledger = Ledger(db_path)
    try:
        assert ledger.get_cursor("pbr_crawl") == datetime(
            2026, 3, 5, tzinfo=timezone.utc)
    finally:
        ledger.close()


# ---- ヘルスチェック ----

def test_unhealthy_crawl_is_refused(crawl_dir, db_path):
    _write_marker(crawl_dir, phase="partial", failedCurrencies=["ETH"])
    _seed(db_path, _tx("2026-03-04", "pbr"))

    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir)

    assert exc.value.code == "unhealthy"
    assert _sources(db_path) == {"pbr": 1}   # 何も消えていない


def test_force_overrides_health_check(crawl_dir, db_path):
    _write_marker(crawl_dir, phase="partial", failedCurrencies=["ETH"])
    result = sync_pbr_crawl(db_path, crawl_dir, force=True)
    assert result["forced"] is True
    assert result["parsed"] == 3
    assert any("force" in w for w in result["sync_warnings"])


def test_missing_marker_is_refused_but_forceable(crawl_dir, db_path):
    (crawl_dir / MARKER_NAME).unlink()
    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir)
    assert exc.value.code == "marker_missing"
    assert sync_pbr_crawl(db_path, crawl_dir, force=True)["parsed"] == 3


def test_missing_artifact_is_not_forceable(crawl_dir, db_path):
    (crawl_dir / ARTIFACT_NAME).unlink()
    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir, force=True)
    assert exc.value.code == "artifact_missing"


def test_unconfigured_dir_raises(db_path, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, None)
    assert exc.value.code == "not_configured"


def test_env_var_is_used_when_dir_omitted(crawl_dir, db_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(crawl_dir))
    assert sync_pbr_crawl(db_path, None)["parsed"] == 3


def test_empty_result_aborts_without_deleting(crawl_dir, db_path):
    _seed(db_path, _tx("2026-03-04", "pbr"))
    _write_artifact(crawl_dir, daily_ranges=[], transfer_events=[])

    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir)

    assert exc.value.code == "no_rows"
    assert _sources(db_path) == {"pbr": 1}


def test_invalid_artifact_raises(crawl_dir, db_path):
    (crawl_dir / ARTIFACT_NAME).write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(PbrSyncError) as exc:
        sync_pbr_crawl(db_path, crawl_dir)
    assert exc.value.code == "artifact_invalid"


# ---- dry run ----

def test_dry_run_changes_nothing(crawl_dir, db_path):
    _seed(db_path, _tx("2026-03-04", "pbr"))

    result = sync_pbr_crawl(db_path, crawl_dir, dry_run=True)

    assert result["dry_run"] is True
    assert result["deleted"] == {"pbr": 1, "total": 1}
    assert result["parsed"] == 3
    assert _sources(db_path) == {"pbr": 1}
    assert not sync_state_path(db_path).exists()


# ---- 残高照合 ----

_SNAPSHOTS = [{
    "date": "2026-03-05", "currency": "BTC",
    "amount": "0.00003", "accrued_interest": "0",
}]


def test_reconciliation_ok_when_balances_match(crawl_dir, db_path):
    _write_artifact(crawl_dir, balance_snapshots=_SNAPSHOTS)
    result = sync_pbr_crawl(db_path, crawl_dir)
    row = result["reconciliation"][0]
    assert row["currency"] == "BTC"
    assert row["status"] == "ok"
    # 表示は末尾ゼロを落とした形（0E-10 のような読みにくい表記にしない）
    assert row["drift"] == "0"
    assert row["ledger"] == "0.00003"


def test_reconciliation_accrued_pending(crawl_dir, db_path):
    """差分が未収利息の範囲内なら「未付与の利息」として説明できる。"""
    _write_artifact(crawl_dir, balance_snapshots=[{
        "date": "2026-03-05", "currency": "BTC",
        "amount": "0.00004", "accrued_interest": "0.00001",
    }])
    result = sync_pbr_crawl(db_path, crawl_dir)
    assert result["reconciliation"][0]["status"] == "accrued_pending"


def test_reconciliation_warns_when_drift_exceeds_accrued(crawl_dir, db_path):
    _write_artifact(crawl_dir, balance_snapshots=[{
        "date": "2026-03-05", "currency": "BTC",
        "amount": "5", "accrued_interest": "0.00001",
    }])
    result = sync_pbr_crawl(db_path, crawl_dir)
    assert result["reconciliation"][0]["status"] == "warn"
    assert result["ok"] is True      # 照合の警告では同期を失敗させない


def test_reconciliation_includes_legacy_pbr_rows(crawl_dir, db_path):
    """照合対象は pbr + pbr_crawl の合算（旧システム期間の残高も含む）。"""
    _seed(db_path, _tx("2026-02-15", "pbr", "pbr_deposit"))   # +0.5 BTC
    _write_artifact(crawl_dir, balance_snapshots=[{
        "date": "2026-03-05", "currency": "BTC",
        "amount": "0.50003", "accrued_interest": "0",
    }])
    result = sync_pbr_crawl(db_path, crawl_dir)
    assert result["reconciliation"][0]["status"] == "ok"


# ---- サイドカー ----

def test_sync_state_roundtrip(crawl_dir, db_path):
    result = sync_pbr_crawl(db_path, crawl_dir)
    state = load_sync_state(db_path)
    assert sync_state_path(db_path).name == "ledger.pbr_sync.json"
    assert state["last_sync"]["run_id"] == result["run_id"]
    assert state["last_sync"]["parsed"] == 3
    assert state["last_sync"]["window"] == result["window"]
    assert state["last_sync"]["batch_id"] == result["batch_id"]


def test_load_sync_state_missing_is_empty(db_path):
    assert load_sync_state(db_path) == {}


# ---- 年次パージ ----

def test_purge_year_removes_only_that_years_crawl_rows(crawl_dir, db_path):
    sync_pbr_crawl(db_path, crawl_dir)
    _seed(
        db_path,
        _tx("2027-01-01", "pbr_crawl"),          # 翌年: 残る
        _tx("2026-02-15", "pbr", "pbr_deposit"),  # 公式 CSV 由来: 残る
    )

    result = purge_pbr_crawl_year(db_path, 2026)

    assert result == {"ok": True, "year": 2026, "deleted": 3}
    assert _sources(db_path) == {"pbr": 1, "pbr_crawl": 1}
    assert load_sync_state(db_path)["last_purge"]["year"] == 2026


def test_resync_after_purge_restores_rows(crawl_dir, db_path):
    sync_pbr_crawl(db_path, crawl_dir)
    purge_pbr_crawl_year(db_path, 2026)
    sync_pbr_crawl(db_path, crawl_dir)
    assert _sources(db_path)["pbr_crawl"] == 3
