# pragma version 0.5.0b1
# The core's unbounded callback type (Bytes[INF]) requires the Venom backend.
# pragma experimental-codegen
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CoreHarness
@author Curve Finance
@license MIT
@notice Test harness exposing the dutch_auction core and the adapters layer
        with configurable hooks.
@custom:kill Testing-only contract, never deployed to production.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters import adapters
from contracts.burners.auction import dutch_auction
from contracts.burners.auction import yearn_auction
from contracts.utils import roles

initializes: roles
initializes: dutch_auction
initializes: yearn_auction[dutch_auction := dutch_auction]
initializes: adapters
# The harness exports role_source/emergency_owner for tests; the burner
# exports only owner.
exports: (
    roles.role_source,
    roles.owner,
    roles.emergency_owner,
)
exports: (
    dutch_auction.auction_length,
    dutch_auction.want,
    dutch_auction.receiver,
    dutch_auction.start_total,
    dutch_auction.floor_total,
    dutch_auction.step_duration,
    dutch_auction.lots,
    dutch_auction.window,
    dutch_auction.available,
    dutch_auction.price,
    dutch_auction.getAmountNeeded,
    dutch_auction.take,
    dutch_auction.take_with_limits,
    dutch_auction.check_order,
)
exports: (
    yearn_auction.isActive,
    yearn_auction.auctionLength,
    yearn_auction.auctions,
)
exports: (
    adapters.registry,
    adapters.sync_executor_approvals,
    adapters.isValidSignature,
)

WEEK: constant(uint256) = 7 * 24 * 60 * 60

# Window start of the reference week; other weeks shift it by whole weeks.
frame_start: public(uint256)
not_sellable: public(HashMap[address, bool])


@deploy
def __init__(
    _want: address,
    _receiver: address,
    _registry: address,
    _role_source: address,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
    _auction_length: uint256,
):
    roles.__init__(roles.RoleSource(_role_source))
    dutch_auction.__init__(
        IERC20(_want),
        _receiver,
        _start_total,
        _floor_total,
        _step_duration,
        _auction_length,
    )
    adapters.__init__(_registry)
    self.frame_start = block.timestamp


# Hook configuration


@external
def set_frame(_start: uint256):
    self.frame_start = _start


@external
@view
def frame_end() -> uint256:
    return self.frame_start + dutch_auction.auction_length


@external
def set_sellable(_token: address, _sellable: bool):
    self.not_sellable[_token] = not _sellable


# Internal-function wrappers


@external
def stage(_token: address) -> uint256:
    return dutch_auction._stage_lot(IERC20(_token))


@external
def resync(
    _want: address,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
):
    dutch_auction._set_economics(IERC20(_want), _start_total, _floor_total, _step_duration)


@external
def set_receiver(_receiver: address):
    dutch_auction._set_receiver(_receiver)


# Core hook overrides


@override(dutch_auction)
@view
def _lot_start(_token: IERC20, _staged_at: uint256) -> uint256:
    # A weekly calendar built from the configurable frame start: the window
    # of a timestamp is the frame shifted into that timestamp's week, so a
    # lot staged last week keeps last week's window.
    week: uint256 = _staged_at // WEEK * WEEK
    frame_week: uint256 = self.frame_start // WEEK * WEEK
    return self.frame_start + week - frame_week


@override(dutch_auction)
@view
def _sellable(_token: address) -> bool:
    return not self.not_sellable[_token]
