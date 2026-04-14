# TheStrat 15m Trading Bot — Product Requirements Document

**Version:** 2.0 (Draft)
**Date:** 2026-04-14
**Status:** Draft — pending review
**Owner:** dkalfa

---

## 1. Executive Summary

A deterministic Python trading bot that applies **Rob Smith's "The Strat"** price-action methodology to a configurable watchlist of US equities on the **15-minute timeframe**, using **Alpaca** for real-time market data and order execution. The system enters mechanical setups with bracket orders (OCO stop + 3R target), enforces full timeframe continuity (FTFC), and ships paper-first with a live-trading config flag.

**Why no LLM agents:** The Strat reduces to precise, unambiguous candle relationships (inside/directional/outside bar) plus well-defined multi-bar combos. LLMs would add latency, cost, and non-determinism for no gain over a state machine. No LLMs anywhere in the stack — the structured daily report covers post-session review.

---

## 2. Goals & Non-Goals

### Goals (v1)
- Scan a configurable watchlist of liquid US equities every 15m bar close.
- Detect Strat reversal/continuation patterns: 3-2-2 (both sides), 2-2 reversals (both sides), 3-1-2 and Rev Strats.
- Gate every entry on Full Timeframe Continuity (FTFC) across Daily / 4H / 1H.
- Enforce a hard minimum **3R** reward:risk filter and account-level risk caps.
- Place bracket orders (entry + stop + target) via Alpaca paper account.
- Emit structured logs, a trade journal, and daily performance report.
- Provide a backtest harness replaying historical Alpaca bars.

### Non-Goals (v1)
- No crypto, options, or futures.
- No ML/LLM in the signal or execution path.
- No dynamic ticker scanner (watchlist is static in config; scanner is v2).
- No portfolio optimization or position sizing beyond fixed-% risk per trade.
- No trailing stops beyond the defined initial 3R target (v2 enhancement).
- No GUI / web dashboard — CLI + logs + optional PostHog/Grafana later.

---

## 3. Background: The Strat Methodology

The Strat, developed by Rob Smith, classifies every candle by its relationship to the **prior candle**:

| Scenario | Name | Definition |
|---|---|---|
| **1** | Inside bar | Current high ≤ prior high **and** current low ≥ prior low. Consolidation. |
| **2** | Directional bar | Breaks **one** side of prior bar only. `2U` if breaks high, `2D` if breaks low. |
| **3** | Outside bar | Breaks **both** sides of prior bar (engulfs range). Expansion. |

### Full Timeframe Continuity (FTFC)
Strength of a setup is measured by how many timeframes show the same directional bias simultaneously. A long is "full green" when Daily, 4H, and 1H are all trading above their respective opens; "full red" is the mirror for shorts.

### Actionable Patterns Traded in v1

| Pattern | Meaning | Entry Trigger |
|---|---|---|
| **3-2-2 Bullish Reversal** | Red outside bar → red 2D → green 2U | Break of high of the 2U candle |
| **3-2-2 Bearish Reversal** | Green outside bar → green 2U → red 2D | Break of low of the 2D candle |
| **2-2 Bullish Reversal** | Red 2D → green 2U (at support / FTFC flip) | Break of high of the 2U candle |
| **2-2 Bearish Reversal** | Green 2U → red 2D (at resistance / FTFC flip) | Break of low of the 2D candle |
| **3-1-2 Bullish** | Outside bar → inside bar → 2U | Break of high of the 2U candle |
| **3-1-2 Bearish** | Outside bar → inside bar → 2D | Break of low of the 2D candle |
| **Rev Strat (1-2-2)** | Inside bar → failed 2 → reversed 2 | Break of the reversal candle |

### Key Sources Consulted
- [Introduction to The Strat Patterns — strat.trading](https://strat.trading/introduction/introduction-to-the-strat-patterns/)
- [TheStrat Candlestick Patterns — TrendSpider Learning Center](https://trendspider.com/learning-center/thestrat-candlestick-patterns-a-traders-guide/)
- [The Strat Fundamentals (Alaric Securities PDF)](https://alaricsecurities.com/downloads/Alaric_Secuties_The_Strat_Fundamentals_%20Practical_Guide.pdf)
- [How Can You Use the STRAT Method — FXOpen](https://fxopen.com/blog/en/how-can-you-use-the-strat-method-in-trading/)
- [Strat Candlestick Patterns — LuxAlgo](https://www.luxalgo.com/blog/strat-candlestick-patterns-an-innovative-guide/)
- [Three Scenarios in The Strat Patterns — strat.trading](https://strat.trading/the-scenarios/three-scenarios-in-the-strat-patterns/)

---

## 4. Strategy Specification (15m)

### 4.1 Bar Labeling
For each new 15m bar `B_t` and its predecessor `B_{t-1}`:
```
if B_t.high <= B_{t-1}.high and B_t.low >= B_{t-1}.low:        scenario = 1
elif B_t.high > B_{t-1}.high and B_t.low < B_{t-1}.low:        scenario = 3
elif B_t.high > B_{t-1}.high and B_t.low >= B_{t-1}.low:       scenario = 2U
elif B_t.low  < B_{t-1}.low  and B_t.high <= B_{t-1}.high:     scenario = 2D
else:                                                           scenario = 1   # degenerate
```
Color: `green` if close > open, `red` if close < open, `doji` if equal.

### 4.2 FTFC Gate
Before any entry, all three higher TFs must agree with the trade direction:
- **Long:** current price of Daily, 4H, 1H each > that TF's open.
- **Short:** current price of Daily, 4H, 1H each < that TF's open.

If FTFC is not met → **skip**. No partial-continuity trades in v1.

### 4.3 Entry / Stop / Target (Long example; shorts are mirror)
- **Signal candle:** the confirming 2U (green) that completes a pattern above.
- **Entry:** `signal.high + 1 tick` via stop-limit or stop-market (configurable). Order valid for next bar only; cancel if not filled within N minutes (default: one bar, 15m).
- **Stop loss:** `signal.low - 1 tick` (the Strat convention; tighter than the draft's "below open").
- **Target:** `entry + 3 * (entry - stop)`. If the next identifiable broadening-formation resistance or prior swing high is **closer** than 3R → **skip** the setup (don't lower the bar).
- **Position size:** `floor((account_equity * risk_pct_per_trade) / (entry - stop))`.

### 4.4 Session & Hygiene Rules
- Trade only **09:30–15:45 ET** (no new entries in the final 15 min; existing positions may exit via bracket).
- **Flat by 15:55 ET** — force-close any open position.
- No trades during scheduled FOMC / CPI releases (config: blackout list by datetime).
- Skip if the 15m bar's range is < `min_bar_range_atr * ATR(14)` (too quiet, default 0.5).
- One open position per symbol; max `N` concurrent positions across portfolio (default 3).

---

## 5. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Orchestrator                         │
│  (asyncio event loop; subscribes to bar events + clock)      │
└────┬───────────────┬──────────────┬──────────────┬──────────┘
     │               │              │              │
┌────▼────┐   ┌──────▼─────┐  ┌─────▼─────┐  ┌────▼────────┐
│ Market  │   │  Strategy  │  │   Risk    │  │  Execution  │
│  Data   │──▶│   Engine   │─▶│  Manager  │─▶│   Broker    │
│ (ws+    │   │ (labeler + │  │ (3R, caps,│  │  (Alpaca    │
│  rest)  │   │  patterns) │  │  sizing)  │  │   orders)   │
└─────────┘   └────────────┘  └───────────┘  └─────────────┘
     │                                               │
     └──────────────┬───────────────────────┬────────┘
                    ▼                       ▼
             ┌────────────┐         ┌──────────────┐
             │  Journal   │         │  State Store │
             │ (SQLite +  │         │  (positions, │
             │  JSONL)    │         │   orders)    │
             └────────────┘         └──────────────┘
```

No LLMs in this path. Single process, async I/O, in-memory bar cache per symbol per TF, persisted to SQLite for crash recovery.

### Modules
| Module | Responsibility |
|---|---|
| `market_data` | Alpaca WS subscription (15m bars), REST backfill for Daily/4H/1H, bar cache |
| `strategy` | Scenario labeling, pattern detection, FTFC check |
| `risk` | Position sizing, 3R validation, exposure caps, blackout windows |
| `execution` | Bracket order placement/cancellation via Alpaca REST |
| `journal` | Structured trade log (SQLite) + JSONL event stream |
| `backtest` | Replay historical bars through the same strategy/risk modules |
| `config` | Pydantic settings (env + YAML) |
| `cli` | Entry points: `run`, `backtest`, `report` |

---

## 6. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| **Language** | Python 3.12 | Better asyncio, pattern matching, type inference |
| **Packaging / env** | `uv` | Fast resolver + lockfile; replaces pip/poetry/venv |
| **Async runtime** | stdlib `asyncio` | Single event loop; no extra framework needed |
| **Broker + market data** | `alpaca-py` (official SDK) | WS streams + REST in one SDK. `alpaca-trade-api` is legacy. |
| **Data math** | `pandas` + `numpy` | Bar aggregation, higher-TF rollup, backtest math |
| **Config** | `pydantic` v2 + `pyyaml` + `python-dotenv` | Typed config; YAML for humans; `.env` for secrets |
| **Storage** | stdlib `sqlite3` via `aiosqlite` + **raw SQL** (no ORM) | Schema is ~5 tables; ORM is pure overhead here |
| **Event log** | stdlib `json` → append-only `events.jsonl` | Human-greppable; cheap to archive |
| **Logging** | `structlog` over stdlib `logging` | Structured JSON; one format for stdout + file |
| **TUI** | `rich` (live tables via `rich.live`) | Simple; render in-process. `textual` deferred unless we need interactive keybindings |
| **CLI** | `typer` | Clean subcommands (`run`, `stop`, `pause`, `reconcile`, `status`...) |
| **HTTP** | `httpx` | For Slack webhook (v1.1); async-first |
| **Testing** | `pytest` + `pytest-asyncio` + `hypothesis` + `time-machine` | Hypothesis is a natural fit for property-testing the scenario labeler |
| **Lint / format** | `ruff` | Replaces black + flake8 + isort |
| **Type checking** | `pyright` | Faster and sharper inference than mypy |
| **Process mgmt (v1)** | `tmux` | Zero code for detach/attach |
| **Process mgmt (v1.1)** | `systemd` unit on Linux VPS | Restart-on-failure, logs via journald |
| **Containerization (v1.1)** | Docker (single multi-stage image) | Optional for VPS deployment |

### Deliberately Excluded

| Rejected | Why |
|---|---|
| LangGraph / CrewAI / AutoGen | No LLM agents (§1). Rules are deterministic. |
| Any LLM provider (Anthropic/OpenAI) | Not used anywhere in v1 or v1.1. Structured markdown report is sufficient. |
| TA-Lib | Only indicator needed is ATR(14) — trivial in pandas. Avoids C build dependency. |
| SQLAlchemy / any ORM | Schema too small to justify. Raw parameterized SQL is clearer. |
| Postgres / Supabase / Redis | Deferred — see §12.5. No multi-process writers in v1. |
| FastAPI / React / Vue / any web UI | TUI + Slack covers v1 needs. |
| Docker Compose | Single process; not needed. |
| scikit-learn / torch / any ML | Deterministic rules only. |

---

## 7. Data: Alpaca Integration

- **SDK:** `alpaca-py` (current official SDK; `alpaca-trade-api` is legacy).
- **Real-time:** `StockDataStream` WebSocket subscribing to **1-minute bars** per symbol; the bot **aggregates to 15m** locally (Alpaca's free IEX feed does not guarantee native 15m bars, and local aggregation keeps higher-TF derivation consistent).
- **Historical:** `StockHistoricalDataClient.get_stock_bars` for Daily / 4H / 1H backfill on startup and for backtests.
- **Feed tier:** IEX (free) for v1; `sip` (paid) documented as upgrade path.
- **Reconnect:** exponential backoff; on reconnect, REST-fetch any missed 1m bars since last confirmed bar timestamp.
- **Clock source:** Alpaca `TradingClient.get_clock()` — the single source of truth for market open/close and "is_open".

### Higher-Timeframe Derivation
- **1H, 4H, Daily** are rolled up from the 1m stream in local memory using RTH-aligned buckets (`09:30 ET` anchor for 4H).
- At startup, seed these buckets with REST data (last 30 days Daily, 14 days 4H/1H).

---

## 8. Execution: Alpaca Orders

- **Order type:** OCO bracket via Alpaca's native `bracket` order class:
  - Parent: `stop_limit` at entry
  - Child 1: stop loss (stop-market)
  - Child 2: take profit (limit at 3R)
- **Time in force:** `DAY`.
- **Fractional shares:** disabled in v1 (bracket orders require whole shares on Alpaca).
- **Cancel-replace:** if parent stop-limit not filled within 1 bar, cancel. If filled, children become active.
- **Force-flat job:** 15:55 ET cron sends market close for any open position and cancels any open orders.

---

## 9. Risk Management

| Rule | Default | Configurable |
|---|---|---|
| Risk per trade | 0.5% account equity | Yes |
| Daily loss cap | 2% account equity | Yes |
| Max concurrent positions | 3 | Yes |
| Max trades per day | 5 | Yes |
| Min R:R | 3.0 | Yes (floor 2.0) |
| Min bar ATR filter | 0.5 × ATR(14) | Yes |
| News blackout | FOMC / CPI / NFP | Config list |
| Trading mode | `paper` | `paper \| live` flag |

**Circuit breakers:** on daily loss cap hit → flatten all, disable new entries for the rest of the session, write red-flag journal entry, require manual re-enable next day.

---

## 10. Configuration

Single YAML file + `.env` for secrets.

```yaml
# config.yaml
mode: paper              # paper | live
account:
  sim_equity_usd: 50000    # used by backtest + sizing checks; live mode reads real equity from Alpaca
  risk_pct_per_trade: 0.005
  daily_loss_cap_pct: 0.02
  max_concurrent: 3
  max_trades_per_day: 5
strategy:
  timeframe: 15Min
  patterns: [3-2-2, 2-2, 3-1-2, rev-strat]
  sides: [long, short]
  min_rr: 3.0
  min_bar_atr_mult: 0.5
  ftfc_timeframes: [1D, 4H, 1H]
watchlist:
  - SPY
  - QQQ
  - AAPL
  - NVDA
  - TSLA
session:
  entry_window_et: [09:30, 15:45]
  force_flat_et: 15:55
blackouts:
  - 2026-05-07T14:00:00-04:00   # FOMC
```

```dotenv
# .env
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

---

## 11. Logging, Observability, Journaling

- **Structured logs:** JSON lines via `structlog`, one line per decision event (bar_seen, pattern_detected, ftfc_check, entry_submitted, fill, stop_hit, target_hit, force_flat, skip_reason).
- **Trade journal:** SQLite table `trades` with entry/exit timestamps, pattern, FTFC snapshot, R achieved, P&L, symbol, side.
- **Daily report:** end-of-session CLI command renders: trades taken, win rate, avg R, P&L, FTFC hit rate, patterns-by-frequency. Markdown output to `reports/YYYY-MM-DD.md`.

---

## 12. Runtime UI & Process Management

### 11.1 Runtime UI (v1: Rich TUI)

The bot ships with a terminal UI rendered by the `rich`/`textual` library, running **in-process** alongside the trading loop. No web server, no extra infrastructure.

**Layout (single terminal, refreshed every second):**

```
┌─ TheStrat Bot ────────────────────────────────────  09:47:12 ET ─┐
│ Mode: PAPER  |  Session: OPEN  |  Equity: $10,247.50  (+2.47%)  │
│ Trades today: 2/5  |  Daily P&L: +$124.50  |  Loss cap: -$200   │
├─ Watchlist ──────────────────────────────────────────────────────┤
│ SYM    Price    Bar  FTFC   Pattern       Signal    Status       │
│ SPY    512.45   2U   🟢🟢🟢  3-2-2 bull    armed    waiting break│
│ QQQ    438.10   1    🟢🟢⚪  —             —        no FTFC      │
│ AAPL   178.92   3    🔴🔴🔴  3-2-2 bear    watching setup building│
│ NVDA   892.30   2D   🔴⚪🟢  —             —        mixed        │
│ TSLA   245.60   2U   🟢🟢🟢  2-2 bull      FILLED   long @245.42 │
├─ Open Positions ─────────────────────────────────────────────────┤
│ TSLA  long  20sh @245.42  stop 244.85  tgt 247.13  unreal +$3.60 │
├─ Last Events ────────────────────────────────────────────────────┤
│ 09:45  TSLA  ENTRY_FILLED     20sh @ 245.42                      │
│ 09:45  TSLA  BRACKET_ACTIVE   stop=244.85 tgt=247.13             │
│ 09:30  AAPL  SKIP             reason=ftfc_mismatch (4H red)      │
│ 09:15  SPY   PATTERN_DETECTED 3-2-2 bull, awaiting entry trigger │
└──────────────────────────────────────────────────────────────────┘
```

**Sections:**
- **Header:** mode (paper/live), session state, equity, P&L vs daily loss cap, trade count vs cap.
- **Watchlist:** one row per symbol. Current price, current 15m scenario label, FTFC per-TF dots (1D / 4H / 1H), active pattern if any, entry status (`watching` / `armed` / `filled` / `exited`).
- **Open Positions:** live positions with stop/target and unrealized P&L.
- **Last Events:** rolling tail of the last ~10 structured log events. Full event stream still goes to `data/events.jsonl`.

**Why Rich TUI over a web dashboard:**
- Zero new infra, no port to expose, no auth layer.
- Renders over SSH natively. Works inside tmux.
- ~200 lines of code; the data is already in-memory.
- Slack/email notifications can be added later without replacing this.

### 11.2 Process Model

**v1 (local dev):** single foreground process. Bot + TUI run in the same Python process, sharing the in-memory state. Intended to run inside **tmux** so you can detach and re-attach without killing it.

```
┌─────────────────── tmux session ────────────────────┐
│  trade-strats run --config config.yaml              │
│  ├─ asyncio event loop                              │
│  │    ├─ market_data (Alpaca WS)                    │
│  │    ├─ strategy / risk / execution                │
│  │    └─ journal (SQLite + events.jsonl)            │
│  └─ rich TUI (same process, separate coroutine)     │
└─────────────────────────────────────────────────────┘
```

**v1.1 (VPS / always-on):** split into two processes.
- `trade-strats daemon` — headless worker under `systemd`, restart-on-failure. Exposes a Unix socket for control commands.
- `trade-strats attach` — separate TUI that reads SQLite + tails `events.jsonl` + sends commands over the socket. Multiple clients can attach concurrently.

### 11.3 Control Commands

| Command | Behavior |
|---|---|
| `run` (v1) / `daemon` (v1.1) | Start bot. Reconciles with Alpaca on startup before accepting signals. |
| `attach` (v1.1) | Open the TUI against an already-running daemon. |
| `stop` | **Graceful.** Stop accepting new signals. **Leave open bracket orders in place on Alpaca.** Exit process. Brackets continue to protect open positions server-side. |
| `stop --flat` | Emergency. Cancel all open orders, market-close all positions, exit. Requires confirmation prompt unless `--yes`. |
| `pause` | Bot keeps running and updating state; entry logic disabled; existing brackets untouched. |
| `resume` | Re-enable entries. |
| `status` | Print a one-shot summary (no TUI). Scriptable output with `--json`. |
| `reconcile` | Compare SQLite state to Alpaca (open orders + positions). Report drift. Optionally `--repair` to sync SQLite to Alpaca. |
| `flat-all` | Cancel orders + market-close positions without exiting. For mid-session manual intervention. |

### 11.4 Start / Stop / Resume Lifecycle

**Start:**
1. Load config, validate.
2. Connect to Alpaca REST, fetch account, open orders, positions.
3. Reconcile against SQLite `positions` and `orders` tables. Abort with a diff if mismatched (user must run `reconcile --repair` or investigate).
4. Backfill higher-TF bars (last 30d Daily, 14d 4H/1H).
5. Open WebSocket subscription for 1m bars on watchlist.
6. Start TUI coroutine. Begin processing bar events.

**Graceful stop (`Ctrl-C` or `stop` command):**
1. Receive SIGINT / control command.
2. Set "draining" flag — reject new signal evaluations.
3. Let any in-flight order submission complete (or time out after 5s).
4. Flush SQLite + events.jsonl.
5. Close WebSocket, REST client.
6. **Do not** cancel open bracket orders. They remain active on Alpaca.
7. Exit 0.

**Resume after crash / restart:** identical to "Start" — step 3's reconciliation is the safety net. Any fills that happened while the bot was down are discovered via Alpaca, backfilled into SQLite, and the journal stays consistent.

**Force-flat (end-of-day 15:55 ET):** a scheduled internal task, independent of TUI/control commands. Belt-and-suspenders: a `cron` or `launchd` job at 15:56 ET calls `flat-all` as external redundancy in case the in-process scheduler didn't fire.

### 11.5 Data Storage & Git Hygiene

| Path | Purpose | Committed? |
|---|---|---|
| `src/`, `tests/`, `config/*.example.yaml` | Source, tests, config templates | ✅ Yes |
| `data/schema.sql` | SQLite schema definition | ✅ Yes |
| `data/README.md` | Explains layout, backup plan | ✅ Yes |
| `data/trades.db` | Live SQLite DB | ❌ `.gitignore` |
| `data/events.jsonl` | Append-only event log | ❌ `.gitignore` |
| `reports/` | Daily markdown reports | ❌ `.gitignore` |
| `.env` | Secrets (API keys) | ❌ `.gitignore` |
| `.env.example` | Template for secrets | ✅ Yes |

**Why `trades.db` is not committed:**
- Binary SQLite; merge conflicts are unresolvable.
- Mutates every bar — would pollute git history with hundreds of blobs per session.
- Leaks trading activity if the repo is ever shared or pushed publicly.
- Bloats the repo over time (git stores every version).

**Backup strategy:** nightly copy of `data/` to iCloud/Dropbox (or S3 on VPS). Not git.

**Expected data volume (sanity-check for storage):** ~50 MB/year for journal + event log + bar cache across a 10-symbol watchlist. Low hundreds of MB over 5 years. SQLite handles this trivially; migration to Postgres/Supabase is deferred to when either (a) multi-process writers are needed, or (b) a hosted remote dashboard is wanted.

---

## 13. Testing Plan

### Unit
- Scenario labeler: exhaustive fixtures for 1 / 2U / 2D / 3 edge cases (equal highs, dojis, gaps).
- Pattern detector: synthetic bar sequences for each pattern, each side, including near-misses.
- Risk calculator: sizing, 3R math, cap enforcement, blackout rejection.

### Integration
- Alpaca paper account, subscribed to 3 symbols, run for one full session; assert all orders have matching children and no orphan brackets.
- Reconnect test: kill the WS, verify REST backfill restores missed 1m bars and no duplicate signals fire.

### Backtest
- 5 years of historical data for the default watchlist (SPY, QQQ, AAPL, NVDA, TSLA).
- Walk-forward: train = calibrate no parameters (rules are fixed); report by symbol and by pattern.
- Headline metrics: win rate, profit factor, max DD, Sharpe, avg R, trade count.

### Acceptance
- Backtest profit factor ≥ 1.5 and max DD ≤ 15% over 5y.
- 4 weeks of paper forward-testing with no code-level bugs (orphaned orders, duplicate fills, missed flats).
- Then and only then: enable `mode: live` with 0.25% risk per trade for a 30-day ramp.

---

## 14. Success Metrics (Live)

| Metric | Target |
|---|---|
| Win rate | ≥ 45% |
| Avg R per winner | ≥ 3.0 |
| Profit factor | ≥ 1.8 |
| Max drawdown | < 15% |
| Trades per day | 1–5 |
| FTFC hit rate (of scans) | 10–25% |
| Orphaned/duplicate orders | 0 |

---

## 15. Deployment

- **Dev / v1:** local macOS with `uv` + Python 3.11+, run via `uv run python -m trade_strats run`.
- **v1.1:** Docker image, single container, deployed to a small VPS (e.g., Hetzner CX22) pinned to US-East for Alpaca latency.
- **Secrets:** `.env` for local, cloud provider secret manager for VPS.
- **Uptime:** `systemd` unit with restart-on-failure; cron job `15:55 ET force-flat` as belt-and-suspenders redundancy to the in-process scheduler.

---

## 16. Risks & Open Questions

### Risks
- **FTFC too restrictive:** 15m signals filtered through 3 higher TFs may leave few trades. Mitigation: measure FTFC hit rate in backtest; if < 5%, relax to 2-of-3 TFs.
- **Local 15m aggregation drift:** clock skew or missed 1m bars could misclassify a 15m bar. Mitigation: post-bar reconciliation against REST `/v2/stocks/{sym}/bars?timeframe=15Min`.
- **Bracket order semantics on gaps:** overnight gaps can blow through stops. v1 is intraday-flat so this is moot, but document clearly.
- **IEX-only feed coverage:** IEX is ~2–3% of consolidated volume; small-cap symbols may have sparse 1m bars. Mitigation: restrict watchlist to high-volume names in v1.
- **Regime changes:** rules-only systems degrade in new regimes. Mitigation: quarterly review; no auto-parameter-tuning.

### Resolved Decisions
1. **Partial scale-out at 1R:** Deferred to v2.1. v1 uses fixed full-3R exit.
2. **Notifications:** v1 = CLI/TUI + markdown report only. v1.1 adds Slack webhook for fills + daily report. No email.
3. **Blackouts source:** Manual YAML list in config. Auto economic-calendar integration deferred (not scheduled).
4. **Target account size:** **$50,000 USD** for v1 sizing math, slippage validation, and backtest sim. Stored in config; live trading reads actual equity from Alpaca at runtime.

---

## 17. Assessment (My Take)

**Strengths of approaching Strat this way:**
- The rules are genuinely mechanical. Unlike chart-pattern systems that require "does it look like a head-and-shoulders?" judgment, Strat's scenario labeling is arithmetic on OHLC. That's what makes it a good fit for a bot.
- FTFC is a cheap, powerful filter. Most whipsaw losers are trades taken against higher-timeframe bias; enforcing FTFC probably does more for expectancy than any pattern refinement.
- 3R-minimum is the right discipline. Win rate will be modest; expectancy comes from payoff.

**Weaknesses / things to be honest about:**
- The Strat community tends to oversell it. On 15m US equities, expect profit factor in the 1.3–1.7 range after commissions/slippage, not the 3+ some youtube content implies. The PRD targets (PF ≥ 1.8) are ambitious — treat them as *goals*, not *predictions*.
- Strict FTFC cuts trade count hard. Be prepared to see 0-trade days, especially in chop.
- Alpaca IEX data is fine for liquid ETFs but thin for small-caps; the watchlist constraint is real, not cosmetic.
- The 15m timeframe is noisy. A 30m or 1H variant might well outperform on the same rule set; worth a backtest comparison in v1.

**Recommendation:** Ship v1 exactly as scoped above. Do not add LLM agents, ML filters, or dynamic scanners until the deterministic core has 3 months of clean paper results. The biggest risk to this project is over-engineering before the base strategy is proven; the second-biggest is declaring live-ready before paper numbers justify it.

---

## 18. Roadmap

| Version | Scope |
|---|---|
| **v1.0** | This PRD — deterministic core, watchlist, paper, full pattern set |
| **v1.1** | Slack webhook notifications (fills, stops, daily report) |
| **v1.2** | 30m / 1H timeframe variants; comparison harness |
| **v2.0** | Dynamic scanner (pre-market liquidity + gap ranking) |
| **v2.1** | Trailing stop / partial scale-out at 1R |
| **v2.2** | Options overlay for defined-risk expression of same signals |
