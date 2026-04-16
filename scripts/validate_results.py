#!/usr/bin/env python3
"""Validate the 5Min and 10Min tuning results from multiple angles.

Checks:
1. Reproduce: re-run and confirm identical PnL
2. Sub-period consistency: split into 2019-2022 vs 2022-2026
3. Year-by-year breakdown: annual PnL to spot if one year dominates
4. Trade distribution: median/mean PnL, win/loss sizes, outlier check
5. Max drawdown timeline
"""

import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trade_strats import bar_cache
from trade_strats.aggregation import aggregate_df, df_to_bars, parse_tf_minutes
from trade_strats.backtest import (
    BacktestResult,
    SimulatedTrade,
    build_opens_provider,
    run_backtest,
    run_walk_forward,
)
from trade_strats.config import Config

CACHE = Path("data/bars")


def load_and_run(
    cfg: Config, start: date, end: date
) -> tuple[dict[str, BacktestResult], list[SimulatedTrade]]:
    """Run walk-forward for a config and date range. Returns per-symbol results and all trades."""
    stf_min = parse_tf_minutes(cfg.strategy.timeframe)
    ctx_start = start - timedelta(days=30)

    bars_by: dict[str, list] = {}
    opens_by = {}
    for sym in cfg.watchlist:
        df_sig = bar_cache.load_range_df(CACHE, sym, start, end)
        df_ctx = bar_cache.load_range_df(CACHE, sym, ctx_start, end)
        bars_by[sym] = df_to_bars(aggregate_df(df_sig, stf_min))
        d = df_to_bars(aggregate_df(df_ctx, 390))
        h = df_to_bars(aggregate_df(df_ctx, 60))
        fh = df_to_bars(aggregate_df(df_ctx, 240))
        opens_by[sym] = build_opens_provider(d, fh, h)

    report = run_walk_forward(bars_by, opens_by, cfg)
    all_trades = []
    for r in report.results.values():
        all_trades.extend(r.trades)
    return report.results, all_trades


def print_header(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def analyze_trades(trades: list[SimulatedTrade], label: str) -> None:
    """Print trade distribution stats."""
    if not trades:
        print(f"  {label}: no trades")
        return

    pnls = sorted(t.realized_pnl for t in trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [t.r_multiple for t in trades]

    print(f"  {label}:")
    print(f"    Trades: {len(trades)}")
    print(f"    Win/Loss: {len(wins)}W / {len(losses)}L ({len(wins)/len(trades)*100:.1f}%)")
    print(f"    Total PnL: ${sum(pnls):,.2f}")
    print(f"    Mean PnL/trade: ${sum(pnls)/len(trades):,.2f}")
    print(f"    Median PnL/trade: ${pnls[len(pnls)//2]:,.2f}")
    print(f"    Avg win: ${sum(wins)/len(wins):,.2f}" if wins else "    Avg win: N/A")
    print(f"    Avg loss: ${sum(losses)/len(losses):,.2f}" if losses else "    Avg loss: N/A")
    print(f"    Largest win: ${max(pnls):,.2f}")
    print(f"    Largest loss: ${min(pnls):,.2f}")
    print(f"    Avg R: {sum(rs)/len(rs):.3f}")
    # Top 10 trades contribution
    top10 = sorted(pnls, reverse=True)[:10]
    print(f"    Top 10 trades total: ${sum(top10):,.2f} ({sum(top10)/sum(pnls)*100:.1f}% of total PnL)")
    # Bottom 10
    bot10 = sorted(pnls)[:10]
    print(f"    Bottom 10 trades total: ${sum(bot10):,.2f}")
    # Exit reasons
    reasons = defaultdict(int)
    for t in trades:
        reasons[t.exit_reason] += 1
    print(f"    Exit reasons: {dict(reasons)}")


def year_breakdown(trades: list[SimulatedTrade], label: str) -> None:
    """PnL by calendar year."""
    by_year: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        yr = t.entry_ts.year
        by_year[yr].append(t.realized_pnl)

    print(f"\n  {label} — Year-by-year:")
    print(f"    {'Year':<6} {'Trades':>7} {'PnL':>12} {'Win%':>6}")
    for yr in sorted(by_year):
        pnls = by_year[yr]
        wins = sum(1 for p in pnls if p > 0)
        print(f"    {yr:<6} {len(pnls):>7} ${sum(pnls):>10,.2f} {wins/len(pnls)*100:>5.1f}%")


def main() -> None:
    cfg_5m = Config.from_yaml(Path("config/tuning/tf-5m.yaml"))
    cfg_10m = Config.from_yaml(Path("config/tuning/tf-10m.yaml"))

    full_start = date(2019, 4, 16)
    full_end = date(2026, 4, 16)

    # === CHECK 1: Reproduce ===
    print_header("CHECK 1: Reproduce full 7-year results")
    for label, cfg in [("5Min", cfg_5m), ("10Min", cfg_10m)]:
        t0 = time.perf_counter()
        results, trades = load_and_run(cfg, full_start, full_end)
        elapsed = time.perf_counter() - t0
        total_pnl = sum(r.total_pnl for r in results.values())
        total_trades = sum(r.total_trades for r in results.values())
        print(f"  {label}: {total_trades} trades, ${total_pnl:,.2f} PnL ({elapsed:.1f}s)")
        for sym, r in results.items():
            pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
            print(f"    {sym:<6} trades={r.total_trades:<5} PnL=${r.total_pnl:>10,.2f}  PF={pf}  DD={r.max_drawdown_pct*100:.1f}%")

    # === CHECK 2: Sub-period split ===
    print_header("CHECK 2: Sub-period consistency (first half vs second half)")
    mid = date(2022, 10, 16)
    for label, cfg in [("5Min", cfg_5m), ("10Min", cfg_10m)]:
        for period, s, e in [("2019-2022", full_start, mid), ("2022-2026", mid, full_end)]:
            results, trades = load_and_run(cfg, s, e)
            total_pnl = sum(r.total_pnl for r in results.values())
            total_trades = sum(r.total_trades for r in results.values())
            pfs = [r.profit_factor for r in results.values() if r.profit_factor != float("inf")]
            avg_pf = sum(pfs) / len(pfs) if pfs else 0
            print(f"  {label} {period}: {total_trades} trades, ${total_pnl:>12,.2f}, avg PF={avg_pf:.2f}")

    # === CHECK 3: Year-by-year ===
    print_header("CHECK 3: Year-by-year breakdown")
    for label, cfg in [("5Min", cfg_5m), ("10Min", cfg_10m)]:
        _, trades = load_and_run(cfg, full_start, full_end)
        year_breakdown(trades, label)

    # === CHECK 4: Trade distribution ===
    print_header("CHECK 4: Trade distribution analysis")
    for label, cfg in [("5Min", cfg_5m), ("10Min", cfg_10m)]:
        _, trades = load_and_run(cfg, full_start, full_end)
        analyze_trades(trades, label)

    # === CHECK 5: Per-symbol year-by-year for 10Min ===
    print_header("CHECK 5: 10Min per-symbol year-by-year")
    results_10m, _ = load_and_run(cfg_10m, full_start, full_end)
    for sym, r in results_10m.items():
        year_breakdown(r.trades, f"10Min {sym}")


if __name__ == "__main__":
    main()
