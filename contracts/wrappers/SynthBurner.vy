# @version 0.3.9
"""
@title SynthToken Burner
@notice Swaps synths
"""

interface ERC20:
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view

interface synthERC20:
    def balanceOf(_owner: address) -> uint256: view
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def withdraw(_amount: uint256, _to: address): nonpayable
    def currencyKey() -> bytes32: nonpayable

interface Proxy:
    def burners(_coin: address) -> address: view

interface Synthetix:
    def exchangeWithTracking(
        sourceCurrencyKey: bytes32,
        sourceAmount: uint256,
        destinationCurrencyKey: bytes32,
        rewardAddress: address,
        trackingCode: bytes32,
    ) -> uint256: nonpayable
    def settle(currencyKey: bytes32) -> uint256[3]: nonpayable


struct SwapData:
    currency_key: bytes32
    to: synthERC20  # Same for coins to withdraw

struct SwapDataInput:
    coin: synthERC20
    to: synthERC20


SNX: immutable(Synthetix)
TRACKING_CODE: constant(bytes32) = 0x4355525645000000000000000000000000000000000000000000000000000000

swap_data: public(HashMap[synthERC20, SwapData])
sink_coin: public(synthERC20)  # Default coin to swap to

ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
PROXY: public(immutable(Proxy))

is_killed: public(bool)
killed_coin: public(HashMap[synthERC20, bool])

owner: public(address)
emergency_owner: public(address)
future_owner: public(address)
future_emergency_owner: public(address)


@external
def __init__(_proxy: Proxy, _owner: address, _emergency_owner: address):
    """
    @notice Contract constructor
    @param _proxy Owner of admin fees
    @param _owner Owner address. Can kill the contract.
    @param _emergency_owner Emergency owner address. Can kill the contract.
    """
    PROXY = _proxy
    self.owner = _owner
    self.emergency_owner = _emergency_owner

    SNX = Synthetix(0xC011a73ee8576Fb46F5E1c5751cA3B9Fe0af2a6F)
    self.sink_coin = synthERC20(0x10A5F7D9D65bCc2734763444D4940a31b109275f)


@internal
def _fetch_swap_data(_coin: synthERC20) -> SwapData:
    swap_data: SwapData = self.swap_data[_coin]
    if swap_data.currency_key == empty(bytes32):
        swap_data = SwapData({
            currency_key: _coin.currencyKey(),
            to: self.sink_coin,
        })
        self.swap_data[_coin] = swap_data
    return swap_data


@internal
def _burn(_coin: synthERC20, _amount: uint256):
    """
    @notice Exchanges via Synthetix according to swap_data and sends resulting coins to Proxy
    """
    assert not self.is_killed and not self.killed_coin[_coin], "Is killed"

    swap_data: SwapData = self._fetch_swap_data(_coin)
    if swap_data.to == _coin:
        SNX.settle(swap_data.currency_key)
        _coin.transfer(PROXY.address, _amount)
    else:
        SNX.exchangeWithTracking(
            swap_data.currency_key,
            _amount,
            self._fetch_swap_data(swap_data.to).currency_key,
            PROXY.address,
            TRACKING_CODE,
        )
        amount: uint256 = swap_data.to.balanceOf(self)
        swap_data.to.transfer(PROXY.address, amount)


@external
def burn(_coin: synthERC20) -> bool:
    """
    @notice Unwrap `_coin`
    @param _coin Address of the coin being unwrapped
    @return bool Success, remained for compatibility
    """
    amount: uint256 = _coin.balanceOf(msg.sender)
    if amount != 0:
        _coin.transferFrom(msg.sender, self, amount)

    amount = _coin.balanceOf(self)

    if amount != 0:
        self._burn(_coin, amount)

    return True


@external
def burn_amount(_coin: synthERC20, _amount_to_burn: uint256):
    """
    @notice Burn a specific quantity of `_coin`
    @dev Useful when the total amount to burn is so large that it fails
    @param _coin Address of the coin being converted
    @param _amount_to_burn Amount of the coin to burn
    """
    amount: uint256 = _coin.balanceOf(PROXY.address)
    if amount != 0 and PROXY.burners(_coin.address) == self:
        _coin.transferFrom(PROXY.address, self, amount)

    amount = _coin.balanceOf(self)
    assert amount >= _amount_to_burn, "Insufficient balance"

    self._burn(_coin, _amount_to_burn)


@external
@view
def burns_to(_coin: synthERC20) -> DynArray[address, 8]:
    """
    @notice Get resulting coins of burning `_coin`
    @param _coin Coin to burn
    """
    return [self.swap_data[_coin].to.address]


@external
def set_swap_data(_swap_data: DynArray[SwapDataInput, 16]):
    """
    @notice Set custom swap data, needed for old pools
    @param _swap_data Data needed for burning
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    for data_input in _swap_data:
        swap_data: SwapData = SwapData({
            currency_key: data_input.coin.currencyKey(),
            to: data_input.to,
        })
        self.swap_data[data_input.coin] = swap_data


@external
def set_new_sink_coin(_coin: synthERC20):
    """
    @notice Set default coin to swap to, will be used for synths with no swap_data set
    @param _coin Address of the coin
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    self.sink_coin = _coin


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
def set_killed(_is_killed: bool, _coin: synthERC20=empty(synthERC20)):
    """
    @notice Stop a contract or specific coin to be burnt
    @dev Executable only via owner or emergency owner
    @param _is_killed Boolean value to set
    @param _coin Coin to stop from burning, ZERO_ADDRESS to kill all coins (by default)
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    if _coin == empty(synthERC20):
        self.is_killed = _is_killed
    else:
        self.killed_coin[_coin] = _is_killed


@external
def commit_transfer_ownership(_future_owner: address) -> bool:
    """
    @notice Commit a transfer of ownership
    @dev Must be accepted by the new owner via `accept_transfer_ownership`
    @param _future_owner New owner address
    @return bool success
    """
    assert msg.sender == self.owner, "Only owner"
    self.future_owner = _future_owner

    return True


@external
def accept_transfer_ownership() -> bool:
    """
    @notice Accept a transfer of ownership
    @return bool success
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
    @return bool success
    """
    assert msg.sender == self.emergency_owner, "Only owner"
    self.future_emergency_owner = _future_owner

    return True


@external
def accept_transfer_emergency_ownership() -> bool:
    """
    @notice Accept a transfer of emergency ownership
    @return bool success
    """
    assert msg.sender == self.future_emergency_owner, "Only owner"
    self.emergency_owner = msg.sender

    return True
