# pragma version 0.5.0a4
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Asset recovery module
@author Curve Finance
@license MIT
@notice Returns full ERC-20 or native-coin balances held by the importing
        contract to a destination it supplies; authorization stays with the
        importer.
"""

from ethereum.ercs import IERC20


event Recovered:
    token: indexed(IERC20)
    amount: uint256


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE


@internal
def _recover_coin(_coin: IERC20, _destination: address) -> uint256:
    """
    @notice Send the whole balance of `_coin` to `_destination`.
    @dev ETH_ADDRESS recovers the native-coin balance.
    @return The recovered amount.
    """
    amount: uint256 = 0
    if _coin.address == ETH_ADDRESS:
        amount = self.balance
        if amount != 0:
            raw_call(_destination, b"", value=amount)
    else:
        amount = staticcall _coin.balanceOf(self)
        if amount != 0:
            assert extcall _coin.transfer(_destination, amount, default_return_value=True)
    log Recovered(token=_coin, amount=amount)
    return amount
