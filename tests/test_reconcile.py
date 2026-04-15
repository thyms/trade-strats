from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trade_strats.execution import Executor
from trade_strats.journal import Journal, OrderRecord, TradeRecord
from trade_strats.market_data import AlpacaSettings
from trade_strats.reconcile import DriftKind, format_report, reconcile

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


# --- Fakes ------------------------------------------------------------------


@dataclass
class FakeAccount:
    equity: str = "50000.00"
    cash: str = "50000.00"
    buying_power: str = "200000.00"
    daytrade_count: int = 0


@dataclass
class FakePosition:
    symbol: str
    qty: str
    side: str
    avg_entry_price: str
    current_price: str
    unrealized_pl: str = "0.0"


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


class FakeTradingClient:
    def __init__(self) -> None:
        self.account = FakeAccount()
        self.positions: list[FakePosition] = []
        self.open_orders: list[FakeOrder] = []

    def get_account(self) -> FakeAccount:
        return self.account

    def get_all_positions(self) -> list[FakePosition]:
        return self.positions

    def get_orders(self, request: Any) -> list[FakeOrder]:
        return self.open_orders


def _settings() -> AlpacaSettings:
    return AlpacaSettings(
        api_key="k",
        api_secret="s",
        base_url="https://paper-api.alpaca.markets",
        data_feed="iex",
    )


# --- Fixtures ---------------------------------------------------------------


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


def _trade(symbol: str = "SPY") -> TradeRecord:
    return TradeRecord(
        symbol=symbol,
        side="long",
        pattern="3-2-2",
        entry_ts="2026-04-15T13:45:00Z",
        entry_price=512.45,
        stop_price=511.20,
        target_price=516.20,
        qty=100,
        mode="paper",
    )


def _order(
    order_id: str = "ord-1", symbol: str = "SPY", *, trade_id: int | None = None
) -> OrderRecord:
    return OrderRecord(
        alpaca_order_id=order_id,
        symbol=symbol,
        side="long",
        kind="entry",
        type="stop_limit",
        qty=100,
        status="accepted",
        submitted_ts="2026-04-15T13:45:00Z",
        limit_price=512.45,
        stop_price=512.40,
        trade_id=trade_id,
    )


# --- Clean cases -----------------------------------------------------------


async def test_clean_when_no_state(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    fake_client.positions = []
    fake_client.open_orders = []
    report = await reconcile(executor, journal)
    assert report.clean is True
    assert report.drift == []
    assert report.alpaca_equity == 50_000.0


async def test_clean_when_position_matches_open_trade(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    await journal.insert_trade(_trade("SPY"))
    fake_client.positions = [
        FakePosition(
            symbol="SPY",
            qty="100",
            side="long",
            avg_entry_price="512.45",
            current_price="513.00",
        )
    ]
    report = await reconcile(executor, journal)
    assert report.clean is True


async def test_clean_when_order_is_tracked_in_journal(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    trade_id = await journal.insert_trade(_trade("SPY"))
    await journal.insert_order(_order("ord-1", "SPY", trade_id=trade_id))
    fake_client.positions = [
        FakePosition(
            symbol="SPY",
            qty="100",
            side="long",
            avg_entry_price="512.45",
            current_price="513.00",
        )
    ]
    fake_client.open_orders = [
        FakeOrder(id="ord-1", symbol="SPY", side="buy", order_type="stop_limit")
    ]
    report = await reconcile(executor, journal)
    assert report.clean is True


# --- UNKNOWN_POSITION -----------------------------------------------------


async def test_unknown_position_when_alpaca_has_untracked_symbol(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    fake_client.positions = [
        FakePosition(
            symbol="NVDA",
            qty="50",
            side="long",
            avg_entry_price="900.00",
            current_price="905.00",
        )
    ]
    report = await reconcile(executor, journal)
    assert report.clean is False
    assert len(report.drift) == 1
    issue = report.drift[0]
    assert issue.kind is DriftKind.UNKNOWN_POSITION
    assert issue.symbol == "NVDA"
    assert "no matching open trade" in issue.detail


# --- MISSED_EXIT ----------------------------------------------------------


async def test_missed_exit_when_journal_open_but_alpaca_flat(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    trade_id = await journal.insert_trade(_trade("SPY"))
    fake_client.positions = []
    report = await reconcile(executor, journal)
    assert report.clean is False
    assert len(report.drift) == 1
    issue = report.drift[0]
    assert issue.kind is DriftKind.MISSED_EXIT
    assert issue.symbol == "SPY"
    assert issue.trade_id == trade_id
    assert "no position" in issue.detail


async def test_missed_exit_does_not_flag_closed_trades(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    trade_id = await journal.insert_trade(_trade("SPY"))
    await journal.update_trade_exit(
        trade_id=trade_id,
        exit_ts="2026-04-15T14:00:00Z",
        exit_price=516.20,
        exit_reason="target",
        realized_pnl=375.00,
        r_multiple=3.0,
    )
    fake_client.positions = []
    report = await reconcile(executor, journal)
    assert report.clean is True


# --- ORPHAN_ORDER ---------------------------------------------------------


async def test_orphan_order_when_alpaca_order_not_in_journal(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    fake_client.open_orders = [
        FakeOrder(id="ord-stray", symbol="SPY", side="buy", order_type="stop_limit")
    ]
    report = await reconcile(executor, journal)
    assert report.clean is False
    assert len(report.drift) == 1
    issue = report.drift[0]
    assert issue.kind is DriftKind.ORPHAN_ORDER
    assert issue.order_id == "ord-stray"


# --- Combined drift -------------------------------------------------------


async def test_multiple_drifts_reported_together(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    # UNKNOWN_POSITION: NVDA position without trade
    # MISSED_EXIT: SPY open trade but no Alpaca position
    # ORPHAN_ORDER: stray order on QQQ
    await journal.insert_trade(_trade("SPY"))
    fake_client.positions = [
        FakePosition(
            symbol="NVDA",
            qty="25",
            side="long",
            avg_entry_price="900.00",
            current_price="905.00",
        )
    ]
    fake_client.open_orders = [
        FakeOrder(id="ord-stray", symbol="QQQ", side="buy", order_type="limit")
    ]
    report = await reconcile(executor, journal)
    assert len(report.drift) == 3
    kinds = {d.kind for d in report.drift}
    assert kinds == {
        DriftKind.UNKNOWN_POSITION,
        DriftKind.MISSED_EXIT,
        DriftKind.ORPHAN_ORDER,
    }


# --- format_report --------------------------------------------------------


async def test_format_report_clean(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    fake_client.account = FakeAccount(equity="52345.67")
    report = await reconcile(executor, journal)
    text = format_report(report)
    assert "Alpaca equity:" in text
    assert "$52,345.67" in text
    assert "OK: no drift" in text


async def test_format_report_drift(
    executor: Executor, journal: Journal, fake_client: FakeTradingClient
) -> None:
    await journal.insert_trade(_trade("SPY"))
    report = await reconcile(executor, journal)
    text = format_report(report)
    assert "DRIFT:" in text
    assert "missed_exit" in text
    assert "SPY" in text
