from trade_strats.strategy.labeler import Bar
from trade_strats.strategy.patterns import (
    PatternKind,
    Setup,
    Side,
    detect,
    detect_rev_strat,
    detect_three_one_two,
    detect_three_two_two,
    detect_two_two,
)

# --- Helpers ----------------------------------------------------------------

# Bar(open, high, low, close)
SEED = Bar(open=10.0, high=11.0, low=9.0, close=10.0)


def _assert_long(setup: Setup | None, kind: PatternKind, trigger: float, stop: float) -> None:
    assert setup is not None
    assert setup.kind is kind
    assert setup.side is Side.LONG
    assert setup.trigger_price == trigger
    assert setup.stop_price == stop


def _assert_short(setup: Setup | None, kind: PatternKind, trigger: float, stop: float) -> None:
    assert setup is not None
    assert setup.kind is kind
    assert setup.side is Side.SHORT
    assert setup.trigger_price == trigger
    assert setup.stop_price == stop


# --- 2-2 reversal -----------------------------------------------------------


def test_two_two_bullish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # red 2D: lower low, same-or-lower high, red close
    two_d = Bar(open=9.5, high=10.5, low=8.0, close=8.5)
    # green 2U: higher high, low >= two_d.low, green close
    two_u = Bar(open=9.0, high=12.0, low=8.0, close=11.5)
    setup = detect_two_two([prev, two_d, two_u])
    _assert_long(setup, PatternKind.TWO_TWO, trigger=12.0, stop=8.0)


def test_two_two_bearish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # green 2U
    two_u = Bar(open=10.5, high=12.5, low=10.0, close=12.0)
    # red 2D
    two_d = Bar(open=11.5, high=12.5, low=9.5, close=10.0)
    setup = detect_two_two([prev, two_u, two_d])
    _assert_short(setup, PatternKind.TWO_TWO, trigger=9.5, stop=12.5)


def test_two_two_requires_confirming_color_long() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    two_d = Bar(open=9.5, high=10.5, low=8.0, close=8.5)
    # 2U with red close — invalid
    two_u_red = Bar(open=11.5, high=12.0, low=8.0, close=9.0)
    assert detect_two_two([prev, two_d, two_u_red]) is None


def test_two_two_rejects_inside_middle() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    inside = Bar(open=10, high=10.5, low=9.5, close=10)
    two_u = Bar(open=10, high=12, low=9.5, close=11)
    assert detect_two_two([prev, inside, two_u]) is None


def test_two_two_too_few_bars() -> None:
    assert detect_two_two([SEED, SEED]) is None
    assert detect_two_two([]) is None


# --- 3-2-2 reversal ---------------------------------------------------------


def test_three_two_two_bullish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # red outside (3): high > 11, low < 9, red
    outside = Bar(open=10.5, high=12.0, low=8.0, close=9.5)
    # red 2D: high <= 12, low < 8, red
    two_d = Bar(open=9.0, high=11.0, low=7.0, close=7.5)
    # green 2U: high > 11, low >= 7, green
    two_u = Bar(open=8.0, high=12.5, low=7.0, close=12.0)
    setup = detect_three_two_two([prev, outside, two_d, two_u])
    _assert_long(setup, PatternKind.THREE_TWO_TWO, trigger=12.5, stop=7.0)


def test_three_two_two_bearish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # green outside: high > 11, low < 9, green
    outside = Bar(open=9.5, high=12.0, low=8.0, close=11.5)
    # green 2U: high > 12, low >= 8
    two_u = Bar(open=11.0, high=13.0, low=9.0, close=12.5)
    # red 2D: high <= 13, low < 9, red
    two_d = Bar(open=12.0, high=13.0, low=7.5, close=8.0)
    setup = detect_three_two_two([prev, outside, two_u, two_d])
    _assert_short(setup, PatternKind.THREE_TWO_TWO, trigger=7.5, stop=13.0)


def test_three_two_two_rejects_non_outside_first() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # 2U instead of outside
    not_outside = Bar(open=10.5, high=12.0, low=9.5, close=11.5)
    two_d = Bar(open=11.0, high=11.5, low=8.5, close=9.0)
    two_u = Bar(open=9.0, high=12.5, low=8.5, close=12.0)
    assert detect_three_two_two([prev, not_outside, two_d, two_u]) is None


def test_three_two_two_rejects_wrong_color_on_outside() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # green outside but pattern is bullish variant that requires red outside
    outside_green = Bar(open=8.5, high=12.0, low=8.0, close=11.5)
    two_d = Bar(open=9.0, high=11.0, low=7.0, close=7.5)
    two_u = Bar(open=8.0, high=12.5, low=7.0, close=12.0)
    # bullish variant rejected; no bearish variant either (second bar is 2D, not 2U)
    assert detect_three_two_two([prev, outside_green, two_d, two_u]) is None


def test_three_two_two_too_few_bars() -> None:
    assert detect_three_two_two([SEED, SEED, SEED]) is None


# --- 3-1-2 ------------------------------------------------------------------


def test_three_one_two_bullish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    outside = Bar(open=10.5, high=12.0, low=8.0, close=9.5)  # any color
    inside = Bar(open=10.0, high=11.5, low=8.5, close=10.5)  # inside (high<=12, low>=8)
    two_u = Bar(open=10.0, high=13.0, low=8.5, close=12.5)  # green 2U
    setup = detect_three_one_two([prev, outside, inside, two_u])
    _assert_long(setup, PatternKind.THREE_ONE_TWO, trigger=13.0, stop=8.5)


def test_three_one_two_bearish_positive() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    outside = Bar(open=9.5, high=12.0, low=8.0, close=11.5)
    inside = Bar(open=11.0, high=11.8, low=8.5, close=10.0)  # inside
    two_d = Bar(open=10.5, high=11.8, low=7.0, close=7.5)  # red 2D (low < 8.5)
    setup = detect_three_one_two([prev, outside, inside, two_d])
    _assert_short(setup, PatternKind.THREE_ONE_TWO, trigger=7.0, stop=11.8)


def test_three_one_two_requires_inside_middle() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    outside = Bar(open=10.5, high=12.0, low=8.0, close=9.5)
    # middle is 2U (not inside)
    not_inside = Bar(open=9.0, high=12.5, low=8.5, close=12.0)
    two_u = Bar(open=12.0, high=13.0, low=11.5, close=12.8)
    assert detect_three_one_two([prev, outside, not_inside, two_u]) is None


def test_three_one_two_requires_confirming_color() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    outside = Bar(open=10.5, high=12.0, low=8.0, close=9.5)
    inside = Bar(open=10.0, high=11.5, low=8.5, close=10.5)
    # 2U but red close — invalid
    two_u_red = Bar(open=12.5, high=13.0, low=8.5, close=9.0)
    assert detect_three_one_two([prev, outside, inside, two_u_red]) is None


# --- Rev Strat (1-2-2) -----------------------------------------------------


def test_rev_strat_bullish_positive() -> None:
    # need prev such that `a` classifies as inside
    prev = Bar(open=10, high=12, low=8, close=11)
    inside = Bar(open=10.5, high=11.5, low=9.0, close=10.2)  # inside
    # red 2D: low < 9.0, high <= 11.5, red
    two_d = Bar(open=10.0, high=11.0, low=8.0, close=8.5)
    # green 2U: high > 11.0, low >= 8.0, green
    two_u = Bar(open=9.0, high=12.0, low=8.0, close=11.5)
    setup = detect_rev_strat([prev, inside, two_d, two_u])
    _assert_long(setup, PatternKind.REV_STRAT, trigger=12.0, stop=8.0)


def test_rev_strat_bearish_positive() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    inside = Bar(open=10.5, high=11.5, low=9.0, close=10.2)
    # green 2U: high > 11.5, low >= 9.0, green
    two_u = Bar(open=11.0, high=13.0, low=10.0, close=12.5)
    # red 2D: high <= 13.0, low < 10.0, red
    two_d = Bar(open=12.0, high=13.0, low=8.5, close=9.0)
    setup = detect_rev_strat([prev, inside, two_u, two_d])
    _assert_short(setup, PatternKind.REV_STRAT, trigger=8.5, stop=13.0)


def test_rev_strat_requires_inside_first() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    # not inside — higher high
    not_inside = Bar(open=10.5, high=12.0, low=9.5, close=11.5)
    two_d = Bar(open=11.0, high=11.5, low=8.5, close=9.0)
    two_u = Bar(open=9.0, high=12.5, low=8.5, close=12.0)
    assert detect_rev_strat([prev, not_inside, two_d, two_u]) is None


def test_rev_strat_middle_bar_must_confirm_own_color_long() -> None:
    # Bullish rev strat needs red 2D middle; a green-close 2D is rejected.
    prev = Bar(open=10, high=12, low=8, close=11)
    inside = Bar(open=10.5, high=11.5, low=9.0, close=10.2)
    # 2D but green close (low < 9.0 but closes up)
    two_d_green = Bar(open=9.5, high=11.0, low=8.0, close=10.8)
    two_u = Bar(open=9.0, high=12.0, low=8.0, close=11.5)
    assert detect_rev_strat([prev, inside, two_d_green, two_u]) is None


# --- detect() aggregator ----------------------------------------------------


def test_detect_returns_empty_on_no_match() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    inside = Bar(open=10, high=10.5, low=9.5, close=10)
    another_inside = Bar(open=10, high=10.3, low=9.7, close=10)
    assert detect([prev, inside, another_inside]) == []


def test_detect_returns_two_two_alone_when_no_preceding_outside() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    two_d = Bar(open=9.5, high=10.5, low=8.0, close=8.5)
    two_u = Bar(open=9.0, high=12.0, low=8.0, close=11.5)
    setups = detect([prev, two_d, two_u])
    assert len(setups) == 1
    assert setups[0].kind is PatternKind.TWO_TWO


def test_detect_returns_both_three_two_two_and_two_two_when_applicable() -> None:
    # A 3-2-2 bullish trivially contains a 2-2 bullish at the tail.
    prev = Bar(open=10, high=11, low=9, close=10)
    outside = Bar(open=10.5, high=12.0, low=8.0, close=9.5)
    two_d = Bar(open=9.0, high=11.0, low=7.0, close=7.5)
    two_u = Bar(open=8.0, high=12.5, low=7.0, close=12.0)
    setups = detect([prev, outside, two_d, two_u])
    kinds = {s.kind for s in setups}
    assert PatternKind.THREE_TWO_TWO in kinds
    assert PatternKind.TWO_TWO in kinds


def test_detect_empty_window() -> None:
    assert detect([]) == []
    assert detect([SEED]) == []
    assert detect([SEED, SEED]) == []
