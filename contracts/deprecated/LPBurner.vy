# @version 0.3.9
"""
@title LPBurner
@notice Burns LP into one of underlying coins according to priorities
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view
    def decimals() -> uint256: view

interface CurveToken:
    def minter() -> address: view
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view

interface Swap:
    def coins(_i: uint256) -> address: view
    def get_virtual_price() -> uint256: view
    def price_oracle(_i: uint256=0) -> uint256: view
    def lp_price() -> uint256: view
    def remove_liquidity_one_coin(token_amount: uint256, i: uint256, min_amount: uint256): nonpayable

interface Swap128:
    def remove_liquidity_one_coin(token_amount: uint256, i: int128, min_amount: uint256): nonpayable

interface Proxy:
    def burners(_coin: address) -> address: view


enum Implementation:
    # Price
    CONST  # 1, 1:1
    CONST_METAPOOL  # 2, virtual_price, has to be set manually
    ORACLE  # 4, .price_oracle()
    ORACLE_NUM  # 8, .price_oracle(_num)
    # .lp_price() method, used as bool
    LP_PRICE  # 8
    # int128 for indexes, used as bool
    I128  # 16

IMPLEMENTATION_PRICE: immutable(Implementation)

struct SwapData:
    pool: Swap
    coins: DynArray[ERC20, N_COINS_MAX]
    implementation: Implementation
    base_pool: Swap
    slippage: uint256

struct SwapDataInput:
    coin: CurveToken
    pool: Swap
    implementation: Implementation
    base_pool: Swap  # Needed for metapools, empty(address) will use token as pool
    slippage: uint256  # 0 will use default

struct PriorityInput:
    coin: ERC20
    priority: uint256


N_COINS_MAX: constant(uint256) = 5
ONE: constant(uint256) = 10 ** 18  # Precision
BPS: constant(uint256) = 100 * 100
SLIPPAGE: constant(uint256) = 100  # 1%

swap_data: public(HashMap[CurveToken, SwapData])
priority_of: public(HashMap[ERC20, uint256])  # Coin with the largest priority will be chosen

ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
PROXY: public(immutable(Proxy))

is_killed: public(bool)
killed_coin: public(HashMap[CurveToken, bool])

owner: public(address)
emergency_owner: public(address)
future_owner: public(address)
future_emergency_owner: public(address)


@external
def __init__(_proxy: Proxy, _owner: address, _emergency_owner: address):
    """
    @notice Contract constructor
    @param _proxy Owner of admin fees
    @param _owner Owner address. Can kill the contract, set swap_data and priorities.
    @param _emergency_owner Emergency owner address. Can kill the contract.
    """
    PROXY = _proxy
    self.owner = _owner
    self.emergency_owner = _emergency_owner

    IMPLEMENTATION_PRICE = convert(convert(Implementation.LP_PRICE, uint256) - 1, Implementation)


@internal
@view
def _get_if_exists(_address: address, _calldata: Bytes[64]) -> Bytes[32]:
    """
    @notice Make a call to see if method is implemented. Needed to fetch implementation specifications.
    """
    success: bool = False
    response: Bytes[32] = b""
    success, response = raw_call(
        _address,
        _calldata,
        max_outsize=32,
        is_static_call=True,
        revert_on_failure=False,
    )
    if success:
        return response
    return b""  # UB for response, hence empty bytes


@internal
@view
def _get_coins(_pool: Swap) -> DynArray[ERC20, N_COINS_MAX]:
    coins: DynArray[ERC20, N_COINS_MAX] = empty(DynArray[ERC20, N_COINS_MAX])
    for i in range(N_COINS_MAX):
        response: Bytes[32] = self._get_if_exists(_pool.address, _abi_encode(i, method_id=method_id("coins(uint256)")))
        if response == empty(Bytes[32]):
            break
        coins.append(ERC20(convert(response, address)))
    return coins


@internal
def _fetch_swap_data(_coin: CurveToken) -> SwapData:
    swap_data: SwapData = self.swap_data[_coin]
    if swap_data.pool != empty(Swap):
        return swap_data

    swap_data.pool = Swap(_coin.address)
    # Check if token contract is separate
    response: Bytes[32] = self._get_if_exists(_coin.address, method_id("minter()"))
    if response != empty(Bytes[32]):
        swap_data.pool = Swap(convert(response, address))

    swap_data.coins = self._get_coins(swap_data.pool)

    response = self._get_if_exists(swap_data.pool.address, method_id("lp_price()"))
    if response != empty(Bytes[32]):
        swap_data.implementation |= Implementation.LP_PRICE

    response = self._get_if_exists(swap_data.pool.address,
                                   _abi_encode(empty(uint256), method_id=method_id("price_oracle(uint256)")))
    if response != empty(Bytes[32]):
        swap_data.implementation |= Implementation.ORACLE_NUM
    else:
        response = self._get_if_exists(swap_data.pool.address, method_id("price_oracle()"))
        if response != empty(Bytes[32]):
            assert len(swap_data.coins) == 2
            swap_data.implementation |= Implementation.ORACLE
        else:
            swap_data.implementation |= Implementation.CONST

    self.swap_data[_coin] = swap_data
    return swap_data


@internal
@view
def _get_price(_swap_data: SwapData, i: uint256) -> uint256:
    price: uint256 = 10 ** _swap_data.coins[i].decimals()  # dev: most probably no priority set

    if _swap_data.implementation in Implementation.LP_PRICE:
        price *= _swap_data.pool.lp_price()
    else:
        price *= _swap_data.pool.get_virtual_price()

    # Here price is calculated in .coins(0) with ONE + decimals precision

    if i > 0:
        if _swap_data.implementation in Implementation.ORACLE_NUM:
            price = price * _swap_data.pool.price_oracle(i - 1) / ONE
        elif _swap_data.implementation in Implementation.ORACLE:
            price = price * _swap_data.pool.price_oracle() / ONE
        elif _swap_data.implementation in Implementation.CONST_METAPOOL:
            price = price * ONE / _swap_data.base_pool.get_virtual_price()

    return price / ONE  # decimals might be small, so postpone division to the end


@internal
@view
def _get_prioritized_index(_swap_data: SwapData) -> uint256:
    """
    @notice Get index of coin with the largest priority
    @dev Equal priorities result in bad index, will fail later
    """
    priority: uint256 = 0
    index: uint256 = N_COINS_MAX
    for i in range(N_COINS_MAX):
        if i == len(_swap_data.coins):
            break
        current_priority: uint256 = self.priority_of[_swap_data.coins[i]]
        if current_priority > priority:
            priority = current_priority
            index = i
    return index


@internal
def _remove_liquidity_one_coin(_swap_data: SwapData, _amount: uint256, _i: uint256, _min_amount: uint256):
    if _swap_data.implementation in Implementation.I128:
        Swap128(_swap_data.pool.address).remove_liquidity_one_coin(
            _amount, convert(_i, int128), _min_amount
        )
    else:
        _swap_data.pool.remove_liquidity_one_coin(_amount, _i, _min_amount)


@internal
def _burn(_coin: CurveToken, _amount: uint256):
    """
    @param _coin Address of the coin being converted
    @param _amount Amount of coin to convert
    """
    assert not self.is_killed and not self.killed_coin[_coin], "Is killed"

    swap_data: SwapData = self._fetch_swap_data(_coin)
    i: uint256 = self._get_prioritized_index(swap_data)

    min_amount: uint256 = _amount * self._get_price(swap_data, i) / ONE

    slippage: uint256 = swap_data.slippage
    if slippage == 0:
        slippage = SLIPPAGE
    min_amount -= min_amount * slippage / BPS

    self._remove_liquidity_one_coin(swap_data, _amount, i, min_amount)

    if PROXY.burners(swap_data.coins[i].address) != self:
        assert swap_data.coins[i].transfer(
            PROXY.address, swap_data.coins[i].balanceOf(self), default_return_value=True
        )  # safe


@external
def burn(_coin: CurveToken) -> bool:
    """
    @notice Convert `_coin`
    @param _coin Address of the coin being converted
    @return bool Success, remained for compatibility
    """
    amount: uint256 = _coin.balanceOf(msg.sender)
    if amount != 0:
        _coin.transferFrom(msg.sender, self, amount)  # LP Tokens are safe

    amount = _coin.balanceOf(self)
    if amount != 0:
        self._burn(_coin, amount)

    return True


@external
def burn_amount(_coin: CurveToken, _amount_to_burn: uint256):
    """
    @notice Burn a specific quantity of `_coin`
    @dev Useful when the total amount to burn is so large that it fails from slippage
    @param _coin Address of the coin being converted
    @param _amount_to_burn Amount of the coin to burn
    """
    amount: uint256 = _coin.balanceOf(PROXY.address)
    if amount != 0 and PROXY.burners(_coin.address) == self:
        _coin.transferFrom(PROXY.address, self, amount, default_return_value=True)

    amount = _coin.balanceOf(self)
    assert amount >= _amount_to_burn, "Insufficient balance"

    self._burn(_coin, _amount_to_burn)


@external
@view
def burns_to(_coin: CurveToken) -> DynArray[address, 8]:
    """
    @notice Get resulting coins of burning `_coin`
    @param _coin Coin to burn
    """
    swap_data: SwapData = self.swap_data[_coin]
    coin: address = empty(address)
    if swap_data.pool != empty(Swap):
        i: uint256 = self._get_prioritized_index(swap_data)
        if i < N_COINS_MAX:
            coin = swap_data.coins[i].address
    return [coin]


@internal
@pure
def _check_implementation(_implementation: Implementation):
    # Exactly 1 of price
    subset: uint256 = convert(_implementation & IMPLEMENTATION_PRICE, uint256)
    assert subset & (subset - 1) == 0  # dev: Wrong number of prices set


@internal
def _add_swap_data(_data_input: SwapDataInput):
    assert _data_input.slippage <= BPS
    assert _data_input.base_pool == empty(Swap) or\
            _data_input.implementation in Implementation.CONST_METAPOOL  # dev: only for metapools
    self._check_implementation(_data_input.implementation)

    pool: Swap = _data_input.pool
    if pool == empty(Swap):
        pool = Swap(_data_input.coin.address)

    base_pool: Swap = _data_input.base_pool
    if _data_input.implementation in Implementation.CONST_METAPOOL and base_pool == empty(Swap):
        base_pool = Swap(_data_input.pool.coins(1))

    swap_data: SwapData = SwapData({
        pool: pool,
        coins: self._get_coins(pool),
        implementation: _data_input.implementation,
        base_pool: base_pool,
        slippage: _data_input.slippage,
    })
    self.swap_data[_data_input.coin] = swap_data

    if _data_input.implementation not in Implementation.CONST:
        self._get_price(swap_data, 1)  # Check price calls
        if _data_input.implementation in (Implementation.ORACLE | Implementation.CONST_METAPOOL):
            assert len(swap_data.coins) == 2  # dev: method is for 2-coins pool


@external
def set_swap_data(_swap_data: DynArray[SwapDataInput, 16]):
    """
    @notice Set conversion data
    @dev Executable only via owner
    @param _swap_data Conversion data inputs array of max length 16.
    """
    assert msg.sender == self.owner, "Only owner"

    for data_input in _swap_data:
        if data_input.implementation == empty(Implementation):  # Remove SwapData
            self.swap_data[data_input.coin] = empty(SwapData)
        else:
            self._add_swap_data(data_input)


@external
def set_priorities(_priorities: DynArray[PriorityInput, 16]):
    """
    @notice Set coins priorities
    @dev Executable only via owner
    @param _priorities Priorities of coins to set
    """
    assert msg.sender == self.owner, "Only owner"

    for data_input in _priorities:
        self.priority_of[data_input.coin] = data_input.priority


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
def set_killed(_is_killed: bool, _coin: CurveToken=empty(CurveToken)):
    """
    @notice Stop a contract or specific coin to be burnt
    @dev Executable only via owner or emergency owner
    @param _is_killed Boolean value to set
    @param _coin Coin to stop from burning, ZERO_ADDRESS to kill all coins (by default)
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    if _coin == empty(CurveToken):
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
