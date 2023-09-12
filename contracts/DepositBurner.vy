# @version 0.3.9
"""
@title Deposit Burner
@notice Deposits coins into pool
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view
    def decimals() -> uint256: view

interface Swap:
    def coins(_i: uint256) -> address: view
    def get_virtual_price() -> uint256: view
    def price_oracle(_i: uint256=0) -> uint256: view
    def lp_price() -> uint256: view

interface Swap2:
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256, use_underlying: bool=False): payable

interface Swap3:
    def add_liquidity(amounts: uint256[3], min_mint_amount: uint256, use_underlying: bool=False): payable

interface Swap4:
    def add_liquidity(amounts: uint256[4], min_mint_amount: uint256, use_underlying: bool=False): payable

interface Swap5:
    def add_liquidity(amounts: uint256[5], min_mint_amount: uint256, use_underlying: bool=False): payable

interface Proxy:
    def burners(_coin: address) -> address: view


enum Implementation:
    # Price
    CONST  # 1
    ORACLE  # 2
    ORACLE_NUM  # 4
    # .lp_price() method, used as bool
    LP_PRICE  # 8
    # Type, used as bool
    UNDERLYING  # 16

IMPLEMENTATION_PRICE: immutable(Implementation)

struct SwapData:
    pool: Swap
    token: ERC20
    coins: DynArray[ERC20, N_COINS_MAX]
    mul: DynArray[uint256, N_COINS_MAX]  # decimals multiplier
    implementation: Implementation
    slippage: uint256

struct SwapDataInput:
    coin: ERC20  # Coin to trigger on, last to call in a row
    pool: Swap
    token: ERC20  # In case of external contract for LP token
    implementation: Implementation
    slippage: uint256


N_COINS_MAX: constant(uint256) = 5
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


@internal
def _transfer_in(_coin: ERC20, _from: address) -> uint256:
    """
    @notice Transfer coin whether it is ERC20 or ETH
    """
    if _coin.address == ETH_ADDRESS:
        return self.balance

    amount: uint256 = _coin.balanceOf(_from)
    if amount != 0:
        _coin.transferFrom(_from, self, amount)
        return _coin.balanceOf(self)
    return amount


@internal
def _balance(_coin: ERC20) -> uint256[2]:
    """
    @notice Get _coin balance of self
    @return [balance, eth_amount]
    """
    if _coin.address == ETH_ADDRESS:
        return [self.balance, self.balance]
    return [_coin.balanceOf(self), 0]


@internal
@view
def _get_min_lp(_swap_data: SwapData, _amounts: DynArray[uint256, N_COINS_MAX]) -> uint256:
    min_lp: uint256 = _amounts[0] * _swap_data.mul[0]
    if _swap_data.implementation in Implementation.CONST:
        for i in range(1, N_COINS_MAX):
            if i == len(_swap_data.mul):
                break
            min_lp += _amounts[i] * _swap_data.mul[i]
    elif _swap_data.implementation in Implementation.ORACLE:
        min_lp += _amounts[1] * _swap_data.mul[1] * ONE / _swap_data.pool.price_oracle()
    elif _swap_data.implementation in Implementation.ORACLE_NUM:
        for i in range(1, N_COINS_MAX):
            if i == len(_swap_data.mul):
                break
            min_lp += _amounts[i] * _swap_data.mul[i] * ONE / _swap_data.pool.price_oracle(i - 1)

    # Here min_lp is calculated in .coins(0)

    if _swap_data.implementation in Implementation.LP_PRICE:
        min_lp = min_lp * ONE / _swap_data.pool.lp_price()
    else:
        min_lp = min_lp * ONE / _swap_data.pool.get_virtual_price()

    return min_lp


@internal
def _add_liquidity(_swap_data: SwapData, _amounts: DynArray[uint256, N_COINS_MAX], _eth_amount: uint256, _min_lp: uint256):
    """
    @notice Add Liquidity execution implementation
    @param _swap_data Swap metadata
    @param _amounts Amounts to deposit
    @param _eth_amount ETH amount to include in transaction
    @param _min_lp Minimum amount to receive
    """
    if len(_swap_data.coins) == 2:
        amounts: uint256[2] = [_amounts[0], _amounts[1]]
        if _swap_data.implementation in Implementation.UNDERLYING:
            Swap2(_swap_data.pool.address).add_liquidity(amounts, _min_lp, True, value=_eth_amount)
        else:
            Swap2(_swap_data.pool.address).add_liquidity(amounts, _min_lp, value=_eth_amount)
        return

    if len(_swap_data.coins) == 3:
        amounts: uint256[3] = [_amounts[0], _amounts[1], _amounts[2]]
        if _swap_data.implementation in Implementation.UNDERLYING:
            Swap3(_swap_data.pool.address).add_liquidity(amounts, _min_lp, True, value=_eth_amount)
        else:
            Swap3(_swap_data.pool.address).add_liquidity(amounts, _min_lp, value=_eth_amount)
        return

    if len(_swap_data.coins) == 4:
        amounts: uint256[4] = [_amounts[0], _amounts[1], _amounts[2], _amounts[3]]
        if _swap_data.implementation in Implementation.UNDERLYING:
            Swap4(_swap_data.pool.address).add_liquidity(amounts, _min_lp, True, value=_eth_amount)
        else:
            Swap4(_swap_data.pool.address).add_liquidity(amounts, _min_lp, value=_eth_amount)
        return

    if len(_swap_data.coins) == 5:
        amounts: uint256[5] = [_amounts[0], _amounts[1], _amounts[2], _amounts[3], _amounts[4]]
        if _swap_data.implementation in Implementation.UNDERLYING:
            Swap5(_swap_data.pool.address).add_liquidity(amounts, _min_lp, True, value=_eth_amount)
        else:
            Swap5(_swap_data.pool.address).add_liquidity(amounts, _min_lp, value=_eth_amount)
        return


@internal
def _burn(_swap_data: SwapData, _amounts: DynArray[uint256, N_COINS_MAX], _eth_amount: uint256):
    """
    @param _swap_data Burning metadata
    @param _amounts Amounts of coins to convert
    @param _eth_amount ETH amount to include in transaction
    """
    assert not self.is_killed and not self.killed_coin[_swap_data.token], "Is killed"

    min_lp: uint256 = self._get_min_lp(_swap_data, _amounts)

    slippage: uint256 = _swap_data.slippage
    if slippage == 0:
        slippage = SLIPPAGE
    min_lp -= min_lp * slippage / BPS

    self._add_liquidity(_swap_data, _amounts, _eth_amount, min_lp)

    if PROXY.burners(_swap_data.token.address) != self:
        _swap_data.token.transfer(PROXY.address, _swap_data.token.balanceOf(self))  # LP Tokens are safe


@external
@payable
def burn(_coin: ERC20) -> bool:
    """
    @notice Trigger `_coin` burn
    @param _coin Address of the coin
    @return bool Success, remained for compatibility
    """
    amount: uint256 = self._transfer_in(_coin, msg.sender)

    swap_data: SwapData = self.swap_data[_coin]
    if swap_data.pool != empty(Swap):
        amounts: DynArray[uint256, N_COINS_MAX] = empty(DynArray[uint256, N_COINS_MAX])
        eth_amount: uint256 = 0
        for coin in swap_data.coins:
            balances: uint256[2] = self._balance(coin)
            amounts.append(balances[0])
            eth_amount += balances[1]
        self._burn(swap_data, amounts, eth_amount)

    return True


@external
@payable
def burn_amount(_coin: ERC20, _amounts_to_burn: DynArray[uint256, N_COINS_MAX]):
    """
    @notice Burn a specific amounts of coins
    @dev Useful when the total amount to burn is so large that it fails from slippage
    @param _coin Address of the coin to trigger burn
    @param _amounts_to_burn Amounts of coins to burn
    """
    if PROXY.burners(_coin.address) == self:
        self._transfer_in(_coin, PROXY.address)

    swap_data: SwapData = self.swap_data[_coin]
    assert len(_amounts_to_burn) == len(swap_data.coins), "Incorrect amounts"
    eth_amount: uint256 = 0
    for i in range(N_COINS_MAX):
        if i == len(_amounts_to_burn):
            break
        balances: uint256[2] = self._balance(swap_data.coins[i])
        assert balances[0] >= _amounts_to_burn[i], "Insufficient balance"
        eth_amount += balances[1]

    self._burn(swap_data, _amounts_to_burn, eth_amount)


@external
@view
def burns_to(_coin: ERC20) -> DynArray[address, 8]:
    """
    @notice Get resulting coins of burning `_coin`
    @param _coin Coin to burn
    """
    return [self.swap_data[_coin].token.address]


@internal
def _remove_swap_data(_coin: ERC20):
    swap_data: SwapData = self.swap_data[_coin]
    # Not all coins allow 0-approval
    # Besides it is a vast minority
    # It is not necessary to remove swap data
    for coin in swap_data.coins:
        coin.approve(swap_data.pool.address, 0)
    self.swap_data[_coin] = empty(SwapData)


@internal
@pure
def _check_implementation(_implementation: Implementation):
    # Exactly 1 of price
    subset: uint256 = convert(_implementation & IMPLEMENTATION_PRICE, uint256)
    assert subset & (subset - 1) == 0  # dev: Wrong number of prices set

@internal
def _get_coins(_pool: Swap) -> DynArray[ERC20, N_COINS_MAX]:
    """
    @notice Obtain list of coins of the pool
    """
    coins: DynArray[ERC20, N_COINS_MAX] = empty(DynArray[ERC20, N_COINS_MAX])
    for i in range(N_COINS_MAX):
        success: bool = False
        response: Bytes[32] = b""
        success, response = raw_call(
            _pool.address,
            _abi_encode(i, method_id=method_id("coins(uint256)")),
            max_outsize=32,
            is_static_call=True,
            revert_on_failure=False,
        )
        if not success or response == empty(Bytes[32]):
            break
        coins.append(ERC20(convert(response, address)))
    return coins


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

    coins: DynArray[ERC20, N_COINS_MAX] = self._get_coins(_data_input.pool)
    muls: DynArray[uint256, N_COINS_MAX] = empty(DynArray[uint256, N_COINS_MAX])
    for coin in coins:
        muls.append(ONE / self._decimals(coin))

    token: ERC20 = _data_input.token
    if token == empty(ERC20):
        token = ERC20(_data_input.pool.address)  # Use pool=token by default

    coin: ERC20 = _data_input.coin
    if coin == empty(ERC20):
        coin = coins[len(coins) - 1]  # Use last by default

    swap_data: SwapData = SwapData({
        pool: _data_input.pool,
        token: token,
        coins: coins,
        mul: muls,
        implementation: _data_input.implementation,
        slippage: _data_input.slippage,
    })
    self.swap_data[coin] = swap_data

    if _data_input.implementation not in Implementation.CONST:  # Check price_oracle call
        self._get_min_lp(swap_data, [1, 1, 1, 1, 1])  # Hardcoded N_COINS_MAX usage
        if _data_input.implementation in Implementation.ORACLE:
            assert len(coins) == 2  # dev: method is for 2-coins pool

    for c in coins:
        if c.address != ETH_ADDRESS:
            c.approve(_data_input.pool.address, max_value(uint256))


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
    @dev Executable only via owner or emergency owner. Use LP Token address for specific pools
    @param _is_killed Boolean value to set
    @param _coin LP Token of the pool to stop from burning, ZERO_ADDRESS to kill all coins (by default)
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
