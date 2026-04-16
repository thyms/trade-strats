"""Parquet-based local cache for historical bar data.

Bars fetched from Alpaca are immutable once the market closes, so we store
them on disk and skip the API call on subsequent requests for the same
(symbol, timeframe, start, end) tuple.

Layout::

    data/bars/<SYMBOL>/<TIMEFRAME>/<YYYYMMDD>_<YYYYMMDD>.parquet
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from trade_strats.aggregation import TimedBar


def _parquet_path(
    cache_dir: Path, symbol: str, timeframe: str, start: datetime, end: datetime
) -> Path:
    s = start.strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    return cache_dir / symbol.upper() / timeframe / f"{s}_{e}.parquet"


def load(
    cache_dir: Path, symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[TimedBar] | None:
    """Return cached bars or ``None`` if the cache file does not exist."""
    path = _parquet_path(cache_dir, symbol, timeframe, start, end)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
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


def save(
    cache_dir: Path,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    bars: list[TimedBar],
) -> Path:
    """Persist bars to a Parquet file and return the path written."""
    path = _parquet_path(cache_dir, symbol, timeframe, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
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
    df.to_parquet(path, index=False)
    return path
