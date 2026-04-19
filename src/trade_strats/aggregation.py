from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trade_strats.strategy.labeler import Bar

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class TimedBar:
    ts: datetime  # timezone-aware; represents the start of the bar period
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low}")
        if not self.low <= self.open <= self.high:
            raise ValueError(f"open {self.open} outside [{self.low}, {self.high}]")
        if not self.low <= self.close <= self.high:
            raise ValueError(f"close {self.close} outside [{self.low}, {self.high}]")
        if self.volume < 0:
            raise ValueError(f"volume must be >= 0, got {self.volume}")

    def to_strategy_bar(self) -> Bar:
        return Bar(open=self.open, high=self.high, low=self.low, close=self.close)


BucketFn = Callable[[datetime], datetime]


def _trading_day_open(t: datetime) -> datetime:
    """09:30 ET of the calendar day containing `t` (interpreted in ET)."""
    local = t.astimezone(ET)
    return local.replace(hour=9, minute=30, second=0, microsecond=0)


def _bucket_by_minutes(t: datetime, minutes: int) -> datetime:
    day_open = _trading_day_open(t)
    local = t.astimezone(ET)
    delta_min = int((local - day_open).total_seconds() // 60)
    bucket_min = (delta_min // minutes) * minutes
    return day_open + timedelta(minutes=bucket_min)


def bucket_minutes(n: int) -> BucketFn:
    """Return a bucket function for an arbitrary N-minute interval."""
    if n <= 0:
        raise ValueError(f"bucket size must be positive, got {n}")

    def _bucket(t: datetime) -> datetime:
        return _bucket_by_minutes(t, n)

    return _bucket


def parse_tf_minutes(tf: str) -> int:
    """Extract the minute count from a timeframe string like '15Min', '1H', '4H', '1D'.

    Returns the equivalent number of intraday minutes (1D → 390 = 6.5h RTH).
    """
    if tf.endswith("Min"):
        return int(tf[: -len("Min")])
    if tf.endswith("H"):
        return int(tf[: -len("H")]) * 60
    if tf == "1D":
        return 390  # 6.5 hours of RTH
    raise ValueError(f"cannot parse timeframe: {tf}")


# Convenience aliases used across the codebase
bucket_15m = bucket_minutes(15)
bucket_1h = bucket_minutes(60)
bucket_4h = bucket_minutes(240)


def bucket_1d(t: datetime) -> datetime:
    return _trading_day_open(t)


def is_rth(t: datetime) -> bool:
    local = t.astimezone(ET).time()
    return RTH_OPEN <= local < RTH_CLOSE


@dataclass(slots=True)
class _BucketState:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @classmethod
    def from_bar(cls, bar: TimedBar, bucket_ts: datetime) -> "_BucketState":
        return cls(
            ts=bucket_ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )

    def absorb(self, bar: TimedBar) -> None:
        if bar.high > self.high:
            self.high = bar.high
        if bar.low < self.low:
            self.low = bar.low
        self.close = bar.close
        self.volume += bar.volume

    def to_bar(self) -> TimedBar:
        return TimedBar(
            ts=self.ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class Aggregator:
    """Rolls 1-minute bars into larger-timeframe bars by a bucket function.

    `ingest(bar)` returns a list of completed bars (typically 0 or 1; 0 when the
    bucket is still accumulating, 1 when a new bucket starts). `flush()` emits
    the in-progress bar (for use at session close or shutdown).
    """

    def __init__(self, bucket_fn: BucketFn) -> None:
        self._bucket_fn = bucket_fn
        self._state: _BucketState | None = None

    @property
    def current_open(self) -> float | None:
        """Open price of the in-progress bucket; None if no bucket is active."""
        return self._state.open if self._state is not None else None

    @property
    def current_bucket_ts(self) -> datetime | None:
        return self._state.ts if self._state is not None else None

    def ingest(self, bar: TimedBar) -> list[TimedBar]:
        bucket_ts = self._bucket_fn(bar.ts)
        if self._state is None:
            self._state = _BucketState.from_bar(bar, bucket_ts)
            return []
        if bar.ts < self._state.ts:
            raise ValueError(
                f"out-of-order bar: {bar.ts.isoformat()} < current bucket {self._state.ts.isoformat()}"
            )
        if bucket_ts == self._state.ts:
            self._state.absorb(bar)
            return []
        emitted = self._state.to_bar()
        self._state = _BucketState.from_bar(bar, bucket_ts)
        return [emitted]

    def flush(self) -> list[TimedBar]:
        if self._state is None:
            return []
        emitted = self._state.to_bar()
        self._state = None
        return [emitted]


def aggregate(bars: Iterable[TimedBar], bucket_fn: BucketFn) -> list[TimedBar]:
    """Run bars through a fresh Aggregator and flush at the end.

    Pre-/post-market bars are filtered out automatically so callers
    don't need to worry about non-RTH data from the API.
    """
    agg = Aggregator(bucket_fn)
    out: list[TimedBar] = []
    for bar in bars:
        if not is_rth(bar.ts):
            continue
        out.extend(agg.ingest(bar))
    out.extend(agg.flush())
    return out


# ---------------------------------------------------------------------------
# Fast pandas-native aggregation for batch processing
# ---------------------------------------------------------------------------


def _bucket_key_minutes(ts_series: pd.Series, minutes: int) -> pd.Series:  # type: ignore[type-arg]
    """Assign an integer bucket key to each bar for fast groupby.

    Returns an int Series where each value encodes (date_ordinal, bucket_offset).
    """
    et = ts_series.dt.tz_convert(ET)
    ymd = et.dt.year * 10000 + et.dt.month * 100 + et.dt.day
    mins_since_open = (et.dt.hour - 9) * 60 + (et.dt.minute - 30)
    bucket_offset = (mins_since_open // minutes) * minutes
    return ymd * 1000 + bucket_offset


def aggregate_df(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate a 1Min DataFrame to N-minute bars using pandas groupby.

    Input df must have columns: ts, open, high, low, close, volume.
    The ts column must be timezone-aware. Non-RTH bars are filtered.
    Returns a DataFrame with the same columns, sorted by ts.
    """
    if df.empty:
        return df

    # Ensure ts is datetime
    if not pd.api.types.is_datetime64_any_dtype(df["ts"]):
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

    # Convert to ET once and reuse
    et_all = df["ts"].dt.tz_convert(ET)

    # Filter to RTH only (vectorized via hour/minute math)
    minutes_of_day = et_all.dt.hour * 60 + et_all.dt.minute
    rth_open_min = 9 * 60 + 30   # 09:30
    rth_close_min = 16 * 60       # 16:00
    mask = (minutes_of_day >= rth_open_min) & (minutes_of_day < rth_close_min)
    df = df[mask]

    if df.empty:
        return df

    # Reuse the ET-converted series (filtered)
    et = et_all[mask]
    ymd = et.dt.year * 10000 + et.dt.month * 100 + et.dt.day
    if minutes >= 390:
        bucket_key = ymd
    else:
        mins_since_open = (et.dt.hour - 9) * 60 + (et.dt.minute - 30)
        bucket_offset = (mins_since_open // minutes) * minutes
        bucket_key = ymd * 1000 + bucket_offset

    grouped = df.groupby(bucket_key).agg(
        ts=("ts", "first"),  # first bar's timestamp as the bucket ts
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)

    # Reconstruct proper bucket-start timestamps from the first bar in each group
    if minutes < 390:
        get = grouped["ts"].dt.tz_convert(ET)
        mins_since_open = (get.dt.hour - 9) * 60 + (get.dt.minute - 30)
        aligned_offset = (mins_since_open // minutes) * minutes
        delta = aligned_offset - mins_since_open  # always <= 0
        grouped["ts"] = grouped["ts"] + pd.to_timedelta(delta, unit="min")

    return grouped.sort_values("ts").reset_index(drop=True)


def aggregate_df_24x7(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """24/7 aggregation for crypto. Anchors on UTC midnight, no RTH filter.

    Used for assets that trade continuously (e.g. BTC). The "daily" bucket
    (minutes >= 1440) groups on the UTC calendar day.
    """
    if df.empty:
        return df

    if not pd.api.types.is_datetime64_any_dtype(df["ts"]):
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

    utc = df["ts"].dt.tz_convert("UTC")
    ymd = utc.dt.year * 10000 + utc.dt.month * 100 + utc.dt.day

    if minutes >= 1440:
        bucket_key = ymd
    else:
        mins_of_day = utc.dt.hour * 60 + utc.dt.minute
        bucket_offset = (mins_of_day // minutes) * minutes
        bucket_key = ymd * 10000 + bucket_offset

    grouped = df.groupby(bucket_key).agg(
        ts=("ts", "first"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index(drop=True)

    if minutes < 1440:
        utc_g = grouped["ts"].dt.tz_convert("UTC")
        mins_of_day = utc_g.dt.hour * 60 + utc_g.dt.minute
        aligned = (mins_of_day // minutes) * minutes
        delta = aligned - mins_of_day
        grouped["ts"] = grouped["ts"] + pd.to_timedelta(delta, unit="min")
    else:
        # Snap daily bars to UTC midnight
        utc_g = grouped["ts"].dt.tz_convert("UTC")
        midnight_delta = -(utc_g.dt.hour * 60 + utc_g.dt.minute)
        grouped["ts"] = grouped["ts"] + pd.to_timedelta(midnight_delta, unit="min")

    return grouped.sort_values("ts").reset_index(drop=True)


def df_to_bars(df: pd.DataFrame) -> list[TimedBar]:
    """Convert a DataFrame with ts/open/high/low/close/volume to TimedBar list."""
    if df.empty:
        return []
    ts_list = df["ts"].dt.to_pydatetime().tolist()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    vols = df["volume"].to_numpy()
    return [
        TimedBar(
            ts=ts_list[i],
            open=float(opens[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            close=float(closes[i]),
            volume=int(vols[i]),
        )
        for i in range(len(df))
    ]
