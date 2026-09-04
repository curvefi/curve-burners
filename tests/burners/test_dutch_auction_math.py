from decimal import Decimal, localcontext

import boa
import pytest
import vyper
from hypothesis import example, given, settings
from hypothesis import strategies as st

from .conftest import custom_err


RAY = 10**27
WAD = 10**18
MAX_UINT256 = 2**256 - 1
# Test domain: typical decay factors and up to a week of one-second steps.
# The contract bounds neither: gas is logarithmic in the exponent.
TYPICAL_MIN_DECAY_FACTOR_RAY = RAY // 2
MAX_TESTED_PRICE_STEPS = 604_800
PINNED_VYPER_COMMIT = "577d2534"

DAY = 24 * 60 * 60
REFERENCE_START_TOTAL = 100_000 * WAD
REFERENCE_FLOOR_TOTAL = WAD

# Rounded-down roots which make the floor reachable at the final active step.
DIVISIBLE_DURATION_DECAY_FACTOR_RAY = 992_031_276_831_159_793_484_252_056
NON_DIVISIBLE_DURATION_DECAY_FACTOR_RAY = 992_036_788_574_402_203_131_884_429

MATH_HARNESS = """
# pragma version 0.5.0b1

import contracts.burners.auction.dutch_auction_math as auction_math


@external
@pure
def mul_div_up(a: uint256, b: uint256, denominator: uint256) -> uint256:
    return auction_math.mul_div_up(a, b, denominator)


@external
@pure
def ray_pow(base_ray: uint256, exponent: uint256) -> uint256:
    return auction_math.ray_pow(base_ray, exponent)


@external
@pure
def total_price(
    start_total: uint256,
    floor_total: uint256,
    decay_factor_ray: uint256,
    elapsed: uint256,
    step_duration: uint256,
) -> uint256:
    return auction_math.total_price(
        start_total,
        floor_total,
        decay_factor_ray,
        elapsed,
        step_duration,
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


def ray_mul(a: int, b: int) -> int:
    return (a * b + RAY // 2) // RAY


def ray_pow_model(base_ray: int, exponent: int) -> int:
    result = RAY
    factor = base_ray
    while exponent:
        if exponent & 1:
            result = ray_mul(result, factor)
        exponent >>= 1
        if exponent:
            factor = ray_mul(factor, factor)
    return result


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


@given(
    a=st.integers(min_value=0, max_value=MAX_UINT256),
    b=st.integers(min_value=0, max_value=MAX_UINT256),
    denominator=st.integers(min_value=1, max_value=MAX_UINT256),
)
@settings(max_examples=100, deadline=None)
def test_mul_div_up_matches_unbounded_integer_model(auction_math, a, b, denominator):
    # Checked arithmetic: any product beyond uint256 reverts with the
    # compiler's overflow panic rather than a custom error.
    if a * b > MAX_UINT256:
        with boa.reverts():
            auction_math.mul_div_up(a, b, denominator)
    else:
        assert auction_math.mul_div_up(a, b, denominator) == ceil_div(a * b, denominator)


@pytest.mark.parametrize(
    "a,b,denominator",
    [
        (0, MAX_UINT256, 1),
        (1, 1, 2),
        (MAX_UINT256, 1, MAX_UINT256),
        (MAX_UINT256, 1, 1),
        (2**128 - 1, 2**128 + 1, 2**80),
        (2**200, 2**56 - 1, 2**192 + 5),
    ],
)
def test_mul_div_up_boundaries(auction_math, a, b, denominator):
    assert a * b <= MAX_UINT256
    assert auction_math.mul_div_up(a, b, denominator) == ceil_div(a * b, denominator)


def test_mul_div_up_reverts_on_zero_denominator(auction_math):
    with boa.reverts(custom_err("DivisionByZero()")):
        auction_math.mul_div_up(1, 1, 0)


@pytest.mark.parametrize(
    "a,b",
    [
        (MAX_UINT256, MAX_UINT256),
        (MAX_UINT256, 2),
        (2**129, 2**128),
    ],
)
def test_mul_div_up_reverts_when_product_overflows(auction_math, a, b):
    assert a * b > MAX_UINT256
    with boa.reverts():
        auction_math.mul_div_up(a, b, 1)


@given(
    base_ray=st.integers(min_value=0, max_value=RAY),
    exponent=st.integers(min_value=0, max_value=MAX_TESTED_PRICE_STEPS),
)
@example(base_ray=TYPICAL_MIN_DECAY_FACTOR_RAY, exponent=MAX_TESTED_PRICE_STEPS)
@example(base_ray=RAY - 1, exponent=MAX_TESTED_PRICE_STEPS)
@example(base_ray=RAY, exponent=MAX_TESTED_PRICE_STEPS)
@settings(max_examples=100, deadline=None)
def test_ray_pow_matches_integer_algorithm(auction_math, base_ray, exponent):
    assert auction_math.ray_pow(base_ray, exponent) == ray_pow_model(base_ray, exponent)


@given(
    base_ray=st.integers(min_value=TYPICAL_MIN_DECAY_FACTOR_RAY, max_value=RAY),
    exponent=st.integers(min_value=0, max_value=MAX_TESTED_PRICE_STEPS),
)
@example(base_ray=TYPICAL_MIN_DECAY_FACTOR_RAY, exponent=MAX_TESTED_PRICE_STEPS)
@example(base_ray=RAY - 1, exponent=MAX_TESTED_PRICE_STEPS)
@example(base_ray=RAY, exponent=MAX_TESTED_PRICE_STEPS)
@settings(max_examples=100, deadline=None)
def test_ray_pow_stays_close_to_decimal_reference(auction_math, base_ray, exponent):
    actual = auction_math.ray_pow(base_ray, exponent)
    with localcontext() as context:
        context.prec = 220
        exact = Decimal(RAY) * (Decimal(base_ray) / Decimal(RAY)) ** exponent

    # Nearest rounding per multiplication: the error is dwarfed by execution
    # noise and only its magnitude matters.
    assert abs(actual - int(exact)) <= 4 * exponent + 2


def test_ray_pow_boundaries_and_loop_limit(auction_math):
    assert auction_math.ray_pow(0, 0) == RAY
    assert auction_math.ray_pow(0, 1) == 0
    assert auction_math.ray_pow(2 * RAY, 2) == 4 * RAY
    assert auction_math.ray_pow(RAY, MAX_UINT256) == RAY


@given(
    base_ray=st.integers(min_value=0, max_value=RAY),
    first_exponent=st.integers(min_value=0, max_value=1_000),
    exponent_delta=st.integers(min_value=0, max_value=1_000),
)
@settings(max_examples=50, deadline=None)
def test_ray_pow_is_non_increasing_in_exponent(
    auction_math, base_ray, first_exponent, exponent_delta
):
    first = auction_math.ray_pow(base_ray, first_exponent)
    later = auction_math.ray_pow(base_ray, first_exponent + exponent_delta)
    assert later <= first


@given(
    # start_total * RAY must fit uint256 under the checked-product math.
    start_total=st.integers(min_value=0, max_value=MAX_UINT256 // RAY),
    floor_total=st.integers(min_value=0, max_value=MAX_UINT256),
    decay_factor_ray=st.integers(
        min_value=TYPICAL_MIN_DECAY_FACTOR_RAY,
        max_value=RAY,
    ),
    elapsed=st.integers(min_value=0, max_value=MAX_TESTED_PRICE_STEPS),
    step_duration=st.integers(min_value=1, max_value=10**6),
)
@settings(max_examples=80, deadline=None)
def test_total_price_matches_step_model(
    auction_math,
    start_total,
    floor_total,
    decay_factor_ray,
    elapsed,
    step_duration,
):
    floor_total = min(floor_total, start_total)
    steps = elapsed // step_duration
    expected = max(
        floor_total,
        start_total * ray_pow_model(decay_factor_ray, steps) // RAY,
    )
    assert (
        auction_math.total_price(
            start_total,
            floor_total,
            decay_factor_ray,
            elapsed,
            step_duration,
        )
        == expected
    )


@given(
    # start_total * RAY must fit uint256 under the checked-product math.
    start_total=st.integers(min_value=0, max_value=MAX_UINT256 // RAY),
    decay_factor_ray=st.integers(
        min_value=TYPICAL_MIN_DECAY_FACTOR_RAY,
        max_value=RAY,
    ),
    steps=st.integers(min_value=0, max_value=MAX_TESTED_PRICE_STEPS),
)
@settings(max_examples=80, deadline=None)
def test_total_price_decimal_error_bound(
    auction_math,
    start_total,
    decay_factor_ray,
    steps,
):
    actual = auction_math.total_price(start_total, 0, decay_factor_ray, steps, 1)
    with localcontext() as context:
        context.prec = 220
        exact = Decimal(start_total) * (
            Decimal(decay_factor_ray) / Decimal(RAY)
        ) ** steps

    ray_error_bound = 4 * steps + 2
    total_error_bound = ceil_div(start_total * ray_error_bound, RAY) + 1
    assert abs(actual - int(exact)) <= total_error_bound


def test_total_price_step_boundaries_floor_and_monotonicity(auction_math):
    start_total = 100_000 * WAD
    floor_total = WAD
    decay_factor_ray = 992_036_800_000_000_000_000_000_000
    step_duration = 60

    assert (
        auction_math.total_price(
            start_total, floor_total, decay_factor_ray, step_duration - 1, step_duration
        )
        == start_total
    )
    assert (
        auction_math.total_price(start_total, floor_total, decay_factor_ray, 60, 60)
        < start_total
    )

    quotes = [
        auction_math.total_price(start_total, floor_total, decay_factor_ray, step * 60, 60)
        for step in range(1_600)
    ]
    assert all(later <= earlier for earlier, later in zip(quotes, quotes[1:]))
    assert all(quote >= floor_total for quote in quotes)
    assert quotes[-1] == floor_total


@pytest.mark.parametrize(
    "duration,step_duration,decay_factor_ray,is_divisible",
    [
        (DAY, 60, DIVISIBLE_DURATION_DECAY_FACTOR_RAY, True),
        (DAY + 17, 60, NON_DIVISIBLE_DURATION_DECAY_FACTOR_RAY, False),
    ],
)
def test_floor_is_reached_at_last_active_second(
    auction_math,
    duration,
    step_duration,
    decay_factor_ray,
    is_divisible,
):
    assert (duration % step_duration == 0) is is_divisible
    elapsed = duration - 1
    final_active_step = elapsed // step_duration

    assert final_active_step > 0
    assert (
        auction_math.total_price(
            REFERENCE_START_TOTAL,
            REFERENCE_FLOOR_TOTAL,
            decay_factor_ray,
            elapsed,
            step_duration,
        )
        == REFERENCE_FLOOR_TOTAL
    )
    assert (
        auction_math.total_price(
            REFERENCE_START_TOTAL,
            REFERENCE_FLOOR_TOTAL,
            decay_factor_ray,
            elapsed - step_duration,
            step_duration,
        )
        > REFERENCE_FLOOR_TOTAL
    )


@pytest.mark.parametrize(
    "window_percent,expected_total",
    [
        (20, 10_000 * WAD - 1),
        (40, 1_000 * WAD - 1),
        (60, 100 * WAD - 1),
        (80, 10 * WAD),
    ],
)
def test_reference_geometric_price_vectors(auction_math, window_percent, expected_total):
    duration = DAY + 17  # Non-divisible by the 60-second price step.
    elapsed = duration * window_percent // 100
    quote = auction_math.total_price(
        REFERENCE_START_TOTAL,
        REFERENCE_FLOOR_TOTAL,
        NON_DIVISIBLE_DURATION_DECAY_FACTOR_RAY,
        elapsed,
        60,
    )
    assert quote == expected_total


def test_total_price_rejects_invalid_parameters(auction_math):
    with boa.reverts(custom_err("ZeroStep()")):
        auction_math.total_price(1, 0, RAY, 0, 0)
    with boa.reverts(custom_err("FloorAboveStart()")):
        auction_math.total_price(1, 2, RAY, 0, 1)
    with boa.reverts(custom_err("GrowthFactor()")):
        auction_math.total_price(1, 0, RAY + 1, 0, 1)


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
