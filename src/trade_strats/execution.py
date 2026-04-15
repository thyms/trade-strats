import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from trade_strats.market_data import AlpacaSettings
from trade_strats.risk import TradePlan
from trade_strats.strategy.patterns import Side

# --- Public result types ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmittedBracket:
    parent_order_id: str
    stop_loss_order_id: str | None
    take_profit_order_id: str | None
    symbol: str
    side: str
    qty: int
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class AccountInfo:
    equity: float
    cash: float
    buying_power: float
    daytrade_count: int


@dataclass(frozen=True, slots=True)
class PositionInfo:
    symbol: str
    qty: int
    side: str  # "long" | "short"
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float


@dataclass(frozen=True, slots=True)
class OrderInfo:
    order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    status: str
    submitted_at: datetime
    filled_at: datetime | None
    filled_avg_price: float | None


# --- Helpers ---------------------------------------------------------------


def _round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def _order_side(side: Side) -> OrderSide:
    return OrderSide.BUY if side is Side.LONG else OrderSide.SELL


def _enum_value(obj: Any, default: str = "") -> str:
    """Return .value if present (enum), else str(obj), else default."""
    if obj is None:
        return default
    value = getattr(obj, "value", None)
    if isinstance(value, str):
        return value
    return str(obj)


def _child_id_by_type(legs: list[Any], order_type_value: str) -> str | None:
    for leg in legs:
        leg_type = getattr(leg, "order_type", None) or getattr(leg, "type", None)
        if _enum_value(leg_type) == order_type_value:
            leg_id = getattr(leg, "id", None)
            return None if leg_id is None else str(leg_id)
    return None


def _to_account_info(account: Any) -> AccountInfo:
    return AccountInfo(
        equity=float(account.equity),
        cash=float(account.cash),
        buying_power=float(account.buying_power),
        daytrade_count=int(getattr(account, "daytrade_count", 0) or 0),
    )


def _to_position_info(pos: Any) -> PositionInfo:
    side_str = _enum_value(getattr(pos, "side", None), default="long")
    return PositionInfo(
        symbol=str(pos.symbol),
        qty=abs(int(float(pos.qty))),
        side="long" if side_str.lower() == "long" else "short",
        avg_entry_price=float(pos.avg_entry_price),
        current_price=float(pos.current_price),
        unrealized_pnl=float(getattr(pos, "unrealized_pl", 0.0) or 0.0),
    )


def _to_order_info(order: Any) -> OrderInfo:
    side_str = _enum_value(getattr(order, "side", None))
    raw_type = getattr(order, "order_type", None) or getattr(order, "type", None)
    return OrderInfo(
        order_id=str(order.id),
        symbol=str(order.symbol),
        side="long" if side_str.lower() == "buy" else "short",
        qty=int(float(getattr(order, "qty", 0) or 0)),
        order_type=_enum_value(raw_type),
        status=_enum_value(getattr(order, "status", None)),
        submitted_at=order.submitted_at,
        filled_at=getattr(order, "filled_at", None),
        filled_avg_price=(
            float(order.filled_avg_price) if getattr(order, "filled_avg_price", None) else None
        ),
    )


# --- Executor --------------------------------------------------------------


class Executor:
    """Places + manages Alpaca bracket orders for TradePlans.

    `submit_bracket(symbol, plan)` issues a single parent stop-limit order with
    attached stop-loss and take-profit child orders (alpaca bracket class).
    Child orders live on Alpaca's servers; they continue protecting the position
    even if this bot is offline. This is why `flat_all()` and `close_position()`
    are explicit — graceful shutdown does NOT cancel brackets.
    """

    def __init__(
        self,
        settings: AlpacaSettings,
        *,
        client_factory: Callable[[], Any] | None = None,
        tick_size: float = 0.01,
        limit_slippage_ticks: int = 5,
    ) -> None:
        self._settings = settings
        self._factory = client_factory
        self._client: Any = None
        self._tick = tick_size
        self._slippage_ticks = limit_slippage_ticks

    def _tc(self) -> Any:
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                self._client = TradingClient(
                    api_key=self._settings.api_key,
                    secret_key=self._settings.api_secret,
                    paper=self._settings.paper,
                )
        return self._client

    def _parent_limit(self, plan: TradePlan) -> float:
        slippage = self._tick * self._slippage_ticks
        if plan.side is Side.LONG:
            return _round_to_tick(plan.entry_price + slippage, self._tick)
        return _round_to_tick(plan.entry_price - slippage, self._tick)

    async def submit_bracket(self, symbol: str, plan: TradePlan) -> SubmittedBracket:
        request = StopLimitOrderRequest(
            symbol=symbol,
            qty=plan.qty,
            side=_order_side(plan.side),
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_price=plan.entry_price,
            limit_price=self._parent_limit(plan),
            take_profit=TakeProfitRequest(limit_price=plan.target_price),
            stop_loss=StopLossRequest(stop_price=plan.stop_price),
        )
        order = await asyncio.to_thread(self._tc().submit_order, request)
        legs = list(getattr(order, "legs", None) or [])
        return SubmittedBracket(
            parent_order_id=str(order.id),
            stop_loss_order_id=_child_id_by_type(legs, "stop"),
            take_profit_order_id=_child_id_by_type(legs, "limit"),
            symbol=symbol,
            side=plan.side.value,
            qty=plan.qty,
            submitted_at=order.submitted_at,
        )

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.to_thread(self._tc().cancel_order_by_id, order_id)

    async def cancel_all_orders(self) -> None:
        await asyncio.to_thread(self._tc().cancel_orders)

    async def close_position(self, symbol: str) -> None:
        await asyncio.to_thread(self._tc().close_position, symbol)

    async def flat_all(self) -> None:
        """Emergency flatten: cancel all orders + market-close every position."""
        await asyncio.to_thread(self._tc().cancel_orders)
        await asyncio.to_thread(self._tc().close_all_positions, True)

    async def get_account(self) -> AccountInfo:
        account = await asyncio.to_thread(self._tc().get_account)
        return _to_account_info(account)

    async def get_positions(self) -> list[PositionInfo]:
        positions = await asyncio.to_thread(self._tc().get_all_positions)
        return [_to_position_info(p) for p in positions]

    async def get_open_orders(self) -> list[OrderInfo]:
        request = GetOrdersRequest(status="open")  # pyright: ignore[reportArgumentType]
        orders = await asyncio.to_thread(self._tc().get_orders, request)
        return [_to_order_info(o) for o in orders]
