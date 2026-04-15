from datetime import datetime, timedelta

import pytest

from trade_strats.aggregation import ET, TimedBar
from trade_strats.backtest import (
    BacktestResult,
    SimulatedTrade,
    _compute_metrics,
    run_backtest,
)
from trade_strats.config import Config
from trade_strats.strategy.ftfc import HigherTfOpens
from trade_strats.strategy.patterns import PatternKind, Side

# --- Fixtures --------------------------------------------------------------


def _config() -> Config:
    return Config.model_validate(
        {
            "mode": "paper",
            "account": {
                "sim_equity_usd": 50000,
                "risk_pct_per_trade": 0.005,
                "daily_loss_cap_pct": 0.02,
                "max_concurrent": 3,
                "max_trades_per_day": 5,
            },
            "strategy": {
                "timeframe": "15Min",
                "patterns": ["3-2-2", "2-2", "3-1-2", "rev-strat"],
                "sides": ["long", "short"],
                "min_rr": 3.0,
                "min_bar_atr_mult": 0.0,
                "ftfc_timeframes": ["1D", "4H", "1H"],
            },
            "watchlist": ["SPY"],
            "session": {
                "entry_window_et": ["09:30", "15:45"],
                "force_flat_et": "15:55",
            },
            "blackouts": [],
            "paths": {
                "db": "data/trades.db",
                "events_log": "data/events.jsonl",
                "reports_dir": "reports",
            },
        }
    )


def _flat_bar(ts: datetime, price: float = 10.0) -> TimedBar:
    return TimedBar(
        ts=ts,
        open=price,
        high=price + 0.25,
        low=price - 0.25,
        close=price,
        volume=1000,
    )


def _bullish_sequence() -> list[TimedBar]:
    """15 filler bars + 3-2-2 bullish reversal at bars[15..18]."""
    start = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    filler = [_flat_bar(start + timedelta(minutes=15 * i)) for i in range(15)]
    base = start + timedelta(minutes=15 * 15)
    prev = TimedBar(ts=base, open=10.0, high=11.0, low=9.0, close=10.0, volume=1000)
    outside = TimedBar(
        ts=base + timedelta(minutes=15),
        open=10.5,
        high=12.0,
        low=8.0,
        close=9.5,
        volume=1000,
    )
    two_d = TimedBar(
        ts=base + timedelta(minutes=30),
        open=9.0,
        high=11.0,
        low=7.0,
        close=7.5,
        volume=1000,
    )
    two_u = TimedBar(
        ts=base + timedelta(minutes=45),
        open=8.0,
        high=12.5,
        low=7.0,
        close=12.0,
        volume=1000,
    )
    return [*filler, prev, outside, two_d, two_u]


def _opens_long_bias(_ts: datetime) -> HigherTfOpens:
    # All opens well below the pattern's close (12.0) — FULL_GREEN, allows longs
    return HigherTfOpens(daily=9.0, four_hour=9.5, one_hour=10.0)


# --- Metrics ---------------------------------------------------------------


def _fake_trade(
    pnl: float, r: float, symbol: str = "SPY", minute_offset: int = 0
) -> SimulatedTrade:
    entry_ts = datetime(2026, 4, 15, 10, 0, tzinfo=ET) + timedelta(minutes=minute_offset)
    exit_ts = entry_ts + timedelta(minutes=30)
    return SimulatedTrade(
        symbol=symbol,
        side=Side.LONG,
        kind=PatternKind.THREE_TWO_TWO,
        entry_ts=entry_ts,
        entry_price=100.0,
        stop_price=99.0,
        target_price=103.0,
        qty=100,
        exit_ts=exit_ts,
        exit_price=100.0 + pnl / 100,
        exit_reason="target" if pnl > 0 else "stop",
        realized_pnl=pnl,
        r_multiple=r,
        risk_per_share=1.0,
    )


def test_compute_metrics_no_trades() -> None:
    result = _compute_metrics([], starting_equity=50_000.0)
    assert result.total_trades == 0
    assert result.ending_equity == 50_000.0
    assert result.win_rate == 0.0


def test_compute_metrics_all_winners() -> None:
    trades = [_fake_trade(300, 3.0), _fake_trade(300, 3.0, minute_offset=60)]
    result = _compute_metrics(trades, starting_equity=50_000.0)
    assert result.win_rate == 1.0
    assert result.win_count == 2
    assert result.loss_count == 0
    assert result.total_pnl == 600.0
    assert result.profit_factor == float("inf")
    assert result.max_drawdown == 0.0


def test_compute_metrics_mixed_pnl_and_drawdown() -> None:
    trades = [
        _fake_trade(300, 3.0, minute_offset=0),
        _fake_trade(-100, -1.0, minute_offset=60),
        _fake_trade(-100, -1.0, minute_offset=120),
        _fake_trade(300, 3.0, minute_offset=180),
    ]
    result = _compute_metrics(trades, starting_equity=50_000.0)
    assert result.total_trades == 4
    assert result.win_count == 2
    assert result.loss_count == 2
    assert result.win_rate == 0.5
    assert result.total_pnl == 400.0
    # gross_wins=600, gross_losses=200 → PF=3.0
    assert result.profit_factor == 3.0
    assert result.avg_win_r == 3.0
    assert result.avg_loss_r == -1.0
    # Equity curve: 50000 -> 50300 -> 50200 -> 50100 -> 50400
    # Peak 50300 at trade 1, trough 50100 at trade 3; drawdown 200
    assert result.max_drawdown == 200.0


# --- Fill simulation -------------------------------------------------------


def test_target_hit_closes_trade_at_target() -> None:
    """Bullish 3-2-2 setup; subsequent bar immediately hits the target."""
    bars = _bullish_sequence()
    # Add a bar that gaps through target (entry ≈ 12.51, target ≈ 12.51 + 3 * (12.51 - 6.99) = 29.07)
    # Simpler: add a bar with a high above the target
    last_ts = bars[-1].ts
    trigger_bar = TimedBar(
        ts=last_ts + timedelta(minutes=15),
        open=13.0,
        high=30.0,
        low=12.50,
        close=29.0,
        volume=1000,
    )
    bars_plus = [*bars, trigger_bar]
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars_plus,
        opens_provider=_opens_long_bias,
        config=_config(),
        starting_equity=50_000.0,
    )
    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.side is Side.LONG
    assert trade.exit_reason == "target"
    assert trade.realized_pnl > 0


def test_stop_hit_closes_trade_at_stop() -> None:
    bars = _bullish_sequence()
    last_ts = bars[-1].ts
    # Bar that triggers entry (high >= entry 12.51) then drops through stop 6.98
    trigger_bar = TimedBar(
        ts=last_ts + timedelta(minutes=15),
        open=12.60,
        high=13.0,
        low=6.00,
        close=6.50,
        volume=1000,
    )
    bars_plus = [*bars, trigger_bar]
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars_plus,
        opens_provider=_opens_long_bias,
        config=_config(),
        starting_equity=50_000.0,
    )
    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.realized_pnl < 0


def test_entry_tif_expires_if_trigger_not_reached() -> None:
    bars = _bullish_sequence()
    last_ts = bars[-1].ts
    # Next bar below entry (never triggers)
    no_trigger = TimedBar(
        ts=last_ts + timedelta(minutes=15),
        open=11.0,
        high=11.5,
        low=10.5,
        close=11.2,
        volume=1000,
    )
    bars_plus = [*bars, no_trigger]
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars_plus,
        opens_provider=_opens_long_bias,
        config=_config(),
        starting_equity=50_000.0,
    )
    # Parent not filled → no trade
    assert result.total_trades == 0


def test_ftfc_mismatch_blocks_entry() -> None:
    bars = _bullish_sequence()
    # Provide opens ABOVE price → FULL_RED → blocks long
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars,
        opens_provider=lambda _ts: HigherTfOpens(daily=20.0, four_hour=18.0, one_hour=15.0),
        config=_config(),
        starting_equity=50_000.0,
    )
    assert result.total_trades == 0


def test_no_opens_blocks_entry() -> None:
    bars = _bullish_sequence()
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars,
        opens_provider=lambda _ts: None,
        config=_config(),
        starting_equity=50_000.0,
    )
    assert result.total_trades == 0


def test_eod_flatten_unclosed_position() -> None:
    """Entry fills but neither stop nor target hits → force-close at session last bar close."""
    bars = _bullish_sequence()
    last_ts = bars[-1].ts
    # Trigger entry, but don't hit target or stop
    trigger = TimedBar(
        ts=last_ts + timedelta(minutes=15),
        open=12.6,
        high=13.0,
        low=12.0,
        close=12.8,
        volume=1000,
    )
    # More bars that also don't reach target (high of ~13) or stop (low of ~6.99)
    more = [
        TimedBar(
            ts=last_ts + timedelta(minutes=30 + 15 * i),
            open=12.8,
            high=13.2,
            low=11.0,
            close=12.9,
            volume=1000,
        )
        for i in range(3)
    ]
    bars_plus = [*bars, trigger, *more]
    result = run_backtest(
        symbol="SPY",
        bars_15m=bars_plus,
        opens_provider=_opens_long_bias,
        config=_config(),
        starting_equity=50_000.0,
    )
    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason == "eod"


# --- Summary helpers -------------------------------------------------------


def test_summary_renders_for_empty_result() -> None:
    result = _compute_metrics([], starting_equity=50_000.0)
    text = result.summary()
    assert "Trades:          0" in text
    assert "$50,000.00" in text


def test_summary_renders_profit_factor_inf() -> None:
    trades = [_fake_trade(100, 1.0)]
    result = _compute_metrics(trades, starting_equity=50_000.0)
    text = result.summary()
    assert "Profit factor:   inf" in text


def test_summary_formatting_normal_case() -> None:
    trades = [_fake_trade(300, 3.0), _fake_trade(-100, -1.0, minute_offset=30)]
    result = _compute_metrics(trades, starting_equity=50_000.0)
    text = result.summary()
    assert "Total P&L:       $200.00" in text
    assert "Win rate:        50.0%" in text


# --- BacktestResult default shape -----------------------------------------


def test_backtest_result_defaults() -> None:
    r = BacktestResult(starting_equity=1000.0, ending_equity=1000.0)
    assert r.total_trades == 0
    assert r.trades == []


# pytest use for parametrize — keeps import referenced
_ = pytest
