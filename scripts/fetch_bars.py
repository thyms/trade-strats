#!/usr/bin/env python3
"""Fetch and cache 1Min bars for given symbols."""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from trade_strats import bar_cache
from trade_strats.market_data import AlpacaSettings, MarketData

CACHE = Path("data/bars")


async def fetch_symbol(md: MarketData, symbol: str, start: datetime, end: datetime) -> None:
    start_date = start.date()
    end_date = end.date()
    missing = bar_cache.missing_months(CACHE, symbol, start_date, end_date)
    if not missing:
        print(f"  {symbol}: fully cached ({len(bar_cache.cached_months(CACHE, symbol, start_date, end_date))} months)")
        return

    print(f"  {symbol}: fetching {len(missing)} months...")
    for mk in missing:
        first, last = bar_cache.month_date_range(mk)
        month_start = datetime(first.year, first.month, first.day, tzinfo=UTC)
        month_end = datetime(last.year, last.month, last.day, 23, 59, 59, tzinfo=UTC)
        print(f"    {mk}...", end="", flush=True)
        fetched = await md.backfill(symbol, "1Min", month_start, month_end)
        if fetched:
            bar_cache.save_month(CACHE, symbol, mk, fetched)
            print(f" {len(fetched):,} bars")
        else:
            print(" (no data)")


async def main() -> None:
    load_dotenv()
    settings = AlpacaSettings.from_env()
    md = MarketData(settings)

    symbols = sys.argv[1:] if len(sys.argv) > 1 else ["AMD", "AMZN", "META", "GOOG", "COIN", "MSTR"]
    start = datetime(2019, 4, 16, tzinfo=UTC)
    end = datetime(2026, 4, 17, tzinfo=UTC)

    print(f"Fetching {len(symbols)} symbols from {start.date()} to {end.date()}")
    for sym in symbols:
        await fetch_symbol(md, sym, start, end)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
