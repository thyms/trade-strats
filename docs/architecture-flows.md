# Architecture & Flow Diagrams

How the system works end-to-end, from bar ingestion through order execution, backtesting, reconciliation, and session lifecycle.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         CLI (typer)                          │
│  run | backtest | walk-forward | reconcile | status | flat  │
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
             │  Journal   │         │  Trade       │
             │ (SQLite +  │         │  Updates     │
             │  JSONL)    │         │  (WS fills)  │
             └────────────┘         └──────────────┘
```

### Module → File Map

| Module | File | Responsibility |
|--------|------|----------------|
| CLI | `cli.py` | Typer subcommands, wiring |
| Orchestrator | `orchestrator.py` | Session lifecycle, decision chain |
| Market Data | `market_data.py` | Alpaca WS/REST, bar routing |
| Aggregation | `aggregation.py` | 1m → 15m/1H/4H/1D bar rolling |
| Labeler | `strategy/labeler.py` | Classify bars: 1/2U/2D/3 + color |
| Patterns | `strategy/patterns.py` | Detect 3-2-2, 2-2, 3-1-2, rev-strat |
| FTFC | `strategy/ftfc.py` | Full Timeframe Continuity gate |
| Risk | `risk.py` | Position sizing, caps, blackouts |
| Execution | `execution.py` | Alpaca bracket order submission |
| Trade Updates | `trade_updates.py` | WS fill/cancel events → journal |
| Journal | `journal.py` | SQLite trades/orders + JSONL events |
| Reconcile | `reconcile.py` | Alpaca ↔ SQLite drift detection |
| Backtest | `backtest.py` | Historical replay + sim fills |
| Reports | `reports.py` | Save backtest/walk-forward results |
| TUI | `tui.py` | Rich terminal dashboard |
| Config | `config.py` | Pydantic YAML + env settings |

---

## 1. Live Trading Flow

The main flow when a 15m bar closes during a live session.

```mermaid
sequenceDiagram
    participant Alpaca WS as Alpaca WebSocket
    participant MD as MarketData
    participant Agg as Aggregator (per symbol, per TF)
    participant Orch as Orchestrator (on_bar_closed)
    participant Lab as Labeler
    participant Pat as Pattern Detector
    participant FTFC as FTFC Gate
    participant Risk as Risk Evaluator
    participant Exec as Executor
    participant Jrnl as Journal

    Alpaca WS->>MD: 1m bar (symbol, OHLCV)
    MD->>MD: is_rth(bar.ts)? Drop if outside 09:30-16:00 ET
    MD->>Agg: ingest(bar) for each TF (15m, 1H, 4H, 1D)
    
    Note over Agg: If new bucket starts,<br/>emit completed bar

    Agg-->>MD: completed_bar (e.g., 15m bar closed)
    MD->>Orch: on_bar_closed(symbol, "15Min", bar)
    
    Note over Orch: Only 15m bars trigger<br/>the decision chain

    Orch->>Lab: classify last bars → Scenario (1/2U/2D/3) + Color
    Orch->>Pat: detect(strat_bars) → list[Setup]
    
    alt No setup detected
        Orch-->>Jrnl: record_event("evaluate", outcome="no_setup")
    end

    Orch->>Orch: _filter_by_config(setups, config)
    Orch->>Orch: pick_best_setup(filtered) by priority

    Orch->>MD: current_open(symbol, "1D"/"4H"/"1H")
    Orch->>FTFC: ftfc_state(price, opens) → FULL_GREEN/FULL_RED/MIXED
    Orch->>FTFC: allows(setup.side, state)?
    
    alt FTFC mismatch
        Orch-->>Jrnl: record_event("evaluate", outcome="ftfc_mismatch")
    end

    Orch->>Orch: compute_atr14(bars_15m)
    Orch->>Exec: get_account() → equity
    Orch->>Jrnl: get_session(date) → trades_today, realized_pnl
    Orch->>Exec: get_positions() → open_positions count

    Orch->>Risk: evaluate(setup, snapshot, config, atr, now, blackouts)
    
    alt Rejected
        Risk-->>Orch: Rejection(reason)
        Orch-->>Jrnl: record_event("skip", reason=...)
    else Approved
        Risk-->>Orch: TradePlan(entry, stop, target, qty)
        Orch->>Exec: submit_bracket(symbol, plan)
        Exec->>Exec: Build StopLimitOrderRequest (BRACKET class)
        Exec-->>Orch: SubmittedBracket(parent_id, stop_id, target_id)
        Orch->>Jrnl: insert_trade(...) + insert_order(×3)
        Orch-->>Jrnl: record_event("entry_submitted")
    end
```

---

## 2. Bar Aggregation Detail

How 1-minute bars are rolled up into higher timeframes.

```mermaid
flowchart TD
    A[1m bar arrives from Alpaca WS] --> B{is_rth? 09:30-16:00 ET}
    B -- No --> Z[Drop bar]
    B -- Yes --> C[Route to 4 Aggregators per symbol]
    
    C --> D1[15m Aggregator]
    C --> D2[1H Aggregator]
    C --> D3[4H Aggregator]
    C --> D4[1D Aggregator]
    
    D1 --> E1{Same 15m bucket?}
    E1 -- Yes --> F1[Absorb: update H/L/C/V]
    E1 -- No --> G1[Emit completed 15m bar]
    G1 --> H1[Start new bucket with this 1m bar]
    
    D2 --> E2{Same 1H bucket?}
    E2 -- Yes --> F2[Absorb]
    E2 -- No --> G2[Emit completed 1H bar]
    
    D3 --> E3{Same 4H bucket?}
    E3 -- Yes --> F3[Absorb]
    E3 -- No --> G3[Emit completed 4H bar]
    
    D4 --> E4{Same 1D bucket?}
    E4 -- Yes --> F4[Absorb]
    E4 -- No --> G4[Emit completed 1D bar]
    
    G1 --> I[on_bar_closed callback]
    G2 --> I
    G3 --> I
    G4 --> I
    
    I --> J{tf == 15Min?}
    J -- Yes --> K[Run full decision chain]
    J -- No --> L[Update higher-TF open prices only]
```

Bucket boundaries are anchored to 09:30 ET:
- **15m**: 09:30, 09:45, 10:00, ...
- **1H**: 09:30, 10:30, 11:30, ...
- **4H**: 09:30, 13:30
- **1D**: 09:30 (one bucket per day)

---

## 3. Strategy Decision Chain

The full evaluation pipeline from bar window to trade/skip.

```mermaid
flowchart TD
    A[15m bar window, last 50 bars] --> B{len >= 15?}
    B -- No --> Z1[NOT_ENOUGH_BARS]
    B -- Yes --> C[Convert to strategy Bars]
    
    C --> D[detect: run all 4 pattern detectors]
    D --> D1[detect_three_two_two]
    D --> D2[detect_three_one_two]
    D --> D3[detect_rev_strat]
    D --> D4[detect_two_two]
    
    D1 --> E[Collect all matches]
    D2 --> E
    D3 --> E
    D4 --> E
    
    E --> F{Any matches?}
    F -- No --> Z2[NO_SETUP]
    F -- Yes --> G[Filter by config: allowed patterns + sides]
    
    G --> H{Any surviving?}
    H -- No --> Z3[PATTERN_FILTERED]
    H -- Yes --> I[pick_best_setup by priority]
    
    Note over I: Priority: 3-1-2 > 3-2-2 > rev-strat > 2-2

    I --> J[Get higher-TF opens from aggregators]
    J --> K{Opens available?}
    K -- No --> Z4[FTFC_MISSING]
    K -- Yes --> L[ftfc_state: price vs 1D/4H/1H opens]
    
    L --> M{allows side + state?}
    M -- No --> Z5[FTFC_MISMATCH]
    M -- Yes --> N[Compute ATR14 + build AccountSnapshot]
    
    N --> O[risk.evaluate]
    O --> O1{Daily loss cap hit?}
    O1 -- Yes --> Z6[DAILY_LOSS_CAP]
    O1 -- No --> O2{Max concurrent?}
    O2 -- Yes --> Z7[MAX_CONCURRENT]
    O2 -- No --> O3{Max trades/day?}
    O3 -- Yes --> Z8[MAX_TRADES_PER_DAY]
    O3 -- No --> O4{In blackout?}
    O4 -- Yes --> Z9[BLACKOUT]
    O4 -- No --> O5{Bar range < ATR threshold?}
    O5 -- Yes --> Z10[BAR_TOO_SMALL]
    O5 -- No --> O6[Compute entry/stop/target + position size]
    O6 --> O7{qty > 0?}
    O7 -- No --> Z11[ZERO_QTY]
    O7 -- Yes --> P[Return TradePlan]
    
    P --> Q[Submit bracket order to Alpaca]
    Q --> R[Persist to Journal]
```

---

## 4. Order Lifecycle

From bracket submission through fill/exit on Alpaca.

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant Exec as Executor
    participant Alpaca as Alpaca API
    participant TUH as TradeUpdateHandler
    participant Jrnl as Journal

    Orch->>Exec: submit_bracket(symbol, plan)
    Exec->>Alpaca: StopLimitOrderRequest (BRACKET class)<br/>parent=stop_limit, children=stop+limit
    Alpaca-->>Exec: Order response (parent_id, leg IDs)
    Exec-->>Orch: SubmittedBracket

    Note over Alpaca: Parent order waits for<br/>price to hit entry trigger

    Alpaca->>TUH: trade_update: "fill" (parent filled)
    TUH->>Jrnl: update_order_status(parent_id, "filled")
    TUH->>Jrnl: record_event("trade_update")

    Note over Alpaca: Children now active on Alpaca<br/>Stop loss + Take profit protecting position

    alt Price hits stop loss
        Alpaca->>TUH: trade_update: "fill" (stop child)
        TUH->>Jrnl: update_order_status(stop_id, "filled")
        TUH->>Jrnl: compute realized_pnl + r_multiple
        TUH->>Jrnl: update_trade_exit(reason="stop")
    else Price hits take profit (3R)
        Alpaca->>TUH: trade_update: "fill" (target child)
        TUH->>Jrnl: update_order_status(target_id, "filled")
        TUH->>Jrnl: compute realized_pnl + r_multiple
        TUH->>Jrnl: update_trade_exit(reason="target")
    else 15:55 ET force flat
        Orch->>Exec: flat_all()
        Exec->>Alpaca: cancel_orders() + close_all_positions()
        Alpaca->>TUH: trade_update: "canceled" / "fill" events
        TUH->>Jrnl: update statuses
    end
```

---

## 5. Session Lifecycle (`run_session`)

The startup, main loop, and shutdown sequence.

```mermaid
sequenceDiagram
    participant CLI as cli.py (run command)
    participant Orch as run_session()
    participant MD as MarketData
    participant Exec as Executor
    participant Jrnl as Journal
    participant Recon as reconcile()
    participant TUI as Rich TUI
    participant TUH as TradeUpdateHandler

    CLI->>Orch: run_session(config, schema_path)
    
    rect rgb(240, 248, 255)
    Note over Orch: Phase 1: Initialization
    Orch->>MD: MarketData(settings)
    Orch->>Exec: Executor(settings)
    Orch->>Jrnl: Journal.open(db, events, schema)
    end

    rect rgb(255, 248, 240)
    Note over Orch: Phase 2: Reconciliation
    Orch->>Recon: reconcile(executor, journal)
    Recon->>Exec: get_account(), get_positions(), get_open_orders()
    Recon->>Jrnl: get_open_trades()
    Recon->>Recon: Compare Alpaca ↔ SQLite
    Recon-->>Orch: ReconciliationReport
    alt Drift detected
        Orch->>Orch: ABORT with drift report
    end
    end

    rect rgb(240, 255, 240)
    Note over Orch: Phase 3: Session Start
    Orch->>Exec: get_account() → equity
    Orch->>Jrnl: upsert_session_start(date, equity)
    Orch->>Jrnl: record_event("session_start")
    Orch->>Orch: Initialize SessionState + per-symbol buffers
    Orch->>MD: set_bar_handler(on_bar_closed)
    end

    rect rgb(248, 240, 255)
    Note over Orch: Phase 4: Concurrent Tasks
    par Bar streaming
        Orch->>MD: run(watchlist) — Alpaca WS 1m bars
    and Trade updates
        Orch->>TUH: run() — Alpaca TradingStream
    and Force flat scheduler
        Orch->>Orch: force_flat_at_close(15:55 ET)
    and Terminal UI
        Orch->>TUI: run_tui(state, stop_event)
    end
    end

    Note over Orch: All 4 tasks run concurrently.<br/>First to complete triggers shutdown.

    rect rgb(255, 240, 240)
    Note over Orch: Phase 5: Shutdown
    Orch->>Orch: state.session_status = "stopping"
    Orch->>Orch: Cancel all tasks
    Orch->>Jrnl: record_event("session_end")
    end
```

---

## 6. Backtest Flow

Historical replay through the same strategy + risk logic, with simulated fills.

```mermaid
sequenceDiagram
    participant CLI as cli.py (backtest command)
    participant MD as MarketData
    participant BT as run_backtest()
    participant Pat as Pattern Detector
    participant FTFC as FTFC Gate
    participant Risk as Risk Evaluator

    CLI->>MD: backfill(symbol, "15Min", start, end)
    CLI->>MD: backfill(symbol, "1D"/"1H", context_start, end)
    CLI->>CLI: aggregate(1H bars → 4H via bucket_4h)
    CLI->>BT: build_opens_provider(daily, 4H, 1H)
    CLI->>BT: run_backtest(symbol, bars_15m, provider, config, equity)

    loop For each 15m bar
        BT->>BT: New day? → EOD-close open positions
        
        BT->>BT: Check open positions: stop/target hit?
        Note over BT: Stop checked before target<br/>(conservative assumption)
        
        BT->>BT: Check pending brackets: filled on this bar?
        Note over BT: 1-bar TIF: expire if not filled<br/>Gap fill: use max(open, trigger)
        
        BT->>BT: Same-bar exit check after fill
        
        BT->>Pat: detect(bars[:i+1]) → setups
        BT->>BT: Filter by config patterns + sides
        BT->>BT: pick_best_setup()
        
        BT->>FTFC: opens_provider(bar.ts) → HigherTfOpens
        BT->>FTFC: ftfc_state + allows?
        
        BT->>Risk: evaluate(setup, snapshot, config, atr)
        
        alt Approved
            BT->>BT: Append PendingBracket (1-bar TIF)
        end
    end

    BT->>BT: Final EOD: close remaining positions
    BT->>BT: _compute_metrics(trades, starting_equity)
    BT-->>CLI: BacktestResult (trades, win_rate, PF, DD, ...)
    CLI->>CLI: save_backtest() → JSON + Markdown report
```

### Backtest Fill Simulation Rules

| Rule | Behavior |
|------|----------|
| Entry fill | Parent stop-limit checked against next bar only (1-bar TIF) |
| Gap handling | Long gap-up: fill at `max(bar.open, trigger)` |
| Exit priority | Stop loss checked before target on same bar |
| Same-bar exit | After fill, immediately check if stop/target hit in same bar |
| EOD close | Open positions closed at last bar's close, reason="eod" |
| Equity tracking | Running equity updated after each closed trade |

---

## 7. Walk-Forward Flow

Multi-symbol backtest with per-pattern breakdowns.

```mermaid
flowchart TD
    A[CLI: walk-forward --start --end] --> B[Load config + watchlist]
    
    B --> C[For each symbol in watchlist]
    C --> D[Fetch 15m + 1D + 1H bars from Alpaca REST]
    D --> E[Aggregate 1H → 4H locally]
    E --> F[build_opens_provider per symbol]
    
    F --> G[run_backtest per symbol<br/>Independent equity pools]
    
    G --> H[Collect BacktestResult per symbol]
    H --> I[pattern_breakdowns: group trades by symbol × pattern]
    
    I --> J[WalkForwardReport]
    J --> K[Per-symbol: trades, win%, PnL, PF]
    J --> L[Per-pattern: N, wins, win%, PnL, avgR, PF]
    
    K --> M[save_walk_forward → JSON + Markdown]
    L --> M
```

---

## 8. Reconciliation Flow

Startup safety check comparing Alpaca live state against the SQLite journal.

```mermaid
flowchart TD
    A[reconcile called] --> B[Fetch from Alpaca]
    B --> B1[get_account → equity]
    B --> B2[get_positions → live positions]
    B --> B3[get_open_orders → live orders]
    
    A --> C[Fetch from Journal]
    C --> C1[get_open_trades → SQLite open trades]
    
    B2 --> D{Position in Alpaca<br/>but NOT in SQLite?}
    D -- Yes --> E[UNKNOWN_POSITION drift]
    
    C1 --> F{Trade open in SQLite<br/>but NO Alpaca position?}
    F -- Yes --> G[MISSED_EXIT drift]
    
    B3 --> H{Order on Alpaca<br/>not tracked in journal?}
    H -- Yes --> I[ORPHAN_ORDER drift]
    
    E --> J[ReconciliationReport]
    G --> J
    I --> J
    
    J --> K{report.clean?}
    K -- Yes --> L[Session proceeds normally]
    K -- No --> M[ABORT: print drift report<br/>User must run reconcile --repair]
```

### Drift Types

| Kind | Meaning | Typical Cause |
|------|---------|---------------|
| `UNKNOWN_POSITION` | Alpaca has a position not in SQLite | Manual trade outside bot, or bot crashed before journaling |
| `MISSED_EXIT` | SQLite shows open trade, Alpaca has no position | Fill happened while bot was offline |
| `ORPHAN_ORDER` | Alpaca has an open order not tracked in journal | Stale bracket from a previous session |

---

## 9. Pattern Detection Detail

How the labeler and pattern detectors classify the bar window.

```mermaid
flowchart LR
    subgraph Labeler
        direction TB
        B1[Bar t-1] --> CL[classify prev, curr]
        B2[Bar t] --> CL
        CL --> S{Scenario}
        S --> S1[1: Inside<br/>H≤prevH AND L≥prevL]
        S --> S2[2U: Up<br/>breaks high only]
        S --> S3[2D: Down<br/>breaks low only]
        S --> S4[3: Outside<br/>breaks both]
    end

    subgraph Patterns
        direction TB
        P1["3-2-2 Bullish<br/>red 3 → red 2D → green 2U"]
        P2["3-2-2 Bearish<br/>green 3 → green 2U → red 2D"]
        P3["3-1-2 Bullish<br/>3 → 1 → green 2U"]
        P4["3-1-2 Bearish<br/>3 → 1 → red 2D"]
        P5["Rev Strat Bull<br/>1 → red 2D → green 2U"]
        P6["Rev Strat Bear<br/>1 → green 2U → red 2D"]
        P7["2-2 Bullish<br/>red 2D → green 2U"]
        P8["2-2 Bearish<br/>green 2U → red 2D"]
    end

    subgraph FTFC Gate
        direction TB
        F1[Price > 1D open AND<br/>Price > 4H open AND<br/>Price > 1H open] --> FG[FULL_GREEN → allow LONG]
        F2[Price < 1D open AND<br/>Price < 4H open AND<br/>Price < 1H open] --> FR[FULL_RED → allow SHORT]
        F3[Anything else] --> FM[MIXED → block all]
    end

    Labeler --> Patterns
    Patterns --> FTFC Gate
```

---

## 10. Risk Evaluation Pipeline

Sequential gate checks applied to every approved setup.

```mermaid
flowchart TD
    A[Setup + AccountSnapshot + Config] --> G1
    
    G1{realized_pnl_today ≤<br/>-equity × daily_loss_cap_pct?}
    G1 -- Yes --> R1[REJECT: DAILY_LOSS_CAP]
    G1 -- No --> G2
    
    G2{open_positions ≥<br/>max_concurrent?}
    G2 -- Yes --> R2[REJECT: MAX_CONCURRENT]
    G2 -- No --> G3
    
    G3{trades_today ≥<br/>max_trades_per_day?}
    G3 -- Yes --> R3[REJECT: MAX_TRADES_PER_DAY]
    G3 -- No --> G4
    
    G4{Within blackout<br/>window of scheduled event?}
    G4 -- Yes --> R4[REJECT: BLACKOUT]
    G4 -- No --> G5
    
    G5{signal bar range <<br/>min_bar_atr_mult × ATR14?}
    G5 -- Yes --> R5[REJECT: BAR_TOO_SMALL]
    G5 -- No --> CALC
    
    CALC[Compute prices + sizing]
    CALC --> C1["entry = trigger ± 1 tick"]
    CALC --> C2["stop = stop_price ∓ 1 tick"]
    CALC --> C3["risk_per_share = |entry - stop|"]
    CALC --> C4["target = entry ± min_rr × risk_per_share"]
    CALC --> C5["qty = floor(equity × risk_pct / risk_per_share)"]
    
    C5 --> G6{qty > 0?}
    G6 -- No --> R6[REJECT: ZERO_QTY]
    G6 -- Yes --> PLAN[TradePlan approved]
```

Default risk parameters: 0.5% risk/trade, 2% daily loss cap, 3 max concurrent, 5 max trades/day, 3R minimum target.
