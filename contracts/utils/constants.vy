# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Shared protocol constants
@author Curve Finance
@license MIT
@notice Protocol-wide scales and bounds shared across the burner stack.
@dev Stateless constants-only module; import as `constants as c`.
"""

# Mirrors FeeCollector.MAX_LEN: the transfer/exchange batch bound.
MAX_COINS: constant(uint256) = 64
# Fixed-point percentage scale: 100% = 10**18.
WAD: constant(uint256) = 10**18
