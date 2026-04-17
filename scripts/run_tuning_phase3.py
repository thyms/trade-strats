#!/usr/bin/env python3
"""Phase 3 tuning: slippage, entry window, FTFC, per-symbol optimization.

Requires code changes from this session (slippage in backtest, entry window
filter, FTFC timeframe passthrough).
"""

import copy
from datetime import date, timedelta
from pathlib import Path

import yaml

from trade_strats import bar_cache
from trade_strats.aggregation import aggregate_df, df_to_bars, parse_tf_minutes
from trade_strats.backtest import build_opens_provider, run_walk_forward
from trade_strats.config import Config

CACHE = Path("data/bars")
START = date(2019, 4, 16)
END = date(2026, 4, 16)
CFG_DIR = Path("config/tuning")
CFG_DIR.mkdir(parents=True, exist_ok=True)

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
        "slippage_per_share": 0.0,
    },
    "watchlist": ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"],
    "session": {"entry_window_et": ["09:30", "15:45"], "force_flat_et": "15:55"},
    "blackouts": [],
    "paths": {"db": "data/trades.db", "events_log": "data/events.jsonl", "reports_dir": "reports"},
}


def make_cfg(overrides: dict) -> dict:
    cfg = copy.deepcopy(BASE)
    for key, val in overrides.items():
        parts = key.split(".")
        d = cfg
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = val
    return cfg


def run_one(label: str, cfg_dict: dict) -> dict | None:
    cfg_path = CFG_DIR / f"{label}.yaml"
    cfg_path.write_text(yaml.dump(cfg_dict, default_flow_style=False, sort_keys=False))
    cfg = Config.from_yaml(cfg_path)
    stf = parse_tf_minutes(cfg.strategy.timeframe)
    ctx = START - timedelta(days=30)

    bars_by, opens_by = {}, {}
    for sym in cfg.watchlist:
        df_s = bar_cache.load_range_df(CACHE, sym, START, END)
        df_c = bar_cache.load_range_df(CACHE, sym, ctx, END)
        sig = df_to_bars(aggregate_df(df_s, stf))
        if not sig:
            continue
        bars_by[sym] = sig
        d = df_to_bars(aggregate_df(df_c, 390))
        h = df_to_bars(aggregate_df(df_c, 60))
        fh = df_to_bars(aggregate_df(df_c, 240))
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
            sym: {"trades": r.total_trades, "pnl": r.total_pnl, "pf": r.profit_factor, "dd": r.max_drawdown_pct * 100}
            for sym, r in report.results.items()
        },
    }


def print_results(phase: str, results: list[dict]) -> None:
    results.sort(key=lambda r: r["pnl"], reverse=True)
    print(f"\n{'='*75}")
    print(f"  {phase}")
    print(f"{'='*75}")
    print(f"  {'Label':<25} {'Trades':>6} {'PnL':>12} {'Win%':>6} {'AvgPF':>6} {'MaxDD':>6}")
    print(f"  {'-'*25} {'-'*6} {'-'*12} {'-'*6} {'-'*6} {'-'*6}")
    for r in results:
        print(f"  {r['label']:<25} {r['trades']:>6} ${r['pnl']:>10,.0f} {r['wr']:>5.1f}% {r['avg_pf']:>6.2f} {r['max_dd']:>5.1f}%")


def main() -> None:
    # --- A: Slippage ---
    print("\n### A: Slippage model")
    slip_results = []
    for slip, label in [(0.0, "slip-0.00"), (0.05, "slip-0.05"), (0.10, "slip-0.10"), (0.20, "slip-0.20"), (0.50, "slip-0.50")]:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"strategy.slippage_per_share": slip}))
        if r:
            slip_results.append(r)
    print_results("A: Slippage Model", slip_results)

    # --- B: Entry window ---
    print("\n### B: Entry window")
    win_results = []
    windows = [
        ("win-full", ["09:30", "15:45"]),         # baseline
        ("win-morning", ["09:30", "12:00"]),       # morning only
        ("win-avoid-open", ["10:00", "15:45"]),    # skip first 30min
        ("win-core", ["10:00", "15:00"]),          # avoid open and close
        ("win-early", ["09:30", "11:00"]),         # first 90 min only
    ]
    for label, window in windows:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"session.entry_window_et": window}))
        if r:
            win_results.append(r)
    print_results("B: Entry Window", win_results)

    # --- C: FTFC variations ---
    print("\n### C: FTFC timeframe variations")
    ftfc_results = []
    ftfc_modes = [
        ("ftfc-full", ["1D", "4H", "1H"]),        # baseline
        ("ftfc-no-4h", ["1D", "1H"]),              # skip 4H
        ("ftfc-1d-only", ["1D"]),                  # daily only
        ("ftfc-1h-only", ["1H"]),                  # 1H only
        ("ftfc-none", []),                          # no FTFC filter
    ]
    for label, tfs in ftfc_modes:
        print(f"  Running {label}...")
        r = run_one(label, make_cfg({"strategy.ftfc_timeframes": tfs}))
        if r:
            ftfc_results.append(r)
    print_results("C: FTFC Variations", ftfc_results)

    # --- D: Per-symbol R:R sweep (top 4 tickers) ---
    print("\n### D: Per-symbol R:R optimization")
    for sym in ["NVDA", "TSLA", "COIN", "MSTR"]:
        sym_results = []
        for rr in [2.0, 3.0, 4.0, 5.0, 6.0]:
            label = f"rr-{sym.lower()}-{rr:.0f}"
            print(f"  Running {label}...")
            r = run_one(label, make_cfg({"watchlist": [sym], "strategy.min_rr": rr}))
            if r:
                sym_results.append(r)
        if sym_results:
            print_results(f"D: {sym} R:R Sweep", sym_results)


if __name__ == "__main__":
    main()
