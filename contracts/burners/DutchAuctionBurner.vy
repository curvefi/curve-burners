# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DutchAuctionBurner
@author Curve Finance
@license MIT
@notice Stages weekly fee-token lots and sells them along a geometric Dutch curve.
@custom:kill FeeCollector kill masks stop fills; its owner or emergency owner can also
             disable CoW and recover inventory only back to FeeCollector.
@custom:security The configured start total assumes every staged lot is worth no more
                 than that amount. Tokens with transfer fees, rebases, callbacks, or
                 blacklist behavior are best-effort integrations.
"""


from .modules import dutch_auction_math as auction_math
from .modules import cow_auction
from .modules import yearn_auction

initializes: cow_auction
initializes: yearn_auction
exports: (
    yearn_auction.want,
    yearn_auction.available,
    yearn_auction.price,
    yearn_auction.getAmountNeeded,
    yearn_auction.take,
)
exports: cow_auction.__interface__


interface ERC20:
    def approve(_spender: address, _amount: uint256) -> bool: nonpayable
    def transfer(_receiver: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_sender: address, _receiver: address, _amount: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


interface FeeCollector:
    def fee(_epoch: Epoch = ..., _timestamp: uint256 = ...) -> uint256: view
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch_time_frame(_epoch: Epoch, _timestamp: uint256 = ...) -> (uint256, uint256): view
    def can_exchange(_coins: DynArray[ERC20, MAX_COINS]) -> bool: view
    def transfer(_transfers: DynArray[Transfer, MAX_COINS]): nonpayable


interface AuctionTaker:
    def auctionTakeCallback(
        _from: address,
        _sender: address,
        _amount_taken: uint256,
        _amount_needed: uint256,
        _data: Bytes[MAX_CALLBACK_DATA],
    ): nonpayable


event LotSynced:
    token: indexed(ERC20)
    week: indexed(uint256)
    initial_amount: uint256
    start_total: uint256
    floor_total: uint256
    start: uint256
    end: uint256


event Taken:
    token: indexed(ERC20)
    week: indexed(uint256)
    caller: indexed(address)
    receiver: address
    amount_out: uint256
    payment: uint256
    remaining_balance: uint256


event Recovered:
    token: indexed(ERC20)
    amount: uint256


flag Epoch:
    SLEEP
    COLLECT
    EXCHANGE
    FORWARD


struct Transfer:
    coin: ERC20
    to: address
    amount: uint256


struct Lot:
    week: uint256
    initial_amount: uint256
    native_remaining: uint256
    start_total: uint256
    floor_total: uint256
    start: uint256
    end: uint256


MAX_COINS: constant(uint256) = 64
MAX_CALLBACK_DATA: constant(uint256) = 8192
WAD: constant(uint256) = 10**18
RAY: constant(uint256) = 10**27
WEEK: constant(uint256) = 7 * 24 * 60 * 60
MAX_PRICE_STEPS: constant(uint256) = 100_000
ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE

ERC165_INTERFACE_ID: constant(bytes4) = 0x01ffc9a7
BURNER_INTERFACE_ID: constant(bytes4) = 0xa3b5e311
VERSION: public(constant(String[20])) = "DutchAuction"

# Core deployment configuration
fee_collector: public(immutable(FeeCollector))
target: public(immutable(ERC20))
start_total: public(immutable(uint256))
floor_total: public(immutable(uint256))
decay_factor_ray: public(immutable(uint256))
step_duration: public(immutable(uint256))
cow_order_validity: public(immutable(uint256))
app_data: public(immutable(bytes32))

# Weekly lot accounting
lots: public(HashMap[ERC20, Lot])
cancelled_week: public(HashMap[ERC20, uint256])


@deploy
def __init__(
    _fee_collector: FeeCollector,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _cow_order_validity: uint256,
    _app_data: bytes32,
):
    """@notice Configure immutable weekly-auction economics."""
    assert _fee_collector.address != empty(address), "Bad FeeCollector"
    assert _start_total > 0, "Zero start total"
    assert 0 < _floor_total and _floor_total <= _start_total, "Bad floor"
    assert RAY // 2 <= _decay_factor_ray and _decay_factor_ray < RAY, "Bad decay"
    assert _step_duration > 0, "Zero step"
    assert _cow_order_validity > 0, "Zero CoW validity"

    configured_target: ERC20 = staticcall _fee_collector.target()
    assert configured_target.address != empty(address), "Bad target"

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = staticcall _fee_collector.epoch_time_frame(
        Epoch.EXCHANGE, block.timestamp
    )
    assert exchange_end > exchange_start, "Bad exchange frame"
    assert _cow_order_validity <= exchange_end - exchange_start, "CoW validity too long"
    active_elapsed: uint256 = exchange_end - exchange_start - 1
    assert active_elapsed // _step_duration <= MAX_PRICE_STEPS, "Too many price steps"
    assert auction_math.total_price(
        _start_total,
        _floor_total,
        _decay_factor_ray,
        active_elapsed,
        _step_duration,
    ) == _floor_total, "Decay misses floor"

    self.fee_collector = _fee_collector
    self.target = configured_target
    self.start_total = _start_total
    self.floor_total = _floor_total
    self.decay_factor_ray = _decay_factor_ray
    self.step_duration = _step_duration
    self.cow_order_validity = _cow_order_validity
    self.app_data = _app_data


# Weekly staging


@internal
@view
def _target_is_current() -> bool:
    return staticcall self.fee_collector.target() == self.target


@internal
@view
def _exchange_frame(_timestamp: uint256) -> (uint256, uint256):
    return staticcall self.fee_collector.epoch_time_frame(Epoch.EXCHANGE, _timestamp)


@internal
@pure
def _mul_div_down_wad(_amount: uint256, _wad_fraction: uint256) -> uint256:
    # Splitting the amount preserves floor rounding without overflowing the product.
    return (
        (_amount // WAD) * _wad_fraction
        + (_amount % WAD) * _wad_fraction // WAD
    )


@internal
@view
def _week_at(_timestamp: uint256) -> uint256:
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(_timestamp)
    return exchange_start // WEEK


@external
@view
def current_week() -> uint256:
    """@notice Return the FeeCollector calendar week used by lot snapshots."""
    return self._week_at(block.timestamp)


@external
def burn(_coins: DynArray[ERC20, MAX_COINS], _receiver: address):
    """
    @notice Pay the COLLECT incentive, take custody, and snapshot upcoming lots.
    @param _coins Sorted tokens supplied by FeeCollector.
    @param _receiver Receiver of the FeeCollector COLLECT incentive.
    """
    assert msg.sender == self.fee_collector.address, "Only FeeCollector"
    assert self._target_is_current(), "Target changed"

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    week: uint256 = exchange_start // WEEK

    fee: uint256 = staticcall self.fee_collector.fee(Epoch.COLLECT, block.timestamp)
    fee_payouts: DynArray[Transfer, MAX_COINS] = []
    custody_transfers: DynArray[Transfer, MAX_COINS] = []

    for coin: ERC20 in _coins:
        assert coin != self.target, "Target token"
        cancelled: uint256 = self.cancelled_week[coin]
        assert cancelled == 0 or cancelled != week, "Lot cancelled"
        collector_balance: uint256 = staticcall coin.balanceOf(self.fee_collector.address)
        fee_payouts.append(
            Transfer(
                coin=coin,
                to=_receiver,
                amount=self._mul_div_down_wad(collector_balance, fee),
            )
        )
        custody_transfers.append(
            Transfer(coin=coin, to=self, amount=max_value(uint256))
        )

    extcall self.fee_collector.transfer(fee_payouts)
    extcall self.fee_collector.transfer(custody_transfers)

    for coin: ERC20 in _coins:
        amount: uint256 = staticcall coin.balanceOf(self)
        self.lots[coin] = Lot(
            week=week,
            initial_amount=amount,
            native_remaining=amount,
            start_total=self.start_total,
            floor_total=self.floor_total,
            start=exchange_start,
            end=exchange_end,
        )
        self.cancelled_week[coin] = 0
        log LotSynced(
            token=coin,
            week=week,
            initial_amount=amount,
            start_total=self.start_total,
            floor_total=self.floor_total,
            start=exchange_start,
            end=exchange_end,
        )
        cow_auction._register_cow_order(coin.address, amount)


# Quotes and native settlement


@internal
@view
def _is_active(_from: ERC20, _lot: Lot, _timestamp: uint256) -> bool:
    if _from == self.target or not self._target_is_current():
        return False
    if _lot.week == 0 or _lot.week != self._week_at(_timestamp):
        return False
    if self.cancelled_week[_from] == _lot.week:
        return False
    if _timestamp < _lot.start or _timestamp >= _lot.end:
        return False
    return staticcall self.fee_collector.can_exchange([_from])


@internal
@view
def _native_available(_from: ERC20, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
    if not self._is_active(_from, lot, _timestamp):
        return 0
    balance: uint256 = staticcall _from.balanceOf(self)
    available_amount: uint256 = min(
        lot.initial_amount, min(lot.native_remaining, balance)
    )
    generation: uint256 = cow_auction.cow_generation
    if generation != 0 and cow_auction.registered_generation[_from.address] == generation:
        available_amount = min(
            available_amount, cow_auction._cow_allowance(_from.address)
        )
    return available_amount


@internal
@view
def _cow_available(_from: ERC20, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
    if not self._is_active(_from, lot, _timestamp):
        return 0
    generation: uint256 = cow_auction.cow_generation
    if generation == 0 or cow_auction.registered_generation[_from.address] != generation:
        return 0
    return min(
        lot.initial_amount,
        min(
            lot.native_remaining,
            min(
                staticcall _from.balanceOf(self),
                cow_auction._cow_allowance(_from.address),
            ),
        ),
    )


@internal
@view
def _lot_total_price(_lot: Lot, _timestamp: uint256) -> uint256:
    return auction_math.total_price(
        _lot.start_total,
        _lot.floor_total,
        self.decay_factor_ray,
        _timestamp - _lot.start,
        self.step_duration,
    )


@internal
@view
def _quote_unchecked(_from: ERC20, _amount: uint256, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
    return auction_math.proportional_payment(
        self._lot_total_price(lot, _timestamp), _amount, lot.initial_amount
    )


@override(yearn_auction)
@view
def _want() -> address:
    return self.target.address


@override(yearn_auction)
@view
def _available(_from: address) -> uint256:
    return self._native_available(ERC20(_from), block.timestamp)


@override(yearn_auction)
@view
def _price(_from: address) -> uint256:
    coin: ERC20 = ERC20(_from)
    amount: uint256 = self._native_available(coin, block.timestamp)
    if amount == 0:
        return 0
    lot: Lot = self.lots[coin]
    return auction_math.unit_quote_wad(
        self._lot_total_price(lot, block.timestamp), lot.initial_amount
    )


@override(yearn_auction)
@view
def _get_amount_needed(_from: address, _amount_to_take: uint256) -> uint256:
    coin: ERC20 = ERC20(_from)
    available_amount: uint256 = self._native_available(coin, block.timestamp)
    if available_amount == 0:
        return 0
    assert _amount_to_take <= available_amount, "Amount exceeds available"
    return self._quote_unchecked(coin, _amount_to_take, block.timestamp)


@internal
def _take_core(
    _from: ERC20,
    _max_amount: uint256,
    _receiver: address,
    _data: Bytes[MAX_CALLBACK_DATA],
) -> (uint256, uint256):
    assert _receiver != empty(address), "Zero receiver"
    available_amount: uint256 = self._native_available(_from, block.timestamp)
    amount_taken: uint256 = min(_max_amount, available_amount)
    assert amount_taken > 0, "Nothing available"

    lot: Lot = self.lots[_from]
    payment: uint256 = self._quote_unchecked(_from, amount_taken, block.timestamp)
    collector_before: uint256 = staticcall self.target.balanceOf(self.fee_collector.address)
    burner_before: uint256 = staticcall self.target.balanceOf(self)

    # Effects precede both token transfer and callback. A registered lot's finite
    # relayer allowance is the shared native/CoW budget; live balance alone cannot
    # distinguish a donation from unsold inventory after either settlement path.
    self.lots[_from].native_remaining = lot.native_remaining - amount_taken
    cow_auction._consume_cow_allowance(_from.address, amount_taken)

    assert extcall _from.transfer(_receiver, amount_taken, default_return_value=True)
    if len(_data) != 0:
        extcall AuctionTaker(_receiver).auctionTakeCallback(
            _from.address,
            msg.sender,
            amount_taken,
            payment,
            _data,
        )

    burner_after: uint256 = staticcall self.target.balanceOf(self)
    if burner_after > burner_before:
        assert extcall self.target.transfer(
            self.fee_collector.address,
            burner_after - burner_before,
            default_return_value=True,
        )

    collector_after_callback: uint256 = staticcall self.target.balanceOf(
        self.fee_collector.address
    )
    paid: uint256 = collector_after_callback - collector_before
    if paid < payment:
        assert extcall self.target.transferFrom(
            msg.sender,
            self.fee_collector.address,
            payment - paid,
            default_return_value=True,
        )

    assert staticcall self.target.balanceOf(
        self.fee_collector.address
    ) - collector_before >= payment, "Underpaid"
    remaining: uint256 = min(
        self.lots[_from].native_remaining,
        staticcall _from.balanceOf(self),
    )
    log Taken(
        token=_from,
        week=lot.week,
        caller=msg.sender,
        receiver=_receiver,
        amount_out=amount_taken,
        payment=payment,
        remaining_balance=remaining,
    )
    return amount_taken, payment


@override(yearn_auction)
def _take(
    _from: address,
    _max_amount: uint256,
    _taker_receiver: address,
    _data: Bytes[yearn_auction.MAX_CALLBACK_DATA],
) -> uint256:
    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(
        ERC20(_from), _max_amount, _taker_receiver, _data
    )
    return amount_taken


@external
def take_with_limits(
    _from: address,
    _max_amount: uint256,
    _min_amount: uint256,
    _max_payment: uint256,
    _receiver: address,
    _expected_week: uint256,
    _deadline: uint256,
    _data: Bytes[MAX_CALLBACK_DATA],
) -> (uint256, uint256):
    """@notice Take with explicit inclusion-time week, amount, payment, and deadline limits."""
    assert block.timestamp <= _deadline, "Deadline"
    assert self._week_at(block.timestamp) == _expected_week, "Wrong week"

    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(
        ERC20(_from), _max_amount, _receiver, _data
    )
    assert amount_taken >= _min_amount, "Insufficient amount"
    assert payment <= _max_payment, "Excessive payment"
    return amount_taken, payment


# ComposableCoW lifecycle and compile-time integration hooks


@external
def configure_cow(_composable_cow: address, _vault_relayer: address):
    """@notice Configure a new CoW generation while integration is disabled."""
    assert msg.sender == staticcall self.fee_collector.owner(), "Only owner"
    cow_auction._configure_cow(_composable_cow, _vault_relayer)


@external
def enable_cow():
    """@notice Enable order registration and validation for the current generation."""
    assert msg.sender == staticcall self.fee_collector.owner(), "Only owner"
    cow_auction._enable_cow()


@external
def disable_cow():
    """@notice Emergency-disable CoW without changing its current generation."""
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], "Only owner"
    cow_auction._disable_cow()


@external
def revoke_cow_allowances(
    _tokens: DynArray[ERC20, MAX_COINS], _retired_relayer: address
):
    """@notice Revoke approvals left to a relayer retired by reconfiguration."""
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], "Only owner"
    for coin: ERC20 in _tokens:
        cow_auction._revoke_cow_allowance(coin.address, _retired_relayer)


@external
@view
def created(_token: address) -> bool:
    """@notice Return whether token is registered for the current CoW generation."""
    generation: uint256 = cow_auction.cow_generation
    return generation != 0 and cow_auction.registered_generation[_token] == generation


@override(cow_auction)
@view
def _cow_target() -> address:
    return self.target.address


@override(cow_auction)
@view
def _cow_receiver() -> address:
    return self.fee_collector.address


@override(cow_auction)
@view
def _cow_app_data() -> bytes32:
    return self.app_data


@override(cow_auction)
@view
def _cow_order_validity() -> uint256:
    return self.cow_order_validity


@override(cow_auction)
@view
def _cow_next_poll(_token: address) -> uint256:
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    if block.timestamp < exchange_start:
        return exchange_start
    exchange_start, exchange_end = self._exchange_frame(block.timestamp + WEEK)
    return exchange_start


@override(cow_auction)
@view
def _cow_order_context(
    _token: address,
) -> (bool, uint256, uint256, uint256, uint256):
    coin: ERC20 = ERC20(_token)
    lot: Lot = self.lots[coin]
    active: bool = self._is_active(coin, lot, block.timestamp)
    available_amount: uint256 = 0
    if active:
        available_amount = self._cow_available(coin, block.timestamp)
    return active, available_amount, lot.initial_amount, lot.start, lot.end


@override(cow_auction)
@view
def _cow_quote(_token: address, _sell_amount: uint256, _timestamp: uint256) -> uint256:
    coin: ERC20 = ERC20(_token)
    lot: Lot = self.lots[coin]
    assert lot.start <= _timestamp and _timestamp < lot.end, "Bad quote time"
    assert _sell_amount <= lot.initial_amount, "Amount exceeds lot"
    return self._quote_unchecked(coin, _sell_amount, _timestamp)


@override(cow_auction)
@view
def _cow_signature_allowed() -> bool:
    # The module's transient global lock rejects callbacks during native take.
    return True


# Recovery and interface discovery


@external
def push_target() -> uint256:
    """@notice Permissionlessly return target tokens held by this burner."""
    amount: uint256 = staticcall self.target.balanceOf(self)
    if amount != 0:
        assert extcall self.target.transfer(
            self.fee_collector.address, amount, default_return_value=True
        )
    return amount


@external
def recover(_coins: DynArray[ERC20, MAX_COINS]):
    """@notice Return ERC-20 or native balances only to FeeCollector."""
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], "Only owner"

    recovery_week: uint256 = self._week_at(block.timestamp)
    for coin: ERC20 in _coins:
        amount: uint256 = 0
        if coin.address == ETH_ADDRESS:
            amount = self.balance
            if amount != 0:
                raw_call(self.fee_collector.address, b"", value=amount)
        else:
            amount = staticcall coin.balanceOf(self)
            # Persist cancellation for the FeeCollector auction week, including
            # its permissionless COLLECT frame. A later week's COLLECT clears it.
            self.cancelled_week[coin] = recovery_week
            self.lots[coin].native_remaining = 0
            if amount != 0:
                assert extcall coin.transfer(
                    self.fee_collector.address, amount, default_return_value=True
                )
        log Recovered(token=coin, amount=amount)


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    """@notice Return core interfaces and conditional-order interfaces only while enabled."""
    if _interface_id in [ERC165_INTERFACE_ID, BURNER_INTERFACE_ID]:
        return True
    return cow_auction._cow_supports_interface(_interface_id)
