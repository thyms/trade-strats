#!/usr/bin/env python3
"""Phase 2 tuning: untried variations + new tickers.

Runs on top of the best config from Phase 1 (10Min, RR 4.0, ATR 0.25,
patterns 2-2/3-1-2/rev-strat, both sides).

Tests:
  A. 10Min + drop-AAPL (expected best realistic)
  B. Risk per trade: 0.25%, 0.5% (baseline), 1.0%
  C. Max concurrent: 1, 3 (baseline), 5, 10
  D. Max trades/day: 3, 5 (baseline), 10, 20
  E. New tickers on 10Min best config
  F. Expanded watchlist combos
"""

import copy
import re
import sys
import time
from pathlib import Path
from datetime import date, timedelta

import yaml

from trade_strats import bar_cache
from trade_strats.aggregation import aggregate_df, df_to_bars, parse_tf_minutes
from trade_strats.backtest import build_opens_provider, run_walk_forward
from trade_strats.config import Config

CACHE = Path("data/bars")
START = date(2019, 4, 16)
END = date(2026, 4, 16)

BASE = {
    "mode": "paper",
    "account": {
        "sim_equity_usd": 50000,
        "risk_pct_per_trade": 0.005,
        "daily_loss_cap_pct": 0.02,
        "max_concurrent": 3,
        "max_trades_per_day": 5,
    },
    "strategy": {
        "timeframe": "10Min",
        "patterns": ["2-2", "3-1-2", "rev-strat"],
        "sides": ["long", "short"],
        "min_rr": 4.0,
        "min_bar_atr_mult": 0.25,
        "ftfc_timeframes": ["1D", "4H", "1H"],
    },
    "watchlist": ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"],
    "session": {"entry_window_et": ["09:30", "15:45"], "force_flat_et": "15:55"},
    "blackouts": [],
    "paths": {"db": "data/trades.db", "events_log": "data/events.jsonl", "reports_dir": "reports"},
}


def make_cfg(overrides: dict) -> dict:
    cfg = copy.deepcopy(BASE)
    for key, val in overrides.items():
        if "." in key:
            parts = key.split(".")
            d = cfg
            for p in parts[:-1]:
                d = d[p]
            d[parts[-1]] = val
        else:
            cfg[key] = val
    return cfg


def run_one(label: str, cfg_dict: dict) -> dict | None:
    """Run walk-forward and return summary dict."""
    # Write config
    cfg_path = Path("config/tuning") / f"{label}.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(cfg_dict, default_flow_style=False, sort_keys=False))

    cfg = Config.from_yaml(cfg_path)
    stf = parse_tf_minutes(cfg.strategy.timeframe)
    ctx_start = START - timedelta(days=30)

    bars_by, opens_by = {}, {}
    for sym in cfg.watchlist:
        df_sig = bar_cache.load_range_df(CACHE, sym, START, END)
        df_ctx = bar_cache.load_range_df(CACHE, sym, ctx_start, END)
        sig = df_to_bars(aggregate_df(df_sig, stf))
        if not sig:
            print(f"    {sym}: no bars, skipping")
            continue
        bars_by[sym] = sig
        d = df_to_bars(aggregate_df(df_ctx, 390))
        h = df_to_bars(aggregate_df(df_ctx, 60))
        fh = df_to_bars(aggregate_df(df_ctx, 240))
        opens_by[sym] = build_opens_provider(d, fh, h)

    if not bars_by:
        return None

    report = run_walk_forward(bars_by, opens_by, cfg)
    all_trades = [t for r in report.results.values() for t in r.trades]
    wins = sum(1 for t in all_trades if t.realized_pnl > 0)
    wr = wins / len(all_trades) * 100 if all_trades else 0
    pfs = [r.profit_factor for r in report.results.values() if r.profit_factor != float("inf")]
    avg_pf = sum(pfs) / len(pfs) if pfs else 0
    max_dd = max((r.max_drawdown_pct * 100 for r in report.results.values()), default=0)

    return {
        "label": label,
        "trades": report.total_trades,
        "pnl": report.total_pnl,
        "wr": wr,
        "avg_pf": avg_pf,
        "max_dd": max_dd,
        "per_sym": {
            sym: {
                "trades": r.total_trades,
                "pnl": r.total_pnl,
                "pf": r.profit_factor,
                "dd": r.max_drawdown_pct * 100,
            }
            for sym, r in report.results.items()
        },
    }


def print_results(phase: str, results: list[dict]) -> None:
    results.sort(key=lambda r: r["pnl"], reverse=True)
    print(f"\n{'='*75}")
    print(f"  {phase}")
    print(f"{'='*75}")
    print(f"  {'Label':<22} {'Trades':>6} {'PnL':>12} {'Win%':>6} {'AvgPF':>6} {'MaxDD':>6}")
    print(f"  {'-'*22} {'-'*6} {'-'*12} {'-'*6} {'-'*6} {'-'*6}")
    for r in results:
        print(
            f"  {r['label']:<22} {r['trades']:>6} ${r['pnl']:>10,.0f} {r['wr']:>5.1f}% "
            f"{r['avg_pf']:>6.2f} {r['max_dd']:>5.1f}%"
        )
    # Print per-symbol for the winner
    best = results[0]
    print(f"\n  Winner: {best['label']}")
    for sym, d in best["per_sym"].items():
        pf = "inf" if d["pf"] == float("inf") else f"{d['pf']:.2f}"
        print(f"    {sym:<6} trades={d['trades']:<5} PnL=${d['pnl']:>10,.2f}  PF={pf}  DD={d['dd']:.1f}%")


def main() -> None:
    all_results: list[dict] = []

    # --- A: 10Min + drop AAPL ---
    print("\n### A: 10Min + drop-AAPL")
    r = run_one("10m-no-aapl", make_cfg({"watchlist": ["SPY", "QQQ", "NVDA", "TSLA"]}))
    if r:
        all_results.append(r)
        print(f"  {r['label']}: {r['trades']} trades, ${r['pnl']:,.0f}, PF={r['avg_pf']:.2f}")

    # --- B: Risk per trade ---
    print("\n### B: Risk per trade")
    risk_results = []
    for pct, label in [(0.0025, "risk-0.25pct"), (0.005, "risk-0.50pct"), (0.01, "risk-1.0pct")]:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"account.risk_pct_per_trade": pct}))
        if r:
            risk_results.append(r)
    print_results("B: Risk Per Trade", risk_results)
    all_results.extend(risk_results)

    # --- C: Max concurrent ---
    print("\n### C: Max concurrent positions")
    conc_results = []
    for n, label in [(1, "conc-1"), (3, "conc-3"), (5, "conc-5"), (10, "conc-10")]:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"account.max_concurrent": n}))
        if r:
            conc_results.append(r)
    print_results("C: Max Concurrent", conc_results)
    all_results.extend(conc_results)

    # --- D: Max trades/day ---
    print("\n### D: Max trades per day")
    tpd_results = []
    for n, label in [(3, "tpd-3"), (5, "tpd-5"), (10, "tpd-10"), (20, "tpd-20")]:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"account.max_trades_per_day": n}))
        if r:
            tpd_results.append(r)
    print_results("D: Max Trades/Day", tpd_results)
    all_results.extend(tpd_results)

    # --- E: Individual new tickers ---
    print("\n### E: New tickers (individual, 10Min best config)")
    new_tickers = ["AMD", "AMZN", "META", "GOOG", "COIN", "MSTR"]
    ticker_results = []
    for sym in new_tickers:
        label = f"solo-{sym.lower()}"
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"watchlist": [sym]}))
        if r:
            ticker_results.append(r)
    if ticker_results:
        print_results("E: New Tickers (Solo)", ticker_results)
    all_results.extend(ticker_results)

    # --- F: Expanded watchlists ---
    print("\n### F: Expanded watchlists")
    # Best originals + promising new tickers (based on E results)
    wl_results = []

    # All 11
    r = run_one("all-11", make_cfg({
        "watchlist": ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "AMZN", "META", "GOOG", "COIN", "MSTR"]
    }))
    if r:
        wl_results.append(r)

    # Core 4 + all new
    r = run_one("core4-plus-new", make_cfg({
        "watchlist": ["SPY", "QQQ", "NVDA", "TSLA", "AMD", "AMZN", "META", "GOOG", "COIN", "MSTR"]
    }))
    if r:
        wl_results.append(r)

    # Just the volatile names
    r = run_one("volatile-6", make_cfg({
        "watchlist": ["NVDA", "TSLA", "AMD", "COIN", "MSTR", "META"]
    }))
    if r:
        wl_results.append(r)

    if wl_results:
        print_results("F: Expanded Watchlists", wl_results)
    all_results.extend(wl_results)

    # --- Grand summary ---
    print_results("GRAND SUMMARY (all Phase 2 runs)", all_results)


if __name__ == "__main__":
    main()
