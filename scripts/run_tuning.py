#!/usr/bin/env python3
"""Run the phased parameter tuning plan.

Generates config YAML variants, runs walk-forward for each, and prints
a summary table after each phase. Results are saved with --label so they
land in reports/walk-forward/ for later analysis.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config" / "tuning"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

START = "2019-04-16"
END = "2026-04-16"

BASE_CONFIG = {
    "mode": "paper",
    "account": {
        "sim_equity_usd": 50000,
        "risk_pct_per_trade": 0.005,
        "daily_loss_cap_pct": 0.02,
        "max_concurrent": 3,
        "max_trades_per_day": 5,
    },
    "watchlist": ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"],
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


def make_config(
    *,
    timeframe: str = "15Min",
    patterns: list[str] | None = None,
    sides: list[str] | None = None,
    min_rr: float = 3.0,
    min_bar_atr_mult: float = 0.5,
    watchlist: list[str] | None = None,
) -> dict:
    import copy
    cfg = copy.deepcopy(BASE_CONFIG)
    if watchlist is not None:
        cfg["watchlist"] = watchlist
    cfg["strategy"] = {
        "timeframe": timeframe,
        "patterns": patterns or ["3-2-2", "2-2", "3-1-2", "rev-strat"],
        "sides": sides or ["long", "short"],
        "min_rr": min_rr,
        "min_bar_atr_mult": min_bar_atr_mult,
        "ftfc_timeframes": ["1D", "4H", "1H"],
    }
    return cfg


def write_config(name: str, cfg: dict) -> Path:
    import yaml
    path = CONFIG_DIR / f"{name}.yaml"
    path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    return path


def run_walk_forward(config_path: Path, label: str) -> str:
    """Run walk-forward and return stdout."""
    cmd = [
        sys.executable, "-m", "trade_strats.cli", "walk-forward",
        "--start", START,
        "--end", END,
        "--config", str(config_path),
        "--label", label,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"  FAILED: {label}")
        print(result.stderr[-500:] if result.stderr else "no stderr")
        return ""
    return result.stdout


def extract_summary(output: str, label: str) -> dict:
    """Parse the walk-forward summary from stdout."""
    lines = output.strip().split("\n")
    info = {"label": label, "total_trades": 0, "total_pnl": 0.0, "symbols": {}}
    for line in lines:
        line = line.strip()
        if line.startswith("Total trades:"):
            info["total_trades"] = int(line.split(":")[1].strip())
        elif line.startswith("Total P&L:"):
            info["total_pnl"] = float(line.split("$")[1].strip().replace(",", ""))
        elif line and line[0].isupper() and "trades=" in line:
            parts = line.split()
            sym = parts[0]
            trades = int(parts[1].split("=")[1])
            win_pct = float(parts[2].split("=")[1])
            pnl_str = parts[3].split("$")[1].replace(",", "")
            pnl = float(pnl_str)
            pf_str = parts[4].split("=")[1]
            pf = float("inf") if pf_str == "inf" else float(pf_str)
            info["symbols"][sym] = {
                "trades": trades, "win_pct": win_pct, "pnl": pnl, "pf": pf,
            }
    return info


def print_phase_results(phase: str, results: list[dict]) -> None:
    print(f"\n{'='*70}")
    print(f"  {phase} Results")
    print(f"{'='*70}")
    print(f"  {'Label':<25} {'Trades':>7} {'PnL':>12} {'Avg PF':>8}")
    print(f"  {'-'*25} {'-'*7} {'-'*12} {'-'*8}")
    for r in results:
        pfs = [s["pf"] for s in r["symbols"].values() if s["pf"] != float("inf")]
        avg_pf = sum(pfs) / len(pfs) if pfs else 0
        print(f"  {r['label']:<25} {r['total_trades']:>7} ${r['total_pnl']:>10,.2f} {avg_pf:>8.2f}")
    print()


def find_best(results: list[dict]) -> dict:
    """Pick the run with the highest total PnL."""
    return max(results, key=lambda r: r["total_pnl"])


def main() -> None:
    all_results: dict[str, list[dict]] = {}

    # ---- Phase 1: Pattern selection ----
    print("\n### Phase 1: Pattern selection")
    phase1_runs = [
        ("no-322", ["2-2", "3-1-2", "rev-strat"]),
        ("no-322-no-rev", ["2-2", "3-1-2"]),
        ("22-only", ["2-2"]),
        ("22-312", ["2-2", "3-1-2"]),
    ]
    phase1_results = []
    for label, patterns in phase1_runs:
        print(f"  Running {label}...")
        cfg = make_config(patterns=patterns)
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase1_results.append(extract_summary(out, label))
    print_phase_results("Phase 1: Pattern Selection", phase1_results)
    all_results["phase1"] = phase1_results

    best1 = find_best(phase1_results)
    # Extract winning patterns from config
    import yaml
    best1_cfg = yaml.safe_load((CONFIG_DIR / f"{best1['label']}.yaml").read_text())
    winning_patterns = best1_cfg["strategy"]["patterns"]
    print(f"  Winner: {best1['label']} (patterns={winning_patterns})")

    # ---- Phase 2: R:R ratio ----
    print("\n### Phase 2: R:R ratio")
    phase2_runs = [
        ("rr-1.5", 1.5),
        ("rr-2.0", 2.0),
        ("rr-2.5", 2.5),
        ("rr-3.0", 3.0),
        ("rr-4.0", 4.0),
    ]
    phase2_results = []
    for label, rr in phase2_runs:
        print(f"  Running {label}...")
        cfg = make_config(patterns=winning_patterns, min_rr=rr)
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase2_results.append(extract_summary(out, label))
    print_phase_results("Phase 2: R:R Ratio", phase2_results)
    all_results["phase2"] = phase2_results

    best2 = find_best(phase2_results)
    best2_cfg = yaml.safe_load((CONFIG_DIR / f"{best2['label']}.yaml").read_text())
    winning_rr = best2_cfg["strategy"]["min_rr"]
    print(f"  Winner: {best2['label']} (min_rr={winning_rr})")

    # ---- Phase 3: ATR filter ----
    print("\n### Phase 3: ATR filter")
    phase3_runs = [
        ("atr-0.0", 0.0),
        ("atr-0.25", 0.25),
        ("atr-0.5", 0.5),
        ("atr-0.75", 0.75),
        ("atr-1.0", 1.0),
    ]
    phase3_results = []
    for label, atr in phase3_runs:
        print(f"  Running {label}...")
        cfg = make_config(patterns=winning_patterns, min_rr=winning_rr, min_bar_atr_mult=atr)
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase3_results.append(extract_summary(out, label))
    print_phase_results("Phase 3: ATR Filter", phase3_results)
    all_results["phase3"] = phase3_results

    best3 = find_best(phase3_results)
    best3_cfg = yaml.safe_load((CONFIG_DIR / f"{best3['label']}.yaml").read_text())
    winning_atr = best3_cfg["strategy"]["min_bar_atr_mult"]
    print(f"  Winner: {best3['label']} (min_bar_atr_mult={winning_atr})")

    # ---- Phase 4: Timeframe ----
    print("\n### Phase 4: Timeframe")
    phase4_runs = [
        ("tf-5m", "5Min"),
        ("tf-10m", "10Min"),
        ("tf-15m", "15Min"),
        ("tf-20m", "20Min"),
        ("tf-30m", "30Min"),
    ]
    phase4_results = []
    for label, tf in phase4_runs:
        print(f"  Running {label}...")
        cfg = make_config(
            patterns=winning_patterns, min_rr=winning_rr,
            min_bar_atr_mult=winning_atr, timeframe=tf,
        )
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase4_results.append(extract_summary(out, label))
    print_phase_results("Phase 4: Timeframe", phase4_results)
    all_results["phase4"] = phase4_results

    best4 = find_best(phase4_results)
    best4_cfg = yaml.safe_load((CONFIG_DIR / f"{best4['label']}.yaml").read_text())
    winning_tf = best4_cfg["strategy"]["timeframe"]
    print(f"  Winner: {best4['label']} (timeframe={winning_tf})")

    # ---- Phase 5: Side filter ----
    print("\n### Phase 5: Side filter")
    phase5_runs = [
        ("long-only", ["long"]),
        ("short-only", ["short"]),
    ]
    phase5_results = []
    for label, sides in phase5_runs:
        print(f"  Running {label}...")
        cfg = make_config(
            patterns=winning_patterns, min_rr=winning_rr,
            min_bar_atr_mult=winning_atr, timeframe=winning_tf, sides=sides,
        )
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase5_results.append(extract_summary(out, label))
    # Add the both-sides winner for comparison
    phase5_results.append(extract_summary(
        run_walk_forward(CONFIG_DIR / f"{best4['label']}.yaml", best4["label"]),
        f"{best4['label']} (both)",
    ))
    print_phase_results("Phase 5: Side Filter", phase5_results)
    all_results["phase5"] = phase5_results

    best5 = find_best(phase5_results)
    print(f"  Winner: {best5['label']}")

    # Determine winning sides
    if "long-only" in best5["label"]:
        winning_sides = ["long"]
    elif "short-only" in best5["label"]:
        winning_sides = ["short"]
    else:
        winning_sides = ["long", "short"]

    # ---- Phase 6: Watchlist pruning ----
    print("\n### Phase 6: Watchlist pruning")
    phase6_runs = [
        ("no-aapl", ["SPY", "QQQ", "NVDA", "TSLA"]),
        ("nvda-tsla", ["NVDA", "TSLA"]),
        ("all-5", ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]),
    ]
    phase6_results = []
    for label, wl in phase6_runs:
        print(f"  Running {label}...")
        cfg = make_config(
            patterns=winning_patterns, min_rr=winning_rr,
            min_bar_atr_mult=winning_atr, timeframe=winning_tf,
            sides=winning_sides, watchlist=wl,
        )
        path = write_config(label, cfg)
        out = run_walk_forward(path, label)
        if out:
            phase6_results.append(extract_summary(out, label))
    print_phase_results("Phase 6: Watchlist Pruning", phase6_results)
    all_results["phase6"] = phase6_results

    best6 = find_best(phase6_results)
    print(f"  Winner: {best6['label']}")

    # ---- Final summary ----
    print("\n" + "=" * 70)
    print("  FINAL BEST CONFIGURATION")
    print("=" * 70)
    best_cfg_path = CONFIG_DIR / f"{best6['label']}.yaml"
    print(f"  Config: {best_cfg_path}")
    print(f"  {best6['label']}: {best6['total_trades']} trades, ${best6['total_pnl']:,.2f} PnL")
    print()
    print("  Per-symbol:")
    for sym, data in best6["symbols"].items():
        pf_str = "inf" if data["pf"] == float("inf") else f"{data['pf']:.2f}"
        print(f"    {sym:<6} trades={data['trades']:<5} win%={data['win_pct']:>5.1f}  PnL=${data['pnl']:>10,.2f}  PF={pf_str}")
    print()
    print(f"  Best config YAML:")
    print(textwrap.indent(best_cfg_path.read_text(), "    "))


if __name__ == "__main__":
    main()
