# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title Auction types
@author Curve Finance
@license MIT
@notice Shared Dutch auction records.
@dev Stateless module: lets contracts that only read an auction (interfaces,
     the watchtower handler, the intent resolver) name the lot record without
     importing the stateful core.
"""


# Lot staged for one epoch. Time bounds are not part of the record: the
# importing contract's calendar publishes them via epoch_bounds, and the
# curve is read live, so initial_amount only pins the unit price and caps
# what is available.
struct Lot:
    epoch: uint256
    initial_amount: uint256
