# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title FeeCollector interface module
@author Curve Finance
@license MIT
@notice The FeeCollector ABI surface shared by burners: interface, epoch flag,
        transfer struct, and the calendar/scale constants.
@dev A .vy module rather than a .vyi interface because Vyper does not expose
     .vyi constants to importers; this module carries no state or code.
"""


flag Epoch:
    SLEEP
    COLLECT
    EXCHANGE
    FORWARD


struct Transfer:
    coin: address
    to: address
    amount: uint256


interface FeeCollector:
    def fee(_epoch: Epoch = ..., _timestamp: uint256 = ...) -> uint256: view
    def target() -> address: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch_time_frame(_epoch: Epoch, _timestamp: uint256 = ...) -> (uint256, uint256): view
    def can_exchange(_coins: DynArray[address, MAX_COINS]) -> bool: view
    def transfer(_transfers: DynArray[Transfer, MAX_COINS]): nonpayable


# Mirrors FeeCollector.MAX_LEN: the transfer/exchange batch bound.
MAX_COINS: constant(uint256) = 64
# fee() is WAD-scaled: 100% = 10**18.
WAD: constant(uint256) = 10**18
# The FeeCollector calendar is weekly-periodic.
WEEK: constant(uint256) = 7 * 24 * 60 * 60
