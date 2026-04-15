import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from trade_strats.aggregation import ET, TimedBar
from trade_strats.config import Config
from trade_strats.execution import Executor, SubmittedBracket
from trade_strats.journal import Journal, OrderRecord, TradeRecord
from trade_strats.market_data import AlpacaSettings, MarketData
from trade_strats.reconcile import format_report, reconcile
from trade_strats.risk import AccountSnapshot, Rejection, TradePlan
from trade_strats.risk import evaluate as risk_evaluate
from trade_strats.strategy.ftfc import FtfcState, HigherTfOpens, allows, ftfc_state
from trade_strats.strategy.patterns import PatternKind, Setup, Side, detect
from trade_strats.trade_updates import TradeUpdateHandler
from trade_strats.tui import SessionState, run_tui


class EvalOutcome(StrEnum):
    NO_SETUP = "no_setup"
    NOT_ENOUGH_BARS = "not_enough_bars"
    PATTERN_FILTERED = "pattern_filtered"
    FTFC_MISSING = "ftfc_missing"
    FTFC_MISMATCH = "ftfc_mismatch"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    outcome: EvalOutcome
    symbol: str
    setup: Setup | None = None
    ftfc_state: FtfcState | None = None
    rejection_reason: str | None = None
    trade_id: int | None = None
    bracket: SubmittedBracket | None = None


_PATTERN_PRIORITY: dict[PatternKind, int] = {
    PatternKind.THREE_ONE_TWO: 0,
    PatternKind.THREE_TWO_TWO: 1,
    PatternKind.REV_STRAT: 2,
    PatternKind.TWO_TWO: 3,
}

_MIN_BARS_FOR_ATR = 15


def compute_atr14(bars: Sequence[TimedBar]) -> float:
    """Average True Range over the last 14 bars. Returns 0 if insufficient data."""
    if len(bars) < _MIN_BARS_FOR_ATR:
        return 0.0
    trs: list[float] = []
    for i in range(len(bars) - 14, len(bars)):
        prev = bars[i - 1]
        curr = bars[i]
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


def pick_best_setup(setups: Sequence[Setup]) -> Setup | None:
    if not setups:
        return None
    return min(setups, key=lambda s: _PATTERN_PRIORITY[s.kind])


def get_higher_opens(md: MarketData, symbol: str) -> HigherTfOpens | None:
    daily = md.current_open(symbol, "1D")
    four_hour = md.current_open(symbol, "4H")
    one_hour = md.current_open(symbol, "1H")
    if daily is None or four_hour is None or one_hour is None:
        return None
    return HigherTfOpens(daily=daily, four_hour=four_hour, one_hour=one_hour)


def _filter_by_config(setups: Sequence[Setup], config: Config) -> list[Setup]:
    allowed_patterns = set(config.strategy.patterns)
    allowed_sides = set(config.strategy.sides)
    return [s for s in setups if s.kind.value in allowed_patterns and s.side.value in allowed_sides]


async def _persist_bracket(
    journal: Journal,
    symbol: str,
    plan: TradePlan,
    bracket: SubmittedBracket,
    state: FtfcState,
    mode: str,
) -> int:
    entry_ts = bracket.submitted_at.isoformat()
    trade_id = await journal.insert_trade(
        TradeRecord(
            symbol=symbol,
            side=plan.side.value,
            pattern=plan.kind.value,
            entry_ts=entry_ts,
            entry_price=plan.entry_price,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
            qty=plan.qty,
            mode=mode,
            ftfc_1d=state.value,
            ftfc_4h=state.value,
            ftfc_1h=state.value,
        )
    )
    await journal.insert_order(
        OrderRecord(
            alpaca_order_id=bracket.parent_order_id,
            trade_id=trade_id,
            symbol=symbol,
            side=plan.side.value,
            kind="entry",
            type="stop_limit",
            qty=plan.qty,
            stop_price=plan.entry_price,
            status="accepted",
            submitted_ts=entry_ts,
        )
    )
    if bracket.stop_loss_order_id is not None:
        await journal.insert_order(
            OrderRecord(
                alpaca_order_id=bracket.stop_loss_order_id,
                trade_id=trade_id,
                symbol=symbol,
                side=plan.side.value,
                kind="stop",
                type="stop",
                qty=plan.qty,
                stop_price=plan.stop_price,
                status="accepted",
                submitted_ts=entry_ts,
            )
        )
    if bracket.take_profit_order_id is not None:
        await journal.insert_order(
            OrderRecord(
                alpaca_order_id=bracket.take_profit_order_id,
                trade_id=trade_id,
                symbol=symbol,
                side=plan.side.value,
                kind="target",
                type="limit",
                qty=plan.qty,
                limit_price=plan.target_price,
                status="accepted",
                submitted_ts=entry_ts,
            )
        )
    return trade_id


async def evaluate_and_submit(
    symbol: str,
    bars_15m: Sequence[TimedBar],
    opens: HigherTfOpens | None,
    executor: Executor,
    journal: Journal,
    config: Config,
    now: datetime,
) -> EvaluationResult:
    """Run the full decision chain on a just-closed 15m bar and submit if approved."""
    if len(bars_15m) < _MIN_BARS_FOR_ATR:
        return EvaluationResult(outcome=EvalOutcome.NOT_ENOUGH_BARS, symbol=symbol)

    strat_bars = [b.to_strategy_bar() for b in bars_15m]
    all_setups = detect(strat_bars)
    if not all_setups:
        return EvaluationResult(outcome=EvalOutcome.NO_SETUP, symbol=symbol)

    filtered = _filter_by_config(all_setups, config)
    if not filtered:
        return EvaluationResult(outcome=EvalOutcome.PATTERN_FILTERED, symbol=symbol)

    setup = pick_best_setup(filtered)
    assert setup is not None

    if opens is None:
        return EvaluationResult(outcome=EvalOutcome.FTFC_MISSING, symbol=symbol, setup=setup)

    last_price = bars_15m[-1].close
    state = ftfc_state(last_price, opens)
    if not allows(setup.side, state):
        return EvaluationResult(
            outcome=EvalOutcome.FTFC_MISMATCH,
            symbol=symbol,
            setup=setup,
            ftfc_state=state,
        )

    atr = compute_atr14(bars_15m)
    account = await executor.get_account()
    session_date = now.astimezone(ET).date().isoformat()
    session = await journal.get_session(session_date)
    trades_today = session.trades_count if session is not None else 0
    realized_pnl_today = session.realized_pnl if session is not None else 0.0
    open_positions = len(await executor.get_positions())

    snapshot = AccountSnapshot(
        equity_usd=account.equity,
        realized_pnl_today=realized_pnl_today,
        open_positions=open_positions,
        trades_today=trades_today,
    )
    decision = risk_evaluate(
        setup=setup,
        account=snapshot,
        config=config.risk_config(),
        atr14=atr,
        now=now,
        blackouts=config.blackouts,
    )

    if isinstance(decision, Rejection):
        await journal.record_event(
            "skip",
            symbol=symbol,
            phase="risk",
            reason=decision.reason.value,
            detail=decision.detail,
            pattern=setup.kind.value,
            side=setup.side.value,
        )
        return EvaluationResult(
            outcome=EvalOutcome.RISK_REJECTED,
            symbol=symbol,
            setup=setup,
            ftfc_state=state,
            rejection_reason=decision.reason.value,
        )

    bracket = await executor.submit_bracket(symbol, decision)
    trade_id = await _persist_bracket(
        journal=journal,
        symbol=symbol,
        plan=decision,
        bracket=bracket,
        state=state,
        mode=config.mode,
    )
    await journal.record_event(
        "entry_submitted",
        symbol=symbol,
        trade_id=trade_id,
        pattern=decision.kind.value,
        side=decision.side.value,
        entry=decision.entry_price,
        stop=decision.stop_price,
        target=decision.target_price,
        qty=decision.qty,
        ftfc=state.value,
    )
    return EvaluationResult(
        outcome=EvalOutcome.SUBMITTED,
        symbol=symbol,
        setup=setup,
        ftfc_state=state,
        trade_id=trade_id,
        bracket=bracket,
    )


# ---------------------------------------------------------------------------
# Session runner (the top-level entry point)
# ---------------------------------------------------------------------------


async def _wait_until_et(target_time: str, session_date: str) -> None:
    """Sleep until HH:MM ET on the given session_date (ISO YYYY-MM-DD)."""
    hh, mm = (int(x) for x in target_time.split(":"))
    y, m, d = (int(x) for x in session_date.split("-"))
    target = datetime(y, m, d, hh, mm, tzinfo=ET)
    delta = (target - datetime.now(ET)).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)


async def force_flat_at_close(
    executor: Executor,
    journal: Journal,
    config: Config,
    session_date: str,
) -> None:
    await _wait_until_et(config.session.force_flat_et, session_date)
    await journal.record_event("force_flat", reason="session_close")
    await executor.flat_all()
    account = await executor.get_account()
    await journal.finalize_session(session_date, end_equity=account.equity)


async def run_session(config: Config, schema_path: Path) -> None:
    """Top-level entry point: reconcile, start session, run streams until EOD or cancelled."""
    settings = AlpacaSettings.from_env()
    md = MarketData(settings)
    executor = Executor(settings)

    journal = await Journal.open(
        db_path=config.paths.db,
        events_path=config.paths.events_log,
        schema_path=schema_path,
    )
    async with journal:
        report = await reconcile(executor, journal)
        if not report.clean:
            raise RuntimeError(
                f"startup drift detected; refusing to start.\n{format_report(report)}"
            )

        account = await executor.get_account()
        session_date = datetime.now(ET).date().isoformat()
        await journal.upsert_session_start(session_date, account.equity)
        await journal.record_event("session_start", equity=account.equity, date=session_date)

        state = SessionState(
            mode=config.mode,
            equity=account.equity,
            max_trades=config.account.max_trades_per_day,
            loss_cap_usd=account.equity * config.account.daily_loss_cap_pct,
            session_status="running",
        )
        for symbol in config.watchlist:
            state.upsert_symbol(symbol)

        buffers: dict[tuple[str, str], deque[TimedBar]] = {}

        def _buf(sym: str, tf: str) -> deque[TimedBar]:
            key = (sym, tf)
            if key not in buffers:
                buffers[key] = deque(maxlen=50)
            return buffers[key]

        async def on_bar_closed(symbol: str, tf: str, bar: TimedBar) -> None:
            _buf(symbol, tf).append(bar)
            sym_state = state.upsert_symbol(symbol)
            sym_state.last_price = bar.close
            if tf != "15Min":
                return
            opens = get_higher_opens(md, symbol)
            sym_state.ftfc = ftfc_state(bar.close, opens).value if opens else "-"
            result = await evaluate_and_submit(
                symbol=symbol,
                bars_15m=list(_buf(symbol, "15Min")),
                opens=opens,
                executor=executor,
                journal=journal,
                config=config,
                now=datetime.now(UTC),
            )
            sym_state.last_event = result.outcome.value
            if result.setup is not None:
                sym_state.scenario = result.setup.kind.value
            state.push_event(f"{symbol} {tf} -> {result.outcome.value}")
            await journal.record_event(
                "evaluate",
                symbol=symbol,
                outcome=result.outcome.value,
                trade_id=result.trade_id,
                pattern=result.setup.kind.value if result.setup else None,
            )

        md.set_bar_handler(on_bar_closed)

        updates = TradeUpdateHandler(settings, journal)
        stop_event = asyncio.Event()

        bar_task = asyncio.create_task(md.run(config.watchlist), name="bars")
        updates_task = asyncio.create_task(updates.run(), name="trade_updates")
        flat_task = asyncio.create_task(
            force_flat_at_close(executor, journal, config, session_date),
            name="force_flat",
        )
        tui_task = asyncio.create_task(run_tui(state, stop_event), name="tui")
        tasks = {bar_task, updates_task, flat_task, tui_task}

        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            state.session_status = "stopping"
            stop_event.set()
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await journal.record_event("session_end", date=session_date)


__all__ = [
    "EvalOutcome",
    "EvaluationResult",
    "Side",
    "TradePlan",
    "compute_atr14",
    "evaluate_and_submit",
    "force_flat_at_close",
    "get_higher_opens",
    "pick_best_setup",
    "run_session",
]


# Unused import guard for pyright (timedelta referenced only via re-export)
_ = timedelta
