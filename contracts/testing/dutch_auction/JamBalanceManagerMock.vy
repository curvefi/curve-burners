# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title JamBalanceManagerMock
@author Curve Finance
@license MIT
@notice Minimal JamBalanceManager stand-in: the contract takers approve,
        which pulls their tokens with transferFrom only when its operator
        (the settlement mock) asks. The Permit2 and Blend paths are not
        modelled.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20

from contracts.burners.adapters.bebop import jam


error InvalidCaller:
    pass


operator: public(immutable(address))


@deploy
def __init__(_operator: address):
    self.operator = _operator


@external
def transferTokens(
    _tokens: DynArray[address, jam.MAX_TOKENS],
    _amounts: DynArray[uint256, jam.MAX_TOKENS],
    _sender: address,
    _receiver: address,
):
    assert msg.sender == self.operator, InvalidCaller()
    for i: uint256 in range(len(_tokens), bound=jam.MAX_TOKENS):
        assert extcall IERC20(_tokens[i]).transferFrom(
            _sender, _receiver, _amounts[i], default_return_value=True
        )
