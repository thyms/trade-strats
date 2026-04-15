import asyncio
from pathlib import Path

import typer
from dotenv import load_dotenv

from trade_strats.config import Config
from trade_strats.execution import Executor
from trade_strats.journal import Journal
from trade_strats.market_data import AlpacaSettings
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


if __name__ == "__main__":
    app()
