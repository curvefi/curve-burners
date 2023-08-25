# @version 0.3.9
"""
@title Swap Burner
@notice Swaps asset using Curve pool
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view
    def decimals() -> uint256: view

interface Swap:
    def coins(_i: uint256) -> address: view
    def price_oracle(_i: uint256=0) -> uint256: view

interface StableSwap:
    def exchange(i: int128, j: int128, dx: uint256, min_dy: uint256): payable
    def exchange_underlying(i: int128, j: int128, dx: uint256, min_dy: uint256): payable

interface CryptoSwap:
    def exchange(i: uint256, j: uint256, dx: uint256, min_dy: uint256, use_eth: bool=False): payable
    def exchange_underlying(i: uint256, j: uint256, dx: uint256, min_dy: uint256): payable

interface Proxy:
    def burners(_coin: address) -> address: view


enum Implementation:
    # Price
    CONST  # 1
    ORACLE  # 2
    ORACLE_NUM  # 4
    # Exchange
    STABLE  # 8
    STABLE_UNDERLYING  # 16
    CRYPTO  # 32
    CRYPTO_ETH  # 64
    CRYPTO_UNDERLYING  # 128

IMPLEMENTATION_PRICE: immutable(Implementation)
IMPLEMENTATION_CRYPTO: immutable(Implementation)

struct SwapData:
    to: ERC20
    pool: Swap
    i: int128
    j: int128
    dec: uint256  # decimals multiplier
    implementation: Implementation
    slippage: uint256  # 0 will use default

struct SwapDataInput:
    coin: ERC20
    to: ERC20
    pool: Swap
    implementation: Implementation
    slippage: uint256  # 0 will use default


N_COINS_MAX: constant(uint256) = 8
ONE: constant(uint256) = 10 ** 18  # Precision
BPS: constant(uint256) = 100 * 100
SLIPPAGE: constant(uint256) = 100  # 1%

swap_data: public(HashMap[ERC20, SwapData])

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
    @param _owner Owner address. Can kill the contract and set swap_data.
    @param _emergency_owner Emergency owner address. Can kill the contract.
    """
    PROXY = _proxy
    self.owner = _owner
    self.emergency_owner = _emergency_owner

    IMPLEMENTATION_PRICE = Implementation.CONST | Implementation.ORACLE | Implementation.ORACLE_NUM
    IMPLEMENTATION_CRYPTO = Implementation.CRYPTO | Implementation.CRYPTO_ETH | Implementation.CRYPTO_UNDERLYING


@payable
@external
def __default__():
    # required to receive ether during intermediate swaps
    pass


@internal
def _transfer_in(_coin: ERC20, _from: address) -> (uint256, uint256):
    if _coin.address == ETH_ADDRESS:
        return self.balance, self.balance

    if _from != self:
        amount: uint256 = _coin.balanceOf(_from)
        if amount != 0:
            _coin.transferFrom(_from, self, amount)

    return _coin.balanceOf(self), 0


@internal
def _transfer_out(_coin: ERC20):
    if _coin.address == ETH_ADDRESS:
        raw_call(PROXY.address, b"", value=self.balance)
    else:
        assert _coin.transfer(
            PROXY.address, _coin.balanceOf(self), default_return_value=True
        )  # safe transfer


@internal
@pure
def _get_price(_swap_data: SwapData) -> uint256:
    """
    @notice Get price of j-th token in i-th accounting decimal difference
    @param _swap_data Swap to find price for
    @return Price with ONE precision
    """
    price: uint256 = _swap_data.dec
    if _swap_data.implementation in Implementation.CONST:
        return price  # bind to 1, slippage can be increased if needed

    if _swap_data.implementation in Implementation.ORACLE:
        price_oracle: uint256 = _swap_data.pool.price_oracle()
        if _swap_data.i == 0:
            price = price * price_oracle / ONE
        else:
            price = price * ONE / price_oracle
        return price

    if _swap_data.implementation in Implementation.ORACLE_NUM:
        if _swap_data.j > 0:
            price = price * _swap_data.pool.price_oracle(convert(_swap_data.j, uint256) - 1) / ONE
        if _swap_data.i > 0:
            price = price * ONE / _swap_data.pool.price_oracle(convert(_swap_data.i, uint256) - 1)
        return price

    raise "Wrong Price variable"  # Should be unreachable


@internal
def _exchange(_swap_data: SwapData, _amount: uint256, _eth_amount: uint256, _min_dy: uint256):
    """
    @notice Exchange execution implementation
    @param _swap_data Swap metadata
    @param _amount Amount to exchange
    @param _eth_amount Raw ETH to include in exchange
    @param _min_dy Minimum amount to receive
    """
    if _swap_data.implementation in Implementation.STABLE:
        StableSwap(_swap_data.pool.address).exchange(_swap_data.i, _swap_data.j, _amount, _min_dy, value=_eth_amount)
        return

    if _swap_data.implementation in Implementation.STABLE_UNDERLYING:
        StableSwap(_swap_data.pool.address).exchange_underlying(_swap_data.i, _swap_data.j, _amount, _min_dy, value=_eth_amount)
        return

    if _swap_data.implementation in Implementation.CRYPTO:
        CryptoSwap(_swap_data.pool.address).exchange(
            convert(_swap_data.i, uint256), convert(_swap_data.j, uint256), _amount, _min_dy,
            value=_eth_amount,
        )
        return

    if _swap_data.implementation in Implementation.CRYPTO_ETH:
        use_eth: bool = _swap_data.to.address == ETH_ADDRESS or _eth_amount > 0
        CryptoSwap(_swap_data.pool.address).exchange(
            convert(_swap_data.i, uint256), convert(_swap_data.j, uint256), _amount, _min_dy, use_eth,
            value=_eth_amount,
        )
        return

    if _swap_data.implementation in Implementation.CRYPTO_UNDERLYING:
        CryptoSwap(_swap_data.pool.address).exchange_underlying(
            convert(_swap_data.i, uint256), convert(_swap_data.j, uint256), _amount, _min_dy,
            value=_eth_amount,
        )
        return

    raise "Wrong Implementation"  # Should be unreachable


@internal
def _burn(_coin: ERC20, _amount: uint256, _eth_amount: uint256):
    """
    @param _coin Address of the coin being converted
    @param _amount Amount of coin to convert
    @param _eth_amount Amount of ETH to send
    """
    assert not self.is_killed and not self.killed_coin[_coin], "Is killed"

    swap_data: SwapData = self.swap_data[_coin]
    min_dy: uint256 = _amount * self._get_price(swap_data) / ONE

    slippage: uint256 = swap_data.slippage
    if slippage == 0:
        slippage = SLIPPAGE
    min_dy -= min_dy * slippage / BPS

    self._exchange(swap_data, _amount, _eth_amount, min_dy)

    if PROXY.burners(swap_data.to.address) != self:
        self._transfer_out(swap_data.to)


@external
@payable
def burn(_coin: ERC20) -> bool:
    """
    @notice Convert `_coin` by swapping
    @param _coin Address of the coin being converted
    @return bool Success, remained for compatibility
    """
    amount: uint256 = 0
    eth_amount: uint256 = 0
    amount, eth_amount = self._transfer_in(_coin, msg.sender)

    if amount != 0:
        self._burn(_coin, amount, eth_amount)

    return True


@external
def burn_amount(_coin: ERC20, _amount_to_burn: uint256):
    """
    @notice Burn a specific quantity of `_coin`
    @dev Useful when the total amount to burn is so large that it fails from slippage
    @param _coin Address of the coin being converted
    @param _amount_to_burn Amount of the coin to burn
    """
    amount: uint256 = 0
    eth_amount: uint256 = 0
    if PROXY.burners(_coin.address) == self:
        amount, eth_amount = self._transfer_in(_coin, PROXY.address)
    else:
        # Can not do transfer
        amount, eth_amount = self._transfer_in(_coin, self)

    assert amount >= _amount_to_burn, "Insufficient balance"

    self._burn(_coin, _amount_to_burn, min(eth_amount, _amount_to_burn))


@external
@view
def burns_to(_coin: ERC20) -> DynArray[address, 8]:
    """
    @notice Get resulting coins of burning `_coin`
    @param _coin Coin to burn
    """
    return [self.swap_data[_coin].to.address]


@internal
def _remove_swap_data(_coin: ERC20):
    swap_data: SwapData = self.swap_data[_coin]
    # Not all coins allow 0-approval
    # Besides it is a vast minority
    # It is not necessary to remove swap data
    _coin.approve(swap_data.pool.address, 0)
    self.swap_data[_coin] = empty(SwapData)


@internal
@pure
def _check_implementation(_implementation: Implementation):
    # Exactly 1 of price
    subset: uint256 = convert(_implementation & IMPLEMENTATION_PRICE, uint256)
    assert subset & (subset - 1) == 0  # dev: Wrong number of prices set

    # Exactly 1 of exchange
    subset = convert(_implementation & ~IMPLEMENTATION_PRICE, uint256)
    assert subset & (subset - 1) == 0  # dev: Wrong number of implementations set

    assert not (_implementation in IMPLEMENTATION_CRYPTO and _implementation in Implementation.CONST)  # dev: All crypto pools have oracle


@internal
@pure
def _get_indexes(_data_input: SwapDataInput) -> (int128, int128):
    i: int128 = -1
    j: int128 = -1
    for k in range(N_COINS_MAX):
        coin: address = _data_input.pool.coins(k)  # dev: Bad coins
        if coin == _data_input.coin.address:
            i = convert(k, int128)
            if j >= 0:
                break
        elif coin == _data_input.to.address:  # Will explicitly check ".coin != .to"
            j = convert(k, int128)
            if i >= 0:
                break
    assert i >= 0 and j >= 0,  "Wrong coins"
    return i, j


@internal
@pure
def _decimals(_coin: ERC20) -> uint256:
    if _coin.address == ETH_ADDRESS:
        return 10 ** 18
    return 10 ** _coin.decimals()


@internal
def _add_swap_data(_data_input: SwapDataInput):
    assert _data_input.slippage <= BPS
    self._check_implementation(_data_input.implementation)

    i: int128 = empty(int128)
    j: int128 = empty(int128)
    i, j = self._get_indexes(_data_input)

    swap_data: SwapData = SwapData({
        to: _data_input.to,
        pool: _data_input.pool,
        i: i,
        j: j,
        dec: ONE * self._decimals(_data_input.to) / self._decimals(_data_input.coin),
        implementation: _data_input.implementation,
        slippage: _data_input.slippage,
    })
    self.swap_data[_data_input.coin] = swap_data

    if _data_input.implementation not in Implementation.CONST:
        self._get_price(swap_data)  # check price_oracle call

    if _data_input.coin.address != ETH_ADDRESS:
        _data_input.coin.approve(_data_input.pool.address, max_value(uint256))


@external
def set_swap_data(_swap_data: DynArray[SwapDataInput, 16]):
    """
    @notice Set conversion data
    @dev Executable only via owner
    @param _swap_data Conversion data inputs array of max length 16.
    """
    assert msg.sender == self.owner, "Only owner"

    for data_input in _swap_data:
        if data_input.pool.address == empty(address):
            self._remove_swap_data(data_input.coin)
        else:
            self._add_swap_data(data_input)


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
