from datetime import datetime, timedelta

from trade_strats.aggregation import ET, TimedBar
from trade_strats.backtest import (
    PatternBreakdown,
    SimulatedTrade,
    build_opens_provider,
    pattern_breakdowns,
    run_walk_forward,
)
from trade_strats.config import Config
from trade_strats.strategy.ftfc import HigherTfOpens
from trade_strats.strategy.patterns import PatternKind, Side


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
            "watchlist": ["SPY", "QQQ"],
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


# --- build_opens_provider --------------------------------------------------


def _bar(ts: datetime, open_: float) -> TimedBar:
    return TimedBar(ts=ts, open=open_, high=open_ + 0.5, low=open_ - 0.5, close=open_, volume=100)


def test_opens_provider_returns_none_before_first_bar() -> None:
    ts0 = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    daily = [_bar(ts0, 100.0)]
    hourly = [_bar(ts0, 100.0)]
    provider = build_opens_provider(daily, hourly, hourly)
    # Query 1 hour BEFORE the first bar
    q = ts0 - timedelta(hours=1)
    assert provider(q) is None


def test_opens_provider_returns_active_bucket_open() -> None:
    t9 = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    t13 = datetime(2026, 4, 15, 13, 30, tzinfo=ET)
    daily = [_bar(t9, 100.0)]
    four_hour = [_bar(t9, 100.0), _bar(t13, 105.0)]
    one_hour = [_bar(t9, 100.0), _bar(t9 + timedelta(hours=4), 104.0)]
    provider = build_opens_provider(daily, four_hour, one_hour)

    # At 14:00 ET, the active 4H bucket is 13:30 with open=105
    q = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    opens = provider(q)
    assert opens is not None
    assert opens.four_hour == 105.0
    assert opens.daily == 100.0
    assert opens.one_hour == 104.0


def test_opens_provider_returns_none_if_any_tf_missing() -> None:
    t9 = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    daily = [_bar(t9, 100.0)]
    # No 4H bars at all
    provider = build_opens_provider(daily, [], [_bar(t9, 100.0)])
    q = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    assert provider(q) is None


# --- pattern_breakdowns ----------------------------------------------------


def _trade(
    symbol: str,
    kind: PatternKind,
    pnl: float,
    r: float,
    side: Side = Side.LONG,
    offset_min: int = 0,
) -> SimulatedTrade:
    entry = datetime(2026, 4, 15, 10, 0, tzinfo=ET) + timedelta(minutes=offset_min)
    return SimulatedTrade(
        symbol=symbol,
        side=side,
        kind=kind,
        entry_ts=entry,
        entry_price=100.0,
        stop_price=99.0,
        target_price=103.0,
        qty=100,
        exit_ts=entry + timedelta(minutes=30),
        exit_price=100.0 + pnl / 100,
        exit_reason="target" if pnl > 0 else "stop",
        realized_pnl=pnl,
        r_multiple=r,
        risk_per_share=1.0,
    )


def test_breakdowns_group_by_symbol_and_pattern() -> None:
    trades = {
        "SPY": [
            _trade("SPY", PatternKind.THREE_TWO_TWO, 300, 3.0, offset_min=0),
            _trade("SPY", PatternKind.THREE_TWO_TWO, -100, -1.0, offset_min=15),
            _trade("SPY", PatternKind.TWO_TWO, 300, 3.0, offset_min=30),
        ],
        "QQQ": [
            _trade("QQQ", PatternKind.THREE_TWO_TWO, 300, 3.0, offset_min=0),
        ],
    }
    breakdowns = pattern_breakdowns(trades)
    assert len(breakdowns) == 3

    by_key = {(b.symbol, b.pattern): b for b in breakdowns}
    spy_322 = by_key[("SPY", "3-2-2")]
    assert spy_322.trade_count == 2
    assert spy_322.win_count == 1
    assert spy_322.win_rate == 0.5
    assert spy_322.total_pnl == 200.0
    # gross_wins=300 / gross_losses=100 = 3.0
    assert spy_322.profit_factor == 3.0

    spy_22 = by_key[("SPY", "2-2")]
    assert spy_22.trade_count == 1
    assert spy_22.profit_factor == float("inf")

    qqq_322 = by_key[("QQQ", "3-2-2")]
    assert qqq_322.trade_count == 1


def test_breakdowns_sorted_by_symbol_then_pattern() -> None:
    trades = {
        "QQQ": [_trade("QQQ", PatternKind.TWO_TWO, 100, 1.0)],
        "SPY": [_trade("SPY", PatternKind.THREE_TWO_TWO, 100, 1.0)],
    }
    breakdowns = pattern_breakdowns(trades)
    assert breakdowns[0].symbol == "QQQ"
    assert breakdowns[1].symbol == "SPY"


def test_breakdowns_empty_when_no_trades() -> None:
    assert pattern_breakdowns({}) == []
    assert pattern_breakdowns({"SPY": []}) == []


# --- run_walk_forward ------------------------------------------------------


def _bullish_bars(start_price: float = 10.0) -> list[TimedBar]:
    """15 fillers + 4-bar 3-2-2 bullish pattern."""
    start = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    filler = [
        TimedBar(
            ts=start + timedelta(minutes=15 * i),
            open=start_price,
            high=start_price + 0.25,
            low=start_price - 0.25,
            close=start_price,
            volume=1000,
        )
        for i in range(15)
    ]
    base = start + timedelta(minutes=15 * 15)
    p = start_price
    prev = TimedBar(ts=base, open=p, high=p + 1, low=p - 1, close=p, volume=1000)
    outside = TimedBar(
        ts=base + timedelta(minutes=15),
        open=p + 0.5,
        high=p + 2,
        low=p - 2,
        close=p - 0.5,
        volume=1000,
    )
    two_d = TimedBar(
        ts=base + timedelta(minutes=30),
        open=p - 1,
        high=p + 1,
        low=p - 3,
        close=p - 2.5,
        volume=1000,
    )
    two_u = TimedBar(
        ts=base + timedelta(minutes=45),
        open=p - 2,
        high=p + 2.5,
        low=p - 3,
        close=p + 2,
        volume=1000,
    )
    # Bar after signal: hit target
    trigger = TimedBar(
        ts=base + timedelta(minutes=60),
        open=p + 2.5,
        high=p + 30,
        low=p + 2,
        close=p + 25,
        volume=1000,
    )
    return [*filler, prev, outside, two_d, two_u, trigger]


def _long_bias_opens(_ts: datetime) -> HigherTfOpens:
    return HigherTfOpens(daily=5.0, four_hour=5.5, one_hour=6.0)


def test_walk_forward_runs_each_symbol_and_aggregates() -> None:
    bars_by_symbol = {
        "SPY": _bullish_bars(10.0),
        "QQQ": _bullish_bars(20.0),
    }
    opens_by_symbol = {
        "SPY": _long_bias_opens,
        "QQQ": _long_bias_opens,
    }
    report = run_walk_forward(bars_by_symbol, opens_by_symbol, _config())
    assert "SPY" in report.results
    assert "QQQ" in report.results
    assert report.results["SPY"].total_trades == 1
    assert report.results["QQQ"].total_trades == 1
    assert report.total_trades == 2
    assert len(report.breakdowns) == 2
    assert report.summary().startswith("Walk-forward report")


def test_walk_forward_skips_symbols_without_opens() -> None:
    bars_by_symbol = {"SPY": _bullish_bars(10.0), "QQQ": _bullish_bars(20.0)}
    opens_by_symbol = {"SPY": _long_bias_opens}
    report = run_walk_forward(bars_by_symbol, opens_by_symbol, _config())
    assert "SPY" in report.results
    assert "QQQ" not in report.results


def test_walk_forward_report_summary_includes_pattern_table() -> None:
    bars_by_symbol = {"SPY": _bullish_bars(10.0)}
    opens_by_symbol = {"SPY": _long_bias_opens}
    report = run_walk_forward(bars_by_symbol, opens_by_symbol, _config())
    text = report.summary()
    assert "Per-symbol:" in text
    assert "Per-pattern breakdown:" in text


# Keep unused PatternBreakdown import referenced
_ = PatternBreakdown
