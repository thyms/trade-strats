import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from dotenv import load_dotenv

from trade_strats import bar_cache
from trade_strats.aggregation import (
    TimedBar,
    aggregate_df,
    df_to_bars,
    parse_tf_minutes,
)
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
from trade_strats.reports import save_backtest, save_walk_forward
from trade_strats.scheduler import run_forever

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
    """Start the bot for one session: reconcile, stream bars, force-flat at EOD, exit."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    asyncio.run(run_session(cfg, schema))


@app.command(name="run-live")
def run_live_cmd(
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e", help="Path to .env"),
    schema: Path = typer.Option(DEFAULT_SCHEMA, "--schema", help="Path to SQLite schema.sql"),
) -> None:
    """Run continuously across multiple trading days.

    Sleeps until the next market open, runs a full session through force-flat,
    then sleeps until the following trading day. Skips weekends and known US
    equity holidays. Survives session crashes by logging and retrying.

    Designed to be left running in a terminal / tmux / launchd for weeks.
    Ctrl+C to stop cleanly.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    typer.echo(f"Starting continuous run for config {config} — Ctrl+C to stop.")
    try:
        asyncio.run(run_forever(cfg, schema))
    except KeyboardInterrupt:
        typer.echo("\nStopped by user.")


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


DEFAULT_BAR_CACHE = Path("data/bars")


async def _ensure_1m_cached_df(
    md: MarketData,
    cache_dir: Path,
    symbol: str,
    start: datetime,
    end: datetime,
) -> "pd.DataFrame":
    """Return 1m bars as a DataFrame, fetching uncached months from Alpaca."""
    import pandas as pd  # noqa: F811 — deferred import for type hint

    start_date = start.astimezone(UTC).date()
    end_date = end.astimezone(UTC).date()

    for mk in bar_cache.missing_months(cache_dir, symbol, start_date, end_date):
        first, last = bar_cache.month_date_range(mk)
        month_start = datetime(first.year, first.month, first.day, tzinfo=UTC)
        month_end = datetime(last.year, last.month, last.day, 23, 59, 59, tzinfo=UTC)
        typer.echo(f"  Fetching {symbol} 1Min {mk}...")
        fetched = await md.backfill(symbol, "1Min", month_start, month_end)
        if fetched:
            bar_cache.save_month(cache_dir, symbol, mk, fetched)

    return bar_cache.load_range_df(cache_dir, symbol, start_date, end_date)


async def _fetch_for_backtest(
    md: MarketData,
    symbol: str,
    start: datetime,
    end: datetime,
    cache_dir: Path = DEFAULT_BAR_CACHE,
) -> tuple[list[TimedBar], list[TimedBar], list[TimedBar], list[TimedBar]]:
    """Cache 1m bars, then aggregate to signal-TF + 1H + 4H + 1D locally."""
    context_start = start - timedelta(days=30)

    # Load 1m bars as DataFrames (fast parquet read, no TimedBar construction).
    df_signal = await _ensure_1m_cached_df(md, cache_dir, symbol, start, end)
    df_context = await _ensure_1m_cached_df(md, cache_dir, symbol, context_start, end)

    # Aggregate in pandas (vectorized), then convert to TimedBar at the end.
    stf_minutes = parse_tf_minutes(md.strategy_tf)
    signal_bars = df_to_bars(aggregate_df(df_signal, stf_minutes))
    daily = df_to_bars(aggregate_df(df_context, 390))
    one_hour = df_to_bars(aggregate_df(df_context, 60))
    four_hour = df_to_bars(aggregate_df(df_context, 240))
    return signal_bars, daily, four_hour, one_hour


@app.command()
def backtest(
    symbol: str = typer.Option(..., "--symbol", "-s", help="Ticker to backtest"),
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    equity: float = typer.Option(50_000.0, "--equity", help="Starting equity for sim"),
    label: str = typer.Option("", "--label", help="Filename suffix to distinguish variants"),
) -> None:
    """Fetch historical bars from Alpaca and run the backtest for one symbol."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        stf = cfg.strategy.timeframe
        md = MarketData(settings, strategy_tf=stf)
        typer.echo(f"Fetching {symbol} {stf} bars from {start} to {end}...")
        signal_bars, daily, four_hour, one_hour = await _fetch_for_backtest(
            md, symbol, start_dt, end_dt
        )
        typer.echo(
            f"Fetched: {len(signal_bars)} {stf}, {len(daily)} 1D, "
            f"{len(four_hour)} 4H (aggregated), {len(one_hour)} 1H"
        )
        if not signal_bars:
            typer.echo(f"No {stf} bars in range.", err=True)
            raise typer.Exit(code=1)
        provider = build_opens_provider(daily, four_hour, one_hour)
        result = run_backtest(symbol, signal_bars, provider, cfg, starting_equity=equity)
        typer.echo("")
        typer.echo(result.summary())
        json_path, md_path = save_backtest(
            result,
            out_dir=cfg.paths.reports_dir / "backtest",
            symbol=symbol,
            start=start,
            end=end,
            config=cfg,
            label=label,
        )
        typer.echo("")
        typer.echo(f"Saved: {json_path}")
        typer.echo(f"Saved: {md_path}")

    asyncio.run(_go())


@app.command(name="walk-forward")
def walk_forward_cmd(
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="YYYY-MM-DD"),
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
    env_file: Path = typer.Option(DEFAULT_ENV, "--env", "-e"),
    equity: float = typer.Option(50_000.0, "--equity"),
    label: str = typer.Option("", "--label", help="Filename suffix to distinguish variants"),
) -> None:
    """Run backtest on each watchlist symbol and print per-pattern breakdowns."""
    load_dotenv(env_file)
    cfg = Config.from_yaml(config)
    start_dt = _parse_date(start)
    end_dt = _parse_date(end)

    async def _go() -> None:
        settings = AlpacaSettings.from_env()
        stf = cfg.strategy.timeframe
        md = MarketData(settings, strategy_tf=stf)
        bars_by_symbol: dict[str, list[TimedBar]] = {}
        opens_by_symbol: dict[str, OpensProvider] = {}
        for symbol in cfg.watchlist:
            typer.echo(f"Fetching {symbol}...")
            signal_bars, daily, four_hour, one_hour = await _fetch_for_backtest(
                md, symbol, start_dt, end_dt
            )
            if not signal_bars:
                typer.echo(f"  {symbol}: no {stf} bars, skipping", err=True)
                continue
            bars_by_symbol[symbol] = signal_bars
            opens_by_symbol[symbol] = build_opens_provider(daily, four_hour, one_hour)
        if not bars_by_symbol:
            typer.echo("No data fetched for any symbol.", err=True)
            raise typer.Exit(code=1)
        report = run_walk_forward(bars_by_symbol, opens_by_symbol, cfg, starting_equity=equity)
        typer.echo("")
        typer.echo(report.summary())
        json_path, md_path = save_walk_forward(
            report,
            out_dir=cfg.paths.reports_dir / "walk-forward",
            start=start,
            end=end,
            config=cfg,
            starting_equity=equity,
            label=label,
        )
        typer.echo("")
        typer.echo(f"Saved: {json_path}")
        typer.echo(f"Saved: {md_path}")

    asyncio.run(_go())


if __name__ == "__main__":
    app()
