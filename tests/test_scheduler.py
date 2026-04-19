from datetime import date, datetime, time

from trade_strats.aggregation import ET
from trade_strats.scheduler import (
    is_trading_day,
    next_session_start,
    next_trading_day,
)


def test_is_trading_day_weekday() -> None:
    # Tuesday 2026-04-21 — regular weekday
    assert is_trading_day(date(2026, 4, 21)) is True


def test_is_trading_day_weekend() -> None:
    assert is_trading_day(date(2026, 4, 18)) is False  # Sat
    assert is_trading_day(date(2026, 4, 19)) is False  # Sun


def test_is_trading_day_holiday() -> None:
    assert is_trading_day(date(2026, 7, 3)) is False  # July 4 observed
    assert is_trading_day(date(2026, 12, 25)) is False  # Christmas


def test_next_trading_day_skips_weekend() -> None:
    # Friday -> Monday
    assert next_trading_day(date(2026, 4, 17)) == date(2026, 4, 20)


def test_next_trading_day_skips_holiday() -> None:
    # Day before Christmas 2026 (a Friday)
    # 2026-12-24 Thu -> 2026-12-25 Christmas (Fri, holiday) -> 2026-12-28 Mon
    assert next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)


def test_next_session_start_before_open_same_day() -> None:
    # Tuesday 2026-04-21 at 08:00 ET -> same day 09:25 ET
    now = datetime(2026, 4, 21, 8, 0, tzinfo=ET)
    target = next_session_start(now)
    assert target == datetime(2026, 4, 21, 9, 25, tzinfo=ET)


def test_next_session_start_after_close_same_day() -> None:
    # Tuesday 2026-04-21 at 17:00 ET -> Wednesday 09:25 ET
    now = datetime(2026, 4, 21, 17, 0, tzinfo=ET)
    target = next_session_start(now)
    assert target == datetime(2026, 4, 22, 9, 25, tzinfo=ET)


def test_next_session_start_on_friday_afternoon() -> None:
    # Friday 2026-04-17 at 17:00 ET -> Monday 2026-04-20 09:25 ET
    now = datetime(2026, 4, 17, 17, 0, tzinfo=ET)
    target = next_session_start(now)
    assert target == datetime(2026, 4, 20, 9, 25, tzinfo=ET)


def test_next_session_start_custom_time() -> None:
    now = datetime(2026, 4, 21, 5, 0, tzinfo=ET)
    target = next_session_start(now, start_time_et=time(9, 0))
    assert target == datetime(2026, 4, 21, 9, 0, tzinfo=ET)
