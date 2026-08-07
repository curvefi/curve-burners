# pragma version 0.5.0a4
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

from contracts.auction import dutch_auction
from contracts.auction import adapters
from contracts.auction import adapter_types

initializes: dutch_auction
initializes: adapters
exports: dutch_auction.__interface__
exports: adapters.__interface__


WEEK: constant(uint256) = 7 * 24 * 60 * 60

owner: public(address)
emergency_owner: public(address)
cow_router: public(address)
frame_start: public(uint256)
frame_end: public(uint256)
not_sellable: public(HashMap[address, bool])
embedded_response: public(bytes4)


@deploy
def __init__(
    _want: address,
    _proceeds_receiver: address,
    _registry: address,
    _permit2: address,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    dutch_auction.__init__(
        dutch_auction.ERC20(_want),
        _proceeds_receiver,
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
    )
    adapters.__init__(_registry, _permit2)
    self.owner = msg.sender
    self.emergency_owner = msg.sender
    self.embedded_response = 0xffffffff
    frame_start: uint256 = block.timestamp // WEEK * WEEK
    self.frame_start = frame_start
    self.frame_end = frame_start + WEEK


# Hook configuration


@external
def set_owner(_owner: address):
    self.owner = _owner


@external
def set_emergency_owner(_emergency_owner: address):
    self.emergency_owner = _emergency_owner


@external
def set_cow_router(_cow_router: address):
    self.cow_router = _cow_router


@external
def set_frame(_start: uint256, _end: uint256):
    self.frame_start = _start
    self.frame_end = _end


@external
def set_sellable(_token: address, _sellable: bool):
    self.not_sellable[_token] = not _sellable


@external
def set_embedded_response(_response: bytes4):
    self.embedded_response = _response


# Internal-function wrappers


@external
def stage(_token: address) -> uint256:
    return dutch_auction._stage_lot(
        dutch_auction.ERC20(_token), self.frame_start // WEEK
    )


@external
def cancel(_token: address, _epoch: uint256):
    dutch_auction._cancel_lot(dutch_auction.ERC20(_token), _epoch)


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
    adapters._ensure_router_approvals(adapters.ERC20(_token))


# Adapter layer hook overrides


@override(adapters)
@view
def _owner() -> address:
    return self.owner


@override(adapters)
@view
def _emergency_owner() -> address:
    return self.emergency_owner


@override(adapters)
@view
def _cow_router() -> address:
    return self.cow_router


@override(adapters)
@view
def _auction_want() -> address:
    return dutch_auction.want.address


@override(adapters)
@view
def _validate_embedded_signature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    return self.embedded_response


@override(adapters)
@view
def _check_order_against_lot(
    _order: adapter_types.NormalizedOrder,
    _adapter_id: bytes4,
    _adapter_version: uint16,
) -> bool:
    return dutch_auction._check_signed_order(_order, _adapter_id, _adapter_version)
