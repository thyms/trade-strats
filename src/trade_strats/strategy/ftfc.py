from dataclasses import dataclass
from enum import StrEnum

from trade_strats.strategy.patterns import Side


class FtfcState(StrEnum):
    FULL_GREEN = "full_green"
    FULL_RED = "full_red"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class HigherTfOpens:
    daily: float
    four_hour: float
    one_hour: float

    def __post_init__(self) -> None:
        if self.daily <= 0 or self.four_hour <= 0 or self.one_hour <= 0:
            raise ValueError(
                f"opens must be positive (daily={self.daily}, "
                f"four_hour={self.four_hour}, one_hour={self.one_hour})"
            )


def ftfc_state(
    price: float,
    opens: HigherTfOpens,
    timeframes: tuple[str, ...] = ("1D", "4H", "1H"),
) -> FtfcState:
    """Classify full timeframe continuity against selected opens.

    FULL_GREEN: price strictly above all checked opens (long bias).
    FULL_RED:   price strictly below all checked opens (short bias).
    MIXED:      anything else, including exact equality on any TF.

    Only the timeframes listed in `timeframes` are checked. This allows
    testing variants like 1D-only or 1D+1H (skip 4H).
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    tf_opens = {"1D": opens.daily, "4H": opens.four_hour, "1H": opens.one_hour}
    checked = [tf_opens[tf] for tf in timeframes if tf in tf_opens]
    if not checked:
        return FtfcState.MIXED

    if all(price > o for o in checked):
        return FtfcState.FULL_GREEN
    if all(price < o for o in checked):
        return FtfcState.FULL_RED
    return FtfcState.MIXED


def allows(side: Side, state: FtfcState) -> bool:
    """FTFC gate: LONG requires FULL_GREEN, SHORT requires FULL_RED. No partials."""
    if side is Side.LONG:
        return state is FtfcState.FULL_GREEN
    return state is FtfcState.FULL_RED
