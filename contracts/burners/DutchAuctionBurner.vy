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
        native takes and registry adapters reached by the shape-based ERC-1271
        signature router. CoW settles through the fallback CowAdapter; the
        burner itself only registers watchtower conditional orders.
@custom:kill FeeCollector kill masks stop all fills. The FeeCollector owner or
             emergency owner can disable individual adapters (the CoW rail
             included) and recover inventory — only back to FeeCollector.
             Native take and push_target stay permissionless while a lot is
             alive. Disabling an adapter does not clear its executor's
             allowances: the emergency multisig batches the disable with
             sync_executor_approvals in one transaction.
@custom:security The configured start total assumes every staged lot is worth no
                 more than that amount. Inventory accounting is balance-based:
                 available = min(initial_amount, balanceOf), so tokens donated
                 after the weekly snapshot can be resold along the same curve —
                 always at or above the curve price and always in favor of
                 FeeCollector; available never exceeds the snapshot.
                 Executor approvals are infinite but only toward executors of
                 owner-enabled registry adapters while a reference is held.
                 Signature validation is routed to registry verifiers; every
                 fill they admit is priced by the core's check_order view.
                 Tokens with transfer fees, rebases, callbacks, or blacklist
                 behavior are best-effort integrations.
"""


from ethereum.ercs import IERC20

from ..interfaces import IDutchAuction
from ..interfaces import IFeeCollector
from .auction import dutch_auction
from .auction.adapters import adapters
from ..utils import recovery
from ..utils import roles
from .cow import gpv2
from .cow import watchtower as cow_watchtower

implements: IDutchAuction
initializes: roles
initializes: dutch_auction
initializes: adapters[roles := roles]
initializes: cow_watchtower
exports: (
    dutch_auction.current_epoch,
    dutch_auction.want,
    dutch_auction.available,
    dutch_auction.price,
    dutch_auction.getAmountNeeded,
    dutch_auction.quote,
    dutch_auction.check_order,
    dutch_auction.take,
    dutch_auction.take_with_limits,
    dutch_auction.start_total,
    dutch_auction.floor_total,
    dutch_auction.decay_factor_ray,
    dutch_auction.step_duration,
    dutch_auction.proceeds_receiver,
    dutch_auction.lots,
    dutch_auction.reconfigured_epoch,
)
# Module surfaces are exported method-by-method on purpose: a new external
# function added to a module never enters the burner ABI unreviewed.
exports: (
    roles.role_source,
    roles.owner,
    roles.emergency_owner,
)
exports: (
    adapters.registry,
    adapters.enabled_adapters,
    adapters.fallback_adapter,
    adapters.executor_refcount,
    adapters.executors,
    adapters.enable_adapter,
    adapters.disable_adapter,
    adapters.set_fallback_adapter,
    adapters.sync_executor_approvals,
    adapters.isValidSignature,
)
exports: (
    cow_watchtower.composable_cow,
    cow_watchtower.cow_handler,
    cow_watchtower.cow_generation,
    cow_watchtower.registered_generation,
)


# Shared authorization/lot errors are reused from the auction modules
# (roles.OnlyOwner, dutch_auction.AmountExceedsLot); only
# burner-specific conditions are declared here.
error BadFeeCollector:
    pass


error BadTarget:
    pass


error BadExchangeFrame:
    pass


error OnlyFeeCollector:
    pass


error TargetChanged:
    pass


ERC165_INTERFACE_ID: constant(bytes4) = 0x01ffc9a7
BURNER_INTERFACE_ID: constant(bytes4) = 0xa3b5e311
ERC1271_INTERFACE_ID: constant(bytes4) = 0x1626ba7e
VERSION: public(constant(String[20])) = "DutchAuction"

# FeeCollector integration fixed at deployment. The payment token lives in the
# core as `want`, mirrors fee_collector.target() and follows it only through
# the owner's resync_target; target()/want() getters stay for existing tooling.
fee_collector: public(immutable(IFeeCollector.FeeCollector))


@deploy
def __init__(
    _fee_collector: IFeeCollector.FeeCollector,
    _start_total: uint256,
    _floor_total: uint256,
    _decay_factor_ray: uint256,
    _step_duration: uint256,
    _registry: address,
):
    """
    @notice Configure immutable weekly-auction economics.
    @dev Curve parameters are validated by the core module; the calendar-
         dependent check (decay fitting the EXCHANGE frame) stays here because
         only the burner knows the FeeCollector calendar.
    """
    assert _fee_collector.address != empty(address), BadFeeCollector()
    configured_target: address = staticcall _fee_collector.target()
    assert configured_target != empty(address), BadTarget()

    roles.__init__(roles.RoleSource(_fee_collector.address))
    dutch_auction.__init__(
        IERC20(configured_target),
        _fee_collector.address,
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
    )
    adapters.__init__(_registry)

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = staticcall _fee_collector.epoch_time_frame(
        IFeeCollector.Epoch.EXCHANGE, block.timestamp
    )
    assert exchange_end > exchange_start, BadExchangeFrame()
    dutch_auction._validate_curve_fits(
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
        exchange_end - exchange_start - 1,
    )

    self.fee_collector = _fee_collector


# Shared helpers


# Reentrant like the core's want(): only echoes configuration, and take()
# callbacks need the payment token while the contract-wide lock is held.
@external
@view
@reentrant
def target() -> address:
    """@notice FeeCollector-style alias of want() kept for existing tooling."""
    return dutch_auction.want_token.address


# Reentrant like want(): pure FeeCollector-calendar arithmetic over constants,
# reads no burner storage, and take() callbacks compute deadlines from it.
@external
@view
@reentrant
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
    return staticcall self.fee_collector.target() == dutch_auction.want_token.address


@internal
@view
def _exchange_frame(_timestamp: uint256) -> (uint256, uint256):
    return staticcall self.fee_collector.epoch_time_frame(
        IFeeCollector.Epoch.EXCHANGE, _timestamp
    )


# Weekly staging


@external
def burn(_coins: DynArray[IERC20, IFeeCollector.MAX_COINS], _receiver: address):
    """
    @notice Pay the COLLECT incentive, take custody, and snapshot upcoming lots.
    @dev Staging also tops enabled executors' approvals up to infinity inside
         the core, so a freshly staged lot is immediately pullable by enabled
         adapters. Restaging a leftover lot needs no fresh fees: a
         permissionless FeeCollector.collect during any later COLLECT frame
         re-snapshots the burner's full balance for the upcoming week from
         the top of the curve.
    @param _coins Sorted tokens supplied by FeeCollector.
    @param _receiver Receiver of the FeeCollector COLLECT incentive.
    """
    assert msg.sender == self.fee_collector.address, OnlyFeeCollector()
    assert self._target_is_current(), TargetChanged()

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    epoch: uint256 = exchange_start // IFeeCollector.WEEK

    fee: uint256 = staticcall self.fee_collector.fee(
        IFeeCollector.Epoch.COLLECT, block.timestamp
    )
    fee_payouts: DynArray[IFeeCollector.Transfer, IFeeCollector.MAX_COINS] = []
    custody_transfers: DynArray[IFeeCollector.Transfer, IFeeCollector.MAX_COINS] = []

    for coin: IERC20 in _coins:
        # Fail before any transfer when the target token is among the coins.
        dutch_auction._check_stageable(coin)
        collector_balance: uint256 = staticcall coin.balanceOf(self.fee_collector.address)
        fee_payouts.append(
            IFeeCollector.Transfer(
                coin=coin.address,
                to=_receiver,
                amount=collector_balance * fee // IFeeCollector.WAD,
            )
        )
        custody_transfers.append(
            IFeeCollector.Transfer(coin=coin.address, to=self, amount=max_value(uint256))
        )

    extcall self.fee_collector.transfer(fee_payouts)
    extcall self.fee_collector.transfer(custody_transfers)

    for coin: IERC20 in _coins:
        dutch_auction._stage_lot(coin, epoch)
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
    return exchange_start // IFeeCollector.WEEK


@override(dutch_auction)
@view
def _epoch_bounds(_epoch: uint256) -> (uint256, uint256):
    # Inverse of _auction_epoch: the FeeCollector week anchor is a multiple of
    # WEEK, so an epoch's week start lands inside that distribution week and
    # resolves to its EXCHANGE frame. One staticcall per lookup keeps the
    # calendar defined in exactly one place — the FeeCollector. Epoch 0 is the
    # never-staged sentinel and predates the FeeCollector calendar (whose frame
    # lookup would revert): the empty window keeps every check inactive.
    if _epoch == 0:
        return 0, 0
    return self._exchange_frame(_epoch * IFeeCollector.WEEK)


@override(dutch_auction)
@view
def _sellable(_token: address) -> bool:
    # The core already excludes the want token; this adds the FeeCollector
    # target-migration and kill-mask checks.
    return self._target_is_current() and staticcall self.fee_collector.can_exchange(
        [_token]
    )


@override(dutch_auction)
def _sync_stage_approvals(_token: address):
    adapters._ensure_executor_approvals(IERC20(_token))


# Adapter layer integration hooks


@override(adapters)
@view
def _auction_want() -> address:
    return dutch_auction.want_token.address


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
         constructor. On a target change the core fences out lots of the
         current epoch once its window has opened — fills resume with the next
         epoch's staging; a resync before the window opens fences only the
         previous epoch, so a restage trades the same week. A same-target
         retune keeps live lots untouched and reprices them immediately.
         Stale CoW registrations need no generation bump: published orders for
         fenced lots fail check_order's fence and buy-token terms, while the
         watchtower handler quotes future orders from live views.
    """
    roles._check_owner()
    new_target: address = staticcall self.fee_collector.target()
    assert new_target != empty(address), BadTarget()

    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    dutch_auction._validate_curve_fits(
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
        exchange_end - exchange_start - 1,
    )
    dutch_auction._resync_economics(
        IERC20(new_target),
        _start_total,
        _floor_total,
        _decay_factor_ray,
        _step_duration,
        self._auction_epoch(block.timestamp),
    )


# ComposableCoW watchtower lifecycle


@external
def configure_watchtower(_composable_cow: address, _handler: address):
    """
    @notice Configure the ComposableCoW registration wiring; settlement itself
            runs through the fallback CowAdapter enabled on the adapter layer.
    """
    roles._check_owner()
    cow_watchtower._configure_watchtower(_composable_cow, _handler)


@external
@view
def cow_enabled() -> bool:
    """@notice Whether the CoW rail (the fallback adapter route) is live."""
    return self._cow_rail_enabled()


@external
@view
def created(_token: address) -> bool:
    """@notice Return whether token is registered for the current CoW generation."""
    generation: uint256 = cow_watchtower.cow_generation
    return generation != 0 and cow_watchtower.registered_generation[_token] == generation


# Reentrant: only the FeeCollector calendar, no burner storage.
@external
@view
@reentrant
def cow_next_poll(_token: address) -> uint256:
    """@notice Next timestamp worth polling for a token's conditional order."""
    exchange_start: uint256 = 0
    exchange_end: uint256 = 0
    exchange_start, exchange_end = self._exchange_frame(block.timestamp)
    if block.timestamp < exchange_start:
        return exchange_start
    exchange_start, exchange_end = self._exchange_frame(
        block.timestamp + IFeeCollector.WEEK
    )
    return exchange_start


@override(cow_watchtower)
@view
def _cow_rail_enabled() -> bool:
    fallback: address = adapters.fallback_adapter
    return fallback != empty(address) and adapters.enabled_adapters[fallback]


# Recovery and interface discovery


@external
def push_target() -> uint256:
    """@notice Permissionlessly return target tokens held by this burner."""
    amount: uint256 = staticcall dutch_auction.want_token.balanceOf(self)
    if amount != 0:
        assert extcall dutch_auction.want_token.transfer(
            self.fee_collector.address, amount, default_return_value=True
        )
    return amount


@external
def recover(_coins: DynArray[IERC20, IFeeCollector.MAX_COINS]):
    """
    @notice Return ERC-20 or native balances only to FeeCollector.
    @dev Emptying the balance kills the lot through the balance term of
         available. During the same week's COLLECT frame a permissionless
         collect can pull the token back and restage it, so an emergency
         evacuation batches recover with FeeCollector.set_killed.
    """
    roles._check_owner_or_emergency()

    for coin: IERC20 in _coins:
        recovery._recover_coin(coin, self.fee_collector.address)


# Reentrant: answers from constants only.
@external
@view
@reentrant
def supportsInterface(_interface_id: bytes4) -> bool:
    """
    @notice Return burner interfaces. ERC-1271 is always claimed: the signature
            router stays live for adapters. The conditional-order-generator
            interface belongs to the external watchtower handler; the muxer
            probe must revert (see gpv2._reject_muxer_probe).
    """
    gpv2._reject_muxer_probe(_interface_id)
    return _interface_id in [ERC165_INTERFACE_ID, BURNER_INTERFACE_ID, ERC1271_INTERFACE_ID]
