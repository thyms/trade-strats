from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

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


def bucket_15m(t: datetime) -> datetime:
    return _bucket_by_minutes(t, 15)


def bucket_1h(t: datetime) -> datetime:
    return _bucket_by_minutes(t, 60)


def bucket_4h(t: datetime) -> datetime:
    return _bucket_by_minutes(t, 240)


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
    """Run bars through a fresh Aggregator and flush at the end."""
    agg = Aggregator(bucket_fn)
    out: list[TimedBar] = []
    for bar in bars:
        out.extend(agg.ingest(bar))
    out.extend(agg.flush())
    return out
