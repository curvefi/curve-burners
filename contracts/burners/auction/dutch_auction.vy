# pragma version 0.5.0b1
# pragma nonreentrancy on
# SPDX-License-Identifier: MIT
"""
@title Dutch auction core module
@author Curve Finance
@license MIT
@notice Dutch auction: lot staging, geometric pricing, native take
        settlement, and the signed-order economic checks shared by every
        settlement rail.
@dev The importing contract owns authorization, calendar, and protocol wiring
     through compile-time hooks: the core stores no time bounds — a lot
     records only its staging timestamp, the _lot_start hook maps token and
     staging time onto the window start, and every window is auction_length
     long. Every time value, inside and out, is a plain timestamp. The curve
     is fully determined by start_total, floor_total, step_duration, and
     auction_length: an exponential decay from start to floor (equal time,
     equal percentage drop) that reaches the floor at the auction's last
     active step, evaluated through the logarithms prepared at
     configuration time. Inventory accounting is balance-based:
     available = min(initial_amount, balanceOf).
     There is no finite per-rail budget, so tokens donated after the snapshot
     can be sold along the same curve — always at or above the curve price
     and always in favor of the proceeds receiver; initial_amount pins the
     unit price and caps available. Executor approvals and the ERC-1271
     signature router live in the sibling adapters module; settlement
     adapters price their orders through the external check_order view.
"""

from ethereum.ercs import IERC20

from contracts.burners.auction import dutch_auction_math as auction_math
from contracts.interfaces import IDutchAuction
from contracts.utils import constants as c


error BadWant:
    pass


# The payment token is never sold: raised when staging it.
error WantNotSellable:
    pass


# Zero, or the auction itself: proceeds always leave the auction.
error BadReceiver:
    pass


# Zero, or above int256 (the curve takes its logarithm).
error BadStartTotal:
    pass


error BadFloor:
    pass


error BadAuctionLength:
    pass


# The auction holds fewer than one step: the curve would never decay.
error StepExceedsAuction:
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
    initial_amount: uint256
    start_total: uint256
    floor_total: uint256
    start: uint256
    end: uint256


event Taken:
    token: indexed(IERC20)
    caller: indexed(address)
    receiver: address
    amount_out: uint256
    payment: uint256
    remaining_balance: uint256


event EconomicsSet:
    want: indexed(IERC20)
    start_total: uint256
    floor_total: uint256
    step_duration: uint256


event ReceiverSet:
    receiver: indexed(address)


# Auction economics. Set only through _set_economics, which fences out every
# lot staged up to that block (configured_at): lots store no curve snapshot,
# so neither the curve nor the payment denomination ever changes under a
# staged lot.
# The configuration getters are reentrant on purpose: they only echo settings
# no take can change, and take() callbacks (takers sourcing the payment from
# the received tokens) need the payment token while the contract-wide lock is
# held. Everything derived from balances — quotes, availability, signed-order
# validation — stays locked during a take.
# Every lot trades for exactly this long from its window start; the
# calendar hook only places the start.
auction_length: public(immutable(uint256))
want: public(reentrant(IERC20))
receiver: public(reentrant(address))
start_total: public(reentrant(uint256))
floor_total: public(reentrant(uint256))
step_duration: public(reentrant(uint256))
# The prepared curve, derived from the parameters above and auction_length:
# decay_steps steps take the log price from log_start down by log_drop (both
# WAD). Internal: the public parameters determine it, so off-chain readers
# recompute it with the same arithmetic.
log_start: int256
log_drop: uint256
decay_steps: uint256
# Lots with staged_at <= configured_at can never fill: they were staged
# under the previous economics, so fills (and takers' in-flight
# transactions) could be priced on the old curve or in the old denomination.
# Transaction order inside a block is invisible here, so a lot staged in the
# block that set the economics (the deployment block included) counts as
# stale too: staging counts from the next block.
configured_at: uint256
# The lot record (declared in IDutchAuction for external readers): staged_at
# (0 = never staged) names the calendar window the lot trades in through
# _lot_start, initial_amount pins the unit price and caps availability.
# Reentrant like the configuration getters: a take never rewrites the lot,
# only staging does.
lots: public(reentrant(HashMap[IERC20, IDutchAuction.Lot]))


@deploy
def __init__(
    _want: IERC20,
    _receiver: address,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
    _auction_length: uint256,
):
    """
    @notice Fix the auction economics.
    @param _want Payment token; never sold.
    @param _receiver Receiver of every payment.
    @param _start_total Want price of a full lot at the window start.
    @param _floor_total Want price of a full lot at the window end.
    @param _step_duration Seconds per price step.
    @param _auction_length Length of every lot window in seconds; the curve
           reaches the floor at its last active second (windows exclude
           their end).
    """
    assert _auction_length > 0, BadAuctionLength()
    self.auction_length = _auction_length
    self._set_receiver(_receiver)
    self._set_economics(_want, _start_total, _floor_total, _step_duration)


@internal
def _set_receiver(_receiver: address):
    assert _receiver != empty(address) and _receiver != self, BadReceiver()
    self.receiver = _receiver
    log ReceiverSet(receiver=_receiver)


@internal
def _set_economics(
    _want: IERC20,
    _start_total: uint256,
    _floor_total: uint256,
    _step_duration: uint256,
):
    """
    @notice Pin the auction economics; authorization stays with the caller.
    @dev Every call — a same-want curve retune included — stales every lot
         staged up to and including this block (configured_at): the curve
         is read live, so this is what keeps a lot from ever being repriced
         or redenominated under takers' in-flight transactions. Lots must be
         staged from the next block on. A previous want becomes a regular
         stageable token.
    """
    assert _want.address != empty(address), BadWant()
    assert 0 < _start_total and _start_total <= convert(max_value(int256), uint256), (
        BadStartTotal()
    )
    assert 0 < _floor_total and _floor_total <= _start_total, BadFloor()
    assert _step_duration > 0, auction_math.ZeroStep()
    # The last active second of a window is auction_length - 1.
    steps: uint256 = (self.auction_length - 1) // _step_duration
    assert steps > 0, StepExceedsAuction()

    self.configured_at = block.timestamp
    self.want = _want
    self.start_total = _start_total
    self.floor_total = _floor_total
    self.step_duration = _step_duration
    self.decay_steps = steps
    self.log_start, self.log_drop = auction_math.curve_logs(_start_total, _floor_total)
    log EconomicsSet(
        want=_want,
        start_total=_start_total,
        floor_total=_floor_total,
        step_duration=_step_duration,
    )


# Staging


@internal
def _stage_lot(_token: IERC20) -> uint256:
    """
    @notice Snapshot the caller-custodied balance as a lot staged now.
    @dev The importing contract must transfer custody first; the full current
         balance becomes initial_amount. The lot's active window is not
         stored: it starts at the calendar's _lot_start(token, staged_at),
         owned by the importing contract, and lasts auction_length.
         Settlement-rail allowances are not staging's
         concern (see the adapters module's sync_executor_approvals).
    @return The snapshot initial amount.
    """
    assert _token != self.want, WantNotSellable()
    amount: uint256 = staticcall _token.balanceOf(self)
    self.lots[_token] = IDutchAuction.Lot(staged_at=block.timestamp, initial_amount=amount)
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._window(_token, block.timestamp)
    log LotStaged(
        token=_token,
        initial_amount=amount,
        start_total=self.start_total,
        floor_total=self.floor_total,
        start=start,
        end=end,
    )
    return amount


@internal
@view
def _window(_token: IERC20, _staged_at: uint256) -> (uint256, uint256):
    start: uint256 = self._lot_start(_token, _staged_at)
    return start, start + self.auction_length


# The calendar view, reentrant like the configuration getters: take()
# callbacks run under the contract-wide lock and must be able to call it, and
# it reads only what no take rewrites (the lot record and the calendar hook).
@external
@view
@reentrant
def window(_token: address, _timestamp: uint256 = 0) -> (uint256, uint256):
    """
    @notice The active window [start, end) of a `_token` lot: the one staged
            at `_timestamp`, or by default the token's current lot.
    @dev The window may lie in the future for a lot staged ahead of it, or in
         the past for a leftover; gate on available() rather than on the
         window alone (kill switches and the resync fence also stop fills).
    @param _token Lot token.
    @param _timestamp Staging time to evaluate; 0 (the default) reads the
           token's lot record and answers (0, 0) for a never-staged token.
    @return Window start (inclusive) and end (exclusive).
    """
    staged_at: uint256 = _timestamp
    if staged_at == 0:
        staged_at = self.lots[IERC20(_token)].staged_at
        if staged_at == 0:
            return 0, 0
    return self._window(IERC20(_token), staged_at)


# Quotes


@internal
@view
def _is_active(_from: IERC20, _lot: IDutchAuction.Lot, _timestamp: uint256) -> bool:
    if _from == self.want:
        return False
    # staged_at == 0 (never staged) is covered by the fence: 0 < configured_at.
    if _lot.staged_at <= self.configured_at:
        return False
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._window(_from, _lot.staged_at)
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
def _lot_total_price(_from: IERC20, _lot: IDutchAuction.Lot, _timestamp: uint256) -> uint256:
    start: uint256 = 0
    end: uint256 = 0
    start, end = self._window(_from, _lot.staged_at)
    return auction_math.total_price(
        self.start_total,
        self.floor_total,
        self.log_start,
        self.log_drop,
        self.decay_steps,
        _timestamp - start,
        self.step_duration,
    )


@internal
@view
def _quote_unchecked(_from: IERC20, _amount: uint256, _timestamp: uint256) -> uint256:
    lot: IDutchAuction.Lot = self.lots[_from]
    return auction_math.proportional_payment(
        self._lot_total_price(_from, lot, _timestamp), _amount, lot.initial_amount
    )


@external
@view
def available(_from: address) -> uint256:
    """
    @notice Return the amount of `_from` available to take right now.
    @param _from Token offered by the auction.
    @return Amount of `_from` available; 0 for an inactive lot.
    """
    return self._available(IERC20(_from), block.timestamp)


@external
@view
def price(_from: address, _ts: uint256 = block.timestamp) -> uint256:
    """
    @notice Return the WAD-precision unit price of `_from` at a timestamp:
            raw want units per 1e18 raw units of `_from`.
    @dev Rounded up like every quote; getAmountNeeded is the canonical payment
         quote. For the Yearn mapping (scaler = 1) see IYearnAuction. A
         non-current `_ts` moves only the window and the curve: the balance
         and the importer's sellability policy are read at the current
         block, so the projection is meaningful only while fills are
         currently allowed.
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
    @dev A non-current `_ts` moves only the window and the curve: the balance
         and the importer's sellability policy are read at the current
         block, so the projection is meaningful only while fills are
         currently allowed.
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
    @dev _deadline bounds the inclusion time; pair it with window() to keep
         the take inside the intended lot window.
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
    @notice The economic order check offered to settlement adapters: lot
            activity, receiver, amounts, window, and the live curve quote.
    @dev A helper, not a requirement: the router does not enforce it. An
         adapter proves that its protocol's digest matches these fields and
         delegates the economics here, so pricing rules exist in one place.
         Reverts with a typed error for an unfillable order and returns True
         for a fillable one, so an eth_call classifies any order by the
         error selector. Partial-fill totals are compared against the signed
         lot's initial_amount, not the live remainder. Locked like every
         other quote view.
    @param _sell_token Lot token the order sells.
    @param _buy_token Token the order buys; must be want.
    @param _receiver Receiver of the buy token; must be the auction's receiver.
    @param _sell_amount Total sell amount of the order; at most the lot's
           initial_amount.
    @param _min_buy_amount Minimum buy amount of the order; at least the live
           quote for _sell_amount.
    @param _valid_to Last timestamp (inclusive) the order is valid; at most the
           window end.
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
    lot_start, lot_end = self._window(token, lot.staged_at)
    assert block.timestamp <= _valid_to and _valid_to <= lot_end, BadValidTo()
    assert _min_buy_amount >= self._quote_unchecked(token, _sell_amount, block.timestamp), (
        BadBuyAmount()
    )
    return True


# Compile-time integration hooks implemented by the importing contract.
# The calendar hook: the window start of a `_token` lot staged at
# `_staged_at`; the window then lasts auction_length. The importing contract
# owns the schedule (weekly, daily, per-token slots); the core stores no time
# bounds — every window and elapsed-time computation goes through here, and
# window() publishes it. A start the lot never reaches simply leaves it
# inactive.
@internal
@view
@abstract
def _lot_start(_token: IERC20, _staged_at: uint256) -> uint256: ...


# The policy hook: whether a token may be sold right now (kill switches,
# target migration). The core already excludes the want token.
@internal
@view
@abstract
def _sellable(_token: address) -> bool: ...
