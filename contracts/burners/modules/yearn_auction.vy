# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Yearn Auction ABI Module
@author Curve Finance
@license MIT
@notice Exposes the Yearn-compatible auction surface through compile-time hooks.
@dev The public ABI is independently implemented from the published Yearn selectors.
"""


MAX_CALLBACK_DATA: constant(uint256) = 8192


@abstract
@view
def _want() -> address: ...


@abstract
@view
def _available(_from: address) -> uint256: ...


@abstract
@view
def _price(_from: address) -> uint256: ...


@abstract
@view
def _get_amount_needed(_from: address, _amount_to_take: uint256) -> uint256: ...


@abstract
def _take(
    _from: address,
    _max_amount: uint256,
    _taker_receiver: address,
    _data: Bytes[MAX_CALLBACK_DATA],
) -> uint256: ...


@external
@view
def want() -> address:
    """@notice Return the target token accepted as payment."""
    return self._want()


@external
@view
def available(_from: address) -> uint256:
    """
    @notice Return the amount of `_from` currently available to take.
    @param _from Token offered by the auction.
    """
    return self._available(_from)


@external
@view
def price(_from: address) -> uint256:
    """
    @notice Return the current WAD-scaled unit price for `_from`.
    @param _from Token offered by the auction.
    """
    return self._price(_from)


@external
@view
def getAmountNeeded(_from: address, amountToTake: uint256) -> uint256:
    """
    @notice Return the exact target-token payment required for an amount.
    @param _from Token offered by the auction.
    @param amountToTake Amount of `_from` to quote.
    """
    return self._get_amount_needed(_from, amountToTake)


@external
def take(
    _from: address,
    maxAmount: uint256,
    takerReceiver: address,
    data: Bytes[MAX_CALLBACK_DATA],
) -> uint256:
    """
    @notice Take up to `maxAmount` of an auctioned token.
    @param _from Token offered by the auction.
    @param maxAmount Maximum amount of `_from` to take.
    @param takerReceiver Receiver of the auctioned token.
    @param data Optional data forwarded to the receiver's auction callback.
    @return Amount of `_from` taken.
    """
    return self._take(_from, maxAmount, takerReceiver, data)
