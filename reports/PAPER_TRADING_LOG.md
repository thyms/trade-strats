# Paper Trading Log

Daily / weekly ops log for the paper trading campaign. High-frequency
observations, fill quality stats, bug reports. Rollup decisions go to
`RESEARCH_LOG.md`.

**Ordering: newest entries first.**

---

## Campaign: (not yet started)

**Start date:** TBD
**End date:** TBD (50+ trades, ~4-6 weeks)
**Plan:** see `PAPER_TRADING_PLAN.md`
**Configs:** `config/paper-a.yaml` (NVDA/COIN/MSTR 10Min), `config/paper-b.yaml` (TSLA 15Min)

### Setup checklist

- [x] Alpaca paper account created, equity $100K, keys in `.env`
- [ ] Paper configs written
- [ ] Journal schema extended with backtest-predicted fill fields
- [ ] Fill comparison tool (`scripts/compare_fills.py`)
- [ ] Dry-run day completed without errors
- [ ] Continuous-run scheduler (`run-live` command) tested

### Daily log template (copy for each trading day)

```
### 2026-MM-DD (Day N)

**Session:** 09:25 ET start / 15:55 ET force-flat
**Configs running:** paper-a, paper-b
**Trades:** N submitted / M filled / K targets / J stops / I EOD
**Live PF (today):** X.XX   |  Backtest PF (same bars): X.XX
**Avg entry slippage:** $0.XX/share  |  Avg exit slippage: $0.XX/share
**Cumulative PF since start:** X.XX  (N trades)
**Incidents:** none / [describe]
**Notes:** [anything unusual, regime change, news impact]
```

### Weekly rollup template (every Friday after close)

```
## Week N rollup (2026-MM-DD to 2026-MM-DD)

- Trades this week: N
- Win rate: X%
- Live PnL: $X,XXX
- Simulated PnL on same bars: $X,XXX
- Delta: $X,XXX (X%)
- Cumulative live PF: X.XX
- Cumulative backtest PF on same period: X.XX
- Avg slippage per trade: $X.XX
- Max drawdown since start: X.X%
- Incidents: [list]
- Go/no-go status: [on track / watch / kill]
```
