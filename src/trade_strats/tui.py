import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


@dataclass
class SymbolState:
    symbol: str
    last_price: float | None = None
    scenario: str = "-"
    ftfc: str = "-"
    last_event: str = ""


@dataclass
class PositionRow:
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    unrealized: float


def _empty_symbols() -> dict[str, SymbolState]:
    return {}


def _empty_positions() -> list[PositionRow]:
    return []


def _empty_events() -> list[str]:
    return []


@dataclass
class SessionState:
    mode: str = "paper"
    equity: float = 0.0
    day_pnl: float = 0.0
    trades_today: int = 0
    max_trades: int = 5
    loss_cap_usd: float = 0.0
    session_status: str = "starting"
    symbols: dict[str, SymbolState] = field(default_factory=_empty_symbols)
    positions: list[PositionRow] = field(default_factory=_empty_positions)
    events: list[str] = field(default_factory=_empty_events)

    def push_event(self, line: str) -> None:
        self.events.append(f"{datetime.now().strftime('%H:%M:%S')}  {line}")
        if len(self.events) > 10:
            self.events.pop(0)

    def upsert_symbol(self, symbol: str) -> SymbolState:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol=symbol)
        return self.symbols[symbol]


def _header(state: SessionState) -> Panel:
    line = (
        f"mode={state.mode}   session={state.session_status}   "
        f"equity=${state.equity:,.2f}   day PnL=${state.day_pnl:,.2f}   "
        f"trades={state.trades_today}/{state.max_trades}   "
        f"loss cap=${state.loss_cap_usd:,.2f}"
    )
    return Panel(line, title="TheStrat Bot", border_style="cyan")


def _watchlist_table(state: SessionState) -> Table:
    table = Table(title="Watchlist", expand=True)
    table.add_column("Symbol")
    table.add_column("Price")
    table.add_column("Scenario")
    table.add_column("FTFC")
    table.add_column("Last event", overflow="fold")
    for sym, s in state.symbols.items():
        price = f"{s.last_price:.2f}" if s.last_price is not None else "-"
        table.add_row(sym, price, s.scenario, s.ftfc, s.last_event)
    return table


def _positions_table(state: SessionState) -> Table:
    table = Table(title="Open Positions", expand=True)
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Stop", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("PnL", justify="right")
    for p in state.positions:
        table.add_row(
            p.symbol,
            p.side,
            str(p.qty),
            f"{p.entry:.2f}",
            f"{p.stop:.2f}",
            f"{p.target:.2f}",
            f"{p.unrealized:+.2f}",
        )
    return table


def _events_panel(state: SessionState) -> Panel:
    body = "\n".join(state.events) if state.events else "(no events yet)"
    return Panel(body, title="Recent events", border_style="magenta")


def render(state: SessionState) -> Group:
    return Group(
        _header(state),
        _watchlist_table(state),
        _positions_table(state),
        _events_panel(state),
    )


async def run_tui(state: SessionState, stop_event: asyncio.Event) -> None:
    """Live-render the SessionState until stop_event is set."""
    console = Console()
    with Live(render(state), console=console, refresh_per_second=2, screen=False) as live:
        while not stop_event.is_set():
            live.update(render(state))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
