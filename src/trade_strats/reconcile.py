from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from trade_strats.execution import Executor, OrderInfo, PositionInfo
from trade_strats.journal import Journal


class DriftKind(StrEnum):
    UNKNOWN_POSITION = "unknown_position"
    MISSED_EXIT = "missed_exit"
    ORPHAN_ORDER = "orphan_order"


@dataclass(frozen=True, slots=True)
class DriftIssue:
    kind: DriftKind
    symbol: str
    detail: str
    trade_id: int | None = None
    order_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    checked_at: datetime
    alpaca_equity: float
    alpaca_positions: list[PositionInfo]
    alpaca_open_orders: list[OrderInfo]
    sqlite_open_trades: list[dict[str, Any]]
    drift: list[DriftIssue]

    @property
    def clean(self) -> bool:
        return not self.drift


async def reconcile(executor: Executor, journal: Journal) -> ReconciliationReport:
    """Compare Alpaca live state against SQLite journal. Reports drift, does not repair."""
    account = await executor.get_account()
    positions = await executor.get_positions()
    orders = await executor.get_open_orders()
    open_trades = await journal.get_open_trades()

    alpaca_symbols = {p.symbol for p in positions}
    sqlite_symbols = {t["symbol"] for t in open_trades}

    drift: list[DriftIssue] = []

    for position in positions:
        if position.symbol not in sqlite_symbols:
            drift.append(
                DriftIssue(
                    kind=DriftKind.UNKNOWN_POSITION,
                    symbol=position.symbol,
                    detail=(
                        f"{position.side} {position.qty}sh @ avg "
                        f"{position.avg_entry_price:.2f} — no matching open trade in journal"
                    ),
                )
            )

    for trade in open_trades:
        if trade["symbol"] not in alpaca_symbols:
            drift.append(
                DriftIssue(
                    kind=DriftKind.MISSED_EXIT,
                    symbol=str(trade["symbol"]),
                    detail=(
                        f"trade #{trade['id']} ({trade['side']} {trade['qty']}sh "
                        f"{trade['pattern']}) open in journal but Alpaca shows no position"
                    ),
                    trade_id=int(trade["id"]),
                )
            )

    for order in orders:
        tracked = await journal.get_order(order.order_id)
        if tracked is None:
            drift.append(
                DriftIssue(
                    kind=DriftKind.ORPHAN_ORDER,
                    symbol=order.symbol,
                    detail=(
                        f"open {order.order_type} order on Alpaca "
                        f"({order.side} {order.qty}) not tracked in journal"
                    ),
                    order_id=order.order_id,
                )
            )

    return ReconciliationReport(
        checked_at=datetime.now(UTC),
        alpaca_equity=account.equity,
        alpaca_positions=positions,
        alpaca_open_orders=orders,
        sqlite_open_trades=open_trades,
        drift=drift,
    )


def format_report(report: ReconciliationReport) -> str:
    """Human-readable summary for console output before startup."""
    lines = [
        f"Reconciliation @ {report.checked_at.isoformat()}",
        f"  Alpaca equity:        ${report.alpaca_equity:,.2f}",
        f"  Alpaca positions:     {len(report.alpaca_positions)}",
        f"  Alpaca open orders:   {len(report.alpaca_open_orders)}",
        f"  Journal open trades:  {len(report.sqlite_open_trades)}",
    ]
    if report.clean:
        lines.append("  OK: no drift detected.")
        return "\n".join(lines)

    lines.append(f"  DRIFT: {len(report.drift)} issue(s) detected:")
    for issue in report.drift:
        lines.append(f"    [{issue.kind.value}] {issue.symbol}: {issue.detail}")
    return "\n".join(lines)
