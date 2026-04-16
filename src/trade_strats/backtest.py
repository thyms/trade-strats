import bisect
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
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
    signal_bars: Sequence[TimedBar],
    opens_provider: OpensProvider,
    config: Config,
    starting_equity: float = 50_000.0,
) -> BacktestResult:
    """Replay signal-TF bars through the same strategy + risk logic as live, simulating fills.

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

    for i, bar in enumerate(signal_bars):
        bar_date = bar.ts.date().isoformat()
        if current_date != bar_date:
            # EOD: close anything still open at previous close.
            if open_positions and i > 0:
                prev_close = signal_bars[i - 1].close
                prev_ts = signal_bars[i - 1].ts
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

        if i < 14:
            continue
        # detect() only needs last 4 bars; ATR needs last 15.
        recent = signal_bars[max(0, i - 14) : i + 1]
        strat_bars = [b.to_strategy_bar() for b in recent]
        all_setups = detect(strat_bars)
        # When rev-strat is excluded, also suppress 2-2 matches that overlap
        # with a rev-strat detection (same signal bar, same side).
        rev_strat_excluded = "rev-strat" not in allowed_patterns
        rev_strat_sides: set[Side] = (
            {s.side for s in all_setups if s.kind is PatternKind.REV_STRAT}
            if rev_strat_excluded
            else set()
        )
        setups = [
            s
            for s in all_setups
            if s.kind.value in allowed_patterns
            and s.side.value in allowed_sides
            and not (
                rev_strat_excluded and s.kind is PatternKind.TWO_TWO and s.side in rev_strat_sides
            )
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

        atr = compute_atr14(recent)
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

    if open_positions and signal_bars:
        last = signal_bars[-1]
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


# ---------------------------------------------------------------------------
# Higher-TF opens provider built from historical bar series
# ---------------------------------------------------------------------------


def build_opens_provider(
    daily_bars: Sequence[TimedBar],
    four_hour_bars: Sequence[TimedBar],
    one_hour_bars: Sequence[TimedBar],
) -> OpensProvider:
    """Build an OpensProvider that looks up each TF's active bucket open at a timestamp.

    For each query timestamp, returns the open of the most recent bar whose
    ts is <= target ts per timeframe. Bars must be sorted by ts.
    """
    d_ts = [b.ts for b in daily_bars]
    d_opens = [b.open for b in daily_bars]
    h4_ts = [b.ts for b in four_hour_bars]
    h4_opens = [b.open for b in four_hour_bars]
    h1_ts = [b.ts for b in one_hour_bars]
    h1_opens = [b.open for b in one_hour_bars]

    def _lookup(tsi: list[datetime], opens: list[float], ts: datetime) -> float | None:
        i = bisect.bisect_right(tsi, ts)
        if i == 0:
            return None
        return opens[i - 1]

    def provider(ts: datetime) -> HigherTfOpens | None:
        d = _lookup(d_ts, d_opens, ts)
        h4 = _lookup(h4_ts, h4_opens, ts)
        h1 = _lookup(h1_ts, h1_opens, ts)
        if d is None or h4 is None or h1 is None:
            return None
        return HigherTfOpens(daily=d, four_hour=h4, one_hour=h1)

    return provider


# ---------------------------------------------------------------------------
# Walk-forward harness: run multiple symbols, report pattern breakdowns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatternBreakdown:
    symbol: str
    pattern: str
    trade_count: int
    win_count: int
    win_rate: float
    total_pnl: float
    avg_r: float
    profit_factor: float


def _empty_breakdowns() -> list[PatternBreakdown]:
    return []


def _empty_results() -> dict[str, BacktestResult]:
    return {}


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    results: dict[str, BacktestResult] = field(default_factory=_empty_results)
    breakdowns: list[PatternBreakdown] = field(default_factory=_empty_breakdowns)
    total_pnl: float = 0.0
    total_trades: int = 0

    def summary(self) -> str:
        lines = [
            "Walk-forward report",
            f"  Symbols:      {len(self.results)}",
            f"  Total trades: {self.total_trades}",
            f"  Total P&L:    ${self.total_pnl:,.2f}",
            "",
            "Per-symbol:",
        ]
        for symbol, res in self.results.items():
            pf = "inf" if res.profit_factor == float("inf") else f"{res.profit_factor:.2f}"
            lines.append(
                f"  {symbol:<6}  trades={res.total_trades:<4}  "
                f"win%={res.win_rate * 100:>5.1f}  PnL=${res.total_pnl:>10,.2f}  PF={pf}"
            )
        if self.breakdowns:
            lines.extend(["", "Per-pattern breakdown:"])
            lines.append(
                f"  {'Symbol':<6} {'Pattern':<10} {'N':>4} {'Wins':>5} {'Win%':>6} "
                f"{'PnL':>12} {'AvgR':>6} {'PF':>6}"
            )
            for b in self.breakdowns:
                pf = "inf" if b.profit_factor == float("inf") else f"{b.profit_factor:.2f}"
                lines.append(
                    f"  {b.symbol:<6} {b.pattern:<10} {b.trade_count:>4} {b.win_count:>5} "
                    f"{b.win_rate * 100:>5.1f}% ${b.total_pnl:>10,.2f} "
                    f"{b.avg_r:>5.2f}R {pf:>6}"
                )
        return "\n".join(lines)


def pattern_breakdowns(
    trades_by_symbol: Mapping[str, Sequence[SimulatedTrade]],
) -> list[PatternBreakdown]:
    groups: dict[tuple[str, str], list[SimulatedTrade]] = defaultdict(list)
    for symbol, trades in trades_by_symbol.items():
        for t in trades:
            groups[(symbol, t.kind.value)].append(t)

    out: list[PatternBreakdown] = []
    for (symbol, pattern), ts in groups.items():
        wins = [t for t in ts if t.realized_pnl > 0]
        losses = [t for t in ts if t.realized_pnl <= 0]
        gw = sum(t.realized_pnl for t in wins)
        gl = abs(sum(t.realized_pnl for t in losses))
        pf = gw / gl if gl > 0 else float("inf")
        win_rate = len(wins) / len(ts) if ts else 0.0
        avg_r = sum(t.r_multiple for t in ts) / len(ts) if ts else 0.0
        total = sum(t.realized_pnl for t in ts)
        out.append(
            PatternBreakdown(
                symbol=symbol,
                pattern=pattern,
                trade_count=len(ts),
                win_count=len(wins),
                win_rate=win_rate,
                total_pnl=round(total, 2),
                avg_r=avg_r,
                profit_factor=pf,
            )
        )
    return sorted(out, key=lambda b: (b.symbol, b.pattern))


def run_walk_forward(
    bars_by_symbol: Mapping[str, Sequence[TimedBar]],
    opens_by_symbol: Mapping[str, OpensProvider],
    config: Config,
    starting_equity: float = 50_000.0,
) -> WalkForwardReport:
    """Run backtest on each symbol independently and aggregate.

    Each symbol starts from the same starting_equity (independent equity pools).
    For a combined-equity portfolio backtest, use a different harness.
    """
    results: dict[str, BacktestResult] = {}
    trades_by_symbol: dict[str, list[SimulatedTrade]] = {}
    for symbol, bars in bars_by_symbol.items():
        opens_provider = opens_by_symbol.get(symbol)
        if opens_provider is None:
            continue
        result = run_backtest(
            symbol=symbol,
            signal_bars=bars,
            opens_provider=opens_provider,
            config=config,
            starting_equity=starting_equity,
        )
        results[symbol] = result
        trades_by_symbol[symbol] = result.trades

    breakdowns = pattern_breakdowns(trades_by_symbol)
    total_pnl = sum(r.total_pnl for r in results.values())
    total_trades = sum(r.total_trades for r in results.values())
    return WalkForwardReport(
        results=results,
        breakdowns=breakdowns,
        total_pnl=round(total_pnl, 2),
        total_trades=total_trades,
    )
