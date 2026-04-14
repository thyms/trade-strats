from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from trade_strats.strategy.labeler import Bar
from trade_strats.strategy.patterns import PatternKind, Setup, Side


class RejectReason(StrEnum):
    DAILY_LOSS_CAP = "daily_loss_cap"
    MAX_CONCURRENT = "max_concurrent"
    MAX_TRADES_PER_DAY = "max_trades_per_day"
    BLACKOUT = "blackout"
    BAR_TOO_SMALL = "bar_too_small"
    ZERO_QTY = "zero_qty"
    DEGENERATE_RISK = "degenerate_risk"


@dataclass(frozen=True, slots=True)
class RiskConfig:
    risk_pct_per_trade: float
    daily_loss_cap_pct: float
    max_concurrent: int
    max_trades_per_day: int
    min_rr: float = 3.0
    min_bar_atr_mult: float = 0.5
    tick_size: float = 0.01
    blackout_window_minutes: int = 30

    def __post_init__(self) -> None:
        if not 0 < self.risk_pct_per_trade <= 1:
            raise ValueError(f"risk_pct_per_trade must be in (0, 1], got {self.risk_pct_per_trade}")
        if not 0 < self.daily_loss_cap_pct <= 1:
            raise ValueError(f"daily_loss_cap_pct must be in (0, 1], got {self.daily_loss_cap_pct}")
        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {self.max_concurrent}")
        if self.max_trades_per_day < 1:
            raise ValueError(f"max_trades_per_day must be >= 1, got {self.max_trades_per_day}")
        if self.min_rr < 1:
            raise ValueError(f"min_rr must be >= 1, got {self.min_rr}")
        if self.min_bar_atr_mult < 0:
            raise ValueError(f"min_bar_atr_mult must be >= 0, got {self.min_bar_atr_mult}")
        if self.tick_size <= 0:
            raise ValueError(f"tick_size must be > 0, got {self.tick_size}")
        if self.blackout_window_minutes < 0:
            raise ValueError(
                f"blackout_window_minutes must be >= 0, got {self.blackout_window_minutes}"
            )


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity_usd: float
    realized_pnl_today: float
    open_positions: int
    trades_today: int

    def __post_init__(self) -> None:
        if self.equity_usd <= 0:
            raise ValueError(f"equity_usd must be > 0, got {self.equity_usd}")
        if self.open_positions < 0:
            raise ValueError(f"open_positions cannot be negative, got {self.open_positions}")
        if self.trades_today < 0:
            raise ValueError(f"trades_today cannot be negative, got {self.trades_today}")


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TradePlan:
    kind: PatternKind
    side: Side
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    risk_per_share: float
    total_risk_usd: float
    signal_bar: Bar


Decision = TradePlan | Rejection


def _round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def _in_blackout(when: datetime, blackouts: Sequence[datetime], window_minutes: int) -> bool:
    if not blackouts or window_minutes == 0:
        return False
    seconds = window_minutes * 60
    return any(abs((when - b).total_seconds()) <= seconds for b in blackouts)


def evaluate(
    setup: Setup,
    account: AccountSnapshot,
    config: RiskConfig,
    atr14: float,
    now: datetime,
    blackouts: Sequence[datetime] = (),
) -> Decision:
    """Apply risk rules to a Setup. Returns a TradePlan if approved, else a Rejection.

    Gates applied in order: daily loss cap, max concurrent, max trades/day,
    blackout window, bar-range vs ATR filter, then sizing + tick-adjusted prices.
    """
    if atr14 < 0:
        raise ValueError(f"atr14 must be >= 0, got {atr14}")

    if account.realized_pnl_today <= -account.equity_usd * config.daily_loss_cap_pct:
        return Rejection(RejectReason.DAILY_LOSS_CAP)
    if account.open_positions >= config.max_concurrent:
        return Rejection(RejectReason.MAX_CONCURRENT)
    if account.trades_today >= config.max_trades_per_day:
        return Rejection(RejectReason.MAX_TRADES_PER_DAY)
    if _in_blackout(now, blackouts, config.blackout_window_minutes):
        return Rejection(RejectReason.BLACKOUT)

    bar_range = setup.signal_bar.high - setup.signal_bar.low
    threshold = config.min_bar_atr_mult * atr14
    if bar_range < threshold:
        return Rejection(
            RejectReason.BAR_TOO_SMALL,
            detail=f"range={bar_range:.4f} < threshold={threshold:.4f}",
        )

    tick = config.tick_size
    if setup.side is Side.LONG:
        entry = _round_to_tick(setup.trigger_price + tick, tick)
        stop = _round_to_tick(setup.stop_price - tick, tick)
        risk_per_share = entry - stop
    else:
        entry = _round_to_tick(setup.trigger_price - tick, tick)
        stop = _round_to_tick(setup.stop_price + tick, tick)
        risk_per_share = stop - entry

    if risk_per_share <= 0:
        return Rejection(RejectReason.DEGENERATE_RISK)

    if setup.side is Side.LONG:
        target = _round_to_tick(entry + config.min_rr * risk_per_share, tick)
    else:
        target = _round_to_tick(entry - config.min_rr * risk_per_share, tick)

    risk_budget = account.equity_usd * config.risk_pct_per_trade
    qty = int(risk_budget // risk_per_share)
    if qty <= 0:
        return Rejection(
            RejectReason.ZERO_QTY,
            detail=f"budget=${risk_budget:.2f} / risk_per_share=${risk_per_share:.4f}",
        )

    return TradePlan(
        kind=setup.kind,
        side=setup.side,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        qty=qty,
        risk_per_share=risk_per_share,
        total_risk_usd=round(qty * risk_per_share, 2),
        signal_bar=setup.signal_bar,
    )
