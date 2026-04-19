import sys
from pathlib import Path

# Make the script module importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compare_fills import FillComparison, aggregate  # noqa: E402


def _fill(side: str, kind: str, predicted: float, live: float, qty: int = 100) -> FillComparison:
    return FillComparison(
        order_id="x",
        trade_id=1,
        symbol="TEST",
        side=side,
        kind=kind,
        qty=qty,
        predicted_price=predicted,
        live_price=live,
        filled_ts="2026-04-20T14:00:00Z",
    )


# ---------------------------------------------------------------------------
# Slippage sign conventions (positive = worse for trader)
# ---------------------------------------------------------------------------


def test_long_entry_fills_higher_is_positive_slippage() -> None:
    # Long entered higher than trigger → bad
    f = _fill("long", "entry", predicted=100.0, live=100.05)
    assert f.slippage_per_share == pytest_approx(0.05)


def test_long_entry_fills_lower_is_negative_slippage() -> None:
    # Long entered lower than trigger → good (gap down)
    f = _fill("long", "entry", predicted=100.0, live=99.95)
    assert f.slippage_per_share == pytest_approx(-0.05)


def test_long_stop_fills_below_stop_is_positive_slippage() -> None:
    # Long stopped out below the stop price → worse, more loss
    f = _fill("long", "stop", predicted=95.0, live=94.90)
    assert f.slippage_per_share == pytest_approx(0.10)


def test_long_target_fills_below_target_is_positive_slippage() -> None:
    # Long target filled below limit → missed some upside
    f = _fill("long", "target", predicted=110.0, live=109.95)
    assert f.slippage_per_share == pytest_approx(0.05)


def test_short_entry_fills_lower_is_positive_slippage() -> None:
    # Short sold lower than trigger → bad (sold cheap)
    f = _fill("short", "entry", predicted=100.0, live=99.95)
    assert f.slippage_per_share == pytest_approx(0.05)


def test_short_stop_fills_above_stop_is_positive_slippage() -> None:
    # Short stopped out (buy-to-cover) above stop → more loss
    f = _fill("short", "stop", predicted=105.0, live=105.10)
    assert f.slippage_per_share == pytest_approx(0.10)


def test_short_target_fills_above_target_is_positive_slippage() -> None:
    # Short target (buy-to-cover) above limit → missed some profit
    f = _fill("short", "target", predicted=90.0, live=90.05)
    assert f.slippage_per_share == pytest_approx(0.05)


def test_slippage_dollars_multiplies_by_qty() -> None:
    f = _fill("long", "entry", predicted=100.0, live=100.10, qty=250)
    assert f.slippage_dollars == pytest_approx(25.0)


def test_aggregate_computes_mean_and_total() -> None:
    fills = [
        _fill("long", "entry", predicted=100.0, live=100.10, qty=100),  # +0.10/sh, +$10
        _fill("long", "entry", predicted=200.0, live=199.95, qty=100),  # -0.05/sh, -$5
    ]
    agg = aggregate(fills)
    assert agg.n == 2
    assert agg.total_slippage_dollars == pytest_approx(5.0)
    assert agg.mean_slippage_per_share == pytest_approx(0.025)


def test_aggregate_empty_returns_zeros() -> None:
    agg = aggregate([])
    assert agg.n == 0
    assert agg.total_slippage_dollars == 0.0


# Minimal local approx helper to avoid pytest import noise; pytest is already present.
def pytest_approx(expected: float, tol: float = 1e-9) -> object:
    class _A:
        def __eq__(self, other: object) -> bool:
            return abs(float(other) - expected) < tol

    return _A()
