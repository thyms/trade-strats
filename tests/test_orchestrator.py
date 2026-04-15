from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trade_strats.aggregation import ET, TimedBar
from trade_strats.config import Config
from trade_strats.execution import Executor
from trade_strats.journal import Journal
from trade_strats.market_data import AlpacaSettings
from trade_strats.orchestrator import (
    EvalOutcome,
    compute_atr14,
    evaluate_and_submit,
    pick_best_setup,
)
from trade_strats.strategy.ftfc import HigherTfOpens
from trade_strats.strategy.patterns import PatternKind, Setup, Side

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


# --- Fakes ------------------------------------------------------------------


@dataclass
class FakeAccount:
    equity: str = "50000.00"
    cash: str = "50000.00"
    buying_power: str = "200000.00"
    daytrade_count: int = 0


@dataclass
class FakeOrderLeg:
    id: str
    order_type: str


@dataclass
class FakeOrder:
    id: str
    symbol: str
    side: str = "buy"
    order_type: str = "stop_limit"
    status: str = "accepted"
    qty: str = "100"
    submitted_at: datetime = field(
        default_factory=lambda: datetime(2026, 4, 15, 13, 45, tzinfo=UTC)
    )
    filled_at: datetime | None = None
    filled_avg_price: str | None = None
    legs: list[FakeOrderLeg] = field(default_factory=list)


class FakeTradingClient:
    def __init__(self) -> None:
        self.account = FakeAccount()
        self.positions: list[Any] = []
        self.submitted_requests: list[Any] = []
        self._response_order: FakeOrder | None = None

    def set_submit_response(self, order: FakeOrder) -> None:
        self._response_order = order

    def get_account(self) -> FakeAccount:
        return self.account

    def get_all_positions(self) -> list[Any]:
        return self.positions

    def get_orders(self, request: Any) -> list[Any]:
        return []

    def submit_order(self, request: Any) -> FakeOrder:
        self.submitted_requests.append(request)
        if self._response_order is not None:
            return self._response_order
        return FakeOrder(
            id="parent-1",
            symbol=request.symbol,
            legs=[
                FakeOrderLeg(id="stop-1", order_type="stop"),
                FakeOrderLeg(id="tp-1", order_type="limit"),
            ],
        )

    def cancel_order_by_id(self, _: str) -> None:
        pass

    def cancel_orders(self) -> list[Any]:
        return []

    def close_position(self, _: str) -> None:
        pass

    def close_all_positions(self, _: bool) -> list[Any]:
        return []


def _settings() -> AlpacaSettings:
    return AlpacaSettings(
        api_key="k",
        api_secret="s",
        base_url="https://paper-api.alpaca.markets",
        data_feed="iex",
    )


def _valid_config() -> Config:
    return Config.model_validate(
        {
            "mode": "paper",
            "account": {
                "sim_equity_usd": 50000,
                "risk_pct_per_trade": 0.005,
                "daily_loss_cap_pct": 0.02,
                "max_concurrent": 3,
                "max_trades_per_day": 5,
            },
            "strategy": {
                "timeframe": "15Min",
                "patterns": ["3-2-2", "2-2", "3-1-2", "rev-strat"],
                "sides": ["long", "short"],
                "min_rr": 3.0,
                "min_bar_atr_mult": 0.0,  # disable ATR filter for tests
                "ftfc_timeframes": ["1D", "4H", "1H"],
            },
            "watchlist": ["SPY"],
            "session": {
                "entry_window_et": ["09:30", "15:45"],
                "force_flat_et": "15:55",
            },
            "blackouts": [],
            "paths": {
                "db": "data/trades.db",
                "events_log": "data/events.jsonl",
                "reports_dir": "reports",
            },
        }
    )


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def fake_client() -> FakeTradingClient:
    return FakeTradingClient()


@pytest.fixture
def executor(fake_client: FakeTradingClient) -> Executor:
    return Executor(_settings(), client_factory=lambda: fake_client)


@pytest.fixture
async def journal(tmp_path: Path):
    j = await Journal.open(
        db_path=tmp_path / "trades.db",
        events_path=tmp_path / "events.jsonl",
        schema_path=SCHEMA_PATH,
    )
    async with j:
        yield j


# --- compute_atr14 ---------------------------------------------------------


def test_compute_atr14_insufficient_bars_returns_zero() -> None:
    bars = _bars_at_price(10, count=10)
    assert compute_atr14(bars) == 0.0


def test_compute_atr14_uses_last_14_true_ranges() -> None:
    # Constant bars with range 2 → ATR == 2
    bars = _bars_at_price(100.0, count=20, high_off=1.0, low_off=1.0)
    assert compute_atr14(bars) == pytest.approx(2.0)


def _bars_at_price(
    price: float,
    *,
    count: int,
    high_off: float = 0.5,
    low_off: float = 0.5,
) -> list[TimedBar]:
    start = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    bars: list[TimedBar] = []
    for i in range(count):
        bars.append(
            TimedBar(
                ts=start + timedelta(minutes=15 * i),
                open=price,
                high=price + high_off,
                low=price - low_off,
                close=price,
                volume=1000,
            )
        )
    return bars


# --- pick_best_setup -------------------------------------------------------


def _fake_setup(kind: PatternKind, side: Side = Side.LONG) -> Setup:
    from trade_strats.strategy.labeler import Bar

    signal = Bar(open=100, high=101, low=99, close=100.5)
    return Setup(
        kind=kind,
        side=side,
        signal_bar=signal,
        trigger_price=101.0,
        stop_price=99.0,
    )


def test_pick_best_setup_prefers_3_1_2_over_3_2_2() -> None:
    setups = [_fake_setup(PatternKind.THREE_TWO_TWO), _fake_setup(PatternKind.THREE_ONE_TWO)]
    best = pick_best_setup(setups)
    assert best is not None
    assert best.kind is PatternKind.THREE_ONE_TWO


def test_pick_best_setup_prefers_3_2_2_over_2_2() -> None:
    setups = [_fake_setup(PatternKind.TWO_TWO), _fake_setup(PatternKind.THREE_TWO_TWO)]
    best = pick_best_setup(setups)
    assert best is not None
    assert best.kind is PatternKind.THREE_TWO_TWO


def test_pick_best_setup_returns_none_for_empty() -> None:
    assert pick_best_setup([]) is None


# --- evaluate_and_submit: bar sequences ------------------------------------


def _setup_322_bullish_sequence() -> list[TimedBar]:
    """15 filler bars + 4 bars forming a 3-2-2 bullish reversal (signal at last)."""
    start = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    filler = [
        TimedBar(
            ts=start + timedelta(minutes=15 * i),
            open=10.0,
            high=10.5,
            low=9.5,
            close=10.0,
            volume=1000,
        )
        for i in range(15)
    ]
    # Four pattern bars, timestamps continuing after filler:
    base = start + timedelta(minutes=15 * 15)
    # prev: used by labeler to classify the first pattern bar
    prev = TimedBar(ts=base, open=10.0, high=11.0, low=9.0, close=10.0, volume=1000)
    # outside (red 3): high > 11, low < 9, red close
    outside = TimedBar(
        ts=base + timedelta(minutes=15),
        open=10.5,
        high=12.0,
        low=8.0,
        close=9.5,
        volume=1000,
    )
    # red 2D: high <= 12, low < 8, red
    two_d = TimedBar(
        ts=base + timedelta(minutes=30),
        open=9.0,
        high=11.0,
        low=7.0,
        close=7.5,
        volume=1000,
    )
    # green 2U: high > 11, low >= 7, green (signal bar)
    two_u = TimedBar(
        ts=base + timedelta(minutes=45),
        open=8.0,
        high=12.5,
        low=7.0,
        close=12.0,
        volume=1000,
    )
    return [*filler, prev, outside, two_d, two_u]


async def test_not_enough_bars_returns_outcome(executor: Executor, journal: Journal) -> None:
    bars = _bars_at_price(100.0, count=5)
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=None,
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
    )
    assert result.outcome is EvalOutcome.NOT_ENOUGH_BARS


async def test_no_setup_on_flat_bars(executor: Executor, journal: Journal) -> None:
    bars = _bars_at_price(100.0, count=20)
    # All bars are inside each other (same high/low) → all scenario 1 → no setup
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=90.0, four_hour=90.0, one_hour=90.0),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
    )
    assert result.outcome is EvalOutcome.NO_SETUP


async def test_pattern_filtered_when_config_excludes_kind(
    executor: Executor, journal: Journal
) -> None:
    config = _valid_config()
    # Exclude 3-2-2 and 2-2; only allow 3-1-2 and rev-strat.
    config_dict = config.model_dump()
    config_dict["strategy"]["patterns"] = ["3-1-2", "rev-strat"]
    restricted = Config.model_validate(config_dict)
    bars = _setup_322_bullish_sequence()
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=10.0, four_hour=11.0, one_hour=11.5),
        executor=executor,
        journal=journal,
        config=restricted,
        now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
    )
    assert result.outcome is EvalOutcome.PATTERN_FILTERED


async def test_ftfc_missing_when_opens_none(executor: Executor, journal: Journal) -> None:
    bars = _setup_322_bullish_sequence()
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=None,
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
    )
    assert result.outcome is EvalOutcome.FTFC_MISSING
    assert result.setup is not None


async def test_ftfc_mismatch_on_misaligned_higher_tfs(executor: Executor, journal: Journal) -> None:
    bars = _setup_322_bullish_sequence()
    # Price = 12, opens all above → bearish bias → rejects long
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=15.0, four_hour=14.0, one_hour=13.0),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=datetime(2026, 4, 15, 14, 0, tzinfo=UTC),
    )
    assert result.outcome is EvalOutcome.FTFC_MISMATCH


async def test_risk_rejected_on_loss_cap(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    # Simulate session with realized loss at the cap
    await journal.upsert_session_start("2026-04-15", start_equity=50_000.0)
    await journal.update_session_progress("2026-04-15", trades_count=0, realized_pnl=-1_000.0)
    fake_client.account = FakeAccount(equity="50000.00")
    bars = _setup_322_bullish_sequence()
    now = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=10.0, four_hour=11.0, one_hour=11.5),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=now,
    )
    assert result.outcome is EvalOutcome.RISK_REJECTED
    assert result.rejection_reason == "daily_loss_cap"


async def test_submitted_happy_path_persists_trade_and_orders(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    bars = _setup_322_bullish_sequence()
    now = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=10.0, four_hour=11.0, one_hour=11.5),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=now,
    )
    assert result.outcome is EvalOutcome.SUBMITTED
    assert result.trade_id is not None
    assert result.bracket is not None

    # Trade persisted
    trade = await journal.get_trade(result.trade_id)
    assert trade is not None
    assert trade["symbol"] == "SPY"
    assert trade["pattern"] == "3-2-2"
    assert trade["side"] == "long"
    assert trade["mode"] == "paper"
    assert trade["ftfc_1d"] == "full_green"

    # Parent + child orders persisted
    assert await journal.get_order("parent-1") is not None
    assert await journal.get_order("stop-1") is not None
    assert await journal.get_order("tp-1") is not None

    # Bracket request sent to alpaca
    assert len(fake_client.submitted_requests) == 1
    req = fake_client.submitted_requests[0]
    assert req.symbol == "SPY"
    assert req.side.value == "buy"
    assert req.order_class.value == "bracket"


async def test_submitted_writes_entry_event(
    executor: Executor, journal: Journal, tmp_path: Path
) -> None:
    bars = _setup_322_bullish_sequence()
    now = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=10.0, four_hour=11.0, one_hour=11.5),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=now,
    )
    events = (tmp_path / "events.jsonl").read_text().splitlines()
    assert any('"event":"entry_submitted"' in line for line in events)


async def test_ftfc_short_bias_triggers_short_submission(
    executor: Executor, journal: Journal
) -> None:
    # Build bearish 3-2-2: green outside → green 2U → red 2D
    start = datetime(2026, 4, 15, 9, 30, tzinfo=ET)
    filler = [
        TimedBar(
            ts=start + timedelta(minutes=15 * i),
            open=10.0,
            high=10.5,
            low=9.5,
            close=10.0,
            volume=1000,
        )
        for i in range(15)
    ]
    base = start + timedelta(minutes=15 * 15)
    prev = TimedBar(ts=base, open=10.0, high=11.0, low=9.0, close=10.0, volume=1000)
    outside_green = TimedBar(
        ts=base + timedelta(minutes=15), open=9.5, high=12.0, low=8.0, close=11.5, volume=1000
    )
    two_u_green = TimedBar(
        ts=base + timedelta(minutes=30), open=11.0, high=13.0, low=9.0, close=12.5, volume=1000
    )
    two_d_red = TimedBar(
        ts=base + timedelta(minutes=45), open=12.0, high=13.0, low=7.5, close=8.0, volume=1000
    )
    bars = [*filler, prev, outside_green, two_u_green, two_d_red]
    now = datetime(2026, 4, 15, 14, 0, tzinfo=ET)
    # Price = 8.0, opens all above → bearish bias → allows short
    result = await evaluate_and_submit(
        symbol="SPY",
        bars_15m=bars,
        opens=HigherTfOpens(daily=20.0, four_hour=18.0, one_hour=15.0),
        executor=executor,
        journal=journal,
        config=_valid_config(),
        now=now,
    )
    assert result.outcome is EvalOutcome.SUBMITTED
    assert result.setup is not None
    assert result.setup.side is Side.SHORT
