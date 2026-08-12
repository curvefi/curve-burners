# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title AdapterRegistry
@author Curve Finance
@license MIT
@notice Governance-curated catalog of validator adapters for the Dutch auction
        ERC-1271 dispatcher: pinned validator code, verifier/executor pair,
        authorization mode, and an activation flag per adapter_id.
@dev The registry holds no tokens, calls no external protocols, grants no
     approvals, and never executes adapters. authorization_mode is the closed
     adapter_types MODE_* enum resolved to canonical routers by the auction
     itself, so a mutable registry can never introduce an arbitrary approval
     target. An auction-side signature is valid only while config.active AND
     the adapter is enabled on the auction, so either side can switch an
     adapter off instantly. Versions strictly grow: changed semantics always
     get a new version, old code is never replaced in place.
@custom:kill The emergency owner can only disable_adapter, which immediately
             stops signature validation through that adapter on every auction
             reading this registry. Re-activation and new adapter versions
             require the owner. The registry stores no roles of its own: both
             are read live from the role source (the FeeCollector), so they
             move together with the protocol's ownership.
@custom:security Trust boundary is the owner: it pins validator runtime code by
                 codehash at set and re-checks it at activation, so a validator
                 whose code changed (e.g. redeployed via CREATE2) cannot be
                 activated. Adapters cannot self-register: every write path is
                 owner-gated. An unknown adapter_id reads as a zeroed config
                 (validator == empty(address)), which dispatchers must treat
                 as invalid.
"""

from contracts.burners.auction import adapter_types
from contracts.interfaces import IAdapterRegistry

implements: IAdapterRegistry


interface RoleSource:
    def owner() -> address: view
    def emergency_owner() -> address: view


error BadRoleSource:
    pass


error OnlyOwner:
    pass


error ZeroAdapterId:
    pass


error ZeroValidator:
    pass


error CodehashMismatch:
    pass


error EmptyValidator:
    pass


error ZeroVerifier:
    pass


error ZeroExecutor:
    pass


error BadMode:
    pass


error VersionNotGrown:
    pass


error UnknownAdapter:
    pass


error AlreadyActive:
    pass


error NotActive:
    pass


event AdapterSet:
    adapter_id: indexed(bytes4)
    version: uint16
    validator: address

event AdapterActivated:
    adapter_id: indexed(bytes4)
    version: uint16

event AdapterDisabled:
    adapter_id: indexed(bytes4)
    version: uint16

# Codehash of an existing account with empty code; a fresh account reads as
# empty(bytes32). Both mean "no runtime code", so neither may pin a validator.
EMPTY_CODEHASH: constant(bytes32) = keccak256(b"")

# Both roles are read live from this source (the FeeCollector), so registry
# governance always matches the protocol's current owner and emergency owner.
role_source: public(immutable(RoleSource))

adapters: HashMap[bytes4, adapter_types.AdapterConfig]


@deploy
def __init__(_role_source: RoleSource):
    """
    @notice Pin the role source the registry reads its owners from.
    @param _role_source Contract exposing owner() and emergency_owner() views
           (the FeeCollector); must answer owner() with a nonzero address.
    """
    assert staticcall _role_source.owner() != empty(address), BadRoleSource()
    self.role_source = _role_source


@external
@view
def owner() -> address:
    """@notice Governance owner: sets, activates and disables adapters."""
    return staticcall self.role_source.owner()


@external
@view
def emergency_owner() -> address:
    """@notice Emergency role that can only disable adapters."""
    return staticcall self.role_source.emergency_owner()


# Adapter catalog


@external
@view
def get_adapter(_adapter_id: bytes4) -> adapter_types.AdapterConfig:
    """
    @notice Read the adapter config for an adapter_id.
    @param _adapter_id Registry lookup key (not duplicated inside the config).
    @return Stored config; zeroed (validator == empty(address)) for unknown ids.
    """
    return self.adapters[_adapter_id]


@external
def set_adapter(_adapter_id: bytes4, _config: adapter_types.AdapterConfig):
    """
    @notice Register a new adapter version. Always stored inactive: activation
            is a separate owner step so a set with a stale codehash cannot go
            live in the same transaction.
    @param _adapter_id Adapter key; must be non-zero.
    @param _config Full config. _config.active is ignored and stored as False.
           _config.version must be strictly greater than the stored version,
           so an old version can never be reused or downgraded to.
    """
    assert msg.sender == staticcall self.role_source.owner(), OnlyOwner()
    assert _adapter_id != empty(bytes4), ZeroAdapterId()
    assert _config.validator != empty(address), ZeroValidator()
    # Pin the validator to its current runtime code; EOAs and empty accounts
    # have no code and can never act as validators.
    assert _config.validator_codehash == _config.validator.codehash, CodehashMismatch()
    assert _config.validator_codehash != empty(bytes32), EmptyValidator()
    assert _config.validator_codehash != EMPTY_CODEHASH, EmptyValidator()
    assert _config.verifier != empty(address), ZeroVerifier()
    assert _config.executor != empty(address), ZeroExecutor()
    assert _config.authorization_mode <= adapter_types.MODE_PERMIT2_ALLOWANCE_TRANSFER, BadMode()
    assert _config.version > self.adapters[_adapter_id].version, VersionNotGrown()

    config: adapter_types.AdapterConfig = _config
    config.active = False
    self.adapters[_adapter_id] = config
    log AdapterSet(adapter_id=_adapter_id, version=config.version, validator=config.validator)


@external
def activate_adapter(_adapter_id: bytes4):
    """
    @notice Activate a registered adapter after re-checking that the validator
            runtime code still matches the pinned codehash.
    @param _adapter_id Adapter to activate; must exist and be inactive.
    """
    assert msg.sender == staticcall self.role_source.owner(), OnlyOwner()
    config: adapter_types.AdapterConfig = self.adapters[_adapter_id]
    assert config.validator != empty(address), UnknownAdapter()
    assert not config.active, AlreadyActive()
    # Re-pin at activation time: code that changed since set_adapter (e.g. a
    # CREATE2 redeploy) must go through a fresh set with a new version.
    assert config.validator.codehash == config.validator_codehash, CodehashMismatch()

    self.adapters[_adapter_id].active = True
    log AdapterActivated(adapter_id=_adapter_id, version=config.version)


# Emergency


@external
def disable_adapter(_adapter_id: bytes4):
    """
    @notice Disable an active adapter. Faster than activation on purpose: the
            emergency owner can kill an adapter without being able to add one.
    @param _adapter_id Adapter to disable; must be active.
    """
    assert msg.sender in [
        staticcall self.role_source.owner(),
        staticcall self.role_source.emergency_owner(),
    ], OnlyOwner()
    config: adapter_types.AdapterConfig = self.adapters[_adapter_id]
    assert config.active, NotActive()

    self.adapters[_adapter_id].active = False
    log AdapterDisabled(adapter_id=_adapter_id, version=config.version)
