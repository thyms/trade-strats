import json
from datetime import datetime, timedelta
from pathlib import Path

from trade_strats.aggregation import ET
from trade_strats.backtest import (
    BacktestResult,
    PatternBreakdown,
    SimulatedTrade,
    WalkForwardReport,
)
from trade_strats.config import Config
from trade_strats.reports import (
    backtest_to_dict,
    save_backtest,
    save_walk_forward,
    walk_forward_to_dict,
)
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
                "min_bar_atr_mult": 0.5,
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


def _trade(
    pnl: float, r: float, *, kind: PatternKind = PatternKind.THREE_TWO_TWO
) -> SimulatedTrade:
    entry = datetime(2026, 3, 5, 10, 0, tzinfo=ET)
    return SimulatedTrade(
        symbol="SPY",
        side=Side.LONG,
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


def _sample_backtest_result() -> BacktestResult:
    trades = [_trade(300, 3.0), _trade(-100, -1.0)]
    return BacktestResult(
        trades=trades,
        starting_equity=50_000.0,
        ending_equity=50_200.0,
        total_pnl=200.0,
        total_trades=2,
        win_count=1,
        loss_count=1,
        win_rate=0.5,
        avg_win_r=3.0,
        avg_loss_r=-1.0,
        profit_factor=3.0,
        max_drawdown=100.0,
        max_drawdown_pct=0.002,
    )


# --- Serialization ---------------------------------------------------------


def test_backtest_to_dict_includes_core_fields() -> None:
    result = _sample_backtest_result()
    data = backtest_to_dict(result, "SPY", "2026-03-01", "2026-04-01", _config())
    assert data["kind"] == "backtest"
    assert data["symbol"] == "SPY"
    assert data["range"] == {"start": "2026-03-01", "end": "2026-04-01"}
    assert data["config"]["mode"] == "paper"
    assert data["config"]["min_rr"] == 3.0
    assert data["result"]["total_pnl"] == 200.0
    assert len(data["result"]["trades"]) == 2
    trade = data["result"]["trades"][0]
    assert trade["symbol"] == "SPY"
    assert trade["side"] == "long"
    assert trade["kind"] == "3-2-2"
    assert "entry_ts" in trade


def test_backtest_to_dict_serializes_inf_profit_factor() -> None:
    result = BacktestResult(
        trades=[_trade(300, 3.0)],
        starting_equity=50_000.0,
        ending_equity=50_300.0,
        total_pnl=300.0,
        total_trades=1,
        win_count=1,
        win_rate=1.0,
        avg_win_r=3.0,
        profit_factor=float("inf"),
    )
    data = backtest_to_dict(result, "SPY", "2026-03-01", "2026-04-01", _config())
    assert data["result"]["profit_factor"] == "inf"


def test_walk_forward_to_dict_includes_per_symbol_and_per_pattern() -> None:
    per_symbol = {"SPY": _sample_backtest_result()}
    breakdowns = [
        PatternBreakdown(
            symbol="SPY",
            pattern="3-2-2",
            trade_count=2,
            win_count=1,
            win_rate=0.5,
            total_pnl=200.0,
            avg_r=1.0,
            profit_factor=3.0,
        )
    ]
    report = WalkForwardReport(
        results=per_symbol,
        breakdowns=breakdowns,
        total_pnl=200.0,
        total_trades=2,
    )
    data = walk_forward_to_dict(report, "2026-03-01", "2026-04-01", _config(), 50_000.0)
    assert data["kind"] == "walk_forward"
    assert data["summary"]["total_trades"] == 2
    assert data["summary"]["symbols"] == 1
    assert "SPY" in data["per_symbol"]
    assert data["per_pattern"][0]["pattern"] == "3-2-2"


# --- File output -----------------------------------------------------------


def test_save_backtest_writes_json_and_md(tmp_path: Path) -> None:
    result = _sample_backtest_result()
    json_path, md_path = save_backtest(
        result, tmp_path, "SPY", "2026-03-01", "2026-04-01", _config()
    )
    assert json_path.exists()
    assert md_path.exists()
    assert json_path.suffix == ".json"
    assert md_path.suffix == ".md"

    loaded = json.loads(json_path.read_text())
    assert loaded["symbol"] == "SPY"
    assert loaded["result"]["total_pnl"] == 200.0

    md = md_path.read_text()
    assert "# Backtest: SPY" in md
    assert "Win rate: 50.0%" in md
    assert "Profit factor: 3.00" in md


def test_save_backtest_filenames_include_run_date(tmp_path: Path) -> None:
    result = _sample_backtest_result()
    json_path, md_path = save_backtest(
        result, tmp_path, "NVDA", "2026-01-01", "2026-02-01", _config()
    )
    assert "NVDA_2026-01-01_to_2026-02-01_run-" in json_path.name
    assert "NVDA_2026-01-01_to_2026-02-01_run-" in md_path.name


def test_save_walk_forward_writes_json_and_md(tmp_path: Path) -> None:
    per_symbol = {"SPY": _sample_backtest_result()}
    breakdowns = [
        PatternBreakdown(
            symbol="SPY",
            pattern="3-2-2",
            trade_count=2,
            win_count=1,
            win_rate=0.5,
            total_pnl=200.0,
            avg_r=1.0,
            profit_factor=3.0,
        )
    ]
    report = WalkForwardReport(
        results=per_symbol,
        breakdowns=breakdowns,
        total_pnl=200.0,
        total_trades=2,
    )
    json_path, md_path = save_walk_forward(
        report, tmp_path, "2026-03-01", "2026-04-01", _config(), 50_000.0
    )
    assert json_path.exists()
    assert md_path.exists()

    md = md_path.read_text()
    assert "# Walk-forward: 2026-03-01 to 2026-04-01" in md
    assert "Per-symbol" in md
    assert "Per-pattern breakdown" in md
    assert "| SPY |" in md


def test_save_creates_missing_out_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    result = _sample_backtest_result()
    json_path, _ = save_backtest(result, nested, "SPY", "2026-03-01", "2026-04-01", _config())
    assert nested.exists()
    assert json_path.exists()
