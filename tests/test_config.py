from datetime import datetime
from pathlib import Path

import pytest
import yaml

from trade_strats.config import Config

VALID_CONFIG: dict[str, object] = {
    "mode": "paper",
    "account": {
        "sim_equity_usd": 50000,
        "risk_pct_per_trade": 0.005,
        "daily_loss_cap_pct": 0.02,
        "max_concurrent": 3,
        "max_trades_per_day": 5,
    },
    "strategy": {
        "timeframe": "15Min",
        "patterns": ["3-2-2", "2-2", "3-1-2", "rev-strat"],
        "sides": ["long", "short"],
        "min_rr": 3.0,
        "min_bar_atr_mult": 0.5,
        "ftfc_timeframes": ["1D", "4H", "1H"],
    },
    "watchlist": ["SPY", "QQQ"],
    "session": {"entry_window_et": ["09:30", "15:45"], "force_flat_et": "15:55"},
    "blackouts": [],
    "paths": {
        "db": "data/trades.db",
        "events_log": "data/events.jsonl",
        "reports_dir": "reports",
    },
}


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_loads_example_config(tmp_path: Path) -> None:
    path = _write(tmp_path, VALID_CONFIG)
    config = Config.from_yaml(path)
    assert config.mode == "paper"
    assert config.account.sim_equity_usd == 50000
    assert config.watchlist == ["SPY", "QQQ"]
    assert config.strategy.patterns == ["3-2-2", "2-2", "3-1-2", "rev-strat"]
    assert config.paths.db == Path("data/trades.db")


def test_risk_config_maps_fields() -> None:
    config = Config.model_validate(VALID_CONFIG)
    rc = config.risk_config()
    assert rc.risk_pct_per_trade == 0.005
    assert rc.daily_loss_cap_pct == 0.02
    assert rc.max_concurrent == 3
    assert rc.max_trades_per_day == 5
    assert rc.min_rr == 3.0
    assert rc.min_bar_atr_mult == 0.5


def test_rejects_invalid_mode() -> None:
    bad = dict(VALID_CONFIG)
    bad["mode"] = "demo"
    with pytest.raises(ValueError):
        Config.model_validate(bad)


def test_rejects_non_positive_risk_pct() -> None:
    bad = {**VALID_CONFIG, "account": {**VALID_CONFIG["account"], "risk_pct_per_trade": 0.0}}  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        Config.model_validate(bad)


def test_rejects_invalid_side() -> None:
    bad = {**VALID_CONFIG, "strategy": {**VALID_CONFIG["strategy"], "sides": ["long", "wrong"]}}  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="invalid sides"):
        Config.model_validate(bad)


def test_rejects_empty_watchlist() -> None:
    bad = {**VALID_CONFIG, "watchlist": []}
    with pytest.raises(ValueError):
        Config.model_validate(bad)


def test_parses_blackout_datetimes() -> None:
    with_blackouts = {**VALID_CONFIG, "blackouts": ["2026-05-07T14:00:00-04:00"]}
    config = Config.model_validate(with_blackouts)
    assert len(config.blackouts) == 1
    assert isinstance(config.blackouts[0], datetime)
    assert config.blackouts[0].tzinfo is not None
