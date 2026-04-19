#!/usr/bin/env python3
"""Minimum-viable BTC backtest.

Fetches 1Min BTC/USD bars from Alpaca's crypto endpoint (no auth needed),
caches them as monthly parquet files, and runs a TheStrat walk-forward
on the result. Uses 24/7 aggregation (UTC-midnight anchor, no RTH filter,
no force-flat).

Goal: find out whether TheStrat patterns carry any edge on BTC before
committing to crypto live execution.

Usage::

    uv run python scripts/btc_backtest.py                     # default: 2y range
    uv run python scripts/btc_backtest.py --start 2023-01-01 --end 2026-04-01
    uv run python scripts/btc_backtest.py --timeframe 15Min --rr 4.0
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from trade_strats.aggregation import TimedBar, aggregate_df_24x7, df_to_bars, parse_tf_minutes
from trade_strats.backtest import OpensProvider, run_backtest
from trade_strats.config import Config
from trade_strats.strategy.ftfc import HigherTfOpens
import bisect
import yaml

CACHE_ROOT = Path("data/bars")
SYMBOL_ALPACA = "BTC/USD"
SYMBOL_DIR = "BTC_USD"  # filesystem-safe


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_path(month_key: str) -> Path:
    return CACHE_ROOT / SYMBOL_DIR / "1Min" / f"{month_key}.parquet"


def _months_in_range(start: date, end: date) -> list[str]:
    months: list[str] = []
    d = start.replace(day=1)
    while d <= end:
        months.append(_month_key(d))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _fetch_month(client: CryptoHistoricalDataClient, year: int, month: int) -> pd.DataFrame:
    """Fetch one calendar month of 1m BTC bars."""
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC) - timedelta(seconds=1)

    req = CryptoBarsRequest(
        symbol_or_symbols=SYMBOL_ALPACA,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end,
    )
    bars = client.get_crypto_bars(req)
    raw = bars.data.get(SYMBOL_ALPACA, [])
    rows = [
        {
            "ts": b.timestamp,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume),
        }
        for b in raw
    ]
    return pd.DataFrame(rows)


def ensure_cached(start: date, end: date) -> None:
    """Fetch any missing monthly parquet files for BTC."""
    client = CryptoHistoricalDataClient()
    for mk in _months_in_range(start, end):
        path = _month_path(mk)
        if path.exists():
            continue
        year, month = (int(x) for x in mk.split("-"))
        print(f"  Fetching BTC 1Min {mk}...", end="", flush=True)
        df = _fetch_month(client, year, month)
        if df.empty:
            print(" (no data)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        print(f" {len(df):,} bars")


def load_range_df(start: date, end: date) -> pd.DataFrame:
    """Load cached BTC bars as a DataFrame, trimmed to [start, end]."""
    dfs = []
    for mk in _months_in_range(start, end):
        path = _month_path(mk)
        if path.exists():
            dfs.append(pd.read_parquet(path))
    if not dfs:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.concat(dfs, ignore_index=True)
    start_dt = pd.Timestamp(start, tz="UTC")
    end_dt = pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
    df = df[(df["ts"] >= start_dt) & (df["ts"] <= end_dt)]
    return df.reset_index(drop=True)


def build_btc_opens_provider(
    daily: list[TimedBar], four_hour: list[TimedBar], one_hour: list[TimedBar]
) -> OpensProvider:
    """Same mechanic as the equity version but without ET/RTH assumptions."""
    d_ts = [b.ts for b in daily]; d_opens = [b.open for b in daily]
    h4_ts = [b.ts for b in four_hour]; h4_opens = [b.open for b in four_hour]
    h1_ts = [b.ts for b in one_hour]; h1_opens = [b.open for b in one_hour]

    def _lookup(tsi: list[datetime], opens: list[float], ts: datetime) -> float | None:
        i = bisect.bisect_right(tsi, ts)
        return opens[i - 1] if i > 0 else None

    def provider(ts: datetime) -> HigherTfOpens | None:
        d = _lookup(d_ts, d_opens, ts)
        h4 = _lookup(h4_ts, h4_opens, ts)
        h1 = _lookup(h1_ts, h1_opens, ts)
        if d is None or h4 is None or h1 is None:
            return None
        return HigherTfOpens(daily=d, four_hour=h4, one_hour=h1)

    return provider


def build_config(timeframe: str, rr: float, atr: float, equity: float) -> Config:
    """Build a Config suitable for crypto: 24/7 session, no entry window, no force-flat."""
    cfg_dict = {
        "mode": "paper",
        "account": {
            "sim_equity_usd": equity,
            "risk_pct_per_trade": 0.005,
            "daily_loss_cap_pct": 0.02,
            "max_concurrent": 1,
            "max_trades_per_day": 10,
        },
        "strategy": {
            "timeframe": timeframe,
            "patterns": ["2-2", "3-1-2", "rev-strat"],
            "sides": ["long", "short"],
            "min_rr": rr,
            "min_bar_atr_mult": atr,
            "ftfc_timeframes": ["1D", "4H", "1H"],
            "slippage_per_share": 0.0,
        },
        "watchlist": ["BTC_USD"],
        # Crypto trades 24/7, but the backtest engine uses this window to gate entries.
        # For crypto we open it to cover the full day (UTC) so it's effectively off.
        "session": {"entry_window_et": ["00:00", "23:59"], "force_flat_et": "23:59"},
        "blackouts": [],
        "paths": {"db": "data/trades.db", "events_log": "data/events.jsonl", "reports_dir": "reports"},
    }
    path = Path("config/tuning/btc.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(cfg_dict, default_flow_style=False, sort_keys=False))
    return Config.from_yaml(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC TheStrat backtest")
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD")
    parser.add_argument("--timeframe", type=str, default="10Min")
    parser.add_argument("--rr", type=float, default=4.0)
    parser.add_argument("--atr", type=float, default=0.25)
    parser.add_argument("--equity", type=float, default=50000)
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=730)

    print(f"BTC/USD TheStrat backtest {start} → {end}")
    print(f"Timeframe: {args.timeframe}  |  R:R: {args.rr}  |  ATR mult: {args.atr}\n")

    print("Caching 1Min BTC bars (missing months only)…")
    ensure_cached(start - timedelta(days=30), end)
    print()

    df_signal = load_range_df(start, end)
    df_context = load_range_df(start - timedelta(days=30), end)
    print(f"Loaded signal range: {len(df_signal):,} 1m bars")
    print(f"Loaded context range: {len(df_context):,} 1m bars\n")

    if df_signal.empty:
        print("No BTC data available. Exiting.")
        return

    stf_min = parse_tf_minutes(args.timeframe)
    signal_bars = df_to_bars(aggregate_df_24x7(df_signal, stf_min))
    daily = df_to_bars(aggregate_df_24x7(df_context, 1440))   # 24h = 1440 minutes
    hourly = df_to_bars(aggregate_df_24x7(df_context, 60))
    fourhour = df_to_bars(aggregate_df_24x7(df_context, 240))
    print(
        f"Aggregated: signal({args.timeframe})={len(signal_bars):,}  "
        f"1D={len(daily):,}  4H={len(fourhour):,}  1H={len(hourly):,}"
    )

    if len(signal_bars) < 50:
        print("Not enough signal bars for a meaningful backtest. Exiting.")
        return

    provider = build_btc_opens_provider(daily, fourhour, hourly)
    cfg = build_config(args.timeframe, args.rr, args.atr, args.equity)
    result = run_backtest("BTC_USD", signal_bars, provider, cfg, starting_equity=args.equity)
    print()
    print(result.summary())

    # Exit reason breakdown
    reasons = {"stop": 0, "target": 0, "eod": 0}
    pnl_by_reason = {"stop": 0.0, "target": 0.0, "eod": 0.0}
    for t in result.trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        pnl_by_reason[t.exit_reason] = pnl_by_reason.get(t.exit_reason, 0.0) + t.realized_pnl
    print("\nExit reason breakdown:")
    for r in ("stop", "target", "eod"):
        n = reasons.get(r, 0)
        pnl = pnl_by_reason.get(r, 0.0)
        pct = n / result.total_trades * 100 if result.total_trades else 0
        print(f"  {r:<6} {n:>5} ({pct:>4.1f}%)  PnL=${pnl:>12,.2f}")


if __name__ == "__main__":
    main()
