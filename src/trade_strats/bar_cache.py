"""Parquet-based local cache for 1-minute bar data.

Only 1Min bars are stored on disk — one file per symbol per calendar month.
Higher timeframes are aggregated on the fly from the cached 1m bars,
so changing the strategy timeframe never requires re-fetching.

Layout::

    data/bars/<SYMBOL>/1Min/<YYYY-MM>.parquet
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from trade_strats.aggregation import ET, TimedBar


def _month_key(d: date) -> str:
    """Return 'YYYY-MM' for a date."""
    return f"{d.year:04d}-{d.month:02d}"


def _month_path(cache_dir: Path, symbol: str, month_key: str) -> Path:
    return cache_dir / symbol.upper() / "1Min" / f"{month_key}.parquet"


def _months_in_range(start: date, end: date) -> list[str]:
    """Return every distinct 'YYYY-MM' that overlaps [start, end]."""
    months: list[str] = []
    d = start.replace(day=1)
    while d <= end:
        months.append(_month_key(d))
        # Advance to the first day of the next month.
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)
    return months


def _bars_from_df(df: pd.DataFrame) -> list[TimedBar]:
    """Convert a DataFrame to a list of TimedBar using vectorized column access."""
    ts_list = df["ts"].dt.to_pydatetime().tolist()
    open_list = df["open"].to_numpy()
    high_list = df["high"].to_numpy()
    low_list = df["low"].to_numpy()
    close_list = df["close"].to_numpy()
    vol_list = df["volume"].to_numpy()
    return [
        TimedBar(
            ts=ts_list[i],
            open=float(open_list[i]),
            high=float(high_list[i]),
            low=float(low_list[i]),
            close=float(close_list[i]),
            volume=int(vol_list[i]),
        )
        for i in range(len(df))
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


def cached_months(cache_dir: Path, symbol: str, start: date, end: date) -> set[str]:
    """Return the set of month keys that already have cached 1m bars."""
    found: set[str] = set()
    for mk in _months_in_range(start, end):
        if _month_path(cache_dir, symbol, mk).exists():
            found.add(mk)
    return found


def missing_months(cache_dir: Path, symbol: str, start: date, end: date) -> list[str]:
    """Return month keys in the range that do NOT have a cached parquet file."""
    already = cached_months(cache_dir, symbol, start, end)
    return [mk for mk in _months_in_range(start, end) if mk not in already]


def load_range_df(
    cache_dir: Path, symbol: str, start: date, end: date
) -> pd.DataFrame:
    """Load and concatenate cached 1m bars as a DataFrame.

    Bars outside [start, end] are trimmed so callers get exactly the range
    they asked for.
    """
    dfs: list[pd.DataFrame] = []
    for mk in _months_in_range(start, end):
        path = _month_path(cache_dir, symbol, mk)
        if path.exists():
            dfs.append(pd.read_parquet(path))
    if not dfs:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.concat(dfs, ignore_index=True)
    # Trim to requested range (monthly files may contain bars outside it).
    start_dt = pd.Timestamp(start, tz=ET)
    end_dt = pd.Timestamp(end, tz=ET) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    mask = (df["ts"] >= start_dt) & (df["ts"] <= end_dt)
    return df[mask].reset_index(drop=True)


def load_range(
    cache_dir: Path, symbol: str, start: date, end: date
) -> list[TimedBar]:
    """Load cached 1m bars as TimedBar list. Prefer load_range_df for batch work."""
    df = load_range_df(cache_dir, symbol, start, end)
    return _bars_from_df(df)


def save_month(
    cache_dir: Path, symbol: str, month_key: str, bars: list[TimedBar]
) -> Path:
    """Write bars for a single month to its parquet file."""
    path = _month_path(cache_dir, symbol, month_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    _df_from_bars(bars).to_parquet(path, index=False)
    return path


def month_date_range(month_key: str) -> tuple[date, date]:
    """Return (first_day, last_day) for a 'YYYY-MM' key."""
    year, month = (int(x) for x in month_key.split("-"))
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return first, last
