# trade-strats

Deterministic TheStrat 15m trading bot for US equities via Alpaca. See [`docs/prd/draft.md`](docs/prd/draft.md) for the full spec.

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- An [Alpaca](https://alpaca.markets/) account (paper trading is free)

## First-time setup

```bash
# 1. Install dependencies (creates .venv and uv.lock-pinned deps)
uv sync

# 2. Copy config template and edit if needed
cp config/config.example.yaml config/config.yaml

# 3. Copy env template and add Alpaca credentials
cp .env.example .env
# then edit .env and set ALPACA_API_KEY and ALPACA_API_SECRET
```

Paper-trading keys are generated from the Alpaca paper dashboard
(https://app.alpaca.markets/paper/dashboard/overview) under "Your API Keys".

Neither `config/config.yaml` nor `.env` is committed — both are in `.gitignore`.
`config/config.example.yaml` and `.env.example` are the sources of truth.

## Commands

```bash
# Account snapshot (read-only)
uv run trade-strats status

# Start the bot (subscribes to WS + evaluates 15m closes until EOD)
uv run trade-strats run

# Compare Alpaca live state to SQLite journal; exit non-zero on drift
uv run trade-strats reconcile

# Replay historical data through the same strategy code (no orders)
uv run trade-strats backtest --symbol SPY --start 2026-03-01 --end 2026-04-01

# Same, across the whole config watchlist, with per-pattern breakdown
uv run trade-strats walk-forward --start 2026-03-01 --end 2026-04-01

# Emergency: cancel all orders + market-close all positions
uv run trade-strats flat-all
```

## Development

```bash
# Run the full test suite (uses in-memory fakes by default)
uv run pytest

# Run the live-API integration tests (needs ALPACA_API_KEY in shell env)
set -a && source .env && set +a
uv run pytest -k "alpaca or backfill_returns or get_account_from_live" -v

# Lint + format + type-check
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## Runtime data

The bot writes to `data/trades.db` (SQLite) and `data/events.jsonl` during
live or paper runs. Both are gitignored. `data/schema.sql` is the canonical
schema and is loaded at startup. Back up `data/` outside git if you want
trade history persistence (iCloud/Dropbox/S3 — see PRD §12.5).
