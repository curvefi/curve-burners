# pragma version 0.5.0b1
# pragma nonreentrancy on
# SPDX-License-Identifier: MIT
"""
@title Auction adapter layer
@author Curve Finance
@license MIT
@notice External settlement through registry adapters: the prefix-based
        ERC-1271 signature router and the permissionless executor allowance
        sync. Adapter management lives in the AdapterRegistry; this module
        only reads it.
@dev Signature template: `adapter_address (20 bytes) ++ payload`. The router
     strips the adapter address, requires the adapter to be active in the
     registry, and forwards the payload to the adapter's
     isValidSignature(hash, payload) with a plain staticcall so its revert
     semantics bubble up; anything else is invalid. Routing is
     sender-agnostic (off-chain checkers eth_call from the zero address) and
     rechecks the registry live, so a registry disable stops settlement at
     once.
"""


from ethereum.ercs import IERC20

from contracts.interfaces import IAdapterRegistry
from contracts.utils import constants as c, token


interface Adapter:
    def isValidSignature(_hash: bytes32, _signature: Bytes[INF]) -> bytes4: view


error BadRegistry:
    pass


INVALID_SIGNATURE: constant(bytes4) = 0xffffffff
# The adapter address opening every adapter signature.
ADAPTER_PREFIX_LEN: constant(uint256) = 20

# Adapter catalog, fixed at deployment.
registry: public(immutable(IAdapterRegistry))


@deploy
def __init__(_registry: address):
    """
    @notice Pin the adapter catalog.
    @param _registry AdapterRegistry every routing and allowance decision is
           read from; an empty catalog means native settlement only.
    """
    assert _registry != empty(address), BadRegistry()
    self.registry = IAdapterRegistry(_registry)


# Executor approvals


@external
def sync_executor_approvals(_executor: address, _tokens: DynArray[IERC20, c.MAX_COINS]):
    """
    @notice Permissionlessly drive token allowances to the executor's target
            state.
    @dev The registry is the only input: every token is approved while the
         executor is active and cleared once it is released. Grants only
         ever go from zero to infinity. The payment token is not special
         here: an executor pulls only under a signed order, and no order
         sells the payment token.
    @param _executor Executor whose allowances are synchronized.
    @param _tokens Tokens to synchronize.
    """
    grant: bool = staticcall self.registry.is_executor_active(_executor)
    for coin: IERC20 in _tokens:
        if grant:
            token.max_approve(coin, _executor)
        else:
            token.clear_approve(coin, _executor)


# ERC-1271 router


@external
@view
def isValidSignature(_hash: bytes32, _signature: Bytes[INF]) -> bytes4:
    """
    @notice Validate an ERC-1271 signature over a settlement digest.
    @dev `adapter ++ payload`: an active registry adapter gets the payload
         with the prefix stripped and its reverts bubble up unchanged.
    @param _hash Settlement digest being signed.
    @param _signature Adapter address (20 bytes) followed by its payload.
    @return ERC-1271 magic value for a valid signature, 0xffffffff otherwise.
    """
    if len(_signature) < ADAPTER_PREFIX_LEN:
        return INVALID_SIGNATURE
    adapter: address = convert(
        convert(slice(_signature, 0, ADAPTER_PREFIX_LEN), bytes20), address
    )
    if not (staticcall self.registry.get_adapter(adapter)).active:
        return INVALID_SIGNATURE

    payload: Bytes[INF] = b""
    if len(_signature) > ADAPTER_PREFIX_LEN:
        payload = slice(_signature, ADAPTER_PREFIX_LEN, len(_signature) - ADAPTER_PREFIX_LEN)
    return staticcall Adapter(adapter).isValidSignature(_hash, payload)

