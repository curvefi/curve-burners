# pragma version 0.5.0b1
# pragma nonreentrancy on
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
     available = min(initial_amount, balanceOf). There is no finite per-rail
     budget, so tokens donated after the snapshot can be sold along the same
     curve — always at or above the curve price and always in favor of the
     proceeds receiver; initial_amount pins the unit price and caps available.
     Executor approvals and the ERC-1271 signature router live in the sibling
     adapters module; settlement adapters price their orders through the
     external check_order view.
"""


from ethereum.ercs import IERC20

from contracts.burners.auction import dutch_auction_math as auction_math
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadWant:
    pass


# The payment token never becomes inventory; raised when staging it and, via
# the adapters module's _pre_approve hook, when approving it to an executor.
error WantNotSellable:
    pass


error BadReceiver:
    pass


error ZeroStartTotal:
    pass


error BadFloor:
    pass


error BadDecay:
    pass


error AmountExceedsAvailable:
    pass


error ZeroReceiver:
    pass


error NothingAvailable:
    pass


error Deadline:
    pass


error InsufficientAmount:
    pass


error ExcessivePayment:
    pass


error DecayMissesFloor:
    pass


# Signed-order checks (check_order).
error BadBuyToken:
    pass


error LotInactive:
    pass


error BadSellAmount:
    pass


error BadValidTo:
    pass


error BadBuyAmount:
    pass


interface AuctionTaker:
    def auctionTakeCallback(
        _from: address,
        _sender: address,
        _amount_taken: uint256,
        _amount_needed: uint256,
        _data: Bytes[INF],
    ): nonpayable


event LotStaged:
    token: indexed(IERC20)
    epoch: indexed(uint256)
    initial_amount: uint256
    start_total: uint256
    floor_total: uint256
    start: uint256
    end: uint256


event Taken:
    token: indexed(IERC20)
    epoch: indexed(uint256)
    caller: indexed(address)
    receiver: address
    amount_out: uint256
    payment: uint256
    remaining_balance: uint256


event EconomicsResynced:
    want: indexed(IERC20)
    start_total: uint256
    floor_total: uint256
    decay_factor_ray: uint256
    step_duration: uint256
    reconfigured_epoch: uint256


# Auction economics. Mutable only through _resync_economics: lots store no
# curve snapshot, so a retune reprices live lots immediately; a want change
# under an open window additionally fences out the current epoch's lots so
# the payment denomination never switches while fills can run.
receiver: public(immutable(address))
# The want() getter is reentrant on purpose: it only echoes configuration, and
# take() callbacks (takers sourcing the payment from the received tokens) need
# the payment token while the contract-wide lock is held. Everything else —
# quotes, availability, signed-order validation — stays locked during a take.
want: public(reentrant(IERC20))
start_total: public(uint256)
floor_total: public(uint256)
decay_factor_ray: public(uint256)
step_duration: public(uint256)
# Lots staged in epochs <= reconfigured_epoch can never fill: their epoch's
# window had already opened when the want last changed, so fills (and takers'
# in-flight transactions) could be priced in the old denomination. 0 means
# the want was never changed.
reconfigured_epoch: public(uint256)
# The lot record is declared in IDutchAuction so peer contracts can read lots()
# without importing this stateful module.
lots: public(HashMap[IERC20, IDutchAuction.Lot])


@deploy
def __init__(
    _want: IERC20,
    _receiver: address,
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
    assert _receiver != empty(address), BadReceiver()
    self.receiver = _receiver
    self._set_economics(_want, _start_total, _floor_total, _decay_factor_ray, _step_duration)


@internal
def _set_economics(
    _want: IERC20,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    assert _want.address != empty(address), BadWant()
    assert _start_total > 0, ZeroStartTotal()
    assert 0 < _floor_total and _floor_total <= _start_total, BadFloor()
    assert 0 < _decay_factor_ray and _decay_factor_ray < auction_math.RAY, BadDecay()
    assert _step_duration > 0, auction_math.ZeroStep()

    self.want = _want
    self.start_total = _start_total
    self.floor_total = _floor_total
    self.decay_factor_ray = _decay_factor_ray
    self.step_duration = _step_duration


@internal
@pure
def _validate_curve_fits(
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _active_elapsed: uint256,
):
    """
    @notice Validate that the curve decays all the way to the floor within an
            active window.
    @dev total_price clamps at floor_total from below, so equality at the last
         active second means "the decayed total has reached (or crossed) the
         floor" — not a knife-edge match. A curve that never gets there would
         leave the floor unreachable and the parameter meaningless.
    @param _active_elapsed Largest elapsed time an active lot can reach: the
           window length minus one (windows exclude their end timestamp).
    """
    assert auction_math.total_price(
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _active_elapsed,
        _step_duration,
    ) == _floor_total, DecayMissesFloor()


@internal
def _resync_economics(
    _want: IERC20,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    """
    @notice Re-pin the auction economics; authorization stays with the caller.
    @dev A want change sets the fence (reconfigured_epoch) to the current
         epoch once its window has opened, and to the previous epoch before
         that. A same-want retune sets no fence; the curve is read live, so
         live lots reprice at once. The old want becomes a regular stageable
         token.
    """
    if _want != self.want:
        current_epoch: uint256 = self._auction_epoch(block.timestamp)
        epoch_start: uint256 = 0
        epoch_end: uint256 = 0
        epoch_start, epoch_end = self._epoch_bounds(current_epoch)
        if block.timestamp >= epoch_start:
            self.reconfigured_epoch = current_epoch
        else:
            self.reconfigured_epoch = current_epoch - 1
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
def _check_stageable(_token: IERC20):
    assert _token != self.want, WantNotSellable()


@internal
def _stage_lot(_token: IERC20) -> uint256:
    """
    @notice Snapshot the caller-custodied balance as the current epoch's lot.
    @dev The importing contract must transfer custody first; the full current
         balance becomes initial_amount. The lot's active window is not
         stored: it is the calendar's _epoch_bounds(epoch), owned by the
         importing contract. Settlement-rail allowances are not staging's
         concern (see the adapters module's sync_executor_approvals).
    @return The snapshot initial amount.
    """
    self._check_stageable(_token)
    epoch: uint256 = self._auction_epoch(block.timestamp)
    amount: uint256 = staticcall _token.balanceOf(self)
    self.lots[_token] = IDutchAuction.Lot(epoch=epoch, initial_amount=amount)
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(epoch)
    log LotStaged(
        token=_token,
        epoch=epoch,
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
def _is_active(_from: IERC20, _lot: IDutchAuction.Lot, _timestamp: uint256) -> bool:
    if _from == self.want:
        return False
    if _lot.epoch == 0 or _lot.epoch != self._auction_epoch(_timestamp):
        return False
    # Lots whose epoch window had opened by the last want change stay dead:
    # the payment denomination never switches under a live window.
    if _lot.epoch <= self.reconfigured_epoch:
        return False
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(_lot.epoch)
    if _timestamp < start or _timestamp >= end:
        return False
    return self._sellable(_from.address)


@internal
@view
def _available_unchecked(_from: IERC20, _lot: IDutchAuction.Lot) -> uint256:
    balance: uint256 = staticcall _from.balanceOf(self)
    return min(_lot.initial_amount, balance)


@internal
@view
def _available(_from: IERC20, _timestamp: uint256) -> uint256:
    lot: IDutchAuction.Lot = self.lots[_from]
    if not self._is_active(_from, lot, _timestamp):
        return 0
    return self._available_unchecked(_from, lot)


@internal
@view
def _lot_total_price(_lot: IDutchAuction.Lot, _timestamp: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(_lot.epoch)
    return auction_math.total_price(
        self.start_total,
        self.floor_total,
        self.decay_factor_ray,
        _timestamp - start,
        self.step_duration,
    )


@internal
@view
def _quote_unchecked(_from: IERC20, _amount: uint256, _timestamp: uint256) -> uint256:
    lot: IDutchAuction.Lot = self.lots[_from]
    return auction_math.proportional_payment(
        self._lot_total_price(lot, _timestamp), _amount, lot.initial_amount
    )


@external
@view
def available(_from: address, _ts: uint256 = block.timestamp) -> uint256:
    """
    @notice Return the amount of `_from` available to take at a timestamp.
    @dev Balances are read live: for a non-current `_ts` the result assumes
         today's balance, so historical answers are approximate.
    @param _from Token offered by the auction.
    @param _ts Timestamp to evaluate at; defaults to now.
    @return Amount of `_from` available; 0 for an inactive lot.
    """
    return self._available(IERC20(_from), _ts)


@external
@view
def price(_from: address, _ts: uint256 = block.timestamp) -> uint256:
    """
    @notice Return the WAD-precision unit price of `_from` at a timestamp:
            raw want units per 1e18 raw units of `_from`.
    @dev Rounded up like every quote; getAmountNeeded is the canonical payment
         quote. For the Yearn mapping (scaler = 1) see IDutchAuction.
    @param _from Token offered by the auction.
    @param _ts Timestamp to evaluate at; defaults to now.
    @return Unit price in want per 1e18 raw units; 0 for an inactive lot.
    """
    coin: IERC20 = IERC20(_from)
    if self._available(coin, _ts) == 0:
        return 0
    return self._quote_unchecked(coin, c.WAD, _ts)


@external
@view
def getAmountNeeded(
    _from: address,
    amountToTake: uint256 = max_value(uint256),
    _ts: uint256 = block.timestamp,
) -> uint256:
    """
    @notice Return the exact target-token payment required for an amount.
    @param _from Token offered by the auction.
    @param amountToTake Amount of `_from` to quote; must not exceed available.
           max_value(uint256) quotes everything available, reproducing Yearn's
           single-argument overload.
    @param _ts Timestamp to evaluate at; defaults to now.
    @return Want payment for `amountToTake`; 0 for an inactive lot.
    """
    coin: IERC20 = IERC20(_from)
    available_amount: uint256 = self._available(coin, _ts)
    if available_amount == 0:
        return 0
    amount: uint256 = amountToTake
    if amount == max_value(uint256):
        amount = available_amount
    assert amount <= available_amount, AmountExceedsAvailable()
    return self._quote_unchecked(coin, amount, _ts)


# Yearn-compatible views


@external
@view
def isActive(_from: address) -> bool:
    """@notice Whether `_from` can be taken right now (Yearn ABI)."""
    return self._available(IERC20(_from), block.timestamp) > 0


@external
@view
def auctionLength() -> uint256:
    """
    @notice Length of an auction window (Yearn ABI). A constant in Yearn; here
            the current epoch's calendar window.
    """
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(self._auction_epoch(block.timestamp))
    return end - start


@external
@view
def auctions(_from: address) -> IDutchAuction.AuctionInfo:
    """
    @notice The lot in Yearn's record shape (Yearn ABI): kicked is the lot
            window's start, initialAvailable the snapshot. Zeroed for a
            never-staged token, like Yearn's unenabled auction.
    @dev scaler is 1 and kicked may lie in the future; see IDutchAuction.
    """
    lot: IDutchAuction.Lot = self.lots[IERC20(_from)]
    if lot.epoch == 0:
        return empty(IDutchAuction.AuctionInfo)
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._epoch_bounds(lot.epoch)
    return IDutchAuction.AuctionInfo(
        kicked=convert(start, uint64),
        scaler=1,
        initialAvailable=convert(lot.initial_amount, uint128),
    )


# Native settlement


@internal
def _take(
    _from: IERC20,
    _max_amount: uint256,
    _receiver: address,
    _data: Bytes[INF],
) -> (uint256, uint256):
    assert _receiver != empty(address), ZeroReceiver()
    available_amount: uint256 = self._available(_from, block.timestamp)
    amount_taken: uint256 = min(_max_amount, available_amount)
    assert amount_taken > 0, NothingAvailable()

    lot: IDutchAuction.Lot = self.lots[_from]
    payment: uint256 = self._quote_unchecked(_from, amount_taken, block.timestamp)

    assert extcall _from.transfer(_receiver, amount_taken, default_return_value=True)
    if len(_data) != 0:
        extcall AuctionTaker(_receiver).auctionTakeCallback(
            _from.address,
            msg.sender,
            amount_taken,
            payment,
            _data,
        )

    # The full quote is pulled from the caller's allowance after the callback.
    # Crediting balance deltas at the proceeds receiver instead would let a
    # callback route unrelated third-party inflows (any permissionless push
    # toward the receiver) into its own bill.
    assert extcall self.want.transferFrom(
        msg.sender,
        self.receiver,
        payment,
        default_return_value=True,
    )

    remaining: uint256 = self._available_unchecked(_from, lot)
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
    maxAmount: uint256 = max_value(uint256),
    takerReceiver: address = msg.sender,
    data: Bytes[INF] = b"",
) -> uint256:
    """
    @notice Take up to `maxAmount` of an auctioned token.
    @dev The full quoted payment is pulled from the caller's want allowance
         after the optional callback; the callback lets the taker source the
         funds from the received tokens first. The defaults reproduce Yearn's
         shortened take overloads: everything available, to the caller, with
         no callback.
    @param _from Token offered by the auction.
    @param maxAmount Maximum amount of `_from` to take; defaults to all.
    @param takerReceiver Receiver of the auctioned token; defaults to the caller.
    @param data Optional data forwarded to the receiver's auction callback.
    @return Amount of `_from` taken.
    """
    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take(IERC20(_from), maxAmount, takerReceiver, data)
    return amount_taken


@external
def take_with_limits(
    _from: address,
    _max_amount: uint256,
    _min_amount: uint256,
    _max_payment: uint256,
    _receiver: address,
    _deadline: uint256,
    _data: Bytes[INF],
) -> (uint256, uint256):
    """
    @notice Take with explicit inclusion-time amount, payment, and deadline limits.
    @dev A deadline before the next epoch's window also pins the epoch: at any
         timestamp exactly one epoch is active, so no separate epoch limit is
         needed to protect against filling a restaged lot's fresh curve.
    @param _from Token offered by the auction.
    @param _max_amount Maximum amount of `_from` to take.
    @param _min_amount Minimum amount of `_from` taken; reverts below it.
    @param _max_payment Maximum want payment; reverts above it.
    @param _receiver Receiver of the auctioned token.
    @param _deadline Last timestamp (inclusive) at which the take may execute.
    @param _data Optional data forwarded to the receiver's auction callback.
    @return Amount of `_from` taken and the want payment pulled.
    """
    assert block.timestamp <= _deadline, Deadline()

    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take(IERC20(_from), _max_amount, _receiver, _data)
    assert amount_taken >= _min_amount, InsufficientAmount()
    assert payment <= _max_payment, ExcessivePayment()
    return amount_taken, payment


# Signed-order economics


@external
@view
def check_order(
    _sell_token: address,
    _buy_token: address,
    _receiver: address,
    _sell_amount: uint256,
    _min_buy_amount: uint256,
    _valid_to: uint256,
) -> bool:
    """
    @notice The shared economic order check every settlement adapter runs: lot
            activity, receiver, amounts, window, and the live curve quote.
    @dev Adapters prove that their protocol's digest matches these fields and
         delegate the economics here, so pricing rules exist in exactly one
         place. Reverts with a typed error for an unfillable order and
         returns True for a fillable one, so an eth_call classifies any order
         by the error selector. Partial-fill totals are compared against the
         signed lot's initial_amount, not the live remainder. Locked like
         every other quote view.
    @return True for a fillable order (unfillable orders revert).
    """
    token: IERC20 = IERC20(_sell_token)
    lot: IDutchAuction.Lot = self.lots[token]
    # sell != buy is implied: the buy token must equal want and _is_active
    # rejects want as a lot token.
    assert _buy_token == self.want.address, BadBuyToken()
    assert _receiver == self.receiver, BadReceiver()
    assert self._is_active(token, lot, block.timestamp), LotInactive()
    # The availability term also establishes initial_amount > 0 before the
    # quote divides by it.
    assert self._available_unchecked(token, lot) > 0, NothingAvailable()
    assert 0 < _sell_amount and _sell_amount <= lot.initial_amount, BadSellAmount()
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = self._epoch_bounds(lot.epoch)
    assert block.timestamp <= _valid_to and _valid_to <= lot_end, BadValidTo()
    assert _min_buy_amount >= self._quote_unchecked(token, _sell_amount, block.timestamp), (
        BadBuyAmount()
    )
    return True


# Compile-time integration hooks implemented by the importing contract.
# The cadence hook: maps a timestamp onto a nonzero epoch id. Ids must be
# strictly increasing integers over time: the want fence names the previous
# epoch as `current_epoch - 1`. The importing contract owns the schedule
# (weekly, daily, custom frames); the core only compares epochs for
# staleness and staging identity.
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


# The policy hook: whether a token may be sold right now (kill switches,
# target migration). The core already excludes the want token.
@internal
@view
@abstract
def _sellable(_token: address) -> bool: ...
