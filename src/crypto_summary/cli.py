from __future__ import annotations

from datetime import datetime, timezone
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
