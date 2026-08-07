# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
#
# The 512-bit mulDiv sequence is adapted from contracts/libraries/FullMath.sol
# at Uniswap v3-core commit e3589b192d0be27e100cd0daaf6c97204fdb1899:
# https://github.com/Uniswap/v3-core/commit/e3589b192d0be27e100cd0daaf6c97204fdb1899
# Copyright (c) 2021 Remco Bloemen; distributed under the MIT License.
# FullMath in turn credits Remco Bloemen's MIT-licensed mulDiv derivation:
# https://xn--2-umb.com/21/muldiv
"""
@title Dutch auction math
@author Curve Finance
@license MIT
@notice Full-precision, upward-rounded helpers for step-geometric auction pricing.
@dev No Snekmate or Yearn implementation code was used.
"""


RAY: constant(uint256) = 10**27
WAD: constant(uint256) = 10**18
UINT256_MAX: constant(uint256) = max_value(uint256)

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
    @notice Calculate ceil(a * b / denominator) without overflowing the product.
    @dev Adapted from the credited Uniswap FullMath implementation. The low
         product word and MULMOD modulo 2**256 - 1 uniquely recover the
         high word. After subtracting the remainder from the 512-bit product,
         division by an odd denominator is multiplication by its inverse modulo
         2**256. Eight Newton steps derive all 256 inverse bits from the fact that
         every odd integer is its own inverse modulo 2.
    """
    assert denominator != 0, "Division by zero"

    if a == 0 or b == 0:
        return 0

    product_low: uint256 = unsafe_mul(a, b)
    product_mod_max: uint256 = uint256_mulmod(a, b, UINT256_MAX)
    product_high: uint256 = unsafe_sub(
        unsafe_sub(product_mod_max, product_low),
        convert(product_mod_max < product_low, uint256),
    )
    remainder: uint256 = uint256_mulmod(a, b, denominator)

    if product_high == 0:
        quotient: uint256 = product_low // denominator
        if remainder != 0:
            assert quotient != UINT256_MAX, "mulDiv overflow"
            quotient += 1
        return quotient

    # The high word must be smaller than the denominator for the floor quotient
    # to fit in uint256. The final increment separately checks ceil overflow.
    assert denominator > product_high, "mulDiv overflow"

    # Make the 512-bit numerator exactly divisible by denominator.
    if remainder > product_low:
        product_high = unsafe_sub(product_high, 1)
    product_low = unsafe_sub(product_low, remainder)

    # Divide powers of two conventionally, then move the high-word bits into
    # the low word. `two_complement_scale` represents 2**256 / power_of_two;
    # zero is the correct wrapped representation when power_of_two is one.
    power_of_two: uint256 = denominator & unsafe_sub(0, denominator)
    odd_denominator: uint256 = denominator // power_of_two
    product_low = product_low // power_of_two
    two_complement_scale: uint256 = unsafe_add(
        unsafe_div(unsafe_sub(0, power_of_two), power_of_two),
        1,
    )
    product_low = product_low | unsafe_mul(product_high, two_complement_scale)

    inverse: uint256 = 1
    for _i: uint256 in range(8):
        inverse = unsafe_mul(
            inverse,
            unsafe_sub(2, unsafe_mul(odd_denominator, inverse)),
        )

    quotient: uint256 = unsafe_mul(product_low, inverse)
    if remainder != 0:
        assert quotient != UINT256_MAX, "mulDiv overflow"
        quotient += 1
    return quotient


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
    assert step_duration != 0, "Zero step"
    assert floor_total <= start_total, "Floor above start"
    assert decay_factor_ray <= RAY, "Growth factor"

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
