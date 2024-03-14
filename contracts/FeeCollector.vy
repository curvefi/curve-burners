# @version 0.3.10
"""
@title FeeCollector
@notice Collects fees and delegates to burner for exchange
"""


interface ERC20:
    def approve(_to: address, _value: uint256): nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view

interface wETH:
    def balanceOf(_owner: address) -> uint256: view
    def transferFrom(_sender: address, _receiver: address, _amount: uint256): nonpayable
    def transfer(_receiver: address, _amount: uint256): nonpayable
    def withdraw(_amount: uint256): nonpayable
    def deposit(): payable

interface Curve:
    def withdraw_admin_fees(): nonpayable

interface Burner:
    def burn(_coins: DynArray[ERC20, MAX_LEN], _receiver: address): nonpayable
    def push_target() -> uint256: nonpayable
    def supportsInterface(_interface_id: bytes4) -> bool: view

interface Hooker:
    def callback(_callback: Callback): payable
    def forward(): payable
    def supportsInterface(_interface_id: bytes4) -> bool: view


event Collected:
    coin: indexed(address)
    keeper: indexed(address)
    amount: uint256
    fee_paid: uint256


enum Epoch:
    SLEEP  # 1
    COLLECT  # 2
    EXCHANGE  # 4
    FORWARD  # 8


struct Callback:
    to: address
    data: Bytes[4000]


struct RecoverInput:
    coin: ERC20
    amount: uint256


struct KilledInput:
    coin: ERC20
    killed: Epoch  # True where killed


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
WETH: immutable(wETH)

MAX_LEN: constant(uint256) = 64
ONE: constant(uint256) = 10 ** 18  # Precision

owner: public(address)
emergency_owner: public(address)

START_TIME: constant(uint256) = 1600300800  # ts of distribution start

# Collect fees
COLLECT_EPOCH_TS: immutable(uint256)
max_collect_fee: public(uint256)

EXCHANGE_EPOCH_TS: immutable(uint256)

# Forward
FORWARD_EPOCH_TS: immutable(uint256)
target: public(ERC20)  # coin swapped into
max_forward_fee: public(uint256)

BURNER_INTERFACE_ID: constant(bytes4) = 0x5c144e65
HOOKER_INTERFACE_ID: constant(bytes4) = 0xc8e65276
burner: public(Burner)
hooker: public(Hooker)
is_killed: HashMap[ERC20, Epoch]
ALL_COINS: immutable(ERC20)


@external
def __init__(_target_coin: ERC20, _weth: wETH, _owner: address, _emergency_owner: address):
    """
    @notice Contract constructor
    @param _target_coin Coin to swap to
    @param _owner Owner address.
    @param _emergency_owner Emergency owner address. Can kill the contract.
    """
    self.target = _target_coin
    WETH = _weth
    self.owner = _owner
    self.emergency_owner = _emergency_owner

    self.max_collect_fee = ONE / 100  # 1%
    self.max_forward_fee = ONE / 100  # 1%

    ALL_COINS = ERC20(empty(address))

    COLLECT_EPOCH_TS = 4 * 24 * 3600
    EXCHANGE_EPOCH_TS = 5 * 24 * 3600
    FORWARD_EPOCH_TS = 6 * 24 * 3600
#    COLLECT_EPOCH_TS = 100
#    EXCHANGE_EPOCH_TS = 200
#    FORWARD_EPOCH_TS = 7 * 24 * 3600 - 100


@external
@payable
def __default__():
    # Deposited ETH can be converted using `burn(ETH_ADDRESS)`
    pass


@external
def withdraw_many(_pools: DynArray[address, MAX_LEN]):
    """
    @notice Withdraw admin fees from multiple pools
    @param _pools List of pool address to withdraw admin fees from
    """
    for pool in _pools:
        Curve(pool).withdraw_admin_fees()


@external
@payable
def burn(_coin: address) -> bool:
    """
    @notice Transfer coin from approved contract
    @dev Needed for back compatability along with dealing raw ETH
    @param _coin Coin to transfer
    @return True if did not fail, back compatability
    """
    if _coin == ETH_ADDRESS:  # Deposit
        WETH.deposit(value=self.balance)
    else:
        amount: uint256 = ERC20(_coin).balanceOf(msg.sender)
        assert ERC20(_coin).transferFrom(msg.sender, self, amount, default_return_value=True)  # safe
    return True


@internal
@pure
def _epoch_ts(ts: uint256) -> Epoch:
    ts = (ts - START_TIME) % (7 * 24 * 3600)
    if ts < COLLECT_EPOCH_TS:
        return Epoch.SLEEP
    elif ts < EXCHANGE_EPOCH_TS:
        return Epoch.COLLECT
    elif ts < FORWARD_EPOCH_TS:
        return Epoch.EXCHANGE
    return Epoch.FORWARD


@external
@view
def epoch(ts: uint256=block.timestamp) -> Epoch:
    """
    @notice Get epoch at certain timestamp
    @param ts Timestamp. Current by default
    @return Epoch
    """
    return self._epoch_ts(ts)


@external
@view
def epoch_time_frame(_epoch: Epoch, _ts: uint256=block.timestamp) -> (uint256, uint256):
    """
    @notice Get time frame of certain epoch
    @param _epoch Epoch
    @param _ts Timestamp to anchor to. Current by default
    @return [start, end) time frame boundaries
    """
    ts: uint256 = _ts - (_ts - START_TIME) % (7 * 24 * 3600)
    if _epoch == Epoch.SLEEP:
        return (ts, ts + COLLECT_EPOCH_TS)
    elif _epoch == Epoch.COLLECT:
        return (ts + COLLECT_EPOCH_TS, ts + EXCHANGE_EPOCH_TS)
    elif _epoch == Epoch.EXCHANGE:
        return (ts + EXCHANGE_EPOCH_TS, ts + FORWARD_EPOCH_TS)
    elif _epoch == Epoch.FORWARD:
        return (ts + FORWARD_EPOCH_TS, ts + 7 * 24 * 3600)
    raise "Unknown Epoch"


@internal
@view
def _collect_fee(coin: ERC20, amount: uint256) -> uint256:
    """
    @dev Stable for now, dynamic soon
    """
    return amount * self.max_collect_fee / ONE


@external
@view
def collect_fee(coin: ERC20, amount: uint256) -> uint256:
    """
    @notice Calculate caller's fee for calling `collect`
    @param coin Coin to collect
    @param amount Amount of collected coin
    @return Amount to return to executor
    """
    return self._collect_fee(coin, amount)


@external
@nonreentrant("collect")
@payable
def collect(_coins: DynArray[ERC20, MAX_LEN], _callback: Callback, _receiver: address=msg.sender) -> DynArray[uint256, MAX_LEN]:
    """
    @notice Collect earned fees. Collection should happen under callback to earn caller fees.
    @param _coins Coins to collect sorted in ascending order
    @param _callback Callback for collection
    @param _receiver Receiver of caller `collect_fee`s
    @return Amounts of received fees
    """
    assert self._epoch_ts(block.timestamp) == Epoch.COLLECT
    assert not self.is_killed[ALL_COINS] in Epoch.COLLECT
    balances: DynArray[uint256, MAX_LEN] = []
    for coin in _coins:
        assert not self.is_killed[coin] in Epoch.COLLECT
        balances.append(coin.balanceOf(self))

    self.hooker.callback(_callback, value=msg.value)

    burner: Burner = self.burner
    for i in range(len(_coins), bound=MAX_LEN):
        collected_amount: uint256 = _coins[i].balanceOf(self) - balances[i]
        balances[i] = self._collect_fee(_coins[i], collected_amount)
        _coins[i].transfer(_receiver, balances[i])
        _coins[i].transfer(burner.address, _coins[i].balanceOf(self))
        log Collected(_coins[i].address, msg.sender, collected_amount, balances[i])

        # Eliminate case of repeated coins
        if i > 0:
            assert convert(_coins[i].address, uint160) > convert(_coins[i - 1].address, uint160), "Coins not sorted"

    burner.burn(_coins, _receiver)
    return balances


@external
@view
def exchange(_coins: DynArray[ERC20, MAX_LEN]) -> bool:
    """
    @notice Check whether coins are allowed to be exchanged
    @param _coins Coins to exchange
    @return Boolean value if coins are allowed to be exchanged
    """
    if self.is_killed[ALL_COINS] in Epoch.EXCHANGE:
        return False
    for coin in _coins:
        if self.is_killed[coin] in Epoch.EXCHANGE:
            return False
    return True


@internal
@view
def _forward_fee(amount: uint256) -> uint256:
    """
    @dev Stable for now, dynamic soon
    """
    return amount * self.max_forward_fee / ONE


@external
@view
def forward_fee(amount: uint256) -> uint256:
    """
    @notice Calculate caller's fee for calling `forward`
    @param amount Amount of forwarded coin
    @return Amount to return to executor
    """
    return self._forward_fee(amount)


@external
@payable
@nonreentrant("forward")
def forward(_receiver: address=msg.sender) -> uint256:
    """
    @notice Transfer target coin forward
    @param _receiver Receiver of caller `forward_fee`
    @return Amount of received fee
    """
    assert self._epoch_ts(block.timestamp) == Epoch.FORWARD
    target: ERC20 = self.target
    assert not (self.is_killed[ALL_COINS] | self.is_killed[target]) in Epoch.FORWARD

    self.burner.push_target()
    amount: uint256 = target.balanceOf(self)
    fee: uint256 = self._forward_fee(amount)

    hooker: Hooker = self.hooker
    target.transfer(hooker.address, amount - fee)
    hooker.forward(value=msg.value)

    target.transfer(_receiver, fee)
    return fee


@external
def recover(_recovers: DynArray[RecoverInput, MAX_LEN], _receiver: address):
    """
    @notice Recover ERC20 tokens or Ether from this contract
    @dev Callable only by owner and emergency owner
    @param _recovers (Token, amount) to recover
    @param _receiver Receiver of coins
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    for input in _recovers:
        amount: uint256 = input.amount
        if input.coin.address == ETH_ADDRESS:
            if amount == max_value(uint256):
                amount = self.balance
            raw_call(_receiver, b"", value=amount)
        else:
            if amount == max_value(uint256):
                amount = input.coin.balanceOf(self)
            input.coin.transfer(_receiver, amount)  # do not need safe transfer


@external
def set_burner(_new_burner: Burner):
    """
    @notice Set burner for exchanging coins
    @dev Callable only by owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert _new_burner.supportsInterface(BURNER_INTERFACE_ID)
    self.burner = _new_burner


@external
def set_hooker(_new_hooker: Hooker):
    """
    @notice Set contract for hooks and callbacks
    @dev Callable only by owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert _new_hooker.supportsInterface(HOOKER_INTERFACE_ID)
    self.hooker = _new_hooker


@external
def set_killed(_input: DynArray[KilledInput, MAX_LEN]):
    """
    @notice Stop a contract or specific coin to be burnt
    @dev Callable only by owner or emergency owner
    """
    assert msg.sender in [self.owner, self.emergency_owner], "Only owner"

    for input in _input:
        self.is_killed[input.coin] = input.killed


@external
def set_owner(_new_owner: address):
    """
    @notice Set owner of the contract
    @dev Callable only by current owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert _new_owner != empty(address)
    self.owner = _new_owner


@external
def set_emergency_owner(_new_owner: address):
    """
    @notice Set emergency owner of the contract
    @dev Callable only by current owner
    """
    assert msg.sender == self.owner, "Only owner"
    assert _new_owner != empty(address)
    self.emergency_owner = _new_owner
