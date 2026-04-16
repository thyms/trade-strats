import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from trade_strats.aggregation import ET, TimedBar
from trade_strats.market_data import (
    AlpacaSettings,
    MarketData,
    _data_feed,
    _timeframe,
    _to_timed_bar,
)

# --- Fixtures ---------------------------------------------------------------


def _fake_settings() -> AlpacaSettings:
    return AlpacaSettings(
        api_key="k",
        api_secret="s",
        base_url="https://paper-api.alpaca.markets",
        data_feed="iex",
    )


@dataclass
class FakeAlpacaBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _fake_bar(
    *,
    symbol: str = "SPY",
    ts: datetime,
    o: float = 100.0,
    h: float = 101.0,
    lo: float = 99.0,
    c: float = 100.5,
    v: float = 1000.0,
) -> FakeAlpacaBar:
    return FakeAlpacaBar(
        symbol=symbol,
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
    )


def _et(h: int, m: int, day: int = 15) -> datetime:
    return datetime(2026, 4, day, h, m, tzinfo=ET)


# --- AlpacaSettings.from_env ------------------------------------------------


def test_from_env_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "abc")
    monkeypatch.setenv("ALPACA_API_SECRET", "def")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setenv("ALPACA_DATA_FEED", "iex")
    s = AlpacaSettings.from_env()
    assert s.api_key == "abc"
    assert s.api_secret == "def"
    assert s.data_feed == "iex"


def test_from_env_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "abc")
    monkeypatch.setenv("ALPACA_API_SECRET", "def")
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    monkeypatch.delenv("ALPACA_DATA_FEED", raising=False)
    s = AlpacaSettings.from_env()
    assert s.base_url == "https://paper-api.alpaca.markets"
    assert s.data_feed == "iex"


def test_from_env_rejects_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_API_SECRET", "def")
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        AlpacaSettings.from_env()


def test_from_env_rejects_empty_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "abc")
    monkeypatch.setenv("ALPACA_API_SECRET", "   ")
    with pytest.raises(ValueError, match="ALPACA_API_SECRET"):
        AlpacaSettings.from_env()


def test_from_env_normalizes_feed_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "abc")
    monkeypatch.setenv("ALPACA_API_SECRET", "def")
    monkeypatch.setenv("ALPACA_DATA_FEED", "IEX")
    s = AlpacaSettings.from_env()
    assert s.data_feed == "iex"


# --- Timeframe / data-feed mapping ----------------------------------------


@pytest.mark.parametrize(
    "tf", ["1Min", "5Min", "10Min", "15Min", "20Min", "30Min", "1H", "4H", "1D"]
)
def test_timeframe_supported(tf: str) -> None:
    result = _timeframe(tf)
    assert result is not None


def test_timeframe_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _timeframe("bogus")


def test_data_feed_known() -> None:
    assert _data_feed("iex").value == "iex"
    assert _data_feed("IEX").value == "iex"


def test_data_feed_unknown() -> None:
    with pytest.raises(ValueError, match="unknown data feed"):
        _data_feed("not-a-feed")


# --- Bar conversion --------------------------------------------------------


def test_to_timed_bar_converts_correctly() -> None:
    fake = _fake_bar(
        ts=_et(9, 30),
        o=512.45,
        h=513.10,
        lo=512.30,
        c=512.95,
        v=25_432.0,
    )
    tb = _to_timed_bar(fake)
    assert tb.ts == _et(9, 30)
    assert tb.open == 512.45
    assert tb.high == 513.10
    assert tb.low == 512.30
    assert tb.close == 512.95
    assert tb.volume == 25_432


def test_to_timed_bar_casts_float_volume_to_int() -> None:
    fake = _fake_bar(ts=_et(9, 30), v=1234.9)
    tb = _to_timed_bar(fake)
    assert tb.volume == 1234
    assert isinstance(tb.volume, int)


# --- MarketData routing ----------------------------------------------------


async def test_ingest_routes_to_handler_on_bucket_close() -> None:
    md = MarketData(_fake_settings())
    emissions: list[tuple[str, str, TimedBar]] = []

    async def handler(symbol: str, tf: str, bar: TimedBar) -> None:
        emissions.append((symbol, tf, bar))

    md.set_bar_handler(handler)

    # Feed 15 1-min bars into bucket 09:30, then one bar into 09:45 to close it.
    for i in range(15):
        await md.ingest_minute_bar(
            "SPY",
            TimedBar(
                ts=_et(9, 30) + timedelta(minutes=i),
                open=100 + i * 0.1,
                high=100 + i * 0.1 + 0.1,
                low=100 + i * 0.1 - 0.1,
                close=100 + i * 0.1 + 0.05,
                volume=100,
            ),
        )
    assert emissions == []

    await md.ingest_minute_bar(
        "SPY",
        TimedBar(
            ts=_et(9, 45),
            open=110.0,
            high=110.5,
            low=109.5,
            close=110.2,
            volume=100,
        ),
    )
    # The 09:45 bar closes the 15Min, 1H (partial ongoing), 4H (partial), 1D (partial)
    # Only the 15Min bucket completes at the boundary.
    emitted_tfs = [tf for (_, tf, _) in emissions]
    assert "15Min" in emitted_tfs
    # 1H / 4H / 1D buckets should NOT have emitted yet (same bucket continues).
    assert "1H" not in emitted_tfs
    assert "4H" not in emitted_tfs


async def test_ingest_skips_non_rth_bars() -> None:
    md = MarketData(_fake_settings())
    emissions: list[TimedBar] = []

    async def handler(_symbol: str, _tf: str, bar: TimedBar) -> None:
        emissions.append(bar)

    md.set_bar_handler(handler)

    pre_market = TimedBar(
        ts=_et(8, 0),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=50,
    )
    await md.ingest_minute_bar("SPY", pre_market)
    assert emissions == []
    assert md.current_open("SPY", "15Min") is None


async def test_current_open_tracks_15m_bucket() -> None:
    md = MarketData(_fake_settings())
    md.set_bar_handler(_noop_handler)
    await md.ingest_minute_bar(
        "SPY",
        TimedBar(ts=_et(9, 30), open=200.0, high=200.5, low=199.5, close=200.2, volume=100),
    )
    assert md.current_open("SPY", "15Min") == 200.0
    assert md.current_open("SPY", "1H") == 200.0
    assert md.current_open("SPY", "1D") == 200.0


async def test_current_open_is_none_before_first_bar() -> None:
    md = MarketData(_fake_settings())
    assert md.current_open("SPY", "15Min") is None


async def test_flush_emits_in_progress_bars() -> None:
    md = MarketData(_fake_settings())
    emissions: list[tuple[str, str]] = []

    async def handler(symbol: str, tf: str, _bar: TimedBar) -> None:
        emissions.append((symbol, tf))

    md.set_bar_handler(handler)
    await md.ingest_minute_bar(
        "SPY",
        TimedBar(ts=_et(9, 30), open=100, high=101, low=99, close=100, volume=100),
    )
    await md.flush("SPY")
    tfs = {tf for (_, tf) in emissions}
    assert tfs == {"15Min", "1H", "4H", "1D"}


async def test_multiple_symbols_maintain_separate_state() -> None:
    md = MarketData(_fake_settings())
    md.set_bar_handler(_noop_handler)
    await md.ingest_minute_bar(
        "SPY",
        TimedBar(ts=_et(9, 30), open=500, high=501, low=499, close=500, volume=100),
    )
    await md.ingest_minute_bar(
        "QQQ",
        TimedBar(ts=_et(9, 30), open=400, high=401, low=399, close=400, volume=100),
    )
    assert md.current_open("SPY", "15Min") == 500
    assert md.current_open("QQQ", "15Min") == 400


async def test_run_without_handler_raises() -> None:
    md = MarketData(_fake_settings())
    with pytest.raises(RuntimeError, match="set_bar_handler"):
        await md.run(["SPY"])


async def test_run_empty_symbols_raises() -> None:
    md = MarketData(_fake_settings())
    md.set_bar_handler(_noop_handler)
    with pytest.raises(ValueError, match="symbols"):
        await md.run([])


async def _noop_handler(_symbol: str, _tf: str, _bar: TimedBar) -> None:
    pass


# --- Integration (opt-in: runs only when ALPACA_API_KEY is set) ------------


@pytest.mark.skipif(
    not os.environ.get("ALPACA_API_KEY"),
    reason="ALPACA_API_KEY not set — skipping live-API integration test",
)
async def test_backfill_returns_bars_from_alpaca() -> None:
    settings = AlpacaSettings.from_env()
    md = MarketData(settings)
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    bars = await md.backfill("SPY", "1D", start, end)
    assert len(bars) > 0
    for b in bars:
        assert b.ts.tzinfo is not None
        assert b.high >= b.low
