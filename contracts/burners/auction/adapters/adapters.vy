# pragma version 0.5.0b1
# pragma nonreentrancy on
# SPDX-License-Identifier: MIT
"""
@title Auction adapter layer
@author Curve Finance
@license MIT
@notice External-settlement plumbing around the auction core: refcounted
        executor approvals, the local adapter set, and the shape-based
        ERC-1271 signature router.
@dev Authorization comes from the shared roles module (roles read live from
     the FeeCollector); the importing contract only wires the protected
     payment token through the _auction_want hook. Routing is shape-based and
     sender-agnostic (off-chain checkers eth_call isValidSignature from the
     zero address): a signature whose first 20 bytes name a locally enabled
     verifier is forwarded to it with the prefix stripped, anything else goes
     verbatim to the configured fallback adapter — the historical CoW
     encodings start with the zero padding of an ABI address head and can
     never collide with a deployed verifier address. Verifiers are called
     with a plain staticcall so their canonical revert semantics bubble up.
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


error FallbackNotEnabled:
    pass


error TooManyExecutors:
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


event FallbackAdapterSet:
    verifier: indexed(address)


ERC1271_MAGIC_VALUE: constant(bytes4) = 0x1626ba7e
INVALID_SIGNATURE: constant(bytes4) = 0xffffffff
# Executors referenced by enabled adapters; a handful of settlement protocols
# at most, and staging approves each one per staged token.
MAX_EXECUTORS: constant(uint256) = 8

# Adapter infrastructure, fixed at deployment (may be unset)
registry: public(immutable(IAdapterRegistry))

# Local adapter set and executor approvals. Executors are refcounted because
# several adapters can share one (every permit2-family protocol pulls through
# Permit2 itself); the enumerable list mirrors refcount > 0 so staging can
# approve each referenced executor.
enabled_adapters: public(HashMap[address, bool])
fallback_adapter: public(address)
executor_refcount: public(HashMap[address, uint256])
executors: public(DynArray[address, MAX_EXECUTORS])


@deploy
def __init__(_registry: address):
    self.registry = IAdapterRegistry(_registry)


# Executor approvals


@internal
def _ensure_executor_approvals(_token: IERC20):
    # The approval surface is the enumerable set of executors referenced by
    # enabled adapters; every entry was reviewed by the owner at enable time.
    for executor: address in self.executors:
        token.max_approve(_token, executor)


@external
def sync_executor_approvals(_executor: address, _tokens: DynArray[IERC20, c.MAX_COINS]):
    """
    @notice Permissionlessly drive token allowances to the executor's target
            state.
    @dev The target is derived, never caller-chosen: infinity while the
         executor is referenced by an enabled adapter, zero once fully
         released. Serves as retired-executor cleanup and as the repair path
         for dropped allowances. Only granting refuses the payment token;
         clearing is always allowed so a token promoted to want by a resync
         can shed its stale settlement allowances.
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


@internal
def _retain_executor(_executor: address):
    if self.executor_refcount[_executor] == 0:
        assert len(self.executors) < MAX_EXECUTORS, TooManyExecutors()
        self.executors.append(_executor)
    self.executor_refcount[_executor] += 1


@internal
def _release_executor(_executor: address):
    self.executor_refcount[_executor] -= 1
    if self.executor_refcount[_executor] == 0:
        for i: uint256 in range(MAX_EXECUTORS):
            if i >= len(self.executors):
                break
            if self.executors[i] == _executor:
                self.executors[i] = self.executors[len(self.executors) - 1]
                self.executors.pop()
                break


# Adapter set


@external
def enable_adapter(_verifier: address):
    """
    @notice Enable a registry adapter for the signature router. Staging starts
            approving the adapter's executor from the next lot on.
    @param _verifier Registry adapter identity: its verifier contract.
    """
    roles._check_owner()
    assert self.registry.address != empty(address), NoRegistry()
    assert not self.enabled_adapters[_verifier], AlreadyEnabled()

    config: adapter_types.AdapterConfig = staticcall self.registry.get_adapter(_verifier)
    assert config.executor != empty(address), UnknownAdapter()
    assert config.active, InactiveAdapter()

    self.enabled_adapters[_verifier] = True
    self._retain_executor(config.executor)
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
    self._release_executor(executor)
    log AdapterDisabled(verifier=_verifier, executor=executor)


@external
def set_fallback_adapter(_verifier: address):
    """
    @notice Route signatures without a verifier prefix (the historical CoW
            encodings) to this enabled adapter; empty disconnects the route.
    @param _verifier Enabled adapter to receive unprefixed signatures.
    """
    roles._check_owner()
    assert _verifier == empty(address) or self.enabled_adapters[_verifier], (
        FallbackNotEnabled()
    )
    self.fallback_adapter = _verifier
    log FallbackAdapterSet(verifier=_verifier)


# ERC-1271 router


@internal
@view
def _is_routable(_verifier: address) -> bool:
    # Both switches are live: the local set and the registry activation flag,
    # so either side kills routing immediately.
    if not self.enabled_adapters[_verifier]:
        return False
    return (staticcall self.registry.get_adapter(_verifier)).active


@external
@view
def isValidSignature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_SIGNATURE_LEN]
) -> bytes4:
    """
    @notice Validate an ERC-1271 signature over a settlement digest.
    @dev Shape routing only: the first 20 bytes naming an enabled verifier
         select the adapter path (prefix stripped), anything else goes to the
         fallback adapter verbatim. Verifier reverts bubble up unchanged, so
         each adapter keeps its protocol's canonical revert semantics. The
         contract-wide nonreentrancy lock rejects validation during a native
         take callback.
    """
    if len(_signature) >= adapter_types.ADAPTER_PREFIX_LEN:
        candidate: address = convert(
            convert(slice(_signature, 0, adapter_types.ADAPTER_PREFIX_LEN), bytes20),
            address,
        )
        if self.enabled_adapters[candidate]:
            if not self._is_routable(candidate):
                return INVALID_SIGNATURE
            payload: Bytes[adapter_types.MAX_SIGNATURE_LEN] = b""
            if len(_signature) > adapter_types.ADAPTER_PREFIX_LEN:
                payload = slice(
                    _signature,
                    adapter_types.ADAPTER_PREFIX_LEN,
                    len(_signature) - adapter_types.ADAPTER_PREFIX_LEN,
                )
            return staticcall Verifier(candidate).isValidSignature(_hash, payload)

    fallback: address = self.fallback_adapter
    if fallback == empty(address) or not self._is_routable(fallback):
        return INVALID_SIGNATURE
    return staticcall Verifier(fallback).isValidSignature(_hash, _signature)


# Compile-time integration hooks implemented by the importing contract.
# The auction's payment token: sync_executor_approvals refuses to touch its
# allowances, mirroring the core's staging guard.
@internal
@view
@abstract
def _auction_want() -> address: ...
