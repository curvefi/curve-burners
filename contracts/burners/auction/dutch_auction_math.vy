# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Dutch auction math
@author Curve Finance
@license MIT
@notice Step-geometric auction pricing helpers.
@dev Rounding follows the Maker/Yearn rpow convention: RAY multiplications
     round to nearest, the decayed total rounds down, and only the payment
     quotes round up in favor of the receiver — the residual error is dwarfed
     by execution noise. Products are computed in checked uint256 arithmetic:
     quotes revert if a * b overflows; auction totals, amounts, and RAY
     factors stay far below that domain by deployment policy.
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
def ray_mul(a: uint256, b: uint256) -> uint256:
    """@notice Calculate a * b / RAY rounded to nearest (Maker rpow convention)."""
    return (a * b + RAY // 2) // RAY


@internal
@pure
def ray_pow(base_ray: uint256, exponent: uint256) -> uint256:
    """
    @notice Calculate a RAY fixed-point power by square-and-multiply.
    @dev Each multiplication rounds to nearest, matching the Maker/Yearn rpow
         convention. The loop has one iteration per exponent bit and is
         therefore bounded by the uint256 width. Results which do not fit
         uint256 revert.
    """
    result: uint256 = RAY
    factor: uint256 = base_ray
    remaining_exponent: uint256 = exponent

    for _i: uint256 in range(256):
        if remaining_exponent == 0:
            return result
        if remaining_exponent & 1 != 0:
            result = self.ray_mul(result, factor)
        remaining_exponent >>= 1
        if remaining_exponent != 0:
            factor = self.ray_mul(factor, factor)

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
    @dev Returns max(floor_total, start_total * decay**steps rounded down).
    """
    assert step_duration != 0, ZeroStep()
    assert floor_total <= start_total, FloorAboveStart()
    assert decay_factor_ray <= RAY, GrowthFactor()

    steps: uint256 = elapsed // step_duration
    decayed_total: uint256 = (
        start_total * self.ray_pow(decay_factor_ray, steps) // RAY
    )
    return max(floor_total, decayed_total)


@internal
@pure
def proportional_payment(
    total_price: uint256,
    amount: uint256,
    initial_amount: uint256,
) -> uint256:
    """@notice Calculate ceil(total_price * amount / initial_amount)."""
    return self.mul_div_up(total_price, amount, initial_amount)
