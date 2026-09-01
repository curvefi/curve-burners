# pragma version 0.5.0b1
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title AdapterRegistry
@author Curve Finance
@license MIT
@notice Governance-curated catalog of settlement adapters for Dutch auctions:
        an executor and an activation flag per verifier address.
@dev The verifier address is the adapter's identity and the routing key:
     auctions forward ERC-1271 signatures prefixed with it, and changed
     semantics always mean a new verifier deployment — entries are set once
     and never rewritten, only (de)activated. The executor is the protocol
     contract that pulls sold tokens: auctions approve it while an enabled
     adapter references it, so listing an adapter is a custody-relevant act
     reviewed by the owner. The registry itself holds no tokens, calls no
     external protocols, and never executes adapters.
@custom:kill The emergency owner can only disable_adapter, which immediately
             stops signature routing through that verifier on every auction
             reading this registry. Activation and new entries require the
             owner. The registry stores no roles of its own: both are read
             live from the role source (the FeeCollector).
@custom:security Trust boundary is the owner: every write path is owner-gated,
                 so adapters cannot self-register. A verifier's runtime code
                 cannot change under its address — accounts cannot be
                 redeployed with different code since Cancun, and a verifier
                 behind a proxy must be treated as its proxy admin's code.
                 Activation is a separate owner step so a mistaken set cannot
                 go live in the same transaction. An unknown verifier reads
                 as a zeroed config (executor == empty(address)), which
                 routers must treat as invalid.
"""

from contracts.burners.auction.adapters import adapter_types
from contracts.interfaces import IAdapterRegistry
from contracts.utils import roles

implements: IAdapterRegistry
initializes: roles
exports: (
    roles.role_source,
    roles.owner,
    roles.emergency_owner,
)


error ZeroVerifier:
    pass


error EmptyVerifier:
    pass


error ZeroExecutor:
    pass


error AlreadySet:
    pass


error UnknownAdapter:
    pass


error AlreadyActive:
    pass


error NotActive:
    pass


event AdapterSet:
    verifier: indexed(address)
    executor: indexed(address)

event AdapterActivated:
    verifier: indexed(address)

event AdapterDisabled:
    verifier: indexed(address)

adapters: HashMap[address, adapter_types.AdapterConfig]


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
def get_adapter(_verifier: address) -> adapter_types.AdapterConfig:
    """
    @notice Read the adapter config for a verifier address.
    @param _verifier Registry lookup key: the adapter's verifier contract.
    @return Stored config; zeroed (executor == empty(address)) when unknown.
    """
    return self.adapters[_verifier]


@external
def set_adapter(_verifier: address, _executor: address):
    """
    @notice Register an adapter. Entries are immutable once set — changed
            semantics always mean a new verifier deployment — and stored
            inactive: activation is a separate owner step so a mistaken set
            cannot go live in the same transaction.
    @param _verifier Verifier contract validating this adapter's signatures.
    @param _executor Protocol contract that pulls sold tokens; the approval
           target auctions grant while the adapter is enabled.
    """
    roles._check_owner()
    assert _verifier != empty(address), ZeroVerifier()
    # EOAs and empty accounts can never verify, and set-once entries make a
    # mistyped verifier a permanently burned key.
    assert _verifier.is_contract, EmptyVerifier()
    assert _executor != empty(address), ZeroExecutor()
    assert self.adapters[_verifier].executor == empty(address), AlreadySet()

    self.adapters[_verifier] = adapter_types.AdapterConfig(
        executor=_executor, active=False
    )
    log AdapterSet(verifier=_verifier, executor=_executor)


@external
def activate_adapter(_verifier: address):
    """
    @notice Activate a registered adapter.
    @param _verifier Adapter to activate; must exist and be inactive.
    """
    roles._check_owner()
    config: adapter_types.AdapterConfig = self.adapters[_verifier]
    assert config.executor != empty(address), UnknownAdapter()
    assert not config.active, AlreadyActive()

    self.adapters[_verifier].active = True
    log AdapterActivated(verifier=_verifier)


# Emergency


@external
def disable_adapter(_verifier: address):
    """
    @notice Disable an active adapter. Faster than activation on purpose: the
            emergency owner can kill an adapter without being able to add one.
    @param _verifier Adapter to disable; must be active.
    """
    roles._check_owner_or_emergency()
    assert self.adapters[_verifier].active, NotActive()

    self.adapters[_verifier].active = False
    log AdapterDisabled(verifier=_verifier)
