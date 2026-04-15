import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from trade_strats.aggregation import (
    Aggregator,
    TimedBar,
    bucket_1d,
    bucket_1h,
    bucket_4h,
    bucket_15m,
    is_rth,
)

BarHandler = Callable[[str, str, TimedBar], Awaitable[None]]
AGGREGATED_TIMEFRAMES: tuple[str, ...] = ("15Min", "1H", "4H", "1D")


@dataclass(frozen=True, slots=True)
class AlpacaSettings:
    api_key: str
    api_secret: str
    base_url: str
    data_feed: str

    @classmethod
    def from_env(cls) -> "AlpacaSettings":
        def _required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"missing env var: {name}")
            return value

        return cls(
            api_key=_required("ALPACA_API_KEY"),
            api_secret=_required("ALPACA_API_SECRET"),
            base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip(),
            data_feed=os.environ.get("ALPACA_DATA_FEED", "iex").strip().lower(),
        )


SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1Min", "15Min", "1H", "4H", "1D")


def _tf(amount: int, unit: Any) -> TimeFrame:
    # alpaca-py's TimeFrameUnit is a StrEnum; pyright sees members as str and
    # rejects the constructor. Runtime is correct.
    return TimeFrame(amount, unit)  # pyright: ignore[reportArgumentType]


def _timeframe(tf: str) -> TimeFrame:
    match tf:
        case "1Min":
            return _tf(1, TimeFrameUnit.Minute)
        case "15Min":
            return _tf(15, TimeFrameUnit.Minute)
        case "1H":
            return _tf(1, TimeFrameUnit.Hour)
        case "4H":
            return _tf(4, TimeFrameUnit.Hour)
        case "1D":
            return _tf(1, TimeFrameUnit.Day)
        case _:
            raise ValueError(f"unsupported timeframe: {tf}")


_BUCKET_FNS = {
    "15Min": bucket_15m,
    "1H": bucket_1h,
    "4H": bucket_4h,
    "1D": bucket_1d,
}


def _to_timed_bar(alpaca_bar: Any) -> TimedBar:
    return TimedBar(
        ts=alpaca_bar.timestamp,
        open=float(alpaca_bar.open),
        high=float(alpaca_bar.high),
        low=float(alpaca_bar.low),
        close=float(alpaca_bar.close),
        volume=int(alpaca_bar.volume),
    )


def _data_feed(name: str) -> DataFeed:
    normalized = name.lower()
    for feed in DataFeed:
        if feed.value == normalized:
            return feed
    raise ValueError(f"unknown data feed: {name}")


class MarketData:
    """Thin wrapper around alpaca-py historical + streaming bar data.

    Ingests 1m WS bars and routes them through per-symbol aggregators for
    15m / 1H / 4H / 1D, firing a single async callback on every completed bar.
    Does not auto-reconnect — caller wraps `run()` with retry if desired.
    """

    def __init__(self, settings: AlpacaSettings) -> None:
        self._settings = settings
        self._handler: BarHandler | None = None
        self._aggregators: dict[str, dict[str, Aggregator]] = {}
        self._historical: StockHistoricalDataClient | None = None
        self._stream: StockDataStream | None = None

    def set_bar_handler(self, handler: BarHandler) -> None:
        self._handler = handler

    def _agg_for(self, symbol: str) -> dict[str, Aggregator]:
        if symbol not in self._aggregators:
            self._aggregators[symbol] = {
                tf: Aggregator(_BUCKET_FNS[tf]) for tf in AGGREGATED_TIMEFRAMES
            }
        return self._aggregators[symbol]

    def current_open(self, symbol: str, timeframe: str) -> float | None:
        aggs = self._aggregators.get(symbol)
        if aggs is None:
            return None
        agg = aggs.get(timeframe)
        return None if agg is None else agg.current_open

    async def ingest_minute_bar(self, symbol: str, bar: TimedBar) -> None:
        """Route one 1m bar into all aggregators; fire handler on completions."""
        if not is_rth(bar.ts):
            return
        aggs = self._agg_for(symbol)
        for tf in AGGREGATED_TIMEFRAMES:
            for completed in aggs[tf].ingest(bar):
                if self._handler is not None:
                    await self._handler(symbol, tf, completed)

    async def flush(self, symbol: str) -> None:
        """Emit in-progress bars for one symbol — use at session close."""
        aggs = self._aggregators.get(symbol)
        if aggs is None:
            return
        for tf in AGGREGATED_TIMEFRAMES:
            for completed in aggs[tf].flush():
                if self._handler is not None:
                    await self._handler(symbol, tf, completed)

    async def flush_all(self) -> None:
        for symbol in list(self._aggregators.keys()):
            await self.flush(symbol)

    def _historical_client(self) -> StockHistoricalDataClient:
        if self._historical is None:
            self._historical = StockHistoricalDataClient(
                api_key=self._settings.api_key,
                secret_key=self._settings.api_secret,
            )
        return self._historical

    async def backfill(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[TimedBar]:
        client = self._historical_client()
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_timeframe(timeframe),
            start=start,
            end=end,
            feed=_data_feed(self._settings.data_feed),
        )
        bar_set = await asyncio.to_thread(client.get_stock_bars, request)
        raw: list[Any] = list(
            bar_set.data.get(symbol, [])  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportUnknownArgumentType]
        )
        return [_to_timed_bar(b) for b in raw]

    async def run(self, symbols: Sequence[str]) -> None:
        """Subscribe to 1m WS bars and stream forever. Cancel the task to stop."""
        if self._handler is None:
            raise RuntimeError("set_bar_handler() must be called before run()")
        if not symbols:
            raise ValueError("symbols must be non-empty")

        stream = StockDataStream(
            api_key=self._settings.api_key,
            secret_key=self._settings.api_secret,
            feed=_data_feed(self._settings.data_feed),
        )

        async def on_alpaca_bar(alpaca_bar: Any) -> None:
            await self.ingest_minute_bar(alpaca_bar.symbol, _to_timed_bar(alpaca_bar))

        stream.subscribe_bars(on_alpaca_bar, *symbols)  # pyright: ignore[reportUnknownMemberType]
        self._stream = stream
        try:
            await stream._run_forever()  # pyright: ignore[reportPrivateUsage]
        finally:
            self._stream = None
