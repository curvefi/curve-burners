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

from contracts.burners.auction import dutch_auction
from contracts.burners.auction.adapters import adapters
from contracts.utils import roles

initializes: roles
initializes: dutch_auction
initializes: adapters[roles := roles]
exports: (
    roles.role_source,
    roles.owner,
    roles.emergency_owner,
)
exports: (
    dutch_auction.current_epoch,
    dutch_auction.want,
    dutch_auction.available,
    dutch_auction.price,
    dutch_auction.getAmountNeeded,
    dutch_auction.quote,
    dutch_auction.check_order,
    dutch_auction.take,
    dutch_auction.take_with_limits,
    dutch_auction.start_total,
    dutch_auction.floor_total,
    dutch_auction.decay_factor_ray,
    dutch_auction.step_duration,
    dutch_auction.proceeds_receiver,
    dutch_auction.lots,
    dutch_auction.reconfigured_epoch,
)
exports: (
    adapters.registry,
    adapters.enabled_adapters,
    adapters.fallback_adapter,
    adapters.executor_refcount,
    adapters.executors,
    adapters.enable_adapter,
    adapters.disable_adapter,
    adapters.set_fallback_adapter,
    adapters.sync_executor_approvals,
    adapters.isValidSignature,
)


WEEK: constant(uint256) = 7 * 24 * 60 * 60

frame_start: public(uint256)
frame_end: public(uint256)
not_sellable: public(HashMap[address, bool])


@deploy
def __init__(
    _want: address,
    _proceeds_receiver: address,
    _registry: address,
    _role_source: address,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    roles.__init__(roles.RoleSource(_role_source))
    dutch_auction.__init__(
        IERC20(_want),
        _proceeds_receiver,
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
    )
    adapters.__init__(_registry)
    frame_start: uint256 = block.timestamp // WEEK * WEEK
    self.frame_start = frame_start
    self.frame_end = frame_start + WEEK


# Hook configuration


@external
def set_frame(_start: uint256, _end: uint256):
    self.frame_start = _start
    self.frame_end = _end


@external
def set_sellable(_token: address, _sellable: bool):
    self.not_sellable[_token] = not _sellable


# Internal-function wrappers


@external
def stage(_token: address) -> uint256:
    return dutch_auction._stage_lot(
        IERC20(_token), self.frame_start // WEEK
    )


@external
@view
def epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    return self._epoch_bounds(_epoch)


# Core hook overrides


@override(dutch_auction)
@view
def _auction_epoch(_timestamp: uint256) -> uint256:
    return self.frame_start // WEEK


@override(dutch_auction)
@view
def _epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    # The harness runs one configurable frame; stale epochs never reach the
    # window check because _auction_epoch already rejects them.
    return self.frame_start, self.frame_end


@override(dutch_auction)
@view
def _sellable(_token: address) -> bool:
    return not self.not_sellable[_token]


@override(dutch_auction)
def _sync_stage_approvals(_token: address):
    adapters._ensure_executor_approvals(IERC20(_token))


# Adapter layer hook overrides


@override(adapters)
@view
def _auction_want() -> address:
    return dutch_auction.want_token.address
