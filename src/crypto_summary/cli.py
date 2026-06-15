from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from .core.ledger import Ledger
from .core.models import CanonicalTx, TxType
from .sources.csv_import import EXCHANGE_SOURCES

# .env をカレントディレクトリ起点で検索してロード
try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True) or ".env")
except ImportError:
    pass

console = Console()
DEFAULT_DB = "ledger.db"


@click.group()
@click.option("--db", default=DEFAULT_DB, show_default=True, help="SQLite ledger path")
@click.pass_context
def cli(ctx: click.Context, db: str) -> None:
    """Crypto-Summary: fetch, normalize, and export exchange trade history."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@cli.command("import")
@click.option(
    "--file", "filepath", required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the exchange CSV file",
)
@click.option(
    "--exchange", required=True,
    type=click.Choice(list(EXCHANGE_SOURCES.keys())),
    help="Exchange / format of the CSV",
)
@click.option("--source-id", default=None, help="Custom source identifier (default: exchange name)")
@click.pass_context
def import_cmd(ctx: click.Context, filepath: Path, exchange: str, source_id: str | None) -> None:
    """Import an exchange CSV file into the ledger."""
    sid = source_id or exchange
    source = EXCHANGE_SOURCES[exchange](sid)
    ledger = Ledger(ctx.obj["db"])

    console.print(f"Importing [cyan]{filepath.name}[/cyan] as [bold]{sid}[/bold] ...")
    txs = source.load(filepath)

    if not txs:
        console.print("[yellow]No transactions found in file.[/yellow]")
        return

    before = ledger.count(sid)
    ledger.upsert_many(txs)
    after = ledger.count(sid)

    latest_ts = max(t.timestamp for t in txs)
    ledger.set_cursor(sid, latest_ts)
    ledger.close()

    new = after - before
    console.print(
        f"[green]✓[/green] {len(txs)} rows parsed  |  "
        f"[green]+{new} new[/green]  |  "
        f"{len(txs) - new} already existed (skipped)  |  "
        f"latest: {latest_ts.strftime('%Y-%m-%d %H:%M')} UTC"
    )


# ---------------------------------------------------------------------------
# import-wallet (EVM ウォレット複数CSV取り込み)
# ---------------------------------------------------------------------------

_WALLET_EXCHANGES = ["arbiscan"]


@cli.command("import-wallet")
@click.option(
    "--exchange", required=True,
    type=click.Choice(_WALLET_EXCHANGES),
    help="ブロックエクスプローラーの種類",
)
@click.option(
    "--wallet", "wallet_address", required=True,
    help="ウォレットアドレス (0x...)",
)
@click.option(
    "--normal", "normal_file", required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Normal Transactions CSV（通常トランザクション）",
)
@click.option(
    "--erc20", "erc20_file", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="ERC-20 Token Txns CSV（スワップ・トークン転送）",
)
@click.option(
    "--internal", "internal_file", default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Internal Transactions CSV（コントラクト内部 ETH 転送）",
)
@click.option("--source-id", default=None,
              help="ソース識別子（デフォルト: exchange名）")
@click.option("--record-gas", is_flag=True, default=False,
              help="ガス代を FEE として ledger に記録する")
@click.pass_context
def import_wallet_cmd(
    ctx: click.Context,
    exchange: str,
    wallet_address: str,
    normal_file: Path,
    erc20_file: Path | None,
    internal_file: Path | None,
    source_id: str | None,
    record_gas: bool,
) -> None:
    """EVM ウォレットの取引履歴（Arbiscan / Etherscan CSV）を ledger に取り込む。

    \b
    Arbiscan (arbiscan.io) の「アドレスページ」から各タブの CSV をダウンロードして
    指定する。--normal は必須。--erc20 を追加するとスワップが正確に記録される。

    \b
    例:
      crypto-summary import-wallet \\
        --exchange arbiscan \\
        --wallet 0xABC...123 \\
        --normal export_normal.csv \\
        --erc20 export_erc20.csv \\
        --internal export_internal.csv \\
        --source-id my_arbitrum
    """
    from .sources.evm.arbiscan import ArbiscanCsvSource

    sid = source_id or exchange
    adapter = ArbiscanCsvSource(sid, wallet_address)

    files = [normal_file.name]
    if erc20_file:
        files.append(erc20_file.name)
    if internal_file:
        files.append(internal_file.name)
    console.print(
        f"Importing [cyan]{', '.join(files)}[/cyan] as [bold]{sid}[/bold] ..."
    )

    txs = adapter.load_multi(
        normal_path=normal_file,
        erc20_path=erc20_file,
        internal_path=internal_file,
        record_gas=record_gas,
    )

    if not txs:
        console.print("[yellow]取引が見つかりませんでした。[/yellow]")
        return

    ledger = Ledger(ctx.obj["db"])
    before = ledger.count(sid)
    ledger.upsert_many(txs)
    after = ledger.count(sid)

    latest_ts = max(t.timestamp for t in txs)
    cursor = ledger.get_cursor(sid)
    if cursor is None or latest_ts > cursor:
        ledger.set_cursor(sid, latest_ts)
    ledger.close()

    new = after - before
    console.print(
        f"[green]✓[/green] {len(txs)} 件処理  |  "
        f"[green]+{new} new[/green]  |  "
        f"{len(txs) - new} already existed (skipped)  |  "
        f"latest: {latest_ts.strftime('%Y-%m-%d %H:%M')} UTC"
    )


# ---------------------------------------------------------------------------
# add (手動でトランザクションを1件追加)
# ---------------------------------------------------------------------------

@cli.command("add")
@click.option("--source", required=True,
              help="追加先のソース識別子（例: pbr_lending）")
@click.option("--type", "tx_type", required=True,
              type=click.Choice([t.value for t in TxType]),
              help="取引種別")
@click.option("--date", "date_str", required=True, metavar="YYYY-MM-DD[THH:MM:SS]",
              help="日時（UTC）。時刻省略時は 00:00:00")
@click.option("--received", nargs=2, type=str, default=None, metavar="ASSET AMOUNT",
              help="受取（入金/報酬など）。例: --received BTC 0.1")
@click.option("--sent", nargs=2, type=str, default=None, metavar="ASSET AMOUNT",
              help="送出（出金など）。例: --sent XRP 50")
@click.option("--fee", nargs=2, type=str, default=None, metavar="ASSET AMOUNT",
              help="手数料。例: --fee JPY 100")
@click.option("--note", default=None, help="メモ（label に格納）")
@click.pass_context
def add_cmd(ctx: click.Context, source: str, tx_type: str, date_str: str,
            received: tuple[str, str] | None, sent: tuple[str, str] | None,
            fee: tuple[str, str] | None, note: str | None) -> None:
    """トランザクションを1件、手動で ledger に追加する。

    CSV出力がない期間の入出金などを手で補うための簡易コマンド。
    \b
    例:
      crypto-summary add --source pbr_lending --type deposit \\
          --date 2026-01-13 --received USDC 3000
      crypto-summary add --source pbr_lending --type withdraw \\
          --date 2026-06-02 --sent XRP 50 --note "返還"
    """
    ts = _parse_date(date_str)
    if ts is None:
        raise click.BadParameter("--date は必須です。")

    def _pair(p: tuple[str, str] | None) -> tuple[str | None, Decimal | None]:
        if not p:
            return None, None
        asset, amount = p
        try:
            return asset.upper(), Decimal(amount)
        except InvalidOperation as e:
            raise click.BadParameter(f"金額が不正です: {amount!r}") from e

    recv_asset, recv_amount = _pair(received)
    sent_asset, sent_amount = _pair(sent)
    fee_asset, fee_amount = _pair(fee)

    if not any([recv_amount, sent_amount, fee_amount]):
        raise click.BadParameter(
            "--received / --sent / --fee のいずれか1つは指定してください。")

    # 同一内容の重複追加を避けるため、入力値からIDを生成する
    raw_key = (f"manual|{date_str}|{tx_type}|"
               f"{recv_asset}:{recv_amount}|{sent_asset}:{sent_amount}|"
               f"{fee_asset}:{fee_amount}|{note or ''}")
    tx = CanonicalTx(
        id=CanonicalTx.make_id(source, raw_key),
        source=source,
        timestamp=ts,
        type=TxType(tx_type),
        received_asset=recv_asset, received_amount=recv_amount,
        sent_asset=sent_asset, sent_amount=sent_amount,
        fee_asset=fee_asset, fee_amount=fee_amount,
        label=note,
        raw={"manual": True},
    )

    ledger = Ledger(ctx.obj["db"])
    before = ledger.count(source)
    ledger.upsert(tx)
    after = ledger.count(source)
    cursor = ledger.get_cursor(source)
    if cursor is None or ts > cursor:
        ledger.set_cursor(source, ts)
    ledger.close()

    if after > before:
        console.print(
            f"[green]✓[/green] 追加しました（{source}）: "
            f"{tx_type} {ts.strftime('%Y-%m-%d %H:%M')} UTC  id={tx.id}")
    else:
        console.print(
            f"[yellow]同一内容が既に存在します（スキップ）: id={tx.id}[/yellow]")


# ---------------------------------------------------------------------------
# remove (手動で1件削除 / CSV単位で削除)
# ---------------------------------------------------------------------------

@cli.command("remove")
@click.option("--id", "tx_id", default=None, metavar="TX_ID",
              help="削除するトランザクションのID（show コマンドで確認）")
@click.option("--file", "filepath", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="このCSVをインポートした際の取引のみを削除する")
@click.option("--exchange", default=None,
              type=click.Choice(list(EXCHANGE_SOURCES.keys())),
              help="--file 使用時のCSVフォーマット（import時と同じ値）")
@click.option("--source-id", default=None,
              help="--file 使用時のソースID（import時に指定したもの。省略時は exchange名）")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="確認プロンプトをスキップ")
@click.pass_context
def remove_cmd(ctx: click.Context, tx_id: str | None, filepath: Path | None,
               exchange: str | None, source_id: str | None, yes: bool) -> None:
    """トランザクションを削除する。

    \b
    1件のみ削除（IDを指定）:
      crypto-summary show --source pbr_lending   # IDを確認
      crypto-summary remove --id a1b2c3d4e5f67890

    \b
    特定CSVの内容だけ削除（import時と同じファイル・フォーマットを指定）:
      crypto-summary remove --file binance_2025.csv --exchange binance

    CSVモードは import と同じアダプタで再パースしてIDを復元し、
    そのIDに一致する取引だけを削除する（他のCSV由来の取引は残る）。
    """
    if filepath is not None:
        _remove_by_file(ctx, filepath, exchange, source_id, yes)
        return
    if tx_id is None:
        raise click.BadParameter("--id か --file のどちらかを指定してください。")
    _remove_by_id(ctx, tx_id, yes)


def _remove_by_id(ctx: click.Context, tx_id: str, yes: bool) -> None:
    ledger = Ledger(ctx.obj["db"])
    tx_list = ledger.all(limit=None)
    target = next((t for t in tx_list if t.id == tx_id), None)

    if target is None:
        console.print(f"[yellow]ID '{tx_id}' は見つかりません。[/yellow]")
        ledger.close()
        return

    console.print(
        f"  {target.timestamp.strftime('%Y-%m-%d %H:%M')} UTC  "
        f"[cyan]{target.type.value}[/cyan]  source={target.source}  id={target.id}"
    )
    if not yes:
        if not click.confirm("このトランザクションを削除しますか？"):
            console.print("[dim]キャンセルしました。[/dim]")
            ledger.close()
            return

    ledger.delete_by_id(tx_id)
    ledger.close()
    console.print(f"[green]✓[/green] 削除しました: id={tx_id}")


def _remove_by_file(ctx: click.Context, filepath: Path, exchange: str | None,
                    source_id: str | None, yes: bool) -> None:
    if exchange is None:
        raise click.BadParameter("--file 使用時は --exchange も指定してください。")
    sid = source_id or exchange
    source = EXCHANGE_SOURCES[exchange](sid)

    console.print(
        f"[cyan]{filepath.name}[/cyan] を [bold]{sid}[/bold] として再パースし、"
        f"一致する取引を検索します ...")
    txs = source.load(filepath)
    if not txs:
        console.print("[yellow]CSVから取引を読み取れませんでした。[/yellow]")
        return

    ledger = Ledger(ctx.obj["db"])
    existing = {t.id for t in ledger.all(source=sid, limit=None)}
    target_ids = [t.id for t in txs if t.id in existing]
    missing = len(txs) - len(target_ids)

    if not target_ids:
        console.print(
            f"[yellow]このCSV由来の取引は ledger に見つかりませんでした"
            f"（source={sid}）。[/yellow]")
        ledger.close()
        return

    console.print(
        f"  CSV {len(txs)} 行のうち [bold]{len(target_ids)} 件[/bold] が "
        f"ledger に存在します"
        + (f"（{missing} 件は未登録）" if missing else "") + "。")
    if not yes:
        if not click.confirm(f"{len(target_ids)} 件削除します。よろしいですか？"):
            console.print("[dim]キャンセルしました。[/dim]")
            ledger.close()
            return

    deleted = sum(1 for tid in target_ids if ledger.delete_by_id(tid))
    ledger.close()
    console.print(
        f"[green]✓[/green] {deleted} 件を削除しました（source={sid}）。")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show a summary of the ledger contents."""
    ledger = Ledger(ctx.obj["db"])
    sources = ledger.sources()
    total = ledger.count()
    ledger.close()

    if not sources:
        console.print("[yellow]Ledger is empty. Run 'crypto-summary import' to add data.[/yellow]")
        return

    table = Table(title="Ledger Status", box=box.ROUNDED)
    table.add_column("Source", style="cyan")
    table.add_column("Transactions", justify="right", style="green")
    table.add_column("Latest cursor", style="dim")

    for src, cnt, cursor_ts in sources:
        table.add_row(src, str(cnt), cursor_ts or "-")

    console.print(table)
    console.print(f"\nTotal: [bold]{total}[/bold] transactions  |  DB: [dim]{ctx.obj['db']}[/dim]")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

@cli.command()
def sources() -> None:
    """import --exchange で指定できるソース一覧を表示する。"""
    table = Table(title="利用可能なソース", box=box.ROUNDED)
    table.add_column("--exchange / --source", style="cyan", min_width=28)
    table.add_column("説明", style="dim")

    _DESC = {
        "binance":              "Binance スポット取引履歴",
        "bitlend":              "BitLending 貸出履歴",
        "pbr_lending":          "PBR Lending 貸出履歴",
        "bitflyer":             "bitFlyer TradeHistory.csv（現物総合台帳）",
        "bitflyer_collateral":  "bitFlyer CollateralHistory.csv（FX/CFD 証拠金）",
        "bitflyer_conversion":  "bitFlyer ConversionHistory.csv（両替）",
        "gmo":                  "GMO コイン取引履歴",
        "nexo_spot":            "Nexo Pro スポット取引",
        "nexo_dnw":             "Nexo Pro 入出金",
        "nexo_savings":         "Nexo 貯蓄口座（nexo_transactions_*.csv）",
        "universal":            "汎用CSV（テスト・未対応取引所用）",
    }

    for key in EXCHANGE_SOURCES:
        table.add_row(key, _DESC.get(key, ""))

    console.print(table)
    console.print(f"\n[dim]使い方: crypto-summary import --file <csv> --exchange <name>[/dim]")


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--source", default=None,
              help="削除するソース（省略すると全データ削除）")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="確認プロンプトをスキップ")
@click.pass_context
def clear(ctx: click.Context, source: str | None, yes: bool) -> None:
    """ledger からトランザクションを削除する（再取り込み前のリセット用）。"""
    ledger = Ledger(ctx.obj["db"])
    target_count = ledger.count(source)

    if target_count == 0:
        console.print(f"[yellow]削除対象がありません"
                      f"{f'（source={source}）' if source else ''}。[/yellow]")
        ledger.close()
        return

    if not yes:
        if not click.confirm(f"{target_count} 件削除します（{source or '全ソース'}）。よろしいですか？"):
            console.print("[dim]キャンセルしました。[/dim]")
            ledger.close()
            return

    n = ledger.clear(source)
    ledger.close()
    console.print(f"[green]✓[/green] {n} 件を削除しました（{source or '全ソース'}）。")


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------

_DUST_THRESHOLD = Decimal("0.00000001")


def _filter_dust(bals: dict[str, Decimal], hide_dust: bool) -> dict[str, Decimal]:
    return {a: v for a, v in bals.items()
            if not hide_dust or abs(v) >= _DUST_THRESHOLD}


def _print_balance_table(filtered: dict[str, Decimal], *, title: str,
                         total_before_filter: int, hide_dust: bool) -> None:
    table = Table(title=title, box=box.ROUNDED)
    table.add_column("資産", style="cyan", min_width=8)
    table.add_column("残高", justify="right", min_width=24)

    for asset in sorted(filtered):
        v = filtered[asset]
        style = "red" if v < 0 else "green" if v > 0 else "dim"
        table.add_row(asset, f"[{style}]{v:.8f}[/{style}]")

    console.print(table)
    if hide_dust:
        hidden = total_before_filter - len(filtered)
        if hidden:
            console.print(f"  [dim]（±0.00000001未満の {hidden} 資産を非表示）[/dim]")


@cli.command()
@click.option("--source", "sources", multiple=True,
              help="ソースで絞り込み（複数指定可、省略で全ソース）")
@click.option("--by-source", is_flag=True, default=False,
              help="ソース（口座）ごとに分けて残高を表示")
@click.option("--since", default=None, metavar="YYYY-MM-DD", help="集計開始日（UTC）")
@click.option("--until", default=None, metavar="YYYY-MM-DD", help="集計終了日（UTC）")
@click.option("--hide-dust", is_flag=True, default=True, show_default=True,
              help="残高が±0.00000001未満の資産を非表示")
@click.pass_context
def balance(ctx: click.Context, sources: tuple[str, ...], by_source: bool,
            since: str | None, until: str | None, hide_dust: bool) -> None:
    """資産ごとの純残高（受取 − 送出 − 手数料）を表示する。

    --source は複数指定できる (例: --source nexo_spot --source nexo_dnw)。
    --by-source を付けると口座ごとに内訳を表示する。
    """
    since_dt = _parse_date(since)
    until_dt = _parse_date(until, end_of_day=True)
    source_filter: list[str] | None = list(sources) or None

    range_suffix = ""
    if since_dt: range_suffix += f" from {since_dt.date()}"
    if until_dt: range_suffix += f" until {until_dt.date()}"

    ledger = Ledger(ctx.obj["db"])
    if by_source:
        per_source = ledger.balances_by_source(
            source=source_filter, since=since_dt, until=until_dt)
        ledger.close()

        if not per_source:
            console.print("[yellow]残高データがありません。[/yellow]")
            return

        for src in sorted(per_source):
            _print_balance_table(
                _filter_dust(per_source[src], hide_dust),
                title=f"残高サマリー ({src}){range_suffix}",
                total_before_filter=len(per_source[src]),
                hide_dust=hide_dust,
            )
        return

    bals = ledger.balances(source=source_filter, since=since_dt, until=until_dt)
    ledger.close()

    if not bals:
        console.print("[yellow]残高データがありません。[/yellow]")
        return

    label = ", ".join(sources) if sources else None
    title = "残高サマリー"
    if label: title += f" ({label})"
    title += range_suffix

    _print_balance_table(
        _filter_dust(bals, hide_dust),
        title=title,
        total_before_filter=len(bals),
        hide_dust=hide_dust,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--source", default=None, help="Filter by source")
@click.option("--type", "tx_type", default=None,
              type=click.Choice(["trade", "deposit", "withdraw", "fee", "reward", "transfer"]),
              help="Filter by transaction type")
@click.option("--limit", default=30, show_default=True, help="Max rows to display")
@click.pass_context
def show(ctx: click.Context, source: str | None, tx_type: str | None, limit: int) -> None:
    """Display normalized transactions from the ledger."""
    ledger = Ledger(ctx.obj["db"])
    txs = ledger.all(source=source, tx_type=tx_type, limit=limit)
    ledger.close()

    if not txs:
        console.print("[yellow]No transactions found.[/yellow]")
        return

    title = f"Transactions (latest {len(txs)}"
    if source:
        title += f", source={source}"
    if tx_type:
        title += f", type={tx_type}"
    title += ")"

    table = Table(title=title, box=box.ROUNDED)
    table.add_column("ID",       style="dim",    min_width=16)
    table.add_column("Timestamp (UTC)", style="dim", min_width=16)
    table.add_column("Type",     style="cyan",   min_width=8)
    table.add_column("Source",   style="dim",    min_width=8)
    table.add_column("Received", style="green",  min_width=20)
    table.add_column("Sent",     style="red",    min_width=20)
    table.add_column("Fee",      style="yellow", min_width=16)

    for tx in txs:
        recv = (
            f"{tx.received_amount:.8f} {tx.received_asset}"
            if tx.received_amount is not None and tx.received_asset
            else "-"
        )
        sent = (
            f"{tx.sent_amount:.8f} {tx.sent_asset}"
            if tx.sent_amount is not None and tx.sent_asset
            else "-"
        )
        fee = (
            f"{tx.fee_amount:.8f} {tx.fee_asset}"
            if tx.fee_amount is not None and tx.fee_asset
            else "-"
        )
        table.add_row(
            tx.id,
            tx.timestamp.strftime("%Y-%m-%d %H:%M"),
            tx.type.value.upper(),
            tx.source,
            recv,
            sent,
            fee,
        )

    console.print(table)
    console.print("[dim]  削除: crypto-summary remove --id <ID>[/dim]")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _parse_date(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            if end_of_day and fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    raise click.BadParameter(f"Cannot parse date: {value!r}. Use YYYY-MM-DD.")


@cli.command("export")
@click.option("--sink", required=True, type=click.Choice(["koinly"]),
              help="Export format / destination")
@click.option("--source", "filter_source", default=None,
              help="Filter by source (default: all sources)")
@click.option("--since", default=None, metavar="YYYY-MM-DD",
              help="Include transactions on or after this date (UTC)")
@click.option("--until", default=None, metavar="YYYY-MM-DD",
              help="Include transactions on or before this date (UTC)")
@click.option("--out", "out_path", default=None, metavar="PATH",
              help="Output file path (default: ./out/<sink>.csv)")
@click.pass_context
def export_cmd(
    ctx: click.Context,
    sink: str,
    filter_source: str | None,
    since: str | None,
    until: str | None,
    out_path: str | None,
) -> None:
    """Export normalized transactions to an external format (e.g. Koinly CSV)."""
    since_dt = _parse_date(since)
    until_dt = _parse_date(until, end_of_day=True)

    ledger = Ledger(ctx.obj["db"])
    txs = ledger.all(
        source=filter_source,
        since=since_dt,
        until=until_dt,
        limit=None,
    )
    ledger.close()

    if not txs:
        console.print("[yellow]No transactions matched the filters.[/yellow]")
        return

    if sink == "koinly":
        from .sinks.koinly_csv import write_koinly_csv
        dest = Path(out_path) if out_path else Path("out") / "koinly.csv"
        n = write_koinly_csv(txs, dest)
        console.print(
            f"[green]✓[/green] Exported [bold]{n}[/bold] transactions to "
            f"[cyan]{dest}[/cyan]"
        )
        if since_dt or until_dt:
            range_parts = []
            if since_dt:
                range_parts.append(f"from {since_dt.date()}")
            if until_dt:
                range_parts.append(f"until {until_dt.date()}")
            console.print(f"  Date filter: [dim]{' '.join(range_parts)}[/dim]")
        if filter_source:
            console.print(f"  Source filter: [dim]{filter_source}[/dim]")


# ---------------------------------------------------------------------------
# fetch (API)
# ---------------------------------------------------------------------------

_API_EXCHANGES = ["bitflyer"]


@cli.command("fetch")
@click.option(
    "--exchange", required=True,
    type=click.Choice(_API_EXCHANGES),
    help="API連携する取引所",
)
@click.option("--source-id", default=None, help="カスタムソースID（デフォルト: exchange名）")
@click.option(
    "--api-key", default=None, envvar="BITFLYER_API_KEY",
    help="APIキー（環境変数 BITFLYER_API_KEY でも可）",
)
@click.option(
    "--api-secret", default=None, envvar="BITFLYER_API_SECRET",
    help="APIシークレット（環境変数 BITFLYER_API_SECRET でも可）",
)
@click.pass_context
def fetch_cmd(
    ctx: click.Context,
    exchange: str,
    source_id: str | None,
    api_key: str | None,
    api_secret: str | None,
) -> None:
    """取引所 API から最新データを取得してledgerに保存する。

    APIキーは読み取り専用権限のみ付与してください（出金権限は不要）。
    .env ファイルに BITFLYER_API_KEY / BITFLYER_API_SECRET を書くか、
    OS 環境変数として設定してください。リポジトリには絶対に含めないこと。
    """
    sid = source_id or exchange

    if not api_key or not api_secret:
        console.print(
            "[red]エラー:[/red] APIキーとシークレットが必要です。\n"
            "  .env に BITFLYER_API_KEY / BITFLYER_API_SECRET を設定するか、\n"
            "  --api-key / --api-secret オプションで指定してください。"
        )
        raise click.Abort()

    if exchange == "bitflyer":
        _fetch_bitflyer(ctx, sid, api_key, api_secret)


def _fetch_bitflyer(
    ctx: click.Context, source_id: str, api_key: str, api_secret: str
) -> None:
    from .sources.api.bitflyer import BitflyerApiSource

    ledger = Ledger(ctx.obj["db"])
    before = ledger.count(source_id)
    console.print(f"Fetching [cyan]bitFlyer[/cyan] as [bold]{source_id}[/bold] ...")

    try:
        src = BitflyerApiSource(source_id, api_key, api_secret)
        txs = src.fetch_all()
    except Exception as e:
        ledger.close()
        console.print(f"[red]API エラー:[/red] {e}")
        raise click.Abort()

    if not txs:
        console.print("[yellow]新しいトランザクションがありません。[/yellow]")
        ledger.close()
        return

    ledger.upsert_many(txs)
    latest_ts = max(t.timestamp for t in txs)
    ledger.set_cursor(source_id, latest_ts)
    after = ledger.count(source_id)
    ledger.close()

    new = after - before
    console.print(
        f"[green]✓[/green] {len(txs)} 件取得  |  "
        f"[green]+{new} new[/green]  |  "
        f"{len(txs) - new} already existed (skipped)  |  "
        f"latest: {latest_ts.strftime('%Y-%m-%d %H:%M')} UTC"
    )
