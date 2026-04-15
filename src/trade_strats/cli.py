import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from dotenv import load_dotenv

from trade_strats.aggregation import TimedBar, aggregate, bucket_4h
from trade_strats.backtest import (
    OpensProvider,
    build_opens_provider,
    run_backtest,
    run_walk_forward,
)
from trade_strats.config import Config
from trade_strats.execution import Executor
from trade_strats.journal import Journal
from trade_strats.market_data import AlpacaSettings, MarketData
from trade_strats.orchestrator import run_session
from trade_strats.reconcile import format_report, reconcile

app = typer.Typer(help="TheStrat 15m trading bot")

DEFAULT_CONFIG = Path("config/config.yaml")
DEFAULT_SCHEMA = Path("data/schema.sql")
DEFAULT_ENV = Path(".env")


@app.command()
def run(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e", help="Path to .env"),
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema", help="Path to SQLite schema.sql"),
) -> None:
    """Start the bot: reconcile, seed session, stream bars + trade updates until EOD."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    asyncio.run(run_session(cfg, schema))


@app.command(name="reconcile")
def reconcile_cmd(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema"),
) -> None:
    """Compare Alpaca state to the journal and print a drift report."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        executor = Executor(settings)
        journal = await Journal.open(
            db_path=cfg.paths.db,
            events_path=cfg.paths.events_log,
            schema_path=schema,
        )
        async with journal:
            report = await reconcile(executor, journal)
            typer.echo(format_report(report))
            raise typer.Exit(code=0 if report.clean else 1)

    asyncio.run(_go())


@app.command()
def status(
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
) -> None:
    """Print a one-shot Alpaca account snapshot (no TUI, scriptable)."""
    load_dotenv(env_file)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        executor = Executor(settings)
        account = await executor.get_account()
        positions = await executor.get_positions()
        open_orders = await executor.get_open_orders()
        typer.echo(f"mode:          {'paper' if settings.paper else 'live'}")
        typer.echo(f"equity:        ${account.equity:,.2f}")
        typer.echo(f"cash:          ${account.cash:,.2f}")
        typer.echo(f"buying power:  ${account.buying_power:,.2f}")
        typer.echo(f"positions:     {len(positions)}")
        for p in positions:
            typer.echo(
                f"  {p.symbol:6s} {p.side:5s} {p.qty:>6d} @ {p.avg_entry_price:.2f}"
                f"  (current {p.current_price:.2f}, unreal {p.unrealized_pnl:+.2f})"
            )
        typer.echo(f"open orders:   {len(open_orders)}")
        for o in open_orders:
            typer.echo(f"  {o.symbol:6s} {o.side:5s} {o.qty:>6d}  {o.order_type:12s} {o.status}")

    asyncio.run(_go())


@app.command(name="flat-all")
def flat_all(
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Emergency: cancel all orders and market-close all positions."""
    load_dotenv(env_file)
    if not yes:
        typer.echo("This will CANCEL ALL OPEN ORDERS and MARKET-CLOSE ALL POSITIONS.")
        if not typer.confirm("Continue?"):
            raise typer.Exit(code=1)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        executor = Executor(settings)
        await executor.flat_all()
        typer.echo("flat-all executed")

    asyncio.run(_go())


def _parse_date(value: str) -> datetime:
    """Accept YYYY-MM-DD or full ISO string; return UTC datetime."""
    if "T" in value:
        dt = datetime.fromisoformat(value)
    else:
        dt = datetime.fromisoformat(f"{value}T00:00:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _fetch_for_backtest(
    md: MarketData, symbol: str, start: datetime, end: datetime
) -> tuple[list[TimedBar], list[TimedBar], list[TimedBar], list[TimedBar]]:
    """Fetch 15m + 1H + 1D from Alpaca; locally aggregate 4H from 1H."""
    context_start = start - timedelta(days=30)
    bars_15m = await md.backfill(symbol, "15Min", start, end)
    daily = await md.backfill(symbol, "1D", context_start, end)
    one_hour = await md.backfill(symbol, "1H", context_start, end)
    four_hour = aggregate(one_hour, bucket_4h)
    return bars_15m, daily, four_hour, one_hour


@app.command()
def backtest(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Ticker to backtest"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    equity: float = typer.Option(50_000.0, "--equity", help="Starting equity for sim"),
) -> None:
    """Fetch historical bars from Alpaca and run the backtest for one symbol."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        md = MarketData(settings)
        typer.echo(f"Fetching {symbol} bars from {start} to {end}...")
        bars_15m, daily, four_hour, one_hour = await _fetch_for_backtest(
            md, symbol, start_dt, end_dt
        )
        typer.echo(
            f"Fetched: {len(bars_15m)} 15m, {len(daily)} 1D, "
            f"{len(four_hour)} 4H (aggregated), {len(one_hour)} 1H"
        )
        if not bars_15m:
            typer.echo("No 15m bars in range.", err=True)
            raise typer.Exit(code=1)
        provider = build_opens_provider(daily, four_hour, one_hour)
        result = run_backtest(symbol, bars_15m, provider, cfg, starting_equity=equity)
        typer.echo("")
        typer.echo(result.summary())

    asyncio.run(_go())


@app.command(name="walk-forward")
def walk_forward_cmd(
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    equity: float = typer.Option(50_000.0, "--equity"),
) -> None:
    """Run backtest on each watchlist symbol and print per-pattern breakdowns."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        md = MarketData(settings)
        bars_by_symbol: dict[str, list[TimedBar]] = {}
        opens_by_symbol: dict[str, OpensProvider] = {}
        for symbol in cfg.watchlist:
            typer.echo(f"Fetching {symbol}...")
            bars_15m, daily, four_hour, one_hour = await _fetch_for_backtest(
                md, symbol, start_dt, end_dt
            )
            if not bars_15m:
                typer.echo(f"  {symbol}: no 15m bars, skipping", err=True)
                continue
            bars_by_symbol[symbol] = bars_15m
            opens_by_symbol[symbol] = build_opens_provider(daily, four_hour, one_hour)
        if not bars_by_symbol:
            typer.echo("No data fetched for any symbol.", err=True)
            raise typer.Exit(code=1)
        report = run_walk_forward(bars_by_symbol, opens_by_symbol, cfg, starting_equity=equity)
        typer.echo("")
        typer.echo(report.summary())

    asyncio.run(_go())


if __name__ == "__main__":
    app()
