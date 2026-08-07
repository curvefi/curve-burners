# pragma version 0.5.0a4
# pragma optimize codesize
# Venom backend is required to fit EIP-170: the legacy pipeline emits ~27.4kB
# of runtime code for the combined core + watchtower + burner surface.
# pragma experimental-codegen
# pragma nonreentrancy on
# pragma evm-version cancun
# SPDX-License-Identifier: MIT
"""
@title DutchAuctionBurner
@author Curve Finance
@license MIT
@notice Thin FeeCollector wrapper around the Dutch auction core: stages weekly
        fee-token lots and sells them along a geometric Dutch curve through
        native takes, the embedded ComposableCoW watchtower, and registry
        adapters validated by the adapter layer's ERC-1271 dispatcher.
@custom:kill FeeCollector kill masks stop all fills. The FeeCollector owner or
             emergency owner can disable CoW, disable individual adapters, and
             recover inventory — only back to FeeCollector. Native take and
             push_target stay permissionless while a lot is alive. Disabling a
             rail does not clear its router allowances: the emergency multisig
             batches the disable with sync_router_approvals in one transaction
             (scripts/emergency_cow_disable.py builds the bundle).
@custom:security The configured start total assumes every staged lot is worth no
                 more than that amount. Inventory accounting is balance-based:
                 available = min(initial_amount, native_remaining, balanceOf),
                 so tokens donated after the weekly snapshot can be resold along
                 the same curve — always at or above the curve price and always
                 in favor of FeeCollector; available never exceeds the snapshot.
                 Router approvals are infinite but only toward canonical routers
                 (CoW vault relayer, Permit2) while an enabled rail references
                 them. Tokens with transfer fees, rebases, callbacks, or
                 blacklist behavior are best-effort integrations.
"""


from ..interfaces import IDutchAuction
from ..auction import dutch_auction_math as auction_math
from ..auction import adapter_types
from ..auction import dutch_auction
from ..auction import adapters
from ..cow import gpv2
from ..cow import execution as cow_execution
from ..cow import watchtower as cow_watchtower

implements: IDutchAuction
initializes: dutch_auction
initializes: adapters
initializes: cow_execution
initializes: cow_watchtower
# Everything from the core surface except want(): its public getter returns
# the ERC20 interface type, which implements: cannot match against the
# address return in IDutchAuction, so the burner defines want() itself.
exports: (
    dutch_auction.current_epoch,
    dutch_auction.available,
    dutch_auction.price,
    dutch_auction.getAmountNeeded,
    dutch_auction.quote,
    dutch_auction.take,
    dutch_auction.take_with_limits,
    dutch_auction.start_total,
    dutch_auction.floor_total,
    dutch_auction.decay_factor_ray,
    dutch_auction.step_duration,
    dutch_auction.proceeds_receiver,
    dutch_auction.lots,
    dutch_auction.cancelled_epoch,
    dutch_auction.reconfigured_epoch,
)
exports: adapters.__interface__
exports: cow_execution.__interface__
exports: cow_watchtower.__interface__


# Shared authorization/lot errors are reused from the auction modules
# (adapters.OnlyOwner, dutch_auction.AmountExceedsLot); only
# burner-specific conditions are declared here.
error BadFeeCollector:
    pass


error BadTarget:
    pass


error BadExchangeFrame:
    pass


error CowValidityTooLong:
    pass


error TooManyPriceSteps:
    pass


error DecayMissesFloor:
    pass


error OnlyFeeCollector:
    pass


error TargetChanged:
    pass


error OldRelayerReferenced:
    pass


error BadQuoteTime:
    pass


error NotSignatureVerifierMuxer:
    pass


interface ERC20:
    def transfer(_receiver: address, _amount: uint256) -> bool: nonpayable
    def balanceOf(_owner: address) -> uint256: view


interface FeeCollector:
    def fee(_epoch: Epoch = ..., _timestamp: uint256 = ...) -> uint256: view
    def target() -> ERC20: view
    def owner() -> address: view
    def emergency_owner() -> address: view
    def epoch_time_frame(_epoch: Epoch, _timestamp: uint256 = ...) -> (uint256, uint256): view
    def can_exchange(_coins: DynArray[ERC20, MAX_COINS]) -> bool: view
    def transfer(_transfers: DynArray[Transfer, MAX_COINS]): nonpayable


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


MAX_COINS: constant(uint256) = 64
WAD: constant(uint256) = 10**18
WEEK: constant(uint256) = 7 * 24 * 60 * 60
MAX_PRICE_STEPS: constant(uint256) = 100_000
ETH_ADDRESS: constant(address) = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE

ERC165_INTERFACE_ID: constant(bytes4) = 0x01ffc9a7
BURNER_INTERFACE_ID: constant(bytes4) = 0xa3b5e311
ERC1271_INTERFACE_ID: constant(bytes4) = 0x1626ba7e
VERSION: public(constant(String[20])) = "DutchAuction"

# FeeCollector integration fixed at deployment. The payment token lives in the
# core as `want`, mirrors fee_collector.target() and follows it only through
# the owner's resync_target; target()/want() getters stay for existing tooling.
fee_collector: public(immutable(FeeCollector))
# In-week offset and span of the weekly EXCHANGE frame, pinned from the
# FeeCollector calendar (constants there): the burner's _epoch_bounds hook.
exchange_start_offset: immutable(uint256)
exchange_span: immutable(uint256)


@deploy
def __init__(
    _fee_collector: FeeCollector,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _cow_order_validity: uint256,
    _app_data: bytes32,
    _registry: address,
    _permit2: address,
):
    """
    @notice Configure immutable weekly-auction economics.
    @dev Curve parameters are validated by the core module; the calendar-
         dependent checks (CoW validity and decay fitting the EXCHANGE frame)
         stay here because only the burner knows the FeeCollector calendar.
    """
    assert _fee_collector.address != empty(address), BadFeeCollector()
    configured_target: ERC20 = staticcall _fee_collector.target()
    assert configured_target.address != empty(address), BadTarget()

    dutch_auction.__init__(
        dutch_auction.ERC20(configured_target.address),
        _fee_collector.address,
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
    )
    adapters.__init__(_registry, _permit2)
    cow_execution.__init__(_app_data, _cow_order_validity)

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = staticcall _fee_collector.epoch_time_frame(
        Epoch.EXCHANGE, block.timestamp
    )
    assert exchange_end > exchange_start, BadExchangeFrame()
    assert _cow_order_validity <= exchange_end - exchange_start, CowValidityTooLong()
    self._validate_curve_fits_frame(
        _start_total, _floor_total, _decay_factor_ray, _step_duration, exchange_start, exchange_end
    )

    self.fee_collector = _fee_collector
    # The FeeCollector calendar is weekly-periodic constants, so an epoch's
    # active window is pure arithmetic over offsets pinned here once.
    self.exchange_start_offset = exchange_start % WEEK
    self.exchange_span = exchange_end - exchange_start


@internal
@view
def _validate_curve_fits_frame(
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _exchange_start: uint256,
    _exchange_end: uint256,
):
    active_elapsed: uint256 = _exchange_end - _exchange_start - 1
    assert active_elapsed // _step_duration <= MAX_PRICE_STEPS, TooManyPriceSteps()
    assert auction_math.total_price(
        _start_total,
        _floor_total,
        _decay_factor_ray,
        active_elapsed,
        _step_duration,
    ) == _floor_total, DecayMissesFloor()


# Shared helpers


@external
@view
def want() -> address:
    """@notice Return the target token accepted as payment."""
    return dutch_auction.want.address


@external
@view
def target() -> address:
    """@notice FeeCollector-style alias of want() kept for existing tooling."""
    return dutch_auction.want.address


@external
@view
def epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    """
    @notice Active window of an auction epoch.
    @dev Lots store no time bounds: the calendar lives with this burner, and
         independent contracts (watchtower handler, resolver, keepers) read
         epoch windows from here.
    """
    return self._epoch_bounds(_epoch)


@internal
@view
def _target_is_current() -> bool:
    return (staticcall self.fee_collector.target()).address == dutch_auction.want.address


@internal
@view
def _exchange_frame(_timestamp: uint256) -> (uint256, uint256):
    return staticcall self.fee_collector.epoch_time_frame(Epoch.EXCHANGE, _timestamp)


@internal
@pure
def _mul_div_down_wad(_amount: uint256, _wad_fraction: uint256) -> uint256:
    # Splitting the amount preserves floor rounding without overflowing the product.
    return (_amount // WAD) * _wad_fraction + (_amount % WAD) * _wad_fraction // WAD


# Weekly staging


@external
def burn(_coins: DynArray[ERC20, MAX_COINS], _receiver: address):
    """
    @notice Pay the COLLECT incentive, take custody, and snapshot upcoming lots.
    @dev Staging also tops canonical router approvals up to infinity inside the
         core, so a freshly staged lot is immediately pullable by enabled rails.
    @param _coins Sorted tokens supplied by FeeCollector.
    @param _receiver Receiver of the FeeCollector COLLECT incentive.
    """
    assert msg.sender == self.fee_collector.address, OnlyFeeCollector()
    assert self._target_is_current(), TargetChanged()

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    epoch: uint256 = exchange_start // WEEK

    fee: uint256 = staticcall self.fee_collector.fee(Epoch.COLLECT, block.timestamp)
    fee_payouts: DynArray[Transfer, MAX_COINS] = []
    custody_transfers: DynArray[Transfer, MAX_COINS] = []

    for coin: ERC20 in _coins:
        # Fail before any transfer for target or cancelled-this-week tokens.
        dutch_auction._check_stageable(dutch_auction.ERC20(coin.address), epoch)
        collector_balance: uint256 = staticcall coin.balanceOf(self.fee_collector.address)
        fee_payouts.append(
            Transfer(
                coin=coin,
                to=_receiver,
                amount=self._mul_div_down_wad(collector_balance, fee),
            )
        )
        custody_transfers.append(Transfer(coin=coin, to=self, amount=max_value(uint256)))

    extcall self.fee_collector.transfer(fee_payouts)
    extcall self.fee_collector.transfer(custody_transfers)

    for coin: ERC20 in _coins:
        dutch_auction._stage_lot(dutch_auction.ERC20(coin.address), epoch)
        cow_watchtower._register_cow_order(coin.address)


# Auction core integration hooks


@override(dutch_auction)
@view
def _auction_epoch(_timestamp: uint256) -> uint256:
    # The cadence decision lives here, not in the core: this burner runs one
    # auction per FeeCollector week, numbering epochs by the EXCHANGE frame's
    # calendar week (always nonzero on any live chain).
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(_timestamp)
    return exchange_start // WEEK


@override(dutch_auction)
@view
def _epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    # Inverse of _auction_epoch for the weekly-periodic EXCHANGE frame: pure
    # arithmetic, no FeeCollector call on the quote and validation paths.
    start: uint256 = _epoch * WEEK + self.exchange_start_offset
    return start, start + self.exchange_span


@override(dutch_auction)
@view
def _sellable(_token: address) -> bool:
    # The core already excludes the want token; this adds the FeeCollector
    # target-migration and kill-mask checks.
    if not self._target_is_current():
        return False
    return staticcall self.fee_collector.can_exchange([ERC20(_token)])


@override(dutch_auction)
def _sync_stage_approvals(_token: address):
    adapters._ensure_router_approvals(adapters.ERC20(_token))


# Adapter layer integration hooks


@override(adapters)
@view
def _owner() -> address:
    return staticcall self.fee_collector.owner()


@override(adapters)
@view
def _emergency_owner() -> address:
    return staticcall self.fee_collector.emergency_owner()


@override(adapters)
@view
def _cow_router() -> address:
    return cow_execution.vault_relayer


@override(adapters)
@view
def _auction_want() -> address:
    return dutch_auction.want.address


@override(adapters)
@view
def _validate_embedded_signature(
    _hash: bytes32, _signature: Bytes[adapter_types.MAX_ENVELOPE_LEN]
) -> bytes4:
    return cow_execution._validate_cow_signature(_hash, _signature)


@override(adapters)
@view
def _check_order_against_lot(
    _order: adapter_types.NormalizedOrder,
    _adapter_id: bytes4,
    _adapter_version: uint16,
) -> bool:
    return dutch_auction._check_signed_order(_order, _adapter_id, _adapter_version)


# Economics resync


@external
def resync_target(
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
):
    """
    @notice Re-pin the payment token to the current fee_collector.target() with
            a curve retuned for it. Also serves as a same-target curve retune.
    @dev The new target is read from the FeeCollector, never passed in, so the
         owner cannot detach the payment denomination from the protocol. The
         full curve is revalidated against the EXCHANGE frame exactly like the
         constructor. On a target change the core fences out every lot of the
         current epoch (snapshots are in the old denomination) — fills resume
         with the next epoch's staging; a same-target retune keeps live lots
         untouched and only affects future stagings. Stale CoW registrations
         need no generation bump: published orders for fenced lots fail both
         the fence and the buy_token == want check, while the watchtower
         handler quotes future orders from live views.
    """
    assert msg.sender == staticcall self.fee_collector.owner(), adapters.OnlyOwner()
    new_target: ERC20 = staticcall self.fee_collector.target()
    assert new_target.address != empty(address), BadTarget()

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    self._validate_curve_fits_frame(
        _start_total, _floor_total, _decay_factor_ray, _step_duration, exchange_start, exchange_end
    )
    dutch_auction._resync_economics(
        dutch_auction.ERC20(new_target.address),
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
        self._auction_epoch(block.timestamp),
    )


# ComposableCoW lifecycle


@external
def configure_cow(_settlement: address, _composable_cow: address, _handler: address):
    """
    @notice Configure the CoW rails while disabled: the direct execution rail
            from the settlement (its domain separator and vault relayer are
            read on-chain) and the watchtower registration wiring.
    """
    assert msg.sender == staticcall self.fee_collector.owner(), adapters.OnlyOwner()
    old_relayer: address = cow_execution.vault_relayer
    cow_execution._configure_cow(_settlement)
    if cow_execution.vault_relayer != old_relayer:
        # Refcount-drift guard: adapters pinned to the old relayer must be
        # disabled first, or their references would keep a retired router
        # eligible for infinite approvals.
        assert adapters.router_refcount[old_relayer] == 0, OldRelayerReferenced()
    cow_watchtower._configure_watchtower(_composable_cow, _handler)


@external
def enable_cow():
    """@notice Enable direct settlement and order registration for the current generation."""
    assert msg.sender == staticcall self.fee_collector.owner(), adapters.OnlyOwner()
    cow_execution._enable_cow()
    adapters._retain_router(cow_execution.vault_relayer)


@external
def disable_cow():
    """@notice Emergency-disable both CoW rails without changing the generation."""
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], adapters.OnlyOwner()
    cow_execution._disable_cow()
    adapters._release_router(cow_execution.vault_relayer)


@external
@view
def created(_token: address) -> bool:
    """@notice Return whether token is registered for the current CoW generation."""
    generation: uint256 = cow_watchtower.cow_generation
    return generation != 0 and cow_watchtower.registered_generation[_token] == generation


# CoW execution integration hooks


@override(cow_execution)
@view
def _cow_target() -> address:
    return dutch_auction.want.address


@override(cow_execution)
@view
def _cow_receiver() -> address:
    return self.fee_collector.address


@external
@view
def cow_next_poll(_token: address) -> uint256:
    """@notice Next timestamp worth polling for a token's conditional order."""
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    if block.timestamp < exchange_start:
        return exchange_start
    exchange_start, exchange_end = self._exchange_frame(block.timestamp + WEEK)
    return exchange_start


@override(cow_execution)
@view
def _cow_order_context(_token: address) -> (bool, uint256, uint256, uint256, uint256):
    coin: dutch_auction.ERC20 = dutch_auction.ERC20(_token)
    lot: dutch_auction.Lot = dutch_auction.lots[coin]
    active: bool = dutch_auction._is_active(coin, lot, block.timestamp)
    available_amount: uint256 = 0
    if active:
        available_amount = dutch_auction._available_unchecked(coin, lot)
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = self._epoch_bounds(lot.epoch)
    return active, available_amount, lot.initial_amount, lot_start, lot_end


@override(cow_execution)
@view
def _cow_quote(_token: address, _sell_amount: uint256, _timestamp: uint256) -> uint256:
    coin: dutch_auction.ERC20 = dutch_auction.ERC20(_token)
    lot: dutch_auction.Lot = dutch_auction.lots[coin]
    lot_start: uint256 = 0
    lot_end: uint256 = 0
    lot_start, lot_end = self._epoch_bounds(lot.epoch)
    assert lot_start <= _timestamp and _timestamp < lot_end, BadQuoteTime()
    assert _sell_amount <= lot.initial_amount, dutch_auction.AmountExceedsLot()
    return dutch_auction._quote_unchecked(coin, _sell_amount, _timestamp)


@override(cow_execution)
@view
def _cow_signature_allowed() -> bool:
    # The contract-wide transient lock rejects validation during a native take.
    return True


@override(cow_watchtower)
@view
def _cow_rail_enabled() -> bool:
    return cow_execution.cow_enabled


# Recovery and interface discovery


@external
def push_target() -> uint256:
    """@notice Permissionlessly return target tokens held by this burner."""
    amount: uint256 = staticcall dutch_auction.want.balanceOf(self)
    if amount != 0:
        assert extcall dutch_auction.want.transfer(
            self.fee_collector.address, amount, default_return_value=True
        )
    return amount


@external
def recover(_coins: DynArray[ERC20, MAX_COINS]):
    """@notice Return ERC-20 or native balances only to FeeCollector."""
    assert msg.sender in [
        staticcall self.fee_collector.owner(),
        staticcall self.fee_collector.emergency_owner(),
    ], adapters.OnlyOwner()

    recovery_epoch: uint256 = self._auction_epoch(block.timestamp)
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
            dutch_auction._cancel_lot(dutch_auction.ERC20(coin.address), recovery_epoch)
            if amount != 0:
                assert extcall coin.transfer(
                    self.fee_collector.address, amount, default_return_value=True
                )
        log Recovered(token=coin, amount=amount)


@external
@view
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @notice Return burner interfaces. ERC-1271 is always claimed: the core
            dispatcher stays live for adapters even with CoW disabled. The
            conditional-order-generator interface now belongs to the external
            watchtower handler; the muxer probe must revert — ComposableCoW
            only falls back to the plain ERC-1271 encoding when this call
            reverts, while a successful False is InvalidFallbackHandler().
    """
    assert _interface_id != gpv2.SIGNATURE_VERIFIER_MUXER_INTERFACE, NotSignatureVerifierMuxer()
    return _interface_id in [ERC165_INTERFACE_ID, BURNER_INTERFACE_ID, ERC1271_INTERFACE_ID]
