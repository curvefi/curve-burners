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
     once. Economic authority never leaves the auction: adapters prove
     protocol digests and price fills through the core's check_order view.
     The contract-wide nonreentrancy lock rejects validation during a native
     take callback. The module knows nothing about the auction core: the
     importing contract vetoes allowance grants through the _pre_approve
     hook (the auction refuses its payment token there).
"""


from ethereum.ercs import IERC20

from contracts.interfaces import IAdapterRegistry
from contracts.utils import constants as c, token


error BadExecutor:
    pass


interface Adapter:
    def isValidSignature(_hash: bytes32, _signature: Bytes[INF]) -> bytes4: view


INVALID_SIGNATURE: constant(bytes4) = 0xffffffff
# The adapter address opening every adapter signature.
ADAPTER_PREFIX_LEN: constant(uint256) = 20

# Adapter catalog, fixed at deployment; unset means native settlement only.
registry: public(immutable(IAdapterRegistry))


@deploy
def __init__(_registry: address):
    self.registry = IAdapterRegistry(_registry)


@internal
@view
def _has_registry() -> bool:
    return self.registry.address != empty(address)


# Executor approvals


@external
def sync_executor_approvals(_executor: address, _tokens: DynArray[IERC20, c.MAX_COINS]):
    """
    @notice Permissionlessly drive token allowances to the executor's target
            state.
    @dev The target is derived, never caller-chosen: infinity while an active
         registry adapter references the executor, zero once none does.
         Keepers call it after staging so freshly staged tokens become
         pullable; it doubles as retired-executor cleanup. Allowances are
         never granted implicitly (staging does not touch them): this is the
         only path. Grants pass the importer's _pre_approve veto and only
         ever go from zero to infinity; clears are always allowed so a token
         the importer no longer approves sheds its stale allowance.
    @param _executor Executor whose allowances are synchronized.
    @param _tokens Tokens to synchronize.
    """
    assert _executor != empty(address), BadExecutor()
    grant: bool = self._has_registry() and (
        staticcall self.registry.is_executor_active(_executor)
    )
    for coin: IERC20 in _tokens:
        if grant:
            self._pre_approve(coin)
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
    """
    if not self._has_registry() or len(_signature) < ADAPTER_PREFIX_LEN:
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


# Compile-time integration hook implemented by the importing contract: runs
# before every allowance grant and reverts for tokens that must never be
# approved (the auction's payment token).
@internal
@view
@abstract
def _pre_approve(_coin: IERC20): ...
