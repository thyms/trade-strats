from typing import Any

from alpaca.trading.stream import TradingStream

from trade_strats.journal import Journal
from trade_strats.market_data import AlpacaSettings


def _enum_str(obj: Any, default: str = "") -> str:
    if obj is None:
        return default
    value = getattr(obj, "value", None)
    if isinstance(value, str):
        return value
    return str(obj)


class TradeUpdateHandler:
    """Consumes Alpaca TradingStream events and persists order/trade status changes.

    Delivers three journal effects per fill event on a known order:
    1. Order status row updated (fill timestamp + avg price).
    2. 'trade_update' event line appended to events.jsonl.
    3. For stop/target child fills, the parent trade is closed out with
       realized_pnl and r_multiple computed from entry/exit/qty/side.
    """

    def __init__(self, settings: AlpacaSettings, journal: Journal) -> None:
        self._settings = settings
        self._journal = journal
        self._stream: TradingStream | None = None

    async def dispatch(self, data: Any) -> None:
        """Handle one trade update. Public for testing; ignores malformed payloads."""
        order = getattr(data, "order", None)
        event = getattr(data, "event", None)
        if order is None:
            return

        order_id = str(getattr(order, "id", ""))
        if not order_id:
            return

        event_str = _enum_str(event)
        status_str = _enum_str(getattr(order, "status", None))
        filled_at = getattr(order, "filled_at", None)
        filled_avg_price_raw = getattr(order, "filled_avg_price", None)
        canceled_at = getattr(order, "canceled_at", None)

        filled_avg_price: float | None = (
            float(filled_avg_price_raw) if filled_avg_price_raw is not None else None
        )

        await self._journal.update_order_status(
            alpaca_order_id=order_id,
            status=status_str,
            filled_ts=filled_at.isoformat() if filled_at is not None else None,
            filled_avg_price=filled_avg_price,
            canceled_ts=canceled_at.isoformat() if canceled_at is not None else None,
        )
        await self._journal.record_event(
            "trade_update",
            update_type=event_str,
            order_id=order_id,
            status=status_str,
        )

        if event_str != "fill":
            return

        tracked = await self._journal.get_order(order_id)
        if tracked is None:
            return
        kind = tracked.get("kind")
        if kind not in ("stop", "target"):
            return
        trade_id = tracked.get("trade_id")
        if trade_id is None:
            return

        trade = await self._journal.get_trade(int(trade_id))
        if trade is None or trade.get("exit_ts") is not None:
            return

        exit_price = filled_avg_price if filled_avg_price is not None else 0.0
        entry_price = float(trade["entry_price"])
        stop_price = float(trade["stop_price"])
        qty = int(trade["qty"])
        side = str(trade["side"])
        risk_per_share = abs(entry_price - stop_price)

        if side == "long":
            realized_pnl = (exit_price - entry_price) * qty
        else:
            realized_pnl = (entry_price - exit_price) * qty
        r_multiple = (
            realized_pnl / (risk_per_share * qty) if risk_per_share > 0 and qty > 0 else 0.0
        )

        await self._journal.update_trade_exit(
            trade_id=int(trade_id),
            exit_ts=filled_at.isoformat() if filled_at is not None else "",
            exit_price=exit_price,
            exit_reason=str(kind),
            realized_pnl=round(realized_pnl, 2),
            r_multiple=round(r_multiple, 4),
        )

    async def run(self) -> None:
        """Subscribe to trade updates on Alpaca and stream forever."""
        stream = TradingStream(
            api_key=self._settings.api_key,
            secret_key=self._settings.api_secret,
            paper=self._settings.paper,
        )
        stream.subscribe_trade_updates(self.dispatch)  # pyright: ignore[reportUnknownMemberType]
        self._stream = stream
        try:
            await stream._run_forever()  # pyright: ignore[reportPrivateUsage]
        finally:
            self._stream = None
