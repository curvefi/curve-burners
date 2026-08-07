# pragma version 0.5.0a4
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title Dutch auction core module
@author Curve Finance
@license MIT
@notice Reusable Dutch auction: lot staging, geometric pricing, native take
        settlement, and the signed-order economic checks shared by every
        settlement rail.
@dev The importing contract owns authorization, calendar, and protocol wiring
     through compile-time hooks: the core stores no time bounds — the
     _auction_epoch hook maps timestamps onto monotone epoch numbers (0 is
     reserved for "never staged") and _epoch_bounds maps an epoch onto its
     active window. Inventory accounting is balance-based:
     available = min(initial_amount, native_remaining, balanceOf). There is no
     finite per-rail budget, so tokens donated after the snapshot can be
     sold along the same curve — always at or above the curve price and always
     in favor of the proceeds receiver; available never exceeds initial_amount.
     Router approvals and the ERC-1271 adapter dispatcher live in the sibling
     adapters module; staging reaches it through _sync_stage_approvals and the
     dispatcher validates order economics through _check_signed_order.
"""


from . import dutch_auction_math as auction_math
from . import adapter_types


error BadWant:
    pass


error BadReceiver:
    pass


error ZeroStartTotal:
    pass


error BadFloor:
    pass


error BadDecay:
    pass


error LotCancelled:
    pass


error AmountExceedsAvailable:
    pass


error AmountExceedsLot:
    pass


error ZeroReceiver:
    pass


error NothingAvailable:
    pass


error Underpaid:
    pass


error Deadline:
    pass


error WrongEpoch:
    pass


error InsufficientAmount:
    pass


error ExcessivePayment:
    pass


interface ERC20:
    def transfer(_receiver: address, _amount: uint256) -> bool: nonpayable
    def transferFrom(_sender: address, _receiver: address, _amount: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


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
    epoch: indexed(uint256)
    initial_amount: uint256
    start_total: uint256
    floor_total: uint256
    start: uint256
    end: uint256


event Taken:
    token: indexed(ERC20)
    epoch: indexed(uint256)
    caller: indexed(address)
    receiver: address
    amount_out: uint256
    payment: uint256
    remaining_balance: uint256


event EconomicsResynced:
    want: indexed(ERC20)
    start_total: uint256
    floor_total: uint256
    decay_factor_ray: uint256
    step_duration: uint256
    reconfigured_epoch: uint256


struct Lot:
    epoch: uint256
    initial_amount: uint256
    native_remaining: uint256
    start_total: uint256
    floor_total: uint256


MAX_CALLBACK_DATA: constant(uint256) = 8192
WAD: constant(uint256) = 10**18
RAY: constant(uint256) = 10**27

# Auction economics. Mutable only through _resync_economics: every lot pins
# its own start/floor snapshot at staging, and a want change fences out all
# lots of the resync epoch (their snapshots are denominated in the old want).
proceeds_receiver: public(immutable(address))
want: public(ERC20)
start_total: public(uint256)
floor_total: public(uint256)
decay_factor_ray: public(uint256)
step_duration: public(uint256)
# Lots staged in epochs <= reconfigured_epoch can never fill: their curve
# totals predate the last want change. 0 means the want was never changed.
reconfigured_epoch: public(uint256)

# Per-epoch lot accounting
lots: public(HashMap[ERC20, Lot])
cancelled_epoch: public(HashMap[ERC20, uint256])


@deploy
def __init__(
    _want: ERC20,
    _proceeds_receiver: address,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    """
    @notice Fix the auction economics.
    @dev Calendar-dependent parameter validation (decay reaching the floor
         within the frame, step count bounds) stays with the importing
         contract, which owns the auction calendar hooks.
    """
    assert _proceeds_receiver != empty(address), BadReceiver()
    self.proceeds_receiver = _proceeds_receiver
    self._set_economics(_want, _start_total, _floor_total, _decay_factor_ray, _step_duration)


@internal
def _set_economics(
    _want: ERC20,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    assert _want.address != empty(address), BadWant()
    assert _start_total > 0, ZeroStartTotal()
    assert 0 < _floor_total and _floor_total <= _start_total, BadFloor()
    assert RAY // 2 <= _decay_factor_ray and _decay_factor_ray < RAY, BadDecay()
    assert _step_duration > 0, auction_math.ZeroStep()

    self.want = _want
    self.start_total = _start_total
    self.floor_total = _floor_total
    self.decay_factor_ray = _decay_factor_ray
    self.step_duration = _step_duration


@internal
def _resync_economics(
    _want: ERC20,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _current_epoch: uint256,
):
    """
    @notice Re-pin the auction economics; authorization stays with the caller.
    @dev A want change fences out every lot of the current epoch: their curve
         snapshots are denominated in the old want, so filling them against
         the new one would misprice the inventory. Fills resume with the next
         epoch's staging. A same-want retune needs no fence — live lots keep
         their own valid snapshots and only future stagings pick up the new
         curve. The old want becomes a regular stageable token.
    """
    if _want != self.want:
        self.reconfigured_epoch = _current_epoch
    self._set_economics(_want, _start_total, _floor_total, _decay_factor_ray, _step_duration)
    log EconomicsResynced(
        want=_want,
        start_total=_start_total,
        floor_total=_floor_total,
        decay_factor_ray=_decay_factor_ray,
        step_duration=_step_duration,
        reconfigured_epoch=self.reconfigured_epoch,
    )


# Epoch cadence


@external
@view
def current_epoch() -> uint256:
    """@notice Return the auction epoch used by lot snapshots."""
    return self._auction_epoch(block.timestamp)


# Staging


@internal
@view
def _check_stageable(_token: ERC20, _epoch: uint256):
    assert _token != self.want, adapter_types.TargetToken()
    cancelled: uint256 = self.cancelled_epoch[_token]
    assert cancelled == 0 or cancelled != _epoch, LotCancelled()


@internal
def _stage_lot(_token: ERC20, _epoch: uint256) -> uint256:
    """
    @notice Snapshot the caller-custodied balance as the upcoming epoch's lot.
    @dev The importing contract must transfer custody first; the full current
         balance becomes initial_amount. The lot's active window is not
         stored: it is the calendar's _epoch_bounds(_epoch), owned by the
         importing contract. The _sync_stage_approvals hook lets the importer
         top settlement-rail allowances up so staging alone makes the lot
         pullable by enabled rails.
    @return The snapshot initial amount.
    """
    self._check_stageable(_token, _epoch)
    amount: uint256 = staticcall _token.balanceOf(self)
    self.lots[_token] = Lot(
        epoch=_epoch,
        initial_amount=amount,
        native_remaining=amount,
        start_total=self.start_total,
        floor_total=self.floor_total,
    )
    self.cancelled_epoch[_token] = 0
    self._sync_stage_approvals(_token.address)
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(_epoch)
    log LotSynced(
        token=_token,
        epoch=_epoch,
        initial_amount=amount,
        start_total=self.start_total,
        floor_total=self.floor_total,
        start=start,
        end=end,
    )
    return amount


# Quotes


@internal
@view
def _is_active(_from: ERC20, _lot: Lot, _timestamp: uint256) -> bool:
    if _from == self.want:
        return False
    if _lot.epoch == 0 or _lot.epoch != self._auction_epoch(_timestamp):
        return False
    # Lots staged up to a want resync carry snapshots in the old denomination.
    if _lot.epoch <= self.reconfigured_epoch:
        return False
    if self.cancelled_epoch[_from] == _lot.epoch:
        return False
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(_lot.epoch)
    if _timestamp < start or _timestamp >= end:
        return False
    return self._sellable(_from.address)


@internal
@view
def _available_unchecked(_from: ERC20, _lot: Lot) -> uint256:
    balance: uint256 = staticcall _from.balanceOf(self)
    return min(_lot.initial_amount, min(_lot.native_remaining, balance))


@internal
@view
def _available(_from: ERC20, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
    if not self._is_active(_from, lot, _timestamp):
        return 0
    return self._available_unchecked(_from, lot)


@internal
@view
def _lot_total_price(_lot: Lot, _timestamp: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(_lot.epoch)
    return auction_math.total_price(
        _lot.start_total,
        _lot.floor_total,
        self.decay_factor_ray,
        _timestamp - start,
        self.step_duration,
    )


@internal
@view
def _quote_unchecked(_from: ERC20, _amount: uint256, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
    return auction_math.proportional_payment(
        self._lot_total_price(lot, _timestamp), _amount, lot.initial_amount
    )


@external
@view
def available(_from: address) -> uint256:
    """
    @notice Return the amount of `_from` currently available to take.
    @param _from Token offered by the auction.
    """
    return self._available(ERC20(_from), block.timestamp)


@external
@view
def price(_from: address) -> uint256:
    """
    @notice Return the current WAD-scaled unit price for `_from`.
    @dev Upward-rounded target payment per 1e18 raw units of `_from` — not
         Yearn's decimal-normalized price; for sell tokens with non-18
         decimals the values are incompatible. getAmountNeeded is the
         canonical quote; do not multiply this price for payments.
    @param _from Token offered by the auction.
    """
    coin: ERC20 = ERC20(_from)
    if self._available(coin, block.timestamp) == 0:
        return 0
    lot: Lot = self.lots[coin]
    return auction_math.unit_quote_wad(
        self._lot_total_price(lot, block.timestamp), lot.initial_amount
    )


@external
@view
def getAmountNeeded(_from: address, amountToTake: uint256) -> uint256:
    """
    @notice Return the exact target-token payment required for an amount.
    @param _from Token offered by the auction.
    @param amountToTake Amount of `_from` to quote; must not exceed available.
    """
    coin: ERC20 = ERC20(_from)
    available_amount: uint256 = self._available(coin, block.timestamp)
    if available_amount == 0:
        return 0
    assert amountToTake <= available_amount, AmountExceedsAvailable()
    return self._quote_unchecked(coin, amountToTake, block.timestamp)


@external
@view
def quote(_token: address, _sell_amount: uint256) -> uint256:
    """
    @notice Quote a sell amount against the signed lot total.
    @dev Unlike getAmountNeeded, the bound is the lot's initial_amount, not the
         live availability: persistent partially fillable orders keep quoting
         their signed total after partial fills. Returns 0 while inactive.
    @param _token Token offered by the auction.
    @param _sell_amount Amount of `_token` to quote.
    """
    coin: ERC20 = ERC20(_token)
    lot: Lot = self.lots[coin]
    if not self._is_active(coin, lot, block.timestamp):
        return 0
    assert _sell_amount <= lot.initial_amount, AmountExceedsLot()
    return self._quote_unchecked(coin, _sell_amount, block.timestamp)


# Native settlement


@internal
def _take_core(
    _from: ERC20,
    _max_amount: uint256,
    _receiver: address,
    _data: Bytes[MAX_CALLBACK_DATA],
) -> (uint256, uint256):
    assert _receiver != empty(address), ZeroReceiver()
    available_amount: uint256 = self._available(_from, block.timestamp)
    amount_taken: uint256 = min(_max_amount, available_amount)
    assert amount_taken > 0, NothingAvailable()

    lot: Lot = self.lots[_from]
    payment: uint256 = self._quote_unchecked(_from, amount_taken, block.timestamp)
    collector_before: uint256 = staticcall self.want.balanceOf(self.proceeds_receiver)
    burner_before: uint256 = staticcall self.want.balanceOf(self)

    # Effects precede both the token transfer and the callback.
    self.lots[_from].native_remaining = lot.native_remaining - amount_taken

    assert extcall _from.transfer(_receiver, amount_taken, default_return_value=True)
    if len(_data) != 0:
        extcall AuctionTaker(_receiver).auctionTakeCallback(
            _from.address,
            msg.sender,
            amount_taken,
            payment,
            _data,
        )

    burner_after: uint256 = staticcall self.want.balanceOf(self)
    if burner_after > burner_before:
        assert extcall self.want.transfer(
            self.proceeds_receiver,
            burner_after - burner_before,
            default_return_value=True,
        )

    collector_after_callback: uint256 = staticcall self.want.balanceOf(self.proceeds_receiver)
    paid: uint256 = collector_after_callback - collector_before
    if paid < payment:
        assert extcall self.want.transferFrom(
            msg.sender,
            self.proceeds_receiver,
            payment - paid,
            default_return_value=True,
        )

    assert staticcall self.want.balanceOf(
        self.proceeds_receiver
    ) - collector_before >= payment, Underpaid()
    remaining: uint256 = min(
        self.lots[_from].native_remaining,
        staticcall _from.balanceOf(self),
    )
    log Taken(
        token=_from,
        epoch=lot.epoch,
        caller=msg.sender,
        receiver=_receiver,
        amount_out=amount_taken,
        payment=payment,
        remaining_balance=remaining,
    )
    return amount_taken, payment


@external
def take(
    _from: address,
    maxAmount: uint256,
    takerReceiver: address,
    data: Bytes[MAX_CALLBACK_DATA],
) -> uint256:
    """
    @notice Take up to `maxAmount` of an auctioned token.
    @param _from Token offered by the auction.
    @param maxAmount Maximum amount of `_from` to take.
    @param takerReceiver Receiver of the auctioned token.
    @param data Optional data forwarded to the receiver's auction callback.
    @return Amount of `_from` taken.
    """
    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(ERC20(_from), maxAmount, takerReceiver, data)
    return amount_taken


@external
def take_with_limits(
    _from: address,
    _max_amount: uint256,
    _min_amount: uint256,
    _max_payment: uint256,
    _receiver: address,
    _expected_epoch: uint256,
    _deadline: uint256,
    _data: Bytes[MAX_CALLBACK_DATA],
) -> (uint256, uint256):
    """@notice Take with explicit inclusion-time epoch, amount, payment, and deadline limits."""
    assert block.timestamp <= _deadline, Deadline()
    assert self._auction_epoch(block.timestamp) == _expected_epoch, WrongEpoch()

    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(ERC20(_from), _max_amount, _receiver, _data)
    assert amount_taken >= _min_amount, InsufficientAmount()
    assert payment <= _max_payment, ExcessivePayment()
    return amount_taken, payment


# Signed-order economics


@internal
@view
def _check_signed_order(
    _order: adapter_types.NormalizedOrder,
    _adapter_id: bytes4,
    _adapter_version: uint16,
) -> bool:
    """
    @notice Enforce every economic check the auction refuses to delegate to an
            adapter layer: lot activity, amounts, window, quote, and the
            protocol-hashed replay commitment.
    @dev Partial-fill totals are compared against the signed lot's
         initial_amount, not the live remainder: a persistent partially
         fillable order keeps its original total after partial fills.
    """
    token: ERC20 = ERC20(_order.sell_token)
    lot: Lot = self.lots[token]
    if not self._is_active(token, lot, block.timestamp):
        return False
    if _order.auction_epoch != lot.epoch:
        return False
    # sell_token != buy_token is implied: buy_token must equal want and
    # _is_active rejects want as a lot token.
    if _order.buy_token != self.want.address:
        return False
    if _order.receiver != self.proceeds_receiver:
        return False
    if _order.sell_amount == 0 or _order.sell_amount > lot.initial_amount:
        return False
    if self._available_unchecked(token, lot) == 0:
        return False
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = self._epoch_bounds(lot.epoch)
    if block.timestamp > _order.valid_to or _order.valid_to > lot_end:
        return False
    if _order.min_buy_amount < self._quote_unchecked(
        token, _order.sell_amount, block.timestamp
    ):
        return False

    # Replay protection: the protocol-hashed commitment binds chain, auction,
    # adapter identity/version, and the epoch's lot snapshot into the digest.
    context_hash: bytes32 = keccak256(
        abi_encode(
            chain.id,
            self,
            _adapter_id,
            _adapter_version,
            lot.epoch,
            _order.sell_token,
            self.want.address,
            self.proceeds_receiver,
            lot.initial_amount,
            lot_end,
        )
    )
    return _order.context_hash == context_hash


# Emergency lot cancellation


@internal
def _cancel_lot(_token: ERC20, _epoch: uint256):
    """
    @notice Cancel the token's lot for the given epoch, including its staging.
    @dev A later epoch's staging clears the cancellation.
    """
    self.cancelled_epoch[_token] = _epoch
    self.lots[_token].native_remaining = 0


# Compile-time integration hooks implemented by the importing contract.
# The cadence hook: maps a timestamp onto a monotone nonzero epoch number.
# The importing contract owns the schedule (weekly, daily, custom frames);
# the core only compares epochs for staleness and staging identity.
@internal
@view
@abstract
def _auction_epoch(_timestamp: uint256) -> uint256: ...


# The calendar hook: active window of an epoch. The core stores no time
# bounds — every window and elapsed-time computation goes through here, and
# independent contracts read the same data from the importer's public view.
@internal
@view
@abstract
def _epoch_bounds(_epoch: uint256) -> (uint256, uint256): ...


@internal
@view
@abstract
def _sellable(_token: address) -> bool: ...


# Staging-time settlement-rail approvals live with the importing contract's
# adapter layer; the core only reports that a lot was (re)staged.
@internal
@abstract
def _sync_stage_approvals(_token: address): ...
