# Parameter Tuning Plan

**Goal:** Find the configuration that maximizes risk-adjusted returns
(profit factor, total PnL) over the 7-year dataset (2019-04-16 to 2026-04-16).

**Baseline:** PF ~1.08-1.40 per symbol, $172K total PnL on $250K capital,
all 4 patterns, both sides, 15Min TF, 3.0 R:R, 0.5 ATR filter.

**Data:** 1Min bars cached locally (350 parquet files, 65 MB). All
aggregation is local — runs take seconds, not API calls.

---

## Tunable parameters

| Parameter | Config key | Baseline | Values to test |
|-----------|-----------|----------|----------------|
| Timeframe | `strategy.timeframe` | 15Min | 5Min, 10Min, 15Min, 20Min, 30Min |
| Patterns | `strategy.patterns` | all 4 | drop 3-2-2; drop 3-2-2+rev-strat; 2-2 only; 2-2+3-1-2 |
| Sides | `strategy.sides` | both | long-only, short-only |
| Min R:R | `strategy.min_rr` | 3.0 | 1.5, 2.0, 2.5, 3.0, 4.0 |
| ATR filter | `strategy.min_bar_atr_mult` | 0.5 | 0.0, 0.25, 0.5, 0.75, 1.0 |
| Watchlist | `watchlist` | 5 tickers | drop AAPL; NVDA+TSLA only |

## Phased approach

We test one dimension at a time against the baseline, find the best value,
lock it in, then test the next dimension. This avoids combinatorial explosion
(5×4×2×5×5 = 1,000 runs) while still finding good configurations.

### Phase 1: Pattern selection (4 runs)

Hold everything else at baseline. Test which pattern combos improve PF.

| Run | Label | Patterns |
|-----|-------|----------|
| 1A | `no-322` | 2-2, 3-1-2, rev-strat |
| 1B | `no-322-no-rev` | 2-2, 3-1-2 |
| 1C | `22-only` | 2-2 |
| 1D | `22-312` | 2-2, 3-1-2 |

**Lock winner for subsequent phases.**

### Phase 2: R:R ratio (5 runs)

Use winning patterns from Phase 1. Sweep min_rr.

| Run | Label | min_rr |
|-----|-------|--------|
| 2A | `rr-1.5` | 1.5 |
| 2B | `rr-2.0` | 2.0 |
| 2C | `rr-2.5` | 2.5 |
| 2D | `rr-3.0` | 3.0 (baseline) |
| 2E | `rr-4.0` | 4.0 |

**Lock winner for subsequent phases.**

### Phase 3: ATR filter (5 runs)

Use winning patterns + R:R. Sweep min_bar_atr_mult.

| Run | Label | min_bar_atr_mult |
|-----|-------|-----------------|
| 3A | `atr-0.0` | 0.0 (disabled) |
| 3B | `atr-0.25` | 0.25 |
| 3C | `atr-0.5` | 0.5 (baseline) |
| 3D | `atr-0.75` | 0.75 |
| 3E | `atr-1.0` | 1.0 |

**Lock winner for subsequent phases.**

### Phase 4: Timeframe (5 runs)

Use winning patterns + R:R + ATR. Sweep timeframe.

| Run | Label | timeframe |
|-----|-------|-----------|
| 4A | `tf-5m` | 5Min |
| 4B | `tf-10m` | 10Min |
| 4C | `tf-15m` | 15Min (baseline) |
| 4D | `tf-20m` | 20Min |
| 4E | `tf-30m` | 30Min |

**Lock winner for subsequent phases.**

### Phase 5: Side filter (2 runs)

Test whether restricting to one side helps.

| Run | Label | sides |
|-----|-------|-------|
| 5A | `long-only` | long |
| 5B | `short-only` | short |

Compare to both-sides winner from Phase 4.

### Phase 6: Watchlist pruning (2 runs)

Using the best config from Phases 1-5:

| Run | Label | watchlist |
|-----|-------|-----------|
| 6A | `no-aapl` | SPY, QQQ, NVDA, TSLA |
| 6B | `nvda-tsla` | NVDA, TSLA |

### Phase 7: Final confirmation

Run the single best configuration over 7 years. Compare to baseline.
Report final PF, PnL, win rate, max drawdown per symbol.

---

## Total runs: ~23

All runs use the same 7-year window (2019-04-16 to 2026-04-16)
and the same cached 1m data. Each run takes seconds since no API
calls are needed.

## Success criteria

- Portfolio PF > 1.20 (vs baseline ~1.08)
- Annualized return > 10% (vs baseline ~7.7%)
- No single symbol with PF < 0.90
- Improvement holds across multiple timeframes (not overfit to one)
