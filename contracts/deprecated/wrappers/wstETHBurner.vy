# @version 0.3.7
"""
@title Wrapped stETH Burner
@notice Withdraws stETH from Wrapped stETH
"""

from vyper.interfaces import ERC20


interface wstETH:
    def balanceOf(_owner: address) -> uint256: view
    def transferFrom(_sender: address, _receiver: address, _amount: uint256): nonpayable
    def unwrap(_wstETHAmount: uint256): nonpayable
    def stETH() -> address: view


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
WSTETH: immutable(wstETH)
STETH: immutable(ERC20)
RECEIVER: immutable(address)


@external
def __init__(_wsteth: wstETH, _receiver: address):
    """
    @notice Contract constructor
    @param _wsteth Address of wrapped stETH
    @param _receiver Address of receiver of stETH
    """
    WSTETH = _wsteth
    STETH = ERC20(_wsteth.stETH())
    RECEIVER = _receiver


@view
@external
def receiver() -> address:
    return RECEIVER


@external
def burn(_coin: address) -> bool:
    """
    @notice Unwrap stETH
    @param _coin Remained for compatability
    @return bool success
    """
    amount: uint256 = WSTETH.balanceOf(msg.sender)
    WSTETH.transferFrom(msg.sender, self, amount)
    amount = WSTETH.balanceOf(self)
    WSTETH.unwrap(amount)

    amount = STETH.balanceOf(self)
    STETH.transfer(RECEIVER, amount)
    return True


@external
def recover_balance(_coin: ERC20, _amount: uint256=max_value(uint256)):
    """
    @notice Recover ERC20 tokens or Ether from this contract
    @dev Tokens are sent to proxy
    @param _coin Token address
    @param _amount Amount to recover
    """
    amount: uint256 = _amount
    if _coin.address == ETH_ADDRESS:
        if amount == max_value(uint256):
            amount = self.balance
        raw_call(RECEIVER, b"", value=amount)
    else:
        if amount == max_value(uint256):
            amount = _coin.balanceOf(self)
        _coin.transfer(RECEIVER, amount)  # do not need safe transfer
