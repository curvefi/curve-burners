# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title OKXTokenApproveMock
@author Curve Finance
@license MIT
@notice Minimal stand-in for OKX DEX's TokenApproveProxy and TokenApprove
        pair, collapsed into one contract: tokenApprove() answers the
        approval target (itself by default) and claimTokens pulls with a
        plain transferFrom only for allowed callers, the way
        TokenApproveProxy.allowedApprove gates TokenApprove.claimTokens for
        the settlement. The real pair are two contracts; the adapter only
        follows the two getters.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20


error AccessRestricted:
    pass


# The approval target reported to adapters: this contract unless overridden.
token_approve: immutable(address)
# TokenApproveProxy.allowedApprove: the contracts that may pull.
allowed_approve: public(HashMap[address, bool])


@deploy
def __init__(_token_approve_override: address):
    target: address = _token_approve_override
    if target == empty(address):
        target = self
    self.token_approve = target


@external
def set_allowed(_caller: address, _allowed: bool):
    self.allowed_approve[_caller] = _allowed


@external
@view
def tokenApprove() -> address:
    return self.token_approve


@external
def claimTokens(_token: address, _who: address, _dest: address, _amount: uint256):
    assert self.allowed_approve[msg.sender], AccessRestricted()
    if _amount > 0:
        assert extcall IERC20(_token).transferFrom(_who, _dest, _amount, default_return_value=True)
