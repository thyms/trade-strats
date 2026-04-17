from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from trade_strats.risk import RiskConfig


class AccountConfig(BaseModel):
    sim_equity_usd: float = Field(gt=0)
    risk_pct_per_trade: float = Field(gt=0, le=1)
    daily_loss_cap_pct: float = Field(gt=0, le=1)
    max_concurrent: int = Field(ge=1)
    max_trades_per_day: int = Field(ge=1)


class StrategyConfig(BaseModel):
    timeframe: str = "15Min"
    patterns: list[str]
    sides: list[str]
    min_rr: float = Field(ge=1)
    min_bar_atr_mult: float = Field(ge=0)
    ftfc_timeframes: list[str]
    slippage_per_share: float = Field(default=0.0, ge=0)

    @field_validator("sides")
    @classmethod
    def _validate_sides(cls, v: list[str]) -> list[str]:
        valid = {"long", "short"}
        bad = [s for s in v if s not in valid]
        if bad:
            raise ValueError(f"invalid sides {bad}; must be in {valid}")
        return v


class SessionConfig(BaseModel):
    entry_window_et: tuple[str, str]
    force_flat_et: str


class PathsConfig(BaseModel):
    db: Path
    events_log: Path
    reports_dir: Path


def _empty_datetime_list() -> list[datetime]:
    return []


class Config(BaseModel):
    mode: Literal["paper", "live"]
    account: AccountConfig
    strategy: StrategyConfig
    watchlist: list[str] = Field(min_length=1)
    session: SessionConfig
    blackouts: list[datetime] = Field(default_factory=_empty_datetime_list)
    paths: PathsConfig

    @classmethod
    def from_yaml(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text())
        return cls.model_validate(data)

    def risk_config(self) -> RiskConfig:
        return RiskConfig(
            risk_pct_per_trade=self.account.risk_pct_per_trade,
            daily_loss_cap_pct=self.account.daily_loss_cap_pct,
            max_concurrent=self.account.max_concurrent,
            max_trades_per_day=self.account.max_trades_per_day,
            min_rr=self.strategy.min_rr,
            min_bar_atr_mult=self.strategy.min_bar_atr_mult,
        )
