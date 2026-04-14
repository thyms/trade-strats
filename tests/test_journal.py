import json
from pathlib import Path

import pytest

from trade_strats.journal import Journal, OrderRecord, TradeRecord

SCHEMA_PATH = Path(__file__).parent.parent / "data" / "schema.sql"


@pytest.fixture
async def journal(tmp_path: Path):
    j = await Journal.open(
        db_path=tmp_path / "trades.db",
        events_path=tmp_path / "events.jsonl",
        schema_path=SCHEMA_PATH,
    )
    async with j:
        yield j


# --- Lifecycle --------------------------------------------------------------


async def test_open_creates_files_and_applies_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "a" / "trades.db"
    events_path = tmp_path / "b" / "events.jsonl"
    j = await Journal.open(db_path, events_path, SCHEMA_PATH)
    async with j:
        assert db_path.exists()
        assert events_path.exists()
        # Tables exist:
        open_trades = await j.get_open_trades()
        assert open_trades == []


# --- Events -----------------------------------------------------------------


async def test_record_event_appends_json_line(journal: Journal, tmp_path: Path) -> None:
    await journal.record_event("bar_seen", symbol="SPY", scenario="2U")
    await journal.record_event("skip", symbol="QQQ", reason="ftfc_mismatch")
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["event"] == "bar_seen"
    assert a["symbol"] == "SPY"
    assert a["scenario"] == "2U"
    assert "ts" in a
    assert b["event"] == "skip"
    assert b["reason"] == "ftfc_mismatch"


# --- Trades -----------------------------------------------------------------


def _sample_trade(**overrides: object) -> TradeRecord:
    defaults: dict[str, object] = {
        "symbol": "SPY",
        "side": "long",
        "pattern": "3-2-2",
        "entry_ts": "2026-04-15T13:45:00Z",
        "entry_price": 512.45,
        "stop_price": 511.20,
        "target_price": 516.20,
        "qty": 100,
        "mode": "paper",
        "ftfc_1d": "green",
        "ftfc_4h": "green",
        "ftfc_1h": "green",
    }
    defaults.update(overrides)
    return TradeRecord(**defaults)  # type: ignore[arg-type]


async def test_insert_trade_returns_id_and_persists(journal: Journal) -> None:
    trade_id = await journal.insert_trade(_sample_trade())
    assert trade_id > 0
    fetched = await journal.get_trade(trade_id)
    assert fetched is not None
    assert fetched["symbol"] == "SPY"
    assert fetched["side"] == "long"
    assert fetched["pattern"] == "3-2-2"
    assert fetched["entry_price"] == 512.45
    assert fetched["exit_ts"] is None


async def test_get_open_trades_excludes_exited(journal: Journal) -> None:
    open_id = await journal.insert_trade(_sample_trade(symbol="SPY"))
    closed_id = await journal.insert_trade(_sample_trade(symbol="QQQ"))
    await journal.update_trade_exit(
        trade_id=closed_id,
        exit_ts="2026-04-15T14:00:00Z",
        exit_price=514.00,
        exit_reason="target",
        realized_pnl=155.00,
        r_multiple=3.0,
    )
    open_trades = await journal.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["id"] == open_id


async def test_update_trade_exit_populates_fields(journal: Journal) -> None:
    trade_id = await journal.insert_trade(_sample_trade())
    await journal.update_trade_exit(
        trade_id=trade_id,
        exit_ts="2026-04-15T14:30:00Z",
        exit_price=516.20,
        exit_reason="target",
        realized_pnl=375.00,
        r_multiple=3.0,
    )
    fetched = await journal.get_trade(trade_id)
    assert fetched is not None
    assert fetched["exit_ts"] == "2026-04-15T14:30:00Z"
    assert fetched["exit_price"] == 516.20
    assert fetched["exit_reason"] == "target"
    assert fetched["realized_pnl"] == 375.00
    assert fetched["r_multiple"] == 3.0


async def test_get_trade_returns_none_for_missing_id(journal: Journal) -> None:
    assert await journal.get_trade(9999) is None


# --- Orders -----------------------------------------------------------------


def _sample_order(**overrides: object) -> OrderRecord:
    defaults: dict[str, object] = {
        "alpaca_order_id": "ord-123",
        "symbol": "SPY",
        "side": "long",
        "kind": "entry",
        "type": "stop_limit",
        "qty": 100,
        "status": "accepted",
        "submitted_ts": "2026-04-15T13:45:00Z",
        "limit_price": 512.45,
        "stop_price": 512.40,
    }
    defaults.update(overrides)
    return OrderRecord(**defaults)  # type: ignore[arg-type]


async def test_insert_order_and_fetch(journal: Journal) -> None:
    order_id = await journal.insert_order(_sample_order())
    assert order_id > 0
    fetched = await journal.get_order("ord-123")
    assert fetched is not None
    assert fetched["alpaca_order_id"] == "ord-123"
    assert fetched["kind"] == "entry"
    assert fetched["status"] == "accepted"


async def test_insert_order_with_trade_fk(journal: Journal) -> None:
    trade_id = await journal.insert_trade(_sample_trade())
    await journal.insert_order(_sample_order(trade_id=trade_id))
    fetched = await journal.get_order("ord-123")
    assert fetched is not None
    assert fetched["trade_id"] == trade_id


async def test_update_order_status_to_filled(journal: Journal) -> None:
    await journal.insert_order(_sample_order())
    await journal.update_order_status(
        alpaca_order_id="ord-123",
        status="filled",
        filled_ts="2026-04-15T13:45:15Z",
        filled_avg_price=512.44,
    )
    fetched = await journal.get_order("ord-123")
    assert fetched is not None
    assert fetched["status"] == "filled"
    assert fetched["filled_ts"] == "2026-04-15T13:45:15Z"
    assert fetched["filled_avg_price"] == 512.44


async def test_update_order_status_to_canceled(journal: Journal) -> None:
    await journal.insert_order(_sample_order())
    await journal.update_order_status(
        alpaca_order_id="ord-123",
        status="canceled",
        canceled_ts="2026-04-15T13:50:00Z",
    )
    fetched = await journal.get_order("ord-123")
    assert fetched is not None
    assert fetched["status"] == "canceled"
    assert fetched["canceled_ts"] == "2026-04-15T13:50:00Z"


async def test_get_order_returns_none_for_missing(journal: Journal) -> None:
    assert await journal.get_order("does-not-exist") is None


# --- Sessions ---------------------------------------------------------------


async def test_session_lifecycle(journal: Journal) -> None:
    date = "2026-04-15"
    await journal.upsert_session_start(date, start_equity=50_000.0)
    s = await journal.get_session(date)
    assert s is not None
    assert s.start_equity == 50_000.0
    assert s.end_equity is None
    assert s.trades_count == 0
    assert s.halted is False

    await journal.update_session_progress(date, trades_count=2, realized_pnl=125.50)
    s = await journal.get_session(date)
    assert s is not None
    assert s.trades_count == 2
    assert s.realized_pnl == 125.50

    await journal.finalize_session(date, end_equity=50_125.50, notes="clean session")
    s = await journal.get_session(date)
    assert s is not None
    assert s.end_equity == 50_125.50
    assert s.notes == "clean session"


async def test_upsert_session_start_is_idempotent(journal: Journal) -> None:
    date = "2026-04-15"
    await journal.upsert_session_start(date, start_equity=50_000.0)
    # Second call with different equity must not overwrite:
    await journal.upsert_session_start(date, start_equity=99_999.0)
    s = await journal.get_session(date)
    assert s is not None
    assert s.start_equity == 50_000.0


async def test_session_halted_flag_round_trip(journal: Journal) -> None:
    date = "2026-04-15"
    await journal.upsert_session_start(date, start_equity=50_000.0)
    await journal.update_session_progress(date, trades_count=3, realized_pnl=-1000.0, halted=True)
    s = await journal.get_session(date)
    assert s is not None
    assert s.halted is True


async def test_get_session_returns_none_for_missing_date(journal: Journal) -> None:
    assert await journal.get_session("1999-01-01") is None


# --- Persistence across reopens --------------------------------------------


async def test_data_persists_after_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "trades.db"
    events = tmp_path / "events.jsonl"
    j1 = await Journal.open(db, events, SCHEMA_PATH)
    async with j1:
        trade_id = await j1.insert_trade(_sample_trade())
        await j1.record_event("bar_seen", symbol="SPY")

    j2 = await Journal.open(db, events, SCHEMA_PATH)
    async with j2:
        fetched = await j2.get_trade(trade_id)
        assert fetched is not None
        assert fetched["symbol"] == "SPY"

    # events.jsonl is append-only across reopens:
    lines = events.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "bar_seen"
