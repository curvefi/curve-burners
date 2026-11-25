# pragma version 0.5.0b1
# pragma nonreentrancy on
# SPDX-License-Identifier: MIT
"""
@title Yearn Auction compatibility views
@author Curve Finance
@license MIT
@notice The Yearn Auction selectors the Dutch auction core does not need for
        itself — isActive, auctionLength, auctions — derived from the core's
        lot records. To shed them, remove every mention of this module from
        the importing contract.
@dev Yearn's Auction.sol (yearn/tokenized-strategy-periphery) keys its record
     by token: auctions(token) -> (kicked, scaler, initialAvailable). Here
     kicked is the lot window's start (possibly in the future for a lot
     staged ahead of its window, so Yearn readers must gate on isActive
     rather than subtract kicked from now), scaler is 1 because the core's
     price() is already a raw 1e18-precision quote, and initialAvailable is
     the snapshot. auctionLength is the core's auction_length, a constant
     like Yearn's. Yearn's code is
     AGPL-3.0: only the selectors and their intended semantics were copied.
     Views are locked during a take like the core's own quotes.
"""

from ethereum.ercs import IERC20

from contracts.burners.auction import dutch_auction
from contracts.interfaces import IDutchAuction
from contracts.interfaces import IYearnAuction

uses: dutch_auction


@external
@view
def isActive(_from: address) -> bool:
    """
    @notice Whether `_from` can be taken right now (Yearn ABI).
    @param _from Token offered by the auction.
    @return True while available(_from) is positive.
    """
    return dutch_auction._available(IERC20(_from), block.timestamp) > 0


@external
@view
def auctionLength() -> uint256:
    """
    @notice Length of every auction window (Yearn ABI).
    @return Window length in seconds.
    """
    return dutch_auction.auction_length


@external
@view
def auctions(_from: address) -> IYearnAuction.AuctionInfo:
    """
    @notice The lot in Yearn's record shape (Yearn ABI). Zeroed for a
            never-staged token, like Yearn's unenabled auction.
    @param _from Token offered by the auction.
    @return kicked = window start, scaler = 1, initialAvailable = snapshot.
    """
    record: IDutchAuction.Lot = dutch_auction.lots[IERC20(_from)]
    if record.staged_at == 0:
        return empty(IYearnAuction.AuctionInfo)
    start: uint256 = 0
    end: uint256 = 0
    start, end = dutch_auction._window(IERC20(_from), record.staged_at)
    return IYearnAuction.AuctionInfo(
        kicked=convert(start, uint64),
        scaler=1,
        initialAvailable=convert(record.initial_amount, uint128),
    )
