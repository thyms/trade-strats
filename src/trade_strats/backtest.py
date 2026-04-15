from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trade_strats.aggregation import TimedBar
from trade_strats.config import Config
from trade_strats.orchestrator import compute_atr14, pick_best_setup
from trade_strats.risk import AccountSnapshot, Rejection
from trade_strats.risk import evaluate as risk_evaluate
from trade_strats.strategy.ftfc import HigherTfOpens, allows, ftfc_state
from trade_strats.strategy.patterns import PatternKind, Side, detect

OpensProvider = Callable[[datetime], HigherTfOpens | None]


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    symbol: str
    side: Side
    kind: PatternKind
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    exit_ts: datetime
    exit_price: float
    exit_reason: str  # "stop" | "target" | "eod"
    realized_pnl: float
    r_multiple: float
    risk_per_share: float


@dataclass
class _PendingBracket:
    symbol: str
    side: Side
    kind: PatternKind
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    submitted_bar_ts: datetime


@dataclass
class _OpenPosition:
    symbol: str
    side: Side
    kind: PatternKind
    qty: int
    entry_ts: datetime
    entry_price: float
    stop_price: float
    target_price: float
    risk_per_share: float


def _empty_trades() -> list[SimulatedTrade]:
    return []


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[SimulatedTrade] = field(default_factory=_empty_trades)
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    total_pnl: float = 0.0
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    def summary(self) -> str:
        pf_str = "inf" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}"
        return (
            "Backtest result\n"
            f"  Trades:          {self.total_trades}\n"
            f"  Win rate:        {self.win_rate * 100:.1f}% ({self.win_count}W / {self.loss_count}L)\n"
            f"  Total P&L:       ${self.total_pnl:,.2f}\n"
            f"  Starting equity: ${self.starting_equity:,.2f}\n"
            f"  Ending equity:   ${self.ending_equity:,.2f}\n"
            f"  Avg win (R):     {self.avg_win_r:.2f}\n"
            f"  Avg loss (R):    {self.avg_loss_r:.2f}\n"
            f"  Profit factor:   {pf_str}\n"
            f"  Max drawdown:    ${self.max_drawdown:,.2f} ({self.max_drawdown_pct * 100:.1f}%)"
        )


def _pnl(pos: _OpenPosition, exit_price: float) -> float:
    if pos.side is Side.LONG:
        return (exit_price - pos.entry_price) * pos.qty
    return (pos.entry_price - exit_price) * pos.qty


def _check_exit(pos: _OpenPosition, bar: TimedBar) -> tuple[float, str] | None:
    """Detect exit fill on a bar. If both stop and target hit, assume stop fires first."""
    if pos.side is Side.LONG:
        if bar.low <= pos.stop_price:
            return pos.stop_price, "stop"
        if bar.high >= pos.target_price:
            return pos.target_price, "target"
    else:
        if bar.high >= pos.stop_price:
            return pos.stop_price, "stop"
        if bar.low <= pos.target_price:
            return pos.target_price, "target"
    return None


def _check_entry_fill(pb: _PendingBracket, bar: TimedBar) -> tuple[bool, float]:
    """Return (filled, fill_price) for the parent stop-limit against one bar."""
    if pb.side is Side.LONG:
        if bar.high >= pb.entry_price:
            # Gap-up: fill at open; otherwise at trigger
            return True, max(bar.open, pb.entry_price)
    elif bar.low <= pb.entry_price:
        # Gap-down short: fill at open; otherwise at trigger
        return True, min(bar.open, pb.entry_price)
    return False, 0.0


def _close_position_at(
    pos: _OpenPosition, exit_ts: datetime, exit_price: float, reason: str
) -> SimulatedTrade:
    pnl = _pnl(pos, exit_price)
    r = pnl / (pos.risk_per_share * pos.qty) if pos.risk_per_share > 0 and pos.qty > 0 else 0.0
    return SimulatedTrade(
        symbol=pos.symbol,
        side=pos.side,
        kind=pos.kind,
        entry_ts=pos.entry_ts,
        entry_price=pos.entry_price,
        stop_price=pos.stop_price,
        target_price=pos.target_price,
        qty=pos.qty,
        exit_ts=exit_ts,
        exit_price=exit_price,
        exit_reason=reason,
        realized_pnl=round(pnl, 2),
        r_multiple=round(r, 4),
        risk_per_share=pos.risk_per_share,
    )


def run_backtest(
    symbol: str,
    bars_15m: Sequence[TimedBar],
    opens_provider: OpensProvider,
    config: Config,
    starting_equity: float = 50_000.0,
) -> BacktestResult:
    """Replay 15m bars through the same strategy + risk logic as live, simulating fills.

    Fill simulation: parent orders have 1-bar TIF; exits check stop-then-target
    against each bar's high/low. At end-of-day, remaining positions close at the
    last bar's close with exit_reason='eod'.
    """
    equity = starting_equity
    realized_pnl_day = 0.0
    trades_today = 0
    current_date: str | None = None

    pending_brackets: list[_PendingBracket] = []
    open_positions: list[_OpenPosition] = []
    completed: list[SimulatedTrade] = []

    risk_cfg = config.risk_config()
    allowed_patterns = set(config.strategy.patterns)
    allowed_sides = set(config.strategy.sides)

    for i, bar in enumerate(bars_15m):
        bar_date = bar.ts.date().isoformat()
        if current_date != bar_date:
            # EOD: close anything still open at previous close.
            if open_positions and i > 0:
                prev_close = bars_15m[i - 1].close
                prev_ts = bars_15m[i - 1].ts
                for pos in open_positions:
                    trade = _close_position_at(pos, prev_ts, prev_close, "eod")
                    completed.append(trade)
                    equity += trade.realized_pnl
                open_positions = []
            pending_brackets = []
            realized_pnl_day = 0.0
            trades_today = 0
            current_date = bar_date

        still_open: list[_OpenPosition] = []
        for pos in open_positions:
            exit_info = _check_exit(pos, bar)
            if exit_info is None:
                still_open.append(pos)
                continue
            exit_price, exit_reason = exit_info
            trade = _close_position_at(pos, bar.ts, exit_price, exit_reason)
            completed.append(trade)
            equity += trade.realized_pnl
            realized_pnl_day += trade.realized_pnl
        open_positions = still_open

        expired: list[_PendingBracket] = []
        for pb in pending_brackets:
            if bar.ts <= pb.submitted_bar_ts:
                expired.append(pb)
                continue
            filled, fill_price = _check_entry_fill(pb, bar)
            if not filled:
                continue
            position = _OpenPosition(
                symbol=pb.symbol,
                side=pb.side,
                kind=pb.kind,
                qty=pb.qty,
                entry_ts=bar.ts,
                entry_price=fill_price,
                stop_price=pb.stop_price,
                target_price=pb.target_price,
                risk_per_share=abs(pb.entry_price - pb.stop_price),
            )
            trades_today += 1
            # Intra-bar: price continued through the bar's range after fill;
            # check if stop or target would have been hit in the same bar.
            same_bar_exit = _check_exit(position, bar)
            if same_bar_exit is not None:
                exit_price, exit_reason = same_bar_exit
                trade = _close_position_at(position, bar.ts, exit_price, exit_reason)
                completed.append(trade)
                equity += trade.realized_pnl
                realized_pnl_day += trade.realized_pnl
            else:
                open_positions.append(position)
        pending_brackets = [pb for pb in expired if bar.ts <= pb.submitted_bar_ts]

        window = list(bars_15m[: i + 1])
        if len(window) < 15:
            continue
        strat_bars = [b.to_strategy_bar() for b in window]
        setups = [
            s
            for s in detect(strat_bars)
            if s.kind.value in allowed_patterns and s.side.value in allowed_sides
        ]
        setup = pick_best_setup(setups)
        if setup is None:
            continue

        opens = opens_provider(bar.ts)
        if opens is None:
            continue
        state = ftfc_state(bar.close, opens)
        if not allows(setup.side, state):
            continue

        atr = compute_atr14(window)
        snapshot = AccountSnapshot(
            equity_usd=equity,
            realized_pnl_today=realized_pnl_day,
            open_positions=len(open_positions),
            trades_today=trades_today,
        )
        decision = risk_evaluate(setup, snapshot, risk_cfg, atr, bar.ts, config.blackouts)
        if isinstance(decision, Rejection):
            continue

        pending_brackets.append(
            _PendingBracket(
                symbol=symbol,
                side=decision.side,
                kind=decision.kind,
                entry_price=decision.entry_price,
                stop_price=decision.stop_price,
                target_price=decision.target_price,
                qty=decision.qty,
                submitted_bar_ts=bar.ts,
            )
        )

    if open_positions and bars_15m:
        last = bars_15m[-1]
        for pos in open_positions:
            trade = _close_position_at(pos, last.ts, last.close, "eod")
            completed.append(trade)
            equity += trade.realized_pnl

    return _compute_metrics(completed, starting_equity)


def _compute_metrics(trades: list[SimulatedTrade], starting_equity: float) -> BacktestResult:
    if not trades:
        return BacktestResult(
            starting_equity=starting_equity,
            ending_equity=starting_equity,
        )

    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl <= 0]
    gross_wins = sum(t.realized_pnl for t in wins)
    gross_losses = abs(sum(t.realized_pnl for t in losses))
    total_pnl = gross_wins - gross_losses
    ending_equity = starting_equity + total_pnl
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_win_r = sum(t.r_multiple for t in wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(t.r_multiple for t in losses) / len(losses) if losses else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        equity += t.realized_pnl
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    max_dd_pct = max_dd / peak if peak > 0 else 0.0

    return BacktestResult(
        trades=trades,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        total_pnl=round(total_pnl, 2),
        total_trades=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=win_rate,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        profit_factor=profit_factor,
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=max_dd_pct,
    )
