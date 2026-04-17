# Backlog

Running list of research and engineering tasks. Newest/highest priority on top.
Move items to `RESEARCH_LOG.md` once run (with results).

---

## Validation (do before funding real money)

- **Holdout split — out-of-sample test.** Re-run the current best config
  (10Min, R:R 4.0, ATR 0.25, watchlist NVDA+TSLA+COIN+MSTR) with params
  frozen from a 2019-04 → 2023-04 tune, measured only on 2023-04 → 2026-04.
  Decision rule: out-of-sample PF > 1.15 → proceed to paper trading;
  PF < 1.05 → stop, re-tune. Takes one afternoon, addresses the biggest
  open risk (all current numbers are in-sample).
- **Paper-trade 10Min config on NVDA + TSLA** via Alpaca paper account
  for 1-2 months. `run` command already connects to Alpaca WS — point
  it at the 10Min config.
- **Fill comparison tool.** Log each live fill alongside what the backtest
  would have predicted for the same bar. Key metric: live PF vs backtest PF.
- **Go/no-go gate.** If paper PF > 1.10 after 50+ trades, fund with $10K
  real money on NVDA + TSLA only.

## Backtest realism

- **Slippage model.** Add configurable fixed per-share penalty
  (e.g. $0.10/share) to entry and exit fills in `_check_entry_fill`
  and `_check_exit`. 10Min edge is $65 mean PnL/trade — need to know
  if it survives.
- **Commission model.** Add per-trade fee ($0.50-$1.00 round trip).
  At ~5 trades/day, this is ~$1K-$4K/year drag.

## Strategy exploration

- **Entry window narrowing.** Currently 09:30-15:45. Try 10:00-15:00
  (avoid open/close volatility) and 09:30-12:00 (morning only).
- **Per-symbol parameter optimization.** Run independent R:R and ATR
  sweeps per ticker. NVDA and TSLA may want very different settings
  than SPY. Could unlock edge on currently marginal symbols.
- **FTFC timeframe variations.** Currently uses 1D/4H/1H. Try dropping
  4H, or using only 1D.
- **Replace META in volatile-6.** META is the weakest link (PF 1.05).
  Try AMZN or drop to 5 tickers.
- **Scan for more tickers.** High-volatility, liquid names similar to
  NVDA/TSLA. Candidates beyond the 6 already tested.

---

## Done

Items move here (with a date) once completed — see `RESEARCH_LOG.md`
for full results.

- 2026-04-17 — Phase 2 tuning (risk, concurrency, new tickers)
- 2026-04-16 — Parameter tuning sweep (timeframe, R:R, ATR, patterns, sides, watchlist)
- 2026-04-16 — 7-year walk-forward baseline (in-sample)
- 2026-04-15 — Initial backtest battery (1y + 3y)
