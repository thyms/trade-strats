import pytest
from hypothesis import given
from hypothesis import strategies as st

from trade_strats.strategy.labeler import Bar, Color, Scenario, classify, color


@st.composite
def bars(draw: st.DrawFn) -> Bar:
    # Round prices to cents so arithmetic tests (e.g. mirror symmetry) stay
    # exact under float math. Realistic for US equities with 0.01 tick size.
    low = round(
        draw(st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)),
        2,
    )
    high = round(
        draw(st.floats(min_value=low, max_value=10_000.0, allow_nan=False, allow_infinity=False)),
        2,
    )
    if high < low:
        high = low
    o = round(
        draw(st.floats(min_value=low, max_value=high, allow_nan=False, allow_infinity=False)), 2
    )
    c = round(
        draw(st.floats(min_value=low, max_value=high, allow_nan=False, allow_infinity=False)), 2
    )
    return Bar(open=o, high=high, low=low, close=c)


# --- Bar validation ---------------------------------------------------------


def test_valid_bar_constructs() -> None:
    Bar(open=10, high=12, low=9, close=11)


def test_high_below_low_rejected() -> None:
    with pytest.raises(ValueError, match="high"):
        Bar(open=10, high=8, low=9, close=10)


def test_open_above_high_rejected() -> None:
    with pytest.raises(ValueError, match="open"):
        Bar(open=13, high=12, low=9, close=11)


def test_close_below_low_rejected() -> None:
    with pytest.raises(ValueError, match="close"):
        Bar(open=10, high=12, low=9, close=8)


def test_degenerate_zero_range_bar_valid() -> None:
    Bar(open=10, high=10, low=10, close=10)


# --- classify: concrete cases ----------------------------------------------


def test_equal_bars_are_inside() -> None:
    b = Bar(open=10, high=11, low=9, close=10.5)
    assert classify(b, b) is Scenario.INSIDE


def test_strict_engulfing_is_outside() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    curr = Bar(open=10, high=12, low=8, close=11)
    assert classify(prev, curr) is Scenario.OUTSIDE


def test_higher_high_only_is_two_up() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    curr = Bar(open=10.5, high=12, low=9.5, close=11.5)
    assert classify(prev, curr) is Scenario.TWO_UP


def test_lower_low_only_is_two_down() -> None:
    prev = Bar(open=10, high=11, low=9, close=10)
    curr = Bar(open=9.5, high=10.5, low=8, close=9)
    assert classify(prev, curr) is Scenario.TWO_DOWN


def test_containment_is_inside() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    curr = Bar(open=10, high=11, low=9, close=10)
    assert classify(prev, curr) is Scenario.INSIDE


def test_equal_high_with_higher_low_is_inside() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    curr = Bar(open=10, high=12, low=9, close=11)
    assert classify(prev, curr) is Scenario.INSIDE


def test_equal_low_with_lower_high_is_inside() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    curr = Bar(open=10, high=11, low=8, close=10)
    assert classify(prev, curr) is Scenario.INSIDE


def test_equal_high_and_lower_low_is_two_down() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    curr = Bar(open=10, high=12, low=7, close=9)
    assert classify(prev, curr) is Scenario.TWO_DOWN


def test_equal_low_and_higher_high_is_two_up() -> None:
    prev = Bar(open=10, high=12, low=8, close=11)
    curr = Bar(open=10, high=13, low=8, close=12)
    assert classify(prev, curr) is Scenario.TWO_UP


# --- classify: properties ---------------------------------------------------


@given(prev=bars(), curr=bars())
def test_result_is_always_a_valid_scenario(prev: Bar, curr: Bar) -> None:
    assert classify(prev, curr) in set(Scenario)


@given(prev=bars(), curr=bars())
def test_classification_matches_break_booleans(prev: Bar, curr: Bar) -> None:
    breaks_high = curr.high > prev.high
    breaks_low = curr.low < prev.low
    result = classify(prev, curr)
    if breaks_high and breaks_low:
        assert result is Scenario.OUTSIDE
    elif breaks_high:
        assert result is Scenario.TWO_UP
    elif breaks_low:
        assert result is Scenario.TWO_DOWN
    else:
        assert result is Scenario.INSIDE


@given(b=bars())
def test_self_comparison_is_always_inside(b: Bar) -> None:
    assert classify(b, b) is Scenario.INSIDE


@given(prev=bars(), curr=bars())
def test_price_mirror_swaps_two_up_and_two_down(prev: Bar, curr: Bar) -> None:
    axis = 20_000.0

    def mirror(b: Bar) -> Bar:
        return Bar(
            open=2 * axis - b.open,
            high=2 * axis - b.low,
            low=2 * axis - b.high,
            close=2 * axis - b.close,
        )

    swap = {
        Scenario.TWO_UP: Scenario.TWO_DOWN,
        Scenario.TWO_DOWN: Scenario.TWO_UP,
        Scenario.INSIDE: Scenario.INSIDE,
        Scenario.OUTSIDE: Scenario.OUTSIDE,
    }
    original = classify(prev, curr)
    mirrored = classify(mirror(prev), mirror(curr))
    assert mirrored is swap[original]


# --- color ------------------------------------------------------------------


def test_close_above_open_is_green() -> None:
    assert color(Bar(open=10, high=11, low=9, close=10.5)) is Color.GREEN


def test_close_below_open_is_red() -> None:
    assert color(Bar(open=10, high=11, low=9, close=9.5)) is Color.RED


def test_close_equal_open_is_doji() -> None:
    assert color(Bar(open=10, high=11, low=9, close=10)) is Color.DOJI


@given(b=bars())
def test_color_is_always_a_valid_value(b: Bar) -> None:
    assert color(b) in set(Color)
