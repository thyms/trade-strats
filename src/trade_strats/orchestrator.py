from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trade_strats.aggregation import ET, TimedBar
from trade_strats.config import Config
from trade_strats.execution import Executor, SubmittedBracket
from trade_strats.journal import Journal, OrderRecord, TradeRecord
from trade_strats.market_data import MarketData
from trade_strats.risk import AccountSnapshot, Rejection, TradePlan
from trade_strats.risk import evaluate as risk_evaluate
from trade_strats.strategy.ftfc import FtfcState, HigherTfOpens, allows, ftfc_state
from trade_strats.strategy.patterns import PatternKind, Setup, Side, detect


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


# Keep unused imports referenced so pyright doesn't prune them for re-exporters.
__all__ = [
    "EvalOutcome",
    "EvaluationResult",
    "Side",
    "TradePlan",
    "compute_atr14",
    "evaluate_and_submit",
    "get_higher_opens",
    "pick_best_setup",
]
