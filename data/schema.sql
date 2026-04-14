-- TheStrat bot SQLite schema. Loaded at startup via CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ('long', 'short')),
    pattern         TEXT    NOT NULL,
    entry_ts        TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    stop_price      REAL    NOT NULL,
    target_price    REAL    NOT NULL,
    qty             INTEGER NOT NULL,
    exit_ts         TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    realized_pnl    REAL,
    r_multiple      REAL,
    ftfc_1d         TEXT,
    ftfc_4h         TEXT,
    ftfc_1h         TEXT,
    mode            TEXT    NOT NULL CHECK (mode IN ('paper', 'live'))
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_entry_ts ON trades(entry_ts);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    alpaca_order_id     TEXT    NOT NULL UNIQUE,
    trade_id            INTEGER REFERENCES trades(id) ON DELETE SET NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    kind                TEXT    NOT NULL,  -- entry | stop | target
    type                TEXT    NOT NULL,  -- stop_limit | stop | limit
    qty                 INTEGER NOT NULL,
    limit_price         REAL,
    stop_price          REAL,
    status              TEXT    NOT NULL,
    submitted_ts        TEXT    NOT NULL,
    filled_ts           TEXT,
    filled_avg_price    REAL,
    canceled_ts         TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_trade_id ON orders(trade_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS bars_cache (
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,  -- 1Min | 15Min | 1H | 4H | 1D
    ts              TEXT    NOT NULL,  -- ISO8601 UTC
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          INTEGER NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_date    TEXT    PRIMARY KEY,  -- YYYY-MM-DD ET
    start_equity    REAL    NOT NULL,
    end_equity      REAL,
    trades_count    INTEGER NOT NULL DEFAULT 0,
    realized_pnl    REAL    NOT NULL DEFAULT 0,
    halted          INTEGER NOT NULL DEFAULT 0,  -- boolean: daily loss cap hit
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS state_kv (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    updated_ts TEXT NOT NULL
);
