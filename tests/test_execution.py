import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from trade_strats.execution import (
    AccountInfo,
    Executor,
    OrderInfo,
    PositionInfo,
    SubmittedBracket,
)
from trade_strats.market_data import AlpacaSettings
from trade_strats.risk import TradePlan
from trade_strats.strategy.labeler import Bar
from trade_strats.strategy.patterns import PatternKind, Side

# --- Fakes ------------------------------------------------------------------


def _settings(*, base_url: str = "https://paper-api.alpaca.markets") -> AlpacaSettings:
    return AlpacaSettings(api_key="k", api_secret="s", base_url=base_url, data_feed="iex")


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


class FakeTradingClient:
    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.canceled_ids: list[str] = []
        self.cancel_orders_called = False
        self.closed_positions: list[str] = []
        self.close_all_called = False
        self.account = FakeAccount()
        self.positions: list[FakePosition] = []
        self.open_orders: list[FakeOrder] = []
        self._next_order_id = 1
        self._response_order: FakeOrder | None = None

    def set_submit_response(self, order: FakeOrder) -> None:
        self._response_order = order

    def submit_order(self, request: Any) -> FakeOrder:
        self.submitted.append(request)
        if self._response_order is not None:
            return self._response_order
        order = FakeOrder(id=f"ord-{self._next_order_id}", symbol=request.symbol)
        self._next_order_id += 1
        return order

    def cancel_order_by_id(self, order_id: str) -> None:
        self.canceled_ids.append(order_id)

    def cancel_orders(self) -> list[Any]:
        self.cancel_orders_called = True
        return []

    def close_position(self, symbol: str) -> Any:
        self.closed_positions.append(symbol)
        return None

    def close_all_positions(self, cancel_orders: bool) -> list[Any]:
        self.close_all_called = True
        return []

    def get_account(self) -> FakeAccount:
        return self.account

    def get_all_positions(self) -> list[FakePosition]:
        return self.positions

    def get_orders(self, request: Any) -> list[FakeOrder]:
        return self.open_orders


def _executor(fake: FakeTradingClient) -> Executor:
    return Executor(_settings(), client_factory=lambda: fake)


def _long_plan(qty: int = 100) -> TradePlan:
    return TradePlan(
        kind=PatternKind.THREE_TWO_TWO,
        side=Side.LONG,
        entry_price=101.01,
        stop_price=98.99,
        target_price=107.07,
        qty=qty,
        risk_per_share=2.02,
        total_risk_usd=qty * 2.02,
        signal_bar=Bar(open=100, high=101, low=99, close=100.8),
    )


def _short_plan(qty: int = 100) -> TradePlan:
    return TradePlan(
        kind=PatternKind.THREE_TWO_TWO,
        side=Side.SHORT,
        entry_price=98.99,
        stop_price=101.01,
        target_price=92.93,
        qty=qty,
        risk_per_share=2.02,
        total_risk_usd=qty * 2.02,
        signal_bar=Bar(open=100, high=101, low=99, close=99.2),
    )


# --- AlpacaSettings.paper derivation ----------------------------------------


def test_paper_true_for_paper_url() -> None:
    assert _settings(base_url="https://paper-api.alpaca.markets").paper is True


def test_paper_false_for_live_url() -> None:
    assert _settings(base_url="https://api.alpaca.markets").paper is False


# --- submit_bracket: request construction ----------------------------------


async def test_submit_bracket_long_builds_buy_stop_limit() -> None:
    fake = FakeTradingClient()
    ex = _executor(fake)
    await ex.submit_bracket("SPY", _long_plan())

    assert len(fake.submitted) == 1
    req = fake.submitted[0]
    assert req.symbol == "SPY"
    assert req.qty == 100
    assert req.side.value == "buy"
    assert req.order_class.value == "bracket"
    assert req.time_in_force.value == "day"
    assert req.stop_price == 101.01
    # default 5-tick slippage: 101.01 + 0.05 = 101.06
    assert req.limit_price == pytest.approx(101.06)
    assert req.take_profit.limit_price == 107.07
    assert req.stop_loss.stop_price == 98.99


async def test_submit_bracket_short_builds_sell_stop_limit() -> None:
    fake = FakeTradingClient()
    ex = _executor(fake)
    await ex.submit_bracket("SPY", _short_plan())

    req = fake.submitted[0]
    assert req.side.value == "sell"
    assert req.stop_price == 98.99
    # short limit goes below stop by slippage
    assert req.limit_price == pytest.approx(98.94)
    assert req.take_profit.limit_price == 92.93
    assert req.stop_loss.stop_price == 101.01


async def test_submit_bracket_custom_slippage_ticks() -> None:
    fake = FakeTradingClient()
    ex = Executor(_settings(), client_factory=lambda: fake, limit_slippage_ticks=10)
    await ex.submit_bracket("SPY", _long_plan())
    req = fake.submitted[0]
    assert req.limit_price == pytest.approx(101.11)


async def test_submit_bracket_returns_ids_from_legs() -> None:
    fake = FakeTradingClient()
    fake.set_submit_response(
        FakeOrder(
            id="parent-1",
            symbol="SPY",
            legs=[
                FakeOrderLeg(id="stop-1", order_type="stop"),
                FakeOrderLeg(id="tp-1", order_type="limit"),
            ],
        )
    )
    ex = _executor(fake)
    submitted = await ex.submit_bracket("SPY", _long_plan())
    assert isinstance(submitted, SubmittedBracket)
    assert submitted.parent_order_id == "parent-1"
    assert submitted.stop_loss_order_id == "stop-1"
    assert submitted.take_profit_order_id == "tp-1"
    assert submitted.symbol == "SPY"
    assert submitted.side == "long"
    assert submitted.qty == 100


async def test_submit_bracket_handles_missing_legs() -> None:
    fake = FakeTradingClient()
    fake.set_submit_response(FakeOrder(id="parent-2", symbol="SPY"))
    ex = _executor(fake)
    submitted = await ex.submit_bracket("SPY", _long_plan())
    assert submitted.parent_order_id == "parent-2"
    assert submitted.stop_loss_order_id is None
    assert submitted.take_profit_order_id is None


# --- cancel / close ---------------------------------------------------------


async def test_cancel_order_delegates_to_client() -> None:
    fake = FakeTradingClient()
    await _executor(fake).cancel_order("ord-xyz")
    assert fake.canceled_ids == ["ord-xyz"]


async def test_cancel_all_orders_calls_client_cancel_orders() -> None:
    fake = FakeTradingClient()
    await _executor(fake).cancel_all_orders()
    assert fake.cancel_orders_called is True


async def test_close_position_delegates_by_symbol() -> None:
    fake = FakeTradingClient()
    await _executor(fake).close_position("SPY")
    assert fake.closed_positions == ["SPY"]


async def test_flat_all_cancels_then_closes_all() -> None:
    fake = FakeTradingClient()
    await _executor(fake).flat_all()
    assert fake.cancel_orders_called is True
    assert fake.close_all_called is True


# --- get_account / get_positions / get_open_orders ------------------------


async def test_get_account_returns_account_info() -> None:
    fake = FakeTradingClient()
    fake.account = FakeAccount(
        equity="50123.45",
        cash="10000.00",
        buying_power="200000.00",
        daytrade_count=2,
    )
    info = await _executor(fake).get_account()
    assert isinstance(info, AccountInfo)
    assert info.equity == 50123.45
    assert info.cash == 10000.00
    assert info.buying_power == 200000.00
    assert info.daytrade_count == 2


async def test_get_positions_maps_to_domain_type() -> None:
    fake = FakeTradingClient()
    fake.positions = [
        FakePosition(
            symbol="SPY",
            qty="100",
            side="long",
            avg_entry_price="512.45",
            current_price="513.10",
            unrealized_pl="65.00",
        ),
        FakePosition(
            symbol="QQQ",
            qty="-50",
            side="short",
            avg_entry_price="438.00",
            current_price="437.50",
            unrealized_pl="25.00",
        ),
    ]
    positions = await _executor(fake).get_positions()
    assert len(positions) == 2
    assert positions[0] == PositionInfo(
        symbol="SPY",
        qty=100,
        side="long",
        avg_entry_price=512.45,
        current_price=513.10,
        unrealized_pnl=65.00,
    )
    assert positions[1].symbol == "QQQ"
    assert positions[1].side == "short"
    assert positions[1].qty == 50  # abs value


async def test_get_open_orders_maps_and_filters_by_status() -> None:
    fake = FakeTradingClient()
    fake.open_orders = [
        FakeOrder(
            id="a",
            symbol="SPY",
            side="buy",
            order_type="stop_limit",
            status="accepted",
            qty="100",
        ),
        FakeOrder(
            id="b",
            symbol="SPY",
            side="sell",
            order_type="limit",
            status="accepted",
            qty="100",
        ),
    ]
    orders = await _executor(fake).get_open_orders()
    assert len(orders) == 2
    assert isinstance(orders[0], OrderInfo)
    assert orders[0].order_id == "a"
    assert orders[0].side == "long"
    assert orders[1].side == "short"


# --- Integration (opt-in) --------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("ALPACA_API_KEY"),
    reason="ALPACA_API_KEY not set — skipping live trading API integration test",
)
async def test_get_account_from_live_paper_api() -> None:
    ex = Executor(AlpacaSettings.from_env())
    info = await ex.get_account()
    assert info.equity >= 0.0
    assert info.buying_power >= 0.0
