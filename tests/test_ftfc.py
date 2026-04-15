import pytest
from hypothesis import given
from hypothesis import strategies as st

from trade_strats.strategy.ftfc import FtfcState, HigherTfOpens, allows, ftfc_state
from trade_strats.strategy.patterns import Side

finite_price = st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)


@st.composite
def opens(draw: st.DrawFn) -> HigherTfOpens:
    return HigherTfOpens(
        daily=draw(finite_price),
        four_hour=draw(finite_price),
        one_hour=draw(finite_price),
    )


# --- HigherTfOpens validation -----------------------------------------------


def test_valid_opens_construct() -> None:
    HigherTfOpens(daily=100.0, four_hour=101.0, one_hour=102.0)


def test_non_positive_daily_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        HigherTfOpens(daily=0.0, four_hour=1.0, one_hour=1.0)


def test_non_positive_four_hour_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        HigherTfOpens(daily=1.0, four_hour=-0.01, one_hour=1.0)


def test_non_positive_one_hour_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        HigherTfOpens(daily=1.0, four_hour=1.0, one_hour=0.0)


# --- ftfc_state: concrete cases --------------------------------------------


def test_price_above_all_opens_is_full_green() -> None:
    o = HigherTfOpens(daily=100.0, four_hour=101.0, one_hour=102.0)
    assert ftfc_state(105.0, o) is FtfcState.FULL_GREEN


def test_price_below_all_opens_is_full_red() -> None:
    o = HigherTfOpens(daily=100.0, four_hour=99.0, one_hour=98.0)
    assert ftfc_state(95.0, o) is FtfcState.FULL_RED


def test_price_equal_to_daily_is_mixed() -> None:
    o = HigherTfOpens(daily=100.0, four_hour=95.0, one_hour=95.0)
    assert ftfc_state(100.0, o) is FtfcState.MIXED


def test_price_equal_to_four_hour_is_mixed() -> None:
    o = HigherTfOpens(daily=95.0, four_hour=100.0, one_hour=95.0)
    assert ftfc_state(100.0, o) is FtfcState.MIXED


def test_price_equal_to_one_hour_is_mixed() -> None:
    o = HigherTfOpens(daily=95.0, four_hour=95.0, one_hour=100.0)
    assert ftfc_state(100.0, o) is FtfcState.MIXED


def test_one_open_above_two_below_is_mixed() -> None:
    o = HigherTfOpens(daily=105.0, four_hour=95.0, one_hour=95.0)
    assert ftfc_state(100.0, o) is FtfcState.MIXED


def test_two_opens_above_one_below_is_mixed() -> None:
    o = HigherTfOpens(daily=105.0, four_hour=105.0, one_hour=95.0)
    assert ftfc_state(100.0, o) is FtfcState.MIXED


def test_non_positive_price_rejected() -> None:
    o = HigherTfOpens(daily=100.0, four_hour=100.0, one_hour=100.0)
    with pytest.raises(ValueError, match="price"):
        ftfc_state(0.0, o)
    with pytest.raises(ValueError, match="price"):
        ftfc_state(-1.0, o)


# --- ftfc_state: properties -------------------------------------------------


@given(price=finite_price, o=opens())
def test_result_is_always_valid_state(price: float, o: HigherTfOpens) -> None:
    assert ftfc_state(price, o) in set(FtfcState)


@given(price=finite_price, o=opens())
def test_matches_manual_strict_inequality(price: float, o: HigherTfOpens) -> None:
    all_above = price > o.daily and price > o.four_hour and price > o.one_hour
    all_below = price < o.daily and price < o.four_hour and price < o.one_hour
    result = ftfc_state(price, o)
    if all_above:
        assert result is FtfcState.FULL_GREEN
    elif all_below:
        assert result is FtfcState.FULL_RED
    else:
        assert result is FtfcState.MIXED


@given(price=finite_price, o=opens())
def test_mirror_swaps_green_and_red(price: float, o: HigherTfOpens) -> None:
    axis = 20_000.0
    mirrored_price = 2 * axis - price
    mirrored_opens = HigherTfOpens(
        daily=2 * axis - o.daily,
        four_hour=2 * axis - o.four_hour,
        one_hour=2 * axis - o.one_hour,
    )
    swap = {
        FtfcState.FULL_GREEN: FtfcState.FULL_RED,
        FtfcState.FULL_RED: FtfcState.FULL_GREEN,
        FtfcState.MIXED: FtfcState.MIXED,
    }
    original = ftfc_state(price, o)
    mirrored = ftfc_state(mirrored_price, mirrored_opens)
    assert mirrored is swap[original]


# --- allows() ---------------------------------------------------------------


def test_long_allowed_only_on_full_green() -> None:
    assert allows(Side.LONG, FtfcState.FULL_GREEN) is True
    assert allows(Side.LONG, FtfcState.FULL_RED) is False
    assert allows(Side.LONG, FtfcState.MIXED) is False


def test_short_allowed_only_on_full_red() -> None:
    assert allows(Side.SHORT, FtfcState.FULL_RED) is True
    assert allows(Side.SHORT, FtfcState.FULL_GREEN) is False
    assert allows(Side.SHORT, FtfcState.MIXED) is False
