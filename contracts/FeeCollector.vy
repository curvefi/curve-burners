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
    def act(_hook_inputs: DynArray[HookInput, MAX_HOOK_LEN], _receiver: address=msg.sender, _mandatory: bool=False) -> uint256: payable
    def buffer_amount() -> uint256: view
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


struct HookInput:
    hook_id: uint8
    value: uint256
    data: Bytes[8192]


struct RecoverInput:
    coin: ERC20
    amount: uint256


struct KilledInput:
    coin: ERC20
    killed: Epoch  # True where killed


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
WETH: immutable(wETH)

MAX_LEN: constant(uint256) = 64
MAX_HOOK_LEN: constant(uint256) = 32
ONE: constant(uint256) = 10 ** 18  # Precision

START_TIME: constant(uint256) = 1600300800  # ts of distribution start
EPOCH_TIMESTAMPS: immutable(uint256[17])

target: public(ERC20)  # coin swapped into
max_fee: public(uint256[9])  # max_fee[Epoch]

BURNER_INTERFACE_ID: constant(bytes4) = 0xa3b5e311
HOOKER_INTERFACE_ID: constant(bytes4) = 0xb95b1a35
burner: public(Burner)
hooker: public(Hooker)

is_killed: public(HashMap[ERC20, Epoch])
ALL_COINS: immutable(ERC20)  # Auxiliary indicator for all coins (=ZERO_ADDRESS)

owner: public(address)
emergency_owner: public(address)


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

    self.max_fee[convert(Epoch.COLLECT, uint256)] = ONE / 100  # 1%
    self.max_fee[convert(Epoch.FORWARD, uint256)] = ONE / 100  # 1%

    ALL_COINS = ERC20(empty(address))

    timestamps: uint256[17] = empty(uint256[17])
    if False:  # testing exchange
        # timestamps[1] = 0
        timestamps[2] = 100
        timestamps[4] = 200
        timestamps[8] = 7 * 24 * 3600 - 100
        timestamps[16] = 7 * 24 * 3600
    else:
        # timestamps[1] = 0
        timestamps[2] = 4 * 24 * 3600
        timestamps[4] = 5 * 24 * 3600
        timestamps[8] = 6 * 24 * 3600
        timestamps[16] = 7 * 24 * 3600
    EPOCH_TIMESTAMPS = timestamps

    self.is_killed[empty(ERC20)] = Epoch.COLLECT | Epoch.FORWARD  # Set burner first


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
    for epoch in [Epoch.SLEEP, Epoch.COLLECT, Epoch.EXCHANGE, Epoch.FORWARD]:
        if ts < EPOCH_TIMESTAMPS[2 * convert(epoch, uint256)]:
            return epoch
    raise "Bad Epoch"


@external
@view
def epoch(ts: uint256=block.timestamp) -> Epoch:
    """
    @notice Get epoch at certain timestamp
    @param ts Timestamp. Current by default
    @return Epoch
    """
    return self._epoch_ts(ts)


@internal
@pure
def _epoch_time_frame(epoch: Epoch, ts: uint256) -> (uint256, uint256):
    subset: uint256 = convert(epoch, uint256)
    assert subset & (subset - 1) == 0, "Bad Epoch"

    ts = ts - (ts - START_TIME) % (7 * 24 * 3600)
    return (ts + EPOCH_TIMESTAMPS[convert(epoch, uint256)], ts + EPOCH_TIMESTAMPS[2 * convert(epoch, uint256)])


@external
@view
def epoch_time_frame(_epoch: Epoch, _ts: uint256=block.timestamp) -> (uint256, uint256):
    """
    @notice Get time frame of certain epoch
    @param _epoch Epoch
    @param _ts Timestamp to anchor to. Current by default
    @return [start, end) time frame boundaries
    """
    return self._epoch_time_frame(_epoch, _ts)


@internal
@view
def _fee(epoch: Epoch, ts: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_time_frame(epoch, ts)
    if ts >= end:
        return 0
    return self.max_fee[convert(epoch, uint256)] * (ts - start) / (end - start)


@external
@view
def fee(_epoch: Epoch=empty(Epoch), _ts: uint256=block.timestamp) -> uint256:
    """
    @notice Calculate keeper's fee for calling `collect`
    @param _epoch Epoch to count fee for
    @param _ts Timestamp of collection
    @return Fee with base 10^18
    """
    if _epoch == empty(Epoch):
        return self._fee(self._epoch_ts(_ts), _ts)
    return self._fee(_epoch, _ts)


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
    fee: uint256 = self._fee(Epoch.COLLECT, block.timestamp)
    for i in range(len(_coins), bound=MAX_LEN):
        collected_amount: uint256 = _coins[i].balanceOf(self) - balances[i]
        balances[i] = collected_amount * fee / ONE
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
    if self._epoch_ts(block.timestamp) != Epoch.EXCHANGE or\
        self.is_killed[ALL_COINS] in Epoch.EXCHANGE:
        return False
    for coin in _coins:
        if self.is_killed[coin] in Epoch.EXCHANGE:
            return False
    return True


@external
@payable
@nonreentrant("forward")
def forward(_hook_inputs: DynArray[HookInput, MAX_HOOK_LEN], _receiver: address=msg.sender) -> uint256:
    """
    @notice Transfer target coin forward
    @param _hook_inputs Input parameters for forward hooks
    @param _receiver Receiver of caller `forward_fee`
    @return Amount of received fee
    """
    assert self._epoch_ts(block.timestamp) == Epoch.FORWARD
    target: ERC20 = self.target
    assert not (self.is_killed[ALL_COINS] | self.is_killed[target]) in Epoch.FORWARD

    self.burner.push_target()
    amount: uint256 = target.balanceOf(self)
    fee: uint256 = self._fee(Epoch.FORWARD, block.timestamp) * amount / ONE

    hooker: Hooker = self.hooker
    hooker_buffer: uint256 = hooker.buffer_amount()
    target.transfer(hooker.address, amount - hooker_buffer - fee)
    fee += hooker.act(_hook_inputs, _receiver, True, value=msg.value)
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
def set_max_fee(_epoch: Epoch, _max_fee: uint256):
    """
    @notice Set keeper's max fee
    @dev Callable only by owner
    """
    assert msg.sender == self.owner, "Only owner"
    subset: uint256 = convert(_epoch, uint256)
    assert subset & (subset - 1) == 0, "Bad Epoch"
    assert _max_fee <= ONE, "Bad max_fee"
    self.max_fee[convert(_epoch, uint256)] = _max_fee


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
