"""Multi-day scheduler: wakes at market open, runs a session, sleeps until next.

Designed to be left running for weeks in a terminal / tmux / launchd.
Handles weekends, US market holidays, and transient crashes by logging
and continuing to the next trading day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from trade_strats.aggregation import ET
from trade_strats.config import Config
from trade_strats.orchestrator import run_session

logger = logging.getLogger(__name__)

# US equity market holidays (partial — extend as needed, or wire up a
# proper calendar lib like `pandas_market_calendars` later).
US_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 1),    # New Year
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}
US_HOLIDAYS_2027: set[date] = {
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
}
US_HOLIDAYS: set[date] = US_HOLIDAYS_2026 | US_HOLIDAYS_2027


def is_trading_day(d: date) -> bool:
    """True if US equity markets are open on `d` (weekday and not a known holiday)."""
    if d.weekday() >= 5:  # Sat/Sun
        return False
    return d not in US_HOLIDAYS


def next_trading_day(from_d: date) -> date:
    """Return the next trading day strictly after `from_d`."""
    d = from_d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def next_session_start(now: datetime, start_time_et: time = time(9, 25)) -> datetime:
    """Next market-open datetime (ET-aware) at or after `now`.

    Uses 09:25 ET by default so the session spins up 5 minutes before the
    09:30 open; WS stream has time to subscribe before the first bar.
    """
    now_et = now.astimezone(ET)
    today = now_et.date()
    today_start = datetime.combine(today, start_time_et, tzinfo=ET)

    if is_trading_day(today) and now_et < today_start:
        return today_start
    next_d = next_trading_day(today if now_et >= today_start else today - timedelta(days=1))
    return datetime.combine(next_d, start_time_et, tzinfo=ET)


async def run_forever(
    config: Config,
    schema_path: Path,
    start_time_et: time = time(9, 25),
    restart_delay_seconds: int = 60,
) -> None:
    """Long-running loop: sleep until next market open, run a session, repeat.

    Survives crashes by logging and waiting `restart_delay_seconds` before
    retrying the same day (up to force-flat). Exits cleanly on KeyboardInterrupt.
    """
    while True:
        now = datetime.now(ET)
        target = next_session_start(now, start_time_et)
        wait_s = (target - now).total_seconds()
        if wait_s > 0:
            logger.info(
                "scheduler: sleeping until %s (%.1fh)", target.isoformat(), wait_s / 3600
            )
            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                logger.info("scheduler: cancelled during sleep, exiting")
                raise

        session_date = target.date()
        if not is_trading_day(session_date):
            # Safety: shouldn't happen, but skip and recompute
            logger.warning("scheduler: %s is not a trading day, skipping", session_date)
            continue

        logger.info("scheduler: starting session for %s", session_date)
        try:
            await run_session(config, schema_path)
            logger.info("scheduler: session %s completed cleanly", session_date)
        except KeyboardInterrupt:
            logger.info("scheduler: keyboard interrupt, exiting")
            raise
        except Exception:
            logger.exception(
                "scheduler: session %s crashed; sleeping %ds before recovery",
                session_date,
                restart_delay_seconds,
            )
            await asyncio.sleep(restart_delay_seconds)
            # Loop around; if we're still in-hours for the same session day,
            # run_session will pick up where it can. Otherwise the scheduler
            # will sleep until the next day.
