# @version 0.3.9
"""
@title Wrapped ETH Burner
@notice Withdraws or deposits ETH into Wrapped ETH
"""


interface ERC20:
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view

interface wETH:
    def balanceOf(_owner: address) -> uint256: view
    def transferFrom(_sender: address, _receiver: address, _amount: uint256): nonpayable
    def transfer(_receiver: address, _amount: uint256): nonpayable
    def withdraw(_amount: uint256): nonpayable
    def deposit(): payable


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
WETH: immutable(wETH)
PROXY: immutable(address)


@external
def __init__(_weth: address, _proxy: address):
    """
    @notice Contract constructor
    @param _weth Address of wrapped ETH
    @param _proxy Address of owner of admin fees
    """
    WETH = wETH(_weth)
    PROXY = _proxy


@payable
@external
def __default__():
    pass


@external
def burn(_coin: address) -> bool:
    """
    @notice Wrap/unwrap ETH
    @param _coin Address of the coin
    @return bool success
    """
    if _coin == ETH_ADDRESS:  # Deposit
        WETH.deposit(value=self.balance)
        amount: uint256 = WETH.balanceOf(self)
        WETH.transfer(PROXY, amount)

    elif _coin == WETH.address:  # Withdraw
        amount: uint256 = WETH.balanceOf(msg.sender)
        WETH.transferFrom(msg.sender, self, amount)
        amount = WETH.balanceOf(self)
        WETH.withdraw(amount)
        raw_call(PROXY, b"", value=self.balance)

    else:
        # Unknown coin
        return False
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
        raw_call(PROXY, b"", value=amount)
    else:
        if amount == max_value(uint256):
            amount = _coin.balanceOf(self)
        _coin.transfer(PROXY, amount)  # do not need safe transfer
