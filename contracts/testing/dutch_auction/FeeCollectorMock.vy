# pragma version 0.5.0b1
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title FeeCollectorMock
@author Curve Finance
@license MIT
@notice Configurable-calendar FeeCollector stand-in for test-in-prod runs of
        the Dutch auction stack: the real DutchAuctionBurner, AdapterRegistry,
        and adapters deploy against it unchanged, while the owner picks the
        period, the epoch bounds, the target token, and the keeper fee.
@dev Implements the IFeeCollector surface the burner stack reads, plus
     collect, the burner setter, and recovery. Calendar: periods of `period`
     seconds anchored at `start_time`; inside a period SLEEP = [0, bounds[0]),
     COLLECT = [bounds[0], bounds[1]), EXCHANGE = [bounds[1], bounds[2]),
     FORWARD = [bounds[2], period). The burner pins the EXCHANGE length at
     deployment, so the calendar is set before deploying a burner against it
     and the setter is meant for a fresh burner only.
     No nonreentrancy pragma on purpose: collect() calls burner.burn, which
     calls back into transfer(), fee(), and target() while collect is still
     executing (the production FeeCollector uses separate lock keys for this).
@custom:kill Testing-only contract for throwaway deployments holding small
             balances: the owner or emergency owner kills epochs per coin or
             for all coins (set_killed), the owner swaps the burner, and either
             role recovers every balance. Never used in production.
@custom:security Owner-trusted by design: no timelocks, no two-step role
                 transfers. Keep only test balances on it.
"""

from ethereum.ercs import IERC20

from contracts.interfaces import IBurner
from contracts.interfaces import IFeeCollector
from contracts.utils import constants as c

implements: IFeeCollector


interface ERC165:
    def supportsInterface(_interface_id: bytes4) -> bool: view


error OnlyOwner:
    pass


error OnlyOwnerOrEmergency:
    pass


error OnlyBurner:
    pass


error WrongEpoch:
    pass


error KilledEpoch:
    pass


error KilledCoin:
    pass


error CoinsNotSorted:
    pass


error BadEpoch:
    pass


error BadCalendar:
    pass


error BadBurner:
    pass


error ZeroAddress:
    pass


event SetBurner:
    burner: indexed(address)


event SetCalendar:
    start_time: uint256
    period: uint256
    bounds: uint256[3]


event SetKilled:
    coin: indexed(address)
    killed: IFeeCollector.Epoch


event Recovered:
    coin: indexed(address)
    receiver: indexed(address)
    amount: uint256


ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE
# Kill-mask key standing for every coin.
ALL_COINS: constant(address) = empty(address)

# Payment token the burner sells into; read by the burner as fee_collector.target().
target: public(address)
burner: public(IBurner)

# Calendar: epoch offsets inside a period (see the module @dev).
start_time: public(uint256)
period: public(uint256)
bounds: public(uint256[3])
# Keeper incentive ceiling per epoch, WAD-scaled; indexed by the Epoch bit value.
max_fee: public(uint256[9])
is_killed: public(HashMap[address, IFeeCollector.Epoch])

owner: public(address)
emergency_owner: public(address)


@deploy
def __init__(
    _target: address,
    _owner: address,
    _emergency_owner: address,
    _start_time: uint256,
    _period: uint256,
    _bounds: uint256[3],
):
    """
    @notice Set the target, the roles, and the calendar.
    @param _target Token the burner sells into.
    @param _owner Owner: burner, calendar, fee, kill, and recovery control.
    @param _emergency_owner Emergency owner: kill and recovery control.
    @param _start_time Anchor of the first period; at or before now.
    @param _period Period length in seconds.
    @param _bounds Offsets of the COLLECT, EXCHANGE, and FORWARD starts.
    """
    assert _target != empty(address) and _owner != empty(address), ZeroAddress()
    self.target = _target
    self.owner = _owner
    self.emergency_owner = _emergency_owner
    self._set_calendar(_start_time, _period, _bounds)


# Calendar


@internal
@view
def _epoch(_timestamp: uint256) -> IFeeCollector.Epoch:
    offset: uint256 = (_timestamp - self.start_time) % self.period
    if offset < self.bounds[0]:
        return IFeeCollector.Epoch.SLEEP
    if offset < self.bounds[1]:
        return IFeeCollector.Epoch.COLLECT
    if offset < self.bounds[2]:
        return IFeeCollector.Epoch.EXCHANGE
    return IFeeCollector.Epoch.FORWARD


@internal
@view
def _epoch_time_frame(_epoch: IFeeCollector.Epoch, _timestamp: uint256) -> (uint256, uint256):
    period_start: uint256 = _timestamp - (_timestamp - self.start_time) % self.period
    if _epoch == IFeeCollector.Epoch.SLEEP:
        return period_start, period_start + self.bounds[0]
    if _epoch == IFeeCollector.Epoch.COLLECT:
        return period_start + self.bounds[0], period_start + self.bounds[1]
    if _epoch == IFeeCollector.Epoch.EXCHANGE:
        return period_start + self.bounds[1], period_start + self.bounds[2]
    assert _epoch == IFeeCollector.Epoch.FORWARD, BadEpoch()
    return period_start + self.bounds[2], period_start + self.period


@external
@view
def epoch(_timestamp: uint256 = block.timestamp) -> IFeeCollector.Epoch:
    """
    @notice Epoch at a timestamp (now by default).
    """
    return self._epoch(_timestamp)


@external
@view
def epoch_time_frame(
    _epoch: IFeeCollector.Epoch, _timestamp: uint256 = block.timestamp
) -> (uint256, uint256):
    """
    @notice [start, end) of `_epoch` in the period containing `_timestamp`.
    """
    return self._epoch_time_frame(_epoch, _timestamp)


@internal
@view
def _fee(_epoch: IFeeCollector.Epoch, _timestamp: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_time_frame(_epoch, _timestamp)
    if _timestamp < start or _timestamp >= end:
        return 0
    return self.max_fee[convert(_epoch, uint256)] * (_timestamp + 1 - start) // (end - start)


@external
@view
def fee(
    _epoch: IFeeCollector.Epoch = empty(IFeeCollector.Epoch),
    _timestamp: uint256 = block.timestamp,
) -> uint256:
    """
    @notice Keeper incentive share at `_timestamp`, growing linearly over the
            epoch to max_fee; the current epoch when `_epoch` is empty.
    """
    if _epoch == empty(IFeeCollector.Epoch):
        return self._fee(self._epoch(_timestamp), _timestamp)
    return self._fee(_epoch, _timestamp)


# Burner integration


@external
@view
def can_exchange(_coins: DynArray[address, c.MAX_COINS]) -> bool:
    """
    @notice Whether `_coins` may be sold now: EXCHANGE epoch and no kill mask.
    """
    if (
        self._epoch(block.timestamp) != IFeeCollector.Epoch.EXCHANGE
        or self.is_killed[ALL_COINS] in IFeeCollector.Epoch.EXCHANGE
    ):
        return False
    for coin: address in _coins:
        if self.is_killed[coin] in IFeeCollector.Epoch.EXCHANGE:
            return False
    return True


@external
def transfer(_transfers: DynArray[IFeeCollector.Transfer, c.MAX_COINS]):
    """
    @notice Move coins out on the burner's behalf (max_value = full balance).
    @dev Burner-only, inside COLLECT or EXCHANGE, honoring kill masks.
    """
    assert msg.sender == self.burner.address, OnlyBurner()
    epoch: IFeeCollector.Epoch = self._epoch(block.timestamp)
    assert epoch in IFeeCollector.Epoch.COLLECT | IFeeCollector.Epoch.EXCHANGE, WrongEpoch()
    assert not self.is_killed[ALL_COINS] in epoch, KilledEpoch()

    for transfer: IFeeCollector.Transfer in _transfers:
        assert not self.is_killed[transfer.coin] in epoch, KilledCoin()
        coin: IERC20 = IERC20(transfer.coin)
        amount: uint256 = transfer.amount
        if amount == max_value(uint256):
            amount = staticcall coin.balanceOf(self)
        assert extcall coin.transfer(transfer.to, amount, default_return_value=True)


@external
def collect(_coins: DynArray[IERC20, c.MAX_COINS], _receiver: address = msg.sender):
    """
    @notice Hand the collected coins to the burner (stages the lots).
    @param _coins Coins to collect, sorted ascending by address.
    @param _receiver Receiver of the COLLECT incentive.
    """
    assert self._epoch(block.timestamp) == IFeeCollector.Epoch.COLLECT, WrongEpoch()
    assert not self.is_killed[ALL_COINS] in IFeeCollector.Epoch.COLLECT, KilledEpoch()
    previous: uint256 = 0
    for coin: IERC20 in _coins:
        assert not self.is_killed[coin.address] in IFeeCollector.Epoch.COLLECT, KilledCoin()
        current: uint256 = convert(coin.address, uint256)
        assert current > previous, CoinsNotSorted()
        previous = current
    extcall self.burner.burn(_coins, _receiver)


# Owner


@internal
def _set_calendar(_start_time: uint256, _period: uint256, _bounds: uint256[3]):
    assert _start_time <= block.timestamp, BadCalendar()
    assert 0 < _bounds[0] and _bounds[0] < _bounds[1], BadCalendar()
    assert _bounds[1] < _bounds[2] and _bounds[2] < _period, BadCalendar()
    self.start_time = _start_time
    self.period = _period
    self.bounds = _bounds
    log SetCalendar(start_time=_start_time, period=_period, bounds=_bounds)


@external
def set_calendar(_start_time: uint256, _period: uint256, _bounds: uint256[3]):
    """
    @notice Replace the calendar; only before deploying a burner against it
            (the burner pins the EXCHANGE length).
    """
    assert msg.sender == self.owner, OnlyOwner()
    self._set_calendar(_start_time, _period, _bounds)


@external
def set_burner(_burner: IBurner):
    """
    @notice Set the burner; must claim the burner interface.
    """
    assert msg.sender == self.owner, OnlyOwner()
    assert staticcall ERC165(_burner.address).supportsInterface(c.BURNER_INTERFACE_ID), (
        BadBurner()
    )
    self.burner = _burner
    log SetBurner(burner=_burner.address)


@external
def set_target(_target: address):
    """
    @notice Change the payment token; pair with the burner's resync_target.
    """
    assert msg.sender == self.owner, OnlyOwner()
    assert _target != empty(address), ZeroAddress()
    self.target = _target


@external
def set_max_fee(_epoch: IFeeCollector.Epoch, _max_fee: uint256):
    """
    @notice Keeper incentive ceiling (WAD) for a single epoch.
    """
    assert msg.sender == self.owner, OnlyOwner()
    index: uint256 = convert(_epoch, uint256)
    assert index != 0 and index & (index - 1) == 0, BadEpoch()
    assert _max_fee <= c.WAD, BadEpoch()
    self.max_fee[index] = _max_fee


@external
def set_owner(_owner: address):
    assert msg.sender == self.owner, OnlyOwner()
    assert _owner != empty(address), ZeroAddress()
    self.owner = _owner


@external
def set_emergency_owner(_emergency_owner: address):
    assert msg.sender == self.owner, OnlyOwner()
    self.emergency_owner = _emergency_owner


# Emergency and recovery


@external
def set_killed(_coin: address, _killed: IFeeCollector.Epoch):
    """
    @notice Kill mask for a coin (empty address: every coin).
    """
    assert msg.sender in [self.owner, self.emergency_owner], OnlyOwnerOrEmergency()
    self.is_killed[_coin] = _killed
    log SetKilled(coin=_coin, killed=_killed)


@external
def recover(_coins: DynArray[address, c.MAX_COINS], _receiver: address):
    """
    @notice Send full balances to `_receiver`; ETH_ADDRESS for the native coin.
    """
    assert msg.sender in [self.owner, self.emergency_owner], OnlyOwnerOrEmergency()
    assert _receiver != empty(address), ZeroAddress()
    for coin: address in _coins:
        amount: uint256 = 0
        if coin == ETH_ADDRESS:
            amount = self.balance
            if amount != 0:
                raw_call(_receiver, b"", value=amount)
        else:
            amount = staticcall IERC20(coin).balanceOf(self)
            if amount != 0:
                assert extcall IERC20(coin).transfer(_receiver, amount, default_return_value=True)
        log Recovered(coin=coin, receiver=_receiver, amount=amount)


@external
@payable
def __default__():
    pass
