# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title CowExecutionHarness
@author Curve Finance
@license MIT
@notice Test harness exposing the cow_execution module with controllable hooks.
@custom:kill Testing-only contract, never deployed to production.
"""

from contracts.burners.auction import adapter_types
from contracts.burners.cow import execution as cow_execution

initializes: cow_execution
exports: cow_execution.__interface__


struct LotContext:
    active: bool
    available: uint256
    initial_amount: uint256
    start: uint256
    end: uint256


cow_target: public(address)
cow_receiver: public(address)
signature_allowed: public(bool)
quote_amount: public(uint256)
contexts: public(HashMap[address, LotContext])


@deploy
def __init__(_app_data: bytes32, _cow_order_validity: uint256):
    cow_execution.__init__(_app_data, _cow_order_validity)
    self.signature_allowed = True


# Hook configuration


@external
def set_cow_target(_target: address):
    self.cow_target = _target


@external
def set_cow_receiver(_receiver: address):
    self.cow_receiver = _receiver


@external
def set_signature_allowed(_allowed: bool):
    self.signature_allowed = _allowed


@external
def set_quote(_quote: uint256):
    self.quote_amount = _quote


@external
def set_context(
    _token: address,
    _active: bool,
    _available: uint256,
    _initial_amount: uint256,
    _start: uint256,
    _end: uint256,
):
    self.contexts[_token] = LotContext(
        active=_active,
        available=_available,
        initial_amount=_initial_amount,
        start=_start,
        end=_end,
    )


# Internal-function wrappers


@external
def configure_cow(_settlement: address):
    cow_execution._configure_cow(_settlement)


@external
def enable_cow():
    cow_execution._enable_cow()


@external
def disable_cow():
    cow_execution._disable_cow()


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    return cow_execution._validate_cow_signature(_hash, _signature)


# Hook overrides


@override(cow_execution)
@view
def _cow_target() -> address:
    return self.cow_target


@override(cow_execution)
@view
def _cow_receiver() -> address:
    return self.cow_receiver


@override(cow_execution)
@view
def _cow_order_context(_token: address) -> (bool, uint256, uint256, uint256, uint256):
    context: LotContext = self.contexts[_token]
    return context.active, context.available, context.initial_amount, context.start, context.end


@override(cow_execution)
@view
def _cow_quote(_token: address, _sell_amount: uint256, _timestamp: uint256) -> uint256:
    return self.quote_amount


@override(cow_execution)
@view
def _cow_signature_allowed() -> bool:
    return self.signature_allowed
