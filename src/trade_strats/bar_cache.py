"""Parquet-based local cache for 1-minute bar data.

Only 1Min bars are stored on disk — one file per symbol per trading day.
Higher timeframes are aggregated on the fly from the cached 1m bars,
so changing the strategy timeframe never requires re-fetching.

Layout::

    data/bars/<SYMBOL>/1Min/<YYYYMMDD>/data.parquet
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trade_strats.aggregation import ET, TimedBar


def _day_dir(cache_dir: Path, symbol: str, trading_date: date) -> Path:
    return cache_dir / symbol.upper() / "1Min" / trading_date.strftime("%Y%m%d")


def _day_path(cache_dir: Path, symbol: str, trading_date: date) -> Path:
    return _day_dir(cache_dir, symbol, trading_date) / "data.parquet"


def _trading_date(bar: TimedBar) -> date:
    """Return the ET calendar date for a bar (its trading day)."""
    return bar.ts.astimezone(ET).date()


def _bars_from_df(df: pd.DataFrame) -> list[TimedBar]:
    return [
        TimedBar(
            ts=row["ts"].to_pydatetime(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        )
        for _, row in df.iterrows()
    ]


def _df_from_bars(bars: list[TimedBar]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
    )


def _calendar_dates(start: date, end: date) -> list[date]:
    """Return every calendar date in [start, end] inclusive."""
    days: list[date] = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def cached_dates(cache_dir: Path, symbol: str, start: date, end: date) -> set[date]:
    """Return the set of trading dates that already have cached 1m bars."""
    found: set[date] = set()
    for d in _calendar_dates(start, end):
        if _day_path(cache_dir, symbol, d).exists():
            found.add(d)
    return found


def load_days(
    cache_dir: Path, symbol: str, start: date, end: date
) -> list[TimedBar]:
    """Load and concatenate all cached 1m bars for trading days in [start, end]."""
    bars: list[TimedBar] = []
    for d in _calendar_dates(start, end):
        path = _day_path(cache_dir, symbol, d)
        if path.exists():
            df = pd.read_parquet(path)
            bars.extend(_bars_from_df(df))
    return bars


def save_bars(cache_dir: Path, symbol: str, bars: list[TimedBar]) -> list[Path]:
    """Split bars by trading day and write one parquet per day. Returns paths written."""
    by_day: dict[date, list[TimedBar]] = defaultdict(list)
    for b in bars:
        by_day[_trading_date(b)].append(b)

    paths: list[Path] = []
    for trading_day, day_bars in sorted(by_day.items()):
        path = _day_path(cache_dir, symbol, trading_day)
        path.parent.mkdir(parents=True, exist_ok=True)
        _df_from_bars(day_bars).to_parquet(path, index=False)
        paths.append(path)
    return paths
