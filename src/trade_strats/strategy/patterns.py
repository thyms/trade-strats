from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from trade_strats.strategy.labeler import Bar, Color, Scenario, classify, color


class PatternKind(StrEnum):
    THREE_TWO_TWO = "3-2-2"
    TWO_TWO = "2-2"
    THREE_ONE_TWO = "3-1-2"
    REV_STRAT = "rev-strat"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True, slots=True)
class Setup:
    kind: PatternKind
    side: Side
    signal_bar: Bar
    trigger_price: float  # break level to enter: high for long, low for short
    stop_price: float  # protective stop: low for long, high for short


def _long_setup(kind: PatternKind, signal: Bar) -> Setup:
    return Setup(
        kind=kind,
        side=Side.LONG,
        signal_bar=signal,
        trigger_price=signal.high,
        stop_price=signal.low,
    )


def _short_setup(kind: PatternKind, signal: Bar) -> Setup:
    return Setup(
        kind=kind,
        side=Side.SHORT,
        signal_bar=signal,
        trigger_price=signal.low,
        stop_price=signal.high,
    )


def detect_two_two(bars: Sequence[Bar]) -> Setup | None:
    """2-2 reversal: (red 2D → green 2U) long, (green 2U → red 2D) short.

    Needs the bar before the pattern to classify the first pattern bar.
    """
    if len(bars) < 3:
        return None
    prev, a, b = bars[-3], bars[-2], bars[-1]
    sa = classify(prev, a)
    sb = classify(a, b)
    if (
        sa is Scenario.TWO_DOWN
        and color(a) is Color.RED
        and sb is Scenario.TWO_UP
        and color(b) is Color.GREEN
    ):
        return _long_setup(PatternKind.TWO_TWO, b)
    if (
        sa is Scenario.TWO_UP
        and color(a) is Color.GREEN
        and sb is Scenario.TWO_DOWN
        and color(b) is Color.RED
    ):
        return _short_setup(PatternKind.TWO_TWO, b)
    return None


def detect_three_two_two(bars: Sequence[Bar]) -> Setup | None:
    """3-2-2 reversal: red 3 → red 2D → green 2U (long); mirror for short."""
    if len(bars) < 4:
        return None
    prev, a, b, c = bars[-4], bars[-3], bars[-2], bars[-1]
    sa = classify(prev, a)
    if sa is not Scenario.OUTSIDE:
        return None
    sb = classify(a, b)
    sc = classify(b, c)
    if (
        color(a) is Color.RED
        and sb is Scenario.TWO_DOWN
        and color(b) is Color.RED
        and sc is Scenario.TWO_UP
        and color(c) is Color.GREEN
    ):
        return _long_setup(PatternKind.THREE_TWO_TWO, c)
    if (
        color(a) is Color.GREEN
        and sb is Scenario.TWO_UP
        and color(b) is Color.GREEN
        and sc is Scenario.TWO_DOWN
        and color(c) is Color.RED
    ):
        return _short_setup(PatternKind.THREE_TWO_TWO, c)
    return None


def detect_three_one_two(bars: Sequence[Bar]) -> Setup | None:
    """3-1-2: outside bar → inside bar → directional 2 (color must confirm direction)."""
    if len(bars) < 4:
        return None
    prev, a, b, c = bars[-4], bars[-3], bars[-2], bars[-1]
    sa = classify(prev, a)
    if sa is not Scenario.OUTSIDE:
        return None
    sb = classify(a, b)
    if sb is not Scenario.INSIDE:
        return None
    sc = classify(b, c)
    if sc is Scenario.TWO_UP and color(c) is Color.GREEN:
        return _long_setup(PatternKind.THREE_ONE_TWO, c)
    if sc is Scenario.TWO_DOWN and color(c) is Color.RED:
        return _short_setup(PatternKind.THREE_ONE_TWO, c)
    return None


def detect_rev_strat(bars: Sequence[Bar]) -> Setup | None:
    """Rev Strat (1-2-2): inside → failed 2 → reversing confirming 2.

    Bullish: 1 → red 2D → green 2U. Bearish: 1 → green 2U → red 2D.
    """
    if len(bars) < 4:
        return None
    prev, a, b, c = bars[-4], bars[-3], bars[-2], bars[-1]
    sa = classify(prev, a)
    if sa is not Scenario.INSIDE:
        return None
    sb = classify(a, b)
    sc = classify(b, c)
    if (
        sb is Scenario.TWO_DOWN
        and color(b) is Color.RED
        and sc is Scenario.TWO_UP
        and color(c) is Color.GREEN
    ):
        return _long_setup(PatternKind.REV_STRAT, c)
    if (
        sb is Scenario.TWO_UP
        and color(b) is Color.GREEN
        and sc is Scenario.TWO_DOWN
        and color(c) is Color.RED
    ):
        return _short_setup(PatternKind.REV_STRAT, c)
    return None


Detector = Callable[[Sequence[Bar]], Setup | None]

DETECTORS: tuple[Detector, ...] = (
    detect_three_two_two,
    detect_three_one_two,
    detect_rev_strat,
    detect_two_two,
)


def detect(bars: Sequence[Bar]) -> list[Setup]:
    """Run every detector against the bar window. Returns all matches (possibly overlapping).

    Bars are most-recent-last. The confirming candle is bars[-1]. At least one bar
    before the pattern is required so the first pattern bar can be classified.
    """
    return [s for d in DETECTORS if (s := d(bars)) is not None]
