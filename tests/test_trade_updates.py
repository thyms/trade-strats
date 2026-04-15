from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trade_strats.journal import Journal, OrderRecord, TradeRecord
from trade_strats.market_data import AlpacaSettings
from trade_strats.trade_updates import TradeUpdateHandler

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


def _settings() -> AlpacaSettings:
    return AlpacaSettings(
        api_key="k",
        api_secret="s",
        base_url="https://paper-api.alpaca.markets",
        data_feed="iex",
    )


@dataclass
class FakeOrderPayload:
    id: str
    status: str = "filled"
    filled_at: datetime | None = None
    filled_avg_price: str | None = None
    canceled_at: datetime | None = None


@dataclass
class FakeTradeUpdate:
    event: str
    order: FakeOrderPayload


@pytest.fixture
async def journal(tmp_path: Path):
    j = await Journal.open(
        db_path=tmp_path / "trades.db",
        events_path=tmp_path / "events.jsonl",
        schema_path=SCHEMA_PATH,
    )
    async with j:
        yield j


async def _seed_long_bracket(journal: Journal) -> int:
    trade_id = await journal.insert_trade(
        TradeRecord(
            symbol="SPY",
            side="long",
            pattern="3-2-2",
            entry_ts="2026-04-15T13:45:00Z",
            entry_price=100.00,
            stop_price=98.00,
            target_price=106.00,
            qty=50,
            mode="paper",
        )
    )
    await journal.insert_order(
        OrderRecord(
            alpaca_order_id="parent-1",
            trade_id=trade_id,
            symbol="SPY",
            side="long",
            kind="entry",
            type="stop_limit",
            qty=50,
            stop_price=100.00,
            status="accepted",
            submitted_ts="2026-04-15T13:45:00Z",
        )
    )
    await journal.insert_order(
        OrderRecord(
            alpaca_order_id="stop-1",
            trade_id=trade_id,
            symbol="SPY",
            side="long",
            kind="stop",
            type="stop",
            qty=50,
            stop_price=98.00,
            status="accepted",
            submitted_ts="2026-04-15T13:45:00Z",
        )
    )
    await journal.insert_order(
        OrderRecord(
            alpaca_order_id="target-1",
            trade_id=trade_id,
            symbol="SPY",
            side="long",
            kind="target",
            type="limit",
            qty=50,
            limit_price=106.00,
            status="accepted",
            submitted_ts="2026-04-15T13:45:00Z",
        )
    )
    return trade_id


async def test_parent_fill_updates_order_only(journal: Journal) -> None:
    trade_id = await _seed_long_bracket(journal)
    handler = TradeUpdateHandler(_settings(), journal)
    ts = datetime(2026, 4, 15, 13, 46, tzinfo=UTC)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(
                id="parent-1", status="filled", filled_at=ts, filled_avg_price="100.05"
            ),
        )
    )
    order = await journal.get_order("parent-1")
    assert order is not None
    assert order["status"] == "filled"
    assert order["filled_avg_price"] == 100.05
    # Parent fill does NOT close the trade
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    assert trade["exit_ts"] is None


async def test_target_fill_closes_trade_with_positive_pnl(journal: Journal) -> None:
    trade_id = await _seed_long_bracket(journal)
    handler = TradeUpdateHandler(_settings(), journal)
    ts = datetime(2026, 4, 15, 14, 30, tzinfo=UTC)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(
                id="target-1", status="filled", filled_at=ts, filled_avg_price="106.00"
            ),
        )
    )
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    assert trade["exit_ts"] == ts.isoformat()
    assert trade["exit_price"] == 106.00
    assert trade["exit_reason"] == "target"
    # (106 - 100) * 50 = 300
    assert trade["realized_pnl"] == 300.00
    # risk_per_share = 2, r_multiple = 300 / (2 * 50) = 3.0
    assert trade["r_multiple"] == 3.0


async def test_stop_fill_closes_trade_with_negative_pnl(journal: Journal) -> None:
    trade_id = await _seed_long_bracket(journal)
    handler = TradeUpdateHandler(_settings(), journal)
    ts = datetime(2026, 4, 15, 14, 0, tzinfo=UTC)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(
                id="stop-1", status="filled", filled_at=ts, filled_avg_price="98.00"
            ),
        )
    )
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    assert trade["exit_reason"] == "stop"
    # (98 - 100) * 50 = -100
    assert trade["realized_pnl"] == -100.00
    # r_multiple = -100 / 100 = -1.0
    assert trade["r_multiple"] == -1.0


async def test_short_target_fill_pnl_direction(journal: Journal) -> None:
    trade_id = await journal.insert_trade(
        TradeRecord(
            symbol="QQQ",
            side="short",
            pattern="3-2-2",
            entry_ts="2026-04-15T13:45:00Z",
            entry_price=100.00,
            stop_price=102.00,
            target_price=94.00,
            qty=50,
            mode="paper",
        )
    )
    await journal.insert_order(
        OrderRecord(
            alpaca_order_id="short-target",
            trade_id=trade_id,
            symbol="QQQ",
            side="short",
            kind="target",
            type="limit",
            qty=50,
            limit_price=94.00,
            status="accepted",
            submitted_ts="2026-04-15T13:45:00Z",
        )
    )
    handler = TradeUpdateHandler(_settings(), journal)
    ts = datetime(2026, 4, 15, 14, 30, tzinfo=UTC)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(
                id="short-target", status="filled", filled_at=ts, filled_avg_price="94.00"
            ),
        )
    )
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    # Short: (entry - exit) * qty = (100 - 94) * 50 = 300
    assert trade["realized_pnl"] == 300.00
    assert trade["r_multiple"] == 3.0


async def test_cancel_event_updates_order_status(journal: Journal) -> None:
    trade_id = await _seed_long_bracket(journal)
    handler = TradeUpdateHandler(_settings(), journal)
    ts = datetime(2026, 4, 15, 13, 50, tzinfo=UTC)
    await handler.dispatch(
        FakeTradeUpdate(
            event="canceled",
            order=FakeOrderPayload(id="parent-1", status="canceled", canceled_at=ts),
        )
    )
    order = await journal.get_order("parent-1")
    assert order is not None
    assert order["status"] == "canceled"
    assert order["canceled_ts"] == ts.isoformat()
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    assert trade["exit_ts"] is None


async def test_unknown_order_id_is_ignored(journal: Journal) -> None:
    handler = TradeUpdateHandler(_settings(), journal)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(id="unknown-1", status="filled", filled_avg_price="1"),
        )
    )
    # No exceptions; order gets an UPDATE that affects 0 rows; no-op.


async def test_malformed_payload_without_order_is_ignored(journal: Journal) -> None:
    handler = TradeUpdateHandler(_settings(), journal)

    @dataclass
    class Broken:
        event: str = "fill"

    await handler.dispatch(Broken())
    # No exception raised.


async def test_fill_on_entry_does_not_close_trade_even_if_no_avg_price(
    journal: Journal,
) -> None:
    trade_id = await _seed_long_bracket(journal)
    handler = TradeUpdateHandler(_settings(), journal)
    await handler.dispatch(
        FakeTradeUpdate(
            event="fill",
            order=FakeOrderPayload(id="parent-1", status="filled"),
        )
    )
    trade = await journal.get_trade(trade_id)
    assert trade is not None
    assert trade["exit_ts"] is None
