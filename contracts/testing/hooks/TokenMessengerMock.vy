# pragma version 0.5.0b1
# SPDX-License-Identifier: MIT
"""
@title TokenMessengerMock
@author Curve Finance
@license MIT
@notice CCTP v1 TokenMessenger stand-in that is its own TokenMinter: a
        settable per-token burn limit and a depositForBurn that pulls and
        burns (keeps) the tokens while recording the call.
@custom:kill Testing-only contract, never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

burn_limit: public(HashMap[address, uint256])

last_amount: public(uint256)
last_domain: public(uint32)
last_recipient: public(bytes32)
last_token: public(address)
nonce: public(uint64)


@external
def set_burn_limit(_token: address, _limit: uint256):
    self.burn_limit[_token] = _limit


@external
@view
def localMinter() -> address:
    return self


@external
@view
def burnLimitsPerMessage(_token: address) -> uint256:
    return self.burn_limit[_token]


@external
def depositForBurn(
    _amount: uint256, _destination_domain: uint32, _mint_recipient: bytes32, _burn_token: address
) -> uint64:
    assert _amount > 0, "Amount must be nonzero"
    assert _amount <= self.burn_limit[_burn_token], "Burn amount exceeds per tx limit"
    assert extcall IERC20(_burn_token).transferFrom(
        msg.sender, self, _amount, default_return_value=True
    )
    self.last_amount = _amount
    self.last_domain = _destination_domain
    self.last_recipient = _mint_recipient
    self.last_token = _burn_token
    self.nonce += 1
    return self.nonce
