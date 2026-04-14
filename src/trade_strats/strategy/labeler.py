from dataclasses import dataclass
from enum import StrEnum


class Scenario(StrEnum):
    INSIDE = "1"
    TWO_UP = "2U"
    TWO_DOWN = "2D"
    OUTSIDE = "3"


class Color(StrEnum):
    GREEN = "green"
    RED = "red"
    DOJI = "doji"


@dataclass(frozen=True, slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low}")
        if not self.low <= self.open <= self.high:
            raise ValueError(f"open {self.open} outside [{self.low}, {self.high}]")
        if not self.low <= self.close <= self.high:
            raise ValueError(f"close {self.close} outside [{self.low}, {self.high}]")


def classify(prev: Bar, curr: Bar) -> Scenario:
    breaks_high = curr.high > prev.high
    breaks_low = curr.low < prev.low
    if breaks_high and breaks_low:
        return Scenario.OUTSIDE
    if breaks_high:
        return Scenario.TWO_UP
    if breaks_low:
        return Scenario.TWO_DOWN
    return Scenario.INSIDE


def color(bar: Bar) -> Color:
    if bar.close > bar.open:
        return Color.GREEN
    if bar.close < bar.open:
        return Color.RED
    return Color.DOJI
