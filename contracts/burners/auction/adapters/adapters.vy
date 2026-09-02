# pragma version 0.5.0b1
# pragma nonreentrancy on
# SPDX-License-Identifier: MIT
"""
@title Auction adapter layer
@author Curve Finance
@license MIT
@notice External-settlement plumbing around the auction core: the local
        adapter set with refcounted executors, permissionless executor
        allowance sync, and the prefix-based ERC-1271 signature router for
        registry adapters.
@dev Authorization comes from the shared roles module (roles read live from
     the FeeCollector); the importing contract only wires the protected
     payment token through the _auction_want hook. Routing is prefix-based
     (adapter_types: `verifier ++ payload`) and sender-agnostic — off-chain
     checkers eth_call isValidSignature from the zero address: the verifier
     named by the first 20 bytes must be locally enabled and active in the
     registry, and receives the payload with a plain staticcall so its
     canonical revert semantics bubble up. Anything else is invalid.
     The registry is a trusted immutable deployment dependency and stays
     authoritative: entries are set-once (an adapter's executor can never
     change), and routing rechecks config.active live so a registry disable
     kills settlement everywhere immediately. Economic authority never leaves
     the auction: verifiers prove protocol digests and call the auction's
     check_order view, which prices every fill against the live curve.
"""


from ethereum.ercs import IERC20

from contracts.burners.auction import dutch_auction
from contracts.burners.auction.adapters import adapter_types
from contracts.interfaces import IAdapterRegistry
from contracts.utils import constants as c, roles, token

uses: roles


error BadExecutor:
    pass


error NoRegistry:
    pass


error AlreadyEnabled:
    pass


error NotEnabled:
    pass


error UnknownAdapter:
    pass


error InactiveAdapter:
    pass


interface Verifier:
    def isValidSignature(
        _hash: bytes32, _signature: Bytes[adapter_types.MAX_SIGNATURE_LEN]
    ) -> bytes4: view


event AdapterEnabled:
    verifier: indexed(address)
    executor: address


event AdapterDisabled:
    verifier: indexed(address)
    executor: address


INVALID_SIGNATURE: constant(bytes4) = 0xffffffff

# Adapter infrastructure, fixed at deployment (may be unset)
registry: public(immutable(IAdapterRegistry))

# Local adapter set and executor references. Executors are refcounted because
# several adapters can share one (every permit2-family protocol pulls through
# Permit2 itself); refcount > 0 is the allowance target derived by
# sync_executor_approvals.
enabled_adapters: public(HashMap[address, bool])
executor_refcount: public(HashMap[address, uint256])


@deploy
def __init__(_registry: address):
    self.registry = IAdapterRegistry(_registry)


# Executor approvals


@external
def sync_executor_approvals(_executor: address, _tokens: DynArray[IERC20, c.MAX_COINS]):
    """
    @notice Permissionlessly drive token allowances to the executor's target
            state.
    @dev The target is derived, never caller-chosen: infinity while the
         executor is referenced by an enabled adapter, zero once fully
         released. Keepers call it after staging so freshly staged tokens
         become pullable; it doubles as retired-executor cleanup.
         Allowances are never granted implicitly (staging does not touch
         them): this is the only path. Grants refuse the payment token and only
         ever go from zero to infinity; clears are always allowed so a token
         promoted to want by a resync sheds its stale allowance.
    @param _executor Executor whose allowances are synchronized.
    @param _tokens Tokens to synchronize.
    """
    assert _executor != empty(address), BadExecutor()
    grant: bool = self.executor_refcount[_executor] > 0
    for coin: IERC20 in _tokens:
        if grant:
            assert coin.address != self._auction_want(), dutch_auction.WantNotSellable()
            token.max_approve(coin, _executor)
        else:
            token.clear_approve(coin, _executor)


# Adapter set


@external
def enable_adapter(_verifier: address):
    """
    @notice Enable a registry adapter for the signature router and reference
            its executor; allowances follow through sync_executor_approvals.
    @param _verifier Registry adapter identity: its verifier contract.
    """
    roles._check_owner()
    assert self.registry.address != empty(address), NoRegistry()
    assert not self.enabled_adapters[_verifier], AlreadyEnabled()

    config: adapter_types.AdapterConfig = staticcall self.registry.get_adapter(_verifier)
    assert config.executor != empty(address), UnknownAdapter()
    assert config.active, InactiveAdapter()

    self.enabled_adapters[_verifier] = True
    self.executor_refcount[config.executor] += 1
    log AdapterEnabled(verifier=_verifier, executor=config.executor)


@external
def disable_adapter(_verifier: address):
    """
    @notice Disable an adapter; allowed for the owner or the emergency owner.
    @dev Does not clear the executor's allowances: batch with
         sync_executor_approvals once its refcount reaches zero.
    @param _verifier Registry adapter identity: its verifier contract.
    """
    roles._check_owner_or_emergency()
    assert self.enabled_adapters[_verifier], NotEnabled()

    # Registry entries are set-once, so the executor read here is exactly the
    # one retained at enable time.
    executor: address = (staticcall self.registry.get_adapter(_verifier)).executor
    self.enabled_adapters[_verifier] = False
    self.executor_refcount[executor] -= 1
    log AdapterDisabled(verifier=_verifier, executor=executor)


# ERC-1271 router


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_SIGNATURE_LEN]
) -> bytes4:
    """
    @notice Validate an ERC-1271 signature over a settlement digest.
    @dev Signature template: `verifier ++ payload` (adapter_types). The
         verifier must be enabled locally and active in the registry — both
         switches are live, so either side kills routing immediately — and
         gets the payload with the prefix stripped; its reverts bubble up
         unchanged. Anything else is invalid. The contract-wide nonreentrancy
         lock rejects validation during a native take callback.
    """
    if len(_signature) < adapter_types.ADAPTER_PREFIX_LEN:
        return INVALID_SIGNATURE
    verifier: address = convert(
        convert(slice(_signature, 0, adapter_types.ADAPTER_PREFIX_LEN), bytes20), address
    )
    if not self.enabled_adapters[verifier]:
        return INVALID_SIGNATURE
    if not (staticcall self.registry.get_adapter(verifier)).active:
        return INVALID_SIGNATURE

    payload: Bytes[adapter_types.MAX_SIGNATURE_LEN] = b""
    if len(_signature) > adapter_types.ADAPTER_PREFIX_LEN:
        payload = slice(
            _signature,
            adapter_types.ADAPTER_PREFIX_LEN,
            len(_signature) - adapter_types.ADAPTER_PREFIX_LEN,
        )
    return staticcall Verifier(verifier).isValidSignature(_hash, payload)


# Compile-time integration hooks implemented by the importing contract.
# The auction's payment token: sync_executor_approvals refuses to touch its
# allowances, mirroring the core's staging guard.
@internal
@view
@abstract
def _auction_want() -> address: ...
