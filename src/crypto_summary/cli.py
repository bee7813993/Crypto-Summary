from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from .core.ledger import Ledger
from .core.models import CanonicalTx, TxType
from .core.prices import fetch_prices as _fetch_prices_core
from .sources.csv_import import EXCHANGE_SOURCES


def _fetch_prices(assets: list[str], currency: str) -> dict[str, Decimal]:
    """core.prices.fetch_prices のCLI用ラッパー（警告を rich で表示）。"""
    return _fetch_prices_core(
        assets, currency,
        warn=lambda m: console.print(f"[yellow]警告: {m}[/yellow]"),
    )

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

    # CSV単位での削除に使えるよう、取り込みをバッチ記録する
    import uuid as _uuid
    batch_id = f"batch:{_uuid.uuid4().hex[:12]}"
    ledger.record_import_batch(batch_id, sid, exchange, filepath.name, [t.id for t in txs])
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
@click.option("--gas/--no-gas", "record_gas", default=True, show_default=True,
              help="ガス代を FEE として記録する（実際のウォレット残高と一致させるため既定で有効）")
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
# fetch-wallet (Etherscan V2 API でEVMウォレット取得)
# ---------------------------------------------------------------------------

@cli.command("fetch-wallet")
@click.option(
    "--chain", required=True,
    type=click.Choice([
        "ethereum", "arbitrum", "polygon", "base", "optimism", "solana",
    ]),
    help="取得するチェーン（EVM 5種 + solana）",
)
@click.option("--wallet", "wallet_address", required=True,
              help="ウォレットアドレス（EVM: 0x... / Solana: base58）")
@click.option("--source-id", default=None, help="ソース識別子（デフォルト: chain名）")
@click.option(
    "--api-key", default=None, envvar="ETHERSCAN_API_KEY",
    help="Etherscan V2 APIキー（EVM用、環境変数 ETHERSCAN_API_KEY でも可）",
)
@click.option(
    "--helius-api-key", default=None, envvar="HELIUS_API_KEY",
    help="Helius APIキー（Solana用、環境変数 HELIUS_API_KEY でも可）",
)
@click.option("--gas/--no-gas", "record_gas", default=True, show_default=True,
              help="ガス代を FEE として記録する（実残高と一致させるため既定で有効）")
@click.pass_context
def fetch_wallet_cmd(
    ctx: click.Context,
    chain: str,
    wallet_address: str,
    source_id: str | None,
    api_key: str | None,
    helius_api_key: str | None,
    record_gas: bool,
) -> None:
    """取引履歴を API で取得して ledger に保存する。

    EVM チェーン（Etherscan V2 API）と Solana（Helius API）に対応。
    APIキーはいずれも読み取り専用で出金権限は不要。

    \b
    EVM 例:
      crypto-summary fetch-wallet --chain arbitrum \\
          --wallet 0xABC...123 --source-id my_arbitrum
    Solana 例:
      crypto-summary fetch-wallet --chain solana \\
          --wallet YOURWALLET... --source-id my_solana
    """
    sid = source_id or chain

    if chain == "solana":
        from .sources.solana.helius import HeliusApiSource

        key = helius_api_key
        if not key:
            console.print(
                "[red]エラー:[/red] Solana には Helius APIキーが必要です。\n"
                "  .env に HELIUS_API_KEY を設定するか、--helius-api-key で指定してください。\n"
                "  発行: https://dev.helius.xyz （無料枠で取得可）"
            )
            raise click.Abort()

        adapter = HeliusApiSource(sid, wallet_address, key)
    else:
        from .sources.api.etherscan import EtherscanApiSource, CHAIN_IDS

        key = api_key
        if not key:
            console.print(
                "[red]エラー:[/red] EVM には Etherscan V2 APIキーが必要です。\n"
                "  .env に ETHERSCAN_API_KEY を設定するか、--api-key で指定してください。\n"
                "  発行: https://etherscan.io/myapikey"
            )
            raise click.Abort()

        adapter = EtherscanApiSource(sid, wallet_address, key, CHAIN_IDS[chain])
    console.print(
        f"Fetching [cyan]{chain}[/cyan] wallet "
        f"[dim]{wallet_address}[/dim] as [bold]{sid}[/bold] ..."
    )


    try:
        txs = adapter.fetch_all(record_gas=record_gas)
    except Exception as e:
        console.print(f"[red]API エラー:[/red] {e}")
        raise click.Abort()

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


def _print_balance_table(
    filtered: dict[str, Decimal],
    *,
    title: str,
    total_before_filter: int,
    hide_dust: bool,
    prices: dict[str, Decimal] | None = None,
    currency: str | None = None,
) -> None:
    table = Table(title=title, box=box.ROUNDED)
    table.add_column("資産", style="cyan", min_width=8)
    table.add_column("残高", justify="right", min_width=24)
    if prices is not None and currency:
        table.add_column("評価額", justify="right", min_width=20)

    total_fiat = Decimal(0)
    has_any_price = False

    for asset in sorted(filtered):
        v = filtered[asset]
        style = "red" if v < 0 else "green" if v > 0 else "dim"
        balance_str = f"[{style}]{v:.8f}[/{style}]"

        if prices is not None and currency:
            price = prices.get(asset)
            if price is not None:
                fiat_val = v * price
                total_fiat += fiat_val
                has_any_price = True
                fiat_str = f"{fiat_val:,.2f} {currency}"
            else:
                fiat_str = "-"
            table.add_row(asset, balance_str, fiat_str)
        else:
            table.add_row(asset, balance_str)

    if prices is not None and currency and has_any_price:
        table.add_section()
        table.add_row(
            "[bold]合計[/bold]",
            "",
            f"[bold]{total_fiat:,.2f} {currency}[/bold]",
        )

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
@click.option("--currency", default=None,
              type=click.Choice(["USD", "JPY", "EUR", "GBP"]),
              help="法定通貨建ての評価額を表示（CoinGecko APIを使用）")
@click.pass_context
def balance(ctx: click.Context, sources: tuple[str, ...], by_source: bool,
            since: str | None, until: str | None, hide_dust: bool,
            currency: str | None) -> None:
    """資産ごとの純残高（受取 − 送出 − 手数料）を表示する。

    --source は複数指定できる (例: --source nexo_spot --source nexo_dnw)。
    --by-source を付けると口座ごとに内訳を表示する。
    --currency を指定すると CoinGecko から現在価格を取得して評価額を表示する。
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

        # 全ソースの資産をまとめて一度だけ価格取得する（429 回避）
        prices: dict[str, Decimal] | None = None
        if currency:
            all_assets = {a for bals in per_source.values() for a in bals}
            prices = _fetch_prices(sorted(all_assets), currency)

        for src in sorted(per_source):
            filtered = _filter_dust(per_source[src], hide_dust)
            _print_balance_table(
                filtered,
                title=f"残高サマリー ({src}){range_suffix}",
                total_before_filter=len(per_source[src]),
                hide_dust=hide_dust,
                prices=prices,
                currency=currency,
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

    filtered = _filter_dust(bals, hide_dust)
    prices = None
    if currency:
        prices = _fetch_prices(list(filtered.keys()), currency)

    _print_balance_table(
        filtered,
        title=title,
        total_before_filter=len(bals),
        hide_dust=hide_dust,
        prices=prices,
        currency=currency,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def _compute_running_balances(
    all_txs: list[CanonicalTx],
) -> dict[str, dict[str, Decimal]]:
    """Walk all_txs in timestamp order and return per-tx running balances.

    Returns: {tx_id: {asset: balance_after_tx}}
    """
    balances: dict[str, Decimal] = {}
    result: dict[str, dict[str, Decimal]] = {}

    for tx in all_txs:
        if tx.received_asset and tx.received_amount is not None:
            balances[tx.received_asset] = (
                balances.get(tx.received_asset, Decimal(0)) + tx.received_amount
            )
        if tx.sent_asset and tx.sent_amount is not None:
            balances[tx.sent_asset] = (
                balances.get(tx.sent_asset, Decimal(0)) - tx.sent_amount
            )
        if tx.fee_asset and tx.fee_amount is not None:
            balances[tx.fee_asset] = (
                balances.get(tx.fee_asset, Decimal(0)) - tx.fee_amount
            )
        result[tx.id] = dict(balances)

    return result


def _running_balance_str(tx: CanonicalTx, balances_at: dict[str, dict[str, Decimal]]) -> str:
    """Format the running balance string for a given tx."""
    bal = balances_at.get(tx.id, {})
    tx_type = tx.type.value.lower()

    if tx_type == "trade":
        parts = []
        if tx.sent_asset and tx.sent_asset in bal:
            sv = bal[tx.sent_asset]
            parts.append(f"{tx.sent_asset}: {sv:.6f}")
        if tx.received_asset and tx.received_asset in bal:
            rv = bal[tx.received_asset]
            parts.append(f"{tx.received_asset}: {rv:.6f}")
        return " / ".join(parts) if parts else "-"

    if tx_type == "fee":
        if tx.fee_asset and tx.fee_asset in bal:
            return f"{tx.fee_asset}: {bal[tx.fee_asset]:.6f}"
        return "-"

    if tx_type in ("deposit", "reward"):
        if tx.received_asset and tx.received_asset in bal:
            return f"{tx.received_asset}: {bal[tx.received_asset]:.6f}"
        return "-"

    if tx_type in ("withdraw", "transfer"):
        if tx.sent_asset and tx.sent_asset in bal:
            return f"{tx.sent_asset}: {bal[tx.sent_asset]:.6f}"
        return "-"

    # Fallback: prefer non-fee asset
    for asset in (tx.received_asset, tx.sent_asset, tx.fee_asset):
        if asset and asset in bal:
            return f"{asset}: {bal[asset]:.6f}"
    return "-"


@cli.command()
@click.option("--source", default=None, help="Filter by source")
@click.option("--type", "tx_type", default=None,
              type=click.Choice(["trade", "deposit", "withdraw", "fee", "reward", "transfer"]),
              help="Filter by transaction type")
@click.option("--since", default=None, metavar="YYYY-MM-DD", help="Filter from date (UTC)")
@click.option("--until", default=None, metavar="YYYY-MM-DD", help="Filter until date (UTC)")
@click.option("--limit", default=30, show_default=True, help="Max rows to display")
@click.option("--running-balance", "running_balance", is_flag=True, default=False,
              help="Show per-asset running balance after each transaction")
@click.pass_context
def show(ctx: click.Context, source: str | None, tx_type: str | None,
         since: str | None, until: str | None, limit: int,
         running_balance: bool) -> None:
    """Display normalized transactions from the ledger."""
    since_dt = _parse_date(since)
    until_dt = _parse_date(until, end_of_day=True)

    ledger = Ledger(ctx.obj["db"])

    balances_at: dict[str, dict[str, Decimal]] = {}
    if running_balance:
        # Fetch ALL matching txs (no limit) to compute running balances
        all_txs = ledger.all(
            source=source, tx_type=tx_type,
            since=since_dt, until=until_dt,
            limit=None,
        )
        balances_at = _compute_running_balances(all_txs)

    txs = ledger.all(
        source=source, tx_type=tx_type,
        since=since_dt, until=until_dt,
        limit=limit,
    )
    ledger.close()

    if not txs:
        console.print("[yellow]No transactions found.[/yellow]")
        return

    title = f"Transactions (latest {len(txs)}"
    if source:
        title += f", source={source}"
    if tx_type:
        title += f", type={tx_type}"
    if since_dt:
        title += f", from={since_dt.date()}"
    if until_dt:
        title += f", until={until_dt.date()}"
    title += ")"

    table = Table(title=title, box=box.ROUNDED)
    table.add_column("ID",       style="dim",    min_width=16)
    table.add_column("Timestamp (UTC)", style="dim", min_width=16)
    table.add_column("Type",     style="cyan",   min_width=8)
    table.add_column("Source",   style="dim",    min_width=8)
    table.add_column("Received", style="green",  min_width=20)
    table.add_column("Sent",     style="red",    min_width=20)
    table.add_column("Fee",      style="yellow", min_width=16)
    if running_balance:
        table.add_column("残高", style="blue", min_width=28)

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
        row = [
            tx.id,
            tx.timestamp.strftime("%Y-%m-%d %H:%M"),
            tx.type.value.upper(),
            tx.source,
            recv,
            sent,
            fee,
        ]
        if running_balance:
            row.append(_running_balance_str(tx, balances_at))
        table.add_row(*row)

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

_API_EXCHANGES = ["bitflyer", "bybit"]

# 取引所ごとの APIキー/シークレットを読む環境変数名
_API_ENV: dict[str, tuple[str, str]] = {
    "bitflyer": ("BITFLYER_API_KEY", "BITFLYER_API_SECRET"),
    "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
}

# .env.example のプレースホルダ値（コピーしたまま起動した場合の誤認識を防ぐ）。
_API_KEY_PLACEHOLDERS = frozenset({
    "your_api_key_here",
    "your_api_secret_here",
    "your_api_key",
    "your_api_secret",
    "changeme",
})


def _env_api_value(env_name: str) -> str | None:
    """環境変数から取引所キーを取得する（プレースホルダは未設定扱い）。"""
    import os

    val = os.environ.get(env_name, "").strip()
    if not val or val.lower() in _API_KEY_PLACEHOLDERS:
        return None
    return val


@cli.command("fetch")
@click.option(
    "--exchange", default=None,
    type=click.Choice(_API_EXCHANGES),
    help="API連携する取引所（登録済み口座を --source-id で使う場合は省略可）",
)
@click.option("--source-id", default=None, help="ソースID（登録済み口座のキーを使用）")
@click.option("--api-key", default=None, help="APIキー（未指定時は登録済み口座/環境変数を使用）")
@click.option("--api-secret", default=None,
              help="APIシークレット（未指定時は登録済み口座/環境変数を使用）")
@click.pass_context
def fetch_cmd(
    ctx: click.Context,
    exchange: str | None,
    source_id: str | None,
    api_key: str | None,
    api_secret: str | None,
) -> None:
    """取引所 API から最新データを取得してledgerに保存する。

    APIキーは読み取り専用権限のみ付与してください（出金/送付権限は不要）。
    \b
    使い方:
      事前に口座を登録（推奨。キーは暗号化保存）:
        crypto-summary account add-api --exchange bybit --source-id mybybit \\
            --api-key ... --api-secret ...
        crypto-summary fetch --source-id mybybit
      その場で指定 / 環境変数:
        crypto-summary fetch --exchange bybit --api-key ... --api-secret ...
        （または .env: BYBIT_API_KEY/SECRET, BITFLYER_API_KEY/SECRET）
    """
    if not exchange and not source_id:
        console.print("[red]エラー:[/red] --exchange か --source-id のいずれかを指定してください。")
        raise click.Abort()
    import os

    from .core.secrets import SecretStore, SecretStoreError

    sid = source_id or exchange
    category = "spot"

    # 資格情報の解決順:
    #   1) --api-key/--api-secret が明示された → それを使う（--exchange 必須）
    #   2) 暗号化ストアに口座登録がある → 復号して使う（exchange も保存値）
    #   3) 取引所別の環境変数 → それを使う（--exchange 必須）
    if api_key and api_secret:
        if not exchange:
            console.print("[red]エラー:[/red] --api-key 使用時は --exchange も指定してください。")
            raise click.Abort()
    else:
        store = SecretStore(ctx.obj["db"])
        creds = None
        if source_id:
            try:
                creds = store.get_account_api(source_id)
            except SecretStoreError as e:
                console.print(f"[red]エラー:[/red] {e}")
                raise click.Abort()
        if creds:
            exchange = creds["exchange"]
            api_key = creds["api_key"]
            api_secret = creds["api_secret"]
            category = creds.get("category", "spot")
        elif exchange:
            key_env, secret_env = _API_ENV[exchange]
            api_key = _env_api_value(key_env)
            api_secret = _env_api_value(secret_env)

    if not api_key or not api_secret:
        hint = ""
        if exchange:
            key_env, secret_env = _API_ENV[exchange]
            hint = f"  .env に {key_env} / {secret_env} を設定、または\n"
        console.print(
            "[red]エラー:[/red] APIキーとシークレットが見つかりません。\n"
            f"{hint}"
            "  事前に登録: crypto-summary account add-api ...、または\n"
            "  --api-key / --api-secret で直接指定してください。"
        )
        raise click.Abort()

    if exchange == "bitflyer":
        _fetch_bitflyer(ctx, sid, api_key, api_secret)
    elif exchange == "bybit":
        _fetch_bybit(ctx, sid, api_key, api_secret, category)
    else:
        console.print(f"[red]エラー:[/red] 未対応の取引所です: {exchange}")
        raise click.Abort()


def _save_fetched(
    ctx: click.Context, source_id: str, label: str, txs: list[CanonicalTx]
) -> None:
    """取得済み CanonicalTx を ledger に保存して結果を表示する（共通処理）。"""
    ledger = Ledger(ctx.obj["db"])
    before = ledger.count(source_id)

    if not txs:
        console.print("[yellow]新しいトランザクションがありません。[/yellow]")
        ledger.close()
        return

    ledger.upsert_many(txs)
    latest_ts = max(t.timestamp for t in txs)
    cursor = ledger.get_cursor(source_id)
    if cursor is None or latest_ts > cursor:
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


def _fetch_bitflyer(
    ctx: click.Context, source_id: str, api_key: str, api_secret: str
) -> None:
    from .sources.api.bitflyer import BitflyerApiSource

    console.print(f"Fetching [cyan]bitFlyer[/cyan] as [bold]{source_id}[/bold] ...")
    try:
        src = BitflyerApiSource(source_id, api_key, api_secret)
        txs = src.fetch_all()
    except Exception as e:
        console.print(f"[red]API エラー:[/red] {e}")
        raise click.Abort()

    _save_fetched(ctx, source_id, "bitFlyer", txs)


def _fetch_bybit(
    ctx: click.Context, source_id: str, api_key: str, api_secret: str,
    category: str = "spot",
) -> None:
    from .sources.api.bybit import BybitApiSource

    console.print(f"Fetching [cyan]Bybit[/cyan] as [bold]{source_id}[/bold] ...")
    try:
        src = BybitApiSource(source_id, api_key, api_secret, category=category)
        txs = src.fetch_all()
    except Exception as e:
        console.print(f"[red]API エラー:[/red] {e}")
        raise click.Abort()

    _save_fetched(ctx, source_id, "Bybit", txs)


# ---------------------------------------------------------------------------
# account（口座ごとの API キー登録 / 暗号化保存）
# ---------------------------------------------------------------------------

@cli.group("account")
def account_group() -> None:
    """口座ごとの API キー登録（暗号化保存）。

    APIキーは取引所アカウント（個人）に紐づくため、口座ごとに登録する。
    キーはマスター鍵 CS_SECRET_KEY で暗号化し <dbname>.secrets.json に保存する
    （平文はどの追跡ファイルにも残さない）。読み取り専用キーのみ登録すること。
    """


@account_group.command("gen-key")
def account_gen_key() -> None:
    """マスター鍵を生成して表示する（.env の CS_SECRET_KEY に設定する）。"""
    from .core.secrets import generate_master_key

    key = generate_master_key()
    console.print("生成したマスター鍵（.env に保存してください。紛失すると復号不可）:")
    console.print(f"\n  [bold]CS_SECRET_KEY={key}[/bold]\n")
    console.print("[dim]リポジトリには絶対にコミットしないこと（.env は .gitignore 済み）。[/dim]")


@account_group.command("add-api")
@click.option("--exchange", required=True, type=click.Choice(_API_EXCHANGES),
              help="取引所")
@click.option("--source-id", required=True, help="この口座のソースID（fetch で指定する名前）")
@click.option("--api-key", required=True, help="読み取り専用 APIキー")
@click.option("--api-secret", required=True, help="APIシークレット")
@click.option("--category", default="spot", show_default=True,
              help="Bybit のカテゴリ（spot 等）")
@click.option("--user", "user_id", default="local", show_default=True,
              help="ユーザーID（将来のマルチユーザー用）")
@click.pass_context
def account_add_api(ctx: click.Context, exchange: str, source_id: str,
                    api_key: str, api_secret: str, category: str, user_id: str) -> None:
    """口座の API キーを暗号化して登録する。"""
    from .core.secrets import SecretStore, SecretStoreError

    store = SecretStore(ctx.obj["db"])
    try:
        store.set_account_api(
            source_id, exchange, api_key, api_secret,
            category=category, user_id=user_id,
        )
    except SecretStoreError as e:
        console.print(f"[red]エラー:[/red] {e}")
        raise click.Abort()
    console.print(
        f"[green]✓[/green] 登録しました: [bold]{source_id}[/bold] "
        f"({exchange}, user={user_id})\n"
        f"[dim]取得: crypto-summary fetch --source-id {source_id}[/dim]"
    )


@account_group.command("list-api")
@click.option("--user", "user_id", default="local", show_default=True,
              help="ユーザーID（all で全ユーザー）")
@click.pass_context
def account_list_api(ctx: click.Context, user_id: str) -> None:
    """登録済み口座（API連携）を一覧表示する。秘密情報は表示しない。"""
    from .core.secrets import SecretStore

    store = SecretStore(ctx.obj["db"])
    rows = store.list_accounts(user_id=None if user_id == "all" else user_id)
    if not rows:
        console.print("[yellow]登録済みの口座がありません。[/yellow]")
        return

    table = Table(title="登録済み口座（API連携）", box=box.ROUNDED)
    table.add_column("ユーザー", style="dim")
    table.add_column("ソースID", style="cyan")
    table.add_column("取引所", style="green")
    table.add_column("カテゴリ", style="dim")
    table.add_column("登録日時", style="dim")
    for r in rows:
        table.add_row(r["user_id"], r["source_id"], r["exchange"],
                      r["category"], r["created_at"][:19])
    console.print(table)


@account_group.command("remove-api")
@click.option("--source-id", required=True, help="削除する口座のソースID")
@click.option("--user", "user_id", default="local", show_default=True, help="ユーザーID")
@click.pass_context
def account_remove_api(ctx: click.Context, source_id: str, user_id: str) -> None:
    """口座の API 資格情報を削除する。"""
    from .core.secrets import SecretStore

    store = SecretStore(ctx.obj["db"])
    if store.delete_account(source_id, user_id=user_id):
        console.print(f"[green]✓[/green] 削除しました: {source_id} (user={user_id})")
    else:
        console.print(f"[yellow]登録が見つかりません: {source_id} (user={user_id})[/yellow]")


# ---------------------------------------------------------------------------
# web (ダッシュボード WebUI)
# ---------------------------------------------------------------------------

def _lan_ip() -> str | None:
    """このマシンの LAN IP アドレスを推定して返す（取得失敗時は None）。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 実際には送信しないが、ルーティングから自分の出口IPを得る
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


@cli.command("web")
@click.option("--host", default="127.0.0.1", show_default=True, help="バインドするホスト")
@click.option("--port", default=8000, show_default=True, type=int, help="ポート番号")
@click.option("--lan", is_flag=True, default=False,
              help="LAN 内の他端末（スマホ等）からアクセス可能にする（host=0.0.0.0）")
@click.option("--reload", is_flag=True, default=False, help="開発用オートリロード")
@click.pass_context
def web_cmd(ctx: click.Context, host: str, port: int, lan: bool, reload: bool) -> None:
    """ダッシュボード WebUI を起動する。

    ブラウザで http://HOST:PORT を開くと資産サマリーを表示できる。
    価格は CoinGecko（read-only）から取得する。

    \b
    例:
      crypto-summary web
      crypto-summary web --lan          # スマホなど同じWi-Fiの端末から見る
      crypto-summary --db my.db web --port 8080
    """
    try:
        import uvicorn  # noqa: F401
        from .web import create_app
    except ImportError:
        console.print(
            "[red]エラー:[/red] Web UI には追加の依存が必要です。\n"
            "  [cyan]pip install 'crypto-summary[web]'[/cyan] "
            "または [cyan]pip install fastapi uvicorn[/cyan] を実行してください。"
        )
        raise click.Abort()

    # --lan 指定時は全インターフェースにバインドする
    if lan:
        host = "0.0.0.0"

    db = ctx.obj["db"]
    app = create_app(db)

    # アクセス用URLを案内する
    if host == "0.0.0.0":
        console.print("[green]Crypto-Summary Web UI[/green] でアクセス可能なURL:")
        console.print(f"  [cyan]http://127.0.0.1:{port}[/cyan]  (このPC)")
        ip = _lan_ip()
        if ip:
            console.print(f"  [cyan]http://{ip}:{port}[/cyan]  (同じWi-Fiのスマホ等から)")
        console.print(f"  (db: [dim]{db}[/dim])  [dim]停止: Ctrl+C[/dim]")
    else:
        console.print(
            f"[green]Crypto-Summary Web UI[/green] → "
            f"[cyan]http://{host}:{port}[/cyan]  (db: [dim]{db}[/dim])\n"
            f"[dim]同じWi-Fiのスマホ等から見るには --lan を付けて再起動してください[/dim]\n"
            f"[dim]停止: Ctrl+C[/dim]"
        )
    import uvicorn
    # loop="asyncio" で Windows の ProactorEventLoop を避ける。
    # ProactorEventLoop はブラウザがリクエストをキャンセルした際に
    # WinError 10054 を大量出力するため SelectorEventLoop を使う。
    # 他 OS では uvicorn が loop 引数を無視するので影響なし。
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="warning", loop="asyncio")
