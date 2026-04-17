# AGENTS.md — LLM Coding Agent Guide

## Project overview

Deterministic intraday trading bot implementing TheStrat pattern detection
on US equities via Alpaca. Supports backtesting, walk-forward analysis,
live paper/real trading, and parameter tuning.

**Stack:** Python 3.12, alpaca-py, pandas, pyarrow, pydantic, aiosqlite, typer.
**Package manager:** uv (not pip).

## Quick reference

```bash
# Install / sync deps
uv sync

# Run tests
uv run pytest tests/ -x -q

# Run backtest (single symbol)
uv run trade-strats backtest --symbol NVDA --start 2023-01-01 --end 2026-04-01 --config config/config.example.yaml

# Run walk-forward (all watchlist symbols)
uv run trade-strats walk-forward --start 2019-04-16 --end 2026-04-16 --config config/config.example.yaml

# Fetch and cache 1Min bars for new tickers
uv run python scripts/fetch_bars.py AMD AMZN META

# Run tuning sweeps
uv run python scripts/run_tuning.py          # Phase 1: patterns, RR, ATR, timeframe, sides, watchlist
uv run python scripts/run_tuning_phase2.py    # Phase 2: risk sizing, concurrency, new tickers
uv run python scripts/run_tuning_phase3.py    # Phase 3: slippage, entry window, FTFC, per-symbol RR
```

## Project structure

```
src/trade_strats/
  aggregation.py    # TimedBar, OHLCV aggregation (1m -> any TF), aggregate_df (fast pandas path)
  backtest.py       # run_backtest, run_walk_forward, fill simulation with slippage
  bar_cache.py      # Monthly parquet cache: data/bars/<SYM>/1Min/<YYYY-MM>.parquet
  cli.py            # Typer CLI: run, backtest, walk-forward, status, flat-all, reconcile
  config.py         # Pydantic config models (StrategyConfig, AccountConfig, etc.)
  execution.py      # Alpaca bracket order submission
  journal.py        # SQLite trade/order journal
  market_data.py    # Alpaca WS streaming + historical backfill, MarketData class
  orchestrator.py   # Live session: evaluate_and_submit, run_session
  reconcile.py      # Compare Alpaca state to journal
  reports.py        # Save backtest/walk-forward results as JSON+MD
  risk.py           # Position sizing, risk gates (daily loss cap, max concurrent, etc.)
  trade_updates.py  # Alpaca trade update WS handler
  tui.py            # Rich-based terminal UI for live sessions

  strategy/
    labeler.py      # Bar classification: Scenario (1=inside, 2=directional, 3=outside), Color
    patterns.py     # TheStrat pattern detection: 2-2, 3-2-2, 3-1-2, rev-strat
    ftfc.py         # Full Timeframe Continuity filter (configurable TFs)

config/
  config.example.yaml   # Reference config (copy to config.yaml for local use)
  tuning/               # Generated configs from parameter sweeps

data/
  bars/                 # Cached 1Min parquet files (committed to repo, immutable historical data)
  schema.sql            # SQLite schema for journal

reports/
  RESEARCH_LOG.md       # Living research log — ALL findings go here (newest first)
  TUNING_PLAN.md        # Original tuning plan
  walk-forward/         # Saved experiment results (MD summaries + JSON detail)
  backtest/             # Single-symbol backtest results

scripts/
  fetch_bars.py         # Download and cache 1Min bars from Alpaca
  run_tuning.py         # Phase 1 parameter sweep
  run_tuning_phase2.py  # Phase 2: risk, concurrency, new tickers
  run_tuning_phase3.py  # Phase 3: slippage, entry window, FTFC
  validate_results.py   # Cross-validation: sub-periods, year-by-year, trade distribution
```

## Key config parameters

| Parameter | Config path | Default | Description |
|-----------|------------|---------|-------------|
| Timeframe | `strategy.timeframe` | 15Min | Signal bar timeframe. Supports any NMin (aggregated from cached 1m). |
| Patterns | `strategy.patterns` | all 4 | Which TheStrat patterns to trade. |
| R:R | `strategy.min_rr` | 3.0 | Minimum reward-to-risk ratio for target placement. |
| ATR filter | `strategy.min_bar_atr_mult` | 0.5 | Reject setups where signal bar range < mult * ATR14. |
| Slippage | `strategy.slippage_per_share` | 0.0 | Per-share slippage penalty on all fills. |
| FTFC TFs | `strategy.ftfc_timeframes` | [1D,4H,1H] | Which timeframes to check for continuity. |
| Entry window | `session.entry_window_et` | [09:30,15:45] | Only generate new setups within this ET window. |
| Risk/trade | `account.risk_pct_per_trade` | 0.005 | Fraction of equity risked per trade. |

## Data flow

1. **Cached 1Min bars** in `data/bars/<SYM>/1Min/<YYYY-MM>.parquet` (committed to repo)
2. `bar_cache.load_range_df()` loads as DataFrame (fast, ~0.1s per ticker for 7 years)
3. `aggregate_df(df, minutes)` aggregates to any timeframe in pandas (~1s per TF)
4. `df_to_bars()` converts to `list[TimedBar]` for the backtest engine
5. `run_backtest()` replays bars through pattern detection + risk + fill sim
6. `run_walk_forward()` runs backtest per symbol and aggregates

## Testing

```bash
uv run pytest tests/ -x -q            # All tests (~1.3s)
uv run pytest tests/test_backtest.py   # Backtest-specific
uv run pytest tests/ -k "test_ftfc"    # Pattern match
```

- Tests use fixture configs, not real Alpaca keys
- Alpaca integration tests are skipped unless `ALPACA_API_KEY` is set
- Always run full test suite after modifying backtest.py, aggregation.py, or config.py

## Critical rules

### DO NOT re-run experiments when results already exist

Experiment results are saved in `reports/walk-forward/*.md` and `*.json`.
Before running a new walk-forward, **check if the result already exists**:
```bash
ls reports/walk-forward/*<label>*
```
Parse existing results from the saved files instead of re-computing.
Each walk-forward run takes 20-30 seconds — multiplied across dozens of
experiments this adds up unnecessarily.

### DO NOT run tests unless implementing or fixing one

Tests pass. Only re-run after code changes to backtest.py, aggregation.py,
config.py, strategy/, or risk.py.

### Findings go in reports/RESEARCH_LOG.md

All experiment results, findings, assessments, and decisions must be
logged in `reports/RESEARCH_LOG.md` with a dated section (newest first).
Include tables with key metrics. This is the single source of truth for
research progress.

### Bar data is immutable once cached

Parquet files in `data/bars/` are historical 1Min bars from Alpaca. They
never change. They are committed to the repo so clones don't need Alpaca
API keys for backtesting.

### Config is the experimentation interface

To test a new parameter combination, create a YAML in `config/tuning/`
and run walk-forward against it. Do not hardcode parameter values in
Python code.

### Slippage matters

The strategy's per-trade edge is thin (~$65 mean at 10Min). At $0.05/share
slippage the edge nearly vanishes. Always consider execution realism when
evaluating results. Zero-slippage PnL numbers are upper bounds, not
predictions.

## Current best configuration (as of 2026-04-17)

```yaml
strategy:
  timeframe: 10Min
  patterns: [2-2, 3-1-2, rev-strat]
  sides: [long, short]
  min_rr: 4.0
  min_bar_atr_mult: 0.25
  ftfc_timeframes: [1D, 4H, 1H]
  slippage_per_share: 0.0
watchlist: [NVDA, TSLA, COIN, MSTR]
```

**Caveat:** This is the zero-slippage optimal. At $0.05 slippage the edge
is marginal (PF ~1.14). Not yet validated for live trading.

## Architecture notes for future work

- ~60% of codebase is strategy-agnostic (data, cache, journal, CLI, reports)
- To add a new strategy, implement signal detection and wire into backtest.py
- Options trading would require a different execution layer and pricing model
- See `reports/RESEARCH_LOG.md` "Architecture assessment" section for details
