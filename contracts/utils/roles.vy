# pragma version 0.5.0b1
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Role source module
@author Curve Finance
@license MIT
@notice Live protocol roles read from a pinned role source (the FeeCollector):
        owner and emergency owner views plus the caller checks shared by every
        contract that follows protocol ownership.
@dev The module stores no roles of its own — both are staticcall'ed from the
     role source on every read, so governance always matches the protocol's
     current owner and emergency owner and needs no commit/accept plumbing
     here. The importing contract initializes the module with its role source
     and reuses _check_owner/_check_owner_or_emergency for authorization.
"""


interface RoleSource:
    def owner() -> address: view
    def emergency_owner() -> address: view


error BadRoleSource:
    pass


error OnlyOwner:
    pass


# Both roles are read live from this source (the FeeCollector), so governance
# of the importing contract always matches the protocol's current owners.
role_source: public(immutable(RoleSource))


@deploy
def __init__(_role_source: RoleSource):
    """
    @notice Pin the role source the roles are read from.
    @param _role_source Contract exposing owner() and emergency_owner() views
           (the FeeCollector); must answer owner() with a nonzero address.
    """
    assert staticcall _role_source.owner() != empty(address), BadRoleSource()
    self.role_source = _role_source


@internal
@view
def _owner() -> address:
    return staticcall self.role_source.owner()


@internal
@view
def _emergency_owner() -> address:
    return staticcall self.role_source.emergency_owner()


@internal
@view
def _check_owner():
    assert msg.sender == self._owner(), OnlyOwner()


@internal
@view
def _check_owner_or_emergency():
    assert msg.sender in [self._owner(), self._emergency_owner()], OnlyOwner()


# Both views are reentrant on purpose: they only relay the role source's
# state, which the importer's lock does not guard anyway.
@external
@view
@reentrant
def owner() -> address:
    """@notice Governance owner, read live from the role source."""
    return self._owner()


@external
@view
@reentrant
def emergency_owner() -> address:
    """@notice Emergency role, read live from the role source."""
    return self._emergency_owner()
