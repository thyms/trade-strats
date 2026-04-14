from datetime import UTC, datetime, timedelta

import pytest

from trade_strats.risk import (
    AccountSnapshot,
    Decision,
    Rejection,
    RejectReason,
    RiskConfig,
    TradePlan,
    evaluate,
)
from trade_strats.strategy.labeler import Bar
from trade_strats.strategy.patterns import PatternKind, Setup, Side

# --- Fixtures ---------------------------------------------------------------

SIGNAL_BAR_LONG = Bar(open=100.00, high=101.00, low=99.00, close=100.80)
SIGNAL_BAR_SHORT = Bar(open=100.00, high=101.00, low=99.00, close=99.20)

LONG_SETUP = Setup(
    kind=PatternKind.THREE_TWO_TWO,
    side=Side.LONG,
    signal_bar=SIGNAL_BAR_LONG,
    trigger_price=101.00,
    stop_price=99.00,
)
SHORT_SETUP = Setup(
    kind=PatternKind.THREE_TWO_TWO,
    side=Side.SHORT,
    signal_bar=SIGNAL_BAR_SHORT,
    trigger_price=99.00,
    stop_price=101.00,
)

DEFAULT_CONFIG = RiskConfig(
    risk_pct_per_trade=0.005,
    daily_loss_cap_pct=0.02,
    max_concurrent=3,
    max_trades_per_day=5,
)

HEALTHY_ACCOUNT = AccountSnapshot(
    equity_usd=50_000.0,
    realized_pnl_today=0.0,
    open_positions=0,
    trades_today=0,
)

NOW = datetime(2026, 4, 14, 14, 30, tzinfo=UTC)


def _plan(d: Decision) -> TradePlan:
    assert isinstance(d, TradePlan), f"expected TradePlan, got Rejection({d})"
    return d


def _reject(d: Decision) -> Rejection:
    assert isinstance(d, Rejection), f"expected Rejection, got TradePlan({d})"
    return d


# --- RiskConfig validation --------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_pct_per_trade", 0.0),
        ("risk_pct_per_trade", -0.01),
        ("risk_pct_per_trade", 1.5),
        ("daily_loss_cap_pct", 0.0),
        ("daily_loss_cap_pct", 1.1),
        ("max_concurrent", 0),
        ("max_trades_per_day", 0),
        ("min_rr", 0.5),
        ("min_bar_atr_mult", -0.1),
        ("tick_size", 0.0),
        ("blackout_window_minutes", -1),
    ],
)
def test_risk_config_validation_rejects_bad_values(field: str, value: float) -> None:
    kwargs: dict[str, float | int] = {
        "risk_pct_per_trade": 0.005,
        "daily_loss_cap_pct": 0.02,
        "max_concurrent": 3,
        "max_trades_per_day": 5,
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)  # type: ignore[arg-type]


# --- AccountSnapshot validation --------------------------------------------


def test_account_zero_equity_rejected() -> None:
    with pytest.raises(ValueError, match="equity"):
        AccountSnapshot(equity_usd=0.0, realized_pnl_today=0.0, open_positions=0, trades_today=0)


def test_account_negative_positions_rejected() -> None:
    with pytest.raises(ValueError, match="open_positions"):
        AccountSnapshot(
            equity_usd=1000.0, realized_pnl_today=0.0, open_positions=-1, trades_today=0
        )


def test_account_negative_trades_rejected() -> None:
    with pytest.raises(ValueError, match="trades_today"):
        AccountSnapshot(
            equity_usd=1000.0, realized_pnl_today=0.0, open_positions=0, trades_today=-1
        )


# --- Happy paths ------------------------------------------------------------


def test_long_setup_approved_with_expected_prices_and_qty() -> None:
    # signal.high = 101, trigger=101, stop (signal.low)=99. Tick=0.01.
    # entry = 101.01, stop = 98.99, risk_per_share = 2.02
    # budget = 50000 * 0.005 = 250. qty = floor(250 / 2.02) = 123.
    # target = 101.01 + 3 * 2.02 = 107.07
    plan = _plan(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert plan.entry_price == 101.01
    assert plan.stop_price == 98.99
    assert plan.risk_per_share == pytest.approx(2.02)
    assert plan.qty == 123
    assert plan.target_price == pytest.approx(107.07)
    assert plan.side is Side.LONG
    assert plan.kind is PatternKind.THREE_TWO_TWO
    assert plan.total_risk_usd == pytest.approx(123 * 2.02, abs=0.01)


def test_short_setup_approved_with_expected_prices() -> None:
    # trigger = 99 (signal.low), stop = 101 (signal.high). Tick = 0.01.
    # entry = 98.99, stop = 101.01, risk_per_share = 2.02
    # target = 98.99 - 3*2.02 = 92.93
    plan = _plan(evaluate(SHORT_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert plan.entry_price == 98.99
    assert plan.stop_price == 101.01
    assert plan.risk_per_share == pytest.approx(2.02)
    assert plan.target_price == pytest.approx(92.93)
    assert plan.side is Side.SHORT


# --- Portfolio gates --------------------------------------------------------


def test_rejects_when_daily_loss_cap_reached() -> None:
    account = AccountSnapshot(
        equity_usd=50_000.0,
        realized_pnl_today=-1000.0,  # exactly 2% of 50k
        open_positions=0,
        trades_today=0,
    )
    result = _reject(evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert result.reason is RejectReason.DAILY_LOSS_CAP


def test_approves_just_inside_daily_loss_cap() -> None:
    account = AccountSnapshot(
        equity_usd=50_000.0,
        realized_pnl_today=-999.99,  # just under 2% of 50k
        open_positions=0,
        trades_today=0,
    )
    result = evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=1.0, now=NOW)
    assert isinstance(result, TradePlan)


def test_rejects_when_max_concurrent_reached() -> None:
    account = AccountSnapshot(
        equity_usd=50_000.0, realized_pnl_today=0.0, open_positions=3, trades_today=0
    )
    result = _reject(evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert result.reason is RejectReason.MAX_CONCURRENT


def test_rejects_when_max_trades_per_day_reached() -> None:
    account = AccountSnapshot(
        equity_usd=50_000.0, realized_pnl_today=0.0, open_positions=0, trades_today=5
    )
    result = _reject(evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert result.reason is RejectReason.MAX_TRADES_PER_DAY


# --- Blackouts --------------------------------------------------------------


def test_rejects_when_inside_blackout_window() -> None:
    blackouts = [NOW + timedelta(minutes=10)]
    result = _reject(
        evaluate(
            LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW, blackouts=blackouts
        )
    )
    assert result.reason is RejectReason.BLACKOUT


def test_approves_just_outside_blackout_window() -> None:
    blackouts = [NOW + timedelta(minutes=31)]  # default window is 30
    result = evaluate(
        LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW, blackouts=blackouts
    )
    assert isinstance(result, TradePlan)


def test_approves_when_blackouts_empty() -> None:
    result = evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW)
    assert isinstance(result, TradePlan)


def test_blackout_window_disabled_at_zero_minutes() -> None:
    config = RiskConfig(
        risk_pct_per_trade=0.005,
        daily_loss_cap_pct=0.02,
        max_concurrent=3,
        max_trades_per_day=5,
        blackout_window_minutes=0,
    )
    blackouts = [NOW]  # same moment — would hit any nonzero window
    result = evaluate(LONG_SETUP, HEALTHY_ACCOUNT, config, atr14=1.0, now=NOW, blackouts=blackouts)
    assert isinstance(result, TradePlan)


# --- Bar-range / ATR filter ------------------------------------------------


def test_rejects_when_bar_range_below_atr_threshold() -> None:
    # bar range is 2.0 (high-low=101-99). threshold = 0.5 * atr.
    # Need threshold > 2.0 → atr > 4.0.
    result = _reject(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=5.0, now=NOW))
    assert result.reason is RejectReason.BAR_TOO_SMALL
    assert "range=" in result.detail


def test_approves_when_bar_range_at_or_above_threshold() -> None:
    # threshold = 0.5 * 4.0 = 2.0, bar range = 2.0. At threshold → approve.
    result = evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=4.0, now=NOW)
    assert isinstance(result, TradePlan)


def test_disables_atr_filter_when_mult_zero() -> None:
    config = RiskConfig(
        risk_pct_per_trade=0.005,
        daily_loss_cap_pct=0.02,
        max_concurrent=3,
        max_trades_per_day=5,
        min_bar_atr_mult=0.0,
    )
    # Even with huge ATR, threshold = 0 so filter passes.
    result = evaluate(LONG_SETUP, HEALTHY_ACCOUNT, config, atr14=999.0, now=NOW)
    assert isinstance(result, TradePlan)


def test_negative_atr_rejected() -> None:
    with pytest.raises(ValueError, match="atr14"):
        evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=-0.1, now=NOW)


# --- Zero qty ---------------------------------------------------------------


def test_rejects_zero_qty_when_budget_smaller_than_risk_per_share() -> None:
    # risk_per_share = 2.02, need budget < 2.02 → equity * 0.005 < 2.02 → equity < 404
    tiny = AccountSnapshot(
        equity_usd=100.0, realized_pnl_today=0.0, open_positions=0, trades_today=0
    )
    result = _reject(evaluate(LONG_SETUP, tiny, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert result.reason is RejectReason.ZERO_QTY


# --- Tick rounding ----------------------------------------------------------


def test_tick_adjusted_entry_long() -> None:
    plan = _plan(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    # entry = trigger_price + tick = 101.00 + 0.01 = 101.01
    assert plan.entry_price == 101.01


def test_tick_adjusted_stop_long() -> None:
    plan = _plan(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    # stop = stop_price - tick = 99.00 - 0.01 = 98.99
    assert plan.stop_price == 98.99


def test_tick_adjusted_entry_short() -> None:
    plan = _plan(evaluate(SHORT_SETUP, HEALTHY_ACCOUNT, DEFAULT_CONFIG, atr14=1.0, now=NOW))
    assert plan.entry_price == 98.99
    assert plan.stop_price == 101.01


def test_custom_tick_size() -> None:
    config = RiskConfig(
        risk_pct_per_trade=0.005,
        daily_loss_cap_pct=0.02,
        max_concurrent=3,
        max_trades_per_day=5,
        tick_size=0.05,  # sub-$1 stock tick
    )
    plan = _plan(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, config, atr14=1.0, now=NOW))
    assert plan.entry_price == 101.05
    assert plan.stop_price == 98.95


# --- min_rr configurable ---------------------------------------------------


def test_custom_min_rr_2r() -> None:
    config = RiskConfig(
        risk_pct_per_trade=0.005,
        daily_loss_cap_pct=0.02,
        max_concurrent=3,
        max_trades_per_day=5,
        min_rr=2.0,
    )
    plan = _plan(evaluate(LONG_SETUP, HEALTHY_ACCOUNT, config, atr14=1.0, now=NOW))
    # entry=101.01, risk=2.02, target = 101.01 + 2*2.02 = 105.05
    assert plan.target_price == pytest.approx(105.05)


# --- Gate precedence --------------------------------------------------------


def test_daily_loss_cap_checked_before_bar_filter() -> None:
    # Both conditions triggered; loss cap wins.
    account = AccountSnapshot(
        equity_usd=50_000.0,
        realized_pnl_today=-1_500.0,
        open_positions=0,
        trades_today=0,
    )
    result = _reject(evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=10.0, now=NOW))
    assert result.reason is RejectReason.DAILY_LOSS_CAP


def test_max_concurrent_checked_before_blackout() -> None:
    account = AccountSnapshot(
        equity_usd=50_000.0, realized_pnl_today=0.0, open_positions=3, trades_today=0
    )
    result = _reject(
        evaluate(LONG_SETUP, account, DEFAULT_CONFIG, atr14=1.0, now=NOW, blackouts=[NOW])
    )
    assert result.reason is RejectReason.MAX_CONCURRENT
