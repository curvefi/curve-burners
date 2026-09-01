# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Dutch auction watchtower publishing shim
@author Curve Finance
@license MIT
@notice Minimal ComposableCoW registration: creates conditional orders that
        point at an external IConditionalOrderGenerator handler so the CoW
        watchtower discovers and publishes auction orders automatically.
@dev Discovery only, never authority: settlement validation runs through the
     auction's signature router and its CoW adapter's economic checks, so
     neither the registration nor the handler can weaken what settles. The importing contract stays the
     conditional-order owner because custody and vault-relayer approvals live
     there; order generation and verification logic live in the standalone
     handler contract (contracts/burners/cow/WatchtowerHandler.vy).
"""

from . import gpv2


error BadComposableCow:
    pass


error BadHandler:
    pass


interface ComposableCow:
    def create(_params: gpv2.ConditionalOrderParams, _dispatch: bool): nonpayable


event WatchtowerConfigured:
    composable_cow: indexed(address)
    handler: indexed(address)
    generation: indexed(uint256)


event ConditionalOrderRegistered:
    token: indexed(address)
    generation: indexed(uint256)


# Watchtower wiring. The importing contract deliberately starts unconfigured;
# each reconfiguration bumps the generation so stale registrations are
# re-created for the new handler on the next staging.
composable_cow: public(ComposableCow)
cow_handler: public(address)
cow_generation: public(uint256)
registered_generation: public(HashMap[address, uint256])


@internal
def _configure_watchtower(_composable_cow: address, _handler: address):
    assert _composable_cow != empty(address), BadComposableCow()
    assert _handler != empty(address), BadHandler()

    self.composable_cow = ComposableCow(_composable_cow)
    self.cow_handler = _handler
    self.cow_generation += 1

    log WatchtowerConfigured(
        composable_cow=_composable_cow,
        handler=_handler,
        generation=self.cow_generation,
    )


@internal
def _register_cow_order(_token: address) -> bool:
    """
    @notice Register the token's conditional order for watchtower discovery.
    @dev Called by the importing contract on every staging; creation happens
         once per generation and grants no allowance — router approvals are
         the auction core's responsibility.
    @return True when a new conditional order was created.
    """
    generation: uint256 = self.cow_generation
    if generation == 0 or self.registered_generation[_token] == generation:
        return False
    if not self._cow_rail_enabled():
        return False

    params: gpv2.ConditionalOrderParams = gpv2.ConditionalOrderParams(
        handler=self.cow_handler,
        salt=empty(bytes32),
        staticData=gpv2._encode_static_input(_token, generation),
    )
    extcall self.composable_cow.create(params, True)

    self.registered_generation[_token] = generation
    log ConditionalOrderRegistered(token=_token, generation=generation)
    return True


# Compile-time integration hooks implemented by the importing contract.
@internal
@view
@abstract
def _cow_rail_enabled() -> bool: ...
