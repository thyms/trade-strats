from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trade_strats.aggregation import (
    ET,
    Aggregator,
    TimedBar,
    aggregate,
    bucket_1d,
    bucket_1h,
    bucket_4h,
    bucket_15m,
    is_rth,
)

# --- Helpers ----------------------------------------------------------------


def et(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def bar(
    ts: datetime,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: int = 1000,
) -> TimedBar:
    return TimedBar(ts=ts, open=open_, high=high, low=low, close=close, volume=volume)


# --- TimedBar validation ----------------------------------------------------


def test_timed_bar_rejects_naive_ts() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TimedBar(
            ts=datetime(2026, 4, 15, 13, 30),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=0,
        )


def test_timed_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="volume"):
        TimedBar(ts=et(2026, 4, 15, 9, 30), open=100, high=101, low=99, close=100, volume=-1)


def test_timed_bar_to_strategy_bar_strips_timestamp() -> None:
    tb = bar(et(2026, 4, 15, 9, 30))
    b = tb.to_strategy_bar()
    assert b.open == tb.open
    assert b.high == tb.high
    assert b.low == tb.low
    assert b.close == tb.close


# --- is_rth -----------------------------------------------------------------


def test_is_rth_boundaries() -> None:
    assert is_rth(et(2026, 4, 15, 9, 30)) is True
    assert is_rth(et(2026, 4, 15, 9, 29)) is False
    assert is_rth(et(2026, 4, 15, 15, 59)) is True
    assert is_rth(et(2026, 4, 15, 16, 0)) is False


def test_is_rth_honors_timezone_input() -> None:
    # 13:30 UTC = 09:30 ET in EDT (April is EDT)
    ts_utc = datetime(2026, 4, 15, 13, 30, tzinfo=UTC)
    assert is_rth(ts_utc) is True


# --- Bucket functions: 15m --------------------------------------------------


def test_bucket_15m_at_open_is_open() -> None:
    assert bucket_15m(et(2026, 4, 15, 9, 30)) == et(2026, 4, 15, 9, 30)


def test_bucket_15m_inside_first_bucket() -> None:
    assert bucket_15m(et(2026, 4, 15, 9, 44)) == et(2026, 4, 15, 9, 30)


def test_bucket_15m_second_bucket_boundary() -> None:
    assert bucket_15m(et(2026, 4, 15, 9, 45)) == et(2026, 4, 15, 9, 45)


def test_bucket_15m_last_bucket() -> None:
    assert bucket_15m(et(2026, 4, 15, 15, 59)) == et(2026, 4, 15, 15, 45)


def test_bucket_15m_accepts_utc_input() -> None:
    # 13:45 UTC = 09:45 ET in EDT
    ts_utc = datetime(2026, 4, 15, 13, 45, tzinfo=UTC)
    assert bucket_15m(ts_utc) == et(2026, 4, 15, 9, 45)


# --- Bucket functions: 1h ---------------------------------------------------


def test_bucket_1h_inside_first_hour() -> None:
    assert bucket_1h(et(2026, 4, 15, 10, 29)) == et(2026, 4, 15, 9, 30)


def test_bucket_1h_second_hour_boundary() -> None:
    assert bucket_1h(et(2026, 4, 15, 10, 30)) == et(2026, 4, 15, 10, 30)


def test_bucket_1h_last_partial_hour() -> None:
    # 15:30-16:00 is a 30-min partial bucket; 15:59 belongs to it.
    assert bucket_1h(et(2026, 4, 15, 15, 59)) == et(2026, 4, 15, 15, 30)


# --- Bucket functions: 4h ---------------------------------------------------


def test_bucket_4h_first_four_hours() -> None:
    assert bucket_4h(et(2026, 4, 15, 13, 29)) == et(2026, 4, 15, 9, 30)


def test_bucket_4h_second_bucket_starts_at_1330() -> None:
    assert bucket_4h(et(2026, 4, 15, 13, 30)) == et(2026, 4, 15, 13, 30)


def test_bucket_4h_second_bucket_through_close() -> None:
    assert bucket_4h(et(2026, 4, 15, 15, 59)) == et(2026, 4, 15, 13, 30)


# --- Bucket functions: 1d ---------------------------------------------------


def test_bucket_1d_anchor_is_market_open() -> None:
    assert bucket_1d(et(2026, 4, 15, 12, 0)) == et(2026, 4, 15, 9, 30)
    assert bucket_1d(et(2026, 4, 15, 9, 30)) == et(2026, 4, 15, 9, 30)
    assert bucket_1d(et(2026, 4, 15, 15, 59)) == et(2026, 4, 15, 9, 30)


def test_bucket_1d_different_days_give_different_buckets() -> None:
    assert bucket_1d(et(2026, 4, 15, 10, 0)) != bucket_1d(et(2026, 4, 16, 10, 0))


# --- DST transition --------------------------------------------------------


def test_bucket_15m_across_dst_spring_forward() -> None:
    # 2026-03-08 is the second Sunday in March → DST begins.
    # First trading day post-DST is 2026-03-09 (Monday). UTC offset shifts from
    # -05:00 (EST) to -04:00 (EDT). Bucketing in ET must be unaffected.
    assert bucket_15m(et(2026, 3, 9, 9, 45)) == et(2026, 3, 9, 9, 45)
    assert bucket_15m(et(2026, 3, 9, 15, 45)) == et(2026, 3, 9, 15, 45)


# --- Aggregator: 15m via 1m bars -------------------------------------------


def _minute_bars(start: datetime, n: int, *, open_: float = 100.0) -> list[TimedBar]:
    """n consecutive 1m bars starting at `start`. Prices drift up by 0.10 per minute."""
    out: list[TimedBar] = []
    for i in range(n):
        p = open_ + 0.10 * i
        out.append(
            TimedBar(
                ts=start + timedelta(minutes=i),
                open=p,
                high=p + 0.05,
                low=p - 0.05,
                close=p + 0.02,
                volume=100,
            )
        )
    return out


def test_fifteen_one_minute_bars_produce_one_15m_bar_via_flush() -> None:
    bars = _minute_bars(et(2026, 4, 15, 9, 30), 15, open_=100.0)
    out = aggregate(bars, bucket_15m)
    assert len(out) == 1
    agg_bar = out[0]
    assert agg_bar.ts == et(2026, 4, 15, 9, 30)
    assert agg_bar.open == pytest.approx(100.0)
    # highs are 100.05, 100.15, ..., 101.45 → max 101.45
    assert agg_bar.high == pytest.approx(101.45)
    # lows are 99.95, 100.05, ..., 101.35 → min 99.95
    assert agg_bar.low == pytest.approx(99.95)
    # last close: price[14]=101.40, close=101.42
    assert agg_bar.close == pytest.approx(101.42)
    assert agg_bar.volume == 1500


def test_sixteenth_bar_triggers_emission_of_first_bucket() -> None:
    first_bucket = _minute_bars(et(2026, 4, 15, 9, 30), 15, open_=100.0)
    next_bucket_bar = _minute_bars(et(2026, 4, 15, 9, 45), 1, open_=110.0)[0]
    agg = Aggregator(bucket_15m)
    emitted: list[TimedBar] = []
    for b in first_bucket:
        emitted.extend(agg.ingest(b))
    assert emitted == []  # nothing yet
    emitted.extend(agg.ingest(next_bucket_bar))
    assert len(emitted) == 1
    assert emitted[0].ts == et(2026, 4, 15, 9, 30)
    assert emitted[0].open == pytest.approx(100.0)
    assert emitted[0].close == pytest.approx(101.42)


def test_current_open_reflects_in_progress_bucket() -> None:
    agg = Aggregator(bucket_15m)
    assert agg.current_open is None
    agg.ingest(bar(et(2026, 4, 15, 9, 30), open_=200.0, high=200.5, low=199.5, close=200.2))
    assert agg.current_open == 200.0
    agg.ingest(bar(et(2026, 4, 15, 9, 31), open_=201.0, high=202.0, low=200.5, close=201.5))
    # Still in the same bucket, but open shouldn't change:
    assert agg.current_open == 200.0


def test_current_bucket_ts_rolls_over() -> None:
    agg = Aggregator(bucket_15m)
    agg.ingest(bar(et(2026, 4, 15, 9, 30)))
    assert agg.current_bucket_ts == et(2026, 4, 15, 9, 30)
    agg.ingest(bar(et(2026, 4, 15, 9, 45)))
    assert agg.current_bucket_ts == et(2026, 4, 15, 9, 45)


def test_flush_emits_in_progress_bar_and_clears_state() -> None:
    agg = Aggregator(bucket_15m)
    agg.ingest(bar(et(2026, 4, 15, 9, 30), open_=50.0, high=51.0, low=49.0, close=50.5))
    out = agg.flush()
    assert len(out) == 1
    assert out[0].open == 50.0
    # Flush again: empty
    assert agg.flush() == []
    assert agg.current_open is None


def test_flush_on_empty_aggregator_returns_empty() -> None:
    assert Aggregator(bucket_15m).flush() == []


def test_out_of_order_bar_raises() -> None:
    agg = Aggregator(bucket_15m)
    agg.ingest(bar(et(2026, 4, 15, 10, 0)))
    with pytest.raises(ValueError, match="out-of-order"):
        agg.ingest(bar(et(2026, 4, 15, 9, 30)))


# --- Aggregator: 1h / 4h / 1d via aggregate() -----------------------------


def test_aggregate_1h_from_60_minute_bars() -> None:
    bars = _minute_bars(et(2026, 4, 15, 9, 30), 60, open_=100.0)
    out = aggregate(bars, bucket_1h)
    assert len(out) == 1
    assert out[0].ts == et(2026, 4, 15, 9, 30)


def test_aggregate_1h_across_two_hour_buckets() -> None:
    first = _minute_bars(et(2026, 4, 15, 9, 30), 60, open_=100.0)
    second = _minute_bars(et(2026, 4, 15, 10, 30), 30, open_=110.0)
    out = aggregate(first + second, bucket_1h)
    assert len(out) == 2
    assert out[0].ts == et(2026, 4, 15, 9, 30)
    assert out[1].ts == et(2026, 4, 15, 10, 30)
    assert out[1].volume == 3000


def test_aggregate_4h_single_full_bucket() -> None:
    # 4H bucket 09:30-13:30 is 240 minutes
    bars = _minute_bars(et(2026, 4, 15, 9, 30), 240, open_=100.0)
    out = aggregate(bars, bucket_4h)
    assert len(out) == 1
    assert out[0].ts == et(2026, 4, 15, 9, 30)
    assert out[0].volume == 24_000


def test_aggregate_4h_two_buckets_across_session() -> None:
    first = _minute_bars(et(2026, 4, 15, 9, 30), 240, open_=100.0)
    # Second bucket 13:30-16:00 is 150 minutes (partial)
    second = _minute_bars(et(2026, 4, 15, 13, 30), 150, open_=120.0)
    out = aggregate(first + second, bucket_4h)
    assert len(out) == 2
    assert out[0].ts == et(2026, 4, 15, 9, 30)
    assert out[1].ts == et(2026, 4, 15, 13, 30)
    assert out[1].volume == 15_000  # partial bucket still flushed


def test_aggregate_1d_single_day() -> None:
    # full RTH = 390 minutes
    bars = _minute_bars(et(2026, 4, 15, 9, 30), 390, open_=100.0)
    out = aggregate(bars, bucket_1d)
    assert len(out) == 1
    assert out[0].ts == et(2026, 4, 15, 9, 30)


def test_aggregate_1d_two_days() -> None:
    day1 = _minute_bars(et(2026, 4, 15, 9, 30), 390, open_=100.0)
    day2 = _minute_bars(et(2026, 4, 16, 9, 30), 390, open_=110.0)
    out = aggregate(day1 + day2, bucket_1d)
    assert len(out) == 2
    assert out[0].ts == et(2026, 4, 15, 9, 30)
    assert out[1].ts == et(2026, 4, 16, 9, 30)


# --- Custom bucket_fn injection -------------------------------------------


def test_aggregator_accepts_arbitrary_bucket_fn() -> None:
    # A trivial bucket_fn that puts every bar in its own bucket.
    def per_bar(t: datetime) -> datetime:
        return t

    bars = _minute_bars(et(2026, 4, 15, 9, 30), 3, open_=100.0)
    out = aggregate(bars, per_bar)
    assert len(out) == 3


# --- Timezone parity -------------------------------------------------------


def test_bucket_functions_return_tz_aware_datetimes() -> None:
    assert bucket_15m(et(2026, 4, 15, 10, 0)).tzinfo is not None
    assert bucket_1h(et(2026, 4, 15, 10, 0)).tzinfo is not None
    assert bucket_4h(et(2026, 4, 15, 10, 0)).tzinfo is not None
    assert bucket_1d(et(2026, 4, 15, 10, 0)).tzinfo is not None


def test_bucket_functions_normalize_across_input_tz() -> None:
    utc_ts = datetime(2026, 4, 15, 14, 0, tzinfo=UTC)  # 10:00 ET in EDT
    tokyo_ts = utc_ts.astimezone(ZoneInfo("Asia/Tokyo"))
    assert bucket_15m(utc_ts) == bucket_15m(tokyo_ts)
    assert bucket_1h(utc_ts) == bucket_1h(tokyo_ts)
