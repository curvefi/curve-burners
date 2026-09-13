"""Tests for contracts/burners/auction/dutch_auction_math.vy: the exponential
auction curve and the payment rounding.

The curve is checked against the exact geometric interpolation
P(u) = start^(1-u) * floor^u from Decimal (60 significant digits) and, for
dense full-window sweeps, against the bit-exact Python mirror in
scripts/dutch_auction_curve.py once that mirror is shown to agree with the
EVM on sampled points.
"""

from decimal import Decimal, localcontext

import boa
import pytest
import vyper
from hypothesis import example, given, settings
from hypothesis import strategies as st

from scripts import dutch_auction_curve as curve

from .conftest import custom_err


WAD = 10**18
INT256_MAX = 2**255 - 1
MAX_UINT256 = 2**256 - 1
PINNED_VYPER_COMMIT = "577d2534"

MINUTE = 60
DAY = 24 * 60 * 60
WEEK = 7 * DAY
REFERENCE_START_TOTAL = 100_000 * WAD
REFERENCE_FLOOR_TOTAL = WAD
# Error envelope for the on-chain price against the exact interpolation: the
# measured maximum is ~1.4e5 wei absolute and ~1.3e-18 relative; the bound
# leaves room without hiding a real regression.
PRICE_ABSOLUTE_BOUND_WEI = 10**6
PRICE_RELATIVE_BOUND = Decimal("1e-15")

MATH_HARNESS = """
# pragma version 0.5.0b1

import contracts.burners.auction.dutch_auction_math as auction_math


@external
@pure
def mul_div_up(a: uint256, b: uint256, denominator: uint256) -> uint256:
    return auction_math.mul_div_up(a, b, denominator)


@external
@pure
def curve_logs(start_total: uint256, floor_total: uint256) -> (int256, uint256):
    return auction_math.curve_logs(start_total, floor_total)


@external
@pure
def total_price(
    start_total: uint256,
    floor_total: uint256,
    log_start: int256,
    log_drop: uint256,
    decay_steps: uint256,
    elapsed: uint256,
    step_duration: uint256,
) -> uint256:
    return auction_math.total_price(
        start_total, floor_total, log_start, log_drop, decay_steps, elapsed, step_duration
    )


@external
@pure
def proportional_payment(
    total_price: uint256,
    amount: uint256,
    initial_amount: uint256,
) -> uint256:
    return auction_math.proportional_payment(total_price, amount, initial_amount)
"""


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def exact_price(start_total: int, floor_total: int, step: int, steps: int) -> Decimal:
    """start^(1-u) * floor^u at u = step / steps, exact to 60 digits."""
    with localcontext() as ctx:
        ctx.prec = 60
        start, floor = Decimal(start_total), Decimal(floor_total)
        if step <= 0:
            return start
        if step >= steps:
            return floor
        return (start.ln() - (start.ln() - floor.ln()) * Decimal(step) / Decimal(steps)).exp()


class Curve:
    """A prepared curve: the same parameters the core stores."""

    def __init__(self, start_total: int, floor_total: int, auction_length: int, step_duration: int):
        self.start_total = start_total
        self.floor_total = floor_total
        self.step_duration = step_duration
        self.steps = curve.decay_steps(auction_length, step_duration)
        self.log_start, self.log_drop = curve.curve_logs(start_total, floor_total)

    def on_chain(self, math, elapsed: int) -> int:
        return math.total_price(
            self.start_total,
            self.floor_total,
            self.log_start,
            self.log_drop,
            self.steps,
            elapsed,
            self.step_duration,
        )

    def mirror(self, elapsed: int) -> int:
        return curve.total_price(
            self.start_total,
            self.floor_total,
            self.log_start,
            self.log_drop,
            self.steps,
            elapsed,
            self.step_duration,
        )

    def exact(self, elapsed: int) -> Decimal:
        return exact_price(
            self.start_total, self.floor_total, elapsed // self.step_duration, self.steps
        )


@pytest.fixture(scope="module")
def auction_math():
    return boa.loads(
        MATH_HARNESS,
        name="DutchAuctionMathHarness",
        filename="DutchAuctionMathHarness.vy",
        no_vvm=True,
    )


def test_compiler_pin():
    assert vyper.__version__ == "0.5.0b1"
    assert PINNED_VYPER_COMMIT.startswith(vyper.__commit__.strip())


# Curve preparation


@given(
    start_total=st.integers(min_value=1, max_value=INT256_MAX),
    floor_total=st.integers(min_value=1, max_value=INT256_MAX),
)
@example(start_total=REFERENCE_START_TOTAL, floor_total=REFERENCE_FLOOR_TOTAL)
@example(start_total=INT256_MAX, floor_total=1)
@example(start_total=WAD, floor_total=WAD)
@settings(max_examples=40, deadline=None)
def test_curve_logs_match_mirror_and_reference(auction_math, start_total, floor_total):
    start_total, floor_total = max(start_total, floor_total), min(start_total, floor_total)
    log_start, log_drop = auction_math.curve_logs(start_total, floor_total)
    assert (log_start, log_drop) == curve.curve_logs(start_total, floor_total)
    with localcontext() as ctx:
        ctx.prec = 60
        exact_start = (Decimal(start_total) / WAD).ln() * WAD
        exact_drop = (Decimal(start_total) / Decimal(floor_total)).ln() * WAD
    assert abs(Decimal(log_start) - exact_start) <= 2
    assert abs(Decimal(log_drop) - exact_drop) <= 4


def test_curve_logs_reject_invalid_parameters(auction_math):
    with boa.reverts(custom_err("StartTotalTooLarge()")):
        auction_math.curve_logs(INT256_MAX + 1, 1)
    with boa.reverts(custom_err("FloorAboveStart()")):
        auction_math.curve_logs(WAD, WAD + 1)
    with boa.reverts(custom_err("ZeroFloor()")):
        auction_math.curve_logs(WAD, 0)


# The price function: endpoints, range, steps


@pytest.mark.parametrize(
    "auction_length,step_duration",
    [(DAY, MINUTE), (DAY, 1), (DAY + 17, MINUTE), (WEEK, MINUTE), (WEEK, 1), (DAY, DAY - 1)],
)
def test_endpoints_are_exact_and_window_edges_hold(auction_math, auction_length, step_duration):
    c = Curve(REFERENCE_START_TOTAL, REFERENCE_FLOOR_TOTAL, auction_length, step_duration)
    last_active = auction_length - 1
    # Exactly start during the whole first step.
    assert c.on_chain(auction_math, 0) == REFERENCE_START_TOTAL
    assert c.on_chain(auction_math, step_duration - 1) == REFERENCE_START_TOTAL
    # Below start from the second step on (unless the window holds one step).
    if c.steps > 1:
        assert c.on_chain(auction_math, step_duration) < REFERENCE_START_TOTAL
    # Exactly floor from the start of the last active step, including the
    # last active second, and beyond the window (activity is checked
    # elsewhere; the function itself stays at the floor).
    floor_from = c.steps * step_duration
    assert floor_from <= last_active
    assert c.on_chain(auction_math, floor_from) == REFERENCE_FLOOR_TOTAL
    assert c.on_chain(auction_math, last_active) == REFERENCE_FLOOR_TOTAL
    assert c.on_chain(auction_math, auction_length + WEEK) == REFERENCE_FLOOR_TOTAL
    # Strictly above the floor one step earlier.
    if c.steps > 1:
        assert c.on_chain(auction_math, floor_from - step_duration) > REFERENCE_FLOOR_TOTAL


def test_price_is_constant_within_a_step(auction_math):
    c = Curve(REFERENCE_START_TOTAL, REFERENCE_FLOOR_TOTAL, DAY, MINUTE)
    for step in (1, 2, 700, c.steps - 1):
        first = c.on_chain(auction_math, step * MINUTE)
        assert c.on_chain(auction_math, step * MINUTE + 30) == first
        assert c.on_chain(auction_math, step * MINUTE + MINUTE - 1) == first


@pytest.mark.parametrize(
    "start_total,floor_total",
    [
        (REFERENCE_START_TOTAL, REFERENCE_FLOOR_TOTAL),
        (150_000 * WAD, 100 * WAD),
        (100 * WAD, 1),
        (WAD + 1, WAD),
        (INT256_MAX, 1),
    ],
)
@pytest.mark.parametrize("step_duration", [1, MINUTE])
def test_full_day_sweep_is_bounded_and_non_increasing(
    auction_math, start_total, floor_total, step_duration
):
    """Dense sweep through the Python mirror (agreement with the EVM is
    established on sampled points): every quote stays inside
    [floor, start], never rises between steps, and tracks the exact
    interpolation within the error envelope."""
    c = Curve(start_total, floor_total, DAY, step_duration)
    sample = {0, step_duration, c.steps // 3 * step_duration, DAY - 1, DAY - step_duration}
    for elapsed in sample:
        assert c.on_chain(auction_math, elapsed) == c.mirror(elapsed)
    previous = start_total
    stride = max(1, c.steps // 2000)
    for step in range(0, c.steps + 1, stride):
        price = c.mirror(step * step_duration)
        assert floor_total <= price <= start_total
        assert price <= previous
        previous = price
        exact = c.exact(step * step_duration)
        assert abs(Decimal(price) - exact) <= max(
            PRICE_ABSOLUTE_BOUND_WEI, exact * PRICE_RELATIVE_BOUND
        )


@given(
    start_total=st.integers(min_value=1, max_value=10**30),
    floor_total=st.integers(min_value=1, max_value=10**30),
    auction_length=st.integers(min_value=2, max_value=WEEK),
    step_duration=st.integers(min_value=1, max_value=DAY),
    elapsed=st.integers(min_value=0, max_value=WEEK),
)
@settings(max_examples=60, deadline=None)
def test_on_chain_price_matches_mirror_and_reference(
    auction_math, start_total, floor_total, auction_length, step_duration, elapsed
):
    start_total, floor_total = max(start_total, floor_total), min(start_total, floor_total)
    if curve.decay_steps(auction_length, step_duration) == 0:
        return
    c = Curve(start_total, floor_total, auction_length, step_duration)
    price = c.on_chain(auction_math, elapsed)
    assert price == c.mirror(elapsed)
    assert floor_total <= price <= start_total
    exact = c.exact(elapsed)
    assert abs(Decimal(price) - exact) <= max(PRICE_ABSOLUTE_BOUND_WEI, exact * PRICE_RELATIVE_BOUND)


def test_equal_time_gives_equal_percentage_drop(auction_math):
    """100 000 -> 1 over the window passes 10 000, 1 000, 100, 10 at each
    successive fifth of the decay (at the nearest whole step)."""
    c = Curve(REFERENCE_START_TOTAL, REFERENCE_FLOOR_TOTAL, DAY, MINUTE)
    for fifth, decade in zip((1, 2, 3, 4), (10_000, 1_000, 100, 10)):
        step = c.steps * fifth // 5
        price = c.on_chain(auction_math, step * MINUTE)
        # The whole-step rounding of `step` is the only deviation.
        assert abs(Decimal(price) - c.exact(step * MINUTE)) <= PRICE_ABSOLUTE_BOUND_WEI
        assert abs(price - decade * WAD) <= decade * WAD // 50


def test_flat_curve_stays_at_start(auction_math):
    c = Curve(WAD, WAD, DAY, MINUTE)
    for elapsed in (0, MINUTE, DAY // 2, DAY - 1, WEEK):
        assert c.on_chain(auction_math, elapsed) == WAD


def test_close_start_and_floor_stay_in_range(auction_math):
    for delta in (1, 10, 10**6, 10**12):
        c = Curve(100 * WAD + delta, 100 * WAD, DAY, 1)
        previous = c.start_total
        for elapsed in (0, 1, 2, DAY // 3, DAY // 2, DAY - 2, DAY - 1):
            price = c.on_chain(auction_math, elapsed)
            assert c.floor_total <= price <= c.start_total
            assert price <= previous
            previous = price


def test_total_price_rejects_zero_step(auction_math):
    c = Curve(REFERENCE_START_TOTAL, REFERENCE_FLOOR_TOTAL, DAY, MINUTE)
    with boa.reverts(custom_err("ZeroStep()")):
        auction_math.total_price(
            c.start_total, c.floor_total, c.log_start, c.log_drop, c.steps, 0, 0
        )


# Payment rounding


@given(
    a=st.integers(min_value=0, max_value=MAX_UINT256),
    b=st.integers(min_value=0, max_value=MAX_UINT256),
    denominator=st.integers(min_value=1, max_value=MAX_UINT256),
)
@settings(max_examples=100, deadline=None)
def test_mul_div_up_matches_unbounded_integer_model(auction_math, a, b, denominator):
    if a * b > MAX_UINT256:
        with boa.reverts():
            auction_math.mul_div_up(a, b, denominator)
    else:
        assert auction_math.mul_div_up(a, b, denominator) == ceil_div(a * b, denominator)


@pytest.mark.parametrize(
    "a,b,denominator",
    [(0, 5, 3), (5, 0, 3), (MAX_UINT256, 1, 1), (MAX_UINT256, 1, MAX_UINT256), (7, 3, 5)],
)
def test_mul_div_up_boundaries(auction_math, a, b, denominator):
    assert auction_math.mul_div_up(a, b, denominator) == ceil_div(a * b, denominator)


def test_mul_div_up_reverts_on_zero_denominator(auction_math):
    with boa.reverts(custom_err("DivisionByZero()")):
        auction_math.mul_div_up(1, 1, 0)


@given(
    total=st.integers(min_value=0, max_value=MAX_UINT256),
    amount=st.integers(min_value=0, max_value=MAX_UINT256),
    initial_amount=st.integers(min_value=1, max_value=MAX_UINT256),
)
@settings(max_examples=80, deadline=None)
def test_quote_helpers_round_up(auction_math, total, amount, initial_amount):
    if total * amount > MAX_UINT256:
        with boa.reverts():
            auction_math.proportional_payment(total, amount, initial_amount)
    else:
        assert auction_math.proportional_payment(total, amount, initial_amount) == ceil_div(
            total * amount, initial_amount
        )


def test_quote_helpers_zero_denominator(auction_math):
    with boa.reverts(custom_err("DivisionByZero()")):
        auction_math.proportional_payment(1, 1, 0)
