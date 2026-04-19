# Paper Trading Plan

**Created:** 2026-04-19
**Goal:** Validate whether the backtest-derived edge survives real execution.
The single biggest unknown is slippage on stop-limit entries. Live data
answers this question; more backtest tuning does not.

## Primary question

**Does live fill quality match the zero-slippage simulator assumption?**

If live PF stays within ~0.1 of backtest PF after 50+ trades, the simulator
is trustworthy and we can size up. If it drops more than that, we have our
answer on how much slippage is real and can recalibrate.

## Success criteria

After **50+ live trades** (estimated 4-6 weeks):

| Metric | Target | Kill threshold |
|--------|-------:|---------------:|
| Live PF | >= 1.10 | < 1.0 |
| Live/backtest PF delta | <= 0.15 | > 0.30 |
| Avg slippage per share | <= $0.05 | > $0.15 |
| Max drawdown | <= 15% | > 25% |
| Win rate | 35-45% | < 30% |

All green → fund with $10-20K real money. Any red → stop, analyse, decide
whether to recalibrate the simulator or abandon the approach.

## Paper accounts and allocations

Run **two parallel paper accounts** if Alpaca permits, otherwise pick ONE
of the following configs and run it for the full test. Two is preferred
because it lets us compare NVDA's 10Min vs TSLA's 15Min edge directly.

### Config A: NVDA / COIN / MSTR on 10Min

```yaml
mode: paper
strategy:
  timeframe: 10Min
  patterns: [2-2, 3-1-2, rev-strat]
  sides: [long, short]
  min_rr: 4.0
  min_bar_atr_mult: 0.25
  ftfc_timeframes: [1D, 4H, 1H]
  slippage_per_share: 0.0
account:
  sim_equity_usd: 50000         # paper seed
  risk_pct_per_trade: 0.005     # $250/trade
  daily_loss_cap_pct: 0.02      # $1,000/day
  max_concurrent: 3
  max_trades_per_day: 5
watchlist: [NVDA, COIN, MSTR]
session:
  entry_window_et: ["10:00", "15:00"]   # avoid open/close volatility
  force_flat_et: "15:55"
```

Backtest baseline: PF 1.20-1.33, ~2-4 trades/day, mean PnL $100-180/trade.

### Config B: TSLA on 15Min

```yaml
mode: paper
strategy:
  timeframe: 15Min
  patterns: [2-2, 3-1-2]        # drop rev-strat (worse on TSLA)
  sides: [long, short]
  min_rr: 4.0
  min_bar_atr_mult: 0.25
  ftfc_timeframes: [1D, 4H, 1H]
  slippage_per_share: 0.0
account:
  sim_equity_usd: 50000
  risk_pct_per_trade: 0.005
  daily_loss_cap_pct: 0.02
  max_concurrent: 2
  max_trades_per_day: 3
watchlist: [TSLA]
session:
  entry_window_et: ["10:00", "15:00"]
  force_flat_et: "15:55"
```

Backtest baseline: PF 1.47, ~1-2 trades/day, mean PnL $90/trade, DD 5-9%.

## What to measure

Every entry and exit must log both the **backtest-predicted fill** and the
**actual Alpaca fill** so we can compute slippage directly.

### Per-trade log fields

| Field | Source |
|-------|--------|
| `trade_id` | Journal |
| `symbol`, `side`, `pattern` | Setup |
| `bar_ts` | Signal bar timestamp |
| `trigger_price` | Setup.trigger_price (what backtest would fill at) |
| `stop_price`, `target_price` | Plan |
| `qty` | Plan |
| `bt_entry_price` | What backtest fill sim would use (trigger + 1 tick) |
| `live_entry_price` | Actual Alpaca fill |
| `entry_slippage_per_share` | live - bt (positive = worse for trader) |
| `bt_exit_price` | What backtest would exit at (stop, target, or EOD close) |
| `live_exit_price` | Actual Alpaca exit fill |
| `exit_slippage_per_share` | Same sign convention |
| `exit_reason` | stop | target | eod | force_flat |
| `bt_pnl`, `live_pnl` | Computed |

This is what the **fill comparison tool** (roadmap item 12) needs to produce.

### Daily aggregate metrics

- Trades today (live) vs trades the backtest would have taken on today's bars
- Win rate today
- Total PnL today (live) vs simulated
- Cumulative PF, DD, trade count since paper start

## Timeline

| Week | Activity | Checkpoint |
|------|----------|-----------|
| 0 (this week) | Engineering setup (see Next Steps below). | Bot submits real paper orders, fills logged. |
| 1 | Run daily, manual review each evening. | 10+ trades, no bugs. |
| 2-3 | Run daily. Watch for systematic slippage. | 25+ trades. First PF reading. |
| 4-6 | Continue. Rebalance only if kill threshold hit. | 50+ trades. Go/no-go decision. |
| 7+ | If go: fund real account. If no-go: recalibrate or stop. | — |

## Risk controls

- **Paper first, always.** No real money until 50+ paper trades and PF >= 1.10.
- **`--yes` flag on `flat-all`** for emergency close. Test it once on day 1.
- **Daily loss cap $1,000.** Already in config. Verify it triggers correctly.
- **Max concurrent 3 (or 2 for TSLA).** Already in config.
- **Force-flat at 15:55 ET.** No overnight holds. Already in config.
- **Blackout dates** for FOMC / earnings. Add to config manually before each
  event. See `config.example.yaml` for the format.
- **Daily reconcile.** Run `uv run trade-strats reconcile` every morning to
  confirm journal matches Alpaca state. Fail loudly if it drifts.

## Next steps (concrete, in priority order)

### Step 1: Alpaca paper account setup (user action)

- [ ] Create Alpaca paper account at https://app.alpaca.markets/paper/dashboard/overview
- [ ] Generate API key and secret
- [ ] Add to `.env`:
  ```
  ALPACA_API_KEY=...
  ALPACA_API_SECRET=...
  ALPACA_BASE_URL=https://paper-api.alpaca.markets
  ALPACA_DATA_FEED=iex
  ```
- [ ] Verify with `uv run trade-strats status` — should show $100K paper equity.

### Step 2: Create paper configs (engineering, ~30 min)

- [ ] `config/paper-a.yaml` — Config A above (NVDA/COIN/MSTR 10Min)
- [ ] `config/paper-b.yaml` — Config B above (TSLA 15Min)

### Step 3: Extend journal schema for fill comparison (engineering, ~2 hours)

Current `schema.sql` has `trades` and `orders` tables. Need to add:

- [ ] New columns on `trades`: `bt_entry_price`, `bt_exit_price`, `bt_pnl`,
      `entry_slippage`, `exit_slippage` (all `REAL`).
- [ ] Migration script — or, since this is a fresh DB, just update the schema.
- [ ] Update `orchestrator.py` `_persist_bracket` to record backtest-predicted
      entry price (= `plan.entry_price`) at submission time.
- [ ] Update `trade_updates.py` to record live fill prices and compute slippage.

### Step 4: Fill comparison tool (engineering, ~3 hours)

- [ ] New script `scripts/compare_fills.py` that reads the journal and outputs:
  - Per-trade: side-by-side backtest vs live prices, slippage
  - Daily aggregate: mean/median slippage, live PF, backtest PF, delta
  - 7-day and 30-day rolling versions
- [ ] Run it nightly (manually for now) as part of the evening review.

### Step 5: Dry-run the live bot (engineering + validation, ~1 hour)

- [ ] Run `uv run trade-strats run --config config/paper-a.yaml` during market
      hours on a single day.
- [ ] Verify in the TUI that bars are streaming, setups fire, orders submit.
- [ ] Verify on Alpaca dashboard that orders appear.
- [ ] At EOD, check journal has a complete record with both backtest and live
      prices populated.
- [ ] If anything breaks, fix before Week 1 starts.

### Step 6: Run the campaign (daily, 4-6 weeks)

- [ ] Every morning: `uv run trade-strats reconcile` — confirm clean start.
- [ ] Every morning: start the bot(s).
- [ ] Every evening after close: run fill comparison, log observations.
- [ ] Weekly: update `reports/RESEARCH_LOG.md` with a dated entry covering
      trades taken, live vs backtest PF, surprises, any bugs.

### Step 7: Go / no-go decision (at T+50 trades)

- [ ] Compile final paper-trading report (new file:
      `reports/PAPER_TRADING_RESULTS.md`).
- [ ] Compare against the success criteria table above.
- [ ] **Go:** Fund a real account with $10K. Run the same config with real
      money, starting with half-size positions for the first two weeks.
- [ ] **No-go:** Either recalibrate the backtest slippage model to match
      observed live slippage and re-run the tuning sweep, or accept that
      this is not a viable edge in its current form.

## Estimated effort to start

- **User:** 30 min (Alpaca account + env vars)
- **Engineering:** ~6 hours total (steps 2-5)
- **Daily ops:** ~20 min (morning reconcile + evening review)

## What this plan explicitly does not do

- **No real money.** Not until paper passes.
- **No more backtest tuning.** We have enough data; the question is execution.
- **No new tickers.** Stick to the 4 best performers until paper validates.
- **No strategy changes.** Changes would invalidate the paper test.
- **No options.** That's a separate, larger project (see RESEARCH_LOG
  architecture assessment).
