from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from .core.ledger import Ledger
from .sources.csv_import import EXCHANGE_SOURCES

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
            tx.timestamp.strftime("%Y-%m-%d %H:%M"),
            tx.type.value.upper(),
            tx.source,
            recv,
            sent,
            fee,
        )

    console.print(table)


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
