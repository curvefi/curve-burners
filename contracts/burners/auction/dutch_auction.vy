# pragma version 0.5.0b1
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
     available = min(initial_amount, balanceOf). There is no finite per-rail
     budget, so tokens donated after the snapshot can be sold along the same
     curve — always at or above the curve price and always in favor of the
     proceeds receiver; initial_amount pins the unit price and caps available.
     Router approvals and the ERC-1271 signature router live in the sibling
     adapters module; staging reaches it through _sync_stage_approvals, and
     settlement verifiers price their orders through the external check_order
     view.
"""


from ethereum.ercs import IERC20

from . import dutch_auction_math as auction_math


error BadWant:
    pass


# The payment token never becomes inventory: raised when staging the want
# token here and by the adapters layer's allowance sync. Declared in the core
# (error names are globally unique per compilation unit) so the settlement
# plumbing depends on the core, never the other way around.
error TargetToken:
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


error AmountExceedsLot:
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


# Callback data is unbounded (Bytes[INF]) and forwarded verbatim — the taker
# picks its own bound; unbounded sequence types require the importing contract
# to compile with `# pragma experimental-codegen`.
interface AuctionTaker:
    def auctionTakeCallback(
        _from: address,
        _sender: address,
        _amount_taken: uint256,
        _amount_needed: uint256,
        _data: Bytes[INF],
    ): nonpayable


event LotSynced:
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


struct Lot:
    epoch: uint256
    initial_amount: uint256


# Auction economics. Mutable only through _resync_economics: lots store no
# curve snapshot, so a retune reprices live lots immediately; a want change
# under an open window additionally fences out the current epoch's lots so
# the payment denomination never switches while fills can run.
proceeds_receiver: public(immutable(address))
# Private storage so the module can export an explicit reentrant want() view.
want_token: IERC20
start_total: public(uint256)
floor_total: public(uint256)
decay_factor_ray: public(uint256)
step_duration: public(uint256)
# Lots staged in epochs <= reconfigured_epoch can never fill: their epoch's
# window had already opened when the want last changed, so fills (and takers'
# in-flight transactions) could be priced in the old denomination. 0 means
# the want was never changed.
reconfigured_epoch: public(uint256)

# Per-epoch lot accounting
lots: public(HashMap[IERC20, Lot])


@deploy
def __init__(
    _want: IERC20,
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

    self.want_token = _want
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
    _current_epoch: uint256,
):
    """
    @notice Re-pin the auction economics; authorization stays with the caller.
    @dev A want change while the current epoch's window is open fences out
         every lot of that epoch: fills (and takers' in-flight transactions)
         may already be priced in the old denomination, so it must not switch
         under a live window — fills resume with the next epoch's staging.
         Before the window opens nothing has traded yet, so the fence stops at
         the previous epoch and lots staged this epoch trade against the
         freshly pinned curve — a resync early in the epoch loses no time.
         A same-want retune needs no fence and takes effect immediately — the
         curve is read live, so live lots reprice at once. The old want
         becomes a regular stageable token.
    """
    if _want != self.want_token:
        epoch_start: uint256 = 0
        epoch_end: uint256 = 0
        epoch_start, epoch_end = self._epoch_bounds(_current_epoch)
        if block.timestamp >= epoch_start:
            self.reconfigured_epoch = _current_epoch
        else:
            self.reconfigured_epoch = _current_epoch - 1
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


# want() is reentrant on purpose: it only echoes configuration, and take()
# callbacks (takers sourcing the payment from the received tokens) need the
# payment token while the contract-wide lock is held. Everything else —
# quotes, availability, signed-order validation — stays locked during a take.
@external
@view
@reentrant
def want() -> address:
    """@notice Return the target token accepted as payment."""
    return self.want_token.address


# Staging


@internal
@view
def _check_stageable(_token: IERC20):
    assert _token != self.want_token, TargetToken()


@internal
def _stage_lot(_token: IERC20, _epoch: uint256) -> uint256:
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
    self._check_stageable(_token)
    amount: uint256 = staticcall _token.balanceOf(self)
    self.lots[_token] = Lot(epoch=_epoch, initial_amount=amount)
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
def _is_active(_from: IERC20, _lot: Lot, _timestamp: uint256) -> bool:
    if _from == self.want_token:
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
def _available_unchecked(_from: IERC20, _lot: Lot) -> uint256:
    balance: uint256 = staticcall _from.balanceOf(self)
    return min(_lot.initial_amount, balance)


@internal
@view
def _available(_from: IERC20, _timestamp: uint256) -> uint256:
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
        self.start_total,
        self.floor_total,
        self.decay_factor_ray,
        _timestamp - start,
        self.step_duration,
    )


@internal
@view
def _quote_unchecked(_from: IERC20, _amount: uint256, _timestamp: uint256) -> uint256:
    lot: Lot = self.lots[_from]
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
    """
    return self._available(IERC20(_from), _ts)


@external
@view
def price(_from: address, _ts: uint256 = block.timestamp) -> uint256:
    """
    @notice Return the WAD-scaled unit price for `_from` at a timestamp.
    @dev Upward-rounded target payment per 1e18 raw units of `_from` — not
         Yearn's decimal-normalized price; for sell tokens with non-18
         decimals the values are incompatible. getAmountNeeded is the
         canonical quote; do not multiply this price for payments.
    @param _from Token offered by the auction.
    @param _ts Timestamp to evaluate at; defaults to now.
    """
    coin: IERC20 = IERC20(_from)
    if self._available(coin, _ts) == 0:
        return 0
    lot: Lot = self.lots[coin]
    return auction_math.unit_quote_wad(
        self._lot_total_price(lot, _ts), lot.initial_amount
    )


@external
@view
def getAmountNeeded(
    _from: address, amountToTake: uint256, _ts: uint256 = block.timestamp
) -> uint256:
    """
    @notice Return the exact target-token payment required for an amount.
    @param _from Token offered by the auction.
    @param amountToTake Amount of `_from` to quote; must not exceed available.
    @param _ts Timestamp to evaluate at; defaults to now.
    """
    coin: IERC20 = IERC20(_from)
    available_amount: uint256 = self._available(coin, _ts)
    if available_amount == 0:
        return 0
    assert amountToTake <= available_amount, AmountExceedsAvailable()
    return self._quote_unchecked(coin, amountToTake, _ts)


@external
@view
def quote(_token: address, _sell_amount: uint256, _ts: uint256 = block.timestamp) -> uint256:
    """
    @notice Quote a sell amount against the signed lot total.
    @dev Unlike getAmountNeeded, the bound is the lot's initial_amount, not the
         live availability: persistent partially fillable orders keep quoting
         their signed total after partial fills. Returns 0 while inactive.
    @param _token Token offered by the auction.
    @param _sell_amount Amount of `_token` to quote.
    @param _ts Timestamp to evaluate at; defaults to now.
    """
    coin: IERC20 = IERC20(_token)
    lot: Lot = self.lots[coin]
    if not self._is_active(coin, lot, _ts):
        return 0
    assert _sell_amount <= lot.initial_amount, AmountExceedsLot()
    return self._quote_unchecked(coin, _sell_amount, _ts)


# Native settlement


@internal
def _take_core(
    _from: IERC20,
    _max_amount: uint256,
    _receiver: address,
    _data: Bytes[INF],
) -> (uint256, uint256):
    assert _receiver != empty(address), ZeroReceiver()
    available_amount: uint256 = self._available(_from, block.timestamp)
    amount_taken: uint256 = min(_max_amount, available_amount)
    assert amount_taken > 0, NothingAvailable()

    lot: Lot = self.lots[_from]
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
    assert extcall self.want_token.transferFrom(
        msg.sender,
        self.proceeds_receiver,
        payment,
        default_return_value=True,
    )

    remaining: uint256 = min(
        lot.initial_amount,
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
    data: Bytes[INF],
) -> uint256:
    """
    @notice Take up to `maxAmount` of an auctioned token.
    @dev The full quoted payment is pulled from the caller's want allowance
         after the optional callback; the callback lets the taker source the
         funds from the received tokens first.
    @param _from Token offered by the auction.
    @param maxAmount Maximum amount of `_from` to take.
    @param takerReceiver Receiver of the auctioned token.
    @param data Optional data forwarded to the receiver's auction callback.
    @return Amount of `_from` taken.
    """
    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(IERC20(_from), maxAmount, takerReceiver, data)
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
    """
    assert block.timestamp <= _deadline, Deadline()

    amount_taken: uint256 = 0
    payment: uint256 = 0
    amount_taken, payment = self._take_core(IERC20(_from), _max_amount, _receiver, _data)
    assert amount_taken >= _min_amount, InsufficientAmount()
    assert payment <= _max_payment, ExcessivePayment()
    return amount_taken, payment


# Signed-order economics


# The returned reason strings are a FROZEN cross-contract ABI: CowAdapter
# reverts them verbatim as OrderNotValid(reason) and the off-chain watchtower
# classifies orders by them. Renaming one is a silent breaking change no
# compiler will catch — never edit an existing reason, only add new ones.
@external
@view
def check_order(
    _sell_token: address,
    _buy_token: address,
    _receiver: address,
    _sell_amount: uint256,
    _min_buy_amount: uint256,
    _valid_to: uint256,
) -> String[32]:
    """
    @notice The shared economic order check every settlement adapter runs: lot
            activity, receiver, amounts, window, and the live curve quote.
    @dev Verifiers prove that their protocol's digest matches these fields and
         delegate the economics here, so pricing rules exist in exactly one
         place. Returns an empty string for a fillable order and a
         watchtower-canonical reason otherwise, letting CoW-facing adapters
         revert OrderNotValid(reason) verbatim. Partial-fill totals are
         compared against the signed lot's initial_amount, not the live
         remainder: a persistent partially fillable order keeps its original
         total after partial fills. Replay needs no commitment beyond these
         checks: nothing signs on the auction's behalf, so any payload
         passing them settles at or above the live curve price. The
         contract-wide nonreentrancy lock rejects validation during a native
         take callback.
    """
    token: IERC20 = IERC20(_sell_token)
    lot: Lot = self.lots[token]
    # sell != buy is implied: the buy token must equal want and _is_active
    # rejects want as a lot token.
    if _buy_token != self.want_token.address:
        return "BadToken"
    if _receiver != self.proceeds_receiver:
        return "BadReceiver"
    if not self._is_active(token, lot, block.timestamp):
        return "NotAllowed"
    # The availability term also establishes initial_amount > 0 before the
    # quote divides by it.
    if self._available_unchecked(token, lot) == 0:
        return "ZeroBalance"
    if _sell_amount == 0 or _sell_amount > lot.initial_amount:
        return "BadSellAmount"
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = self._epoch_bounds(lot.epoch)
    if block.timestamp > _valid_to or _valid_to > lot_end:
        return "BadValidTo"
    if _min_buy_amount < self._quote_unchecked(token, _sell_amount, block.timestamp):
        return "BadBuyAmount"
    return ""


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
