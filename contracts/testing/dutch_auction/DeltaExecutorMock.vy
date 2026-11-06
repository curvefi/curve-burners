# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DeltaExecutorMock
@author Curve Finance
@license MIT
@notice The agent-side executor of a Delta settlement: receives the sold
        tokens from the settlement and, when called back with execute,
        pays the dest token amount encoded in the data to the caller (the
        settlement), from its own balance. Works against the mock and the
        real Delta contract alike.
@custom:kill Test-only contract; never deployed to production.
@custom:security Deliberately trusting test double.
"""

from ethereum.ercs import IERC20


@external
def execute(_data: Bytes[1024]):
    """
    @notice Pay `amount` of `token` to the caller.
    @param _data abi.encode(token, amount).
    """
    token: address = empty(address)
    amount: uint256 = 0
    token, amount = abi_decode(_data, (address, uint256))
    if amount != 0:
        assert extcall IERC20(token).transfer(msg.sender, amount, default_return_value=True)
