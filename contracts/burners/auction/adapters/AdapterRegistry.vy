# pragma version 0.5.0b1
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title AdapterRegistry
@author Curve Finance
@license MIT
@notice Governance-curated catalog of settlement adapters for Dutch auctions:
        an executor and an activation flag per adapter address, the list of
        registered adapters, and whether an executor is in use.
@dev An adapter is a settlement rail: any contract that sells auction
     inventory by its own rules. Its executor is the protocol contract that
     pulls sold tokens (often the adapter itself): auctions approve it while
     is_executor_active(executor) (an active adapter references it), so
     listing an adapter is a custody-relevant act reviewed by the owner. The
     registry is the single management point: auctions reading it hold no
     adapter state of their own. It holds no tokens, calls no external
     protocols, and never executes adapters.
@custom:kill The emergency owner can only disable_adapter, which immediately
             releases the executor reference and, for adapters reached
             through the ERC-1271 router, stops routing on every auction
             reading this registry. Activation and new entries require the
             owner. The registry stores no roles of its own: both are read
             live from the role source (the FeeCollector).
@custom:security Trust boundary is the owner: listing and activation are
                 owner-gated (the emergency owner can only disable), so
                 adapters cannot self-register. An adapter's runtime code
                 cannot change under its address — accounts cannot be
                 redeployed with different code since Cancun, and an adapter
                 behind a proxy must be treated as its proxy admin's code.
                 Activation is a separate owner step so a mistaken set cannot
                 go live in the same transaction. An unknown adapter reads
                 as a zeroed config (executor == empty(address)), which
                 routers must treat as invalid.
"""

from contracts.interfaces import IAdapterRegistry
from contracts.utils import constants as c, roles

implements: IAdapterRegistry
initializes: roles
exports: (
    roles.role_source,
    roles.owner,
    roles.emergency_owner,
)


error BadAdapter:
    pass


error UnknownAdapter:
    pass


error AlreadyActive:
    pass


error NotActive:
    pass


event AdapterSet:
    adapter: indexed(address)
    executor: indexed(address)

event AdapterActivated:
    adapter: indexed(address)

event AdapterDisabled:
    adapter: indexed(address)

configs: HashMap[address, IAdapterRegistry.AdapterConfig]
# Registered adapters (executor set), in no particular order.
adapters: DynArray[address, c.MAX_ADAPTERS]
# Active adapters per executor. Several adapters can share one executor (every
# permit2-family protocol pulls through Permit2 itself); auctions approve an
# executor while its count is positive.
executor_refcount: HashMap[address, uint256]


@deploy
def __init__(_role_source: roles.RoleSource):
    """
    @notice Pin the role source the registry reads its owners from.
    @param _role_source Contract exposing owner() and emergency_owner() views
           (the FeeCollector); must answer owner() with a nonzero address.
    """
    roles.__init__(_role_source)


# Adapter catalog


@external
@view
def get_adapter(_adapter: address) -> IAdapterRegistry.AdapterConfig:
    """
    @notice Read an adapter's config.
    @param _adapter Adapter contract.
    @return Stored config; zeroed (executor == empty(address)) when unknown.
    """
    return self.configs[_adapter]


@external
@view
def get_adapters() -> DynArray[address, c.MAX_ADAPTERS]:
    """@notice List every registered adapter, active or not."""
    return self.adapters


@external
@view
def is_executor_active(_executor: address) -> bool:
    """
    @notice Whether an active adapter references the executor; auctions keep
            their allowances toward it while this holds.
    """
    return self.executor_refcount[_executor] > 0


@internal
def _remove_listing(_adapter: address):
    # Swap-remove: order is not part of the listing's contract.
    last: uint256 = len(self.adapters) - 1
    for i: uint256 in range(c.MAX_ADAPTERS):
        if i > last:
            break
        if self.adapters[i] == _adapter:
            self.adapters[i] = self.adapters[last]
            self.adapters.pop()
            break


@external
def set_adapter(_adapter: address, _executor: address):
    """
    @notice Register an adapter, repoint an inactive one at a new executor,
            or remove it (executor == empty(address) reads as unknown and
            drops it from get_adapters). Entries are stored inactive:
            activation is a separate owner step so a mistaken set cannot go
            live in the same transaction, and an active adapter must be
            disabled first so executor references stay consistent.
    @param _adapter Adapter contract.
    @param _executor Protocol contract that pulls sold tokens; the approval
           target auctions grant while the adapter is active. Zero removes
           the entry.
    """
    roles._check_owner()
    assert _adapter != empty(address), BadAdapter()
    assert not self.configs[_adapter].active, AlreadyActive()

    if _executor == empty(address):
        self._remove_listing(_adapter)
    elif self.configs[_adapter].executor == empty(address):
        self.adapters.append(_adapter)  # dev: too many adapters
    self.configs[_adapter] = IAdapterRegistry.AdapterConfig(
        executor=_executor, active=False
    )
    log AdapterSet(adapter=_adapter, executor=_executor)


@external
def activate_adapter(_adapter: address):
    """
    @notice Activate a registered adapter and reference its executor;
            auction allowances follow through their sync_executor_approvals.
    @param _adapter Adapter to activate; must exist and be inactive.
    """
    roles._check_owner()
    config: IAdapterRegistry.AdapterConfig = self.configs[_adapter]
    assert config.executor != empty(address), UnknownAdapter()
    assert not config.active, AlreadyActive()

    self.configs[_adapter].active = True
    self.executor_refcount[config.executor] += 1
    log AdapterActivated(adapter=_adapter)


# Emergency


@external
def disable_adapter(_adapter: address):
    """
    @notice Disable an active adapter and release its executor reference.
            Faster than activation on purpose: the emergency owner can kill an
            adapter without being able to add one.
    @dev Does not touch auction allowances: batch with each auction's
         sync_executor_approvals once the executor is no longer active.
    @param _adapter Adapter to disable; must be active.
    """
    roles._check_owner_or_emergency()
    config: IAdapterRegistry.AdapterConfig = self.configs[_adapter]
    assert config.active, NotActive()

    self.configs[_adapter].active = False
    self.executor_refcount[config.executor] -= 1
    log AdapterDisabled(adapter=_adapter)
