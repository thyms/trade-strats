# Research Log

Running record of backtest experiments, findings, and next steps.
Updated after each meaningful research run. Raw data lives alongside
in `walk-forward/` and `backtest/` subdirectories (JSON + Markdown).

**Ordering: newest entries first.** Add new sessions above the previous one.

---

## 2026-04-16 — Parameter tuning sweep (6 phases, 22 runs)

Full tuning results over 7 years (2019-04-16 to 2026-04-16), 5 symbols,
$50K per symbol. Each phase locks the winner and carries it forward.

### Phase 1: Pattern selection (baseline: all 4 patterns)

| Config | Trades | PnL | Winner? |
|--------|-------:|----:|---------|
| no-322 (drop 3-2-2) | 5,353 | $172,081 | Tied |
| no-322-no-rev | 4,661 | $140,238 | |
| 22-only | 3,968 | $94,066 | |
| 22-312 | 4,661 | $140,238 | |

**Winner: `no-322`** — dropping 3-2-2 costs nothing (was already suppressed
by rev-strat overlap fix). Keeping rev-strat adds $32K over dropping it.

### Phase 2: R:R ratio (locked: no-322 patterns)

| min_rr | Trades | PnL | Winner? |
|-------:|-------:|----:|---------|
| 1.5 | 5,354 | $87,753 | |
| 2.0 | 5,353 | $119,169 | |
| 2.5 | 5,353 | $146,210 | |
| 3.0 | 5,353 | $172,081 | |
| **4.0** | **5,351** | **$196,035** | **Yes** |

**Winner: R:R 4.0** — higher targets pay off despite lower fill rate.
More R per trade compensates for fewer winners.

### Phase 3: ATR filter (locked: no-322, R:R 4.0)

| min_bar_atr_mult | Trades | PnL | Winner? |
|-----------------:|-------:|----:|---------|
| 0.0 | 5,756 | $227,933 | |
| **0.25** | **5,748** | **$228,650** | **Yes** |
| 0.5 | 5,351 | $196,035 | |
| 0.75 | 4,113 | $121,085 | |
| 1.0 | 2,789 | $65,380 | |

**Winner: ATR 0.25** — light filter removes the worst setups without
cutting too much volume. +$32K vs baseline ATR of 0.5.

### Phase 4: Timeframe (locked: no-322, R:R 4.0, ATR 0.25)

| Timeframe | Trades | PnL | Winner? |
|-----------|-------:|----:|---------|
| **5Min** | **14,863** | **$1,645,185** | **Yes** |
| 10Min | 8,073 | $521,392 | |
| 15Min | 5,748 | $228,650 | |
| 20Min | 4,728 | $118,923 | |
| 30Min | 3,317 | $74,579 | |

**Winner: 5Min** — dramatically higher PnL due to 3x more trade opportunities.

**CAUTION:** The 5Min result ($1.65M on $250K = 660%) is likely inflated by
fill assumptions. At 5Min granularity, the 1-bar TIF and stop-before-target
fill model is less realistic (bars are shorter, slippage matters more).
**10Min ($521K, ~30% annualized)** is the more conservative pick.

Per-symbol at 5Min:

| Symbol | PnL | PF | Max DD% |
|--------|----:|---:|--------:|
| SPY | $829,088 | 1.24 | 20.4% |
| QQQ | $161,494 | 1.11 | 17.1% |
| AAPL | $4,044 | 1.00 | 59.8% |
| NVDA | $122,914 | 1.11 | 29.0% |
| TSLA | $527,646 | 1.19 | 24.5% |

Per-symbol at 10Min (more realistic):

| Symbol | PnL | PF | Max DD% |
|--------|----:|---:|--------:|
| SPY | $17,322 | 1.06 | 24.0% |
| QQQ | $83,130 | 1.18 | 16.8% |
| AAPL | $17,591 | 1.04 | 40.2% |
| NVDA | $231,258 | **1.33** | 13.7% |
| TSLA | $172,091 | **1.32** | 10.6% |

### Phase 5: Side filter (locked: no-322, R:R 4.0, ATR 0.25, 5Min)

| Sides | Trades | PnL | Winner? |
|-------|-------:|----:|---------|
| **both** | **14,863** | **$1,645,185** | **Yes** |
| long-only | 7,973 | $458,908 | |
| short-only | 7,000 | $374,139 | |

**Winner: both sides.** Both long and short contribute positively.

### Phase 6: Watchlist pruning (locked: all above, 5Min)

| Watchlist | Trades | PnL | Winner? |
|-----------|-------:|----:|---------|
| **no-aapl (4 tickers)** | **11,978** | **$1,641,141** | **Yes** |
| nvda-tsla (2 tickers) | 5,810 | $650,560 | |

**Winner: drop AAPL** — removes 2,885 trades that contribute $4K while
requiring $50K capital. AAPL's max DD of 59.8% confirms it's a liability.

### Final best configuration

```yaml
strategy:
  timeframe: 5Min    # (or 10Min for conservative)
  patterns: [2-2, 3-1-2, rev-strat]  # drop 3-2-2
  sides: [long, short]
  min_rr: 4.0
  min_bar_atr_mult: 0.25
watchlist: [SPY, QQQ, NVDA, TSLA]   # drop AAPL
```

**5Min result:** $1.64M PnL on $200K capital (820%), ~38% annualized.
**10Min result:** $521K PnL on $250K capital (209%), ~17% annualized.
**Baseline (15Min):** $172K PnL on $250K capital (69%), ~7.7% annualized.

### Validation (same session)

Re-ran both configs and cross-checked from multiple angles:

**Reproduction:** Both reproduce exactly (deterministic backtest).

**Sub-period consistency (first half vs second half):**

| Config | 2019-2022 PF | 2022-2026 PF | Verdict |
|--------|-------------|-------------|---------|
| 5Min | 1.20 | 1.10 | Both positive, slight fade |
| 10Min | 1.27 | 1.15 | Both positive, slight fade |

**Exit reason analysis — RED FLAG for 5Min:**

| Metric | 5Min | 10Min | 15Min |
|--------|------|-------|-------|
| EOD exits | 16.6% → **$2.49M** | 32.1% → $1.33M | 39.0% → $474K |
| Stop hits | 67.3% → -$9.6M | 57.9% → -$2.3M | 49.8% → -$903K |
| Target hits | 16.1% → $8.8M | 10.0% → $1.5M | 11.2% → $601K |

5Min's $1.65M PnL is almost entirely EOD drift — trades that enter, go
the right way, never hit target, and close at EOD for a partial win.
The 67% stop rate means the strategy is **wrong 2 out of 3 times**.
The edge is real but thin ($111 mean PnL/trade) and vulnerable to slippage.

**10Min year-by-year per symbol:**
- NVDA: positive every year ($13K-$55K). Rock solid.
- TSLA: positive every year ($3.7K-$45K). Also solid.
- AAPL: degrades after 2022 — positive early, then 3 losing years.
- SPY: mixed, 3 losing years. Weakest symbol.

**Conclusion:** 10Min is the trustworthy finding. 5Min is likely inflated
by EOD drift mechanics and thin per-trade edge. Recommend 10Min for live
testing, with NVDA and TSLA as primary symbols.

### Untried variations for next session

1. **Risk per trade** — currently 0.5% ($250/trade on $50K). Try 0.25%
   and 1.0% to see how sizing affects PnL and drawdown.
2. **Max concurrent positions** — currently 3. Try 1 (focus), 5 (more
   opportunities), and unlimited.
3. **Max trades per day** — currently 5. Try 3 (selective) and 10.
4. **Entry window narrowing** — currently 09:30-15:45. Try 10:00-15:00
   (avoid open/close volatility) and 09:30-12:00 (morning only).
5. **Per-symbol optimization** — run R:R and ATR sweeps per ticker
   independently. NVDA may want different params than SPY.
6. **Combination of 10Min + no-AAPL** — haven't run this specific combo.
7. **R:R 5.0 and 6.0** — the sweep stopped at 4.0, which won. Higher
   might be even better on volatile names like NVDA/TSLA.
8. **FTFC timeframe variations** — currently uses 1D/4H/1H. Try dropping
   4H, or using only 1D.

---

## 2026-04-16 — 7-year walk-forward baseline

### Full history: 7-year walk-forward (2019-04-16 to 2026-04-16)

**Config:** all 4 patterns, both sides, 3R min, 0.5 ATR filter, $50k per symbol.
**File:** `walk-forward/2019-04-16_to_2026-04-16_run-2026-04-16.json`

| Symbol | Trades | Win % | PnL | PF |
|--------|-------:|------:|----:|---:|
| SPY | 1,092 | 36.9% | +$9,709 | 1.05 |
| QQQ | 1,104 | 39.7% | +$20,536 | 1.11 |
| AAPL | 1,046 | 37.9% | +$1,882 | 1.01 |
| NVDA | 1,065 | 44.3% | +$53,538 | **1.25** |
| TSLA | 1,046 | 45.0% | +$86,416 | **1.40** |
| **Total** | **5,353** | | **+$172,081** | |

Total capital: $250k (5 × $50k independent pools).
**Return: ~68.8% over 7 years (~7.7% annualized).**
Comparable to buy-and-hold S&P (~10-12% annualized over same period),
but the strategy is intraday-only (flat overnight).

### Pattern-level observations (7y)

| Symbol | 2-2 PF | 3-1-2 PF | 3-2-2 PF | rev-strat PF |
|--------|-------:|---------:|---------:|-------------:|
| SPY | 1.13 | **0.78** | **0.57** | 1.20 |
| QQQ | 1.08 | 1.32 | 1.12 | 1.06 |
| AAPL | 1.00 | 1.12 | **0.70** | 1.12 |
| NVDA | 1.14 | **1.82** | 0.90 | **1.52** |
| TSLA | **1.50** | **1.56** | 0.96 | 1.04 |

- **3-2-2 is a consistent loser** — negative on SPY (PF 0.57), AAPL (0.70),
  NVDA (0.90), TSLA (0.96). Only QQQ is barely positive (1.12). Too few
  trades (46-70 per symbol) to be statistically meaningful, but the
  direction is consistent. Strong candidate for removal.
- **3-1-2 is polarized** — excellent on NVDA (1.82) and TSLA (1.56), but
  terrible on SPY (0.78). Symbol-dependent edge.
- **2-2 is the workhorse** — 63-72% of all trades per symbol. Consistently
  positive except AAPL (exactly 1.00). TSLA 2-2 at PF 1.50 is remarkable.
- **rev-strat works best on NVDA** (PF 1.52), mediocre elsewhere.

### Infrastructure improvements

- **Parquet bar cache:** 1Min bars now cached as monthly parquet files in
  `data/bars/<SYM>/1Min/<YYYY-MM>.parquet`. 350 files, 65 MB total for
  5 tickers × 7 years. Subsequent runs skip the API entirely.
- **Parameterized timeframe:** `config.strategy.timeframe` now drives all
  behavior. Non-native Alpaca intervals (10Min, 20Min) supported via
  local aggregation from cached 1m bars.

### Key findings

- Strategy is profitable but underperforms buy-and-hold S&P at default settings.
- TSLA and NVDA carry the portfolio ($140K of $172K).
- AAPL is barely break-even over 7 years (PF 1.01). Confirms 3y finding.
- 3-2-2 pattern should be dropped (consistent loser across tickers).
- Parameter tuning (timeframe, R:R, ATR filter, pattern selection) is the
  next step to push the edge higher. See tuning plan below.

---

## 2026-04-15 — Initial backtest battery

### Baseline: 12-month walk-forward (2025-04-15 to 2026-04-15)

**Config:** all 4 patterns, both sides, 3R min, 0.5 ATR filter, $50k per symbol.
**File:** `walk-forward/2025-04-15_to_2026-04-15_run-2026-04-15.json`

| Symbol | Trades | Win % | PnL | PF |
|--------|-------:|------:|----:|---:|
| SPY | 156 | 31.4% | -$5,334 | 0.76 |
| QQQ | 147 | 38.8% | +$4,106 | 1.21 |
| AAPL | 123 | 32.5% | -$3,864 | 0.78 |
| NVDA | 153 | 45.8% | +$10,660 | **1.51** |
| TSLA | 126 | 37.3% | +$2,232 | 1.13 |
| **Total** | **705** | **37.2%** | **+$7,800** | **~1.08** |

### Experiment 1: disable rev-strat via config

**File:** `walk-forward/2025-04-15_to_2026-04-15_run-2026-04-15_no-revstrat.json`

**Result:** totals identical (705 trades, +$7,800). Config filtering is a
relabel, not a suppression — rev-strat bars (1-2-2) always also fire the
2-2 detector on the same last two bars, so `pick_best_setup` falls through
to 2-2.

**Takeaway:** to truly suppress rev-strat signals, we need a code-level
filter that rejects a 2-2 when the preceding bar was a scenario-1 inside
bar (easy follow-up, not yet implemented).

### Experiment 2: NVDA only

**File:** `walk-forward/2025-04-15_to_2026-04-15_run-2026-04-15_nvda-only.json`

153 trades, 45.8% WR, PF 1.51, +$10,660. Every pattern is net positive
on NVDA; rev-strat has the best avg R (0.43). Confirms NVDA is where the
edge lives in the current window.

### Experiment 3: 3-year walk-forward (2023-04-15 to 2026-04-15)

**File:** `walk-forward/2023-04-15_to_2026-04-15_run-2026-04-15_3y.json`

| Symbol | 1y PF | 3y PF | 3y PnL |
|--------|------:|------:|-------:|
| SPY | 0.76 | 0.96 | -$2,833 |
| QQQ | 1.21 | 1.08 | +$5,707 |
| AAPL | 0.78 | **0.79** | **-$11,906** |
| NVDA | 1.51 | **1.36** | +$23,979 |
| TSLA | 1.13 | **1.26** | +$15,572 |
| **Total** | ~1.08 | **~1.09** | **+$30,520** |

**Key findings:**

- **AAPL is a structural loser.** Consistently PF < 0.8 across both 1y and
  3y. Not noise. Should be dropped from the default watchlist.
- **NVDA's 1y PF (1.51) is above its 3y mean (1.36).** Expect regression.
  Still the strongest symbol by a wide margin.
- **TSLA's 1y PF (1.13) is below its 3y mean (1.26).** Last year was a
  worse-than-average stretch; longer view is more favorable.
- **SPY is near break-even over 3y (PF 0.96).** Not edge-negative but not
  worth the trade count. Consider dropping or moving to a longer timeframe.
- **Portfolio PF ~1.09 over 3 years** — positive but well below the PRD
  target of 1.8 and even below the "honest assessment" range of 1.3-1.7.

### Pattern-level observations (3y window)

- **2-2** is the workhorse: 1,372 of 2,180 trades (63%). Net positive on
  NVDA, QQQ, TSLA; net negative on AAPL and near-zero on SPY.
- **3-1-2** is mixed: positive on NVDA and TSLA; negative on AAPL, QQQ, SPY.
- **3-2-2** is too rare for signal (104 trades over 3 years, 5 symbols).
- **rev-strat** is polarized: strong on NVDA (PF 1.69), consistently
  negative on QQQ (0.57), TSLA (0.70), SPY (0.78), AAPL (0.92).

---

## Open questions for next session

1. **Drop AAPL from watchlist.** Expected improvement: +$12k over 3y,
   portfolio PF to ~1.18. Easy config change, run to confirm.
2. **Real rev-strat filter.** Code change to reject 2-2 trades where
   bar[-4] is scenario-1 inside bar. Would separate "true 2-2" from
   "rev-strat-that-falls-through-to-2-2". Validates whether the pattern
   label actually predicts anything.
3. **Min R:R sensitivity.** Avg win R is often well below 3.0 (many
   trades exit at EOD not at target). Try min_rr=2.0 to see if allowing
   more modest targets improves win rate and net PF.
4. **Timeframe comparison.** Run the same walk-forward on 30m or 1H bars
   to test PRD §17's hypothesis that "a 30m or 1H variant might
   outperform on the same rule set."
5. **NVDA-only live paper test.** If one symbol must go live first,
   NVDA has the best edge across both windows. Simplest path to
   collecting real-world fill data.
