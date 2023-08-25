# @version 0.3.9
"""
@title Burner  # ALTER
@notice Converts using converter  # ALTER
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view
    def decimals() -> uint256: view

# ALTER: other interfaces

interface Proxy:
    def burners(_coin: address) -> address: view


# ALTER
# --------------------------------------
struct SwapData:
    to: ERC20

struct SwapDataInput:
    coin: ERC20
    to: ERC20


ONE: constant(uint256) = 10 ** 18  # Precision
BPS: constant(uint256) = 100 * 100
SLIPPAGE: constant(uint256) = 100  # 1%

swap_data: public(HashMap[ERC20, SwapData])
# --------------------------------------

ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
PROXY: public(immutable(Proxy))

is_killed: public(bool)
killed_coin: public(HashMap[ERC20, bool])

owner: public(address)
emergency_owner: public(address)
future_owner: public(address)
future_emergency_owner: public(address)


@external
def __init__(_proxy: Proxy, _owner: address, _emergency_owner: address):
    """
    @notice Contract constructor
    @param _proxy Owner of admin fees
    @param _owner Owner address. Can kill the contract and set swap_data.  # ALTER: update roles
    @param _emergency_owner Emergency owner address. Can kill the contract.  # ALTER: update roles
    """
    PROXY = _proxy
    self.owner = _owner
    self.emergency_owner = _emergency_owner

    # ALTER: initial variables


@internal
def _burn(_coin: ERC20, _amount: uint256):
    """
    @param _coin Address of the coin being converted
    @param _amount Amount of coin to convert
    """
    assert not self.is_killed and not self.killed_coin[_coin], "Is killed"

    swap_data: SwapData = self.swap_data[_coin]
    # ALTER: make the swap

    if PROXY.burners(swap_data.to.address) != self:
        assert swap_data.to.transfer(
            PROXY.address, _coin.balanceOf(self), default_return_value=True
        )  # safe


@external
def burn(_coin: ERC20) -> bool:
    """
    @notice Convert `_coin`
    @param _coin Address of the coin being converted
    @return bool Success, remained for compatibility
    """
    amount: uint256 = _coin.balanceOf(msg.sender)
    if amount != 0:
        assert _coin.transferFrom(msg.sender, self, amount, default_return_value=True)  # safe

    amount = _coin.balanceOf(self)
    if amount != 0:
        self._burn(_coin, amount)

    return True


@external
def burn_amount(_coin: ERC20, _amount_to_burn: uint256):
    """
    @notice Burn a specific quantity of `_coin`
    @dev Useful when the total amount to burn is so large that it fails from slippage
    @param _coin Address of the coin being converted
    @param _amount_to_burn Amount of the coin to burn
    """
    amount: uint256 = _coin.balanceOf(PROXY.address)
    if amount != 0 and PROXY.burners(_coin.address) == self:
        assert _coin.transferFrom(PROXY.address, self, amount, default_return_value=True)  # safe

    amount = _coin.balanceOf(self)
    assert amount >= _amount_to_burn, "Insufficient balance"

    self._burn(_coin, _amount_to_burn)


@external
@view  # ALTER: might be pure sometimes
def burns_to(_coin: ERC20) -> DynArray[address, 8]:
    """
    @notice Get resulting coins of burning `_coin`
    @param _coin Coin to burn
    """
    return [self.swap_data[_coin].to.address]


@external
def set_swap_data(_swap_data: DynArray[SwapDataInput, 16]):
    """
    @notice Set conversion data
    @dev Executable only via owner
    @param _swap_data Conversion data inputs array of max length 16.
    """
    assert msg.sender == self.owner, "Only owner"

    for data_input in _swap_data:
        # ALTER
        self.swap_data[data_input.coin] = SwapData({to: data_input.to})


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
        raw_call(PROXY.address, b"", value=amount)
    else:
        if amount == max_value(uint256):
            amount = _coin.balanceOf(self)
        _coin.transfer(PROXY.address, amount)  # do not need safe transfer


@external
def set_killed(_is_killed: bool, _coin: ERC20=empty(ERC20)):
    """
    @notice Stop a contract or specific coin to be burnt
    @dev Executable only via owner or emergency owner
    @param _is_killed Boolean value to set
    @param _coin Coin to stop from burning, ZERO_ADDRESS to kill all coins (by default)
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    if _coin == empty(ERC20):
        self.is_killed = _is_killed
    else:
        self.killed_coin[_coin] = _is_killed


@external
def commit_transfer_ownership(_future_owner: address) -> bool:
    """
    @notice Commit a transfer of ownership
    @dev Must be accepted by the new owner via `accept_transfer_ownership`
    @param _future_owner New owner address
    @return bool Success
    """
    assert msg.sender == self.owner, "Only owner"
    self.future_owner = _future_owner

    return True


@external
def accept_transfer_ownership() -> bool:
    """
    @notice Accept a transfer of ownership
    @return bool Success
    """
    assert msg.sender == self.future_owner, "Only owner"
    self.owner = msg.sender

    return True


@external
def commit_transfer_emergency_ownership(_future_owner: address) -> bool:
    """
    @notice Commit a transfer of emergency ownership
    @dev Must be accepted by the new owner via `accept_transfer_emergency_ownership`
    @param _future_owner New owner address
    @return bool Success
    """
    assert msg.sender == self.emergency_owner, "Only owner"
    self.future_emergency_owner = _future_owner

    return True


@external
def accept_transfer_emergency_ownership() -> bool:
    """
    @notice Accept a transfer of emergency ownership
    @return bool Success
    """
    assert msg.sender == self.future_emergency_owner, "Only owner"
    self.emergency_owner = msg.sender

    return True
