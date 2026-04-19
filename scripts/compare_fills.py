#!/usr/bin/env python3
"""Compare backtest-predicted fills against actual Alpaca fills.

Reads the SQLite journal and produces slippage statistics per trade,
per day, and cumulative. Designed to run nightly as part of the evening
review during the paper trading campaign.

Usage::

    uv run python scripts/compare_fills.py                       # full history
    uv run python scripts/compare_fills.py --since 2026-04-20    # from date
    uv run python scripts/compare_fills.py --markdown            # MD output
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_DB = Path("data/trades.db")


@dataclass
class FillComparison:
    """One filled order with its backtest-predicted price and live fill."""

    order_id: str
    trade_id: int
    symbol: str
    side: str         # long | short
    kind: str         # entry | stop | target
    qty: int
    predicted_price: float
    live_price: float
    filled_ts: str    # ISO

    @property
    def slippage_per_share(self) -> float:
        """Positive = worse for the trader, negative = better than predicted.

        Entries: fill higher than trigger hurts a long, helps a short.
        Stops:   fill worse than stop (past stop) hurts both sides.
        Targets: fill worse than target (short of target) hurts both sides.
        """
        delta = self.live_price - self.predicted_price
        # For LONG: buying higher than predicted = bad = positive slippage.
        # For LONG stop (sell at stop_price): filling below stop = worse = positive slippage.
        # For LONG target (sell at target): filling below target = worse = positive slippage.
        # For SHORT: selling lower than predicted = bad = positive slippage.
        # For SHORT stop (buy-to-cover at stop): filling above stop = worse = positive.
        # For SHORT target (buy-to-cover at target): filling above target = worse = positive.

        if self.side == "long":
            if self.kind == "entry":
                return delta                 # paying more hurts
            return -delta                    # selling lower than predicted hurts
        # short
        if self.kind == "entry":
            return -delta                    # selling lower hurts
        return delta                         # buying-to-cover higher hurts

    @property
    def slippage_dollars(self) -> float:
        return self.slippage_per_share * self.qty

    @property
    def filled_date(self) -> date:
        return datetime.fromisoformat(self.filled_ts.replace("Z", "+00:00")).date()


def load_fills(
    db_path: Path = DEFAULT_DB, since: date | None = None
) -> list[FillComparison]:
    """Return all filled orders from the journal with predicted+live prices."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT
            o.alpaca_order_id AS order_id,
            o.trade_id        AS trade_id,
            o.symbol          AS symbol,
            o.side            AS side,
            o.kind            AS kind,
            o.qty             AS qty,
            o.stop_price      AS stop_price,
            o.limit_price     AS limit_price,
            o.filled_avg_price AS live_price,
            o.filled_ts       AS filled_ts
        FROM orders o
        WHERE o.filled_ts IS NOT NULL
          AND o.filled_avg_price IS NOT NULL
          AND o.trade_id IS NOT NULL
    """
    params: tuple = ()
    if since is not None:
        query += " AND o.filled_ts >= ?"
        params = (f"{since.isoformat()}T00:00:00Z",)
    query += " ORDER BY o.filled_ts ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    out: list[FillComparison] = []
    for row in rows:
        predicted = _predicted_price(row["kind"], row["stop_price"], row["limit_price"])
        if predicted is None:
            continue
        out.append(
            FillComparison(
                order_id=row["order_id"],
                trade_id=int(row["trade_id"]),
                symbol=row["symbol"],
                side=row["side"],
                kind=row["kind"],
                qty=int(row["qty"]),
                predicted_price=float(predicted),
                live_price=float(row["live_price"]),
                filled_ts=row["filled_ts"],
            )
        )
    return out


def _predicted_price(kind: str, stop_price: float | None, limit_price: float | None) -> float | None:
    """Which column holds the backtest-predicted fill for each order kind?"""
    if kind == "entry":
        return stop_price                    # stop-limit trigger
    if kind == "stop":
        return stop_price
    if kind == "target":
        return limit_price
    return None


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


@dataclass
class AggregateStats:
    n: int
    total_slippage_dollars: float
    mean_slippage_per_share: float
    median_slippage_per_share: float
    p95_slippage_per_share: float
    max_slippage_per_share: float


def aggregate(fills: list[FillComparison]) -> AggregateStats:
    if not fills:
        return AggregateStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    slips_per_share = [f.slippage_per_share for f in fills]
    slips_per_share_sorted = sorted(slips_per_share)
    p95_idx = min(len(slips_per_share_sorted) - 1, int(len(slips_per_share_sorted) * 0.95))
    return AggregateStats(
        n=len(fills),
        total_slippage_dollars=sum(f.slippage_dollars for f in fills),
        mean_slippage_per_share=statistics.mean(slips_per_share),
        median_slippage_per_share=statistics.median(slips_per_share),
        p95_slippage_per_share=slips_per_share_sorted[p95_idx],
        max_slippage_per_share=max(slips_per_share),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_report(fills: list[FillComparison], markdown: bool = False) -> None:
    """Print slippage report: per-kind, per-symbol, per-day, cumulative."""
    if not fills:
        print("No filled orders found in the journal.")
        return

    header = lambda s: print(f"\n### {s}\n") if markdown else print(f"\n=== {s} ===")
    overall = aggregate(fills)

    header("Overall")
    _print_aggregate(overall, markdown)

    header("By order kind")
    by_kind: dict[str, list[FillComparison]] = defaultdict(list)
    for f in fills:
        by_kind[f.kind].append(f)
    for kind in ("entry", "stop", "target"):
        if kind in by_kind:
            agg = aggregate(by_kind[kind])
            print(f"  [{kind}]")
            _print_aggregate(agg, markdown, indent="    ")

    header("By symbol")
    by_sym: dict[str, list[FillComparison]] = defaultdict(list)
    for f in fills:
        by_sym[f.symbol].append(f)
    for sym in sorted(by_sym):
        agg = aggregate(by_sym[sym])
        print(f"  [{sym}]")
        _print_aggregate(agg, markdown, indent="    ")

    header("By day (last 14)")
    by_day: dict[date, list[FillComparison]] = defaultdict(list)
    for f in fills:
        by_day[f.filled_date].append(f)
    recent_days = sorted(by_day)[-14:]
    print(f"  {'Date':<12} {'N':>5} {'Total $':>12} {'Mean/sh':>10} {'Med/sh':>10} {'Max/sh':>10}")
    for d in recent_days:
        a = aggregate(by_day[d])
        print(
            f"  {d.isoformat():<12} {a.n:>5} ${a.total_slippage_dollars:>10,.2f} "
            f"${a.mean_slippage_per_share:>8.4f} ${a.median_slippage_per_share:>8.4f} "
            f"${a.max_slippage_per_share:>8.4f}"
        )

    header("Cumulative slippage drag")
    # How much PnL has slippage eaten compared to a zero-slippage backtest
    print(f"  Trades (round trips): {len(fills) // 2}  (entries+exits combined: {overall.n})")
    print(f"  Cumulative slippage cost: ${overall.total_slippage_dollars:,.2f}")
    print(f"  Average drag per fill: ${overall.mean_slippage_per_share * _avg_qty(fills):,.2f}")


def _avg_qty(fills: list[FillComparison]) -> float:
    if not fills:
        return 0.0
    return statistics.mean(f.qty for f in fills)


def _print_aggregate(a: AggregateStats, markdown: bool, indent: str = "  ") -> None:
    print(f"{indent}N fills: {a.n}")
    print(f"{indent}Total $ slippage: ${a.total_slippage_dollars:,.2f}")
    print(f"{indent}Mean  slippage/share: ${a.mean_slippage_per_share:>8.4f}")
    print(f"{indent}Median slippage/share: ${a.median_slippage_per_share:>8.4f}")
    print(f"{indent}P95   slippage/share: ${a.p95_slippage_per_share:>8.4f}")
    print(f"{indent}Max   slippage/share: ${a.max_slippage_per_share:>8.4f}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Slippage comparison from the trade journal.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite journal.")
    parser.add_argument("--since", type=str, default=None, help="YYYY-MM-DD lower bound.")
    parser.add_argument("--markdown", action="store_true", help="Markdown header style for logs.")
    args = parser.parse_args()

    since = date.fromisoformat(args.since) if args.since else None
    fills = load_fills(args.db, since)
    print_report(fills, markdown=args.markdown)


if __name__ == "__main__":
    main()
