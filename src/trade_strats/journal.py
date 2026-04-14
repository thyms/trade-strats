import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO

import aiosqlite


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    symbol: str
    side: str
    pattern: str
    entry_ts: str
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    mode: str
    ftfc_1d: str | None = None
    ftfc_4h: str | None = None
    ftfc_1h: str | None = None


@dataclass(frozen=True, slots=True)
class OrderRecord:
    alpaca_order_id: str
    symbol: str
    side: str
    kind: str  # entry | stop | target
    type: str  # stop_limit | stop | limit
    qty: int
    status: str
    submitted_ts: str
    trade_id: int | None = None
    limit_price: float | None = None
    stop_price: float | None = None


@dataclass(frozen=True, slots=True)
class SessionStats:
    session_date: str
    start_equity: float
    end_equity: float | None
    trades_count: int
    realized_pnl: float
    halted: bool
    notes: str | None


class Journal:
    """Persists trades + orders to SQLite and decision events to JSONL.

    Lifecycle: `j = await Journal.open(...)`, then `async with j: ...`.
    All write methods commit immediately; crash recovery relies on SQLite's
    WAL. events.jsonl is line-buffered append-only.
    """

    def __init__(self, db: aiosqlite.Connection, events_file: TextIO) -> None:
        self._db = db
        self._events_file = events_file

    @classmethod
    async def open(
        cls,
        db_path: Path,
        events_path: Path,
        schema_path: Path,
    ) -> Self:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        schema_sql = schema_path.read_text()
        db = await aiosqlite.connect(db_path)
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(schema_sql)
        await db.commit()
        events_file = events_path.open("a", buffering=1)
        return cls(db=db, events_file=events_file)

    async def close(self) -> None:
        await self._db.close()
        self._events_file.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # --- Events -------------------------------------------------------------

    async def record_event(self, event: str, **data: Any) -> None:
        payload: dict[str, Any] = {"ts": utcnow_iso(), "event": event, **data}
        line = json.dumps(payload, separators=(",", ":"), default=str)
        self._events_file.write(line + "\n")

    # --- Trades -------------------------------------------------------------

    async def insert_trade(self, trade: TradeRecord) -> int:
        cursor = await self._db.execute(
            """
            INSERT INTO trades (
                symbol, side, pattern, entry_ts, entry_price,
                stop_price, target_price, qty, mode,
                ftfc_1d, ftfc_4h, ftfc_1h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.symbol,
                trade.side,
                trade.pattern,
                trade.entry_ts,
                trade.entry_price,
                trade.stop_price,
                trade.target_price,
                trade.qty,
                trade.mode,
                trade.ftfc_1d,
                trade.ftfc_4h,
                trade.ftfc_1h,
            ),
        )
        await self._db.commit()
        trade_id = cursor.lastrowid
        if trade_id is None:
            raise RuntimeError("insert_trade did not return a row id")
        return trade_id

    async def update_trade_exit(
        self,
        trade_id: int,
        exit_ts: str,
        exit_price: float,
        exit_reason: str,
        realized_pnl: float,
        r_multiple: float,
    ) -> None:
        await self._db.execute(
            """
            UPDATE trades
               SET exit_ts=?, exit_price=?, exit_reason=?, realized_pnl=?, r_multiple=?
             WHERE id=?
            """,
            (exit_ts, exit_price, exit_reason, realized_pnl, r_multiple, trade_id),
        )
        await self._db.commit()

    async def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        async with self._db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
            return dict(zip(columns, row, strict=True))

    async def get_open_trades(self) -> list[dict[str, Any]]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE exit_ts IS NULL ORDER BY entry_ts"
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in rows]

    # --- Orders -------------------------------------------------------------

    async def insert_order(self, order: OrderRecord) -> int:
        cursor = await self._db.execute(
            """
            INSERT INTO orders (
                alpaca_order_id, trade_id, symbol, side, kind, type, qty,
                limit_price, stop_price, status, submitted_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.alpaca_order_id,
                order.trade_id,
                order.symbol,
                order.side,
                order.kind,
                order.type,
                order.qty,
                order.limit_price,
                order.stop_price,
                order.status,
                order.submitted_ts,
            ),
        )
        await self._db.commit()
        order_id = cursor.lastrowid
        if order_id is None:
            raise RuntimeError("insert_order did not return a row id")
        return order_id

    async def update_order_status(
        self,
        alpaca_order_id: str,
        status: str,
        filled_ts: str | None = None,
        filled_avg_price: float | None = None,
        canceled_ts: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE orders
               SET status=?, filled_ts=?, filled_avg_price=?, canceled_ts=?
             WHERE alpaca_order_id=?
            """,
            (status, filled_ts, filled_avg_price, canceled_ts, alpaca_order_id),
        )
        await self._db.commit()

    async def get_order(self, alpaca_order_id: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT * FROM orders WHERE alpaca_order_id=?", (alpaca_order_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
            return dict(zip(columns, row, strict=True))

    # --- Sessions -----------------------------------------------------------

    async def upsert_session_start(self, session_date: str, start_equity: float) -> None:
        await self._db.execute(
            """
            INSERT INTO sessions (session_date, start_equity)
            VALUES (?, ?)
            ON CONFLICT(session_date) DO NOTHING
            """,
            (session_date, start_equity),
        )
        await self._db.commit()

    async def update_session_progress(
        self,
        session_date: str,
        trades_count: int,
        realized_pnl: float,
        halted: bool = False,
    ) -> None:
        await self._db.execute(
            """
            UPDATE sessions
               SET trades_count=?, realized_pnl=?, halted=?
             WHERE session_date=?
            """,
            (trades_count, realized_pnl, int(halted), session_date),
        )
        await self._db.commit()

    async def finalize_session(
        self,
        session_date: str,
        end_equity: float,
        notes: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            UPDATE sessions
               SET end_equity=?, notes=?
             WHERE session_date=?
            """,
            (end_equity, notes, session_date),
        )
        await self._db.commit()

    async def get_session(self, session_date: str) -> SessionStats | None:
        async with self._db.execute(
            "SELECT * FROM sessions WHERE session_date=?", (session_date,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            columns = [d[0] for d in cursor.description]
            d = dict(zip(columns, row, strict=True))
            return SessionStats(
                session_date=d["session_date"],
                start_equity=d["start_equity"],
                end_equity=d["end_equity"],
                trades_count=d["trades_count"],
                realized_pnl=d["realized_pnl"],
                halted=bool(d["halted"]),
                notes=d["notes"],
            )
