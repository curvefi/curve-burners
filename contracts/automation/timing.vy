# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
# Compiler: vyper@03e096e74b53993e652ed83dddecbee6f889fcc5
"""
@title JobBoard timing module
@author Curve Finance
@license MIT
@notice Stateless week math and the dutch-auction reward curve.
        Anchored to the same distribution start as FeeCollector.
"""

START_TIME: constant(uint256) = 1600300800  # ts of distribution start
WEEK: constant(uint256) = 7 * 24 * 3600


@pure
@internal
def week_number(ts: uint256) -> uint256:
    """
    @notice Sequential week number used as cooldown reset key
    """
    return (ts - START_TIME) // WEEK


@pure
@internal
def reward_per_unit(amount: uint256, start: uint256, end: uint256, dutch: bool, ts: uint256) -> uint256:
    """
    @notice Reward for one unit of work at timestamp `ts`
    @param amount Max reward per unit (dutch ceiling), in target terms
    @param start Payout window start inside the week, [0, WEEK)
    @param end Payout window end inside the week, [0, WEEK); end <= start wraps
    @param dutch If True, reward grows linearly 0 -> amount across the window
    @param ts Timestamp to evaluate at
    @return Reward per unit in target terms, 0 if out of window
    """
    if amount == 0:
        return 0

    t: uint256 = (ts - START_TIME) % WEEK
    if t < start:
        t += WEEK
    e: uint256 = end
    if e <= start:
        e += WEEK
    if e <= t:  # out of window
        return 0

    if dutch:
        return amount * (t - start) // (e - start)
    return amount
