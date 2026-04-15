import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trade_strats.backtest import (
    BacktestResult,
    PatternBreakdown,
    SimulatedTrade,
    WalkForwardReport,
)
from trade_strats.config import Config


def _pf(value: float) -> float | str:
    return "inf" if value == float("inf") else value


def _config_summary(config: Config, starting_equity: float) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "patterns": config.strategy.patterns,
        "sides": config.strategy.sides,
        "min_rr": config.strategy.min_rr,
        "min_bar_atr_mult": config.strategy.min_bar_atr_mult,
        "ftfc_timeframes": config.strategy.ftfc_timeframes,
        "risk_pct_per_trade": config.account.risk_pct_per_trade,
        "daily_loss_cap_pct": config.account.daily_loss_cap_pct,
        "max_concurrent": config.account.max_concurrent,
        "max_trades_per_day": config.account.max_trades_per_day,
        "starting_equity": starting_equity,
    }


def _trade_to_dict(t: SimulatedTrade) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "side": t.side.value,
        "kind": t.kind.value,
        "entry_ts": t.entry_ts.isoformat(),
        "entry_price": t.entry_price,
        "stop_price": t.stop_price,
        "target_price": t.target_price,
        "qty": t.qty,
        "exit_ts": t.exit_ts.isoformat(),
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "realized_pnl": t.realized_pnl,
        "r_multiple": t.r_multiple,
        "risk_per_share": t.risk_per_share,
    }


def _result_to_dict(r: BacktestResult) -> dict[str, Any]:
    return {
        "starting_equity": r.starting_equity,
        "ending_equity": r.ending_equity,
        "total_pnl": r.total_pnl,
        "total_trades": r.total_trades,
        "win_count": r.win_count,
        "loss_count": r.loss_count,
        "win_rate": r.win_rate,
        "avg_win_r": r.avg_win_r,
        "avg_loss_r": r.avg_loss_r,
        "profit_factor": _pf(r.profit_factor),
        "max_drawdown": r.max_drawdown,
        "max_drawdown_pct": r.max_drawdown_pct,
        "trades": [_trade_to_dict(t) for t in r.trades],
    }


def _breakdown_to_dict(b: PatternBreakdown) -> dict[str, Any]:
    return {
        "symbol": b.symbol,
        "pattern": b.pattern,
        "trade_count": b.trade_count,
        "win_count": b.win_count,
        "win_rate": b.win_rate,
        "total_pnl": b.total_pnl,
        "avg_r": b.avg_r,
        "profit_factor": _pf(b.profit_factor),
    }


def backtest_to_dict(
    result: BacktestResult,
    symbol: str,
    start: str,
    end: str,
    config: Config,
) -> dict[str, Any]:
    return {
        "kind": "backtest",
        "symbol": symbol,
        "run_at": datetime.now(UTC).isoformat(),
        "range": {"start": start, "end": end},
        "config": _config_summary(config, result.starting_equity),
        "result": _result_to_dict(result),
    }


def walk_forward_to_dict(
    report: WalkForwardReport,
    start: str,
    end: str,
    config: Config,
    starting_equity: float,
) -> dict[str, Any]:
    return {
        "kind": "walk_forward",
        "run_at": datetime.now(UTC).isoformat(),
        "range": {"start": start, "end": end},
        "config": _config_summary(config, starting_equity),
        "summary": {
            "total_pnl": report.total_pnl,
            "total_trades": report.total_trades,
            "symbols": len(report.results),
        },
        "per_symbol": {
            symbol: _result_to_dict(result) for symbol, result in report.results.items()
        },
        "per_pattern": [_breakdown_to_dict(b) for b in report.breakdowns],
    }


def _backtest_markdown(data: dict[str, Any]) -> str:
    result = data["result"]
    pf = result["profit_factor"]
    pf_str = str(pf) if isinstance(pf, str) else f"{pf:.2f}"
    lines = [
        f"# Backtest: {data['symbol']} {data['range']['start']} to {data['range']['end']}",
        "",
        f"**Run at:** {data['run_at']}",
        f"**Mode:** {data['config']['mode']}",
        f"**Starting equity:** ${data['config']['starting_equity']:,.2f}",
        "",
        "## Result",
        "",
        f"- Trades: {result['total_trades']} ({result['win_count']}W / {result['loss_count']}L)",
        f"- Win rate: {result['win_rate'] * 100:.1f}%",
        f"- Total P&L: ${result['total_pnl']:,.2f}",
        f"- Ending equity: ${result['ending_equity']:,.2f}",
        f"- Avg win: {result['avg_win_r']:.2f}R",
        f"- Avg loss: {result['avg_loss_r']:.2f}R",
        f"- Profit factor: {pf_str}",
        f"- Max drawdown: ${result['max_drawdown']:,.2f} ({result['max_drawdown_pct'] * 100:.1f}%)",
        "",
    ]
    return "\n".join(lines) + "\n"


def _walk_forward_markdown(data: dict[str, Any]) -> str:
    cfg = data["config"]
    summary = data["summary"]
    lines = [
        f"# Walk-forward: {data['range']['start']} to {data['range']['end']}",
        "",
        f"**Run at:** {data['run_at']}",
        f"**Mode:** {cfg['mode']}",
        f"**Starting equity (per symbol):** ${cfg['starting_equity']:,.2f}",
        f"**Patterns:** {', '.join(cfg['patterns'])}",
        f"**Sides:** {', '.join(cfg['sides'])}",
        f"**Min R:R:** {cfg['min_rr']}",
        "",
        "## Summary",
        "",
        f"- Symbols: {summary['symbols']}",
        f"- Total trades: {summary['total_trades']}",
        f"- Total P&L: ${summary['total_pnl']:,.2f}",
        "",
        "## Per-symbol",
        "",
        "| Symbol | Trades | Win % | PnL | PF | Max DD |",
        "|--------|-------:|------:|----:|---:|-------:|",
    ]
    for symbol, result in data["per_symbol"].items():
        pf = result["profit_factor"]
        pf_str = str(pf) if isinstance(pf, str) else f"{pf:.2f}"
        lines.append(
            f"| {symbol} | {result['total_trades']} | "
            f"{result['win_rate'] * 100:.1f}% | "
            f"${result['total_pnl']:,.2f} | {pf_str} | "
            f"${result['max_drawdown']:,.2f} ({result['max_drawdown_pct'] * 100:.1f}%) |"
        )
    lines.extend(
        [
            "",
            "## Per-pattern breakdown",
            "",
            "| Symbol | Pattern | N | Wins | Win % | PnL | Avg R | PF |",
            "|--------|---------|--:|-----:|------:|----:|------:|---:|",
        ]
    )
    for b in data["per_pattern"]:
        pf = b["profit_factor"]
        pf_str = str(pf) if isinstance(pf, str) else f"{pf:.2f}"
        lines.append(
            f"| {b['symbol']} | {b['pattern']} | {b['trade_count']} | "
            f"{b['win_count']} | {b['win_rate'] * 100:.1f}% | "
            f"${b['total_pnl']:,.2f} | {b['avg_r']:.2f} | {pf_str} |"
        )
    return "\n".join(lines) + "\n"


def save_backtest(
    result: BacktestResult,
    out_dir: Path,
    symbol: str,
    start: str,
    end: str,
    config: Config,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(UTC).date().isoformat()
    stem = f"{symbol}_{start}_to_{end}_run-{run_date}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    data = backtest_to_dict(result, symbol, start, end, config)
    json_path.write_text(json.dumps(data, indent=2))
    md_path.write_text(_backtest_markdown(data))
    return json_path, md_path


def save_walk_forward(
    report: WalkForwardReport,
    out_dir: Path,
    start: str,
    end: str,
    config: Config,
    starting_equity: float,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(UTC).date().isoformat()
    stem = f"{start}_to_{end}_run-{run_date}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    data = walk_forward_to_dict(report, start, end, config, starting_equity)
    json_path.write_text(json.dumps(data, indent=2))
    md_path.write_text(_walk_forward_markdown(data))
    return json_path, md_path
