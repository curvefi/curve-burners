"""Property tests for the WAD ln and exp the auction curve relies on
(snekmate.utils.math, the dependency pinned in requirements.in).

Two independent references: the exact values from Decimal at 60 significant
digits bound the approximation error, and the bit-exact Python mirror in
scripts/dutch_auction_curve.py (the model off-chain quoting uses) must agree
with the EVM integer for integer on the domain the curve uses (x > 0 for ln).
"""

from decimal import Decimal, localcontext

import boa
import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from scripts.dutch_auction_curve import wad_exp, wad_ln

WAD = 10**18
INT256_MAX = 2**255 - 1
# Below this input exp rounds to zero; from this input up it no longer fits.
EXP_ZERO_BELOW = -41446531673892822313
EXP_OVERFLOW_FROM = 135305999368893231589
# Observed error envelope: at most one wei of rounding plus a 1e-17 relative
# term (the measured relative error is ~1e-20; the margin keeps the test
# about behavior, not about one implementation's exact digits).
EXP_RELATIVE_BOUND = Decimal("1e-17")
LN_ABSOLUTE_BOUND_WEI = 2

HARNESS = """
# pragma version 0.5.0b1

from snekmate.utils import math as wad_math


@external
@pure
def ln(x: int256) -> int256:
    return wad_math._wad_ln(x)


@external
@pure
def exp(x: int256) -> int256:
    return wad_math._wad_exp(x)
"""


@pytest.fixture(scope="module")
def math():
    return boa.loads(HARNESS, name="WadMathHarness", filename="WadMathHarness.vy", no_vvm=True)


def exact_ln_wad(x: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return (Decimal(x) / WAD).ln() * WAD


def exact_exp_wad(x: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 60
        return (Decimal(x) / WAD).exp() * WAD


# Differential: the EVM and the Python mirror are the same function.


@given(x=st.integers(min_value=1, max_value=INT256_MAX))
@example(x=1)
@example(x=WAD)
@example(x=INT256_MAX)
@settings(max_examples=60, deadline=None)
def test_ln_matches_python_mirror(math, x):
    assert math.ln(x) == wad_ln(x)


@given(x=st.integers(min_value=-60 * WAD, max_value=EXP_OVERFLOW_FROM - 1))
@example(x=0)
@example(x=EXP_ZERO_BELOW)
@example(x=EXP_ZERO_BELOW + 1)
@example(x=EXP_OVERFLOW_FROM - 1)
@settings(max_examples=60, deadline=None)
def test_exp_matches_python_mirror(math, x):
    assert math.exp(x) == wad_exp(x)


# Accuracy against the high-precision reference.


@given(x=st.integers(min_value=1, max_value=10**45))
@example(x=1)
@example(x=WAD - 1)
@example(x=WAD)
@example(x=WAD + 1)
@example(x=150_000 * WAD)
@settings(max_examples=60, deadline=None)
def test_ln_is_within_two_wei_of_exact(math, x):
    assert abs(Decimal(math.ln(x)) - exact_ln_wad(x)) <= LN_ABSOLUTE_BOUND_WEI


@given(x=st.integers(min_value=-40 * WAD, max_value=EXP_OVERFLOW_FROM - 1))
@example(x=0)
@example(x=-40 * WAD)
@example(x=EXP_OVERFLOW_FROM - 1)
@settings(max_examples=60, deadline=None)
def test_exp_is_within_one_wei_plus_relative_bound(math, x):
    exact = exact_exp_wad(x)
    assert abs(Decimal(math.exp(x)) - exact) <= 1 + exact * EXP_RELATIVE_BOUND


@given(x=st.integers(min_value=10**6, max_value=10**40))
@settings(max_examples=40, deadline=None)
def test_exp_inverts_ln(math, x):
    roundtrip = math.exp(math.ln(x))
    assert abs(roundtrip - x) <= 1 + x // 10**15


# Shape and domain.


@given(a=st.integers(min_value=1, max_value=10**40), b=st.integers(min_value=1, max_value=10**40))
@settings(max_examples=40, deadline=None)
def test_ln_is_non_decreasing(math, a, b):
    lo, hi = sorted((a, b))
    assert math.ln(lo) <= math.ln(hi)


@given(
    a=st.integers(min_value=-45 * WAD, max_value=EXP_OVERFLOW_FROM - 1),
    b=st.integers(min_value=-45 * WAD, max_value=EXP_OVERFLOW_FROM - 1),
)
@settings(max_examples=40, deadline=None)
def test_exp_is_non_decreasing(math, a, b):
    lo, hi = sorted((a, b))
    assert math.exp(lo) <= math.exp(hi)


def test_exp_domain_edges(math):
    assert math.exp(EXP_ZERO_BELOW) == 0
    assert math.exp(EXP_ZERO_BELOW - 10**30) == 0
    assert math.exp(EXP_ZERO_BELOW + 1) >= 0
    assert abs(math.exp(0) - WAD) <= 1
    assert math.exp(EXP_OVERFLOW_FROM - 1) <= INT256_MAX
    with boa.reverts("math: wad_exp overflow"):
        math.exp(EXP_OVERFLOW_FROM)
    with boa.reverts("math: wad_exp overflow"):
        math.exp(INT256_MAX)


def test_ln_domain_edges(math):
    assert abs(math.ln(WAD)) <= 1
    assert math.ln(1) < 0
    assert math.ln(INT256_MAX) > 0
    # snekmate answers ln(0) with 0 instead of reverting; the curve guards
    # the zero floor itself (dutch_auction_math.ZeroFloor).
    assert math.ln(0) == 0
    with boa.reverts("math: wad_ln undefined"):
        math.ln(-1)
