# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title 1inch Fusion settlement extension library
@author Curve Finance
@license MIT
@notice The byte layout the Fusion settlement adds on top of FeeTaker
        (1inch/limit-order-settlement contracts/SimpleSettlement.sol; the
        Ethereum deployment is Settlement.sol, the same contract with a
        priority-fee cap on fills): the auction details that prefix the
        getter extraData, the post-interaction whitelist with allowed times,
        and the surplus block.
@dev Pure parametric helpers only: no storage, no external calls. Pinned to
     the deployed settlement (0x2Ad5004c60e16E54d5007C80CE329Adde5B51Ef5 on
     Ethereum and Polygon, verified source identical to master up to the
     pragma):
     getter extraData (after the 20-byte settlement address):
       auction details: 3 bytes gas bump estimate, 4 bytes gas price
       estimate, 4 bytes auction start time, 3 bytes auction duration,
       3 bytes initial rate bump (1e7 = 100%), 1 byte point count,
       (3 bytes rate bump, 2 bytes time delta)[N] points; then the FeeTaker
       fee data and getter whitelist (feetaker.vy).
     post-interaction extraData (after the settlement address): the FeeTaker
       header and fee data, then the whitelist as the settlement reads it
       (4 bytes allowed time, 1 byte size, (10 bytes address half, 2 bytes
       time delta)[N]), then the surplus block (32 bytes estimated taking
       amount, 1 byte protocol surplus fee in 1e2), then the FeeTaker tail.
     Economics (SimpleSettlement): the getter applies the rate bump after
     the fee gross-up, taking amount up (rounded up) and making amount down
     (rounded down) by (1e7 + bump) / 1e7, where the bump is the piecewise
     linear auction bump net of the gas bump estimate and never negative,
     so no fill pays less than the FeeTaker amount. The surplus fee takes at
     most the whole excess of the net taking amount over the estimated one
     scaled to the fill (rounded up), and the estimate must be at least the
     order's taking amount (InvalidEstimatedTakingAmount), so the custom
     receiver still gets at least the linear base. After the auction ends
     the bump is zero and the order stands at its linear price until it
     expires.
"""

from contracts.burners.adapters.oneinch import oneinch


# AuctionDetails as SimpleSettlement._getRateBump reads it; the points
# follow the header and are only counted here (the bump curve depends on
# block.basefee and only ever raises the price).
struct AuctionDetails:
    gas_bump_estimate: uint256
    gas_price_estimate: uint256
    start_time: uint256
    duration: uint256
    initial_rate_bump: uint256  # 1e7 = 100%
    points: uint256


# The surplus block of the post-interaction.
struct Surplus:
    estimated_taking_amount: uint256
    protocol_fee: uint256  # 1e2 = 100% of the excess over the estimate


# Layout
# Gas bump and price estimates, start, duration, initial bump, point count.
AUCTION_HEADER_LEN: constant(uint256) = 3 + 4 + 4 + 3 + 3 + 1
POINT_LEN: constant(uint256) = 3 + 2
# Allowed time ahead of the one-byte size.
POST_WHITELIST_PREFIX_LEN: constant(uint256) = 4
# Address half and time delta.
POST_WHITELIST_ENTRY_LEN: constant(uint256) = 10 + 2
SURPLUS_LEN: constant(uint256) = 32 + 1
BASE_POINTS: constant(uint256) = 10_000_000
BASE_1E2: constant(uint256) = 100


@internal
@pure
def _parse_auction_details(
    _data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256
) -> (AuctionDetails, uint256):
    """
    @notice The auction details at `_pos`; returns them and the position
            after the points.
    """
    assert len(_data) >= _pos + AUCTION_HEADER_LEN, (
        oneinch.OrderNotValid(reason="BadAuctionDetails")
    )
    details: AuctionDetails = AuctionDetails(
        gas_bump_estimate=convert(slice(_data, _pos, 3), uint256),
        gas_price_estimate=convert(slice(_data, _pos + 3, 4), uint256),
        start_time=convert(slice(_data, _pos + 7, 4), uint256),
        duration=convert(slice(_data, _pos + 11, 3), uint256),
        initial_rate_bump=convert(slice(_data, _pos + 14, 3), uint256),
        points=convert(slice(_data, _pos + 17, 1), uint256),
    )
    end: uint256 = _pos + AUCTION_HEADER_LEN + details.points * POINT_LEN
    assert len(_data) >= end, oneinch.OrderNotValid(reason="BadAuctionDetails")
    return details, end


@internal
@pure
def _parse_surplus(_data: Bytes[oneinch.MAX_EXTENSION], _pos: uint256) -> (Surplus, uint256):
    """
    @notice The surplus block at `_pos`, with the settlement's fee bound
            (InvalidProtocolSurplusFee) mirrored as BadSurplus; returns it
            and the next position.
    """
    assert len(_data) >= _pos + SURPLUS_LEN, oneinch.OrderNotValid(reason="BadSurplus")
    surplus: Surplus = Surplus(
        estimated_taking_amount=convert(slice(_data, _pos, 32), uint256),
        protocol_fee=convert(slice(_data, _pos + 32, 1), uint256),
    )
    assert surplus.protocol_fee <= BASE_1E2, oneinch.OrderNotValid(reason="BadSurplus")
    return surplus, _pos + SURPLUS_LEN
