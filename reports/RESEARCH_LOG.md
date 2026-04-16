# Research Log

Running record of backtest experiments, findings, and next steps.
Updated after each meaningful research run. Raw data lives alongside
in `walk-forward/` and `backtest/` subdirectories (JSON + Markdown).

**Ordering: newest entries first.** Add new sessions above the previous one.

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
