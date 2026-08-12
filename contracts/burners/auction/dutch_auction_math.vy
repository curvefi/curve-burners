# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title Dutch auction math
@author Curve Finance
@license MIT
@notice Upward-rounded helpers for step-geometric auction pricing.
@dev Products are computed in checked uint256 arithmetic: quotes revert if
     a * b overflows. Auction totals, amounts, and RAY factors stay far below
     that domain by deployment policy. No Snekmate or Yearn implementation
     code was used.
"""


error DivisionByZero:
    pass


error ZeroStep:
    pass


error FloorAboveStart:
    pass


error GrowthFactor:
    pass


RAY: constant(uint256) = 10**27
WAD: constant(uint256) = 10**18

# Deployment parameters must keep the decay factor and active step count in
# this domain. The integrating contract enforces these bounds when it validates
# immutable curve parameters.
MIN_SUPPORTED_DECAY_FACTOR_RAY: constant(uint256) = RAY // 2
MAX_SUPPORTED_PRICE_STEPS: constant(uint256) = 100_000

# Within the supported deployment domain, if exact = RAY * (base / RAY)**n:
#   ceil(exact) <= ray_pow_up(base, n) <= ceil(exact) + 8 * n + 1
# in raw RAY units. Consequently, total_price's excess over the exact geometric
# quote is at most ceil(start_total * (8 * n + 2) / RAY) + 1 raw target units;
# the extra RAY atom covers ceil(exact) - exact.
# The bound conservatively covers every upward rounding in at most 17 squarings
# and 17 accumulator multiplications for n <= MAX_SUPPORTED_PRICE_STEPS.
RAY_POW_ERROR_PER_STEP: constant(uint256) = 8


@internal
@pure
def mul_div_up(a: uint256, b: uint256, denominator: uint256) -> uint256:
    """
    @notice Calculate ceil(a * b / denominator).
    @dev The subtract-then-increment form rounds up without the overflow the
         usual `+ denominator - 1` adjustment could add on top of a * b.
    """
    assert denominator != 0, DivisionByZero()
    if a == 0 or b == 0:
        return 0
    return (a * b - 1) // denominator + 1


@internal
@pure
def ray_pow_up(base_ray: uint256, exponent: uint256) -> uint256:
    """
    @notice Calculate an upward-rounded RAY fixed-point power.
    @dev The loop has one iteration per exponent bit and is therefore bounded
         by the uint256 width. Results which do not fit uint256 revert. Auction
         deployments support base_ray in [RAY / 2, RAY] and exponent <= 100,000;
         callers must enforce that domain as part of curve validation.
    """
    result: uint256 = RAY
    factor: uint256 = base_ray
    remaining_exponent: uint256 = exponent

    for _i: uint256 in range(256):
        if remaining_exponent == 0:
            return result
        if remaining_exponent & 1 != 0:
            result = self.mul_div_up(result, factor, RAY)
        remaining_exponent >>= 1
        if remaining_exponent != 0:
            factor = self.mul_div_up(factor, factor, RAY)

    return result


@internal
@pure
def total_price(
    start_total: uint256,
    floor_total: uint256,
    decay_factor_ray: uint256,
    elapsed: uint256,
    step_duration: uint256,
) -> uint256:
    """
    @notice Quote the full lot at a discrete elapsed-time step.
    @dev Returns max(floor_total, ceil(start_total * decay**steps)).
         The integrating contract must enforce the supported factor/step domain
         documented above when validating immutable deployment parameters.
    """
    assert step_duration != 0, ZeroStep()
    assert floor_total <= start_total, FloorAboveStart()
    assert decay_factor_ray <= RAY, GrowthFactor()

    steps: uint256 = elapsed // step_duration
    decayed_total: uint256 = self.mul_div_up(
        start_total,
        self.ray_pow_up(decay_factor_ray, steps),
        RAY,
    )
    return max(floor_total, decayed_total)


@internal
@pure
def unit_quote_wad(total_price: uint256, initial_amount: uint256) -> uint256:
    """@notice Calculate ceil(total_price * WAD / initial_amount)."""
    return self.mul_div_up(total_price, WAD, initial_amount)


@internal
@pure
def proportional_payment(
    total_price: uint256,
    amount: uint256,
    initial_amount: uint256,
) -> uint256:
    """@notice Calculate ceil(total_price * amount / initial_amount)."""
    return self.mul_div_up(total_price, amount, initial_amount)
