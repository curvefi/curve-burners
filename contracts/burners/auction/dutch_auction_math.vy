# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Dutch auction math
@author Curve Finance
@license MIT
@notice Exponential (geometric) auction curve and payment rounding.
@dev The total price of the full lot decays from start_total to floor_total
     along P(u) = start_total^(1-u) * floor_total^u, u being the fraction of
     the decay steps elapsed: equal time buys the same percentage drop. The
     curve is evaluated as exp(ln(start) - u * ln(start / floor)) in WAD
     fixed point through snekmate's wad_ln/wad_exp, with the logarithms
     prepared once per configuration (curve_logs) so quotes cost one exp.
     The endpoints are exact by explicit branches and the result is clamped
     into [floor_total, start_total], so the approximation error of ln/exp
     never leaves the price range. Two roundings, both in favor of the
     receiver: the step offset log_drop * step // decay_steps truncates,
     which raises the price, and the payment quotes round up. Products are
     computed in checked uint256 arithmetic: quotes revert if a * b
     overflows; auction totals and amounts stay far below that domain by
     deployment policy.
"""

from snekmate.utils import math as wad_math


error DivisionByZero:
    pass


error ZeroStep:
    pass


error FloorAboveStart:
    pass


error StartTotalTooLarge:
    pass


# wad_ln(0) answers 0 instead of reverting; the curve needs a real floor.
error ZeroFloor:
    pass


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
def curve_logs(start_total: uint256, floor_total: uint256) -> (int256, uint256):
    """
    @notice Prepare the curve: ln(start_total) and the total log drop
            ln(start_total) - ln(floor_total), both WAD.
    @dev Reverts for floor_total > start_total, floor_total == 0 (ln
         undefined) and start_total above int256. The drop is kept whole:
         dividing it by the step count ahead of time would lose precision,
         so total_price multiplies first and divides last.
    """
    assert start_total <= convert(max_value(int256), uint256), StartTotalTooLarge()
    assert floor_total <= start_total, FloorAboveStart()
    assert floor_total != 0, ZeroFloor()
    log_start: int256 = wad_math._wad_ln(convert(start_total, int256))
    log_floor: int256 = wad_math._wad_ln(convert(floor_total, int256))
    return log_start, convert(log_start - log_floor, uint256)


@internal
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
    """
    @notice Quote the full lot at a discrete elapsed-time step.
    @dev Constant within a step. Exactly start_total during the first step
         and exactly floor_total from step decay_steps on; in between,
         exp(log_start - log_drop * step / decay_steps) clamped into the
         price range. Non-strict decrease between steps: two adjacent steps
         may quote the same integer.
    """
    assert step_duration != 0, ZeroStep()
    step: uint256 = elapsed // step_duration

    if step == 0:
        return start_total
    if step >= decay_steps:
        return floor_total
    if start_total == floor_total:
        return start_total

    log_offset: uint256 = log_drop * step // decay_steps
    price: uint256 = convert(
        wad_math._wad_exp(log_start - convert(log_offset, int256)), uint256
    )
    return min(start_total, max(floor_total, price))


@internal
@pure
def proportional_payment(
    total_price: uint256,
    amount: uint256,
    initial_amount: uint256,
) -> uint256:
    """@notice Calculate ceil(total_price * amount / initial_amount)."""
    return self.mul_div_up(total_price, amount, initial_amount)
